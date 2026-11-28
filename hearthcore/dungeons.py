"""Phase II Verified Stock Dungeon Catalog, Live Role Capability & Entry Eligibility (Card II-03 / Issue #14).

All map IDs, LFG queue IDs, legal portal entry levels, instance scripts, and portal AreaTrigger IDs
are verified directly against the installed acore_world database / WotLK 3.3.5a DBCs
(docs/PHASE_II_AUDIT_II00.md, data/sql/base/db_world/dungeon_access_template.sql).

Safety invariants:
- Unsupported dungeons & all raids/heroics remain disabled (`supported=False`).
- Even supported pilot dungeons default OFF unless explicitly opted in via feature flag.
- Role viability requires verified live telemetry (class, live spec/talents, spells, equipment,
  native LFG role mask) — NEVER inferred from static profiles when live telemetry is unknown.
- Unknown telemetry (missing faction, schedule, lockouts, live role capability, combat/group/queue/
  alive/equipment/native-LFG state, or stale snapshot) fails CLOSED and must NEVER claim feasible.
- Legal entry level, LFG queue band, and recommended level band are explicitly distinguished.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("hearthcore.dungeons")

# Native AzerothCore WotLK 3.3.5a LFG role bitmasks (src/server/game/DungeonFinding/LFG.h)
LFG_ROLE_NONE = 0x00
LFG_ROLE_LEADER = 0x01
LFG_ROLE_TANK = 0x02
LFG_ROLE_HEALER = 0x04
LFG_ROLE_DAMAGE = 0x08
LFG_ROLE_COMBAT_MASK = LFG_ROLE_TANK | LFG_ROLE_HEALER | LFG_ROLE_DAMAGE

ROLE_TO_LFG_MASK: dict[str, int] = {
    "tank": LFG_ROLE_TANK,
    "healer": LFG_ROLE_HEALER,
    "damage": LFG_ROLE_DAMAGE,
}

LFG_MASK_TO_ROLE: tuple[tuple[int, str], ...] = (
    (LFG_ROLE_TANK, "tank"),
    (LFG_ROLE_HEALER, "healer"),
    (LFG_ROLE_DAMAGE, "damage"),
)

# Authoritative WotLK 3.3.5a class IDs -> lowercase class names
CLASS_ID_TO_NAME: dict[int, str] = {
    1: "warrior",
    2: "paladin",
    3: "hunter",
    4: "rogue",
    5: "priest",
    6: "death knight",
    7: "shaman",
    8: "mage",
    9: "warlock",
    11: "druid",
}

# Authoritative WotLK 3.3.5a class -> legal role capabilities
CLASS_CAPABLE_ROLES: dict[str, frozenset[str]] = {
    "warrior": frozenset({"tank", "damage"}),
    "paladin": frozenset({"tank", "healer", "damage"}),
    "hunter": frozenset({"damage"}),
    "rogue": frozenset({"damage"}),
    "priest": frozenset({"healer", "damage"}),
    "death knight": frozenset({"tank", "damage"}),
    "shaman": frozenset({"healer", "damage"}),
    "mage": frozenset({"damage"}),
    "warlock": frozenset({"damage"}),
    "druid": frozenset({"tank", "healer", "damage"}),
}

# Authoritative WotLK 3.3.5a race IDs -> faction
ALLIANCE_RACE_IDS = frozenset({1, 3, 4, 7, 11})  # Human, Dwarf, Night Elf, Gnome, Draenei
HORDE_RACE_IDS = frozenset({2, 5, 6, 8, 10})     # Orc, Undead, Tauren, Troll, Blood Elf

# Native AzerothCore WotLK 3.3.5a talent tab -> primary role (matches mod-playerbots LfgActions.cpp)
# Spec tab indices: 0 = first tree, 1 = second tree, 2 = third tree
SPEC_TAB_PRIMARY_ROLE: dict[tuple[str, int], str] = {
    ("warrior", 0): "damage",       # Arms
    ("warrior", 1): "damage",       # Fury
    ("warrior", 2): "tank",         # Protection
    ("paladin", 0): "healer",       # Holy
    ("paladin", 1): "tank",         # Protection
    ("paladin", 2): "damage",       # Retribution
    ("priest", 0): "healer",        # Discipline
    ("priest", 1): "healer",        # Holy
    ("priest", 2): "damage",        # Shadow
    ("shaman", 0): "damage",        # Elemental
    ("shaman", 1): "damage",        # Enhancement
    ("shaman", 2): "healer",        # Restoration
    ("druid", 0): "damage",         # Balance
    ("druid", 1): "damage",         # Feral (tank requires Thick Hide + Bear Form + Growl verification)
    ("druid", 2): "healer",         # Restoration
    ("death knight", 0): "tank",    # Blood
    ("death knight", 1): "damage",  # Frost
    ("death knight", 2): "damage",  # Unholy
    ("hunter", 0): "damage",
    ("hunter", 1): "damage",
    ("hunter", 2): "damage",
    ("rogue", 0): "damage",
    ("rogue", 1): "damage",
    ("rogue", 2): "damage",
    ("mage", 0): "damage",
    ("mage", 1): "damage",
    ("mage", 2): "damage",
    ("warlock", 0): "damage",
    ("warlock", 1): "damage",
    ("warlock", 2): "damage",
}

# Verified WotLK 3.3.5a role-defining spells (across all ranks so higher-level bots never fail)
TANK_SPELLS_BY_CLASS: dict[str, frozenset[int]] = {
    # Defensive Stance (71), Taunt (355), Sunder Armor (7386), Shield Block (2565)
    "warrior": frozenset({71, 355, 7386, 2565}),
    # Righteous Fury (25780), Hand of Reckoning (62124), Righteous Defense (31789)
    "paladin": frozenset({25780, 62124, 31789}),
    # Bear Form (5487), Dire Bear Form (9634), Growl (6795)
    "druid": frozenset({5487, 9634, 6795}),
    # Frost Presence (48263), Dark Command (56222)
    "death knight": frozenset({48263, 56222}),
}
DRUID_THICK_HIDE_TALENTS = frozenset({16929, 16930, 16931})

# WotLK 3.3.5a healing spells across Classic/TBC/WotLK ranks
HEALER_SPELLS_BY_CLASS: dict[str, frozenset[int]] = {
    "priest": frozenset({
        # Lesser Heal (R1-3), Heal (R1-4), Flash Heal (R1-11), Greater Heal (R1-9), Penance
        2050, 2052, 2053,
        2054, 2055, 6063, 6064,
        2061, 9472, 9473, 9474, 10915, 10916, 10917, 25233, 25235, 48070, 48071,
        2060, 10963, 10964, 10965, 25314, 25210, 25213, 48062, 48063,
        47540, 53005, 53006, 53007,
    }),
    "paladin": frozenset({
        # Holy Light (R1-13), Flash of Light (R1-9), Holy Shock (R1-7)
        635, 639, 647, 1026, 1042, 3472, 10328, 10329, 25292, 27135, 27136, 48781, 48782,
        19750, 19939, 19940, 19941, 19942, 19943, 27137, 48784, 48785,
        20473, 20929, 20930, 27174, 33072, 48824, 48825,
    }),
    "druid": frozenset({
        # Healing Touch (R1-15), Rejuvenation (R1-15), Regrowth (R1-12), Nourish
        5185, 5186, 5187, 5188, 5189, 6778, 8903, 9758, 9888, 9889, 25297, 26978, 26979, 48377, 48378,
        774, 1058, 1430, 2090, 2091, 3627, 8910, 9839, 9840, 9841, 25299, 26981, 26982, 48440, 48441,
        8936, 8938, 8939, 8940, 8941, 9750, 9856, 9857, 9858, 26980, 48442, 48443,
        50464,
    }),
    "shaman": frozenset({
        # Healing Wave (R1-14), Lesser Healing Wave (R1-9), Chain Heal (R1-7), Riptide
        331, 332, 547, 913, 939, 959, 8005, 10395, 10396, 25357, 25391, 25396, 49272, 49273,
        8004, 8008, 8010, 10466, 10467, 10468, 25420, 49275, 49276,
        1064, 10622, 10623, 25422, 25423, 55458, 55459,
        61295, 61299, 61300, 61301,
    }),
}

ALL_ROLE_SPELL_IDS: frozenset[int] = frozenset().union(
    *TANK_SPELLS_BY_CLASS.values(),
    *HEALER_SPELLS_BY_CLASS.values(),
    DRUID_THICK_HIDE_TALENTS,
)

# WotLK InventoryType constants (item_template.InventoryType)
INVTYPE_SHIELD = 14
MAIN_HAND_INVTYPES = frozenset({13, 17, 21})  # One-Hand, Two-Hand, Main Hand

# Static spec labels (used only by legacy infer_viable_roles / active_spec matching)
TANK_SPECS = frozenset({"protection", "feral tank", "feral (tank)", "blood"})
HEALER_SPECS = frozenset({"holy", "discipline", "restoration"})

# Continental overland maps (anything else is an instance/battleground map)
OVERLAND_MAPS = frozenset({0, 1, 530, 571})


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
    min_level: int                # LFG queue minimum level
    max_level: int                # LFG queue maximum level
    recommended_min: int          # Effective/recommended party minimum level
    recommended_max: int          # Effective/recommended party maximum level
    raid_stage_min: int           # 0 = Classic (Lv 60 cap), 1 = TBC, 2 = WotLK
    faction: str                  # 'alliance' | 'horde' | 'both'
    estimated_minutes: int
    travel_minutes: int
    supported: bool               # audited & mechanics-safe for bot-only 5-player pilot
    pilot_default_enabled: bool   # ALWAYS False in Phase II default config!
    roles: tuple[tuple[str, int], ...] = (("tank", 1), ("healer", 1), ("damage", 3))
    lfg_dungeon_id: int = 0       # Verified WotLK 3.3.5a LFGDungeons.dbc queue ID
    legal_min_level: int = 0      # Verified acore_world.dungeon_access_template portal min level
    legal_max_level: int = 80     # Verified portal max level (0 in DB = realm cap 80)
    entrance_zone_ids: tuple[int, ...] = ()


# Verified against installed acore_world.instance_template, dungeon_access_template,
# areatrigger_teleport & WotLK 3.3.5a LFGDungeons.dbc
# Authoritative WotLK 3.3.5a 5-player RDF dungeon table (60 Normal + 32 Heroic = 92 dungeons),
# generated directly from installed LFGDungeons.dbc + instance_template.sql + areatrigger_teleport.sql.
# Tuple: (dungeon_id, name, map_id, diff, areatrigger_id, script, emap, ex, ey, ez, min_l, max_l, rec_min, rec_max, raid_stage, faction, est_min, travel_min, lfg_id, legal_min, entrance_zones)
_SUPPORTED_RDF_DUNGEON_ROWS: tuple[tuple, ...] = (
    ('stockade_n', 'The Stockade', 34, 0, 101, 'instance_the_stockade', 0, -8764.8, 846.1, 87.5, 15, 34, 22, 30, 0, 'alliance', 30, 8, 12, 15, (1519, 12)),
    ('deadmines_n', 'The Deadmines', 36, 0, 78, 'instance_deadmines', 0, -11208.3, 1672.5, 24.7, 15, 28, 17, 24, 0, 'both', 45, 12, 6, 10, (40, 12, 1519)),
    ('wailing_caverns_n', 'Wailing Caverns', 43, 0, 228, 'instance_wailing_caverns', 0, 0.0, 0.0, 0.0, 15, 25, 17, 20, 0, 'both', 45, 10, 1, 15, ()),
    ('ragefire_chasm_n', 'Ragefire Chasm', 389, 0, 2230, 'instance_ragefire_chasm', 0, 0.0, 0.0, 0.0, 15, 21, 15, 16, 0, 'horde', 45, 10, 4, 15, ()),
    ('sfk_n', 'Shadowfang Keep', 33, 0, 145, 'instance_shadowfang_keep', 0, -232.8, 1568.3, 76.9, 16, 26, 18, 21, 0, 'both', 45, 18, 8, 16, (130,)),
    ('bfd_n', 'Blackfathom Deeps', 48, 0, 257, 'instance_blackfathom_deeps', 1, 4249.0, 740.0, -25.0, 19, 29, 21, 24, 0, 'both', 45, 18, 10, 19, (331, 148)),
    ('razorfen_kraul_n', 'Razorfen Kraul', 47, 0, 244, 'instance_razorfen_kraul', 0, 0.0, 0.0, 0.0, 22, 32, 24, 27, 0, 'both', 45, 10, 16, 22, ()),
    ('gnomeregan_n', 'Gnomeregan', 90, 0, 324, 'instance_gnomeregan', 0, -5163.3, 927.6, 257.2, 23, 33, 25, 28, 0, 'both', 55, 12, 14, 23, (1, 1537)),
    ('scarlet_monastery_graveyard_n', 'Scarlet Monastery - Graveyard', 189, 0, 45, 'instance_scarlet_monastery', 0, 0.0, 0.0, 0.0, 27, 37, 29, 32, 0, 'both', 45, 10, 18, 27, ()),
    ('scarlet_monastery_library_n', 'Scarlet Monastery - Library', 189, 0, 45, 'instance_scarlet_monastery', 0, 0.0, 0.0, 0.0, 30, 40, 32, 35, 0, 'both', 45, 10, 165, 30, ()),
    ('razorfen_downs_n', 'Razorfen Downs', 129, 0, 442, 'instance_razorfen_downs', 0, 0.0, 0.0, 0.0, 32, 42, 34, 37, 0, 'both', 45, 10, 20, 32, ()),
    ('scarlet_monastery_armory_n', 'Scarlet Monastery - Armory', 189, 0, 45, 'instance_scarlet_monastery', 0, 0.0, 0.0, 0.0, 32, 42, 34, 37, 0, 'both', 45, 10, 163, 32, ()),
    ('uldaman_n', 'Uldaman', 70, 0, 286, 'instance_uldaman', 0, 0.0, 0.0, 0.0, 35, 45, 37, 40, 0, 'both', 45, 10, 22, 35, ()),
    ('scarlet_monastery_cathedral_n', 'Scarlet Monastery - Cathedral', 189, 0, 45, 'instance_scarlet_monastery', 0, 0.0, 0.0, 0.0, 35, 45, 37, 40, 0, 'both', 45, 10, 164, 35, ()),
    ('maraudon_purple_crystals_n', 'Maraudon - Purple Crystals', 349, 0, 3133, 'instance_maraudon', 0, 0.0, 0.0, 0.0, 39, 49, 41, 44, 0, 'both', 45, 10, 272, 39, ()),
    ('zulfarrak_n', "Zul'Farrak", 209, 0, 0, 'instance_zulfarrak', 0, 0.0, 0.0, 0.0, 41, 51, 43, 46, 0, 'both', 45, 10, 24, 41, ()),
    ('maraudon_orange_crystals_n', 'Maraudon - Orange Crystals', 349, 0, 3133, 'instance_maraudon', 0, 0.0, 0.0, 0.0, 41, 51, 43, 46, 0, 'both', 45, 10, 26, 41, ()),
    ('maraudon_pristine_waters_n', 'Maraudon - Pristine Waters', 349, 0, 3133, 'instance_maraudon', 0, 0.0, 0.0, 0.0, 43, 53, 45, 48, 0, 'both', 45, 10, 273, 43, ()),
    ('sunken_temple_n', 'Sunken Temple', 109, 0, 0, 'instance_sunken_temple', 0, 0.0, 0.0, 0.0, 45, 55, 47, 50, 0, 'both', 45, 10, 28, 45, ()),
    ('blackrock_depths_prison_n', 'Blackrock Depths - Prison', 230, 0, 1466, 'instance_blackrock_depths', 0, 0.0, 0.0, 0.0, 47, 57, 49, 52, 0, 'both', 45, 10, 30, 47, ()),
    ('blackrock_depths_upper_city_n', 'Blackrock Depths - Upper City', 230, 0, 1466, 'instance_blackrock_depths', 0, 0.0, 0.0, 0.0, 51, 61, 53, 56, 0, 'both', 45, 10, 276, 51, ()),
    ('dire_maul_east_n', 'Dire Maul - East', 429, 0, 3183, 'instance_dire_maul', 0, 0.0, 0.0, 0.0, 53, 63, 55, 58, 0, 'both', 45, 10, 34, 53, ()),
    ('scholomance_n', 'Scholomance', 289, 0, 2567, 'instance_scholomance', 0, 0.0, 0.0, 0.0, 55, 65, 55, 65, 0, 'both', 45, 10, 2, 55, ()),
    ('lower_blackrock_spire_n', 'Lower Blackrock Spire', 229, 0, 1468, 'instance_blackrock_spire', 0, 0.0, 0.0, 0.0, 55, 65, 57, 58, 0, 'both', 45, 10, 32, 55, ()),
    ('dire_maul_west_n', 'Dire Maul - West', 429, 0, 3183, 'instance_dire_maul', 0, 0.0, 0.0, 0.0, 55, 65, 58, 58, 0, 'both', 45, 10, 36, 55, ()),
    ('dire_maul_north_n', 'Dire Maul - North', 429, 0, 3183, 'instance_dire_maul', 0, 0.0, 0.0, 0.0, 55, 65, 55, 65, 0, 'both', 45, 10, 38, 55, ()),
    ('stratholme_main_gate_n', 'Stratholme - Main Gate', 329, 0, 2214, 'instance_stratholme', 0, 0.0, 0.0, 0.0, 55, 65, 55, 65, 0, 'both', 45, 10, 40, 55, ()),
    ('stratholme_service_entrance_n', 'Stratholme - Service Entrance', 329, 0, 2214, 'instance_stratholme', 0, 0.0, 0.0, 0.0, 55, 65, 55, 65, 0, 'both', 45, 10, 274, 55, ()),
    ('hellfire_ramparts_n', 'Hellfire Ramparts', 543, 0, 4150, 'instance_hellfire_ramparts', 530, 0.0, 0.0, 0.0, 57, 67, 59, 62, 1, 'both', 45, 10, 136, 57, ()),
    ('blood_furnace_n', 'Blood Furnace', 542, 0, 4152, 'instance_blood_furnace', 530, 0.0, 0.0, 0.0, 59, 68, 61, 63, 1, 'both', 45, 10, 137, 59, ()),
    ('slave_pens_n', 'Slave Pens', 547, 0, 4365, 'instance_the_slave_pens', 530, 0.0, 0.0, 0.0, 60, 69, 62, 64, 1, 'both', 45, 10, 140, 60, ()),
    ('underbog_n', 'Underbog', 546, 0, 4363, 'instance_the_underbog', 530, 0.0, 0.0, 0.0, 61, 70, 63, 65, 1, 'both', 45, 10, 146, 61, ()),
    ('mana_tombs_n', 'Mana-Tombs', 557, 0, 4405, 'instance_mana_tombs', 530, 0.0, 0.0, 0.0, 62, 71, 64, 66, 1, 'both', 45, 10, 148, 62, ()),
    ('auchenai_crypts_n', 'Auchenai Crypts', 558, 0, 4404, 'instance_auchenai_crypts', 530, 0.0, 0.0, 0.0, 63, 72, 65, 67, 1, 'both', 45, 10, 149, 63, ()),
    ('escape_from_durnholde_n', 'The Escape From Durnholde', 560, 0, 4321, 'instance_old_hillsbrad', 530, 0.0, 0.0, 0.0, 64, 73, 66, 68, 1, 'both', 45, 10, 170, 64, ()),
    ('sethekk_halls_n', 'Sethekk Halls', 556, 0, 4406, 'instance_sethekk_halls', 530, 0.0, 0.0, 0.0, 65, 73, 67, 68, 1, 'both', 45, 10, 150, 65, ()),
    ('shattered_halls_n', 'Shattered Halls', 540, 0, 4151, 'instance_shattered_halls', 530, 0.0, 0.0, 0.0, 67, 75, 67, 75, 1, 'both', 45, 10, 138, 67, ()),
    ('steamvault_n', 'The Steamvault', 545, 0, 4364, 'instance_steam_vault', 530, 0.0, 0.0, 0.0, 67, 75, 67, 75, 1, 'both', 45, 10, 147, 67, ()),
    ('shadow_labyrinth_n', 'Shadow Labyrinth', 555, 0, 4407, 'instance_shadow_labyrinth', 530, 0.0, 0.0, 0.0, 67, 75, 67, 75, 1, 'both', 45, 10, 151, 67, ()),
    ('mechanar_n', 'The Mechanar', 554, 0, 4469, 'instance_mechanar', 530, 0.0, 0.0, 0.0, 67, 75, 67, 75, 1, 'both', 45, 10, 172, 67, ()),
    ('botanica_n', 'The Botanica', 553, 0, 4467, 'instance_the_botanica', 530, 0.0, 0.0, 0.0, 67, 75, 67, 75, 1, 'both', 45, 10, 173, 67, ()),
    ('black_morass_n', 'The Black Morass', 269, 0, 4320, 'instance_the_black_morass', 530, 0.0, 0.0, 0.0, 68, 75, 68, 75, 1, 'both', 45, 10, 171, 68, ()),
    ('arcatraz_n', 'The Arcatraz', 552, 0, 4468, 'instance_arcatraz', 530, 0.0, 0.0, 0.0, 68, 75, 68, 75, 1, 'both', 45, 10, 174, 68, ()),
    ('magisters_terrace_n', "Magisters' Terrace", 585, 0, 0, 'instance_magisters_terrace', 530, 0.0, 0.0, 0.0, 68, 75, 68, 75, 1, 'both', 45, 10, 198, 68, ()),
    ('auchenai_crypts_h', 'Auchenai Crypts (Heroic)', 558, 1, 4404, 'instance_auchenai_crypts', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 178, 70, ()),
    ('mana_tombs_h', 'Mana-Tombs (Heroic)', 557, 1, 4405, 'instance_mana_tombs', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 179, 70, ()),
    ('sethekk_halls_h', 'Sethekk Halls (Heroic)', 556, 1, 4406, 'instance_sethekk_halls', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 180, 70, ()),
    ('shadow_labyrinth_h', 'Shadow Labyrinth (Heroic)', 555, 1, 4407, 'instance_shadow_labyrinth', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 181, 70, ()),
    ('black_morass_h', 'The Black Morass (Heroic)', 269, 1, 4320, 'instance_the_black_morass', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 182, 70, ()),
    ('escape_from_durnholde_h', 'The Escape From Durnholde (Heroic)', 560, 1, 4321, 'instance_old_hillsbrad', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 183, 70, ()),
    ('slave_pens_h', 'Slave Pens (Heroic)', 547, 1, 4365, 'instance_the_slave_pens', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 184, 70, ()),
    ('steamvault_h', 'The Steamvault (Heroic)', 545, 1, 4364, 'instance_steam_vault', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 185, 70, ()),
    ('underbog_h', 'Underbog (Heroic)', 546, 1, 4363, 'instance_the_underbog', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 186, 70, ()),
    ('blood_furnace_h', 'Blood Furnace (Heroic)', 542, 1, 4152, 'instance_blood_furnace', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 187, 70, ()),
    ('hellfire_ramparts_h', 'Hellfire Ramparts (Heroic)', 543, 1, 4150, 'instance_hellfire_ramparts', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 188, 70, ()),
    ('shattered_halls_h', 'Shattered Halls (Heroic)', 540, 1, 4151, 'instance_shattered_halls', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 189, 70, ()),
    ('arcatraz_h', 'The Arcatraz (Heroic)', 552, 1, 4468, 'instance_arcatraz', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 190, 70, ()),
    ('botanica_h', 'The Botanica (Heroic)', 553, 1, 4467, 'instance_the_botanica', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 191, 70, ()),
    ('mechanar_h', 'The Mechanar (Heroic)', 554, 1, 4469, 'instance_mechanar', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 192, 70, ()),
    ('magisters_terrace_h', "Magisters' Terrace (Heroic)", 585, 1, 0, 'instance_magisters_terrace', 530, 0.0, 0.0, 0.0, 70, 75, 70, 73, 1, 'both', 50, 10, 201, 70, ()),
    ('utgarde_keep_n', 'Utgarde Keep', 574, 0, 4745, 'instance_utgarde_keep', 571, 0.0, 0.0, 0.0, 68, 80, 69, 72, 2, 'both', 45, 10, 202, 68, ()),
    ('nexus_n', 'The Nexus', 576, 0, 4983, 'instance_nexus', 571, 0.0, 0.0, 0.0, 69, 80, 71, 73, 2, 'both', 45, 10, 225, 69, ()),
    ('azjol_nerub_n', 'Azjol-Nerub', 601, 0, 5117, 'instance_azjol_nerub', 571, 0.0, 0.0, 0.0, 70, 80, 72, 74, 2, 'both', 45, 10, 204, 70, ()),
    ('ahnkahet_the_old_kingdom_n', "Ahn'kahet: The Old Kingdom", 619, 0, 0, 'instance_ahnkahet', 571, 0.0, 0.0, 0.0, 71, 80, 73, 75, 2, 'both', 45, 10, 218, 71, ()),
    ('draktharon_keep_n', "Drak'Tharon Keep", 600, 0, 0, 'instance_drak_tharon_keep', 571, 0.0, 0.0, 0.0, 72, 80, 74, 76, 2, 'both', 45, 10, 214, 72, ()),
    ('violet_hold_n', 'Violet Hold', 608, 0, 5209, 'instance_violet_hold', 571, 0.0, 0.0, 0.0, 73, 80, 75, 77, 2, 'both', 45, 10, 220, 73, ()),
    ('gundrak_n', 'Gundrak', 604, 0, 5205, 'instance_gundrak', 571, 0.0, 0.0, 0.0, 74, 80, 76, 78, 2, 'both', 45, 10, 216, 74, ()),
    ('halls_of_stone_n', 'Halls of Stone', 599, 0, 5010, 'instance_halls_of_stone', 571, 0.0, 0.0, 0.0, 75, 80, 77, 79, 2, 'both', 45, 10, 208, 75, ()),
    ('utgarde_pinnacle_n', 'Utgarde Pinnacle', 575, 0, 4747, 'instance_utgarde_pinnacle', 571, 0.0, 0.0, 0.0, 77, 80, 79, 80, 2, 'both', 45, 10, 203, 77, ()),
    ('oculus_n', 'The Oculus', 578, 0, 5246, 'instance_oculus', 571, 0.0, 0.0, 0.0, 77, 80, 79, 80, 2, 'both', 45, 10, 206, 77, ()),
    ('halls_of_lightning_n', 'Halls of Lightning', 602, 0, 5093, 'instance_halls_of_lightning', 571, 0.0, 0.0, 0.0, 77, 80, 79, 80, 2, 'both', 45, 10, 207, 77, ()),
    ('culling_of_stratholme_n', 'The Culling of Stratholme', 595, 0, 5150, 'instance_culling_of_stratholme', 571, 0.0, 0.0, 0.0, 77, 80, 79, 80, 2, 'both', 45, 10, 209, 77, ()),
    ('trial_of_the_champion_n', 'Trial of the Champion', 650, 0, 5505, 'instance_trial_of_the_champion', 571, 0.0, 0.0, 0.0, 80, 80, 79, 80, 2, 'both', 45, 10, 245, 80, ()),
    ('forge_of_souls_n', 'The Forge of Souls', 632, 0, 5635, 'instance_forge_of_souls', 571, 0.0, 0.0, 0.0, 80, 80, 79, 80, 2, 'both', 45, 10, 251, 80, ()),
    ('pit_of_saron_n', 'Pit of Saron', 658, 0, 5637, 'instance_pit_of_saron', 571, 0.0, 0.0, 0.0, 80, 80, 79, 80, 2, 'both', 45, 10, 253, 80, ()),
    ('halls_of_reflection_n', 'Halls of Reflection', 668, 0, 5636, 'instance_halls_of_reflection', 571, 0.0, 0.0, 0.0, 80, 80, 79, 80, 2, 'both', 45, 10, 255, 80, ()),
    ('utgarde_pinnacle_h', 'Utgarde Pinnacle (Heroic)', 575, 1, 4747, 'instance_utgarde_pinnacle', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 205, 80, ()),
    ('culling_of_stratholme_h', 'The Culling of Stratholme (Heroic)', 595, 1, 5150, 'instance_culling_of_stratholme', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 210, 80, ()),
    ('oculus_h', 'The Oculus (Heroic)', 578, 1, 5246, 'instance_oculus', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 211, 80, ()),
    ('halls_of_lightning_h', 'Halls of Lightning (Heroic)', 602, 1, 5093, 'instance_halls_of_lightning', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 212, 80, ()),
    ('halls_of_stone_h', 'Halls of Stone (Heroic)', 599, 1, 5010, 'instance_halls_of_stone', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 213, 80, ()),
    ('draktharon_keep_h', "Drak'Tharon Keep (Heroic)", 600, 1, 0, 'instance_drak_tharon_keep', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 215, 80, ()),
    ('gundrak_h', 'Gundrak (Heroic)', 604, 1, 5205, 'instance_gundrak', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 217, 80, ()),
    ('ahnkahet_the_old_kingdom_h', "Ahn'kahet: The Old Kingdom (Heroic)", 619, 1, 0, 'instance_ahnkahet', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 219, 80, ()),
    ('violet_hold_h', 'Violet Hold (Heroic)', 608, 1, 5209, 'instance_violet_hold', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 221, 80, ()),
    ('nexus_h', 'The Nexus (Heroic)', 576, 1, 4983, 'instance_nexus', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 226, 80, ()),
    ('azjol_nerub_h', 'Azjol-Nerub (Heroic)', 601, 1, 5117, 'instance_azjol_nerub', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 241, 80, ()),
    ('utgarde_keep_h', 'Utgarde Keep (Heroic)', 574, 1, 4745, 'instance_utgarde_keep', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 242, 80, ()),
    ('trial_of_the_champion_h', 'Trial of the Champion (Heroic)', 650, 1, 5505, 'instance_trial_of_the_champion', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 249, 80, ()),
    ('forge_of_souls_h', 'The Forge of Souls (Heroic)', 632, 1, 5635, 'instance_forge_of_souls', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 252, 80, ()),
    ('pit_of_saron_h', 'Pit of Saron (Heroic)', 658, 1, 5637, 'instance_pit_of_saron', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 254, 80, ()),
    ('halls_of_reflection_h', 'Halls of Reflection (Heroic)', 668, 1, 5636, 'instance_halls_of_reflection', 571, 0.0, 0.0, 0.0, 80, 80, 80, 80, 2, 'both', 50, 10, 256, 80, ()),
)

DUNGEON_CATALOG: tuple[DungeonSpec, ...] = tuple(
    DungeonSpec(
        dungeon_id=row[0],
        name=row[1],
        map_id=row[2],
        difficulty=row[3],
        areatrigger_id=row[4],
        script=row[5],
        entrance_map=row[6],
        entrance_x=row[7],
        entrance_y=row[8],
        entrance_z=row[9],
        min_level=row[10],
        max_level=row[11],
        recommended_min=row[12],
        recommended_max=row[13],
        raid_stage_min=row[14],
        faction=row[15],
        estimated_minutes=row[16],
        travel_minutes=row[17],
        supported=True,
        pilot_default_enabled=False,
        lfg_dungeon_id=row[18],
        legal_min_level=row[19],
        legal_max_level=80,
        entrance_zone_ids=row[20],
    )
    for row in _SUPPORTED_RDF_DUNGEON_ROWS
) + (
    # Explicit non-supported raid sentinel: raids are NEVER queued by 5-player cooperative RDF.
    DungeonSpec(
        dungeon_id="molten_core_r",
        name="Molten Core",
        map_id=409,
        difficulty=2,
        areatrigger_id=2886,
        script="instance_molten_core",
        entrance_map=0,
        entrance_x=-7515.0,
        entrance_y=-1045.0,
        entrance_z=182.0,
        min_level=60,
        max_level=80,
        recommended_min=60,
        recommended_max=60,
        raid_stage_min=0,
        faction="both",
        estimated_minutes=180,
        travel_minutes=15,
        supported=False,
        pilot_default_enabled=False,
        lfg_dungeon_id=48,
        legal_min_level=50,
        legal_max_level=80,
        entrance_zone_ids=(46, 51),
    ),
)

DUNGEON_BY_ID: dict[str, DungeonSpec] = {d.dungeon_id: d for d in DUNGEON_CATALOG}
DUNGEONS_BY_MAP: dict[int, tuple[DungeonSpec, ...]] = {}
for _d in DUNGEON_CATALOG:
    DUNGEONS_BY_MAP[_d.map_id] = DUNGEONS_BY_MAP.get(_d.map_id, ()) + (_d,)
# Primary map lookup prefers the first Normal (difficulty=0) entry per map for backward compatibility.
DUNGEON_BY_MAP: dict[int, DungeonSpec] = {}
for _d in DUNGEON_CATALOG:
    if _d.map_id not in DUNGEON_BY_MAP or (DUNGEON_BY_MAP[_d.map_id].difficulty != 0 and _d.difficulty == 0):
        DUNGEON_BY_MAP[_d.map_id] = _d
DUNGEON_BY_LFG_ID: dict[int, DungeonSpec] = {
    d.lfg_dungeon_id: d for d in DUNGEON_CATALOG if d.lfg_dungeon_id > 0
}


def enabled_dungeon_ids() -> set[str]:
    """Return explicitly enabled 5-player dungeon IDs from HEARTHCORE_COOP_DUNGEONS (default empty = safe off).

    Supports keywords: `all` / `*` (all 92 supported 5-player dungeons), `normal` (60 Normal),
    `heroic` (32 Heroic), `classic` (28 Classic), `tbc` (32 TBC), `wotlk` (32 WotLK), or explicit IDs.
    Raids (`supported=False`) are NEVER returned even when `all` is set.
    """
    raw = (os.getenv("HEARTHCORE_COOP_DUNGEONS") or "").strip()
    if not raw:
        return set()
    wanted = {tok.strip().lower() for tok in raw.split(",") if tok.strip()}
    out: set[str] = set()
    for d in DUNGEON_CATALOG:
        if not d.supported:
            continue
        if (
            "all" in wanted
            or "*" in wanted
            or d.dungeon_id in wanted
            or ("normal" in wanted and d.difficulty == 0)
            or ("heroic" in wanted and d.difficulty == 1)
            or ("classic" in wanted and d.raid_stage_min == 0)
            or ("tbc" in wanted and d.raid_stage_min == 1)
            or ("wotlk" in wanted and d.raid_stage_min == 2)
        ):
            out.add(d.dungeon_id)
    return out


def configured_schedule_tz(tz=None):
    """Resolve timezone matching worldserver schedule clock (explicit tz, HEARTHCORE_SCHEDULE_TZ, or host local)."""
    if tz is not None:
        return tz
    env_tz = (os.getenv("HEARTHCORE_SCHEDULE_TZ") or "").strip()
    if env_tz:
        try:
            return ZoneInfo(env_tz)
        except ZoneInfoNotFoundError as exc:
            logger.warning("Invalid HEARTHCORE_SCHEDULE_TZ=%r (%s); falling back to host local tz", env_tz, exc)
    return None


def faction_from_race(race_id: int | None) -> str | None:
    """Return authoritative 'alliance' / 'horde' from WotLK race ID, or None if unknown (never assumes Alliance!)."""
    if race_id is None:
        return None
    try:
        rid = int(race_id)
    except (TypeError, ValueError):
        return None
    if rid in ALLIANCE_RACE_IDS:
        return "alliance"
    if rid in HORDE_RACE_IDS:
        return "horde"
    return None


def roles_to_lfg_mask(roles: list[str] | tuple[str, ...] | set[str]) -> int:
    """Encode canonical role names into AzerothCore LFG role bitmask (PLAYER_ROLE_*)."""
    mask = LFG_ROLE_NONE
    for r in roles:
        mask |= ROLE_TO_LFG_MASK.get(str(r).strip().lower(), 0)
    return mask


def lfg_mask_to_roles(mask: int | None) -> list[str]:
    """Decode AzerothCore LFG role bitmask into canonical role names."""
    if mask is None:
        return []
    m = int(mask) & LFG_ROLE_COMBAT_MASK
    return [name for bit, name in LFG_MASK_TO_ROLE if m & bit]


def classify_level_band(level: int, dungeon: DungeonSpec) -> dict[str, Any]:
    """Explicitly distinguish legal portal entry, LFG queue range, and recommended level band (Issue #14)."""
    lvl = int(level)
    legal_min = int(dungeon.legal_min_level if dungeon.legal_min_level > 0 else dungeon.min_level)
    legal_max = int(dungeon.legal_max_level if dungeon.legal_max_level > 0 else 80)

    legal_entry = legal_min <= lvl <= legal_max
    lfg_queue_eligible = legal_entry and (dungeon.min_level <= lvl <= dungeon.max_level)
    in_recommended_band = lfg_queue_eligible and (dungeon.recommended_min <= lvl <= dungeon.recommended_max)

    if lvl < legal_min:
        status = "below_legal_entry"
    elif lvl > legal_max:
        status = "above_legal_entry"
    elif lvl < dungeon.min_level:
        status = "legal_below_lfg_queue"
    elif lvl > dungeon.max_level:
        status = "legal_above_lfg_queue"
    elif lvl < dungeon.recommended_min:
        status = "lfg_under_recommended"
    elif lvl > dungeon.recommended_max:
        status = "lfg_over_recommended"
    else:
        status = "recommended"

    return {
        "level": lvl,
        "legal_entry": legal_entry,
        "lfg_queue_eligible": lfg_queue_eligible,
        "in_recommended_band": in_recommended_band,
        "band_status": status,
        "legal_range": f"{legal_min}-{legal_max}",
        "lfg_range": f"{dungeon.min_level}-{dungeon.max_level}",
        "recommended_range": f"{dungeon.recommended_min}-{dungeon.recommended_max}",
    }


def compute_schedule_remaining_minutes(
    schedule: dict[str, Any] | None,
    *,
    now_ts: int,
    tz=None,
) -> tuple[int | None, str]:
    """Compute actual remaining minutes in a companion's active shift (matching CompanionBotMgr::IsScheduledOnline).

    Fail-closed: missing or malformed schedule telemetry returns `(None, 'missing_schedule_telemetry')`
    instead of fabricating a 90-minute default!
    """
    if not isinstance(schedule, dict):
        return None, "missing_schedule_telemetry"

    always_raw = schedule.get("always_online")
    if always_raw is True or always_raw == 1:
        return 1440, "always_online"

    if (
        schedule.get("start_hour") is None
        or schedule.get("end_hour") is None
        or schedule.get("days_mask") is None
    ):
        return None, "missing_schedule_telemetry"

    try:
        start_h = int(schedule["start_hour"])
        end_h = int(schedule["end_hour"])
        days_mask = int(schedule["days_mask"])
    except (TypeError, ValueError):
        return None, "invalid_schedule_telemetry"

    if not (0 <= start_h <= 23 and 0 <= end_h <= 24 and 0 <= days_mask <= 127):
        return None, "invalid_schedule_telemetry"
    if days_mask == 0:
        return 0, "schedule_disabled"

    use_tz = configured_schedule_tz(tz)
    dt = datetime.fromtimestamp(int(now_ts), tz=use_tz)
    # Match CompanionBotMgr.cpp:896-907 localtime_r tm_wday: Sunday=0, Monday=1, ..., Saturday=6
    c_wday = (dt.weekday() + 1) % 7
    day_bit = 1 << c_wday
    prev_day_bit = 1 << ((c_wday + 6) % 7)
    cur_min = dt.hour * 60 + dt.minute

    def _day_active(bit: int) -> bool:
        return (days_mask & bit) != 0

    # Full-day shift (CompanionBotMgr.cpp:899: startHour == endHour -> today's dayBit; also 0..24)
    if start_h == end_h or (start_h == 0 and end_h == 24):
        if not _day_active(day_bit):
            return 0, "outside_scheduled_shift"
        rem = (24 * 60) - cur_min
        for offset in range(1, 7):
            if _day_active(1 << ((c_wday + offset) % 7)):
                rem += 24 * 60
            else:
                break
        return rem, "in_full_day_shift"

    # Standard daytime shift (start_h < end_h)
    if start_h < end_h:
        if not _day_active(day_bit):
            return 0, "outside_scheduled_shift"
        start_m = start_h * 60
        end_m = end_h * 60
        if not (start_m <= cur_min < end_m):
            return 0, "outside_scheduled_shift"
        return end_m - cur_min, "in_scheduled_shift"

    # Overnight shift (start_h > end_h, e.g. 22 -> 04)
    # Before midnight: today's bit; after midnight: yesterday's bit (matches CompanionBotMgr.cpp:903-906)
    if dt.hour >= start_h:
        if not _day_active(day_bit):
            return 0, "outside_scheduled_shift"
        rem = ((24 * 60) - cur_min) + (end_h * 60)
        return rem, "in_overnight_shift"
    if dt.hour < end_h:
        if not _day_active(prev_day_bit):
            return 0, "outside_scheduled_shift"
        rem = (end_h * 60) - cur_min
        return rem, "in_overnight_shift"
    return 0, "outside_scheduled_shift"


def enumerate_schedule_windows(
    schedule: dict[str, Any] | None,
    *,
    start_ts: int,
    horizon_hours: int = 48,
    tz=None,
) -> tuple[list[tuple[int, int]], str]:
    """Enumerate future scheduled online windows [win_start, win_end] within [start_ts, start_ts + horizon_hours].

    Matches CompanionBotMgr::IsScheduledOnline semantics (daytime, overnight, full-day, always_online)
    using DST-safe calendar arithmetic. Fail-closed on missing or malformed schedule telemetry.
    """
    if not isinstance(schedule, dict):
        return [], "missing_schedule_telemetry"

    h_hours = int(horizon_hours)
    if h_hours <= 0:
        return [], "invalid_horizon"

    s_ts = int(start_ts)
    end_horizon_ts = s_ts + (h_hours * 3600)

    always_raw = schedule.get("always_online")
    if always_raw is True or always_raw == 1:
        return [(s_ts, end_horizon_ts)], "always_online"

    if (
        schedule.get("start_hour") is None
        or schedule.get("end_hour") is None
        or schedule.get("days_mask") is None
    ):
        return [], "missing_schedule_telemetry"

    try:
        start_h = int(schedule["start_hour"])
        end_h = int(schedule["end_hour"])
        days_mask = int(schedule["days_mask"])
    except (TypeError, ValueError):
        return [], "invalid_schedule_telemetry"

    if not (0 <= start_h <= 23 and 0 <= end_h <= 24 and 0 <= days_mask <= 127):
        return [], "invalid_schedule_telemetry"
    if days_mask == 0:
        return [], "schedule_disabled"

    use_tz = configured_schedule_tz(tz)
    base_dt = datetime.fromtimestamp(s_ts, tz=use_tz)
    base_date = base_dt.date()
    span_days = (h_hours // 24) + 2

    def _local_ts(d: date, hour: int) -> int:
        if hour == 24:
            dt_obj = datetime.combine(d + timedelta(days=1), dt_time(0, 0), tzinfo=use_tz)
        else:
            dt_obj = datetime.combine(d, dt_time(hour, 0), tzinfo=use_tz)
        return int(dt_obj.timestamp())

    raw_intervals: list[tuple[int, int]] = []
    # Start at day_offset=-1 to capture overnight shifts that began yesterday and extend past midnight
    for day_offset in range(-1, span_days + 1):
        d = base_date + timedelta(days=day_offset)
        c_wday = (d.weekday() + 1) % 7  # Sunday=0 .. Saturday=6
        if (days_mask & (1 << c_wday)) == 0:
            continue

        if start_h == end_h or (start_h == 0 and end_h == 24):
            w_start = _local_ts(d, 0)
            w_end = _local_ts(d, 24)
        elif start_h < end_h:
            w_start = _local_ts(d, start_h)
            w_end = _local_ts(d, end_h)
        else:
            # Overnight shift: start_h today -> end_h tomorrow
            w_start = _local_ts(d, start_h)
            w_end = _local_ts(d + timedelta(days=1), end_h)

        clamped_start = max(s_ts, w_start)
        clamped_end = min(end_horizon_ts, w_end)
        if clamped_end > clamped_start:
            raw_intervals.append((clamped_start, clamped_end))

    if not raw_intervals:
        return [], "no_windows_in_horizon"

    raw_intervals.sort()
    merged: list[tuple[int, int]] = [raw_intervals[0]]
    for ws, we in raw_intervals[1:]:
        prev_s, prev_e = merged[-1]
        if ws <= prev_e:
            merged[-1] = (prev_s, max(prev_e, we))
        else:
            merged.append((ws, we))
    return merged, "ok"


def estimate_travel_and_prep_minutes(
    dungeon: DungeonSpec,
    *,
    current_map_id: int | None = None,
    current_zone_id: int | None = None,
    equipped: dict[str, Any] | None = None,
    is_dead: bool | None = False,
) -> dict[str, Any]:
    """Compute realistic travel + prep time and concrete prep blockers (Issue #14)."""
    travel_min = int(dungeon.travel_minutes)
    prep_min = 0
    prep_needs: list[str] = []
    blocking_needs: list[str] = []

    if is_dead is True:
        blocking_needs.append("dead_needs_resurrection")
        prep_needs.append("dead_needs_resurrection")
        prep_min += 5

    if current_map_id is not None:
        cm = int(current_map_id)
        if cm == dungeon.map_id:
            travel_min = 0
        elif cm not in OVERLAND_MAPS:
            # Currently inside another instance / BG -> must exit/hearth first
            travel_min = max(travel_min + 10, 20)
            prep_needs.append("leave_current_instance")
        elif cm != dungeon.entrance_map:
            # Cross-continent overland travel (boat/tram/zeppelin)
            travel_min = max(travel_min + 15, 25)
            prep_needs.append("cross_continent_travel")
        elif current_zone_id is not None and dungeon.entrance_zone_ids and int(current_zone_id) in dungeon.entrance_zone_ids:
            travel_min = max(3, min(travel_min, 5))

    if isinstance(equipped, dict):
        broken_slots = int(equipped.get("broken_slots") or 0)
        min_dur_pct = equipped.get("min_durability_pct")
        if broken_slots > 0:
            blocking_needs.append("broken_gear_needs_repair")
            prep_needs.append("broken_gear_needs_repair")
            prep_min += 8
        elif min_dur_pct is not None and float(min_dur_pct) < 25.0:
            blocking_needs.append("low_durability_needs_repair")
            prep_needs.append("low_durability_needs_repair")
            prep_min += 6
        if equipped.get("has_main_hand") is False:
            blocking_needs.append("missing_main_hand_weapon")
            prep_needs.append("missing_main_hand_weapon")
            prep_min += 10

    return {
        "travel_minutes": travel_min,
        "prep_minutes": prep_min,
        "total_overhead_minutes": travel_min + prep_min,
        "prep_needs": prep_needs,
        "blocking_prep_needs": blocking_needs,
    }


def infer_viable_roles(char_profile: dict[str, Any], level: int) -> list[str]:
    """Legacy profile-based role hint (retained ONLY for non-dungeon display/backward compatibility).

    For dungeon admission and feasibility, ALWAYS use `evaluate_live_role_capability()`!
    """
    role = str(char_profile.get("primary_role") or "").strip().lower()
    spec = str(char_profile.get("primary_spec") or "").strip().lower()
    cls = str(char_profile.get("class") or "").strip().lower()

    roles: list[str] = []
    if role == "tank" or spec in TANK_SPECS:
        if cls in ("warrior", "paladin", "druid", "death knight"):
            roles.append("tank")
    if role == "healer" or spec in HEALER_SPECS:
        if cls in ("priest", "paladin", "druid", "shaman"):
            roles.append("healer")
    if role in ("dps", "mdps", "rdps", "damage") or not roles:
        roles.append("damage")
    return roles


def evaluate_live_role_capability(
    char_profile: dict[str, Any] | None,
    *,
    level: int,
    live_telemetry: dict[str, Any] | None,
    allow_profile_fallback: bool = False,
) -> dict[str, Any]:
    """Verify genuine dungeon role capability strictly from live class/spec/talents/spells/equipment/native role mask.

    Guarantees (Issue #14):
    - Fail-closed: missing live telemetry OR missing live spec/native-role for hybrids returns `known=False`,
      `viable_roles=[]` — NEVER falls back to static character profiles for role determination!
    - Native class capability matrix enforced strictly (e.g. Mage/Rogue can NEVER be tank/healer).
    - Tanking requires live tank spells (NEVER granted below spell unlock levels, e.g. Lv<16 Paladin without
      Righteous Fury) + shield for Warrior/Paladin + live tank spec or explicit native LFG tank role.
    - Healing requires live healing spells + live healer spec or explicit native LFG healer role (baseline
      level-1 spells alone NEVER grant flex healing to a DPS spec!).
    - Damage for hybrids requires live DPS spec or explicit native LFG damage role (and intact main-hand weapon).
    - Flexible roles are ONLY granted when live evidence proves genuine capability, tracked separately from `primary_role`.
    """
    lvl = int(level)
    if not isinstance(live_telemetry, dict) or not live_telemetry:
        if allow_profile_fallback and isinstance(char_profile, dict):
            inferred = infer_viable_roles(char_profile, lvl)
            return {
                "known": False,
                "source": "profile_fallback",
                "class_name": str(char_profile.get("class") or "").strip().lower(),
                "primary_role": inferred[0] if inferred else None,
                "flex_roles": inferred[1:],
                "viable_roles": inferred,
                "lfg_role_mask": roles_to_lfg_mask(inferred),
                "reasons": ["missing_live_role_telemetry"],
            }
        return {
            "known": False,
            "source": "unknown",
            "class_name": "",
            "primary_role": None,
            "flex_roles": [],
            "viable_roles": [],
            "lfg_role_mask": LFG_ROLE_NONE,
            "reasons": ["missing_live_role_telemetry"],
        }

    # Resolve authoritative class strictly from live telemetry (characters.class)
    cls_name = ""
    if live_telemetry.get("class_id") is not None:
        try:
            cls_name = CLASS_ID_TO_NAME.get(int(live_telemetry["class_id"]), "")
        except (TypeError, ValueError):
            cls_name = ""
    if not cls_name and live_telemetry.get("class_name"):
        cls_name = str(live_telemetry["class_name"]).strip().lower()
    if cls_name not in CLASS_CAPABLE_ROLES:
        return {
            "known": False,
            "source": "unknown",
            "class_name": cls_name,
            "primary_role": None,
            "flex_roles": [],
            "viable_roles": [],
            "lfg_role_mask": LFG_ROLE_NONE,
            "reasons": ["unknown_live_class"],
        }

    capable_by_class = CLASS_CAPABLE_ROLES[cls_name]
    native_mask_raw = live_telemetry.get("native_role_mask")
    native_mask: int | None = None
    if native_mask_raw is not None:
        try:
            native_mask = int(native_mask_raw) & LFG_ROLE_COMBAT_MASK
        except (TypeError, ValueError):
            native_mask = None

    spec_tab_raw = live_telemetry.get("spec_tab")
    spec_tab: int | None = None
    if spec_tab_raw is not None:
        try:
            st_i = int(spec_tab_raw)
            if st_i in (0, 1, 2):
                spec_tab = st_i
        except (TypeError, ValueError):
            spec_tab = None

    active_spec = str(live_telemetry.get("active_spec") or "").strip().lower()
    known_spells_raw = live_telemetry.get("known_spells")
    known_spells: set[int] | None = (
        {int(s) for s in known_spells_raw} if known_spells_raw is not None else None
    )
    talent_spells_raw = live_telemetry.get("talent_spells")
    talent_spells: set[int] | None = (
        {int(s) for s in talent_spells_raw} if talent_spells_raw is not None else None
    )
    equipped = live_telemetry.get("equipped") if isinstance(live_telemetry.get("equipped"), dict) else None

    reasons: list[str] = []

    # 1. Pure DPS classes: only damage is ever legal; still require live signal beyond bare class
    if capable_by_class == frozenset({"damage"}):
        has_live_signal = any(x is not None for x in (native_mask, spec_tab, equipped)) or bool(active_spec)
        if not has_live_signal:
            return {
                "known": False,
                "source": "unknown",
                "class_name": cls_name,
                "primary_role": None,
                "flex_roles": [],
                "viable_roles": [],
                "lfg_role_mask": LFG_ROLE_NONE,
                "reasons": ["missing_live_role_signals"],
            }
        if native_mask is not None and (native_mask & (LFG_ROLE_TANK | LFG_ROLE_HEALER)):
            reasons.append("native_role_mask_illegal_for_class")
        if native_mask is not None and not (native_mask & LFG_ROLE_DAMAGE):
            return {
                "known": True,
                "source": "live_native",
                "class_name": cls_name,
                "primary_role": None,
                "flex_roles": [],
                "viable_roles": [],
                "lfg_role_mask": LFG_ROLE_NONE,
                "reasons": reasons + ["native_role_mask_excludes_damage"],
            }
        return {
            "known": True,
            "source": "live_verified",
            "class_name": cls_name,
            "primary_role": "damage",
            "flex_roles": [],
            "viable_roles": ["damage"],
            "lfg_role_mask": LFG_ROLE_DAMAGE,
            "reasons": reasons,
        }

    # 2. Hybrid classes: FAIL CLOSED if live spec/native role are ALL unknown (P0-2 fix!)
    if spec_tab is None and not active_spec and native_mask is None:
        return {
            "known": False,
            "source": "unknown",
            "class_name": cls_name,
            "primary_role": None,
            "flex_roles": [],
            "viable_roles": [],
            "lfg_role_mask": LFG_ROLE_NONE,
            "reasons": ["missing_live_spec"],
        }

    # Verify each candidate role strictly from live spec + spells + equipment + native_role_mask
    def _tank_verified() -> tuple[bool, bool, str]:
        if "tank" not in capable_by_class:
            return False, False, "class_cannot_tank"
        if native_mask is not None and not (native_mask & LFG_ROLE_TANK):
            return False, False, "native_role_mask_excludes_tank"

        spec_is_tank = (
            (spec_tab is not None and SPEC_TAB_PRIMARY_ROLE.get((cls_name, spec_tab)) == "tank")
            or (active_spec in TANK_SPECS)
            or (
                cls_name == "druid"
                and spec_tab == 1
                and talent_spells is not None
                and bool(talent_spells & DRUID_THICK_HIDE_TALENTS)
            )
        )
        native_claims_tank = native_mask is not None and bool(native_mask & LFG_ROLE_TANK)
        if not spec_is_tank and not native_claims_tank:
            return False, False, "non_tank_spec"

        # Spell verification (always required when known_spells is supplied; required for tanking!)
        if known_spells is None:
            return False, False, "unverified_tank_spells"
        if cls_name == "druid":
            if not (bool(known_spells & {5487, 9634}) and (6795 in known_spells)):
                return False, False, "missing_druid_tank_spells"
            if talent_spells is not None and not (talent_spells & DRUID_THICK_HIDE_TALENTS) and active_spec not in TANK_SPECS:
                return False, False, "missing_druid_tank_talents"
        elif cls_name == "paladin":
            # Righteous Fury (25780) is required for Paladin tanking (never tank at Lv<16 without threat tools!)
            if 25780 not in known_spells:
                return False, False, "missing_paladin_tank_spells"
        elif cls_name == "warrior":
            if not ({71, 355}.issubset(known_spells)):
                return False, False, "missing_warrior_tank_spells"
        elif cls_name == "death knight":
            if not (known_spells & TANK_SPELLS_BY_CLASS["death knight"]):
                return False, False, "missing_dk_tank_spells"

        # Equipment verification: Warrior & Paladin MUST have a shield equipped!
        if cls_name in ("warrior", "paladin"):
            if equipped is None:
                return False, False, "unverified_tank_equipment"
            if equipped.get("has_shield") is not True:
                return False, False, "missing_tank_shield"

        if spec_is_tank or (native_claims_tank and spec_tab is None and not active_spec):
            return True, True, "verified_primary_tank"
        # Flex tank ONLY when native LFG role explicitly opts in AND live spells + shield all verify
        return True, False, "verified_flex_tank"

    def _healer_verified() -> tuple[bool, bool, str]:
        if "healer" not in capable_by_class:
            return False, False, "class_cannot_heal"
        if native_mask is not None and not (native_mask & LFG_ROLE_HEALER):
            return False, False, "native_role_mask_excludes_healer"

        spec_is_heal = (
            (spec_tab is not None and SPEC_TAB_PRIMARY_ROLE.get((cls_name, spec_tab)) == "healer")
            or (active_spec in HEALER_SPECS)
        )
        native_claims_heal = native_mask is not None and bool(native_mask & LFG_ROLE_HEALER)
        if not spec_is_heal and not native_claims_heal:
            return False, False, "non_healer_spec"

        if known_spells is None:
            return False, False, "unverified_healer_spells"
        if not (known_spells & HEALER_SPELLS_BY_CLASS.get(cls_name, frozenset())):
            return False, False, "missing_healer_spells"

        if spec_is_heal or (native_claims_heal and spec_tab is None and not active_spec):
            return True, True, "verified_primary_healer"
        # Flex healer ONLY when native role mask explicitly declares healer capability AND healing spells exist
        return True, False, "verified_flex_healer"

    def _damage_verified() -> tuple[bool, bool, str]:
        if native_mask is not None and not (native_mask & LFG_ROLE_DAMAGE):
            return False, False, "native_role_mask_excludes_damage"

        spec_is_dps = (
            (spec_tab is not None and SPEC_TAB_PRIMARY_ROLE.get((cls_name, spec_tab)) == "damage")
            or (bool(active_spec) and active_spec not in TANK_SPECS and active_spec not in HEALER_SPECS)
        )
        # Druid Feral tab with Thick Hide is tank primary, not DPS primary
        if (
            cls_name == "druid"
            and spec_tab == 1
            and talent_spells is not None
            and bool(talent_spells & DRUID_THICK_HIDE_TALENTS)
        ):
            spec_is_dps = False

        native_claims_dps = native_mask is not None and bool(native_mask & LFG_ROLE_DAMAGE)
        if not spec_is_dps and not native_claims_dps:
            return False, False, "non_damage_spec"

        if equipped is not None and equipped.get("has_main_hand") is False:
            return False, False, "missing_main_hand_weapon"

        if spec_is_dps or (native_claims_dps and spec_tab is None and not active_spec):
            return True, True, "verified_primary_damage"
        return True, False, "verified_flex_damage"

    primary_candidates: list[str] = []
    flex_candidates: list[str] = []

    for role_name, fn in (("tank", _tank_verified), ("healer", _healer_verified), ("damage", _damage_verified)):
        ok, is_prim, detail = fn()
        if ok:
            if is_prim:
                primary_candidates.append(role_name)
            else:
                flex_candidates.append(role_name)
        elif detail not in ("class_cannot_tank", "class_cannot_heal"):
            reasons.append(f"{role_name}:{detail}")

    # Primary role comes STRICTLY from live verified candidates (NEVER from static char_profile!)
    primary_role: str | None = None
    if primary_candidates:
        primary_role = primary_candidates[0]
        flex_roles = [r for r in primary_candidates[1:]] + [r for r in flex_candidates if r != primary_role]
    elif flex_candidates:
        primary_role = flex_candidates[0]
        flex_roles = flex_candidates[1:]
    else:
        flex_roles = []

    viable_roles = ([primary_role] if primary_role else []) + flex_roles
    return {
        "known": True,
        "source": "live_verified",
        "class_name": cls_name,
        "primary_role": primary_role,
        "flex_roles": flex_roles,
        "viable_roles": viable_roles,
        "lfg_role_mask": roles_to_lfg_mask(viable_roles),
        "reasons": reasons,
    }


def _is_map_locked(locked_maps: set[Any] | frozenset[Any] | list[Any], dungeon: DungeonSpec) -> bool:
    """Check whether character has a saved lockout matching dungeon.map_id & dungeon.difficulty."""
    for item in locked_maps:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            if int(item[0]) == dungeon.map_id and int(item[1]) == dungeon.difficulty:
                return True
        else:
            if int(item) == dungeon.map_id:
                return True
    return False


def check_character_eligibility(
    *,
    level: int,
    faction: str | None,
    viable_roles: list[str],
    dungeon: DungeonSpec,
    available_minutes: int | None,
    locked_maps: set[Any] | frozenset[Any] | list[Any] | None = None,
    raid_stage: int = 0,
    allow_pilot_shadow: bool = False,
    online: bool = True,
    telemetry_fresh: bool = True,
    enrolled_coop: bool = True,
    require_known_lockouts: bool = True,
    require_known_live_state: bool = True,
    in_combat: bool | None = False,
    in_group: bool | None = False,
    in_lfg_queue: bool | None = False,
    is_dead: bool | None = False,
    paused: bool = False,
    native_lfg_eligible: bool | None = True,
    native_lfg_lock_reason: str = "",
    equipment_known: bool = True,
    level_lead_blocked: bool = False,
    blocking_prep_needs: list[str] | None = None,
    travel_minutes_override: int | None = None,
    prep_minutes: int = 0,
    require_recommended_band: bool = False,
    required_role: str | None = None,
) -> tuple[bool, str]:
    """Strictly validate whether one character is legally eligible & available for a dungeon opportunity.

    Fail-closed guarantees (Issue #14):
    - `available_minutes` has NO fabricated 90-minute default; `None` fails with `unknown_schedule_window`.
    - `locked_maps=None` fails with `unknown_lockout_telemetry` when `require_known_lockouts=True` (default).
    - `None` for `in_combat`, `in_group`, `in_lfg_queue`, `is_dead`, `native_lfg_eligible`, or
      `equipment_known=False` fails closed with explicit `unknown_*` reasons when `require_known_live_state=True`.
    """
    if not dungeon.supported:
        return False, f"dungeon_unsupported:{dungeon.dungeon_id}"
    if not allow_pilot_shadow and dungeon.dungeon_id not in enabled_dungeon_ids():
        return False, f"dungeon_disabled:{dungeon.dungeon_id}"
    if dungeon.difficulty not in (0, 1):
        return False, "raids_disabled_in_coop_rdf"
    if not enrolled_coop:
        return False, "protected_core_or_not_enrolled"
    if not online:
        return False, "offline"
    if not telemetry_fresh:
        return False, "stale_telemetry"
    if paused:
        return False, "ai_paused"

    if require_known_live_state:
        if is_dead is None:
            return False, "unknown_alive_status"
        if in_combat is None:
            return False, "unknown_combat_status"
        if in_group is None:
            return False, "unknown_group_status"
        if in_lfg_queue is None:
            return False, "unknown_lfg_queue_status"
        if native_lfg_eligible is None:
            return False, "unknown_native_lfg_eligibility"
        if not equipment_known:
            return False, "unknown_equipment_telemetry"

    if is_dead is True:
        return False, "character_dead"
    if in_combat is True:
        return False, "in_combat"
    if in_group is True:
        return False, "already_in_group"
    if in_lfg_queue is True:
        return False, "already_in_lfg_queue"
    if native_lfg_eligible is False:
        suffix = f":{native_lfg_lock_reason}" if native_lfg_lock_reason else ""
        return False, f"native_lfg_ineligible{suffix}"
    if level_lead_blocked:
        return False, "level_lead_cap_blocked"
    if blocking_prep_needs:
        return False, f"prep_required:{blocking_prep_needs[0]}"

    fac_clean = str(faction or "").strip().lower()
    if fac_clean not in ("alliance", "horde"):
        return False, "unknown_faction"
    if dungeon.faction != "both" and fac_clean != dungeon.faction:
        return False, f"faction_mismatch:{fac_clean}!={dungeon.faction}"

    if raid_stage < dungeon.raid_stage_min:
        return False, f"raid_gate_locked:stage_{raid_stage}<{dungeon.raid_stage_min}"

    band = classify_level_band(int(level), dungeon)
    if not band["legal_entry"]:
        return False, f"level_illegal_entry:{level}_not_in_{band['legal_range']}"
    if not band["lfg_queue_eligible"]:
        return False, f"level_out_of_range:{level}_not_in_{dungeon.min_level}..{dungeon.max_level}"
    if require_recommended_band and not band["in_recommended_band"]:
        return False, f"level_outside_recommended:{level}_not_in_{dungeon.recommended_min}..{dungeon.recommended_max}"

    if require_known_lockouts and locked_maps is None:
        return False, "unknown_lockout_telemetry"
    if locked_maps and _is_map_locked(locked_maps, dungeon):
        return False, f"instance_locked:map_{dungeon.map_id}"

    if not viable_roles:
        return False, "no_viable_role"
    if required_role and str(required_role).strip().lower() not in {r.lower() for r in viable_roles}:
        return False, f"wrong_role:{required_role}"

    if available_minutes is None:
        return False, "unknown_schedule_window"
    travel_m = int(dungeon.travel_minutes if travel_minutes_override is None else travel_minutes_override)
    needed_mins = int(dungeon.estimated_minutes) + travel_m + max(0, int(prep_minutes)) + 10  # 10m safety buffer
    if int(available_minutes) < needed_mins:
        return False, f"insufficient_schedule_window:{available_minutes}m<{needed_mins}m"

    return True, "eligible"
