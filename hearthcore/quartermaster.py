"""Claude Guild Quartermaster (Sonnet 5.5 medium) — Guild-wide supply strategy & expanded work orders.

Reads authoritative 40-character profession deficits (Classic 1-300 guide) + Hearthguard Guild Bank stock,
then asks Claude (strictly allowlist-validated) to:
  1. Fill up to 12 active Guild Work Orders in acore_characters.companion_work_orders:
     - Priority 1: Profession-leveling intermediates & training supplies (bars, kits, bags, bandages)
     - Priority 2: High-value leveling consumables (Smoked Sagefish / Sagefish Delight, Minor/Lesser
       Wizard & Mana Oils, Elixirs of Lion's Strength/Agility/Wisdom/Fortitude/Defense, Sharpening Stones)
  2. Maintain top 20 gathering directives in acore_characters.companion_bank_requests so C++
     CompanionBrain / CompanionCraft preserve & deposit exact bottleneck mats automatically.
  3. Publish runtime/quartermaster.json for individual bot Claude AI Brain plans & Azeroth Watch.

Zero server restart needed: CompanionLife::ReloadAll() and CompanionBrain::ReloadBankRequests()
automatically pick up DB changes within 5 minutes. Zero LLM text goes to in-game chat.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bridge import (
    BRAIN_DB_PATH,
    RUNTIME_DIR,
    acquire_run_lock,
    is_brain_disabled,
    run_sql,
)
from .budget import (
    BudgetExceeded,
    BudgetLedger,
    InvalidWithUsage,
    ProviderBackoffError,
    extract_usage_meta,
)
from .core import Invalid
from .guides import (
    LEVELING_ORDER_CATALOG,
    MAT_CATALOG,
    SKILL_NAMES,
    compute_supply_report,
)
from .provider import (
    DEFAULT_BUDGET_DB,
    VALID_EFFORTS,
    _strip_json_fences,
    load_env,
    parse_retry_after_seconds,
)

QUARTERMASTER_JSON = RUNTIME_DIR / "quartermaster.json"
QUARTERMASTER_SHADOW_JSON = RUNTIME_DIR / "quartermaster_shadow.json"
HEARTHGUARD_GUILD_ID = 6
TARGET_OPEN_ORDERS = 12
MAX_BANK_REQUESTS = 20
QM_MAX_TOKENS = 2800


def _sql_esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "''")


def fetch_40char_supply_state() -> dict[str, Any]:
    """Fetch all 40 Hearthguard characters' skills, Guild Bank counts, and currently open work orders."""
    char_rows = run_sql("""
        SELECT c.guid, c.name, c.level, c.online
        FROM characters c
        WHERE c.name IN (SELECT name FROM companion_schedule)
           OR c.name IN ('Wonky', 'Logan')
        ORDER BY c.level DESC, c.name;
    """)
    chars_by_guid: dict[int, dict[str, Any]] = {}
    for r in char_rows:
        if len(r) >= 4:
            g = int(r[0])
            chars_by_guid[g] = {
                "guid": g,
                "name": r[1],
                "level": int(r[2]),
                "online": int(r[3]) == 1,
                "skills": {},
            }

    if chars_by_guid:
        glist = ",".join(str(g) for g in chars_by_guid)
        sk_rows = run_sql(
            f"SELECT guid, skill, value, max FROM character_skills "
            f"WHERE guid IN ({glist}) AND skill IN (164,165,171,182,186,197,202,333,393,755,773,129,185,356);"
        )
        for r in sk_rows:
            if len(r) >= 4:
                g = int(r[0])
                if g in chars_by_guid:
                    chars_by_guid[g]["skills"][int(r[1])] = (int(r[2]), int(r[3]))

    # Hearthguard Guild Bank current counts
    vault_counts: dict[int, int] = {}
    bank_rows = run_sql(f"""
        SELECT ii.itemEntry, SUM(ii.count)
        FROM guild_bank_item gbi
        JOIN item_instance ii ON ii.guid = gbi.item_guid
        WHERE gbi.guildid = {HEARTHGUARD_GUILD_ID}
        GROUP BY ii.itemEntry;
    """)
    for r in bank_rows:
        if len(r) >= 2:
            vault_counts[int(r[0])] = int(r[1])

    # Currently open/claimed work orders
    open_orders: list[dict[str, Any]] = []
    wo_rows = run_sql("""
        SELECT id, requester_guid, requester_name, item_entry, item_name, quantity, status, story
        FROM companion_work_orders
        WHERE status IN ('open', 'claimed')
        ORDER BY id DESC LIMIT 30;
    """)
    for r in wo_rows:
        if len(r) >= 8:
            open_orders.append({
                "id": int(r[0]),
                "requester_guid": int(r[1]),
                "requester_name": r[2],
                "item_entry": int(r[3]),
                "item_name": r[4],
                "quantity": int(r[5]),
                "status": r[6],
                "story": r[7],
            })

    chars = list(chars_by_guid.values())
    supply = compute_supply_report(chars, vault_counts)
    return {
        "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "characters": chars,
        "vault_counts": vault_counts,
        "open_orders": open_orders,
        "supply": supply,
    }


def build_eligible_menus(state: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build strictly verified allowlists of candidate work orders & bank gathering requests."""
    chars = state["characters"]
    open_entries = {o["item_entry"] for o in state["open_orders"]}

    # Highest skill per profession across guild (and count of crafters within 25 pts of recipe)
    max_skill: dict[int, int] = {}
    skill_holders: dict[int, int] = {}
    max_guild_lvl = max((c["level"] for c in chars), default=1)
    for c in chars:
        for sid, (val, _mx) in c["skills"].items():
            max_skill[sid] = max(max_skill.get(sid, 0), val)
            skill_holders[sid] = skill_holders.get(sid, 0) + 1

    order_menu: list[dict[str, Any]] = []
    for tpl in LEVELING_ORDER_CATALOG:
        if tpl.item_entry in open_entries:
            continue
        if skill_holders.get(tpl.skill_id, 0) == 0:
            continue
        # Only offer orders a guild crafter can make now or is within 35 skill points of unlocking
        if max_skill.get(tpl.skill_id, 0) + 35 < tpl.min_skill:
            continue
        if tpl.min_guild_lvl > max_guild_lvl + 3:
            continue
        have_vault = state["vault_counts"].get(tpl.item_entry, 0)
        order_menu.append({
            "item_entry": tpl.item_entry,
            "name": tpl.item_name,
            "default_qty": tpl.qty,
            "skill": SKILL_NAMES.get(tpl.skill_id, str(tpl.skill_id)),
            "skill_id": tpl.skill_id,
            "min_skill": tpl.min_skill,
            "guild_max_skill": max_skill.get(tpl.skill_id, 0),
            "priority": tpl.priority,
            "category": tpl.category,
            "vault_have": have_vault,
            "purpose": tpl.story_hint,
        })

    # Candidate bank requests = top 30 raw mats with active deficit (plus Raw Sagefish!)
    bank_menu: list[dict[str, Any]] = []
    for m in state["supply"]["materials"][:30]:
        if m["active_need"] <= 0 and m["item_entry"] != 21071:
            continue
        bank_menu.append({
            "item_entry": m["item_entry"],
            "name": m["name"],
            "category": m["category"],
            "gather_skill": m["gather_skill_name"],
            "vault_have": m["vault_have"],
            "active_need": m["active_need"],
            "active_deficit": m["active_deficit"],
            "full_300_need": m["full_300_need"],
            "driven_by": m["driven_by_active"] or m["driven_by_all"],
        })

    return order_menu, bank_menu


def _validate_quartermaster_output(
    raw: dict[str, Any],
    order_menu: list[dict[str, Any]],
    bank_menu: list[dict[str, Any]],
    slots_needed: int,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise Invalid("Quartermaster output is not a JSON object")
    summary = str(raw.get("strategy_summary") or "").strip()[:420]
    if not summary:
        raise Invalid("Missing strategy_summary")

    valid_orders = {o["item_entry"]: o for o in order_menu}
    valid_banks = {b["item_entry"]: b for b in bank_menu}

    chosen_orders: list[dict[str, Any]] = []
    seen_o: set[int] = set()
    for item in raw.get("work_orders") or []:
        if not isinstance(item, dict):
            continue
        entry = int(item.get("item_entry") or 0)
        if entry not in valid_orders or entry in seen_o:
            continue
        meta = valid_orders[entry]
        qty = max(1, min(30, int(item.get("quantity") or meta["default_qty"])))
        reason = str(item.get("reason") or meta["purpose"]).strip()[:140]
        chosen_orders.append({
            "item_entry": entry,
            "name": meta["name"],
            "quantity": qty,
            "priority": meta["priority"],
            "category": meta["category"],
            "skill_id": meta["skill_id"],
            "reason": reason,
        })
        seen_o.add(entry)
        if len(chosen_orders) >= max(0, slots_needed):
            break

    chosen_banks: list[dict[str, Any]] = []
    seen_b: set[int] = set()
    for item in raw.get("bank_requests") or []:
        if not isinstance(item, dict):
            continue
        entry = int(item.get("item_entry") or 0)
        if entry not in valid_banks or entry in seen_b:
            continue
        meta = valid_banks[entry]
        min_cnt = max(20, min(200, int(item.get("min_count") or min(200, max(20, meta["active_need"] // 4)))))
        note = str(item.get("note") or f"{meta['driven_by']} (need {meta['active_need']})").strip()[:115]
        chosen_banks.append({
            "item_entry": entry,
            "name": meta["name"],
            "min_count": min_cnt,
            "note": note,
            "category": meta["category"],
            "gather_skill": meta["gather_skill"],
        })
        seen_b.add(entry)
        if len(chosen_banks) >= MAX_BANK_REQUESTS:
            break

    if not chosen_banks:
        raise Invalid("Quartermaster selected zero valid bank_requests")

    focus_skills = [str(x)[:32] for x in (raw.get("focus_skills") or []) if isinstance(x, str)][:6]
    return {
        "strategy_summary": summary,
        "work_orders": chosen_orders,
        "bank_requests": chosen_banks,
        "focus_skills": focus_skills,
    }


def _fallback_quartermaster(
    order_menu: list[dict[str, Any]],
    bank_menu: list[dict[str, Any]],
    slots_needed: int,
) -> dict[str, Any]:
    # Balanced mix: half Priority 1 (bars/kits/bags) and half Priority 2 (Sagefish/Wizard Oil/Elixirs/Stones/Potions)
    p1 = [o for o in order_menu if o["priority"] == 1]
    p2 = [o for o in order_menu if o["priority"] == 2]
    picked: list[dict[str, Any]] = []
    for i in range(max(len(p1), len(p2))):
        if len(picked) < slots_needed and i < len(p1):
            o = p1[i]
            picked.append({"item_entry": o["item_entry"], "quantity": o["default_qty"], "reason": o["purpose"]})
        if len(picked) < slots_needed and i < len(p2):
            o = p2[i]
            picked.append({"item_entry": o["item_entry"], "quantity": o["default_qty"], "reason": o["purpose"]})
    banks = [
        {
            "item_entry": b["item_entry"],
            "min_count": min(200, max(20, b["active_need"] // 4)),
            "note": f"{b['driven_by']} (need {b['active_need']})"[:115],
        }
        for b in bank_menu[:MAX_BANK_REQUESTS]
    ]
    return _validate_quartermaster_output(
        {
            "strategy_summary": "Deterministic guild supply balance across 40-char leveling deficits and Priority 2 leveling buffs.",
            "work_orders": picked,
            "bank_requests": banks,
            "focus_skills": ["Mining", "Herbalism", "Skinning", "Fishing", "Alchemy", "Cooking"],
        },
        order_menu,
        bank_menu,
        slots_needed,
    )


def call_claude_quartermaster(
    state: dict[str, Any],
    order_menu: list[dict[str, Any]],
    bank_menu: list[dict[str, Any]],
    slots_needed: int,
    model: str | None = None,
    effort: str | None = None,
    *,
    budget: BudgetLedger | None = None,
    budget_db_path: Path | None = None,
    allow_unbudgeted_for_test: bool = False,
    is_shadow: bool = False,
    http_post_fn=None,
    now_ts: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    load_env()
    key = os.getenv("ANTHROPIC_API_KEY")
    use_model = model or os.getenv("HEARTHCORE_STRATEGIST_MODEL") or "claude-sonnet-5-5"
    use_effort = (effort or os.getenv("ANTHROPIC_EFFORT") or "medium").strip().lower()
    if http_post_fn is None and not key:
        raise Invalid("ANTHROPIC_API_KEY not set")

    ledger: BudgetLedger | None = budget
    if ledger is None and not allow_unbudgeted_for_test:
        ledger = BudgetLedger(budget_db_path or DEFAULT_BUDGET_DB)
    if ledger is not None:
        ledger.check_provider_available("anthropic", now_ts=now_ts)

    system = (
        "You are the Guild Quartermaster strategist for Hearthguard, a 40-character Alliance WoW WotLK 3.3.5a "
        "guild (Wonky Lv30 Priest, Logan Lv10 Warrior, and 38 AI companions). "
        "Your job is two-fold:\n"
        "  PRIORITY 1: Direct gathering & intermediate crafting to level all 40 characters' professions efficiently "
        "through Classic (1-300) using the live material deficit ledger.\n"
        "  PRIORITY 2: Keep high-value leveling consumables stocked for Wonky, Logan, and the guild "
        "(Smoked Sagefish / Sagefish Delight for Mp5, Minor/Lesser Wizard & Mana Oils, Elixirs of Lion's Strength / "
        "Minor Agility / Minor Fortitude / Wisdom / Defense, Sharpening/Weightstones, Healing/Mana Potions, Bags, Bandages).\n"
        "RULES: ONLY pick item_entry integers that appear verbatim in the provided order_menu and bank_menu. "
        "Never invent item IDs. Return ONLY raw JSON with exact keys:\n"
        "  strategy_summary (1-2 concise sentences summarizing current bottlenecks and buff goals),\n"
        "  work_orders (array of up to slots_needed objects: {item_entry, quantity, reason} — aim for a balanced mix "
        "of Priority 1 leveling intermediates AND Priority 2 leveling buffs like Sagefish, Wizard/Mana Oil, Elixirs, Stones),\n"
        "  bank_requests (array of 12-20 objects from bank_menu: {item_entry, min_count (20-200), note (<=110 chars)}),\n"
        "  focus_skills (array of up to 6 profession names to prioritize right now).\n"
        "No markdown fences, no commentary, raw JSON only."
    )

    user_payload = {
        "guild_characters": state["supply"]["characters_analyzed"],
        "professions": state["supply"]["professions"],
        "already_open_orders": [
            {"item_entry": o["item_entry"], "name": o["item_name"], "qty": o["quantity"], "requester": o["requester_name"]}
            for o in state["open_orders"]
        ],
        "slots_needed": slots_needed,
        "order_menu": order_menu,
        "bank_menu": bank_menu,
    }
    user_json = json.dumps(user_payload, separators=(",", ":"))

    body: dict[str, Any] = {
        "model": use_model,
        "max_tokens": QM_MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user_json}],
    }
    if use_effort in VALID_EFFORTS:
        body["output_config"] = {"effort": use_effort}

    res_id: int | None = None
    if ledger is not None:
        est_in = max(1200, (len(system) + len(user_json)) // 3)
        res_id = ledger.reserve(
            caller="shadow_quartermaster" if is_shadow else "quartermaster",
            model=use_model,
            est_input_tokens=est_in,
            max_output_tokens=QM_MAX_TOKENS,
            is_shadow=is_shadow,
            now_ts=now_ts,
        )

    t0 = time.monotonic()
    try:
        if http_post_fn is not None:
            res = http_post_fn(body)
        else:
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "x-api-key": key or "",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.load(r)
        latency_ms = int((time.monotonic() - t0) * 1000)
    except urllib.error.HTTPError as exc:
        retry_after = parse_retry_after_seconds(getattr(exc, "headers", None), now_ts=now_ts)
        if ledger is not None:
            if res_id is not None:
                st = "error" if 400 <= exc.code < 500 else "uncertain"
                ledger.settle(res_id, status=st, error_note=f"HTTP {exc.code}", now_ts=now_ts)
            ledger.record_provider_failure(
                "anthropic",
                error_note=f"QM HTTP {exc.code}",
                retry_after_sec=retry_after,
                now_ts=now_ts,
            )
        suffix = f" (Retry-After {retry_after}s)" if retry_after > 0 else ""
        raise Invalid(f"Quartermaster API HTTP {exc.code}{suffix}") from exc
    except (BudgetExceeded, ProviderBackoffError):
        raise
    except Exception as exc:
        if ledger is not None:
            if res_id is not None:
                ledger.settle(res_id, status="uncertain", error_note=f"transport:{type(exc).__name__}", now_ts=now_ts)
            ledger.record_provider_failure(
                "anthropic",
                error_note=f"QM transport:{type(exc).__name__}",
                now_ts=now_ts,
            )
        raise Invalid(f"Quartermaster API error: {type(exc).__name__}") from exc

    if ledger is not None:
        ledger.record_provider_success("anthropic", now_ts=now_ts)

    # Extract billable usage BEFORE checking truncation or parsing response content! (Issues #8 & #55)
    meta = extract_usage_meta(
        res if isinstance(res, dict) else {},
        fallback_model=use_model,
        effort=use_effort if use_effort in VALID_EFFORTS else "",
        latency_ms=latency_ms,
    )

    if isinstance(res, dict) and str(res.get("stop_reason") or "") == "max_tokens":
        note = "Quartermaster response truncated: stop_reason=max_tokens"
        if ledger is not None and res_id is not None:
            ledger.settle(res_id, status="rejected", meta=meta, error_note=note, now_ts=now_ts)
        raise InvalidWithUsage(note, meta=meta)

    try:
        raw_txt = "".join(b.get("text", "") for b in res.get("content", []) if isinstance(b, dict) and b.get("type") == "text")
        parsed = json.loads(_strip_json_fences(raw_txt))
        validated = _validate_quartermaster_output(parsed, order_menu, bank_menu, slots_needed)
    except Invalid as exc:
        if ledger is not None and res_id is not None:
            ledger.settle(res_id, status="rejected", meta=meta, error_note=str(exc), now_ts=now_ts)
        raise InvalidWithUsage(str(exc), meta=meta) from exc
    except Exception as exc:
        note = f"Quartermaster invalid payload: {type(exc).__name__}"
        if ledger is not None and res_id is not None:
            ledger.settle(res_id, status="rejected", meta=meta, error_note=note, now_ts=now_ts)
        raise InvalidWithUsage(note, meta=meta) from exc

    if ledger is not None and res_id is not None:
        ledger.settle(res_id, status="ok", meta=meta, now_ts=now_ts)
    return validated, meta


def _pick_requester(order: dict[str, Any], chars: list[dict[str, Any]], idx: int) -> tuple[int, str]:
    """Pick a fitting Hearthguard character (preferably Wonky, Logan, or a non-crafter guildmate) for the order."""
    by_name = {c["name"]: c for c in chars}
    cat = order["category"]
    # Priority 2 leveling buffs for Wonky / Logan when appropriate!
    if cat in ("oil", "sagefish") and "Wonky" in by_name:
        return by_name["Wonky"]["guid"], "Wonky"
    if cat in ("stone", "elixir") and idx % 2 == 0 and "Logan" in by_name:
        return by_name["Logan"]["guid"], "Logan"
    # Otherwise pick an active companion who does NOT hold the crafting skill (avoid self-ordering!)
    skill_id = order["skill_id"]
    pool = [c for c in chars if c["name"] not in ("Wonky", "Logan") and skill_id not in c["skills"]]
    if not pool:
        pool = [c for c in chars if c["name"] not in ("Wonky", "Logan")]
    if not pool:
        pool = list(chars)
    chosen = pool[idx % len(pool)]
    return chosen["guid"], chosen["name"]


def apply_quartermaster_plan(
    plan: dict[str, Any],
    state: dict[str, Any],
    *,
    sql_exec=None,
    target_open_orders: int = TARGET_OPEN_ORDERS,
) -> tuple[int, int]:
    """Atomically apply validated work orders & bank requests in one MySQL transaction (Issue #54).

    Invariants:
    - Single transaction (START TRANSACTION ... COMMIT): native reloads never observe empty/half-written sets.
    - Re-checks open/claimed order count & duplicate item_entry INSIDE the transaction with FOR UPDATE.
    - Upserts Quartermaster bank requests first and deletes stale Quartermaster rows after (no empty gap).
    - NEVER modifies or deletes manual GM rows (requested_by != 'Quartermaster').
    """
    now = int(time.time())
    stmts: list[str] = ["START TRANSACTION;"]
    stmts.append("SELECT id FROM companion_work_orders WHERE status IN ('open', 'claimed') FOR UPDATE;")
    stmts.append("SET @qm_orders_added = 0;")

    for idx, wo in enumerate(plan["work_orders"]):
        req_guid, req_name = _pick_requester(wo, state["characters"], idx)
        prefix = "[P1 Supply]" if wo["priority"] == 1 else "[P2 Buff]"
        story = f"{req_name}: {prefix} {wo['reason']}"[:215]
        entry = int(wo["item_entry"])
        qty = int(wo["quantity"])
        stmts.append(
            "INSERT INTO companion_work_orders "
            "(requester_guid, requester_name, crafter_guid, crafter_name, item_entry, item_name, quantity, story, status, created_at, updated_at) "
            f"SELECT {int(req_guid)}, '{_sql_esc(req_name)}', 0, '', {entry}, "
            f"'{_sql_esc(wo['name'])}', {qty}, '{_sql_esc(story)}', 'open', {now}, {now} FROM DUAL "
            f"WHERE (SELECT COUNT(*) FROM (SELECT id FROM companion_work_orders WHERE status IN ('open', 'claimed')) AS open_cnt) < {int(target_open_orders)} "
            f"AND NOT EXISTS (SELECT 1 FROM (SELECT item_entry FROM companion_work_orders WHERE status IN ('open', 'claimed')) AS open_items WHERE item_entry = {entry});"
        )
        stmts.append("SET @qm_orders_added = @qm_orders_added + ROW_COUNT();")

    # Upsert Quartermaster-owned rows first; NEVER modify manual GM rows (requested_by != 'Quartermaster')!
    desired_entries: list[int] = []
    for br in plan["bank_requests"]:
        entry = int(br["item_entry"])
        desired_entries.append(entry)
        stmts.append(
            "INSERT INTO companion_bank_requests (item_entry, requested_by, min_count, note) "
            f"VALUES ({entry}, 'Quartermaster', {int(br['min_count'])}, '{_sql_esc(br['note'])}') "
            "ON DUPLICATE KEY UPDATE "
            "min_count = IF(requested_by = 'Quartermaster', VALUES(min_count), min_count), "
            "note = IF(requested_by = 'Quartermaster', VALUES(note), note);"
        )

    # Delete ONLY stale Quartermaster rows not in the new set (preserving all manual rows and avoiding any empty gap)
    if desired_entries:
        in_list = ",".join(str(e) for e in desired_entries)
        stmts.append(
            f"DELETE FROM companion_bank_requests WHERE requested_by = 'Quartermaster' AND item_entry NOT IN ({in_list});"
        )
    else:
        stmts.append("DELETE FROM companion_bank_requests WHERE requested_by = 'Quartermaster';")

    stmts.append(
        "SELECT @qm_orders_added, (SELECT COUNT(*) FROM companion_bank_requests WHERE requested_by = 'Quartermaster');"
    )
    stmts.append("COMMIT;")

    runner = sql_exec if sql_exec is not None else run_sql
    rows = runner("\n".join(stmts) + "\n")
    orders_added = len(plan["work_orders"])
    banks_set = len(desired_entries)
    if rows:
        last = rows[-1]
        if len(last) >= 2 and str(last[0]).isdigit() and str(last[1]).isdigit():
            orders_added = int(last[0])
            banks_set = int(last[1])
    return orders_added, banks_set


def _record_quartermaster_run(
    plan: dict[str, Any],
    source: str,
    meta: dict[str, Any],
    orders_added: int,
    banks_set: int,
    err_note: str,
    *,
    is_shadow: bool = False,
    db_path: Path = BRAIN_DB_PATH,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS quartermaster_runs (
                id INTEGER PRIMARY KEY,
                ran_at INTEGER NOT NULL,
                ran_iso TEXT NOT NULL,
                source TEXT NOT NULL,
                model TEXT NOT NULL,
                effort TEXT NOT NULL,
                strategy_summary TEXT NOT NULL,
                orders_added INTEGER NOT NULL,
                banks_set INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                error_note TEXT NOT NULL DEFAULT ''
            )
        """)
        existing_cols = {r[1] for r in db.execute("PRAGMA table_info(quartermaster_runs)").fetchall()}
        if "is_shadow" not in existing_cols:
            db.execute("ALTER TABLE quartermaster_runs ADD COLUMN is_shadow INTEGER NOT NULL DEFAULT 0")
        now = int(time.time())
        db.execute(
            """
            INSERT INTO quartermaster_runs (
                ran_at, ran_iso, source, model, effort, strategy_summary,
                orders_added, banks_set, input_tokens, output_tokens, latency_ms, error_note, is_shadow
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                now,
                datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                source,
                meta.get("model") or source,
                meta.get("effort") or "",
                plan["strategy_summary"],
                orders_added,
                banks_set,
                int(meta.get("input_tokens", 0)),
                int(meta.get("output_tokens", 0)),
                int(meta.get("latency_ms", 0)),
                err_note[:200],
                1 if is_shadow else 0,
            ),
        )


def run_quartermaster(
    *,
    provider: str = "anthropic",
    apply: bool = True,
    model: str | None = None,
    effort: str | None = None,
    db_path: Path = BRAIN_DB_PATH,
    runtime_dir: Path = RUNTIME_DIR,
    state_override: dict[str, Any] | None = None,
    http_post_fn=None,
    sql_exec=None,
    lock_timeout_sec: float = 2.0,
) -> dict[str, Any]:
    load_env()
    budget = BudgetLedger(db_path)
    budget.configured_caps()

    if apply and is_brain_disabled(runtime_dir):
        return {
            "source": "disabled",
            "meta": {},
            "error_note": "brain_disabled",
            "plan": {"strategy_summary": "Skipped: brain kill switch active.", "work_orders": [], "bank_requests": [], "focus_skills": []},
            "orders_added": 0,
            "banks_set": 0,
            "is_shadow": False,
            "disabled": True,
            "supply": {},
        }

    with acquire_run_lock(runtime_dir, timeout_sec=lock_timeout_sec):
        state = state_override if state_override is not None else fetch_40char_supply_state()
        slots_needed = max(0, TARGET_OPEN_ORDERS - len(state["open_orders"]))
        order_menu, bank_menu = build_eligible_menus(state)

        source = provider
        meta: dict[str, Any] = {}
        err_note = ""

        if provider == "anthropic":
            try:
                plan, meta = call_claude_quartermaster(
                    state,
                    order_menu,
                    bank_menu,
                    slots_needed,
                    model=model,
                    effort=effort,
                    budget=budget,
                    is_shadow=not apply,
                    http_post_fn=http_post_fn,
                )
            except (BudgetExceeded, ProviderBackoffError) as exc:
                source = "fallback"
                err_note = str(exc)
                plan = _fallback_quartermaster(order_menu, bank_menu, slots_needed)
            except InvalidWithUsage as exc:
                source = "fallback_after_api"
                err_note = str(exc)
                meta = exc.meta
                plan = _fallback_quartermaster(order_menu, bank_menu, slots_needed)
            except Invalid as exc:
                source = "fallback_after_api"
                err_note = str(exc)
                plan = _fallback_quartermaster(order_menu, bank_menu, slots_needed)
        else:
            plan = _fallback_quartermaster(order_menu, bank_menu, slots_needed)

        orders_added, banks_set = (0, 0)
        if apply:
            orders_added, banks_set = apply_quartermaster_plan(plan, state, sql_exec=sql_exec)

        _record_quartermaster_run(
            plan, source, meta, orders_added, banks_set, err_note, is_shadow=not apply, db_path=db_path
        )

        # Strict shadow isolation (Issue #7): shadow runs write quartermaster_shadow.json, NEVER live quartermaster.json!
        snapshot_out = {
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "is_shadow": not apply,
            "source": source,
            "model": meta.get("model") or source,
            "effort": meta.get("effort") or "",
            "latency_ms": meta.get("latency_ms", 0),
            "input_tokens": meta.get("input_tokens", 0),
            "output_tokens": meta.get("output_tokens", 0),
            "error_note": err_note,
            "strategy_summary": plan["strategy_summary"],
            "focus_skills": plan["focus_skills"],
            "bank_requests": plan["bank_requests"],
            "orders_added_last_run": orders_added,
            "target_open_orders": TARGET_OPEN_ORDERS,
            "budget": budget.summary(),
            "supply": {
                "characters_analyzed": state["supply"]["characters_analyzed"],
                "professions": state["supply"]["professions"],
                "top_deficits": state["supply"]["materials"][:24],
            },
        }
        runtime_dir.mkdir(parents=True, exist_ok=True)
        target_path = (runtime_dir / "quartermaster.json") if apply else (runtime_dir / "quartermaster_shadow.json")
        tmp = target_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot_out, indent=2) + "\n", encoding="utf-8")
        tmp.replace(target_path)
        return {
            "source": source,
            "meta": meta,
            "error_note": err_note,
            "plan": plan,
            "orders_added": orders_added,
            "banks_set": banks_set,
            "is_shadow": not apply,
            "supply": state["supply"],
        }
