"""Phase II Typed Opportunity Protocol, Atomic Reservations & Voluntary Matchmaking (Cards II-02 & II-04).

Design guarantees (docs/PHASE_II_LIVING_GUILD.md Sections 5 & 10):
- Opportunities are offers of OPTIONAL cooperative gameplay, never forced assignments.
- Existing `companion_work_orders` are exposed via a read-only adapter (no duplicate order state).
- Atomic `BEGIN IMMEDIATE` conditional transitions on `expected_revision` with strict unique
  active reservation per `persona_id` (two opportunities competing for one tank can NEVER both
  reserve the same persona; partial invite failures cleanly roll back all reservations).
- Pure shadow matchmaking (`run_shadow_match_snapshot`) writes ONLY `runtime/phase2_shadow.json`
  and never modifies live opportunities, reservations, or game tables.
- Resource admission guard enforces i5-7400 RAM/tick/instance caps before admitting any group.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("hearthcore.opportunities")

from .core import ROOT, profile, profiles
from .dungeons import (
    DUNGEON_BY_ID,
    DUNGEON_CATALOG,
    DungeonSpec,
    check_character_eligibility,
    enabled_dungeon_ids,
    infer_viable_roles,
)
from .identity import PROTECTED_OWNER_GROUPS, PersonaRegistry, persona_id_for_slug

SCHEMA_VERSION = 1
DEFAULT_RECRUIT_TTL_SEC = 20 * 60
DEFAULT_CONFIRM_TTL_SEC = 2 * 60
DEFAULT_MAX_GROUPS = 1
MIN_AVAIL_RAM_MIB = 512.0
MAX_TICK_P99_MS = 45.0
NATIVE_STATE_ORDER: dict[str, int] = {
    "queued": 1,
    "proposal_sent": 2,
    "in_instance": 3,
    "active": 3,
}


@dataclass(frozen=True)
class NativeReconcileSnapshot:
    """Authoritative live native queue/group/instance snapshot for crash reconciliation (Issue #13).

    Fail-closed: when `authoritative=False` or a set is `None`, reconcile will NOT abort active runs
    or release confirmed handoffs on missing/unknown data.
    """
    authoritative: bool = False
    active_group_ids: frozenset[int] | None = None
    active_queue_ids: frozenset[str] | None = None
    active_run_ids: frozenset[str] | None = None
    # correlation_key -> {"native_state": ..., "native_queue_id": ..., "group_id": ..., "native_run_id": ...}
    correlation_map: dict[str, dict[str, Any]] | None = None


def coop_enabled() -> bool:
    """Global feature flag for live Phase II cooperative execution (default False = safe off)."""
    return (os.getenv("HEARTHCORE_COOP_ENABLED") or "0").strip().lower() in ("1", "true", "yes", "on")


def check_resource_headroom(
    *,
    mem_avail_mib: float | None,
    tick_p99_ms: float | None,
    active_managed_groups: int,
    max_managed_groups: int = DEFAULT_MAX_GROUPS,
    prev_paused: bool = False,
) -> dict[str, Any]:
    """Check Acer i5-7400 hardware headroom with FAIL-CLOSED telemetry & hysteresis (Issue #61)."""
    reasons: list[str] = []
    if active_managed_groups >= max_managed_groups:
        reasons.append(f"group_cap_reached:{active_managed_groups}/{max_managed_groups}")

    # Hysteresis: pause at <512 MiB / >45 ms; recover only once >=640 MiB and <=38 ms
    ram_thresh = 640.0 if prev_paused else MIN_AVAIL_RAM_MIB
    tick_thresh = 38.0 if prev_paused else MAX_TICK_P99_MS

    # Fail closed on missing telemetry (Issue #61): missing data means DO NOT ADMIT!
    if mem_avail_mib is None:
        reasons.append("missing_ram_telemetry")
    elif mem_avail_mib < ram_thresh:
        reasons.append(f"low_ram_headroom:{mem_avail_mib:.0f}MiB<{ram_thresh:.0f}MiB")

    if tick_p99_ms is None:
        reasons.append("missing_tick_telemetry")
    elif tick_p99_ms > tick_thresh:
        reasons.append(f"high_tick_p99:{tick_p99_ms:.1f}ms>{tick_thresh:.1f}ms")

    admit_ok = len(reasons) == 0
    return {
        "admit_ok": admit_ok,
        "paused": not admit_ok,
        "prev_paused": bool(prev_paused),
        "reasons": reasons,
        "active_managed_groups": active_managed_groups,
        "max_managed_groups": max_managed_groups,
        "mem_avail_mib": round(mem_avail_mib, 1) if mem_avail_mib is not None else None,
        "tick_p99_ms": round(tick_p99_ms, 1) if tick_p99_ms is not None else None,
    }


def evaluate_persisted_headroom(
    state_path: Path,
    *,
    mem_avail_mib: float | None,
    tick_p99_ms: float | None,
    active_managed_groups: int,
    max_managed_groups: int = DEFAULT_MAX_GROUPS,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Evaluate headroom using persisted hysteresis state on disk so pause/recover survives runs (Issue #61)."""
    ts = int(now_ts if now_ts is not None else time.time())
    prev_paused = False
    try:
        if state_path.is_file():
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            prev_paused = bool(saved.get("paused", False))
    except Exception as exc:
        logger.warning("Corrupt headroom state at %s (%s); failing closed", state_path, exc)
        prev_paused = True  # fail closed on corrupt state

    res = check_resource_headroom(
        mem_avail_mib=mem_avail_mib,
        tick_p99_ms=tick_p99_ms,
        active_managed_groups=active_managed_groups,
        max_managed_groups=max_managed_groups,
        prev_paused=prev_paused,
    )
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"paused": bool(res["paused"]), "updated_at": ts, "reasons": res["reasons"]}, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(state_path)
    except OSError as exc:
        logger.warning("Failed persisting headroom state to %s: %s", state_path, exc)
    return res


def admit_cooperative_opportunity(
    coordinator: "OpportunityCoordinator",
    opportunity_id: int,
    *,
    expected_revision: int,
    state_path: Path,
    mem_avail_mib: float | None,
    tick_p99_ms: float | None,
    active_managed_groups: int,
    max_managed_groups: int = DEFAULT_MAX_GROUPS,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Live cooperative admission path enforcing fail-closed persisted headroom & hysteresis (Issue #61)."""
    hr = evaluate_persisted_headroom(
        state_path,
        mem_avail_mib=mem_avail_mib,
        tick_p99_ms=tick_p99_ms,
        active_managed_groups=active_managed_groups,
        max_managed_groups=max_managed_groups,
        now_ts=now_ts,
    )
    if not hr["admit_ok"]:
        logger.warning(
            "Cooperative opportunity #%s blocked by resource headroom: %s",
            opportunity_id,
            ",".join(hr["reasons"]),
        )
        return {
            "ok": False,
            "reason": "resource_headroom_blocked",
            "headroom": hr,
        }
    res = coordinator.reserve_party(
        opportunity_id,
        expected_revision=expected_revision,
        now_ts=now_ts,
    )
    res["headroom"] = hr
    return res


def adapt_work_orders_as_opportunities(work_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read-only Phase II opportunity adapter over existing `companion_work_orders` (no duplication)."""
    out: list[dict[str, Any]] = []
    for wo in work_orders:
        out.append({
            "schema_version": SCHEMA_VERSION,
            "opportunity_id": f"wo:{wo['id']}",
            "revision": 1,
            "kind": "craft_work_order",
            "creator_guid": int(wo.get("requester_guid") or 0),
            "creator_name": str(wo.get("requester_name") or ""),
            "visibility": {"faction": "alliance", "guild_id": 6},
            "target": {
                "work_order_id": int(wo["id"]),
                "item_entry": int(wo.get("item_entry") or 0),
                "item_name": str(wo.get("item_name") or ""),
                "quantity": int(wo.get("quantity") or 1),
                "skill_id": int(wo.get("skill") or 0),
            },
            "state": "recruiting",
            "source_table": "companion_work_orders",
        })
    return out


class OpportunityCoordinator:
    """Atomic transactional voluntary opportunity & reservation coordinator (SQLite WAL)."""

    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("""
                CREATE TABLE IF NOT EXISTS opportunities (
                    id INTEGER PRIMARY KEY,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 1,
                    kind TEXT NOT NULL,
                    creator_guid INTEGER NOT NULL DEFAULT 0,
                    faction TEXT NOT NULL DEFAULT 'alliance',
                    guild_id INTEGER NOT NULL DEFAULT 6,
                    dungeon_id TEXT NOT NULL,
                    dungeon_map_id INTEGER NOT NULL,
                    difficulty INTEGER NOT NULL DEFAULT 0,
                    roles_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    recruit_until INTEGER NOT NULL,
                    confirm_until INTEGER NOT NULL DEFAULT 0,
                    correlation_key TEXT NOT NULL DEFAULT '',
                    estimated_minutes INTEGER NOT NULL DEFAULT 45,
                    group_id INTEGER NOT NULL DEFAULT 0,
                    native_queue_id TEXT NOT NULL DEFAULT '',
                    native_run_id TEXT NOT NULL DEFAULT '',
                    native_state TEXT NOT NULL DEFAULT '',
                    started_at INTEGER NOT NULL DEFAULT 0,
                    ended_at INTEGER NOT NULL DEFAULT 0,
                    outcome_reason TEXT NOT NULL DEFAULT '',
                    is_shadow INTEGER NOT NULL DEFAULT 1
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS opportunity_interests (
                    id INTEGER PRIMARY KEY,
                    opportunity_id INTEGER NOT NULL,
                    opportunity_revision INTEGER NOT NULL,
                    persona_id TEXT NOT NULL,
                    realm_id INTEGER NOT NULL DEFAULT 1,
                    character_guid INTEGER NOT NULL,
                    incarnation INTEGER NOT NULL DEFAULT 1,
                    character_slug TEXT NOT NULL,
                    role TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reason_code TEXT NOT NULL DEFAULT '',
                    available_until INTEGER NOT NULL,
                    reserved_until INTEGER NOT NULL DEFAULT 0,
                    confirmed_at INTEGER NOT NULL DEFAULT 0,
                    ack_token TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(opportunity_id, persona_id)
                )
            """)
            # Additive schema migration for pre-existing brain.db files (Issue #13)
            opp_cols = {r[1] for r in db.execute("PRAGMA table_info(opportunities)").fetchall()}
            for col, typ in (
                ("confirm_until", "INTEGER NOT NULL DEFAULT 0"),
                ("correlation_key", "TEXT NOT NULL DEFAULT ''"),
                ("native_queue_id", "TEXT NOT NULL DEFAULT ''"),
                ("native_run_id", "TEXT NOT NULL DEFAULT ''"),
                ("native_state", "TEXT NOT NULL DEFAULT ''"),
                ("started_at", "INTEGER NOT NULL DEFAULT 0"),
                ("ended_at", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col not in opp_cols:
                    db.execute(f"ALTER TABLE opportunities ADD COLUMN {col} {typ}")
            # Backfill confirm_until on pre-#13 assembling rows so they can never stay stranded!
            db.execute(
                """
                UPDATE opportunities
                SET confirm_until = COALESCE(
                    (SELECT MAX(reserved_until) FROM opportunity_interests oi
                     WHERE oi.opportunity_id = opportunities.id AND oi.reserved_until > 0),
                    created_at
                )
                WHERE state = 'assembling' AND confirm_until = 0
                """
            )
            oi_cols = {r[1] for r in db.execute("PRAGMA table_info(opportunity_interests)").fetchall()}
            for col, typ in (
                ("realm_id", "INTEGER NOT NULL DEFAULT 1"),
                ("incarnation", "INTEGER NOT NULL DEFAULT 1"),
                ("confirmed_at", "INTEGER NOT NULL DEFAULT 0"),
                ("ack_token", "TEXT NOT NULL DEFAULT ''"),
            ):
                if col not in oi_cols:
                    db.execute(f"ALTER TABLE opportunity_interests ADD COLUMN {col} {typ}")
            db.execute("CREATE INDEX IF NOT EXISTS idx_oi_persona_state ON opportunity_interests(persona_id, state)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_oi_state_reserved ON opportunity_interests(state, reserved_until)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_opp_state ON opportunities(state, is_shadow)")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_opp_native_run_id "
                "ON opportunities(native_run_id) WHERE native_run_id != ''"
            )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def create_opportunity(
        self,
        *,
        dungeon_id: str,
        creator_guid: int,
        faction: str = "alliance",
        guild_id: int = 6,
        now_ts: int | None = None,
        recruit_ttl_sec: int = DEFAULT_RECRUIT_TTL_SEC,
        is_shadow: bool = True,
    ) -> dict[str, Any]:
        spec = DUNGEON_BY_ID.get(dungeon_id)
        if not spec or not spec.supported:
            raise ValueError(f"Unsupported dungeon_id: {dungeon_id}")
        ts = int(now_ts if now_ts is not None else time.time())
        roles_dict = dict(spec.roles)
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                INSERT INTO opportunities (
                    schema_version, revision, kind, creator_guid, faction, guild_id,
                    dungeon_id, dungeon_map_id, difficulty, roles_json, state,
                    created_at, recruit_until, estimated_minutes, is_shadow
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    SCHEMA_VERSION, 1, "dungeon_run", int(creator_guid), faction.lower(), int(guild_id),
                    spec.dungeon_id, spec.map_id, spec.difficulty, json.dumps(roles_dict, sort_keys=True),
                    "recruiting", ts, ts + int(recruit_ttl_sec), spec.estimated_minutes, 1 if is_shadow else 0,
                ),
            )
            opp_id = int(cur.lastrowid)
            conn.execute("COMMIT")
            return {
                "id": opp_id,
                "revision": 1,
                "dungeon_id": spec.dungeon_id,
                "state": "recruiting",
                "is_shadow": is_shadow,
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def express_interest(
        self,
        *,
        opportunity_id: int,
        expected_revision: int,
        persona_id: str,
        character_guid: int,
        character_slug: str,
        role: str,
        reason_code: str,
        available_until: int,
        realm_id: int = 1,
        incarnation: int = 1,
        now_ts: int | None = None,
    ) -> bool:
        """Record voluntary character interest if opportunity is recruiting at expected_revision."""
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            opp = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
            if not opp or str(opp["state"]) != "recruiting" or int(opp["revision"]) != int(expected_revision):
                conn.execute("ROLLBACK")
                return False
            if int(opp["recruit_until"]) <= ts or int(available_until) <= ts:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                """
                INSERT INTO opportunity_interests (
                    opportunity_id, opportunity_revision, persona_id, realm_id, character_guid, incarnation,
                    character_slug, role, state, reason_code, available_until, reserved_until, confirmed_at,
                    ack_token, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,'',?,?)
                ON CONFLICT(opportunity_id, persona_id) DO UPDATE SET
                    opportunity_revision = excluded.opportunity_revision,
                    realm_id = excluded.realm_id,
                    character_guid = excluded.character_guid,
                    incarnation = excluded.incarnation,
                    character_slug = excluded.character_slug,
                    role = excluded.role,
                    state = CASE WHEN state IN ('withdrawn','expired','declined','released') THEN 'interested' ELSE state END,
                    reason_code = excluded.reason_code,
                    available_until = excluded.available_until,
                    updated_at = excluded.updated_at
                """,
                (
                    int(opportunity_id), int(expected_revision), persona_id, int(realm_id), int(character_guid),
                    int(incarnation), character_slug.lower(), role.lower(), "interested", reason_code[:64],
                    int(available_until), ts, ts,
                ),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def withdraw_interest(self, opportunity_id: int, persona_id: str, reason: str = "withdrawn", now_ts: int | None = None) -> dict[str, Any]:
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            opp = conn.execute("SELECT state, is_shadow FROM opportunities WHERE id=?", (int(opportunity_id),)).fetchone()
            if not opp:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "not_found"}
            opp_state = str(opp["state"])
            # Refuse blind local withdrawal once handed off to native ('confirmed'/'active'/'cancelling') on live runs!
            if int(opp["is_shadow"]) == 0 and opp_state in ("confirmed", "active", "cancelling"):
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"requires_native_leave:{opp_state}"}

            row = conn.execute(
                "SELECT state FROM opportunity_interests WHERE opportunity_id=? AND persona_id=?",
                (int(opportunity_id), persona_id),
            ).fetchone()
            was_committed = bool(row and str(row["state"]) in ("reserved", "accepted"))
            conn.execute(
                "UPDATE opportunity_interests SET state='withdrawn', reason_code=?, reserved_until=0, updated_at=? "
                "WHERE opportunity_id=? AND persona_id=? AND state IN ('interested','reserved','accepted')",
                (reason[:64], ts, int(opportunity_id), persona_id),
            )
            if was_committed and opp_state == "assembling":
                conn.execute(
                    "UPDATE opportunity_interests SET state='released', reserved_until=0, reason_code=?, updated_at=? "
                    "WHERE opportunity_id=? AND state IN ('reserved','accepted')",
                    (f"member_withdrew:{reason}"[:64], ts, int(opportunity_id)),
                )
                conn.execute(
                    "UPDATE opportunities SET state='cancelled', outcome_reason=?, confirm_until=0, ended_at=?, revision=revision+1 "
                    "WHERE id=? AND state='assembling'",
                    (f"member_withdrew:{persona_id}"[:120], ts, int(opportunity_id)),
                )
            conn.execute("COMMIT")
            return {"ok": True}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _expire_stale_in_tx(self, conn: sqlite3.Connection, now_ts: int) -> dict[str, int]:
        """Expire stale recruiting/assembling opportunities & reservations inside an open IMMEDIATE tx."""
        # 1. Identify any assembling opportunity whose confirm_until is missing (0) or elapsed,
        #    OR has an expired reserved member (zero stranded assembling!)
        stranded_rows = conn.execute(
            """
            SELECT DISTINCT o.id
            FROM opportunities o
            LEFT JOIN opportunity_interests oi ON oi.opportunity_id = o.id
            WHERE o.state = 'assembling'
              AND (
                  o.confirm_until <= ?
                  OR (oi.state = 'reserved' AND (oi.reserved_until = 0 OR oi.reserved_until <= ?))
              )
            """,
            (now_ts, now_ts),
        ).fetchall()
        stranded_ids = [int(r["id"]) for r in stranded_rows]

        c_stranded = 0
        for oid in stranded_ids:
            conn.execute(
                "UPDATE opportunity_interests SET state = CASE WHEN state='reserved' AND (reserved_until = 0 OR reserved_until <= ?) "
                "THEN 'expired' ELSE 'released' END, "
                "reserved_until = 0, reason_code = 'confirm_ttl_expired', updated_at = ? "
                "WHERE opportunity_id = ? AND state IN ('reserved', 'accepted')",
                (now_ts, now_ts, oid),
            )
            c_stranded += conn.execute(
                "UPDATE opportunities SET state='expired', outcome_reason='confirm_ttl_expired', "
                "confirm_until=0, ended_at=?, revision=revision+1 WHERE id=? AND state='assembling'",
                (now_ts, oid),
            ).rowcount

        # 2. Expire any remaining standalone expired reservations
        c1 = conn.execute(
            "UPDATE opportunity_interests SET state='expired', reserved_until=0, updated_at=? "
            "WHERE state='reserved' AND (reserved_until = 0 OR reserved_until <= ?)",
            (now_ts, now_ts),
        ).rowcount
        c2 = conn.execute(
            "UPDATE opportunity_interests SET state='expired', updated_at=? "
            "WHERE state='interested' AND available_until <= ?",
            (now_ts, now_ts),
        ).rowcount
        c3 = conn.execute(
            "UPDATE opportunities SET state='expired', outcome_reason='recruit_ttl_expired', ended_at=?, revision=revision+1 "
            "WHERE state='recruiting' AND recruit_until <= ?",
            (now_ts, now_ts),
        ).rowcount
        return {
            "expired_reservations": c1,
            "expired_interests": c2,
            "expired_opportunities": c3 + c_stranded,
            "expired_assembling": c_stranded,
        }

    def expire_stale(self, now_ts: int) -> dict[str, int]:
        """Expire past-TTL recruiting & stranded assembling opportunities cleanly (zero stranding, Issue #13)."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            res = self._expire_stale_in_tx(conn, int(now_ts))
            conn.execute("COMMIT")
            return res
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def get_opportunity(self, opportunity_id: int) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM opportunities WHERE id = ?", (int(opportunity_id),)).fetchone()
            if not row:
                return None
            members = [
                dict(m)
                for m in conn.execute(
                    "SELECT * FROM opportunity_interests WHERE opportunity_id = ? ORDER BY id ASC",
                    (int(opportunity_id),),
                ).fetchall()
            ]
            out = dict(row)
            out["members"] = members
            return out
        finally:
            conn.close()

    def reserve_party(
        self,
        opportunity_id: int,
        *,
        expected_revision: int,
        now_ts: int | None = None,
        confirm_ttl_sec: int = DEFAULT_CONFIRM_TTL_SEC,
    ) -> dict[str, Any]:
        """Atomically reserve 1 tank, 1 healer, 3 damage among voluntary interests.

        Enforces unique active reservation/commitment per `persona_id` across ALL opportunities so two
        competing opportunities can never double-book the same tank/healer/DPS. Accepted & active
        commitments remain strictly exclusive regardless of confirmation TTL (Issue #13).
        """
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Persist expiry sweep cleanly even if the subsequent reservation check returns early!
            self._expire_stale_in_tx(conn, ts)
            opp = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
            if not opp:
                conn.execute("COMMIT")
                return {"ok": False, "reason": "not_found"}
            if str(opp["state"]) == "expired":
                conn.execute("COMMIT")
                return {"ok": False, "reason": "expired"}
            if str(opp["state"]) != "recruiting" or int(opp["revision"]) != int(expected_revision):
                conn.execute("COMMIT")
                return {"ok": False, "reason": "revision_or_state_mismatch"}
            if int(opp["recruit_until"]) <= ts:
                conn.execute(
                    "UPDATE opportunities SET state='expired', outcome_reason='recruit_ttl_expired', ended_at=?, revision=revision+1 WHERE id=?",
                    (ts, opportunity_id),
                )
                conn.execute("COMMIT")
                return {"ok": False, "reason": "expired"}

            roles_needed: dict[str, int] = json.loads(str(opp["roles_json"]))
            opp_is_shadow = int(opp["is_shadow"])
            # Issue #13 & #15: Accepted/active commitments stay strictly exclusive regardless of reserved_until;
            # shadow reservations NEVER block live slots and vice versa!
            busy_rows = conn.execute(
                """
                SELECT DISTINCT oi.persona_id
                FROM opportunity_interests oi
                JOIN opportunities o ON o.id = oi.opportunity_id
                WHERE o.is_shadow = ?
                  AND (
                      oi.state IN ('accepted', 'active')
                      OR (oi.state = 'reserved' AND oi.reserved_until > ?)
                  )
                """,
                (opp_is_shadow, ts),
            ).fetchall()
            busy_personas = {str(r["persona_id"]) for r in busy_rows}

            # Fair deterministic order: earliest interest first (waiting age), then character_guid
            cand_rows = conn.execute(
                """
                SELECT * FROM opportunity_interests
                WHERE opportunity_id = ? AND state = 'interested' AND available_until > ?
                ORDER BY created_at ASC, character_guid ASC
                """,
                (opportunity_id, ts),
            ).fetchall()

            picked: list[dict[str, Any]] = []
            used_personas: set[str] = set(busy_personas)
            shortfalls: dict[str, int] = {}

            for role_name in ("tank", "healer", "damage"):
                need = int(roles_needed.get(role_name, 0))
                got = 0
                for r in cand_rows:
                    if got >= need:
                        break
                    pid = str(r["persona_id"])
                    if str(r["role"]) != role_name or pid in used_personas:
                        continue
                    used_personas.add(pid)
                    picked.append({
                        "interest_id": int(r["id"]),
                        "persona_id": pid,
                        "realm_id": int(r["realm_id"]),
                        "character_guid": int(r["character_guid"]),
                        "incarnation": int(r["incarnation"]),
                        "character_slug": str(r["character_slug"]),
                        "role": role_name,
                    })
                    got += 1
                if got < need:
                    shortfalls[role_name] = need - got

            if shortfalls:
                conn.execute("COMMIT")
                return {"ok": False, "reason": "insufficient_roles", "shortfalls": shortfalls}

            # Atomically advance opportunity to 'assembling' (revision + 1) with explicit confirm_until TTL
            res_until = ts + max(1, int(confirm_ttl_sec))
            new_rev = int(opp["revision"]) + 1
            conn.execute(
                "UPDATE opportunities SET state='assembling', confirm_until=?, revision=? WHERE id=? AND revision=?",
                (res_until, new_rev, opportunity_id, int(opp["revision"])),
            )
            for p in picked:
                conn.execute(
                    "UPDATE opportunity_interests SET state='reserved', reserved_until=?, opportunity_revision=?, updated_at=? WHERE id=?",
                    (res_until, new_rev, ts, p["interest_id"]),
                )
            conn.execute("COMMIT")
            return {
                "ok": True,
                "opportunity_id": opportunity_id,
                "revision": new_rev,
                "reserved_until": res_until,
                "confirm_until": res_until,
                "party": picked,
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def confirm_member(
        self,
        opportunity_id: int,
        *,
        expected_revision: int,
        persona_id: str,
        character_guid: int,
        realm_id: int = 1,
        incarnation: int = 1,
        accepted: bool = True,
        ack_token: str = "",
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Atomically confirm or decline a reserved party member (idempotent, revalidating char key & rev, Issue #13)."""
        ts = int(now_ts if now_ts is not None else time.time())
        clean_tok = str(ack_token or "").strip()[:64]
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_stale_in_tx(conn, ts)
            opp = conn.execute("SELECT * FROM opportunities WHERE id = ?", (int(opportunity_id),)).fetchone()
            if not opp:
                conn.execute("COMMIT")
                return {"ok": False, "reason": "not_found"}

            opp_state = str(opp["state"])
            opp_rev = int(opp["revision"])
            member = conn.execute(
                "SELECT * FROM opportunity_interests WHERE opportunity_id=? AND persona_id=?",
                (int(opportunity_id), persona_id),
            ).fetchone()
            if not member:
                conn.execute("COMMIT")
                return {"ok": False, "reason": "member_not_found"}

            # Revalidate exact character key (realm_id, character_guid, incarnation)
            if (
                int(member["character_guid"]) != int(character_guid)
                or int(member["realm_id"]) != int(realm_id)
                or int(member["incarnation"]) != int(incarnation)
            ):
                conn.execute("COMMIT")
                return {"ok": False, "reason": "character_key_mismatch"}

            m_state = str(member["state"])
            stored_tok = str(member["ack_token"] or "")

            # Idempotent duplicate accept (in assembling, confirmed, or active)
            if accepted and m_state in ("accepted", "active") and opp_state in ("assembling", "confirmed", "active"):
                if stored_tok and clean_tok and stored_tok != clean_tok:
                    conn.execute("COMMIT")
                    return {"ok": False, "reason": "ack_token_mismatch"}
                if int(expected_revision) <= opp_rev:
                    conn.execute("COMMIT")
                    return {
                        "ok": True,
                        "idempotent": True,
                        "state": opp_state,
                        "revision": opp_rev,
                        "all_confirmed": opp_state in ("confirmed", "active"),
                    }

            # Idempotent duplicate decline on already-cancelled opportunity
            if (not accepted) and m_state == "declined" and opp_state == "cancelled":
                if stored_tok and clean_tok and stored_tok != clean_tok:
                    conn.execute("COMMIT")
                    return {"ok": False, "reason": "ack_token_mismatch"}
                conn.execute("COMMIT")
                return {"ok": True, "idempotent": True, "accepted": False, "state": "cancelled", "revision": opp_rev, "all_confirmed": False}

            if opp_state == "expired":
                conn.execute("COMMIT")
                return {"ok": False, "reason": "confirm_ttl_expired"}

            if opp_state != "assembling" or opp_rev != int(expected_revision):
                conn.execute("COMMIT")
                return {"ok": False, "reason": f"revision_or_state_mismatch:{opp_state}:rev{opp_rev}"}

            if m_state not in ("reserved", "accepted"):
                conn.execute("COMMIT")
                return {"ok": False, "reason": f"member_not_reserved:{m_state}"}

            if not accepted:
                # Partial accept failure (even if this member previously accepted while still assembling!)
                conn.execute(
                    "UPDATE opportunity_interests SET state='declined', reserved_until=0, reason_code='member_declined', ack_token=?, updated_at=? WHERE id=?",
                    (clean_tok, ts, int(member["id"])),
                )
                conn.execute(
                    "UPDATE opportunity_interests SET state='released', reserved_until=0, reason_code='party_member_declined', updated_at=? "
                    "WHERE opportunity_id=? AND id != ? AND state IN ('reserved','accepted')",
                    (ts, int(opportunity_id), int(member["id"])),
                )
                new_rev = opp_rev + 1
                conn.execute(
                    "UPDATE opportunities SET state='cancelled', outcome_reason=?, confirm_until=0, ended_at=?, revision=? WHERE id=?",
                    (f"member_declined:{persona_id}"[:120], ts, new_rev, int(opportunity_id)),
                )
                conn.execute("COMMIT")
                return {"ok": True, "accepted": False, "state": "cancelled", "revision": new_rev, "all_confirmed": False}

            # Mark member accepted (clear reserved_until -> durable commitment, NOT bounded by confirmation TTL!)
            conn.execute(
                "UPDATE opportunity_interests SET state='accepted', reserved_until=0, confirmed_at=?, ack_token=?, updated_at=? WHERE id=?",
                (ts, clean_tok, ts, int(member["id"])),
            )
            counts = conn.execute(
                "SELECT SUM(CASE WHEN state='accepted' THEN 1 ELSE 0 END) AS acc, "
                "SUM(CASE WHEN state='reserved' THEN 1 ELSE 0 END) AS res "
                "FROM opportunity_interests WHERE opportunity_id=?",
                (int(opportunity_id),),
            ).fetchone()
            acc_n = int(counts["acc"] or 0)
            res_n = int(counts["res"] or 0)
            roles_needed: dict[str, int] = json.loads(str(opp["roles_json"]))
            total_needed = sum(int(v) for v in roles_needed.values())

            if res_n == 0 and acc_n == total_needed:
                new_rev = opp_rev + 1
                corr_key = f"opp:{int(opportunity_id)}:rev{new_rev}"
                # Deterministic correlation key + handoff grace window so lost native handoff responses reconcile cleanly!
                conn.execute(
                    "UPDATE opportunities SET state='confirmed', confirm_until=?, correlation_key=?, revision=? WHERE id=?",
                    (ts + DEFAULT_CONFIRM_TTL_SEC, corr_key, new_rev, int(opportunity_id)),
                )
                conn.execute("COMMIT")
                return {
                    "ok": True,
                    "accepted": True,
                    "state": "confirmed",
                    "revision": new_rev,
                    "correlation_key": corr_key,
                    "all_confirmed": True,
                    "confirmed_count": acc_n,
                    "needed_count": total_needed,
                }

            conn.execute("COMMIT")
            return {
                "ok": True,
                "accepted": True,
                "state": "assembling",
                "revision": opp_rev,
                "all_confirmed": False,
                "confirmed_count": acc_n,
                "needed_count": total_needed,
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def record_native_handoff(
        self,
        opportunity_id: int,
        *,
        expected_revision: int,
        native_state: str,
        native_queue_id: str = "",
        group_id: int = 0,
        native_run_id: str = "",
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Idempotently record authoritative native LFG queue/group/run handoff from 'confirmed' -> 'active'."""
        ts = int(now_ts if now_ts is not None else time.time())
        n_state = str(native_state or "").strip().lower()
        q_id = str(native_queue_id or "").strip()[:64]
        r_id = str(native_run_id or "").strip()[:64]
        g_id = max(0, int(group_id or 0))
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            opp = conn.execute("SELECT * FROM opportunities WHERE id = ?", (int(opportunity_id),)).fetchone()
            if not opp:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "not_found"}

            if int(opp["is_shadow"]) == 1:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "shadow_opportunity"}

            opp_state = str(opp["state"])
            opp_rev = int(opp["revision"])

            # Late handoff after cancellation/expiry -> instruct caller to tear down any orphaned native queue!
            if opp_state in ("cancelled", "expired", "aborted"):
                conn.execute("ROLLBACK")
                return {
                    "ok": False,
                    "reason": f"late_handoff_on_{opp_state}",
                    "compensate": "leave_queue",
                }

            # Idempotent forward-only replay when already 'active' (never overwrite non-empty IDs or go backwards!)
            if opp_state == "active" and n_state in NATIVE_STATE_ORDER:
                st_q = str(opp["native_queue_id"] or "")
                st_r = str(opp["native_run_id"] or "")
                st_g = int(opp["group_id"] or 0)
                st_ns = str(opp["native_state"] or "")
                # Reject conflicting non-empty native IDs!
                if (q_id and st_q and q_id != st_q) or (r_id and st_r and r_id != st_r) or (g_id > 0 and st_g > 0 and g_id != st_g):
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "native_identity_conflict"}
                has_match = (q_id and q_id == st_q) or (r_id and r_id == st_r) or (g_id > 0 and g_id == st_g)
                if not has_match:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "native_identity_mismatch"}
                if NATIVE_STATE_ORDER.get(n_state, 0) < NATIVE_STATE_ORDER.get(st_ns, 0):
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": f"backward_native_state:{st_ns}->{n_state}"}
                # Fill only currently-empty native IDs and advance native_state forward
                conn.execute(
                    "UPDATE opportunities SET native_state=?, "
                    "native_queue_id=CASE WHEN native_queue_id='' THEN ? ELSE native_queue_id END, "
                    "group_id=CASE WHEN group_id=0 THEN ? ELSE group_id END, "
                    "native_run_id=CASE WHEN native_run_id='' THEN ? ELSE native_run_id END "
                    "WHERE id=?",
                    (n_state, q_id, g_id, r_id, int(opportunity_id)),
                )
                conn.execute("COMMIT")
                return {"ok": True, "idempotent": True, "state": "active", "revision": opp_rev}

            if opp_state != "confirmed" or opp_rev != int(expected_revision):
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"revision_or_state_mismatch:{opp_state}:rev{opp_rev}"}

            if n_state in ("rejected", "failed", "cancelled"):
                conn.execute(
                    "UPDATE opportunity_interests SET state='released', reserved_until=0, reason_code=?, updated_at=? "
                    "WHERE opportunity_id=? AND state IN ('reserved','accepted','active')",
                    (f"native_{n_state}"[:64], ts, int(opportunity_id)),
                )
                new_rev = opp_rev + 1
                conn.execute(
                    "UPDATE opportunities SET state='cancelled', native_state=?, outcome_reason=?, "
                    "confirm_until=0, ended_at=?, revision=? WHERE id=?",
                    (n_state, f"native_{n_state}", ts, new_rev, int(opportunity_id)),
                )
                conn.execute("COMMIT")
                return {"ok": True, "state": "cancelled", "revision": new_rev}

            if n_state not in NATIVE_STATE_ORDER:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"invalid_native_state:{n_state}"}
            if not q_id and not r_id and g_id <= 0:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "missing_native_identifier"}

            new_rev = opp_rev + 1
            conn.execute(
                "UPDATE opportunities SET state='active', native_state=?, native_queue_id=?, group_id=?, "
                "native_run_id=?, confirm_until=0, started_at=?, revision=? WHERE id=?",
                (n_state, q_id, g_id, r_id, ts, new_rev, int(opportunity_id)),
            )
            conn.execute(
                "UPDATE opportunity_interests SET state='active', reserved_until=0, opportunity_revision=?, updated_at=? "
                "WHERE opportunity_id=? AND state='accepted'",
                (new_rev, ts, int(opportunity_id)),
            )
            conn.execute("COMMIT")
            return {
                "ok": True,
                "idempotent": False,
                "state": "active",
                "revision": new_rev,
                "native_queue_id": q_id,
                "group_id": g_id,
                "native_run_id": r_id,
            }
        except sqlite3.IntegrityError:
            if conn.in_transaction:
                conn.rollback()
            return {"ok": False, "reason": "duplicate_native_run_id"}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def record_native_outcome(
        self,
        opportunity_id: int,
        *,
        native_run_id: str = "",
        native_queue_id: str = "",
        group_id: int = 0,
        outcome: str,
        outcome_reason: str = "",
        expected_revision: int | None = None,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Record terminal dungeon outcome ONLY from authoritative native run completion/abort (Issue #13)."""
        ts = int(now_ts if now_ts is not None else time.time())
        r_id = str(native_run_id or "").strip()[:64]
        q_id = str(native_queue_id or "").strip()[:64]
        g_id = max(0, int(group_id or 0))
        out_s = str(outcome or "").strip().lower()
        if out_s not in ("completed", "aborted", "failed", "disbanded"):
            return {"ok": False, "reason": f"invalid_outcome:{out_s}"}

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            opp = conn.execute("SELECT * FROM opportunities WHERE id = ?", (int(opportunity_id),)).fetchone()
            if not opp:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "not_found"}

            if int(opp["is_shadow"]) == 1:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "shadow_opportunity"}

            opp_state = str(opp["state"])
            opp_rev = int(opp["revision"])
            stored_run = str(opp["native_run_id"] or "")
            stored_q = str(opp["native_queue_id"] or "")
            stored_g = int(opp["group_id"] or 0)
            target_state = "completed" if out_s == "completed" else "aborted"

            # Idempotent duplicate terminal callback with matching run ID
            if opp_state == target_state and (
                (r_id and stored_run == r_id)
                or (target_state == "aborted" and not stored_run and ((q_id and q_id == stored_q) or (g_id > 0 and g_id == stored_g)))
            ):
                conn.execute("COMMIT")
                return {"ok": True, "idempotent": True, "state": opp_state, "revision": opp_rev}

            # Late genuine completion after reconcile aborted a run as missing -> update opportunity ledger ONLY!
            if (
                out_s == "completed"
                and opp_state == "aborted"
                and str(opp["outcome_reason"]) == "reconciled_missing_native_run"
                and stored_run
                and r_id == stored_run
            ):
                new_rev = opp_rev + 1
                reason_txt = (outcome_reason or "late_native_completed")[:120]
                conn.execute(
                    "UPDATE opportunities SET state='completed', native_state='completed', outcome_reason=?, "
                    "ended_at=?, revision=? WHERE id=?",
                    (reason_txt, ts, new_rev, int(opportunity_id)),
                )
                conn.execute("COMMIT")
                return {"ok": True, "idempotent": False, "late_reconciled": True, "state": "completed", "revision": new_rev}

            if opp_state not in ("active", "cancelling"):
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"not_active:{opp_state}"}
            if expected_revision is not None and opp_rev != int(expected_revision):
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"revision_mismatch:{opp_rev}"}

            # Strict native identity binding (P0.2):
            # - 'completed' REQUIRES a non-empty stored_run that matches r_id exactly!
            # - abort/failed/disbanded without stored_run MUST match stored_q or stored_g!
            if out_s == "completed":
                if not stored_run or not r_id or stored_run != r_id:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "native_run_id_required_and_must_match"}
            else:
                if stored_run:
                    if not r_id or stored_run != r_id:
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": f"native_run_id_mismatch:{stored_run}!={r_id}"}
                else:
                    matched_alt = (q_id and stored_q and q_id == stored_q) or (g_id > 0 and stored_g > 0 and g_id == stored_g)
                    if not matched_alt:
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": "native_queue_or_group_id_must_match"}

            new_rev = opp_rev + 1
            member_final = "completed" if out_s == "completed" else "released"
            reason_txt = (outcome_reason or f"native_{out_s}")[:120]
            conn.execute(
                "UPDATE opportunities SET state=?, native_state=?, outcome_reason=?, "
                "ended_at=?, revision=? WHERE id=?",
                (target_state, out_s, reason_txt, ts, new_rev, int(opportunity_id)),
            )
            conn.execute(
                "UPDATE opportunity_interests SET state=?, reserved_until=0, reason_code=?, updated_at=? "
                "WHERE opportunity_id=? AND state IN ('active','accepted','reserved')",
                (member_final, reason_txt[:64], ts, int(opportunity_id)),
            )
            conn.execute("COMMIT")
            return {"ok": True, "idempotent": False, "state": target_state, "revision": new_rev, "native_run_id": stored_run or r_id}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def reconcile_native_state(
        self,
        snapshot: NativeReconcileSnapshot | None = None,
        *,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Fail-closed crash & restart reconciliation against an authoritative NativeReconcileSnapshot (Issue #13)."""
        ts = int(now_ts if now_ts is not None else time.time())
        snap = snapshot if snapshot is not None else NativeReconcileSnapshot(authoritative=False)

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            exp_stats = self._expire_stale_in_tx(conn, ts)

            # Shadow confirmed rows past confirm_until get a distinct shadow label (never pollute live crash metrics)
            shadow_expired = 0
            for sr in conn.execute(
                "SELECT id FROM opportunities WHERE is_shadow=1 AND state='confirmed' AND confirm_until > 0 AND confirm_until <= ?",
                (ts,),
            ).fetchall():
                oid = int(sr["id"])
                conn.execute(
                    "UPDATE opportunity_interests SET state='released', reserved_until=0, "
                    "reason_code='shadow_confirm_expired', updated_at=? WHERE opportunity_id=? AND state IN ('reserved','accepted')",
                    (ts, oid),
                )
                shadow_expired += conn.execute(
                    "UPDATE opportunities SET state='expired', outcome_reason='shadow_confirm_expired', "
                    "confirm_until=0, ended_at=?, revision=revision+1 WHERE id=?",
                    (ts, oid),
                ).rowcount

            # Fail CLOSED: if native snapshot is NOT authoritative, NEVER abort live active runs or cancel live confirmed!
            if not snap.authoritative:
                conn.execute("COMMIT")
                return {
                    **exp_stats,
                    "authoritative": False,
                    "shadow_confirmed_expired": shadow_expired,
                    "adopted_confirmed": 0,
                    "lost_confirmed_released": 0,
                    "aborted_missing_native": 0,
                }

            groups = snap.active_group_ids
            queues = snap.active_queue_ids
            runs = snap.active_run_ids
            corr_map = snap.correlation_map or {}

            # 1. Reconcile live 'confirmed' opportunities via deterministic correlation_key:
            #    Adopt into 'active' if native created it (lost response!); cancel ONLY if absent & past confirm_until!
            adopted = 0
            lost_confirmed = 0
            for r in conn.execute(
                "SELECT id, revision, correlation_key, confirm_until FROM opportunities WHERE is_shadow=0 AND state='confirmed'"
            ).fetchall():
                oid = int(r["id"])
                ckey = str(r["correlation_key"] or "")
                found = corr_map.get(ckey) if ckey else None
                if isinstance(found, dict):
                    n_st = str(found.get("native_state") or "queued").lower()
                    q_id = str(found.get("native_queue_id") or "")[:64]
                    r_id = str(found.get("native_run_id") or "")[:64]
                    g_id = max(0, int(found.get("group_id") or 0))
                    new_rev = int(r["revision"]) + 1
                    conn.execute(
                        "UPDATE opportunities SET state='active', native_state=?, native_queue_id=?, "
                        "group_id=?, native_run_id=?, confirm_until=0, started_at=?, revision=? WHERE id=?",
                        (n_st, q_id, g_id, r_id, ts, new_rev, oid),
                    )
                    conn.execute(
                        "UPDATE opportunity_interests SET state='active', reserved_until=0, "
                        "opportunity_revision=?, updated_at=? WHERE opportunity_id=? AND state='accepted'",
                        (new_rev, ts, oid),
                    )
                    adopted += 1
                elif snap.correlation_map is not None and int(r["confirm_until"]) > 0 and int(r["confirm_until"]) <= ts:
                    conn.execute(
                        "UPDATE opportunity_interests SET state='released', reserved_until=0, "
                        "reason_code='crash_lost_native_handoff', updated_at=? "
                        "WHERE opportunity_id=? AND state IN ('reserved','accepted')",
                        (ts, oid),
                    )
                    lost_confirmed += conn.execute(
                        "UPDATE opportunities SET state='cancelled', outcome_reason='crash_lost_native_handoff', "
                        "confirm_until=0, ended_at=?, revision=revision+1 WHERE id=?",
                        (ts, oid),
                    ).rowcount

            # 2. Reconcile live 'active'/'cancelling' opportunities ONLY when every relevant native ID set is known (non-None!)
            aborted_missing = 0
            for r in conn.execute(
                "SELECT id, state, group_id, native_queue_id, native_run_id FROM opportunities "
                "WHERE state IN ('active','cancelling') AND is_shadow=0"
            ).fetchall():
                oid = int(r["id"])
                gid = int(r["group_id"] or 0)
                qid = str(r["native_queue_id"] or "").strip()
                rid = str(r["native_run_id"] or "").strip()

                # Fail closed per-identifier: if an identifier is set on the opportunity but its corresponding
                # authoritative set wasn't supplied (None), treat as unknown and DO NOT abort!
                unknown_check = (
                    (gid > 0 and groups is None)
                    or (bool(qid) and queues is None)
                    or (bool(rid) and runs is None)
                )
                if unknown_check:
                    continue

                still_alive = (
                    (gid > 0 and groups is not None and gid in groups)
                    or (bool(qid) and queues is not None and qid in queues)
                    or (bool(rid) and runs is not None and rid in runs)
                )
                if not still_alive:
                    conn.execute(
                        "UPDATE opportunity_interests SET state='released', reserved_until=0, "
                        "reason_code='reconciled_missing_native_run', updated_at=? "
                        "WHERE opportunity_id=? AND state IN ('active','accepted','reserved')",
                        (ts, oid),
                    )
                    final_st = "cancelled" if str(r["state"]) == "cancelling" else "aborted"
                    aborted_missing += conn.execute(
                        "UPDATE opportunities SET state=?, native_state='missing', "
                        "outcome_reason='reconciled_missing_native_run', ended_at=?, revision=revision+1 WHERE id=?",
                        (final_st, ts, oid),
                    ).rowcount

            conn.execute("COMMIT")
            return {
                **exp_stats,
                "authoritative": True,
                "shadow_confirmed_expired": shadow_expired,
                "adopted_confirmed": adopted,
                "lost_confirmed_released": lost_confirmed,
                "aborted_missing_native": aborted_missing,
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def release_party(
        self,
        opportunity_id: int,
        reason: str,
        *,
        cancel_opp: bool = True,
        native_confirmed: bool = False,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Saga compensation: release reservations; for live active/confirmed runs without native_confirmed,
        enter 'cancelling' so members remain exclusive until native teardown completes (Issue #13)."""
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            opp = conn.execute("SELECT state, is_shadow, revision FROM opportunities WHERE id=?", (int(opportunity_id),)).fetchone()
            if not opp:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "not_found"}
            opp_state = str(opp["state"])
            is_live_native = int(opp["is_shadow"]) == 0 and opp_state in ("confirmed", "active")

            if is_live_native and not native_confirmed:
                new_rev = int(opp["revision"]) + 1
                conn.execute(
                    "UPDATE opportunities SET state='cancelling', outcome_reason=?, revision=? WHERE id=?",
                    (reason[:120], new_rev, int(opportunity_id)),
                )
                conn.execute("COMMIT")
                return {"ok": True, "state": "cancelling", "revision": new_rev, "awaiting_native_teardown": True}

            conn.execute(
                "UPDATE opportunity_interests SET state='released', reserved_until=0, reason_code=?, updated_at=? "
                "WHERE opportunity_id=? AND state IN ('reserved','accepted','active')",
                (reason[:64], ts, int(opportunity_id)),
            )
            if cancel_opp:
                conn.execute(
                    "UPDATE opportunities SET state='cancelled', outcome_reason=?, confirm_until=0, ended_at=?, revision=revision+1 "
                    "WHERE id=? AND state IN ('recruiting','assembling','confirmed','active','cancelling')",
                    (reason[:120], ts, int(opportunity_id)),
                )
            conn.execute("COMMIT")
            return {"ok": True, "state": "cancelled", "awaiting_native_teardown": False}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()


def _read_existing_enrollments_readonly(db_path: Path) -> dict[int, tuple[str, bool]]:
    """Strictly read-only lookup of existing persona enrollments; NEVER creates or mutates db_path (Issue #15)."""
    if not db_path.is_file():
        return {}
    out: dict[int, tuple[str, bool]] = {}
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "companion_personas" not in tables or "companion_persona_characters" not in tables:
            return {}
        rows = conn.execute(
            """
            SELECT c.character_guid, p.persona_id, p.enrolled_coop
            FROM companion_persona_characters c
            JOIN companion_personas p ON p.persona_id = c.persona_id
            """
        ).fetchall()
        for r in rows:
            out[int(r["character_guid"])] = (str(r["persona_id"]), bool(int(r["enrolled_coop"])))
    except sqlite3.Error as exc:
        logger.warning("Read-only persona enrollment lookup failed for %s: %s", db_path, exc)
        return {}
    finally:
        if conn is not None:
            conn.close()
    return out


def read_shadow_artifact(path: Path, *, now_ts: int | None = None, max_age_sec: int = 3600) -> dict[str, Any]:
    """Safely load and validate phase2_shadow.json, flagging missing, corrupted, or stale artifacts (Issue #15)."""
    ts = int(now_ts if now_ts is not None else time.time())
    if not path.is_file():
        return {"ok": False, "status": "missing", "stale": True, "data": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not raw.get("is_shadow"):
            return {"ok": False, "status": "corrupted", "stale": True, "data": None}
        upd = str(raw.get("updated_at") or "")
        age_sec: int | None = None
        stale = False
        if upd:
            dt = datetime.strptime(upd, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age_sec = max(0, ts - int(dt.timestamp()))
            stale = age_sec > int(max_age_sec)
        else:
            stale = True
        return {
            "ok": not stale,
            "status": "stale" if stale else "fresh",
            "stale": stale,
            "age_sec": age_sec,
            "data": raw,
        }
    except Exception as exc:
        logger.warning("Failed reading shadow artifact %s: %s: %s", path, type(exc).__name__, exc)
        return {"ok": False, "status": "corrupted", "stale": True, "data": None}


def run_shadow_match_snapshot(
    world_ctx: dict[str, Any],
    *,
    db_path: Path,
    runtime_dir: Path,
    vitals: dict[str, Any] | None = None,
    raid_stage: int = 0,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Pure shadow matchmaking pass over current live roster -> runtime/phase2_shadow.json ONLY.

    Guarantees (Issues #7 & #15 + Card II-02/II-04):
    - STRICTLY READ-ONLY on db_path: NEVER mutates PersonaRegistry, opportunities, MySQL,
      reservations, schedules, memory, or active_plans.json.
    - Protects core-wonky and core-logan by default (`enrolled_coop = False`).
    - Clearly labels every output as `is_shadow = True` and `synthetic_feasibility = True`.
    """
    ts = int(now_ts if now_ts is not None else time.time())
    existing_enrollments = _read_existing_enrollments_readonly(Path(db_path))
    companions = world_ctx.get("companions") or {}

    known = set(profiles())
    v = vitals or {}
    mem_avail_mib = float(v["mem_avail_gib"]) * 1024.0 if v.get("mem_avail_gib") is not None else None
    tick_p99 = float(v["tick_p99"]) if v.get("tick_p99") is not None else None
    # Issue #15: Shadow pass reads persisted headroom state READ-ONLY and NEVER writes headroom_state.json!
    prev_paused = False
    hr_path = runtime_dir / "headroom_state.json"
    try:
        if hr_path.is_file():
            prev_paused = bool(json.loads(hr_path.read_text(encoding="utf-8")).get("paused", False))
    except Exception as exc:
        logger.warning("Corrupt headroom state at %s during shadow read (%s); assuming paused", hr_path, exc)
        prev_paused = True
    headroom = check_resource_headroom(
        mem_avail_mib=mem_avail_mib,
        tick_p99_ms=tick_p99,
        active_managed_groups=0,
        max_managed_groups=DEFAULT_MAX_GROUPS,
        prev_paused=prev_paused,
    )

    shadow_dungeons: list[dict[str, Any]] = []
    for spec in DUNGEON_CATALOG:
        if not spec.supported:
            continue
        pool_by_role: dict[str, list[dict[str, Any]]] = {"tank": [], "healer": [], "damage": []}
        excluded_core = 0
        missing_identity = 0
        for slug, comp in sorted(companions.items()):
            if slug not in known or not comp.get("online"):
                continue
            guid = int(comp["guid"])
            shift_group = str(comp.get("shift_group") or "")
            if guid in existing_enrollments:
                persona_id, enrolled_coop = existing_enrollments[guid]
            else:
                missing_identity += 1
                persona_id = str(comp.get("persona_id") or persona_id_for_slug(slug))
                enrolled_coop = shift_group.strip().lower() not in PROTECTED_OWNER_GROUPS
            if not enrolled_coop:
                excluded_core += 1
                continue
            prof = profile(slug)
            roles = infer_viable_roles(prof, int(comp["level"]))
            ok, _reason = check_character_eligibility(
                level=int(comp["level"]),
                faction="alliance",
                viable_roles=roles,
                dungeon=spec,
                raid_stage=raid_stage,
                available_minutes=90,
                allow_pilot_shadow=True,
            )
            if not ok:
                continue
            primary_r = roles[0]
            pool_by_role[primary_r].append({
                "persona_id": persona_id,
                "slug": slug,
                "name": comp["name"],
                "level": int(comp["level"]),
                "role": primary_r,
            })

        # Check whether a full 1T / 1H / 3D voluntary party is synthetically feasible right now
        roles_req = dict(spec.roles)
        shortfalls = {
            r: max(0, int(roles_req.get(r, 0)) - len(pool_by_role[r]))
            for r in ("tank", "healer", "damage")
        }
        feasible = all(v == 0 for v in shortfalls.values())
        shadow_dungeons.append({
            "dungeon_id": spec.dungeon_id,
            "name": spec.name,
            "map_id": spec.map_id,
            "level_band": f"{spec.min_level}-{spec.max_level}",
            "pilot_enabled_live": spec.dungeon_id in enabled_dungeon_ids(),
            "synthetic_feasibility": True,
            "eligible_counts": {r: len(pool_by_role[r]) for r in ("tank", "healer", "damage")},
            "shortfalls": shortfalls,
            "feasible_now": feasible,
            "excluded_protected_core": excluded_core,
            "missing_identity_fallback_count": missing_identity,
            "candidates": pool_by_role,
        })

    out = {
        "updated_at": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "is_shadow": True,
        "synthetic_feasibility": True,
        "mutated_live_state": False,
        "coop_enabled_live": coop_enabled(),
        "enabled_dungeons_live": sorted(enabled_dungeon_ids()),
        "headroom": headroom,
        "work_order_opportunities": len(adapt_work_orders_as_opportunities(world_ctx.get("work_orders") or [])),
        "shadow_dungeons": shadow_dungeons,
    }
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / "phase2_shadow.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return out
