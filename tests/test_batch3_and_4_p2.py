"""Comprehensive regression tests for all 9 zero-downtime P2 issues (#57, #60, #61, #62, #64, #65, #72, #74, #75)."""
from __future__ import annotations

import http.client
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hearthcore.bridge import (
    BrainStore,
    NATIVE_MERGE_RULE,
    PROFILE_PREF_TO_NATIVE_WEIGHTS,
    is_fallback_holdout,
    load_fresh_quartermaster_directives,
    parse_mysql_tsv,
    persona_baseline_weights,
    reconcile_state,
    replay_snapshot,
    run_cycle,
    sync_event_journal,
    weights_for_cpp,
)
from hearthcore.core import ROOT, fallback, profile, snapshot
from hearthcore.memory import MemoryEngine
from hearthcore.opportunities import check_resource_headroom, evaluate_persisted_headroom


class Batch3And4P2Tests(unittest.TestCase):
    # ------------------------------------------------------------------
    # Issue #57: Event journal cursor advances past filtered pages, detects resets & pruning gaps on every run
    # ------------------------------------------------------------------
    def test_issue57_journal_cursor_filtered_pages_reset_and_post_first_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = MemoryEngine(Path(tmp) / "brain.db")
            guid_to_slug = {101: "brannoc"}

            # 1. Page of ONLY non-companion rows (guid=999) MUST still advance persisted cursor!
            def sql_filtered(sql: str):
                if "MIN(id)" in sql:
                    return [["1", "10"]]
                if "WHERE id > 0" in sql:
                    return [[str(i), "999", "level_up", "other", "0", "12", "1800000000"] for i in range(1, 11)]
                return []

            info1 = sync_event_journal(mem, guid_to_slug, 1_800_000_000, sql_exec=sql_filtered)
            self.assertEqual(info1["cursor"], 10)
            self.assertEqual(mem.get_event_cursor(), 10)
            self.assertEqual(info1["journal_lag"], 0)

            # 2. Pruning gap AFTER first sync (min_avail jumps from 10 to 50) MUST set gap_detected!
            def sql_pruned(sql: str):
                if "MIN(id)" in sql:
                    return [["50", "51"]]
                if "WHERE id > 10" in sql:
                    return [["50", "101", "level_up", "Brannoc reached 30", "0", "12", "1800000100"]]
                return []

            info2 = sync_event_journal(mem, guid_to_slug, 1_800_000_100, sql_exec=sql_pruned)
            self.assertTrue(info2["gap_detected"])
            self.assertEqual(info2["cursor"], 50)
            self.assertEqual(info2["inserted"], 1)

            # 3. Truncated / counter-reset source table (max_id=2 < cursor=50) rewinds cursor & ingests new rows!
            def sql_reset(sql: str):
                if "MIN(id)" in sql:
                    return [["1", "2"]]
                if "WHERE id > 0" in sql:
                    return [["2", "101", "boss_kill", "Hogger slain", "0", "12", "1800000200"]]
                return []

            info3 = sync_event_journal(mem, guid_to_slug, 1_800_000_200, sql_exec=sql_reset)
            self.assertEqual(info3["cursor"], 2)
            self.assertEqual(info3["inserted"], 1)
            self.assertTrue(mem.journal_status()["gap_detected"])

    # ------------------------------------------------------------------
    # Issue #60: Canonical weight namespace, persona baseline preservation & sync manual protection
    # ------------------------------------------------------------------
    def test_issue60_weight_namespace_persona_preservation_and_sync_protection(self):
        import importlib.util
        from hearthcore.core import profile as load_profile, profiles as list_profiles

        # 1. Single canonical key namespace between ops/hearthcore-sync.py and hearthcore/bridge.py
        sync_path = ROOT / "ops" / "hearthcore-sync.py"
        spec = importlib.util.spec_from_file_location("hearthcore_sync_mod", sync_path)
        assert spec and spec.loader
        sync_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sync_mod)
        self.assertEqual(sync_mod.KEY_MAP, PROFILE_PREF_TO_NATIVE_WEIGHTS)

        # 2. Verify actual mirrored CompanionBrain.cpp implements `src == "manual"` precedence + REPLACE overrides
        cpp_path = ROOT / "mod-playerbots-companions" / "src" / "Bot" / "CompanionBrain.cpp"
        cpp_txt = cpp_path.read_text(encoding="utf-8")
        self.assertIn('if (src == "manual")', cpp_txt)
        self.assertIn("manualKeys[guid].insert(key);", cpp_txt)
        self.assertIn("rows[guid][key] = std::clamp(val, WEIGHT_MIN_FACTOR, WEIGHT_MAX_FACTOR);", cpp_txt)

        # 3. Verify EVERY character profile in characters/*/profile.json preserves unmentioned persona
        #    preferences across weights_for_cpp() instead of flattening to 1.0!
        snap = {
            "character_id": "testbot",
            "observed_at": "2026-11-25T16:00:00Z",
            "candidates": [{"id": "quest:zone_12", "type": "quest", "score": 80, "description": "Quest"}],
        }
        plan = {
            "character_id": "testbot",
            "primary_goal_id": "quest:zone_12",
            "secondary_goal_ids": [],
            "weight_modifiers": {"quest": 1.2},  # deliberately omits fish/craft/gather/train/bank/travel/rest/repair
            "session_minutes": 60,
            "reason_code": "quest_push",
        }
        all_slugs = list_profiles()
        self.assertGreaterEqual(len(all_slugs), 30)
        for slug in all_slugs:
            prof = load_profile(slug)
            base = persona_baseline_weights(prof)
            w = weights_for_cpp(plan, snap, char_profile=prof)
            for k in ("fish", "craft", "gather", "train", "bank", "travel", "hearth", "repair", "sell"):
                self.assertAlmostEqual(
                    w[k], base[k], places=4,
                    msg=f"Profile {slug} key {k} flattened from baseline {base[k]} to {w[k]}",
                )

        # 4. Behavioral check on build_sync_sql() default vs --overwrite-manual
        sample_prof = [{"name": "Brannoc", "slug": "brannoc", "preferences": {"craft": 1.35, "gather": 1.25}}]
        sample_existing = {"brannoc": {"guid": 101, "level": 33}}
        default_sql, _, _ = sync_mod.build_sync_sql(sample_prof, sample_existing, overwrite_manual=False, db_name="acore_characters_test")
        overwrite_sql, _, _ = sync_mod.build_sync_sql(sample_prof, sample_existing, overwrite_manual=True, db_name="acore_characters_test")
        self.assertEqual(len(default_sql), 2)
        self.assertTrue(all("IF(`source` = 'hearthcore'" in s for s in default_sql))
        self.assertTrue(all("IF(`source`" not in s for s in overwrite_sql))

    # ------------------------------------------------------------------
    # Issue #61: Fail-closed headroom + persisted hysteresis + stale Quartermaster directives
    # ------------------------------------------------------------------
    def test_issue61_fail_closed_headroom_hysteresis_and_stale_qm_directives(self):
        # Missing telemetry MUST fail closed (admit_ok=False)!
        h_miss = check_resource_headroom(mem_avail_mib=None, tick_p99_ms=None, active_managed_groups=0)
        self.assertFalse(h_miss["admit_ok"])
        self.assertIn("missing_ram_telemetry", h_miss["reasons"])
        self.assertIn("missing_tick_telemetry", h_miss["reasons"])

        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            st_path = tp / "headroom_state.json"
            # Drop below 512 MiB -> pauses and persists paused=True
            r1 = evaluate_persisted_headroom(st_path, mem_avail_mib=480.0, tick_p99_ms=20.0, active_managed_groups=0, now_ts=1000)
            self.assertFalse(r1["admit_ok"])
            # Recover to 580 MiB (< 640 hysteresis threshold) -> stays paused across invocations!
            r2 = evaluate_persisted_headroom(st_path, mem_avail_mib=580.0, tick_p99_ms=20.0, active_managed_groups=0, now_ts=1060)
            self.assertFalse(r2["admit_ok"])
            self.assertTrue(r2["prev_paused"])
            # Recover to 700 MiB (>= 640) -> admits cleanly!
            r3 = evaluate_persisted_headroom(st_path, mem_avail_mib=700.0, tick_p99_ms=20.0, active_managed_groups=0, now_ts=1120)
            self.assertTrue(r3["admit_ok"])

            # Stale quartermaster.json (> max_age_sec) is ignored cleanly
            qm_path = tp / "quartermaster.json"
            qm_path.write_text(
                json.dumps({
                    "updated_at": "2026-11-20T00:00:00Z",
                    "is_shadow": False,
                    "focus_skills": ["Mining"],
                    "bank_requests": [{"gather_skill": "Mining", "name": "Copper Ore"}],
                }),
                encoding="utf-8",
            )
            focus, mats, meta = load_fresh_quartermaster_directives(qm_path, now_ts=1_795_631_200, max_age_sec=3600)
            self.assertTrue(meta["stale"])
            self.assertEqual(focus, set())
            self.assertEqual(mats, {})

            # Live cooperative admission guard enforces fail-closed headroom + persisted hysteresis
            from hearthcore.opportunities import OpportunityCoordinator, admit_cooperative_opportunity
            coord = OpportunityCoordinator(tp / "coop.db")
            opp = coord.create_opportunity(dungeon_id="deadmines_n", creator_guid=101, now_ts=2000, is_shadow=False)
            for idx, (slug, role) in enumerate((("brannoc", "tank"), ("mira", "healer"), ("talia", "damage"), ("garrick", "damage"), ("aldric", "damage")), start=1):
                coord.express_interest(
                    opportunity_id=opp["id"], expected_revision=1, persona_id=f"p-{slug}",
                    character_guid=200 + idx, character_slug=slug, role=role,
                    reason_code="volunteer", available_until=5000, now_ts=2000,
                )
            live_hr = tp / "live_headroom.json"
            blk_missing = admit_cooperative_opportunity(
                coord, opp["id"], expected_revision=1, state_path=live_hr,
                mem_avail_mib=None, tick_p99_ms=20.0, active_managed_groups=0, now_ts=2001,
            )
            self.assertFalse(blk_missing["ok"])
            self.assertEqual(blk_missing["reason"], "resource_headroom_blocked")
            # Hysteresis keeps live path paused at 580 MiB (<640)
            blk_hyst = admit_cooperative_opportunity(
                coord, opp["id"], expected_revision=1, state_path=live_hr,
                mem_avail_mib=580.0, tick_p99_ms=20.0, active_managed_groups=0, now_ts=2005,
            )
            self.assertFalse(blk_hyst["ok"])
            # Recovery >=640 MiB and <=38 ms admits and reserves party!
            admitted = admit_cooperative_opportunity(
                coord, opp["id"], expected_revision=1, state_path=live_hr,
                mem_avail_mib=700.0, tick_p99_ms=20.0, active_managed_groups=0, now_ts=2010,
            )
            self.assertTrue(admitted["ok"])
            self.assertEqual(len(admitted["party"]), 5)

            # Structured cycle_summary JSON log is emitted on hearthcore.bridge logger
            from hearthcore.bridge import run_cycle
            world = {
                "observed_at": "2026-11-25T16:00:00Z",
                "weekly_event": "",
                "companions": {
                    "brannoc": {
                        "guid": 101, "name": "Brannoc", "slug": "brannoc", "level": 30,
                        "xp": 0, "zone_id": 10, "zone": "Duskwood", "map_id": 0, "gold": 25.0,
                        "online": True, "shift_group": "core-wonky",
                        "skills": {164: (100, 150), 186: (100, 150)},
                        "orders_delivered": 0, "quests_rewarded": 0, "ledger_ops": 0,
                        "recent_events": [], "social_bonds": [],
                    }
                },
                "work_orders": [], "humans": [], "owner_levels": {"wonky": 30},
                "qm_focus": set(), "qm_mats_by_skill": {}, "qm_meta": {}, "proposals": {},
            }
            with self.assertLogs("hearthcore", level="INFO") as logs:
                run_cycle(
                    provider="fallback", apply=False, character="brannoc",
                    db_path=tp / "brain.db", runtime_dir=tp / "rt",
                    world_ctx_override=world, sql_exec=lambda _s: [], now_ts_override=1_800_000_000,
                )
            self.assertTrue(any("cycle_summary" in msg for msg in logs.output))

        # AST invariant: ZERO silent `except ...: pass` handlers anywhere in hearthcore/*.py!
        import ast
        silent_handlers = []
        for py in sorted((ROOT / "hearthcore").glob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                        silent_handlers.append(f"{py.name}:{node.lineno}")
        self.assertEqual(silent_handlers, [], f"Found silent except:pass handlers: {silent_handlers}")

    # ------------------------------------------------------------------
    # Issue #62: TSV parser handles NULL, tab, newline, U+2028 & fails loudly on malformed rows
    # ------------------------------------------------------------------
    def test_issue62_robust_tsv_parser_and_prompt_boundary(self):
        # U+2028 (Line Separator), \x0b, \t escaped as \\t, \n escaped as \\n, SQL NULL
        raw = "101\tHello\\tWorld\\nLine2\u2028StillSameRow\tNULL\n"
        rows = parse_mysql_tsv(raw, expected_cols=3)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "101")
        self.assertEqual(rows[0][1], "Hello\tWorld\nLine2\u2028StillSameRow")
        self.assertIsNone(rows[0][2])

        # Malformed row raises ValueError loudly!
        with self.assertRaises(ValueError):
            parse_mysql_tsv("101\tonly_two_cols\n", expected_cols=3)

        # Prompt injection boundary statement present in both provider & quartermaster
        self.assertIn("untrusted game data", (ROOT / "hearthcore" / "provider.py").read_text(encoding="utf-8"))
        self.assertIn("untrusted game data", (ROOT / "hearthcore" / "quartermaster.py").read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # Issue #64: Checked-in DDL, reconcile drift detection & offline replay harness
    # ------------------------------------------------------------------
    def test_issue64_ddl_reconcile_and_offline_replay(self):
        ddl = (ROOT / "ops" / "sql" / "companion_schema.sql").read_text(encoding="utf-8")
        for tbl in ("companion_weight_overrides", "companion_proposals", "companion_work_orders", "companion_bank_requests", "companion_events"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS `{tbl}`", ddl)

        # Offline no-network replay harness
        snap_raw = json.loads((ROOT / "examples" / "brannoc.json").read_text(encoding="utf-8"))
        rep_out = replay_snapshot("brannoc", snap_raw, guid=101, plan_id=42, now_ts=1_800_000_000)
        self.assertEqual(rep_out["character_id"], "brannoc")
        self.assertIn("START TRANSACTION;", rep_out["sql"])
        self.assertIn("companion_proposals", rep_out["sql"])

        # Reconcile detects orphan SQLite plan missing from MySQL proposals!
        with tempfile.TemporaryDirectory() as tmp:
            db_p = Path(tmp) / "brain.db"
            store = BrainStore(db_p)
            store.record_plan(
                now_ts=1_800_000_000, slug="brannoc", guid=101, source="anthropic",
                model="claude-sonnet-5-5", effort="medium", plan=rep_out["plan"],
                primary_desc="Test", cpp_weights=rep_out["cpp_weights"], meta={},
                applied=True, error_note="", observed_at="2026-11-25T16:00:00Z",
            )
            def empty_mysql(sql: str):
                if "MAX(id)" in sql:
                    return [["0"]]
                return []
            rec = reconcile_state(db_path=db_p, sql_exec=empty_mysql, now_ts=1_800_000_060)
            self.assertFalse(rec["ok"])
            self.assertEqual(len(rec["orphan_plans"]), 1)

    # ------------------------------------------------------------------
    # Issue #65: Rotated fallback holdout & A/B outcome/cost report
    # ------------------------------------------------------------------
    def test_issue65_fallback_holdout_and_ab_report(self):
        # 100% holdout -> True; 0% holdout -> False
        self.assertTrue(is_fallback_holdout("brannoc", "2026-11-25", holdout_pct=100))
        self.assertFalse(is_fallback_holdout("brannoc", "2026-11-25", holdout_pct=0))

        with tempfile.TemporaryDirectory() as tmp:
            db_p = Path(tmp) / "brain.db"
            store = BrainStore(db_p)
            snap_raw = json.loads((ROOT / "examples" / "brannoc.json").read_text(encoding="utf-8"))
            plan = fallback(profile("brannoc"), snapshot(snap_raw, "brannoc"))
            pid_ai = store.record_plan(
                now_ts=1_800_000_000, slug="brannoc", guid=101, source="anthropic",
                model="claude-sonnet-5-5", effort="medium", plan=plan,
                primary_desc="AI test", cpp_weights={"craft": 1.3},
                meta={"input_tokens": 1000, "output_tokens": 400},
                applied=True, error_note="", observed_at="2026-11-25T16:00:00Z",
                start_level=30, start_skill_sum=100,
            )
            pid_ho = store.record_plan(
                now_ts=1_800_000_000, slug="kelda", guid=102, source="fallback_holdout",
                model="fallback_holdout", effort="", plan=plan,
                primary_desc="Holdout test", cpp_weights={"craft": 1.2}, meta={},
                applied=True, error_note="", observed_at="2026-11-25T16:00:00Z",
                start_level=30, start_skill_sum=100,
            )
            with store._conn() as db:
                db.execute("UPDATE plans SET outcome_skillups = 5 WHERE id = ?", (pid_ai,))
                db.execute("UPDATE plans SET outcome_skillups = 0 WHERE id = ?", (pid_ho,))

            report = store.ab_outcome_report(days=14, now_ts=1_800_000_100)
            self.assertEqual(report["holdout_pct"], 0)
            self.assertIn("anthropic", report["by_source"])
            self.assertIn("fallback_holdout", report["by_source"])
            self.assertGreater(report["by_source"]["anthropic"]["cost_per_session_usd"], 0.0)
            self.assertEqual(report["by_source"]["fallback_holdout"]["cost_per_session_usd"], 0.0)
            self.assertEqual(report["recommendation"]["status"], "insufficient_data")

            # Verify recommend_planner_tuning covers all 4 decision regimes
            from hearthcore.bridge import recommend_planner_tuning
            r_strong = recommend_planner_tuning({
                "holdout_pct": 10,
                "by_source": {
                    "anthropic": {"sessions": 20, "progress_rate": 0.80, "cost_per_session_usd": 0.005},
                    "fallback_holdout": {"sessions": 15, "progress_rate": 0.60, "cost_per_session_usd": 0.0},
                },
            })
            self.assertEqual(r_strong["status"], "strong_ai_lift")
            self.assertEqual(r_strong["lift_pp"], 20.0)

            r_marg = recommend_planner_tuning({
                "holdout_pct": 10,
                "by_source": {
                    "anthropic": {"sessions": 20, "progress_rate": 0.65, "cost_per_session_usd": 0.01},
                    "fallback_holdout": {"sessions": 15, "progress_rate": 0.60, "cost_per_session_usd": 0.0},
                },
            })
            self.assertEqual(r_marg["status"], "marginal_lift_optimize_cost")

            r_none = recommend_planner_tuning({
                "holdout_pct": 10,
                "by_source": {
                    "anthropic": {"sessions": 20, "progress_rate": 0.50, "cost_per_session_usd": 0.01},
                    "fallback_holdout": {"sessions": 15, "progress_rate": 0.55, "cost_per_session_usd": 0.0},
                },
            })
            self.assertEqual(r_none["status"], "no_measured_lift")

    # ------------------------------------------------------------------
    # Issue #72: Dashboard non-ASCII token safety, session revocation, CSP, separate 0600 overrides & collector signaling
    # ------------------------------------------------------------------
    def test_issue72_dashboard_robustness_revocation_csp_and_separate_overrides(self):
        aw_dir = str((ROOT / "azeroth-watch").resolve())
        if aw_dir not in sys.path:
            sys.path.insert(0, aw_dir)
        import server as aw_server  # type: ignore

        refresh_signaled = []

        class DummyCollector:
            def get_snapshot(self):
                return 7, {"db_ok": True, "data_age_sec": 2, "ai_brain": {"balance": {"spent_total_usd": 1.0}}}

            def request_refresh(self):
                refresh_signaled.append(True)

            def refresh_once(self):
                raise AssertionError("HTTP handler must signal collector thread, not call refresh_once synchronously!")

        with tempfile.TemporaryDirectory() as tmp:
            api_env = Path(tmp) / ".env"
            api_env.write_text("ANTHROPIC_API_KEY=do_not_touch_me\n", encoding="utf-8")
            overrides = Path(tmp) / "dashboard_overrides.env"
            tok = "issue72_secret_admin_token_xyz"
            handler_cls = aw_server.make_handler(
                DummyCollector(),
                admin_token=tok,
                env_path=str(api_env),
                overrides_path=str(overrides),
                audit_path=os.path.join(tmp, "audit.log"),
                allowed_hosts={"127.0.0.1", "localhost"},
            )
            srv = aw_server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
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
                    out_h = dict(resp.getheaders())
                    conn.close()
                    return resp.status, data, out_h

                # 1. CSP header present & db_ok/data_age_sec propagated
                st, body, hdrs = req("GET", "/api/state")
                self.assertEqual(st, 200)
                self.assertIn("default-src 'self'", hdrs.get("Content-Security-Policy", ""))
                self.assertTrue(json.loads(body)["db_ok"])

                # 2. Non-ASCII token does NOT raise 500 TypeError (clean 401!)
                st_na, _, _ = req("POST", "/api/brain/auth", json.dumps({"token": "bad-token-\u2603"}))
                self.assertEqual(st_na, 401)

                # 3. Login -> change model -> writes separate 0600 overrides file, NEVER touches .env, signals collector!
                st_in, _, hdrs_in = req("POST", "/api/brain/auth", json.dumps({"token": tok}))
                self.assertEqual(st_in, 200)
                cookie_pair = hdrs_in["Set-Cookie"].split(";", 1)[0]

                st_m, _, _ = req(
                    "POST", "/api/brain/model", json.dumps({"model": "claude-sonnet-5-5"}),
                    headers={"Cookie": cookie_pair, "Origin": f"http://127.0.0.1:{port}"},
                )
                self.assertEqual(st_m, 200)
                self.assertEqual(len(refresh_signaled), 1)
                self.assertEqual(api_env.read_text(encoding="utf-8"), "ANTHROPIC_API_KEY=do_not_touch_me\n")
                self.assertTrue(overrides.is_file())
                self.assertEqual(stat.S_IMODE(overrides.stat().st_mode), 0o600)
                self.assertIn("ANTHROPIC_MODEL=claude-sonnet-5-5", overrides.read_text(encoding="utf-8"))

                # 4. Logout REVOKES server-side session immediately (reusing old cookie -> 401!)
                st_out, _, _ = req(
                    "POST", "/api/brain/logout", "{}",
                    headers={"Cookie": cookie_pair, "Origin": f"http://127.0.0.1:{port}"},
                )
                self.assertEqual(st_out, 200)
                st_revoked, _, _ = req(
                    "POST", "/api/brain/model", json.dumps({"model": "claude-haiku-5-5"}),
                    headers={"Cookie": cookie_pair, "Origin": f"http://127.0.0.1:{port}"},
                )
                self.assertEqual(st_revoked, 401)
            finally:
                srv.shutdown()
                srv.server_close()

    # ------------------------------------------------------------------
    # Issue #74 & #75: Safe sync secret gate, hardened units, maintenance flag & backup/restore scripts
    # ------------------------------------------------------------------
    def test_issue74_and_75_ops_scripts_and_systemd_hardening(self):
        # 1. Secret scan mode passes cleanly on repo
        res = subprocess.run(
            ["bash", str(ROOT / "ops" / "sync-monorepo.sh"), "--scan-only"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

        # 2. Maintenance override respected by heal-realmlist.sh
        with tempfile.TemporaryDirectory() as tmp:
            mflag = Path(tmp) / "maintenance.flag"
            mflag.write_text("maintenance\n", encoding="utf-8")
            env = dict(os.environ, HEARTHCORE_MAINTENANCE_FLAG=str(mflag))
            r_heal = subprocess.run(
                ["bash", str(ROOT / "ops" / "heal-realmlist.sh")],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(r_heal.returncode, 0)
            self.assertIn("Maintenance flag present", r_heal.stdout)

        # 3. Backup & restore-drill scripts present and valid bash syntax
        for script in ("backup.sh", "restore-drill.sh", "sync-monorepo.sh", "heal-realmlist.sh"):
            chk = subprocess.run(["bash", "-n", str(ROOT / "ops" / script)], capture_output=True, text=True)
            self.assertEqual(chk.returncode, 0, f"Syntax error in {script}: {chk.stderr}")

        # 4. Systemd hardening directives present
        for unit in ("azeroth-watch.service", "hearthcore-brain.service", "hearthcore-quartermaster.service", "hearthcore-perf-check.service"):
            utxt = (ROOT / "ops" / "systemd" / unit).read_text(encoding="utf-8")
            self.assertIn("NoNewPrivileges=yes", utxt, unit)
            self.assertIn("OnFailure=hearthcore-failure@%n.service", utxt, unit)
        self.assertTrue((ROOT / "ops" / "systemd" / "hearthcore-perf-check.timer").is_file())


if __name__ == "__main__":
    unittest.main()
