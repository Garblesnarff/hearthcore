"""Behavioral regression tests for every P1/P2 risk identified in the a72d87a independent review."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from hearthcore.bridge import (
    BrainStore,
    DEFAULT_FALLBACK_HOLDOUT_PCT,
    acquire_run_lock,
    parse_mysql_tsv,
    run_cycle,
    sync_event_journal,
)
from hearthcore.core import ROOT
from hearthcore.memory import MemoryEngine
from hearthcore.opportunities import run_shadow_match_snapshot


def _two_comp_world():
    return {
        "observed_at": "2026-11-25T19:30:00Z",
        "weekly_event": "",
        "humans": [],
        "owner_levels": {"wonky": 30, "logan": 10},
        "qm_focus": set(),
        "qm_mats_by_skill": {},
        "work_orders": [],
        "proposals_by_plan_id": {},
        "companions": {
            "brannoc": {
                "guid": 101, "name": "Brannoc", "slug": "brannoc", "level": 28, "xp": 100,
                "zone_id": 10, "zone": "Duskwood", "map_id": 0, "gold": 10.0, "online": True,
                "shift_group": "core-wonky", "skills": {164: (100, 150), 186: (110, 150)},
                "orders_delivered": 0, "quests_rewarded": 5, "ledger_ops": 2,
                "recent_events": [], "social_bonds": [],
            },
            "kelda": {
                "guid": 102, "name": "Kelda", "slug": "kelda", "level": 28, "xp": 100,
                "zone_id": 10, "zone": "Duskwood", "map_id": 0, "gold": 10.0, "online": True,
                "shift_group": "core-wonky", "skills": {165: (60, 75), 393: (65, 75)},
                "orders_delivered": 0, "quests_rewarded": 5, "ledger_ops": 2,
                "recent_events": [], "social_bonds": [],
            },
            "aldric": {
                "guid": 103, "name": "Aldric", "slug": "aldric", "level": 28, "xp": 100,
                "zone_id": 10, "zone": "Duskwood", "map_id": 0, "gold": 10.0, "online": True,
                "shift_group": "core-wonky", "skills": {171: (60, 75), 182: (65, 75)},
                "orders_delivered": 0, "quests_rewarded": 5, "ledger_ops": 2,
                "recent_events": [], "social_bonds": [],
            },
        },
    }


class ReviewRiskRegressionTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # Risk 1: MySQL integration test refuses non-_test DB or missing HEARTHCORE_TEST_DB=1
    # ------------------------------------------------------------------
    def test_mysql_integration_safety_guard(self):
        import tests.test_mysql_integration as mi

        with mock.patch.dict(os.environ, {"HEARTHCORE_DB_HOST": "127.0.0.1", "HEARTHCORE_DB_NAME": "acore_characters"}, clear=False):
            os.environ.pop("HEARTHCORE_TEST_DB", None)
            self.assertFalse(mi._is_safe_test_db())
        with mock.patch.dict(os.environ, {"HEARTHCORE_TEST_DB": "1", "HEARTHCORE_DB_HOST": "127.0.0.1", "HEARTHCORE_DB_NAME": "acore_characters"}):
            self.assertFalse(mi._is_safe_test_db())
        with mock.patch.dict(os.environ, {"HEARTHCORE_TEST_DB": "1", "HEARTHCORE_DB_HOST": "127.0.0.1", "HEARTHCORE_DB_NAME": "acore_characters_test"}):
            self.assertTrue(mi._is_safe_test_db())

    # ------------------------------------------------------------------
    # Risk 2: Least-privilege SQL template has no hardcoded passwords, no '%' host, column-scoped account
    # ------------------------------------------------------------------
    def test_least_priv_sql_template_safety(self):
        sql_txt = (ROOT / "ops" / "sql" / "create_least_priv_users.sql").read_text(encoding="utf-8")
        self.assertNotIn("CHANGE_ME", sql_txt)
        self.assertNotIn("@'%'", sql_txt)
        self.assertIn("QUOTE(@brain_pw)", sql_txt)
        self.assertIn("QUOTE(@watch_pw)", sql_txt)
        self.assertIn("GRANT SELECT (id, username) ON acore_auth.account TO", sql_txt)

    # ------------------------------------------------------------------
    # Risk 3: Persistent MySQL apply failure halts paid API calls & backs off per companion
    # ------------------------------------------------------------------
    def test_persistent_mysql_failure_halts_paid_calls_and_backs_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            db_p = tp / "brain.db"
            rt_p = tp / "runtime"
            world = _two_comp_world()
            api_calls = []

            def fake_api(payload):
                api_calls.append(payload["model"])
                snap = json.loads(payload["messages"][0]["content"])["snapshot"]
                cid = snap["character_id"]
                gid = snap["candidates"][0]["id"]
                return {
                    "model": "claude-sonnet-5-5",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 500, "output_tokens": 120},
                    "content": [{"type": "text", "text": json.dumps({
                        "character_id": cid,
                        "primary_goal_id": gid,
                        "secondary_goal_ids": [],
                        "weight_modifiers": {"quest": 1.1},
                        "session_minutes": 60,
                        "reason_code": "test_plan",
                    })}],
                }

            def always_fail_apply(sql: str):
                if "SELECT GREATEST(" in sql:
                    return [["0"]]
                raise RuntimeError("MySQL read-only / down")

            with mock.patch.dict(os.environ, {
                "ANTHROPIC_MODEL": "claude-sonnet-5-5",
                "HEARTHCORE_MAX_APPLY_FAILURES": "2",
                "HEARTHCORE_DAILY_CALL_CAP": "100",
            }):
                res1 = run_cycle(
                    provider="anthropic",
                    apply=True,
                    db_path=db_p,
                    runtime_dir=rt_p,
                    world_ctx_override=world,
                    http_post_fn=fake_api,
                    sql_exec=always_fail_apply,
                    now_ts_override=1_800_000_000,
                )
                # 3 companions online, max_apply_failures=2 -> stops after 2 failures (does NOT burn 3rd API call!)
                self.assertEqual(len(api_calls), 2)
                self.assertEqual(len(res1["failed"]), 2)

                # Next tick 60s later: the 2 failed companions are in paid-API backoff, so only the 3rd tries (1 call)!
                api_calls.clear()
                run_cycle(
                    provider="anthropic",
                    apply=True,
                    db_path=db_p,
                    runtime_dir=rt_p,
                    world_ctx_override=world,
                    http_post_fn=fake_api,
                    sql_exec=always_fail_apply,
                    now_ts_override=1_800_000_060,
                )
                self.assertEqual(len(api_calls), 1)

    # ------------------------------------------------------------------
    # Risk 3b (#53): Remote revision probe fails CLOSED when MySQL is unreachable
    # ------------------------------------------------------------------
    def test_remote_revision_probe_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            with self.assertRaises(RuntimeError):
                run_cycle(
                    provider="fallback",
                    apply=True,
                    db_path=tp / "brain.db",
                    runtime_dir=tp / "runtime",
                    world_ctx_override=_two_comp_world(),
                    sql_exec=lambda _s: (_ for _ in ()).throw(RuntimeError("MySQL unreachable")),
                    now_ts_override=1_800_000_000,
                )

    # ------------------------------------------------------------------
    # Risk 6 & P2: Dashboard GET auth rate limiting & live non-frozen data_age_sec
    # ------------------------------------------------------------------
    def test_dashboard_get_rate_limit_and_live_data_age(self):
        aw_dir = str((ROOT / "azeroth-watch").resolve())
        if aw_dir not in sys.path:
            sys.path.insert(0, aw_dir)
        import server as aw_server  # type: ignore

        age_box = {"age": 2}

        class AgeCollector:
            def get_snapshot(self):
                # Same version=1, increasing data_age_sec simulating stalled DB!
                return 1, {"db_ok": age_box["age"] < 10, "data_age_sec": age_box["age"]}

            def request_refresh(self):
                return

        with tempfile.TemporaryDirectory() as tmp:
            tok = "review_risk_admin_token_999"
            handler_cls = aw_server.make_handler(
                AgeCollector(),
                admin_token=tok,
                env_path=os.path.join(tmp, ".env"),
                overrides_path=os.path.join(tmp, "overrides.env"),
                audit_path=os.path.join(tmp, "audit.log"),
                allowed_hosts={"127.0.0.1", "localhost"},
            )
            srv = aw_server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                def get(path, extra=None):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    hdrs = {"Host": f"127.0.0.1:{port}"}
                    if extra:
                        hdrs.update(extra)
                    conn.request("GET", path, headers=hdrs)
                    resp = conn.getresponse()
                    body = resp.read().decode("utf-8")
                    conn.close()
                    return resp.status, body

                # 1. data_age_sec is NOT frozen in cache when DB goes stale!
                st1, b1 = get("/api/state")
                self.assertEqual(st1, 200)
                self.assertEqual(json.loads(b1)["data_age_sec"], 2)
                age_box["age"] = 45
                st2, b2 = get("/api/state")
                self.assertEqual(st2, 200)
                self.assertEqual(json.loads(b2)["data_age_sec"], 45)
                self.assertFalse(json.loads(b2)["db_ok"])

                # 2. Repeated wrong tokens on GET are rate-limited (429 on 6th attempt)!
                for _ in range(5):
                    st_try, _ = get("/api/state", {"X-Azeroth-Admin-Token": "wrong_guess"})
                    self.assertEqual(st_try, 200)
                st_lim, _ = get("/api/state", {"X-Azeroth-Admin-Token": "wrong_guess"})
                self.assertEqual(st_lim, 429)
            finally:
                srv.shutdown()
                srv.server_close()

    # ------------------------------------------------------------------
    # P2 fixes: Default holdout is 0%, journal reset keeps reused IDs, shadow writes no headroom_state.json
    # ------------------------------------------------------------------
    def test_p2_holdout_default_zero_journal_epoch_reset_and_shadow_purity(self):
        # 1. Holdout default is 0% (opt-in only)
        self.assertEqual(DEFAULT_FALLBACK_HOLDOUT_PCT, 0)

        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)

            # 2. Event journal counter reset increments epoch so reused ID=1 is NOT dropped!
            mem = MemoryEngine(tp / "brain.db")
            sync_event_journal(
                mem, {101: "brannoc"}, 1_800_000_000,
                sql_exec=lambda s: [["1", "2"]] if "MIN(id)" in s else [
                    ["1", "101", "level_up", "Old Lv20", "0", "12", "1800000000"],
                    ["2", "101", "level_up", "Old Lv21", "0", "12", "1800000001"],
                ],
            )
            self.assertEqual(mem.journal_status()["total_events"], 2)
            # Table truncated -> new event reuses ID=1!
            sync_event_journal(
                mem, {101: "brannoc"}, 1_800_000_100,
                sql_exec=lambda s: [["1", "1"]] if "MIN(id)" in s else [
                    ["1", "101", "boss_kill", "New Hogger after reset", "0", "12", "1800000100"],
                ],
            )
            self.assertEqual(mem.journal_status()["total_events"], 3, "Reused ID=1 after reset MUST be ingested under new epoch!")

            # 3. Shadow match snapshot NEVER creates headroom_state.json!
            rt = tp / "rt"
            run_shadow_match_snapshot(
                {"companions": _two_comp_world()["companions"], "work_orders": []},
                db_path=tp / "nonexistent.db",
                runtime_dir=rt,
                vitals={"mem_avail_gib": 1.0, "tick_p99": 15.0},
                now_ts=1_800_000_000,
            )
            self.assertFalse((rt / "headroom_state.json").exists())
            self.assertFalse((tp / "nonexistent.db").exists())

            # 4. Embedded lone \r preserved inside mysql TSV fields
            parsed = parse_mysql_tsv("1\tfoo\rbar\tNULL\n", expected_cols=3)
            self.assertEqual(parsed[0], ["1", "foo\rbar", None])

    def test_azeroth_watch_default_hearthcore_root_deployed_sibling_dir(self):
        """Regression test: ~/azeroth-watch sibling of ~/hearthcore MUST resolve to ~/hearthcore, not ~!"""
        aw_dir = str((ROOT / "azeroth-watch").resolve())
        if aw_dir not in sys.path:
            sys.path.insert(0, aw_dir)
        import server as aw_server  # type: ignore

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            # Deployed layout: ~/azeroth-watch next to ~/hearthcore/hearthcore/__init__.py
            (fake_home / "azeroth-watch").mkdir()
            (fake_home / "hearthcore" / "hearthcore").mkdir(parents=True)
            (fake_home / "hearthcore" / "hearthcore" / "__init__.py").write_text("", encoding="utf-8")

            with mock.patch.dict(os.environ, {"HEARTHCORE_ROOT": ""}):
                resolved_deployed = aw_server._default_hearthcore_root(
                    base_dir=fake_home / "azeroth-watch", home_dir=fake_home
                )
                self.assertEqual(resolved_deployed, (fake_home / "hearthcore").resolve())

                # In-repo layout: ~/hearthcore/azeroth-watch
                (fake_home / "hearthcore" / "azeroth-watch").mkdir()
                resolved_repo = aw_server._default_hearthcore_root(
                    base_dir=fake_home / "hearthcore" / "azeroth-watch", home_dir=fake_home
                )
                self.assertEqual(resolved_repo, (fake_home / "hearthcore").resolve())


if __name__ == "__main__":
    unittest.main()
