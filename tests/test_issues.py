"""Comprehensive regression tests covering GitHub Issues #3 through #9.

Zero real API calls, zero live MySQL mutations — 100% isolated temp SQLite & local HTTP test server.
"""
from __future__ import annotations

import copy
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hearthcore.bridge import (
    BrainStore,
    apply_plan_to_db,
    build_proposal_spec,
    build_snapshot,
    run_cycle,
)
from hearthcore.budget import BudgetExceeded, BudgetLedger, InvalidWithUsage, estimate_cost_usd
from hearthcore.core import profile
from hearthcore.memory import MemoryEngine, classify_plan_outcome
from hearthcore.provider import anthropic_with_meta
from hearthcore.quartermaster import run_quartermaster


def _sample_world(now_iso: str = "2026-11-24T21:00:00Z") -> dict:
    return {
        "observed_at": now_iso,
        "weekly_event": "",
        "companions": {
            "brannoc": {
                "guid": 101,
                "name": "Brannoc",
                "slug": "brannoc",
                "level": 30,
                "xp": 1200,
                "zone_id": 1,
                "zone": "Dun Morogh",
                "map_id": 0,
                "gold": 12.5,
                "online": True,
                "shift_group": "core-wonky",
                "skills": {164: (132, 150), 186: (140, 150)},
                "orders_delivered": 0,
                "quests_rewarded": 10,
                "ledger_ops": 5,
                "recent_events": [],
                "social_bonds": [],
            }
        },
        "work_orders": [
            {
                "id": 77,
                "requester_guid": 202,
                "requester_name": "Kelda",
                "item_entry": 2862,
                "item_name": "Rough Sharpening Stone",
                "quantity": 10,
                "story": "Leveling blades",
                "skill": 164,
            }
        ],
        "humans": [],
        "qm_focus": set(),
        "qm_mats_by_skill": {},
        "proposals_by_plan_id": {},
    }


class IssueRegressionTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # Issue #3: Bind AI-selected goal IDs to native objectives & advisory labels
    # ------------------------------------------------------------------
    def test_issue3_proposal_spec_and_advisory_flags(self):
        world = _sample_world()
        comp = world["companions"]["brannoc"]
        snap = build_snapshot(comp, world)

        order_plan = {
            "character_id": "brannoc",
            "primary_goal_id": "guild_order:77:rough_sharpening_stone",
            "secondary_goal_ids": ["craft:blacksmithing"],
            "weight_modifiers": {"guild_order": 1.3},
            "session_minutes": 90,
            "reason_code": "fulfill_order",
        }
        p_ord = build_proposal_spec(order_plan, snap)
        self.assertEqual(p_ord["goal_type"], "guild_order")
        self.assertEqual(p_ord["target_id"], 77)
        self.assertEqual(p_ord["advisory_only"], 0)

        adv_plan = {
            "character_id": "brannoc",
            "primary_goal_id": "dungeon:prep",
            "secondary_goal_ids": [],
            "weight_modifiers": {"dungeon": 1.2},
            "session_minutes": 60,
            "reason_code": "prep",
        }
        p_adv = build_proposal_spec(adv_plan, snap)
        self.assertEqual(p_adv["goal_type"], "dungeon")
        self.assertEqual(p_adv["advisory_only"], 1)

        captured_sql = []
        apply_plan_to_db(
            101,
            "Brannoc",
            order_plan,
            snap,
            "anthropic",
            plan_id=42,
            now_ts=1_800_000_000,
            sql_exec=lambda s: captured_sql.append(s) or [],
        )
        sql_blob = "\n".join(captured_sql)
        self.assertIn("companion_proposals", sql_blob)
        self.assertIn("guild_order:77:rough_sharpening_stone", sql_blob)
        # Must NOT touch companion_weights canonical baseline (Issue #6)
        self.assertNotIn("INTO companion_weights ", sql_blob)
        self.assertIn("INTO companion_weight_overrides", sql_blob)

    # ------------------------------------------------------------------
    # Issue #4: Durable event journal beyond 12-event UI window
    # ------------------------------------------------------------------
    def test_issue4_durable_event_journal_replay_and_gap_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = MemoryEngine(Path(tmp) / "brain.db")
            # Initial ingestion starts at source_id=50 -> honest gap detection!
            events = [
                {
                    "source_id": 50,
                    "guid": 101,
                    "character_id": "brannoc",
                    "event_type": "level_up",
                    "summary": "Reached Level 25 in Loch Modan",
                    "created_at": 1_800_000_000,
                }
            ]
            # Add 20 routine craft events after the milestone (>12 window)
            for i in range(1, 21):
                events.append({
                    "source_id": 50 + i,
                    "guid": 101,
                    "character_id": "brannoc",
                    "event_type": "craft_done",
                    "summary": f"Crafted Copper Bar #{i}",
                    "created_at": 1_800_000_000 + i * 60,
                })
            # Quiet companion has 1 milestone while noisy companion has 20 events
            events.append({
                "source_id": 55_000,
                "guid": 102,
                "character_id": "kelda",
                "event_type": "boss_kill",
                "summary": "Defeated Edwin VanCleef",
                "created_at": 1_800_000_100,
            })

            res1 = mem.ingest_events(events, now_ts=1_800_005_000, min_available_source_id=50)
            self.assertEqual(res1["inserted"], 22)
            self.assertTrue(res1["gap_detected"])

            # Duplicate replay must insert 0 rows!
            res2 = mem.ingest_events(events, now_ts=1_800_005_100)
            self.assertEqual(res2["inserted"], 0)

            # Milestone survives >12 later routine events & restarts (fresh MemoryEngine instance)
            mem2 = MemoryEngine(Path(tmp) / "brain.db")
            b_ms = mem2.get_character_milestones("brannoc", limit=5)
            self.assertTrue(any("Reached Level 25" in m for m in b_ms))
            k_ms = mem2.get_character_milestones("kelda", limit=5)
            self.assertTrue(any("Edwin VanCleef" in m for m in k_ms))

    # ------------------------------------------------------------------
    # Issue #5: Goal-specific observed outcomes & non-overlapping attribution
    # ------------------------------------------------------------------
    def test_issue5_goal_specific_outcomes_and_non_overlapping_deltas(self):
        # 1. XP without level-up is progressed (not stalled)
        self.assertEqual(
            classify_plan_outcome({
                "applied": 1, "plan_state": "applied", "primary_goal_id": "quest:zone_1",
                "start_level": 20, "outcome_levels": 0, "outcome_xp": 450,
            }),
            "progressed",
        )
        # 2. Gathering at skill cap with ledger deposits is progressed (not stalled)
        self.assertEqual(
            classify_plan_outcome({
                "applied": 1, "plan_state": "applied", "primary_goal_id": "gather:mining",
                "start_level": 30, "outcome_skillups": 0, "outcome_ledger_ops": 3,
            }),
            "progressed",
        )
        # 3. Interrupted or expired/rejected proposals are NOT failed attempts
        self.assertEqual(
            classify_plan_outcome({
                "applied": 1, "plan_state": "applied", "primary_goal_id": "guild_order:77:stone",
                "proposal_status": "interrupted", "start_level": 30,
            }),
            "interrupted",
        )
        self.assertEqual(
            classify_plan_outcome({
                "applied": 1, "plan_state": "applied", "primary_goal_id": "guild_order:77:stone",
                "proposal_status": "expired", "start_level": 30,
            }),
            "not_attempted",
        )
        # 4. Advisory goals suppress behavioral stall penalty
        self.assertEqual(
            classify_plan_outcome({
                "applied": 1, "plan_state": "applied", "primary_goal_id": "dungeon:prep",
                "advisory_only": 1, "start_level": 30,
            }),
            "unknown",
        )
        # 5. True verified stall on non-advisory goal with 0 progress
        self.assertEqual(
            classify_plan_outcome({
                "applied": 1, "plan_state": "applied", "primary_goal_id": "craft:blacksmithing",
                "start_level": 30, "outcome_skillups": 0, "outcome_orders": 0, "outcome_ledger_ops": 0,
            }),
            "stalled",
        )

        # 6. Overlapping plan non-double-credit check
        with tempfile.TemporaryDirectory() as tmp:
            store = BrainStore(Path(tmp) / "brain.db")
            world = _sample_world()
            comp = world["companions"]["brannoc"]
            stub = {
                "character_id": "brannoc",
                "primary_goal_id": "quest:zone_1",
                "secondary_goal_ids": [],
                "weight_modifiers": {"quest": 1.2},
                "session_minutes": 60,
                "reason_code": "q1",
            }
            p1 = store.record_plan(
                now_ts=1_800_000_000, slug="brannoc", guid=101, source="fallback", model="fallback",
                effort="", plan=stub, primary_desc="Q1", cpp_weights={"quest_work": 1.2}, meta={},
                applied=True, error_note="", observed_at="2026-11-24T20:00:00Z",
                start_level=30, start_xp=1200, start_skill_sum=272, start_orders_done=0,
                start_quests_done=10, start_ledger_ops=5,
            )
            # Finalize p1 with +300 xp BEFORE p2 starts
            comp_mid = copy.deepcopy(comp)
            comp_mid["xp"] = 1500
            store.finalize_character_open_plans("brannoc", comp_mid, 1_800_001_800)

            p2 = store.record_plan(
                now_ts=1_800_001_800, slug="brannoc", guid=101, source="fallback", model="fallback",
                effort="", plan=stub, primary_desc="Q2", cpp_weights={"quest_work": 1.2}, meta={},
                applied=True, error_note="", observed_at="2026-11-24T20:30:00Z",
                start_level=30, start_xp=1500, start_skill_sum=272, start_orders_done=0,
                start_quests_done=10, start_ledger_ops=5,
            )
            # Later gain +900 xp during p2 must ONLY credit p2, NEVER inflate finalized p1!
            comp_end = copy.deepcopy(comp_mid)
            comp_end["xp"] = 2400
            store.refresh_outcomes({"brannoc": comp_end}, 1_800_003_600)
            with store._conn() as db:
                r1 = db.execute("SELECT outcome_xp, outcome_finalized FROM plans WHERE id=?", (p1,)).fetchone()
                r2 = db.execute("SELECT outcome_xp FROM plans WHERE id=?", (p2,)).fetchone()
            self.assertEqual(int(r1["outcome_xp"]), 300)
            self.assertEqual(int(r1["outcome_finalized"]), 1)
            self.assertEqual(int(r2["outcome_xp"]), 900)

    # ------------------------------------------------------------------
    # Issue #6: Expiring overrides preserve canonical baseline & monotonic revision
    # ------------------------------------------------------------------
    def test_issue6_override_sql_expiry_and_monotonic_revision(self):
        world = _sample_world()
        snap = build_snapshot(world["companions"]["brannoc"], world)
        plan = {
            "character_id": "brannoc",
            "primary_goal_id": "craft:blacksmithing",
            "secondary_goal_ids": [],
            "weight_modifiers": {"craft": 1.4},
            "session_minutes": 45,
            "reason_code": "craft_bs",
        }
        stmts = []
        apply_plan_to_db(
            101, "Brannoc", plan, snap, "anthropic",
            plan_id=10, now_ts=1_800_000_000, sql_exec=lambda s: stmts.append(s) or [],
        )
        sql = stmts[0]
        self.assertIn("START TRANSACTION;", sql)
        self.assertIn("companion_weight_overrides", sql)
        self.assertIn("IF(VALUES(`revision`) >= `revision`", sql)
        self.assertIn(str(1_800_000_000 + 45 * 60), sql)  # expires_at
        self.assertNotIn("INSERT INTO companion_weights ", sql)

    # ------------------------------------------------------------------
    # Issue #7: Strict shadow isolation for individual planner & quartermaster
    # ------------------------------------------------------------------
    def test_issue7_shadow_isolation_planner_and_quartermaster(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            db_path = tmp_p / "brain.db"
            rt_dir = tmp_p / "runtime"
            world = _sample_world()

            # Write a sentinel live active_plans.json and quartermaster.json
            rt_dir.mkdir(parents=True, exist_ok=True)
            live_ap = rt_dir / "active_plans.json"
            live_qm = rt_dir / "quartermaster.json"
            live_ap.write_text('{"sentinel":"live_plans"}\n', encoding="utf-8")
            live_qm.write_text('{"sentinel":"live_qm"}\n', encoding="utf-8")

            # Run shadow individual cycle -> must NOT touch active_plans.json, intentions, active_plan, or reservations!
            res = run_cycle(
                provider="fallback",
                apply=False,
                force=True,
                db_path=db_path,
                runtime_dir=rt_dir,
                world_ctx_override=world,
                now_ts_override=1_800_000_000,
            )
            self.assertEqual(res["planned_count"], 1)
            self.assertEqual(json.loads(live_ap.read_text())["sentinel"], "live_plans")
            self.assertTrue((rt_dir / "shadow_plans.json").is_file())

            store = BrainStore(db_path)
            mem = MemoryEngine(db_path)
            self.assertIsNone(store.active_plan("brannoc", 1_800_000_000))
            self.assertEqual(mem.active_order_reservations(1_800_000_000), {})
            self.assertEqual(mem.all_open_intentions(), {})

            # Run shadow quartermaster -> must NOT overwrite live quartermaster.json!
            from hearthcore.guides import compute_supply_report
            chars = [{"guid": 1, "name": "Wonky", "level": 30, "online": True, "skills": {333: (100, 150), 185: (138, 150)}}]
            sup = compute_supply_report(chars, {2840: 10})
            qm_state = {"characters": chars, "vault_counts": {2840: 10}, "open_orders": [], "supply": sup}
            qm_res = run_quartermaster(
                provider="fallback",
                apply=False,
                db_path=db_path,
                runtime_dir=rt_dir,
                state_override=qm_state,
            )
            self.assertTrue(qm_res["is_shadow"])
            self.assertEqual(json.loads(live_qm.read_text())["sentinel"], "live_qm")
            self.assertTrue((rt_dir / "quartermaster_shadow.json").is_file())

    # ------------------------------------------------------------------
    # Issue #8: Shared transactional API budget, rejected-response usage,
    #           concurrency, uncertain timeouts, month rollover & cap exhaustion
    # ------------------------------------------------------------------
    def test_issue8_shared_budget_usage_concurrency_rejection_rollover(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "brain.db"
            ledger = BudgetLedger(db_path)

            # 1. Thinking tokens must NOT be double-counted (already part of output_tokens)
            c1 = estimate_cost_usd("claude-sonnet-5-5", 1000, 500)
            self.assertAlmostEqual(c1, (1000 * 3.0 + 500 * 15.0) / 1_000_000.0, places=6)

            # 2. Rejected JSON/invalid goal response MUST still record billable usage!
            world = _sample_world()
            snap = build_snapshot(world["companions"]["brannoc"], world)
            prof = profile("brannoc")

            def bad_goal_post(_payload):
                return {
                    "model": "claude-sonnet-5-5",
                    "content": [{"type": "text", "text": '{"character_id":"brannoc","primary_goal_id":"invented:illegal"}'}],
                    "usage": {"input_tokens": 1800, "output_tokens": 220},
                }

            with self.assertRaises(InvalidWithUsage) as ctx:
                anthropic_with_meta(
                    prof, snap, model="claude-sonnet-5-5", effort="medium",
                    budget=ledger, caller="planner", http_post_fn=bad_goal_post,
                )
            self.assertEqual(ctx.exception.meta["input_tokens"], 1800)
            self.assertEqual(ctx.exception.meta["output_tokens"], 220)
            s_after_rej = ledger.summary()
            self.assertEqual(s_after_rej["month_rejected"], 1)
            self.assertGreater(s_after_rej["monthly_settled_usd"], 0.0)

            # 3. Transport timeout reserves conservatively as 'uncertain'
            def timeout_post(_payload):
                raise TimeoutError("timed out")

            with self.assertRaises(Exception):
                anthropic_with_meta(
                    prof, snap, model="claude-sonnet-5-5", effort="medium",
                    budget=ledger, caller="planner", http_post_fn=timeout_post,
                )
            s_after_to = ledger.summary()
            self.assertEqual(s_after_to["month_uncertain"], 1)
            self.assertGreater(s_after_to["monthly_reserved_usd"], 0.0)

            # 4. Concurrent atomic reservations against tight monthly cap -> never overspend!
            tight_ledger = BudgetLedger(Path(tmp) / "tight.db")
            # Each sonnet reserve (~1250 in + 1600 out) is ~$0.02775; cap at $0.06 allows exactly 2
            def try_reserve(_i):
                try:
                    tight_ledger.reserve(
                        caller="planner", model="claude-sonnet-5-5",
                        est_input_tokens=1000, max_output_tokens=1600,
                        now_ts=1_800_000_000, monthly_usd_cap=0.06, daily_call_cap=100,
                    )
                    return True
                except BudgetExceeded:
                    return False

            with ThreadPoolExecutor(max_workers=6) as ex:
                results = list(ex.map(try_reserve, range(8)))
            self.assertEqual(sum(results), 2)

            # 5. Month rollover resets monthly ceiling cleanly!
            next_month_ts = 1_800_000_000 + 35 * 86400
            rid = tight_ledger.reserve(
                caller="quartermaster", model="claude-sonnet-5-5",
                est_input_tokens=1000, max_output_tokens=1600,
                now_ts=next_month_ts, monthly_usd_cap=0.06, daily_call_cap=100,
            )
            self.assertGreater(rid, 0)

    # ------------------------------------------------------------------
    # Issue #9: Azeroth Watch admin auth, CSRF/Origin, size cap, audit log
    # ------------------------------------------------------------------
    def test_issue9_azeroth_watch_admin_auth_csrf_and_audit(self):
        aw_dir = "/home/wonky/azeroth-watch"
        if aw_dir not in sys.path:
            sys.path.insert(0, aw_dir)
        import server as aw_server  # type: ignore

        class DummyCollector:
            def get_snapshot(self):
                return 1, {"ai_brain": {"balance": {"spent_total_usd": 1.25}}}

            def refresh_once(self):
                return

        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, ".env")
            audit_path = os.path.join(tmp, "admin_audit.log")
            started_units = []
            tok = "test_super_secret_admin_token_123456"

            handler_cls = aw_server.make_handler(
                DummyCollector(),
                admin_token=tok,
                env_path=env_path,
                audit_path=audit_path,
                systemctl_runner=lambda u: started_units.append(u),
            )
            srv = aw_server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
            port = srv.server_address[1]
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            try:
                def req(method, path, body=None, headers=None):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    hdrs = {"Host": f"127.0.0.1:{port}"}
                    if body is not None:
                        hdrs["Content-Type"] = "application/json"
                    if headers:
                        hdrs.update(headers)
                    conn.request(method, path, body=body, headers=hdrs)
                    resp = conn.getresponse()
                    data = resp.read().decode("utf-8")
                    hdr_out = dict(resp.getheaders())
                    conn.close()
                    return resp.status, data, hdr_out

                # 1. Read-only GET remains accessible & never leaks token
                st, body, _ = req("GET", "/api/state")
                self.assertEqual(st, 200)
                self.assertNotIn(tok, body)

                # 2. Unauthenticated POST is rejected with 401
                st, _, _ = req("POST", "/api/brain/model", json.dumps({"model": "claude-sonnet-5-5"}))
                self.assertEqual(st, 401)

                # 3. Cross-origin POST is rejected with 403 even WITH valid token!
                st, _, _ = req(
                    "POST",
                    "/api/brain/model",
                    json.dumps({"model": "claude-sonnet-5-5"}),
                    headers={"Origin": "http://evil.example", "X-Azeroth-Admin-Token": tok},
                )
                self.assertEqual(st, 403)

                # 4. Oversized payload rejected with 413
                st, _, _ = req(
                    "POST",
                    "/api/brain/model",
                    json.dumps({"model": "x" * 5000}),
                    headers={"X-Azeroth-Admin-Token": tok},
                )
                self.assertEqual(st, 413)

                # 5. Auth login issues HttpOnly SameSite=Strict cookie & enables authorized POST
                st, _, hdrs = req("POST", "/api/brain/auth", json.dumps({"token": tok}))
                self.assertEqual(st, 200)
                set_cookie = hdrs.get("Set-Cookie") or ""
                self.assertIn("HttpOnly", set_cookie)
                self.assertIn("SameSite=Strict", set_cookie)
                cookie_pair = set_cookie.split(";", 1)[0]

                st, body, _ = req(
                    "POST",
                    "/api/brain/model",
                    json.dumps({"model": "claude-sonnet-5-5"}),
                    headers={"Cookie": cookie_pair, "Origin": f"http://127.0.0.1:{port}"},
                )
                self.assertEqual(st, 200)
                self.assertIn("claude-sonnet-5-5", body)
                self.assertIn("ANTHROPIC_MODEL=claude-sonnet-5-5", Path(env_path).read_text())

                # 6. Audit log recorded actions without leaking the secret token
                audit_txt = Path(audit_path).read_text()
                self.assertIn("model=claude-sonnet-5-5", audit_txt)
                self.assertNotIn(tok, audit_txt)
            finally:
                srv.shutdown()
                srv.server_close()
