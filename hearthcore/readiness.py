"""Phase II Specific Encounter & Roster Raid Readiness Read Model (Card II-08 / Issue #16).

Design guarantees (docs/PHASE_II_LIVING_GUILD.md Section 7, Issue #16):
- Separate per-raid definitions (ZG20, AQ20, MC40, BWL40, AQ40, Karazhan 10, Gruul 25,
  Naxxramas 10/25, Onyxia 10/25). Map IDs, difficulties, and portal entry levels match
  `acore_world.dungeon_access_template` (and are cross-checked at runtime by
  `fetch_raid_readiness_telemetry`); role splits, iLvl floors, and potion counts are documented
  baseline heuristics that the owner can override.
- Owner retains 100% authority over raid selection, human slots, roster pins/exclusions, tactics
  attestations, and raid start (`owner_initiated_only=True`, `auto_raid_enabled=False`, read-only).
- Every component reports `pass` | `fail` | `unknown` | `not_applicable` with authoritative evidence.
  Unknown gear, access/lockout, live spec/role, encounter coverage/tactics, personal supplies,
  stage, persona DB, or stale telemetry NEVER counts as ready (no false readiness from profiles!).
- Bank consumable arithmetic is STRICTLY modeled as POTENTIAL allocation (NOT a live reservation):
  - `feasible_now` == `feasible_obtained_only` (what the roster can run RIGHT NOW with personal bags).
  - `feasible_with_potential_bank` is reported separately (or `None` if bank pool is unknown).
  - Exact min-cost max-flow assignment finds the true minimum Guild Bank draw across distinct
    available personas, with zero double-claims, never wasting stock on unavailable/duplicate chars,
    and never letting bench members consume active-roster bank allocation.
"""
from __future__ import annotations

import heapq
import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import profile, profiles
from .dungeons import evaluate_live_role_capability
from .identity import PersonaRegistry, persona_id_for_slug

logger = logging.getLogger("hearthcore.readiness")

# Known WotLK 3.3.5a classes capable of decursing/dispelling or raid-specific encounter roles
DECURSE_DISPEL_CLASSES = frozenset({"mage", "druid", "priest", "paladin", "shaman"})
TRANQ_SHOT_SPELLS = frozenset({19801})
ALLOWED_HUMAN_ROLES = frozenset({"tank", "healer", "damage"})


@dataclass(frozen=True)
class RaidSpec:
    raid_id: str
    name: str
    map_id: int
    difficulty: int                   # 0 = Normal / Classic / TBC / WotLK 10m; 1 = WotLK 25m
    raid_size: int                    # 10, 20, 25, or 40
    raid_stage_min: int               # 0 = Classic (Lv60), 1 = TBC (Lv70), 2 = WotLK (Lv80)
    legal_min_level: int              # Baseline from acore_world.dungeon_access_template
    min_ready_level: int              # Combat-viable minimum level heuristic
    target_level: int
    min_avg_ilvl: float               # Baseline average equipped iLvl heuristic (over full gear slots)
    roles: tuple[tuple[str, int], ...]
    consumable_entry: int
    consumable_name: str
    min_consumables_per_raider: int
    required_items: tuple[int, ...] = ()
    required_quests: tuple[int, ...] = ()
    # Roster-level encounter coverage requirements: tuple of (capability_key, min_count)
    roster_encounter_requirements: tuple[tuple[str, int], ...] = ()
    # Per-character capability keys required on every raider (in addition to tactics_verified)
    character_encounter_requirements: tuple[str, ...] = ()

    def roles_dict(self) -> dict[str, int]:
        return {r: int(n) for r, n in self.roles}


# Map IDs, difficulties, and legal_min_level align with acore_world.dungeon_access_template;
# role splits, min_avg_ilvl, and consumable counts are baseline readiness heuristics.
RAID_CATALOG: tuple[RaidSpec, ...] = (
    # --- Stage 0: Classic (Lv 60) ---
    RaidSpec(
        raid_id="zg_20",
        name="Zul'Gurub (20)",
        map_id=309,
        difficulty=0,
        raid_size=20,
        raid_stage_min=0,
        legal_min_level=50,
        min_ready_level=58,
        target_level=60,
        min_avg_ilvl=52.0,
        roles=(("tank", 2), ("healer", 4), ("damage", 14)),
        consumable_entry=13446,
        consumable_name="Major Healing Potion",
        min_consumables_per_raider=5,
        roster_encounter_requirements=(("decurse_dispel", 2),),
    ),
    RaidSpec(
        raid_id="aq20_20",
        name="Ruins of Ahn'Qiraj (AQ20)",
        map_id=509,
        difficulty=0,
        raid_size=20,
        raid_stage_min=0,
        legal_min_level=50,
        min_ready_level=58,
        target_level=60,
        min_avg_ilvl=54.0,
        roles=(("tank", 2), ("healer", 4), ("damage", 14)),
        consumable_entry=13446,
        consumable_name="Major Healing Potion",
        min_consumables_per_raider=5,
    ),
    RaidSpec(
        raid_id="mc_40",
        name="Molten Core (40)",
        map_id=409,
        difficulty=0,
        raid_size=40,
        raid_stage_min=0,
        legal_min_level=50,
        min_ready_level=58,
        target_level=60,
        min_avg_ilvl=56.0,
        roles=(("tank", 4), ("healer", 10), ("damage", 26)),
        consumable_entry=13446,
        consumable_name="Major Healing Potion",
        min_consumables_per_raider=5,
        # Note: Tranquilizing Shot drops inside MC (Lucifron), so it is NOT a circular MC entry gate;
        # decurse/dispel coverage IS an entry requirement for Lucifron/Gehennas/Shazzrah.
        roster_encounter_requirements=(("decurse_dispel", 4),),
    ),
    RaidSpec(
        raid_id="bwl_40",
        name="Blackwing Lair (40)",
        map_id=469,
        difficulty=0,
        raid_size=40,
        raid_stage_min=0,
        legal_min_level=60,
        min_ready_level=60,
        target_level=60,
        min_avg_ilvl=64.0,
        roles=(("tank", 4), ("healer", 10), ("damage", 26)),
        consumable_entry=13446,
        consumable_name="Major Healing Potion",
        min_consumables_per_raider=8,
        required_items=(15138,),  # Onyxia Scale Cloak (Shadow Flame survival)
        roster_encounter_requirements=(("tranquilizing_shot", 2), ("decurse_dispel", 4)),
    ),
    RaidSpec(
        raid_id="aq40_40",
        name="Temple of Ahn'Qiraj (AQ40)",
        map_id=531,
        difficulty=0,
        raid_size=40,
        raid_stage_min=0,
        legal_min_level=50,
        min_ready_level=60,
        target_level=60,
        min_avg_ilvl=70.0,
        roles=(("tank", 4), ("healer", 10), ("damage", 26)),
        consumable_entry=13446,
        consumable_name="Major Healing Potion",
        min_consumables_per_raider=10,
        roster_encounter_requirements=(("nature_resistance_soakers", 4),),
    ),
    # --- Stage 1: TBC (Lv 70) ---
    RaidSpec(
        raid_id="karazhan_10",
        name="Karazhan (10)",
        map_id=532,
        difficulty=0,
        raid_size=10,
        raid_stage_min=1,
        legal_min_level=68,
        min_ready_level=70,
        target_level=70,
        min_avg_ilvl=100.0,
        roles=(("tank", 2), ("healer", 3), ("damage", 5)),
        consumable_entry=22829,
        consumable_name="Super Healing Potion",
        min_consumables_per_raider=5,
    ),
    RaidSpec(
        raid_id="gruul_25",
        name="Gruul's Lair (25)",
        map_id=565,
        difficulty=0,
        raid_size=25,
        raid_stage_min=1,
        legal_min_level=70,
        min_ready_level=70,
        target_level=70,
        min_avg_ilvl=112.0,
        roles=(("tank", 3), ("healer", 6), ("damage", 16)),
        consumable_entry=22829,
        consumable_name="Super Healing Potion",
        min_consumables_per_raider=5,
        roster_encounter_requirements=(("mage_tank_krosh", 1),),
    ),
    # --- Stage 2: WotLK (Lv 80) ---
    RaidSpec(
        raid_id="naxx_10",
        name="Naxxramas (10)",
        map_id=533,
        difficulty=0,
        raid_size=10,
        raid_stage_min=2,
        legal_min_level=80,
        min_ready_level=80,
        target_level=80,
        min_avg_ilvl=185.0,
        roles=(("tank", 2), ("healer", 3), ("damage", 5)),
        consumable_entry=33447,
        consumable_name="Runic Healing Potion",
        min_consumables_per_raider=5,
    ),
    RaidSpec(
        raid_id="naxx_25",
        name="Naxxramas (25)",
        map_id=533,
        difficulty=1,
        raid_size=25,
        raid_stage_min=2,
        legal_min_level=80,
        min_ready_level=80,
        target_level=80,
        min_avg_ilvl=195.0,
        roles=(("tank", 3), ("healer", 6), ("damage", 16)),
        consumable_entry=33447,
        consumable_name="Runic Healing Potion",
        min_consumables_per_raider=5,
    ),
    RaidSpec(
        raid_id="onyxia_10",
        name="Onyxia's Lair (10)",
        map_id=249,
        difficulty=0,
        raid_size=10,
        raid_stage_min=2,
        legal_min_level=80,
        min_ready_level=80,
        target_level=80,
        min_avg_ilvl=200.0,
        roles=(("tank", 2), ("healer", 3), ("damage", 5)),
        consumable_entry=33447,
        consumable_name="Runic Healing Potion",
        min_consumables_per_raider=5,
    ),
    RaidSpec(
        raid_id="onyxia_25",
        name="Onyxia's Lair (25)",
        map_id=249,
        difficulty=1,
        raid_size=25,
        raid_stage_min=2,
        legal_min_level=80,
        min_ready_level=80,
        target_level=80,
        min_avg_ilvl=213.0,
        roles=(("tank", 3), ("healer", 6), ("damage", 16)),
        consumable_entry=33447,
        consumable_name="Runic Healing Potion",
        min_consumables_per_raider=5,
    ),
)

RAID_BY_ID: dict[str, RaidSpec] = {r.raid_id: r for r in RAID_CATALOG}
DEFAULT_RAID_BY_STAGE: dict[int, str] = {
    0: "mc_40",
    1: "karazhan_10",
    2: "naxx_10",
}

STAGE_RAID_SPECS: dict[int, dict[str, Any]] = {
    st: {
        "raid_id": RAID_BY_ID[rid].raid_id,
        "tier_name": RAID_BY_ID[rid].name,
        "min_level": RAID_BY_ID[rid].min_ready_level,
        "target_level": RAID_BY_ID[rid].target_level,
        "raid_size": RAID_BY_ID[rid].raid_size,
        "roles": RAID_BY_ID[rid].roles_dict(),
        "consumable_entry": RAID_BY_ID[rid].consumable_entry,
        "min_consumables_per_raider": RAID_BY_ID[rid].min_consumables_per_raider,
    }
    for st, rid in DEFAULT_RAID_BY_STAGE.items()
}


def resolve_raid_spec(raid_id: str | None = None, *, raid_stage: int | None = None) -> RaidSpec:
    """Resolve an owner-selected raid_id (or verified stage default if raid_id is omitted).

    Fail-closed (P1-7): raises ValueError when BOTH raid_id and raid_stage are None or unknown!
    """
    if raid_id is not None and str(raid_id).strip():
        key = str(raid_id).strip().lower()
        if key not in RAID_BY_ID:
            raise ValueError(f"Unknown raid_id: {raid_id!r}. Valid: {', '.join(sorted(RAID_BY_ID))}")
        return RAID_BY_ID[key]
    if raid_stage is None:
        raise ValueError("Must specify --raid (or a known raid_stage 0/1/2); refusing to silently default to stage 0")
    st = int(raid_stage)
    if st not in DEFAULT_RAID_BY_STAGE:
        raise ValueError(f"Unknown raid_stage: {raid_stage!r}. Expected one of {sorted(DEFAULT_RAID_BY_STAGE)}")
    return RAID_BY_ID[DEFAULT_RAID_BY_STAGE[st]]


def _parse_ts(observed_at: str | int | float | None) -> int | None:
    """Parse unix timestamp or ISO-8601 string (including fractional seconds and Z/+00:00 offsets)."""
    if observed_at is None or observed_at == "":
        return None
    if isinstance(observed_at, (int, float)) and not isinstance(observed_at, bool):
        return int(observed_at)
    s = str(observed_at).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def load_owner_attestations(
    path: Path | None,
    *,
    raid_id: str,
    now_ts: int | None = None,
    default_ttl_sec: int = 7 * 86400,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Read-only loader for owner-signed encounter tactics & capability attestations (P1-5).

    JSON format:
    {
      "attestations": [
        {
          "slug": "ironbulwark",
          "raid_id": "mc_40",
          "tactics_verified": true,
          "encounter_capabilities": {"decurse_dispel": true},
          "verified_at": "2026-11-27T01:00:00Z",
          "ttl_sec": 604800,
          "by": "Wonky"
        }
      ]
    }
    Expired or malformed entries are ignored (leaving character `unknown`, never `pass`).
    """
    if path is None:
        return {}, []
    ts = int(now_ts if now_ts is not None else time.time())
    errors: list[str] = []
    if not path.is_file():
        return {}, [f"attestations_file_missing:{path}"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = raw.get("attestations") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            return {}, ["attestations_invalid_schema"]
    except Exception as exc:
        return {}, [f"attestations_read_error:{type(exc).__name__}"]

    out: dict[str, dict[str, Any]] = {}
    rid_clean = raid_id.strip().lower()
    for item in entries:
        if not isinstance(item, dict):
            continue
        item_rid = str(item.get("raid_id") or "").strip().lower()
        if item_rid not in (rid_clean, "*"):
            continue
        slug = str(item.get("slug") or "").strip().lower()
        if not slug:
            continue
        ver_ts = _parse_ts(item.get("verified_at"))
        if ver_ts is None:
            errors.append(f"attestation_missing_timestamp:{slug}")
            continue
        ttl = int(item.get("ttl_sec") or default_ttl_sec)
        age = ts - ver_ts
        if age < -5 or age > ttl:
            errors.append(f"attestation_expired:{slug}")
            continue
        out[slug] = {
            "tactics_verified": item.get("tactics_verified"),
            "encounter_capabilities": dict(item.get("encounter_capabilities") or {}),
            "verified_at": item.get("verified_at"),
            "age_sec": max(0, age),
            "by": str(item.get("by") or "owner"),
        }
    return out, errors


def _read_persona_map_readonly(
    db_path: Path | None,
    *,
    allow_no_persona_db: bool = False,
) -> tuple[dict[str, Any], bool, list[str]]:
    """Strictly read-only lookup of (realm_id, character_guid) -> persona record + active sessions from brain.db."""
    empty = {"by_key": {}, "by_persona_session": {}, "open_dual_session_personas": set()}
    if db_path is None:
        if allow_no_persona_db:
            return empty, True, []
        return empty, False, ["persona_db_path_missing"]
    if not db_path.is_file():
        return empty, False, [f"persona_db_missing:{db_path}"]

    snap = PersonaRegistry.read_only_snapshot(db_path)
    bundle = {
        "by_key": dict(snap["by_key"]),
        "by_persona_session": dict(snap["by_persona_session"]),
        "open_dual_session_personas": set(snap["open_dual_session_personas"]),
    }
    if not snap["ok"]:
        return bundle, False, list(snap["errors"])
    if not snap["has_registry"]:
        return empty, False, ["persona_tables_missing"]
    return bundle, True, []


def _is_raid_locked(locked_instances: Any, spec: RaidSpec, *, now_ts: int) -> tuple[bool, bool]:
    """Return `(is_locked, valid_shape)` checking `(map_id, difficulty[, resettime])` and expiry (P1-4/P3)."""
    if not isinstance(locked_instances, (list, tuple, set, frozenset)):
        return False, False
    for item in locked_instances:
        if isinstance(item, dict):
            if "map_id" not in item:
                return False, False
            mid = int(item["map_id"])
            diff = int(item.get("difficulty", 0))
            rst = int(item.get("resettime", 0))
            if mid == spec.map_id and diff == spec.difficulty and (rst == 0 or rst > now_ts):
                return True, True
        elif isinstance(item, (tuple, list)):
            if len(item) < 2:
                return False, False
            mid = int(item[0])
            diff = int(item[1])
            rst = int(item[2]) if len(item) >= 3 else 0
            if mid == spec.map_id and diff == spec.difficulty and (rst == 0 or rst > now_ts):
                return True, True
        elif isinstance(item, int) and not isinstance(item, bool):
            if item == spec.map_id:
                return True, True
        else:
            return False, False
    return False, True


def evaluate_character_readiness(
    comp: dict[str, Any],
    char_prof: dict[str, Any] | None,
    *,
    raid_id: str | None = None,
    raid_stage: int | None = None,
    bank_allocatable_consumables: int = 0,
    personal_consumables: int | None = None,
    durability_pct: float | None = None,
    avg_ilvl: float | None = None,
    filled_slots: int | None = None,
    expected_slots: int | None = None,
    broken_slots: int | None = None,
    in_other_group: bool | None = None,
    owner_group_id: int | None = None,
    recall_eta_min: int | None = None,
    locked_instances: Any = None,
    tactics_verified: bool | None = None,
    tactics_evidence: str = "",
    encounter_capabilities: dict[str, bool | None] | None = None,
    owned_items: set[int] | list[int] | None = None,
    completed_quests: set[int] | list[int] | None = None,
    live_telemetry: dict[str, Any] | None = None,
    live_observed_at: str | int | None = None,
    sql_observed_at: str | int | None = None,
    access_verified_against_db: bool = True,
    db_legal_min_level: int | None = None,
    observed_at: str = "",
    now_ts: int | None = None,
    max_telemetry_age_sec: int = 300,
    allocate_bank: bool = False,
) -> tuple[dict[str, Any], int]:
    """Evaluate one character's component-level readiness for a specific raid encounter.

    Fail-closed guarantees (Issue #16):
    - `raid_stage` defaults to `None` (never silently assumes stage 0!).
    - Per-source freshness: checks BOTH live telemetry timestamp and SQL snapshot timestamp when
      provided (never masks stale data with current clock!).
    - Static profile labels NEVER satisfy `role_spec` readiness.
    - Empty equipment slots fail iLvl/slot coverage checks.
    - Companions already grouped with `owner_group_id` remain available!
    - Potential Guild Bank allocation NEVER overwrites obtained readiness (`overall` stays
      honestly based on obtained bags; `overall_with_potential` tracks `ready_pending_distribution`).
    """
    spec = resolve_raid_spec(raid_id, raid_stage=raid_stage)
    ts = int(now_ts if now_ts is not None else time.time())
    lvl = int(comp.get("level") or 0)
    need_cons = int(spec.min_consumables_per_raider)
    eff_legal_min = int(db_legal_min_level if db_legal_min_level is not None else spec.legal_min_level)

    lt = live_telemetry if isinstance(live_telemetry, dict) else (
        comp.get("live_telemetry") if isinstance(comp.get("live_telemetry"), dict) else None
    )
    role_eval = evaluate_live_role_capability(
        char_prof, level=lvl, live_telemetry=lt, allow_profile_fallback=False
    )
    primary_role = role_eval["primary_role"] if role_eval["known"] else None
    flex_roles = list(role_eval["flex_roles"]) if role_eval["known"] else []
    viable_roles = list(role_eval["viable_roles"]) if role_eval["known"] else []

    components: dict[str, dict[str, Any]] = {}

    # 1. Per-source telemetry freshness (P0-2: check live & SQL timestamps separately; allow 5s skew!)
    live_ts = _parse_ts(
        live_observed_at
        if live_observed_at is not None
        else ((lt or {}).get("observed_at") if (lt or {}).get("observed_at") is not None else comp.get("telemetry_observed_at"))
    )
    sql_ts = _parse_ts(sql_observed_at if sql_observed_at is not None else comp.get("sql_observed_at"))
    # Fallback to explicit observed_at ONLY if neither per-source timestamp was supplied
    fallback_ts = _parse_ts(observed_at) if (live_ts is None and sql_ts is None) else None

    stale_notes: list[str] = []
    fresh_notes: list[str] = []
    if live_ts is None and sql_ts is None and fallback_ts is None:
        stale_notes.append("missing telemetry observation timestamp")
    else:
        for label, src_ts in (("live", live_ts), ("sql", sql_ts), ("snapshot", fallback_ts)):
            if src_ts is None:
                continue
            age = ts - src_ts
            if age < -5:
                stale_notes.append(f"{label} timestamp in future ({age}s)")
            elif age > int(max_telemetry_age_sec):
                stale_notes.append(f"stale {label} telemetry ({age}s > {max_telemetry_age_sec}s)")
            else:
                fresh_notes.append(f"{label}={max(0, age)}s")
        # If live_pos_fresh is explicitly False on live_telemetry -> stale!
        if isinstance(lt, dict) and lt.get("live_pos_fresh") is False and live_observed_at is None:
            stale_notes.append("live_positions.json stale or missing")

    if stale_notes:
        components["telemetry_freshness"] = {"state": "unknown", "evidence": "; ".join(stale_notes)}
    else:
        components["telemetry_freshness"] = {"state": "pass", "evidence": f"Fresh ({', '.join(fresh_notes)})"}

    # 2. Installed server progression stage
    if raid_stage is None:
        components["progression_stage"] = {"state": "unknown", "evidence": "Current server raid_stage not sampled"}
    elif int(raid_stage) < spec.raid_stage_min:
        components["progression_stage"] = {
            "state": "fail",
            "evidence": f"Raid locked by progression stage {raid_stage} < required {spec.raid_stage_min}",
        }
    else:
        components["progression_stage"] = {"state": "pass", "evidence": f"Stage {raid_stage} >= {spec.raid_stage_min} unlocked"}

    # 3. Level (checks both installed legal portal min level and raid combat-ready level)
    if lvl < eff_legal_min:
        components["level"] = {"state": "fail", "evidence": f"Lv {lvl} < legal portal minimum {eff_legal_min}"}
    elif lvl < spec.min_ready_level:
        components["level"] = {"state": "fail", "evidence": f"Lv {lvl} < raid-ready {spec.min_ready_level} (target {spec.target_level})"}
    else:
        components["level"] = {"state": "pass", "evidence": f"Lv {lvl} >= {spec.min_ready_level} (target {spec.target_level})"}

    # 4. Live verified role/spec (NO false readiness from static profile label!)
    if not role_eval["known"]:
        reason = role_eval["reasons"][0] if role_eval["reasons"] else "missing_live_role_telemetry"
        components["role_spec"] = {
            "state": "unknown",
            "evidence": f"Live spec/role unverified ({reason}); static profile cannot grant readiness",
        }
    elif not viable_roles:
        components["role_spec"] = {
            "state": "fail",
            "evidence": f"No verified viable role ({', '.join(role_eval['reasons']) or 'invalid_role'})",
        }
    else:
        flex_txt = f" (flex: {', '.join(flex_roles)})" if flex_roles else ""
        components["role_spec"] = {
            "state": "pass",
            "evidence": f"Verified live {primary_role}{flex_txt} ({role_eval['class_name']})",
        }

    # 5. Access / lockouts / attunements / required encounter items
    locks = locked_instances
    if locks is None:
        if comp.get("locked_instances") is not None:
            locks = comp["locked_instances"]
        elif comp.get("locked_maps") is not None:
            locks = comp["locked_maps"]

    access_fail: list[str] = []
    access_unknown: list[str] = []
    if not access_verified_against_db:
        access_unknown.append("installed dungeon_access_template not verified")
    if locks is None:
        access_unknown.append("raid lockout status not sampled")
    else:
        is_locked, valid_shape = _is_raid_locked(locks, spec, now_ts=ts)
        if not valid_shape:
            access_unknown.append("malformed raid lockout telemetry")
        elif is_locked:
            access_fail.append(f"locked to map {spec.map_id} diff {spec.difficulty}")

    if spec.required_quests:
        if completed_quests is None:
            access_unknown.append("attunement quests not sampled")
        else:
            cq_set = {int(q) for q in completed_quests}
            missing_q = [q for q in spec.required_quests if q not in cq_set]
            if missing_q:
                access_fail.append(f"missing attunement quests {missing_q}")

    if spec.required_items:
        if owned_items is None:
            access_unknown.append(f"required encounter items {list(spec.required_items)} not sampled")
        else:
            oi_set = {int(i) for i in owned_items}
            missing_i = [i for i in spec.required_items if i not in oi_set]
            if missing_i:
                access_fail.append(f"missing required items {missing_i}")

    if access_fail:
        components["access_lockout"] = {"state": "fail", "evidence": "; ".join(access_fail)}
    elif access_unknown:
        components["access_lockout"] = {"state": "unknown", "evidence": "; ".join(access_unknown)}
    else:
        components["access_lockout"] = {"state": "pass", "evidence": f"Unlocked & eligible for {spec.name}"}

    # 6. Equipment & durability (prefer fresher live equipped values when available; penalize empty slots!)
    eq_dict = (lt or {}).get("equipped") if isinstance((lt or {}).get("equipped"), dict) else {}
    live_is_fresher = (live_ts is not None and (sql_ts is None or live_ts >= sql_ts))
    if live_is_fresher and eq_dict.get("min_durability_pct") is not None:
        eff_dur = eq_dict.get("min_durability_pct")
    else:
        eff_dur = durability_pct if durability_pct is not None else eq_dict.get("min_durability_pct")

    if live_is_fresher and eq_dict.get("avg_ilvl") is not None:
        eff_ilvl = eq_dict.get("avg_ilvl")
    else:
        eff_ilvl = avg_ilvl if avg_ilvl is not None else eq_dict.get("avg_ilvl")

    eff_broken = broken_slots if broken_slots is not None else eq_dict.get("broken_slots")
    eff_filled = filled_slots if filled_slots is not None else eq_dict.get("filled_slots")
    eff_expected = expected_slots if expected_slots is not None else eq_dict.get("expected_slots")

    gear_fails: list[str] = []
    gear_unknowns: list[str] = []
    if eff_ilvl is None:
        gear_unknowns.append("equipped avg_ilvl not sampled")
    elif float(eff_ilvl) < spec.min_avg_ilvl:
        gear_fails.append(f"avg iLvl {float(eff_ilvl):.1f} < {spec.min_avg_ilvl:.1f}")

    if eff_filled is not None and eff_expected is not None and int(eff_filled) < int(eff_expected):
        gear_fails.append(f"empty gear slots ({int(eff_filled)}/{int(eff_expected)} filled)")

    if eff_dur is None:
        gear_unknowns.append("durability not sampled")
    elif float(eff_dur) < 80.0:
        gear_fails.append(f"durability {float(eff_dur):.0f}% < 80%")

    if eff_broken is not None and int(eff_broken) > 0:
        gear_fails.append(f"{int(eff_broken)} broken slot(s)")
    if eq_dict.get("has_main_hand") is False:
        gear_fails.append("missing main-hand weapon")

    if gear_fails:
        components["gear_durability"] = {"state": "fail", "evidence": "; ".join(gear_fails)}
    elif gear_unknowns:
        components["gear_durability"] = {"state": "unknown", "evidence": "; ".join(gear_unknowns)}
    else:
        components["gear_durability"] = {
            "state": "pass",
            "evidence": f"avg iLvl {float(eff_ilvl):.1f} >= {spec.min_avg_ilvl:.1f}, {float(eff_dur):.0f}% durability",
        }

    # 7. Encounter tactics & per-character capabilities (missing required key -> unknown, NEVER pass!)
    enc_caps: dict[str, bool | None] = dict(encounter_capabilities or {})
    if tactics_verified is False:
        components["encounter_tactics"] = {"state": "fail", "evidence": f"Not verified for {spec.name} encounter tactics"}
    elif tactics_verified is None:
        components["encounter_tactics"] = {"state": "unknown", "evidence": f"{spec.name} encounter tactics not attested"}
    else:
        missing_req_caps = [k for k in spec.character_encounter_requirements if k not in enc_caps or enc_caps[k] is None]
        failed_caps = [k for k, v in enc_caps.items() if v is False]
        if failed_caps:
            components["encounter_tactics"] = {"state": "fail", "evidence": f"Failed encounter checks: {', '.join(failed_caps)}"}
        elif missing_req_caps:
            components["encounter_tactics"] = {"state": "unknown", "evidence": f"Unverified encounter checks: {', '.join(missing_req_caps)}"}
        else:
            ev_txt = tactics_evidence or f"Verified for {spec.name} mechanics"
            components["encounter_tactics"] = {"state": "pass", "evidence": ev_txt}

    # 8. Consumables (obtained in personal bags vs potential bank allocation — NEVER conflated!)
    potential_bank_allocated = 0
    obtained_count: int | None = None
    shortfall_before_bank: int | None = None
    shortfall_after_bank: int | None = None

    if personal_consumables is None:
        components["consumables"] = {
            "state": "unknown",
            "evidence": f"Personal bag count for {spec.consumable_name} not sampled",
        }
    else:
        obtained_count = max(0, int(personal_consumables))
        shortfall_before_bank = max(0, need_cons - obtained_count)
        if shortfall_before_bank == 0:
            shortfall_after_bank = 0
            components["consumables"] = {
                "state": "pass",
                "evidence": f"{obtained_count}/{need_cons} {spec.consumable_name} obtained in bags",
            }
        elif allocate_bank and bank_allocatable_consumables >= shortfall_before_bank:
            potential_bank_allocated = shortfall_before_bank
            shortfall_after_bank = 0
            components["consumables"] = {
                "state": "pass_potential",
                "evidence": (
                    f"{obtained_count}/{need_cons} obtained + {potential_bank_allocated} potential from guild bank "
                    "(not yet distributed)"
                ),
            }
        else:
            shortfall_after_bank = shortfall_before_bank
            components["consumables"] = {
                "state": "fail",
                "evidence": (
                    f"{obtained_count}/{need_cons} obtained in bags; shortfall {shortfall_before_bank}"
                ),
            }

    # 9. Availability / recall (P1-3: grouped WITH owner's forming raid counts as AVAILABLE!)
    online = bool(comp.get("online"))
    comp_gid = comp.get("group_id")
    in_owner_raid = (
        owner_group_id is not None
        and int(owner_group_id) > 0
        and comp_gid is not None
        and int(comp_gid) == int(owner_group_id)
    )
    eff_in_other_group = False if in_owner_raid else (
        in_other_group if in_other_group is not None else comp.get("in_group")
    )
    is_dead = (lt or {}).get("is_dead")
    if not online:
        components["availability"] = {"state": "fail", "evidence": f"Offline (shift: {comp.get('shift_group', '?')})"}
    elif eff_in_other_group is None:
        components["availability"] = {"state": "unknown", "evidence": "Current group status not sampled"}
    elif bool(eff_in_other_group):
        eta = f" (~{recall_eta_min}m recall)" if recall_eta_min is not None else ""
        components["availability"] = {"state": "fail", "evidence": f"Committed in another group{eta}"}
    elif is_dead is True:
        components["availability"] = {"state": "fail", "evidence": "Character is dead"}
    elif in_owner_raid:
        components["availability"] = {"state": "pass", "evidence": f"Online in owner's forming raid (group #{owner_group_id})"}
    else:
        components["availability"] = {"state": "pass", "evidence": f"Online & free in {comp.get('zone', '?')}"}

    non_cons_core_keys = (
        "telemetry_freshness",
        "progression_stage",
        "level",
        "role_spec",
        "access_lockout",
        "gear_durability",
        "encounter_tactics",
    )
    non_cons_states = [components[k]["state"] for k in non_cons_core_keys]
    non_cons_pass = all(s == "pass" for s in non_cons_states)
    avail_pass = components["availability"]["state"] == "pass"
    avail_unknown = components["availability"]["state"] == "unknown"

    # Obtained-only overall status (honest: potential bank stock NEVER makes overall == ready_available!)
    cons_state = components["consumables"]["state"]
    if any(s == "fail" for s in non_cons_states) or cons_state in ("fail", "pass_potential"):
        overall_obtained = "not_ready"
    elif any(s == "unknown" for s in non_cons_states) or cons_state == "unknown" or avail_unknown:
        overall_obtained = "unknown"
    elif avail_pass:
        overall_obtained = "ready_available"
    else:
        overall_obtained = "ready_but_unavailable"

    if cons_state == "pass_potential" and non_cons_pass:
        overall_with_potential = "ready_pending_distribution" if avail_pass else (
            "unknown" if avail_unknown else "ready_but_unavailable"
        )
    else:
        overall_with_potential = overall_obtained

    obtained_ready = non_cons_pass and (shortfall_before_bank == 0)

    # Derive roster-level encounter capability indicators from verified live telemetry + explicit capabilities
    derived_caps: dict[str, bool | None] = dict(enc_caps)
    cls_lower = str(role_eval.get("class_name") or "").lower()
    known_sp = set((lt or {}).get("known_spells") or []) if isinstance((lt or {}).get("known_spells"), (list, set, tuple)) else None
    if "decurse_dispel" not in derived_caps and role_eval["known"] and cls_lower:
        derived_caps["decurse_dispel"] = cls_lower in DECURSE_DISPEL_CLASSES
    if "mage_tank_krosh" not in derived_caps and role_eval["known"] and cls_lower:
        derived_caps["mage_tank_krosh"] = (cls_lower == "mage")
    if "tranquilizing_shot" not in derived_caps and role_eval["known"] and cls_lower:
        if cls_lower != "hunter":
            derived_caps["tranquilizing_shot"] = False
        elif known_sp is not None:
            derived_caps["tranquilizing_shot"] = bool(known_sp & TRANQ_SHOT_SPELLS)
        else:
            derived_caps["tranquilizing_shot"] = None

    rec = {
        "persona_id": str(comp.get("persona_id") or persona_id_for_slug(comp["slug"])),
        "character_guid": int(comp["guid"]),
        "name": comp["name"],
        "slug": comp["slug"],
        "level": lvl,
        "class_name": role_eval.get("class_name", ""),
        "role": primary_role or "unknown",
        "primary_role": primary_role,
        "flex_roles": flex_roles,
        "viable_roles": viable_roles,
        "shift_group": comp.get("shift_group", ""),
        "overall": overall_obtained,
        "overall_with_potential": overall_with_potential,
        "non_consumable_ready": non_cons_pass,
        "available_now": avail_pass,
        "obtained_ready": obtained_ready,
        "consumables_needed": need_cons,
        "consumables_obtained": obtained_count,
        "consumables_shortfall_before_bank": shortfall_before_bank,
        "consumables_potential_from_bank": potential_bank_allocated,
        "consumables_shortfall_after_bank": shortfall_after_bank,
        "encounter_capabilities": derived_caps,
        "observed_at": observed_at or datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "components": components,
    }
    return rec, potential_bank_allocated


def _apply_potential_bank_to_active_record(rec: dict[str, Any], allocated: int, spec: RaidSpec) -> None:
    """Annotate an ACTIVE-ROSTER record with its potential Guild Bank allocation (P1-1: never fakes obtained!)."""
    if allocated <= 0:
        return
    need_cons = int(spec.min_consumables_per_raider)
    obtained = int(rec["consumables_obtained"] or 0)
    rec["consumables_potential_from_bank"] = int(allocated)
    rec["consumables_shortfall_after_bank"] = max(0, need_cons - obtained - int(allocated))
    if rec["consumables_shortfall_after_bank"] == 0:
        rec["components"]["consumables"] = {
            "state": "pass_potential",
            "evidence": (
                f"{obtained}/{need_cons} obtained + {allocated} potential from guild bank "
                "(not yet distributed)"
            ),
        }
        if rec["non_consumable_ready"] and rec["available_now"]:
            rec["overall_with_potential"] = "ready_pending_distribution"


def _min_cost_raid_matching(
    candidates_by_persona: dict[str, list[dict[str, Any]]],
    remaining_roles: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int], int]:
    """Exact minimum-cost maximum-cardinality distinct-persona raid role matching (P2-1!).

    Graph:
      SOURCE -> each persona node (cap=1, cost=0)
      persona node -> each role node `r` that persona can fill (cap=1, cost = best_char_cost(pid, r))
      role node `r` -> SINK (cap=remaining_roles[r], cost=0)

    Edge cost encodes `(bank_shortfall * 1000 + flex_penalty * 10)`:
    - Strictly maximizes filled raid slots FIRST (max flow),
    - Minimizes exact total Guild Bank consumables needed SECOND (exact minimum bank draw!),
    - Minimizes flex-role usage THIRD (prefers primary roles over flex roles!).
    """
    persona_ids = sorted(candidates_by_persona.keys())
    role_names = ("tank", "healer", "damage")
    role_caps = {r: max(0, int(remaining_roles.get(r, 0))) for r in role_names}
    total_needed = sum(role_caps.values())

    # Precompute best character per (persona, role)
    best_edge: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    for pid in persona_ids:
        for r in role_names:
            if role_caps[r] <= 0:
                continue
            opts = []
            for c in candidates_by_persona[pid]:
                is_prim = c.get("primary_role") == r
                is_flex = r in (c.get("flex_roles") or ())
                if not (is_prim or is_flex):
                    continue
                shortfall = max(0, int(c.get("consumables_shortfall_before_bank") or 0))
                edge_cost = shortfall * 1000 + (0 if is_prim else 10)
                opts.append((edge_cost, -int(c.get("level") or 0), c["slug"], c))
            if opts:
                opts.sort(key=lambda x: x[:3])
                best_edge[(pid, r)] = (opts[0][0], opts[0][3])

    # Build residual flow network: 0=SRC, 1..P=personas, P+1..P+3=roles, P+4=SINK
    n_p = len(persona_ids)
    SRC = 0
    ROLE_BASE = 1 + n_p
    SINK = ROLE_BASE + 3
    N = SINK + 1

    # Each edge: [to, rev_idx, cap, cost, meta]
    adj: list[list[list[Any]]] = [[] for _ in range(N)]

    def add_edge(u: int, v: int, cap: int, cost: int, meta: Any = None) -> None:
        fwd = [v, len(adj[v]), cap, cost, meta]
        bwd = [u, len(adj[u]), 0, -cost, None]
        adj[u].append(fwd)
        adj[v].append(bwd)

    for i, pid in enumerate(persona_ids):
        p_node = 1 + i
        add_edge(SRC, p_node, 1, 0)
        for r_idx, r in enumerate(role_names):
            if (pid, r) in best_edge:
                cost, ch = best_edge[(pid, r)]
                add_edge(p_node, ROLE_BASE + r_idx, 1, cost, (pid, r, ch))

    for r_idx, r in enumerate(role_names):
        if role_caps[r] > 0:
            add_edge(ROLE_BASE + r_idx, SINK, role_caps[r], 0)

    # Successive shortest augmenting path with Johnson potentials (all initial costs >= 0)
    INF = 10**15
    pot = [0] * N
    flow = 0
    while flow < total_needed:
        dist = [INF] * N
        parent_v = [-1] * N
        parent_e = [-1] * N
        dist[SRC] = 0
        pq: list[tuple[int, int]] = [(0, SRC)]
        while pq:
            d, u = heapq.heappop(pq)
            if d != dist[u]:
                continue
            for ei, e in enumerate(adj[u]):
                v, _rev, cap, cost, _meta = e
                if cap <= 0:
                    continue
                nd = d + cost + pot[u] - pot[v]
                if nd < dist[v]:
                    dist[v] = nd
                    parent_v[v] = u
                    parent_e[v] = ei
                    heapq.heappush(pq, (nd, v))
        if dist[SINK] == INF:
            break
        for u in range(N):
            if dist[u] < INF:
                pot[u] += dist[u]
        # Augment 1 unit of flow along shortest path
        v = SINK
        while v != SRC:
            u = parent_v[v]
            ei = parent_e[v]
            e = adj[u][ei]
            e[2] -= 1
            adj[v][e[1]][2] += 1
            v = u
        flow += 1

    # Extract matched assignments
    assigned: list[dict[str, Any]] = []
    assigned_personas: set[str] = set()
    filled_counts = {"tank": 0, "healer": 0, "damage": 0}
    total_bank_need = 0

    for i in range(n_p):
        p_node = 1 + i
        for e in adj[p_node]:
            v, _rev, cap, _cost, meta = e
            if meta is not None and cap == 0:  # edge saturated -> selected!
                pid, role_name, ch = meta
                assigned_personas.add(pid)
                filled_counts[role_name] += 1
                need = max(0, int(ch.get("consumables_shortfall_before_bank") or 0))
                total_bank_need += need
                assigned.append({
                    "kind": "companion",
                    "persona_id": pid,
                    "character_guid": ch["character_guid"],
                    "slug": ch["slug"],
                    "name": ch["name"],
                    "level": ch["level"],
                    "assigned_role": role_name,
                    "primary_role": ch["primary_role"],
                    "used_flex": ch["primary_role"] != role_name,
                    "consumables_obtained": ch["consumables_obtained"],
                    "consumables_shortfall_before_bank": need,
                    "consumables_potential_from_bank": 0,
                    "_rec": ch,
                })

    # Remaining unassigned distinct personas form the candidate bench
    bench: list[dict[str, Any]] = []
    for pid in persona_ids:
        if pid in assigned_personas:
            continue
        best_c = sorted(
            candidates_by_persona[pid],
            key=lambda c: (
                0 if int(c.get("consumables_shortfall_before_bank") or 0) == 0 else 1,
                int(c.get("consumables_shortfall_before_bank") or 0),
                -int(c.get("level") or 0),
                c["slug"],
            ),
        )[0]
        bench.append({
            "persona_id": pid,
            "character_guid": best_c["character_guid"],
            "slug": best_c["slug"],
            "name": best_c["name"],
            "level": best_c["level"],
            "primary_role": best_c["primary_role"],
            "flex_roles": list(best_c["flex_roles"]),
            "obtained_ready": bool(best_c["obtained_ready"]),
            "consumables_shortfall_before_bank": int(best_c.get("consumables_shortfall_before_bank") or 0),
            "_rec": best_c,
        })

    return assigned, bench, filled_counts, total_bank_need


def _evaluate_roster_encounter_coverage(
    spec: RaidSpec,
    active_humans: list[dict[str, Any]],
    active_companions: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Enforce RaidSpec.roster_encounter_requirements across the active raid roster (P0-3!)."""
    coverage: dict[str, dict[str, Any]] = {}
    bottlenecks: list[str] = []
    for req_key, min_count in spec.roster_encounter_requirements:
        covered_names: list[str] = []
        unknown_names: list[str] = []
        for h in active_humans:
            caps = h.get("encounter_capabilities") or {}
            if caps.get(req_key) is True:
                covered_names.append(h["name"])
            elif req_key not in caps or caps.get(req_key) is None:
                unknown_names.append(h["name"])
        for c in active_companions:
            rec = c.get("_rec") or {}
            caps = rec.get("encounter_capabilities") or {}
            if caps.get(req_key) is True:
                covered_names.append(c["name"])
            elif req_key not in caps or caps.get(req_key) is None:
                unknown_names.append(c["name"])
        if len(covered_names) >= min_count:
            state = "pass"
        elif len(covered_names) + len(unknown_names) >= min_count and len(unknown_names) > 0:
            state = "unknown"
            bottlenecks.append(f"encounter:{req_key}: {len(covered_names)}/{min_count} verified ({len(unknown_names)} unknown)")
        else:
            state = "fail"
            bottlenecks.append(f"encounter:{req_key}: {len(covered_names)}/{min_count} covered (short {min_count - len(covered_names)})")
        coverage[req_key] = {
            "required": int(min_count),
            "covered": len(covered_names),
            "unknown": len(unknown_names),
            "state": state,
            "covered_by": covered_names,
        }
    return coverage, bottlenecks


def evaluate_raid_roster(
    companions: dict[str, dict[str, Any]],
    *,
    raid_id: str | None = None,
    raid_stage: int | None = None,
    raid_stage_source: str = "caller",
    bank_consumable_pool: int | None = 0,
    telemetry_by_slug: dict[str, dict[str, Any]] | None = None,
    human_slots: list[dict[str, Any]] | None = None,
    owner_selected_slugs: set[str] | list[str] | None = None,
    owner_excluded_slugs: set[str] | list[str] | None = None,
    owner_pinned_roles: dict[str, str] | None = None,
    owner_group_id: int | None = None,
    exclude_core_protected: bool = False,
    access_verified_against_db: bool = True,
    db_legal_min_level: int | None = None,
    db_path: Path | None = None,
    allow_no_persona_db: bool = False,
    observed_at: str = "",
    now_ts: int | None = None,
    max_telemetry_age_sec: int = 300,
) -> dict[str, Any]:
    """Evaluate distinct-persona raid readiness for one specific owner-selected raid (zero auto-raids).

    Key safety invariants (Issue #16):
    - `feasible_now` == `feasible_obtained_only` (personal bags ONLY). Potential Guild Bank support
      is reported separately as `feasible_with_potential_bank` (`None` if bank pool unknown).
    - Fails closed if `db_path` is missing/unreadable/lacks persona tables (unless `allow_no_persona_db=True`).
    - Exact min-cost max-flow distinct-persona solver minimizes Guild Bank draw & never double-claims.
    - Bench viability is evaluated against leftover bank stock WITHOUT consuming or mutating bank allocation.
    - Human slots are strictly validated (no invalid roles, duplicates, companion collisions, or silent overflows).
    - Roster-level encounter requirements (`decurse_dispel`, `tranquilizing_shot`, `nature_resistance_soakers`,
      `mage_tank_krosh`) are actively enforced on the matched roster.
    """
    spec = resolve_raid_spec(raid_id, raid_stage=raid_stage)
    ts = int(now_ts if now_ts is not None else time.time())
    obs_iso = observed_at or datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    known = set(profiles())
    telem = telemetry_by_slug or {}
    bank_pool_known = bank_consumable_pool is not None
    total_bank_pool = max(0, int(bank_consumable_pool)) if bank_pool_known else 0

    persona_bundle, db_ok, db_errors = _read_persona_map_readonly(
        db_path, allow_no_persona_db=allow_no_persona_db
    )
    persona_map = persona_bundle["by_key"]
    by_persona_session = persona_bundle["by_persona_session"]
    open_dual_session_personas = persona_bundle["open_dual_session_personas"]

    sel_filter = {s.strip().lower() for s in owner_selected_slugs} if owner_selected_slugs is not None else None
    exc_filter = {s.strip().lower() for s in owner_excluded_slugs} if owner_excluded_slugs is not None else set()
    pins = {k.strip().lower(): v.strip().lower() for k, v in (owner_pinned_roles or {}).items()}
    comp_slugs_lower = {str(s).strip().lower() for s in companions}
    comp_names_lower = {str(c.get("name") or "").strip().lower() for c in companions.values()}

    # 1. Strictly validate owner-confirmed human slots (P1-6: zero silent drops or double counts!)
    role_req = spec.roles_dict()
    remaining_roles = dict(role_req)
    confirmed_humans: list[dict[str, Any]] = []
    rejected_humans: list[dict[str, Any]] = []
    human_by_role: dict[str, list[str]] = {"tank": [], "healer": [], "damage": []}
    seen_human_names: set[str] = set()

    for hs in (human_slots or []):
        h_name = str(hs.get("name") or "").strip()
        h_key = h_name.lower()
        h_role_raw = str(hs.get("role") or "").strip().lower()
        h_role = "damage" if h_role_raw in ("dps", "mdps", "rdps") else h_role_raw
        h_status = str(hs.get("status") or "confirmed").strip().lower()

        if not h_name:
            rejected_humans.append({"entry": hs, "reason": "missing_name"})
            continue
        if h_role not in ALLOWED_HUMAN_ROLES:
            rejected_humans.append({"name": h_name, "role": h_role_raw, "reason": f"invalid_role:{h_role_raw}"})
            continue
        if h_key in seen_human_names:
            rejected_humans.append({"name": h_name, "role": h_role, "reason": "duplicate_human"})
            continue
        if h_key in comp_slugs_lower or h_key in comp_names_lower:
            rejected_humans.append({"name": h_name, "role": h_role, "reason": "collides_with_companion"})
            continue
        if h_status not in ("confirmed", "ready", "online"):
            rejected_humans.append({"name": h_name, "role": h_role, "reason": f"not_confirmed:{h_status}"})
            continue
        if remaining_roles[h_role] <= 0:
            rejected_humans.append({"name": h_name, "role": h_role, "reason": f"role_overflow:{h_role}"})
            continue

        seen_human_names.add(h_key)
        remaining_roles[h_role] -= 1
        human_by_role[h_role].append(h_name)
        confirmed_humans.append({
            "kind": "human",
            "name": h_name,
            "assigned_role": h_role,
            "status": h_status,
            "encounter_capabilities": dict(hs.get("encounter_capabilities") or {}),
        })

    # 2. Phase A: Evaluate every companion's intrinsic readiness WITHOUT allocating Guild Bank!
    records: list[dict[str, Any]] = []
    by_persona_all: dict[str, list[dict[str, Any]]] = {}
    rec_by_slug: dict[str, dict[str, Any]] = {}
    avail_obtained_by_persona: dict[str, list[dict[str, Any]]] = {}
    avail_bank_eligible_by_persona: dict[str, list[dict[str, Any]]] = {}
    unknown_reason_histogram: dict[str, int] = {}

    for comp in sorted(companions.values(), key=lambda c: (-int(c.get("level") or 0), str(c.get("slug") or ""))):
        slug = str(comp["slug"]).strip().lower()
        guid = int(comp["guid"])
        raw_realm = comp.get("realm_id")
        if raw_realm is not None:
            realm_id = int(raw_realm)
            if realm_id <= 0:
                continue
        else:
            realm_id = 1
        ckey = (realm_id, guid)
        pmap_rec = persona_map.get(ckey)
        if slug not in known and pmap_rec is None:
            continue
        if sel_filter is not None and slug not in sel_filter:
            continue
        if slug in exc_filter:
            continue

        # When a real PersonaRegistry was loaded, enforce resolution, quarantine, dual-session, and optional core latch!
        unresolved_or_quarantined = False
        if db_path is not None and db_ok:
            if pmap_rec is None:
                unresolved_or_quarantined = True
                pid = str(comp.get("persona_id") or f"unresolved_r{realm_id}_g{guid}")
            else:
                pid = str(pmap_rec["persona_id"])
                if pmap_rec.get("state") != "active" or not pmap_rec.get("char_active", True):
                    unresolved_or_quarantined = True
                if pid in open_dual_session_personas:
                    unresolved_or_quarantined = True
                active_sess = by_persona_session.get(pid)
                if active_sess is not None and active_sess != (realm_id, guid, int(pmap_rec["incarnation"])):
                    unresolved_or_quarantined = True
                if exclude_core_protected and pmap_rec.get("core_protected") and not pmap_rec.get("enrolled_coop"):
                    unresolved_or_quarantined = True
        else:
            pid = str(comp.get("persona_id") or persona_id_for_slug(slug))
        comp_with_pid = {**comp, "persona_id": pid}
        t = telem.get(slug) or {}
        char_prof = profile(slug) if slug in known else None

        rec, _ = evaluate_character_readiness(
            comp_with_pid,
            char_prof,
            raid_id=spec.raid_id,
            raid_stage=raid_stage,
            bank_allocatable_consumables=0,
            personal_consumables=t.get("personal_consumables"),
            durability_pct=t.get("durability_pct"),
            avg_ilvl=t.get("avg_ilvl"),
            filled_slots=t.get("filled_slots"),
            expected_slots=t.get("expected_slots"),
            broken_slots=t.get("broken_slots"),
            in_other_group=t.get("in_other_group"),
            owner_group_id=owner_group_id,
            recall_eta_min=t.get("recall_eta_min"),
            locked_instances=t.get("locked_instances", t.get("locked_maps")),
            tactics_verified=t.get("tactics_verified"),
            tactics_evidence=str(t.get("tactics_evidence") or ""),
            encounter_capabilities=t.get("encounter_capabilities"),
            owned_items=t.get("owned_items"),
            completed_quests=t.get("completed_quests"),
            live_telemetry=t.get("live_telemetry") if isinstance(t.get("live_telemetry"), dict) else comp.get("live_telemetry"),
            live_observed_at=t.get("live_observed_at"),
            sql_observed_at=t.get("sql_observed_at"),
            access_verified_against_db=access_verified_against_db,
            db_legal_min_level=db_legal_min_level,
            observed_at=str(t.get("observed_at") or comp.get("observed_at") or observed_at),
            now_ts=ts,
            max_telemetry_age_sec=max_telemetry_age_sec,
            allocate_bank=False,
        )
        records.append(rec)
        rec_by_slug[slug] = rec
        by_persona_all.setdefault(pid, []).append(rec)

        for cname, cdata in rec["components"].items():
            if cdata["state"] == "unknown":
                unknown_reason_histogram[cname] = unknown_reason_histogram.get(cname, 0) + 1

        if (
            db_ok
            and not unresolved_or_quarantined
            and rec["available_now"]
            and rec["non_consumable_ready"]
            and rec["consumables_shortfall_before_bank"] is not None
        ):
            if rec["consumables_shortfall_before_bank"] == 0:
                avail_obtained_by_persona.setdefault(pid, []).append(rec)
                avail_bank_eligible_by_persona.setdefault(pid, []).append(rec)
            elif bank_pool_known and rec["consumables_shortfall_before_bank"] <= total_bank_pool:
                avail_bank_eligible_by_persona.setdefault(pid, []).append(rec)

    # 3. Handle owner-pinned companions FIRST (P2-8: hard pin slug:role!)
    pinned_assigned: list[dict[str, Any]] = []
    unmet_owner_pins: list[dict[str, Any]] = []
    pinned_personas: set[str] = set()
    rem_roles_after_pins = dict(remaining_roles)

    for pin_slug, pin_role in sorted(pins.items()):
        pr = "damage" if pin_role in ("dps", "mdps", "rdps") else pin_role
        if pr not in ALLOWED_HUMAN_ROLES:
            unmet_owner_pins.append({"slug": pin_slug, "role": pin_role, "reason": f"invalid_role:{pin_role}"})
            continue
        rec = rec_by_slug.get(pin_slug)
        if rec is None:
            unmet_owner_pins.append({"slug": pin_slug, "role": pr, "reason": "companion_not_found"})
            continue
        if rec["persona_id"] in pinned_personas:
            unmet_owner_pins.append({"slug": pin_slug, "role": pr, "reason": "duplicate_persona_pin"})
            continue
        if not (db_ok and rec["available_now"] and rec["non_consumable_ready"]):
            unmet_owner_pins.append({"slug": pin_slug, "role": pr, "reason": f"not_ready_or_unavailable:{rec['overall']}"})
            continue
        if pr not in (rec.get("viable_roles") or ()):
            unmet_owner_pins.append({"slug": pin_slug, "role": pr, "reason": f"role_not_capable:{pr}"})
            continue
        if rem_roles_after_pins[pr] <= 0:
            unmet_owner_pins.append({"slug": pin_slug, "role": pr, "reason": f"role_overflow:{pr}"})
            continue
        rem_roles_after_pins[pr] -= 1
        pinned_personas.add(rec["persona_id"])
        pinned_assigned.append({
            "kind": "companion",
            "pinned": True,
            "persona_id": rec["persona_id"],
            "character_guid": rec["character_guid"],
            "slug": rec["slug"],
            "name": rec["name"],
            "level": rec["level"],
            "assigned_role": pr,
            "primary_role": rec["primary_role"],
            "used_flex": rec["primary_role"] != pr,
            "consumables_obtained": rec["consumables_obtained"],
            "consumables_shortfall_before_bank": int(rec["consumables_shortfall_before_bank"] or 0),
            "consumables_potential_from_bank": 0,
            "_rec": rec,
        })

    # 4. Phase B1: Obtained-only exact matching (personal bags ONLY, zero Guild Bank touch!)
    #    Owner-pinned raiders are HARD requirements: if ANY pinned raider lacks obtained consumables,
    #    obtained-only readiness is False (never silently unpin an owner-pinned character!).
    obt_pool = {pid: cs for pid, cs in avail_obtained_by_persona.items() if pid not in pinned_personas}
    pins_all_obtained = all(p["consumables_shortfall_before_bank"] == 0 for p in pinned_assigned)
    obt_pins = [p for p in pinned_assigned if p["consumables_shortfall_before_bank"] == 0]
    obt_rem_roles = dict(remaining_roles)
    for p in obt_pins:
        obt_rem_roles[p["assigned_role"]] = max(0, obt_rem_roles[p["assigned_role"]] - 1)
    obt_matched, _obt_bench, obt_filled, _ = _min_cost_raid_matching(obt_pool, obt_rem_roles)
    all_obt_comp = obt_pins + obt_matched
    obt_total_filled = {
        r: len(human_by_role[r]) + sum(1 for p in obt_pins if p["assigned_role"] == r) + obt_filled[r]
        for r in ("tank", "healer", "damage")
    }
    obtained_shortfalls = {
        r: max(0, role_req[r] - obt_total_filled[r])
        for r in ("tank", "healer", "damage")
    }
    obt_enc_cov, obt_enc_bn = _evaluate_roster_encounter_coverage(spec, confirmed_humans, all_obt_comp)
    obt_enc_ok = all(v["state"] == "pass" for v in obt_enc_cov.values())

    feasible_obtained_only = (
        db_ok
        and not rejected_humans
        and not unmet_owner_pins
        and pins_all_obtained
        and raid_stage is not None
        and int(raid_stage) >= spec.raid_stage_min
        and all(v == 0 for v in obtained_shortfalls.values())
        and obt_enc_ok
    )

    # 5. Phase B2: Exact min-cost potential Guild Bank allocation (active roster ONLY; bench NEVER consumes stock!)
    remaining_bank: int | None = total_bank_pool if bank_pool_known else None
    bank_allocated_total = 0
    active_companion_raw: list[dict[str, Any]] = []
    bench_candidates: list[dict[str, Any]] = []

    if bank_pool_known and db_ok:
        pot_pool = {pid: list(cs) for pid, cs in avail_bank_eligible_by_persona.items() if pid not in pinned_personas}
        pin_bank_need = sum(p["consumables_shortfall_before_bank"] for p in pinned_assigned)
        matched_raw, bench_raw, _filled_m, match_bank_need = _min_cost_raid_matching(pot_pool, rem_roles_after_pins)

        # Because _min_cost_raid_matching finds the exact minimum bank cost for maximum cardinality,
        # if pin_bank_need + match_bank_need > total_bank_pool, we must trim unaffordable high-cost candidates
        # only when we want the largest affordable partial roster!
        combined_need = pin_bank_need + match_bank_need
        if combined_need <= total_bank_pool:
            active_companion_raw = list(pinned_assigned) + matched_raw
            bench_candidates = bench_raw
        else:
            # Partial roster that fits within total_bank_pool (pins first if affordable, then lowest-cost slots)
            budget = total_bank_pool
            for entry in sorted(
                list(pinned_assigned) + matched_raw,
                key=lambda e: (0 if e.get("pinned") else 1, int(e["consumables_shortfall_before_bank"]), e["slug"]),
            ):
                need = int(entry["consumables_shortfall_before_bank"])
                if need <= budget:
                    budget -= need
                    active_companion_raw.append(entry)
            bench_candidates = bench_raw

        # Commit potential bank allocations ONLY to active_companion_raw (NEVER bench!)
        assert remaining_bank is not None
        for entry in active_companion_raw:
            need = int(entry["consumables_shortfall_before_bank"])
            if need > 0:
                if remaining_bank < need:
                    raise RuntimeError(f"Bank allocation invariant violated: remaining={remaining_bank} < need={need}")
                remaining_bank -= need
                bank_allocated_total += need
                entry["consumables_potential_from_bank"] = need
                _apply_potential_bank_to_active_record(entry["_rec"], need, spec)
    else:
        # Bank pool unknown -> active roster uses obtained-only matches
        active_companion_raw = list(all_obt_comp)
        bench_candidates = _obt_bench

    # 6. Viable bench: check against leftover `remaining_bank` WITHOUT subtracting or mutating records! (P1-8!)
    viable_bench: list[dict[str, Any]] = []
    bench_potential_need = 0
    for b in sorted(bench_candidates, key=lambda e: (int(e["consumables_shortfall_before_bank"]), e["slug"])):
        need = int(b["consumables_shortfall_before_bank"])
        if need == 0:
            viable_bench.append({
                **{k: v for k, v in b.items() if k != "_rec"},
                "consumables_status": "obtained_ready",
            })
        elif remaining_bank is not None and need <= remaining_bank:
            bench_potential_need += need
            viable_bench.append({
                **{k: v for k, v in b.items() if k != "_rec"},
                "consumables_status": "potential_bank_coverable_not_allocated",
            })

    # 7. Distinct-persona summary counts: BOTH obtained-only AND with-potential (P1-1!)
    _RANK_OBT = {"ready_available": 0, "ready_but_unavailable": 1, "unknown": 2, "not_ready": 3}
    _RANK_POT = {"ready_available": 0, "ready_pending_distribution": 1, "ready_but_unavailable": 2, "unknown": 3, "not_ready": 4}
    summary_obtained = {"ready_available": 0, "ready_but_unavailable": 0, "not_ready": 0, "unknown": 0}
    summary_potential = {"ready_available": 0, "ready_pending_distribution": 0, "ready_but_unavailable": 0, "not_ready": 0, "unknown": 0}
    ready_by_role: dict[str, list[str]] = {"tank": [], "healer": [], "damage": []}
    ready_unavail_by_role: dict[str, list[str]] = {"tank": [], "healer": [], "damage": []}

    for pid, chars in sorted(by_persona_all.items()):
        best_obt = sorted(chars, key=lambda r: (_RANK_OBT.get(r["overall"], 9), -int(r["level"]), r["slug"]))[0]
        ov_o = best_obt["overall"]
        summary_obtained[ov_o] = summary_obtained.get(ov_o, 0) + 1
        r_name = best_obt["role"]
        if r_name in ready_by_role:
            if ov_o == "ready_available":
                ready_by_role[r_name].append(best_obt["name"])
            elif ov_o == "ready_but_unavailable":
                ready_unavail_by_role[r_name].append(best_obt["name"])

        best_pot = sorted(chars, key=lambda r: (_RANK_POT.get(r["overall_with_potential"], 9), -int(r["level"]), r["slug"]))[0]
        ov_p = best_pot["overall_with_potential"]
        summary_potential[ov_p] = summary_potential.get(ov_p, 0) + 1

    # 8. Roster-level encounter coverage & shortfalls
    enc_coverage, enc_bottlenecks = _evaluate_roster_encounter_coverage(spec, confirmed_humans, active_companion_raw)
    enc_ok = all(v["state"] == "pass" for v in enc_coverage.values())

    pot_total_filled = {
        r: len(human_by_role[r]) + sum(1 for c in active_companion_raw if c["assigned_role"] == r)
        for r in ("tank", "healer", "damage")
    }
    shortfalls = {r: max(0, role_req[r] - pot_total_filled[r]) for r in ("tank", "healer", "damage")}

    bottlenecks: list[str] = []
    if not db_ok:
        bottlenecks.append(f"Persona DB unavailable ({'; '.join(db_errors)})")
    if raid_stage is None:
        bottlenecks.append("Current server raid_stage unknown (fail-closed)")
    elif int(raid_stage) < spec.raid_stage_min:
        bottlenecks.append(f"Progression locked: stage {raid_stage} < required {spec.raid_stage_min} for {spec.name}")
    if rejected_humans:
        bottlenecks.append(f"Invalid/rejected human slots: {len(rejected_humans)}")
    if unmet_owner_pins:
        bottlenecks.append(f"Unmet owner pins: {', '.join(p['slug'] for p in unmet_owner_pins)}")
    if not bank_pool_known:
        bottlenecks.append("Guild bank consumable pool unknown (potential allocation unavailable)")
    for r in ("tank", "healer", "damage"):
        need = int(role_req.get(r, 0))
        have = pot_total_filled[r]
        if have < need:
            bottlenecks.append(f"{r}: {have}/{need} ready & available (short {need - have})")
    bottlenecks.extend(enc_bottlenecks)
    if unknown_reason_histogram:
        top_unk = ", ".join(f"{k}={v}" for k, v in sorted(unknown_reason_histogram.items(), key=lambda x: (-x[1], x[0]))[:4])
        bottlenecks.append(f"unknown telemetry components: {top_unk}")

    if not bank_pool_known:
        feasible_with_potential_bank: bool | None = None
    else:
        feasible_with_potential_bank = (
            db_ok
            and not rejected_humans
            and not unmet_owner_pins
            and raid_stage is not None
            and int(raid_stage) >= spec.raid_stage_min
            and all(v == 0 for v in shortfalls.values())
            and enc_ok
        )

    active_roster = confirmed_humans + [{k: v for k, v in c.items() if k != "_rec"} for c in active_companion_raw]

    return {
        "observed_at": obs_iso,
        "read_only": True,
        "owner_initiated_only": True,
        "auto_raid_enabled": False,
        "raid_id": spec.raid_id,
        "tier_name": spec.name,
        "map_id": spec.map_id,
        "difficulty": spec.difficulty,
        "raid_size": spec.raid_size,
        "raid_stage": raid_stage,
        "raid_stage_source": raid_stage_source,
        "raid_stage_min": spec.raid_stage_min,
        "min_level": spec.min_ready_level,
        "legal_min_level": int(db_legal_min_level if db_legal_min_level is not None else spec.legal_min_level),
        "min_avg_ilvl": spec.min_avg_ilvl,
        "consumable_entry": spec.consumable_entry,
        "consumable_name": spec.consumable_name,
        "min_consumables_per_raider": spec.min_consumables_per_raider,
        "encounter_coverage": enc_coverage,
        "db_ok": db_ok,
        "db_errors": db_errors,
        "distinct_personas_evaluated": len(by_persona_all),
        "summary_counts": summary_obtained,
        "summary_counts_obtained": summary_obtained,
        "summary_counts_with_potential": summary_potential,
        "unknown_reason_histogram": unknown_reason_histogram,
        "role_requirements": role_req,
        "human_slots": confirmed_humans,
        "rejected_humans": rejected_humans,
        "unmet_owner_pins": unmet_owner_pins,
        "human_counts_by_role": {r: len(human_by_role[r]) for r in ("tank", "healer", "damage")},
        "matched_counts_by_role": pot_total_filled,
        "matched_counts_obtained_by_role": obt_total_filled,
        "ready_available_by_role": ready_by_role,
        "ready_but_unavailable_by_role": ready_unavail_by_role,
        "active_roster": active_roster,
        "viable_bench": viable_bench,
        "bench_potential_consumables_need": bench_potential_need,
        "shortfalls": shortfalls,
        "obtained_only_shortfalls": obtained_shortfalls,
        "bottlenecks": bottlenecks,
        # P1-1: Headline feasible_now STRICTLY reflects obtained-only readiness (NEVER potential bank!)
        "feasible_now": feasible_obtained_only,
        "feasible_obtained_only": feasible_obtained_only,
        "feasible_with_potential_bank": feasible_with_potential_bank,
        "bank_allocation_mode": "potential_allocation_not_reservation",
        "bank_consumables_pool": total_bank_pool if bank_pool_known else None,
        "bank_consumables_potential_allocated": bank_allocated_total,
        "bank_consumables_unallocated": remaining_bank,
        "characters": records,
    }


def fetch_raid_readiness_telemetry(
    world_ctx: dict[str, Any],
    spec: RaidSpec,
    *,
    sql_exec=None,
    guild_id: int | None = None,
    attestations: dict[str, dict[str, Any]] | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Read-only MySQL gather of guild-scoped bank stock, personal bag consumables, full-slot avg_ilvl,
    permanent unexpired lockouts, installed `dungeon_access_template`, and owner attestations.

    Fail-closed on every query: returns `None` (never fabricated 0 or empty) when a read fails!
    """
    from .bridge import run_sql

    runner = sql_exec if sql_exec is not None else run_sql
    ts = int(now_ts if now_ts is not None else time.time())
    companions = world_ctx.get("companions") or {}
    att = attestations or {}
    errors: list[str] = []
    world_db = (os.getenv("HEARTHCORE_WORLD_DB_NAME") or "acore_world").strip() or "acore_world"
    if not re.match(r"^[A-Za-z0-9_]+$", world_db):
        world_db = "acore_world"

    telem: dict[str, dict[str, Any]] = {}
    guid_to_slug: dict[int, str] = {}
    for slug, comp in companions.items():
        guid = int(comp["guid"])
        guid_to_slug[guid] = slug
        a = att.get(slug.lower()) or {}
        live_obs = comp.get("live_pos_ts") or world_ctx.get("live_pos_ts")
        telem[slug] = {
            "personal_consumables": None,
            "durability_pct": None,
            "avg_ilvl": None,
            "filled_slots": None,
            "expected_slots": None,
            "broken_slots": None,
            "in_other_group": comp.get("in_group"),
            "locked_instances": None,
            "tactics_verified": a.get("tactics_verified"),
            "tactics_evidence": (
                f"Attested by {a['by']} ({a['age_sec']}s ago)" if a.get("tactics_verified") is True else ""
            ),
            "encounter_capabilities": dict(a.get("encounter_capabilities") or {}),
            "owned_items": None,
            "completed_quests": None,
            "live_telemetry": comp.get("live_telemetry"),
            "live_observed_at": live_obs,
            "sql_observed_at": ts,
        }

    # 0. Verify installed acore_world.dungeon_access_template at runtime (P1-9!)
    access_verified_against_db = False
    db_legal_min_level: int | None = None
    try:
        acc_rows = runner(
            f"SELECT min_level, max_level FROM `{world_db}`.dungeon_access_template "
            f"WHERE map_id = {int(spec.map_id)} AND difficulty = {int(spec.difficulty)} LIMIT 1;"
        )
        if acc_rows and acc_rows[0] and acc_rows[0][0] is not None:
            db_legal_min_level = int(acc_rows[0][0])
            access_verified_against_db = True
            if db_legal_min_level != spec.legal_min_level:
                errors.append(
                    f"access_level_drift:{spec.raid_id}:db={db_legal_min_level}!=catalog={spec.legal_min_level}"
                )
        else:
            errors.append(f"dungeon_access_template_missing_row:{spec.map_id}:{spec.difficulty}")
    except Exception as exc:
        errors.append(f"dungeon_access_template:{type(exc).__name__}")
        logger.warning("Failed reading installed dungeon_access_template: %s", exc)

    if not guid_to_slug:
        return {
            "telemetry_by_slug": telem,
            "bank_pool": None,
            "guild_id": guild_id,
            "access_verified_against_db": access_verified_against_db,
            "db_legal_min_level": db_legal_min_level,
            "errors": errors,
        }

    glist = ",".join(str(g) for g in sorted(guid_to_slug))

    # 1. Personal bag consumables (backpack slots 23..38 + items inside equipped bag slots 19..22, NOT personal bank!)
    try:
        rows = runner(
            f"SELECT ci.guid, COALESCE(SUM(ii.count), 0) "
            f"FROM character_inventory ci "
            f"JOIN item_instance ii ON ii.guid = ci.item "
            f"WHERE ci.guid IN ({glist}) AND ii.itemEntry = {int(spec.consumable_entry)} "
            f"AND ((ci.bag = 0 AND ci.slot BETWEEN 23 AND 38) "
            f"OR ci.bag IN (SELECT b.item FROM character_inventory b WHERE b.guid = ci.guid AND b.bag = 0 AND b.slot BETWEEN 19 AND 22)) "
            f"GROUP BY ci.guid;"
        )
        for t in telem.values():
            t["personal_consumables"] = 0
        for r in rows:
            if len(r) >= 2:
                sl = guid_to_slug.get(int(r[0]))
                if sl:
                    telem[sl]["personal_consumables"] = int(r[1])
    except Exception as exc:
        errors.append(f"personal_consumables:{type(exc).__name__}")
        logger.warning("Failed reading personal raid consumables: %s", exc)

    # 2. Guild-scoped Guild Bank consumable pool (P0-4 & P2-6: NEVER sum across unrelated guilds; None on error!)
    resolved_guild_id = guild_id
    bank_pool: int | None = None
    try:
        if resolved_guild_id is None:
            g_rows = runner(
                f"SELECT DISTINCT guildid FROM guild_member WHERE guid IN ({glist}) AND guildid > 0;"
            )
            gids = sorted({int(r[0]) for r in g_rows if r and r[0] is not None and int(r[0]) > 0})
            if len(gids) == 1:
                resolved_guild_id = gids[0]
            elif len(gids) == 0:
                errors.append("guild_id_not_found_for_companions")
            else:
                errors.append(f"ambiguous_multiple_guilds:{gids}")
        if resolved_guild_id is not None:
            b_rows = runner(
                f"SELECT COALESCE(SUM(ii.count), 0) "
                f"FROM guild_bank_item gbi JOIN item_instance ii ON ii.guid = gbi.item_guid "
                f"WHERE gbi.guildid = {int(resolved_guild_id)} AND ii.itemEntry = {int(spec.consumable_entry)};"
            )
            if b_rows and b_rows[0] and b_rows[0][0] is not None:
                bank_pool = max(0, int(b_rows[0][0]))
            else:
                bank_pool = 0
    except Exception as exc:
        bank_pool = None
        errors.append(f"bank_consumables:{type(exc).__name__}")
        logger.warning("Failed reading guild-scoped bank consumable pool: %s", exc)

    # 3. Full-slot equipped avg_ilvl & durability (P1-2: divides SUM(ItemLevel) by expected gear slots, never ignores empty slots!)
    try:
        eq_rows = runner(
            f"SELECT ci.guid, COALESCE(SUM(it.ItemLevel), 0), COUNT(DISTINCT ci.slot), "
            f"MAX(CASE WHEN ci.slot = 15 AND it.InventoryType = 17 THEN 1 ELSE 0 END), "
            f"MIN(CASE WHEN it.MaxDurability > 0 THEN (ii.durability * 100.0 / it.MaxDurability) ELSE NULL END), "
            f"SUM(CASE WHEN it.MaxDurability > 0 AND ii.durability <= 0 THEN 1 ELSE 0 END) "
            f"FROM character_inventory ci "
            f"JOIN item_instance ii ON ii.guid = ci.item "
            f"JOIN `{world_db}`.item_template it ON it.entry = ii.itemEntry "
            f"WHERE ci.guid IN ({glist}) AND ci.bag = 0 AND ci.slot BETWEEN 0 AND 17 AND ci.slot != 3 "
            f"GROUP BY ci.guid;"
        )
        for r in eq_rows:
            if len(r) >= 6:
                sl = guid_to_slug.get(int(r[0]))
                if sl:
                    sum_ilvl = float(r[1] or 0.0)
                    filled = int(r[2] or 0)
                    has_2h = int(r[3] or 0) == 1
                    expected = 16 if has_2h else 17
                    telem[sl]["avg_ilvl"] = round(sum_ilvl / float(expected), 1)
                    telem[sl]["filled_slots"] = filled
                    telem[sl]["expected_slots"] = expected
                    telem[sl]["durability_pct"] = round(float(r[4]), 1) if r[4] is not None else None
                    telem[sl]["broken_slots"] = int(r[5]) if r[5] is not None else 0
    except Exception as exc:
        errors.append(f"equipped_ilvl:{type(exc).__name__}")
        logger.warning("Failed reading equipped avg_ilvl/durability: %s", exc)

    # 4. Permanent unexpired instance lockouts (P1-4!)
    try:
        lock_rows = runner(
            f"SELECT ci.guid, i.map, i.difficulty, i.resettime "
            f"FROM character_instance ci JOIN instance i ON i.id = ci.instance "
            f"WHERE ci.guid IN ({glist}) AND ci.permanent = 1 AND (i.resettime = 0 OR i.resettime > {int(ts)});"
        )
        for t in telem.values():
            t["locked_instances"] = []
        for r in lock_rows:
            if len(r) >= 4:
                sl = guid_to_slug.get(int(r[0]))
                if sl:
                    telem[sl]["locked_instances"].append((int(r[1]), int(r[2]), int(r[3])))
    except Exception as exc:
        errors.append(f"raid_lockouts:{type(exc).__name__}")
        logger.warning("Failed reading permanent raid lockouts (fail-closed -> None): %s", exc)

    # 5. Required encounter items (e.g. Onyxia Scale Cloak 15138 for BWL)
    if spec.required_items:
        try:
            items_csv = ",".join(str(int(i)) for i in spec.required_items)
            i_rows = runner(
                f"SELECT DISTINCT ci.guid, ii.itemEntry "
                f"FROM character_inventory ci JOIN item_instance ii ON ii.guid = ci.item "
                f"WHERE ci.guid IN ({glist}) AND ii.itemEntry IN ({items_csv});"
            )
            for t in telem.values():
                t["owned_items"] = []
            for r in i_rows:
                if len(r) >= 2:
                    sl = guid_to_slug.get(int(r[0]))
                    if sl:
                        telem[sl]["owned_items"].append(int(r[1]))
        except Exception as exc:
            errors.append(f"required_items:{type(exc).__name__}")
            logger.warning("Failed reading required raid items: %s", exc)

    # 6. Required attunement quests (if any)
    if spec.required_quests:
        try:
            q_csv = ",".join(str(int(q)) for q in spec.required_quests)
            q_rows = runner(
                f"SELECT guid, quest FROM character_queststatus_rewarded "
                f"WHERE guid IN ({glist}) AND quest IN ({q_csv});"
            )
            for t in telem.values():
                t["completed_quests"] = []
            for r in q_rows:
                if len(r) >= 2:
                    sl = guid_to_slug.get(int(r[0]))
                    if sl:
                        telem[sl]["completed_quests"].append(int(r[1]))
        except Exception as exc:
            errors.append(f"required_quests:{type(exc).__name__}")
            logger.warning("Failed reading required raid quests: %s", exc)

    return {
        "telemetry_by_slug": telem,
        "bank_pool": bank_pool,
        "guild_id": resolved_guild_id,
        "access_verified_against_db": access_verified_against_db,
        "db_legal_min_level": db_legal_min_level,
        "errors": errors,
    }
