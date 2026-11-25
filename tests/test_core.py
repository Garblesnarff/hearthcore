import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from hearthcore.bridge import weights_for_cpp
from hearthcore.core import Invalid, fallback, profile, profiles, snapshot, validate
from hearthcore.provider import _strip_json_fences
from hearthcore.store import record

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "brannoc.json"


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.p = profile("brannoc")
        self.s = snapshot(json.loads(EXAMPLE.read_text()), "brannoc")

    def test_profiles(self):
        all_slugs = profiles()
        self.assertEqual(len(all_slugs), 38)
        for slug in all_slugs:
            profile(slug)

    def test_fallback(self):
        self.assertEqual(fallback(self.p, self.s), fallback(self.p, self.s))

    def test_wrong_character(self):
        with self.assertRaises(Invalid):
            snapshot(self.s, "kelda")

    def test_unapproved_goal(self):
        plan = fallback(self.p, self.s)
        plan["primary_goal_id"] = "free:epic"
        with self.assertRaises(Invalid):
            validate(plan, self.s)

    def test_excessive_weight(self):
        plan = fallback(self.p, self.s)
        plan["weight_modifiers"]["quest"] = 2
        with self.assertRaises(Invalid):
            validate(plan, self.s)

    def test_duplicate_candidates(self):
        s = copy.deepcopy(self.s)
        s["candidates"].append(s["candidates"][0])
        with self.assertRaises(Invalid):
            snapshot(s, "brannoc")

    def test_no_unexpected_fields(self):
        plan = fallback(self.p, self.s)
        plan["grant_gold"] = True
        with self.assertRaises(Invalid):
            validate(plan, self.s)

    def test_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "audit.db")
            record(path, fallback(self.p, self.s), "fallback", self.s["observed_at"])
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0], 1)

    def test_cpp_weights_bounds(self):
        plan = fallback(self.p, self.s)
        w = weights_for_cpp(plan, self.s)
        self.assertIn("quest_work", w)
        self.assertIn("craft", w)
        for k, v in w.items():
            self.assertTrue(0.5 <= v <= 1.5, f"{k}={v}")

    def test_strip_json_fences(self):
        self.assertEqual(_strip_json_fences('```json\n{"a":1}\n```'), '{"a":1}')

    def test_supply_report_and_quartermaster_fallback(self):
        from hearthcore.guides import compute_supply_report
        from hearthcore.quartermaster import _fallback_quartermaster, build_eligible_menus

        chars = [
            {"guid": 1, "name": "Wonky", "level": 30, "online": True, "skills": {333: (100, 150), 185: (138, 150)}},
            {"guid": 2, "name": "Logan", "level": 10, "online": False, "skills": {164: (5, 75), 186: (10, 75)}},
        ]
        sup = compute_supply_report(chars, {2840: 20})
        self.assertEqual(sup["characters_analyzed"], 2)
        copper = next(m for m in sup["materials"] if m["item_entry"] == 2840)
        self.assertEqual(copper["vault_have"], 20)
        self.assertGreater(copper["active_deficit"], 0)

        st = {"characters": chars, "vault_counts": {2840: 20}, "open_orders": [], "supply": sup}
        om, bm = build_eligible_menus(st)
        self.assertTrue(any(o["item_entry"] == 21072 for o in om))  # Smoked Sagefish
        self.assertTrue(any(o["item_entry"] == 20744 for o in om))  # Minor Wizard Oil
        fb = _fallback_quartermaster(om, bm, 6)
        self.assertEqual(len(fb["work_orders"]), 6)
        self.assertGreater(len(fb["bank_requests"]), 0)

    def test_memory_v1_intentions_fatigue_stall_and_reservations(self):
        from hearthcore.bridge import BrainStore, build_snapshot
        from hearthcore.memory import MemoryEngine

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "brain.db"
            store = BrainStore(db_path)
            mem = MemoryEngine(db_path)
            now_ts = 1_800_000_000

            comp = {
                "guid": 101,
                "name": "Brannoc",
                "slug": "brannoc",
                "level": 28,
                "zone_id": 1,
                "zone": "Dun Morogh",
                "shift_group": "core-wonky",
                "skills": {164: (132, 150), 186: (140, 150)},
                "recent_events": ["level_up: Reached Lv 28"],
                "social_bonds": ["Kelda (affinity 42, 6 runs, 3 helps)"],
            }
            world_ctx = {
                "observed_at": "2026-11-24T20:00:00Z",
                "weekly_event": "",
                "work_orders": [
                    {
                        "id": 50,
                        "requester_guid": 202,
                        "requester_name": "Kelda",
                        "item_entry": 2862,
                        "item_name": "Rough Sharpening Stone",
                        "quantity": 10,
                        "story": "Leveling blades",
                        "skill": 164,
                    },
                    {
                        "id": 51,
                        "requester_guid": 203,
                        "requester_name": "Aldric",
                        "item_entry": 2863,
                        "item_name": "Coarse Sharpening Stone",
                        "quantity": 10,
                        "story": "More blades",
                        "skill": 164,
                    },
                ],
                "humans": [],
                "qm_focus": set(),
                "qm_mats_by_skill": {},
            }

            # 1. Auto-seed skill_tier intention (Mining 140 -> 150 is closest gap=10)
            mctx = mem.build_packet(comp, world_ctx, now_ts)
            self.assertTrue(any("150" in s for s in mctx["packet"]["open_intentions"]))

            # 2. Auto-complete skill_tier intention when authoritative skill reaches 150
            comp_done = copy.deepcopy(comp)
            comp_done["skills"][186] = (150, 150)
            mem.reconcile_intentions(comp_done, {50, 51}, now_ts + 60)
            done_list = mem.recent_completed_intentions(5)
            self.assertTrue(any("Mining 150" in d["evidence"] for d in done_list))

            # 3. Record 2 consecutive 0-progress aged plans on the exact same goal -> fatigue + scoped stall alert
            stub_plan = {
                "character_id": "brannoc",
                "primary_goal_id": "craft:blacksmithing",
                "secondary_goal_ids": ["gather:mining"],
                "weight_modifiers": {"craft": 1.3},
                "session_minutes": 60,
                "reason_code": "craft_focus",
            }
            for offset in (-7200, -3600):
                store.record_plan(
                    now_ts=now_ts + offset,
                    slug="brannoc",
                    guid=101,
                    source="anthropic",
                    model="claude-sonnet-5-5",
                    effort="medium",
                    plan=stub_plan,
                    primary_desc="Advance Blacksmithing",
                    cpp_weights={"craft": 1.3},
                    meta={},
                    applied=True,
                    error_note="",
                    observed_at="2026-11-24T18:00:00Z",
                    start_level=28,
                    start_xp=100,
                    start_skill_sum=272,
                    start_orders_done=0,
                )

            # Another companion holds an unexpired reservation on order #50
            other_plan = {
                "character_id": "forgedeep",
                "primary_goal_id": "guild_order:50:rough_sharpening_stone",
                "secondary_goal_ids": [],
                "weight_modifiers": {"guild_order": 1.3},
                "session_minutes": 90,
                "reason_code": "guild_order_claim",
            }
            store.record_plan(
                now_ts=now_ts - 300,
                slug="forgedeep",
                guid=303,
                source="anthropic",
                model="claude-sonnet-5-5",
                effort="medium",
                plan=other_plan,
                primary_desc="Order #50",
                cpp_weights={"craft": 1.4},
                meta={},
                applied=True,
                error_note="",
                observed_at="2026-11-24T19:55:00Z",
            )

            res_map = mem.active_order_reservations(now_ts)
            self.assertEqual(res_map.get(50), "forgedeep")

            mctx2 = mem.build_packet(comp, world_ctx, now_ts)
            self.assertEqual(mctx2["fatigue_category"], "craft")
            self.assertEqual(mctx2["fatigue_streak"], 2)
            self.assertEqual(mctx2["stalled_goal_id"], "craft:blacksmithing")
            self.assertIsNotNone(mctx2["operational_alert"])
            self.assertIn("variety_hint", mctx2["packet"])
            self.assertIn("stall_caution", mctx2["packet"])

            # Build snapshot with reservations + memory_ctx -> order #50 penalized & tagged, #51 preferred, stalled goal tagged
            snap = build_snapshot(comp, world_ctx, memory_ctx=mctx2, order_reservations=res_map)
            by_id = {c["id"]: c for c in snap["candidates"]}
            c50 = next(c for c in snap["candidates"] if c["id"].startswith("guild_order:50:"))
            c51 = next(c for c in snap["candidates"] if c["id"].startswith("guild_order:51:"))
            self.assertIn("Claimed by Forgedeep", c50["description"])
            self.assertGreater(c51["score"], c50["score"])
            self.assertIn("Stalled: 0 progress", by_id["craft:blacksmithing"]["description"])

    def test_xp_capped_profession_catchup_priority(self):
        from hearthcore.bridge import build_snapshot, weights_for_cpp

        comp = {
            "guid": 105,
            "name": "Aldric",
            "slug": "aldric",
            "level": 33,
            "zone_id": 33,
            "zone": "Stranglethorn Vale",
            "shift_group": "core-wonky",
            "skills": {171: (47, 75), 182: (34, 75)},
            "recent_events": [],
            "social_bonds": [],
        }
        world_ctx = {
            "observed_at": "2026-11-25T13:00:00Z",
            "weekly_event": "",
            "work_orders": [],
            "humans": [],
            "owner_levels": {"wonky": 30, "logan": 10},
            "qm_focus": set(),
            "qm_mats_by_skill": {},
        }
        snap = build_snapshot(comp, world_ctx)
        by_id = {c["id"]: c for c in snap["candidates"]}
        self.assertLess(by_id["quest:zone_33"]["score"], 40)
        self.assertGreater(by_id["craft:alchemy"]["score"], 75)
        self.assertGreater(by_id["gather:herbalism"]["score"], 75)
        plan = {
            "character_id": "aldric",
            "primary_goal_id": "gather:herbalism",
            "secondary_goal_ids": ["craft:alchemy"],
            "weight_modifiers": {"gather": 1.3, "craft": 1.2, "quest": 0.9},
            "session_minutes": 120,
            "reason_code": "catchup_herbalism",
        }
        w = weights_for_cpp(plan, snap, xp_capped_catchup=True)
        self.assertLessEqual(w["quest_work"], 0.55)
        self.assertGreaterEqual(w["craft"], 1.45)
        self.assertGreaterEqual(w["gather"], 1.45)
        self.assertLessEqual(w["travel"], 0.65)
