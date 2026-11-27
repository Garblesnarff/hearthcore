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

import fcntl
import json
import logging
import math
import os
import sqlite3
import time
import urllib.parse
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
    classify_level_band,
    compute_schedule_remaining_minutes,
    enabled_dungeon_ids,
    estimate_travel_and_prep_minutes,
    evaluate_live_role_capability,
    faction_from_race,
)
from .identity import (
    PROTECTED_OWNER_GROUPS,
    PersonaRegistry,
    is_protected_owner_group,
    persona_id_for_slug,
)

SCHEMA_VERSION = 1
DEFAULT_RECRUIT_TTL_SEC = 20 * 60
DEFAULT_CONFIRM_TTL_SEC = 2 * 60
DEFAULT_MAX_GROUPS = 1
DEFAULT_MAX_TOTAL_INSTANCES = 4
DEFAULT_MAX_ONLINE_POPULATION = 45
DEFAULT_MAX_HEADROOM_TELEMETRY_AGE_SEC = 300
MIN_AVAIL_RAM_MIB = 512.0
RECOVER_AVAIL_RAM_MIB = 640.0
MAX_TICK_P99_MS = 45.0
RECOVER_TICK_P99_MS = 38.0
MAX_SWAP_IO_PAGES_PER_SEC = 25.0
RECOVER_SWAP_IO_PAGES_PER_SEC = 10.0
MAX_CPU_PSI_SOME_AVG10 = 20.0
RECOVER_CPU_PSI_SOME_AVG10 = 10.0
MAX_CPU_THROTTLED_PCT = 15.0
RECOVER_CPU_THROTTLED_PCT = 5.0
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
    # opportunity_id -> outcome_reason for runs neutrally drained on the native MySQL side (Issue #22)
    drained_outcomes: dict[int, str] | None = None


def coop_mode() -> str:
    """Global 3-state Phase II cooperative admission mode: 'off' | 'draining' | 'enabled' (Issue #22).

    Safe default is 'off'. When 'draining' or 'off', new admissions are blocked while already-committed
    native groups finish combat safely and drain at a safe boundary.
    """
    raw_mode = (os.getenv("HEARTHCORE_COOP_MODE") or "").strip().lower()
    if raw_mode in ("draining", "drain"):
        return "draining"
    if raw_mode in ("enabled", "on", "1", "true", "yes"):
        return "enabled"
    if raw_mode in ("off", "disabled", "0", "false", "no"):
        return "off"
    flag = (os.getenv("HEARTHCORE_COOP_ENABLED") or "0").strip().lower()
    return "enabled" if flag in ("1", "true", "yes", "on") else "off"


def coop_enabled() -> bool:
    """True ONLY when cooperative admission mode is 'enabled' (default False = safe off)."""
    return coop_mode() == "enabled"


def _valid_nonneg_float(val: Any) -> float | None:
    """Return finite non-negative float or None (rejecting bool, None, NaN, +/-inf, negative)."""
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0.0:
        return None
    return f


def _valid_nonneg_int(val: Any) -> int | None:
    """Return non-negative int or None (rejecting bool, None, NaN, negative)."""
    if val is None or isinstance(val, bool):
        return None
    try:
        if isinstance(val, float) and not math.isfinite(val):
            return None
        iv = int(val)
    except (TypeError, ValueError):
        return None
    return iv if iv >= 0 else None


def check_resource_headroom(
    *,
    mem_avail_mib: float | None,
    tick_p99_ms: float | None,
    active_managed_groups: int | None,
    max_managed_groups: int = DEFAULT_MAX_GROUPS,
    prev_paused: bool = False,
    pending_proposals: int = 0,
    active_instances: int | None = None,
    human_instances: int = 0,
    max_total_instances: int = DEFAULT_MAX_TOTAL_INSTANCES,
    swap_io_pages_per_sec: float | None = None,
    cpu_psi_some_avg10: float | None = None,
    cpu_throttled_pct: float | None = None,
    online_population: int | None = None,
    max_online_population: int = DEFAULT_MAX_ONLINE_POPULATION,
    telemetry_ts: int | None = None,
    now_ts: int | None = None,
    max_telemetry_age_sec: int = DEFAULT_MAX_HEADROOM_TELEMETRY_AGE_SEC,
    require_extended_telemetry: bool = False,
) -> dict[str, Any]:
    """Check Acer i5-7400 hardware headroom with FAIL-CLOSED telemetry, NaN/staleness checks & hysteresis (Issues #12 & #61).

    Guards ONLY new automated queue admissions and proposal acceptances (never interrupts existing combat or humans).
    """
    reasons: list[str] = []
    headroom_reasons: list[str] = []
    cap_reasons: list[str] = []

    def _add_hr(msg: str) -> None:
        reasons.append(msg)
        headroom_reasons.append(msg)

    def _add_cap(msg: str) -> None:
        reasons.append(msg)
        cap_reasons.append(msg)

    # 1. Telemetry freshness (rejects stale or future-skewed timestamps; fails closed when required)
    if telemetry_ts is not None:
        t_ts = _valid_nonneg_int(telemetry_ts)
        n_ts = _valid_nonneg_int(now_ts if now_ts is not None else int(time.time()))
        if t_ts is None or t_ts <= 0 or n_ts is None:
            _add_hr("invalid_telemetry_timestamp")
        else:
            age = n_ts - t_ts
            if age < 0 or age > int(max_telemetry_age_sec):
                _add_hr(f"stale_telemetry:{age}s>{int(max_telemetry_age_sec)}s")
    elif require_extended_telemetry:
        _add_hr("missing_telemetry_timestamp")

    # 2. Managed groups + pending proposals cap (cap blocks do NOT trigger hardware headroom hysteresis pause!)
    act_grps = _valid_nonneg_int(active_managed_groups)
    pend_props = _valid_nonneg_int(pending_proposals)
    if act_grps is None:
        _add_hr("missing_group_count_telemetry")
        effective_groups: int | None = None
    elif pend_props is None:
        _add_hr("invalid_pending_proposals")
        effective_groups = None
    else:
        effective_groups = act_grps + pend_props
        if effective_groups >= int(max_managed_groups):
            _add_cap(f"group_cap_reached:{effective_groups}/{int(max_managed_groups)}")

    # 3. Hysteresis thresholds (pause vs recover — applies ONLY to hardware/telemetry headroom!)
    ram_thresh = RECOVER_AVAIL_RAM_MIB if prev_paused else MIN_AVAIL_RAM_MIB
    tick_thresh = RECOVER_TICK_P99_MS if prev_paused else MAX_TICK_P99_MS
    swap_thresh = RECOVER_SWAP_IO_PAGES_PER_SEC if prev_paused else MAX_SWAP_IO_PAGES_PER_SEC
    cpu_psi_thresh = RECOVER_CPU_PSI_SOME_AVG10 if prev_paused else MAX_CPU_PSI_SOME_AVG10
    cpu_thr_thresh = RECOVER_CPU_THROTTLED_PCT if prev_paused else MAX_CPU_THROTTLED_PCT

    # 4. RAM & world tick p99 (ALWAYS required, NaN/Inf/negative fail closed!)
    ram_val = _valid_nonneg_float(mem_avail_mib)
    if mem_avail_mib is None:
        _add_hr("missing_ram_telemetry")
    elif ram_val is None:
        _add_hr("invalid_ram_telemetry")
    elif ram_val < ram_thresh:
        _add_hr(f"low_ram_headroom:{ram_val:.0f}MiB<{ram_thresh:.0f}MiB")

    tick_val = _valid_nonneg_float(tick_p99_ms)
    if tick_p99_ms is None:
        _add_hr("missing_tick_telemetry")
    elif tick_val is None:
        _add_hr("invalid_tick_telemetry")
    elif tick_val > tick_thresh:
        _add_hr(f"high_tick_p99:{tick_val:.1f}ms>{tick_thresh:.1f}ms")

    # 5. Swap I/O rate (fail closed on NaN/negative/above-threshold, or missing when extended required)
    swap_val = _valid_nonneg_float(swap_io_pages_per_sec)
    if swap_io_pages_per_sec is None:
        if require_extended_telemetry:
            _add_hr("missing_swap_telemetry")
    elif swap_val is None:
        _add_hr("invalid_swap_telemetry")
    elif swap_val > swap_thresh:
        _add_hr(f"high_swap_io:{swap_val:.1f}/s>{swap_thresh:.1f}/s")

    # 6. CPU PSI & cgroup CPU throttling
    psi_val = _valid_nonneg_float(cpu_psi_some_avg10)
    if cpu_psi_some_avg10 is None:
        if require_extended_telemetry:
            _add_hr("missing_cpu_psi_telemetry")
    elif psi_val is None:
        _add_hr("invalid_cpu_psi_telemetry")
    elif psi_val > cpu_psi_thresh:
        _add_hr(f"high_cpu_psi:{psi_val:.1f}%>{cpu_psi_thresh:.1f}%")

    thr_val = _valid_nonneg_float(cpu_throttled_pct)
    if cpu_throttled_pct is not None:
        if thr_val is None:
            _add_hr("invalid_cpu_throttle_telemetry")
        elif thr_val > cpu_thr_thresh:
            _add_hr(f"high_cpu_throttle:{thr_val:.1f}%>{cpu_thr_thresh:.1f}%")

    # 7. Active + optional additive human instances + pending proposals cap
    #    Note: when passing total sMapMgr->GetNumInstances (which already includes human instances), pass human_instances=0.
    inst_val = _valid_nonneg_int(active_instances)
    hum_inst = _valid_nonneg_int(human_instances)
    effective_instances: int | None = None
    if active_instances is None:
        if require_extended_telemetry:
            _add_hr("missing_instance_telemetry")
    elif inst_val is None or hum_inst is None:
        _add_hr("invalid_instance_telemetry")
    else:
        effective_instances = inst_val + hum_inst + (pend_props or 0)
        if effective_instances >= int(max_total_instances):
            _add_cap(f"instance_cap_reached:{effective_instances}/{int(max_total_instances)}")

    # 8. Online population cap (no forced mass logins)
    pop_val = _valid_nonneg_int(online_population)
    if online_population is None:
        if require_extended_telemetry:
            _add_hr("missing_population_telemetry")
    elif pop_val is None:
        _add_hr("invalid_population_telemetry")
    elif pop_val >= int(max_online_population):
        _add_cap(f"population_cap_reached:{pop_val}/{int(max_online_population)}")

    admit_ok = len(reasons) == 0
    headroom_paused = len(headroom_reasons) > 0
    return {
        "admit_ok": admit_ok,
        "paused": headroom_paused,
        "headroom_paused": headroom_paused,
        "cap_blocked": len(cap_reasons) > 0,
        "prev_paused": bool(prev_paused),
        "reasons": reasons,
        "headroom_reasons": headroom_reasons,
        "cap_reasons": cap_reasons,
        "active_managed_groups": act_grps,
        "pending_proposals": pend_props,
        "effective_groups": effective_groups,
        "max_managed_groups": int(max_managed_groups),
        "effective_instances": effective_instances,
        "max_total_instances": int(max_total_instances),
        "online_population": pop_val,
        "max_online_population": int(max_online_population),
        "mem_avail_mib": round(ram_val, 1) if ram_val is not None else None,
        "tick_p99_ms": round(tick_val, 1) if tick_val is not None else None,
        "swap_io_pages_per_sec": round(swap_val, 1) if swap_val is not None else None,
        "cpu_psi_some_avg10": round(psi_val, 1) if psi_val is not None else None,
    }


def evaluate_persisted_headroom(
    state_path: Path,
    *,
    mem_avail_mib: float | None,
    tick_p99_ms: float | None,
    active_managed_groups: int | None,
    max_managed_groups: int = DEFAULT_MAX_GROUPS,
    now_ts: int | None = None,
    pending_proposals: int = 0,
    active_instances: int | None = None,
    human_instances: int = 0,
    max_total_instances: int = DEFAULT_MAX_TOTAL_INSTANCES,
    swap_io_pages_per_sec: float | None = None,
    cpu_psi_some_avg10: float | None = None,
    cpu_throttled_pct: float | None = None,
    online_population: int | None = None,
    max_online_population: int = DEFAULT_MAX_ONLINE_POPULATION,
    telemetry_ts: int | None = None,
    max_telemetry_age_sec: int = DEFAULT_MAX_HEADROOM_TELEMETRY_AGE_SEC,
    require_extended_telemetry: bool = False,
) -> dict[str, Any]:
    """Evaluate headroom with flock-serialized persisted hysteresis on disk (Issues #12 & #61)."""
    ts = int(now_ts if now_ts is not None else time.time())
    lock_path = state_path.with_suffix(".lock")

    def _run_eval(prev_paused: bool) -> dict[str, Any]:
        return check_resource_headroom(
            mem_avail_mib=mem_avail_mib,
            tick_p99_ms=tick_p99_ms,
            active_managed_groups=active_managed_groups,
            max_managed_groups=max_managed_groups,
            prev_paused=prev_paused,
            pending_proposals=pending_proposals,
            active_instances=active_instances,
            human_instances=human_instances,
            max_total_instances=max_total_instances,
            swap_io_pages_per_sec=swap_io_pages_per_sec,
            cpu_psi_some_avg10=cpu_psi_some_avg10,
            cpu_throttled_pct=cpu_throttled_pct,
            online_population=online_population,
            max_online_population=max_online_population,
            telemetry_ts=telemetry_ts,
            now_ts=ts,
            max_telemetry_age_sec=max_telemetry_age_sec,
            require_extended_telemetry=require_extended_telemetry,
        )

    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                prev_paused = False
                try:
                    if state_path.is_file():
                        saved = json.loads(state_path.read_text(encoding="utf-8"))
                        if not isinstance(saved, dict) or not isinstance(saved.get("paused"), bool):
                            raise ValueError("missing boolean 'paused' key")
                        prev_paused = saved["paused"]
                except Exception as exc:
                    logger.warning("Corrupt headroom state at %s (%s); failing closed", state_path, exc)
                    prev_paused = True  # fail closed on corrupt state

                res = _run_eval(prev_paused)
                try:
                    tmp = state_path.with_suffix(".json.tmp")
                    tmp.write_text(
                        json.dumps(
                            {"paused": bool(res["headroom_paused"]), "updated_at": ts, "reasons": res["headroom_reasons"]},
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    tmp.replace(state_path)
                except OSError as exc:
                    logger.warning("Failed persisting headroom state to %s: %s; failing closed", state_path, exc)
                    res["admit_ok"] = False
                    res["paused"] = True
                    res["headroom_paused"] = True
                    if "headroom_state_unwritable" not in res["reasons"]:
                        res["reasons"].append("headroom_state_unwritable")
                        res["headroom_reasons"].append("headroom_state_unwritable")
                return res
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        logger.warning("Unable to open/lock headroom state dir for %s (%s); failing closed", state_path, exc)
        # Fail closed when runtime directory is read-only / lock cannot be opened
        res = _run_eval(prev_paused=True)
        res["admit_ok"] = False
        res["paused"] = True
        res["headroom_paused"] = True
        if "headroom_state_unwritable" not in res["reasons"]:
            res["reasons"].append("headroom_state_unwritable")
            res["headroom_reasons"].append("headroom_state_unwritable")
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
    active_instances: int | None = None,
    human_instances: int = 0,
    max_total_instances: int = DEFAULT_MAX_TOTAL_INSTANCES,
    swap_io_pages_per_sec: float | None = None,
    cpu_psi_some_avg10: float | None = None,
    cpu_throttled_pct: float | None = None,
    online_population: int | None = None,
    max_online_population: int = DEFAULT_MAX_ONLINE_POPULATION,
    telemetry_ts: int | None = None,
    max_telemetry_age_sec: int = DEFAULT_MAX_HEADROOM_TELEMETRY_AGE_SEC,
    enforce_live_flags: bool = True,
    require_extended_telemetry: bool = False,
    additional_logins_needed: int = 0,
) -> dict[str, Any]:
    """Cooperative admission path enforcing persisted headroom, hysteresis & atomic DB caps (Issues #12 & #61)."""
    if additional_logins_needed > 0:
        return {"ok": False, "reason": "forced_mass_login_forbidden"}
    hr = evaluate_persisted_headroom(
        state_path,
        mem_avail_mib=mem_avail_mib,
        tick_p99_ms=tick_p99_ms,
        active_managed_groups=active_managed_groups,
        max_managed_groups=max_managed_groups,
        now_ts=now_ts,
        active_instances=active_instances,
        human_instances=human_instances,
        max_total_instances=max_total_instances,
        swap_io_pages_per_sec=swap_io_pages_per_sec,
        cpu_psi_some_avg10=cpu_psi_some_avg10,
        cpu_throttled_pct=cpu_throttled_pct,
        online_population=online_population,
        max_online_population=max_online_population,
        telemetry_ts=telemetry_ts,
        max_telemetry_age_sec=max_telemetry_age_sec,
        require_extended_telemetry=require_extended_telemetry,
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
        enforce_live_flags=enforce_live_flags,
        max_managed_groups=max_managed_groups,
        external_active_groups=active_managed_groups,
        active_instances=(int(active_instances) + int(human_instances)) if active_instances is not None else None,
        max_total_instances=max_total_instances,
        online_population=online_population,
        max_online_population=max_online_population,
    )
    res["headroom"] = hr
    return res


def admit_live_cooperative_opportunity(
    coordinator: "OpportunityCoordinator",
    opportunity_id: int,
    *,
    expected_revision: int,
    state_path: Path,
    mem_avail_mib: float | None,
    tick_p99_ms: float | None,
    swap_io_pages_per_sec: float | None,
    cpu_psi_some_avg10: float | None,
    active_managed_groups: int,
    active_instances: int | None,
    online_population: int | None,
    telemetry_ts: int | None,
    human_instances: int = 0,
    cpu_throttled_pct: float | None = None,
    max_managed_groups: int = DEFAULT_MAX_GROUPS,
    max_total_instances: int = DEFAULT_MAX_TOTAL_INSTANCES,
    max_online_population: int = DEFAULT_MAX_ONLINE_POPULATION,
    max_telemetry_age_sec: int = DEFAULT_MAX_HEADROOM_TELEMETRY_AGE_SEC,
    additional_logins_needed: int = 0,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Strict production/staging live admission entrypoint (Issue #12).

    Enforces ALL gates fail-closed:
    - HEARTHCORE_COOP_ENABLED=1 & dungeon in HEARTHCORE_COOP_DUNGEONS
    - Fresh timestamped telemetry (RAM, tick p99, swap I/O, CPU PSI, live instances, population; rejects NaN/stale)
    - Persisted hysteresis (flock-serialized)
    - Zero forced mass logins (additional_logins_needed == 0)
    - Atomic BEGIN IMMEDIATE group/instance/population cap enforcement across concurrent proposals!
    """
    return admit_cooperative_opportunity(
        coordinator,
        opportunity_id,
        expected_revision=expected_revision,
        state_path=state_path,
        mem_avail_mib=mem_avail_mib,
        tick_p99_ms=tick_p99_ms,
        active_managed_groups=active_managed_groups,
        max_managed_groups=max_managed_groups,
        now_ts=now_ts,
        active_instances=active_instances,
        human_instances=human_instances,
        max_total_instances=max_total_instances,
        swap_io_pages_per_sec=swap_io_pages_per_sec,
        cpu_psi_some_avg10=cpu_psi_some_avg10,
        cpu_throttled_pct=cpu_throttled_pct,
        online_population=online_population,
        max_online_population=max_online_population,
        telemetry_ts=telemetry_ts,
        max_telemetry_age_sec=max_telemetry_age_sec,
        enforce_live_flags=True,
        require_extended_telemetry=True,
        additional_logins_needed=additional_logins_needed,
    )


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
        eligibility: dict[str, Any] | None = None,
    ) -> bool:
        """Record voluntary character interest if opportunity is recruiting at expected_revision.

        When `eligibility` is provided, strictly validates AC1 live eligibility (wrong role, stale/
        offline/locked/short shift, wrong faction, protected core, disabled/unsupported dungeon,
        unknown live state) BEFORE writing to the database.
        """
        return self._express_interest_tx(
            opportunity_id=opportunity_id,
            expected_revision=expected_revision,
            persona_id=persona_id,
            character_guid=character_guid,
            character_slug=character_slug,
            role=role,
            reason_code=reason_code,
            available_until=available_until,
            realm_id=realm_id,
            incarnation=incarnation,
            now_ts=now_ts,
            eligibility=eligibility,
            require_persona_registry=False,
        )["ok"]

    def _express_interest_tx(
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
        eligibility: dict[str, Any] | None = None,
        require_persona_registry: bool = False,
        allow_legacy_no_registry: bool = False,
    ) -> dict[str, Any]:
        """Atomic BEGIN IMMEDIATE interest registration with optional in-transaction PersonaRegistry verification (P1-2/P1-3)."""
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            opp = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
            if not opp or str(opp["state"]) != "recruiting" or int(opp["revision"]) != int(expected_revision):
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "opportunity_not_recruiting_or_revision_mismatch"}
            if int(opp["recruit_until"]) <= ts or int(available_until) <= ts:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": "opportunity_or_availability_expired"}

            verified_inc = int(incarnation)
            if eligibility is not None:
                spec = DUNGEON_BY_ID.get(str(opp["dungeon_id"]))
                if spec is None:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": f"unknown_dungeon:{opp['dungeon_id']}"}
                if is_protected_owner_group(eligibility.get("shift_group")):
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "protected_core_excluded"}
                ev = validate_candidate_for_dungeon(
                    eligibility,
                    spec,
                    required_role=role,
                    allow_pilot_shadow=bool(int(opp["is_shadow"])),
                    raid_stage=int(eligibility.get("raid_stage") or 0),
                    now_ts=ts,
                    tz=eligibility.get("tz"),
                )
                if not ev["available"]:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": ev["reason"], "evaluation": ev}

            if require_persona_registry:
                # P1-3: Check PersonaRegistry INSIDE the same BEGIN IMMEDIATE transaction (zero TOCTOU gap!)
                psnap = PersonaRegistry.read_only_snapshot(self.path, conn=conn)
                if not psnap["ok"]:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": "persona_registry_unreadable"}
                if not psnap["has_registry"]:
                    if not allow_legacy_no_registry:
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": "persona_registry_missing"}
                else:
                    if int(realm_id) <= 0:
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": "invalid_realm_id"}
                    prec = psnap["by_key"].get((int(realm_id), int(character_guid)))
                    if prec is None:
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": "unresolved_persona_mapping"}
                    if prec["persona_id"] != persona_id:
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": "persona_id_mismatch"}
                    if prec["state"] != "active":
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": f"quarantined_persona:{prec['state']}"}
                    if not prec["char_active"]:
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": "inactive_persona_character"}
                    if prec["core_protected"] and not prec["enrolled_coop"]:
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": "protected_core_excluded"}
                    if not prec["enrolled_coop"]:
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": "not_enrolled_coop"}
                    # P1-2: Enforce one active session per persona at admission!
                    if persona_id in psnap["open_dual_session_personas"]:
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": "persona_dual_session_conflict"}
                    active_sess = psnap["by_persona_session"].get(persona_id)
                    if active_sess is not None and active_sess != (int(realm_id), int(character_guid), int(prec["incarnation"])):
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": "persona_session_on_other_character"}
                    verified_inc = int(prec["incarnation"])

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
                    verified_inc, character_slug.lower(), role.lower(), "interested", reason_code[:64],
                    int(available_until), ts, ts,
                ),
            )
            conn.execute("COMMIT")
            return {"ok": True, "reason": "admitted", "role": role.lower(), "incarnation": verified_inc, "available_until": int(available_until)}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def admit_candidate(
        self,
        *,
        opportunity_id: int,
        expected_revision: int,
        persona_id: str,
        character_guid: int,
        character_slug: str,
        role: str,
        reason_code: str,
        candidate: dict[str, Any],
        raid_stage: int = 0,
        now_ts: int | None = None,
        tz=None,
        allow_legacy_no_registry: bool = False,
    ) -> dict[str, Any]:
        """Strict fail-closed AC1 & Issue #25 admission gate + voluntary interest registration.

        Runs eligibility AND PersonaRegistry verification atomically inside the write transaction.
        """
        ts = int(now_ts if now_ts is not None else time.time())
        opp = self.get_opportunity(opportunity_id)
        if not opp:
            return {"ok": False, "reason": "not_found"}
        spec = DUNGEON_BY_ID.get(str(opp["dungeon_id"]))
        if spec is None:
            return {"ok": False, "reason": f"unknown_dungeon:{opp['dungeon_id']}"}

        # P0-2: Validate realm_id explicitly (never coerce 0 -> 1!)
        raw_realm = candidate.get("realm_id")
        if raw_realm is not None:
            try:
                realm_id = int(raw_realm)
            except (TypeError, ValueError):
                return {"ok": False, "reason": "invalid_realm_id"}
            if realm_id <= 0:
                return {"ok": False, "reason": "invalid_realm_id"}
        else:
            realm_id = 1

        ev = validate_candidate_for_dungeon(
            candidate,
            spec,
            required_role=role,
            allow_pilot_shadow=bool(opp["is_shadow"]),
            raid_stage=raid_stage,
            now_ts=ts,
            tz=tz,
        )
        if not ev["available"]:
            return {"ok": False, "reason": ev["reason"], "evaluation": ev}

        avail_mins = int(ev["available_minutes"])
        avail_until = ts + max(60, avail_mins * 60)
        return self._express_interest_tx(
            opportunity_id=opportunity_id,
            expected_revision=expected_revision,
            persona_id=persona_id,
            character_guid=character_guid,
            character_slug=character_slug,
            role=role,
            reason_code=reason_code,
            available_until=avail_until,
            realm_id=realm_id,
            now_ts=ts,
            eligibility={**candidate, "raid_stage": raid_stage, "tz": tz},
            require_persona_registry=True,
            allow_legacy_no_registry=allow_legacy_no_registry,
        )

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
        enforce_live_flags: bool = False,
        max_managed_groups: int | None = DEFAULT_MAX_GROUPS,
        external_active_groups: int = 0,
        active_instances: int | None = None,
        max_total_instances: int | None = None,
        online_population: int | None = None,
        max_online_population: int | None = None,
    ) -> dict[str, Any]:
        """Atomically reserve 1 tank, 1 healer, 3 damage among voluntary interests.

        Enforces unique active reservation/commitment per `persona_id` across ALL opportunities AND
        (for live opportunities) atomically enforces feature flags, supported/enabled dungeon allowlist,
        and group/instance/population caps inside the same `BEGIN IMMEDIATE` transaction (Issues #12 & #13).
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

            opp_is_shadow = int(opp["is_shadow"])
            dungeon_id = str(opp["dungeon_id"])
            if opp_is_shadow == 0:
                spec = DUNGEON_BY_ID.get(dungeon_id)
                if spec is None or not spec.supported:
                    conn.execute("COMMIT")
                    return {"ok": False, "reason": f"unsupported_dungeon:{dungeon_id}"}
                if enforce_live_flags:
                    if not coop_enabled():
                        conn.execute("COMMIT")
                        return {"ok": False, "reason": "coop_disabled"}
                    if dungeon_id not in enabled_dungeon_ids():
                        conn.execute("COMMIT")
                        return {"ok": False, "reason": f"dungeon_disabled_live:{dungeon_id}"}

            roles_needed: dict[str, int] = json.loads(str(opp["roles_json"]))
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

            if opp_is_shadow == 0:
                # Atomic BEGIN IMMEDIATE cap check across concurrent proposals (Issue #12):
                # Count ALL unexpired live non-shadow assembling/confirmed/active/cancelling groups in DB!
                live_row = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total_live,
                        SUM(CASE WHEN state IN ('assembling', 'confirmed') THEN 1 ELSE 0 END) AS pending_live
                    FROM opportunities
                    WHERE is_shadow = 0
                      AND id != ?
                      AND (
                          (state IN ('assembling', 'confirmed') AND (confirm_until = 0 OR confirm_until > ?))
                          OR state IN ('active', 'cancelling')
                      )
                    """,
                    (int(opportunity_id), ts),
                ).fetchone()
                db_live_groups = int(live_row["total_live"] or 0) if live_row else 0
                db_pending_live = int(live_row["pending_live"] or 0) if live_row else 0

                if max_managed_groups is not None:
                    eff_groups = max(db_live_groups, int(external_active_groups or 0))
                    if eff_groups >= int(max_managed_groups):
                        conn.execute("COMMIT")
                        return {
                            "ok": False,
                            "reason": "group_cap_reached",
                            "active_managed_groups": eff_groups,
                            "max_managed_groups": int(max_managed_groups),
                        }
                if active_instances is not None and max_total_instances is not None:
                    eff_instances = int(active_instances) + db_pending_live
                    if eff_instances >= int(max_total_instances):
                        conn.execute("COMMIT")
                        return {
                            "ok": False,
                            "reason": "instance_cap_reached",
                            "effective_instances": eff_instances,
                            "max_total_instances": int(max_total_instances),
                        }
                if online_population is not None and max_online_population is not None:
                    if int(online_population) >= int(max_online_population):
                        conn.execute("COMMIT")
                        return {
                            "ok": False,
                            "reason": "population_cap_reached",
                            "online_population": int(online_population),
                            "max_online_population": int(max_online_population),
                        }

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
            if opp_state in ("cancelled", "expired", "aborted", "drained"):
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
        if out_s not in ("completed", "aborted", "failed", "disbanded", "drained"):
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
            target_state = "completed" if out_s == "completed" else ("drained" if out_s == "drained" else "aborted")

            # Idempotent duplicate terminal callback with matching run ID
            if opp_state == target_state and (
                (r_id and stored_run == r_id)
                or (target_state in ("aborted", "drained") and not stored_run and ((q_id and q_id == stored_q) or (g_id > 0 and g_id == stored_g)))
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
                    "drained_reconciled": 0,
                }

            groups = snap.active_group_ids
            queues = snap.active_queue_ids
            runs = snap.active_run_ids
            corr_map = snap.correlation_map or {}
            drained_map = snap.drained_outcomes or {}

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

            # 2. Reconcile live 'active'/'cancelling' opportunities:
            #    - First map native 'drained' outcomes neutrally (Issue #22) so draining/off mode never aborts them!
            #    - Otherwise abort ONLY when every relevant native ID set is known (non-None!) and run is gone.
            aborted_missing = 0
            drained_reconciled = 0
            for r in conn.execute(
                "SELECT id, state, group_id, native_queue_id, native_run_id FROM opportunities "
                "WHERE state IN ('active','cancelling') AND is_shadow=0"
            ).fetchall():
                oid = int(r["id"])
                if oid in drained_map:
                    reason_txt = (str(drained_map[oid] or "coop_drained").strip() or "coop_drained")[:120]
                    conn.execute(
                        "UPDATE opportunity_interests SET state='released', reserved_until=0, "
                        "reason_code=?, updated_at=? "
                        "WHERE opportunity_id=? AND state IN ('active','accepted','reserved')",
                        (reason_txt[:64], ts, oid),
                    )
                    drained_reconciled += conn.execute(
                        "UPDATE opportunities SET state='drained', native_state='drained', "
                        "outcome_reason=?, ended_at=?, revision=revision+1 WHERE id=?",
                        (reason_txt, ts, oid),
                    ).rowcount
                    continue
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
                "drained_reconciled": drained_reconciled,
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


def _read_existing_enrollments_readonly(db_path: Path) -> dict[tuple[int, int], tuple[str, bool]]:
    """Strictly read-only lookup of existing persona enrollments keyed by (realm_id, guid); NEVER creates or mutates db_path."""
    ctx = _read_shadow_db_context_readonly(db_path, now_ts=int(time.time()))
    return {
        key: (rec["persona_id"], rec["enrolled_coop"])
        for key, rec in ctx["enrollments"].items()
    }


def _read_shadow_db_context_readonly(db_path: Path, *, now_ts: int) -> dict[str, Any]:
    """Strictly read-only single-snapshot lookup of persona enrollments, active sessions, pauses, and unexpired live interests.

    Guarantees (Issues #14, #15 & #25):
    - Uses ONE single read-only SQLite connection so persona & opportunity views are consistent (P2-9).
    - NEVER creates or mutates db_path.
    - Fails closed (`ok=False`, `errors=[...]`) on any SQLite read error or mapping conflict.
    """
    out: dict[str, Any] = {
        "ok": True,
        "has_registry": False,
        "errors": [],
        "enrollments": {},                  # (realm_id, guid) -> record dict
        "by_persona_session": {},           # persona_id -> (realm_id, guid, inc)
        "open_dual_session_personas": set(),
        "paused_slugs": set(),
        "busy_personas": set(),
        "queued_by_dungeon": {},
        "future_intents_by_dungeon": {},
        "future_commitments_by_dungeon": {},
        "active_live_groups": 0,
        "pending_live_proposals": 0,
    }
    if not db_path.is_file():
        return out
    conn: sqlite3.Connection | None = None
    try:
        quoted = urllib.parse.quote(str(db_path.resolve()), safe="/")
        conn = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row

        psnap = PersonaRegistry.read_only_snapshot(db_path, conn=conn)
        out["has_registry"] = bool(psnap["has_registry"])
        if not psnap["ok"]:
            out["ok"] = False
            out["errors"].extend(psnap["errors"])
        out["enrollments"] = dict(psnap["by_key"])
        out["by_persona_session"] = dict(psnap["by_persona_session"])
        out["open_dual_session_personas"] = set(psnap["open_dual_session_personas"])

        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        if "paused_characters" in tables:
            for r in conn.execute("SELECT character_id FROM paused_characters WHERE paused = 1").fetchall():
                out["paused_slugs"].add(str(r["character_id"]).strip().lower())

        if "opportunities" in tables and "opportunity_interests" in tables:
            opp_cols = {r[1] for r in conn.execute("PRAGMA table_info(opportunities)").fetchall()}
            has_confirm = "confirm_until" in opp_cols
            # Issue #12: Count ACTUAL live active & pending assembling/confirmed groups (never hardcode 0 in shadow!)
            live_cnt_clause = (
                "((state IN ('assembling','confirmed') AND (confirm_until = 0 OR confirm_until > ?)) "
                "OR state IN ('active','cancelling'))"
                if has_confirm
                else "state IN ('assembling','confirmed','active','cancelling')"
            )
            live_cnt_params: tuple[Any, ...] = (int(now_ts),) if has_confirm else ()
            cnt_row = conn.execute(
                f"""
                SELECT
                    SUM(CASE WHEN state IN ('active','cancelling') THEN 1 ELSE 0 END) AS active_cnt,
                    SUM(CASE WHEN state IN ('assembling','confirmed') THEN 1 ELSE 0 END) AS pending_cnt
                FROM opportunities
                WHERE is_shadow = 0 AND {live_cnt_clause}
                """,
                live_cnt_params,
            ).fetchone()
            if cnt_row:
                out["active_live_groups"] = int(cnt_row["active_cnt"] or 0)
                out["pending_live_proposals"] = int(cnt_row["pending_cnt"] or 0)
            opp_ttl_clause = (
                "((o.state = 'recruiting' AND o.recruit_until > ?) "
                "OR (o.state IN ('assembling','confirmed') AND (o.confirm_until = 0 OR o.confirm_until > ?)) "
                "OR o.state IN ('active','cancelling'))"
                if has_confirm
                else "((o.state = 'recruiting' AND o.recruit_until > ?) OR o.state IN ('assembling','confirmed','active','cancelling'))"
            )
            params: tuple[Any, ...] = (int(now_ts), int(now_ts), int(now_ts)) if has_confirm else (int(now_ts), int(now_ts))
            rows = conn.execute(
                f"""
                SELECT o.dungeon_id, o.state AS opp_state, oi.persona_id, oi.character_guid, oi.role, oi.state
                FROM opportunity_interests oi
                JOIN opportunities o ON o.id = oi.opportunity_id
                WHERE o.is_shadow = 0
                  AND {opp_ttl_clause}
                  AND oi.state IN ('interested', 'reserved', 'accepted', 'active')
                  AND (oi.state = 'active' OR oi.available_until > ?)
                """,
                params,
            ).fetchall()
            for r in rows:
                did = str(r["dungeon_id"])
                pid = str(r["persona_id"])
                st = str(r["state"])
                opp_st = str(r["opp_state"])
                # Active runs & cancelling runs are BUSY (not available/queued for a new party!)
                if st == "active" or opp_st in ("active", "cancelling"):
                    out["busy_personas"].add(pid)
                    continue
                out["queued_by_dungeon"].setdefault(did, {})[pid] = {
                    "character_guid": int(r["character_guid"]),
                    "role": str(r["role"]).strip().lower(),
                    "state": st,
                }
                if st in ("reserved", "accepted"):
                    out["busy_personas"].add(pid)

        if "future_dungeon_intents" in tables:
            for r in conn.execute(
                """
                SELECT dungeon_id, persona_id, realm_id, character_guid, character_slug, faction,
                       roles_json, earliest_start_ts, latest_end_ts, expires_at
                FROM future_dungeon_intents
                WHERE is_shadow = 1 AND state = 'open' AND expires_at > ? AND latest_end_ts > ?
                ORDER BY created_at ASC, id ASC
                """,
                (int(now_ts), int(now_ts)),
            ).fetchall():
                # P2-9: Apply PersonaRegistry identity/core filter so quarantined/core personas never leak into shadow report!
                if out["has_registry"]:
                    rec = out["enrollments"].get((int(r["realm_id"]), int(r["character_guid"])))
                    if (
                        rec is None
                        or rec["persona_id"] != str(r["persona_id"])
                        or rec["state"] != "active"
                        or not rec["char_active"]
                        or not rec["enrolled_coop"]
                    ):
                        continue
                try:
                    roles_parsed = json.loads(str(r["roles_json"] or "[]"))
                except (ValueError, TypeError) as exc:
                    logger.warning("Skipping malformed roles_json in shadow intent read: %s", exc)
                    continue
                did = str(r["dungeon_id"])
                out["future_intents_by_dungeon"].setdefault(did, []).append({
                    "persona_id": str(r["persona_id"]),
                    "slug": str(r["character_slug"]),
                    "faction": str(r["faction"]),
                    "roles": roles_parsed,
                    "earliest_start_ts": int(r["earliest_start_ts"]),
                    "latest_end_ts": int(r["latest_end_ts"]),
                    "expires_at": int(r["expires_at"]),
                })

        if "future_commitments" in tables:
            for r in conn.execute(
                """
                SELECT id, dungeon_id, faction, window_start, window_end, state, accept_until, is_shadow
                FROM future_commitments
                WHERE is_shadow = 1 AND state IN ('proposed', 'scheduled', 'activating', 'activated') AND window_end > ?
                ORDER BY window_start ASC, id ASC
                """,
                (int(now_ts),),
            ).fetchall():
                did = str(r["dungeon_id"])
                out["future_commitments_by_dungeon"].setdefault(did, []).append({
                    "commitment_id": int(r["id"]),
                    "faction": str(r["faction"]),
                    "window_start": int(r["window_start"]),
                    "window_end": int(r["window_end"]),
                    "state": str(r["state"]),
                    "accept_until": int(r["accept_until"]),
                    "is_shadow": bool(int(r["is_shadow"])),
                })

            # P1-8: Mark personas with imminent LIVE scheduled/activating commitments as busy!
            if "future_commitment_members" in tables:
                lead_cutoff = int(now_ts) + 900
                for mr in conn.execute(
                    """
                    SELECT DISTINCT m.persona_id
                    FROM future_commitment_members m
                    JOIN future_commitments c ON c.id = m.commitment_id
                    WHERE c.is_shadow = 0
                      AND c.state IN ('scheduled', 'activating')
                      AND m.state IN ('accepted', 'activated')
                      AND c.window_start <= ?
                      AND c.window_end > ?
                    """,
                    (lead_cutoff, int(now_ts)),
                ).fetchall():
                    out["busy_personas"].add(str(mr["persona_id"]))
    except sqlite3.Error as exc:
        out["ok"] = False
        out["errors"].append(f"{type(exc).__name__}: {exc}")
        logger.warning("Read-only shadow DB context lookup failed for %s: %s", db_path, exc)
    finally:
        if conn is not None:
            conn.close()
    return out


def validate_candidate_for_dungeon(
    comp: dict[str, Any],
    spec: DungeonSpec,
    *,
    required_role: str | None = None,
    allow_pilot_shadow: bool = True,
    raid_stage: int = 0,
    now_ts: int,
    tz=None,
    max_telemetry_age_sec: int = 300,
    world_ctx: dict[str, Any] | None = None,
    char_profile: dict[str, Any] | None = None,
    enrolled_coop: bool | None = None,
    persona_state: str = "active",
    char_active: bool = True,
    paused: bool = False,
    queued_here_native: bool = False,
    queued_elsewhere: bool = False,
) -> dict[str, Any]:
    """Single source of truth for Stage 1 (hard eligibility) and Stage 2 (current availability) (Issue #14).

    Fail-closed: unknown faction, schedule, lockouts, live spec/role, combat, group, queue, alive,
    equipment, native LFG eligibility, or stale telemetry NEVER passes!
    """
    shift_group = str(comp.get("shift_group") or "").strip().lower()
    # Hard protected-core override (P2: never auto-admit core-wonky / core-logan even if DB row stale!)
    is_protected_core = shift_group in PROTECTED_OWNER_GROUPS
    eff_enrolled = (not is_protected_core) if enrolled_coop is None else (bool(enrolled_coop) and not is_protected_core)

    def _fail(stage: str, reason: str, *, unknown: bool = False, **extra: Any) -> dict[str, Any]:
        return {
            "eligible": stage != "eligible",
            "available": False,
            "reason": reason,
            "unknown_telemetry": unknown,
            "is_protected_core": is_protected_core,
            **extra,
        }

    if not eff_enrolled:
        return _fail("eligible", "protected_core_excluded")
    if persona_state != "active":
        return _fail("eligible", "quarantined_persona")
    if not char_active:
        return _fail("eligible", "inactive_persona_character")
    if not comp.get("online"):
        return _fail("eligible", "offline")

    # Telemetry freshness check (fail-closed if missing or stale)
    telem_fresh_explicit = comp.get("telemetry_fresh")
    if telem_fresh_explicit is not None:
        telem_fresh = bool(telem_fresh_explicit)
    else:
        obs_ts = _parse_observed_timestamp(world_ctx or {}, comp)
        if obs_ts is None:
            telem_fresh = False
        else:
            age = int(now_ts) - obs_ts
            telem_fresh = (0 <= age <= int(max_telemetry_age_sec))
        if world_ctx and world_ctx.get("live_pos_fresh") is False and comp.get("telemetry_fresh") is None:
            telem_fresh = False
    if not telem_fresh:
        return _fail("eligible", "stale_or_missing_telemetry", unknown=True)

    lvl = int(comp.get("level") or 0)
    band = classify_level_band(lvl, spec)

    faction = comp.get("faction")
    if not faction and comp.get("race_id") is not None:
        faction = faction_from_race(comp.get("race_id"))

    # Prefer difficulty-aware locked_instances when present, else locked_maps
    raw_locks = comp.get("locked_instances") if comp.get("locked_instances") is not None else comp.get("locked_maps")
    locked_maps = list(raw_locks) if raw_locks is not None else None

    live_telem = comp.get("live_telemetry") if isinstance(comp.get("live_telemetry"), dict) else None
    role_eval = evaluate_live_role_capability(
        char_profile, level=lvl, live_telemetry=live_telem, allow_profile_fallback=False
    )
    if not role_eval["known"]:
        reason = role_eval["reasons"][0] if role_eval["reasons"] else "missing_live_role_telemetry"
        return _fail("eligible", reason, unknown=True, band=band, role_eval=role_eval)

    native_lfg_elig = (live_telem or {}).get("native_lfg_eligible")
    native_lfg_lock = str((live_telem or {}).get("native_lfg_lock_reason") or "")
    ll_state = comp.get("level_lead_state") if isinstance(comp.get("level_lead_state"), dict) else {}
    level_lead_blocked = bool(ll_state.get("at_level_cap")) or (comp.get("xp_eligible") is False)

    # Stage 1: Hard legal/capability/policy eligibility (independent of transient combat/group/dead/gear/shift)
    elig_ok, elig_reason = check_character_eligibility(
        level=lvl,
        faction=faction,
        viable_roles=role_eval["viable_roles"],
        dungeon=spec,
        available_minutes=9999,  # real schedule window validated in Stage 2!
        locked_maps=locked_maps,
        raid_stage=raid_stage,
        allow_pilot_shadow=allow_pilot_shadow,
        online=True,
        telemetry_fresh=True,
        enrolled_coop=True,
        require_known_lockouts=True,
        require_known_live_state=False,  # transient live-state checks run in Stage 2 so eligible stays accurate!
        in_combat=False,
        in_group=False,
        in_lfg_queue=False,
        is_dead=False,
        paused=False,
        native_lfg_eligible=True if native_lfg_elig is None else native_lfg_elig,
        native_lfg_lock_reason=native_lfg_lock,
        equipment_known=True,
        level_lead_blocked=level_lead_blocked,
        required_role=required_role,
    )
    if not elig_ok:
        is_unk = elig_reason in ("unknown_faction", "unknown_lockout_telemetry")
        return _fail("eligible", elig_reason, unknown=is_unk, band=band, role_eval=role_eval)

    # Stage 2: Actual live state, equipment/prep, schedule window, combat/group/queue/native-LFG
    equipped = (live_telem or {}).get("equipped") if isinstance((live_telem or {}).get("equipped"), dict) else None
    is_dead_raw = (live_telem or {}).get("is_dead")
    in_combat_raw = (live_telem or {}).get("in_combat")
    in_group_raw = comp.get("in_group")
    in_lfg_raw = (live_telem or {}).get("in_lfg_queue")

    travel_prep = estimate_travel_and_prep_minutes(
        spec,
        current_map_id=comp.get("map_id"),
        current_zone_id=comp.get("zone_id"),
        equipped=equipped,
        is_dead=is_dead_raw,
    )

    if comp.get("available_minutes") is not None:
        avail_mins: int | None = int(comp["available_minutes"])
        sched_reason = "explicit_available_minutes"
    else:
        avail_mins, sched_reason = compute_schedule_remaining_minutes(
            comp.get("schedule"), now_ts=now_ts, tz=tz
        )

    # If in_lfg_queue is None -> fail closed; if queued HERE natively, not blocked by queued_elsewhere
    eff_in_lfg: bool | None = None if in_lfg_raw is None else bool(queued_elsewhere)

    avail_ok, avail_reason = check_character_eligibility(
        level=lvl,
        faction=faction,
        viable_roles=role_eval["viable_roles"],
        dungeon=spec,
        available_minutes=avail_mins,
        locked_maps=locked_maps,
        raid_stage=raid_stage,
        allow_pilot_shadow=allow_pilot_shadow,
        online=True,
        telemetry_fresh=True,
        enrolled_coop=True,
        require_known_lockouts=True,
        require_known_live_state=True,
        in_combat=in_combat_raw,
        in_group=in_group_raw,
        in_lfg_queue=eff_in_lfg,
        is_dead=is_dead_raw,
        paused=paused,
        native_lfg_eligible=native_lfg_elig,
        native_lfg_lock_reason=native_lfg_lock,
        equipment_known=(equipped is not None),
        level_lead_blocked=level_lead_blocked,
        blocking_prep_needs=travel_prep["blocking_prep_needs"],
        travel_minutes_override=travel_prep["travel_minutes"],
        prep_minutes=travel_prep["prep_minutes"],
        required_role=required_role,
    )
    is_unk_stage2 = avail_reason.startswith("unknown_")
    return {
        "eligible": True,
        "available": avail_ok,
        "reason": "available" if avail_ok else avail_reason,
        "unknown_telemetry": is_unk_stage2,
        "is_protected_core": False,
        "faction": faction,
        "band": band,
        "role_eval": role_eval,
        "available_minutes": avail_mins,
        "schedule_status": sched_reason,
        "travel_prep": travel_prep,
        "near_level_cap": bool(ll_state.get("near_level_cap")),
    }


def _match_party_roles(
    candidates: list[dict[str, Any]],
    required_roles: dict[str, int],
) -> dict[str, Any]:
    """Primary-first augmenting-path bipartite role assignment over distinct personas & characters.

    Guarantees (Issue #14):
    - Never assigns the same `persona_id` or `character_guid` to more than one slot.
    - Respects each candidate's `volunteered_roles` when present (a volunteer who signed up for DPS
      is NEVER silently reassigned to tank or healer!).
    - Attempts primary-role assignments first, then verified `flex_roles` for remaining slots.
    """
    unique_cands: list[dict[str, Any]] = []
    seen_p: set[str] = set()
    seen_g: set[int] = set()
    for c in candidates:
        pid = str(c["persona_id"])
        guid = int(c["character_guid"])
        if pid in seen_p or guid in seen_g:
            continue
        seen_p.add(pid)
        seen_g.add(guid)
        unique_cands.append(c)

    slots: list[str] = []
    for role_name in ("tank", "healer", "damage"):
        slots.extend([role_name] * max(0, int(required_roles.get(role_name, 0))))

    slot_owner: list[int | None] = [None] * len(slots)
    cand_slot: dict[int, int] = {}

    def _can_fill(cand_idx: int, role_name: str, allow_flex: bool) -> bool:
        c = unique_cands[cand_idx]
        vol_roles = c.get("volunteered_roles")
        if vol_roles is not None and role_name not in vol_roles:
            return False
        if c.get("primary_role") == role_name:
            return True
        return allow_flex and (role_name in (c.get("flex_roles") or ()))

    def _try_assign(slot_idx: int, allow_flex: bool, visited: set[int]) -> bool:
        role_needed = slots[slot_idx]
        cand_order = sorted(
            range(len(unique_cands)),
            key=lambda i: (
                0 if unique_cands[i].get("primary_role") == role_needed else 1,
                unique_cands[i]["slug"],
            ),
        )
        for ci in cand_order:
            if ci in visited or not _can_fill(ci, role_needed, allow_flex):
                continue
            visited.add(ci)
            prev_slot = cand_slot.get(ci)
            if prev_slot is None or _try_assign(prev_slot, allow_flex, visited):
                slot_owner[slot_idx] = ci
                cand_slot[ci] = slot_idx
                return True
        return False

    for s_idx in range(len(slots)):
        if slot_owner[s_idx] is None:
            _try_assign(s_idx, allow_flex=False, visited=set())

    for s_idx in range(len(slots)):
        if slot_owner[s_idx] is None:
            _try_assign(s_idx, allow_flex=True, visited=set())

    party: list[dict[str, Any]] = []
    matched_counts: dict[str, int] = {"tank": 0, "healer": 0, "damage": 0}
    used_flex: list[dict[str, Any]] = []
    for s_idx, role_name in enumerate(slots):
        ci = slot_owner[s_idx]
        if ci is None:
            continue
        c = unique_cands[ci]
        is_flex = c.get("primary_role") != role_name
        matched_counts[role_name] = matched_counts.get(role_name, 0) + 1
        entry = {
            "persona_id": c["persona_id"],
            "character_guid": c["character_guid"],
            "slug": c["slug"],
            "name": c["name"],
            "level": c["level"],
            "faction": c.get("faction"),
            "assigned_role": role_name,
            "primary_role": c.get("primary_role"),
            "used_flex": is_flex,
            "lfg_role_mask": c.get("lfg_role_mask", 0),
            "available_minutes": c.get("available_minutes"),
            "travel_minutes": c.get("travel_minutes", 0),
            "prep_minutes": c.get("prep_minutes", 0),
        }
        party.append(entry)
        if is_flex:
            used_flex.append({"slug": c["slug"], "primary_role": c.get("primary_role"), "assigned_role": role_name})

    shortfalls = {
        r: max(0, int(required_roles.get(r, 0)) - matched_counts.get(r, 0))
        for r in ("tank", "healer", "damage")
    }
    return {
        "complete": all(v == 0 for v in shortfalls.values()) and len(slots) > 0,
        "party": party,
        "matched_counts": matched_counts,
        "shortfalls": shortfalls,
        "used_flex_roles": used_flex,
        "party_shift_window_ok": True,
    }


def _match_single_faction_party(
    candidates: list[dict[str, Any]],
    spec: DungeonSpec,
) -> dict[str, Any]:
    """Match a single-faction party AND enforce party-wide shift window (P1-3 + P2 mixed-faction fix!).

    A dungeon party cannot mix Alliance and Horde, and cannot start until its slowest traveler arrives:
      `min(avail_i) >= spec.estimated_minutes + max(travel_i + prep_i) + 10`.
    """
    roles_req = dict(spec.roles)
    factions = ("alliance", "horde") if spec.faction == "both" else (spec.faction,)

    best: dict[str, Any] | None = None
    for fac in factions:
        pool = [c for c in candidates if str(c.get("faction") or "").lower() == fac]
        cur_pool = list(pool)
        match = _match_party_roles(cur_pool, roles_req)

        # Party-level shift-window check: if slowest traveler leaves someone with too little time, drop bottleneck & retry
        while match["complete"] and match["party"]:
            max_overhead = max(
                int(m.get("travel_minutes") or 0) + int(m.get("prep_minutes") or 0)
                for m in match["party"]
            )
            needed_total = int(spec.estimated_minutes) + max_overhead + 10
            bottlenecks = [
                m for m in match["party"]
                if m.get("available_minutes") is None or int(m["available_minutes"]) < needed_total
            ]
            if not bottlenecks:
                break
            # Try excluding either the tightest-shift member or the slowest traveler and pick whichever yields a complete party
            tightest_pid = min(bottlenecks, key=lambda m: int(m.get("available_minutes") or 0))["persona_id"]
            slowest_pid = max(
                match["party"],
                key=lambda m: int(m.get("travel_minutes") or 0) + int(m.get("prep_minutes") or 0),
            )["persona_id"]
            alt_a = _match_party_roles([c for c in cur_pool if c["persona_id"] != tightest_pid], roles_req)
            alt_b = _match_party_roles([c for c in cur_pool if c["persona_id"] != slowest_pid], roles_req)
            if alt_b["complete"] and not alt_a["complete"]:
                cur_pool = [c for c in cur_pool if c["persona_id"] != slowest_pid]
                match = alt_b
            else:
                cur_pool = [c for c in cur_pool if c["persona_id"] != tightest_pid]
                match = alt_a
            if not match["complete"]:
                match["party_shift_window_ok"] = False
                break

        if best is None or (match["complete"] and not best["complete"]) or (
            match["complete"] == best["complete"] and len(match["party"]) > len(best["party"])
        ):
            best = match

    if best is None:
        return _match_party_roles([], roles_req)
    return best


def _parse_observed_timestamp(world_ctx: dict[str, Any], comp: dict[str, Any]) -> int | None:
    """Resolve telemetry observation unix timestamp (None if unknown -> fail closed)."""
    if comp.get("telemetry_observed_at") is not None:
        try:
            return int(comp["telemetry_observed_at"])
        except (TypeError, ValueError):
            return None
    if world_ctx.get("telemetry_observed_at") is not None:
        try:
            return int(world_ctx["telemetry_observed_at"])
        except (TypeError, ValueError):
            return None
    obs_str = str(comp.get("observed_at") or world_ctx.get("observed_at") or "").strip()
    if obs_str:
        try:
            dt = datetime.strptime(obs_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            return None
    return None


def _count_by_primary_role(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"tank": 0, "healer": 0, "damage": 0}
    for it in items:
        pr = str(it.get("primary_role") or "")
        if pr in counts:
            counts[pr] += 1
    return counts


def _extract_volunteer_choice(
    slug: str,
    persona_id: str,
    comp: dict[str, Any],
    spec: DungeonSpec,
    *,
    volunteer_choices: dict[str, Any] | None,
    queued_map: dict[str, dict[str, Any]],
) -> tuple[bool, set[str] | None]:
    """Return `(is_willing, volunteered_roles_or_None)` from EXPLICIT volunteer choices ONLY.

    NEVER fabricates volunteer choice from the eligible roster!
    When a volunteer chose specific role(s) (e.g. via opportunity_interests.role or a dict mapping
    dungeon_id -> [roles]), returns that exact set so the matcher never reassigns them to an unwanted role.
    """
    chosen_roles: set[str] = set()
    is_willing = False

    if persona_id in queued_map:
        is_willing = True
        qr = str(queued_map[persona_id].get("role") or "").strip().lower()
        if qr in ("tank", "healer", "damage"):
            chosen_roles.add(qr)

    accepted_keys = {spec.dungeon_id, str(spec.lfg_dungeon_id), spec.lfg_dungeon_id}

    def _consume_spec(val: Any) -> None:
        nonlocal is_willing
        if isinstance(val, (list, tuple, set, frozenset)):
            if any(x in accepted_keys for x in val):
                is_willing = True
        elif isinstance(val, dict):
            for k in (spec.dungeon_id, spec.lfg_dungeon_id, str(spec.lfg_dungeon_id)):
                if k in val and val[k]:
                    is_willing = True
                    v = val[k]
                    if isinstance(v, str) and v.strip().lower() in ("tank", "healer", "damage"):
                        chosen_roles.add(v.strip().lower())
                    elif isinstance(v, (list, tuple, set, frozenset)):
                        for r in v:
                            rl = str(r).strip().lower()
                            if rl in ("tank", "healer", "damage"):
                                chosen_roles.add(rl)

    _consume_spec(comp.get("willing_dungeons"))
    if isinstance(comp.get("willing_roles"), (list, tuple, set, frozenset)) and is_willing:
        for r in comp["willing_roles"]:
            rl = str(r).strip().lower()
            if rl in ("tank", "healer", "damage"):
                chosen_roles.add(rl)

    if isinstance(volunteer_choices, dict):
        for key in (slug, persona_id):
            if key in volunteer_choices:
                _consume_spec(volunteer_choices[key])

    return is_willing, (chosen_roles if chosen_roles else None)


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
    tz=None,
    max_telemetry_age_sec: int = 300,
    volunteer_choices: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure shadow matchmaking pass over current live roster -> runtime/phase2_shadow.json ONLY.

    Guarantees (Issues #7, #14 & #15 + Cards II-02/II-03/II-04):
    - STRICTLY READ-ONLY on db_path: NEVER mutates PersonaRegistry, opportunities, MySQL,
      reservations, schedules, memory, or active_plans.json.
    - Uses ACTUAL faction, ACTUAL schedule remaining minutes, ACTUAL instance lockouts,
      ACTUAL live role capability (spec/talents/spells/equipment/native role mask),
      ACTUAL combat/group/queue/alive/paused/staleness/native-LFG/level-lead/travel-prep status.
    - Fail-closed: unknown telemetry or unreadable brain.db NEVER claims feasible.
    - Protects core-wonky and core-logan unconditionally (`enrolled_coop = False`).
    - Explicitly distinguishes `eligible`, `available`, `willing`, `queued`, `willing_and_available`,
      and `matched` as independent non-conflated sets (actual volunteer choice strictly separated!).
    - Respects volunteer role choice, single-faction parties, party-wide shift window, and distinct personas.
    """
    ts = int(now_ts if now_ts is not None else time.time())
    db_ctx = _read_shadow_db_context_readonly(Path(db_path), now_ts=ts)
    db_ok = bool(db_ctx["ok"])
    has_registry = bool(db_ctx["has_registry"])
    db_errors = list(db_ctx["errors"])
    existing_enrollments = db_ctx["enrollments"]
    by_persona_session = db_ctx["by_persona_session"]
    open_dual_session_personas = db_ctx["open_dual_session_personas"]
    paused_slugs = db_ctx["paused_slugs"]
    busy_personas = db_ctx["busy_personas"]
    queued_by_dungeon = db_ctx["queued_by_dungeon"]
    future_intents_by_dungeon = db_ctx["future_intents_by_dungeon"]
    future_commitments_by_dungeon = db_ctx["future_commitments_by_dungeon"]
    companions = world_ctx.get("companions") or {}

    known = set(profiles())
    v = vitals if isinstance(vitals, dict) else {}
    raw_mem_gib = v.get("mem_avail_gib")
    if raw_mem_gib is None:
        mem_avail_mib: float | None = None
    else:
        valid_gib = _valid_nonneg_float(raw_mem_gib)
        mem_avail_mib = (valid_gib * 1024.0) if valid_gib is not None else math.nan

    raw_tick = v.get("tick_p99")
    if raw_tick is None:
        tick_p99: float | None = None
    else:
        valid_tick = _valid_nonneg_float(raw_tick)
        tick_p99 = valid_tick if valid_tick is not None else math.nan

    # Issue #12 & #15: Shadow reads persisted headroom state READ-ONLY (never writes headroom_state.json)
    # AND uses ACTUAL live active/pending group counts from brain.db + vitals (never hardcoding 0!).
    prev_paused = False
    hr_path = runtime_dir / "headroom_state.json"
    try:
        if hr_path.is_file():
            saved = json.loads(hr_path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or not isinstance(saved.get("paused"), bool):
                raise ValueError("invalid headroom_state.json")
            prev_paused = saved["paused"]
    except Exception as exc:
        logger.warning("Corrupt headroom state at %s during shadow read (%s); assuming paused", hr_path, exc)
        prev_paused = True

    def _safe_int_vital(key: str, default: int) -> int | None:
        raw = v.get(key)
        if raw is None:
            return default
        return _valid_nonneg_int(raw)

    v_act = _safe_int_vital("active_managed_groups", 0)
    v_pend = _safe_int_vital("pending_proposals", 0)
    actual_active_groups: int | None = None if v_act is None else max(int(db_ctx.get("active_live_groups") or 0), v_act)
    actual_pending_proposals: int | None = None if v_pend is None else max(int(db_ctx.get("pending_live_proposals") or 0), v_pend)

    max_grp = _safe_int_vital("max_managed_groups", DEFAULT_MAX_GROUPS)
    max_inst = _safe_int_vital("max_total_instances", DEFAULT_MAX_TOTAL_INSTANCES)
    max_pop = _safe_int_vital("max_online_population", DEFAULT_MAX_ONLINE_POPULATION)
    hum_inst = _safe_int_vital("human_instances", 0)

    vitals_ts = v.get("observed_at")
    if isinstance(vitals_ts, str) and vitals_ts.strip():
        try:
            vitals_ts = int(datetime.strptime(vitals_ts.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            vitals_ts = -1  # malformed timestamp -> fail closed
    headroom = check_resource_headroom(
        mem_avail_mib=mem_avail_mib,
        tick_p99_ms=tick_p99,
        active_managed_groups=actual_active_groups,
        pending_proposals=actual_pending_proposals if actual_pending_proposals is not None else -1,
        max_managed_groups=max_grp if (max_grp is not None and max_grp > 0) else DEFAULT_MAX_GROUPS,
        prev_paused=prev_paused,
        active_instances=v.get("active_instances"),
        human_instances=hum_inst if hum_inst is not None else -1,
        max_total_instances=max_inst if (max_inst is not None and max_inst > 0) else DEFAULT_MAX_TOTAL_INSTANCES,
        swap_io_pages_per_sec=v.get("swap_io_pages_per_sec"),
        cpu_psi_some_avg10=v.get("cpu_psi_some_avg10"),
        cpu_throttled_pct=v.get("cpu_throttled_pct"),
        online_population=v.get("online_population"),
        max_online_population=max_pop if (max_pop is not None and max_pop > 0) else DEFAULT_MAX_ONLINE_POPULATION,
        telemetry_ts=vitals_ts,
        now_ts=ts,
        max_telemetry_age_sec=max_telemetry_age_sec,
    )

    shadow_dungeons: list[dict[str, Any]] = []
    for spec in DUNGEON_CATALOG:
        if not spec.supported:
            continue

        eligible_list: list[dict[str, Any]] = []
        available_list: list[dict[str, Any]] = []
        willing_list: list[dict[str, Any]] = []
        queued_list: list[dict[str, Any]] = []
        willing_available_list: list[dict[str, Any]] = []
        pool_by_role: dict[str, list[dict[str, Any]]] = {"tank": [], "healer": [], "damage": []}

        excluded_core = 0
        missing_identity = 0
        unknown_telemetry = 0
        excluded_reasons: dict[str, int] = {}
        band_breakdown = {"legal_entry": 0, "lfg_queue_eligible": 0, "recommended": 0}
        seen_guids: set[int] = set()
        seen_personas: set[str] = set()
        queued_map = queued_by_dungeon.get(spec.dungeon_id, {})

        def _bump(reason_key: str) -> None:
            k = reason_key.split(":", 1)[0]
            excluded_reasons[k] = excluded_reasons.get(k, 0) + 1

        if not db_ok:
            _bump("brain_db_unreadable")

        for slug, comp in sorted(companions.items()):
            guid = int(comp["guid"])
            raw_realm = comp.get("realm_id")
            if raw_realm is not None:
                try:
                    realm_id = int(raw_realm)
                except (TypeError, ValueError):
                    _bump("invalid_realm_id")
                    continue
                if realm_id <= 0:
                    _bump("invalid_realm_id")
                    continue
            else:
                realm_id = 1
            char_key = (realm_id, guid)
            if slug not in known and char_key not in existing_enrollments:
                _bump("unknown_profile")
                continue
            if not comp.get("online"):
                _bump("offline")
                continue

            if char_key in seen_guids:
                _bump("duplicate_character")
                continue
            seen_guids.add(char_key)

            shift_group = str(comp.get("shift_group") or "").strip().lower()
            if char_key in existing_enrollments:
                rec = existing_enrollments[char_key]
                persona_id = rec["persona_id"]
                enrolled_coop = rec["enrolled_coop"]
                core_protected = rec["core_protected"]
                persona_state = rec["state"]
                char_active = rec["char_active"]
                char_inc = int(rec["incarnation"])
                # P1-2: Enforce one active session per persona & exclude open dual-session conflicts!
                if persona_id in open_dual_session_personas:
                    _bump("persona_dual_session_conflict")
                    continue
                active_sess = by_persona_session.get(persona_id)
                if active_sess is not None and active_sess != (realm_id, guid, char_inc):
                    _bump("persona_session_on_other_character")
                    continue
            elif has_registry:
                # Issue #25: When a PersonaRegistry exists, unresolved mappings MUST exclude admission!
                missing_identity += 1
                _bump("unresolved_persona_mapping")
                continue
            else:
                missing_identity += 1
                persona_id = str(comp.get("persona_id") or persona_id_for_slug(slug))
                core_protected = is_protected_owner_group(shift_group)
                enrolled_coop = not core_protected
                persona_state = str(comp.get("persona_state") or "active").lower()
                char_active = True

            # Hard protected-core override (honors sticky core_protected latch even after schedule changes!)
            if (core_protected and not enrolled_coop) or not enrolled_coop or is_protected_owner_group(shift_group):
                excluded_core += 1
                _bump("protected_core_excluded")
                continue
            if persona_state != "active":
                _bump("quarantined_persona")
                continue
            if not char_active:
                _bump("inactive_persona_character")
                continue

            # Single-active-character per persona across all stages (no duplicate persona!)
            if persona_id in seen_personas:
                _bump("duplicate_persona")
                continue

            live_telem = comp.get("live_telemetry") if isinstance(comp.get("live_telemetry"), dict) else None
            in_lfg_raw = (live_telem or {}).get("in_lfg_queue")
            in_lfg_dungeons = {int(x) for x in ((live_telem or {}).get("in_lfg_queue_dungeons") or [])}
            queued_here_native = (in_lfg_raw is True) and (spec.lfg_dungeon_id in in_lfg_dungeons)
            queued_here_opp = persona_id in queued_map
            queued_elsewhere = (
                ((in_lfg_raw is True) and not queued_here_native)
                or (persona_id in busy_personas and not queued_here_opp)
            )
            is_paused = (slug in paused_slugs) or bool(comp.get("paused"))
            prof = profile(slug) if slug in known else None

            ev = validate_candidate_for_dungeon(
                comp,
                spec,
                allow_pilot_shadow=True,
                raid_stage=raid_stage,
                now_ts=ts,
                tz=tz,
                max_telemetry_age_sec=max_telemetry_age_sec,
                world_ctx=world_ctx,
                char_profile=prof,
                enrolled_coop=enrolled_coop,
                persona_state=persona_state,
                char_active=char_active,
                paused=is_paused,
                queued_here_native=queued_here_native,
                queued_elsewhere=queued_elsewhere,
            )

            if not ev["eligible"]:
                if ev["unknown_telemetry"]:
                    unknown_telemetry += 1
                _bump(ev["reason"])
                continue

            seen_personas.add(persona_id)
            band = ev["band"]
            if band["legal_entry"]:
                band_breakdown["legal_entry"] += 1
            if band["lfg_queue_eligible"]:
                band_breakdown["lfg_queue_eligible"] += 1
            if band["in_recommended_band"]:
                band_breakdown["recommended"] += 1

            role_eval = ev["role_eval"]
            travel_prep = ev["travel_prep"]

            is_willing, volunteered_roles = _extract_volunteer_choice(
                slug,
                persona_id,
                comp,
                spec,
                volunteer_choices=volunteer_choices,
                queued_map=queued_map,
            )
            if queued_here_native:
                is_willing = True

            # Validate volunteer's chosen role(s) against verified live capability (P1-1!)
            vol_role_mismatch = False
            if is_willing and volunteered_roles is not None:
                valid_vol_roles = volunteered_roles & set(role_eval["viable_roles"])
                if not valid_vol_roles:
                    vol_role_mismatch = True
                    _bump("volunteered_role_not_capable")
                else:
                    volunteered_roles = valid_vol_roles

            cand_rec = {
                "persona_id": persona_id,
                "character_guid": guid,
                "slug": slug,
                "name": comp["name"],
                "level": int(comp["level"]),
                "faction": ev["faction"],
                "class_name": role_eval["class_name"],
                "role": role_eval["primary_role"],
                "primary_role": role_eval["primary_role"],
                "flex_roles": list(role_eval["flex_roles"]),
                "viable_roles": list(role_eval["viable_roles"]),
                "volunteered_roles": sorted(volunteered_roles) if volunteered_roles is not None else None,
                "lfg_role_mask": role_eval["lfg_role_mask"],
                "band_status": band["band_status"],
                "in_recommended_band": band["in_recommended_band"],
                "available_minutes": ev["available_minutes"],
                "schedule_status": ev["schedule_status"],
                "travel_minutes": travel_prep["travel_minutes"],
                "prep_minutes": travel_prep["prep_minutes"],
                "prep_needs": travel_prep["prep_needs"],
                "near_level_cap": ev.get("near_level_cap", False),
            }

            # Stage 1: Eligible (hard legal/capability/policy gates passed)
            eligible_list.append(cand_rec)

            # Stage 3 & 4 (UN-NESTED per P1-2!): willing & queued recorded independently of transient combat/shift!
            if is_willing and not vol_role_mismatch:
                willing_list.append(cand_rec)
            elif not is_willing:
                _bump("not_willing")

            if queued_here_opp or queued_here_native:
                queued_list.append({
                    **cand_rec,
                    "queue_source": "native_lfg" if queued_here_native else "opportunity_interest",
                    "queue_state": "queued" if queued_here_native else queued_map[persona_id]["state"],
                })

            # Stage 2: Available right now (fails closed if brain.db unreadable OR any Stage 2 check fails!)
            if not db_ok:
                continue
            if not ev["available"]:
                if ev["unknown_telemetry"]:
                    unknown_telemetry += 1
                _bump(ev["reason"])
                continue

            available_list.append(cand_rec)
            if cand_rec["primary_role"] in pool_by_role:
                pool_by_role[cand_rec["primary_role"]].append(cand_rec)

            if is_willing and not vol_role_mismatch:
                willing_available_list.append(cand_rec)

        # Stage 5: Single-faction distinct-persona role matching with party-level shift-window check
        if db_ok:
            volunteer_match = _match_single_faction_party(willing_available_list, spec)
            roster_match = _match_single_faction_party(available_list, spec)
        else:
            volunteer_match = _match_party_roles([], dict(spec.roles))
            roster_match = _match_party_roles([], dict(spec.roles))

        if not volunteer_match.get("party_shift_window_ok", True):
            _bump("party_shift_window_insufficient")

        feasible_now = bool(db_ok and volunteer_match["complete"])
        roster_feasible_now = bool(db_ok and roster_match["complete"])

        shadow_dungeons.append({
            "dungeon_id": spec.dungeon_id,
            "lfg_dungeon_id": spec.lfg_dungeon_id,
            "name": spec.name,
            "map_id": spec.map_id,
            "level_band": f"{spec.min_level}-{spec.max_level}",
            "legal_level_band": f"{spec.legal_min_level}-{spec.legal_max_level}",
            "lfg_level_band": f"{spec.min_level}-{spec.max_level}",
            "recommended_level_band": f"{spec.recommended_min}-{spec.recommended_max}",
            "band_breakdown": band_breakdown,
            "pilot_enabled_live": spec.dungeon_id in enabled_dungeon_ids(),
            "synthetic_feasibility": True,
            "eligible_counts": _count_by_primary_role(eligible_list),
            "available_counts": _count_by_primary_role(available_list),
            "willing_counts": _count_by_primary_role(willing_list),
            "queued_counts": _count_by_primary_role(queued_list),
            "matched_counts": volunteer_match["matched_counts"],
            "shortfalls": volunteer_match["shortfalls"],
            "roster_shortfalls": roster_match["shortfalls"],
            "feasible_now": feasible_now,
            "volunteer_feasible_now": feasible_now,
            "roster_feasible_now": roster_feasible_now,
            "used_flex_roles": volunteer_match["used_flex_roles"],
            "roster_used_flex_roles": roster_match["used_flex_roles"],
            "stages": {
                "eligible": {
                    "count": len(eligible_list),
                    "by_primary_role": _count_by_primary_role(eligible_list),
                    "personas": [c["persona_id"] for c in eligible_list],
                    "members": eligible_list,
                },
                "available": {
                    "count": len(available_list),
                    "by_primary_role": _count_by_primary_role(available_list),
                    "personas": [c["persona_id"] for c in available_list],
                    "members": available_list,
                },
                "willing": {
                    "count": len(willing_list),
                    "by_primary_role": _count_by_primary_role(willing_list),
                    "personas": [c["persona_id"] for c in willing_list],
                    "members": willing_list,
                },
                "willing_and_available": {
                    "count": len(willing_available_list),
                    "by_primary_role": _count_by_primary_role(willing_available_list),
                    "personas": [c["persona_id"] for c in willing_available_list],
                },
                "queued": {
                    "count": len(queued_list),
                    "by_primary_role": _count_by_primary_role(queued_list),
                    "personas": [c["persona_id"] for c in queued_list],
                    "members": queued_list,
                },
                "matched": {
                    "count": len(volunteer_match["party"]),
                    "complete": volunteer_match["complete"],
                    "by_role": volunteer_match["matched_counts"],
                    "personas": [m["persona_id"] for m in volunteer_match["party"]],
                    "party": volunteer_match["party"],
                    "used_flex_roles": volunteer_match["used_flex_roles"],
                },
            },
            "excluded_protected_core": excluded_core,
            "excluded_reasons": excluded_reasons,
            "unknown_telemetry_count": unknown_telemetry,
            "missing_identity_fallback_count": missing_identity,
            "future_intents": future_intents_by_dungeon.get(spec.dungeon_id, []),
            "future_commitments": future_commitments_by_dungeon.get(spec.dungeon_id, []),
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
        "db_ok": db_ok,
        "db_errors": db_errors,
        "telemetry_errors": list(world_ctx.get("telemetry_errors") or []),
        "shared_pool_note": "Per-dungeon feasibility evaluates the shared roster independently; simultaneous runs cannot share personas.",
        "work_order_opportunities": len(adapt_work_orders_as_opportunities(world_ctx.get("work_orders") or [])),
        "shadow_dungeons": shadow_dungeons,
    }
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / "phase2_shadow.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return out
