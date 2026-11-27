"""Phase II Voluntary Party Continuation, Goal Resolution & Bounded Rerun Decisions (Issue #19).

Design guarantees:
- A completed dungeon run is NOT equivalent to every participant fulfilling their personal ambition:
  per-member goals (`loot_item`, `dungeon_quest`, `practice`, `social_help`) resolve independently
  strictly from idempotent, run-bound, full-identity native events (`run_goal_events` deduplication;
  zero fabricated drops/rewards; valid loot attempt with no drop stays `unfulfilled` with
  `technical_failure=False`, cleanly separated from explicit `technical_failure` events).
- Continuation decisions open ONLY at explicit safe boundaries (`run_completed`, `wipe_recovery`,
  `leader_departure`) with `safe_boundary=True` and `in_combat=False` (cross-checked against
  `world_ctx` when supplied). Runs off the game thread; routine tactics stay 100% native.
- Wipe retries (`max_wipe_retries <= 3`, default 2), technical failures, and consecutive rerun
  chain depth (`max_consecutive_reruns <= 3`, default 2) are DERIVED AND PERSISTED server-side
  from DB history — never bypassable by caller arguments. Only ONE `run_completed` boundary may
  ever open per `(opportunity_id, native_run_id)`.
- Resolution uses an atomic `open -> resolving` CAS lease (`lease_epoch` + `resolving_until`),
  crash recovery (`reconcile_stale_boundaries`), idempotent `handoff_key` + immediate
  `rerun_opportunity_id` persistence, exception rollback, and strict `rowcount == 1` CAS commit.
- Owner recalls and raid readiness overrides are stored in a dedicated `continuation_overrides`
  table (supporting multiple concurrent targets without overwriting), applied BEFORE duplicate-vote
  checks and BEFORE human-approval gates, rechecked inside Step 3 before committing, and forwarded
  to `rerun_opportunity_id` if arriving after resolution.
- Human-participant groups (`is_human=1` or `has_human_participant=True`) NEVER auto-disband or
  replace without explicit human approval and bound waiting via `human_approval_until`
  (`awaiting_human_timeout_hold`).
- Reruns and retries enforce live `PersonaRegistry` identity/enrollment/single-session state,
  native LFG restrictions/Deserter/random cooldown, account-level WotLK 5-instances/hour cap,
  incomplete-run Deserter guards, actual remaining shift time, intact native party membership,
  and live `coop_enabled()` + `enabled_dungeon_ids()` + persisted headroom gates across ALL
  non-exit outcomes.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from .commitments import _parse_realm_id, _verify_persona_with_snapshot
from .core import profile, profiles
from .dungeons import DUNGEON_BY_ID, enabled_dungeon_ids
from .identity import PersonaRegistry
from .opportunities import (
    DEFAULT_MAX_GROUPS,
    OpportunityCoordinator,
    coop_enabled,
    evaluate_persisted_headroom,
    validate_candidate_for_dungeon,
)

logger = logging.getLogger("hearthcore.continuation")

SCHEMA_VERSION = 1
DEFAULT_DECISION_TTL_SEC = 120
DEFAULT_HUMAN_APPROVAL_TTL_SEC = 180
DEFAULT_RESOLVING_LEASE_SEC = 60
DEFAULT_MAX_WIPE_RETRIES = 2
MAX_WIPE_RETRIES_CEILING = 3
DEFAULT_MAX_CONSECUTIVE_RERUNS = 2
MAX_CONSECUTIVE_RERUNS_CEILING = 3
DEFAULT_MAX_TECHNICAL_FAILURES = 2
WOTLK_MAX_INSTANCES_PER_HOUR = 5
DEFAULT_TIME_BUFFER_MINUTES = 10

VALID_GOAL_KINDS = frozenset({"loot_item", "dungeon_quest", "practice", "social_help"})
VALID_BOUNDARY_KINDS = frozenset({"run_completed", "wipe_recovery", "leader_departure"})
VALID_DECISIONS_BY_BOUNDARY: dict[str, frozenset[str]] = {
    "run_completed": frozenset({"requeue", "leave"}),
    "wipe_recovery": frozenset({"retry", "leave"}),
    "leader_departure": frozenset({"continue_reelect", "requeue", "leave"}),
}
VALID_RECOVERY_REASONS = frozenset({
    "overpull_recovery",
    "patrol_add_recovery",
    "healer_oom_recovery",
    "tank_death_recovery",
    "mechanic_failure_recovery",
    "disconnect_recovery",
    "technical_failure_recovery",
    "unknown_wipe_cause",
})
VALID_OVERRIDE_KINDS = frozenset({"owner_recall", "raid_readiness"})


def _safe_int(val: Any) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _check_native_lfg_and_lock_restrictions(
    comp: dict[str, Any],
    *,
    decision: str,
    boundary_kind: str = "run_completed",
    requeue_mode: str = "specific",
) -> tuple[bool, str]:
    """Enforce native WotLK 3.3.5a LFG restrictions, Deserter penalties, and account hourly instance cap.

    Fail-closed on unknown/missing native LFG eligibility, Deserter, queue status, or hourly cap.
    """
    live = comp.get("live_telemetry") if isinstance(comp.get("live_telemetry"), dict) else None
    if live is None:
        return False, "missing_live_telemetry"

    if decision == "requeue":
        if live.get("native_lfg_eligible") is None:
            return False, "unknown_native_lfg_eligibility"
        if live.get("native_lfg_eligible") is False:
            lock_r = str(live.get("native_lfg_lock_reason") or "native_lfg_restricted").strip()
            return False, f"native_lfg_restricted:{lock_r}"

        if live.get("in_lfg_queue") is True:
            return False, "already_in_lfg_queue"

        deserter = _safe_int(live.get("lfg_deserter_remaining_sec"))
        if deserter is None:
            return False, "unknown_lfg_deserter_state"
        if deserter > 0:
            return False, f"lfg_deserter_active:{deserter}s"

        # P1-9: Requeueing from an incomplete run (e.g. leader_departure) leaves the current LFG
        # instance early and incurs Deserter unless native explicitly certifies exemption!
        if boundary_kind != "run_completed":
            would_deserter = live.get("would_incur_deserter")
            if would_deserter is not False:
                return False, "requeue_would_incur_deserter"

        if requeue_mode == "random":
            rand_cd = _safe_int(live.get("lfg_random_cooldown_sec"))
            if rand_cd is None:
                return False, "unknown_lfg_random_cooldown"
            if rand_cd > 0:
                return False, f"lfg_random_cooldown_active:{rand_cd}s"

        # P2-8: Prefer account-level hourly instance count (WotLK 5/hr is per-account!), fallback to char count
        raw_inst = live.get("account_instances_entered_last_hour")
        if raw_inst is None:
            raw_inst = live.get("instances_entered_last_hour")
        inst_hr = _safe_int(raw_inst)
        if inst_hr is None:
            return False, "unknown_hourly_instance_count"
        if inst_hr >= WOTLK_MAX_INSTANCES_PER_HOUR:
            return False, f"hourly_instance_cap_reached:{inst_hr}/{WOTLK_MAX_INSTANCES_PER_HOUR}"

    return True, "ok"


class PartyContinuationCoordinator:
    """Transactional SQLite coordinator for per-member goal resolution & voluntary rerun/recovery decisions."""

    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._conn()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_member_goals (
                    id INTEGER PRIMARY KEY,
                    opportunity_id INTEGER NOT NULL,
                    native_run_id TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    realm_id INTEGER NOT NULL DEFAULT 1,
                    character_guid INTEGER NOT NULL,
                    incarnation INTEGER NOT NULL DEFAULT 1,
                    goal_id TEXT NOT NULL,
                    goal_kind TEXT NOT NULL,
                    target_entry INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    eligible_loot_attempts INTEGER NOT NULL DEFAULT 0,
                    technical_failure INTEGER NOT NULL DEFAULT 0,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(opportunity_id, native_run_id, persona_id, goal_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_goal_events (
                    native_event_id TEXT PRIMARY KEY,
                    goal_row_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS continuation_boundaries (
                    id INTEGER PRIMARY KEY,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 1,
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    opportunity_id INTEGER NOT NULL,
                    native_run_id TEXT NOT NULL,
                    parent_boundary_id INTEGER NOT NULL DEFAULT 0,
                    chain_root_boundary_id INTEGER NOT NULL DEFAULT 0,
                    dungeon_id TEXT NOT NULL,
                    faction TEXT NOT NULL,
                    group_id INTEGER NOT NULL DEFAULT 0,
                    leader_persona_id TEXT NOT NULL DEFAULT '',
                    boundary_kind TEXT NOT NULL,
                    recovery_reason TEXT NOT NULL DEFAULT '',
                    wipe_count INTEGER NOT NULL DEFAULT 0,
                    max_wipe_retries INTEGER NOT NULL DEFAULT 2,
                    technical_failure_count INTEGER NOT NULL DEFAULT 0,
                    rerun_chain_depth INTEGER NOT NULL DEFAULT 0,
                    max_consecutive_reruns INTEGER NOT NULL DEFAULT 2,
                    remaining_dungeon_minutes INTEGER NOT NULL DEFAULT 0,
                    has_human_participant INTEGER NOT NULL DEFAULT 0,
                    human_approved INTEGER NOT NULL DEFAULT 0,
                    human_approval_until INTEGER NOT NULL DEFAULT 0,
                    requeue_mode TEXT NOT NULL DEFAULT 'specific',
                    state TEXT NOT NULL,
                    decision_until INTEGER NOT NULL,
                    resolving_until INTEGER NOT NULL DEFAULT 0,
                    handoff_phase TEXT NOT NULL DEFAULT '',
                    handoff_key TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL DEFAULT '',
                    outcome_reason TEXT NOT NULL DEFAULT '',
                    new_leader_persona_id TEXT NOT NULL DEFAULT '',
                    rerun_opportunity_id INTEGER NOT NULL DEFAULT 0,
                    is_shadow INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    ended_at INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS continuation_votes (
                    id INTEGER PRIMARY KEY,
                    boundary_id INTEGER NOT NULL,
                    persona_id TEXT NOT NULL,
                    realm_id INTEGER NOT NULL DEFAULT 1,
                    character_guid INTEGER NOT NULL,
                    incarnation INTEGER NOT NULL DEFAULT 1,
                    character_slug TEXT NOT NULL,
                    role TEXT NOT NULL,
                    is_human INTEGER NOT NULL DEFAULT 0,
                    willing_leader INTEGER NOT NULL DEFAULT 0,
                    decision TEXT NOT NULL DEFAULT 'pending',
                    reason_code TEXT NOT NULL DEFAULT '',
                    lfg_penalty_warning INTEGER NOT NULL DEFAULT 0,
                    voted_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(boundary_id, persona_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS continuation_overrides (
                    id INTEGER PRIMARY KEY,
                    boundary_id INTEGER NOT NULL,
                    persona_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    reason_code TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    applied_at INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(boundary_id, persona_id, kind)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS continuation_events (
                    id INTEGER PRIMARY KEY,
                    boundary_id INTEGER NOT NULL DEFAULT 0,
                    opportunity_id INTEGER NOT NULL DEFAULT 0,
                    persona_id TEXT NOT NULL DEFAULT '',
                    is_shadow INTEGER NOT NULL DEFAULT 1,
                    event_type TEXT NOT NULL,
                    reason_code TEXT NOT NULL DEFAULT '',
                    neutral INTEGER NOT NULL DEFAULT 1,
                    penalty_applied INTEGER NOT NULL DEFAULT 0,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rmg_run ON run_member_goals(opportunity_id, native_run_id, persona_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cb_state ON continuation_boundaries(state, is_shadow, opportunity_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cb_rerun_opp ON continuation_boundaries(rerun_opportunity_id) "
                "WHERE rerun_opportunity_id > 0"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_cb_one_open_per_run "
                "ON continuation_boundaries(opportunity_id, native_run_id) "
                "WHERE state IN ('open', 'resolving')"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_cb_one_completion_per_run "
                "ON continuation_boundaries(opportunity_id, native_run_id) "
                "WHERE boundary_kind = 'run_completed'"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cv_boundary ON continuation_votes(boundary_id, decision)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_co_pending ON continuation_overrides(boundary_id, applied_at)"
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _log_event(
        self,
        conn: sqlite3.Connection,
        *,
        event_type: str,
        now_ts: int,
        boundary_id: int = 0,
        opportunity_id: int = 0,
        persona_id: str = "",
        is_shadow: bool = True,
        reason_code: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO continuation_events (
                boundary_id, opportunity_id, persona_id, is_shadow, event_type,
                reason_code, neutral, penalty_applied, details_json, created_at
            ) VALUES (?,?,?,?,?,?,1,0,?,?)
            """,
            (
                int(boundary_id),
                int(opportunity_id),
                str(persona_id or ""),
                1 if is_shadow else 0,
                str(event_type)[:64],
                str(reason_code or "")[:120],
                json.dumps(details or {}, sort_keys=True),
                int(now_ts),
            ),
        )

    # -------------------------------------------------------------------------
    # AC1: Independent Per-Member Goal Registration & Idempotent Resolution
    # -------------------------------------------------------------------------
    def register_member_goal(
        self,
        *,
        opportunity_id: int,
        native_run_id: str,
        persona_id: str,
        character_guid: int,
        goal_id: str,
        goal_kind: str,
        target_entry: int = 0,
        realm_id: int = 1,
        incarnation: int = 1,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Register an individual member's personal ambition for a dungeon run (immutable once created/resolved, P1-3)."""
        ts = int(now_ts if now_ts is not None else time.time())
        kind = str(goal_kind or "").strip().lower()
        if kind not in VALID_GOAL_KINDS:
            return {"ok": False, "reason": f"invalid_goal_kind:{kind}"}
        rid = _parse_realm_id(realm_id)
        if rid is None:
            return {"ok": False, "reason": "invalid_realm_id"}
        cguid = _safe_int(character_guid)
        inc = _safe_int(incarnation)
        t_entry = _safe_int(target_entry)
        if cguid is None or cguid <= 0 or inc is None or inc <= 0 or t_entry is None or t_entry < 0:
            return {"ok": False, "reason": "invalid_goal_numeric_fields"}
        gid_str = str(goal_id or "").strip()[:64]
        run_id = str(native_run_id or "").strip()[:64]
        if not gid_str or not run_id:
            return {"ok": False, "reason": "missing_goal_or_run_id"}
        if kind in ("loot_item", "dungeon_quest") and t_entry <= 0:
            return {"ok": False, "reason": "target_entry_required_for_item_or_quest_goal"}

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM run_member_goals
                WHERE opportunity_id=? AND native_run_id=? AND persona_id=? AND goal_id=?
                """,
                (int(opportunity_id), run_id, persona_id, gid_str),
            ).fetchone()
            if existing:
                # P1-3: NEVER allow mutating a resolved goal OR silently changing identity/target!
                if str(existing["state"]) != "open":
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": f"goal_already_resolved:{existing['state']}"}
                if (
                    int(existing["realm_id"]) != rid
                    or int(existing["character_guid"]) != cguid
                    or int(existing["incarnation"]) != inc
                ):
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "goal_identity_mismatch"}
                if str(existing["goal_kind"]) != kind or int(existing["target_entry"]) != t_entry:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "goal_spec_immutable"}
                conn.execute("COMMIT")
                return {"ok": True, "idempotent": True, "goal_id": gid_str, "persona_id": persona_id, "state": "open"}

            conn.execute(
                """
                INSERT INTO run_member_goals (
                    opportunity_id, native_run_id, persona_id, realm_id, character_guid, incarnation,
                    goal_id, goal_kind, target_entry, state, eligible_loot_attempts, technical_failure,
                    evidence_json, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,'open',0,0,'{}',?,?)
                """,
                (
                    int(opportunity_id),
                    run_id,
                    persona_id,
                    rid,
                    cguid,
                    inc,
                    gid_str,
                    kind,
                    t_entry,
                    ts,
                    ts,
                ),
            )
            conn.execute("COMMIT")
            return {"ok": True, "idempotent": False, "goal_id": gid_str, "persona_id": persona_id, "state": "open"}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def resolve_member_goals(
        self,
        *,
        opportunity_id: int,
        native_run_id: str,
        run_completed: bool,
        observed_events: list[dict[str, Any]],
        completed_participants: set[tuple[str, int, int, int]] | None = None,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Resolve each participant's personal goals INDEPENDENTLY from idempotent, run-bound native events (AC1).

        Guarantees (P1-1, P1-2, P2-1..P2-4):
        - Every event MUST carry a non-empty `native_event_id` (deduplicated in `run_goal_events` so
          repeated calls NEVER double-count `eligible_loot_attempts`) and matching `native_run_id`.
        - Loot/quest/technical_failure events match strictly on FULL identity
          `(persona_id, realm_id, character_guid, incarnation)` and are verified against `PersonaRegistry`
          when present (mismatches logged and dropped as `evidence_identity_mismatch`).
        - Entry field is extracted strictly by event type (never conflating `quest_id` with reward `item_entry`).
        - Distinguishes genuine `technical_failure` events from normal no-drop `unfulfilled` (`technical_failure=False`).
        - `practice`/`social_help` require `run_completed=True` AND verified presence at completion
          (either via `completed_participants` or a `run_completed` native event for that character).
        """
        ts = int(now_ts if now_ts is not None else time.time())
        run_id = str(native_run_id or "").strip()[:64]
        malformed_evidence = 0
        identity_mismatches = 0
        wrong_run_ignored = 0
        duplicate_events_ignored = 0

        conn = self._conn()
        results: dict[str, list[dict[str, Any]]] = {}
        try:
            conn.execute("BEGIN IMMEDIATE")
            psnap = PersonaRegistry.read_only_snapshot(self.path, conn=conn)
            rows = conn.execute(
                "SELECT * FROM run_member_goals WHERE opportunity_id=? AND native_run_id=? ORDER BY id ASC",
                (int(opportunity_id), run_id),
            ).fetchall()

            # Index goals by full character identity key for strict matching
            # Key: (persona_id, realm_id, character_guid, incarnation) -> list of goal rows
            goals_by_full_key: dict[tuple[str, int, int, int], list[sqlite3.Row]] = {}
            for r in rows:
                fkey = (str(r["persona_id"]), int(r["realm_id"]), int(r["character_guid"]), int(r["incarnation"]))
                goals_by_full_key.setdefault(fkey, []).append(r)

            # Parse & validate observed events strictly
            validated_events: list[dict[str, Any]] = []
            run_completed_present: set[tuple[str, int, int, int]] = set(completed_participants or ())
            seen_batch_event_ids: set[str] = set()

            for ev in (observed_events or []):
                if not isinstance(ev, dict):
                    malformed_evidence += 1
                    continue
                ev_id = str(ev.get("native_event_id") or "").strip()[:96]
                if not ev_id:
                    malformed_evidence += 1
                    continue
                if ev_id in seen_batch_event_ids:
                    duplicate_events_ignored += 1
                    continue
                seen_batch_event_ids.add(ev_id)

                ev_run = str(ev.get("native_run_id") or "").strip()[:64]
                if ev_run != run_id:
                    wrong_run_ignored += 1
                    continue

                etype = str(ev.get("type") or "").strip().lower()
                pid = str(ev.get("persona_id") or "").strip()
                rid = _parse_realm_id(ev.get("realm_id"), default_when_none=None)
                cguid = _safe_int(ev.get("character_guid"))
                inc = _safe_int(ev.get("incarnation"))
                if not pid or rid is None or cguid is None or cguid <= 0 or inc is None or inc <= 0:
                    malformed_evidence += 1
                    continue

                # Cross-check against PersonaRegistry when present (P1-2!)
                if psnap["ok"] and psnap["has_registry"]:
                    prec = psnap["by_key"].get((rid, cguid))
                    if prec is None or prec["persona_id"] != pid or int(prec["incarnation"]) != inc:
                        identity_mismatches += 1
                        continue

                fkey = (pid, rid, cguid, inc)
                if etype == "run_completed":
                    run_completed_present.add(fkey)
                    continue

                # P2-2: Strict type-specific entry extraction (never conflate quest_id & item_entry!)
                if etype in ("actual_loot", "loot_opportunity", "technical_failure"):
                    entry = _safe_int(ev.get("item_entry") if ev.get("item_entry") is not None else ev.get("target_entry"))
                elif etype == "quest_completed":
                    entry = _safe_int(ev.get("quest_id") if ev.get("quest_id") is not None else ev.get("target_entry"))
                else:
                    malformed_evidence += 1
                    continue

                if entry is None or entry <= 0:
                    malformed_evidence += 1
                    continue

                validated_events.append({
                    "native_event_id": ev_id,
                    "type": etype,
                    "fkey": fkey,
                    "entry": entry,
                    "raw": ev,
                })

            # Apply validated events idempotently via run_goal_events PRIMARY KEY!
            for row in rows:
                row_id = int(row["id"])
                pid = str(row["persona_id"])
                fkey = (pid, int(row["realm_id"]), int(row["character_guid"]), int(row["incarnation"]))
                kind = str(row["goal_kind"])
                entry = int(row["target_entry"])
                cur_state = str(row["state"])
                attempts = int(row["eligible_loot_attempts"])
                tech_fail = bool(int(row["technical_failure"]))
                try:
                    evidence = json.loads(str(row["evidence_json"] or "{}"))
                except (ValueError, TypeError):
                    evidence = {}

                if cur_state == "fulfilled":
                    results.setdefault(pid, []).append({
                        "goal_id": str(row["goal_id"]),
                        "goal_kind": kind,
                        "target_entry": entry,
                        "state": "fulfilled",
                        "eligible_loot_attempts": attempts,
                        "technical_failure": False,
                    })
                    continue

                new_state = cur_state if cur_state != "open" else "unfulfilled"
                evidence["run_completed"] = bool(run_completed)

                for ve in validated_events:
                    if ve["fkey"] != fkey or ve["entry"] != entry:
                        continue
                    etype = ve["type"]
                    relevant = (
                        (kind == "loot_item" and etype in ("actual_loot", "loot_opportunity", "technical_failure"))
                        or (kind == "dungeon_quest" and etype in ("quest_completed", "technical_failure"))
                    )
                    if not relevant:
                        continue
                    # Idempotent dedup: skip if native_event_id was already processed!
                    ins = conn.execute(
                        "INSERT OR IGNORE INTO run_goal_events (native_event_id, goal_row_id, event_type, created_at) VALUES (?,?,?,?)",
                        (ve["native_event_id"], row_id, etype, ts),
                    ).rowcount
                    if ins == 0:
                        duplicate_events_ignored += 1
                        continue

                    if etype == "loot_opportunity":
                        attempts += 1
                        evidence["no_drop_or_lost_roll"] = (new_state != "fulfilled")
                    elif etype == "actual_loot":
                        new_state = "fulfilled"
                        tech_fail = False
                        if attempts == 0:
                            attempts = 1
                        evidence["actual_loot_event_id"] = ve["native_event_id"]
                        evidence["no_drop_or_lost_roll"] = False
                    elif etype == "quest_completed":
                        new_state = "fulfilled"
                        tech_fail = False
                        evidence["quest_completed_event_id"] = ve["native_event_id"]
                    elif etype == "technical_failure" and new_state != "fulfilled":
                        new_state = "technical_failure"
                        tech_fail = True
                        evidence["technical_failure_event_id"] = ve["native_event_id"]

                if kind in ("practice", "social_help") and new_state != "fulfilled":
                    if run_completed and fkey in run_completed_present:
                        new_state = "fulfilled"
                        tech_fail = False
                        evidence["completed_run_present"] = True

                conn.execute(
                    "UPDATE run_member_goals SET state=?, eligible_loot_attempts=?, technical_failure=?, evidence_json=?, updated_at=? WHERE id=?",
                    (new_state, attempts, 1 if tech_fail else 0, json.dumps(evidence, sort_keys=True), ts, row_id),
                )
                results.setdefault(pid, []).append({
                    "goal_id": str(row["goal_id"]),
                    "goal_kind": kind,
                    "target_entry": entry,
                    "state": new_state,
                    "eligible_loot_attempts": attempts,
                    "technical_failure": tech_fail,
                })

            conn.execute("COMMIT")
            return {
                "ok": True,
                "opportunity_id": int(opportunity_id),
                "native_run_id": run_id,
                "run_completed": bool(run_completed),
                "goals_by_persona": results,
                "malformed_evidence": malformed_evidence,
                "evidence_identity_mismatches": identity_mismatches,
                "wrong_run_ignored": wrong_run_ignored,
                "duplicate_events_ignored": duplicate_events_ignored,
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # AC2 & AC3: Safe-Boundary Decision Session, Independent Votes & Resolution
    # -------------------------------------------------------------------------
    def open_boundary(
        self,
        *,
        opportunity_id: int,
        native_run_id: str,
        dungeon_id: str,
        faction: str,
        group_id: int,
        leader_persona_id: str,
        boundary_kind: str,
        members: list[dict[str, Any]],
        safe_boundary: bool,
        in_combat: bool,
        world_ctx: dict[str, Any] | None = None,
        recovery_reason: str = "",
        wipe_count: int | None = None,
        max_wipe_retries: int = DEFAULT_MAX_WIPE_RETRIES,
        technical_failure_count: int = 0,
        rerun_chain_depth: int | None = None,
        max_consecutive_reruns: int = DEFAULT_MAX_CONSECUTIVE_RERUNS,
        remaining_dungeon_minutes: int | None = None,
        has_human_participant: bool = False,
        requeue_mode: str = "specific",
        decision_ttl_sec: int = DEFAULT_DECISION_TTL_SEC,
        human_approval_ttl_sec: int = DEFAULT_HUMAN_APPROVAL_TTL_SEC,
        is_shadow: bool = True,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Open a voluntary decision boundary ONLY at an explicit safe out-of-combat checkpoint.

        P0-4 & P1-10 guarantees:
        - Derives `wipe_count`, `rerun_chain_depth`, and `technical_failure_count` directly from
          persisted DB history (caller arguments can never reset or bypass bounds!).
        - Enforces at most ONE `run_completed` boundary per `(opportunity_id, native_run_id)` and
          rejects any new boundary once the run is completed.
        """
        ts = int(now_ts if now_ts is not None else time.time())
        bkind = str(boundary_kind or "").strip().lower()
        if bkind not in VALID_BOUNDARY_KINDS:
            return {"ok": False, "reason": f"invalid_boundary_kind:{bkind}"}
        if not safe_boundary or in_combat:
            return {"ok": False, "reason": "unsafe_boundary_mid_combat"}

        spec = DUNGEON_BY_ID.get(str(dungeon_id or "").strip().lower())
        if spec is None or not spec.supported:
            return {"ok": False, "reason": f"unsupported_dungeon:{dungeon_id}"}
        if not is_shadow:
            if not coop_enabled():
                return {"ok": False, "reason": "coop_execution_disabled"}
            if spec.dungeon_id not in enabled_dungeon_ids():
                return {"ok": False, "reason": f"dungeon_not_enabled_live:{spec.dungeon_id}"}

        fac = str(faction or "").strip().lower()
        if fac not in ("alliance", "horde"):
            return {"ok": False, "reason": "unknown_faction"}
        run_id = str(native_run_id or "").strip()[:64]
        if not run_id:
            return {"ok": False, "reason": "missing_native_run_id"}

        rec_reason = str(recovery_reason or "").strip().lower()
        if bkind == "wipe_recovery":
            if rec_reason not in VALID_RECOVERY_REASONS:
                return {"ok": False, "reason": f"invalid_recovery_reason:{rec_reason or 'missing'}"}
            if wipe_count is not None and (_safe_int(wipe_count) is None or int(wipe_count) < 1):
                return {"ok": False, "reason": "invalid_wipe_count_must_be_ge_1"}

        req_mode = str(requeue_mode or "specific").strip().lower()
        if req_mode not in ("specific", "random"):
            return {"ok": False, "reason": f"invalid_requeue_mode:{req_mode}"}

        if not members:
            return {"ok": False, "reason": "empty_party_members"}

        # P2-6: Validate & deduplicate BOTH personas AND (realm_id, character_guid)!
        seen_pids: set[str] = set()
        seen_chars: set[tuple[int, int]] = set()
        clean_members: list[dict[str, Any]] = []
        any_human_member = False
        for m in members:
            if not isinstance(m, dict):
                return {"ok": False, "reason": "invalid_member_record"}
            pid = str(m.get("persona_id") or "").strip()
            rid = _parse_realm_id(m.get("realm_id"))
            cguid = _safe_int(m.get("character_guid"))
            inc = _safe_int(m.get("incarnation") if m.get("incarnation") is not None else 1)
            role = str(m.get("role") or "").strip().lower()
            is_h = bool(m.get("is_human"))
            if is_h:
                any_human_member = True
            if (
                not pid
                or pid in seen_pids
                or rid is None
                or cguid is None
                or cguid <= 0
                or inc is None
                or inc <= 0
                or (rid, cguid) in seen_chars
                or role not in ("tank", "healer", "damage")
            ):
                return {"ok": False, "reason": "invalid_or_duplicate_member_record"}
            seen_pids.add(pid)
            seen_chars.add((rid, cguid))
            clean_members.append({
                "persona_id": pid,
                "realm_id": rid,
                "character_guid": cguid,
                "incarnation": inc,
                "character_slug": str(m.get("character_slug") or m.get("slug") or "").strip().lower(),
                "role": role,
                "is_human": 1 if is_h else 0,
                "willing_leader": 1 if m.get("willing_leader") else 0,
            })

        # P1-8: Validate leader_persona_id membership consistency!
        leader_pid = str(leader_persona_id or "").strip()
        if bkind == "leader_departure":
            if leader_pid and leader_pid in seen_pids:
                return {"ok": False, "reason": "departed_leader_still_in_members"}
        else:
            if leader_pid and leader_pid not in seen_pids:
                return {"ok": False, "reason": "leader_not_in_members"}

        # P2-5: Cross-check live world_ctx combat state when supplied!
        if isinstance(world_ctx, dict):
            comps_map = world_ctx.get("companions") or {}
            member_char_set = {(cm["realm_id"], cm["character_guid"]) for cm in clean_members if not cm["is_human"]}
            for comp in comps_map.values():
                if not isinstance(comp, dict):
                    continue
                cr = _parse_realm_id(comp.get("realm_id"))
                cg = _safe_int(comp.get("guid"))
                if cr is not None and cg is not None and (cr, cg) in member_char_set:
                    lt = comp.get("live_telemetry") if isinstance(comp.get("live_telemetry"), dict) else {}
                    if lt.get("in_combat") is True:
                        return {"ok": False, "reason": "unsafe_boundary_mid_combat"}

        eff_has_human = bool(has_human_participant or any_human_member)
        eff_max_wipes = max(0, min(int(max_wipe_retries), MAX_WIPE_RETRIES_CEILING))
        eff_max_reruns = max(0, min(int(max_consecutive_reruns), MAX_CONSECUTIVE_RERUNS_CEILING))
        rem_mins = int(spec.estimated_minutes if remaining_dungeon_minutes is None else max(5, int(remaining_dungeon_minutes)))
        dec_until = ts + max(15, min(int(decision_ttl_sec), 600))
        human_until = (ts + max(30, min(int(human_approval_ttl_sec), 900))) if eff_has_human else 0

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # P1-10: Reject any boundary if run already completed!
            completed_exists = conn.execute(
                "SELECT 1 FROM continuation_boundaries WHERE opportunity_id=? AND native_run_id=? AND boundary_kind='run_completed' LIMIT 1",
                (int(opportunity_id), run_id),
            ).fetchone()
            if completed_exists:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "run_already_completed"}

            # P0-4: Derive authoritative wipe_count, technical_failure_count & rerun_chain_depth from DB history!
            derived_wipe_count = 0
            if bkind == "wipe_recovery":
                prior_wipes = conn.execute(
                    """
                    SELECT COUNT(*) FROM continuation_boundaries
                    WHERE opportunity_id=? AND native_run_id=? AND boundary_kind='wipe_recovery'
                      AND outcome='retry_approved'
                    """,
                    (int(opportunity_id), run_id),
                ).fetchone()[0]
                derived_wipe_count = max(int(prior_wipes) + 1, int(wipe_count or 1))

            prior_tech = conn.execute(
                """
                SELECT COUNT(*) FROM continuation_boundaries
                WHERE opportunity_id=? AND native_run_id=? AND recovery_reason='technical_failure_recovery'
                """,
                (int(opportunity_id), run_id),
            ).fetchone()[0]
            derived_tech = max(
                int(prior_tech) + (1 if rec_reason == "technical_failure_recovery" else 0),
                max(0, int(technical_failure_count)),
            )

            parent_row = conn.execute(
                """
                SELECT id, rerun_chain_depth, chain_root_boundary_id, max_consecutive_reruns
                FROM continuation_boundaries
                WHERE rerun_opportunity_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (int(opportunity_id),),
            ).fetchone()
            parent_bid = 0
            root_bid = 0
            derived_depth = max(0, int(rerun_chain_depth or 0))
            if parent_row:
                parent_bid = int(parent_row["id"])
                root_bid = int(parent_row["chain_root_boundary_id"] or parent_bid)
                derived_depth = max(derived_depth, int(parent_row["rerun_chain_depth"]) + 1)
                eff_max_reruns = min(eff_max_reruns, int(parent_row["max_consecutive_reruns"]))

            retry_allowed = (
                bkind == "wipe_recovery"
                and derived_wipe_count <= eff_max_wipes
                and derived_tech < DEFAULT_MAX_TECHNICAL_FAILURES
            )
            requeue_allowed = (
                bkind in ("run_completed", "leader_departure")
                and derived_depth < eff_max_reruns
                and derived_tech < DEFAULT_MAX_TECHNICAL_FAILURES
            )

            cur = conn.execute(
                """
                INSERT INTO continuation_boundaries (
                    schema_version, revision, lease_epoch, opportunity_id, native_run_id,
                    parent_boundary_id, chain_root_boundary_id, dungeon_id, faction,
                    group_id, leader_persona_id, boundary_kind, recovery_reason, wipe_count,
                    max_wipe_retries, technical_failure_count, rerun_chain_depth, max_consecutive_reruns,
                    remaining_dungeon_minutes, has_human_participant, human_approval_until,
                    requeue_mode, state, decision_until, is_shadow, created_at, updated_at
                ) VALUES (?,?,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    SCHEMA_VERSION,
                    1,
                    int(opportunity_id),
                    run_id,
                    parent_bid,
                    root_bid,
                    spec.dungeon_id,
                    fac,
                    max(0, int(group_id or 0)),
                    leader_pid,
                    bkind,
                    rec_reason,
                    derived_wipe_count,
                    eff_max_wipes,
                    derived_tech,
                    derived_depth,
                    eff_max_reruns,
                    rem_mins,
                    1 if eff_has_human else 0,
                    human_until,
                    req_mode,
                    "open",
                    dec_until,
                    1 if is_shadow else 0,
                    ts,
                    ts,
                ),
            )
            bid = int(cur.lastrowid)
            if root_bid == 0:
                conn.execute("UPDATE continuation_boundaries SET chain_root_boundary_id=? WHERE id=?", (bid, bid))
            for cm in clean_members:
                conn.execute(
                    """
                    INSERT INTO continuation_votes (
                        boundary_id, persona_id, realm_id, character_guid, incarnation,
                        character_slug, role, is_human, willing_leader, decision, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)
                    """,
                    (
                        bid,
                        cm["persona_id"],
                        cm["realm_id"],
                        cm["character_guid"],
                        cm["incarnation"],
                        cm["character_slug"],
                        cm["role"],
                        cm["is_human"],
                        cm["willing_leader"],
                        ts,
                        ts,
                    ),
                )
            self._log_event(
                conn,
                event_type="boundary_opened",
                boundary_id=bid,
                opportunity_id=int(opportunity_id),
                is_shadow=is_shadow,
                reason_code=f"{bkind}:{rec_reason or 'safe_checkpoint'}",
                details={
                    "wipe_count": derived_wipe_count,
                    "max_wipe_retries": eff_max_wipes,
                    "technical_failure_count": derived_tech,
                    "retry_allowed": retry_allowed,
                    "requeue_allowed": requeue_allowed,
                    "rerun_chain_depth": derived_depth,
                    "parent_boundary_id": parent_bid,
                },
                now_ts=ts,
            )
            conn.execute("COMMIT")
            return {
                "ok": True,
                "boundary_id": bid,
                "revision": 1,
                "state": "open",
                "boundary_kind": bkind,
                "wipe_count": derived_wipe_count,
                "rerun_chain_depth": derived_depth,
                "technical_failure_count": derived_tech,
                "retry_allowed": retry_allowed,
                "requeue_allowed": requeue_allowed,
                "decision_until": dec_until,
            }
        except sqlite3.IntegrityError as exc:
            if conn.in_transaction:
                conn.rollback()
            msg = str(exc).lower()
            if "idx_cb_one_completion_per_run" in msg or "boundary_kind" in msg:
                return {"ok": False, "reason": "run_already_completed"}
            if "idx_cb_one_open_per_run" in msg or "opportunity_id" in msg:
                return {"ok": False, "reason": "boundary_already_open_for_run"}
            return {"ok": False, "reason": f"integrity_error:{exc}"}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def get_boundary(self, boundary_id: int) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM continuation_boundaries WHERE id=?", (int(boundary_id),)).fetchone()
            if not row:
                return None
            votes = [
                dict(v)
                for v in conn.execute(
                    "SELECT * FROM continuation_votes WHERE boundary_id=? ORDER BY id ASC",
                    (int(boundary_id),),
                ).fetchall()
            ]
            overrides = [
                dict(o)
                for o in conn.execute(
                    "SELECT * FROM continuation_overrides WHERE boundary_id=? ORDER BY id ASC",
                    (int(boundary_id),),
                ).fetchall()
            ]
            out = dict(row)
            out["votes"] = votes
            out["overrides"] = overrides
            return out
        finally:
            conn.close()

    def request_owner_recall(
        self,
        boundary_id: int,
        *,
        override_kind: str = "owner_recall",
        target_persona_id: str,
        safe_boundary: bool,
        opportunity_coordinator: OpportunityCoordinator | None = None,
        reason_code: str = "",
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Record an owner recall or raid readiness override safely (P0-2, P2-7).

        Guarantees:
        - Verifies `target_persona_id` is a member of this boundary.
        - Stores overrides in `continuation_overrides` so multiple concurrent recalls NEVER overwrite each other.
        - Does NOT bump boundary `revision` while `state='resolving'` (preventing Step 3 CAS mismatch while
          Step 3 atomically drains all unapplied `continuation_overrides`!).
        - If boundary is already `resolved` with a `rerun_opportunity_id` and `opportunity_coordinator` is
          supplied, forwards withdrawal directly to that rerun opportunity!
        """
        kind = str(override_kind or "").strip().lower()
        if kind not in VALID_OVERRIDE_KINDS:
            return {"ok": False, "reason": f"invalid_override_kind:{kind}"}
        target_pid = str(target_persona_id or "").strip()
        if not target_pid:
            return {"ok": False, "reason": "missing_target_persona_id"}
        ts = int(now_ts if now_ts is not None else time.time())
        rc = (reason_code or kind)[:64]

        conn = self._conn()
        forward_opp_id = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            b = conn.execute("SELECT * FROM continuation_boundaries WHERE id=?", (int(boundary_id),)).fetchone()
            if not b:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "not_found"}

            member = conn.execute(
                "SELECT id FROM continuation_votes WHERE boundary_id=? AND persona_id=?",
                (int(boundary_id), target_pid),
            ).fetchone()
            if not member:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "member_not_found"}

            b_state = str(b["state"])
            is_sh = bool(int(b["is_shadow"]))
            b_kind = str(b["boundary_kind"])

            if b_state == "resolved":
                forward_opp_id = int(b["rerun_opportunity_id"] or 0)
                if forward_opp_id > 0 and opportunity_coordinator is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO continuation_overrides (boundary_id, persona_id, kind, reason_code, created_at, applied_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (int(boundary_id), target_pid, kind, rc, ts, ts),
                    )
                    self._log_event(
                        conn,
                        event_type=f"{kind}_forwarded_to_rerun",
                        boundary_id=int(boundary_id),
                        opportunity_id=forward_opp_id,
                        persona_id=target_pid,
                        is_shadow=is_sh,
                        reason_code=rc,
                        now_ts=ts,
                    )
                    conn.execute("COMMIT")
                else:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "already_resolved"}
            elif not safe_boundary or b_state == "resolving":
                # P0-2: Persist into continuation_overrides without breaking an active 'resolving' revision CAS!
                conn.execute(
                    """
                    INSERT INTO continuation_overrides (boundary_id, persona_id, kind, reason_code, created_at, applied_at)
                    VALUES (?,?,?,?,?,0)
                    ON CONFLICT(boundary_id, persona_id, kind) DO UPDATE SET
                        reason_code = excluded.reason_code,
                        applied_at = CASE WHEN applied_at = 0 THEN 0 ELSE applied_at END
                    """,
                    (int(boundary_id), target_pid, kind, rc, ts),
                )
                self._log_event(
                    conn,
                    event_type=f"pending_{kind}",
                    boundary_id=int(boundary_id),
                    opportunity_id=int(b["opportunity_id"]),
                    persona_id=target_pid,
                    is_shadow=is_sh,
                    reason_code=(reason_code or f"deferred_until_safe_boundary:{kind}")[:120],
                    now_ts=ts,
                )
                cur_rev = int(b["revision"])
                conn.execute("COMMIT")
                return {
                    "ok": True,
                    "deferred_to_safe_boundary": True,
                    "pending_override": kind,
                    "pending_override_target": target_pid,
                    "revision": cur_rev,
                    "neutral": True,
                }
            else:
                # Safe boundary on 'open' row: apply immediately!
                lfg_warn = 1 if b_kind in ("wipe_recovery", "leader_departure") else 0
                conn.execute(
                    """
                    INSERT INTO continuation_overrides (boundary_id, persona_id, kind, reason_code, created_at, applied_at)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(boundary_id, persona_id, kind) DO UPDATE SET
                        reason_code = excluded.reason_code,
                        applied_at = excluded.applied_at
                    """,
                    (int(boundary_id), target_pid, kind, rc, ts, ts),
                )
                conn.execute(
                    "UPDATE continuation_votes SET decision='leave', reason_code=?, lfg_penalty_warning=?, "
                    "voted_at=?, updated_at=? WHERE boundary_id=? AND persona_id=?",
                    (rc, lfg_warn, ts, ts, int(boundary_id), target_pid),
                )
                new_rev = int(b["revision"]) + 1
                conn.execute(
                    "UPDATE continuation_boundaries SET updated_at=?, revision=? WHERE id=?",
                    (ts, new_rev, int(boundary_id)),
                )
                self._log_event(
                    conn,
                    event_type=f"{kind}_applied",
                    boundary_id=int(boundary_id),
                    opportunity_id=int(b["opportunity_id"]),
                    persona_id=target_pid,
                    is_shadow=is_sh,
                    reason_code=rc,
                    now_ts=ts,
                )
                conn.execute("COMMIT")
                return {
                    "ok": True,
                    "deferred_to_safe_boundary": False,
                    "target_decision": "leave",
                    "lfg_penalty_warning": bool(lfg_warn),
                    "revision": new_rev,
                    "neutral": True,
                }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

        if forward_opp_id > 0 and opportunity_coordinator is not None:
            w_res = opportunity_coordinator.withdraw_interest(forward_opp_id, target_pid, reason=rc, now_ts=ts)
            return {
                "ok": True,
                "forwarded_to_rerun_opportunity": True,
                "rerun_opportunity_id": forward_opp_id,
                "withdraw_result": w_res,
                "neutral": True,
            }
        return {"ok": False, "reason": "already_resolved"}

    def submit_member_decision(
        self,
        boundary_id: int,
        *,
        expected_revision: int,
        persona_id: str,
        character_guid: int,
        decision: str,
        realm_id: int = 1,
        incarnation: int = 1,
        reason_code: str = "",
        willing_leader: bool | None = None,
        candidate: dict[str, Any] | None = None,
        owner_recalled: bool = False,
        readiness_obligated: bool = False,
        raid_stage: int = 0,
        now_ts: int | None = None,
        tz=None,
        allow_legacy_no_registry: bool = False,
    ) -> dict[str, Any]:
        """Record an individual member's voluntary decision (`retry`, `requeue`, `continue_reelect`, `leave`)."""
        ts = int(now_ts if now_ts is not None else time.time())
        dec = str(decision or "").strip().lower()

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            b = conn.execute("SELECT * FROM continuation_boundaries WHERE id=?", (int(boundary_id),)).fetchone()
            if not b:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "not_found"}

            b_state = str(b["state"])
            b_rev = int(b["revision"])
            b_kind = str(b["boundary_kind"])
            is_sh = bool(int(b["is_shadow"]))
            if b_state != "open" or b_rev != int(expected_revision):
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"revision_or_state_mismatch:{b_state}:rev{b_rev}"}
            if int(b["decision_until"]) <= ts:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "decision_window_expired"}

            valid_decs = VALID_DECISIONS_BY_BOUNDARY.get(b_kind, frozenset())
            if dec not in valid_decs:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"invalid_decision_for_{b_kind}:{dec}"}

            vote = conn.execute(
                "SELECT * FROM continuation_votes WHERE boundary_id=? AND persona_id=?",
                (int(boundary_id), persona_id),
            ).fetchone()
            if not vote:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "member_not_found"}

            if (
                int(vote["character_guid"]) != int(character_guid)
                or int(vote["realm_id"]) != int(realm_id)
                or int(vote["incarnation"]) != int(incarnation)
            ):
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "character_key_mismatch"}

            # P1-4: Owner recall / raid readiness override runs BEFORE idempotent duplicate check!
            lfg_penalty_warning = 1 if (b_kind in ("wipe_recovery", "leader_departure") and dec in ("leave", "requeue")) else 0
            if (owner_recalled or readiness_obligated) and dec != "leave":
                ov_rc = "owner_recall" if owner_recalled else "raid_readiness"
                warn_ov = 1 if b_kind in ("wipe_recovery", "leader_departure") else 0
                conn.execute(
                    "UPDATE continuation_votes SET decision='leave', reason_code=?, lfg_penalty_warning=?, voted_at=?, updated_at=? WHERE id=?",
                    (ov_rc, warn_ov, ts, ts, int(vote["id"])),
                )
                conn.execute(
                    """
                    INSERT INTO continuation_overrides (boundary_id, persona_id, kind, reason_code, created_at, applied_at)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(boundary_id, persona_id, kind) DO UPDATE SET applied_at = excluded.applied_at
                    """,
                    (int(boundary_id), persona_id, ov_rc, ov_rc, ts, ts),
                )
                self._log_event(
                    conn,
                    event_type=f"{ov_rc}_applied",
                    boundary_id=int(boundary_id),
                    opportunity_id=int(b["opportunity_id"]),
                    persona_id=persona_id,
                    is_shadow=is_sh,
                    reason_code=ov_rc,
                    details={"lfg_penalty_warning": bool(warn_ov)},
                    now_ts=ts,
                )
                conn.execute("COMMIT")
                return {
                    "ok": True,
                    "overridden_to_leave": True,
                    "decision": "leave",
                    "reason": ov_rc,
                    "lfg_penalty_warning": bool(warn_ov),
                    "neutral": True,
                }

            # Idempotent duplicate vote
            if str(vote["decision"]) == dec and dec != "pending":
                conn.execute("COMMIT")
                return {"ok": True, "idempotent": True, "decision": dec, "revision": b_rev}

            # P1-6: Live kill-switch check on non-leave votes!
            if not is_sh and dec != "leave":
                if not coop_enabled():
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "coop_execution_disabled"}
                if str(b["dungeon_id"]) not in enabled_dungeon_ids():
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": f"dungeon_not_enabled_live:{b['dungeon_id']}"}

            # Hard bounds on wipe retries & consecutive reruns
            if dec == "retry":
                if int(b["wipe_count"]) > int(b["max_wipe_retries"]):
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": f"wipe_retry_limit_exceeded:{b['wipe_count']}>{b['max_wipe_retries']}"}
                if int(b["technical_failure_count"]) >= DEFAULT_MAX_TECHNICAL_FAILURES:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "technical_failure_cooldown_active"}
            if dec == "requeue":
                if int(b["rerun_chain_depth"]) >= int(b["max_consecutive_reruns"]):
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": f"max_consecutive_reruns_reached:{b['rerun_chain_depth']}"}
                if int(b["technical_failure_count"]) >= DEFAULT_MAX_TECHNICAL_FAILURES:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "technical_failure_cooldown_active"}

            if dec != "leave" and int(vote["is_human"]) == 0:
                # P3: Require live candidate snapshot for bot non-leave votes (never blindly accept unverified retry/requeue!)
                if candidate is None:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "candidate_telemetry_required"}

                psnap = PersonaRegistry.read_only_snapshot(self.path, conn=conn)
                shift_grp = str(candidate.get("shift_group") or "")
                ok_p, r_p, _ = _verify_persona_with_snapshot(
                    psnap,
                    persona_id=persona_id,
                    realm_id=int(realm_id),
                    character_guid=int(character_guid),
                    allow_legacy_no_registry=allow_legacy_no_registry,
                    is_shadow=is_sh,
                    shift_group=shift_grp,
                    enforce_single_session_on_char=True,
                    expected_incarnation=int(incarnation),
                )
                if not ok_p:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": r_p}

                spec = DUNGEON_BY_ID.get(str(b["dungeon_id"]))
                if spec is None:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": f"unknown_dungeon:{b['dungeon_id']}"}

                ok_lfg, r_lfg = _check_native_lfg_and_lock_restrictions(
                    candidate, decision=dec, boundary_kind=b_kind, requeue_mode=str(b["requeue_mode"])
                )
                if not ok_lfg:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": r_lfg}

                b_gid = int(b["group_id"] or 0)
                c_in_group = candidate.get("in_group")
                c_gid = _safe_int(candidate.get("group_id")) or 0
                if dec in ("retry", "continue_reelect"):
                    if c_in_group is not True or (b_gid > 0 and c_gid != b_gid):
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": "native_party_not_intact"}
                elif dec == "requeue":
                    if c_in_group is True and b_gid > 0 and c_gid != b_gid:
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": "in_different_group"}

                # P1-7: Read actual enrolled_coop, persona_state, char_active from psnap!
                prec = psnap["by_key"].get((int(realm_id), int(character_guid))) if psnap["has_registry"] else None
                eff_enrolled = bool(prec["enrolled_coop"]) if prec else True
                eff_pstate = str(prec["state"]) if prec else "active"
                eff_cactive = bool(prec["char_active"]) if prec else True

                eval_cand = dict(candidate)
                if c_in_group is True and (b_gid == 0 or c_gid == b_gid):
                    eval_cand["in_group"] = False
                known_profs = set(profiles())
                slug = str(vote["character_slug"])
                prof = profile(slug) if slug in known_profs else None
                ev = validate_candidate_for_dungeon(
                    eval_cand,
                    spec,
                    required_role=str(vote["role"]),
                    allow_pilot_shadow=is_sh,
                    raid_stage=raid_stage,
                    now_ts=ts,
                    tz=tz,
                    char_profile=prof,
                    enrolled_coop=eff_enrolled,
                    persona_state=eff_pstate,
                    char_active=eff_cactive,
                )
                if not ev["available"]:
                    if dec == "retry" and str(ev["reason"]).startswith(("insufficient_schedule_window", "insufficient_shift_time")):
                        avail_m = ev.get("available_minutes")
                        prep_m = int((ev.get("travel_prep") or {}).get("prep_minutes") or 0)
                        needed_retry = int(b["remaining_dungeon_minutes"]) + prep_m + DEFAULT_TIME_BUFFER_MINUTES
                        if avail_m is None or int(avail_m) < needed_retry:
                            conn.execute("ROLLBACK")
                            return {"ok": False, "reason": f"insufficient_shift_time_for_retry:{avail_m}<{needed_retry}"}
                    else:
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": ev["reason"]}

            eff_wl = int(vote["willing_leader"]) if willing_leader is None else (1 if willing_leader else 0)
            rc = (reason_code or f"voluntary_{dec}")[:64]
            conn.execute(
                "UPDATE continuation_votes SET decision=?, reason_code=?, willing_leader=?, "
                "lfg_penalty_warning=?, voted_at=?, updated_at=? WHERE id=?",
                (dec, rc, eff_wl, lfg_penalty_warning, ts, ts, int(vote["id"])),
            )
            self._log_event(
                conn,
                event_type="member_decision_recorded",
                boundary_id=int(boundary_id),
                opportunity_id=int(b["opportunity_id"]),
                persona_id=persona_id,
                is_shadow=is_sh,
                reason_code=f"{dec}:{rc}",
                details={"lfg_penalty_warning": bool(lfg_penalty_warning)},
                now_ts=ts,
            )
            conn.execute("COMMIT")
            return {
                "ok": True,
                "idempotent": False,
                "decision": dec,
                "lfg_penalty_warning": bool(lfg_penalty_warning),
                "revision": b_rev,
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def reconcile_stale_boundaries(
        self,
        *,
        opportunity_coordinator: OpportunityCoordinator | None = None,
        now_ts: int | None = None,
    ) -> dict[str, int]:
        """Crash recovery for boundaries stuck in 'resolving' past `resolving_until` (P0-1 & P0-3)."""
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            stuck = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM continuation_boundaries WHERE state='resolving' AND resolving_until <= ?",
                    (ts,),
                ).fetchall()
            ]
        finally:
            conn.close()

        reverted = 0
        released_opps = 0
        for row in stuck:
            bid = int(row["id"])
            rerun_opp = int(row["rerun_opportunity_id"] or 0)
            if rerun_opp > 0 and opportunity_coordinator is not None:
                opp = opportunity_coordinator.get_opportunity(rerun_opp)
                if opp and str(opp["state"]) in ("recruiting", "assembling"):
                    opportunity_coordinator.release_party(rerun_opp, "resolving_lease_crash_recovery", now_ts=ts)
                    released_opps += 1

            c2 = self._conn()
            try:
                c2.execute("BEGIN IMMEDIATE")
                upd = c2.execute(
                    "UPDATE continuation_boundaries SET state='open', resolving_until=0, "
                    "handoff_phase='', handoff_key='', rerun_opportunity_id=0, updated_at=?, revision=revision+1 "
                    "WHERE id=? AND state='resolving' AND revision=?",
                    (ts, bid, int(row["revision"])),
                ).rowcount
                if upd == 1:
                    self._log_event(
                        c2,
                        event_type="stale_resolving_reverted",
                        boundary_id=bid,
                        opportunity_id=int(row["opportunity_id"]),
                        is_shadow=bool(int(row["is_shadow"])),
                        reason_code="resolving_lease_expired",
                        details={"released_rerun_opp_id": rerun_opp},
                        now_ts=ts,
                    )
                    reverted += 1
                c2.execute("COMMIT")
            except Exception:
                if c2.in_transaction:
                    c2.rollback()
                raise
            finally:
                c2.close()
        return {"reverted_to_open": reverted, "released_orphan_opps": released_opps}

    def resolve_boundary(
        self,
        boundary_id: int,
        *,
        expected_revision: int,
        world_ctx: dict[str, Any],
        opportunity_coordinator: OpportunityCoordinator | None = None,
        owner_recalled_personas: set[str] | frozenset[str] | None = None,
        readiness_obligated_personas: set[str] | frozenset[str] | None = None,
        human_approved: bool = False,
        headroom_state_path: Path | None = None,
        mem_avail_mib: float | None = None,
        tick_p99_ms: float | None = None,
        active_managed_groups: int = 0,
        max_managed_groups: int = DEFAULT_MAX_GROUPS,
        raid_stage: int = 0,
        now_ts: int | None = None,
        tz=None,
        max_telemetry_age_sec: int = 300,
        allow_legacy_no_registry: bool = False,
    ) -> dict[str, Any]:
        """Atomically resolve a safe boundary with lease recovery, override draining, and full compensation."""
        ts = int(now_ts if now_ts is not None else time.time())
        recalled_arg = set(owner_recalled_personas or ())
        readiness_arg = set(readiness_obligated_personas or ())

        # Step 1: Atomically persist incoming recalls, check human/vote gates, and CAS -> 'resolving' (reclaiming expired lease!)
        conn = self._conn()
        claimed_rev = 0
        claimed_epoch = 0
        db_active_groups = 0
        stale_opp_to_release = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            b = conn.execute("SELECT * FROM continuation_boundaries WHERE id=?", (int(boundary_id),)).fetchone()
            if not b:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "not_found"}

            b_state = str(b["state"])
            b_rev = int(b["revision"])
            is_sh = bool(int(b["is_shadow"]))
            b_kind = str(b["boundary_kind"])
            if b_state == "resolved":
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "already_resolved", "outcome": str(b["outcome"])}

            reclaiming_stale = (b_state == "resolving" and int(b["resolving_until"]) <= ts)
            if b_state == "resolving" and not reclaiming_stale:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "already_resolving"}
            if b_state not in ("open", "resolving") or b_rev != int(expected_revision):
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"revision_or_state_mismatch:{b_state}:rev{b_rev}"}

            if reclaiming_stale and int(b["rerun_opportunity_id"] or 0) > 0:
                stale_opp_to_release = int(b["rerun_opportunity_id"])

            # P1-5: Persist & apply ALL incoming + queued overrides BEFORE checking human approval or pending votes!
            votes = [
                dict(v)
                for v in conn.execute(
                    "SELECT * FROM continuation_votes WHERE boundary_id=? ORDER BY id ASC",
                    (int(boundary_id),),
                ).fetchall()
            ]
            member_pids = {str(v["persona_id"]) for v in votes}
            for rpid in recalled_arg:
                if rpid in member_pids:
                    conn.execute(
                        "INSERT OR IGNORE INTO continuation_overrides (boundary_id, persona_id, kind, reason_code, created_at, applied_at) "
                        "VALUES (?,?,?,?,?,0)",
                        (int(boundary_id), rpid, "owner_recall", "owner_recall", ts),
                    )
            for rpid in readiness_arg:
                if rpid in member_pids:
                    conn.execute(
                        "INSERT OR IGNORE INTO continuation_overrides (boundary_id, persona_id, kind, reason_code, created_at, applied_at) "
                        "VALUES (?,?,?,?,?,0)",
                        (int(boundary_id), rpid, "raid_readiness", "raid_readiness", ts),
                    )

            unapplied_ovs = conn.execute(
                "SELECT * FROM continuation_overrides WHERE boundary_id=? AND applied_at=0 ORDER BY id ASC",
                (int(boundary_id),),
            ).fetchall()
            override_by_pid: dict[str, str] = {}
            for ov in unapplied_ovs:
                opid = str(ov["persona_id"])
                # owner_recall takes priority over raid_readiness if both present
                if opid not in override_by_pid or str(ov["kind"]) == "owner_recall":
                    override_by_pid[opid] = str(ov["kind"])
                conn.execute("UPDATE continuation_overrides SET applied_at=? WHERE id=?", (ts, int(ov["id"])))

            lfg_warn_inc = 1 if b_kind in ("wipe_recovery", "leader_departure") else 0
            for v in votes:
                pid = str(v["persona_id"])
                if pid in override_by_pid:
                    ov_k = override_by_pid[pid]
                    conn.execute(
                        "UPDATE continuation_votes SET decision='leave', reason_code=?, lfg_penalty_warning=?, voted_at=?, updated_at=? WHERE id=?",
                        (ov_k, lfg_warn_inc, ts, ts, int(v["id"])),
                    )
                    v["decision"] = "leave"
                    v["reason_code"] = ov_k

            # Check pending bot votes (human rows are non-bot participants, P1-5!)
            expired = int(b["decision_until"]) <= ts
            pending_bots = [
                v for v in votes
                if int(v["is_human"]) == 0 and str(v["decision"]) == "pending"
            ]
            if pending_bots and not expired:
                # Commit any persisted recalls before returning awaiting_member_decisions (P3 fix!)
                conn.execute("COMMIT")
                return {
                    "ok": False,
                    "reason": "awaiting_member_decisions",
                    "pending_personas": [v["persona_id"] for v in pending_bots],
                }

            # P1-5: Human-approval gate with bounded TTL (`human_approval_until` -> non-destructive hold!)
            if int(b["has_human_participant"]) == 1 and not human_approved:
                h_until = int(b["human_approval_until"] or 0)
                if h_until > 0 and ts >= h_until:
                    final_rev = b_rev + 1
                    conn.execute(
                        "UPDATE continuation_boundaries SET state='resolved', outcome='awaiting_human_timeout_hold', "
                        "outcome_reason='human_approval_timed_out_non_destructive', ended_at=?, updated_at=?, revision=? WHERE id=?",
                        (ts, ts, final_rev, int(boundary_id)),
                    )
                    self._log_event(
                        conn,
                        event_type="boundary_resolved",
                        boundary_id=int(boundary_id),
                        opportunity_id=int(b["opportunity_id"]),
                        is_shadow=is_sh,
                        reason_code="awaiting_human_timeout_hold",
                        now_ts=ts,
                    )
                    conn.execute("COMMIT")
                    return {
                        "ok": True,
                        "boundary_id": int(boundary_id),
                        "state": "resolved",
                        "revision": final_rev,
                        "outcome": "awaiting_human_timeout_hold",
                        "outcome_reason": "human_approval_timed_out_non_destructive",
                        "leaving_personas": [str(v["persona_id"]) for v in votes if str(v["decision"]) == "leave"],
                        "admitted_rerun_personas": [],
                        "continue_reelect_unfulfilled": [],
                        "revalidation_rejections": [],
                        "new_leader_persona_id": "",
                        "rerun_opportunity_id": 0,
                    }
                conn.execute("COMMIT")
                return {"ok": False, "reason": "awaiting_human_approval"}

            # Expire any remaining pending bot votes safely to 'leave'
            for v in votes:
                if int(v["is_human"]) == 0 and str(v["decision"]) == "pending":
                    conn.execute(
                        "UPDATE continuation_votes SET decision='leave', reason_code='decision_timeout_safe_leave', "
                        "lfg_penalty_warning=?, updated_at=? WHERE id=?",
                        (lfg_warn_inc, ts, int(v["id"])),
                    )
                    v["decision"] = "leave"
                    v["reason_code"] = "decision_timeout_safe_leave"

            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "opportunities" in tables:
                cnt_row = conn.execute(
                    "SELECT COUNT(*) FROM opportunities WHERE is_shadow = ? AND state IN ('assembling','confirmed','active','cancelling')",
                    (1 if is_sh else 0,),
                ).fetchone()
                db_active_groups = int(cnt_row[0] or 0)

            claimed_rev = b_rev + 1
            claimed_epoch = int(b["lease_epoch"]) + 1
            lease_until = ts + DEFAULT_RESOLVING_LEASE_SEC
            updated = conn.execute(
                "UPDATE continuation_boundaries SET state='resolving', resolving_until=?, lease_epoch=?, "
                "handoff_phase='', handoff_key='', rerun_opportunity_id=0, human_approved=?, updated_at=?, revision=? "
                "WHERE id=? AND state=? AND revision=?",
                (lease_until, claimed_epoch, 1 if human_approved else 0, ts, claimed_rev, int(boundary_id), b_state, b_rev),
            ).rowcount
            b_dict = dict(b)
            conn.execute("COMMIT")
            if updated != 1:
                return {"ok": False, "reason": "boundary_claim_raced"}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

        # If we reclaimed a stale lease with an orphaned rerun opportunity, release it cleanly!
        if stale_opp_to_release > 0 and opportunity_coordinator is not None:
            try:
                opportunity_coordinator.release_party(stale_opp_to_release, "stale_resolving_reclaim_cleanup", now_ts=ts)
            except Exception as exc:
                logger.warning("Failed releasing stale rerun opp %s: %s", stale_opp_to_release, exc)

        def _revert_resolving_on_error() -> None:
            c_err = self._conn()
            try:
                c_err.execute("BEGIN IMMEDIATE")
                c_err.execute(
                    "UPDATE continuation_boundaries SET state='open', resolving_until=0, handoff_phase='', "
                    "handoff_key='', rerun_opportunity_id=0, updated_at=?, revision=revision+1 "
                    "WHERE id=? AND state='resolving' AND lease_epoch=?",
                    (ts, int(boundary_id), claimed_epoch),
                )
                c_err.execute("COMMIT")
            except Exception:
                if c_err.in_transaction:
                    c_err.rollback()
                raise
            finally:
                c_err.close()

        # Step 2: Wrapped in try/except so NO exception can ever leave the boundary stuck in 'resolving' (P0-1!)
        is_sh = bool(int(b_dict["is_shadow"]))
        b_kind = str(b_dict["boundary_kind"])
        b_gid = int(b_dict["group_id"] or 0)
        bot_votes = [v for v in votes if int(v["is_human"]) == 0]
        leaving_personas = [str(v["persona_id"]) for v in bot_votes if str(v["decision"]) == "leave"]
        continue_unfulfilled: list[dict[str, str]] = []
        revalidation_rejections: list[dict[str, str]] = []
        admitted_rerun_personas: list[str] = []
        outcome = "safe_exit"
        outcome_reason = "members_chose_leave"
        new_leader_pid = ""
        rerun_opp_id = 0

        try:
            spec = DUNGEON_BY_ID.get(str(b_dict["dungeon_id"]))
            if spec is None or not spec.supported:
                outcome = "safe_exit_recovery"
                outcome_reason = f"unsupported_dungeon:{b_dict['dungeon_id']}"
            else:
                # P1-6: Check live kill-switch & dungeon gate for ALL non-exit live outcomes!
                live_disabled_reason = ""
                if not is_sh:
                    if not coop_enabled():
                        live_disabled_reason = "coop_execution_disabled"
                    elif spec.dungeon_id not in enabled_dungeon_ids():
                        live_disabled_reason = f"dungeon_not_enabled_live:{spec.dungeon_id}"

                # Index live companions safely (never crashing on malformed guid/group_id!)
                comps_by_key: dict[tuple[int, int], tuple[str, dict[str, Any]]] = {}
                for slug, comp in ((world_ctx or {}).get("companions") or {}).items():
                    if not isinstance(comp, dict):
                        continue
                    cg = _safe_int(comp.get("guid"))
                    cr = _parse_realm_id(comp.get("realm_id"))
                    if cg is None or cg <= 0 or cr is None:
                        continue
                    comps_by_key[(cr, cg)] = (str(slug), comp)

                known_profs = set(profiles())
                psnap = PersonaRegistry.read_only_snapshot(self.path)

                def _revalidate_member(v: dict[str, Any], dec: str) -> tuple[bool, str, dict[str, Any] | None, dict[str, Any] | None]:
                    pid = str(v["persona_id"])
                    rid = int(v["realm_id"])
                    guid = int(v["character_guid"])
                    inc = int(v["incarnation"])
                    found = comps_by_key.get((rid, guid))
                    shift_grp = str(found[1].get("shift_group") or "") if found else ""

                    ok_p, r_p, _ = _verify_persona_with_snapshot(
                        psnap,
                        persona_id=pid,
                        realm_id=rid,
                        character_guid=guid,
                        allow_legacy_no_registry=allow_legacy_no_registry,
                        is_shadow=is_sh,
                        shift_group=shift_grp,
                        enforce_single_session_on_char=True,
                        expected_incarnation=inc,
                    )
                    if not ok_p:
                        return False, r_p, None, None
                    if found is None:
                        return False, "offline_or_missing_in_world_ctx", None, None
                    slug, comp = found
                    ok_lfg, r_lfg = _check_native_lfg_and_lock_restrictions(
                        comp, decision=dec, boundary_kind=b_kind, requeue_mode=str(b_dict["requeue_mode"])
                    )
                    if not ok_lfg:
                        return False, r_lfg, None, None

                    c_in_group = comp.get("in_group")
                    c_gid = _safe_int(comp.get("group_id")) or 0
                    if dec in ("retry", "continue_reelect"):
                        if c_in_group is not True or (b_gid > 0 and c_gid != b_gid):
                            return False, "native_party_not_intact", None, None
                    elif dec == "requeue":
                        if c_in_group is True and b_gid > 0 and c_gid != b_gid:
                            return False, "in_different_group", None, None

                    prec = psnap["by_key"].get((rid, guid)) if psnap["has_registry"] else None
                    eff_enrolled = bool(prec["enrolled_coop"]) if prec else True
                    eff_pstate = str(prec["state"]) if prec else "active"
                    eff_cactive = bool(prec["char_active"]) if prec else True

                    eval_comp = dict(comp)
                    if c_in_group is True and (b_gid == 0 or c_gid == b_gid):
                        eval_comp["in_group"] = False
                    prof = profile(slug) if slug in known_profs else None
                    ev = validate_candidate_for_dungeon(
                        eval_comp,
                        spec,
                        required_role=str(v["role"]),
                        allow_pilot_shadow=is_sh,
                        raid_stage=raid_stage,
                        now_ts=ts,
                        tz=tz,
                        max_telemetry_age_sec=max_telemetry_age_sec,
                        world_ctx=world_ctx,
                        char_profile=prof,
                        enrolled_coop=eff_enrolled,
                        persona_state=eff_pstate,
                        char_active=eff_cactive,
                    )
                    if not ev["available"]:
                        if dec == "retry" and str(ev["reason"]).startswith(("insufficient_schedule_window", "insufficient_shift_time")):
                            avail_m = ev.get("available_minutes")
                            prep_m = int((ev.get("travel_prep") or {}).get("prep_minutes") or 0)
                            needed_retry = int(b_dict["remaining_dungeon_minutes"]) + prep_m + DEFAULT_TIME_BUFFER_MINUTES
                            if avail_m is not None and int(avail_m) >= needed_retry:
                                return True, "ok", eval_comp, ev
                        return False, ev["reason"], None, None
                    return True, "ok", eval_comp, ev

                if b_kind == "wipe_recovery":
                    retry_voters = [v for v in bot_votes if str(v["decision"]) == "retry"]
                    if live_disabled_reason:
                        outcome = "safe_exit_recovery"
                        outcome_reason = live_disabled_reason
                    elif int(b_dict["wipe_count"]) > int(b_dict["max_wipe_retries"]):
                        outcome = "safe_exit_recovery"
                        outcome_reason = f"wipe_retry_limit_reached:{b_dict['wipe_count']}/{b_dict['max_wipe_retries']}"
                    elif int(b_dict["technical_failure_count"]) >= DEFAULT_MAX_TECHNICAL_FAILURES:
                        outcome = "safe_exit_recovery"
                        outcome_reason = "technical_failure_cooldown"
                    elif len(retry_voters) != len(bot_votes):
                        leave_reasons = sorted({str(v["reason_code"] or "voluntary_leave") for v in bot_votes if str(v["decision"]) == "leave"})
                        outcome = "safe_exit_recovery"
                        outcome_reason = f"member_chose_leave:{','.join(leave_reasons)}"[:120]
                    else:
                        all_ok = True
                        for v in retry_voters:
                            ok_m, r_m, _, _ = _revalidate_member(v, "retry")
                            if not ok_m:
                                all_ok = False
                                revalidation_rejections.append({"persona_id": str(v["persona_id"]), "reason": r_m})
                        if all_ok:
                            outcome = "retry_approved"
                            outcome_reason = f"bounded_retry_{b_dict['wipe_count']}_of_{b_dict['max_wipe_retries']}:{b_dict['recovery_reason']}"
                        else:
                            outcome = "safe_exit_recovery"
                            outcome_reason = "retry_revalidation_failed:" + ",".join(
                                f"{x['persona_id']}:{x['reason']}" for x in revalidation_rejections
                            )[:90]

                elif b_kind == "leader_departure":
                    cont_voters = [v for v in bot_votes if str(v["decision"]) == "continue_reelect"]
                    req_voters = [v for v in bot_votes if str(v["decision"]) == "requeue"]
                    if live_disabled_reason:
                        outcome = "safe_exit_leader_departed"
                        outcome_reason = live_disabled_reason
                    elif cont_voters and len(cont_voters) == len(bot_votes):
                        valid_cont: list[dict[str, Any]] = []
                        for v in cont_voters:
                            ok_m, r_m, _, _ = _revalidate_member(v, "continue_reelect")
                            if ok_m:
                                valid_cont.append(v)
                            else:
                                revalidation_rejections.append({"persona_id": str(v["persona_id"]), "reason": r_m})
                        # Prefer human participant if present, else willing non-tank leader, else any willing leader
                        human_members = [v for v in votes if int(v["is_human"]) == 1]
                        willing_leaders = [v for v in valid_cont if int(v["willing_leader"]) == 1]
                        non_tank_leaders = [v for v in willing_leaders if str(v["role"]) != "tank"]
                        elected = (
                            human_members[0]
                            if human_members
                            else (non_tank_leaders[0] if non_tank_leaders else (willing_leaders[0] if willing_leaders else None))
                        )
                        if len(valid_cont) == len(bot_votes) and elected is not None:
                            new_leader_pid = str(elected["persona_id"])
                            outcome = "continue_with_new_leader"
                            outcome_reason = f"leader_reelected:{new_leader_pid}"
                        else:
                            outcome = "safe_exit_leader_departed"
                            outcome_reason = "no_qualified_willing_leader" if elected is None else "continue_revalidation_failed"
                    elif req_voters:
                        # P1-8: Account explicitly for continue_reelect voters who couldn't continue because teammates chose requeue/leave!
                        for cv in cont_voters:
                            continue_unfulfilled.append({
                                "persona_id": str(cv["persona_id"]),
                                "reason": "party_split_insufficient_continue_consensus",
                            })
                        outcome = "pending_requeue"
                    else:
                        for cv in cont_voters:
                            continue_unfulfilled.append({
                                "persona_id": str(cv["persona_id"]),
                                "reason": "party_split_members_left",
                            })
                        outcome = "safe_exit_leader_departed"
                        outcome_reason = "party_departed_after_leader_left"

                elif b_kind == "run_completed":
                    req_voters = [v for v in bot_votes if str(v["decision"]) == "requeue"]
                    if not req_voters:
                        outcome = "all_left_cleanly"
                        outcome_reason = "no_rerun_requested"
                    elif int(b_dict["rerun_chain_depth"]) >= int(b_dict["max_consecutive_reruns"]):
                        outcome = "all_left_cleanly"
                        outcome_reason = f"max_consecutive_reruns_reached:{b_dict['rerun_chain_depth']}"
                    else:
                        outcome = "pending_requeue"

                if outcome == "pending_requeue":
                    req_voters = [v for v in bot_votes if str(v["decision"]) == "requeue"]
                    gate_blocked = live_disabled_reason
                    if not gate_blocked and int(b_dict["rerun_chain_depth"]) >= int(b_dict["max_consecutive_reruns"]):
                        gate_blocked = f"max_consecutive_reruns_reached:{b_dict['rerun_chain_depth']}"
                    if not gate_blocked and int(b_dict["technical_failure_count"]) >= DEFAULT_MAX_TECHNICAL_FAILURES:
                        gate_blocked = "technical_failure_cooldown"
                    if not gate_blocked and not is_sh:
                        if opportunity_coordinator is None:
                            gate_blocked = "opportunity_coordinator_required_for_live"
                        elif headroom_state_path is None:
                            gate_blocked = "headroom_state_path_required_for_live"
                        else:
                            hr = evaluate_persisted_headroom(
                                headroom_state_path,
                                mem_avail_mib=mem_avail_mib,
                                tick_p99_ms=tick_p99_ms,
                                active_managed_groups=max(db_active_groups, int(active_managed_groups)),
                                max_managed_groups=max_managed_groups,
                                now_ts=ts,
                            )
                            if not hr["admit_ok"]:
                                gate_blocked = "resource_headroom_blocked"

                    if gate_blocked:
                        outcome = "requeue_blocked"
                        outcome_reason = gate_blocked
                    else:
                        valid_requeue: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
                        for v in req_voters:
                            ok_m, r_m, eval_comp, ev = _revalidate_member(v, "requeue")
                            if ok_m and eval_comp is not None and ev is not None:
                                valid_requeue.append((v, eval_comp, ev))
                            else:
                                revalidation_rejections.append({"persona_id": str(v["persona_id"]), "reason": r_m})

                        if valid_requeue:
                            max_ov = max(
                                int((ev.get("travel_prep") or {}).get("travel_minutes") or 0)
                                + int((ev.get("travel_prep") or {}).get("prep_minutes") or 0)
                                for _v, _c, ev in valid_requeue
                            )
                            needed_total = int(spec.estimated_minutes) + max_ov + DEFAULT_TIME_BUFFER_MINUTES
                            filtered_requeue: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
                            for v, eval_comp, ev in valid_requeue:
                                avail_m = ev.get("available_minutes")
                                if avail_m is None or int(avail_m) < needed_total:
                                    revalidation_rejections.append({
                                        "persona_id": str(v["persona_id"]),
                                        "reason": f"party_shift_window_insufficient:{avail_m}<{needed_total}",
                                    })
                                else:
                                    filtered_requeue.append((v, eval_comp, ev))
                            valid_requeue = filtered_requeue

                        if not valid_requeue:
                            outcome = "requeue_blocked"
                            outcome_reason = "no_eligible_requeue_volunteers"
                        elif opportunity_coordinator is not None:
                            # P0-3: Persist handoff intent BEFORE creating opportunity, then persist rerun_opportunity_id IMMEDIATELY!
                            h_key = f"cont:{int(boundary_id)}:ep{claimed_epoch}"
                            c_pre = self._conn()
                            try:
                                c_pre.execute("BEGIN IMMEDIATE")
                                c_pre.execute(
                                    "UPDATE continuation_boundaries SET handoff_phase='creating', handoff_key=?, updated_at=? "
                                    "WHERE id=? AND state='resolving' AND lease_epoch=?",
                                    (h_key, ts, int(boundary_id), claimed_epoch),
                                )
                                c_pre.execute("COMMIT")
                            finally:
                                c_pre.close()

                            creator_guid = int(valid_requeue[0][0]["character_guid"])
                            opp = opportunity_coordinator.create_opportunity(
                                dungeon_id=spec.dungeon_id,
                                creator_guid=creator_guid,
                                faction=str(b_dict["faction"]),
                                now_ts=ts,
                                is_shadow=is_sh,
                            )
                            if not isinstance(opp, dict) or not opp.get("id"):
                                raise RuntimeError("rerun_opportunity_creation_failed")
                            rerun_opp_id = int(opp["id"])

                            c_mid = self._conn()
                            try:
                                c_mid.execute("BEGIN IMMEDIATE")
                                c_mid.execute(
                                    "UPDATE continuation_boundaries SET handoff_phase='admitting', rerun_opportunity_id=?, updated_at=? "
                                    "WHERE id=? AND state='resolving' AND lease_epoch=?",
                                    (rerun_opp_id, ts, int(boundary_id), claimed_epoch),
                                )
                                c_mid.execute("COMMIT")
                            finally:
                                c_mid.close()

                            for v, eval_comp, _ev in valid_requeue:
                                adm = opportunity_coordinator.admit_candidate(
                                    opportunity_id=rerun_opp_id,
                                    expected_revision=int(opp["revision"]),
                                    persona_id=str(v["persona_id"]),
                                    character_guid=int(v["character_guid"]),
                                    character_slug=str(v["character_slug"]),
                                    role=str(v["role"]),
                                    reason_code=f"voluntary_rerun_depth_{int(b_dict['rerun_chain_depth']) + 1}",
                                    candidate={**eval_comp, "realm_id": int(v["realm_id"])},
                                    raid_stage=raid_stage,
                                    now_ts=ts,
                                    tz=tz,
                                    allow_legacy_no_registry=allow_legacy_no_registry,
                                )
                                if adm["ok"]:
                                    admitted_rerun_personas.append(str(v["persona_id"]))
                                else:
                                    revalidation_rejections.append({
                                        "persona_id": str(v["persona_id"]),
                                        "reason": adm["reason"],
                                    })
                            if not admitted_rerun_personas:
                                opportunity_coordinator.release_party(rerun_opp_id, "rerun_zero_admitted", now_ts=ts)
                                rerun_opp_id = 0
                                outcome = "requeue_blocked"
                                outcome_reason = "rerun_admission_failed"
                            else:
                                outcome = (
                                    "full_party_requeued"
                                    if len(admitted_rerun_personas) == len(bot_votes) and not int(b_dict["has_human_participant"])
                                    else "partial_party_requeued"
                                )
                                outcome_reason = f"voluntary_rerun_chain_{int(b_dict['rerun_chain_depth']) + 1}"
                        else:
                            admitted_rerun_personas = [str(v["persona_id"]) for v, _c, _e in valid_requeue]
                            outcome = (
                                "full_party_requeued"
                                if len(admitted_rerun_personas) == len(bot_votes) and not int(b_dict["has_human_participant"])
                                else "partial_party_requeued"
                            )
                            outcome_reason = f"voluntary_rerun_ready_chain_{int(b_dict['rerun_chain_depth']) + 1}"
        except Exception:
            if opportunity_coordinator is not None and rerun_opp_id > 0:
                try:
                    opportunity_coordinator.release_party(rerun_opp_id, "rerun_handoff_exception", now_ts=ts)
                except Exception as comp_exc:
                    logger.warning("Compensation release_party failed for rerun opp %s: %s", rerun_opp_id, comp_exc)
            _revert_resolving_on_error()
            raise

        # Step 3: Drain any overrides that arrived DURING resolution + enforce rowcount == 1 CAS commit (P0-2 & P1-11!)
        conn = self._conn()
        mid_recall_withdraws: list[tuple[str, str]] = []
        release_entire_rerun = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            late_ovs = conn.execute(
                "SELECT * FROM continuation_overrides WHERE boundary_id=? AND applied_at=0 ORDER BY id ASC",
                (int(boundary_id),),
            ).fetchall()
            if late_ovs:
                lfg_warn_inc = 1 if b_kind in ("wipe_recovery", "leader_departure") else 0
                for ov in late_ovs:
                    opid = str(ov["persona_id"])
                    okind = str(ov["kind"])
                    conn.execute("UPDATE continuation_overrides SET applied_at=? WHERE id=?", (ts, int(ov["id"])))
                    conn.execute(
                        "UPDATE continuation_votes SET decision='leave', reason_code=?, lfg_penalty_warning=?, voted_at=?, updated_at=? "
                        "WHERE boundary_id=? AND persona_id=?",
                        (okind, lfg_warn_inc, ts, ts, int(boundary_id), opid),
                    )
                    if opid not in leaving_personas:
                        leaving_personas.append(opid)
                    if opid in admitted_rerun_personas:
                        admitted_rerun_personas.remove(opid)
                        mid_recall_withdraws.append((opid, okind))
                    if outcome == "retry_approved":
                        outcome = "safe_exit_recovery"
                        outcome_reason = f"mid_resolve_{okind}:{opid}"
                    elif outcome == "continue_with_new_leader":
                        outcome = "safe_exit_leader_departed"
                        outcome_reason = f"mid_resolve_{okind}:{opid}"
                        new_leader_pid = ""

                if mid_recall_withdraws:
                    if not admitted_rerun_personas:
                        release_entire_rerun = True
                        outcome = "requeue_blocked"
                        outcome_reason = "all_requeued_recalled_during_resolution"
                    else:
                        outcome = "partial_party_requeued"
                        outcome_reason = f"voluntary_rerun_chain_{int(b_dict['rerun_chain_depth']) + 1}_after_recall"

            final_rev = claimed_rev + 1
            stored_rerun_id = 0 if release_entire_rerun else rerun_opp_id
            updated = conn.execute(
                """
                UPDATE continuation_boundaries
                SET state='resolved', resolving_until=0, handoff_phase='done', outcome=?, outcome_reason=?,
                    new_leader_persona_id=?, rerun_opportunity_id=?, ended_at=?, updated_at=?, revision=?
                WHERE id=? AND state='resolving' AND revision=? AND lease_epoch=?
                """,
                (
                    outcome,
                    outcome_reason[:120],
                    new_leader_pid,
                    stored_rerun_id,
                    ts,
                    ts,
                    final_rev,
                    int(boundary_id),
                    claimed_rev,
                    claimed_epoch,
                ),
            ).rowcount
            if updated != 1:
                conn.execute("ROLLBACK")
                if opportunity_coordinator is not None and rerun_opp_id > 0:
                    opportunity_coordinator.release_party(rerun_opp_id, "resolution_lost_lease", now_ts=ts)
                return {"ok": False, "reason": "resolution_lost_lease"}

            self._log_event(
                conn,
                event_type="boundary_resolved",
                boundary_id=int(boundary_id),
                opportunity_id=int(b_dict["opportunity_id"]),
                is_shadow=is_sh,
                reason_code=f"{outcome}:{outcome_reason}"[:120],
                details={
                    "leaving_personas": leaving_personas,
                    "admitted_rerun_personas": admitted_rerun_personas,
                    "continue_reelect_unfulfilled": continue_unfulfilled,
                    "revalidation_rejections": revalidation_rejections,
                    "new_leader_persona_id": new_leader_pid,
                    "rerun_opportunity_id": stored_rerun_id,
                },
                now_ts=ts,
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            if opportunity_coordinator is not None and rerun_opp_id > 0:
                try:
                    opportunity_coordinator.release_party(rerun_opp_id, "resolution_commit_exception", now_ts=ts)
                except Exception as comp_exc:
                    logger.warning("Compensation release_party failed after commit error: %s", comp_exc)
            raise
        finally:
            conn.close()

        # Withdraw recalled members (or release entire rerun opp) on OpportunityCoordinator cleanly outside SQLite lock
        if opportunity_coordinator is not None and rerun_opp_id > 0:
            if release_entire_rerun:
                opportunity_coordinator.release_party(rerun_opp_id, "all_requeued_recalled_during_resolution", now_ts=ts)
            else:
                for opid, okind in mid_recall_withdraws:
                    opportunity_coordinator.withdraw_interest(rerun_opp_id, opid, reason=okind, now_ts=ts)

        return {
            "ok": True,
            "boundary_id": int(boundary_id),
            "state": "resolved",
            "revision": final_rev,
            "outcome": outcome,
            "outcome_reason": outcome_reason,
            "leaving_personas": leaving_personas,
            "admitted_rerun_personas": admitted_rerun_personas,
            "continue_reelect_unfulfilled": continue_unfulfilled,
            "revalidation_rejections": revalidation_rejections,
            "new_leader_persona_id": new_leader_pid,
            "rerun_opportunity_id": 0 if release_entire_rerun else rerun_opp_id,
        }
