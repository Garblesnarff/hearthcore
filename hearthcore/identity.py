"""Phase II Persistent Persona Identity & Multi-Character Mapping (Card II-01 / Issue #25).

Design guarantees (docs/PHASE_II_LIVING_GUILD.md Section 4, Issue #25):
- Immutable opaque `persona_id` (`per_<16-hex>`) decoupled from character slug/name so character
  renames NEVER change persona identity. Additive non-destructive backfill preserves any legacy
  `per_<slug>` rows in existing databases without destructive rekeying.
- Verified `(realm_id, character_guid, incarnation)` character key mapping:
  - Never binds an unmapped character key to an existing persona by slug/name.
  - Resolves omitted `incarnation` to the active incarnation for `(realm_id, guid)` (never silently
    reactivating or reviving a retired incarnation!).
  - Supports explicit `reactivate_incarnation` (true restore) and `bind_new_incarnation` (monotonic
    recreate under the same persona).
- Preserves display names & full ordered rename history in `persona_aliases` (`first_seen_at`, `last_seen_at`).
- Tracks verified `faction` (`faction_verified`), `owner_group`, explicit `enrolled_coop` with durable
  `coop_override_at` (surviving subsequent sync ticks!), and sticky `core_protected` latch so core
  companions (`core-wonky`, `core-logan`, `core-*`) stay protected EVEN AFTER schedule/shift changes.
- Uses structured JSON identity fingerprints (comparing overlapping immutable keys only, merging new
  fields cleanly) and deduplicated `persona_conflicts` with `resolve_conflict()` recovery.
- Refuses silent alt reassignment across personas (insert-only `register_alt`) and enforces unique
  active `(realm_id, slug)` across both mains and alts.
- Enforces AT MOST ONE active online session per `persona_id` via SQLite partial unique index
  `idx_pc_one_active_session` and transactional session switching, exposed in `read_only_snapshot`
  for admission/shadow/readiness enforcement.
- Strictly read-only `read_only_snapshot` (accepting optional shared read connection) that NEVER
  creates or mutates `brain.db`; unresolved or conflicted mappings exclude admission.
- No account creation or alt creation in Phase II.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dungeons import faction_from_race

logger = logging.getLogger("hearthcore.identity")

PROTECTED_OWNER_GROUPS = frozenset({"core-wonky", "core-logan"})
SCHEMA_USER_VERSION = 2


def is_protected_owner_group(owner_group: str | None) -> bool:
    """True for explicit protected owner groups (`core-wonky`, `core-logan`, or any `core-<name>` group)."""
    og = str(owner_group or "").strip().lower()
    return og in PROTECTED_OWNER_GROUPS or (og.startswith("core-") and len(og) > 5)


def generate_opaque_persona_id() -> str:
    """Mint a new immutable opaque persona ID completely decoupled from character slug/name."""
    return f"per_{uuid.uuid4().hex[:16]}"


def persona_id_for_slug(slug: str) -> str:
    """Legacy deterministic slug-derived persona ID helper (retained for tests & legacy rows only)."""
    return f"per_{slug.strip().lower()}"


def _parse_bool_strict(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes")
    return False


def _extract_fingerprint_dict(comp: dict[str, Any]) -> dict[str, int]:
    """Extract immutable character attributes as a structured dict (never trusts raw precomputed strings!)."""
    out: dict[str, int] = {}
    for key in ("account_id", "race_id", "class_id", "created_at"):
        val = comp.get(key)
        if val is not None and not isinstance(val, bool):
            try:
                iv = int(val)
                if iv > 0:
                    out[key] = iv
            except (TypeError, ValueError) as exc:
                logger.debug("Ignoring non-integer fingerprint field %s=%r: %s", key, val, exc)
    return out


def _parse_stored_fingerprint(raw: str) -> dict[str, int]:
    s = str(raw or "").strip()
    if not s:
        return {}
    if s.startswith("{"):
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                return {str(k): int(v) for k, v in d.items() if v is not None}
        except (ValueError, TypeError):
            return {}
    return {}


def _dump_fingerprint(fp: dict[str, int]) -> str:
    if not fp:
        return ""
    return json.dumps(fp, sort_keys=True, separators=(",", ":"))


def _compare_fingerprints(old_fp: dict[str, int], new_fp: dict[str, int]) -> tuple[str, dict[str, int]]:
    """Compare overlapping structured fingerprint fields; merge additive fields cleanly.

    Returns `(status, merged_fp)` where `status` is:
    - `'match'`: all overlapping fields agree (with at least 1 immutable key overlapping)
    - `'compatible_empty'`: no overlapping fields yet (additive enrichment only)
    - `'race_changed'`: only `race_id` changed (reviewable, not hard GUID reuse)
    - `'hard_mismatch'`: `account_id`, `class_id`, or `created_at` diverged (GUID reuse / recreate!)
    """
    overlap = set(old_fp.keys()) & set(new_fp.keys())
    hard_keys = {"account_id", "class_id", "created_at"}
    for k in sorted(overlap & hard_keys):
        if old_fp[k] != new_fp[k]:
            return "hard_mismatch", old_fp
    if "race_id" in overlap and old_fp["race_id"] != new_fp["race_id"]:
        return "race_changed", old_fp
    merged = dict(old_fp)
    merged.update(new_fp)
    if overlap:
        return "match", merged
    return "compatible_empty", merged


def _resolve_faction(comp: dict[str, Any]) -> tuple[str, bool]:
    raw = str(comp.get("faction") or "").strip().lower()
    if raw in ("alliance", "horde"):
        return raw, True
    if comp.get("race_id") is not None:
        fac = faction_from_race(comp.get("race_id"))
        if fac:
            return fac, True
    return "unknown", False


@dataclass(frozen=True)
class CharacterKey:
    realm_id: int
    guid: int
    incarnation: int = 1

    def token(self) -> str:
        return f"r{self.realm_id}:g{self.guid}:i{self.incarnation}"


@dataclass(frozen=True)
class PersonaRecord:
    persona_id: str
    slug: str
    faction: str
    owner_group: str
    enrolled_coop: bool
    state: str = "active"
    core_protected: bool = False
    faction_verified: bool = False
    quarantine_reason: str = ""
    aliases: tuple[str, ...] = ()


class PersonaRegistry:
    """SQLite persistent store for immutable Persona <-> (realm_id, guid, incarnation) identity mapping."""

    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._conn()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            # P1-4: Atomic transactional DDL + additive migration under BEGIN IMMEDIATE!
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS personas (
                    persona_id TEXT PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE,
                    faction TEXT NOT NULL DEFAULT 'unknown',
                    faction_verified INTEGER NOT NULL DEFAULT 0,
                    owner_group TEXT NOT NULL DEFAULT 'guild',
                    core_protected INTEGER NOT NULL DEFAULT 0,
                    enrolled_coop INTEGER NOT NULL DEFAULT 0,
                    coop_override_at INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'active',
                    quarantine_reason TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS persona_characters (
                    realm_id INTEGER NOT NULL DEFAULT 1,
                    character_guid INTEGER NOT NULL,
                    incarnation INTEGER NOT NULL DEFAULT 1,
                    persona_id TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    fingerprint TEXT NOT NULL DEFAULT '',
                    slot_kind TEXT NOT NULL DEFAULT 'main',
                    active INTEGER NOT NULL DEFAULT 1,
                    session_active INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (realm_id, character_guid, incarnation)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS persona_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    persona_id TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    realm_id INTEGER NOT NULL,
                    character_guid INTEGER NOT NULL,
                    incarnation INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    UNIQUE (persona_id, alias, realm_id, character_guid, incarnation)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS persona_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    realm_id INTEGER NOT NULL,
                    character_guid INTEGER NOT NULL,
                    incarnation INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    existing_persona_id TEXT NOT NULL,
                    incoming_slug TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    seen_count INTEGER NOT NULL DEFAULT 1,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    resolved_at INTEGER NOT NULL DEFAULT 0,
                    resolution_note TEXT NOT NULL DEFAULT '',
                    UNIQUE (realm_id, character_guid, incarnation, kind, incoming_slug)
                )
            """)
            self._migrate_additive_columns(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pc_persona ON persona_characters(persona_id, active)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pa_alias ON persona_aliases(alias)")
            # Enforce unique active slug per realm across BOTH mains and alts (P2-6)!
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_pc_realm_slug_active "
                "ON persona_characters(realm_id, slug) WHERE active = 1"
            )
            # Enforce AT MOST ONE active online session per persona at the database layer!
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_pc_one_active_session "
                "ON persona_characters(persona_id) WHERE session_active = 1"
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_USER_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _migrate_additive_columns(db: sqlite3.Connection) -> None:
        """Atomic additive migration for existing brain.db files (never destructively rekeys IDs or overwrites explicit overrides!)."""
        p_cols = {r[1] for r in db.execute("PRAGMA table_info(personas)").fetchall()}
        if "core_protected" not in p_cols:
            db.execute("ALTER TABLE personas ADD COLUMN core_protected INTEGER NOT NULL DEFAULT 0")
        if "quarantine_reason" not in p_cols:
            db.execute("ALTER TABLE personas ADD COLUMN quarantine_reason TEXT NOT NULL DEFAULT ''")
        if "coop_override_at" not in p_cols:
            db.execute("ALTER TABLE personas ADD COLUMN coop_override_at INTEGER NOT NULL DEFAULT 0")
        if "faction_verified" not in p_cols:
            db.execute("ALTER TABLE personas ADD COLUMN faction_verified INTEGER NOT NULL DEFAULT 0")

        # Backfill sticky core_protected latch ONLY (leave explicit legacy enrolled_coop intact and log!)
        legacy_enrolled_core = db.execute(
            "SELECT persona_id, owner_group FROM personas "
            "WHERE core_protected = 0 AND enrolled_coop = 1 "
            "AND (LOWER(owner_group) IN ('core-wonky', 'core-logan') "
            "OR (LOWER(owner_group) LIKE 'core-%' AND LENGTH(owner_group) > 5))"
        ).fetchall()
        for row in legacy_enrolled_core:
            logger.warning(
                "Backfilling core_protected=1 on legacy enrolled core persona %s (%s); preserving explicit enrolled_coop",
                row["persona_id"], row["owner_group"],
            )
            db.execute(
                "UPDATE personas SET core_protected = 1, coop_override_at = CASE WHEN coop_override_at = 0 THEN updated_at ELSE coop_override_at END "
                "WHERE persona_id = ?",
                (row["persona_id"],),
            )
        db.execute(
            "UPDATE personas SET core_protected = 1 "
            "WHERE core_protected = 0 "
            "AND (LOWER(owner_group) IN ('core-wonky', 'core-logan') "
            "OR (LOWER(owner_group) LIKE 'core-%' AND LENGTH(owner_group) > 5))"
        )

        pc_cols = {r[1] for r in db.execute("PRAGMA table_info(persona_characters)").fetchall()}
        if "fingerprint" not in pc_cols:
            db.execute("ALTER TABLE persona_characters ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''")
        if "session_active" not in pc_cols:
            db.execute("ALTER TABLE persona_characters ADD COLUMN session_active INTEGER NOT NULL DEFAULT 0")

        pa_cols = {r[1] for r in db.execute("PRAGMA table_info(persona_aliases)").fetchall()}
        if "first_seen_at" not in pa_cols and "recorded_at" in pa_cols:
            db.execute("ALTER TABLE persona_aliases ADD COLUMN first_seen_at INTEGER NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE persona_aliases ADD COLUMN last_seen_at INTEGER NOT NULL DEFAULT 0")
            db.execute("UPDATE persona_aliases SET first_seen_at = recorded_at, last_seen_at = recorded_at")

        # Additive alias backfill for any pre-Issue-#25 persona_characters rows
        db.execute("""
            INSERT OR IGNORE INTO persona_aliases
                (persona_id, alias, realm_id, character_guid, incarnation, active, first_seen_at, last_seen_at)
            SELECT persona_id, LOWER(slug), realm_id, character_guid, incarnation, active, updated_at, updated_at
            FROM persona_characters
        """)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _record_alias(
        self,
        db: sqlite3.Connection,
        *,
        persona_id: str,
        alias: str,
        realm_id: int,
        character_guid: int,
        incarnation: int,
        active: bool,
        ts: int,
    ) -> None:
        al = alias.strip().lower()
        if not al:
            return
        db.execute(
            """
            INSERT INTO persona_aliases
                (persona_id, alias, realm_id, character_guid, incarnation, active, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(persona_id, alias, realm_id, character_guid, incarnation) DO UPDATE SET
                active = excluded.active,
                last_seen_at = excluded.last_seen_at
            """,
            (persona_id, al, int(realm_id), int(character_guid), int(incarnation), 1 if active else 0, ts, ts),
        )

    def _record_conflict(
        self,
        db: sqlite3.Connection,
        *,
        realm_id: int,
        character_guid: int,
        incarnation: int,
        kind: str,
        existing_persona_id: str,
        incoming_slug: str,
        reason: str,
        ts: int,
    ) -> None:
        """Deduplicated conflict upsert (P2-3: prevents unbounded table growth across sync ticks)."""
        db.execute(
            """
            INSERT INTO persona_conflicts
                (realm_id, character_guid, incarnation, kind, existing_persona_id, incoming_slug,
                 reason, seen_count, first_seen_at, last_seen_at, resolved_at, resolution_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, '')
            ON CONFLICT(realm_id, character_guid, incarnation, kind, incoming_slug) DO UPDATE SET
                existing_persona_id = excluded.existing_persona_id,
                reason = excluded.reason,
                seen_count = persona_conflicts.seen_count + 1,
                last_seen_at = excluded.last_seen_at,
                resolved_at = 0
            """,
            (
                int(realm_id), int(character_guid), int(incarnation), kind[:48],
                existing_persona_id, incoming_slug.strip().lower()[:64], reason[:200], ts, ts,
            ),
        )

    def sync_from_companions(
        self,
        companions: dict[str, dict[str, Any]],
        *,
        realm_id: int | None = 1,
        now_ts: int | None = None,
        strict_realm: bool = False,
    ) -> dict[str, Any]:
        """Idempotently sync authoritative companions without destructive rekeying or name-based binding.

        Guarantees (Issue #25):
        - Never infers a binding from slug/name on unmapped keys (quarantines same-realm slug collisions;
          creates a distinct persona on another realm!).
        - Resolves omitted `incarnation` to the current ACTIVE incarnation for `(realm_id, guid)` (never
          matches or revives a retired incarnation!).
        - Per-entry `SAVEPOINT` isolation so one bad companion entry never aborts the entire sync batch.
        - Preserves owner `coop_override_at` across re-syncs while keeping `core_protected` latched.
        - Only updates persona-level `owner_group`/`faction` from `slot_kind == 'main'` characters.
        - Enforces AT MOST ONE `session_active = 1` per persona and records open `dual_active_session` conflicts.
        """
        ts = int(now_ts if now_ts is not None else time.time())
        synced = 0
        renamed: list[str] = []
        quarantined: list[str] = []
        session_conflicts: list[str] = []

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            online_seen_by_persona: dict[str, tuple[int, int, int]] = {}

            for idx, (slug_key, comp) in enumerate(sorted(companions.items())):
                sp_name = f"sp_sync_{idx}"
                conn.execute(f"SAVEPOINT {sp_name}")
                try:
                    if not isinstance(comp, dict):
                        quarantined.append(f"invalid_companion_entry:{slug_key}")
                        conn.execute(f"RELEASE {sp_name}")
                        continue
                    slug = str(comp.get("slug") or slug_key).strip().lower()
                    if not slug or comp.get("guid") is None or isinstance(comp.get("guid"), bool):
                        quarantined.append(f"invalid_companion_entry:{slug_key}")
                        conn.execute(f"RELEASE {sp_name}")
                        continue
                    guid = int(comp["guid"])
                    if guid <= 0:
                        quarantined.append(f"invalid_guid:{guid}:{slug}")
                        conn.execute(f"RELEASE {sp_name}")
                        continue

                    if comp.get("realm_id") is not None:
                        eff_realm = int(comp["realm_id"])
                    elif strict_realm or realm_id is None:
                        quarantined.append(f"missing_realm_id:{slug}:guid_{guid}")
                        conn.execute(f"RELEASE {sp_name}")
                        continue
                    else:
                        eff_realm = int(realm_id)
                    if eff_realm <= 0:
                        quarantined.append(f"invalid_realm_id:{eff_realm}:{slug}")
                        conn.execute(f"RELEASE {sp_name}")
                        continue

                    # P0-2: Resolve incarnation from active DB row when omitted (NEVER assume 1 on a retired key!)
                    inc_raw = comp.get("incarnation")
                    if inc_raw is not None:
                        eff_inc = int(inc_raw)
                        if eff_inc <= 0:
                            quarantined.append(f"invalid_incarnation:{eff_inc}:{slug}")
                            conn.execute(f"RELEASE {sp_name}")
                            continue
                    else:
                        active_inc_row = conn.execute(
                            "SELECT MAX(incarnation) AS mx FROM persona_characters "
                            "WHERE realm_id = ? AND character_guid = ? AND active = 1",
                            (eff_realm, guid),
                        ).fetchone()
                        if active_inc_row and active_inc_row["mx"] is not None:
                            eff_inc = int(active_inc_row["mx"])
                        else:
                            any_inc_row = conn.execute(
                                "SELECT MAX(incarnation) AS mx, persona_id FROM persona_characters "
                                "WHERE realm_id = ? AND character_guid = ?",
                                (eff_realm, guid),
                            ).fetchone()
                            if any_inc_row and any_inc_row["mx"] is not None:
                                msg = f"retired_incarnation_seen:r{eff_realm}:g{guid}:last_i{any_inc_row['mx']}"
                                quarantined.append(msg)
                                self._record_conflict(
                                    conn, realm_id=eff_realm, character_guid=guid,
                                    incarnation=int(any_inc_row["mx"]), kind="retired_incarnation_seen",
                                    existing_persona_id=str(any_inc_row["persona_id"]),
                                    incoming_slug=slug, reason=msg, ts=ts,
                                )
                                conn.execute(f"RELEASE {sp_name}")
                                continue
                            eff_inc = 1

                    # Prefer explicit owner_group over schedule shift_group (P2-1)
                    ogroup = str(comp.get("owner_group") or comp.get("shift_group") or "guild").strip().lower() or "guild"
                    protected_now = is_protected_owner_group(ogroup) or is_protected_owner_group(comp.get("shift_group"))
                    faction, fac_verified = _resolve_faction(comp)
                    in_fp = _extract_fingerprint_dict(comp)
                    is_online = _parse_bool_strict(comp.get("online"))

                    # 1. Look up existing row for (eff_realm, guid, eff_inc)
                    existing_char = conn.execute(
                        """
                        SELECT pc.persona_id, pc.slug, pc.fingerprint, pc.slot_kind, pc.active,
                               p.state, p.core_protected, p.coop_override_at
                        FROM persona_characters pc
                        JOIN personas p ON p.persona_id = pc.persona_id
                        WHERE pc.realm_id = ? AND pc.character_guid = ? AND pc.incarnation = ?
                        """,
                        (eff_realm, guid, eff_inc),
                    ).fetchone()

                    if existing_char:
                        pid = str(existing_char["persona_id"])
                        # P0-2: Refuse to match or revive a retired/inactive incarnation!
                        if int(existing_char["active"]) != 1:
                            msg = f"retired_incarnation_seen:r{eff_realm}:g{guid}:i{eff_inc}"
                            quarantined.append(msg)
                            self._record_conflict(
                                conn, realm_id=eff_realm, character_guid=guid, incarnation=eff_inc,
                                kind="retired_incarnation_seen", existing_persona_id=pid,
                                incoming_slug=slug, reason=msg, ts=ts,
                            )
                            conn.execute(f"RELEASE {sp_name}")
                            continue

                        old_slug = str(existing_char["slug"]).strip().lower()
                        old_fp = _parse_stored_fingerprint(existing_char["fingerprint"])
                        slot_kind = str(existing_char["slot_kind"] or "main")

                        fp_status, merged_fp = _compare_fingerprints(old_fp, in_fp)
                        if fp_status == "hard_mismatch":
                            msg = f"fingerprint_mismatch:r{eff_realm}:g{guid}:i{eff_inc}:{_dump_fingerprint(old_fp)}!={_dump_fingerprint(in_fp)}"
                            quarantined.append(msg)
                            self._record_conflict(
                                conn, realm_id=eff_realm, character_guid=guid, incarnation=eff_inc,
                                kind="fingerprint_mismatch", existing_persona_id=pid,
                                incoming_slug=slug, reason=msg, ts=ts,
                            )
                            conn.execute(f"RELEASE {sp_name}")
                            continue
                        if fp_status == "race_changed":
                            msg = f"race_change_review:r{eff_realm}:g{guid}:i{eff_inc}:{old_fp.get('race_id')}->{in_fp.get('race_id')}"
                            quarantined.append(msg)
                            self._record_conflict(
                                conn, realm_id=eff_realm, character_guid=guid, incarnation=eff_inc,
                                kind="race_change_review", existing_persona_id=pid,
                                incoming_slug=slug, reason=msg, ts=ts,
                            )
                            conn.execute(f"RELEASE {sp_name}")
                            continue

                        if slug != old_slug:
                            # Check active slug collision across BOTH mains and alts on this realm (P2-6)
                            slug_holder = conn.execute(
                                "SELECT persona_id FROM persona_characters "
                                "WHERE realm_id = ? AND slug = ? AND active = 1",
                                (eff_realm, slug),
                            ).fetchone()
                            if slug_holder and str(slug_holder["persona_id"]) != pid:
                                msg = f"guid {guid} ({pid}/{old_slug}) rename collision with active slug {slug} owned by {slug_holder['persona_id']}"
                                quarantined.append(msg)
                                self._record_conflict(
                                    conn, realm_id=eff_realm, character_guid=guid, incarnation=eff_inc,
                                    kind="rename_slug_collision", existing_persona_id=pid,
                                    incoming_slug=slug, reason=msg, ts=ts,
                                )
                                conn.execute(f"RELEASE {sp_name}")
                                continue
                            # Require verified matching structured fingerprint to auto-rename in sync!
                            if fp_status != "match":
                                msg = f"guid {guid} mapped to {pid} ({old_slug}) != {slug} (unverified rename)"
                                quarantined.append(msg)
                                self._record_conflict(
                                    conn, realm_id=eff_realm, character_guid=guid, incarnation=eff_inc,
                                    kind="unverified_rename", existing_persona_id=pid,
                                    incoming_slug=slug, reason=msg, ts=ts,
                                )
                                conn.execute(f"RELEASE {sp_name}")
                                continue

                            self._record_alias(
                                conn, persona_id=pid, alias=old_slug, realm_id=eff_realm,
                                character_guid=guid, incarnation=eff_inc, active=False, ts=ts,
                            )
                            self._record_alias(
                                conn, persona_id=pid, alias=slug, realm_id=eff_realm,
                                character_guid=guid, incarnation=eff_inc, active=True, ts=ts,
                            )
                            conn.execute(
                                "UPDATE persona_characters SET slug=?, updated_at=? "
                                "WHERE realm_id=? AND character_guid=? AND incarnation=?",
                                (slug, ts, eff_realm, guid, eff_inc),
                            )
                            if slot_kind == "main":
                                # Avoid cross-realm personas.slug UNIQUE collision
                                p_slug_taken = conn.execute(
                                    "SELECT persona_id FROM personas WHERE slug = ? AND persona_id != ?",
                                    (slug, pid),
                                ).fetchone()
                                new_p_slug = f"{slug}@r{eff_realm}" if p_slug_taken else slug
                                conn.execute(
                                    "UPDATE personas SET slug=?, updated_at=? WHERE persona_id=?",
                                    (new_p_slug, ts, pid),
                                )
                            renamed.append(f"{pid}:{old_slug}->{slug}")

                        # Persist merged additive fingerprint if enriched
                        merged_fp_str = _dump_fingerprint(merged_fp)
                        if merged_fp_str != str(existing_char["fingerprint"] or ""):
                            conn.execute(
                                "UPDATE persona_characters SET fingerprint=?, updated_at=? "
                                "WHERE realm_id=? AND character_guid=? AND incarnation=?",
                                (merged_fp_str, ts, eff_realm, guid, eff_inc),
                            )

                        # Update persona-level fields ONLY from main character (P2-1) & preserve owner override (P1-1)!
                        if slot_kind == "main":
                            conn.execute(
                                """
                                UPDATE personas
                                SET owner_group = ?,
                                    faction = CASE WHEN ? = 1 THEN ? ELSE faction END,
                                    faction_verified = CASE WHEN ? = 1 THEN 1 ELSE faction_verified END,
                                    core_protected = CASE WHEN core_protected = 1 OR ? = 1 THEN 1 ELSE 0 END,
                                    enrolled_coop = CASE
                                        WHEN (core_protected = 0 AND ? = 1) THEN 0
                                        WHEN (core_protected = 1 AND coop_override_at = 0) THEN 0
                                        ELSE enrolled_coop
                                    END,
                                    updated_at = ?
                                WHERE persona_id = ?
                                """,
                                (
                                    ogroup,
                                    1 if fac_verified else 0, faction,
                                    1 if fac_verified else 0,
                                    1 if protected_now else 0,
                                    1 if protected_now else 0,
                                    ts, pid,
                                ),
                            )
                        elif protected_now:
                            # Even an alt entering a core-* group latches core_protected safely!
                            conn.execute(
                                """
                                UPDATE personas
                                SET core_protected = 1,
                                    enrolled_coop = CASE WHEN coop_override_at = 0 THEN 0 ELSE enrolled_coop END,
                                    updated_at = ?
                                WHERE persona_id = ?
                                """,
                                (ts, pid),
                            )
                    else:
                        # 2. Unmapped (eff_realm, guid, eff_inc)!
                        # P0-1: Check if an active incarnation ALREADY exists for (eff_realm, guid) -> quarantine unexpected bump!
                        existing_active_guid = conn.execute(
                            "SELECT persona_id, incarnation FROM persona_characters "
                            "WHERE realm_id = ? AND character_guid = ? AND active = 1",
                            (eff_realm, guid),
                        ).fetchone()
                        if existing_active_guid:
                            msg = (
                                f"unexpected_incarnation_bump:r{eff_realm}:g{guid}:active_i{existing_active_guid['incarnation']}!=in_i{eff_inc}"
                            )
                            quarantined.append(msg)
                            self._record_conflict(
                                conn, realm_id=eff_realm, character_guid=guid, incarnation=eff_inc,
                                kind="unexpected_incarnation_bump", existing_persona_id=str(existing_active_guid["persona_id"]),
                                incoming_slug=slug, reason=msg, ts=ts,
                            )
                            conn.execute(f"RELEASE {sp_name}")
                            continue

                        # P0-1: NEVER bind an unmapped key to an existing persona by slug on the same realm!
                        same_realm_slug = conn.execute(
                            "SELECT persona_id, character_guid FROM persona_characters "
                            "WHERE realm_id = ? AND slug = ? ORDER BY active DESC, incarnation DESC LIMIT 1",
                            (eff_realm, slug),
                        ).fetchone()
                        if same_realm_slug:
                            msg = (
                                f"unmapped_key_slug_collision:r{eff_realm}:g{guid}:slug_{slug} "
                                f"already associated with {same_realm_slug['persona_id']} (guid {same_realm_slug['character_guid']})"
                            )
                            quarantined.append(msg)
                            self._record_conflict(
                                conn, realm_id=eff_realm, character_guid=guid, incarnation=eff_inc,
                                kind="unmapped_key_slug_collision", existing_persona_id=str(same_realm_slug["persona_id"]),
                                incoming_slug=slug, reason=msg, ts=ts,
                            )
                            conn.execute(f"RELEASE {sp_name}")
                            continue

                        # Also check if a persona with this slug exists with NO character links yet -> quarantine rather than name-bind!
                        orphan_persona = conn.execute(
                            "SELECT p.persona_id FROM personas p "
                            "WHERE p.slug = ? AND NOT EXISTS (SELECT 1 FROM persona_characters pc WHERE pc.persona_id = p.persona_id)",
                            (slug,),
                        ).fetchone()
                        if orphan_persona:
                            msg = f"unmapped_key_slug_collision:r{eff_realm}:g{guid}:orphan_persona_{orphan_persona['persona_id']}"
                            quarantined.append(msg)
                            self._record_conflict(
                                conn, realm_id=eff_realm, character_guid=guid, incarnation=eff_inc,
                                kind="unmapped_key_slug_collision", existing_persona_id=str(orphan_persona["persona_id"]),
                                incoming_slug=slug, reason=msg, ts=ts,
                            )
                            conn.execute(f"RELEASE {sp_name}")
                            continue

                        # Brand new persona -> ALWAYS mint an IMMUTABLE OPAQUE persona_id (P1-6: never trust comp['persona_id']!)
                        pid = generate_opaque_persona_id()
                        # Cross-realm same-name handling: if another realm already used `slug` in `personas.slug`,
                        # store `slug@r{realm}` in `personas.slug` so realm 2 gets its OWN completely separate persona!
                        cross_realm_slug = conn.execute("SELECT 1 FROM personas WHERE slug = ?", (slug,)).fetchone()
                        persona_slug = f"{slug}@r{eff_realm}" if cross_realm_slug else slug
                        default_coop = 0 if protected_now else (
                            1 if comp.get("enrolled_coop") is None else (1 if _parse_bool_strict(comp["enrolled_coop"]) else 0)
                        )
                        conn.execute(
                            """
                            INSERT INTO personas
                                (persona_id, slug, faction, faction_verified, owner_group, core_protected,
                                 enrolled_coop, coop_override_at, state, quarantine_reason, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'active', '', ?)
                            """,
                            (
                                pid, persona_slug, faction, 1 if fac_verified else 0,
                                ogroup, 1 if protected_now else 0, default_coop, ts,
                            ),
                        )
                        conn.execute(
                            """
                            INSERT INTO persona_characters
                                (realm_id, character_guid, incarnation, persona_id, slug, fingerprint,
                                 slot_kind, active, session_active, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, 'main', 1, 0, ?)
                            """,
                            (eff_realm, guid, eff_inc, pid, slug, _dump_fingerprint(in_fp), ts),
                        )
                        self._record_alias(
                            conn, persona_id=pid, alias=slug, realm_id=eff_realm,
                            character_guid=guid, incarnation=eff_inc, active=True, ts=ts,
                        )

                    # 3. Single-active-session enforcement per persona!
                    if is_online:
                        if pid in online_seen_by_persona:
                            prev_key = online_seen_by_persona[pid]
                            msg = (
                                f"dual_active_session:{pid}:r{prev_key[0]}:g{prev_key[1]}:i{prev_key[2]} "
                                f"and r{eff_realm}:g{guid}:i{eff_inc}"
                            )
                            session_conflicts.append(msg)
                            self._record_conflict(
                                conn, realm_id=eff_realm, character_guid=guid, incarnation=eff_inc,
                                kind="dual_active_session", existing_persona_id=pid,
                                incoming_slug=slug, reason=msg, ts=ts,
                            )
                        else:
                            online_seen_by_persona[pid] = (eff_realm, guid, eff_inc)
                            conn.execute(
                                "UPDATE persona_characters SET session_active = 0 WHERE persona_id = ? AND session_active = 1",
                                (pid,),
                            )
                            conn.execute(
                                "UPDATE persona_characters SET session_active = 1, updated_at = ? "
                                "WHERE realm_id = ? AND character_guid = ? AND incarnation = ?",
                                (ts, eff_realm, guid, eff_inc),
                            )
                    else:
                        conn.execute(
                            "UPDATE persona_characters SET session_active = 0, updated_at = ? "
                            "WHERE realm_id = ? AND character_guid = ? AND incarnation = ? AND session_active = 1",
                            (ts, eff_realm, guid, eff_inc),
                        )

                    conn.execute(f"RELEASE {sp_name}")
                    synced += 1
                except Exception as exc:
                    conn.execute(f"ROLLBACK TO {sp_name}")
                    conn.execute(f"RELEASE {sp_name}")
                    quarantined.append(f"sync_entry_error:{slug_key}:{type(exc).__name__}")
                    logger.warning("Quarantined companion sync entry %s: %s", slug_key, exc)

            conn.execute("COMMIT")
            return {
                "synced": synced,
                "renamed": renamed,
                "quarantined": quarantined,
                "session_conflicts": session_conflicts,
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def register_alt(
        self,
        *,
        persona_id: str,
        slug: str,
        character_guid: int,
        realm_id: int = 1,
        incarnation: int = 1,
        slot_kind: str = "alt",
        fingerprint: dict[str, int] | None = None,
        now_ts: int | None = None,
    ) -> None:
        """Insert-only registration of an additional character under an EXISTING active persona (synthetic/fixture API; no alt creation in Phase II).

        Refuses silent alt reassignment, duplicate keys, inactive/quarantined personas, or active slug collisions.
        """
        ts = int(now_ts if now_ts is not None else time.time())
        clean_slug = slug.strip().lower()
        if not clean_slug or int(realm_id) <= 0 or int(incarnation) <= 0 or int(character_guid) <= 0:
            raise ValueError("Invalid slug, character_guid, realm_id, or incarnation")

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            p_row = conn.execute("SELECT persona_id, state FROM personas WHERE persona_id = ?", (persona_id,)).fetchone()
            if not p_row:
                conn.execute("ROLLBACK")
                raise ValueError(f"Unknown persona_id: {persona_id!r}")
            if str(p_row["state"]) != "active":
                conn.execute("ROLLBACK")
                raise ValueError(f"Cannot register alt on non-active persona {persona_id!r} (state={p_row['state']})")

            existing = conn.execute(
                "SELECT persona_id, slug, active FROM persona_characters WHERE realm_id=? AND character_guid=? AND incarnation=?",
                (int(realm_id), int(character_guid), int(incarnation)),
            ).fetchone()
            if existing:
                reason = (
                    f"alt_reassignment_refused:r{realm_id}:g{character_guid}:i{incarnation} "
                    f"already mapped to {existing['persona_id']} (active={existing['active']})"
                )
                self._record_conflict(
                    conn, realm_id=int(realm_id), character_guid=int(character_guid), incarnation=int(incarnation),
                    kind="alt_reassignment_refused", existing_persona_id=str(existing["persona_id"]),
                    incoming_slug=clean_slug, reason=reason, ts=ts,
                )
                conn.execute("COMMIT")
                raise ValueError(reason)

            slug_taken = conn.execute(
                "SELECT persona_id FROM persona_characters WHERE realm_id=? AND slug=? AND active=1",
                (int(realm_id), clean_slug),
            ).fetchone()
            if slug_taken:
                conn.execute("ROLLBACK")
                raise ValueError(f"Active slug {clean_slug!r} already exists on realm {realm_id} ({slug_taken['persona_id']})")

            fp_str = _dump_fingerprint(fingerprint) if isinstance(fingerprint, dict) else ""
            conn.execute(
                """
                INSERT INTO persona_characters
                    (realm_id, character_guid, incarnation, persona_id, slug, fingerprint, slot_kind, active, session_active, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
                """,
                (int(realm_id), int(character_guid), int(incarnation), persona_id, clean_slug, fp_str, slot_kind, ts),
            )
            self._record_alias(
                conn, persona_id=persona_id, alias=clean_slug, realm_id=int(realm_id),
                character_guid=int(character_guid), incarnation=int(incarnation), active=True, ts=ts,
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def rename_character(
        self,
        *,
        realm_id: int,
        character_guid: int,
        new_slug: str,
        incarnation: int | None = None,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Explicitly record a verified character rename while preserving immutable persona_id & alias history."""
        ts = int(now_ts if now_ts is not None else time.time())
        clean = new_slug.strip().lower()
        if not clean:
            return {"ok": False, "reason": "empty_slug"}

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if incarnation is not None:
                row = conn.execute(
                    "SELECT * FROM persona_characters WHERE realm_id=? AND character_guid=? AND incarnation=? AND active=1",
                    (int(realm_id), int(character_guid), int(incarnation)),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM persona_characters WHERE realm_id=? AND character_guid=? AND active=1 "
                    "ORDER BY incarnation DESC LIMIT 1",
                    (int(realm_id), int(character_guid)),
                ).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "character_not_found"}

            pid = str(row["persona_id"])
            old_slug = str(row["slug"]).strip().lower()
            inc = int(row["incarnation"])
            if clean == old_slug:
                conn.execute("COMMIT")
                return {"ok": True, "idempotent": True, "persona_id": pid, "old_slug": old_slug, "new_slug": clean, "incarnation": inc}

            # Check active character slug collision across BOTH mains and alts on this realm (P2-6)!
            conflict = conn.execute(
                "SELECT persona_id FROM persona_characters WHERE realm_id=? AND slug=? AND active=1",
                (int(realm_id), clean),
            ).fetchone()
            if conflict and str(conflict["persona_id"]) != pid:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"slug_owned_by_other_persona:{conflict['persona_id']}"}
            if conflict and str(conflict["persona_id"]) == pid:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "slug_already_active_on_persona"}

            self._record_alias(
                conn, persona_id=pid, alias=old_slug, realm_id=int(realm_id),
                character_guid=int(character_guid), incarnation=inc, active=False, ts=ts,
            )
            self._record_alias(
                conn, persona_id=pid, alias=clean, realm_id=int(realm_id),
                character_guid=int(character_guid), incarnation=inc, active=True, ts=ts,
            )
            conn.execute(
                "UPDATE persona_characters SET slug=?, updated_at=? WHERE realm_id=? AND character_guid=? AND incarnation=?",
                (clean, ts, int(realm_id), int(character_guid), inc),
            )
            if str(row["slot_kind"]) == "main":
                taken = conn.execute("SELECT 1 FROM personas WHERE slug=? AND persona_id!=?", (clean, pid)).fetchone()
                p_slug = f"{clean}@r{int(realm_id)}" if taken else clean
                conn.execute("UPDATE personas SET slug=?, updated_at=? WHERE persona_id=?", (p_slug, ts, pid))
            conn.execute("COMMIT")
            return {"ok": True, "persona_id": pid, "old_slug": old_slug, "new_slug": clean, "incarnation": inc}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def bind_new_incarnation(
        self,
        *,
        persona_id: str,
        realm_id: int,
        character_guid: int,
        slug: str,
        fingerprint: dict[str, int] | None = None,
        slot_kind: str = "main",
        transfer: bool = False,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Bind a recreated character under a new monotonic incarnation (P2-5: enforces prior-owner check & alias/slug bookkeeping)."""
        ts = int(now_ts if now_ts is not None else time.time())
        clean = slug.strip().lower()
        if not clean or int(realm_id) <= 0 or int(character_guid) <= 0:
            return {"ok": False, "reason": "invalid_arguments"}

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            p_row = conn.execute("SELECT persona_id, state FROM personas WHERE persona_id=?", (persona_id,)).fetchone()
            if not p_row:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "unknown_persona_id"}
            if str(p_row["state"]) != "active":
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"persona_not_active:{p_row['state']}"}

            # Check highest existing incarnation of (realm_id, character_guid) (active OR retired!)
            latest = conn.execute(
                "SELECT persona_id, incarnation, slug, active FROM persona_characters "
                "WHERE realm_id=? AND character_guid=? ORDER BY incarnation DESC LIMIT 1",
                (int(realm_id), int(character_guid)),
            ).fetchone()
            if latest and str(latest["persona_id"]) != persona_id:
                if int(latest["active"]) == 1 or not transfer:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": f"guid_owned_by_other_persona:{latest['persona_id']}"}
                self._record_conflict(
                    conn, realm_id=int(realm_id), character_guid=int(character_guid),
                    incarnation=int(latest["incarnation"]) + 1, kind="guid_transfer",
                    existing_persona_id=str(latest["persona_id"]), incoming_slug=clean,
                    reason=f"explicit_transfer:{latest['persona_id']}->{persona_id}", ts=ts,
                )

            # Active slug collision check against other characters on this realm
            slug_holder = conn.execute(
                "SELECT persona_id, character_guid FROM persona_characters "
                "WHERE realm_id=? AND slug=? AND active=1 AND NOT (character_guid=?)",
                (int(realm_id), clean, int(character_guid)),
            ).fetchone()
            if slug_holder:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"slug_owned_by_other_character:{slug_holder['persona_id']}"}

            new_inc = (int(latest["incarnation"]) + 1) if latest else 1
            if latest:
                self._record_alias(
                    conn, persona_id=str(latest["persona_id"]), alias=str(latest["slug"]),
                    realm_id=int(realm_id), character_guid=int(character_guid),
                    incarnation=int(latest["incarnation"]), active=False, ts=ts,
                )

            conn.execute(
                "UPDATE persona_characters SET active=0, session_active=0, updated_at=? "
                "WHERE realm_id=? AND character_guid=? AND active=1",
                (ts, int(realm_id), int(character_guid)),
            )
            fp_str = _dump_fingerprint(fingerprint) if isinstance(fingerprint, dict) else ""
            conn.execute(
                """
                INSERT INTO persona_characters
                    (realm_id, character_guid, incarnation, persona_id, slug, fingerprint, slot_kind, active, session_active, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
                """,
                (int(realm_id), int(character_guid), new_inc, persona_id, clean, fp_str, slot_kind, ts),
            )
            self._record_alias(
                conn, persona_id=persona_id, alias=clean, realm_id=int(realm_id),
                character_guid=int(character_guid), incarnation=new_inc, active=True, ts=ts,
            )
            if slot_kind == "main":
                taken = conn.execute("SELECT 1 FROM personas WHERE slug=? AND persona_id!=?", (clean, persona_id)).fetchone()
                p_slug = f"{clean}@r{int(realm_id)}" if taken else clean
                conn.execute("UPDATE personas SET slug=?, updated_at=? WHERE persona_id=?", (p_slug, ts, persona_id))
            conn.execute("COMMIT")
            return {
                "ok": True,
                "persona_id": persona_id,
                "realm_id": int(realm_id),
                "character_guid": int(character_guid),
                "incarnation": new_inc,
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def reactivate_incarnation(
        self,
        *,
        persona_id: str,
        realm_id: int,
        character_guid: int,
        incarnation: int,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """True character restore: reactivate an existing retired incarnation without minting a new one (P2-11)."""
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            target = conn.execute(
                "SELECT * FROM persona_characters WHERE persona_id=? AND realm_id=? AND character_guid=? AND incarnation=?",
                (persona_id, int(realm_id), int(character_guid), int(incarnation)),
            ).fetchone()
            if not target:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "incarnation_not_found"}
            other_active = conn.execute(
                "SELECT incarnation FROM persona_characters WHERE realm_id=? AND character_guid=? AND active=1 AND incarnation!=?",
                (int(realm_id), int(character_guid), int(incarnation)),
            ).fetchone()
            if other_active:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"another_incarnation_active:{other_active['incarnation']}"}
            conn.execute(
                "UPDATE persona_characters SET active=1, updated_at=? WHERE realm_id=? AND character_guid=? AND incarnation=?",
                (ts, int(realm_id), int(character_guid), int(incarnation)),
            )
            self._record_alias(
                conn, persona_id=persona_id, alias=str(target["slug"]),
                realm_id=int(realm_id), character_guid=int(character_guid),
                incarnation=int(incarnation), active=True, ts=ts,
            )
            conn.execute("COMMIT")
            return {"ok": True, "persona_id": persona_id, "incarnation": int(incarnation)}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def quarantine_persona(self, persona_id: str, reason: str, *, now_ts: int | None = None) -> dict[str, Any]:
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE personas SET state='quarantined', quarantine_reason=?, updated_at=? WHERE persona_id=?",
                (reason[:180], ts, persona_id),
            )
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "not_found"}
            conn.execute("COMMIT")
            return {"ok": True, "persona_id": persona_id, "state": "quarantined"}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def resolve_conflict(
        self,
        conflict_id: int,
        *,
        action: str = "dismiss",
        note: str = "",
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Owner conflict recovery API (P1-5): resolve an open conflict and optionally reactivate a quarantined persona."""
        ts = int(now_ts if now_ts is not None else time.time())
        act = action.strip().lower()
        if act not in ("dismiss", "reactivate_persona"):
            return {"ok": False, "reason": f"invalid_action:{act}"}
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM persona_conflicts WHERE id=?", (int(conflict_id),)).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "conflict_not_found"}
            conn.execute(
                "UPDATE persona_conflicts SET resolved_at=?, resolution_note=? WHERE id=?",
                (ts, (note or act)[:180], int(conflict_id)),
            )
            if act == "reactivate_persona" and row["existing_persona_id"]:
                conn.execute(
                    "UPDATE personas SET state='active', quarantine_reason='', updated_at=? WHERE persona_id=?",
                    (ts, str(row["existing_persona_id"])),
                )
            conn.execute("COMMIT")
            return {"ok": True, "conflict_id": int(conflict_id), "action": act}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def set_coop_enrollment(
        self,
        persona_id: str,
        *,
        enrolled: bool,
        owner_override: bool = False,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Explicitly toggle cooperative enrollment; core_protected personas require owner_override=True (persists across syncs!)."""
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT core_protected, state FROM personas WHERE persona_id=?", (persona_id,)
            ).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "not_found"}
            if enrolled and int(row["core_protected"]) == 1 and not owner_override:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "core_protected_requires_owner_override"}
            if enrolled and str(row["state"]) != "active":
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"persona_not_active:{row['state']}"}
            override_ts = ts if (enrolled and owner_override) else 0
            conn.execute(
                "UPDATE personas SET enrolled_coop=?, coop_override_at=?, updated_at=? WHERE persona_id=?",
                (1 if enrolled else 0, override_ts, ts, persona_id),
            )
            conn.execute("COMMIT")
            return {"ok": True, "persona_id": persona_id, "enrolled_coop": bool(enrolled), "coop_override_at": override_ts}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def set_active_session(
        self,
        *,
        persona_id: str,
        realm_id: int,
        character_guid: int,
        incarnation: int = 1,
        active: bool = True,
        switch: bool = False,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Enforce AT MOST ONE active online session per persona_id (transactional + partial unique index)."""
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            target = conn.execute(
                "SELECT active, session_active FROM persona_characters "
                "WHERE persona_id=? AND realm_id=? AND character_guid=? AND incarnation=?",
                (persona_id, int(realm_id), int(character_guid), int(incarnation)),
            ).fetchone()
            if not target:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "character_not_mapped_to_persona"}
            if active and int(target["active"]) != 1:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "character_incarnation_inactive"}

            if not active:
                conn.execute(
                    "UPDATE persona_characters SET session_active=0, updated_at=? "
                    "WHERE persona_id=? AND realm_id=? AND character_guid=? AND incarnation=?",
                    (ts, persona_id, int(realm_id), int(character_guid), int(incarnation)),
                )
                conn.execute("COMMIT")
                return {"ok": True, "session_active": False}

            other = conn.execute(
                "SELECT realm_id, character_guid, incarnation, slug FROM persona_characters "
                "WHERE persona_id=? AND session_active=1 "
                "AND NOT (realm_id=? AND character_guid=? AND incarnation=?)",
                (persona_id, int(realm_id), int(character_guid), int(incarnation)),
            ).fetchone()
            if other:
                if not switch:
                    conn.execute("ROLLBACK")
                    return {
                        "ok": False,
                        "reason": "persona_already_has_active_session",
                        "active_character": {
                            "realm_id": int(other["realm_id"]),
                            "character_guid": int(other["character_guid"]),
                            "incarnation": int(other["incarnation"]),
                            "slug": str(other["slug"]),
                        },
                    }
                conn.execute(
                    "UPDATE persona_characters SET session_active=0, updated_at=? WHERE persona_id=? AND session_active=1",
                    (ts, persona_id),
                )

            conn.execute(
                "UPDATE persona_characters SET session_active=1, updated_at=? "
                "WHERE persona_id=? AND realm_id=? AND character_guid=? AND incarnation=?",
                (ts, persona_id, int(realm_id), int(character_guid), int(incarnation)),
            )
            conn.execute("COMMIT")
            return {"ok": True, "session_active": True, "switched": bool(other)}
        except sqlite3.IntegrityError:
            if conn.in_transaction:
                conn.rollback()
            return {"ok": False, "reason": "persona_already_has_active_session"}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def active_character_for_persona(self, persona_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT realm_id, character_guid, incarnation, slug, slot_kind FROM persona_characters "
                "WHERE persona_id=? AND session_active=1 AND active=1 LIMIT 1",
                (persona_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def aliases_for_persona(self, persona_id: str) -> list[str]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT alias FROM persona_aliases WHERE persona_id=? "
                "GROUP BY alias ORDER BY MIN(first_seen_at) ASC, alias ASC",
                (persona_id,),
            ).fetchall()
            return [str(r["alias"]) for r in rows]
        finally:
            conn.close()

    def persona_for_alias(self, alias: str, *, realm_id: int | None = None) -> PersonaRecord | None:
        conn = self._conn()
        try:
            if realm_id is not None:
                row = conn.execute(
                    """
                    SELECT p.persona_id, p.slug, p.faction, p.faction_verified, p.owner_group,
                           p.enrolled_coop, p.state, p.core_protected, p.quarantine_reason
                    FROM persona_aliases pa
                    JOIN personas p ON p.persona_id = pa.persona_id
                    WHERE pa.alias = ? AND pa.realm_id = ?
                    ORDER BY pa.active DESC, pa.last_seen_at DESC LIMIT 1
                    """,
                    (alias.strip().lower(), int(realm_id)),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT p.persona_id, p.slug, p.faction, p.faction_verified, p.owner_group,
                           p.enrolled_coop, p.state, p.core_protected, p.quarantine_reason
                    FROM persona_aliases pa
                    JOIN personas p ON p.persona_id = pa.persona_id
                    WHERE pa.alias = ?
                    ORDER BY pa.active DESC, pa.last_seen_at DESC LIMIT 1
                    """,
                    (alias.strip().lower(),),
                ).fetchone()
            if not row:
                return None
            return self._row_to_record(conn, row)
        finally:
            conn.close()

    def _row_to_record(self, conn: sqlite3.Connection, row: sqlite3.Row) -> PersonaRecord:
        aliases = tuple(
            str(r["alias"])
            for r in conn.execute(
                "SELECT alias FROM persona_aliases WHERE persona_id=? "
                "GROUP BY alias ORDER BY MIN(first_seen_at) ASC, alias ASC",
                (row["persona_id"],),
            ).fetchall()
        )
        return PersonaRecord(
            persona_id=str(row["persona_id"]),
            slug=str(row["slug"]),
            faction=str(row["faction"]),
            owner_group=str(row["owner_group"]),
            enrolled_coop=bool(row["enrolled_coop"]),
            state=str(row["state"]),
            core_protected=bool(row["core_protected"]),
            faction_verified=bool(row["faction_verified"]),
            quarantine_reason=str(row["quarantine_reason"] or ""),
            aliases=aliases,
        )

    def persona_for_guid(
        self,
        character_guid: int,
        realm_id: int = 1,
        *,
        incarnation: int | None = None,
        include_inactive: bool = False,
    ) -> PersonaRecord | None:
        conn = self._conn()
        try:
            act_clause = "" if include_inactive else "AND pc.active = 1"
            if incarnation is not None:
                row = conn.execute(
                    f"""
                    SELECT p.persona_id, p.slug, p.faction, p.faction_verified, p.owner_group,
                           p.enrolled_coop, p.state, p.core_protected, p.quarantine_reason
                    FROM persona_characters pc
                    JOIN personas p ON p.persona_id = pc.persona_id
                    WHERE pc.realm_id = ? AND pc.character_guid = ? AND pc.incarnation = ? {act_clause}
                    LIMIT 1
                    """,
                    (int(realm_id), int(character_guid), int(incarnation)),
                ).fetchone()
            else:
                row = conn.execute(
                    f"""
                    SELECT p.persona_id, p.slug, p.faction, p.faction_verified, p.owner_group,
                           p.enrolled_coop, p.state, p.core_protected, p.quarantine_reason
                    FROM persona_characters pc
                    JOIN personas p ON p.persona_id = pc.persona_id
                    WHERE pc.realm_id = ? AND pc.character_guid = ? {act_clause}
                    ORDER BY pc.active DESC, pc.incarnation DESC LIMIT 1
                    """,
                    (int(realm_id), int(character_guid)),
                ).fetchone()
            if not row:
                return None
            return self._row_to_record(conn, row)
        finally:
            conn.close()

    def conflicts(self, *, only_open: bool = False) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            q = "SELECT * FROM persona_conflicts WHERE resolved_at = 0 ORDER BY id ASC" if only_open else "SELECT * FROM persona_conflicts ORDER BY id ASC"
            return [dict(r) for r in conn.execute(q).fetchall()]
        finally:
            conn.close()

    def distinct_personas(
        self,
        guids: list[int],
        realm_id: int = 1,
        *,
        exclude_unresolved: bool = True,
    ) -> list[tuple[str, int]]:
        """Filter character GUIDs to at most ONE character per distinct active persona (P2-7: fail-closed by default!)."""
        seen: set[str] = set()
        out: list[tuple[str, int]] = []
        for g in guids:
            rec = self.persona_for_guid(g, realm_id=realm_id, include_inactive=True)
            if rec is None:
                if exclude_unresolved:
                    continue
                pid = f"unmapped_{g}"
            else:
                if rec.state != "active" and exclude_unresolved:
                    continue
                pid = rec.persona_id
            if pid not in seen:
                seen.add(pid)
                out.append((pid, g))
        return out

    @staticmethod
    def read_only_snapshot(
        db_path: Path | None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Strictly read-only projection of persona mappings & active sessions (P1-2, P1-8, P2-8, P2-9).

        - Accepts an optional open `conn` so callers share one consistent SQLite snapshot!
        - Reads legacy `companion_*` tables FIRST, canonical `personas` SECOND.
        - Flags ANY `(realm_id, guid)` mapped to >1 distinct `persona_id` across `active=1` rows as a conflict!
        - Exposes `by_persona_session` (`persona_id -> (realm_id, guid, inc)`) and `open_dual_session_personas`.
        """
        out: dict[str, Any] = {
            "ok": True,
            "has_registry": False,
            "errors": [],
            "by_key": {},                      # (realm_id, character_guid) -> record dict
            "by_persona_session": {},          # persona_id -> (realm_id, character_guid, incarnation)
            "open_dual_session_personas": set(),
        }
        owned_conn = False
        if conn is None:
            if db_path is None or not Path(db_path).is_file():
                return out
            try:
                quoted = urllib.parse.quote(str(Path(db_path).resolve()), safe="/")
                conn = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=3.0)
                conn.row_factory = sqlite3.Row
                owned_conn = True
            except sqlite3.Error as exc:
                out["ok"] = False
                out["errors"].append(f"{type(exc).__name__}: {exc}")
                return out

        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            active_personas_per_key: dict[tuple[int, int], set[str]] = {}

            # P1-8: Legacy table FIRST, canonical table SECOND; pick by active DESC, incarnation DESC!
            for p_tbl, pc_tbl in (
                ("companion_personas", "companion_persona_characters"),
                ("personas", "persona_characters"),
            ):
                if p_tbl in tables and pc_tbl in tables:
                    out["has_registry"] = True
                    p_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({p_tbl})").fetchall()}
                    pc_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({pc_tbl})").fetchall()}
                    cp_expr = "p.core_protected" if "core_protected" in p_cols else "0"
                    sa_expr = "c.session_active" if "session_active" in pc_cols else "0"
                    rows = conn.execute(
                        f"""
                        SELECT c.realm_id, c.character_guid, c.incarnation, c.active,
                               {sa_expr} AS session_active, p.persona_id, p.faction,
                               p.owner_group, p.enrolled_coop, p.state, {cp_expr} AS core_protected
                        FROM {pc_tbl} c
                        JOIN {p_tbl} p ON p.persona_id = c.persona_id
                        ORDER BY c.active ASC, c.incarnation ASC
                        """
                    ).fetchall()
                    for r in rows:
                        key = (int(r["realm_id"]), int(r["character_guid"]))
                        pid = str(r["persona_id"])
                        is_act = bool(int(r["active"]))
                        is_sess = bool(int(r["session_active"]))
                        rec = {
                            "realm_id": int(r["realm_id"]),
                            "character_guid": int(r["character_guid"]),
                            "incarnation": int(r["incarnation"]),
                            "persona_id": pid,
                            "faction": str(r["faction"] or "unknown"),
                            "owner_group": str(r["owner_group"] or "guild"),
                            "enrolled_coop": bool(int(r["enrolled_coop"])),
                            "core_protected": bool(int(r["core_protected"])) or is_protected_owner_group(r["owner_group"]),
                            "state": str(r["state"] or "active").lower(),
                            "char_active": is_act,
                            "session_active": is_sess,
                        }
                        if is_act:
                            active_personas_per_key.setdefault(key, set()).add(pid)
                        if is_act and is_sess:
                            out["by_persona_session"][pid] = (key[0], key[1], rec["incarnation"])
                        out["by_key"][key] = rec

            for key, pids in active_personas_per_key.items():
                if len(pids) > 1:
                    out["ok"] = False
                    out["errors"].append(f"persona_mapping_conflict:r{key[0]}:g{key[1]}:{sorted(pids)}")

            if "persona_conflicts" in tables:
                pc_c_cols = {r[1] for r in conn.execute("PRAGMA table_info(persona_conflicts)").fetchall()}
                if "kind" in pc_c_cols and "resolved_at" in pc_c_cols:
                    for r in conn.execute(
                        "SELECT DISTINCT existing_persona_id FROM persona_conflicts "
                        "WHERE kind = 'dual_active_session' AND resolved_at = 0"
                    ).fetchall():
                        out["open_dual_session_personas"].add(str(r["existing_persona_id"]))
        except sqlite3.Error as exc:
            out["ok"] = False
            out["errors"].append(f"{type(exc).__name__}: {exc}")
            logger.warning("Read-only persona snapshot failed for %s: %s", db_path, exc)
        finally:
            if owned_conn and conn is not None:
                conn.close()
        return out
