"""Static WotLK 3.3.5a reference maps for Azeroth Watch."""

CLASSES = {
    1: {"name": "Warrior", "short": "WAR", "color": "#C79C6E", "default_role": "tank"},
    2: {"name": "Paladin", "short": "PAL", "color": "#F58CBA", "default_role": "tank"},
    3: {"name": "Hunter", "short": "HUN", "color": "#ABD473", "default_role": "rdps"},
    4: {"name": "Rogue", "short": "ROG", "color": "#FFF569", "default_role": "mdps"},
    5: {"name": "Priest", "short": "PRI", "color": "#FFFFFF", "default_role": "healer"},
    6: {"name": "Death Knight", "short": "DK", "color": "#C41F3B", "default_role": "mdps"},
    7: {"name": "Shaman", "short": "SHA", "color": "#0070DE", "default_role": "healer"},
    8: {"name": "Mage", "short": "MAG", "color": "#69CCF0", "default_role": "rdps"},
    9: {"name": "Warlock", "short": "WLK", "color": "#9482C9", "default_role": "rdps"},
    11: {"name": "Druid", "short": "DRU", "color": "#FF7D0A", "default_role": "healer"},
}

RACES = {
    1: "Human",
    2: "Orc",
    3: "Dwarf",
    4: "Night Elf",
    5: "Undead",
    6: "Tauren",
    7: "Gnome",
    8: "Troll",
    10: "Blood Elf",
    11: "Draenei",
}

# Explicit Hearthguard companion raid roles (overriding class defaults where useful)
COMPANION_ROLES = {
    "Brannoc": "tank",
    "Aldric": "healer",
    "Kelda": "rdps",
    "Wren": "mdps",
    "Ashwick": "healer",
    "Flintlock": "rdps",
    "Bramblepaw": "tank",
    "Tidecaller": "healer",
    "Ironbulwark": "tank",
    "Kraghammer": "tank",
    "Alaric": "tank",
    "Korrath": "tank",
    "Roehl": "tank",
    "Solara": "healer",
    "Mirella": "healer",
    "Moonwhisper": "healer",
    "Stormcaller": "healer",
    "Aelindra": "healer",
    "Lyraeth": "healer",
    "Thornweaver": "healer",
}

PRIMARY_SKILLS = {
    171: {"name": "Alchemy", "kind": "craft", "short": "Alc"},
    164: {"name": "Blacksmithing", "kind": "craft", "short": "BS"},
    333: {"name": "Enchanting", "kind": "craft", "short": "Ench"},
    202: {"name": "Engineering", "kind": "craft", "short": "Eng"},
    182: {"name": "Herbalism", "kind": "gather", "short": "Herb"},
    773: {"name": "Inscription", "kind": "craft", "short": "Insc"},
    755: {"name": "Jewelcrafting", "kind": "craft", "short": "JC"},
    165: {"name": "Leatherworking", "kind": "craft", "short": "LW"},
    186: {"name": "Mining", "kind": "gather", "short": "Mine"},
    393: {"name": "Skinning", "kind": "gather", "short": "Skin"},
    197: {"name": "Tailoring", "kind": "craft", "short": "Tail"},
}

SECONDARY_SKILLS = {
    185: {"name": "Cooking", "kind": "secondary", "short": "Cook"},
    129: {"name": "First Aid", "kind": "secondary", "short": "FA"},
    356: {"name": "Fishing", "kind": "secondary", "short": "Fish"},
}

ALL_TRACKED_SKILLS = {**PRIMARY_SKILLS, **SECONDARY_SKILLS}

ITEM_QUALITY_COLORS = {
    0: "#9d9d9d",
    1: "#e8ecf2",
    2: "#1eff00",
    3: "#3b9eff",
    4: "#c06eff",
    5: "#ff8000",
}

def categorize_item(item_class: int, subclass: int, name: str = "") -> str:
    """Group Guild Bank items into raid/crafting material buckets."""
    lname = name.lower()
    if item_class == 0:  # Consumables
        if subclass in (1, 2, 3):  # Potion, Elixir, Flask
            return "Potions & Flasks"
        if subclass == 5:  # Food & Drink
            return "Food & Bandages"
        if subclass == 7:  # Bandage
            return "Food & Bandages"
        return "Potions & Flasks"
    if item_class == 3:  # Gems
        return "Gems & Enchants"
    if item_class == 7:  # Trade Goods
        if subclass == 7:  # Metal & Stone
            return "Ores & Bars"
        if subclass == 9:  # Herb
            return "Herbs & Alchemy"
        if subclass in (5, 6):  # Cloth, Leather
            return "Cloth & Leather"
        if subclass == 12:  # Enchanting
            return "Gems & Enchants"
        if subclass == 4:  # Jewelcrafting
            return "Gems & Enchants"
        if subclass == 8:  # Meat / Cooking
            return "Food & Bandages"
        if subclass in (1, 2, 3):  # Parts, Explosives, Devices
            return "Engineering & Parts"
        if subclass == 10:  # Elemental
            return "Elementals & Misc"
        if any(k in lname for k in ("herb", "leaf", "root", "bloom", "lotus", "moss")):
            return "Herbs & Alchemy"
        if any(k in lname for k in ("ore", "bar", "stone")):
            return "Ores & Bars"
        return "Elementals & Misc"
    if item_class == 9:
        return "Recipes & Plans"
    return "Gear & Misc"

MAT_CATEGORY_ORDER = [
    "Ores & Bars",
    "Herbs & Alchemy",
    "Cloth & Leather",
    "Potions & Flasks",
    "Gems & Enchants",
    "Food & Bandages",
    "Engineering & Parts",
    "Elementals & Misc",
    "Recipes & Plans",
    "Gear & Misc",
]

REP_TIERS = [
    (-42000, -6000, "Hated"),
    (-6000, -3000, "Hostile"),
    (-3000, 0, "Unfriendly"),
    (0, 3000, "Neutral"),
    (3000, 9000, "Friendly"),
    (9000, 21000, "Honored"),
    (21000, 42000, "Revered"),
    (42000, 42999, "Exalted"),
]

def rep_standing_info(standing: int):
    for low, high, label in REP_TIERS:
        if standing < high:
            span = max(1, high - low)
            cur = max(0, standing - low)
            pct = round(min(100.0, (standing / 42000.0) * 100.0), 1) if standing > 0 else 0.0
            return {"rank": label, "current": cur, "max": span, "to_exalted_pct": pct}
    return {"rank": "Exalted", "current": 999, "max": 999, "to_exalted_pct": 100.0}

# Comprehensive WotLK 3.3.5a Zone IDs -> Names
ZONE_NAMES = {
    # Eastern Kingdoms
    1: "Dun Morogh",
    3: "Badlands",
    4: "Blasted Lands",
    8: "Swamp of Sorrows",
    10: "Duskwood",
    11: "Wetlands",
    12: "Elwynn Forest",
    25: "Blackrock Mountain",
    28: "Western Plaguelands",
    33: "Stranglethorn Vale",
    36: "Alterac Mountains",
    38: "Loch Modan",
    40: "Westfall",
    41: "Deadwind Pass",
    44: "Redridge Mountains",
    45: "Arathi Highlands",
    46: "Burning Steppes",
    47: "The Hinterlands",
    51: "Searing Gorge",
    85: "Tirisfal Glades",
    130: "Silverpine Forest",
    139: "Eastern Plaguelands",
    267: "Hillsbrad Foothills",
    1497: "Undercity",
    1519: "Stormwind City",
    1537: "Ironforge",
    2257: "Deeprun Tram",
    3430: "Eversong Woods",
    3433: "Ghostlands",
    3487: "Silvermoon City",
    4080: "Isle of Quel'Danas",
    # Kalimdor
    14: "Durotar",
    15: "Dustwallow Marsh",
    16: "Azshara",
    17: "The Barrens",
    141: "Teldrassil",
    148: "Darkshore",
    215: "Mulgore",
    331: "Ashenvale",
    357: "Feralas",
    361: "Felwood",
    400: "Thousand Needles",
    405: "Desolace",
    406: "Stonetalon Mountains",
    440: "Tanaris",
    490: "Un'Goro Crater",
    493: "Moonglade",
    618: "Winterspring",
    1377: "Silithus",
    1637: "Orgrimmar",
    1638: "Thunder Bluff",
    1657: "Darnassus",
    3524: "Azuremyst Isle",
    3525: "Bloodmyst Isle",
    3557: "The Exodar",
    # Outland
    3483: "Hellfire Peninsula",
    3518: "Nagrand",
    3519: "Terokkar Forest",
    3520: "Shadowmoon Valley",
    3521: "Zangarmarsh",
    3522: "Blade's Edge Mountains",
    3523: "Netherstorm",
    3703: "Shattrath City",
    # Northrend
    65: "Dragonblight",
    66: "Zul'Drak",
    67: "The Storm Peaks",
    210: "Icecrown",
    394: "Grizzly Hills",
    495: "Howling Fjord",
    2817: "Crystalsong Forest",
    3537: "Borean Tundra",
    3711: "Sholazar Basin",
    4197: "Wintergrasp",
    4395: "Dalaran",
    # Classic Dungeons & Raids
    209: "Shadowfang Keep",
    491: "Razorfen Kraul",
    717: "The Stockade",
    718: "Wailing Caverns",
    719: "Blackfathom Deeps",
    721: "Gnomeregan",
    722: "Razorfen Downs",
    796: "Scarlet Monastery",
    1176: "Zul'Farrak",
    1337: "Uldaman",
    1477: "Sunken Temple",
    1581: "The Deadmines",
    1583: "Blackrock Spire",
    1584: "Blackrock Depths",
    1977: "Zul'Gurub",
    2017: "Stratholme",
    2057: "Scholomance",
    2100: "Maraudon",
    2159: "Onyxia's Lair",
    2437: "Ragefire Chasm",
    2557: "Dire Maul",
    2677: "Blackwing Lair",
    2717: "Molten Core",
    3428: "Temple of Ahn'Qiraj",
    3429: "Ruins of Ahn'Qiraj",
    # TBC Dungeons & Raids
    3457: "Karazhan",
    3562: "Hellfire Ramparts",
    3606: "Hyjal Summit",
    3607: "Serpentshrine Cavern",
    3713: "The Blood Furnace",
    3714: "The Shattered Halls",
    3715: "The Steamvault",
    3716: "The Underbog",
    3717: "The Slave Pens",
    3789: "Shadow Labyrinth",
    3790: "Auchenai Crypts",
    3791: "Sethekk Halls",
    3792: "Mana-Tombs",
    3805: "Zul'Aman",
    3836: "Magtheridon's Lair",
    3845: "Tempest Keep",
    3923: "Gruul's Lair",
    3959: "Black Temple",
    4075: "Sunwell Plateau",
    # WotLK Dungeons & Raids
    206: "Utgarde Keep",
    1196: "Utgarde Pinnacle",
    3456: "Naxxramas",
    4100: "Culling of Stratholme",
    4228: "The Oculus",
    4264: "Halls of Stone",
    4265: "The Nexus",
    4272: "Halls of Lightning",
    4273: "Ulduar",
    4277: "Azjol-Nerub",
    4493: "The Obsidian Sanctum",
    4494: "Ahn'kahet",
    4500: "The Eye of Eternity",
    4722: "Trial of the Crusader",
    4812: "Icecrown Citadel",
    4987: "The Ruby Sanctum",
}

SHIFT_SCHEDULE = [
    {"id": "core-wonky", "label": "Core Wonky", "start": 0, "end": 24, "days": "Daily", "always": True},
    {"id": "core-logan", "label": "Core Logan", "start": 0, "end": 24, "days": "Daily", "always": True},
    {"id": "night-owls", "label": "Night Owls", "start": 0, "end": 5, "days": "Daily", "always": False},
    {"id": "early-birds", "label": "Early Birds", "start": 5, "end": 10, "days": "Daily", "always": False},
    {"id": "midday-crew", "label": "Midday Crew", "start": 10, "end": 15, "days": "Daily", "always": False},
    {"id": "afternoon-crew", "label": "Afternoon Crew", "start": 15, "end": 19, "days": "Daily", "always": False},
    {"id": "prime-time", "label": "Prime Time", "start": 19, "end": 24, "days": "Daily", "always": False},
    {"id": "weekend-raiders", "label": "Weekend Raiders", "start": 13, "end": 21, "days": "Fri-Sun", "always": False},
]

def zone_name(zone_id: int) -> str:
    return ZONE_NAMES.get(zone_id, f"Zone #{zone_id}")
