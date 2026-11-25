"""Phase II Raid Readiness Read Model & Resource Commitments (Card II-08).

Design guarantees (docs/PHASE_II_LIVING_GUILD.md Section 7):
- Readiness is a separate evidence-backed contract, NOT an activity preference or fake % score.
- Every component reports `pass` | `fail` | `unknown` | `not_applicable` with authoritative evidence.
  `unknown` NEVER counts as ready.
- Distinguishes `ready_available`, `ready_but_unavailable`, `not_ready`, and `unknown`.
- Distinct-persona roster solver prevents counting a persona's main + alt twice.
- Guild Bank supply reservations prevent double-counting the same consumables across raiders.
- Owner retains 100% authority over raid initiation, roster, and progression unlocks (zero auto-raids).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .core import profile, profiles
from .dungeons import infer_viable_roles
from .identity import persona_id_for_slug

# Stage-aware Classic (Lv 60) / TBC (Lv 70) / WotLK (Lv 80) entry raid targets
STAGE_RAID_SPECS: dict[int, dict[str, Any]] = {
    0: {
        "tier_name": "Classic 60 (Molten Core / Onyxia)",
        "min_level": 58,
        "target_level": 60,
        "roles": {"tank": 2, "healer": 5, "damage": 13},
        "consumable_entry": 13446,  # Major Healing Potion / stage placeholder
        "min_consumables_per_raider": 5,
    },
    1: {
        "tier_name": "TBC 70 (Karazhan / Gruul)",
        "min_level": 68,
        "target_level": 70,
        "roles": {"tank": 2, "healer": 3, "damage": 5},
        "consumable_entry": 22829,
        "min_consumables_per_raider": 5,
    },
    2: {
        "tier_name": "WotLK 80 (Naxxramas)",
        "min_level": 78,
        "target_level": 80,
        "roles": {"tank": 2, "healer": 5, "damage": 18},
        "consumable_entry": 33447,
        "min_consumables_per_raider": 5,
    },
}


def evaluate_character_readiness(
    comp: dict[str, Any],
    char_prof: dict[str, Any],
    *,
    raid_stage: int = 0,
    bank_allocatable_consumables: int = 0,
    personal_consumables: int | None = None,
    durability_pct: float | None = None,
    in_other_group: bool = False,
    recall_eta_min: int | None = None,
    observed_at: str = "",
) -> tuple[dict[str, Any], int]:
    """Evaluate one character's component-level raid readiness.

    Returns (readiness_record, bank_consumables_claimed) so callers subtract claimed shared bank
    supplies before evaluating the next raider (zero double-counting!).
    """
    spec = STAGE_RAID_SPECS.get(raid_stage, STAGE_RAID_SPECS[0])
    lvl = int(comp.get("level") or 0)
    roles = infer_viable_roles(char_prof, lvl)
    role = roles[0] if roles else "unknown"
    need_cons = int(spec["min_consumables_per_raider"])

    components: dict[str, dict[str, Any]] = {}

    # 1. Level component
    if lvl >= int(spec["min_level"]):
        components["level"] = {"state": "pass", "evidence": f"Lv {lvl} >= {spec['min_level']}"}
    else:
        components["level"] = {"state": "fail", "evidence": f"Lv {lvl} < {spec['min_level']} ({spec['target_level']} cap)"}

    # 2. Role viability
    if role in ("tank", "healer", "damage"):
        components["role_spec"] = {
            "state": "pass",
            "evidence": f"{role} ({char_prof.get('class')}/{char_prof.get('primary_spec')})",
        }
    else:
        components["role_spec"] = {"state": "unknown", "evidence": "No verified role/spec mapping"}

    # 3. Durability / repair (unknown if telemetry not provided — NEVER assume pass!)
    if durability_pct is None:
        components["durability"] = {"state": "unknown", "evidence": "Durability telemetry not sampled"}
    elif durability_pct >= 80.0:
        components["durability"] = {"state": "pass", "evidence": f"{durability_pct:.0f}% durability"}
    else:
        components["durability"] = {"state": "fail", "evidence": f"{durability_pct:.0f}% durability (<80%)"}

    # 4. Consumables (personal inventory first; reserve scarce bank stock without double-counting)
    claimed_from_bank = 0
    if personal_consumables is None:
        components["consumables"] = {"state": "unknown", "evidence": "Personal bag consumable count not sampled"}
    elif personal_consumables >= need_cons:
        components["consumables"] = {"state": "pass", "evidence": f"{personal_consumables} in bags"}
    else:
        short = need_cons - personal_consumables
        if bank_allocatable_consumables >= short:
            claimed_from_bank = short
            components["consumables"] = {
                "state": "pass",
                "evidence": f"{personal_consumables} in bags + {short} reserved from guild bank",
            }
        else:
            components["consumables"] = {
                "state": "fail",
                "evidence": f"{personal_consumables} in bags, bank pool {bank_allocatable_consumables} < {short} needed",
            }

    # 5. Availability / recall
    online = bool(comp.get("online"))
    if online and not in_other_group:
        components["availability"] = {"state": "pass", "evidence": f"Online in {comp.get('zone', '?')}"}
    elif online and in_other_group:
        eta = f" (~{recall_eta_min}m recall)" if recall_eta_min is not None else ""
        components["availability"] = {"state": "fail", "evidence": f"Committed in another group{eta}"}
    else:
        components["availability"] = {"state": "fail", "evidence": f"Offline (shift: {comp.get('shift_group', '?')})"}

    # Derive overall state honestly (unknown NEVER counts as ready!)
    core_keys = ("level", "role_spec", "durability", "consumables")
    core_states = [components[k]["state"] for k in core_keys]
    if any(s == "fail" for s in core_states):
        overall = "not_ready"
    elif any(s == "unknown" for s in core_states):
        overall = "unknown"
    elif components["availability"]["state"] == "pass":
        overall = "ready_available"
    else:
        overall = "ready_but_unavailable"

    rec = {
        "persona_id": str(comp.get("persona_id") or persona_id_for_slug(comp["slug"])),
        "character_guid": int(comp["guid"]),
        "name": comp["name"],
        "slug": comp["slug"],
        "level": lvl,
        "role": role,
        "shift_group": comp.get("shift_group", ""),
        "overall": overall,
        "observed_at": observed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "components": components,
    }
    return rec, claimed_from_bank


def evaluate_raid_roster(
    companions: dict[str, dict[str, Any]],
    *,
    raid_stage: int = 0,
    bank_consumable_pool: int = 0,
    telemetry_by_slug: dict[str, dict[str, Any]] | None = None,
    observed_at: str = "",
) -> dict[str, Any]:
    """Evaluate feasible distinct-persona raid roster & exact role shortfalls (zero auto-raids)."""
    spec = STAGE_RAID_SPECS.get(raid_stage, STAGE_RAID_SPECS[0])
    known = set(profiles())
    telem = telemetry_by_slug or {}
    remaining_bank = max(0, int(bank_consumable_pool))

    # Evaluate highest-level characters first so scarce bank reserves go to closest-to-ready raiders
    ordered = sorted(companions.values(), key=lambda c: (-int(c.get("level") or 0), str(c.get("slug") or "")))
    records: list[dict[str, Any]] = []
    seen_personas: set[str] = set()
    ready_by_role: dict[str, list[str]] = {"tank": [], "healer": [], "damage": []}
    ready_unavail_by_role: dict[str, list[str]] = {"tank": [], "healer": [], "damage": []}
    counts = {"ready_available": 0, "ready_but_unavailable": 0, "not_ready": 0, "unknown": 0}

    for comp in ordered:
        slug = str(comp["slug"])
        if slug not in known:
            continue
        pid = str(comp.get("persona_id") or persona_id_for_slug(slug))
        t = telem.get(slug) or {}
        rec, claimed = evaluate_character_readiness(
            comp,
            profile(slug),
            raid_stage=raid_stage,
            bank_allocatable_consumables=remaining_bank,
            personal_consumables=t.get("personal_consumables"),
            durability_pct=t.get("durability_pct"),
            in_other_group=bool(t.get("in_other_group")),
            recall_eta_min=t.get("recall_eta_min"),
            observed_at=observed_at,
        )
        remaining_bank = max(0, remaining_bank - claimed)
        records.append(rec)

        # Distinct-persona rule: never count two characters belonging to the same persona!
        if pid in seen_personas:
            continue
        seen_personas.add(pid)
        ov = rec["overall"]
        counts[ov] = counts.get(ov, 0) + 1
        r = rec["role"]
        if r in ready_by_role:
            if ov == "ready_available":
                ready_by_role[r].append(rec["name"])
            elif ov == "ready_but_unavailable":
                ready_unavail_by_role[r].append(rec["name"])

    role_req = spec["roles"]
    shortfalls: dict[str, int] = {}
    bottlenecks: list[str] = []
    for r, need in role_req.items():
        have = len(ready_by_role.get(r, []))
        short = max(0, int(need) - have)
        shortfalls[r] = short
        if short > 0:
            bottlenecks.append(f"{r}: {have}/{need} ready & available (short {short})")

    return {
        "observed_at": observed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "raid_stage": raid_stage,
        "tier_name": spec["tier_name"],
        "min_level": spec["min_level"],
        "distinct_personas_evaluated": len(seen_personas),
        "summary_counts": counts,
        "role_requirements": role_req,
        "ready_available_by_role": ready_by_role,
        "ready_but_unavailable_by_role": ready_unavail_by_role,
        "shortfalls": shortfalls,
        "bottlenecks": bottlenecks,
        "feasible_now": all(v == 0 for v in shortfalls.values()),
        "bank_consumables_unallocated": remaining_bank,
        "characters": records,
    }
