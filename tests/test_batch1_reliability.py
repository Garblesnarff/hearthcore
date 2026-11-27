"""Comprehensive regression tests for Batch 1 P1 reliability & hygiene issues (#52, #53, #54, #55, #59, #63, #73)."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

from hearthcore.bridge import (
    BrainStore,
    RunLockBusy,
    acquire_run_lock,
    is_brain_disabled,
    run_cycle,
    set_brain_disabled,
)
from hearthcore.budget import (
    MOST_EXPENSIVE_PRICING,
    BudgetExceeded,
    BudgetLedger,
    InvalidWithUsage,
    ProviderBackoffError,
    _day_key,
    _month_key,
    pricing_for_model,
)
from hearthcore.core import Invalid, ROOT, profile, snapshot
from hearthcore.provider import anthropic, anthropic_with_meta
from hearthcore.quartermaster import apply_quartermaster_plan, call_claude_quartermaster, run_quartermaster


def _dummy_world_ctx():
    return {
        "observed_at": "2026-11-25T16:00:00Z",
        "weekly_event": "",
        "humans": [],
        "owner_levels": {"wonky": 30, "logan": 10},
        "qm_focus": set(),
        "qm_mats_by_skill": {},
        "work_orders": [],
        "proposals_by_plan_id": {},
        "companions": {
            "brannoc": {
                "guid": 101,
                "name": "Brannoc",
                "slug": "brannoc",
                "level": 28,
                "xp": 1200,
                "zone_id": 10,
                "zone": "Duskwood",
                "map_id": 0,
                "gold": 12.5,
                "online": True,
                "shift_group": "core-wonky",
                "skills": {164: (100, 150), 186: (110, 150)},
                "orders_delivered": 1,
                "quests_rewarded": 20,
                "ledger_ops": 10,
                "recent_events": [],
                "social_bonds": [],
            },
            "kelda": {
                "guid": 102,
                "name": "Kelda",
                "slug": "kelda",
                "level": 28,
                "xp": 900,
                "zone_id": 10,
                "zone": "Duskwood",
                "map_id": 0,
                "gold": 9.0,
                "online": True,
                "shift_group": "core-wonky",
                "skills": {165: (60, 75), 393: (65, 75)},
                "orders_delivered": 0,
                "quests_rewarded": 15,
                "ledger_ops": 8,
                "recent_events": [],
                "social_bonds": [],
            },
        },
    }


class Batch1ReliabilityTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # Issue #52: Apply MySQL before marking applied=1 + per-companion isolation
    # ------------------------------------------------------------------
    def test_issue52_failed_sql_isolates_companion_and_never_blocks_replan(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            db_path = tmp_p / "brain.db"
            rt_dir = tmp_p / "runtime"
            world = _dummy_world_ctx()
            calls = []

            def flaky_sql(sql: str):
                calls.append(sql)
                if "SELECT GREATEST(" in sql:
                    return [["0"]]
                if "VALUES (101," in sql:
                    raise RuntimeError("simulated mysql deadlock for brannoc")
                return []

            res = run_cycle(
                provider="fallback",
                apply=True,
                db_path=db_path,
                runtime_dir=rt_dir,
                world_ctx_override=world,
                sql_exec=flaky_sql,
                now_ts_override=1_800_000_000,
            )

            # Companion N (brannoc) failed; companion N+1 (kelda) STILL planned and applied!
            self.assertEqual(res["planned_count"], 1)
            self.assertEqual(res["planned"][0]["name"], "Kelda")
            self.assertEqual(len(res["failed"]), 1)
            self.assertEqual(res["failed"][0]["character_id"], "brannoc")

            # active_plans.json was rewritten and includes kelda, NOT brannoc
            ap = json.loads((rt_dir / "active_plans.json").read_text(encoding="utf-8"))
            self.assertIn("kelda", ap["plans"])
            self.assertNotIn("brannoc", ap["plans"])

            # Brannoc has NO applied row blocking replanning on the next tick!
            store = BrainStore(db_path)
            self.assertIsNone(store.active_plan("brannoc", 1_800_000_060))
            self.assertIsNotNone(store.active_plan("kelda", 1_800_000_060))

            # Next tick succeeds cleanly for Brannoc when DB recovers
            res2 = run_cycle(
                provider="fallback",
                apply=True,
                db_path=db_path,
                runtime_dir=rt_dir,
                world_ctx_override=world,
                sql_exec=lambda s: [["0"]] if "SELECT GREATEST(" in s else [],
                now_ts_override=1_800_000_060,
            )
            self.assertEqual(res2["planned_count"], 1)
            self.assertEqual(res2["planned"][0]["name"], "Brannoc")

    # ------------------------------------------------------------------
    # Issue #53: Single durable monotonic revision/plan_id surviving brain.db loss
    # ------------------------------------------------------------------
    def test_issue53_empty_brain_db_resyncs_above_existing_mysql_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            db_path = tmp_p / "fresh_empty_brain.db"
            rt_dir = tmp_p / "runtime"
            world = _dummy_world_ctx()
            written_sql = []

            def mysql_fixture(sql: str):
                if "SELECT GREATEST(" in sql:
                    return [["500"]]
                written_sql.append(sql)
                return []

            res = run_cycle(
                provider="fallback",
                apply=True,
                character="brannoc",
                db_path=db_path,
                runtime_dir=rt_dir,
                world_ctx_override=world,
                sql_exec=mysql_fixture,
                now_ts_override=1_800_000_000,
            )

            self.assertTrue(res["sequence_sync"]["resynced"])
            self.assertEqual(res["planned"][0]["plan_id"], 501)
            self.assertTrue(any(", 501, 501," in s for s in written_sql))

    # ------------------------------------------------------------------
    # Issue #54: Atomic Quartermaster transaction, upsert+delete-missing, preserve manual rows
    # ------------------------------------------------------------------
    def test_issue54_quartermaster_atomic_sql_preserves_manual_rows_and_rechecks_slots(self):
        captured = []

        def fake_sql(sql: str):
            captured.append(sql)
            return [["1", "2"]]

        plan = {
            "strategy_summary": "Atomic test",
            "work_orders": [
                {"item_entry": 2840, "name": "Copper Bar", "quantity": 10, "priority": 1, "category": "bar", "skill_id": 186, "reason": "leveling"},
            ],
            "bank_requests": [
                {"item_entry": 2770, "name": "Copper Ore", "min_count": 80, "note": "Mining need", "category": "ore", "gather_skill": "Mining"},
                {"item_entry": 2447, "name": "Peacebloom", "min_count": 60, "note": "Herb need", "category": "herb", "gather_skill": "Herbalism"},
            ],
            "focus_skills": ["Mining"],
        }
        state = {
            "characters": [
                {"guid": 1, "name": "Wonky", "level": 30, "skills": {}},
                {"guid": 102, "name": "Kelda", "level": 28, "skills": {165: (60, 75)}},
            ]
        }

        added, banks = apply_quartermaster_plan(plan, state, sql_exec=fake_sql, target_open_orders=12)
        self.assertEqual((added, banks), (1, 2))
        self.assertEqual(len(captured), 1)
        sql = captured[0]
        self.assertTrue(sql.startswith("START TRANSACTION;"))
        self.assertIn("FOR UPDATE;", sql)
        self.assertIn("COMMIT;", sql)
        # Upsert happens BEFORE DELETE missing, and NEVER modifies manual GM rows!
        upsert_pos = sql.index("INSERT INTO companion_bank_requests")
        delete_pos = sql.index("DELETE FROM companion_bank_requests")
        self.assertLess(upsert_pos, delete_pos)
        self.assertIn("IF(requested_by = 'Quartermaster', VALUES(min_count), min_count)", sql)
        self.assertIn("WHERE requested_by = 'Quartermaster' AND item_entry NOT IN (2770,2447);", sql)

    # ------------------------------------------------------------------
    # Issue #55 (#56): Mandatory ledger, unknown model pricing, cap 0, UTC/reset, circuit breaker, max_tokens
    # ------------------------------------------------------------------
    def test_issue55_unknown_model_pricing_cap_zero_and_reset_window(self):
        # Unknown model priced at most expensive tier (Opus)
        self.assertEqual(pricing_for_model("claude-mystery-9-9"), MOST_EXPENSIVE_PRICING)

        # Unified UTC day + billing period aligned with credit reset day
        self.assertEqual(_day_key(1795631200), "2026-11-25")  # 2026-11-25 UTC
        self.assertEqual(_month_key(1795631200, credit_reset="2026-12-02"), "2026-11-02")
        # Dec 2 2026 00:00:00 UTC rolls cleanly into the new billing period
        self.assertEqual(_month_key(1796169600, credit_reset="2026-12-02"), "2026-12-02")

        with tempfile.TemporaryDirectory() as tmp:
            ledger = BudgetLedger(Path(tmp) / "budget.db")

            # Cap value 0 unambiguously blocks
            with self.assertRaises(BudgetExceeded):
                ledger.reserve(
                    caller="planner", model="claude-sonnet-5-5",
                    est_input_tokens=500, max_output_tokens=500,
                    monthly_usd_cap=50.0, daily_call_cap=0,
                )

            # Invalid env var fails cleanly at startup
            with mock.patch.dict(os.environ, {"HEARTHCORE_DAILY_CALL_CAP": "not-an-int"}):
                with self.assertRaises(Invalid):
                    ledger.configured_caps()

    def test_issue55_mandatory_ledger_max_tokens_and_circuit_breaker_retry_after(self):
        p = profile("brannoc")
        snap = snapshot(json.loads((ROOT / "examples" / "brannoc.json").read_text(encoding="utf-8")), "brannoc")

        with tempfile.TemporaryDirectory() as tmp:
            db_p = Path(tmp) / "brain.db"
            ledger = BudgetLedger(db_p)

            # 1. stop_reason == "max_tokens" is reported explicitly (not misreported as invalid JSON)
            def truncated_api(_payload):
                return {
                    "model": "claude-sonnet-5-5",
                    "stop_reason": "max_tokens",
                    "usage": {"input_tokens": 900, "output_tokens": 1600},
                    "content": [{"type": "text", "text": '{"character_id": "bran'}],
                }

            with mock.patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-sonnet-5-5", "HEARTHCORE_DAILY_CALL_CAP": "50"}):
                with self.assertRaises(InvalidWithUsage) as ctx:
                    anthropic_with_meta(p, snap, budget=ledger, http_post_fn=truncated_api, now_ts=1_800_000_000)
                self.assertIn("stop_reason=max_tokens", str(ctx.exception))
                self.assertEqual(ledger.summary(1_800_000_000)["month_rejected"], 1)

            # 2. HTTP 429 with Retry-After activates backoff and blocks subsequent calls
            def rate_limited_api(_payload):
                hdrs = Message()
                hdrs["Retry-After"] = "120"
                raise urllib.error.HTTPError("https://api.anthropic.com/v1/messages", 429, "Too Many Requests", hdrs, io.BytesIO(b""))

            with mock.patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-sonnet-5-5", "HEARTHCORE_DAILY_CALL_CAP": "50"}):
                with self.assertRaises(Invalid) as ctx429:
                    anthropic_with_meta(p, snap, budget=ledger, http_post_fn=rate_limited_api, now_ts=1_800_000_010)
                self.assertIn("Retry-After 120s", str(ctx429.exception))
                with self.assertRaises(ProviderBackoffError):
                    anthropic_with_meta(p, snap, budget=ledger, http_post_fn=truncated_api, now_ts=1_800_000_050)

            # 3. `plan --provider anthropic` helper `anthropic()` ALWAYS enforces BudgetLedger (never unbudgeted!)
            cli_db = Path(tmp) / "cli_brain.db"
            with mock.patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-sonnet-5-5", "HEARTHCORE_DAILY_CALL_CAP": "0"}):
                with self.assertRaises(BudgetExceeded):
                    anthropic(p, snap, budget_db_path=cli_db, http_post_fn=truncated_api)

    # ------------------------------------------------------------------
    # Issue #59: Shared flock & brain kill switch
    # ------------------------------------------------------------------
    def test_issue59_shared_flock_and_kill_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = Path(tmp) / "runtime"
            db_p = rt / "brain.db"

            # Shared flock prevents overlapping brain-tick / quartermaster-tick runs
            with acquire_run_lock(rt, timeout_sec=0.0):
                with self.assertRaises(RunLockBusy):
                    with acquire_run_lock(rt, timeout_sec=0.0):
                        pass

            # Kill switch disables live cycles cleanly without touching MySQL
            res_off = set_brain_disabled(True, runtime_dir=rt, skip_db=True)
            self.assertTrue(res_off["flag_present"])
            self.assertTrue(is_brain_disabled(rt))

            cyc = run_cycle(
                provider="fallback",
                apply=True,
                db_path=db_p,
                runtime_dir=rt,
                world_ctx_override=_dummy_world_ctx(),
                sql_exec=lambda _s: (_ for _ in ()).throw(AssertionError("must not call SQL while disabled")),
            )
            self.assertTrue(cyc.get("disabled"))
            self.assertEqual(cyc["planned_count"], 0)

            qm = run_quartermaster(
                provider="fallback",
                apply=True,
                db_path=db_p,
                runtime_dir=rt,
                sql_exec=lambda _s: (_ for _ in ()).throw(AssertionError("must not call SQL while disabled")),
            )
            self.assertTrue(qm.get("disabled"))

            res_on = set_brain_disabled(False, runtime_dir=rt, skip_db=True)
            self.assertFalse(res_on["flag_present"])

    # ------------------------------------------------------------------
    # Issue #63 & #73: CI workflow hardening & license files present
    # ------------------------------------------------------------------
    def test_issue63_and_73_ci_and_licenses(self):
        ci_txt = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("permissions:", ci_txt)
        self.assertIn("contents: read", ci_txt)
        self.assertIn("actions/checkout@11d5960a326750d5838078e36cf38b85af677262", ci_txt)
        self.assertIn("actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065", ci_txt)

        self.assertTrue((ROOT / "LICENSE").is_file())
        self.assertTrue((ROOT / "mod-playerbots-companions" / "LICENSE").is_file())
        self.assertTrue((ROOT / "mod-playerbots-companions" / "AUTHORS").is_file())
        self.assertTrue((ROOT / "mod-playerbots-companions" / "NOTICE.md").is_file())
        self.assertIn("405f8119", (ROOT / "mod-playerbots-companions" / "NOTICE.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
