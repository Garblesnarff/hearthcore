"""Optional out-of-loop API planning."""
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from .core import Invalid, ROOT, validate

VALID_EFFORTS = {"low", "medium", "high", "max"}
FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*\Z", re.DOTALL)


def load_env(env_path: Path | None = None) -> None:
    """Populate os.environ from .env (setdefault only, never prints secrets)."""
    path = env_path or (ROOT / ".env")
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and v:
            os.environ.setdefault(k, v)


def _strip_json_fences(text: str) -> str:
    m = FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text.strip()


def anthropic_with_meta(
    p,
    snap,
    model: str | None = None,
    effort: str | None = None,
    memory: dict | None = None,
):
    """Call Anthropic Messages API for one validated session plan, returning (plan, meta)."""
    load_env()
    key = os.getenv("ANTHROPIC_API_KEY")
    use_model = model or os.getenv("ANTHROPIC_MODEL")
    use_effort = (effort or os.getenv("ANTHROPIC_EFFORT") or "medium").strip().lower()
    if not key or not use_model:
        raise Invalid("Set ANTHROPIC_API_KEY and ANTHROPIC_MODEL explicitly")

    system = (
        "You are the out-of-game session goal planner for a single Alliance companion in a private "
        "WoW WotLK guild (Hearthguard). Pick coherent session goals that fit the character's "
        "archetype, culture, professions, motivations, persistent memory (open intentions, milestones, "
        "social bonds, variety/stall hints), and current verified candidates. "
        "Only use eligible candidate IDs provided in snapshot.candidates. Respect '[Claimed by ...]' "
        "soft reservations on guild orders when another good candidate exists, and heed stall_caution "
        "or variety_hint when present. Never invent game events, items, or privileges. "
        "Return ONLY a raw JSON object with exact keys: "
        "character_id, primary_goal_id, secondary_goal_ids, weight_modifiers, session_minutes, reason_code. "
        "Rules: max 3 unique secondary_goal_ids (none equal to primary_goal_id); weight_modifiers keys "
        "must be from allowed candidate types and values strictly between 0.5 and 1.5; session_minutes "
        "integer 20-240; reason_code short snake_case identifier (alphanumeric/underscore/colon/dot/dash, "
        "no spaces). No markdown fences, no commentary, JSON only."
    )

    user_body = {"profile": p, "snapshot": snap}
    if memory:
        user_body["memory"] = memory

    payload = {
        "model": use_model,
        "max_tokens": 1600,
        "system": system,
        "messages": [
            {
                "role": "user",
                "content": json.dumps(user_body, separators=(",", ":")),
            }
        ],
    }
    if use_effort in VALID_EFFORTS:
        payload["output_config"] = {"effort": use_effort}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            result = json.load(r)
        latency_ms = int((time.monotonic() - t0) * 1000)
        raw_text = "".join(x.get("text", "") for x in result.get("content", []) if x.get("type") == "text")
        cleaned = _strip_json_fences(raw_text)
        parsed = json.loads(cleaned)
        plan = validate(parsed, snap)
        usage = result.get("usage") or {}
        out_details = usage.get("output_tokens_details") or {}
        meta = {
            "model": result.get("model") or use_model,
            "effort": use_effort if use_effort in VALID_EFFORTS else "",
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "thinking_tokens": int(out_details.get("thinking_tokens") or 0),
            "latency_ms": latency_ms,
        }
        return plan, meta
    except urllib.error.HTTPError as exc:
        raise Invalid(f"API HTTP {exc.code}") from exc
    except Invalid:
        raise
    except Exception as exc:
        raise Invalid("API failed or returned invalid data: " + type(exc).__name__) from exc


def anthropic(p, snap):
    plan, _ = anthropic_with_meta(p, snap)
    return plan
