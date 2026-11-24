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
