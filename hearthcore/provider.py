"""Optional out-of-loop API planning."""
import json
import os
import urllib.request
from .core import Invalid, validate

def anthropic(p, snap):
    key, model = os.getenv("ANTHROPIC_API_KEY"), os.getenv("ANTHROPIC_MODEL")
    if not key or not model:
        raise Invalid("Set ANTHROPIC_API_KEY and ANTHROPIC_MODEL explicitly")
    system = ("You plan one WoW character session. Only use eligible candidate IDs provided. "
              "Never invent game events or privileges. Return ONLY JSON with exactly "
              "character_id, primary_goal_id, secondary_goal_ids, weight_modifiers, "
              "session_minutes, reason_code. Max 3 unique secondary IDs; weights 0.5-1.5; "
              "duration 20-240; short reason_code. No markdown or prose.")
    data = json.dumps({"model": model, "max_tokens": 700, "system": system,
      "messages": [{"role": "user", "content": json.dumps({"profile": p, "snapshot": snap})}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=data,
      headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=35) as r:
            result = json.load(r)
        value = "".join(x["text"] for x in result["content"] if x.get("type") == "text")
        return validate(json.loads(value), snap)
    except Exception as exc:
        raise Invalid("API failed or returned invalid data: " + type(exc).__name__) from exc
