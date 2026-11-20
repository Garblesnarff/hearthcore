"""Validation and deterministic fallback, never game-side actions."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TYPES = {"quest", "dungeon", "gear", "profession", "gather", "craft", "train", "bank", "repair", "travel", "fish", "explore", "rest", "guild_order"}
ID = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")

class Invalid(ValueError):
    pass

def profiles():
    return sorted(p.parent.name for p in (ROOT / "characters").glob("*/profile.json"))

def profile(slug):
    if not re.fullmatch(r"[a-z0-9-]{1,64}", slug):
        raise Invalid("Invalid slug")
    file = ROOT / "characters" / slug / "profile.json"
    if not file.is_file():
        raise Invalid("Unknown character")
    p = json.loads(file.read_text(encoding="utf-8"))
    if p.get("slug") != slug or not isinstance(p.get("preferences"), dict):
        raise Invalid("Invalid profile")
    for key, value in p["preferences"].items():
        if key not in TYPES or type(value) not in (int, float) or not 0.5 <= value <= 1.5:
            raise Invalid("Bad profile preference")
    if type(p.get("session_minutes")) is not int or not 20 <= p["session_minutes"] <= 240:
        raise Invalid("Bad duration")
    return p

def snapshot(data, slug):
    if not isinstance(data, dict) or data.get("character_id") != slug or not isinstance(data.get("observed_at"), str):
        raise Invalid("Character/timestamp mismatch")
    cs = data.get("candidates")
    if not isinstance(cs, list) or not 1 <= len(cs) <= 100:
        raise Invalid("Requires 1-100 eligible candidates")
    seen = set()
    for c in cs:
        if not isinstance(c, dict) or set(c) != {"id", "type", "score", "description"}:
            raise Invalid("Bad candidate fields")
        if not isinstance(c["id"], str) or not ID.fullmatch(c["id"]) or c["id"] in seen:
            raise Invalid("Bad/duplicate candidate ID")
        seen.add(c["id"])
        if c["type"] not in TYPES or type(c["score"]) not in (int, float) or not 0 <= c["score"] <= 100:
            raise Invalid("Bad type/score")
        if not isinstance(c["description"], str) or len(c["description"]) > 250:
            raise Invalid("Bad description")
    return data

def validate(plan, snap):
    keys = {"character_id", "primary_goal_id", "secondary_goal_ids", "weight_modifiers", "session_minutes", "reason_code"}
    if not isinstance(plan, dict) or set(plan) != keys:
        raise Invalid("Unexpected plan fields")
    ids = {c["id"] for c in snap["candidates"]}
    if plan["character_id"] != snap["character_id"] or plan["primary_goal_id"] not in ids:
        raise Invalid("Unknown primary goal")
    other = plan["secondary_goal_ids"]
    if not isinstance(other, list) or len(other) > 3 or any(not isinstance(x, str) for x in other) or len(set(other)) != len(other) or any(x not in ids or x == plan["primary_goal_id"] for x in other):
        raise Invalid("Bad secondary goals")
    mods = plan["weight_modifiers"]
    if not isinstance(mods, dict) or any(k not in TYPES or type(v) not in (int, float) or not 0.5 <= v <= 1.5 for k, v in mods.items()):
        raise Invalid("Bad modifiers")
    if type(plan["session_minutes"]) is not int or not 20 <= plan["session_minutes"] <= 240:
        raise Invalid("Bad duration")
    if not isinstance(plan["reason_code"], str) or not ID.fullmatch(plan["reason_code"]):
        raise Invalid("Bad reason code")
    return plan

def fallback(p, snap):
    ranked = sorted(snap["candidates"], key=lambda c: (-c["score"] * p["preferences"].get(c["type"], 1), c["id"]))
    return validate({
        "character_id": snap["character_id"],
        "primary_goal_id": ranked[0]["id"],
        "secondary_goal_ids": [c["id"] for c in ranked[1:3]],
        "weight_modifiers": dict(p["preferences"]),
        "session_minutes": p["session_minutes"],
        "reason_code": "fallback_score",
    }, snap)
