"""Trusted live game-state bridge: exporter, session-boundary AI planner, and safe DB importer.

Safety invariants (see docs/ARCHITECTURE.md):
- Game server & DB remain authoritative for level, gear, items, quests, GUIDs, and actions.
- Plans are generated strictly out-of-loop at session boundaries (never per tick).
- Every plan (AI or fallback) passes core.validate() allowlist checks before touching DB.
- Only bounded 0.5-1.5 numeric preference weights are written to companion_weights.
- Zero LLM text is ever sent to in-game chat.
- API failures or daily budget caps fall back cleanly to deterministic scoring.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import Invalid, ROOT, fallback, profile, profiles, snapshot, validate
from .guides import LEVELING_ORDER_CATALOG, SKILL_NAMES
from .provider import anthropic_with_meta, load_env

DB_CONTAINER = "ac-database"
LIVE_POS_FILE = Path("/home/wonky/AzerothCore/azerothcore-wotlk/env/dist/logs/live_positions.json")
RUNTIME_DIR = ROOT / "runtime"
BRAIN_DB_PATH = RUNTIME_DIR / "brain.db"
ACTIVE_PLANS_PATH = RUNTIME_DIR / "active_plans.json"
QUARTERMASTER_PATH = RUNTIME_DIR / "quartermaster.json"

CRAFT_SKILLS = {164, 165, 171, 197, 202, 333, 755, 773}
GATHER_SKILLS = {182, 186, 393}

# All guild work-order items (Guild Life v2 + Claude Quartermaster Priority 1 & Priority 2 catalog)
WORK_ORDER_SKILL = {t.item_entry: t.skill_id for t in LEVELING_ORDER_CATALOG}
WORK_ORDER_SKILL.update({3470: 164, 3486: 164, 3576: 186})

ZONE_NAMES = {
    1: "Dun Morogh", 10: "Duskwood", 11: "Wetlands", 12: "Elwynn Forest",
    33: "Stranglethorn Vale", 38: "Loch Modan", 40: "Westfall", 44: "Redridge Mountains",
    45: "Arathi Highlands", 141: "Teldrassil", 148: "Darkshore", 331: "Ashenvale",
    1519: "Stormwind City", 1537: "Ironforge", 1657: "Darnassus",
    3524: "Azuremyst Isle", 3525: "Bloodmyst Isle", 3557: "The Exodar",
}


def _clamp(val: float, lo: float = 0.5, hi: float = 1.5) -> float:
    return round(max(lo, min(hi, float(val))), 3)


def _slug_token(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")[:24] or "item"


def run_sql(sql: str) -> list[list[str]]:
    res = subprocess.run(
        [
            "docker", "exec", "-i", DB_CONTAINER, "sh", "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -u root -N -B acore_characters',
        ],
        input=sql,
        text=True,
        capture_output=True,
        check=True,
    )
    rows: list[list[str]] = []
    for line in res.stdout.splitlines():
        if line != "":
            rows.append(line.split("\t"))
    return rows


def read_live_context() -> dict[str, Any]:
    """Read fresh weekly_event and live zone overrides from live_positions.json if present."""
    ctx: dict[str, Any] = {"weekly_event": "", "live_by_guid": {}}
    try:
        st = LIVE_POS_FILE.stat()
        if time.time() - st.st_mtime < 30.0:
            raw = json.loads(LIVE_POS_FILE.read_text(encoding="utf-8"))
            ctx["weekly_event"] = str(raw.get("weekly_event") or "")
            for u in raw.get("units", []):
                ctx["live_by_guid"][int(u["g"])] = u
    except Exception:
        pass
    return ctx


def fetch_world_context() -> dict[str, Any]:
    """Query live MySQL for online Hearthguard companions, skills, open work orders, and recent events."""
    live = read_live_context()

    comp_rows = run_sql("""
        SELECT c.guid, c.name, c.level, c.zone, c.map, c.money, c.online,
               COALESCE(cs.shift_group, '')
        FROM companion_schedule cs
        JOIN characters c ON c.name = cs.name
        ORDER BY c.online DESC, c.name;
    """)
    companions: dict[str, dict[str, Any]] = {}
    guid_to_slug: dict[int, str] = {}
    for r in comp_rows:
        if len(r) < 8:
            continue
        guid = int(r[0])
        name = r[1]
        slug = name.lower()
        lp = live["live_by_guid"].get(guid)
        zid = int(lp["z"]) if lp else int(r[3])
        mid = int(lp["m"]) if lp else int(r[4])
        companions[slug] = {
            "guid": guid,
            "name": name,
            "slug": slug,
            "level": int(r[2]),
            "zone_id": zid,
            "zone": ZONE_NAMES.get(zid, f"Zone {zid}"),
            "map_id": mid,
            "gold": round(int(r[5]) / 10000.0, 2),
            "online": (int(r[6]) == 1) or (guid in live["live_by_guid"]),
            "shift_group": r[7],
            "skills": {},
            "recent_events": [],
        }
        guid_to_slug[guid] = slug

    # Skills for online companions
    online_guids = [str(c["guid"]) for c in companions.values() if c["online"]]
    if online_guids:
        glist = ",".join(online_guids)
        for r in run_sql(f"SELECT guid, skill, value, max FROM character_skills WHERE guid IN ({glist}) AND skill IN (164,165,171,182,186,197,202,333,393,755,773,129,185,356);"):
            if len(r) >= 4:
                sl = guid_to_slug.get(int(r[0]))
                if sl:
                    companions[sl]["skills"][int(r[1])] = (int(r[2]), int(r[3]))

        # Recent 4 events per online companion
        for r in run_sql(f"SELECT guid, event_type, summary FROM companion_events WHERE guid IN ({glist}) ORDER BY id DESC LIMIT 120;"):
            if len(r) >= 3:
                sl = guid_to_slug.get(int(r[0]))
                if sl and len(companions[sl]["recent_events"]) < 4:
                    companions[sl]["recent_events"].append(f"{r[1]}: {r[2][:70]}")

    # Open guild work orders
    work_orders: list[dict[str, Any]] = []
    for r in run_sql("SELECT id, requester_guid, requester_name, item_entry, item_name, quantity, story FROM companion_work_orders WHERE status IN ('open','claimed') ORDER BY id DESC LIMIT 16;"):
        if len(r) >= 7:
            item_entry = int(r[3])
            work_orders.append({
                "id": int(r[0]),
                "requester_guid": int(r[1]),
                "requester_name": r[2],
                "item_entry": item_entry,
                "item_name": r[4],
                "quantity": int(r[5]),
                "story": r[6],
                "skill": WORK_ORDER_SKILL.get(item_entry, 0),
            })

    # Online humans
    human_rows = run_sql("""
        SELECT c.name, c.level, c.zone
        FROM characters c
        JOIN acore_auth.account a ON a.id = c.account
        WHERE c.online = 1
          AND a.username NOT LIKE 'RNDBOT%'
          AND c.name NOT IN (SELECT name FROM companion_schedule);
    """)
    humans = [
        {"name": r[0], "level": int(r[1]), "zone_id": int(r[2]), "zone": ZONE_NAMES.get(int(r[2]), f"Zone {r[2]}")}
        for r in human_rows if len(r) >= 3
    ]

    # Optional Quartermaster guild-wide directives (so individual companions align with guild deficits!)
    qm_focus: set[str] = set()
    qm_mats_by_skill: dict[str, list[str]] = {}
    try:
        if QUARTERMASTER_PATH.is_file():
            qm = json.loads(QUARTERMASTER_PATH.read_text(encoding="utf-8"))
            qm_focus = {str(s).lower() for s in (qm.get("focus_skills") or [])}
            for br in qm.get("bank_requests") or []:
                gsk = str(br.get("gather_skill") or "").lower()
                if gsk:
                    qm_mats_by_skill.setdefault(gsk, []).append(str(br.get("name") or ""))
    except Exception:
        pass

    return {
        "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "weekly_event": live["weekly_event"],
        "companions": companions,
        "work_orders": work_orders,
        "humans": humans,
        "qm_focus": qm_focus,
        "qm_mats_by_skill": qm_mats_by_skill,
    }


def build_snapshot(comp: dict[str, Any], world_ctx: dict[str, Any]) -> dict[str, Any]:
    """Generate a strictly validated candidate snapshot for one companion from authoritative DB state."""
    lvl = comp["level"]
    zone = comp["zone"]
    skills = comp["skills"]
    weekly = world_ctx.get("weekly_event", "")
    is_craft_night = "Craft" in weekly
    is_fish_night = "Fishing" in weekly
    is_sunday = "Sunday" in weekly

    candidates: list[dict[str, Any]] = []

    # 1. Questing in current/bracket zone (always eligible)
    q_score = 78 if lvl < 20 else 72
    if comp["shift_group"] in ("core-wonky", "core-logan"):
        q_score = min(92, q_score + 8)
    candidates.append({
        "id": f"quest:zone_{comp['zone_id']}",
        "type": "quest",
        "score": q_score,
        "description": f"Complete quests and turn-ins around {zone} (Lv {lvl})",
    })

    # 2. Eligible open guild work orders matching a skill this companion actually holds
    added_wo = 0
    for wo in world_ctx.get("work_orders", []):
        if wo["requester_guid"] == comp["guid"]:
            continue
        sk = wo["skill"]
        if sk and sk in skills and added_wo < 2:
            score = 88 if is_craft_night else 80
            candidates.append({
                "id": f"guild_order:{wo['id']}:{_slug_token(wo['item_name'])}",
                "type": "guild_order",
                "score": score,
                "description": f"Craft {wo['quantity']}x {wo['item_name']} for {wo['requester_name']} ({SKILL_NAMES.get(sk, 'craft')})",
            })
            added_wo += 1

    # 3. Primary crafting & gathering professions (aligned with Claude Quartermaster guild deficits!)
    qm_focus = world_ctx.get("qm_focus") or set()
    qm_mats = world_ctx.get("qm_mats_by_skill") or {}
    for sk_id, (val, mx) in sorted(skills.items()):
        if sk_id in CRAFT_SKILLS:
            sk_name = SKILL_NAMES[sk_id]
            gap = max(0, min(lvl * 5, mx) - val)
            qm_boost = 10 if sk_name.lower() in qm_focus else 0
            score = min(92, 52 + gap // 2 + (14 if is_craft_night else 0) + qm_boost)
            candidates.append({
                "id": f"craft:{_slug_token(sk_name)}",
                "type": "craft",
                "score": max(45, score),
                "description": f"Advance {sk_name} ({val}/{mx}) and supply Hearthguard"
                               + (" [Guild Priority]" if qm_boost else ""),
            })
        elif sk_id in GATHER_SKILLS:
            sk_name = SKILL_NAMES[sk_id]
            gap = max(0, min(lvl * 5, mx) - val)
            needed_names = qm_mats.get(sk_name.lower(), [])[:3]
            qm_boost = 12 if (sk_name.lower() in qm_focus or needed_names) else 0
            score = min(92, 54 + gap // 2 + (8 if is_craft_night else 0) + qm_boost)
            need_txt = f" — guild needs {', '.join(needed_names)}" if needed_names else ""
            candidates.append({
                "id": f"gather:{_slug_token(sk_name)}",
                "type": "gather",
                "score": max(45, score),
                "description": f"Gather {sk_name} ({val}/{mx}) for 40-char guild vault{need_txt}"[:240],
            })

    # 4. Secondary Cooking / First Aid / Fishing
    if 356 in skills:
        f_val, f_mx = skills[356]
        fish_need = qm_mats.get("fishing", [])[:2]
        f_boost = 12 if ("fishing" in qm_focus or fish_need) else 0
        f_score = 88 if is_fish_night else min(86, (58 if f_val < lvl * 4 else 48) + f_boost)
        fish_txt = f" ({', '.join(fish_need)} for Mp5 buffs)" if fish_need else ""
        candidates.append({
            "id": "fish:hearthguard",
            "type": "fish",
            "score": f_score,
            "description": (f"Fish for guild cooking supplies (Fishing {f_val}/{f_mx}){fish_txt}"
                            + (" [Fishing Night!]" if is_fish_night else ""))[:240],
        })

    if 185 in skills or 129 in skills:
        parts = []
        if 185 in skills:
            parts.append(f"Cooking {skills[185][0]}")
        if 129 in skills:
            parts.append(f"First Aid {skills[129][0]}")
        candidates.append({
            "id": "profession:secondaries",
            "type": "profession",
            "score": 56 + (10 if is_craft_night else 0),
            "description": f"Keep secondary skills stocked ({', '.join(parts)})",
        })

    # 5. Dungeon readiness (only eligible at level >= 15)
    if lvl >= 15:
        d_score = 84 if is_sunday else 66
        candidates.append({
            "id": "dungeon:prep",
            "type": "dungeon",
            "score": d_score,
            "description": f"Prepare gear/consumables and stay ready for Lv {lvl} dungeons"
                           + (" [Sunday Dungeon Day!]" if is_sunday else ""),
        })

    # 6. Training, Banking, Vendor/Repair, Exploration/Neighbourhood
    t_score = 70 if (lvl % 2 == 0 and lvl >= 4) else 52
    candidates.append({
        "id": "train:skills",
        "type": "train",
        "score": t_score,
        "description": f"Visit class or profession trainer at level {lvl}",
    })
    candidates.append({
        "id": "bank:vault",
        "type": "bank",
        "score": 54,
        "description": "Deposit spare trade goods and herbs into Hearthguard guild bank",
    })
    candidates.append({
        "id": "repair:vendor",
        "type": "repair",
        "score": 55,
        "description": "Sell gray clutter, restock essentials, and repair gear",
    })

    # Neighbourhood presence when an owner is nearby in level
    near_owner = next(
        (h for h in world_ctx.get("humans", []) if abs(h["level"] - lvl) <= 6),
        None,
    )
    if near_owner:
        candidates.append({
            "id": f"explore:near_{_slug_token(near_owner['name'])}",
            "type": "explore",
            "score": 76 if not is_sunday else 84,
            "description": f"Adventure in {near_owner['zone']} alongside {near_owner['name']} (Lv {near_owner['level']})",
        })
    else:
        candidates.append({
            "id": "explore:frontier",
            "type": "explore",
            "score": 52,
            "description": f"Explore neighboring hubs and questing roads near {zone}",
        })

    snap = {
        "character_id": comp["slug"],
        "observed_at": world_ctx["observed_at"],
        "candidates": candidates,
    }
    return snapshot(snap, comp["slug"])


def weights_for_cpp(plan: dict[str, Any], snap: dict[str, Any]) -> dict[str, float]:
    """Translate validated Hearthcore plan into bounded 0.5-1.5 keys read by CompanionBrain.cpp."""
    mods = plan["weight_modifiers"]
    cand_type = {c["id"]: c["type"] for c in snap["candidates"]}
    primary_type = cand_type.get(plan["primary_goal_id"], "")

    def eff(t: str, default: float = 1.0) -> float:
        base = float(mods.get(t, default))
        if primary_type == t:
            base += 0.15
        return _clamp(base)

    q = eff("quest")
    if primary_type in ("explore", "dungeon"):
        q = _clamp(max(q, eff(primary_type) * 0.95))

    craft_val = max(
        eff("craft"),
        eff("profession") * 0.98 if "profession" in mods or primary_type == "profession" else 0.5,
        eff("guild_order") if "guild_order" in mods or primary_type == "guild_order" else 0.5,
    )
    craft_val = _clamp(craft_val)

    out = {
        "quest_work": q,
        "quest_turnin": _clamp(max(q, 1.0)),
        "quest_hub": q,
        "quest_grind": _clamp(min(q, 1.15)),  # prefer real quests over pure mob grinding
        "craft": craft_val,
        "gather": eff("gather"),
        "fish": eff("fish"),
        "train": eff("train"),
        "bank": eff("bank"),
        "repair": eff("repair"),
        "sell": _clamp((eff("repair") + eff("bank")) / 2.0),
        "travel": eff("travel"),
        "hearth": eff("rest"),
    }
    return out


def apply_plan_to_db(guid: int, plan: dict[str, Any], snap: dict[str, Any], source: str) -> dict[str, float]:
    """Re-validate plan and upsert bounded weights into acore_characters.companion_weights."""
    validate(plan, snap)
    cpp_weights = weights_for_cpp(plan, snap)
    db_src = "hearthcore_ai" if source == "anthropic" else "hearthcore_fb"
    stmts = []
    for k, v in sorted(cpp_weights.items()):
        stmts.append(
            f"INSERT INTO companion_weights (`guid`, `key`, `value`, `source`) "
            f"VALUES ({int(guid)}, '{k}', {v:.3f}, '{db_src}') "
            f"ON DUPLICATE KEY UPDATE `value` = VALUES(`value`), `source` = VALUES(`source`);"
        )
    run_sql("\n".join(stmts) + "\n")
    return cpp_weights


class BrainStore:
    """SQLite audit, session-expiry tracker, and daily API budget guard."""

    def __init__(self, path: Path = BRAIN_DB_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY,
                    planned_at INTEGER NOT NULL,
                    planned_iso TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    character_id TEXT NOT NULL,
                    game_guid INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    model TEXT NOT NULL,
                    effort TEXT NOT NULL,
                    primary_goal_id TEXT NOT NULL,
                    primary_desc TEXT NOT NULL,
                    secondary_goal_ids TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    session_minutes INTEGER NOT NULL,
                    weight_modifiers TEXT NOT NULL,
                    cpp_weights TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    thinking_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    applied INTEGER NOT NULL DEFAULT 0,
                    error_note TEXT NOT NULL DEFAULT '',
                    observed_at TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_plans_char_exp ON plans(character_id, expires_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_plans_day ON plans(day_key, source)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def active_plan(self, slug: str, now_ts: int) -> dict[str, Any] | None:
        with self._conn() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM plans WHERE character_id = ? AND expires_at > ? ORDER BY id DESC LIMIT 1",
                (slug, now_ts),
            ).fetchone()
            return dict(row) if row else None

    def daily_api_calls(self, day_key: str) -> int:
        with self._conn() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM plans WHERE day_key = ? AND source IN ('anthropic', 'fallback_after_api')",
                (day_key,),
            ).fetchone()
            return int(row[0]) if row else 0

    def record_plan(
        self,
        *,
        now_ts: int,
        slug: str,
        guid: int,
        source: str,
        model: str,
        effort: str,
        plan: dict[str, Any],
        primary_desc: str,
        cpp_weights: dict[str, float],
        meta: dict[str, Any],
        applied: bool,
        error_note: str,
        observed_at: str,
    ) -> None:
        dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        day_key = datetime.now().strftime("%Y-%m-%d")
        expires_at = now_ts + int(plan["session_minutes"]) * 60
        with self._conn() as db:
            db.execute(
                """
                INSERT INTO plans (
                    planned_at, planned_iso, day_key, expires_at, character_id, game_guid,
                    source, model, effort, primary_goal_id, primary_desc, secondary_goal_ids,
                    reason_code, session_minutes, weight_modifiers, cpp_weights,
                    input_tokens, output_tokens, thinking_tokens, latency_ms,
                    applied, error_note, observed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    now_ts,
                    dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    day_key,
                    expires_at,
                    slug,
                    int(guid),
                    source,
                    model,
                    effort,
                    plan["primary_goal_id"],
                    primary_desc,
                    json.dumps(plan["secondary_goal_ids"]),
                    plan["reason_code"],
                    int(plan["session_minutes"]),
                    json.dumps(plan["weight_modifiers"], sort_keys=True),
                    json.dumps(cpp_weights, sort_keys=True),
                    int(meta.get("input_tokens", 0)),
                    int(meta.get("output_tokens", 0)),
                    int(meta.get("thinking_tokens", 0)),
                    int(meta.get("latency_ms", 0)),
                    1 if applied else 0,
                    error_note[:200],
                    observed_at,
                ),
            )

    def summary(self, now_ts: int) -> dict[str, Any]:
        day_key = datetime.now().strftime("%Y-%m-%d")
        with self._conn() as db:
            db.row_factory = sqlite3.Row
            # Latest plan per character
            latest_rows = db.execute("""
                SELECT p.* FROM plans p
                INNER JOIN (
                    SELECT character_id, MAX(id) AS max_id FROM plans GROUP BY character_id
                ) m ON m.max_id = p.id
                ORDER BY p.planned_at DESC
            """).fetchall()
            day_row = db.execute("""
                SELECT
                    COUNT(*) AS total_today,
                    SUM(CASE WHEN source='anthropic' THEN 1 ELSE 0 END) AS ai_ok,
                    SUM(CASE WHEN source='fallback_after_api' THEN 1 ELSE 0 END) AS ai_err,
                    SUM(CASE WHEN source='fallback' THEN 1 ELSE 0 END) AS fb_only,
                    COALESCE(SUM(input_tokens), 0) AS in_tok,
                    COALESCE(SUM(output_tokens), 0) AS out_tok,
                    COALESCE(AVG(CASE WHEN source='anthropic' THEN latency_ms END), 0) AS avg_ms
                FROM plans WHERE day_key = ?
            """, (day_key,)).fetchone()

        active: dict[str, Any] = {}
        for r in latest_rows:
            d = dict(r)
            active[d["character_id"]] = {
                "character_id": d["character_id"],
                "source": d["source"],
                "model": d["model"],
                "effort": d["effort"],
                "primary_goal_id": d["primary_goal_id"],
                "primary_desc": d["primary_desc"],
                "secondary_goal_ids": json.loads(d["secondary_goal_ids"]),
                "reason_code": d["reason_code"],
                "session_minutes": d["session_minutes"],
                "planned_at": d["planned_at"],
                "planned_iso": d["planned_iso"],
                "expires_at": d["expires_at"],
                "active": d["expires_at"] > now_ts,
                "applied": bool(d["applied"]),
                "error_note": d["error_note"],
            }

        return {
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "day": day_key,
            "model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5"),
            "effort": os.getenv("ANTHROPIC_EFFORT", "medium"),
            "daily_cap": int(os.getenv("HEARTHCORE_DAILY_CALL_CAP", "100")),
            "today": {
                "total": int(day_row["total_today"] or 0),
                "ai_ok": int(day_row["ai_ok"] or 0),
                "ai_err": int(day_row["ai_err"] or 0),
                "fallback": int(day_row["fb_only"] or 0),
                "input_tokens": int(day_row["in_tok"] or 0),
                "output_tokens": int(day_row["out_tok"] or 0),
                "avg_latency_ms": int(day_row["avg_ms"] or 0),
            },
            "plans": active,
        }


def write_active_plans_json(store: BrainStore, now_ts: int) -> dict[str, Any]:
    data = store.summary(now_ts)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ACTIVE_PLANS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(ACTIVE_PLANS_PATH)
    return data


def run_cycle(
    *,
    provider: str = "anthropic",
    apply: bool = True,
    force: bool = False,
    limit: int = 0,
    character: str | None = None,
) -> dict[str, Any]:
    """Run one session-boundary planning cycle for online Hearthguard companions."""
    load_env()
    now_ts = int(time.time())
    day_key = datetime.now().strftime("%Y-%m-%d")
    daily_cap = int(os.getenv("HEARTHCORE_DAILY_CALL_CAP", "100"))
    cfg_model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
    cfg_effort = os.getenv("ANTHROPIC_EFFORT", "medium")

    store = BrainStore()
    world_ctx = fetch_world_context()
    known_slugs = set(profiles())

    targets = []
    for slug, comp in sorted(world_ctx["companions"].items()):
        if character and slug != character.lower():
            continue
        if not comp["online"] and not character:
            continue
        if slug not in known_slugs:
            continue
        if not force and store.active_plan(slug, now_ts) is not None:
            continue
        targets.append(comp)

    if limit > 0:
        targets = targets[:limit]

    planned = []
    api_calls_used = store.daily_api_calls(day_key)

    for comp in targets:
        slug = comp["slug"]
        char_prof = profile(slug)
        snap = build_snapshot(comp, world_ctx)
        desc_by_id = {c["id"]: c["description"] for c in snap["candidates"]}

        source = provider
        meta: dict[str, Any] = {}
        err_note = ""

        if provider == "anthropic":
            if api_calls_used >= daily_cap:
                source = "fallback"
                err_note = f"daily_cap_reached:{daily_cap}"
                plan = fallback(char_prof, snap)
            else:
                try:
                    plan, meta = anthropic_with_meta(char_prof, snap, model=cfg_model, effort=cfg_effort)
                    api_calls_used += 1
                except Invalid as exc:
                    source = "fallback_after_api"
                    err_note = str(exc)
                    api_calls_used += 1
                    plan = fallback(char_prof, snap)
        else:
            plan = fallback(char_prof, snap)

        cpp_w = weights_for_cpp(plan, snap)
        if apply:
            cpp_w = apply_plan_to_db(comp["guid"], plan, snap, source)

        primary_desc = desc_by_id.get(plan["primary_goal_id"], plan["primary_goal_id"])
        store.record_plan(
            now_ts=now_ts,
            slug=slug,
            guid=comp["guid"],
            source=source,
            model=meta.get("model") or (cfg_model if source == "anthropic" else "fallback"),
            effort=meta.get("effort") or (cfg_effort if source == "anthropic" else ""),
            plan=plan,
            primary_desc=primary_desc,
            cpp_weights=cpp_w,
            meta=meta,
            applied=apply,
            error_note=err_note,
            observed_at=snap["observed_at"],
        )
        planned.append({
            "name": comp["name"],
            "level": comp["level"],
            "source": source,
            "model": meta.get("model") or "fallback",
            "primary_goal_id": plan["primary_goal_id"],
            "reason_code": plan["reason_code"],
            "session_minutes": plan["session_minutes"],
            "tokens": f"{meta.get('input_tokens', 0)}/{meta.get('output_tokens', 0)}",
            "latency_ms": meta.get("latency_ms", 0),
            "error": err_note,
        })

    summary = write_active_plans_json(store, now_ts)
    return {
        "planned_count": len(planned),
        "planned": planned,
        "summary": summary,
    }
