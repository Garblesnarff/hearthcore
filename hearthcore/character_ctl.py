"""Character Sheet & Companion Direction Controls for Azeroth Watch (zero worldserver downtime).

Provides:
- On-demand full WoW Character Sheet reads (`fetch_character_sheet`) for BOTH AI companions
  and human players (Wonky, Logan, etc.), keeping the 4-second dashboard polling loop untouched.
- All 5 live companion direction controls:
  1. One-Click Focus Presets -> expiring `companion_weight_overrides` (`source='operator'`, monotonic revision)
  2. Manual Weight Sliders (`0.5x` - `1.5x`) + Pin/Unpin (`companion_weights`, `source='manual'`)
  3. Free-Text Directive to Claude ("Next Plan Note") + immediate single-character replan
  4. Per-Character Pause / Resume AI (`companion_weight_overrides.enabled = 0/1`)
  5. Quick Guild Work Order creation + Shift Schedule / Always-Online toggle

Least-privilege DB separation:
- Reads use `AZEROTH_WATCH_DB_*` (`azeroth_watch`, strictly SELECT-only).
- Operator writes use `HEARTHCORE_OPERATOR_DB_*` (`hearthcore_operator`, narrowly scoped to
  `companion_weights`, `companion_weight_overrides`, `companion_work_orders`, `companion_schedule`),
  keeping `hearthcore_brain` strictly unable to write `companion_weights` or `companion_schedule`.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bridge import (
    BRAIN_DB_PATH,
    DB_CONTAINER,
    DEFAULT_SQL_TIMEOUT_SEC,
    LIVE_POS_FILE,
    PROFILE_PREF_TO_NATIVE_WEIGHTS,
    RUNTIME_DIR,
    BrainStore,
    _clamp,
    _int_or,
    _sql_esc,
    fetch_remote_max_revision,
    is_brain_disabled,
    parse_mysql_tsv,
    persona_baseline_weights,
    read_live_context,
    run_sql,
)
from .core import Invalid, ROOT, profile, profiles
from .guides import LEVELING_ORDER_CATALOG, ORDER_BY_ENTRY, SKILL_NAMES
from .memory import MemoryEngine

logger = logging.getLogger("hearthcore.character_ctl")

SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")

CANONICAL_WEIGHT_KEYS: tuple[str, ...] = (
    "quest_work",
    "quest_turnin",
    "quest_hub",
    "quest_grind",
    "craft",
    "gather",
    "fish",
    "train",
    "bank",
    "repair",
    "sell",
    "travel",
    "hearth",
)

WEIGHT_META: dict[str, dict[str, str]] = {
    "quest_work": {"label": "Quest Objectives", "group": "Questing"},
    "quest_turnin": {"label": "Quest Turn-Ins", "group": "Questing"},
    "quest_hub": {"label": "Quest Hub Pickups", "group": "Questing"},
    "quest_grind": {"label": "Mob Grinding", "group": "Questing"},
    "craft": {"label": "Crafting & Orders", "group": "Professions"},
    "gather": {"label": "Gathering Nodes", "group": "Professions"},
    "fish": {"label": "Fishing", "group": "Professions"},
    "train": {"label": "Visit Trainers", "group": "Town & Upkeep"},
    "bank": {"label": "Guild Bank", "group": "Town & Upkeep"},
    "repair": {"label": "Repair Gear", "group": "Town & Upkeep"},
    "sell": {"label": "Vendor Loot", "group": "Town & Upkeep"},
    "travel": {"label": "World Travel", "group": "Town & Upkeep"},
    "hearth": {"label": "Hearth / Rest", "group": "Town & Upkeep"},
}

FOCUS_PRESETS: dict[str, dict[str, Any]] = {
    "craft": {
        "id": "craft",
        "label": "Craft & Profs",
        "icon": "⚒",
        "summary": "Prioritize crafting recipes, guild bank mats, and trainer unlocks.",
        "weights": {
            "craft": 1.50,
            "bank": 1.25,
            "gather": 1.15,
            "train": 1.15,
            "quest_work": 0.65,
            "quest_grind": 0.55,
            "fish": 0.65,
        },
    },
    "gather": {
        "id": "gather",
        "label": "Gather Mats",
        "icon": "⛏",
        "summary": "Farm ore, herbs, or leather nodes and stock the Hearthguard vault.",
        "weights": {
            "gather": 1.50,
            "bank": 1.25,
            "travel": 1.15,
            "craft": 0.90,
            "quest_work": 0.65,
            "quest_grind": 0.55,
            "fish": 0.65,
        },
    },
    "quest": {
        "id": "quest",
        "label": "Push Quests",
        "icon": "📜",
        "summary": "Focus on leveling, completing zone quests, and turning in objectives.",
        "weights": {
            "quest_work": 1.50,
            "quest_turnin": 1.45,
            "quest_hub": 1.35,
            "quest_grind": 1.20,
            "repair": 1.10,
            "craft": 0.65,
            "gather": 0.65,
            "fish": 0.55,
        },
    },
    "fish": {
        "id": "fish",
        "label": "Relax & Fish",
        "icon": "🎣",
        "summary": "Spend the session fishing for guild cooking supplies and banking catches.",
        "weights": {
            "fish": 1.50,
            "bank": 1.20,
            "quest_work": 0.60,
            "quest_grind": 0.50,
            "craft": 0.70,
            "gather": 0.70,
        },
    },
    "orders": {
        "id": "orders",
        "label": "Fill Work Orders",
        "icon": "📦",
        "summary": "Focus on open guild work orders, crafting items, and vault withdrawals.",
        "weights": {
            "craft": 1.50,
            "bank": 1.40,
            "gather": 1.25,
            "quest_work": 0.60,
            "quest_grind": 0.50,
            "fish": 0.55,
        },
    },
    "train_repair": {
        "id": "train_repair",
        "label": "Trainer & Repair",
        "icon": "🎓",
        "summary": "Head to town to visit class/profession trainers, repair, sell junk, and bank.",
        "weights": {
            "train": 1.50,
            "repair": 1.45,
            "sell": 1.35,
            "hearth": 1.35,
            "bank": 1.25,
            "quest_grind": 0.55,
        },
    },
}

VALID_SHIFT_GROUPS: dict[str, dict[str, Any]] = {
    "core-wonky": {"label": "Core Wonky", "start_hour": 0, "end_hour": 24, "days_mask": 127, "default_always": 1},
    "core-logan": {"label": "Core Logan", "start_hour": 0, "end_hour": 24, "days_mask": 127, "default_always": 1},
    "night-owls": {"label": "Night Owls", "start_hour": 0, "end_hour": 5, "days_mask": 127, "default_always": 0},
    "early-birds": {"label": "Early Birds", "start_hour": 5, "end_hour": 10, "days_mask": 127, "default_always": 0},
    "midday-crew": {"label": "Midday Crew", "start_hour": 10, "end_hour": 15, "days_mask": 127, "default_always": 0},
    "afternoon-crew": {"label": "Afternoon Crew", "start_hour": 15, "end_hour": 19, "days_mask": 127, "default_always": 0},
    "prime-time": {"label": "Prime Time", "start_hour": 19, "end_hour": 24, "days_mask": 127, "default_always": 0},
    "weekend-raiders": {"label": "Weekend Raiders", "start_hour": 13, "end_hour": 21, "days_mask": 97, "default_always": 0},
}

EQUIP_SLOTS: tuple[tuple[int, str, str], ...] = (
    (0, "Head", "left"),
    (1, "Neck", "left"),
    (2, "Shoulder", "left"),
    (14, "Back", "left"),
    (4, "Chest", "left"),
    (3, "Shirt", "left"),
    (18, "Tabard", "left"),
    (8, "Wrist", "left"),
    (9, "Hands", "right"),
    (5, "Waist", "right"),
    (6, "Legs", "right"),
    (7, "Feet", "right"),
    (10, "Finger 1", "right"),
    (11, "Finger 2", "right"),
    (12, "Trinket 1", "right"),
    (13, "Trinket 2", "right"),
    (15, "Main Hand", "weapon"),
    (16, "Off Hand", "weapon"),
    (17, "Ranged / Relic", "weapon"),
)

ITEM_QUALITY_META: dict[int, dict[str, str]] = {
    0: {"name": "Poor", "color": "#9d9d9d"},
    1: {"name": "Common", "color": "#e8ecf2"},
    2: {"name": "Uncommon", "color": "#1eff00"},
    3: {"name": "Rare", "color": "#3b9eff"},
    4: {"name": "Epic", "color": "#c06eff"},
    5: {"name": "Legendary", "color": "#ff8000"},
    6: {"name": "Artifact", "color": "#e6cc80"},
    7: {"name": "Heirloom", "color": "#e6cc80"},
}

CLASSES: dict[int, dict[str, str]] = {
    1: {"name": "Warrior", "short": "WAR", "color": "#C79C6E", "power": "Rage"},
    2: {"name": "Paladin", "short": "PAL", "color": "#F58CBA", "power": "Mana"},
    3: {"name": "Hunter", "short": "HUN", "color": "#ABD473", "power": "Mana"},
    4: {"name": "Rogue", "short": "ROG", "color": "#FFF569", "power": "Energy"},
    5: {"name": "Priest", "short": "PRI", "color": "#FFFFFF", "power": "Mana"},
    6: {"name": "Death Knight", "short": "DK", "color": "#C41F3B", "power": "Runic Power"},
    7: {"name": "Shaman", "short": "SHA", "color": "#0070DE", "power": "Mana"},
    8: {"name": "Mage", "short": "MAG", "color": "#69CCF0", "power": "Mana"},
    9: {"name": "Warlock", "short": "WLK", "color": "#9482C9", "power": "Mana"},
    11: {"name": "Druid", "short": "DRU", "color": "#FF7D0A", "power": "Mana"},
}

RACES: dict[int, str] = {
    1: "Human", 2: "Orc", 3: "Dwarf", 4: "Night Elf", 5: "Undead",
    6: "Tauren", 7: "Gnome", 8: "Troll", 10: "Blood Elf", 11: "Draenei",
}

FACTION_NAMES: dict[int, str] = {
    21: "Booty Bay",
    47: "Ironforge",
    54: "Gnomeregan Exiles",
    59: "Thorium Brotherhood",
    67: "Horde",
    69: "Darnassus",
    70: "Syndicate",
    72: "Stormwind",
    76: "Orgrimmar",
    87: "Bloodsail Buccaneers",
    270: "Zandalar Tribe",
    349: "Ravenholdt",
    369: "Gadgetzan",
    469: "Alliance",
    470: "Ratchet",
    509: "The League of Arathor",
    529: "Argent Dawn",
    576: "Timbermaw Hold",
    577: "Everlook",
    589: "Wintersaber Trainers",
    609: "Cenarion Circle",
    730: "Stormpike Guard",
    749: "Hydraxian Waterlords",
    809: "Shen'dralar",
    890: "Silverwing Sentinels",
    909: "Darkmoon Faire",
    910: "Brood of Nozdormu",
    930: "Exodar",
    942: "Cenarion Expedition",
    946: "Honor Hold",
    978: "Kurenai",
    1037: "Alliance Vanguard",
    1050: "Valiance Expedition",
    1068: "Explorers' League",
    1090: "Kirin Tor",
    1091: "The Wyrmrest Accord",
    1098: "Knights of the Ebon Blade",
    1106: "Argent Crusade",
    1119: "The Sons of Hodir",
}

REP_TIERS = (
    (-42000, -6000, "Hated"),
    (-6000, -3000, "Hostile"),
    (-3000, 0, "Unfriendly"),
    (0, 3000, "Neutral"),
    (3000, 9000, "Friendly"),
    (9000, 21000, "Honored"),
    (21000, 42000, "Revered"),
    (42000, 42999, "Exalted"),
)

PRIMARY_SKILL_IDS = {164, 165, 171, 182, 186, 197, 202, 333, 393, 755, 773}
SECONDARY_SKILL_IDS = {129, 185, 356}
ALL_TRACKED_SKILL_IDS = PRIMARY_SKILL_IDS | SECONDARY_SKILL_IDS

# Import rich WotLK zone names from azeroth-watch/data_maps via importlib (never exec), else fallback
def _load_zone_names() -> dict[int, str]:
    try:
        aw_maps = ROOT / "azeroth-watch" / "data_maps.py"
        if aw_maps.is_file():
            spec = importlib.util.spec_from_file_location("_aw_data_maps_ro", aw_maps)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                zm = getattr(mod, "ZONE_NAMES", None)
                if isinstance(zm, dict):
                    return dict(zm)
    except Exception as exc:
        logger.debug("Falling back to minimal ZONE_NAMES: %s", exc)
    return {
        1: "Dun Morogh", 10: "Duskwood", 11: "Wetlands", 12: "Elwynn Forest",
        33: "Stranglethorn Vale", 38: "Loch Modan", 40: "Westfall", 44: "Redridge Mountains",
        45: "Arathi Highlands", 141: "Teldrassil", 148: "Darkshore", 331: "Ashenvale",
        1519: "Stormwind City", 1537: "Ironforge", 1657: "Darnassus",
        3524: "Azuremyst Isle", 3525: "Bloodmyst Isle", 3557: "The Exodar",
    }


ZONE_NAMES = _load_zone_names()


def _rep_standing_info(standing: int) -> dict[str, Any]:
    for low, high, label in REP_TIERS:
        if standing < high:
            span = max(1, high - low)
            cur = max(0, standing - low)
            pct = round(min(100.0, (standing / 42000.0) * 100.0), 1) if standing > 0 else 0.0
            tier_pct = round(min(100.0, (cur / span) * 100.0), 1)
            return {"rank": label, "current": cur, "max": span, "tier_pct": tier_pct, "to_exalted_pct": pct}
    return {"rank": "Exalted", "current": 999, "max": 999, "tier_pct": 100.0, "to_exalted_pct": 100.0}


def _read_dotenv_var(key: str) -> str:
    val = os.getenv(key, "").strip()
    if val:
        return val
    env_path = Path(os.getenv("HEARTHCORE_ENV_PATH", str(ROOT / ".env")))
    if not env_path.is_file():
        return ""
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            ln = raw.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            ek, ev = ln.split("=", 1)
            if ek.strip() == key:
                return ev.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def _exec_mysql_with_profile(
    sql: str,
    *,
    profile_label: str,
    defaults_env: str,
    container_defaults_env: str,
    user_env: str = "",
    pass_env: str = "",
    host_env: str = "",
    port_env: str = "",
    timeout: float | None = None,
    expected_cols: int | None = None,
) -> list[list[Any]]:
    use_timeout = float(timeout if timeout is not None else os.getenv("HEARTHCORE_SQL_TIMEOUT_SEC", str(DEFAULT_SQL_TIMEOUT_SEC)))
    db_name = os.getenv("HEARTHCORE_DB_NAME", "acore_characters").strip()
    defaults_file = _read_dotenv_var(defaults_env)
    container_defaults = _read_dotenv_var(container_defaults_env)
    db_host = os.getenv(host_env, "").strip() if host_env else ""
    db_user = os.getenv(user_env, "").strip() if user_env else ""
    db_pass = os.getenv(pass_env, "") if pass_env else ""
    db_port = os.getenv(port_env, "3306").strip() if port_env else "3306"

    sql_bytes = sql.encode("utf-8")
    if defaults_file:
        cmd = ["mysql", f"--defaults-extra-file={defaults_file}", "-N", "-B", db_name]
        res = subprocess.run(cmd, input=sql_bytes, capture_output=True, check=True, timeout=max(1.0, use_timeout))
        return parse_mysql_tsv(res.stdout.decode("utf-8", errors="replace"), expected_cols=expected_cols)
    if container_defaults and not (os.getenv("HEARTHCORE_TEST_DB") == "1" and os.getenv("HEARTHCORE_DB_HOST", "").strip()):
        cmd = [
            "docker", "exec", "-i", DB_CONTAINER,
            "mysql", f"--defaults-extra-file={container_defaults}", "-N", "-B", db_name,
        ]
        res = subprocess.run(cmd, input=sql_bytes, capture_output=True, check=True, timeout=max(1.0, use_timeout))
        return parse_mysql_tsv(res.stdout.decode("utf-8", errors="replace"), expected_cols=expected_cols)
    if db_host and db_user:
        env = dict(os.environ, MYSQL_PWD=db_pass)
        cmd = ["mysql", f"-h{db_host}", f"-P{db_port}", f"-u{db_user}", "-N", "-B", db_name]
        res = subprocess.run(cmd, input=sql_bytes, capture_output=True, check=True, timeout=max(1.0, use_timeout), env=env)
        return parse_mysql_tsv(res.stdout.decode("utf-8", errors="replace"), expected_cols=expected_cols)
    # Only allow CI throwaway-DB fallback when HEARTHCORE_TEST_DB=1 AND *_test DB name is explicitly configured!
    if os.getenv("HEARTHCORE_TEST_DB") == "1" and os.getenv("HEARTHCORE_DB_HOST", "").strip() and db_name.endswith("_test"):
        return run_sql(sql, timeout=use_timeout, expected_cols=expected_cols)
    # Fail closed in production — NEVER silently escalate to root or hearthcore_brain!
    raise Invalid(f"{profile_label} DB credentials not configured ({defaults_env} or {container_defaults_env})")


def run_read_sql(sql: str, *, timeout: float | None = None, expected_cols: int | None = None) -> list[list[Any]]:
    """Run read-only character sheet SQL using the SELECT-only azeroth_watch account (fail-closed)."""
    return _exec_mysql_with_profile(
        sql,
        profile_label="azeroth_watch read-only",
        defaults_env="AZEROTH_WATCH_DB_DEFAULTS_FILE",
        container_defaults_env="AZEROTH_WATCH_DB_CONTAINER_DEFAULTS_FILE",
        user_env="AZEROTH_WATCH_DB_USER",
        pass_env="AZEROTH_WATCH_DB_PASSWORD",
        host_env="AZEROTH_WATCH_DB_HOST",
        port_env="AZEROTH_WATCH_DB_PORT",
        timeout=timeout,
        expected_cols=expected_cols,
    )


def run_operator_sql(sql: str, *, timeout: float | None = None, expected_cols: int | None = None) -> list[list[Any]]:
    """Run operator direction SQL using the narrowly scoped hearthcore_operator account (fail-closed)."""
    return _exec_mysql_with_profile(
        sql,
        profile_label="hearthcore_operator",
        defaults_env="HEARTHCORE_OPERATOR_DB_DEFAULTS_FILE",
        container_defaults_env="HEARTHCORE_OPERATOR_DB_CONTAINER_DEFAULTS_FILE",
        user_env="HEARTHCORE_OPERATOR_DB_USER",
        pass_env="HEARTHCORE_OPERATOR_DB_PASSWORD",
        host_env="HEARTHCORE_OPERATOR_DB_HOST",
        port_env="HEARTHCORE_OPERATOR_DB_PORT",
        timeout=timeout,
        expected_cols=expected_cols,
    )


def validate_slug(raw: str) -> str:
    s = str(raw or "").strip().lower()
    if not s or not SLUG_RE.match(s):
        raise Invalid("Invalid character identifier")
    return s


def _resolve_character_row(slug: str, sql_exec) -> dict[str, Any] | None:
    clean = validate_slug(slug)
    rows = sql_exec(
        f"SELECT c.guid, c.name, c.race, c.class, c.gender, c.level, c.xp, c.money, "
        f"c.zone, c.map, c.online, c.totaltime, c.health, c.power1, "
        f"COALESCE(cs.shift_group, ''), COALESCE(cs.start_hour, 0), COALESCE(cs.end_hour, 24), "
        f"COALESCE(cs.days_mask, 127), COALESCE(cs.always_online, 0), COALESCE(cs.note, ''), "
        f"CASE WHEN cs.name IS NOT NULL THEN 1 ELSE 0 END "
        f"FROM characters c "
        f"LEFT JOIN companion_schedule cs ON LOWER(cs.name) = LOWER(c.name) "
        f"WHERE LOWER(c.name) = '{_sql_esc(clean)}' "
        f"LIMIT 1;"
    )
    if not rows or len(rows[0]) < 21:
        return None
    r = rows[0]
    return {
        "guid": _int_or(r[0]),
        "name": str(r[1] or ""),
        "slug": str(r[1] or "").lower(),
        "race_id": _int_or(r[2]),
        "class_id": _int_or(r[3]),
        "gender": _int_or(r[4]),
        "level": _int_or(r[5]),
        "xp": _int_or(r[6]),
        "money": _int_or(r[7]),
        "zone_id": _int_or(r[8]),
        "map_id": _int_or(r[9]),
        "online": _int_or(r[10]) == 1,
        "totaltime": _int_or(r[11]),
        "health": _int_or(r[12]),
        "power1": _int_or(r[13]),
        "schedule": {
            "shift_group": str(r[14] or ""),
            "start_hour": _int_or(r[15]),
            "end_hour": _int_or(r[16], 24),
            "days_mask": _int_or(r[17], 127),
            "always_online": _int_or(r[18]) == 1,
            "note": str(r[19] or ""),
        },
        "is_companion": _int_or(r[20]) == 1,
    }


def _safe_load_profile(slug: str) -> dict[str, Any] | None:
    try:
        if slug in set(profiles()):
            return profile(slug)
    except Exception:
        return None
    return None


def _parse_perm_enchant(ench_str: str | None) -> int:
    if not ench_str:
        return 0
    parts = str(ench_str).strip().split()
    if parts and parts[0].isdigit():
        return int(parts[0])
    return 0


def _money_parts(copper: int) -> dict[str, int]:
    c = max(0, int(copper))
    return {"gold": c // 10000, "silver": (c % 10000) // 100, "copper": c % 100, "total_copper": c}


def _work_order_catalog_for_skills(skill_ids: set[int]) -> list[dict[str, Any]]:
    """Return curated craftable work-order items matching this character's skills (or full catalog)."""
    items = []
    for t in LEVELING_ORDER_CATALOG:
        if not skill_ids or t.skill_id in skill_ids:
            items.append({
                "item_entry": t.item_entry,
                "name": t.item_name,
                "skill_id": t.skill_id,
                "skill_name": SKILL_NAMES.get(t.skill_id, ""),
                "min_skill": t.min_skill,
                "default_qty": max(1, min(20, t.qty)),
            })
    if not items:
        for t in LEVELING_ORDER_CATALOG[:25]:
            items.append({
                "item_entry": t.item_entry,
                "name": t.item_name,
                "skill_id": t.skill_id,
                "skill_name": SKILL_NAMES.get(t.skill_id, ""),
                "min_skill": t.min_skill,
                "default_qty": max(1, min(20, t.qty)),
            })
    return items[:40]


def fetch_character_sheet(
    slug_or_name: str,
    *,
    sql_exec=None,
    db_path: Path = BRAIN_DB_PATH,
    now_ts: int | None = None,
    redact_admin: bool = False,
    can_control: bool = False,
) -> dict[str, Any] | None:
    """Assemble full on-demand WoW Character Sheet + AI Brain + Direction state for one character."""
    runner = sql_exec if sql_exec is not None else run_read_sql
    ts = int(now_ts if now_ts is not None else time.time())
    char = _resolve_character_row(slug_or_name, runner)
    if char is None:
        return None

    guid = char["guid"]
    slug = char["slug"]
    is_comp = bool(char["is_companion"])
    char_prof = _safe_load_profile(slug) if is_comp else None

    # Live position overlay if fresh
    live = read_live_context()
    lp = (live.get("live_by_guid") or {}).get(guid)
    if isinstance(lp, dict):
        char["zone_id"] = int(lp.get("z", char["zone_id"]))
        char["map_id"] = int(lp.get("m", char["map_id"]))
        char["online"] = True
        dead = bool(lp.get("d"))
    else:
        dead = False

    cls_meta = CLASSES.get(char["class_id"], {"name": f"Class {char['class_id']}", "short": "???", "color": "#e8ecf2", "power": "Mana"})
    race_name = RACES.get(char["race_id"], f"Race {char['race_id']}")

    # XP cap for next level
    xp_max = 0
    try:
        xp_rows = runner(f"SELECT Experience FROM acore_world.player_xp_for_level WHERE level = {int(char['level'])};")
        if xp_rows and xp_rows[0]:
            xp_max = _int_or(xp_rows[0][0], 0)
    except Exception:
        xp_max = 0
    xp_pct = round(min(100.0, (char["xp"] / xp_max) * 100.0), 1) if xp_max > 0 else 100.0

    # Homebind
    homebind = {"zone_id": 0, "zone": "Unbound", "map_id": 0}
    try:
        hb_rows = runner(f"SELECT mapId, zoneId FROM character_homebind WHERE guid = {guid} LIMIT 1;")
        if hb_rows and len(hb_rows[0]) >= 2:
            hz = _int_or(hb_rows[0][1])
            homebind = {"map_id": _int_or(hb_rows[0][0]), "zone_id": hz, "zone": ZONE_NAMES.get(hz, f"Zone #{hz}")}
    except Exception as exc:
        logger.debug("Homebind query failed for guid=%s: %s", guid, exc)

    # 19-slot equipped gear (bag = 0, slot 0..18)
    equipped_by_slot: dict[int, dict[str, Any]] = {}
    try:
        eq_rows = runner(
            "SELECT ci.slot, ii.itemEntry, it.name, it.Quality, it.ItemLevel, it.RequiredLevel, "
            "it.armor, it.InventoryType, ii.enchantments, ii.durability "
            "FROM character_inventory ci "
            "JOIN item_instance ii ON ii.guid = ci.item "
            "JOIN acore_world.item_template it ON it.entry = ii.itemEntry "
            f"WHERE ci.guid = {guid} AND ci.bag = 0 AND ci.slot BETWEEN 0 AND 18 "
            "ORDER BY ci.slot;"
        )
        for r in eq_rows:
            if len(r) < 10:
                continue
            slot = _int_or(r[0])
            qual = _int_or(r[3], 1)
            qmeta = ITEM_QUALITY_META.get(qual, ITEM_QUALITY_META[1])
            ench_id = _parse_perm_enchant(r[8])
            equipped_by_slot[slot] = {
                "slot": slot,
                "item_entry": _int_or(r[1]),
                "name": str(r[2] or "Unknown Item"),
                "quality": qual,
                "quality_name": qmeta["name"],
                "color": qmeta["color"],
                "item_level": _int_or(r[4]),
                "req_level": _int_or(r[5]),
                "armor": _int_or(r[6]),
                "inv_type": _int_or(r[7]),
                "enchanted": ench_id > 0,
                "enchant_id": ench_id,
                "durability": _int_or(r[9]),
            }
    except Exception as exc:
        logger.debug("Equipment query failed for guid=%s: %s", guid, exc)

    gear_slots: list[dict[str, Any]] = []
    ilvl_vals: list[int] = []
    for slot_id, slot_label, side in EQUIP_SLOTS:
        item = equipped_by_slot.get(slot_id)
        gear_slots.append({
            "slot": slot_id,
            "label": slot_label,
            "side": side,
            "item": item,
        })
        # Exclude Shirt (3) & Tabard (18) from average iLvl calculation
        if item and slot_id not in (3, 18) and item["item_level"] > 0:
            ilvl_vals.append(item["item_level"])
    avg_ilvl = round(sum(ilvl_vals) / len(ilvl_vals), 1) if ilvl_vals else 0.0

    # Live combat stats
    stats: dict[str, Any] = {
        "max_health": char["health"],
        "max_mana": char["power1"],
        "strength": 0,
        "agility": 0,
        "stamina": 0,
        "intellect": 0,
        "spirit": 0,
        "armor": 0,
        "attack_power": 0,
        "ranged_attack_power": 0,
        "spell_power": 0,
        "crit_pct": 0.0,
        "ranged_crit_pct": 0.0,
        "spell_crit_pct": 0.0,
        "dodge_pct": 0.0,
        "parry_pct": 0.0,
        "block_pct": 0.0,
        "resilience": 0,
    }
    try:
        st_rows = runner(
            "SELECT maxhealth, maxpower1, strength, agility, stamina, intellect, spirit, armor, "
            "attackPower, rangedAttackPower, spellPower, critPct, rangedCritPct, spellCritPct, "
            "dodgePct, parryPct, blockPct, resilience "
            f"FROM character_stats WHERE guid = {guid} LIMIT 1;"
        )
        if st_rows and len(st_rows[0]) >= 18:
            sr = st_rows[0]
            stats = {
                "max_health": _int_or(sr[0], char["health"]),
                "max_mana": _int_or(sr[1], char["power1"]),
                "strength": _int_or(sr[2]),
                "agility": _int_or(sr[3]),
                "stamina": _int_or(sr[4]),
                "intellect": _int_or(sr[5]),
                "spirit": _int_or(sr[6]),
                "armor": _int_or(sr[7]),
                "attack_power": _int_or(sr[8]),
                "ranged_attack_power": _int_or(sr[9]),
                "spell_power": _int_or(sr[10]),
                "crit_pct": round(float(sr[11] or 0.0), 2),
                "ranged_crit_pct": round(float(sr[12] or 0.0), 2),
                "spell_crit_pct": round(float(sr[13] or 0.0), 2),
                "dodge_pct": round(float(sr[14] or 0.0), 2),
                "parry_pct": round(float(sr[15] or 0.0), 2),
                "block_pct": round(float(sr[16] or 0.0), 2),
                "resilience": _int_or(sr[17]),
            }
    except Exception as exc:
        logger.debug("Stats query failed for guid=%s: %s", guid, exc)

    # Professions / skills
    professions: list[dict[str, Any]] = []
    char_skill_ids: set[int] = set()
    try:
        sk_list = ",".join(str(s) for s in sorted(ALL_TRACKED_SKILL_IDS))
        sk_rows = runner(
            f"SELECT skill, value, max FROM character_skills WHERE guid = {guid} AND skill IN ({sk_list}) ORDER BY skill;"
        )
        for r in sk_rows:
            if len(r) < 3:
                continue
            sid = _int_or(r[0])
            val = _int_or(r[1])
            mx = _int_or(r[2])
            char_skill_ids.add(sid)
            kind = "primary" if sid in PRIMARY_SKILL_IDS else "secondary"
            professions.append({
                "skill_id": sid,
                "name": SKILL_NAMES.get(sid, f"Skill {sid}"),
                "kind": kind,
                "value": val,
                "max": mx,
                "pct": round(min(100.0, (val / max(1, mx)) * 100.0), 1),
                "at_trained_cap": bool(val >= mx and mx < 300),
            })
        professions.sort(key=lambda x: (0 if x["kind"] == "primary" else 1, -x["value"], x["name"]))
    except Exception as exc:
        logger.debug("Skills query failed for guid=%s: %s", guid, exc)

    # Active quests & completed count
    active_quests: list[dict[str, Any]] = []
    quests_completed_count = 0
    try:
        qc_rows = runner(f"SELECT COUNT(*) FROM character_queststatus_rewarded WHERE guid = {guid};")
        if qc_rows and qc_rows[0]:
            quests_completed_count = _int_or(qc_rows[0][0])
    except Exception as exc:
        logger.debug("Completed quests query failed for guid=%s: %s", guid, exc)
    try:
        q_rows = runner(
            "SELECT qs.quest, qs.status, COALESCE(qt.LogTitle, CONCAT('Quest #', qs.quest)), COALESCE(qt.QuestLevel, 0) "
            "FROM character_queststatus qs "
            "LEFT JOIN acore_world.quest_template qt ON qt.ID = qs.quest "
            f"WHERE qs.guid = {guid} AND qs.status IN (1, 3) "
            "ORDER BY qs.status ASC, qt.QuestLevel DESC, qs.quest ASC LIMIT 25;"
        )
        for r in q_rows:
            if len(r) < 4:
                continue
            st = _int_or(r[1])
            active_quests.append({
                "quest_id": _int_or(r[0]),
                "status": "complete" if st == 1 else "in_progress",
                "title": str(r[2] or f"Quest #{r[0]}"),
                "level": _int_or(r[3]),
            })
    except Exception as exc:
        logger.debug("Active quests query failed for guid=%s: %s", guid, exc)

    # Reputations (visible factions + companion_rep_goals)
    reputations: list[dict[str, Any]] = []
    goal_factions: dict[int, str] = {}
    try:
        rg_rows = runner(f"SELECT faction, note FROM companion_rep_goals WHERE guid = {guid};")
        for r in rg_rows:
            if len(r) >= 2:
                goal_factions[_int_or(r[0])] = str(r[1] or "")
    except Exception as exc:
        logger.debug("Rep goals query failed for guid=%s: %s", guid, exc)
    try:
        rep_rows = runner(
            f"SELECT faction, standing, flags FROM character_reputation "
            f"WHERE guid = {guid} AND ((flags & 1) = 1 OR standing != 0) "
            "ORDER BY standing DESC LIMIT 20;"
        )
        for r in rep_rows:
            if len(r) < 3:
                continue
            fid = _int_or(r[0])
            standing = _int_or(r[1])
            info = _rep_standing_info(standing)
            reputations.append({
                "faction_id": fid,
                "name": goal_factions.get(fid) or FACTION_NAMES.get(fid, f"Faction #{fid}"),
                "standing": standing,
                "is_goal": fid in goal_factions,
                **info,
            })
    except Exception as exc:
        logger.debug("Reputations query failed for guid=%s: %s", guid, exc)

    # Companion-only data: profile/bio, weights, overrides, AI brain, bonds, events, ledger, work orders
    weights_list: list[dict[str, Any]] = []
    ai_paused = False
    override_summary: dict[str, Any] = {"active": False, "source": "", "expires_at": 0, "rem_minutes": 0, "enabled": True}
    social_bonds: list[dict[str, Any]] = []
    recent_events: list[dict[str, Any]] = []
    recent_ledger: list[dict[str, Any]] = []
    work_orders: list[dict[str, Any]] = []
    ai_brain: dict[str, Any] | None = None
    companion_profile: dict[str, Any] = {}

    if is_comp:
        # DB companion_profiles + JSON profile
        db_prof = {"archetype": "", "culture": "", "flaw": "", "bio": ""}
        try:
            cp_rows = runner(f"SELECT archetype, culture, flaw, bio FROM companion_profiles WHERE guid = {guid} LIMIT 1;")
            if cp_rows and len(cp_rows[0]) >= 4:
                db_prof = {
                    "archetype": str(cp_rows[0][0] or ""),
                    "culture": str(cp_rows[0][1] or ""),
                    "flaw": str(cp_rows[0][2] or ""),
                    "bio": str(cp_rows[0][3] or ""),
                }
        except Exception as exc:
            logger.debug("Profile query failed for guid=%s: %s", guid, exc)
        companion_profile = {
            "archetype": db_prof["archetype"] or (char_prof or {}).get("archetype", ""),
            "culture": db_prof["culture"] or (char_prof or {}).get("culture", ""),
            "flaw": db_prof["flaw"],
            "bio": db_prof["bio"] or (char_prof or {}).get("notes", ""),
            "primary_spec": (char_prof or {}).get("primary_spec", ""),
            "primary_role": (char_prof or {}).get("primary_role", ""),
            "motivations": list((char_prof or {}).get("motivations") or []),
            "session_minutes": int((char_prof or {}).get("session_minutes") or 90),
        }

        # Weights & Overrides (exact C++ CompanionBrain::ReloadWeights merge semantics!)
        persona_base = persona_baseline_weights(char_prof)
        base_rows: dict[str, tuple[float, str]] = {}
        try:
            w_rows = runner(f"SELECT `key`, `value`, `source` FROM companion_weights WHERE guid = {guid};")
            for r in w_rows:
                if len(r) >= 3:
                    base_rows[str(r[0])] = (round(float(r[1] or 1.0), 3), str(r[2] or ""))
        except Exception as exc:
            logger.debug("Weights query failed for guid=%s: %s", guid, exc)

        ov_rows: dict[str, dict[str, Any]] = {}
        try:
            o_rows = runner(
                "SELECT `key`, `value`, `plan_id`, `revision`, `source`, `enabled`, `created_at`, `expires_at` "
                f"FROM companion_weight_overrides WHERE guid = {guid};"
            )
            any_disabled = False
            any_enabled = False
            max_exp = 0
            latest_src = ""
            for r in o_rows:
                if len(r) < 8:
                    continue
                k = str(r[0])
                en = _int_or(r[5], 1) == 1
                exp = _int_or(r[7], 0)
                if not en:
                    any_disabled = True
                else:
                    any_enabled = True
                if exp > max_exp:
                    max_exp = exp
                    latest_src = str(r[4] or "")
                ov_rows[k] = {
                    "value": round(float(r[1] or 1.0), 3),
                    "plan_id": _int_or(r[2]),
                    "revision": _int_or(r[3]),
                    "source": str(r[4] or ""),
                    "enabled": en,
                    "created_at": _int_or(r[6]),
                    "expires_at": exp,
                }
            ai_paused = bool(any_disabled and not any_enabled)
            if ov_rows:
                active_ov = bool(not ai_paused and max_exp > ts)
                override_summary = {
                    "active": active_ov,
                    "source": latest_src,
                    "expires_at": max_exp,
                    "rem_minutes": max(0, (max_exp - ts) // 60) if max_exp > ts else 0,
                    "enabled": not ai_paused,
                }
        except Exception as exc:
            logger.debug("Overrides query failed for guid=%s: %s", guid, exc)

        for k in CANONICAL_WEIGHT_KEYS:
            b_val, b_src = base_rows.get(k, (persona_base.get(k, 1.0), "persona"))
            pinned = (b_src == "manual")
            ov = ov_rows.get(k)
            ov_active = bool(ov and ov["enabled"] and ov["expires_at"] > ts)
            if pinned:
                eff_val = b_val
                eff_src = "manual"
            elif ov_active and ov is not None:
                eff_val = ov["value"]
                eff_src = ov["source"] or "override"
            else:
                eff_val = b_val
                eff_src = b_src or "persona"
            meta = WEIGHT_META[k]
            weights_list.append({
                "key": k,
                "label": meta["label"],
                "group": meta["group"],
                "baseline": round(float(b_val), 2),
                "baseline_source": b_src,
                "pinned": pinned,
                "override_value": round(float(ov["value"]), 2) if ov_active and ov else None,
                "override_source": ov["source"] if ov_active and ov else "",
                "effective": round(float(eff_val), 2),
                "effective_source": eff_src,
            })

        # Social bonds
        try:
            rel_rows = runner(
                "SELECT r.guid_a, ca.name, ca.class, r.guid_b, cb.name, cb.class, "
                "r.runs_together, r.help_given, r.affinity, r.last_event "
                "FROM companion_relationships r "
                "JOIN characters ca ON ca.guid = r.guid_a "
                "JOIN characters cb ON cb.guid = r.guid_b "
                f"WHERE r.guid_a = {guid} OR r.guid_b = {guid} "
                "ORDER BY r.affinity DESC, r.runs_together DESC LIMIT 12;"
            )
            for r in rel_rows:
                if len(r) < 10:
                    continue
                ga = _int_or(r[0])
                other_name = str(r[4] if ga == guid else r[1])
                other_cls = _int_or(r[5] if ga == guid else r[2])
                social_bonds.append({
                    "partner": other_name,
                    "partner_slug": other_name.lower(),
                    "partner_color": CLASSES.get(other_cls, {}).get("color", "#e8ecf2"),
                    "runs": _int_or(r[6]),
                    "helps": _int_or(r[7]),
                    "affinity": _int_or(r[8]),
                    "last_event": str(r[9] or ""),
                })
        except Exception as exc:
            logger.debug("Relationships query failed for guid=%s: %s", guid, exc)

        # Recent events
        try:
            ev_rows = runner(
                "SELECT event_type, summary, zone_id, created_at FROM companion_events "
                f"WHERE guid = {guid} ORDER BY id DESC LIMIT 15;"
            )
            for r in ev_rows:
                if len(r) < 4:
                    continue
                cts = _int_or(r[3])
                zid = _int_or(r[2])
                tstr = datetime.fromtimestamp(cts).strftime("%m-%d %H:%M") if cts > 0 else ""
                recent_events.append({
                    "type": str(r[0] or ""),
                    "summary": str(r[1] or ""),
                    "zone": ZONE_NAMES.get(zid, ""),
                    "created_at": cts,
                    "time_str": tstr,
                })
        except Exception as exc:
            logger.debug("Events query failed for guid=%s: %s", guid, exc)

        # Recent ledger
        try:
            ld_rows = runner(
                "SELECT l.time, l.action, l.item, COALESCE(it.name, ''), l.count, l.note "
                "FROM companion_ledger l "
                "LEFT JOIN acore_world.item_template it ON it.entry = l.item "
                f"WHERE l.guid = {guid} ORDER BY l.id DESC LIMIT 15;"
            )
            for r in ld_rows:
                if len(r) < 6:
                    continue
                recent_ledger.append({
                    "time": str(r[0] or ""),
                    "action": str(r[1] or ""),
                    "item_entry": _int_or(r[2]),
                    "item_name": str(r[3] or ""),
                    "count": _int_or(r[4]),
                    "note": str(r[5] or ""),
                })
        except Exception as exc:
            logger.debug("Ledger query failed for guid=%s: %s", guid, exc)

        # Work orders involving this character or currently open in guild
        try:
            wo_rows = runner(
                "SELECT id, requester_name, crafter_guid, crafter_name, item_entry, item_name, quantity, story, status, created_at "
                "FROM companion_work_orders "
                f"WHERE status IN ('open', 'claimed') OR crafter_guid = {guid} OR requester_guid = {guid} "
                "ORDER BY FIELD(status, 'claimed', 'open', 'delivered', 'expired'), id DESC LIMIT 15;"
            )
            for r in wo_rows:
                if len(r) < 10:
                    continue
                work_orders.append({
                    "id": _int_or(r[0]),
                    "requester": str(r[1] or ""),
                    "crafter_guid": _int_or(r[2]),
                    "crafter": str(r[3] or ""),
                    "item_entry": _int_or(r[4]),
                    "item_name": str(r[5] or f"Item #{r[4]}"),
                    "quantity": _int_or(r[6], 1),
                    "story": str(r[7] or ""),
                    "status": str(r[8] or "open"),
                    "assigned_here": _int_or(r[2]) == guid,
                })
        except Exception as exc:
            logger.debug("Work orders query failed for guid=%s: %s", guid, exc)

        # AI Brain & operator directive from brain.db (STRICTLY READ-ONLY SQLite connection; never runs DDL/UPDATE on GET!)
        if redact_admin:
            ai_brain = {
                "redacted": True,
                "paused": False,
                "override": {"active": False, "source": "", "expires_at": 0, "rem_minutes": 0, "enabled": True},
                "current_plan": None,
                "recent_plans": [],
                "operator_directive": None,
                "open_intentions": [],
                "fatigue_category": "",
                "fatigue_streak": 0,
                "stalled_goal_id": "",
            }
        else:
            ai_brain = {
                "redacted": False,
                "paused": ai_paused,
                "override": override_summary,
                "current_plan": None,
                "recent_plans": [],
                "operator_directive": None,
                "open_intentions": [],
                "fatigue_category": "",
                "fatigue_streak": 0,
                "stalled_goal_id": "",
            }
        if not redact_admin and db_path.is_file():
            ro_conn: sqlite3.Connection | None = None
            try:
                ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.5)
                ro_conn.row_factory = sqlite3.Row
                tables = {r[0] for r in ro_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                if "paused_characters" in tables:
                    if ro_conn.execute("SELECT 1 FROM paused_characters WHERE character_id = ?", (slug,)).fetchone():
                        ai_brain["paused"] = True
                if "operator_directives" in tables:
                    drow = ro_conn.execute(
                        "SELECT character_id, note, updated_at, expires_at, consumed_at "
                        "FROM operator_directives WHERE character_id = ? AND expires_at > ?",
                        (slug, ts),
                    ).fetchone()
                    if drow:
                        ai_brain["operator_directive"] = {
                            "character_id": str(drow["character_id"]),
                            "note": str(drow["note"]),
                            "active": int(drow["consumed_at"] or 0) == 0,
                            "updated_at": int(drow["updated_at"]),
                            "expires_at": int(drow["expires_at"]),
                            "consumed_at": int(drow["consumed_at"] or 0),
                        }
                if "plans" in tables:
                    p_rows = ro_conn.execute(
                        """
                        SELECT id, planned_at, planned_iso, expires_at, source, model, effort,
                               primary_goal_id, primary_desc, secondary_goal_ids, reason_code,
                               session_minutes, applied, plan_state, proposal_status, proposal_reason,
                               advisory_only, outcome_levels, outcome_xp, outcome_skillups,
                               outcome_orders, outcome_quests, error_note
                        FROM plans
                        WHERE character_id = ? AND applied = 1 AND plan_state = 'applied'
                        ORDER BY id DESC LIMIT 8
                        """,
                        (slug,),
                    ).fetchall()
                    for idx, pr in enumerate(p_rows):
                        item = {
                            "plan_id": int(pr["id"]),
                            "planned_at": int(pr["planned_at"]),
                            "planned_iso": str(pr["planned_iso"]),
                            "expires_at": int(pr["expires_at"]),
                            "active": int(pr["expires_at"]) > ts,
                            "rem_minutes": max(0, (int(pr["expires_at"]) - ts) // 60),
                            "source": str(pr["source"]),
                            "model": str(pr["model"]),
                            "primary_goal_id": str(pr["primary_goal_id"]),
                            "primary_desc": str(pr["primary_desc"]),
                            "secondary_goal_ids": json.loads(pr["secondary_goal_ids"] or "[]"),
                            "reason_code": str(pr["reason_code"]),
                            "session_minutes": int(pr["session_minutes"]),
                            "proposal_status": str(pr["proposal_status"] or ""),
                            "proposal_reason": str(pr["proposal_reason"] or ""),
                            "advisory_only": bool(pr["advisory_only"]),
                            "outcome_levels": int(pr["outcome_levels"] or 0),
                            "outcome_xp": int(pr["outcome_xp"] or 0),
                            "outcome_skillups": int(pr["outcome_skillups"] or 0),
                            "outcome_orders": int(pr["outcome_orders"] or 0),
                            "outcome_quests": int(pr["outcome_quests"] or 0),
                            "error_note": str(pr["error_note"] or ""),
                        }
                        if idx == 0:
                            ai_brain["current_plan"] = item
                        ai_brain["recent_plans"].append(item)
                if "intentions" in tables:
                    i_rows = ro_conn.execute(
                        "SELECT label FROM intentions WHERE character_id = ? AND status = 'open' ORDER BY updated_at DESC LIMIT 5",
                        (slug,),
                    ).fetchall()
                    ai_brain["open_intentions"] = [str(r["label"]) for r in i_rows if r["label"]]
            except Exception as exc:
                logger.debug("Read-only Brain DB read failed for %s: %s", slug, exc)
            finally:
                if ro_conn is not None:
                    ro_conn.close()

    # Issue #71 / PR #110 QA: Unauthenticated LAN viewers get redacted public view for human characters!
    if not is_comp and redact_admin:
        return {
            "ok": True,
            "redacted": True,
            "generated_at": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "can_control": False,
            "character": {
                "guid": guid,
                "name": char["name"],
                "slug": slug,
                "is_companion": False,
                "race_id": char["race_id"],
                "race": race_name,
                "class_id": char["class_id"],
                "class_name": cls_meta["name"],
                "class_short": cls_meta["short"],
                "class_color": cls_meta["color"],
                "power_type": cls_meta["power"],
                "gender": char["gender"],
                "level": char["level"],
                "xp": char["xp"],
                "xp_max": xp_max,
                "xp_pct": xp_pct,
                "money": None,
                "zone_id": char["zone_id"],
                "zone": ZONE_NAMES.get(char["zone_id"], f"Zone #{char['zone_id']}"),
                "map_id": char["map_id"],
                "homebind": None,
                "online": char["online"],
                "dead": dead,
                "played_hours": None,
                "avg_ilvl": None,
                "schedule": None,
                "profile": None,
            },
            "gear": [],
            "stats": {},
            "professions": [],
            "quests": {"active": [], "completed_count": 0},
            "reputations": [],
            "weights": [],
            "ai_brain": None,
            "social_bonds": [],
            "recent_events": [],
            "recent_ledger": [],
            "work_orders": [],
            "controls_meta": {"presets": [], "shift_groups": [], "work_order_catalog": []},
        }

    return {
        "ok": True,
        "generated_at": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "can_control": bool(can_control and is_comp),
        "character": {
            "guid": guid,
            "name": char["name"],
            "slug": slug,
            "is_companion": is_comp,
            "race_id": char["race_id"],
            "race": race_name,
            "class_id": char["class_id"],
            "class_name": cls_meta["name"],
            "class_short": cls_meta["short"],
            "class_color": cls_meta["color"],
            "power_type": cls_meta["power"],
            "gender": char["gender"],
            "level": char["level"],
            "xp": char["xp"],
            "xp_max": xp_max,
            "xp_pct": xp_pct,
            "money": _money_parts(char["money"]),
            "zone_id": char["zone_id"],
            "zone": ZONE_NAMES.get(char["zone_id"], f"Zone #{char['zone_id']}"),
            "map_id": char["map_id"],
            "homebind": homebind,
            "online": char["online"],
            "dead": dead,
            "played_hours": round(char["totaltime"] / 3600.0, 1),
            "avg_ilvl": avg_ilvl,
            "schedule": char["schedule"] if is_comp else None,
            "profile": companion_profile if is_comp else None,
        },
        "gear": gear_slots,
        "stats": stats,
        "professions": professions,
        "quests": {
            "active": active_quests,
            "completed_count": quests_completed_count,
        },
        "reputations": reputations,
        "weights": weights_list,
        "ai_brain": ai_brain,
        "social_bonds": social_bonds,
        "recent_events": recent_events,
        "recent_ledger": recent_ledger,
        "work_orders": work_orders,
        "controls_meta": {
            "presets": [
                {"id": p["id"], "label": p["label"], "icon": p["icon"], "summary": p["summary"]}
                for p in FOCUS_PRESETS.values()
            ],
            "shift_groups": [
                {"id": sid, "label": info["label"], "start_hour": info["start_hour"], "end_hour": info["end_hour"]}
                for sid, info in VALID_SHIFT_GROUPS.items()
            ],
            "work_order_catalog": _work_order_catalog_for_skills(char_skill_ids) if is_comp else [],
        },
    }


def _require_companion(slug: str, sql_exec) -> dict[str, Any]:
    char = _resolve_character_row(slug, sql_exec)
    if char is None:
        raise Invalid(f"Character not found: {slug}")
    if not char["is_companion"]:
        raise Invalid(f"{char['name']} is a human character, not an AI companion")
    return char


# ---------------------------------------------------------------------------
# Control 1: One-Click Focus Presets -> expiring companion_weight_overrides
# ---------------------------------------------------------------------------
def apply_focus_preset(
    slug: str,
    preset_id: str,
    hours: int = 2,
    *,
    sql_exec=None,
    db_path: Path = BRAIN_DB_PATH,
    runtime_dir: Path = RUNTIME_DIR,
    now_ts: int | None = None,
) -> dict[str, Any]:
    if is_brain_disabled(runtime_dir):
        raise Invalid("Global AI Brain kill switch (brain.disabled) is active")
    runner = sql_exec if sql_exec is not None else run_operator_sql
    ts = int(now_ts if now_ts is not None else time.time())
    pid = str(preset_id or "").strip().lower()
    preset = FOCUS_PRESETS.get(pid)
    if not preset:
        raise Invalid(f"Unknown focus preset: {preset_id!r}")
    dur_hours = max(1, min(6, int(hours)))
    char = _require_companion(slug, runner)
    guid = char["guid"]
    if db_path.is_file() and BrainStore(db_path).is_character_paused(char["slug"]):
        raise Invalid(f"AI is paused for {char['name']}; resume AI before applying a focus preset")
    char_prof = _safe_load_profile(char["slug"])
    base = persona_baseline_weights(char_prof)
    merged = dict(base)
    for k, v in preset["weights"].items():
        if k in CANONICAL_WEIGHT_KEYS:
            merged[k] = _clamp(v)

    # Use the SAME monotonic revision counter as BrainStore (remote_max + 1, NEVER epoch ts!)
    # so presets never trigger false brain_sequence_resync alerts!
    remote_max = fetch_remote_max_revision(sql_exec=runner)
    revision = remote_max + 1
    expires_at = ts + dur_hours * 3600

    stmts = ["START TRANSACTION;"]
    for k in CANONICAL_WEIGHT_KEYS:
        v = _clamp(merged.get(k, 1.0))
        stmts.append(
            "INSERT INTO companion_weight_overrides "
            "(`guid`, `key`, `value`, `plan_id`, `revision`, `source`, `enabled`, `created_at`, `expires_at`) "
            f"VALUES ({guid}, '{_sql_esc(k)}', {v:.3f}, 0, {revision}, 'operator', 1, {ts}, {expires_at}) "
            "ON DUPLICATE KEY UPDATE "
            "`value` = IF(VALUES(`revision`) >= `revision`, VALUES(`value`), `value`), "
            "`source` = IF(VALUES(`revision`) >= `revision`, 'operator', `source`), "
            "`created_at` = IF(VALUES(`revision`) >= `revision`, VALUES(`created_at`), `created_at`), "
            "`expires_at` = IF(VALUES(`revision`) >= `revision`, VALUES(`expires_at`), `expires_at`), "
            "`revision` = GREATEST(`revision`, VALUES(`revision`));"
        )
    stmts.append("COMMIT;")
    runner("\n".join(stmts) + "\n")
    return {
        "ok": True,
        "character": char["name"],
        "guid": guid,
        "preset": preset["id"],
        "label": preset["label"],
        "hours": dur_hours,
        "revision": revision,
        "expires_at": expires_at,
    }


# ---------------------------------------------------------------------------
# Control 2: Manual Weight Sliders + Pin/Unpin (companion_weights source='manual')
# ---------------------------------------------------------------------------
def set_manual_weight(
    slug: str,
    key: str,
    value: float,
    pinned: bool = True,
    *,
    sql_exec=None,
) -> dict[str, Any]:
    runner = sql_exec if sql_exec is not None else run_operator_sql
    wkey = str(key or "").strip()
    if wkey not in CANONICAL_WEIGHT_KEYS:
        raise Invalid(f"Unsupported weight key: {key!r}")
    val = _clamp(float(value), 0.5, 1.5)
    char = _require_companion(slug, runner)
    guid = char["guid"]

    if pinned:
        sql = (
            "INSERT INTO companion_weights (`guid`, `key`, `value`, `source`) "
            f"VALUES ({guid}, '{_sql_esc(wkey)}', {val:.3f}, 'manual') "
            "ON DUPLICATE KEY UPDATE `value` = VALUES(`value`), `source` = 'manual';\n"
        )
        src = "manual"
    else:
        # Restore baseline from persona profile and clear 'manual' lock so AI & presets take effect again
        char_prof = _safe_load_profile(char["slug"])
        base_val = persona_baseline_weights(char_prof).get(wkey, 1.0)
        sql = (
            "INSERT INTO companion_weights (`guid`, `key`, `value`, `source`) "
            f"VALUES ({guid}, '{_sql_esc(wkey)}', {base_val:.3f}, 'hearthcore') "
            "ON DUPLICATE KEY UPDATE `value` = VALUES(`value`), `source` = 'hearthcore';\n"
        )
        val = round(float(base_val), 3)
        src = "hearthcore"

    runner(sql)
    return {
        "ok": True,
        "character": char["name"],
        "guid": guid,
        "key": wkey,
        "value": val,
        "pinned": bool(pinned),
        "source": src,
    }


# ---------------------------------------------------------------------------
# Control 3: Free-Text Directive to Claude ("Next Plan Note")
# ---------------------------------------------------------------------------
def set_character_directive(
    slug: str,
    note: str,
    *,
    sql_exec=None,
    db_path: Path = BRAIN_DB_PATH,
    ttl_hours: int = 24,
    now_ts: int | None = None,
) -> dict[str, Any]:
    runner = sql_exec if sql_exec is not None else run_read_sql
    char = _require_companion(slug, runner)
    store = BrainStore(db_path)
    res = store.set_operator_directive(char["slug"], note, now_ts=now_ts, ttl_hours=ttl_hours)
    return {"ok": True, "character": char["name"], "guid": char["guid"], "directive": res}


# ---------------------------------------------------------------------------
# Control 4: Pause / Resume AI for Just This Character
# ---------------------------------------------------------------------------
def set_character_ai_paused(
    slug: str,
    paused: bool,
    *,
    sql_exec=None,
    db_path: Path = BRAIN_DB_PATH,
    runtime_dir: Path = RUNTIME_DIR,
    now_ts: int | None = None,
) -> dict[str, Any]:
    if not paused and is_brain_disabled(runtime_dir):
        raise Invalid("Global AI Brain kill switch (brain.disabled) is active; cannot resume per-character overrides")
    runner = sql_exec if sql_exec is not None else run_operator_sql
    ts = int(now_ts if now_ts is not None else time.time())
    char = _require_companion(slug, runner)
    guid = char["guid"]
    flag = 0 if paused else 1

    # Persist authoritative pause state in brain.db so run_cycle skips Claude calls for paused bots!
    BrainStore(db_path).set_character_paused(char["slug"], bool(paused), now_ts=ts)

    existing = runner(f"SELECT COUNT(*) FROM companion_weight_overrides WHERE guid = {guid};")
    has_rows = bool(existing and existing[0] and _int_or(existing[0][0], 0) > 0)
    if has_rows:
        runner(f"UPDATE companion_weight_overrides SET enabled = {flag} WHERE guid = {guid};\n")
    elif paused:
        # Persist paused state in MySQL even when no overrides existed yet (using monotonic remote_max + 1)
        char_prof = _safe_load_profile(char["slug"])
        base = persona_baseline_weights(char_prof)
        remote_max = fetch_remote_max_revision(sql_exec=runner)
        revision = remote_max + 1
        stmts = ["START TRANSACTION;"]
        for k in CANONICAL_WEIGHT_KEYS:
            v = _clamp(base.get(k, 1.0))
            stmts.append(
                "INSERT INTO companion_weight_overrides "
                "(`guid`, `key`, `value`, `plan_id`, `revision`, `source`, `enabled`, `created_at`, `expires_at`) "
                f"VALUES ({guid}, '{_sql_esc(k)}', {v:.3f}, 0, {revision}, 'operator', 0, {ts}, {ts + 86400}) "
                "ON DUPLICATE KEY UPDATE `enabled` = 0;"
            )
        stmts.append("COMMIT;")
        runner("\n".join(stmts) + "\n")

    return {"ok": True, "character": char["name"], "guid": guid, "paused": bool(paused), "enabled": not bool(paused)}


# ---------------------------------------------------------------------------
# Control 5a: Quick Guild Work Order Assignment
# ---------------------------------------------------------------------------
def create_character_work_order(
    slug: str,
    item_entry: int,
    quantity: int = 5,
    story: str = "",
    *,
    assign_to_character: bool = True,
    sql_exec=None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    runner = sql_exec if sql_exec is not None else run_operator_sql
    ts = int(now_ts if now_ts is not None else time.time())
    char = _require_companion(slug, runner)
    entry = int(item_entry)
    if entry <= 0 or entry > 200000:
        raise Invalid("Invalid item_entry")
    qty = max(1, min(40, int(quantity)))

    # Resolve authoritative item name from item_template (or catalog fallback)
    item_name = ""
    tpl = ORDER_BY_ENTRY.get(entry)
    if tpl:
        item_name = tpl.item_name
    try:
        it_rows = runner(f"SELECT name FROM acore_world.item_template WHERE entry = {entry} LIMIT 1;")
        if it_rows and it_rows[0] and it_rows[0][0]:
            item_name = str(it_rows[0][0])
    except Exception as exc:
        logger.debug("Item template lookup failed for entry=%s: %s", entry, exc)
    if not item_name:
        raise Invalid(f"Unknown item_entry: {entry}")

    clean_story = re.sub(r"[\x00-\x1f\x7f]+", " ", str(story or "")).strip()[:180]
    if not clean_story:
        clean_story = f"Requested via Azeroth Watch for {char['name']}"

    crafter_guid = char["guid"] if assign_to_character else 0
    crafter_name = char["name"] if assign_to_character else ""
    status = "claimed" if assign_to_character else "open"

    runner(
        "INSERT INTO companion_work_orders "
        "(`requester_guid`, `requester_name`, `crafter_guid`, `crafter_name`, "
        "`item_entry`, `item_name`, `quantity`, `story`, `status`, `created_at`, `updated_at`) "
        f"VALUES (0, 'Operator', {crafter_guid}, '{_sql_esc(crafter_name)}', "
        f"{entry}, '{_sql_esc(item_name[:80])}', {qty}, '{_sql_esc(clean_story)}', '{status}', {ts}, {ts});\n"
    )
    return {
        "ok": True,
        "character": char["name"],
        "item_entry": entry,
        "item_name": item_name,
        "quantity": qty,
        "status": status,
        "crafter_name": crafter_name,
    }


# ---------------------------------------------------------------------------
# Control 5b: Shift Schedule & Always-Online Toggle
# ---------------------------------------------------------------------------
def update_character_schedule(
    slug: str,
    *,
    shift_group: str | None = None,
    always_online: bool | None = None,
    sql_exec=None,
) -> dict[str, Any]:
    runner = sql_exec if sql_exec is not None else run_operator_sql
    char = _require_companion(slug, runner)

    updates: list[str] = []
    new_shift = char["schedule"]["shift_group"]
    new_always = char["schedule"]["always_online"]

    if shift_group is not None:
        sg = str(shift_group).strip().lower()
        info = VALID_SHIFT_GROUPS.get(sg)
        if not info:
            raise Invalid(f"Invalid shift_group: {shift_group!r}")
        new_shift = sg
        updates.append(f"`shift_group` = '{_sql_esc(sg)}'")
        updates.append(f"`start_hour` = {int(info['start_hour'])}")
        updates.append(f"`end_hour` = {int(info['end_hour'])}")
        updates.append(f"`days_mask` = {int(info['days_mask'])}")
        if always_online is None:
            new_always = bool(info["default_always"])
            updates.append(f"`always_online` = {int(info['default_always'])}")

    if always_online is not None:
        new_always = bool(always_online)
        updates.append(f"`always_online` = {1 if new_always else 0}")

    if not updates:
        raise Invalid("Specify shift_group and/or always_online")

    runner(
        f"UPDATE companion_schedule SET {', '.join(updates)} "
        f"WHERE LOWER(`name`) = '{_sql_esc(char['slug'])}';\n"
    )
    return {
        "ok": True,
        "character": char["name"],
        "shift_group": new_shift,
        "always_online": new_always,
    }
