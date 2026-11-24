"""Authoritative WotLK 3.3.5a Classic (1-300) Profession Leveling Guide & Leveling Consumables Catalog.

Every item_entry is verified against acore_world.item_template on Hearthguard.
Per-character material quantities are modeled on standard WotLK 3.3.5a (wow-professions.com)
1-300 leveling routes split into four skill brackets:
  (0, 75), (75, 150), (150, 225), (225, 300).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SKILL_NAMES = {
    164: "Blacksmithing",
    165: "Leatherworking",
    171: "Alchemy",
    182: "Herbalism",
    186: "Mining",
    197: "Tailoring",
    202: "Engineering",
    333: "Enchanting",
    393: "Skinning",
    755: "Jewelcrafting",
    773: "Inscription",
    129: "First Aid",
    185: "Cooking",
    356: "Fishing",
}

# Verified item_entry -> (canonical name, gather_skill_or_0, category)
MAT_CATALOG: dict[int, tuple[str, int, str]] = {
    # Ores & Bars & Stones (Mining 186)
    2770: ("Copper Ore", 186, "Mining"),
    2840: ("Copper Bar", 186, "Mining"),
    2771: ("Tin Ore", 186, "Mining"),
    2841: ("Bronze Bar", 186, "Mining"),
    2775: ("Silver Ore", 186, "Mining"),
    2842: ("Silver Bar", 186, "Mining"),
    2772: ("Iron Ore", 186, "Mining"),
    3575: ("Iron Bar", 186, "Mining"),
    2776: ("Gold Ore", 186, "Mining"),
    3577: ("Gold Bar", 186, "Mining"),
    3859: ("Steel Bar", 186, "Mining"),
    3858: ("Mithril Ore", 186, "Mining"),
    3860: ("Mithril Bar", 186, "Mining"),
    7911: ("Truesilver Ore", 186, "Mining"),
    6037: ("Truesilver Bar", 186, "Mining"),
    10620: ("Thorium Ore", 186, "Mining"),
    12359: ("Thorium Bar", 186, "Mining"),
    2835: ("Rough Stone", 186, "Mining"),
    2836: ("Coarse Stone", 186, "Mining"),
    2838: ("Heavy Stone", 186, "Mining"),
    7912: ("Solid Stone", 186, "Mining"),
    12365: ("Dense Stone", 186, "Mining"),
    # Gems (Mining/Prospecting)
    818: ("Tigerseye", 186, "Gems"),
    774: ("Malachite", 186, "Gems"),
    1210: ("Shadowgem", 186, "Gems"),
    1206: ("Moss Agate", 186, "Gems"),
    1705: ("Lesser Moonstone", 186, "Gems"),
    1529: ("Jade", 186, "Gems"),
    3864: ("Citrine", 186, "Gems"),
    7909: ("Aquamarine", 186, "Gems"),
    7910: ("Star Ruby", 186, "Gems"),
    # Herbs (Herbalism 182)
    2447: ("Peacebloom", 182, "Herbs"),
    765: ("Silverleaf", 182, "Herbs"),
    2449: ("Earthroot", 182, "Herbs"),
    785: ("Mageroyal", 182, "Herbs"),
    2450: ("Briarthorn", 182, "Herbs"),
    3820: ("Stranglekelp", 182, "Herbs"),
    2453: ("Bruiseweed", 182, "Herbs"),
    3355: ("Wild Steelbloom", 182, "Herbs"),
    3356: ("Kingsblood", 182, "Herbs"),
    3357: ("Liferoot", 182, "Herbs"),
    3818: ("Fadeleaf", 182, "Herbs"),
    3821: ("Goldthorn", 182, "Herbs"),
    8838: ("Sungrass", 182, "Herbs"),
    8839: ("Blindweed", 182, "Herbs"),
    8846: ("Gromsblood", 182, "Herbs"),
    13464: ("Golden Sansam", 182, "Herbs"),
    13465: ("Mountain Silversage", 182, "Herbs"),
    13463: ("Dreamfoil", 182, "Herbs"),
    # Leather (Skinning 393)
    2934: ("Ruined Leather Scraps", 393, "Leather"),
    2318: ("Light Leather", 393, "Leather"),
    2319: ("Medium Leather", 393, "Leather"),
    4234: ("Heavy Leather", 393, "Leather"),
    4304: ("Thick Leather", 393, "Leather"),
    8170: ("Rugged Leather", 393, "Leather"),
    # Cloth (Humanoid drops)
    2589: ("Linen Cloth", 0, "Cloth"),
    2592: ("Wool Cloth", 0, "Cloth"),
    4306: ("Silk Cloth", 0, "Cloth"),
    4338: ("Mageweave Cloth", 0, "Cloth"),
    14047: ("Runecloth", 0, "Cloth"),
    # Enchanting (Disenchanting 333)
    10940: ("Strange Dust", 333, "Enchanting"),
    10938: ("Lesser Magic Essence", 333, "Enchanting"),
    10939: ("Greater Magic Essence", 333, "Enchanting"),
    11083: ("Soul Dust", 333, "Enchanting"),
    10998: ("Lesser Astral Essence", 333, "Enchanting"),
    11082: ("Greater Astral Essence", 333, "Enchanting"),
    11137: ("Vision Dust", 333, "Enchanting"),
    11176: ("Dream Dust", 333, "Enchanting"),
    16204: ("Illusion Dust", 333, "Enchanting"),
    # Key Leveling Fish (Fishing 356)
    21071: ("Raw Sagefish", 356, "Fishing"),
    21153: ("Raw Greater Sagefish", 356, "Fishing"),
}

# Equivalent raw/smelted pairs so guild vault copper bars count toward copper demand, etc.
VAULT_EQUIVALENTS: dict[int, list[int]] = {
    2840: [2840, 2770],       # Copper Bar + Copper Ore
    2770: [2770, 2840],
    2841: [2841, 2771],       # Bronze Bar + Tin Ore
    2842: [2842, 2775],       # Silver Bar + Silver Ore
    3575: [3575, 2772],       # Iron Bar + Iron Ore
    3577: [3577, 2776],       # Gold Bar + Gold Ore
    3860: [3860, 3858],       # Mithril Bar + Mithril Ore
    6037: [6037, 7911],       # Truesilver Bar + Truesilver Ore
    12359: [12359, 10620],    # Thorium Bar + Thorium Ore
}

# Per-character WotLK 3.3.5a (1-300) leveling guide material budgets by skill tier.
# Format: skill_id -> list of (lo, hi, {item_entry: qty_per_character})
# Sources: classic/WotLK 3.3.5a wow-professions.com optimal routes (averaged with ~10% buffer).
PROFESSION_GUIDE_TIERS: dict[int, list[tuple[int, int, dict[int, int]]]] = {
    # Alchemy (171) — 1-300
    171: [
        (0, 75, {2447: 60, 765: 60, 2450: 15}),
        (75, 150, {2450: 40, 2453: 30, 785: 25, 3820: 20}),
        (150, 225, {3820: 20, 3357: 30, 3356: 45, 3821: 30, 3355: 25}),
        (225, 300, {8838: 70, 8839: 35, 13464: 65, 13465: 40, 8846: 25, 13463: 20}),
    ],
    # Blacksmithing (164) — 1-300
    164: [
        (0, 75, {2835: 90, 2840: 120}),
        (75, 150, {2836: 80, 2840: 30, 2841: 140, 2842: 5}),
        (150, 225, {2838: 105, 2841: 20, 3575: 190, 3577: 5, 3859: 20, 2318: 20}),
        (225, 300, {7912: 60, 3860: 250, 12365: 40, 12359: 320, 4338: 40}),
    ],
    # Engineering (202) — 1-300
    202: [
        (0, 75, {2835: 60, 2840: 85, 2589: 20}),
        (75, 150, {2836: 60, 2841: 95, 2592: 30, 2842: 10, 1206: 10}),
        (150, 225, {2838: 90, 3575: 55, 3859: 10, 4306: 40, 7912: 30}),
        (225, 300, {7912: 75, 3860: 150, 4338: 40, 12365: 50, 12359: 190, 14047: 40}),
    ],
    # Tailoring (197) — 1-300
    197: [
        (0, 75, {2589: 160}),
        (75, 150, {2589: 40, 2592: 150}),
        (150, 225, {4306: 380, 4338: 40}),
        (225, 300, {4338: 360, 14047: 540}),
    ],
    # Leatherworking (165) — 1-300
    165: [
        (0, 75, {2934: 55, 2318: 130}),
        (75, 150, {2318: 40, 2319: 165}),
        (150, 225, {2319: 35, 4234: 260}),
        (225, 300, {4304: 340, 8170: 320}),
    ],
    # Enchanting (333) — 1-300
    333: [
        (0, 75, {10940: 90, 10938: 12}),
        (75, 150, {10940: 80, 10939: 20, 11083: 35, 10998: 15}),
        (150, 225, {11083: 75, 11082: 15, 11137: 110}),
        (225, 300, {11137: 70, 11176: 160, 16204: 80}),
    ],
    # Jewelcrafting (755) — 1-300
    755: [
        (0, 75, {2840: 100, 2835: 40, 818: 15, 774: 15}),
        (75, 150, {2841: 80, 2842: 20, 2836: 40, 1210: 15, 1206: 15}),
        (150, 225, {2838: 60, 3577: 15, 3860: 70, 1705: 20, 1529: 15}),
        (225, 300, {3860: 50, 6037: 25, 12359: 110, 3864: 20, 7909: 20, 7910: 15}),
    ],
    # Inscription (773) — 1-300 (milling herbs into pigments/inks)
    773: [
        (0, 75, {2447: 55, 765: 55, 2449: 35}),
        (75, 150, {785: 50, 2450: 50, 2453: 40, 3820: 25}),
        (150, 225, {3355: 45, 3356: 55, 3357: 35, 3818: 35, 3821: 45}),
        (225, 300, {8838: 60, 8839: 40, 13464: 55, 13465: 45, 13463: 35}),
    ],
    # First Aid (129) — 1-300 (compact budget per character)
    129: [
        (0, 75, {2589: 75}),
        (75, 150, {2589: 30, 2592: 65}),
        (150, 225, {4306: 110}),
        (225, 300, {4338: 110, 14047: 100}),
    ],
    # Cooking (185) — focus guild-wide fishing/sagefish synergy for 80-225
    185: [
        (75, 150, {21071: 20}),
        (150, 225, {21153: 20}),
    ],
}


@dataclass(frozen=True)
class OrderTemplate:
    item_entry: int
    item_name: str
    qty: int
    skill_id: int
    min_skill: int
    priority: int           # 1 = profession leveling supply/intermediate, 2 = leveling consumable/buff
    category: str           # e.g. "bars", "sagefish", "oil", "elixir", "potion", "stone", "kit", "bag", "bandage"
    min_guild_lvl: int
    story_hint: str


# Verified against acore_world.item_template!
# Priority 1 = Guild profession leveling supplies/intermediates; Priority 2 = Useful leveling consumables & buffs.
LEVELING_ORDER_CATALOG: list[OrderTemplate] = [
    # --- Priority 1: Profession Leveling Intermediates & Craftable Staples ---
    OrderTemplate(2840, "Copper Bar", 20, 186, 1, 1, "bars", 1, "smelt Copper Bars for guild Blacksmiths, Engineers, and Jewelcrafters"),
    OrderTemplate(2841, "Bronze Bar", 20, 186, 65, 1, "bars", 10, "smelt Bronze Bars to push guild BS/Eng/JC through 75-150"),
    OrderTemplate(2842, "Silver Bar", 10, 186, 75, 1, "bars", 15, "smelt Silver Bars for guild Blacksmithing and Jewelcrafting rods/rings"),
    OrderTemplate(3575, "Iron Bar", 20, 186, 125, 1, "bars", 25, "smelt Iron Bars for 150-225 Blacksmithing and Engineering"),
    OrderTemplate(3859, "Steel Bar", 15, 186, 165, 1, "bars", 30, "smelt Steel Bars for mid-tier guild weapons and tools"),
    OrderTemplate(3860, "Mithril Bar", 20, 186, 175, 1, "bars", 35, "smelt Mithril Bars for 225+ Blacksmithing/Engineering/JC"),
    OrderTemplate(2304, "Light Armor Kit", 6, 165, 1, 1, "kit", 1, "stitch Light Armor Kits to train Leatherworking and buff leveling gear"),
    OrderTemplate(2313, "Medium Armor Kit", 6, 165, 100, 1, "kit", 15, "craft Medium Armor Kits for dungeon tanks and Leatherworking skill-ups"),
    OrderTemplate(4265, "Heavy Armor Kit", 6, 165, 150, 1, "kit", 25, "craft Heavy Armor Kits for level 25+ leveling armor"),
    OrderTemplate(4238, "Linen Bag", 2, 197, 45, 1, "bag", 1, "stitch Linen Bags for guild alts and Tailoring skill-ups"),
    OrderTemplate(4240, "Woolen Bag", 2, 197, 80, 1, "bag", 12, "stitch Woolen Bags to level Tailoring and expand guild bag space"),
    OrderTemplate(10050, "Mageweave Bag", 2, 197, 225, 1, "bag", 35, "stitch Mageweave Bags for guild crafters and Tailoring 225+"),

    # --- Priority 2: High-Value Leveling Consumables (Sagefish, Wizard/Mana Oils, Elixirs, Stones, Potions) ---
    # Sagefish (Mp5 mana food!) & Cooking leveling buffs
    OrderTemplate(21072, "Smoked Sagefish", 10, 185, 80, 2, "sagefish", 10, "cook Smoked Sagefish (Mp5 mana food) for casters and healers leveling"),
    OrderTemplate(21217, "Sagefish Delight", 10, 185, 175, 2, "sagefish", 30, "cook Sagefish Delight (Mp5 mana food) for level 30+ dungeon runs"),
    OrderTemplate(6888, "Herb Baked Egg", 15, 185, 1, 2, "food", 1, "cook Herb Baked Eggs (+Stam/+Spirit well-fed leveling food)"),
    OrderTemplate(6890, "Smoked Bear Meat", 15, 185, 40, 2, "food", 5, "cook Smoked Bear Meat (+Stam/+Spirit well-fed buff for questing)"),
    OrderTemplate(4592, "Longjaw Mud Snapper", 15, 185, 50, 2, "food", 5, "cook Longjaw Mud Snappers for steady questing food"),
    OrderTemplate(4593, "Bristle Whisker Catfish", 15, 185, 100, 2, "food", 15, "cook Bristle Whisker Catfish for mid-level questing and dungeons"),

    # Enchanting Weapon Oils (huge leveling boost for casters & healers!)
    OrderTemplate(20744, "Minor Wizard Oil", 4, 333, 45, 2, "oil", 5, "brew Minor Wizard Oil (+8 Spell Power) for Wonky and guild casters"),
    OrderTemplate(20745, "Minor Mana Oil", 4, 333, 150, 2, "oil", 20, "brew Minor Mana Oil (+4 Mp5) for healers and casters in dungeons"),
    OrderTemplate(20746, "Lesser Wizard Oil", 4, 333, 200, 2, "oil", 30, "brew Lesser Wizard Oil (+16 Spell Power) for level 30+ casters"),
    OrderTemplate(20747, "Lesser Mana Oil", 4, 333, 250, 2, "oil", 40, "brew Lesser Mana Oil (+8 Mp5) for level 40+ healers"),

    # Alchemy Leveling Elixirs & Potions (huge leveling boost for Logan, Wonky & companions!)
    OrderTemplate(2454, "Elixir of Lion's Strength", 5, 171, 1, 2, "elixir", 1, "brew Elixirs of Lion's Strength (+4 Str) for Logan and melee questing"),
    OrderTemplate(5997, "Elixir of Minor Defense", 5, 171, 1, 2, "elixir", 1, "brew Elixirs of Minor Defense (+50 Armor) for tanks and melee"),
    OrderTemplate(2457, "Elixir of Minor Agility", 5, 171, 50, 2, "elixir", 2, "brew Elixirs of Minor Agility (+4 Agi) for hunters, rogues, and ferals"),
    OrderTemplate(2458, "Elixir of Minor Fortitude", 5, 171, 50, 2, "elixir", 2, "brew Elixirs of Minor Fortitude (+27 HP) for safe questing"),
    OrderTemplate(3383, "Elixir of Wisdom", 5, 171, 90, 2, "elixir", 10, "brew Elixirs of Wisdom (+6 Int) for Wonky and guild casters"),
    OrderTemplate(3389, "Elixir of Defense", 5, 171, 130, 2, "elixir", 16, "brew Elixirs of Defense (+150 Armor) for dungeon tanks"),
    OrderTemplate(8949, "Elixir of Agility", 5, 171, 185, 2, "elixir", 27, "brew Elixirs of Agility (+15 Agi) for level 27+ hunters and rogues"),
    OrderTemplate(2459, "Swiftness Potion", 5, 171, 60, 2, "potion", 5, "brew Swiftness Potions for faster travel and emergency escapes"),
    OrderTemplate(118, "Minor Healing Potion", 10, 171, 1, 2, "potion", 1, "stock Minor Healing Potions for low-level questing safety"),
    OrderTemplate(2455, "Minor Mana Potion", 10, 171, 25, 2, "potion", 5, "stock Minor Mana Potions for casters and healers"),
    OrderTemplate(858, "Lesser Healing Potion", 10, 171, 55, 2, "potion", 8, "stock Lesser Healing Potions for leveling and dungeons"),
    OrderTemplate(3385, "Lesser Mana Potion", 10, 171, 120, 2, "potion", 14, "stock Lesser Mana Potions for Wonky and dungeon healers"),
    OrderTemplate(929, "Healing Potion", 10, 171, 110, 2, "potion", 16, "stock Healing Potions for level 20-35 dungeon prep"),
    OrderTemplate(3827, "Mana Potion", 10, 171, 160, 2, "potion", 22, "stock Mana Potions for Wonky and level 25+ casters"),

    # Blacksmithing Sharpening & Weightstones (melee weapon buffs for Logan & tanks!)
    OrderTemplate(2862, "Rough Sharpening Stone", 10, 164, 1, 2, "stone", 1, "forge Rough Sharpening Stones (+2 weapon dmg) for Logan and melee"),
    OrderTemplate(2863, "Coarse Sharpening Stone", 10, 164, 65, 2, "stone", 5, "forge Coarse Sharpening Stones (+3 weapon dmg) for melee leveling"),
    OrderTemplate(2871, "Heavy Sharpening Stone", 10, 164, 125, 2, "stone", 15, "forge Heavy Sharpening Stones (+4 weapon dmg) for dungeon melee"),
    OrderTemplate(3239, "Rough Weightstone", 10, 164, 1, 2, "stone", 1, "forge Rough Weightstones (+2 mace dmg) for paladins and warriors"),
    OrderTemplate(3240, "Coarse Weightstone", 10, 164, 65, 2, "stone", 5, "forge Coarse Weightstones (+3 mace dmg) for mid-low melee"),

    # First Aid Bandages (downtime-free leveling!)
    OrderTemplate(1251, "Linen Bandage", 15, 129, 1, 2, "bandage", 1, "roll Linen Bandages for zero-downtime questing"),
    OrderTemplate(2581, "Heavy Linen Bandage", 15, 129, 40, 2, "bandage", 5, "roll Heavy Linen Bandages for Logan and melee companions"),
    OrderTemplate(3530, "Wool Bandage", 15, 129, 80, 2, "bandage", 10, "roll Wool Bandages for level 10-25 questing"),
    OrderTemplate(3531, "Heavy Wool Bandage", 15, 129, 115, 2, "bandage", 15, "roll Heavy Wool Bandages for dungeon recovery"),
    OrderTemplate(6450, "Silk Bandage", 15, 129, 150, 2, "bandage", 25, "roll Silk Bandages for Wonky's level 30+ dungeon group"),
]

ORDER_BY_ENTRY: dict[int, OrderTemplate] = {t.item_entry: t for t in LEVELING_ORDER_CATALOG}

# Verified top Alliance farming zones from live acore_world node/school/beast counts:
# gather_skill -> list of (min_lvl, max_lvl, zone_name, mats_summary)
BEST_FARM_ZONES: dict[int, list[tuple[int, int, str, str]]] = {
    186: [  # Mining
        (1, 14, "Dun Morogh / Elwynn / Azuremyst", "Copper Vein & Rough Stone (159/127/91 nodes)"),
        (10, 22, "Westfall / Loch Modan / Darkshore / Bloodmyst", "Copper & Tin Veins (99-103 Copper, 33-46 Tin)"),
        (18, 32, "Redridge / Duskwood / Wetlands / Ashenvale", "Tin & Silver Veins (44-63 Tin nodes)"),
        (28, 42, "Stranglethorn Vale / Arathi / Badlands", "Iron & Mithril Deposits (82-98 Iron nodes)"),
        (40, 52, "The Hinterlands / Tanaris / Searing Gorge", "Mithril Deposits (52-79 nodes)"),
        (50, 60, "Un'Goro Crater / Eastern Plaguelands / Burning Steppes", "Thorium Veins (211 in Un'Goro!)"),
    ],
    182: [  # Herbalism
        (1, 14, "Teldrassil / Dun Morogh / Elwynn / Azuremyst", "Peacebloom, Silverleaf, Earthroot (182-240 herbs)"),
        (10, 24, "Westfall / Loch Modan / Darkshore / Bloodmyst", "Briarthorn, Mageroyal, Bruiseweed, Stranglekelp"),
        (20, 34, "Wetlands / Duskwood / Redridge / Ashenvale", "Briarthorn, Kingsblood, Liferoot, Stranglekelp"),
        (30, 44, "Stranglethorn Vale / Arathi / Dustwallow Marsh", "Goldthorn, Kingsblood, Liferoot, Wild Steelbloom"),
        (42, 54, "Feralas / The Hinterlands / Azshara", "Sungrass & Golden Sansam"),
        (50, 60, "Un'Goro Crater / Eastern Plaguelands / Winterspring", "Mountain Silversage, Golden Sansam, Dreamfoil"),
    ],
    393: [  # Skinning
        (1, 14, "Elwynn / Dun Morogh / Teldrassil / Azuremyst", "Ruined Leather Scraps & Light Leather"),
        (12, 24, "Westfall / Loch Modan / Darkshore / Wetlands", "Light & Medium Leather"),
        (24, 38, "Duskwood / Ashenvale / Stranglethorn Vale", "Medium & Heavy Leather"),
        (38, 50, "Feralas / Tanaris / The Hinterlands", "Heavy & Thick Leather"),
        (50, 60, "Un'Goro Crater / Winterspring / Burning Steppes", "Thick & Rugged Leather"),
    ],
    356: [  # Fishing (Sagefish priority!)
        (10, 25, "Darkshore / Loch Modan (Lesser Sagefish Schools)", "Raw Sagefish (25 schools in Darkshore!)"),
        (22, 36, "Hillsbrad Foothills / Ashenvale (Sagefish Schools)", "Raw Sagefish (16 schools in Hillsbrad!)"),
        (32, 48, "Stranglethorn Vale / Hinterlands (Greater Sagefish Schools)", "Raw Greater Sagefish"),
    ],
}


def best_farm_hint(skill_id: int, level: int) -> str:
    zones = BEST_FARM_ZONES.get(skill_id)
    if not zones:
        return ""
    for lo, hi, zname, detail in zones:
        if lo <= level <= hi:
            return f"Best spots: {zname} ({detail})"
    lo, hi, zname, detail = zones[-1]
    return f"Best spots: {zname} ({detail})"


def compute_supply_report(
    char_skills: list[dict[str, Any]],
    vault_counts: dict[int, int],
) -> dict[str, Any]:
    """Compute exact 40-character Classic (1-300) material demand vs Hearthguard Guild Bank stock.

    char_skills: list of {"name": str, "level": int, "skills": {skill_id: (val, max)}}
    vault_counts: {item_entry: total_qty_in_guild_bank}
    """
    prof_summary: dict[int, dict[str, Any]] = {}
    # item_entry -> accumulator
    active_need: dict[int, float] = {}
    full300_need: dict[int, float] = {}
    drivers_active: dict[int, dict[int, int]] = {}  # item_entry -> {skill_id: char_count}
    drivers_full: dict[int, dict[int, int]] = {}

    for ch in char_skills:
        for sk_id, (val, _mx) in ch["skills"].items():
            if sk_id in SKILL_NAMES:
                ps = prof_summary.setdefault(
                    sk_id,
                    {"skill_id": sk_id, "name": SKILL_NAMES[sk_id], "count": 0, "min": 999, "max": 0, "sum": 0, "tiers": {"1-75": 0, "75-150": 0, "150-225": 0, "225-300": 0}},
                )
                ps["count"] += 1
                ps["min"] = min(ps["min"], val)
                ps["max"] = max(ps["max"], val)
                ps["sum"] += val
                if val < 75:
                    ps["tiers"]["1-75"] += 1
                elif val < 150:
                    ps["tiers"]["75-150"] += 1
                elif val < 225:
                    ps["tiers"]["150-225"] += 1
                else:
                    ps["tiers"]["225-300"] += 1

            tiers = PROFESSION_GUIDE_TIERS.get(sk_id)
            if not tiers or val >= 300:
                continue

            for lo, hi, mats in tiers:
                if val >= hi:
                    continue
                # Remaining fraction of this bracket
                frac = 1.0 if val <= lo else max(0.15, (hi - val) / float(hi - lo))
                # "Active now" = character's current bracket (lo <= val < hi) OR within 15 pts of entering it
                is_active_tier = (val < hi) and (val + 15 >= lo)
                for item_entry, qty in mats.items():
                    need_q = qty * frac
                    full300_need[item_entry] = full300_need.get(item_entry, 0.0) + need_q
                    df = drivers_full.setdefault(item_entry, {})
                    df[sk_id] = df.get(sk_id, 0) + 1
                    if is_active_tier:
                        active_need[item_entry] = active_need.get(item_entry, 0.0) + need_q
                        da = drivers_active.setdefault(item_entry, {})
                        da[sk_id] = da.get(sk_id, 0) + 1

    # Add priority-2 guild-wide Sagefish reserve (Cooking 80+ Mp5 leveling food for Wonky & casters)
    active_need[21071] = active_need.get(21071, 0.0) + 80
    full300_need[21071] = full300_need.get(21071, 0.0) + 160
    drivers_active.setdefault(21071, {})[185] = drivers_active.get(21071, {}).get(185, 0) + 1

    def vault_have_for(entry: int) -> int:
        eq = VAULT_EQUIVALENTS.get(entry, [entry])
        return sum(vault_counts.get(e, 0) for e in eq)

    def fmt_drivers(dmap: dict[int, int]) -> str:
        parts = [f"{cnt} {SKILL_NAMES.get(sid, str(sid))}" for sid, cnt in sorted(dmap.items(), key=lambda kv: -kv[1])]
        return ", ".join(parts[:4])

    materials: list[dict[str, Any]] = []
    for entry, full_q in full300_need.items():
        if entry not in MAT_CATALOG:
            continue
        name, g_skill, cat = MAT_CATALOG[entry]
        act_q = int(round(active_need.get(entry, 0.0)))
        tot_q = int(round(full_q))
        have = vault_have_for(entry)
        act_def = max(0, act_q - have)
        tot_def = max(0, tot_q - have)
        pct_act = round(min(100.0, 100.0 * have / max(1, act_q)), 1) if act_q > 0 else 100.0
        pct_300 = round(min(100.0, 100.0 * have / max(1, tot_q)), 1) if tot_q > 0 else 100.0
        materials.append({
            "item_entry": entry,
            "name": name,
            "category": cat,
            "gather_skill": g_skill,
            "gather_skill_name": SKILL_NAMES.get(g_skill, "Drops/Loot"),
            "vault_have": have,
            "active_need": act_q,
            "active_deficit": act_def,
            "active_pct": pct_act,
            "full_300_need": tot_q,
            "full_300_deficit": tot_def,
            "full_300_pct": pct_300,
            "driven_by_active": fmt_drivers(drivers_active.get(entry, {})),
            "driven_by_all": fmt_drivers(drivers_full.get(entry, {})),
        })

    materials.sort(key=lambda m: (-m["active_deficit"], -m["full_300_deficit"], m["name"]))

    profs_out = []
    for sid, ps in sorted(prof_summary.items(), key=lambda kv: kv[1]["name"]):
        profs_out.append({
            "skill_id": sid,
            "name": ps["name"],
            "count": ps["count"],
            "min": ps["min"] if ps["count"] else 0,
            "avg": int(round(ps["sum"] / max(1, ps["count"]))),
            "max": ps["max"],
            "tiers": ps["tiers"],
        })

    return {
        "characters_analyzed": len(char_skills),
        "professions": profs_out,
        "materials": materials,
    }
