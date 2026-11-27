"""Behavioral & security tests for Dedicated Character Sheet & 5 Companion Direction Controls."""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from hearthcore.bridge import BrainStore, run_cycle
from hearthcore.character_ctl import (
    CANONICAL_WEIGHT_KEYS,
    apply_focus_preset,
    create_character_work_order,
    fetch_character_sheet,
    set_character_ai_paused,
    set_character_directive,
    set_manual_weight,
    update_character_schedule,
)
from hearthcore.core import Invalid, ROOT


def _mock_sql_for_characters(recorded_sql: list[str], *, is_companion: bool = True, existing_overrides: bool = True):
    """Fake MySQL executor simulating realistic AzerothCore character sheet queries & operator writes."""
    def _exec(sql: str):
        recorded_sql.append(sql)
        s = " ".join(sql.split())
        if "FROM characters c LEFT JOIN companion_schedule cs" in s:
            if "'wonky'" in s.lower() or not is_companion:
                return [["1001", "Wonky", "3", "5", "0", "31", "10644", "1026183", "1537", "0", "1", "89555", "1407", "1900", "", "0", "24", "127", "0", "", "0"]]
            if "'brannoc'" in s.lower():
                return [["1556", "Brannoc", "4", "1", "1", "33", "3397", "761128", "1519", "0", "1", "744611", "1588", "0", "core-wonky", "0", "24", "127", "1", "Core tank", "1"]]
            return []
        if "FROM acore_world.player_xp_for_level" in s:
            return [["48600"]]
        if "FROM character_homebind" in s:
            return [["0", "1537"]]
        if "FROM character_inventory ci" in s:
            return [
                ["2", "42949", "Polished Spaulders of Valor", "7", "1", "1", "200", "3", "0 0 0", "0"],
                ["15", "44096", "Battleworn Thrash Blade", "7", "1", "1", "0", "13", "1898 0 0", "0"],
                ["6", "6087", "Chausses of Westfall", "3", "24", "19", "120", "7", "16 0 0", "80"],
            ]
        if "FROM character_stats" in s:
            return [["1588", "0", "92", "54", "110", "30", "35", "1450", "240", "85", "0", "12.5", "6.2", "4.0", "8.1", "5.0", "6.5", "0"]]
        if "FROM character_skills" in s:
            return [["164", "150", "150"], ["186", "120", "150"], ["356", "90", "150"]]
        if "FROM character_queststatus_rewarded" in s:
            return [["42"]]
        if "FROM character_queststatus qs" in s:
            return [["101", "1", "Defias Brotherhood", "22"], ["102", "3", "Bounty on Garrick Padfoot", "25"]]
        if "FROM companion_rep_goals" in s:
            return [["47", "Ironforge"]]
        if "FROM character_reputation" in s:
            return [["47", "11500", "1"], ["72", "4500", "1"]]
        if "FROM companion_profiles" in s:
            return [["Guardian", "dutiful", "Overprotects healers", "Steadfast dwarf-trained shieldbearer."]]
        if "FROM companion_weights WHERE" in s:
            return [["craft", "1.45", "manual"], ["gather", "1.10", "hearthcore"]]
        if "SELECT COUNT(*) FROM companion_weight_overrides" in s:
            return [["13" if existing_overrides else "0"]]
        if "FROM companion_weight_overrides WHERE" in s:
            return [
                ["craft", "0.80", "50", "50", "hearthcore_ai", "1", "1799999000", "1800003600"],
                ["gather", "1.35", "50", "50", "hearthcore_ai", "1", "1799999000", "1800003600"],
            ]
        if "SELECT GREATEST(" in s:
            return [["50"]]
        if "FROM companion_relationships" in s:
            return [["1556", "Brannoc", "1", "1557", "Aldric", "5", "12", "8", "45", "Cleared Stockades"]]
        if "FROM companion_events" in s:
            return [["level_up", "Brannoc reached level 33", "1519", "1800000000"]]
        if "FROM companion_ledger" in s:
            return [["2026-11-27 00:10:00", "deposit", "2840", "Copper Bar", "20", "Vault restock"]]
        if "FROM companion_work_orders" in s:
            return [["7", "Operator", "1556", "Brannoc", "2840", "Copper Bar", "10", "For guild", "claimed", "1800000000"]]
        if "SELECT name FROM acore_world.item_template" in s:
            return [["Copper Bar"]]
        return []
    return _exec


class CharacterSheetAndControlsTests(unittest.TestCase):
    def test_fetch_character_sheet_companion_and_human_and_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_p = Path(tmp) / "brain.db"
            store = BrainStore(db_p)
            store.set_operator_directive("brannoc", "Smelt bronze bars today!", now_ts=1_800_000_000)

            sql_log: list[str] = []
            sheet = fetch_character_sheet(
                "brannoc",
                sql_exec=_mock_sql_for_characters(sql_log, is_companion=True),
                db_path=db_p,
                now_ts=1_800_000_050,
                redact_admin=False,
                can_control=True,
            )
            self.assertIsNotNone(sheet)
            assert sheet is not None
            self.assertTrue(sheet["character"]["is_companion"])
            self.assertTrue(sheet["can_control"])
            self.assertEqual(sheet["character"]["name"], "Brannoc")
            self.assertEqual(sheet["character"]["homebind"]["zone"], "Ironforge")
            self.assertEqual(len(sheet["gear"]), 19)
            # Verify Enchanted detection on slot 15 (Main Hand)
            mh = next(s for s in sheet["gear"] if s["slot"] == 15)
            self.assertTrue(mh["item"]["enchanted"])
            # Verify trainer-cap detection on Blacksmithing (150/150 < 300)
            bs = next(p for p in sheet["professions"] if p["skill_id"] == 164)
            self.assertTrue(bs["at_trained_cap"])
            # Verify exact C++ weight merge: manual pin on 'craft' (1.45) beats override (0.80),
            # while non-manual 'gather' uses active override (1.35)!
            w_by_k = {w["key"]: w for w in sheet["weights"]}
            self.assertEqual(w_by_k["craft"]["effective"], 1.45)
            self.assertTrue(w_by_k["craft"]["pinned"])
            self.assertEqual(w_by_k["gather"]["effective"], 1.35)
            self.assertFalse(w_by_k["gather"]["pinned"])
            # Operator directive visible when not redacted
            self.assertEqual(sheet["ai_brain"]["operator_directive"]["note"], "Smelt bronze bars today!")

            # Redacted LAN kiosk mode hides directive & sets can_control=False
            sheet_ro = fetch_character_sheet(
                "brannoc",
                sql_exec=_mock_sql_for_characters([], is_companion=True),
                db_path=db_p,
                now_ts=1_800_000_050,
                redact_admin=True,
                can_control=False,
            )
            assert sheet_ro is not None
            self.assertFalse(sheet_ro["can_control"])
            self.assertTrue(sheet_ro["ai_brain"]["redacted"])
            self.assertIsNone(sheet_ro["ai_brain"]["operator_directive"])

            # Human character (Wonky) returns full sheet with is_companion=False & can_control=False
            human_sheet = fetch_character_sheet(
                "wonky",
                sql_exec=_mock_sql_for_characters([], is_companion=False),
                db_path=db_p,
                now_ts=1_800_000_050,
                can_control=True,
            )
            assert human_sheet is not None
            self.assertFalse(human_sheet["character"]["is_companion"])
            self.assertFalse(human_sheet["can_control"])
            self.assertIsNone(human_sheet["ai_brain"])
            self.assertEqual(len(human_sheet["gear"]), 19)

    def test_all_five_companion_direction_controls_and_human_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_p = Path(tmp) / "brain.db"
            sql_log: list[str] = []
            runner = _mock_sql_for_characters(sql_log, is_companion=True, existing_overrides=True)

            # 1. Focus preset writes monotonic revision > remote_max (50) with source='operator'
            res_p = apply_focus_preset("brannoc", "craft", hours=3, sql_exec=runner, now_ts=1_800_000_000)
            self.assertTrue(res_p["ok"])
            self.assertEqual(res_p["hours"], 3)
            self.assertGreater(res_p["revision"], 50)
            self.assertTrue(any("INSERT INTO companion_weight_overrides" in q and "'operator'" in q for q in sql_log))

            # 2. Manual weight slider pin + unpin + invalid key rejection
            sql_log.clear()
            res_pin = set_manual_weight("brannoc", "craft", 1.99, pinned=True, sql_exec=runner)
            self.assertEqual(res_pin["value"], 1.5)  # Clamped safely to 1.5!
            self.assertTrue(res_pin["pinned"])
            self.assertTrue(any("companion_weights" in q and "'manual'" in q for q in sql_log))

            sql_log.clear()
            res_unpin = set_manual_weight("brannoc", "craft", 1.0, pinned=False, sql_exec=runner)
            self.assertFalse(res_unpin["pinned"])
            self.assertEqual(res_unpin["source"], "hearthcore")
            with self.assertRaises(Invalid):
                set_manual_weight("brannoc", "not_a_key", 1.2, pinned=True, sql_exec=runner)

            # 3. Free-text directive stored & cleared in brain.db
            res_dir = set_character_directive("brannoc", "Level Mining in Wetlands!\n\x00", sql_exec=runner, db_path=db_p, now_ts=1_800_000_000)
            self.assertEqual(res_dir["directive"]["note"], "Level Mining in Wetlands!")
            self.assertTrue(res_dir["directive"]["active"])
            res_clr = set_character_directive("brannoc", "", sql_exec=runner, db_path=db_p, now_ts=1_800_000_010)
            self.assertFalse(res_clr["directive"]["active"])

            # 4. Pause / Resume AI (both when overrides exist and when none exist yet)
            sql_log.clear()
            res_pause = set_character_ai_paused("brannoc", True, sql_exec=runner, now_ts=1_800_000_000)
            self.assertTrue(res_pause["paused"])
            self.assertTrue(any("UPDATE companion_weight_overrides SET enabled = 0 WHERE guid = 1556" in q for q in sql_log))

            sql_no_ov: list[str] = []
            runner_no_ov = _mock_sql_for_characters(sql_no_ov, is_companion=True, existing_overrides=False)
            set_character_ai_paused("brannoc", True, sql_exec=runner_no_ov, now_ts=1_800_000_000)
            self.assertTrue(any("INSERT INTO companion_weight_overrides" in q and ", 0, " in q for q in sql_no_ov))

            # 5. Work Order creation & Shift Schedule update
            sql_log.clear()
            res_wo = create_character_work_order("brannoc", 2840, 12, "For Ironforge", sql_exec=runner, now_ts=1_800_000_000)
            self.assertEqual(res_wo["item_name"], "Copper Bar")
            self.assertEqual(res_wo["quantity"], 12)
            self.assertEqual(res_wo["status"], "claimed")
            self.assertTrue(any("INSERT INTO companion_work_orders" in q for q in sql_log))

            sql_log.clear()
            res_sc = update_character_schedule("brannoc", shift_group="prime-time", always_online=False, sql_exec=runner)
            self.assertEqual(res_sc["shift_group"], "prime-time")
            self.assertFalse(res_sc["always_online"])
            self.assertTrue(any("UPDATE companion_schedule SET" in q and "'prime-time'" in q for q in sql_log))

            # Reject direction controls on human characters!
            human_runner = _mock_sql_for_characters([], is_companion=False)
            with self.assertRaises(Invalid):
                apply_focus_preset("wonky", "craft", sql_exec=human_runner)

    def test_operator_directive_injected_into_claude_prompt_and_marked_consumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            db_p = tp / "brain.db"
            rt_p = tp / "runtime"
            store = BrainStore(db_p)
            store.set_operator_directive("brannoc", "Prioritize Blacksmithing skillups over quests!", now_ts=1_800_000_000)

            captured_payloads = []

            def fake_api(payload):
                captured_payloads.append(payload)
                user_obj = json.loads(payload["messages"][0]["content"])
                cid = user_obj["snapshot"]["character_id"]
                gid = user_obj["snapshot"]["candidates"][0]["id"]
                return {
                    "model": "claude-sonnet-5-5",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 600, "output_tokens": 110},
                    "content": [{"type": "text", "text": json.dumps({
                        "character_id": cid,
                        "primary_goal_id": gid,
                        "secondary_goal_ids": [],
                        "weight_modifiers": {"craft": 1.4},
                        "session_minutes": 60,
                        "reason_code": "operator_craft_priority",
                    })}],
                }

            world = {
                "observed_at": "2026-11-27T00:00:00Z",
                "weekly_event": "",
                "humans": [],
                "owner_levels": {"wonky": 31},
                "qm_focus": set(),
                "qm_mats_by_skill": {},
                "work_orders": [],
                "proposals_by_plan_id": {},
                "companions": {
                    "brannoc": {
                        "guid": 1556, "name": "Brannoc", "slug": "brannoc", "level": 33, "xp": 3000,
                        "zone_id": 1519, "zone": "Stormwind City", "map_id": 0, "gold": 76.0, "online": True,
                        "shift_group": "core-wonky", "skills": {164: (100, 150), 186: (120, 150)},
                        "orders_delivered": 0, "quests_rewarded": 10, "ledger_ops": 5,
                        "recent_events": [], "social_bonds": [],
                    }
                },
            }

            with mock.patch.dict(os.environ, {"ANTHROPIC_MODEL": "claude-sonnet-5-5", "HEARTHCORE_DAILY_CALL_CAP": "100"}):
                run_cycle(
                    provider="anthropic",
                    apply=True,
                    character="brannoc",
                    db_path=db_p,
                    runtime_dir=rt_p,
                    world_ctx_override=world,
                    http_post_fn=fake_api,
                    sql_exec=lambda s: [["0"]] if "SELECT GREATEST(" in s else [],
                    now_ts_override=1_800_000_100,
                )

            self.assertEqual(len(captured_payloads), 1)
            user_obj = json.loads(captured_payloads[0]["messages"][0]["content"])
            self.assertEqual(user_obj["operator_directive"], "Prioritize Blacksmithing skillups over quests!")
            self.assertIn("operator_directive", captured_payloads[0]["system"])

            # Verify directive was stamped as consumed!
            after = store.get_operator_directive("brannoc", now_ts=1_800_000_200)
            self.assertIsNotNone(after)
            assert after is not None
            self.assertEqual(after["consumed_at"], 1_800_000_100)

    def test_http_character_routes_and_loopback_write_csrf_security(self):
        aw_dir = str((ROOT / "azeroth-watch").resolve())
        if aw_dir not in sys.path:
            sys.path.insert(0, aw_dir)
        import server as aw_server  # type: ignore

        calls: list[tuple] = []
        replans: list[str] = []

        class FakeCtl:
            def fetch_character_sheet(self, slug, *, redact_admin=False, can_control=False):
                if slug == "nobody":
                    return None
                return {
                    "ok": True,
                    "can_control": can_control,
                    "character": {"name": slug.title(), "slug": slug, "is_companion": slug != "wonky"},
                    "ai_brain": {"redacted": redact_admin} if slug != "wonky" else None,
                }

            def apply_focus_preset(self, slug, preset, hours):
                calls.append(("preset", slug, preset, hours))
                return {"ok": True, "preset": preset, "hours": hours}

            def set_manual_weight(self, slug, key, value, pinned=True):
                calls.append(("weight", slug, key, value, pinned))
                return {"ok": True, "key": key, "value": value, "pinned": pinned}

            def set_character_directive(self, slug, note, ttl_hours=24):
                calls.append(("directive", slug, note))
                return {"ok": True, "directive": {"note": note, "active": bool(note)}}

            def set_character_ai_paused(self, slug, paused):
                calls.append(("pause", slug, paused))
                return {"ok": True, "paused": paused}

            def create_character_work_order(self, slug, item_entry, qty, story, assign_to_character=True):
                calls.append(("work-order", slug, item_entry, qty, assign_to_character))
                return {"ok": True, "item_entry": item_entry, "quantity": qty}

            def update_character_schedule(self, slug, shift_group=None, always_online=None):
                calls.append(("schedule", slug, shift_group, always_online))
                return {"ok": True, "shift_group": shift_group, "always_online": always_online}

        class DummyCollector:
            def get_snapshot(self):
                return 1, {"db_ok": True, "data_age_sec": 1}

            def request_refresh(self):
                return

        # 1. Default mode (trust_loopback_writes=False): POST returns 401 without token!
        with tempfile.TemporaryDirectory() as tmp:
            audit_p = os.path.join(tmp, "audit.log")
            h_strict = aw_server.make_handler(
                DummyCollector(),
                admin_token="strict_tok_12345678",
                env_path=os.path.join(tmp, ".env"),
                overrides_path=os.path.join(tmp, "ov.env"),
                audit_path=audit_p,
                character_ctl=FakeCtl(),
                allowed_hosts={"127.0.0.1", "localhost"},
                trust_loopback_writes=False,
            )
            srv = aw_server.ThreadingHTTPServer(("127.0.0.1", 0), h_strict)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/character/brannoc", headers={"Host": f"127.0.0.1:{port}"})
                r_html = conn.getresponse()
                html_b = r_html.read().decode("utf-8")
                conn.close()
                self.assertEqual(r_html.status, 200)
                self.assertIn("Direct This Companion", html_b)

                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/api/character/nobody", headers={"Host": f"127.0.0.1:{port}"})
                r_404 = conn.getresponse()
                r_404.read()
                conn.close()
                self.assertEqual(r_404.status, 404)

                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request(
                    "POST", "/api/character/brannoc/preset",
                    body=json.dumps({"preset": "craft", "hours": 2}),
                    headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"},
                )
                r_unauth = conn.getresponse()
                r_unauth.read()
                conn.close()
                self.assertEqual(r_unauth.status, 401, "Default mode MUST require token even on loopback!")
            finally:
                srv.shutdown()
                srv.server_close()

        # 2. Opt-in Acer loopback-open mode (trust_loopback_writes=True):
        #    Same-origin loopback POSTs succeed without token; cross-origin CSRF STILL rejected (403)!
        with tempfile.TemporaryDirectory() as tmp:
            audit_p = os.path.join(tmp, "audit.log")
            aw_server._LAST_RUN_TS.clear()
            h_open = aw_server.make_handler(
                DummyCollector(),
                admin_token="strict_tok_12345678",
                env_path=os.path.join(tmp, ".env"),
                overrides_path=os.path.join(tmp, "ov.env"),
                audit_path=audit_p,
                character_ctl=FakeCtl(),
                replan_runner=lambda s: replans.append(s),
                allowed_hosts={"127.0.0.1", "localhost"},
                trust_loopback_writes=True,
            )
            srv = aw_server.ThreadingHTTPServer(("127.0.0.1", 0), h_open)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                def post(path, payload, origin=None):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    hdrs = {
                        "Host": f"127.0.0.1:{port}",
                        "Content-Type": "application/json",
                        "Origin": origin if origin is not None else f"http://127.0.0.1:{port}",
                    }
                    conn.request("POST", path, body=json.dumps(payload), headers=hdrs)
                    resp = conn.getresponse()
                    raw = resp.read().decode("utf-8")
                    conn.close()
                    return resp.status, json.loads(raw)

                # Cross-origin CSRF MUST still be blocked (403) even in loopback-open mode!
                st_csrf, _ = post("/api/character/brannoc/preset", {"preset": "craft"}, origin="http://evil.example")
                self.assertEqual(st_csrf, 403)

                # All 6 same-origin loopback actions succeed
                self.assertEqual(post("/api/character/brannoc/preset", {"preset": "gather", "hours": 3})[0], 200)
                self.assertEqual(post("/api/character/brannoc/weight", {"key": "craft", "value": 1.35, "pinned": True})[0], 200)
                self.assertEqual(post("/api/character/brannoc/directive", {"note": "Smelt tin!", "replan": True})[0], 200)
                self.assertEqual(replans, ["brannoc"])
                # Immediate second replan is rate-limited (429)
                self.assertEqual(post("/api/character/brannoc/directive", {"note": "Again", "replan": True})[0], 429)
                self.assertEqual(post("/api/character/brannoc/pause", {"paused": True})[0], 200)
                self.assertEqual(post("/api/character/brannoc/work-order", {"item_entry": 2840, "quantity": 8})[0], 200)
                self.assertEqual(post("/api/character/brannoc/schedule", {"shift_group": "prime-time", "always_online": False})[0], 200)

                # Audit log captured all actions
                audit_lines = Path(audit_p).read_text(encoding="utf-8").splitlines()
                self.assertGreaterEqual(len(audit_lines), 7)

                # Verify GET /api/character/brannoc reports can_control=True in loopback-open mode
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/api/character/brannoc", headers={"Host": f"127.0.0.1:{port}"})
                r_api = conn.getresponse()
                d_api = json.loads(r_api.read().decode("utf-8"))
                conn.close()
                self.assertEqual(r_api.status, 200)
                self.assertTrue(d_api["can_control"])
                self.assertFalse(d_api["ai_brain"]["redacted"])
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
