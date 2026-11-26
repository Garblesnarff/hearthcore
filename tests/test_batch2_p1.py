"""Comprehensive regression tests for Batch 2 zero-downtime P1 issues (#15, #23, #47, #70, #71)."""
from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from hearthcore.benchmark import build_benchmark_artifact
from hearthcore.bridge import _xp_cap_prof_catchup_state, build_snapshot, weights_for_cpp
from hearthcore.core import ROOT
from hearthcore.identity import persona_id_for_slug
from hearthcore.opportunities import (
    OpportunityCoordinator,
    read_shadow_artifact,
    run_shadow_match_snapshot,
)


class Batch2ZeroDowntimeP1Tests(unittest.TestCase):
    # ------------------------------------------------------------------
    # Issue #15: Strict Phase II shadow isolation (no DB mutation, no cross-blocking, corrupt/stale artifacts)
    # ------------------------------------------------------------------
    def test_issue15_shadow_does_not_mutate_db_or_block_live_reservations(self):
        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            db_p = tp / "brain.db"
            rt_dir = tp / "runtime"
            now = 1_800_000_000

            # 1. Shadow snapshot on non-existent brain.db MUST NOT create or mutate brain.db!
            roster = {
                "ironbulwark": {"guid": 201, "name": "Ironbulwark", "slug": "ironbulwark", "level": 24, "online": True, "shift_group": "early-birds"},
                "brannoc": {"guid": 101, "name": "Brannoc", "slug": "brannoc", "level": 24, "online": True, "shift_group": "core-wonky"},
            }
            out = run_shadow_match_snapshot(
                {"companions": roster, "work_orders": []},
                db_path=db_p,
                runtime_dir=rt_dir,
                vitals={"mem_avail_gib": 1.2, "tick_p99": 18.0},
                now_ts=now,
            )
            self.assertFalse(db_p.exists(), "Shadow match must NEVER create or write to live brain.db!")
            self.assertTrue(out["is_shadow"])
            self.assertTrue(out["synthetic_feasibility"])
            self.assertFalse(out["mutated_live_state"])

            # 2. Shadow reservation MUST NOT block a concurrent LIVE reservation for the same personas!
            coord = OpportunityCoordinator(db_p)
            o_shadow = coord.create_opportunity(dungeon_id="stockade_n", creator_guid=201, is_shadow=True, now_ts=now)
            o_live = coord.create_opportunity(dungeon_id="stockade_n", creator_guid=201, is_shadow=False, now_ts=now)
            party = [
                ("ironbulwark", 201, "tank"),
                ("voss", 202, "healer"),
                ("halvar", 203, "damage"),
                ("bryn", 204, "damage"),
                ("lyraeth", 205, "damage"),
            ]
            for opp in (o_shadow, o_live):
                for slug, guid, role in party:
                    self.assertTrue(
                        coord.express_interest(
                            opportunity_id=opp["id"], expected_revision=1,
                            persona_id=persona_id_for_slug(slug), character_guid=guid,
                            character_slug=slug, role=role, reason_code="test",
                            available_until=now + 3600, now_ts=now,
                        )
                    )
            r_sh = coord.reserve_party(o_shadow["id"], expected_revision=1, now_ts=now + 1)
            r_lv = coord.reserve_party(o_live["id"], expected_revision=1, now_ts=now + 1)
            self.assertTrue(r_sh["ok"])
            self.assertTrue(r_lv["ok"], "Shadow reservation must NEVER block live reservation!")

            # 3. Corrupted and stale shadow artifact handling
            sh_path = rt_dir / "phase2_shadow.json"
            self.assertEqual(read_shadow_artifact(sh_path, now_ts=now + 60)["status"], "fresh")
            self.assertEqual(read_shadow_artifact(sh_path, now_ts=now + 7200)["status"], "stale")
            sh_path.write_text("{corrupt json", encoding="utf-8")
            self.assertEqual(read_shadow_artifact(sh_path, now_ts=now + 60)["status"], "corrupted")
            sh_path.unlink()
            self.assertEqual(read_shadow_artifact(sh_path, now_ts=now + 60)["status"], "missing")

    # ------------------------------------------------------------------
    # Issue #23: Per-character owner-cap scoping, near-cap vs at-cap, trained cap, travel preserved
    # ------------------------------------------------------------------
    def test_issue23_per_character_xp_cap_scoping_and_trained_ceiling(self):
        world_ctx = {
            "observed_at": "2026-11-25T16:00:00Z",
            "weekly_event": "",
            "work_orders": [],
            "humans": [],
            "owner_levels": {"wonky": 30, "logan": 10},
            "qm_focus": set(),
            "qm_mats_by_skill": {},
        }

        # 1. Ordinary guild bot (day-crew) at Lv 33 MUST NOT inherit Wonky's cap!
        ordinary = {
            "guid": 250, "name": "Halvar", "slug": "halvar", "level": 33, "zone_id": 33, "zone": "STV",
            "shift_group": "day-crew", "skills": {164: (40, 75), 186: (45, 75)}, "recent_events": [], "social_bonds": [],
        }
        st_ord = _xp_cap_prof_catchup_state(ordinary, world_ctx)
        self.assertEqual(st_ord["mode"], "none")

        # 2. Separate owner: core-logan at Lv 13 (Logan 10 + 3) IS at_cap!
        logan_comp = {
            "guid": 110, "name": "Cinder", "slug": "cinder", "level": 13, "zone_id": 12, "zone": "Elwynn",
            "shift_group": "core-logan", "skills": {171: (20, 75), 182: (25, 75)}, "recent_events": [], "social_bonds": [],
        }
        st_logan = _xp_cap_prof_catchup_state(logan_comp, world_ctx)
        self.assertEqual(st_logan["mode"], "at_cap")
        self.assertEqual(st_logan["cap_level"], 13)

        # 3. 1 level below cap (core-wonky Lv 32 vs cap 33) -> near_cap, NOT at_cap (still earns XP!)
        near_comp = {
            "guid": 105, "name": "Aldric", "slug": "aldric", "level": 32, "zone_id": 33, "zone": "STV",
            "shift_group": "core-wonky", "skills": {171: (40, 75), 182: (35, 75)}, "recent_events": [], "social_bonds": [],
        }
        st_near = _xp_cap_prof_catchup_state(near_comp, world_ctx)
        self.assertEqual(st_near["mode"], "near_cap")
        snap_near = build_snapshot(near_comp, world_ctx)
        q_near = next(c for c in snap_near["candidates"] if c["id"] == "quest:zone_33")
        self.assertGreaterEqual(q_near["score"], 70)
        self.assertNotIn("0 XP", q_near["description"])

        # 4. At trained ceiling (75/75) -> boosts train:skills to 90 so bot visits trainer!
        capped_skill_comp = {
            "guid": 106, "name": "Kelda", "slug": "kelda", "level": 33, "zone_id": 33, "zone": "STV",
            "shift_group": "core-wonky", "skills": {165: (75, 75), 393: (75, 75)}, "recent_events": [], "social_bonds": [],
        }
        st_tr = _xp_cap_prof_catchup_state(capped_skill_comp, world_ctx)
        self.assertTrue(st_tr["needs_trainer"])
        snap_tr = build_snapshot(capped_skill_comp, world_ctx)
        t_cand = next(c for c in snap_tr["candidates"] if c["id"] == "train:skills")
        self.assertEqual(t_cand["score"], 90)

    # ------------------------------------------------------------------
    # Issue #47: Benchmark synthetic histogram, workload attribution & missing/stale telemetry failure
    # ------------------------------------------------------------------
    def test_issue47_benchmark_histogram_workload_and_missing_telemetry_fail(self):
        # 1. Known synthetic histogram -> exact p50 / p95 / p99 / max
        art_hist = build_benchmark_artifact(
            scenario="staging_synthetic",
            samples=[
                {
                    "connected_humans": 2,
                    "companions_online": 14,
                    "random_bots_online": 50,
                    "active_ai_bots": 64,
                    "managed_groups": 1,
                    "pending_groups": 0,
                    "active_instances": 1,
                    "worldserver_rss_mib": 2550.0,
                    "worldserver_cpu_pct": 28.5,
                    "mem_avail_mib": 1600.0,
                    "swap_used_mib": 120.0,
                }
            ],
            tick_histogram_ms=[(10.0, 90), (20.0, 5), (30.0, 4), (45.0, 1)],
            correctness={"worldserver_up": True},
            hearthcore_sha="abc1234",
            mod_playerbots_sha="def5678",
        )
        self.assertTrue(art_hist["all_correctness_passed"])
        self.assertEqual(art_hist["sampling"]["tick_distribution_method"], "raw_histogram")
        self.assertEqual(art_hist["metrics"]["tick_p50_ms"], 10.0)
        self.assertEqual(art_hist["metrics"]["tick_p95_ms"], 20.0)
        self.assertEqual(art_hist["metrics"]["tick_p99_ms"], 30.0)
        self.assertEqual(art_hist["metrics"]["tick_max_ms"], 45.0)
        self.assertEqual(art_hist["workload"]["connected_humans_max"], 2)
        self.assertEqual(art_hist["workload"]["logged_in_bots_max"], 64)
        self.assertEqual(art_hist["workload"]["active_instances_max"], 1)

        # 2. Missing tick telemetry -> MUST NOT fabricate pass!
        art_miss = build_benchmark_artifact(
            scenario="missing_telemetry",
            samples=[{"connected_humans": 0, "companions_online": 10}],
            correctness={"worldserver_up": True},
            hearthcore_sha="abc1234",
            mod_playerbots_sha="def5678",
        )
        self.assertFalse(art_miss["all_correctness_passed"])
        self.assertEqual(art_miss["telemetry_status"], "unknown_missing_or_stale")
        self.assertIsNone(art_miss["metrics"]["tick_p99_ms"])

    # ------------------------------------------------------------------
    # Issue #71: Dashboard Host allow-list, account stripping & unauth kiosk redaction
    # ------------------------------------------------------------------
    def test_issue71_dashboard_host_allowlist_and_kiosk_vs_admin_redaction(self):
        aw_dir = str((ROOT / "azeroth-watch").resolve())
        if aw_dir not in sys.path:
            sys.path.insert(0, aw_dir)
        import server as aw_server  # type: ignore

        class DummyCollector:
            def get_snapshot(self):
                return 1, {
                    "generated_at": "2026-11-25 16:00:00",
                    "errors_bytes": 0,
                    "last_error": "secret stacktrace line",
                    "humans": [{"guid": 1, "name": "Wonky", "level": 30, "account": "WONKY_LOGIN", "gold": 99.5}],
                    "ai_brain": {
                        "balance": {"remaining_usd": 95.00, "spent_total_usd": 3.00},
                        "active_list": [{"character_id": "brannoc", "active": True}],
                    },
                }

            def refresh_once(self):
                return

        with tempfile.TemporaryDirectory() as tmp:
            tok = "test_admin_token_issue71_secret"
            handler_cls = aw_server.make_handler(
                DummyCollector(),
                admin_token=tok,
                env_path=os.path.join(tmp, ".env"),
                audit_path=os.path.join(tmp, "audit.log"),
                allowed_hosts={"127.0.0.1", "localhost"},
            )
            srv = aw_server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                def get(path, host_hdr, extra_hdrs=None):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    hdrs = {"Host": host_hdr}
                    if extra_hdrs:
                        hdrs.update(extra_hdrs)
                    conn.request("GET", path, headers=hdrs)
                    resp = conn.getresponse()
                    body = resp.read().decode("utf-8")
                    conn.close()
                    return resp.status, body

                # 1. DNS-rebound Host header rejected with 403!
                st_bad, _ = get("/api/state", "attacker.evil:8787")
                self.assertEqual(st_bad, 403)

                # 2. Unauthenticated kiosk view strips account, last_error, gold, and API spend!
                st_pub, body_pub = get("/api/state", f"127.0.0.1:{port}")
                self.assertEqual(st_pub, 200)
                data_pub = json.loads(body_pub)
                self.assertEqual(data_pub["view_mode"], "kiosk")
                self.assertNotIn("WONKY_LOGIN", body_pub)
                self.assertNotIn("secret stacktrace", body_pub)
                self.assertNotIn("remaining_usd", body_pub)
                self.assertNotIn("gold", data_pub["humans"][0])
                self.assertTrue(data_pub["ai_brain"]["redacted"])

                # 3. Authenticated admin view gets ai_brain balance, STILL never leaks account username!
                st_adm, body_adm = get("/api/state", f"127.0.0.1:{port}", {"X-Azeroth-Admin-Token": tok})
                self.assertEqual(st_adm, 200)
                data_adm = json.loads(body_adm)
                self.assertEqual(data_adm["view_mode"], "admin")
                self.assertEqual(data_adm["ai_brain"]["balance"]["remaining_usd"], 95.0)
                self.assertNotIn("WONKY_LOGIN", body_adm)
            finally:
                srv.shutdown()
                srv.server_close()

    # ------------------------------------------------------------------
    # Issue #70: On-demand LAN share executes real installer end-to-end with out-of-band checksums,
    # redacted logs, single-use /wow.tar, and concurrent tar cap!
    # ------------------------------------------------------------------
    def test_issue70_lan_share_token_checksums_concurrency_and_no_thirdparty_pipe(self):
        import subprocess

        serve_path = ROOT / "ops" / "wow-lan-share" / "serve.py"
        setup_script_path = ROOT / "ops" / "wow-lan-share" / "setup-wow-laptop.sh"
        spec = importlib.util.spec_from_file_location("wow_lan_share_serve", serve_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Verify unit is on-demand only (no WantedBy, has RuntimeMaxSec)
        unit_txt = (ROOT / "ops" / "systemd" / "wow-lan-share.service").read_text(encoding="utf-8")
        self.assertNotIn("WantedBy=", unit_txt)
        self.assertIn("RuntimeMaxSec=", unit_txt)
        self.assertIn("Restart=no", unit_txt)
        self.assertNotIn("| bash", setup_script_path.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            fake_dll = tp / "d3d9.dll"
            fake_dll.write_bytes(b"FAKE_DXVK_DLL")
            fake_wow = tp / "wow_src"
            fake_wow.mkdir()
            (fake_wow / "Wow.exe").write_bytes(b"MZ_FAKE_WOW_BINARY")
            (fake_wow / "DivxDecoder.dll").write_bytes(b"MZ_FAKE_DLL")

            tok = "secret_one_time_lan_share_tok"
            lock = threading.Lock()
            access_logs: list[str] = []
            handler_cls = mod.make_handler(
                token=tok,
                setup_script=setup_script_path,
                wow_dir=fake_wow,
                dxvk_dll=fake_dll,
                tar_lock=lock,
                max_tar_uses=1,
                max_total_uses=12,
                log_sink=access_logs,
            )
            srv = mod.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                def get(path):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("GET", path)
                    resp = conn.getresponse()
                    data = resp.read()
                    hdrs = dict(resp.getheaders())
                    conn.close()
                    return resp.status, data, hdrs

                # 1. Unauthenticated / wrong token -> 403
                self.assertEqual(get("/setup")[0], 403)
                self.assertEqual(get("/wrongtok/setup")[0], 403)

                # 2. Fetch published manifest & compute out-of-band digest
                st_c, data_c, _ = get(f"/{tok}/checksums.sha256")
                self.assertEqual(st_c, 200)
                manifest_sha = hashlib.sha256(data_c).hexdigest()
                self.assertIn(b"wow/DivxDecoder.dll", data_c)
                self.assertIn(b"Wow.exe", data_c)

                # 3. Concurrent tar lock returns 429 when held
                self.assertTrue(lock.acquire(blocking=False))
                try:
                    self.assertEqual(get(f"/{tok}/wow.tar")[0], 429)
                finally:
                    lock.release()

                # 4. Run ACTUAL setup-wow-laptop.sh with WRONG out-of-band checksum -> MUST ABORT before install!
                bad_home = tp / "bad_home"
                bad_home.mkdir()
                bad_env = dict(
                    os.environ,
                    HOME=str(bad_home),
                    SERVER_IP="127.0.0.1",
                    SHARE_PORT=str(port),
                    SHARE_TOKEN=tok,
                    EXPECTED_CHECKSUMS_SHA256="0" * 64,
                    SKIP_SYSTEM_PACKAGES="1",
                    SKIP_WINE_INIT="1",
                )
                r_bad = subprocess.run(["bash", str(setup_script_path)], env=bad_env, capture_output=True, text=True)
                self.assertNotEqual(r_bad.returncode, 0)
                self.assertIn("Out-of-band SHA-256 mismatch", r_bad.stderr)
                self.assertFalse((bad_home / "WoW335a" / "Wow.exe").exists())

                # 5. Run ACTUAL setup-wow-laptop.sh with VALID out-of-band checksum -> downloads, extracts & verifies!
                good_home = tp / "good_home"
                good_home.mkdir()
                good_env = dict(
                    os.environ,
                    HOME=str(good_home),
                    SERVER_IP="127.0.0.1",
                    SHARE_PORT=str(port),
                    SHARE_TOKEN=tok,
                    EXPECTED_CHECKSUMS_SHA256=manifest_sha,
                    SKIP_SYSTEM_PACKAGES="1",
                    SKIP_WINE_INIT="1",
                )
                r_good = subprocess.run(["bash", str(setup_script_path)], env=good_env, capture_output=True, text=True)
                self.assertEqual(r_good.returncode, 0, f"stderr={r_good.stderr}\nstdout={r_good.stdout}")
                self.assertEqual((good_home / "WoW335a" / "Wow.exe").read_bytes(), b"MZ_FAKE_WOW_BINARY")
                self.assertEqual((good_home / "WoW335a" / "DivxDecoder.dll").read_bytes(), b"MZ_FAKE_DLL")
                self.assertIn("set realmlist 127.0.0.1", (good_home / "WoW335a" / "Data" / "enUS" / "realmlist.wtf").read_text())
                self.assertTrue((good_home / "wow.sh").is_file())

                # 6. Single-use /wow.tar: second download with same token MUST return 403!
                self.assertEqual(get(f"/{tok}/wow.tar")[0], 403)

                # 7. Access logs MUST NEVER contain the raw token!
                self.assertTrue(access_logs)
                for line in access_logs:
                    self.assertNotIn(tok, line, f"Token leaked into access log: {line}")
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
