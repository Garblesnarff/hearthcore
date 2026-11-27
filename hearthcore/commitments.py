"""Phase II Future Voluntary Dungeon Commitments Across Staggered Shifts (Issue #18).

Design guarantees:
- Future dungeon intents are durable across sessions so personas on staggered shifts can discover
  each other's voluntary interest WITHOUT fictitious offline play or offline LFG queueing.
- Compatible availability windows are computed from actual schedules (`enumerate_schedule_windows`)
  and verified against concurrency caps (`max_concurrent_online`) and managed group capacity
  (`max_managed_groups`) across the EXACT stored window `[window_start, window_end)`.
  No forced attendance, no forced concurrency increase, no silent shift expansion.
- Bounded schedule adjustments are DISABLED by default (`ScheduleAdjustmentPolicy(enabled=False)`)
  and allowed ONLY when an explicitly reviewed policy (`policy_id`, `reviewed_by`, strict bounds)
  is supplied AND the persona opted in (`allow_bounded_adjustment=True`) AND the slide's removed
  time does not overlap existing commitments AND weekly per-lane quota & concurrency capacity hold.
- Declines, withdrawals, expiries, and owner/readiness overrides are recorded NEUTRALLY
  (`penalty_applied = 0`) and refund any unused weekly adjustment quota.
- Activation uses an atomic `scheduled -> activating` CAS lease with crash reconciliation
  (`reconcile_activating`), transient vs terminal revalidation handling, full `PersonaRegistry` +
  live dungeon/role/schedule/headroom verification, and full compensation on partial admission failure.
- Native LFG / `OpportunityCoordinator` remains actual matchmaking; cooperative live execution stays
  gated behind `coop_enabled()` and `enabled_dungeon_ids()`. Runs off the game thread.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .core import profile, profiles
from .dungeons import (
    CLASS_CAPABLE_ROLES,
    DUNGEON_BY_ID,
    enabled_dungeon_ids,
    enumerate_schedule_windows,
)
from .identity import PersonaRegistry, is_protected_owner_group
from .opportunities import (
    DEFAULT_MAX_GROUPS,
    OpportunityCoordinator,
    coop_enabled,
    evaluate_persisted_headroom,
    validate_candidate_for_dungeon,
)

logger = logging.getLogger("hearthcore.commitments")

SCHEMA_VERSION = 1
DEFAULT_INTENT_TTL_SEC = 48 * 3600
MAX_INTENT_TTL_SEC = 72 * 3600
DEFAULT_ACCEPT_TTL_SEC = 6 * 3600
DEFAULT_HORIZON_HOURS = 48
DEFAULT_WINDOW_BUFFER_MINUTES = 10
DEFAULT_ACTIVATION_GRACE_SEC = 5 * 60
DEFAULT_ACTIVATING_LEASE_SEC = 2 * 60

# Hard constitutional ceilings on reviewed schedule adjustment policies (never silent shift expansion!)
MAX_POLICY_SHIFT_MINUTES = 30
MAX_POLICY_NET_EXPANSION_MINUTES = 15
MAX_POLICY_ADJUSTMENTS_PER_WEEK = 2

VALID_ROLES = frozenset({"tank", "healer", "damage"})
VALID_OVERRIDE_KINDS = frozenset({"owner_recall", "raid_readiness"})
TRANSIENT_REVALIDATION_REASONS = frozenset({
    "offline",
    "offline_or_missing_in_world_ctx",
    "in_combat",
    "in_group",
    "stale_or_missing_telemetry",
    "dead_needs_resurrection",
    "paused",
})


@dataclass(frozen=True)
class ScheduleAdjustmentPolicy:
    """Explicitly reviewed policy for bounded voluntary shift adjustment (default OFF)."""
    enabled: bool = False
    policy_id: str = ""
    reviewed_by: str = ""
    max_shift_start_early_minutes: int = 0
    max_shift_end_extend_minutes: int = 0
    preserve_total_shift_duration: bool = True
    allow_net_expansion: bool = False
    max_net_expansion_minutes: int = 0
    max_adjustments_per_persona_per_week: int = 1
    respect_concurrency_cap: bool = True


def _is_strict_int(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def validate_adjustment_policy(policy: ScheduleAdjustmentPolicy | None) -> tuple[bool, str]:
    """Validate a ScheduleAdjustmentPolicy fail-closed with strict type checks."""
    if policy is None or not isinstance(policy, ScheduleAdjustmentPolicy):
        return False, "policy_disabled"
    if not isinstance(policy.enabled, bool) or not policy.enabled:
        return False, "policy_disabled"
    if not isinstance(policy.policy_id, str) or not policy.policy_id.strip():
        return False, "policy_missing_id"
    if not isinstance(policy.reviewed_by, str) or not policy.reviewed_by.strip():
        return False, "policy_unreviewed"
    if not _is_strict_int(policy.max_shift_start_early_minutes) or not _is_strict_int(policy.max_shift_end_extend_minutes):
        return False, "policy_invalid_int_bounds"
    if not _is_strict_int(policy.max_net_expansion_minutes) or not _is_strict_int(policy.max_adjustments_per_persona_per_week):
        return False, "policy_invalid_int_bounds"
    if not isinstance(policy.preserve_total_shift_duration, bool) or not isinstance(policy.allow_net_expansion, bool):
        return False, "policy_invalid_bool_flags"
    if not isinstance(policy.respect_concurrency_cap, bool) or not policy.respect_concurrency_cap:
        return False, "policy_must_respect_concurrency_cap"

    if policy.max_shift_start_early_minutes < 0 or policy.max_shift_end_extend_minutes < 0:
        return False, "policy_negative_bounds"
    if (
        policy.max_shift_start_early_minutes > MAX_POLICY_SHIFT_MINUTES
        or policy.max_shift_end_extend_minutes > MAX_POLICY_SHIFT_MINUTES
    ):
        return False, f"policy_exceeds_max_shift:{MAX_POLICY_SHIFT_MINUTES}m"
    if policy.max_shift_start_early_minutes == 0 and policy.max_shift_end_extend_minutes == 0:
        return False, "policy_zero_bounds"
    if policy.preserve_total_shift_duration:
        if policy.allow_net_expansion or policy.max_net_expansion_minutes > 0:
            return False, "policy_conflicting_expansion_flags"
    else:
        if not policy.allow_net_expansion:
            return False, "policy_net_expansion_not_explicitly_allowed"
        if policy.max_net_expansion_minutes <= 0 or policy.max_net_expansion_minutes > MAX_POLICY_NET_EXPANSION_MINUTES:
            return False, f"policy_exceeds_max_net_expansion:{MAX_POLICY_NET_EXPANSION_MINUTES}m"
    if not (1 <= policy.max_adjustments_per_persona_per_week <= MAX_POLICY_ADJUSTMENTS_PER_WEEK):
        return False, f"policy_invalid_weekly_limit:{policy.max_adjustments_per_persona_per_week}"
    return True, "ok"


def _intersect_two_window_lists(
    a: list[tuple[int, int]],
    b: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    i = 0
    j = 0
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if e > s:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _intervals_overlap(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> bool:
    for s1, e1 in a:
        for s2, e2 in b:
            if min(e1, e2) > max(s1, s2):
                return True
    return False


def _subtract_blocked_intervals(
    windows: list[tuple[int, int]],
    blocked: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    if not windows or not blocked:
        return list(windows)
    cur = list(windows)
    for bs, be in sorted(blocked):
        if be <= bs:
            continue
        nxt: list[tuple[int, int]] = []
        for ws, we in cur:
            if be <= ws or bs >= we:
                nxt.append((ws, we))
            else:
                if bs > ws:
                    nxt.append((ws, bs))
                if be < we:
                    nxt.append((be, we))
        cur = nxt
    return cur


def _adjust_natural_windows_with_policy(
    natural_windows: list[tuple[int, int]],
    policy: ScheduleAdjustmentPolicy,
    *,
    min_ts: int,
    max_ts: int,
    busy_windows: list[tuple[int, int]],
) -> list[tuple[int, int, dict[str, Any]]]:
    """Compute bounded adjusted windows from UNCLAMPED natural shifts (P1-4 fix!).

    Checks that any time REMOVED by a shift slide does NOT intersect `busy_windows`
    (existing commitments of this persona) before returning the adjusted shift!
    """
    out: list[tuple[int, int, dict[str, Any]]] = []
    early_sec = int(policy.max_shift_start_early_minutes) * 60
    extend_sec = int(policy.max_shift_end_extend_minutes) * 60
    max_exp_sec = int(policy.max_net_expansion_minutes) * 60 if not policy.preserve_total_shift_duration else 0

    for ws, we in natural_windows:
        # 0. Natural shift (unadjusted)
        out.append((
            ws,
            we,
            {
                "adjusted": False,
                "start_early_min": 0,
                "end_extend_min": 0,
                "net_expansion_min": 0,
                "added_intervals": [],
                "removed_intervals": [],
                "adjusted_window": [ws, we],
                "base_window": [ws, we],
            },
        ))

        # 1. Slide/start earlier by early_sec
        if early_sec > 0:
            ns = max(min_ts, ws - early_sec)
            actual_early = ws - ns
            if actual_early > 0:
                if policy.preserve_total_shift_duration:
                    ne = we - actual_early
                    net_exp = 0
                else:
                    net_exp = min(actual_early, max_exp_sec)
                    ne = we - (actual_early - net_exp)
                removed = [(ne, we)] if ne < we else []
                if ne > ns and not _intervals_overlap(removed, busy_windows):
                    out.append((
                        ns,
                        ne,
                        {
                            "adjusted": True,
                            "start_early_min": actual_early // 60,
                            "end_extend_min": 0,
                            "net_expansion_min": net_exp // 60,
                            "added_intervals": [[ns, ws]],
                            "removed_intervals": [[rs, re] for rs, re in removed],
                            "adjusted_window": [ns, ne],
                            "base_window": [ws, we],
                        },
                    ))

        # 2. Slide/extend later by extend_sec
        if extend_sec > 0:
            ne = min(max_ts, we + extend_sec)
            actual_ext = ne - we
            if actual_ext > 0:
                if policy.preserve_total_shift_duration:
                    ns = ws + actual_ext
                    net_exp = 0
                else:
                    net_exp = min(actual_ext, max_exp_sec)
                    ns = ws + (actual_ext - net_exp)
                removed = [(ws, ns)] if ns > ws else []
                if ne > ns and not _intervals_overlap(removed, busy_windows):
                    out.append((
                        ns,
                        ne,
                        {
                            "adjusted": True,
                            "start_early_min": 0,
                            "end_extend_min": actual_ext // 60,
                            "net_expansion_min": net_exp // 60,
                            "added_intervals": [[we, ne]],
                            "removed_intervals": [[rs, re] for rs, re in removed],
                            "adjusted_window": [ns, ne],
                            "base_window": [ws, we],
                        },
                    ))
    return out


def _peak_concurrency_in_interval(
    interval_start: int,
    interval_end: int,
    roster_windows: dict[Any, list[tuple[int, int]]],
    extra_intervals_by_key: dict[Any, list[tuple[int, int]]] | None = None,
) -> int:
    """Compute exact peak concurrent online characters during [interval_start, interval_end) via sweep-line."""
    if interval_end <= interval_start:
        return 0
    events: list[tuple[int, int]] = []
    all_keys = set(roster_windows.keys())
    if extra_intervals_by_key:
        all_keys |= set(extra_intervals_by_key.keys())

    for key in all_keys:
        segs = list(roster_windows.get(key) or [])
        if extra_intervals_by_key and key in extra_intervals_by_key:
            segs.extend(extra_intervals_by_key[key])
        clipped: list[tuple[int, int]] = []
        for s, e in segs:
            cs = max(interval_start, int(s))
            ce = min(interval_end, int(e))
            if ce > cs:
                clipped.append((cs, ce))
        if not clipped:
            continue
        clipped.sort()
        merged: list[tuple[int, int]] = [clipped[0]]
        for s, e in clipped[1:]:
            if s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        for s, e in merged:
            events.append((s, 1))
            events.append((e, -1))

    events.sort(key=lambda x: (x[0], x[1]))
    cur = 0
    peak = 0
    for _, delta in events:
        cur += delta
        if cur > peak:
            peak = cur
    return peak


def _peak_overlapping_groups(
    interval_start: int,
    interval_end: int,
    existing_windows: list[tuple[int, int]],
) -> int:
    """Exact peak count of existing committed/active windows overlapping [interval_start, interval_end)."""
    if interval_end <= interval_start or not existing_windows:
        return 0
    events: list[tuple[int, int]] = []
    for ws, we in existing_windows:
        cs = max(interval_start, int(ws))
        ce = min(interval_end, int(we))
        if ce > cs:
            events.append((cs, 1))
            events.append((ce, -1))
    events.sort(key=lambda x: (x[0], x[1]))
    cur = 0
    peak = 0
    for _, d in events:
        cur += d
        if cur > peak:
            peak = cur
    return peak


def _parse_realm_id(raw_realm: Any, *, default_when_none: int | None = 1) -> int | None:
    """Validate realm_id strictly (rejects <=0, bool, non-int; never coerces 0 -> 1)."""
    if raw_realm is None:
        return default_when_none
    if isinstance(raw_realm, bool):
        return None
    try:
        rid = int(raw_realm)
    except (TypeError, ValueError):
        return None
    return rid if rid > 0 else None


def _lookup_schedule_for_member(
    roster_schedules: dict[Any, dict[str, Any]],
    *,
    realm_id: int,
    character_guid: int,
    persona_id: str,
) -> tuple[Any, dict[str, Any] | None]:
    """Support character-keyed `(realm_id, guid)` schedules first, falling back to `persona_id` (P2-13)."""
    char_key = (int(realm_id), int(character_guid))
    if char_key in roster_schedules:
        return char_key, roster_schedules[char_key]
    if persona_id in roster_schedules:
        return persona_id, roster_schedules[persona_id]
    return char_key, None


def _verify_persona_with_snapshot(
    psnap: dict[str, Any],
    *,
    persona_id: str,
    realm_id: int,
    character_guid: int,
    allow_legacy_no_registry: bool,
    is_shadow: bool = True,
    shift_group: str = "",
    enforce_single_session_on_char: bool = False,
    expected_incarnation: int | None = None,
) -> tuple[bool, str, int]:
    """Single source of truth for PersonaRegistry verification across advertise, propose & revalidate."""
    if not psnap["ok"]:
        return False, "persona_registry_unreadable", 1
    if not psnap["has_registry"]:
        if not is_shadow:
            return False, "legacy_no_registry_forbidden_live", 1
        if not allow_legacy_no_registry:
            return False, "persona_registry_missing", 1
        if is_protected_owner_group(shift_group):
            return False, "protected_core_excluded", 1
        return True, "legacy_ok", int(expected_incarnation or 1)

    if realm_id <= 0:
        return False, "invalid_realm_id", 1
    prec = psnap["by_key"].get((int(realm_id), int(character_guid)))
    if prec is None:
        return False, "unresolved_persona_mapping", 1
    if prec["persona_id"] != persona_id:
        return False, "persona_id_mismatch", 1
    if prec["state"] != "active":
        return False, f"quarantined_persona:{prec['state']}", 1
    if not prec["char_active"]:
        return False, "inactive_persona_character", 1
    if prec["core_protected"] and not prec["enrolled_coop"]:
        return False, "protected_core_excluded", 1
    if not prec["enrolled_coop"]:
        return False, "not_enrolled_coop", 1
    if persona_id in psnap["open_dual_session_personas"]:
        return False, "persona_dual_session_conflict", 1
    verified_inc = int(prec["incarnation"])
    if expected_incarnation is not None and int(expected_incarnation) != verified_inc:
        return False, "incarnation_mismatch", verified_inc
    if enforce_single_session_on_char:
        active_sess = psnap["by_persona_session"].get(persona_id)
        if active_sess is not None and active_sess != (int(realm_id), int(character_guid), verified_inc):
            return False, "persona_session_on_other_character", verified_inc
    return True, "ok", verified_inc


class FutureCommitmentCoordinator:
    """Transactional SQLite coordinator for cross-shift future voluntary dungeon commitments (Issue #18)."""

    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._conn()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS future_dungeon_intents (
                    id INTEGER PRIMARY KEY,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    persona_id TEXT NOT NULL,
                    realm_id INTEGER NOT NULL DEFAULT 1,
                    character_guid INTEGER NOT NULL,
                    incarnation INTEGER NOT NULL DEFAULT 1,
                    character_slug TEXT NOT NULL,
                    faction TEXT NOT NULL,
                    dungeon_id TEXT NOT NULL,
                    roles_json TEXT NOT NULL,
                    reason_code TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    earliest_start_ts INTEGER NOT NULL,
                    latest_end_ts INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    allow_bounded_adjustment INTEGER NOT NULL DEFAULT 0,
                    is_shadow INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    closed_reason TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS future_commitments (
                    id INTEGER PRIMARY KEY,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 1,
                    dungeon_id TEXT NOT NULL,
                    faction TEXT NOT NULL,
                    window_start INTEGER NOT NULL,
                    window_end INTEGER NOT NULL,
                    estimated_minutes INTEGER NOT NULL,
                    required_minutes INTEGER NOT NULL DEFAULT 0,
                    accept_until INTEGER NOT NULL,
                    activating_until INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    pending_override TEXT NOT NULL DEFAULT '',
                    pending_override_target TEXT NOT NULL DEFAULT '',
                    override_requested_at INTEGER NOT NULL DEFAULT 0,
                    adjustment_policy_id TEXT NOT NULL DEFAULT '',
                    adjustment_reviewed_by TEXT NOT NULL DEFAULT '',
                    adjustment_applied_json TEXT NOT NULL DEFAULT '{}',
                    linked_opportunity_id INTEGER NOT NULL DEFAULT 0,
                    outcome_reason TEXT NOT NULL DEFAULT '',
                    is_shadow INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    ended_at INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS future_commitment_members (
                    id INTEGER PRIMARY KEY,
                    commitment_id INTEGER NOT NULL,
                    intent_id INTEGER NOT NULL DEFAULT 0,
                    persona_id TEXT NOT NULL,
                    realm_id INTEGER NOT NULL DEFAULT 1,
                    character_guid INTEGER NOT NULL,
                    incarnation INTEGER NOT NULL DEFAULT 1,
                    character_slug TEXT NOT NULL,
                    role TEXT NOT NULL,
                    state TEXT NOT NULL,
                    adjusted_minutes INTEGER NOT NULL DEFAULT 0,
                    adjustment_json TEXT NOT NULL DEFAULT '{}',
                    response_reason TEXT NOT NULL DEFAULT '',
                    responded_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(commitment_id, persona_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS future_commitment_events (
                    id INTEGER PRIMARY KEY,
                    commitment_id INTEGER NOT NULL DEFAULT 0,
                    intent_id INTEGER NOT NULL DEFAULT 0,
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
            # Additive column migrations for any pre-existing tables
            fc_cols = {r[1] for r in conn.execute("PRAGMA table_info(future_commitments)").fetchall()}
            for col, typ in (
                ("required_minutes", "INTEGER NOT NULL DEFAULT 0"),
                ("activating_until", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_override_target", "TEXT NOT NULL DEFAULT ''"),
            ):
                if col not in fc_cols:
                    conn.execute(f"ALTER TABLE future_commitments ADD COLUMN {col} {typ}")

            fce_cols = {r[1] for r in conn.execute("PRAGMA table_info(future_commitment_events)").fetchall()}
            if "is_shadow" not in fce_cols:
                conn.execute("ALTER TABLE future_commitment_events ADD COLUMN is_shadow INTEGER NOT NULL DEFAULT 1")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fdi_lookup "
                "ON future_dungeon_intents(state, is_shadow, dungeon_id, expires_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fdi_persona "
                "ON future_dungeon_intents(persona_id, state, is_shadow)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_fdi_unique_active "
                "ON future_dungeon_intents(persona_id, dungeon_id, is_shadow) "
                "WHERE state IN ('open', 'matched')"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fc_state_window "
                "ON future_commitments(state, is_shadow, window_start, window_end)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fcm_persona_state "
                "ON future_commitment_members(persona_id, state)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fce_persona_type "
                "ON future_commitment_events(persona_id, is_shadow, event_type, created_at)"
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
        is_shadow: bool = True,
        commitment_id: int = 0,
        intent_id: int = 0,
        persona_id: str = "",
        reason_code: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO future_commitment_events (
                commitment_id, intent_id, persona_id, is_shadow, event_type, reason_code,
                neutral, penalty_applied, details_json, created_at
            ) VALUES (?,?,?,?,?,?,1,0,?,?)
            """,
            (
                int(commitment_id),
                int(intent_id),
                str(persona_id or ""),
                1 if is_shadow else 0,
                str(event_type)[:64],
                str(reason_code or "")[:120],
                json.dumps(details or {}, sort_keys=True),
                int(now_ts),
            ),
        )

    def _weekly_adjustment_usage(
        self,
        conn: sqlite3.Connection,
        *,
        is_shadow: bool,
        now_ts: int,
        exclude_commitment_id: int = 0,
    ) -> dict[str, int]:
        """Count active/in-flight/completed bounded adjustments per persona within the last 7 days in ONE lane.

        P1-1 & P1-2 guarantee:
        - Includes in-flight ('proposed', 'scheduled', 'activating') AND 'activated'/'completed' rows
          so two concurrent proposals cannot double-spend quota.
        - Automatically refunds quota whenever a commitment ends as declined/expired/cancelled/overridden!
        - Strictly lane-isolated (`c.is_shadow = ?`).
        """
        cutoff = int(now_ts) - (7 * 86400)
        want_shadow = 1 if is_shadow else 0
        rows = conn.execute(
            """
            SELECT m.persona_id, COUNT(*) AS cnt
            FROM future_commitment_members m
            JOIN future_commitments c ON c.id = m.commitment_id
            WHERE c.is_shadow = ?
              AND c.id != ?
              AND m.adjusted_minutes > 0
              AND c.state IN ('proposed', 'scheduled', 'activating', 'activated', 'completed')
              AND m.state IN ('offered', 'accepted', 'activated', 'completed')
              AND c.created_at >= ?
            GROUP BY m.persona_id
            """,
            (want_shadow, int(exclude_commitment_id), cutoff),
        ).fetchall()
        return {str(r["persona_id"]): int(r["cnt"]) for r in rows}

    def _log_adjustment_refunds_if_any(
        self,
        conn: sqlite3.Connection,
        commitment_id: int,
        *,
        is_shadow: bool,
        reason_code: str,
        now_ts: int,
    ) -> None:
        rows = conn.execute(
            "SELECT persona_id, adjusted_minutes FROM future_commitment_members WHERE commitment_id=? AND adjusted_minutes > 0",
            (int(commitment_id),),
        ).fetchall()
        for r in rows:
            self._log_event(
                conn,
                event_type="bounded_adjustment_refunded",
                commitment_id=int(commitment_id),
                persona_id=str(r["persona_id"]),
                is_shadow=is_shadow,
                reason_code=reason_code,
                details={"adjusted_minutes": int(r["adjusted_minutes"])},
                now_ts=now_ts,
            )

    def _reopen_intent_if_valid(self, conn: sqlite3.Connection, intent_id: int, now_ts: int) -> None:
        if intent_id <= 0:
            return
        row = conn.execute(
            "SELECT persona_id, dungeon_id, is_shadow, state, expires_at, latest_end_ts FROM future_dungeon_intents WHERE id=?",
            (int(intent_id),),
        ).fetchone()
        if not row or str(row["state"]) != "matched":
            return
        if int(row["expires_at"]) <= now_ts or int(row["latest_end_ts"]) <= now_ts:
            conn.execute(
                "UPDATE future_dungeon_intents SET state='expired', closed_reason='intent_ttl_expired', updated_at=? WHERE id=?",
                (now_ts, int(intent_id)),
            )
            return
        # Guard against unique-index conflict if persona already opened another active intent
        dup = conn.execute(
            "SELECT 1 FROM future_dungeon_intents WHERE persona_id=? AND dungeon_id=? AND is_shadow=? AND state IN ('open','matched') AND id != ?",
            (str(row["persona_id"]), str(row["dungeon_id"]), int(row["is_shadow"]), int(intent_id)),
        ).fetchone()
        if dup:
            conn.execute(
                "UPDATE future_dungeon_intents SET state='superseded', closed_reason='newer_intent_exists', updated_at=? WHERE id=?",
                (now_ts, int(intent_id)),
            )
            return
        conn.execute(
            "UPDATE future_dungeon_intents SET state='open', updated_at=?, closed_reason='' WHERE id=?",
            (now_ts, int(intent_id)),
        )

    def _close_intent(self, conn: sqlite3.Connection, intent_id: int, *, state: str, reason: str, now_ts: int) -> None:
        if intent_id <= 0:
            return
        conn.execute(
            "UPDATE future_dungeon_intents SET state=?, closed_reason=?, updated_at=? WHERE id=? AND state IN ('open','matched')",
            (str(state)[:32], str(reason)[:64], int(now_ts), int(intent_id)),
        )

    def _expire_stale_in_tx(self, conn: sqlite3.Connection, now_ts: int) -> dict[str, int]:
        """Expire past-TTL intents and proposed/scheduled commitments neutrally inside open IMMEDIATE tx."""
        exp_intents = conn.execute(
            """
            SELECT id, persona_id, is_shadow FROM future_dungeon_intents
            WHERE state = 'open' AND (expires_at <= ? OR latest_end_ts <= ?)
            """,
            (now_ts, now_ts),
        ).fetchall()
        for row in exp_intents:
            conn.execute(
                "UPDATE future_dungeon_intents SET state='expired', closed_reason='intent_ttl_expired', updated_at=? WHERE id=?",
                (now_ts, int(row["id"])),
            )
            self._log_event(
                conn,
                event_type="intent_expired",
                intent_id=int(row["id"]),
                persona_id=str(row["persona_id"]),
                is_shadow=bool(int(row["is_shadow"])),
                reason_code="intent_ttl_expired",
                now_ts=now_ts,
            )

        exp_props = conn.execute(
            """
            SELECT id, window_end, estimated_minutes, required_minutes, is_shadow FROM future_commitments
            WHERE state = 'proposed'
              AND (
                  accept_until <= ?
                  OR window_end <= ?
                  OR (window_end - ?) < (CASE WHEN required_minutes > 0 THEN required_minutes ELSE estimated_minutes END * 60)
              )
            """,
            (now_ts, now_ts, now_ts),
        ).fetchall()
        for row in exp_props:
            cid = int(row["id"])
            is_sh = bool(int(row["is_shadow"]))
            members = conn.execute(
                "SELECT id, intent_id, persona_id, state FROM future_commitment_members WHERE commitment_id=?",
                (cid,),
            ).fetchall()
            for m in members:
                if str(m["state"]) in ("offered", "accepted"):
                    new_mst = "expired" if str(m["state"]) == "offered" else "released"
                    conn.execute(
                        "UPDATE future_commitment_members SET state=?, response_reason='accept_window_expired', updated_at=? WHERE id=?",
                        (new_mst, now_ts, int(m["id"])),
                    )
                    self._reopen_intent_if_valid(conn, int(m["intent_id"]), now_ts)
            conn.execute(
                "UPDATE future_commitments SET state='expired', outcome_reason='accept_window_expired', "
                "ended_at=?, updated_at=?, revision=revision+1 WHERE id=?",
                (now_ts, now_ts, cid),
            )
            self._log_adjustment_refunds_if_any(conn, cid, is_shadow=is_sh, reason_code="accept_window_expired", now_ts=now_ts)
            self._log_event(
                conn,
                event_type="commitment_expired",
                commitment_id=cid,
                is_shadow=is_sh,
                reason_code="accept_window_expired",
                now_ts=now_ts,
            )

        exp_sched = conn.execute(
            """
            SELECT id, is_shadow FROM future_commitments
            WHERE state = 'scheduled'
              AND (
                  window_end <= ?
                  OR (window_end - ?) < (CASE WHEN required_minutes > 0 THEN required_minutes ELSE estimated_minutes END * 60)
              )
            """,
            (now_ts, now_ts),
        ).fetchall()
        for row in exp_sched:
            cid = int(row["id"])
            is_sh = bool(int(row["is_shadow"]))
            members = conn.execute(
                "SELECT id, intent_id, persona_id FROM future_commitment_members WHERE commitment_id=? AND state='accepted'",
                (cid,),
            ).fetchall()
            for m in members:
                conn.execute(
                    "UPDATE future_commitment_members SET state='expired', response_reason='scheduled_window_expired', updated_at=? WHERE id=?",
                    (now_ts, int(m["id"])),
                )
                self._reopen_intent_if_valid(conn, int(m["intent_id"]), now_ts)
            conn.execute(
                "UPDATE future_commitments SET state='expired', outcome_reason='scheduled_window_expired', "
                "ended_at=?, updated_at=?, revision=revision+1 WHERE id=?",
                (now_ts, now_ts, cid),
            )
            self._log_adjustment_refunds_if_any(conn, cid, is_shadow=is_sh, reason_code="scheduled_window_expired", now_ts=now_ts)
            self._log_event(
                conn,
                event_type="commitment_expired",
                commitment_id=cid,
                is_shadow=is_sh,
                reason_code="scheduled_window_expired",
                now_ts=now_ts,
            )

        return {
            "expired_intents": len(exp_intents),
            "expired_proposed": len(exp_props),
            "expired_scheduled": len(exp_sched),
        }

    def expire_stale(self, now_ts: int | None = None) -> dict[str, int]:
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            res = self._expire_stale_in_tx(conn, ts)
            conn.execute("COMMIT")
            return res
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def advertise_future_intent(
        self,
        *,
        persona_id: str,
        character_guid: int,
        character_slug: str,
        faction: str,
        dungeon_id: str,
        roles: list[str] | tuple[str, ...] | set[str],
        class_name: str | None = None,
        reason_code: str = "future_dungeon_interest",
        realm_id: int = 1,
        earliest_start_ts: int | None = None,
        latest_end_ts: int | None = None,
        ttl_sec: int = DEFAULT_INTENT_TTL_SEC,
        allow_bounded_adjustment: bool = False,
        shift_group: str = "",
        is_shadow: bool = True,
        now_ts: int | None = None,
        allow_legacy_no_registry: bool = False,
    ) -> dict[str, Any]:
        """Advertise a persona's voluntary future dungeon intent across upcoming shifts.

        Does NOT queue the character in native LFG or create any offline activity.
        """
        ts = int(now_ts if now_ts is not None else time.time())
        spec = DUNGEON_BY_ID.get(str(dungeon_id or "").strip().lower())
        if spec is None or not spec.supported:
            return {"ok": False, "reason": f"unsupported_dungeon:{dungeon_id}"}
        if not is_shadow:
            if not coop_enabled():
                return {"ok": False, "reason": "coop_execution_disabled"}
            if spec.dungeon_id not in enabled_dungeon_ids():
                return {"ok": False, "reason": f"dungeon_not_enabled_live:{spec.dungeon_id}"}

        if is_protected_owner_group(shift_group):
            return {"ok": False, "reason": "protected_core_excluded"}

        rid = _parse_realm_id(realm_id)
        if rid is None:
            return {"ok": False, "reason": "invalid_realm_id"}

        fac = str(faction or "").strip().lower()
        if fac not in ("alliance", "horde"):
            return {"ok": False, "reason": "unknown_faction"}
        if spec.faction != "both" and spec.faction != fac:
            return {"ok": False, "reason": f"wrong_faction:{fac}!={spec.faction}"}

        clean_roles = sorted({str(r).strip().lower() for r in (roles or ()) if str(r).strip().lower() in VALID_ROLES})
        if not clean_roles:
            return {"ok": False, "reason": "invalid_or_empty_roles"}

        if class_name is not None:
            cname = str(class_name).strip().lower()
            legal_class_roles = CLASS_CAPABLE_ROLES.get(cname)
            if legal_class_roles is None:
                return {"ok": False, "reason": f"unknown_class:{cname}"}
            if any(r not in legal_class_roles for r in clean_roles):
                return {"ok": False, "reason": f"class_cannot_perform_roles:{cname}:{','.join(clean_roles)}"}

        eff_ttl = max(300, min(int(ttl_sec), MAX_INTENT_TTL_SEC))
        e_start = int(earliest_start_ts if earliest_start_ts is not None else ts)
        l_end = int(latest_end_ts if latest_end_ts is not None else (e_start + eff_ttl))
        exp_ts = min(l_end, ts + eff_ttl)
        required_sec = (int(spec.estimated_minutes) + int(spec.travel_minutes) + DEFAULT_WINDOW_BUFFER_MINUTES) * 60
        if l_end <= e_start or exp_ts <= ts:
            return {"ok": False, "reason": "invalid_future_window"}
        # P2-3: Ensure intent doesn't expire before enough usable time exists after earliest_start_ts!
        if (min(l_end, exp_ts) - max(e_start, ts)) < required_sec:
            return {"ok": False, "reason": "window_shorter_than_dungeon"}

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_stale_in_tx(conn, ts)
            psnap = PersonaRegistry.read_only_snapshot(self.path, conn=conn)
            ok_p, reason_p, verified_inc = _verify_persona_with_snapshot(
                psnap,
                persona_id=persona_id,
                realm_id=rid,
                character_guid=int(character_guid),
                allow_legacy_no_registry=allow_legacy_no_registry,
                is_shadow=is_shadow,
                shift_group=shift_group,
            )
            if not ok_p:
                conn.execute("COMMIT")
                return {"ok": False, "reason": reason_p}

            want_shadow = 1 if is_shadow else 0
            # P2-4: Refuse duplicate active intent if persona already has a 'matched' intent in an active commitment!
            matched_row = conn.execute(
                """
                SELECT id FROM future_dungeon_intents
                WHERE persona_id = ? AND dungeon_id = ? AND is_shadow = ? AND state = 'matched'
                LIMIT 1
                """,
                (persona_id, spec.dungeon_id, want_shadow),
            ).fetchone()
            if matched_row:
                conn.execute("COMMIT")
                return {"ok": False, "reason": "intent_already_matched_in_commitment"}

            existing = conn.execute(
                """
                SELECT id FROM future_dungeon_intents
                WHERE persona_id = ? AND dungeon_id = ? AND is_shadow = ? AND state = 'open'
                ORDER BY id DESC LIMIT 1
                """,
                (persona_id, spec.dungeon_id, want_shadow),
            ).fetchone()
            if existing:
                intent_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE future_dungeon_intents
                    SET realm_id=?, character_guid=?, incarnation=?, character_slug=?, faction=?,
                        roles_json=?, reason_code=?, earliest_start_ts=?, latest_end_ts=?,
                        expires_at=?, allow_bounded_adjustment=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        rid,
                        int(character_guid),
                        verified_inc,
                        character_slug.strip().lower(),
                        fac,
                        json.dumps(clean_roles),
                        str(reason_code or "")[:64],
                        e_start,
                        l_end,
                        exp_ts,
                        1 if allow_bounded_adjustment else 0,
                        ts,
                        intent_id,
                    ),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO future_dungeon_intents (
                        schema_version, persona_id, realm_id, character_guid, incarnation,
                        character_slug, faction, dungeon_id, roles_json, reason_code, state,
                        earliest_start_ts, latest_end_ts, expires_at, allow_bounded_adjustment,
                        is_shadow, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        SCHEMA_VERSION,
                        persona_id,
                        rid,
                        int(character_guid),
                        verified_inc,
                        character_slug.strip().lower(),
                        fac,
                        spec.dungeon_id,
                        json.dumps(clean_roles),
                        str(reason_code or "")[:64],
                        "open",
                        e_start,
                        l_end,
                        exp_ts,
                        1 if allow_bounded_adjustment else 0,
                        want_shadow,
                        ts,
                        ts,
                    ),
                )
                intent_id = int(cur.lastrowid)

            self._log_event(
                conn,
                event_type="intent_advertised",
                intent_id=intent_id,
                persona_id=persona_id,
                is_shadow=is_shadow,
                reason_code=str(reason_code or "")[:64],
                details={"dungeon_id": spec.dungeon_id, "roles": clean_roles, "expires_at": exp_ts},
                now_ts=ts,
            )
            conn.execute("COMMIT")
            return {
                "ok": True,
                "intent_id": intent_id,
                "persona_id": persona_id,
                "dungeon_id": spec.dungeon_id,
                "roles": clean_roles,
                "incarnation": verified_inc,
                "expires_at": exp_ts,
                "is_shadow": bool(is_shadow),
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def withdraw_future_intent(
        self,
        intent_id: int,
        *,
        persona_id: str,
        character_guid: int | None = None,
        realm_id: int | None = None,
        incarnation: int | None = None,
        reason_code: str = "voluntary_withdraw",
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Neutrally withdraw an open future dungeon intent (zero penalty)."""
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, state, is_shadow, realm_id, character_guid, incarnation FROM future_dungeon_intents WHERE id=? AND persona_id=?",
                (int(intent_id), persona_id),
            ).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "not_found"}
            if (
                (character_guid is not None and int(row["character_guid"]) != int(character_guid))
                or (realm_id is not None and int(row["realm_id"]) != int(realm_id))
                or (incarnation is not None and int(row["incarnation"]) != int(incarnation))
            ):
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "character_key_mismatch"}
            if str(row["state"]) != "open":
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"intent_not_open:{row['state']}"}
            conn.execute(
                "UPDATE future_dungeon_intents SET state='withdrawn', closed_reason=?, updated_at=? WHERE id=?",
                (str(reason_code or "voluntary_withdraw")[:64], ts, int(intent_id)),
            )
            self._log_event(
                conn,
                event_type="intent_withdrawn",
                intent_id=int(intent_id),
                persona_id=persona_id,
                is_shadow=bool(int(row["is_shadow"])),
                reason_code=reason_code,
                now_ts=ts,
            )
            conn.execute("COMMIT")
            return {"ok": True, "intent_id": int(intent_id), "state": "withdrawn", "neutral": True}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def discover_future_intents(
        self,
        *,
        dungeon_id: str | None = None,
        faction: str | None = None,
        persona_id: str | None = None,
        is_shadow: bool = True,
        now_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read-only cross-session discovery of unexpired open future dungeon intents."""
        ts = int(now_ts if now_ts is not None else time.time())
        snap = FutureCommitmentCoordinator.read_only_snapshot(self.path, now_ts=ts)
        if not snap["ok"]:
            return []
        out: list[dict[str, Any]] = []
        want_shadow = 1 if is_shadow else 0
        for row in snap["open_intents"]:
            if int(row["is_shadow"]) != want_shadow:
                continue
            if dungeon_id is not None and row["dungeon_id"] != dungeon_id:
                continue
            if faction is not None and row["faction"] != faction.lower():
                continue
            if persona_id is not None and row["persona_id"] != persona_id:
                continue
            out.append(dict(row))
        return out

    def get_commitment(self, commitment_id: int) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM future_commitments WHERE id=?", (int(commitment_id),)).fetchone()
            if not row:
                return None
            members = [
                dict(m)
                for m in conn.execute(
                    "SELECT * FROM future_commitment_members WHERE commitment_id=? ORDER BY id ASC",
                    (int(commitment_id),),
                ).fetchall()
            ]
            out = dict(row)
            out["members"] = members
            try:
                out["adjustment_applied"] = json.loads(str(row["adjustment_applied_json"] or "{}"))
            except (ValueError, TypeError):
                out["adjustment_applied"] = {}
            return out
        finally:
            conn.close()

    def get_active_schedule_overrides(
        self,
        *,
        is_shadow: bool = True,
        now_ts: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return approved temporary shift-slide windows for scheduled/activating/activated commitments (P1-3).

        Enables the off-thread scheduler to apply a reviewed temporary shift adjustment for a
        committed window and automatically revert once the commitment finishes or is cancelled.
        """
        ts = int(now_ts if now_ts is not None else time.time())
        want_shadow = 1 if is_shadow else 0
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT c.id AS commitment_id, c.dungeon_id, c.window_start, c.window_end,
                       c.adjustment_policy_id, c.adjustment_reviewed_by,
                       m.persona_id, m.realm_id, m.character_guid, m.adjustment_json
                FROM future_commitment_members m
                JOIN future_commitments c ON c.id = m.commitment_id
                WHERE c.is_shadow = ?
                  AND c.state IN ('scheduled', 'activating', 'activated')
                  AND m.state IN ('accepted', 'activated')
                  AND m.adjusted_minutes > 0
                  AND c.adjustment_policy_id != ''
                  AND c.adjustment_reviewed_by != ''
                  AND c.window_end > ?
                ORDER BY c.window_start ASC
                """,
                (want_shadow, ts),
            ).fetchall()
            out: dict[str, dict[str, Any]] = {}
            for r in rows:
                try:
                    adj = json.loads(str(r["adjustment_json"] or "{}"))
                except (ValueError, TypeError):
                    continue
                aw = adj.get("adjusted_window")
                if not (isinstance(aw, list) and len(aw) == 2):
                    continue
                pid = str(r["persona_id"])
                if pid not in out:
                    out[pid] = {
                        "commitment_id": int(r["commitment_id"]),
                        "dungeon_id": str(r["dungeon_id"]),
                        "realm_id": int(r["realm_id"]),
                        "character_guid": int(r["character_guid"]),
                        "policy_id": str(r["adjustment_policy_id"]),
                        "reviewed_by": str(r["adjustment_reviewed_by"]),
                        "adjusted_start_ts": int(aw[0]),
                        "adjusted_end_ts": int(aw[1]),
                        "window_start": int(r["window_start"]),
                        "window_end": int(r["window_end"]),
                        "net_expansion_min": int(adj.get("net_expansion_min", 0)),
                    }
            return out
        finally:
            conn.close()

    def get_neutral_events(self, *, commitment_id: int | None = None, persona_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            clauses: list[str] = []
            params: list[Any] = []
            if commitment_id is not None:
                clauses.append("commitment_id = ?")
                params.append(int(commitment_id))
            if persona_id is not None:
                clauses.append("persona_id = ?")
                params.append(persona_id)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM future_commitment_events {where} ORDER BY id ASC",
                tuple(params),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def propose_future_commitment(
        self,
        *,
        dungeon_id: str,
        roster_schedules: dict[Any, dict[str, Any]],
        max_concurrent_online: int,
        max_managed_groups: int = DEFAULT_MAX_GROUPS,
        adjustment_policy: ScheduleAdjustmentPolicy | None = None,
        owner_recalled_personas: set[str] | frozenset[str] | None = None,
        readiness_obligated_personas: set[str] | frozenset[str] | None = None,
        accept_ttl_sec: int = DEFAULT_ACCEPT_TTL_SEC,
        activation_grace_sec: int = DEFAULT_ACTIVATION_GRACE_SEC,
        horizon_hours: int = DEFAULT_HORIZON_HOURS,
        is_shadow: bool = True,
        now_ts: int | None = None,
        tz=None,
        allow_legacy_no_registry: bool = False,
    ) -> dict[str, Any]:
        """Find a compatible future window across staggered shifts and propose an optional commitment.

        Guarantees:
        - Runs off the game thread with a single PersonaRegistry snapshot per call (P2-12).
        - Evaluates Pass 0 (natural overlap, zero adjustment) across ALL factions BEFORE Pass 1 (P2-1).
        - Stores the EXACT capacity-checked `[window_start, window_end]` (P0-4).
        - Never undercounts bad schedules or already-scheduled adjusted intervals (P1-9).
        - Checks in-flight & active weekly adjustment quota per lane (P1-1), non-overlapping
          OpportunityCoordinator bookings (P1-8), and owner recall/readiness exclusions (P1-10).
        """
        ts = int(now_ts if now_ts is not None else time.time())
        spec = DUNGEON_BY_ID.get(str(dungeon_id or "").strip().lower())
        if spec is None or not spec.supported:
            return {"ok": False, "reason": f"unsupported_dungeon:{dungeon_id}"}
        if not is_shadow:
            if not coop_enabled():
                return {"ok": False, "reason": "coop_execution_disabled"}
            if spec.dungeon_id not in enabled_dungeon_ids():
                return {"ok": False, "reason": f"dungeon_not_enabled_live:{spec.dungeon_id}"}
            if allow_legacy_no_registry:
                return {"ok": False, "reason": "legacy_no_registry_forbidden_live"}
        if int(max_concurrent_online) <= 0:
            return {"ok": False, "reason": "invalid_max_concurrent_online"}
        if int(max_managed_groups) <= 0:
            return {"ok": False, "reason": "invalid_max_managed_groups"}

        policy_ok = False
        if adjustment_policy is not None and adjustment_policy.enabled:
            policy_ok, policy_reason = validate_adjustment_policy(adjustment_policy)
            if not policy_ok:
                return {"ok": False, "reason": policy_reason}

        recalled = set(owner_recalled_personas or ())
        readiness_req = set(readiness_obligated_personas or ())

        h_hours = max(1, min(int(horizon_hours), 72))
        horizon_end = ts + (h_hours * 3600)
        required_min = int(spec.estimated_minutes) + int(spec.travel_minutes) + DEFAULT_WINDOW_BUFFER_MINUTES
        required_sec = required_min * 60
        grace_sec = max(0, min(int(activation_grace_sec), 15 * 60))

        # Precompute roster windows BEFORE acquiring write lock (P2-12) and fail closed on bad schedules (P1-9)!
        roster_windows: dict[Any, list[tuple[int, int]]] = {}
        for rkey, sched in (roster_schedules or {}).items():
            wins, st = enumerate_schedule_windows(sched, start_ts=ts, horizon_hours=h_hours, tz=tz)
            if st in ("ok", "always_online"):
                roster_windows[rkey] = wins
            elif st == "schedule_disabled":
                roster_windows[rkey] = []
            elif st == "no_windows_in_horizon":
                roster_windows[rkey] = []
            else:
                # P1-9: Invalid/missing schedule telemetry conservatively counts as online across horizon!
                roster_windows[rkey] = [(ts, horizon_end)]

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_stale_in_tx(conn, ts)
            psnap = PersonaRegistry.read_only_snapshot(self.path, conn=conn)

            want_shadow = 1 if is_shadow else 0
            raw_intents = conn.execute(
                """
                SELECT * FROM future_dungeon_intents
                WHERE state = 'open' AND is_shadow = ? AND dungeon_id = ? AND expires_at > ? AND latest_end_ts > ?
                ORDER BY created_at ASC, id ASC
                """,
                (want_shadow, spec.dungeon_id, ts, ts),
            ).fetchall()

            # Load existing commitment windows + their added adjustment intervals (P1-9)
            existing_comm_rows = conn.execute(
                """
                SELECT id, window_start, window_end FROM future_commitments
                WHERE is_shadow = ? AND state IN ('proposed', 'scheduled', 'activating', 'activated') AND window_end > ?
                """,
                (want_shadow, ts),
            ).fetchall()
            existing_group_windows = [(int(r["window_start"]), int(r["window_end"])) for r in existing_comm_rows]

            persona_busy_windows: dict[str, list[tuple[int, int]]] = {}
            existing_extra_intervals: dict[Any, list[tuple[int, int]]] = {}
            busy_rows = conn.execute(
                """
                SELECT m.persona_id, m.realm_id, m.character_guid, m.adjustment_json,
                       c.window_start, c.window_end
                FROM future_commitment_members m
                JOIN future_commitments c ON c.id = m.commitment_id
                WHERE c.is_shadow = ? AND c.state IN ('proposed', 'scheduled', 'activating', 'activated')
                  AND m.state IN ('offered', 'accepted', 'activated') AND c.window_end > ?
                """,
                (want_shadow, ts),
            ).fetchall()
            for br in busy_rows:
                pid_b = str(br["persona_id"])
                persona_busy_windows.setdefault(pid_b, []).append((int(br["window_start"]), int(br["window_end"])))
                try:
                    adj_b = json.loads(str(br["adjustment_json"] or "{}"))
                except (ValueError, TypeError):
                    adj_b = {}
                added = adj_b.get("added_intervals")
                if isinstance(added, list) and added:
                    sched_key, _ = _lookup_schedule_for_member(
                        roster_schedules,
                        realm_id=int(br["realm_id"]),
                        character_guid=int(br["character_guid"]),
                        persona_id=pid_b,
                    )
                    for seg in added:
                        if isinstance(seg, list) and len(seg) == 2:
                            existing_extra_intervals.setdefault(sched_key, []).append((int(seg[0]), int(seg[1])))

            # P1-8: Also fold active/reserved OpportunityCoordinator bookings into busy & group windows!
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "opportunities" in tables and "opportunity_interests" in tables:
                opp_rows = conn.execute(
                    """
                    SELECT id, estimated_minutes, started_at, created_at
                    FROM opportunities
                    WHERE is_shadow = ? AND state IN ('assembling', 'confirmed', 'active', 'cancelling')
                    """,
                    (want_shadow,),
                ).fetchall()
                opp_end_by_id: dict[int, int] = {}
                for orow in opp_rows:
                    o_start = int(orow["started_at"] or orow["created_at"] or ts)
                    o_end = max(ts + 60, o_start + (int(orow["estimated_minutes"] or 45) + 15) * 60)
                    opp_end_by_id[int(orow["id"])] = o_end
                    existing_group_windows.append((ts, o_end))

                oi_rows = conn.execute(
                    """
                    SELECT oi.persona_id, oi.opportunity_id, oi.available_until
                    FROM opportunity_interests oi
                    JOIN opportunities o ON o.id = oi.opportunity_id
                    WHERE o.is_shadow = ?
                      AND o.state IN ('assembling', 'confirmed', 'active', 'cancelling')
                      AND oi.state IN ('reserved', 'accepted', 'active')
                    """,
                    (want_shadow,),
                ).fetchall()
                for oir in oi_rows:
                    o_end = opp_end_by_id.get(int(oir["opportunity_id"]), ts + 3600)
                    persona_busy_windows.setdefault(str(oir["persona_id"]), []).append((ts, o_end))

            # Weekly adjustment usage in this lane (includes in-flight proposed + scheduled + activating + activated!)
            adj_counts = self._weekly_adjustment_usage(conn, is_shadow=is_shadow, now_ts=ts)

            # Filter intents once using the single PersonaRegistry snapshot
            eligible_intents: list[dict[str, Any]] = []
            skipped_reasons: dict[str, int] = {}
            for row in raw_intents:
                pid = str(row["persona_id"])
                if pid in recalled:
                    skipped_reasons["owner_recalled"] = skipped_reasons.get("owner_recalled", 0) + 1
                    continue
                if pid in readiness_req:
                    skipped_reasons["raid_readiness_obligated"] = skipped_reasons.get("raid_readiness_obligated", 0) + 1
                    continue

                ok_p, r_p, v_inc = _verify_persona_with_snapshot(
                    psnap,
                    persona_id=pid,
                    realm_id=int(row["realm_id"]),
                    character_guid=int(row["character_guid"]),
                    allow_legacy_no_registry=allow_legacy_no_registry,
                    is_shadow=is_shadow,
                    expected_incarnation=int(row["incarnation"]),
                )
                if not ok_p:
                    skipped_reasons[r_p] = skipped_reasons.get(r_p, 0) + 1
                    continue

                sched_key, sched = _lookup_schedule_for_member(
                    roster_schedules,
                    realm_id=int(row["realm_id"]),
                    character_guid=int(row["character_guid"]),
                    persona_id=pid,
                )
                natural_wins, sched_st = enumerate_schedule_windows(sched, start_ts=ts, horizon_hours=h_hours, tz=tz)
                if sched_st not in ("ok", "always_online") or not natural_wins:
                    skipped_reasons[sched_st] = skipped_reasons.get(sched_st, 0) + 1
                    continue

                adv_start = max(ts, int(row["earliest_start_ts"]))
                adv_end = min(horizon_end, int(row["latest_end_ts"]), int(row["expires_at"]))
                if adv_end - adv_start < required_sec:
                    skipped_reasons["no_free_window"] = skipped_reasons.get("no_free_window", 0) + 1
                    continue

                try:
                    roles_list = [r for r in json.loads(str(row["roles_json"] or "[]")) if r in VALID_ROLES]
                except (ValueError, TypeError):
                    roles_list = []
                if not roles_list:
                    skipped_reasons["no_valid_roles"] = skipped_reasons.get("no_valid_roles", 0) + 1
                    continue

                eligible_intents.append({
                    "intent_id": int(row["id"]),
                    "persona_id": pid,
                    "realm_id": int(row["realm_id"]),
                    "character_guid": int(row["character_guid"]),
                    "incarnation": v_inc,
                    "character_slug": str(row["character_slug"]),
                    "faction": str(row["faction"]),
                    "roles": roles_list,
                    "allow_bounded_adjustment": bool(int(row["allow_bounded_adjustment"])),
                    "sched_key": sched_key,
                    "natural_windows": natural_wins,
                    "adv_bounds": (adv_start, adv_end),
                })

            factions = ("alliance", "horde") if spec.faction == "both" else (spec.faction,)
            roles_req = dict(spec.roles)
            best_match: dict[str, Any] | None = None
            last_failure = "insufficient_future_intents"

            # P2-1: Run Pass 0 (STRICT natural overlap) across ALL factions BEFORE Pass 1 (bounded adjustment)!
            for fac in factions:
                fac_pool = [it for it in eligible_intents if it["faction"] == fac]
                if not fac_pool:
                    continue
                found, fail_r = self._search_party_window(
                    fac_pool,
                    roles_req=roles_req,
                    required_sec=required_sec,
                    grace_sec=grace_sec,
                    existing_group_windows=existing_group_windows,
                    max_managed_groups=int(max_managed_groups),
                    roster_windows=roster_windows,
                    existing_extra_intervals=existing_extra_intervals,
                    max_concurrent_online=int(max_concurrent_online),
                    policy=None,
                    adj_counts=adj_counts,
                    now_ts=ts,
                    horizon_end=horizon_end,
                    persona_busy_windows=persona_busy_windows,
                )
                if found is not None:
                    best_match = {**found, "faction": fac}
                    break
                if fail_r:
                    last_failure = fail_r

            if best_match is None and policy_ok and adjustment_policy is not None:
                for fac in factions:
                    fac_pool = [it for it in eligible_intents if it["faction"] == fac]
                    if not fac_pool:
                        continue
                    found_adj, fail_adj = self._search_party_window(
                        fac_pool,
                        roles_req=roles_req,
                        required_sec=required_sec,
                        grace_sec=grace_sec,
                        existing_group_windows=existing_group_windows,
                        max_managed_groups=int(max_managed_groups),
                        roster_windows=roster_windows,
                        existing_extra_intervals=existing_extra_intervals,
                        max_concurrent_online=int(max_concurrent_online),
                        policy=adjustment_policy,
                        adj_counts=adj_counts,
                        now_ts=ts,
                        horizon_end=horizon_end,
                        persona_busy_windows=persona_busy_windows,
                    )
                    if found_adj is not None:
                        best_match = {**found_adj, "faction": fac}
                        break
                    if fail_adj:
                        last_failure = fail_adj

            if best_match is None:
                conn.execute("COMMIT")
                return {
                    "ok": False,
                    "reason": last_failure,
                    "skipped_reasons": skipped_reasons,
                }

            win_start = int(best_match["window_start"])
            win_end = int(best_match["window_end"])
            # P2-2: Accept deadline MUST leave enough time before win_end - required_sec (no fake max floor!)
            latest_accept = win_end - required_sec
            if latest_accept < ts + 60:
                conn.execute("COMMIT")
                return {"ok": False, "reason": "window_too_imminent_for_acceptance"}
            accept_until = min(latest_accept, ts + max(60, int(accept_ttl_sec)))

            adj_summary = best_match["adjustments"]
            pol_id = adjustment_policy.policy_id if (adj_summary and adjustment_policy) else ""
            pol_rev = adjustment_policy.reviewed_by if (adj_summary and adjustment_policy) else ""

            cur = conn.execute(
                """
                INSERT INTO future_commitments (
                    schema_version, revision, dungeon_id, faction, window_start, window_end,
                    estimated_minutes, required_minutes, accept_until, state, adjustment_policy_id,
                    adjustment_reviewed_by, adjustment_applied_json, is_shadow, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    SCHEMA_VERSION,
                    1,
                    spec.dungeon_id,
                    best_match["faction"],
                    win_start,
                    win_end,
                    int(spec.estimated_minutes),
                    required_min,
                    accept_until,
                    "proposed",
                    pol_id,
                    pol_rev,
                    json.dumps(adj_summary, sort_keys=True),
                    want_shadow,
                    ts,
                    ts,
                ),
            )
            comm_id = int(cur.lastrowid)

            for m in best_match["party"]:
                m_adj = adj_summary.get(m["persona_id"]) or {}
                adj_mins = int(m_adj.get("start_early_min", 0)) + int(m_adj.get("end_extend_min", 0))
                conn.execute(
                    """
                    INSERT INTO future_commitment_members (
                        commitment_id, intent_id, persona_id, realm_id, character_guid, incarnation,
                        character_slug, role, state, adjusted_minutes, adjustment_json, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        comm_id,
                        int(m["intent_id"]),
                        m["persona_id"],
                        int(m["realm_id"]),
                        int(m["character_guid"]),
                        int(m["incarnation"]),
                        m["character_slug"],
                        m["role"],
                        "offered",
                        adj_mins,
                        json.dumps(m_adj, sort_keys=True),
                        ts,
                        ts,
                    ),
                )
                conn.execute(
                    "UPDATE future_dungeon_intents SET state='matched', updated_at=? WHERE id=?",
                    (ts, int(m["intent_id"])),
                )

            self._log_event(
                conn,
                event_type="commitment_proposed",
                commitment_id=comm_id,
                is_shadow=is_shadow,
                reason_code="compatible_window_found",
                details={
                    "dungeon_id": spec.dungeon_id,
                    "window_start": win_start,
                    "window_end": win_end,
                    "adjustments": adj_summary,
                },
                now_ts=ts,
            )
            conn.execute("COMMIT")
            return {
                "ok": True,
                "commitment_id": comm_id,
                "revision": 1,
                "state": "proposed",
                "dungeon_id": spec.dungeon_id,
                "faction": best_match["faction"],
                "window_start": win_start,
                "window_end": win_end,
                "required_minutes": required_min,
                "accept_until": accept_until,
                "adjustment_applied": adj_summary,
                "policy_id": pol_id,
                "party": [
                    {
                        "persona_id": m["persona_id"],
                        "character_guid": m["character_guid"],
                        "character_slug": m["character_slug"],
                        "role": m["role"],
                    }
                    for m in best_match["party"]
                ],
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _search_party_window(
        self,
        pool: list[dict[str, Any]],
        *,
        roles_req: dict[str, int],
        required_sec: int,
        grace_sec: int,
        existing_group_windows: list[tuple[int, int]],
        max_managed_groups: int,
        roster_windows: dict[Any, list[tuple[int, int]]],
        existing_extra_intervals: dict[Any, list[tuple[int, int]]],
        max_concurrent_online: int,
        policy: ScheduleAdjustmentPolicy | None,
        adj_counts: dict[str, int],
        now_ts: int,
        horizon_end: int,
        persona_busy_windows: dict[str, list[tuple[int, int]]],
    ) -> tuple[dict[str, Any] | None, str]:
        """Search for the earliest valid 1T/1H/3D party + EXACT capacity-checked window."""
        need_t = int(roles_req.get("tank", 0))
        need_h = int(roles_req.get("healer", 0))
        need_d = int(roles_req.get("damage", 0))
        total_need = need_t + need_h + need_d
        if len(pool) < total_need:
            return None, "insufficient_future_intents"

        # Quick role feasibility check before window search
        for rname, need_cnt in (("tank", need_t), ("healer", need_h), ("damage", need_d)):
            if need_cnt > 0 and sum(1 for it in pool if rname in it["roles"]) < need_cnt:
                return None, "no_overlapping_role_composition"

        cand_options: dict[str, list[tuple[int, int, dict[str, Any]]]] = {}
        for it in pool:
            pid = it["persona_id"]
            adv_s, adv_e = it["adv_bounds"]
            busy = persona_busy_windows.get(pid, [])
            if (
                policy is not None
                and it["allow_bounded_adjustment"]
                and adj_counts.get(pid, 0) < int(policy.max_adjustments_per_persona_per_week)
            ):
                # P1-4: Generate slides on UNCLAMPED natural windows & reject if removed_intervals hit busy!
                raw_opts = _adjust_natural_windows_with_policy(
                    it["natural_windows"],
                    policy,
                    min_ts=now_ts,
                    max_ts=horizon_end,
                    busy_windows=busy,
                )
            else:
                raw_opts = [
                    (
                        ws,
                        we,
                        {
                            "adjusted": False,
                            "start_early_min": 0,
                            "end_extend_min": 0,
                            "net_expansion_min": 0,
                            "added_intervals": [],
                            "removed_intervals": [],
                            "adjusted_window": [ws, we],
                            "base_window": [ws, we],
                        },
                    )
                    for ws, we in it["natural_windows"]
                ]

            cleaned: list[tuple[int, int, dict[str, Any]]] = []
            for ws, we, meta in raw_opts:
                for cs, ce in _intersect_two_window_lists([(ws, we)], [(adv_s, adv_e)]):
                    for fs, fe in _subtract_blocked_intervals([(cs, ce)], busy):
                        if fe - fs >= required_sec:
                            cleaned.append((fs, fe, meta))
            cand_options[pid] = cleaned

        # P2-7: Anchor starts include candidate option starts + existing commitment ends + roster shift ends!
        anchor_starts: set[int] = set()
        for opts in cand_options.values():
            for ws, we, _ in opts:
                if we - ws >= required_sec:
                    anchor_starts.add(ws)
        for _gs, ge in existing_group_windows:
            if now_ts <= ge < horizon_end:
                anchor_starts.add(ge)
        for rwins in roster_windows.values():
            for _rs, re in rwins:
                if now_ts <= re < horizon_end:
                    anchor_starts.add(re)
        if not anchor_starts:
            return None, "no_compatible_window_duration"

        saw_group_cap_block = False
        saw_concurrency_block = False
        saw_role_gap = False

        for win_start in sorted(anchor_starts):
            # Require at least 60s of acceptance window before start (unless grace provides buffer)
            min_end = win_start + required_sec

            # P0-4: Determine exact stored window [win_start, stored_end] FIRST and check caps on THAT EXACT window!
            # Try with bounded activation grace (so accept_until & transient login retries work cleanly);
            # fall back to exact required_sec if grace collides with group cap.
            candidate_ends = [min_end + grace_sec, min_end] if grace_sec > 0 else [min_end]
            for win_end in candidate_ends:
                # Must leave >= 60s before latest_accept = win_end - required_sec
                if (win_end - required_sec) < (now_ts + 60):
                    # If win_start is right now, require enough grace so latest_accept >= now_ts + 60
                    if win_end == min_end and grace_sec == 0:
                        continue
                    continue

                if _peak_overlapping_groups(win_start, win_end, existing_group_windows) >= max_managed_groups:
                    saw_group_cap_block = True
                    continue

                covering: list[tuple[dict[str, Any], dict[str, Any]]] = []
                for it in pool:
                    pid = it["persona_id"]
                    opts = cand_options.get(pid, [])
                    valid_opts = [
                        (ws, we, meta)
                        for ws, we, meta in opts
                        if ws <= win_start and we >= win_end
                    ]
                    if not valid_opts:
                        continue
                    valid_opts.sort(
                        key=lambda x: (
                            1 if x[2]["adjusted"] else 0,
                            x[2]["start_early_min"] + x[2]["end_extend_min"],
                        )
                    )
                    covering.append((it, valid_opts[0][2]))

                if len(covering) < total_need:
                    saw_role_gap = True
                    continue

                covering.sort(
                    key=lambda pair: (
                        1 if pair[1]["adjusted"] else 0,
                        pair[0]["intent_id"],
                    )
                )
                role_slots = (["tank"] * need_t) + (["healer"] * need_h) + (["damage"] * need_d)
                picked = self._assign_roles_backtrack(covering, role_slots)
                if picked is None:
                    saw_role_gap = True
                    continue

                # Build combined extra intervals: existing scheduled adjustments (P1-9) + new candidate adjustments
                combined_extra: dict[Any, list[tuple[int, int]]] = {
                    k: list(v) for k, v in existing_extra_intervals.items()
                }
                adjustments: dict[str, dict[str, Any]] = {}
                check_intervals: list[tuple[int, int]] = [(win_start, win_end)]

                for item, meta, _role in picked:
                    if meta["adjusted"]:
                        pid = item["persona_id"]
                        skey = item["sched_key"]
                        added_tuples = [(int(s), int(e)) for s, e in meta["added_intervals"]]
                        combined_extra.setdefault(skey, []).extend(added_tuples)
                        check_intervals.extend(added_tuples)
                        adjustments[pid] = {
                            "start_early_min": meta["start_early_min"],
                            "end_extend_min": meta["end_extend_min"],
                            "net_expansion_min": meta["net_expansion_min"],
                            "added_intervals": meta["added_intervals"],
                            "removed_intervals": meta["removed_intervals"],
                            "adjusted_window": meta["adjusted_window"],
                        }

                concurrency_ok = True
                for cs, ce in check_intervals:
                    peak = _peak_concurrency_in_interval(cs, ce, roster_windows, combined_extra)
                    if peak > max_concurrent_online:
                        concurrency_ok = False
                        break
                if not concurrency_ok:
                    saw_concurrency_block = True
                    continue

                # P0-4: Return the EXACT capacity-checked [win_start, win_end] (NEVER widened past checked bounds!)
                party_out = [{**item, "role": assigned_role} for item, _m, assigned_role in picked]
                return {
                    "window_start": win_start,
                    "window_end": win_end,
                    "party": party_out,
                    "adjustments": adjustments,
                }, ""

        if saw_concurrency_block:
            return None, "concurrency_cap_exceeded"
        if saw_group_cap_block:
            return None, "group_capacity_exceeded"
        if saw_role_gap:
            return None, "no_overlapping_role_composition"
        return None, "no_compatible_window"

    def _assign_roles_backtrack(
        self,
        covering: list[tuple[dict[str, Any], dict[str, Any]]],
        role_slots: list[str],
    ) -> list[tuple[dict[str, Any], dict[str, Any], str]] | None:
        used: set[str] = set()
        result: list[tuple[dict[str, Any], dict[str, Any], str]] = []

        def _dfs(idx: int) -> bool:
            if idx == len(role_slots):
                return True
            need_role = role_slots[idx]
            for item, meta in covering:
                pid = item["persona_id"]
                if pid in used or need_role not in item["roles"]:
                    continue
                used.add(pid)
                result.append((item, meta, need_role))
                if _dfs(idx + 1):
                    return True
                result.pop()
                used.remove(pid)
            return False

        return result if _dfs(0) else None

    def respond_to_commitment(
        self,
        commitment_id: int,
        *,
        expected_revision: int,
        persona_id: str,
        character_guid: int,
        realm_id: int = 1,
        incarnation: int = 1,
        accepted: bool,
        reason_code: str = "",
        max_adjustments_per_week: int = MAX_POLICY_ADJUSTMENTS_PER_WEEK,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Voluntarily accept or neutrally decline a proposed future commitment (idempotent)."""
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_stale_in_tx(conn, ts)
            comm = conn.execute("SELECT * FROM future_commitments WHERE id=?", (int(commitment_id),)).fetchone()
            if not comm:
                conn.execute("COMMIT")
                return {"ok": False, "reason": "not_found"}

            c_state = str(comm["state"])
            c_rev = int(comm["revision"])
            is_sh = bool(int(comm["is_shadow"]))
            member = conn.execute(
                "SELECT * FROM future_commitment_members WHERE commitment_id=? AND persona_id=?",
                (int(commitment_id), persona_id),
            ).fetchone()
            if not member:
                conn.execute("COMMIT")
                return {"ok": False, "reason": "member_not_found"}

            if (
                int(member["character_guid"]) != int(character_guid)
                or int(member["realm_id"]) != int(realm_id)
                or int(member["incarnation"]) != int(incarnation)
            ):
                conn.execute("COMMIT")
                return {"ok": False, "reason": "character_key_mismatch"}

            m_state = str(member["state"])

            if accepted and m_state == "accepted" and c_state in ("proposed", "scheduled") and int(expected_revision) <= c_rev:
                conn.execute("COMMIT")
                return {
                    "ok": True,
                    "idempotent": True,
                    "state": c_state,
                    "revision": c_rev,
                    "all_accepted": c_state == "scheduled",
                }
            if (not accepted) and m_state == "declined" and c_state == "declined":
                conn.execute("COMMIT")
                return {
                    "ok": True,
                    "idempotent": True,
                    "accepted": False,
                    "state": "declined",
                    "revision": c_rev,
                    "neutral": True,
                }

            if c_state == "expired":
                conn.execute("COMMIT")
                return {"ok": False, "reason": "commitment_expired"}
            if c_state != "proposed" or c_rev != int(expected_revision):
                conn.execute("COMMIT")
                return {"ok": False, "reason": f"revision_or_state_mismatch:{c_state}:rev{c_rev}"}
            if m_state not in ("offered", "accepted"):
                conn.execute("COMMIT")
                return {"ok": False, "reason": f"member_not_offered:{m_state}"}

            if not accepted:
                rc = (reason_code or "voluntary_decline")[:64]
                conn.execute(
                    "UPDATE future_commitment_members SET state='declined', response_reason=?, responded_at=?, updated_at=? WHERE id=?",
                    (rc, ts, ts, int(member["id"])),
                )
                others = conn.execute(
                    "SELECT id, intent_id FROM future_commitment_members WHERE commitment_id=? AND id != ? AND state IN ('offered','accepted')",
                    (int(commitment_id), int(member["id"])),
                ).fetchall()
                for o in others:
                    conn.execute(
                        "UPDATE future_commitment_members SET state='released', response_reason='party_member_declined', updated_at=? WHERE id=?",
                        (ts, int(o["id"])),
                    )
                    self._reopen_intent_if_valid(conn, int(o["intent_id"]), ts)
                # P2-6: Record distinct neutral 'declined_offer' state (NOT 'withdrawn')!
                self._close_intent(conn, int(member["intent_id"]), state="declined_offer", reason=rc, now_ts=ts)
                new_rev = c_rev + 1
                conn.execute(
                    "UPDATE future_commitments SET state='declined', outcome_reason=?, ended_at=?, updated_at=?, revision=? WHERE id=?",
                    (f"member_declined:{persona_id}"[:120], ts, ts, new_rev, int(commitment_id)),
                )
                self._log_adjustment_refunds_if_any(conn, int(commitment_id), is_shadow=is_sh, reason_code="member_declined", now_ts=ts)
                self._log_event(
                    conn,
                    event_type="commitment_declined",
                    commitment_id=int(commitment_id),
                    intent_id=int(member["intent_id"]),
                    persona_id=persona_id,
                    is_shadow=is_sh,
                    reason_code=rc,
                    now_ts=ts,
                )
                conn.execute("COMMIT")
                return {
                    "ok": True,
                    "accepted": False,
                    "state": "declined",
                    "revision": new_rev,
                    "neutral": True,
                    "penalty_applied": False,
                }

            # P1-1: Recheck weekly bounded adjustment quota INSIDE the accept transaction!
            if int(member["adjusted_minutes"]) > 0:
                used_other = self._weekly_adjustment_usage(
                    conn, is_shadow=is_sh, now_ts=ts, exclude_commitment_id=int(commitment_id)
                ).get(persona_id, 0)
                if used_other >= int(max_adjustments_per_week):
                    conn.execute("COMMIT")
                    return {"ok": False, "reason": "weekly_adjustment_quota_exceeded"}

            rc = (reason_code or "voluntary_accept")[:64]
            conn.execute(
                "UPDATE future_commitment_members SET state='accepted', response_reason=?, responded_at=?, updated_at=? WHERE id=?",
                (rc, ts, ts, int(member["id"])),
            )
            counts = conn.execute(
                "SELECT SUM(CASE WHEN state='accepted' THEN 1 ELSE 0 END) AS acc, "
                "SUM(CASE WHEN state='offered' THEN 1 ELSE 0 END) AS off "
                "FROM future_commitment_members WHERE commitment_id=?",
                (int(commitment_id),),
            ).fetchone()
            acc_n = int(counts["acc"] or 0)
            off_n = int(counts["off"] or 0)

            if off_n == 0 and acc_n > 0:
                new_rev = c_rev + 1
                conn.execute(
                    "UPDATE future_commitments SET state='scheduled', updated_at=?, revision=? WHERE id=?",
                    (ts, new_rev, int(commitment_id)),
                )
                self._log_event(
                    conn,
                    event_type="commitment_scheduled",
                    commitment_id=int(commitment_id),
                    is_shadow=is_sh,
                    reason_code="all_members_accepted",
                    details={"window_start": int(comm["window_start"]), "window_end": int(comm["window_end"])},
                    now_ts=ts,
                )
                conn.execute("COMMIT")
                return {
                    "ok": True,
                    "accepted": True,
                    "state": "scheduled",
                    "revision": new_rev,
                    "all_accepted": True,
                    "accepted_count": acc_n,
                }

            conn.execute("COMMIT")
            return {
                "ok": True,
                "accepted": True,
                "state": "proposed",
                "revision": c_rev,
                "all_accepted": False,
                "accepted_count": acc_n,
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def withdraw_commitment(
        self,
        commitment_id: int,
        *,
        persona_id: str,
        character_guid: int | None = None,
        realm_id: int | None = None,
        incarnation: int | None = None,
        reason_code: str = "voluntary_withdraw",
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Voluntarily withdraw from a proposed or scheduled commitment neutrally before activation."""
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            comm = conn.execute("SELECT * FROM future_commitments WHERE id=?", (int(commitment_id),)).fetchone()
            if not comm:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "not_found"}
            c_state = str(comm["state"])
            is_sh = bool(int(comm["is_shadow"]))
            if c_state not in ("proposed", "scheduled"):
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"cannot_withdraw_state:{c_state}"}

            member = conn.execute(
                "SELECT id, intent_id, realm_id, character_guid, incarnation FROM future_commitment_members WHERE commitment_id=? AND persona_id=?",
                (int(commitment_id), persona_id),
            ).fetchone()
            if not member:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "member_not_found"}
            if (
                (character_guid is not None and int(member["character_guid"]) != int(character_guid))
                or (realm_id is not None and int(member["realm_id"]) != int(realm_id))
                or (incarnation is not None and int(member["incarnation"]) != int(incarnation))
            ):
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "character_key_mismatch"}

            rc = (reason_code or "voluntary_withdraw")[:64]
            conn.execute(
                "UPDATE future_commitment_members SET state='withdrawn', response_reason=?, updated_at=? WHERE id=?",
                (rc, ts, int(member["id"])),
            )
            others = conn.execute(
                "SELECT id, intent_id FROM future_commitment_members WHERE commitment_id=? AND id != ? AND state IN ('offered','accepted')",
                (int(commitment_id), int(member["id"])),
            ).fetchall()
            for o in others:
                conn.execute(
                    "UPDATE future_commitment_members SET state='released', response_reason='member_withdrew', updated_at=? WHERE id=?",
                    (ts, int(o["id"])),
                )
                self._reopen_intent_if_valid(conn, int(o["intent_id"]), ts)

            self._close_intent(conn, int(member["intent_id"]), state="withdrawn", reason=rc, now_ts=ts)
            new_rev = int(comm["revision"]) + 1
            conn.execute(
                "UPDATE future_commitments SET state='cancelled', outcome_reason=?, ended_at=?, updated_at=?, revision=? WHERE id=?",
                (f"member_withdrew:{persona_id}"[:120], ts, ts, new_rev, int(commitment_id)),
            )
            self._log_adjustment_refunds_if_any(conn, int(commitment_id), is_shadow=is_sh, reason_code="member_withdrew", now_ts=ts)
            self._log_event(
                conn,
                event_type="commitment_withdrawn",
                commitment_id=int(commitment_id),
                intent_id=int(member["intent_id"]),
                persona_id=persona_id,
                is_shadow=is_sh,
                reason_code=rc,
                now_ts=ts,
            )
            conn.execute("COMMIT")
            return {
                "ok": True,
                "state": "cancelled",
                "revision": new_rev,
                "neutral": True,
                "penalty_applied": False,
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def override_commitment(
        self,
        commitment_id: int,
        *,
        override_kind: str,
        target_persona_id: str = "",
        safe_boundary: bool | None = None,
        opportunity_coordinator: OpportunityCoordinator | None = None,
        reason_code: str = "",
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Override a future commitment for owner recall or raid readiness at a safe boundary.

        P0-1/P0-2/P0-3/P1-10/P2-15 guarantees:
        - Persists `pending_override_target` AND bumps `revision` when deferred (`safe_boundary=False`).
        - Explicitly requires `safe_boundary` (`True`/`False`) and `opportunity_coordinator` when state is `activated`.
        - Releases the linked `OpportunityCoordinator` party when overriding an `activated` commitment!
        - Explicitly closes the recalled/readiness target persona's matched intent AND all their open intents!
        """
        kind = str(override_kind or "").strip().lower()
        if kind not in VALID_OVERRIDE_KINDS:
            return {"ok": False, "reason": f"invalid_override_kind:{kind}"}

        ts = int(now_ts if now_ts is not None else time.time())
        target_pid = str(target_persona_id or "").strip()

        # Pre-check state so we can fail closed before mutating if activated needs coordinator/safe_boundary
        conn = self._conn()
        linked_opp_id = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            comm = conn.execute("SELECT * FROM future_commitments WHERE id=?", (int(commitment_id),)).fetchone()
            if not comm:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "not_found"}

            c_state = str(comm["state"])
            is_sh = bool(int(comm["is_shadow"]))
            if c_state in ("declined", "expired", "cancelled", "completed", "overridden_owner_recall", "overridden_readiness"):
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"already_terminal:{c_state}"}

            # P2-15: Require explicit safe_boundary on activating/activated rows!
            if c_state in ("activating", "activated") and safe_boundary is None:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "safe_boundary_required_for_active"}
            eff_safe = True if safe_boundary is None else bool(safe_boundary)

            # P0-1/P0-3: Defer safely with persisted target + revision bump!
            if not eff_safe or c_state == "activating":
                new_rev = int(comm["revision"]) + 1
                conn.execute(
                    "UPDATE future_commitments SET pending_override=?, pending_override_target=?, "
                    "override_requested_at=?, updated_at=?, revision=? WHERE id=?",
                    (kind, target_pid, ts, ts, new_rev, int(commitment_id)),
                )
                self._log_event(
                    conn,
                    event_type=f"pending_{kind}_override",
                    commitment_id=int(commitment_id),
                    persona_id=target_pid,
                    is_shadow=is_sh,
                    reason_code=(reason_code or f"deferred_until_safe_boundary:{kind}")[:120],
                    now_ts=ts,
                )
                conn.execute("COMMIT")
                return {
                    "ok": True,
                    "deferred_to_safe_boundary": True,
                    "pending_override": kind,
                    "pending_override_target": target_pid,
                    "state": c_state,
                    "revision": new_rev,
                    "neutral": True,
                }

            linked_opp_id = int(comm["linked_opportunity_id"] or 0)
            if c_state == "activated" and linked_opp_id > 0 and opportunity_coordinator is None:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "opportunity_coordinator_required_for_activated_override"}

            final_state = "overridden_owner_recall" if kind == "owner_recall" else "overridden_readiness"
            rc = (reason_code or final_state)[:64]
            members = conn.execute(
                "SELECT id, intent_id, persona_id FROM future_commitment_members WHERE commitment_id=? AND state IN ('offered','accepted','activated')",
                (int(commitment_id),),
            ).fetchall()
            for m in members:
                mpid = str(m["persona_id"])
                conn.execute(
                    "UPDATE future_commitment_members SET state='released', response_reason=?, updated_at=? WHERE id=?",
                    (rc, ts, int(m["id"])),
                )
                if target_pid and mpid == target_pid:
                    # P0-1: Explicitly close recalled/obligated target's intent so they are NOT re-proposed!
                    self._close_intent(conn, int(m["intent_id"]), state="withdrawn", reason=final_state, now_ts=ts)
                else:
                    self._reopen_intent_if_valid(conn, int(m["intent_id"]), ts)

            # P1-10: Also withdraw any other open intents belonging to target_pid across ALL dungeons!
            if target_pid:
                conn.execute(
                    "UPDATE future_dungeon_intents SET state='withdrawn', closed_reason=?, updated_at=? "
                    "WHERE persona_id=? AND state='open'",
                    (final_state, ts, target_pid),
                )

            new_rev = int(comm["revision"]) + 1
            conn.execute(
                "UPDATE future_commitments SET state=?, pending_override='', pending_override_target='', "
                "outcome_reason=?, ended_at=?, updated_at=?, revision=? WHERE id=?",
                (final_state, rc, ts, ts, new_rev, int(commitment_id)),
            )
            self._log_adjustment_refunds_if_any(conn, int(commitment_id), is_shadow=is_sh, reason_code=final_state, now_ts=ts)
            self._log_event(
                conn,
                event_type=final_state,
                commitment_id=int(commitment_id),
                persona_id=target_pid,
                is_shadow=is_sh,
                reason_code=rc,
                now_ts=ts,
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

        # P0-2: Release linked OpportunityCoordinator party when overriding an activated commitment!
        opp_release = None
        if linked_opp_id > 0 and opportunity_coordinator is not None:
            opp_release = opportunity_coordinator.release_party(
                linked_opp_id,
                f"{kind}_override",
                now_ts=ts,
            )

        return {
            "ok": True,
            "deferred_to_safe_boundary": False,
            "state": final_state,
            "revision": new_rev,
            "linked_opportunity_released": opp_release,
            "neutral": True,
            "penalty_applied": False,
        }

    def process_pending_overrides(
        self,
        *,
        opportunity_coordinator: OpportunityCoordinator | None = None,
        safe_boundary_fn: Callable[[dict[str, Any]], bool] | None = None,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Sweep and honor deferred overrides across proposed, scheduled, AND activated commitments (P0-2)."""
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            rows = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT * FROM future_commitments
                    WHERE pending_override != '' AND state IN ('proposed', 'scheduled', 'activated')
                    ORDER BY id ASC
                    """
                ).fetchall()
            ]
        finally:
            conn.close()

        processed: list[dict[str, Any]] = []
        deferred: list[int] = []
        for row in rows:
            cid = int(row["id"])
            is_safe = bool(safe_boundary_fn(row)) if safe_boundary_fn is not None else (str(row["state"]) != "activated")
            if not is_safe:
                deferred.append(cid)
                continue
            res = self.override_commitment(
                cid,
                override_kind=str(row["pending_override"]),
                target_persona_id=str(row["pending_override_target"] or ""),
                safe_boundary=True,
                opportunity_coordinator=opportunity_coordinator,
                reason_code=f"honored_pending_{row['pending_override']}",
                now_ts=ts,
            )
            processed.append({"commitment_id": cid, "result": res})
        return {"processed": processed, "still_deferred": deferred}

    def reconcile_activating(
        self,
        *,
        opportunity_coordinator: OpportunityCoordinator | None = None,
        now_ts: int | None = None,
    ) -> dict[str, int]:
        """Crash recovery for commitments stuck in 'activating' past their lease (P1-5)."""
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            stuck = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM future_commitments WHERE state='activating' AND activating_until <= ?",
                    (ts,),
                ).fetchall()
            ]
        finally:
            conn.close()

        reverted = 0
        expired = 0
        released_opps = 0
        for row in stuck:
            cid = int(row["id"])
            linked_opp = int(row["linked_opportunity_id"] or 0)
            if linked_opp > 0 and opportunity_coordinator is not None:
                opp = opportunity_coordinator.get_opportunity(linked_opp)
                if opp and str(opp["state"]) in ("recruiting", "assembling"):
                    opportunity_coordinator.release_party(linked_opp, "activating_lease_crash_recovery", now_ts=ts)
                    released_opps += 1

            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute("SELECT * FROM future_commitments WHERE id=? AND state='activating'", (cid,)).fetchone()
                if not cur:
                    conn.execute("COMMIT")
                    continue
                req_sec = (int(cur["required_minutes"] or cur["estimated_minutes"]) * 60)
                if int(cur["window_end"]) - ts >= req_sec:
                    conn.execute(
                        "UPDATE future_commitments SET state='scheduled', activating_until=0, linked_opportunity_id=0, "
                        "updated_at=?, revision=revision+1 WHERE id=?",
                        (ts, cid),
                    )
                    reverted += 1
                else:
                    conn.execute(
                        "UPDATE future_commitments SET state='expired', activating_until=0, outcome_reason='activating_crash_window_expired', "
                        "ended_at=?, updated_at=?, revision=revision+1 WHERE id=?",
                        (ts, ts, cid),
                    )
                    members = conn.execute(
                        "SELECT id, intent_id FROM future_commitment_members WHERE commitment_id=? AND state='accepted'",
                        (cid,),
                    ).fetchall()
                    for m in members:
                        conn.execute(
                            "UPDATE future_commitment_members SET state='expired', response_reason='activating_crash_window_expired', updated_at=? WHERE id=?",
                            (ts, int(m["id"])),
                        )
                        self._reopen_intent_if_valid(conn, int(m["intent_id"]), ts)
                    self._log_adjustment_refunds_if_any(
                        conn, cid, is_shadow=bool(int(cur["is_shadow"])), reason_code="activating_crash_window_expired", now_ts=ts
                    )
                    expired += 1
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

        return {"reverted_to_scheduled": reverted, "expired": expired, "released_orphan_opps": released_opps}

    def complete_commitment(
        self,
        commitment_id: int,
        *,
        outcome: str,
        reason_code: str = "",
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Finalize an 'activated' commitment from the linked opportunity's terminal outcome (P2-5)."""
        out_s = str(outcome or "").strip().lower()
        if out_s not in ("completed", "released", "aborted", "cancelled"):
            return {"ok": False, "reason": f"invalid_outcome:{out_s}"}
        ts = int(now_ts if now_ts is not None else time.time())
        final_state = "completed" if out_s == "completed" else "cancelled"
        member_state = "completed" if out_s == "completed" else "released"
        intent_state = "completed" if out_s == "completed" else "released"

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            comm = conn.execute("SELECT * FROM future_commitments WHERE id=?", (int(commitment_id),)).fetchone()
            if not comm:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "not_found"}
            if str(comm["state"]) != "activated":
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": f"commitment_not_activated:{comm['state']}"}
            is_sh = bool(int(comm["is_shadow"]))
            rc = (reason_code or f"opportunity_{out_s}")[:64]
            members = conn.execute(
                "SELECT id, intent_id FROM future_commitment_members WHERE commitment_id=?",
                (int(commitment_id),),
            ).fetchall()
            for m in members:
                conn.execute(
                    "UPDATE future_commitment_members SET state=?, response_reason=?, updated_at=? WHERE id=?",
                    (member_state, rc, ts, int(m["id"])),
                )
                self._close_intent(conn, int(m["intent_id"]), state=intent_state, reason=rc, now_ts=ts)
            new_rev = int(comm["revision"]) + 1
            conn.execute(
                "UPDATE future_commitments SET state=?, outcome_reason=?, ended_at=?, updated_at=?, revision=? WHERE id=?",
                (final_state, rc, ts, ts, new_rev, int(commitment_id)),
            )
            self._log_event(
                conn,
                event_type=f"commitment_{final_state}",
                commitment_id=int(commitment_id),
                is_shadow=is_sh,
                reason_code=rc,
                now_ts=ts,
            )
            conn.execute("COMMIT")
            return {"ok": True, "state": final_state, "revision": new_rev}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def revalidate_and_activate_commitment(
        self,
        commitment_id: int,
        *,
        world_ctx: dict[str, Any],
        opportunity_coordinator: OpportunityCoordinator | None = None,
        owner_recalled_personas: set[str] | frozenset[str] | None = None,
        readiness_obligated_personas: set[str] | frozenset[str] | None = None,
        headroom_state_path: Path | None = None,
        mem_avail_mib: float | None = None,
        tick_p99_ms: float | None = None,
        active_managed_groups: int = 0,
        max_managed_groups: int = DEFAULT_MAX_GROUPS,
        raid_stage: int = 0,
        now_ts: int | None = None,
        tz=None,
        max_telemetry_age_sec: int = 300,
        activating_lease_sec: int = DEFAULT_ACTIVATING_LEASE_SEC,
        allow_legacy_no_registry: bool = False,
    ) -> dict[str, Any]:
        """Atomically claim (`scheduled -> activating`), revalidate live, and hand off to OpportunityCoordinator.

        Guarantees:
        - Atomic CAS prevents concurrent double activation (P1-5) and races with deferred overrides (P0-3).
        - Honors pending overrides with saved `pending_override_target` (P0-1).
        - Distinguishes transient failures (retry later within window) from terminal failures (cancel) (P1-6).
        - Fails closed for live commitments when `opportunity_coordinator` or `headroom_state_path` is omitted,
          and reads actual active group count from DB (P1-7).
        - Applies approved temporary adjusted shift windows for online members so adjusted commitments
          activate cleanly end-to-end (P1-3).
        - Enforces `window_end - now >= needed_total * 60` so late activation never overruns reserved window (P0-4/P2-11).
        """
        ts = int(now_ts if now_ts is not None else time.time())
        recalled = set(owner_recalled_personas or ())
        readiness_req = set(readiness_obligated_personas or ())

        # Step 1: Atomically inspect, honor immediate overrides/checks, and CAS scheduled -> activating!
        conn = self._conn()
        claimed_rev = 0
        db_active_groups = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_stale_in_tx(conn, ts)
            comm = conn.execute("SELECT * FROM future_commitments WHERE id=?", (int(commitment_id),)).fetchone()
            if not comm:
                conn.execute("COMMIT")
                return {"ok": False, "reason": "not_found"}

            c_state = str(comm["state"])
            if c_state == "expired":
                conn.execute("COMMIT")
                return {"ok": False, "reason": "commitment_expired"}
            if c_state == "activating":
                conn.execute("COMMIT")
                return {"ok": False, "reason": "already_activating"}
            if c_state != "scheduled":
                conn.execute("COMMIT")
                return {"ok": False, "reason": f"commitment_not_scheduled:{c_state}"}

            is_shadow = bool(int(comm["is_shadow"]))
            if not is_shadow:
                if not coop_enabled():
                    conn.execute("COMMIT")
                    return {"ok": False, "reason": "coop_execution_disabled"}
                if str(comm["dungeon_id"]) not in enabled_dungeon_ids():
                    conn.execute("COMMIT")
                    return {"ok": False, "reason": f"dungeon_not_enabled_live:{comm['dungeon_id']}"}
                if allow_legacy_no_registry:
                    conn.execute("COMMIT")
                    return {"ok": False, "reason": "legacy_no_registry_forbidden_live"}
                # P1-7: Fail closed for live activation without coordinator or headroom path!
                if opportunity_coordinator is None:
                    conn.execute("COMMIT")
                    return {"ok": False, "reason": "opportunity_coordinator_required_for_live"}
                if headroom_state_path is None:
                    conn.execute("COMMIT")
                    return {"ok": False, "reason": "headroom_state_path_required_for_live"}

            pending_ov = str(comm["pending_override"] or "").strip()
            pending_target = str(comm["pending_override_target"] or "").strip()
            win_start = int(comm["window_start"])
            win_end = int(comm["window_end"])
            members = [
                dict(m)
                for m in conn.execute(
                    "SELECT * FROM future_commitment_members WHERE commitment_id=? AND state='accepted' ORDER BY id ASC",
                    (int(commitment_id),),
                ).fetchall()
            ]
            comm_dict = dict(comm)

            # Check pending override, recalled/readiness members, or window-not-open BEFORE claiming
            if pending_ov in VALID_OVERRIDE_KINDS:
                conn.execute("COMMIT")
                ov = self.override_commitment(
                    commitment_id,
                    override_kind=pending_ov,
                    target_persona_id=pending_target,
                    safe_boundary=True,
                    reason_code=f"honored_pending_{pending_ov}",
                    now_ts=ts,
                )
                return {"ok": False, "reason": ov.get("state", pending_ov), "override": ov}

            for m in members:
                pid = str(m["persona_id"])
                if pid in recalled:
                    conn.execute("COMMIT")
                    ov = self.override_commitment(
                        commitment_id,
                        override_kind="owner_recall",
                        target_persona_id=pid,
                        safe_boundary=True,
                        reason_code=f"owner_recall:{pid}",
                        now_ts=ts,
                    )
                    return {"ok": False, "reason": "overridden_owner_recall", "override": ov}
                if pid in readiness_req:
                    conn.execute("COMMIT")
                    ov = self.override_commitment(
                        commitment_id,
                        override_kind="raid_readiness",
                        target_persona_id=pid,
                        safe_boundary=True,
                        reason_code=f"raid_readiness:{pid}",
                        now_ts=ts,
                    )
                    return {"ok": False, "reason": "overridden_readiness", "override": ov}

            if ts < win_start:
                conn.execute("COMMIT")
                return {"ok": False, "reason": "window_not_yet_open", "window_start": win_start}
            if ts >= win_end:
                conn.execute("COMMIT")
                self.expire_stale(ts)
                return {"ok": False, "reason": "commitment_expired"}

            # Derive actual live active managed groups from DB (P1-7!)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "opportunities" in tables:
                cnt_row = conn.execute(
                    "SELECT COUNT(*) FROM opportunities WHERE is_shadow = ? AND state IN ('assembling','confirmed','active','cancelling')",
                    (1 if is_shadow else 0,),
                ).fetchone()
                db_active_groups = int(cnt_row[0] or 0)

            # Atomic CAS: scheduled -> activating (with lease) so concurrent callers cannot race!
            claimed_rev = int(comm["revision"]) + 1
            lease_until = ts + max(30, int(activating_lease_sec))
            updated = conn.execute(
                "UPDATE future_commitments SET state='activating', activating_until=?, updated_at=?, revision=? "
                "WHERE id=? AND state='scheduled' AND revision=? AND pending_override=''",
                (lease_until, ts, claimed_rev, int(commitment_id), int(comm["revision"])),
            ).rowcount
            conn.execute("COMMIT")
            if updated != 1:
                return {"ok": False, "reason": "commitment_claim_raced"}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

        def _revert_to_scheduled() -> None:
            c2 = self._conn()
            try:
                c2.execute("BEGIN IMMEDIATE")
                c2.execute(
                    "UPDATE future_commitments SET state='scheduled', activating_until=0, linked_opportunity_id=0, updated_at=? "
                    "WHERE id=? AND state='activating'",
                    (ts, int(commitment_id)),
                )
                c2.execute("COMMIT")
            except Exception:
                if c2.in_transaction:
                    c2.rollback()
                raise
            finally:
                c2.close()

        spec = DUNGEON_BY_ID.get(str(comm_dict["dungeon_id"]))
        if spec is None or not spec.supported:
            _revert_to_scheduled()
            return {"ok": False, "reason": f"unsupported_dungeon:{comm_dict['dungeon_id']}"}

        # Check fail-closed persisted hardware headroom for live commitments (using real DB active count!)
        if not is_shadow and headroom_state_path is not None:
            eff_active = max(db_active_groups, int(active_managed_groups))
            hr = evaluate_persisted_headroom(
                headroom_state_path,
                mem_avail_mib=mem_avail_mib,
                tick_p99_ms=tick_p99_ms,
                active_managed_groups=eff_active,
                max_managed_groups=max_managed_groups,
                now_ts=ts,
            )
            if not hr["admit_ok"]:
                _revert_to_scheduled()
                return {"ok": False, "reason": "resource_headroom_blocked", "headroom": hr}

        # Index live companions by (realm_id, guid)
        comps_by_key: dict[tuple[int, int], tuple[str, dict[str, Any]]] = {}
        for slug, comp in (world_ctx.get("companions") or {}).items():
            if not isinstance(comp, dict) or comp.get("guid") is None:
                continue
            c_rid = _parse_realm_id(comp.get("realm_id"))
            if c_rid is None:
                continue
            comps_by_key[(c_rid, int(comp["guid"]))] = (str(slug), comp)

        known_profiles = set(profiles())
        psnap = PersonaRegistry.read_only_snapshot(self.path)
        has_reviewed_policy = bool(
            str(comm_dict.get("adjustment_policy_id") or "").strip()
            and str(comm_dict.get("adjustment_reviewed_by") or "").strip()
        )

        # Revalidate EVERY member live
        member_failures: list[dict[str, Any]] = []
        validated_members: list[dict[str, Any]] = []
        for m in members:
            pid = str(m["persona_id"])
            rid = int(m["realm_id"])
            guid = int(m["character_guid"])
            inc = int(m["incarnation"])
            role = str(m["role"])

            found_comp = comps_by_key.get((rid, guid))
            shift_grp = str(found_comp[1].get("shift_group") or "") if found_comp else ""

            ok_p, r_p, _ = _verify_persona_with_snapshot(
                psnap,
                persona_id=pid,
                realm_id=rid,
                character_guid=guid,
                allow_legacy_no_registry=allow_legacy_no_registry,
                is_shadow=is_shadow,
                shift_group=shift_grp,
                enforce_single_session_on_char=True,
                expected_incarnation=inc,
            )
            if not ok_p:
                member_failures.append({"persona_id": pid, "reason": r_p, "transient": False})
                continue

            if found_comp is None:
                member_failures.append({"persona_id": pid, "reason": "offline_or_missing_in_world_ctx", "transient": True})
                continue
            slug, comp = found_comp

            # P1-3: If this member has a reviewed bounded schedule adjustment AND is actually online right now,
            # compute their effective remaining minutes from the approved temporary adjusted shift window!
            eff_comp = dict(comp)
            if has_reviewed_policy and int(m.get("adjusted_minutes") or 0) > 0 and comp.get("online"):
                try:
                    m_adj = json.loads(str(m.get("adjustment_json") or "{}"))
                except (ValueError, TypeError):
                    m_adj = {}
                aw = m_adj.get("adjusted_window")
                if isinstance(aw, list) and len(aw) == 2 and int(aw[0]) <= ts < int(aw[1]):
                    eff_comp["available_minutes"] = max(0, (int(aw[1]) - ts) // 60)

            prof = profile(slug) if slug in known_profiles else None
            ev = validate_candidate_for_dungeon(
                eff_comp,
                spec,
                required_role=role,
                allow_pilot_shadow=is_shadow,
                raid_stage=raid_stage,
                now_ts=ts,
                tz=tz,
                max_telemetry_age_sec=max_telemetry_age_sec,
                world_ctx=world_ctx,
                char_profile=prof,
                enrolled_coop=True,
                persona_state="active",
                char_active=True,
            )
            if not ev["available"]:
                base_r = str(ev["reason"]).split(":", 1)[0]
                is_trans = base_r in TRANSIENT_REVALIDATION_REASONS
                member_failures.append({"persona_id": pid, "reason": ev["reason"], "transient": is_trans})
                continue
            validated_members.append({
                "member": m,
                "slug": slug,
                "comp": eff_comp,
                "ev": ev,
            })

        # Party-wide joint shift window AND reserved window_end bound check (P0-4 & P2-11!)
        if not member_failures and validated_members:
            max_overhead = max(
                int(vm["ev"]["travel_prep"]["travel_minutes"]) + int(vm["ev"]["travel_prep"]["prep_minutes"])
                for vm in validated_members
            )
            needed_total = int(spec.estimated_minutes) + max_overhead + DEFAULT_WINDOW_BUFFER_MINUTES
            rem_window_min = (win_end - ts) // 60
            if rem_window_min < needed_total:
                member_failures.append({
                    "persona_id": validated_members[0]["member"]["persona_id"],
                    "reason": f"reserved_window_remaining_insufficient:{rem_window_min}<{needed_total}",
                    "transient": False,
                })
            else:
                for vm in validated_members:
                    avail_m = vm["ev"].get("available_minutes")
                    if avail_m is None or int(avail_m) < needed_total:
                        member_failures.append({
                            "persona_id": vm["member"]["persona_id"],
                            "reason": f"party_shift_window_insufficient:{avail_m}<{needed_total}",
                            "transient": False,
                        })

        if member_failures:
            # P1-6: Transient-only failures revert to 'scheduled' so next poll within grace window can succeed!
            all_transient = all(bool(f.get("transient")) for f in member_failures)
            req_sec = int(comm_dict.get("required_minutes") or spec.estimated_minutes) * 60
            if all_transient and (win_end - ts) > req_sec:
                _revert_to_scheduled()
                return {
                    "ok": False,
                    "reason": "revalidation_transient_retry",
                    "retryable": True,
                    "member_failures": member_failures,
                    "neutral": True,
                }
            self._fail_revalidation(commitment_id, member_failures, now_ts=ts)
            return {
                "ok": False,
                "reason": "revalidation_failed",
                "retryable": False,
                "member_failures": member_failures,
                "neutral": True,
            }

        # Step 2: Hand off to OpportunityCoordinator with full exception compensation & immediate persistence (P1-5!)
        linked_opp_id = 0
        try:
            if opportunity_coordinator is not None:
                creator_guid = int(validated_members[0]["member"]["character_guid"])
                opp = opportunity_coordinator.create_opportunity(
                    dungeon_id=spec.dungeon_id,
                    creator_guid=creator_guid,
                    faction=str(comm_dict["faction"]),
                    now_ts=ts,
                    is_shadow=is_shadow,
                )
                if not isinstance(opp, dict) or not opp.get("id"):
                    raise RuntimeError("opportunity_creation_failed")
                linked_opp_id = int(opp["id"])

                # Persist linked_opportunity_id immediately so crash recovery can release it!
                c_link = self._conn()
                try:
                    c_link.execute("BEGIN IMMEDIATE")
                    c_link.execute(
                        "UPDATE future_commitments SET linked_opportunity_id=?, updated_at=? WHERE id=? AND state='activating'",
                        (linked_opp_id, ts, int(commitment_id)),
                    )
                    c_link.execute("COMMIT")
                finally:
                    c_link.close()

                admit_failures: list[dict[str, Any]] = []
                for vm in validated_members:
                    m = vm["member"]
                    admit_res = opportunity_coordinator.admit_candidate(
                        opportunity_id=linked_opp_id,
                        expected_revision=int(opp["revision"]),
                        persona_id=str(m["persona_id"]),
                        character_guid=int(m["character_guid"]),
                        character_slug=str(vm["slug"]),
                        role=str(m["role"]),
                        reason_code=f"future_commitment:{commitment_id}",
                        candidate={**vm["comp"], "realm_id": int(m["realm_id"])},
                        raid_stage=raid_stage,
                        now_ts=ts,
                        tz=tz,
                        allow_legacy_no_registry=allow_legacy_no_registry,
                    )
                    if not admit_res["ok"]:
                        admit_failures.append({
                            "persona_id": str(m["persona_id"]),
                            "reason": admit_res["reason"],
                        })
                if admit_failures:
                    opportunity_coordinator.release_party(
                        linked_opp_id,
                        "future_commitment_partial_admit_failed",
                        now_ts=ts,
                    )
                    self._fail_revalidation(commitment_id, admit_failures, now_ts=ts)
                    return {
                        "ok": False,
                        "reason": "opportunity_admission_failed",
                        "member_failures": admit_failures,
                        "neutral": True,
                    }
        except Exception:
            if opportunity_coordinator is not None and linked_opp_id > 0:
                try:
                    opportunity_coordinator.release_party(
                        linked_opp_id,
                        "future_commitment_handoff_exception",
                        now_ts=ts,
                    )
                except Exception as comp_exc:
                    logger.warning("Compensation release_party failed for opp %s: %s", linked_opp_id, comp_exc)
            _revert_to_scheduled()
            raise

        # Step 3: Atomically advance activating -> activated, verifying NO deferred override arrived! (P0-3!)
        conn = self._conn()
        late_override_kind = ""
        late_override_target = ""
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur_row = conn.execute("SELECT * FROM future_commitments WHERE id=?", (int(commitment_id),)).fetchone()
            if (
                not cur_row
                or str(cur_row["state"]) != "activating"
                or int(cur_row["revision"]) != claimed_rev
                or str(cur_row["pending_override"] or "").strip() != ""
            ):
                if cur_row and str(cur_row["pending_override"] or "").strip():
                    late_override_kind = str(cur_row["pending_override"]).strip()
                    late_override_target = str(cur_row["pending_override_target"] or "").strip()
                    conn.execute(
                        "UPDATE future_commitments SET state='scheduled', activating_until=0, linked_opportunity_id=0, updated_at=? WHERE id=? AND state='activating'",
                        (ts, int(commitment_id)),
                    )
                    conn.execute("COMMIT")
                else:
                    conn.execute("ROLLBACK")
                if opportunity_coordinator is not None and linked_opp_id > 0:
                    opportunity_coordinator.release_party(linked_opp_id, "commitment_override_or_state_raced", now_ts=ts)
                if late_override_kind in VALID_OVERRIDE_KINDS:
                    ov = self.override_commitment(
                        commitment_id,
                        override_kind=late_override_kind,
                        target_persona_id=late_override_target,
                        safe_boundary=True,
                        reason_code=f"honored_late_{late_override_kind}",
                        now_ts=ts,
                    )
                    return {"ok": False, "reason": ov.get("state", late_override_kind), "override": ov}
                return {"ok": False, "reason": "commitment_state_changed_during_activation"}

            new_rev = claimed_rev + 1
            conn.execute(
                "UPDATE future_commitments SET state='activated', activating_until=0, linked_opportunity_id=?, "
                "outcome_reason='revalidated_and_activated', updated_at=?, revision=? WHERE id=?",
                (linked_opp_id, ts, new_rev, int(commitment_id)),
            )
            conn.execute(
                "UPDATE future_commitment_members SET state='activated', updated_at=? WHERE commitment_id=? AND state='accepted'",
                (ts, int(commitment_id)),
            )
            # Log bounded adjustment actually applied at activation (P1-2 audit!)
            adj_map = json.loads(str(cur_row["adjustment_applied_json"] or "{}"))
            for adj_pid, adj_info in adj_map.items():
                self._log_event(
                    conn,
                    event_type="bounded_adjustment_applied",
                    commitment_id=int(commitment_id),
                    persona_id=str(adj_pid),
                    is_shadow=is_shadow,
                    reason_code=str(cur_row["adjustment_policy_id"] or "reviewed_policy"),
                    details={
                        "policy_id": str(cur_row["adjustment_policy_id"] or ""),
                        "reviewed_by": str(cur_row["adjustment_reviewed_by"] or ""),
                        **adj_info,
                    },
                    now_ts=ts,
                )
            self._log_event(
                conn,
                event_type="commitment_activated",
                commitment_id=int(commitment_id),
                is_shadow=is_shadow,
                reason_code="revalidated_and_handed_off",
                details={"linked_opportunity_id": linked_opp_id},
                now_ts=ts,
            )
            conn.execute("COMMIT")
            return {
                "ok": True,
                "commitment_id": int(commitment_id),
                "state": "activated",
                "revision": new_rev,
                "linked_opportunity_id": linked_opp_id,
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            if opportunity_coordinator is not None and linked_opp_id > 0:
                try:
                    opportunity_coordinator.release_party(linked_opp_id, "activation_commit_exception", now_ts=ts)
                except Exception as comp_exc:
                    logger.warning("Compensation release_party failed after commit error: %s", comp_exc)
            raise
        finally:
            conn.close()

    def _fail_revalidation(self, commitment_id: int, failures: list[dict[str, Any]], *, now_ts: int) -> None:
        failed_map = {str(f["persona_id"]): str(f["reason"]) for f in failures}
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            comm = conn.execute(
                "SELECT state, is_shadow FROM future_commitments WHERE id=?",
                (int(commitment_id),),
            ).fetchone()
            if not comm or str(comm["state"]) not in ("scheduled", "activating"):
                conn.execute("ROLLBACK")
                return
            is_sh = bool(int(comm["is_shadow"]))
            summary = ",".join(f"{k}:{v}" for k, v in sorted(failed_map.items()))[:120]
            updated = conn.execute(
                "UPDATE future_commitments SET state='cancelled', activating_until=0, linked_opportunity_id=0, "
                "outcome_reason=?, ended_at=?, updated_at=?, revision=revision+1 "
                "WHERE id=? AND state IN ('scheduled','activating')",
                (f"revalidation_failed:{summary}"[:120], now_ts, now_ts, int(commitment_id)),
            ).rowcount
            if updated != 1:
                conn.execute("ROLLBACK")
                return

            members = conn.execute(
                "SELECT id, intent_id, persona_id FROM future_commitment_members WHERE commitment_id=? AND state='accepted'",
                (int(commitment_id),),
            ).fetchall()
            for m in members:
                pid = str(m["persona_id"])
                if pid in failed_map:
                    conn.execute(
                        "UPDATE future_commitment_members SET state='revalidation_failed', response_reason=?, updated_at=? WHERE id=?",
                        (failed_map[pid][:64], now_ts, int(m["id"])),
                    )
                    # P2-5: Close failed member's intent cleanly so it doesn't stay stuck at 'matched'!
                    self._close_intent(conn, int(m["intent_id"]), state="revalidation_failed", reason=failed_map[pid], now_ts=now_ts)
                else:
                    conn.execute(
                        "UPDATE future_commitment_members SET state='released', response_reason='teammate_revalidation_failed', updated_at=? WHERE id=?",
                        (now_ts, int(m["id"])),
                    )
                    self._reopen_intent_if_valid(conn, int(m["intent_id"]), now_ts)

            self._log_adjustment_refunds_if_any(conn, int(commitment_id), is_shadow=is_sh, reason_code="revalidation_failed", now_ts=now_ts)
            self._log_event(
                conn,
                event_type="revalidation_failed",
                commitment_id=int(commitment_id),
                is_shadow=is_sh,
                reason_code=summary,
                details={"failures": failures},
                now_ts=now_ts,
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def read_only_snapshot(
        db_path: Path,
        *,
        now_ts: int,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Strictly read-only snapshot of unexpired future intents & commitments (zero DB mutation!)."""
        out: dict[str, Any] = {
            "ok": True,
            "has_tables": False,
            "open_intents": [],
            "commitments": [],
            "errors": [],
        }
        own_conn = False
        if conn is None:
            if not Path(db_path).is_file():
                return out
            try:
                quoted = urllib.parse.quote(str(Path(db_path).resolve()), safe="/")
                conn = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=3.0)
                conn.row_factory = sqlite3.Row
                own_conn = True
            except sqlite3.Error as exc:
                out["ok"] = False
                out["errors"].append(f"{type(exc).__name__}: {exc}")
                return out

        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "future_dungeon_intents" not in tables and "future_commitments" not in tables:
                return out
            out["has_tables"] = True
            if "future_dungeon_intents" in tables:
                rows = conn.execute(
                    """
                    SELECT * FROM future_dungeon_intents
                    WHERE state = 'open' AND expires_at > ? AND latest_end_ts > ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (int(now_ts), int(now_ts)),
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    try:
                        d["roles"] = json.loads(str(d.get("roles_json") or "[]"))
                    except (ValueError, TypeError) as exc:
                        logger.warning("Skipping malformed roles_json on intent %s: %s", d.get("id"), exc)
                        continue
                    out["open_intents"].append(d)
            if "future_commitments" in tables:
                crows = conn.execute(
                    """
                    SELECT * FROM future_commitments
                    WHERE state IN ('proposed', 'scheduled', 'activating', 'activated') AND window_end > ?
                    ORDER BY window_start ASC, id ASC
                    """,
                    (int(now_ts),),
                ).fetchall()
                for r in crows:
                    d = dict(r)
                    try:
                        d["adjustment_applied"] = json.loads(str(d.get("adjustment_applied_json") or "{}"))
                    except (ValueError, TypeError) as exc:
                        logger.warning("Malformed adjustment_applied_json on commitment %s: %s", d.get("id"), exc)
                        d["adjustment_applied"] = {}
                    out["commitments"].append(d)
        except (sqlite3.Error, ValueError, TypeError) as exc:
            out["ok"] = False
            out["errors"].append(f"{type(exc).__name__}: {exc}")
            logger.warning("Read-only future commitment snapshot failed for %s: %s", db_path, exc)
        finally:
            if own_conn and conn is not None:
                conn.close()
        return out
