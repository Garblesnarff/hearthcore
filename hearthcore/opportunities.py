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
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    except Exception:
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
    except OSError:
        pass
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
                    estimated_minutes INTEGER NOT NULL DEFAULT 45,
                    group_id INTEGER NOT NULL DEFAULT 0,
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
                    character_guid INTEGER NOT NULL,
                    character_slug TEXT NOT NULL,
                    role TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reason_code TEXT NOT NULL DEFAULT '',
                    available_until INTEGER NOT NULL,
                    reserved_until INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(opportunity_id, persona_id)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_oi_persona_state ON opportunity_interests(persona_id, state)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_opp_state ON opportunities(state, is_shadow)")

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
                    opportunity_id, opportunity_revision, persona_id, character_guid, character_slug,
                    role, state, reason_code, available_until, reserved_until, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,0,?,?)
                ON CONFLICT(opportunity_id, persona_id) DO UPDATE SET
                    character_guid = excluded.character_guid,
                    role = excluded.role,
                    state = CASE WHEN state IN ('withdrawn','expired','declined') THEN 'interested' ELSE state END,
                    reason_code = excluded.reason_code,
                    available_until = excluded.available_until,
                    updated_at = excluded.updated_at
                """,
                (
                    int(opportunity_id), int(expected_revision), persona_id, int(character_guid),
                    character_slug.lower(), role.lower(), "interested", reason_code[:64],
                    int(available_until), ts, ts,
                ),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def withdraw_interest(self, opportunity_id: int, persona_id: str, reason: str = "withdrawn", now_ts: int | None = None) -> None:
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE opportunity_interests SET state='withdrawn', reason_code=?, reserved_until=0, updated_at=? "
                "WHERE opportunity_id=? AND persona_id=? AND state IN ('interested','reserved')",
                (reason[:64], ts, int(opportunity_id), persona_id),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def expire_stale(self, now_ts: int) -> dict[str, int]:
        """Expire past-TTL recruiting opportunities & stale reservations cleanly."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            c1 = conn.execute(
                "UPDATE opportunity_interests SET state='expired', reserved_until=0, updated_at=? "
                "WHERE state='reserved' AND reserved_until > 0 AND reserved_until <= ?",
                (now_ts, now_ts),
            ).rowcount
            c2 = conn.execute(
                "UPDATE opportunity_interests SET state='expired', updated_at=? "
                "WHERE state='interested' AND available_until <= ?",
                (now_ts, now_ts),
            ).rowcount
            c3 = conn.execute(
                "UPDATE opportunities SET state='expired', outcome_reason='recruit_ttl_expired', revision=revision+1 "
                "WHERE state='recruiting' AND recruit_until <= ?",
                (now_ts,),
            ).rowcount
            conn.execute("COMMIT")
            return {"expired_reservations": c1, "expired_interests": c2, "expired_opportunities": c3}
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

        Enforces unique active reservation per `persona_id` across ALL opportunities so two
        competing opportunities can never double-book the same tank/healer/DPS.
        """
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Clear expired reservations first inside the transaction
            conn.execute(
                "UPDATE opportunity_interests SET state='expired', reserved_until=0, updated_at=? "
                "WHERE state='reserved' AND reserved_until > 0 AND reserved_until <= ?",
                (ts, ts),
            )
            opp = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
            if not opp:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "not_found"}
            if str(opp["state"]) != "recruiting" or int(opp["revision"]) != int(expected_revision):
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "revision_or_state_mismatch"}
            if int(opp["recruit_until"]) <= ts:
                conn.execute(
                    "UPDATE opportunities SET state='expired', outcome_reason='recruit_ttl_expired', revision=revision+1 WHERE id=?",
                    (opportunity_id,),
                )
                conn.execute("COMMIT")
                return {"ok": False, "reason": "expired"}

            roles_needed: dict[str, int] = json.loads(str(opp["roles_json"]))
            opp_is_shadow = int(opp["is_shadow"])
            # Issue #15: Strict shadow/live isolation — shadow reservations NEVER block live slots and vice versa!
            busy_rows = conn.execute(
                """
                SELECT DISTINCT oi.persona_id
                FROM opportunity_interests oi
                JOIN opportunities o ON o.id = oi.opportunity_id
                WHERE o.is_shadow = ?
                  AND oi.state IN ('reserved', 'accepted', 'active')
                  AND (oi.reserved_until = 0 OR oi.reserved_until > ?)
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
                        "character_guid": int(r["character_guid"]),
                        "character_slug": str(r["character_slug"]),
                        "role": role_name,
                    })
                    got += 1
                if got < need:
                    shortfalls[role_name] = need - got

            if shortfalls:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "insufficient_roles", "shortfalls": shortfalls}

            # Atomically advance opportunity to 'assembling' (revision + 1) and reserve all 5 slots
            res_until = ts + int(confirm_ttl_sec)
            new_rev = int(opp["revision"]) + 1
            conn.execute(
                "UPDATE opportunities SET state='assembling', revision=? WHERE id=? AND revision=?",
                (new_rev, opportunity_id, int(opp["revision"])),
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
                "party": picked,
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def release_party(self, opportunity_id: int, reason: str, *, cancel_opp: bool = True, now_ts: int | None = None) -> None:
        """Saga compensation: release all reservations on partial invite failure, recall, or toggle-off."""
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE opportunity_interests SET state='released', reserved_until=0, reason_code=?, updated_at=? "
                "WHERE opportunity_id=? AND state IN ('reserved','accepted','active')",
                (reason[:64], ts, int(opportunity_id)),
            )
            if cancel_opp:
                conn.execute(
                    "UPDATE opportunities SET state='cancelled', outcome_reason=?, revision=revision+1 "
                    "WHERE id=? AND state IN ('recruiting','assembling','active')",
                    (reason[:120], int(opportunity_id)),
                )
            conn.execute("COMMIT")
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
    except sqlite3.Error:
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
    except Exception:
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
    headroom = evaluate_persisted_headroom(
        runtime_dir / "headroom_state.json",
        mem_avail_mib=mem_avail_mib,
        tick_p99_ms=tick_p99,
        active_managed_groups=0,
        max_managed_groups=DEFAULT_MAX_GROUPS,
        now_ts=ts,
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
