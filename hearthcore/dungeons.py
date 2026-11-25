"""Phase II Verified Stock Dungeon Catalog & Role Eligibility (Card II-03).

All map IDs, instance scripts, and portal AreaTrigger IDs are verified directly against
the installed acore_world database (docs/PHASE_II_AUDIT_II00.md).
- Unsupported dungeons & all raids/heroics remain disabled (`supported=False`).
- Even supported pilot dungeons default OFF unless explicitly opted in via feature flag.
- Role viability checks actual spec/role capability (not merely character class).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DungeonSpec:
    dungeon_id: str
    name: str
    map_id: int
    difficulty: int               # 0 = normal 5-player
    areatrigger_id: int
    script: str
    entrance_map: int
    entrance_x: float
    entrance_y: float
    entrance_z: float
    min_level: int
    max_level: int
    recommended_min: int
    recommended_max: int
    raid_stage_min: int           # 0 = Classic (Lv 60 cap), 1 = TBC, 2 = WotLK
    faction: str                  # 'alliance' | 'both'
    estimated_minutes: int
    travel_minutes: int
    supported: bool               # audited & mechanics-safe for bot-only 5-player pilot
    pilot_default_enabled: bool   # ALWAYS False in Phase II default config!
    roles: tuple[tuple[str, int], ...] = (("tank", 1), ("healer", 1), ("damage", 3))


# Verified against installed acore_world.instance_template + areatrigger_teleport (2026-11-25)
DUNGEON_CATALOG: tuple[DungeonSpec, ...] = (
    DungeonSpec(
        dungeon_id="stockade_n",
        name="The Stockade",
        map_id=34,
        difficulty=0,
        areatrigger_id=101,
        script="instance_the_stockade",
        entrance_map=0,
        entrance_x=-8764.8,
        entrance_y=846.1,
        entrance_z=87.5,
        min_level=15,
        max_level=34,
        recommended_min=22,
        recommended_max=30,
        raid_stage_min=0,
        faction="alliance",
        estimated_minutes=30,
        travel_minutes=8,
        supported=True,
        pilot_default_enabled=False,
    ),
    DungeonSpec(
        dungeon_id="deadmines_n",
        name="The Deadmines",
        map_id=36,
        difficulty=0,
        areatrigger_id=78,
        script="instance_deadmines",
        entrance_map=0,
        entrance_x=-11208.3,
        entrance_y=1672.5,
        entrance_z=24.7,
        min_level=15,
        max_level=28,
        recommended_min=17,
        recommended_max=24,
        raid_stage_min=0,
        faction="both",
        estimated_minutes=45,
        travel_minutes=12,
        supported=True,
        pilot_default_enabled=False,
    ),
    # Cataloged but explicitly DISABLED until encounter & pathing audit completes:
    DungeonSpec(
        dungeon_id="sfk_n",
        name="Shadowfang Keep",
        map_id=33,
        difficulty=0,
        areatrigger_id=145,
        script="instance_shadowfang_keep",
        entrance_map=0,
        entrance_x=-232.8,
        entrance_y=1568.3,
        entrance_z=76.9,
        min_level=18,
        max_level=30,
        recommended_min=20,
        recommended_max=28,
        raid_stage_min=0,
        faction="both",
        estimated_minutes=45,
        travel_minutes=18,
        supported=False,
        pilot_default_enabled=False,
    ),
    DungeonSpec(
        dungeon_id="bfd_n",
        name="Blackfathom Deeps",
        map_id=48,
        difficulty=0,
        areatrigger_id=257,
        script="instance_blackfathom_deeps",
        entrance_map=1,
        entrance_x=4249.0,
        entrance_y=740.0,
        entrance_z=-25.0,
        min_level=20,
        max_level=32,
        recommended_min=22,
        recommended_max=30,
        raid_stage_min=0,
        faction="both",
        estimated_minutes=45,
        travel_minutes=18,
        supported=False,
        pilot_default_enabled=False,
    ),
    DungeonSpec(
        dungeon_id="gnomeregan_n",
        name="Gnomeregan",
        map_id=90,
        difficulty=0,
        areatrigger_id=324,
        script="instance_gnomeregan",
        entrance_map=0,
        entrance_x=-5163.3,
        entrance_y=927.6,
        entrance_z=257.2,
        min_level=24,
        max_level=38,
        recommended_min=26,
        recommended_max=35,
        raid_stage_min=0,
        faction="alliance",
        estimated_minutes=55,
        travel_minutes=12,
        supported=False,
        pilot_default_enabled=False,
    ),
)

DUNGEON_BY_ID: dict[str, DungeonSpec] = {d.dungeon_id: d for d in DUNGEON_CATALOG}
DUNGEON_BY_MAP: dict[int, DungeonSpec] = {d.map_id: d for d in DUNGEON_CATALOG}

# Verified spec -> role mapping (role capability depends on actual spec/role, not just class!)
TANK_SPECS = frozenset({"protection", "feral tank", "feral (tank)", "blood"})
HEALER_SPECS = frozenset({"holy", "discipline", "restoration"})


def enabled_dungeon_ids() -> set[str]:
    """Return explicitly enabled pilot dungeon IDs from HEARTHCORE_COOP_DUNGEONS (default empty = safe off)."""
    raw = (os.getenv("HEARTHCORE_COOP_DUNGEONS") or "").strip()
    if not raw:
        return set()
    wanted = {tok.strip().lower() for tok in raw.split(",") if tok.strip()}
    return {d.dungeon_id for d in DUNGEON_CATALOG if d.supported and d.dungeon_id in wanted}


def infer_viable_roles(char_profile: dict[str, Any], level: int) -> list[str]:
    """Determine viable dungeon roles from reviewed primary_role + primary_spec (not class alone)."""
    role = str(char_profile.get("primary_role") or "").strip().lower()
    spec = str(char_profile.get("primary_spec") or "").strip().lower()
    cls = str(char_profile.get("class") or "").strip().lower()

    roles: list[str] = []
    if role == "tank" or spec in TANK_SPECS:
        # Only tank-capable classes with tank spec/role
        if cls in ("warrior", "paladin", "druid", "death knight"):
            roles.append("tank")
    if role == "healer" or spec in HEALER_SPECS:
        if cls in ("priest", "paladin", "druid", "shaman"):
            roles.append("healer")
    if role in ("dps", "mdps", "rdps", "damage") or not roles:
        roles.append("damage")
    elif level < 25 and cls in ("warrior", "paladin", "druid", "shaman", "priest") and "damage" not in roles:
        # Hybrids in low-level dungeons can optionally flex DPS only if declared or low level
        pass
    return roles


def check_character_eligibility(
    *,
    level: int,
    faction: str,
    viable_roles: list[str],
    dungeon: DungeonSpec,
    raid_stage: int = 0,
    locked_maps: set[int] | None = None,
    available_minutes: int = 90,
    allow_pilot_shadow: bool = False,
) -> tuple[bool, str]:
    """Strictly validate whether one character is legally eligible for a dungeon opportunity."""
    if not dungeon.supported:
        return False, f"dungeon_unsupported:{dungeon.dungeon_id}"
    if not allow_pilot_shadow and dungeon.dungeon_id not in enabled_dungeon_ids():
        return False, f"dungeon_disabled:{dungeon.dungeon_id}"
    if dungeon.difficulty != 0:
        return False, "heroics_and_raids_disabled_in_phase2"
    if dungeon.faction != "both" and faction.lower() != dungeon.faction:
        return False, f"faction_mismatch:{faction}!={dungeon.faction}"
    if raid_stage < dungeon.raid_stage_min:
        return False, f"raid_gate_locked:stage_{raid_stage}<{dungeon.raid_stage_min}"
    if level < dungeon.min_level or level > dungeon.max_level:
        return False, f"level_out_of_range:{level}_not_in_{dungeon.min_level}..{dungeon.max_level}"
    if locked_maps and dungeon.map_id in locked_maps:
        return False, f"instance_locked:map_{dungeon.map_id}"
    if not viable_roles:
        return False, "no_viable_role"
    needed_mins = dungeon.estimated_minutes + dungeon.travel_minutes + 10  # 10m safety buffer
    if available_minutes < needed_mins:
        return False, f"insufficient_schedule_window:{available_minutes}m<{needed_mins}m"
    return True, "eligible"
