"""Phase II Bracketed Item-Specific Gear Ambition Engine (Issue #24).

Design guarantees:
- Deterministic installed-data catalog & character state loaders (`load_catalog_from_db`,
  `load_character_state_from_db`, `load_market_snapshot_from_db`) matching the exact AzerothCore
  WotLK 3.3.5a `acore_world` / `acore_characters` schemas. Zero fabricated items or rewards:
  every `ItemSource` requires an explicit row-level `evidence_key`.
- Exact WotLK 3.3.5a legality: class/race bitmasks, `FlagsExtra` faction bits (Horde/Alliance),
  armor proficiency, weapon subclasses, shield/relic/holdable restrictions, dual-wield level &
  spell checks, `max_count` (Unique / Unique(N)), `ITEM_FLAG_UNIQUE_EQUIPPABLE`, and
  `ItemLimitCategory`.
- Bidirectional 2H vs 1H+Offhand combo evaluation, cross-slot paired-slot deduplication (rings,
  trinkets, 1H weapons), bag copy accounting, and fail-closed handling of unknown equipped items.
- Multi-bracket planning (`current`, `next`, `cap`) respecting server `raid_stage` cap
  (`0=Classic Lv60`, `1=TBC Lv70`, `2=WotLK Lv80`).
- Unified live-OFF & registered-executor gate (`_is_proposal_executable`): while `coop_enabled()`
  is OFF (default) OR no native executor is registered for a proposal kind, EVERY emitted proposal
  is strictly `executable=False, advisory_only=True`.
- Shrinking gold budget pass across all paid attainable purchases preserving raid readiness gold
  reserves and protected readiness slots (including skipping 2H when `off_hand` is protected).
- Persistent `GearAmbitionStore` with full catalog/source validation, per-slot caps,
  `PersonaRegistry` identity verification across set/reconcile/abandon, composite
  `(native_event_id, ambition_id)` deduplication, and full reconciliation (equipped, bags,
  outleveled bracket/dungeon, role change, completed quest, superseded, voluntary abandon).
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .commitments import _parse_realm_id, _verify_persona_with_snapshot
from .dungeons import CLASS_CAPABLE_ROLES, CLASS_ID_TO_NAME, DUNGEON_BY_ID, DUNGEON_BY_MAP, enabled_dungeon_ids
from .identity import PersonaRegistry
from .opportunities import coop_enabled

logger = logging.getLogger("hearthcore.gear_ambition")

SCHEMA_VERSION = 2
DEFAULT_MAX_TARGETS_PER_SLOT = 3
MAX_TARGETS_PER_SLOT_CEILING = 5
DEFAULT_MIN_UPGRADE_DELTA = 1.0

# WotLK 3.3.5a item_template constants
ITEM_CLASS_WEAPON = 2
ITEM_CLASS_ARMOR = 4

ARMOR_SUBCLASS_MISC = 0
ARMOR_SUBCLASS_CLOTH = 1
ARMOR_SUBCLASS_LEATHER = 2
ARMOR_SUBCLASS_MAIL = 3
ARMOR_SUBCLASS_PLATE = 4
ARMOR_SUBCLASS_SHIELD = 6
ARMOR_SUBCLASS_LIBRAM = 7
ARMOR_SUBCLASS_IDOL = 8
ARMOR_SUBCLASS_TOTEM = 9
ARMOR_SUBCLASS_SIGIL = 10

ITEM_FLAG_UNIQUE_EQUIPPABLE = 0x80000
ITEM_FLAG_EXTRA_HORDE_ONLY = 0x01
ITEM_FLAG_EXTRA_ALLIANCE_ONLY = 0x02

INVTYPE_ONE_HAND = 13
INVTYPE_SHIELD = 14
INVTYPE_CLOAK = 16
INVTYPE_TWO_HAND = 17
INVTYPE_MAIN_HAND = 21
INVTYPE_OFFHAND_WEAPON = 22
INVTYPE_HOLDABLE = 23

# WotLK 3.3.5a InventoryType -> canonical equipment slot(s)
INVTYPE_TO_SLOTS: dict[int, tuple[str, ...]] = {
    1: ("head",),
    2: ("neck",),
    3: ("shoulder",),
    5: ("chest",),
    20: ("chest",),           # Robe
    6: ("waist",),
    7: ("legs",),
    8: ("feet",),
    9: ("wrist",),
    10: ("hands",),
    11: ("finger1", "finger2"),
    12: ("trinket1", "trinket2"),
    13: ("main_hand", "off_hand"),  # One-Hand
    14: ("off_hand",),        # Shield
    15: ("ranged",),          # Bow
    16: ("back",),            # Cloak
    17: ("main_hand",),       # Two-Hand
    21: ("main_hand",),       # Main Hand
    22: ("off_hand",),        # Off Hand weapon
    23: ("off_hand",),        # Holdable (Tome/Orb)
    25: ("ranged",),          # Thrown
    26: ("ranged",),          # Gun/Crossbow/Wand
    28: ("ranged",),          # Relic
}

# WotLK 3.3.5a character_inventory slot indices (bag = 0) -> canonical slot names
WOW_EQUIP_SLOT_MAP: dict[int, str] = {
    0: "head",
    1: "neck",
    2: "shoulder",
    4: "chest",
    5: "waist",
    6: "legs",
    7: "feet",
    8: "wrist",
    9: "hands",
    10: "finger1",
    11: "finger2",
    12: "trinket1",
    13: "trinket2",
    14: "back",
    15: "main_hand",
    16: "off_hand",
    17: "ranged",
}

PAIRED_SLOTS: dict[str, str] = {
    "finger1": "finger2",
    "finger2": "finger1",
    "trinket1": "trinket2",
    "trinket2": "trinket1",
    "main_hand": "off_hand",
    "off_hand": "main_hand",
}

ALL_EQUIPMENT_SLOTS: tuple[str, ...] = (
    "head", "neck", "shoulder", "back", "chest", "wrist",
    "hands", "waist", "legs", "feet",
    "finger1", "finger2", "trinket1", "trinket2",
    "main_hand", "off_hand", "ranged",
)

CLASS_ARMOR_MAX: dict[str, tuple[int, int]] = {
    "warrior": (ARMOR_SUBCLASS_MAIL, ARMOR_SUBCLASS_PLATE),
    "paladin": (ARMOR_SUBCLASS_MAIL, ARMOR_SUBCLASS_PLATE),
    "death knight": (ARMOR_SUBCLASS_PLATE, ARMOR_SUBCLASS_PLATE),
    "hunter": (ARMOR_SUBCLASS_LEATHER, ARMOR_SUBCLASS_MAIL),
    "shaman": (ARMOR_SUBCLASS_LEATHER, ARMOR_SUBCLASS_MAIL),
    "rogue": (ARMOR_SUBCLASS_LEATHER, ARMOR_SUBCLASS_LEATHER),
    "druid": (ARMOR_SUBCLASS_LEATHER, ARMOR_SUBCLASS_LEATHER),
    "priest": (ARMOR_SUBCLASS_CLOTH, ARMOR_SUBCLASS_CLOTH),
    "mage": (ARMOR_SUBCLASS_CLOTH, ARMOR_SUBCLASS_CLOTH),
    "warlock": (ARMOR_SUBCLASS_CLOTH, ARMOR_SUBCLASS_CLOTH),
}

SHIELD_CLASSES = frozenset({"warrior", "paladin", "shaman"})
# Dual-wield minimum trained level by class (Shaman requires Enhancement talent spell 30798 at Lv40+)
DUAL_WIELD_MIN_LEVEL: dict[str, int] = {
    "rogue": 10,
    "warrior": 20,
    "hunter": 20,
    "shaman": 40,
    "death knight": 55,
}
SHAMAN_DUAL_WIELD_SPELL_ID = 30798

RELIC_SUBCLASS_BY_CLASS: dict[str, int] = {
    "paladin": ARMOR_SUBCLASS_LIBRAM,
    "druid": ARMOR_SUBCLASS_IDOL,
    "shaman": ARMOR_SUBCLASS_TOTEM,
    "death knight": ARMOR_SUBCLASS_SIGIL,
}

CLASS_USABLE_WEAPONS: dict[str, frozenset[int]] = {
    "warrior": frozenset({0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 13, 15, 16, 18}),
    "paladin": frozenset({0, 1, 4, 5, 6, 7, 8}),
    "hunter": frozenset({0, 1, 2, 3, 6, 7, 8, 10, 13, 15, 16, 18}),
    "rogue": frozenset({0, 2, 3, 4, 7, 13, 15, 16, 18}),
    "priest": frozenset({4, 10, 15, 19}),
    "death knight": frozenset({0, 1, 4, 5, 6, 7, 8}),
    "shaman": frozenset({0, 1, 4, 5, 10, 13, 15}),
    "mage": frozenset({7, 10, 15, 19}),
    "warlock": frozenset({7, 10, 15, 19}),
    "druid": frozenset({4, 5, 6, 10, 13, 15}),
}

# P2-5: Separate melee/ranged/spell/feral stat types cleanly!
STAT_TYPE_MAP: dict[int, str] = {
    3: "agility",
    4: "strength",
    5: "intellect",
    6: "spirit",
    7: "stamina",
    12: "defense_rating",
    13: "dodge_rating",
    14: "parry_rating",
    15: "block_rating",
    16: "melee_hit_rating",
    17: "ranged_hit_rating",
    18: "spell_hit_rating",
    19: "melee_crit_rating",
    20: "ranged_crit_rating",
    21: "spell_crit_rating",
    28: "melee_haste_rating",
    29: "ranged_haste_rating",
    30: "spell_haste_rating",
    31: "hit_rating",         # WotLK unified hit rating
    32: "crit_rating",        # WotLK unified crit rating
    35: "resilience",
    36: "haste_rating",       # WotLK unified haste rating
    37: "expertise_rating",
    38: "attack_power",
    39: "ranged_attack_power",
    40: "feral_attack_power",
    41: "spell_power",
    42: "spell_power",
    43: "mp5",
    44: "armor_pen_rating",
    45: "spell_power",
    48: "block_value",
}

LEVEL_BRACKETS: tuple[tuple[str, int, int, int], ...] = (
    ("1-9", 1, 9, 0),
    ("10-19", 10, 19, 0),
    ("20-29", 20, 29, 0),
    ("30-39", 30, 39, 0),
    ("40-49", 40, 49, 0),
    ("50-59", 50, 59, 0),
    ("60_classic_cap", 60, 60, 0),
    ("61-69", 61, 69, 1),
    ("70_tbc_cap", 70, 70, 1),
    ("71-79", 71, 79, 2),
    ("80_wotlk_cap", 80, 80, 2),
)
BRACKET_BY_ID: dict[str, tuple[str, int, int, int]] = {b[0]: b for b in LEVEL_BRACKETS}

RAID_STAGE_LEVEL_CAP: dict[int, int] = {0: 60, 1: 70, 2: 80}

# P0-3: Explicit registry of proposal kinds that have a verified native C++ executor.
# Empty by default until native staging verification registers specific executors!
REGISTERED_NATIVE_EXECUTOR_KINDS: frozenset[str] = frozenset()

PROPOSAL_KIND_BY_SOURCE_KIND: dict[str, frozenset[str]] = {
    "quest_reward": frozenset({"native_quest_accept", "advisory_gear_target"}),
    "crafted": frozenset({"native_self_craft", "native_guild_work_order", "advisory_gear_target"}),
    "dungeon_drop": frozenset({"rdf_dungeon_queue", "advisory_gear_target"}),
    "vendor": frozenset({"native_vendor_purchase", "advisory_gear_target"}),
    "reputation_vendor": frozenset({"native_vendor_purchase", "advisory_gear_target"}),
    "auction_house": frozenset({"native_ah_buyout", "advisory_gear_target"}),
    "raid_drop": frozenset({"advisory_gear_target"}),
    "world_drop": frozenset({"advisory_gear_target"}),
}

VALID_SOURCE_KINDS = frozenset(PROPOSAL_KIND_BY_SOURCE_KIND.keys())

ROLE_STAT_WEIGHTS: dict[tuple[str, str], dict[str, float]] = {
    ("warrior", "tank"): {
        "stamina": 2.4, "defense_rating": 2.2, "dodge_rating": 1.8, "parry_rating": 1.6,
        "block_rating": 1.4, "block_value": 0.9, "strength": 1.1, "agility": 0.9,
        "expertise_rating": 1.2, "hit_rating": 1.1, "melee_hit_rating": 1.1, "armor": 0.08, "melee_dps": 1.2,
    },
    ("paladin", "tank"): {
        "stamina": 2.4, "defense_rating": 2.2, "dodge_rating": 1.8, "parry_rating": 1.5,
        "block_rating": 1.6, "block_value": 1.1, "strength": 1.1, "intellect": 0.6,
        "spell_power": 0.7, "expertise_rating": 1.2, "hit_rating": 1.1, "melee_hit_rating": 1.1, "armor": 0.08, "melee_dps": 1.2,
    },
    ("druid", "tank"): {
        "stamina": 2.6, "agility": 2.2, "dodge_rating": 1.9, "defense_rating": 1.6,
        "strength": 0.9, "attack_power": 0.4, "feral_attack_power": 0.6,
        "expertise_rating": 1.3, "hit_rating": 1.1, "melee_hit_rating": 1.1, "armor": 0.12, "melee_dps": 0.5,
    },
    ("death knight", "tank"): {
        "stamina": 2.5, "defense_rating": 2.2, "dodge_rating": 1.9, "parry_rating": 1.7,
        "strength": 1.3, "expertise_rating": 1.2, "hit_rating": 1.1, "melee_hit_rating": 1.1, "armor": 0.08, "melee_dps": 1.6,
    },
    ("priest", "healer"): {
        "spell_power": 2.2, "intellect": 1.8, "spirit": 1.7, "mp5": 2.0,
        "haste_rating": 1.4, "spell_haste_rating": 1.4, "crit_rating": 1.1, "spell_crit_rating": 1.1, "stamina": 0.7,
    },
    ("paladin", "healer"): {
        "intellect": 2.3, "spell_power": 2.0, "crit_rating": 1.7, "spell_crit_rating": 1.7,
        "mp5": 1.8, "haste_rating": 1.5, "spell_haste_rating": 1.5, "stamina": 0.7,
    },
    ("druid", "healer"): {
        "spell_power": 2.2, "spirit": 1.9, "intellect": 1.7,
        "haste_rating": 1.6, "spell_haste_rating": 1.6, "mp5": 1.7, "crit_rating": 1.0, "spell_crit_rating": 1.0, "stamina": 0.7,
    },
    ("shaman", "healer"): {
        "spell_power": 2.2, "intellect": 1.9, "mp5": 2.0,
        "haste_rating": 1.7, "spell_haste_rating": 1.7, "crit_rating": 1.4, "spell_crit_rating": 1.4, "stamina": 0.7,
    },
    ("warrior", "damage"): {
        "strength": 2.2, "attack_power": 0.9, "crit_rating": 1.6, "melee_crit_rating": 1.6,
        "hit_rating": 1.8, "melee_hit_rating": 1.8, "armor_pen_rating": 1.7,
        "expertise_rating": 1.6, "agility": 1.1, "stamina": 0.6, "melee_dps": 4.5,
    },
    ("paladin", "damage"): {
        "strength": 2.3, "attack_power": 0.9, "crit_rating": 1.6, "melee_crit_rating": 1.6,
        "hit_rating": 1.8, "melee_hit_rating": 1.8, "haste_rating": 1.3, "melee_haste_rating": 1.3,
        "expertise_rating": 1.5, "agility": 0.9, "stamina": 0.6, "melee_dps": 4.5,
    },
    ("death knight", "damage"): {
        "strength": 2.3, "attack_power": 0.9, "hit_rating": 1.8, "melee_hit_rating": 1.8,
        "crit_rating": 1.5, "melee_crit_rating": 1.5, "haste_rating": 1.4, "melee_haste_rating": 1.4,
        "expertise_rating": 1.5, "stamina": 0.6, "melee_dps": 4.5,
    },
    ("rogue", "damage"): {
        "agility": 2.3, "attack_power": 0.9, "hit_rating": 1.9, "melee_hit_rating": 1.9,
        "expertise_rating": 1.7, "crit_rating": 1.5, "melee_crit_rating": 1.5,
        "haste_rating": 1.5, "melee_haste_rating": 1.5, "armor_pen_rating": 1.4, "stamina": 0.5, "melee_dps": 4.5,
    },
    ("hunter", "damage"): {
        "agility": 2.4, "attack_power": 0.9, "ranged_attack_power": 0.95,
        "hit_rating": 1.8, "ranged_hit_rating": 1.8, "crit_rating": 1.7, "ranged_crit_rating": 1.7,
        "armor_pen_rating": 1.5, "intellect": 0.8, "stamina": 0.5, "ranged_dps": 4.8,
    },
    ("druid", "damage"): {
        "agility": 1.9, "feral_attack_power": 0.8, "spell_power": 1.9, "intellect": 1.3,
        "crit_rating": 1.5, "melee_crit_rating": 1.2, "spell_crit_rating": 1.2,
        "hit_rating": 1.7, "spell_hit_rating": 1.4, "haste_rating": 1.4, "stamina": 0.6, "melee_dps": 1.5,
    },
    ("shaman", "damage"): {
        "spell_power": 2.0, "agility": 1.6, "intellect": 1.4,
        "hit_rating": 1.8, "spell_hit_rating": 1.5, "melee_hit_rating": 1.5,
        "crit_rating": 1.5, "haste_rating": 1.5, "attack_power": 0.7, "stamina": 0.6, "melee_dps": 2.0,
    },
    ("mage", "damage"): {
        "spell_power": 2.4, "hit_rating": 2.0, "spell_hit_rating": 2.0,
        "crit_rating": 1.7, "spell_crit_rating": 1.7, "haste_rating": 1.7, "spell_haste_rating": 1.7,
        "intellect": 1.4, "spirit": 0.7, "stamina": 0.5,
    },
    ("warlock", "damage"): {
        "spell_power": 2.4, "hit_rating": 2.0, "spell_hit_rating": 2.0,
        "haste_rating": 1.7, "spell_haste_rating": 1.7, "crit_rating": 1.6, "spell_crit_rating": 1.6,
        "intellect": 1.2, "spirit": 1.0, "stamina": 0.7,
    },
    ("priest", "damage"): {
        "spell_power": 2.4, "hit_rating": 2.0, "spell_hit_rating": 2.0,
        "haste_rating": 1.7, "spell_haste_rating": 1.7, "crit_rating": 1.6, "spell_crit_rating": 1.6,
        "intellect": 1.2, "spirit": 1.1, "stamina": 0.6,
    },
}


@dataclass(frozen=True)
class ItemSource:
    """Verified installed-data item acquisition source (NO fail-open defaults — P1-10)."""
    source_kind: str
    source_id: int
    source_name: str
    faction: str                              # MUST be 'alliance' | 'horde' | 'both'
    evidence_table: str
    evidence_key: tuple[Any, ...]             # Non-empty row-level DB key!
    min_level: int = 1
    drop_chance_pct: float | None = None      # Required for drops (None fails closed to advisory!)
    cost_copper: int = 0
    extended_cost: int = 0                    # >0 (emblems/badges/honor) -> advisory until currency modeled (P1-9)
    vendor_max_count: int = 0                 # >0 (limited stock) -> advisory (P1-9)
    required_skill: int = 0
    required_skill_rank: int = 0
    recipe_spell_id: int = 0
    required_reputation_faction: int = 0
    required_reputation_rank: int = 0
    dungeon_id: str = ""
    boss_entry: int = 0
    raid_stage_min: int = 0
    estimated_minutes: int | None = None
    # Quest metadata (P1-8)
    prev_quest_id: int = 0
    exclusive_group: int = 0
    quest_allowable_races: int = 0            # 0 = all races
    quest_allowable_classes: int = 0          # 0 = all classes
    suggested_group_num: int = 0              # >1 = group/dungeon quest
    is_choice_reward: bool = False


@dataclass(frozen=True)
class MarketListing:
    """Volatile runtime Auction House listing separated from the frozen world catalog (P2-8)."""
    auction_id: int
    item_entry: int
    buyout_copper: int
    faction: str
    expires_at: int
    evidence_key: tuple[Any, ...]


@dataclass(frozen=True)
class MarketSnapshot:
    """Timestamped volatile market & guild-crafter state."""
    observed_at: int
    expires_at: int
    ah_listings_by_entry: dict[int, tuple[MarketListing, ...]] = field(default_factory=dict)
    guild_craftable_entries: frozenset[int] = frozenset()


@dataclass(frozen=True)
class CatalogItem:
    entry: int
    name: str
    item_level: int
    required_level: int
    quality: int
    item_class: int
    item_subclass: int
    inventory_type: int
    allowable_class: int = -1
    allowable_race: int = -1
    bonding: int = 0
    max_count: int = 0
    flags: int = 0
    flags_extra: int = 0                      # 0x01=Horde only, 0x02=Alliance only (P2-1)
    item_limit_category: int = 0              # Unique-Equipped category group (P2-2)
    required_skill: int = 0
    required_skill_rank: int = 0
    required_reputation_faction: int = 0
    required_reputation_rank: int = 0
    armor: int = 0
    dps: float = 0.0
    weapon_speed: float = 0.0
    stats: dict[str, float] = field(default_factory=dict)
    raid_stage_min: int = 0
    sources: tuple[ItemSource, ...] = ()
    evidence_table: str = "acore_world.item_template"


def _is_proposal_executable(
    proposal_kind: str,
    *,
    dungeon_id: str = "",
    registered_executors: frozenset[str] | set[str] | None = None,
) -> tuple[bool, str]:
    """Single unified gate for ALL proposals (P0-2 & P0-3): requires coop_enabled() AND registered executor!"""
    if not coop_enabled():
        return False, "coop_execution_disabled"
    exec_set = REGISTERED_NATIVE_EXECUTOR_KINDS if registered_executors is None else frozenset(registered_executors)
    if proposal_kind not in exec_set:
        return False, f"no_registered_executor:{proposal_kind}"
    if proposal_kind == "rdf_dungeon_queue":
        if not dungeon_id or dungeon_id not in enabled_dungeon_ids():
            return False, f"dungeon_not_enabled_live:{dungeon_id or 'none'}"
    return True, "executable"


def resolve_bracket(
    level: int,
    *,
    raid_stage: int = 0,
    xp_cap_level: int | None = None,
    xp_fail_closed: bool = False,
) -> dict[str, Any]:
    """Resolve the character's active level bracket & progression cap; fails closed on unknown raid_stage (P3 / Issue #31).

    Invariants (Issue #31):
    - Bots cannot lose levels: if `xp_cap_level < level` (e.g. owner recreated at Lv 1 while bot is Lv 10),
      effective level cap clamps to `min(server_cap, max(level, xp_cap_level))` so the bot stays in its real
      current bracket rather than regressing to a lower bracket.
    - When `xp_fail_closed=True` or `xp_cap_level == 0`, fail closed by capping effective level at
      `min(server_cap, level)` (current bracket only — never permissively planning up to Lv 60/80!).
    """
    st = int(raid_stage)
    if st not in RAID_STAGE_LEVEL_CAP:
        raise ValueError(f"Unknown raid_stage: {raid_stage}")
    lvl = max(1, int(level))
    server_cap = RAID_STAGE_LEVEL_CAP[st]
    eff_lvl = min(lvl, server_cap)
    xp_cap_authoritative = False
    if xp_fail_closed or (xp_cap_level is not None and int(xp_cap_level) <= 0):
        cap = eff_lvl
        xp_cap_authoritative = True
    elif xp_cap_level is not None and int(xp_cap_level) > 0:
        cap = min(server_cap, max(eff_lvl, int(xp_cap_level)))
        xp_cap_authoritative = True
    else:
        cap = server_cap

    for idx, (b_id, b_min, b_max, b_stage) in enumerate(LEVEL_BRACKETS):
        if b_min <= eff_lvl <= b_max:
            return {
                "bracket_id": b_id,
                "bracket_index": idx,
                "min_level": b_min,
                "max_level": min(b_max, cap),
                "server_level_cap": server_cap,
                "effective_level_cap": cap,
                "xp_cap_authoritative": xp_cap_authoritative,
                "xp_fail_closed": bool(xp_fail_closed or (xp_cap_level is not None and int(xp_cap_level) <= 0)),
                "raid_stage": st,
                "bracket_stage": b_stage,
            }
    raise ValueError(f"Unable to resolve bracket for level={level}, raid_stage={raid_stage}")


def resolve_horizon_brackets(
    level: int,
    *,
    raid_stage: int = 0,
    brackets: tuple[str, ...] = ("current",),
    xp_cap_level: int | None = None,
    xp_fail_closed: bool = False,
) -> list[dict[str, Any]]:
    """Resolve requested bracket windows ('current', 'next', 'cap') deterministically (P1-7 / Issue #31)."""
    cur = resolve_bracket(level, raid_stage=raid_stage, xp_cap_level=xp_cap_level, xp_fail_closed=xp_fail_closed)
    cap = int(cur["effective_level_cap"])
    server_cap = int(cur["server_level_cap"])
    cap_bracket = resolve_bracket(cap, raid_stage=raid_stage, xp_cap_level=xp_cap_level, xp_fail_closed=xp_fail_closed)
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for tag in brackets:
        if tag == "current":
            b = {**cur, "horizon_kind": "current"}
        elif tag == "next":
            nxt_idx = int(cur["bracket_index"]) + 1
            if nxt_idx < len(LEVEL_BRACKETS) and LEVEL_BRACKETS[nxt_idx][1] <= cap:
                nb = LEVEL_BRACKETS[nxt_idx]
                b = {
                    "bracket_id": nb[0],
                    "bracket_index": nxt_idx,
                    "min_level": nb[1],
                    "max_level": min(nb[2], cap),
                    "server_level_cap": server_cap,
                    "effective_level_cap": cap,
                    "xp_cap_authoritative": bool(cur["xp_cap_authoritative"]),
                    "xp_fail_closed": bool(cur["xp_fail_closed"]),
                    "raid_stage": int(raid_stage),
                    "bracket_stage": nb[3],
                    "horizon_kind": "next",
                }
            else:
                continue
        elif tag == "cap":
            b = {**cap_bracket, "horizon_kind": "cap"}
        else:
            raise ValueError(f"Unknown bracket selector: {tag}")
        if b["bracket_id"] not in seen_ids:
            seen_ids.add(b["bracket_id"])
            out.append(b)
    return out


def decode_item_template_row(row: dict[str, Any], *, sources: tuple[ItemSource, ...] = ()) -> CatalogItem:
    """Deterministically decode a real `acore_world.item_template` row into a `CatalogItem`."""
    stats: dict[str, float] = {}
    for i in range(1, 11):
        st = int(row.get(f"stat_type{i}") or 0)
        sv = float(row.get(f"stat_value{i}") or 0)
        if st > 0 and sv != 0.0:
            cname = STAT_TYPE_MAP.get(st)
            if cname:
                stats[cname] = stats.get(cname, 0.0) + sv

    delay_ms = int(row.get("delay") or 0)
    dmg_min = float(row.get("dmg_min1") or 0) + float(row.get("dmg_min2") or 0)
    dmg_max = float(row.get("dmg_max1") or 0) + float(row.get("dmg_max2") or 0)
    speed_sec = (delay_ms / 1000.0) if delay_ms > 0 else 0.0
    dps = round(((dmg_min + dmg_max) / 2.0) / speed_sec, 2) if speed_sec > 0 and (dmg_min + dmg_max) > 0 else 0.0

    req_lvl = int(row.get("RequiredLevel") or 0)
    ilvl = int(row.get("ItemLevel") or 0)
    inferred_stage = 2 if (req_lvl > 70 or ilvl >= 165) else (1 if (req_lvl > 60 or ilvl >= 95) else 0)
    if sources:
        inferred_stage = max(inferred_stage, min(s.raid_stage_min for s in sources))

    return CatalogItem(
        entry=int(row["entry"]),
        name=str(row["name"]),
        item_level=ilvl,
        required_level=req_lvl,
        quality=int(row.get("Quality") or 0),
        item_class=int(row.get("class") or 0),
        item_subclass=int(row.get("subclass") or 0),
        inventory_type=int(row.get("InventoryType") or 0),
        allowable_class=int(row.get("AllowableClass") if row.get("AllowableClass") is not None else -1),
        allowable_race=int(row.get("AllowableRace") if row.get("AllowableRace") is not None else -1),
        bonding=int(row.get("bonding") or 0),
        max_count=int(row.get("maxcount") or 0),
        flags=int(row.get("Flags") or 0),
        flags_extra=int(row.get("FlagsExtra") or 0),
        item_limit_category=int(row.get("ItemLimitCategory") or 0),
        required_skill=int(row.get("RequiredSkill") or 0),
        required_skill_rank=int(row.get("RequiredSkillRank") or 0),
        required_reputation_faction=int(row.get("RequiredReputationFaction") or 0),
        required_reputation_rank=int(row.get("RequiredReputationRank") or 0),
        armor=int(row.get("armor") or 0),
        dps=dps,
        weapon_speed=round(speed_sec, 2),
        stats=stats,
        raid_stage_min=inferred_stage,
        sources=tuple(sources),
        evidence_table="acore_world.item_template",
    )


def _fetch_dicts(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        cols = [d[0] for d in (cur.description or [])]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        cur.close()


def _existing_tables(conn: Any) -> set[str]:
    try:
        rows = _fetch_dicts(conn, "SELECT name FROM sqlite_master WHERE type='table'")
        return {str(r["name"]).lower() for r in rows}
    except Exception:
        try:
            rows = _fetch_dicts(conn, "SHOW TABLES")
            return {str(next(iter(r.values()))).lower() for r in rows}
        except Exception as exc:
            logger.warning("Failed enumerating tables for catalog loader: %s", exc)
            return set()


def _faction_from_race_mask(mask: int) -> str:
    m = int(mask or 0)
    if m <= 0:
        return "both"
    # WotLK 3.3.5a Alliance races: 1(0),3(2),4(3),7(6),11(10) -> 0x44D (1101)
    # Horde races: 2(1),5(4),6(5),8(7),10(9) -> 0x2B2 (690)
    all_a = 0x44D
    all_h = 0x2B2
    has_a = (m & all_a) != 0
    has_h = (m & all_h) != 0
    if has_a and not has_h:
        return "alliance"
    if has_h and not has_a:
        return "horde"
    return "both"


def load_catalog_from_db(
    world_conn: Any,
    *,
    entries: list[int] | set[int] | None = None,
    boss_dungeon_map: dict[int, str] | None = None,
) -> dict[int, CatalogItem]:
    """Deterministic installed-data loader reading `item_template`, `quest_template`, `creature_loot_template`,
    `reference_loot_template`, `npc_vendor`, and optional `crafted_recipes` with exact row-level `evidence_key`s (P0-1).
    """
    tables = _existing_tables(world_conn)
    if "item_template" not in tables:
        return {}

    where = "WHERE class IN (2, 4) AND InventoryType > 0"
    params: tuple[Any, ...] = ()
    if entries is not None:
        clean_e = sorted({int(e) for e in entries if int(e) > 0})
        if not clean_e:
            return {}
        placeholders = ",".join("?" for _ in clean_e)
        where += f" AND entry IN ({placeholders})"
        params = tuple(clean_e)

    item_rows = _fetch_dicts(world_conn, f"SELECT * FROM item_template {where} ORDER BY entry ASC", params)
    if not item_rows:
        return {}
    item_map = {int(r["entry"]): r for r in item_rows}
    sources_by_entry: dict[int, list[ItemSource]] = {e: [] for e in item_map}

    # 1. Quest rewards (`quest_template` LEFT JOIN `quest_template_addon`)
    if "quest_template" in tables:
        has_addon = "quest_template_addon" in tables
        q_sql = (
            "SELECT q.*, a.PrevQuestID, a.ExclusiveGroup, a.AllowableClasses, "
            "a.RequiredSkillID, a.RequiredSkillPoints, a.RequiredMinRepFaction, a.RequiredMinRepValue "
            "FROM quest_template q LEFT JOIN quest_template_addon a ON a.ID = q.ID ORDER BY q.ID ASC"
            if has_addon
            else "SELECT * FROM quest_template ORDER BY ID ASC"
        )
        for q in _fetch_dicts(world_conn, q_sql):
            qid = int(q["ID"])
            qname = str(q.get("LogTitle") or f"Quest #{qid}")
            q_min = max(1, int(q.get("MinLevel") or 1))
            q_races = int(q.get("AllowableRaces") or 0)
            q_fac = _faction_from_race_mask(q_races)
            q_group = int(q.get("SuggestedGroupNum") or 0)
            q_prev = int(q.get("PrevQuestID") or 0)
            q_excl = int(q.get("ExclusiveGroup") or 0)
            q_cls = int(q.get("AllowableClasses") or 0)
            q_stage = 2 if q_min > 70 else (1 if q_min > 60 else 0)
            est_m = 45 if q_group > 1 else 25

            for col_prefix, is_choice, count in (("RewardItem", False, 4), ("RewardChoiceItemID", True, 6)):
                for i in range(1, count + 1):
                    col = f"{col_prefix}{i}"
                    ientry = int(q.get(col) or 0)
                    if ientry in sources_by_entry:
                        sources_by_entry[ientry].append(ItemSource(
                            source_kind="quest_reward",
                            source_id=qid,
                            source_name=qname,
                            faction=q_fac,
                            evidence_table="acore_world.quest_template",
                            evidence_key=("quest_template", qid, col),
                            min_level=q_min,
                            drop_chance_pct=100.0,
                            cost_copper=0,
                            raid_stage_min=q_stage,
                            estimated_minutes=est_m,
                            prev_quest_id=q_prev,
                            exclusive_group=q_excl,
                            quest_allowable_races=q_races,
                            quest_allowable_classes=q_cls,
                            suggested_group_num=q_group,
                            is_choice_reward=is_choice,
                        ))

    # 2. Creature / Dungeon loot (`creature_loot_template` + optional `reference_loot_template` + `creature` map)
    if "creature_loot_template" in tables:
        boss_dmap = dict(boss_dungeon_map or {})
        creature_names: dict[int, str] = {}
        if "creature_template" in tables:
            for cr in _fetch_dicts(world_conn, "SELECT entry, name FROM creature_template"):
                creature_names[int(cr["entry"])] = str(cr["name"])
        if "creature" in tables:
            for sp in _fetch_dicts(world_conn, "SELECT id, map FROM creature"):
                cid = int(sp["id"])
                cmap = int(sp["map"])
                if cid not in boss_dmap and cmap in DUNGEON_BY_MAP:
                    boss_dmap[cid] = DUNGEON_BY_MAP[cmap].dungeon_id

        ref_rows_by_entry: dict[int, list[dict[str, Any]]] = {}
        if "reference_loot_template" in tables:
            for rr in _fetch_dicts(world_conn, "SELECT Entry, Item, Reference, Chance, GroupId FROM reference_loot_template"):
                ref_rows_by_entry.setdefault(int(rr["Entry"]), []).append(rr)

        clt_rows = _fetch_dicts(world_conn, "SELECT Entry, Item, Reference, Chance, GroupId FROM creature_loot_template ORDER BY Entry ASC, Item ASC")
        # Compute equal-weight group denominators for Chance == 0 rows (P1-10!)
        zero_group_counts: dict[tuple[int, int], int] = {}
        for r in clt_rows:
            if float(r.get("Chance") or 0.0) == 0.0 and int(r.get("GroupId") or 0) > 0:
                gk = (int(r["Entry"]), int(r["GroupId"]))
                zero_group_counts[gk] = zero_group_counts.get(gk, 0) + 1

        def _append_loot_source(boss_id: int, ientry: int, chance_val: float, group_id: int, ev_table: str, ev_key: tuple[Any, ...]) -> None:
            if ientry not in sources_by_entry:
                return
            did = boss_dmap.get(boss_id, "")
            spec = DUNGEON_BY_ID.get(did) if did else None
            bname = creature_names.get(boss_id, f"Creature #{boss_id}")
            if spec is not None:
                sources_by_entry[ientry].append(ItemSource(
                    source_kind="dungeon_drop",
                    source_id=spec.lfg_dungeon_id,
                    source_name=bname,
                    faction=spec.faction,
                    evidence_table=ev_table,
                    evidence_key=ev_key,
                    min_level=spec.min_level,
                    drop_chance_pct=round(max(0.01, min(100.0, chance_val)), 3),
                    dungeon_id=spec.dungeon_id,
                    boss_entry=boss_id,
                    raid_stage_min=spec.raid_stage_min,
                    estimated_minutes=spec.estimated_minutes + spec.travel_minutes,
                ))
            else:
                sources_by_entry[ientry].append(ItemSource(
                    source_kind="world_drop",
                    source_id=boss_id,
                    source_name=bname,
                    faction="both",
                    evidence_table=ev_table,
                    evidence_key=ev_key,
                    min_level=max(1, int(item_map[ientry].get("RequiredLevel") or 1)),
                    drop_chance_pct=round(max(0.01, min(100.0, chance_val)), 3),
                    boss_entry=boss_id,
                    raid_stage_min=0,
                    estimated_minutes=60,
                ))

        for r in clt_rows:
            boss_id = int(r["Entry"])
            ref_id = int(r.get("Reference") or 0)
            gid = int(r.get("GroupId") or 0)
            raw_ch = float(r.get("Chance") or 0.0)
            eff_ch = (100.0 / zero_group_counts[(boss_id, gid)]) if (raw_ch == 0.0 and gid > 0 and (boss_id, gid) in zero_group_counts) else raw_ch

            if ref_id > 0 and ref_id in ref_rows_by_entry:
                ref_list = ref_rows_by_entry[ref_id]
                ref_zeros: dict[int, int] = {}
                for rr in ref_list:
                    if float(rr.get("Chance") or 0.0) == 0.0 and int(rr.get("GroupId") or 0) > 0:
                        rg = int(rr["GroupId"])
                        ref_zeros[rg] = ref_zeros.get(rg, 0) + 1
                for rr in ref_list:
                    rg = int(rr.get("GroupId") or 0)
                    rch = float(rr.get("Chance") or 0.0)
                    eff_rch = (100.0 / ref_zeros[rg]) if (rch == 0.0 and rg > 0 and rg in ref_zeros) else rch
                    combined = (eff_ch / 100.0) * eff_rch if eff_ch > 0 else eff_rch
                    _append_loot_source(
                        boss_id, int(rr["Item"]), combined, rg,
                        "acore_world.reference_loot_template",
                        ("reference_loot_template", boss_id, ref_id, int(rr["Item"]), rg),
                    )
            else:
                _append_loot_source(
                    boss_id, int(r["Item"]), eff_ch, gid,
                    "acore_world.creature_loot_template",
                    ("creature_loot_template", boss_id, int(r["Item"]), gid),
                )

    # 3. Vendors (`npc_vendor`)
    if "npc_vendor" in tables:
        for v in _fetch_dicts(world_conn, "SELECT entry, item, maxcount, ExtendedCost FROM npc_vendor ORDER BY entry ASC, item ASC"):
            ientry = int(v["item"])
            if ientry not in sources_by_entry:
                continue
            it_row = item_map[ientry]
            req_rep_fac = int(it_row.get("RequiredReputationFaction") or 0)
            req_rep_rank = int(it_row.get("RequiredReputationRank") or 0)
            skind = "reputation_vendor" if req_rep_fac > 0 else "vendor"
            buy_price = max(0, int(it_row.get("BuyPrice") or 0))
            ext_cost = int(v.get("ExtendedCost") or 0)
            vmax = int(v.get("maxcount") or 0)
            sources_by_entry[ientry].append(ItemSource(
                source_kind=skind,
                source_id=int(v["entry"]),
                source_name=f"Vendor #{int(v['entry'])}",
                faction="both",
                evidence_table="acore_world.npc_vendor",
                evidence_key=("npc_vendor", int(v["entry"]), ientry, ext_cost),
                min_level=max(1, int(it_row.get("RequiredLevel") or 1)),
                drop_chance_pct=100.0,
                cost_copper=buy_price,
                extended_cost=ext_cost,
                vendor_max_count=vmax,
                required_reputation_faction=req_rep_fac,
                required_reputation_rank=req_rep_rank,
                raid_stage_min=0,
                estimated_minutes=10,
            ))

    # 4. Crafted recipes (optional `crafted_recipes` projection table keyed by `recipe_spell_id`)
    if "crafted_recipes" in tables:
        for cr in _fetch_dicts(world_conn, "SELECT * FROM crafted_recipes ORDER BY recipe_spell_id ASC"):
            ientry = int(cr["item_entry"])
            if ientry not in sources_by_entry:
                continue
            sources_by_entry[ientry].append(ItemSource(
                source_kind="crafted",
                source_id=int(cr["recipe_spell_id"]),
                source_name=str(cr.get("recipe_name") or f"Recipe #{cr['recipe_spell_id']}"),
                faction="both",
                evidence_table="acore_world.crafted_recipes",
                evidence_key=("crafted_recipes", int(cr["recipe_spell_id"]), ientry),
                min_level=max(1, int(item_map[ientry].get("RequiredLevel") or 1)),
                drop_chance_pct=100.0,
                cost_copper=max(0, int(cr.get("material_cost_copper") or 0)),
                required_skill=int(cr.get("required_skill") or 0),
                required_skill_rank=int(cr.get("required_skill_rank") or 0),
                recipe_spell_id=int(cr["recipe_spell_id"]),
                raid_stage_min=int(cr.get("raid_stage_min") or 0),
                estimated_minutes=max(5, int(cr.get("estimated_minutes") or 15)),
            ))

    out: dict[int, CatalogItem] = {}
    for entry, row in sorted(item_map.items()):
        out[entry] = decode_item_template_row(row, sources=tuple(sources_by_entry.get(entry, ())))
    return out


def load_character_state_from_db(char_conn: Any, guid: int) -> dict[str, Any]:
    """Deterministic installed-data loader for actual character gear, bags, quests, skills, rep, spells & money (P0-1)."""
    tables = _existing_tables(char_conn)
    crows = _fetch_dicts(char_conn, "SELECT guid, name, race, class, level, money FROM characters WHERE guid = ?", (int(guid),))
    if not crows:
        return {"ok": False, "reason": f"character_not_found:{guid}"}
    ch = crows[0]

    equipped: dict[str, int] = {}
    bag_counts: dict[int, int] = {}
    if "character_inventory" in tables and "item_instance" in tables:
        inv_rows = _fetch_dicts(
            char_conn,
            "SELECT ci.bag, ci.slot, ii.itemEntry, ii.count "
            "FROM character_inventory ci JOIN item_instance ii ON ii.guid = ci.item "
            "WHERE ci.guid = ? ORDER BY ci.bag ASC, ci.slot ASC",
            (int(guid),),
        )
        for r in inv_rows:
            bag = int(r["bag"])
            slot = int(r["slot"])
            entry = int(r["itemEntry"])
            cnt = max(1, int(r.get("count") or 1))
            if bag == 0 and slot in WOW_EQUIP_SLOT_MAP:
                equipped[WOW_EQUIP_SLOT_MAP[slot]] = entry
            else:
                bag_counts[entry] = bag_counts.get(entry, 0) + cnt

    completed_quests: set[int] = set()
    if "character_queststatus_rewarded" in tables:
        for r in _fetch_dicts(char_conn, "SELECT quest FROM character_queststatus_rewarded WHERE guid = ?", (int(guid),)):
            completed_quests.add(int(r["quest"]))

    skills: dict[int, int] = {}
    if "character_skills" in tables:
        for r in _fetch_dicts(char_conn, "SELECT skill, value FROM character_skills WHERE guid = ?", (int(guid),)):
            skills[int(r["skill"])] = int(r["value"])

    reputations: dict[int, int] = {}
    if "character_reputation" in tables:
        for r in _fetch_dicts(char_conn, "SELECT faction, standing FROM character_reputation WHERE guid = ?", (int(guid),)):
            # Convert WotLK raw standing value to reputation rank (0..7: Hated..Exalted)
            st = int(r["standing"])
            rank = 7 if st >= 42000 else (6 if st >= 21000 else (5 if st >= 9000 else (4 if st >= 3000 else (3 if st >= 0 else 2))))
            reputations[int(r["faction"])] = rank

    known_spells: set[int] = set()
    if "character_spell" in tables:
        for r in _fetch_dicts(char_conn, "SELECT spell FROM character_spell WHERE guid = ? AND active = 1", (int(guid),)):
            known_spells.add(int(r["spell"]))

    race_id = int(ch["race"])
    faction = "alliance" if race_id in (1, 3, 4, 7, 11) else ("horde" if race_id in (2, 5, 6, 8, 10) else "")
    return {
        "ok": True,
        "guid": int(ch["guid"]),
        "name": str(ch["name"]),
        "race_id": race_id,
        "class_id": int(ch["class"]),
        "faction": faction,
        "level": int(ch["level"]),
        "gold_copper": max(0, int(ch["money"] or 0)),
        "equipped_items": equipped,
        "bag_item_counts": bag_counts,
        "completed_quests": completed_quests,
        "skills": skills,
        "reputations": reputations,
        "known_spells": known_spells,
    }


def load_market_snapshot_from_db(
    char_conn: Any,
    *,
    observed_at: int,
    ttl_sec: int = 600,
    guild_craftable_entries: set[int] | frozenset[int] = frozenset(),
) -> MarketSnapshot:
    """Load volatile AH buyout listings from `auctionhouse` JOIN `item_instance` into a timestamped `MarketSnapshot` (P2-8)."""
    tables = _existing_tables(char_conn)
    listings_by_entry: dict[int, list[MarketListing]] = {}
    if "auctionhouse" in tables and "item_instance" in tables:
        rows = _fetch_dicts(
            char_conn,
            "SELECT ah.id, ah.houseid, ah.buyoutprice, ah.time, ii.itemEntry "
            "FROM auctionhouse ah JOIN item_instance ii ON ii.guid = ah.itemguid "
            "WHERE ah.buyoutprice > 0 AND ah.time > ? ORDER BY ah.buyoutprice ASC, ah.id ASC",
            (int(observed_at),),
        )
        for r in rows:
            hid = int(r["houseid"])
            # AzerothCore houseid: 2=Alliance, 6=Horde, 7=Neutral/Shared
            fac = "alliance" if hid == 2 else ("horde" if hid == 6 else "both")
            ientry = int(r["itemEntry"])
            listings_by_entry.setdefault(ientry, []).append(MarketListing(
                auction_id=int(r["id"]),
                item_entry=ientry,
                buyout_copper=int(r["buyoutprice"]),
                faction=fac,
                expires_at=int(r["time"]),
                evidence_key=("auctionhouse", int(r["id"]), ientry),
            ))
    return MarketSnapshot(
        observed_at=int(observed_at),
        expires_at=int(observed_at) + max(60, int(ttl_sec)),
        ah_listings_by_entry={k: tuple(v) for k, v in listings_by_entry.items()},
        guild_craftable_entries=frozenset(int(x) for x in guild_craftable_entries),
    )


def can_character_equip_item(
    item: CatalogItem,
    *,
    class_id: int,
    race_id: int,
    faction: str,
    level: int,
    role: str,
    target_slot: str,
    skills: dict[int, int] | None = None,
    reputations: dict[int, int] | None = None,
    known_spells: set[int] | frozenset[int] | None = None,
    check_level_now: bool = True,
) -> tuple[bool, str]:
    """Strict WotLK 3.3.5a legality check (class/race masks, FlagsExtra faction, armor, weapons, dual-wield, shield, holdable, relic)."""
    cls_name = CLASS_ID_TO_NAME.get(int(class_id), "")
    if not cls_name:
        return False, "unknown_class"
    if role not in CLASS_CAPABLE_ROLES.get(cls_name, frozenset()):
        return False, f"class_cannot_role:{cls_name}:{role}"

    fac = str(faction or "").strip().lower()
    if fac not in ("alliance", "horde"):
        return False, "unknown_faction"

    # P2-1: Enforce item_template.FlagsExtra faction restrictions!
    if (item.flags_extra & ITEM_FLAG_EXTRA_HORDE_ONLY) and fac != "horde":
        return False, "item_horde_only"
    if (item.flags_extra & ITEM_FLAG_EXTRA_ALLIANCE_ONLY) and fac != "alliance":
        return False, "item_alliance_only"

    valid_slots = INVTYPE_TO_SLOTS.get(item.inventory_type, ())
    if target_slot not in valid_slots:
        return False, "wrong_inventory_slot"

    if item.allowable_class != -1:
        if (item.allowable_class & (1 << (int(class_id) - 1))) == 0:
            return False, "class_mask_disallowed"
    if item.allowable_race != -1:
        if (item.allowable_race & (1 << (int(race_id) - 1))) == 0:
            return False, "race_mask_disallowed"

    # Armor & off-hand proficiency checks
    if item.item_class == ITEM_CLASS_ARMOR:
        if item.item_subclass == ARMOR_SUBCLASS_SHIELD:
            if cls_name not in SHIELD_CLASSES:
                return False, "class_cannot_use_shield"
        elif item.inventory_type == INVTYPE_HOLDABLE:
            # P2-7: Shield tanks require a shield, not a caster holdable orb/tome!
            if role == "tank" and cls_name in ("warrior", "paladin"):
                return False, "tank_requires_shield_not_holdable"
        elif item.item_subclass in (ARMOR_SUBCLASS_LIBRAM, ARMOR_SUBCLASS_IDOL, ARMOR_SUBCLASS_TOTEM, ARMOR_SUBCLASS_SIGIL):
            if RELIC_SUBCLASS_BY_CLASS.get(cls_name) != item.item_subclass:
                return False, "wrong_class_relic"
        elif item.item_subclass in (ARMOR_SUBCLASS_CLOTH, ARMOR_SUBCLASS_LEATHER, ARMOR_SUBCLASS_MAIL, ARMOR_SUBCLASS_PLATE):
            if item.inventory_type != INVTYPE_CLOAK:
                max_pre40, max_40plus = CLASS_ARMOR_MAX[cls_name]
                eff_max = max_40plus if int(level) >= 40 else max_pre40
                if item.item_subclass > eff_max:
                    return False, "armor_subclass_above_class_proficiency"
            else:
                # Cloaks are always Cloth in stock WotLK; reject non-Cloth cloaks for Cloth-only classes
                max_pre40, max_40plus = CLASS_ARMOR_MAX[cls_name]
                eff_max = max_40plus if int(level) >= 40 else max_pre40
                if item.item_subclass > eff_max:
                    return False, "cloak_armor_subclass_above_class_proficiency"

    # Weapon usability & dual-wield level/talent checks (P2-6!)
    if item.item_class == ITEM_CLASS_WEAPON:
        if item.item_subclass not in CLASS_USABLE_WEAPONS.get(cls_name, frozenset()):
            return False, f"weapon_subclass_unusable:{item.item_subclass}"
        if target_slot == "off_hand" and item.inventory_type in (INVTYPE_ONE_HAND, INVTYPE_OFFHAND_WEAPON):
            dw_min = DUAL_WIELD_MIN_LEVEL.get(cls_name)
            if dw_min is None:
                return False, "class_cannot_dual_wield"
            if int(level) < dw_min:
                return False, f"dual_wield_requires_level:{dw_min}"
            if cls_name == "shaman" and known_spells is not None and SHAMAN_DUAL_WIELD_SPELL_ID not in known_spells:
                return False, "shaman_dual_wield_talent_missing"
            if role == "tank" and cls_name in ("warrior", "paladin"):
                return False, "tank_requires_shield_in_offhand"
        if target_slot == "main_hand" and item.inventory_type == INVTYPE_TWO_HAND:
            if role == "tank" and cls_name in ("warrior", "paladin"):
                return False, "shield_tank_cannot_use_2h"

    if check_level_now:
        if item.required_level > int(level):
            return False, f"requires_level:{item.required_level}"
        if item.required_skill > 0 and int((skills or {}).get(item.required_skill, 0)) < item.required_skill_rank:
            return False, f"requires_skill:{item.required_skill}:{item.required_skill_rank}"
        if item.required_reputation_faction > 0 and int((reputations or {}).get(item.required_reputation_faction, 0)) < item.required_reputation_rank:
            return False, f"requires_reputation:{item.required_reputation_faction}:{item.required_reputation_rank}"

    return True, "ok"


def score_item_for_role(
    item: CatalogItem,
    *,
    class_id: int,
    role: str,
    level: int,
) -> tuple[float, dict[str, float]]:
    """Deterministic transparent item score + per-stat breakdown for `(class_id, role)`."""
    cls_name = CLASS_ID_TO_NAME.get(int(class_id), "")
    weights = ROLE_STAT_WEIGHTS.get((cls_name, role), {"stamina": 1.0})
    breakdown: dict[str, float] = {}
    total = 0.0

    for stat_name, val in sorted(item.stats.items()):
        w = weights.get(stat_name, 0.0)
        if w != 0.0 and val != 0.0:
            contrib = round(val * w, 3)
            breakdown[stat_name] = contrib
            total += contrib

    if item.armor > 0 and weights.get("armor", 0.0) > 0:
        c_arm = round(item.armor * weights["armor"], 3)
        breakdown["armor"] = c_arm
        total += c_arm

    # P2-5: Separate melee_dps vs ranged_dps so melee warriors don't get 4.5x DPS on a stat-stick bow!
    if item.dps > 0 and item.item_class == ITEM_CLASS_WEAPON:
        is_ranged = item.inventory_type in (15, 25, 26)
        dps_key = "ranged_dps" if is_ranged else "melee_dps"
        if weights.get(dps_key, 0.0) > 0:
            c_dps = round(item.dps * weights[dps_key], 3)
            breakdown[dps_key] = c_dps
            total += c_dps

    ilvl_tie = round(item.item_level * 0.05, 3)
    breakdown["ilvl_tiebreak"] = ilvl_tie
    total += ilvl_tie

    # Policy penalty (soft, NOT hard legality — P2-7) for wearing below-primary armor tier
    if (
        item.item_class == ITEM_CLASS_ARMOR
        and item.inventory_type != INVTYPE_CLOAK
        and item.item_subclass in (ARMOR_SUBCLASS_CLOTH, ARMOR_SUBCLASS_LEATHER, ARMOR_SUBCLASS_MAIL, ARMOR_SUBCLASS_PLATE)
        and cls_name in CLASS_ARMOR_MAX
    ):
        max_pre40, max_40plus = CLASS_ARMOR_MAX[cls_name]
        preferred = max_40plus if int(level) >= 40 else max_pre40
        if item.item_subclass < preferred:
            mult = 6.0 if role == "tank" else (1.5 if role == "healer" else 3.0)
            pen = round(-mult * (preferred - item.item_subclass), 3)
            breakdown["suboptimal_armor_tier"] = pen
            total += pen

    return round(total, 3), breakdown


def _build_proposal(
    *,
    proposal_kind: str,
    source_kind: str,
    item: CatalogItem,
    target_id: int,
    target_name: str,
    evidence_table: str,
    evidence_key: tuple[Any, ...],
    reason: str,
    identity: dict[str, Any] | None = None,
    now_ts: int = 0,
    dungeon_id: str = "",
    extra: dict[str, Any] | None = None,
    force_advisory: bool = False,
    registered_executors: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Construct a typed native proposal passing through the single live-OFF + executor registry gate (P0-2/P0-3/P2-9)."""
    if force_advisory or proposal_kind == "advisory_gear_target":
        is_exec = False
        exec_reason = reason
    else:
        is_exec, gate_reason = _is_proposal_executable(
            proposal_kind, dungeon_id=dungeon_id, registered_executors=registered_executors
        )
        exec_reason = reason if is_exec else gate_reason

    ident = identity or {}
    pid = str(ident.get("persona_id") or "")
    rid = int(ident.get("realm_id") or 0)
    cguid = int(ident.get("character_guid") or 0)
    inc = int(ident.get("incarnation") or 0)
    idem_key = f"gear:{pid}:{rid}:{cguid}:{inc}:{proposal_kind}:{item.entry}:{target_id}"
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "proposal_kind": proposal_kind,
        "source_kind": source_kind,
        "item_entry": item.entry,
        "target_id": int(target_id),
        "target_name": target_name,
        "executable": bool(is_exec),
        "advisory_only": not bool(is_exec),
        "reason": exec_reason[:96],
        "evidence_table": evidence_table,
        "evidence_key": list(evidence_key),
        "persona_id": pid,
        "realm_id": rid,
        "character_guid": cguid,
        "incarnation": inc,
        "idempotency_key": idem_key,
        "expires_at": (int(now_ts) + 3600) if int(now_ts) > 0 else 0,
    }
    if extra:
        payload.update(extra)
    return payload


def evaluate_source_attainability(
    item: CatalogItem,
    source: ItemSource,
    *,
    class_id: int,
    race_id: int,
    level: int,
    faction: str,
    raid_stage: int,
    completed_quests: set[int],
    skills: dict[int, int],
    reputations: dict[int, int],
    known_spells: set[int] | None,
    gold_copper: int,
    raid_readiness_gold_reserve_copper: int,
    locked_dungeon_maps: set[int],
    bracket_max_level: int,
    market_snapshot: MarketSnapshot | None = None,
    identity: dict[str, Any] | None = None,
    now_ts: int = 0,
    registered_executors: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Classify a single verified `ItemSource` with strict provenance & fail-closed checks (P0-1..P0-3, P1-8..P1-10)."""
    if source.source_kind not in VALID_SOURCE_KINDS:
        return {"status": "unavailable", "reason": f"unknown_source_kind:{source.source_kind}"}
    if not source.evidence_table or not source.evidence_key:
        return {"status": "unavailable", "reason": "missing_source_evidence_provenance"}
    if source.faction not in ("alliance", "horde", "both"):
        return {"status": "unavailable", "reason": "unknown_source_faction"}
    if source.faction != "both" and source.faction != faction.lower():
        return {"status": "unavailable", "reason": f"wrong_faction:{faction}!={source.faction}"}

    eff_stage_req = max(item.raid_stage_min, source.raid_stage_min)
    if eff_stage_req > int(raid_stage):
        return {
            "status": "aspirational",
            "reason": f"future_raid_stage:{eff_stage_req}>{raid_stage}",
            "proposal": _build_proposal(
                proposal_kind="advisory_gear_target", source_kind=source.source_kind, item=item,
                target_id=source.source_id, target_name=source.source_name,
                evidence_table=source.evidence_table, evidence_key=source.evidence_key,
                reason=f"future_raid_stage:{eff_stage_req}", identity=identity, now_ts=now_ts, force_advisory=True,
            ),
        }

    req_lvl = max(item.required_level, source.min_level)
    if req_lvl > int(bracket_max_level):
        return {"status": "unavailable", "reason": f"above_bracket_cap:{req_lvl}>{bracket_max_level}"}

    discretionary_copper = max(0, int(gold_copper) - max(0, int(raid_readiness_gold_reserve_copper)))
    aspirational_reasons: list[str] = []

    if req_lvl > int(level):
        aspirational_reasons.append(f"requires_level:{req_lvl}")
    if item.required_reputation_faction > 0:
        if int(reputations.get(item.required_reputation_faction, 0)) < item.required_reputation_rank:
            aspirational_reasons.append(f"requires_item_rep:{item.required_reputation_faction}:{item.required_reputation_rank}")
    if source.required_reputation_faction > 0:
        if int(reputations.get(source.required_reputation_faction, 0)) < source.required_reputation_rank:
            aspirational_reasons.append(f"requires_source_rep:{source.required_reputation_faction}:{source.required_reputation_rank}")
    if item.required_skill > 0 and int(skills.get(item.required_skill, 0)) < item.required_skill_rank:
        aspirational_reasons.append(f"requires_item_skill:{item.required_skill}:{item.required_skill_rank}")

    if source.source_kind == "quest_reward":
        if source.source_id in completed_quests:
            return {"status": "unavailable", "reason": f"quest_already_completed:{source.source_id}"}
        # P1-8: Quest race/class masks, prerequisite chain & group quest checks!
        if source.quest_allowable_races > 0 and (source.quest_allowable_races & (1 << (int(race_id) - 1))) == 0:
            return {"status": "unavailable", "reason": "quest_race_disallowed"}
        if source.quest_allowable_classes > 0 and (source.quest_allowable_classes & (1 << (int(class_id) - 1))) == 0:
            return {"status": "unavailable", "reason": "quest_class_disallowed"}
        if source.prev_quest_id > 0 and source.prev_quest_id not in completed_quests:
            aspirational_reasons.append(f"requires_prev_quest:{source.prev_quest_id}")
        if aspirational_reasons:
            return {
                "status": "aspirational",
                "reason": ",".join(aspirational_reasons),
                "expected_minutes": int(source.estimated_minutes or 30),
                "expected_runs": 1,
                "proposal": _build_proposal(
                    proposal_kind="advisory_gear_target", source_kind="quest_reward", item=item,
                    target_id=source.source_id, target_name=source.source_name,
                    evidence_table=source.evidence_table, evidence_key=source.evidence_key,
                    reason=aspirational_reasons[0], identity=identity, now_ts=now_ts, force_advisory=True,
                ),
            }
        # Group quests (suggested_group_num > 1) are advisory until a group forms
        is_group_q = int(source.suggested_group_num) > 1
        return {
            "status": "attainable_now",
            "reason": "group_quest_advisory" if is_group_q else "quest_available",
            "expected_minutes": max(5, int(source.estimated_minutes or (45 if is_group_q else 25))),
            "expected_runs": 1,
            "proposal": _build_proposal(
                proposal_kind="native_quest_accept", source_kind="quest_reward", item=item,
                target_id=source.source_id, target_name=source.source_name,
                evidence_table=source.evidence_table, evidence_key=source.evidence_key,
                reason="group_quest_requires_party" if is_group_q else "quest_reward_upgrade",
                identity=identity, now_ts=now_ts, force_advisory=is_group_q,
                registered_executors=registered_executors,
                extra={"is_choice_reward": bool(source.is_choice_reward), "suggested_group_num": int(source.suggested_group_num)},
            ),
        }

    if source.source_kind == "crafted":
        has_skill = source.required_skill > 0 and int(skills.get(source.required_skill, 0)) >= source.required_skill_rank
        has_recipe = (source.recipe_spell_id <= 0) or (known_spells is None) or (source.recipe_spell_id in known_spells)
        can_self_craft = has_skill and has_recipe
        guild_has_crafter = (
            market_snapshot is not None
            and item.entry in market_snapshot.guild_craftable_entries
            and item.bonding != 1
        )
        if not can_self_craft and not guild_has_crafter:
            aspirational_reasons.append(f"requires_craft_skill_or_guild_crafter:{source.required_skill}:{source.required_skill_rank}")
        if source.cost_copper > discretionary_copper:
            aspirational_reasons.append(f"insufficient_gold_preserving_readiness:{source.cost_copper}>{discretionary_copper}")
        if aspirational_reasons:
            return {
                "status": "aspirational",
                "reason": ",".join(aspirational_reasons),
                "expected_minutes": int(source.estimated_minutes or 20),
                "expected_runs": 1,
                "proposal": _build_proposal(
                    proposal_kind="advisory_gear_target", source_kind="crafted", item=item,
                    target_id=source.source_id, target_name=source.source_name,
                    evidence_table=source.evidence_table, evidence_key=source.evidence_key,
                    reason=aspirational_reasons[0], identity=identity, now_ts=now_ts, force_advisory=True,
                ),
            }
        prop_kind = "native_self_craft" if can_self_craft else "native_guild_work_order"
        return {
            "status": "attainable_now",
            "reason": "craftable_now" if can_self_craft else "guild_work_order_eligible",
            "expected_minutes": max(5, int(source.estimated_minutes or 15)),
            "expected_runs": 1,
            "proposal": _build_proposal(
                proposal_kind=prop_kind, source_kind="crafted", item=item,
                target_id=source.source_id, target_name=source.source_name,
                evidence_table=source.evidence_table, evidence_key=source.evidence_key,
                reason=prop_kind, identity=identity, now_ts=now_ts,
                registered_executors=registered_executors,
                extra={"cost_copper": int(source.cost_copper)},
            ),
        }

    if source.source_kind in ("vendor", "reputation_vendor"):
        # P1-9: ExtendedCost (emblems/badges/honor) & limited stock (maxcount > 0) stay aspirational/advisory!
        if source.extended_cost > 0:
            aspirational_reasons.append(f"extended_cost_unmodeled:{source.extended_cost}")
        if source.vendor_max_count > 0:
            aspirational_reasons.append(f"limited_vendor_stock:{source.vendor_max_count}")
        if source.cost_copper > discretionary_copper:
            aspirational_reasons.append(f"insufficient_gold_preserving_readiness:{source.cost_copper}>{discretionary_copper}")
        if aspirational_reasons:
            return {
                "status": "aspirational",
                "reason": ",".join(aspirational_reasons),
                "expected_minutes": int(source.estimated_minutes or 10),
                "expected_runs": 1,
                "proposal": _build_proposal(
                    proposal_kind="advisory_gear_target", source_kind=source.source_kind, item=item,
                    target_id=source.source_id, target_name=source.source_name,
                    evidence_table=source.evidence_table, evidence_key=source.evidence_key,
                    reason=aspirational_reasons[0], identity=identity, now_ts=now_ts, force_advisory=True,
                ),
            }
        return {
            "status": "attainable_now",
            "reason": "vendor_purchasable_now",
            "expected_minutes": max(5, int(source.estimated_minutes or 10)),
            "expected_runs": 1,
            "proposal": _build_proposal(
                proposal_kind="native_vendor_purchase", source_kind=source.source_kind, item=item,
                target_id=source.source_id, target_name=source.source_name,
                evidence_table=source.evidence_table, evidence_key=source.evidence_key,
                reason="vendor_upgrade_within_budget", identity=identity, now_ts=now_ts,
                registered_executors=registered_executors,
                extra={"cost_copper": int(source.cost_copper)},
            ),
        }

    if source.source_kind == "auction_house":
        if item.bonding == 1:
            return {"status": "unavailable", "reason": "bop_cannot_be_auctioned"}
        if source.cost_copper <= 0 or source.cost_copper > discretionary_copper:
            aspirational_reasons.append(f"insufficient_gold_preserving_readiness:{source.cost_copper}>{discretionary_copper}")
        if aspirational_reasons:
            return {
                "status": "aspirational",
                "reason": ",".join(aspirational_reasons),
                "expected_minutes": 5,
                "expected_runs": 1,
                "proposal": _build_proposal(
                    proposal_kind="advisory_gear_target", source_kind="auction_house", item=item,
                    target_id=source.source_id, target_name=source.source_name,
                    evidence_table=source.evidence_table, evidence_key=source.evidence_key,
                    reason=aspirational_reasons[0], identity=identity, now_ts=now_ts, force_advisory=True,
                ),
            }
        return {
            "status": "attainable_now",
            "reason": "ah_buyout_available",
            "expected_minutes": 5,
            "expected_runs": 1,
            "proposal": _build_proposal(
                proposal_kind="native_ah_buyout", source_kind="auction_house", item=item,
                target_id=source.source_id, target_name=source.source_name,
                evidence_table=source.evidence_table, evidence_key=source.evidence_key,
                reason="ah_upgrade_within_budget", identity=identity, now_ts=now_ts,
                registered_executors=registered_executors,
                extra={"cost_copper": int(source.cost_copper)},
            ),
        }

    if source.source_kind == "dungeon_drop":
        spec = DUNGEON_BY_ID.get(source.dungeon_id)
        if spec is None:
            return {
                "status": "aspirational",
                "reason": f"uncataloged_dungeon:{source.dungeon_id}",
                "expected_minutes": int(source.estimated_minutes or 60),
                "expected_runs": 1,
                "proposal": _build_proposal(
                    proposal_kind="advisory_gear_target", source_kind="dungeon_drop", item=item,
                    target_id=source.source_id, target_name=source.source_name,
                    evidence_table=source.evidence_table, evidence_key=source.evidence_key,
                    reason="uncataloged_dungeon", identity=identity, now_ts=now_ts, force_advisory=True,
                ),
            }
        if spec.faction != "both" and spec.faction != faction.lower():
            return {"status": "unavailable", "reason": f"dungeon_wrong_faction:{faction}"}
        # P1-10: Fail closed when drop_chance_pct is unknown/None!
        if source.drop_chance_pct is None or float(source.drop_chance_pct) <= 0.0:
            aspirational_reasons.append("unknown_drop_chance")
            exp_runs = 10
            drop_pct = 0.0
        else:
            drop_pct = min(100.0, float(source.drop_chance_pct))
            exp_runs = max(1, int(math.ceil(100.0 / drop_pct)))
        exp_mins = exp_runs * (spec.estimated_minutes + spec.travel_minutes)

        if spec.map_id in locked_dungeon_maps:
            aspirational_reasons.append(f"instance_locked:{spec.map_id}")
        if int(level) < spec.min_level or int(level) > spec.max_level:
            aspirational_reasons.append(f"outside_lfg_band:{level}!={spec.min_level}-{spec.max_level}")
        if not spec.supported:
            aspirational_reasons.append(f"unsupported_dungeon:{spec.dungeon_id}")

        if aspirational_reasons:
            return {
                "status": "aspirational",
                "reason": ",".join(aspirational_reasons),
                "expected_minutes": exp_mins,
                "expected_runs": exp_runs,
                "proposal": _build_proposal(
                    proposal_kind="advisory_gear_target", source_kind="dungeon_drop", item=item,
                    target_id=spec.lfg_dungeon_id, target_name=f"{spec.name} ({source.source_name})",
                    evidence_table=source.evidence_table, evidence_key=source.evidence_key,
                    reason=aspirational_reasons[0], identity=identity, now_ts=now_ts, force_advisory=True,
                ),
            }
        return {
            "status": "attainable_now",
            "reason": "rdf_dungeon_drop_eligible",
            "expected_minutes": exp_mins,
            "expected_runs": exp_runs,
            "proposal": _build_proposal(
                proposal_kind="rdf_dungeon_queue", source_kind="dungeon_drop", item=item,
                target_id=spec.lfg_dungeon_id, target_name=f"{spec.name} ({source.source_name})",
                evidence_table=source.evidence_table, evidence_key=source.evidence_key,
                reason="rdf_queue_eligible", identity=identity, now_ts=now_ts,
                dungeon_id=spec.dungeon_id, registered_executors=registered_executors,
                extra={"dungeon_id": spec.dungeon_id, "boss_entry": int(source.boss_entry), "drop_chance_pct": drop_pct},
            ),
        }

    # raid_drop / world_drop -> ALWAYS aspirational + advisory_only
    dp = float(source.drop_chance_pct) if (source.drop_chance_pct is not None and source.drop_chance_pct > 0) else 5.0
    return {
        "status": "aspirational",
        "reason": f"unsupported_autonomous_source:{source.source_kind}",
        "expected_minutes": max(60, int(source.estimated_minutes or 60)),
        "expected_runs": max(1, int(math.ceil(100.0 / dp))),
        "proposal": _build_proposal(
            proposal_kind="advisory_gear_target", source_kind=source.source_kind, item=item,
            target_id=source.source_id, target_name=source.source_name,
            evidence_table=source.evidence_table, evidence_key=source.evidence_key,
            reason=f"unsupported_autonomous_source:{source.source_kind}", identity=identity, now_ts=now_ts, force_advisory=True,
        ),
    }


def _is_unique_item(item: CatalogItem) -> bool:
    return (item.max_count == 1) or bool(item.flags & ITEM_FLAG_UNIQUE_EQUIPPABLE)


def _index_catalog_by_slot(catalog: dict[int, CatalogItem]) -> dict[str, list[CatalogItem]]:
    """Index catalog items by canonical slot sorted deterministically by entry ID (P3 performance & determinism!)."""
    by_slot: dict[str, list[CatalogItem]] = {s: [] for s in ALL_EQUIPMENT_SLOTS}
    for entry in sorted(catalog.keys()):
        it = catalog[entry]
        for s in INVTYPE_TO_SLOTS.get(it.inventory_type, ()):
            by_slot[s].append(it)
    return by_slot


def plan_character_gear_ambitions(
    *,
    catalog: dict[int, CatalogItem],
    class_id: int,
    race_id: int,
    faction: str,
    level: int,
    role: str,
    equipped_items: dict[str, Any],
    bag_item_entries: set[int] | frozenset[int] | None = None,
    bag_item_counts: dict[int, int] | None = None,
    completed_quests: set[int] | frozenset[int] = frozenset(),
    skills: dict[int, int] | None = None,
    reputations: dict[int, int] | None = None,
    known_spells: set[int] | frozenset[int] | None = None,
    gold_copper: int = 0,
    raid_readiness_gold_reserve_copper: int = 0,
    raid_readiness_protected_slots: set[str] | frozenset[str] = frozenset(),
    readiness_policy: dict[str, Any] | None = None,
    require_readiness_policy: bool = False,
    locked_dungeon_maps: set[int] | frozenset[int] = frozenset(),
    market_snapshot: MarketSnapshot | None = None,
    active_ambition_entries: set[int] | frozenset[int] = frozenset(),
    identity: dict[str, Any] | None = None,
    raid_stage: int = 0,
    brackets: tuple[str, ...] = ("current",),
    xp_cap_level: int | None = None,
    xp_fail_closed: bool = False,
    max_targets_per_slot: int = DEFAULT_MAX_TARGETS_PER_SLOT,
    min_upgrade_delta: float = DEFAULT_MIN_UPGRADE_DELTA,
    now_ts: int | None = None,
    registered_executors: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Compute bounded per-slot ranked attainable vs aspirational gear ambitions across requested brackets."""
    ts = int(now_ts if now_ts is not None else time.time())
    if require_readiness_policy and (not isinstance(readiness_policy, dict) or not readiness_policy.get("ok")):
        return {"ok": False, "reason": "readiness_policy_missing_or_unhealthy"}

    eff_reserve = int(raid_readiness_gold_reserve_copper)
    eff_protected = {str(s).strip().lower() for s in (raid_readiness_protected_slots or ())}
    if isinstance(readiness_policy, dict):
        eff_reserve = max(eff_reserve, int(readiness_policy.get("gold_reserve_copper") or 0))
        for ps in (readiness_policy.get("protected_slots") or ()):
            eff_protected.add(str(ps).strip().lower())

    eff_max_targets = max(1, min(int(max_targets_per_slot), MAX_TARGETS_PER_SLOT_CEILING))
    try:
        horizon_brackets = resolve_horizon_brackets(
            level,
            raid_stage=raid_stage,
            brackets=brackets,
            xp_cap_level=xp_cap_level,
            xp_fail_closed=xp_fail_closed,
        )
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}

    cls_name = CLASS_ID_TO_NAME.get(int(class_id), "")
    if not cls_name or role not in CLASS_CAPABLE_ROLES.get(cls_name, frozenset()):
        return {"ok": False, "reason": f"invalid_class_or_role:{class_id}:{role}"}

    skills_map = dict(skills or {})
    rep_map = dict(reputations or {})
    spells_set = set(known_spells) if known_spells is not None else None
    comp_q = set(completed_quests or ())
    locked_maps = set(locked_dungeon_maps or ())

    # Merge bag_item_counts & legacy bag_item_entries cleanly
    bags: dict[int, int] = {}
    if bag_item_counts:
        for k, v in bag_item_counts.items():
            if int(k) > 0 and int(v) > 0:
                bags[int(k)] = int(v)
    if bag_item_entries:
        for e in bag_item_entries:
            if int(e) > 0 and int(e) not in bags:
                bags[int(e)] = 1

    # Resolve equipped items; P1-3 fail closed on unknown equipped item entries!
    equipped_resolved: dict[str, CatalogItem | None] = {}
    equipped_scores: dict[str, float] = {}
    unknown_equipped_slots: set[str] = set()
    equipped_counts: dict[int, int] = {}
    equipped_limit_cats: dict[int, int] = {}

    for slot in ALL_EQUIPMENT_SLOTS:
        raw_eq = equipped_items.get(slot)
        c_item: CatalogItem | None = None
        if isinstance(raw_eq, CatalogItem):
            c_item = raw_eq
        elif raw_eq is not None and raw_eq != "" and raw_eq != 0:
            try:
                eq_id = int(raw_eq)
            except (TypeError, ValueError):
                unknown_equipped_slots.add(slot)
                equipped_resolved[slot] = None
                equipped_scores[slot] = 0.0
                continue
            if eq_id > 0:
                c_item = catalog.get(eq_id)
                if c_item is None:
                    # P1-3: Fail closed — never treat an uncataloged equipped item as score 0!
                    unknown_equipped_slots.add(slot)
        equipped_resolved[slot] = c_item
        if c_item is not None:
            equipped_counts[c_item.entry] = equipped_counts.get(c_item.entry, 0) + 1
            if c_item.item_limit_category > 0:
                equipped_limit_cats[c_item.item_limit_category] = equipped_limit_cats.get(c_item.item_limit_category, 0) + 1
            sc, _ = score_item_for_role(c_item, class_id=class_id, role=role, level=level)
            equipped_scores[slot] = sc
        else:
            equipped_scores[slot] = 0.0

    # Active market snapshot valid ONLY if unexpired
    active_market = market_snapshot if (market_snapshot is not None and market_snapshot.expires_at > ts) else None
    by_slot = _index_catalog_by_slot(catalog)

    # P1-2: Compute best usable 1H and OH scores available in bags/equipped so 2H <-> 1H+OH comparisons are exact!
    eq_mh = equipped_resolved.get("main_hand")
    eq_oh = equipped_resolved.get("off_hand")
    wearing_2h = (eq_mh is not None and eq_mh.inventory_type == INVTYPE_TWO_HAND)
    current_weapon_combo_score = equipped_scores.get("main_hand", 0.0) + equipped_scores.get("off_hand", 0.0)

    best_owned_1h_score = 0.0
    best_owned_oh_score = equipped_scores.get("off_hand", 0.0) if not wearing_2h else 0.0
    for b_entry in sorted(bags.keys()):
        bit = catalog.get(b_entry)
        if bit is None:
            continue
        if bit.inventory_type in (INVTYPE_ONE_HAND, INVTYPE_MAIN_HAND):
            ok_1h, _ = can_character_equip_item(
                bit, class_id=class_id, race_id=race_id, faction=faction, level=level, role=role,
                target_slot="main_hand", skills=skills_map, reputations=rep_map, known_spells=spells_set, check_level_now=True,
            )
            if ok_1h:
                best_owned_1h_score = max(best_owned_1h_score, score_item_for_role(bit, class_id=class_id, role=role, level=level)[0])
        if "off_hand" in INVTYPE_TO_SLOTS.get(bit.inventory_type, ()):
            ok_oh, _ = can_character_equip_item(
                bit, class_id=class_id, race_id=race_id, faction=faction, level=level, role=role,
                target_slot="off_hand", skills=skills_map, reputations=rep_map, known_spells=spells_set, check_level_now=True,
            )
            if ok_oh:
                best_owned_oh_score = max(best_owned_oh_score, score_item_for_role(bit, class_id=class_id, role=role, level=level)[0])

    def _plan_for_bracket(bracket_info: dict[str, Any]) -> dict[str, Any]:
        is_future_bracket = bracket_info["horizon_kind"] != "current"
        b_min = int(bracket_info["min_level"])
        b_max = int(bracket_info["max_level"])
        # Endgame cap brackets (60/70/80) include endgame pre-BiS items within 5 levels of cap (e.g. Lv55-60)!
        eff_b_min = max(1, b_max - 5) if (b_min == b_max and b_max >= 60) else b_min

        # Track bag upgrades per item so 1 copy in bags emits AT MOST ONE equip proposal (P1-1!)
        bag_upgrade_proposals: dict[int, list[dict[str, Any]]] = {}
        raw_slot_att: dict[str, list[dict[str, Any]]] = {}
        raw_slot_asp: dict[str, list[dict[str, Any]]] = {}
        slot_meta: dict[str, dict[str, Any]] = {}

        for slot in ALL_EQUIPMENT_SLOTS:
            cur_item = equipped_resolved[slot]
            cur_score = equipped_scores[slot]
            if slot in eff_protected:
                slot_meta[slot] = {"protected_by_raid_readiness": True, "unknown_equipped": False}
                raw_slot_att[slot] = []
                raw_slot_asp[slot] = []
                continue
            if slot in unknown_equipped_slots:
                slot_meta[slot] = {"protected_by_raid_readiness": False, "unknown_equipped": True}
                raw_slot_att[slot] = []
                raw_slot_asp[slot] = []
                continue

            slot_meta[slot] = {"protected_by_raid_readiness": False, "unknown_equipped": False}
            paired_slot = PAIRED_SLOTS.get(slot)
            paired_item = equipped_resolved.get(paired_slot) if paired_slot else None

            att_list: list[dict[str, Any]] = []
            asp_list: list[dict[str, Any]] = []

            for item in by_slot[slot]:
                if cur_item is not None and item.entry == cur_item.entry:
                    continue
                # P1-2: Never recommend a 2H weapon when off_hand is a raid-readiness protected slot!
                if slot == "main_hand" and item.inventory_type == INVTYPE_TWO_HAND and "off_hand" in eff_protected:
                    continue
                # Unique-Equipped & ItemLimitCategory conflict with paired/equipped slots
                if _is_unique_item(item) and paired_item is not None and paired_item.entry == item.entry:
                    continue
                if item.item_limit_category > 0 and equipped_limit_cats.get(item.item_limit_category, 0) > 0:
                    if cur_item is None or cur_item.item_limit_category != item.item_limit_category:
                        continue
                # Unique / Unique(N) owned count cap
                total_owned = equipped_counts.get(item.entry, 0) + bags.get(item.entry, 0)
                if item.max_count > 0 and total_owned >= item.max_count and item.entry not in bags:
                    continue

                if item.required_level > b_max:
                    continue
                if is_future_bracket and item.required_level < eff_b_min:
                    continue

                ok_struct, _ = can_character_equip_item(
                    item, class_id=class_id, race_id=race_id, faction=faction,
                    level=max(level, min(item.required_level, b_max)), role=role, target_slot=slot,
                    skills=skills_map, reputations=rep_map, known_spells=spells_set, check_level_now=False,
                )
                if not ok_struct:
                    continue

                cand_score, breakdown = score_item_for_role(item, class_id=class_id, role=role, level=level)

                # P1-2: Bidirectional 2H vs 1H+OH combo scoring!
                if slot == "main_hand" and item.inventory_type == INVTYPE_TWO_HAND:
                    upgrade_delta = round(cand_score - current_weapon_combo_score, 3)
                elif slot == "main_hand" and wearing_2h:
                    upgrade_delta = round((cand_score + best_owned_oh_score) - current_weapon_combo_score, 3)
                elif slot == "off_hand" and wearing_2h:
                    if best_owned_1h_score <= 0.0:
                        continue  # Cannot equip an OH alone without unequipping the 2H!
                    upgrade_delta = round((cand_score + best_owned_1h_score) - current_weapon_combo_score, 3)
                else:
                    upgrade_delta = round(cand_score - cur_score, 3)

                if upgrade_delta < float(min_upgrade_delta):
                    continue

                # Owned in bags -> collect candidate equip proposal (deduped across paired slots below!)
                if bags.get(item.entry, 0) > 0 and not is_future_bracket:
                    ok_now, _ = can_character_equip_item(
                        item, class_id=class_id, race_id=race_id, faction=faction, level=level, role=role,
                        target_slot=slot, skills=skills_map, reputations=rep_map, known_spells=spells_set, check_level_now=True,
                    )
                    if ok_now:
                        bag_upgrade_proposals.setdefault(item.entry, []).append({
                            "slot": slot,
                            "item_entry": item.entry,
                            "item_name": item.name,
                            "score": cand_score,
                            "upgrade_delta": upgrade_delta,
                            "is_unique": _is_unique_item(item),
                            "proposal": _build_proposal(
                                proposal_kind="native_equip_owned_item", source_kind="owned_in_bags", item=item,
                                target_id=item.entry, target_name=item.name,
                                evidence_table=item.evidence_table, evidence_key=("character_inventory", item.entry),
                                reason="owned_bag_upgrade", identity=identity, now_ts=ts,
                                registered_executors=registered_executors,
                            ),
                        })
                    continue

                # Combine deterministic catalog sources + active volatile AH market listings (P2-8)
                all_sources: list[ItemSource] = list(item.sources)
                if active_market is not None and item.bonding != 1:
                    for ml in active_market.ah_listings_by_entry.get(item.entry, ()):
                        if ml.expires_at > ts:
                            all_sources.append(ItemSource(
                                source_kind="auction_house",
                                source_id=ml.auction_id,
                                source_name=f"Auction House #{ml.auction_id}",
                                faction=ml.faction,
                                evidence_table="acore_characters.auctionhouse",
                                evidence_key=ml.evidence_key,
                                min_level=max(1, item.required_level),
                                drop_chance_pct=100.0,
                                cost_copper=ml.buyout_copper,
                                raid_stage_min=item.raid_stage_min,
                                estimated_minutes=5,
                            ))

                if not all_sources:
                    continue

                # Active ambition Unique-Equipped conflict check (P1-1)
                if _is_unique_item(item) and item.entry in active_ambition_entries:
                    continue

                ok_equip_now, equip_now_reason = can_character_equip_item(
                    item, class_id=class_id, race_id=race_id, faction=faction, level=level, role=role,
                    target_slot=slot, skills=skills_map, reputations=rep_map, known_spells=spells_set, check_level_now=True,
                )

                ev_att: list[dict[str, Any]] = []
                ev_asp: list[dict[str, Any]] = []
                for src in all_sources:
                    ev_s = evaluate_source_attainability(
                        item, src, class_id=class_id, race_id=race_id, level=level, faction=faction,
                        raid_stage=raid_stage, completed_quests=comp_q, skills=skills_map, reputations=rep_map,
                        known_spells=spells_set, gold_copper=gold_copper,
                        raid_readiness_gold_reserve_copper=eff_reserve, locked_dungeon_maps=locked_maps,
                        bracket_max_level=b_max, market_snapshot=active_market, identity=identity,
                        now_ts=ts, registered_executors=registered_executors,
                    )
                    if ev_s["status"] == "unavailable":
                        continue
                    src_sum = {
                        "source_kind": src.source_kind,
                        "source_id": src.source_id,
                        "source_name": src.source_name,
                        "cost_copper": int(src.cost_copper),
                        "drop_chance_pct": src.drop_chance_pct,
                        "expected_minutes": int(ev_s.get("expected_minutes") or src.estimated_minutes or 30),
                        "expected_runs": int(ev_s.get("expected_runs") or 1),
                        "reason": ev_s["reason"],
                        "evidence_table": src.evidence_table,
                        "evidence_key": list(src.evidence_key),
                        "proposal": ev_s["proposal"],
                    }
                    if (not is_future_bracket) and ev_s["status"] == "attainable_now" and ok_equip_now:
                        ev_att.append(src_sum)
                    else:
                        if is_future_bracket:
                            src_sum["reason"] = f"future_bracket:{bracket_info['bracket_id']}"
                            src_sum["proposal"] = _build_proposal(
                                proposal_kind="advisory_gear_target", source_kind=src.source_kind, item=item,
                                target_id=src.source_id, target_name=src.source_name,
                                evidence_table=src.evidence_table, evidence_key=src.evidence_key,
                                reason=src_sum["reason"], identity=identity, now_ts=ts, force_advisory=True,
                            )
                        elif not ok_equip_now and ev_s["status"] == "attainable_now":
                            src_sum["reason"] = equip_now_reason
                            src_sum["proposal"] = _build_proposal(
                                proposal_kind="advisory_gear_target", source_kind=src.source_kind, item=item,
                                target_id=src.source_id, target_name=src.source_name,
                                evidence_table=src.evidence_table, evidence_key=src.evidence_key,
                                reason=equip_now_reason, identity=identity, now_ts=ts, force_advisory=True,
                            )
                        ev_asp.append(src_sum)

                if ev_att:
                    ev_att.sort(key=lambda s: (s["expected_minutes"], s["cost_copper"], s["source_id"], s["source_kind"]))
                    best_s = ev_att[0]
                    h_fac = 1.0 / (1.0 + (best_s["expected_minutes"] / 180.0))
                    util = round(upgrade_delta * (0.65 + 0.35 * h_fac), 3)
                    att_list.append({
                        "slot": slot,
                        "item_entry": item.entry,
                        "item_name": item.name,
                        "item_level": item.item_level,
                        "required_level": item.required_level,
                        "inventory_type": item.inventory_type,
                        "is_unique": _is_unique_item(item),
                        "item_limit_category": item.item_limit_category,
                        "score": cand_score,
                        "upgrade_delta": upgrade_delta,
                        "rank_utility": util,
                        "stat_breakdown": breakdown,
                        "best_source": best_s,
                        "alternative_sources": ev_att[1:],
                        "proposal": best_s["proposal"],
                        "voluntary": True,
                        "evidence_table": item.evidence_table,
                    })
                elif ev_asp:
                    ev_asp.sort(key=lambda s: (s["expected_minutes"], s["cost_copper"], s["source_id"], s["source_kind"]))
                    best_a = ev_asp[0]
                    asp_list.append({
                        "slot": slot,
                        "item_entry": item.entry,
                        "item_name": item.name,
                        "item_level": item.item_level,
                        "required_level": item.required_level,
                        "inventory_type": item.inventory_type,
                        "is_unique": _is_unique_item(item),
                        "item_limit_category": item.item_limit_category,
                        "score": cand_score,
                        "upgrade_delta": upgrade_delta,
                        "stat_breakdown": breakdown,
                        "best_source": best_a,
                        "alternative_sources": ev_asp[1:],
                        "proposal": best_a["proposal"],
                        "voluntary": True,
                        "evidence_table": item.evidence_table,
                    })

            att_list.sort(key=lambda c: (-c["rank_utility"], -c["upgrade_delta"], c["item_entry"]))
            asp_list.sort(key=lambda c: (-c["upgrade_delta"], c["required_level"], c["item_entry"]))
            raw_slot_att[slot] = att_list
            raw_slot_asp[slot] = asp_list

        # P1-1: Deduplicate bag-owned equip proposals respecting actual bag copy counts & uniqueness!
        equip_owned_out: list[dict[str, Any]] = []
        for entry_id in sorted(bag_upgrade_proposals.keys()):
            cands = sorted(bag_upgrade_proposals[entry_id], key=lambda x: (-x["upgrade_delta"], x["slot"]))
            max_assign = 1 if cands[0]["is_unique"] else min(len(cands), bags.get(entry_id, 1))
            for chosen in cands[:max_assign]:
                equip_owned_out.append({k: v for k, v in chosen.items() if k != "is_unique"})
        equip_owned_out.sort(key=lambda x: (x["slot"], -x["upgrade_delta"], x["item_entry"]))

        # P1-1: Cross-slot Unique / Unique-Equipped / ItemLimitCategory deduplication across paired slots!
        claimed_unique_entries: set[int] = set(active_ambition_entries)
        claimed_limit_cats: set[int] = set()
        # Process highest-utility candidates across all slots first so the best slot claims the Unique item
        all_att_flat = sorted(
            (c for lst in raw_slot_att.values() for c in lst),
            key=lambda c: (-c["rank_utility"], -c["upgrade_delta"], c["slot"], c["item_entry"]),
        )
        kept_att_by_slot: dict[str, list[dict[str, Any]]] = {s: [] for s in ALL_EQUIPMENT_SLOTS}
        for c in all_att_flat:
            if c["is_unique"] and c["item_entry"] in claimed_unique_entries:
                continue
            if c["item_limit_category"] > 0 and c["item_limit_category"] in claimed_limit_cats:
                continue
            kept_att_by_slot[c["slot"]].append(c)
            if c["is_unique"]:
                claimed_unique_entries.add(c["item_entry"])
            if c["item_limit_category"] > 0:
                claimed_limit_cats.add(c["item_limit_category"])

        # P1-4: Shrinking discretionary gold budget pass across ALL paid attainable top picks!
        remaining_copper = max(0, int(gold_copper) - max(0, eff_reserve))
        top_paid_picks: list[dict[str, Any]] = []
        for s in ALL_EQUIPMENT_SLOTS:
            if kept_att_by_slot[s]:
                top = kept_att_by_slot[s][0]
                if top["best_source"]["cost_copper"] > 0:
                    top_paid_picks.append(top)
        top_paid_picks.sort(key=lambda c: (-c["rank_utility"], -c["upgrade_delta"], c["slot"], c["item_entry"]))

        for pick in top_paid_picks:
            cost = int(pick["best_source"]["cost_copper"])
            if cost <= remaining_copper:
                remaining_copper -= cost
            else:
                # Demote cleanly to aspirational: budget_exhausted!
                s = pick["slot"]
                kept_att_by_slot[s] = [x for x in kept_att_by_slot[s] if x["item_entry"] != pick["item_entry"]]
                demoted = dict(pick)
                demoted["best_source"] = {**pick["best_source"], "reason": "budget_exhausted"}
                demoted["proposal"] = {
                    **pick["proposal"],
                    "proposal_kind": "advisory_gear_target",
                    "executable": False,
                    "advisory_only": True,
                    "reason": "budget_exhausted",
                }
                raw_slot_asp[s].insert(0, demoted)

        # Cross-slot dedup on aspirational Unique items too!
        kept_asp_by_slot: dict[str, list[dict[str, Any]]] = {s: [] for s in ALL_EQUIPMENT_SLOTS}
        all_asp_flat = sorted(
            (c for lst in raw_slot_asp.values() for c in lst),
            key=lambda c: (-c["upgrade_delta"], c["required_level"], c["slot"], c["item_entry"]),
        )
        for c in all_asp_flat:
            if c["is_unique"] and c["item_entry"] in claimed_unique_entries:
                continue
            if c["item_limit_category"] > 0 and c["item_limit_category"] in claimed_limit_cats:
                continue
            kept_asp_by_slot[c["slot"]].append(c)
            if c["is_unique"]:
                claimed_unique_entries.add(c["item_entry"])
            if c["item_limit_category"] > 0:
                claimed_limit_cats.add(c["item_limit_category"])

        # Assemble final bounded per-slot plans with viable alternatives
        slot_plans: dict[str, dict[str, Any]] = {}
        for slot in ALL_EQUIPMENT_SLOTS:
            cur_item = equipped_resolved[slot]
            bounded_att = kept_att_by_slot[slot][:eff_max_targets]
            bounded_asp = kept_asp_by_slot[slot][:eff_max_targets]
            if len(bounded_att) > 1:
                top = bounded_att[0]
                top["viable_alternatives"] = [
                    {
                        "item_entry": alt["item_entry"],
                        "item_name": alt["item_name"],
                        "score_delta_vs_top": round(alt["score"] - top["score"], 3),
                        "source_kind": alt["best_source"]["source_kind"],
                        "expected_minutes": alt["best_source"]["expected_minutes"],
                        "cost_copper": alt["best_source"]["cost_copper"],
                    }
                    for alt in bounded_att[1:]
                ]
            slot_plans[slot] = {
                "slot": slot,
                "protected_by_raid_readiness": slot_meta[slot]["protected_by_raid_readiness"],
                "unknown_equipped": slot_meta[slot]["unknown_equipped"],
                "equipped_entry": cur_item.entry if cur_item else 0,
                "equipped_name": cur_item.name if cur_item else "",
                "equipped_score": equipped_scores[slot],
                "attainable": bounded_att,
                "aspirational": bounded_asp,
            }

        return {
            "bracket": bracket_info,
            "equip_owned_upgrades": equip_owned_out,
            "remaining_discretionary_copper": remaining_copper,
            "slots": slot_plans,
        }

    bracket_plans = {b["horizon_kind"]: _plan_for_bracket(b) for b in horizon_brackets}
    primary = bracket_plans["current"]
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "class_name": cls_name,
        "role": role,
        "level": int(level),
        "bracket": primary["bracket"],
        "xp_cap_authoritative": bool(primary["bracket"].get("xp_cap_authoritative")),
        "xp_fail_closed": bool(primary["bracket"].get("xp_fail_closed")),
        "coop_enabled_live": coop_enabled(),
        "raid_readiness_gold_reserve_copper": eff_reserve,
        "discretionary_gold_copper": max(0, int(gold_copper) - max(0, eff_reserve)),
        "remaining_discretionary_copper": primary["remaining_discretionary_copper"],
        "equip_owned_upgrades": primary["equip_owned_upgrades"],
        "slots": primary["slots"],
        "bracket_plans": bracket_plans,
    }


def extract_companion_cap_params(companion: dict[str, Any]) -> dict[str, Any]:
    """Single shared extractor for per-companion authoritative XP cap & fail-closed state (Issue #31)."""
    ll = companion.get("level_lead_state") if isinstance(companion.get("level_lead_state"), dict) else {}
    raw_cap = companion.get("xp_cap_level")
    if raw_cap is None:
        raw_cap = ll.get("owner_cap_level")
    xp_cap_level = int(raw_cap) if raw_cap is not None and int(raw_cap) > 0 else None
    xp_fail_closed = bool(
        companion.get("xp_fail_closed") is True
        or ll.get("xp_fail_closed") is True
        or str(companion.get("xp_cap_state") or ll.get("xp_cap_state") or "").strip().lower()
        in ("unresolved_assignment", "level_unknown", "stale_level", "fail_closed")
    )
    if xp_cap_level is None and (companion.get("xp_eligible") is False or ll.get("xp_eligible") is False):
        xp_fail_closed = True
    return {
        "xp_cap_level": xp_cap_level,
        "xp_fail_closed": xp_fail_closed,
    }


def plan_companion_gear_ambitions(
    companion: dict[str, Any],
    *,
    catalog: dict[int, CatalogItem],
    role: str,
    equipped_items: dict[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    """Production companion wrapper that forwards authoritative per-companion XP cap & fail-closed state (Issue #31)."""
    raw_class_id = companion.get("class_id")
    if raw_class_id is None or int(raw_class_id) <= 0:
        return {"ok": False, "reason": "missing_class_id"}
    cap_params = extract_companion_cap_params(companion)
    kwargs.setdefault("xp_cap_level", cap_params["xp_cap_level"])
    kwargs.setdefault("xp_fail_closed", cap_params["xp_fail_closed"])
    return plan_character_gear_ambitions(
        catalog=catalog,
        class_id=int(raw_class_id),
        race_id=int(companion.get("race_id") or 1),
        faction=str(companion.get("faction") or "alliance"),
        level=int(companion.get("level") or 1),
        role=role,
        equipped_items=equipped_items,
        **kwargs,
    )


class GearAmbitionStore:
    """Durable SQLite WAL store for per-character gear ambitions & idempotent reconciliation (Issue #24)."""

    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._conn()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS gear_ambitions (
                    id INTEGER PRIMARY KEY,
                    persona_id TEXT NOT NULL,
                    realm_id INTEGER NOT NULL DEFAULT 1,
                    character_guid INTEGER NOT NULL,
                    incarnation INTEGER NOT NULL DEFAULT 1,
                    slot TEXT NOT NULL,
                    item_entry INTEGER NOT NULL,
                    item_name TEXT NOT NULL,
                    bracket_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_id INTEGER NOT NULL DEFAULT 0,
                    target_score REAL NOT NULL DEFAULT 0.0,
                    state TEXT NOT NULL,
                    eligible_loot_attempts INTEGER NOT NULL DEFAULT 0,
                    close_reason TEXT NOT NULL DEFAULT '',
                    proposal_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(persona_id, realm_id, character_guid, incarnation, slot, item_entry)
                )
            """)
            # P1-5: Composite PRIMARY KEY (native_event_id, ambition_id) so one boss kill credits ALL matching ambitions!
            conn.execute("""
                CREATE TABLE IF NOT EXISTS gear_ambition_events (
                    native_event_id TEXT NOT NULL,
                    ambition_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (native_event_id, ambition_id),
                    FOREIGN KEY (ambition_id) REFERENCES gear_ambitions(id) ON DELETE CASCADE
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ga_char_state "
                "ON gear_ambitions(persona_id, realm_id, character_guid, incarnation, state)"
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
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def set_ambition(
        self,
        *,
        persona_id: str,
        realm_id: int,
        character_guid: int,
        incarnation: int,
        slot: str,
        item_entry: int,
        bracket_id: str,
        source_kind: str,
        source_id: int,
        target_score: float,
        proposal: dict[str, Any],
        catalog: dict[int, CatalogItem],
        class_id: int,
        race_id: int,
        faction: str,
        level: int,
        role: str,
        market_snapshot: MarketSnapshot | None = None,
        currently_owned_entries: set[int] | frozenset[int] = frozenset(),
        is_shadow: bool = True,
        now_ts: int | None = None,
        allow_legacy_no_registry: bool = False,
    ) -> dict[str, Any]:
        """Validate against installed `catalog` + `PersonaRegistry` and persist a voluntary gear ambition (P1-6)."""
        ts = int(now_ts if now_ts is not None else time.time())
        rid = _parse_realm_id(realm_id, default_when_none=None)
        if rid is None:
            return {"ok": False, "reason": "invalid_realm_id"}
        if slot not in ALL_EQUIPMENT_SLOTS:
            return {"ok": False, "reason": f"invalid_slot:{slot}"}
        if bracket_id not in BRACKET_BY_ID:
            return {"ok": False, "reason": f"invalid_bracket_id:{bracket_id}"}

        # P1-6: Validate item exists in catalog, slot matches InventoryType, class/race legal, and source verified!
        item = catalog.get(int(item_entry))
        if item is None:
            return {"ok": False, "reason": f"item_not_in_catalog:{item_entry}"}
        if slot not in INVTYPE_TO_SLOTS.get(item.inventory_type, ()):
            return {"ok": False, "reason": f"slot_mismatch_for_item:{slot}"}

        ok_leg, leg_r = can_character_equip_item(
            item, class_id=class_id, race_id=race_id, faction=faction,
            level=max(int(level), item.required_level), role=role, target_slot=slot, check_level_now=False,
        )
        if not ok_leg:
            return {"ok": False, "reason": leg_r}

        allowed_pks = PROPOSAL_KIND_BY_SOURCE_KIND.get(source_kind)
        if allowed_pks is None:
            return {"ok": False, "reason": f"invalid_source_kind:{source_kind}"}
        pk = str((proposal or {}).get("proposal_kind") or "")
        if pk not in allowed_pks:
            return {"ok": False, "reason": f"proposal_kind_mismatch:{pk}!={source_kind}"}

        source_verified = any(
            s.source_kind == source_kind and int(s.source_id) == int(source_id) and bool(s.evidence_key)
            for s in item.sources
        )
        if not source_verified and source_kind == "auction_house" and market_snapshot is not None:
            source_verified = any(
                int(ml.auction_id) == int(source_id) and ml.expires_at > ts
                for ml in market_snapshot.ah_listings_by_entry.get(item.entry, ())
            )
        if not source_verified:
            return {"ok": False, "reason": f"unverified_source:{source_kind}:{source_id}"}

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            psnap = PersonaRegistry.read_only_snapshot(self.path, conn=conn)
            ok_p, r_p, v_inc = _verify_persona_with_snapshot(
                psnap,
                persona_id=persona_id,
                realm_id=rid,
                character_guid=int(character_guid),
                allow_legacy_no_registry=allow_legacy_no_registry,
                is_shadow=is_shadow,
                expected_incarnation=int(incarnation),
            )
            if not ok_p:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": r_p}

            # Enforce per-slot active cap & Unique/Unique-Equipped across paired slots!
            active_rows = conn.execute(
                """
                SELECT id, slot, item_entry FROM gear_ambitions
                WHERE persona_id=? AND realm_id=? AND character_guid=? AND incarnation=? AND state='active'
                """,
                (persona_id, rid, int(character_guid), v_inc),
            ).fetchall()
            if _is_unique_item(item):
                for ar in active_rows:
                    if int(ar["item_entry"]) == item.entry and str(ar["slot"]) != slot:
                        conn.execute("ROLLBACK")
                        return {"ok": False, "reason": "unique_item_already_active_in_other_slot"}

            existing = conn.execute(
                """
                SELECT id, state FROM gear_ambitions
                WHERE persona_id=? AND realm_id=? AND character_guid=? AND incarnation=? AND slot=? AND item_entry=?
                """,
                (persona_id, rid, int(character_guid), v_inc, slot, item.entry),
            ).fetchone()

            if existing is None:
                same_slot_cnt = sum(1 for ar in active_rows if str(ar["slot"]) == slot)
                if same_slot_cnt >= MAX_TARGETS_PER_SLOT_CEILING:
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": f"slot_ambition_cap_reached:{MAX_TARGETS_PER_SLOT_CEILING}"}

            if existing:
                # Allow reopening if previously 'obtained_in_bags' ONLY if item is no longer owned (P1-6!)
                if str(existing["state"]) == "obtained_equipped" or (
                    str(existing["state"]) == "obtained_in_bags" and item.entry in currently_owned_entries
                ):
                    conn.execute("ROLLBACK")
                    return {"ok": False, "reason": f"ambition_already_obtained:{existing['state']}"}
                conn.execute(
                    "UPDATE gear_ambitions SET state='active', bracket_id=?, source_kind=?, source_id=?, "
                    "target_score=?, proposal_json=?, close_reason='', updated_at=? WHERE id=?",
                    (bracket_id, source_kind, int(source_id), float(target_score), json.dumps(proposal, sort_keys=True), ts, int(existing["id"])),
                )
                aid = int(existing["id"])
            else:
                cur = conn.execute(
                    """
                    INSERT INTO gear_ambitions (
                        persona_id, realm_id, character_guid, incarnation, slot, item_entry, item_name,
                        bracket_id, source_kind, source_id, target_score, state, eligible_loot_attempts,
                        proposal_json, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',0,?,?,?)
                    """,
                    (
                        persona_id, rid, int(character_guid), v_inc, slot, item.entry,
                        item.name[:120], str(bracket_id)[:32], str(source_kind)[:32],
                        int(source_id), float(target_score), json.dumps(proposal, sort_keys=True), ts, ts,
                    ),
                )
                aid = int(cur.lastrowid)
            conn.execute("COMMIT")
            return {"ok": True, "ambition_id": aid, "state": "active", "voluntary": True}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def abandon_ambition(
        self,
        ambition_id: int,
        *,
        persona_id: str,
        realm_id: int,
        character_guid: int,
        incarnation: int,
        reason: str = "voluntary_abandon",
        is_shadow: bool = True,
        now_ts: int | None = None,
        allow_legacy_no_registry: bool = False,
    ) -> dict[str, Any]:
        """Voluntarily abandon an active gear ambition with full identity & PersonaRegistry verification (P1-6)."""
        ts = int(now_ts if now_ts is not None else time.time())
        rid = _parse_realm_id(realm_id, default_when_none=None)
        if rid is None:
            return {"ok": False, "reason": "invalid_realm_id"}
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            psnap = PersonaRegistry.read_only_snapshot(self.path, conn=conn)
            ok_p, r_p, v_inc = _verify_persona_with_snapshot(
                psnap,
                persona_id=persona_id,
                realm_id=rid,
                character_guid=int(character_guid),
                allow_legacy_no_registry=allow_legacy_no_registry,
                is_shadow=is_shadow,
                expected_incarnation=int(incarnation),
            )
            if not ok_p:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": r_p}

            upd = conn.execute(
                "UPDATE gear_ambitions SET state='abandoned', close_reason=?, updated_at=? "
                "WHERE id=? AND persona_id=? AND realm_id=? AND character_guid=? AND incarnation=? AND state='active'",
                (str(reason or "voluntary_abandon")[:64], ts, int(ambition_id), persona_id, rid, int(character_guid), v_inc),
            ).rowcount
            conn.execute("COMMIT")
            if upd != 1:
                return {"ok": False, "reason": "not_found_or_identity_mismatch"}
            return {"ok": True, "state": "abandoned", "neutral": True, "penalty_applied": False}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def reconcile_ambitions(
        self,
        *,
        persona_id: str,
        realm_id: int,
        character_guid: int,
        incarnation: int,
        catalog: dict[int, CatalogItem],
        class_id: int,
        race_id: int,
        faction: str,
        role: str,
        level: int,
        raid_stage: int = 0,
        equipped_items: dict[str, Any],
        bag_item_entries: set[int] | frozenset[int] = frozenset(),
        completed_quests: set[int] | frozenset[int] = frozenset(),
        observed_events: list[dict[str, Any]] | None = None,
        is_shadow: bool = True,
        now_ts: int | None = None,
        allow_legacy_no_registry: bool = False,
    ) -> dict[str, Any]:
        """Reconcile active ambitions against PersonaRegistry, inventory, outleveling, role changes, quests & loot events (P1-5)."""
        ts = int(now_ts if now_ts is not None else time.time())
        rid = _parse_realm_id(realm_id, default_when_none=None)
        if rid is None:
            return {"ok": False, "reason": "invalid_realm_id"}

        cur_bracket = resolve_bracket(level, raid_stage=raid_stage)
        cur_b_min = int(cur_bracket["min_level"])
        bag_set = {int(e) for e in (bag_item_entries or ())}
        comp_q = set(completed_quests or ())

        eq_entries_by_slot: dict[str, int] = {}
        eq_scores_by_slot: dict[str, float] = {}
        for s in ALL_EQUIPMENT_SLOTS:
            raw = equipped_items.get(s)
            ci: CatalogItem | None = None
            if isinstance(raw, CatalogItem):
                ci = raw
            elif raw is not None and raw != "" and raw != 0:
                try:
                    ci = catalog.get(int(raw))
                except (TypeError, ValueError):
                    ci = None
            eq_entries_by_slot[s] = ci.entry if ci else 0
            eq_scores_by_slot[s] = score_item_for_role(ci, class_id=class_id, role=role, level=level)[0] if ci else 0.0

        conn = self._conn()
        reconciled: list[dict[str, Any]] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            psnap = PersonaRegistry.read_only_snapshot(self.path, conn=conn)
            ok_p, r_p, v_inc = _verify_persona_with_snapshot(
                psnap,
                persona_id=persona_id,
                realm_id=rid,
                character_guid=int(character_guid),
                allow_legacy_no_registry=allow_legacy_no_registry,
                is_shadow=is_shadow,
                expected_incarnation=int(incarnation),
            )
            if not ok_p:
                conn.execute("ROLLBACK")
                return {"ok": False, "reason": r_p}

            rows = conn.execute(
                """
                SELECT * FROM gear_ambitions
                WHERE persona_id=? AND realm_id=? AND character_guid=? AND incarnation=? AND state='active'
                ORDER BY id ASC
                """,
                (persona_id, rid, int(character_guid), v_inc),
            ).fetchall()

            for row in rows:
                aid = int(row["id"])
                slot = str(row["slot"])
                entry = int(row["item_entry"])
                b_id = str(row["bracket_id"])
                skind = str(row["source_kind"])
                sid = int(row["source_id"])
                prev_attempts = int(row["eligible_loot_attempts"])
                attempts = prev_attempts
                target_ci = catalog.get(entry)

                # Build allowed boss/dungeon provenance set for loot event validation (P1-5)
                valid_bosses = {int(s.boss_entry) for s in (target_ci.sources if target_ci else ()) if s.source_kind == "dungeon_drop" and s.boss_entry > 0}
                valid_dungeons = {s.dungeon_id for s in (target_ci.sources if target_ci else ()) if s.source_kind == "dungeon_drop" and s.dungeon_id}

                for ev in (observed_events or []):
                    if not isinstance(ev, dict):
                        continue
                    ev_id = str(ev.get("native_event_id") or "").strip()[:96]
                    if not ev_id:
                        continue
                    if (
                        str(ev.get("type") or "").strip().lower() == "loot_opportunity"
                        and str(ev.get("persona_id") or "") == persona_id
                        and _parse_realm_id(ev.get("realm_id"), default_when_none=None) == rid
                        and int(ev.get("character_guid") or 0) == int(character_guid)
                        and int(ev.get("incarnation") or 0) == v_inc
                        and int(ev.get("item_entry") or 0) == entry
                    ):
                        # Validate boss/dungeon provenance when present!
                        ev_boss = int(ev.get("boss_entry") or 0)
                        ev_dung = str(ev.get("dungeon_id") or "").strip()
                        if valid_bosses and ev_boss > 0 and ev_boss not in valid_bosses:
                            continue
                        if valid_dungeons and ev_dung and ev_dung not in valid_dungeons:
                            continue
                        ins = conn.execute(
                            "INSERT OR IGNORE INTO gear_ambition_events (native_event_id, ambition_id, event_type, created_at) VALUES (?,?,?,?)",
                            (ev_id, aid, "loot_opportunity", ts),
                        ).rowcount
                        if ins == 1:
                            attempts += 1

                new_state = "active"
                close_reason = ""
                paired = PAIRED_SLOTS.get(slot)
                is_uniq = _is_unique_item(target_ci) if target_ci is not None else False

                # P1-5: Match paired slot ONLY if item is Unique / Unique-Equipped (never close 2nd copy of 1H weapon!)
                if eq_entries_by_slot.get(slot) == entry or (is_uniq and paired and eq_entries_by_slot.get(paired) == entry):
                    new_state = "obtained_equipped"
                    close_reason = "equipped_observed"
                elif entry in bag_set:
                    new_state = "obtained_in_bags"
                    close_reason = "owned_in_bags_observed"
                else:
                    # Check outleveled bracket, outleveled dungeon, role change, quest already finished, or superseded!
                    b_tuple = BRACKET_BY_ID.get(b_id)
                    if b_tuple is not None and b_tuple[2] < cur_b_min:
                        new_state = "obsolete_outleveled"
                        close_reason = f"left_bracket:{b_id}<{cur_bracket['bracket_id']}"
                    elif skind == "quest_reward" and sid in comp_q:
                        new_state = "obsolete_quest_completed"
                        close_reason = f"quest_completed_without_item:{sid}"
                    elif target_ci is not None:
                        ok_role, role_r = can_character_equip_item(
                            target_ci, class_id=class_id, race_id=race_id, faction=faction,
                            level=max(level, target_ci.required_level), role=role, target_slot=slot, check_level_now=False,
                        )
                        if not ok_role:
                            new_state = "obsolete_role_changed"
                            close_reason = role_r
                        elif skind == "dungeon_drop":
                            # Outleveled LFG dungeon band for every matching dungeon source?
                            matching_specs = [
                                DUNGEON_BY_ID[s.dungeon_id]
                                for s in target_ci.sources
                                if s.source_kind == "dungeon_drop" and s.dungeon_id in DUNGEON_BY_ID
                            ]
                            if matching_specs and all(int(level) > sp.max_level for sp in matching_specs):
                                new_state = "obsolete_outleveled"
                                close_reason = "outleveled_dungeon_band"
                        if new_state == "active":
                            t_score = score_item_for_role(target_ci, class_id=class_id, role=role, level=level)[0]
                            if eq_scores_by_slot.get(slot, 0.0) >= t_score:
                                new_state = "obsolete_superseded"
                                close_reason = "better_or_equal_equipped"
                    elif eq_scores_by_slot.get(slot, 0.0) >= float(row["target_score"]):
                        new_state = "obsolete_superseded"
                        close_reason = "better_or_equal_equipped"

                # P3: Only touch updated_at when state or attempts actually changed!
                if new_state != "active" or attempts != prev_attempts:
                    conn.execute(
                        "UPDATE gear_ambitions SET state=?, eligible_loot_attempts=?, close_reason=?, updated_at=? WHERE id=?",
                        (new_state, attempts, close_reason[:64], ts, aid),
                    )
                reconciled.append({
                    "ambition_id": aid,
                    "slot": slot,
                    "item_entry": entry,
                    "state": new_state,
                    "eligible_loot_attempts": attempts,
                    "close_reason": close_reason,
                })

            conn.execute("COMMIT")
            return {"ok": True, "reconciled": reconciled}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def list_ambitions(
        self,
        *,
        persona_id: str,
        realm_id: int,
        character_guid: int,
        incarnation: int,
        include_closed: bool = False,
    ) -> list[dict[str, Any]]:
        """List ambitions for an explicit `(persona_id, realm_id, character_guid, incarnation)` (P2-10: no silent defaults!)."""
        rid = _parse_realm_id(realm_id, default_when_none=None)
        if rid is None:
            raise ValueError("invalid_realm_id")
        conn = self._conn()
        try:
            clause = "" if include_closed else "AND state='active'"
            rows = conn.execute(
                f"""
                SELECT * FROM gear_ambitions
                WHERE persona_id=? AND realm_id=? AND character_guid=? AND incarnation=? {clause}
                ORDER BY id ASC
                """,
                (persona_id, rid, int(character_guid), int(incarnation)),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
