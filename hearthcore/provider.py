"""Optional out-of-loop API planning with shared budget & billable-usage accounting."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .budget import BudgetExceeded, BudgetLedger, InvalidWithUsage, extract_usage_meta
from .core import Invalid, ROOT, validate

VALID_EFFORTS = {"low", "medium", "high", "max"}
FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*\Z", re.DOTALL)
DEFAULT_MAX_TOKENS = 1600


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
    p: dict[str, Any],
    snap: dict[str, Any],
    model: str | None = None,
    effort: str | None = None,
    memory: dict[str, Any] | None = None,
    *,
    budget: BudgetLedger | None = None,
    caller: str = "planner",
    is_shadow: bool = False,
    http_post_fn=None,
):
    """Call Anthropic Messages API for one validated session plan, returning (plan, meta).

    Accounting invariant (Issue #8):
    - Reserves conservative worst-case budget atomically BEFORE making the HTTP call.
    - Extracts response usage tokens IMMEDIATELY upon HTTP 200 BEFORE JSON parsing or validate().
    - Settles 'rejected' with exact billable usage if JSON/schema validation fails, 'error' ($0)
      on clear pre-generation HTTP 4xx, and 'uncertain' (keeping conservative reserve) on timeout
      or ambiguous transport failure.
    """
    load_env()
    key = os.getenv("ANTHROPIC_API_KEY")
    use_model = model or os.getenv("ANTHROPIC_MODEL")
    use_effort = (effort or os.getenv("ANTHROPIC_EFFORT") or "medium").strip().lower()
    if not use_model or (http_post_fn is None and not key):
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

    user_body: dict[str, Any] = {"profile": p, "snapshot": snap}
    if memory:
        user_body["memory"] = memory
    user_json = json.dumps(user_body, separators=(",", ":"))

    payload: dict[str, Any] = {
        "model": use_model,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user_json}],
    }
    if use_effort in VALID_EFFORTS:
        payload["output_config"] = {"effort": use_effort}

    res_id: int | None = None
    if budget is not None:
        est_in = max(600, (len(system) + len(user_json)) // 3)
        res_id = budget.reserve(
            caller=caller,
            model=use_model,
            est_input_tokens=est_in,
            max_output_tokens=DEFAULT_MAX_TOKENS,
            character_id=str(snap.get("character_id") or ""),
            is_shadow=is_shadow,
        )

    t0 = time.monotonic()
    try:
        if http_post_fn is not None:
            result = http_post_fn(payload)
        else:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=data,
                headers={
                    "x-api-key": key or "",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=45) as r:
                result = json.load(r)
        latency_ms = int((time.monotonic() - t0) * 1000)
    except urllib.error.HTTPError as exc:
        if budget is not None and res_id is not None:
            # 4xx (400/401/403/404/429) failed before generation; 5xx may be uncertain
            st = "error" if 400 <= exc.code < 500 else "uncertain"
            budget.settle(res_id, status=st, error_note=f"HTTP {exc.code}")
        raise Invalid(f"API HTTP {exc.code}") from exc
    except BudgetExceeded:
        raise
    except Exception as exc:
        if budget is not None and res_id is not None:
            budget.settle(res_id, status="uncertain", error_note=f"transport:{type(exc).__name__}")
        raise Invalid("API transport error: " + type(exc).__name__) from exc

    # Extract billable usage BEFORE parsing/validating response content!
    meta = extract_usage_meta(
        result if isinstance(result, dict) else {},
        fallback_model=use_model,
        effort=use_effort if use_effort in VALID_EFFORTS else "",
        latency_ms=latency_ms,
    )

    try:
        raw_text = "".join(
            x.get("text", "") for x in result.get("content", []) if isinstance(x, dict) and x.get("type") == "text"
        )
        cleaned = _strip_json_fences(raw_text)
        parsed = json.loads(cleaned)
        plan = validate(parsed, snap)
    except Invalid as exc:
        if budget is not None and res_id is not None:
            budget.settle(res_id, status="rejected", meta=meta, error_note=str(exc))
        raise InvalidWithUsage(str(exc), meta=meta) from exc
    except Exception as exc:
        note = f"Invalid response payload: {type(exc).__name__}"
        if budget is not None and res_id is not None:
            budget.settle(res_id, status="rejected", meta=meta, error_note=note)
        raise InvalidWithUsage(note, meta=meta) from exc

    if budget is not None and res_id is not None:
        budget.settle(res_id, status="ok", meta=meta)
    return plan, meta


def anthropic(p, snap):
    plan, _ = anthropic_with_meta(p, snap)
    return plan
