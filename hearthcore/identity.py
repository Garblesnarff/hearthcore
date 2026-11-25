"""Phase II Identity Seam (Card II-01): Persona <-> Character (realm_id, guid, incarnation).

Design guarantees (docs/PHASE_II_LIVING_GUILD.md Section 4):
- Every currently enrolled Hearthguard companion starts as a 1-character persona (`per_<slug>`).
- Existing character slugs remain full display aliases; zero destructive rekeying.
- Enforces at most ONE active character per persona when checking admissions/readiness so
  future alts/twinks can never be double-counted in the same raid or dungeon group.
- Core owner groups (`core-wonky`, `core-logan`) default to `enrolled_coop = False` (protected).
- No account creation or alt creation in Phase II.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROTECTED_OWNER_GROUPS = frozenset({"core-wonky", "core-logan"})


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


def persona_id_for_slug(slug: str) -> str:
    return f"per_{slug.strip().lower()}"


class PersonaRegistry:
    """SQLite read/projection store for Persona <-> Character mapping & single-active-char enforcement."""

    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("""
                CREATE TABLE IF NOT EXISTS personas (
                    persona_id TEXT PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE,
                    faction TEXT NOT NULL DEFAULT 'alliance',
                    owner_group TEXT NOT NULL DEFAULT 'guild',
                    enrolled_coop INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'active',
                    updated_at INTEGER NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS persona_characters (
                    realm_id INTEGER NOT NULL DEFAULT 1,
                    character_guid INTEGER NOT NULL,
                    incarnation INTEGER NOT NULL DEFAULT 1,
                    persona_id TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    slot_kind TEXT NOT NULL DEFAULT 'main',
                    active INTEGER NOT NULL DEFAULT 1,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (realm_id, character_guid, incarnation)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_pc_persona ON persona_characters(persona_id, active)")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def sync_from_companions(
        self,
        companions: dict[str, dict[str, Any]],
        *,
        realm_id: int = 1,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Idempotently backfill/sync authoritative companions without overwriting explicit coop enrollment."""
        ts = int(now_ts if now_ts is not None else time.time())
        synced = 0
        quarantined: list[str] = []

        with self._conn() as db:
            for slug, comp in sorted(companions.items()):
                guid = int(comp["guid"])
                pid = persona_id_for_slug(slug)
                ogroup = str(comp.get("shift_group") or "guild")
                default_coop = 0 if ogroup in PROTECTED_OWNER_GROUPS else 1

                # Detect GUID reuse conflict honestly -> quarantine rather than silently reassign
                existing_char = db.execute(
                    "SELECT persona_id, slug FROM persona_characters WHERE realm_id=? AND character_guid=? AND incarnation=1",
                    (realm_id, guid),
                ).fetchone()
                if existing_char and str(existing_char["persona_id"]) != pid:
                    quarantined.append(f"guid {guid} mapped to {existing_char['persona_id']} != {pid}")
                    continue

                db.execute(
                    """
                    INSERT INTO personas (persona_id, slug, faction, owner_group, enrolled_coop, state, updated_at)
                    VALUES (?, ?, 'alliance', ?, ?, 'active', ?)
                    ON CONFLICT(persona_id) DO UPDATE SET
                        owner_group = excluded.owner_group,
                        updated_at = excluded.updated_at
                    """,
                    (pid, slug, ogroup, default_coop, ts),
                )
                db.execute(
                    """
                    INSERT INTO persona_characters (realm_id, character_guid, incarnation, persona_id, slug, slot_kind, active, updated_at)
                    VALUES (?, ?, 1, ?, ?, 'main', 1, ?)
                    ON CONFLICT(realm_id, character_guid, incarnation) DO UPDATE SET
                        slug = excluded.slug,
                        updated_at = excluded.updated_at
                    """,
                    (realm_id, guid, pid, slug, ts),
                )
                synced += 1

        return {"synced": synced, "quarantined": quarantined}

    def register_alt(
        self,
        *,
        persona_id: str,
        slug: str,
        character_guid: int,
        realm_id: int = 1,
        incarnation: int = 1,
        slot_kind: str = "alt",
        now_ts: int | None = None,
    ) -> None:
        """Register an additional character under an existing persona (used by synthetic multi-char tests)."""
        ts = int(now_ts if now_ts is not None else time.time())
        with self._conn() as db:
            db.execute(
                """
                INSERT INTO persona_characters (realm_id, character_guid, incarnation, persona_id, slug, slot_kind, active, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(realm_id, character_guid, incarnation) DO UPDATE SET
                    persona_id = excluded.persona_id,
                    slug = excluded.slug,
                    slot_kind = excluded.slot_kind,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (realm_id, int(character_guid), int(incarnation), persona_id, slug.lower(), slot_kind, ts),
            )

    def persona_for_guid(self, character_guid: int, realm_id: int = 1) -> PersonaRecord | None:
        with self._conn() as db:
            row = db.execute(
                """
                SELECT p.persona_id, p.slug, p.faction, p.owner_group, p.enrolled_coop, p.state
                FROM persona_characters pc
                JOIN personas p ON p.persona_id = pc.persona_id
                WHERE pc.realm_id = ? AND pc.character_guid = ? AND pc.active = 1
                ORDER BY pc.incarnation DESC LIMIT 1
                """,
                (realm_id, int(character_guid)),
            ).fetchone()
            if not row:
                return None
            return PersonaRecord(
                persona_id=str(row["persona_id"]),
                slug=str(row["slug"]),
                faction=str(row["faction"]),
                owner_group=str(row["owner_group"]),
                enrolled_coop=bool(row["enrolled_coop"]),
                state=str(row["state"]),
            )

    def distinct_personas(self, guids: list[int], realm_id: int = 1) -> list[tuple[str, int]]:
        """Filter a list of character GUIDs to at most ONE character per distinct persona."""
        seen: set[str] = set()
        out: list[tuple[str, int]] = []
        for g in guids:
            rec = self.persona_for_guid(g, realm_id=realm_id)
            pid = rec.persona_id if rec else f"unmapped_{g}"
            if pid not in seen:
                seen.add(pid)
                out.append((pid, g))
        return out
