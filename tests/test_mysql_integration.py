"""Real MySQL 8.4 schema & SQL integration tests (Issues #53, #54, #62, #64).

SAFETY GUARD:
- Skipped unless BOTH `HEARTHCORE_TEST_DB=1` AND `HEARTHCORE_DB_HOST` are set AND
  `HEARTHCORE_DB_NAME` explicitly ends with `_test` (e.g. `acore_characters_test`).
- Never runs TRUNCATE against a live `acore_characters` database!
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from hearthcore.bridge import apply_plan_to_db, fetch_remote_max_revision, reconcile_state, run_sql
from hearthcore.core import ROOT, fallback, profile, snapshot
from hearthcore.quartermaster import apply_quartermaster_plan


def _is_safe_test_db() -> bool:
    return (
        os.getenv("HEARTHCORE_TEST_DB") == "1"
        and bool(os.getenv("HEARTHCORE_DB_HOST", "").strip())
        and os.getenv("HEARTHCORE_DB_NAME", "").strip().endswith("_test")
    )


@unittest.skipUnless(_is_safe_test_db(), "Requires HEARTHCORE_TEST_DB=1, HEARTHCORE_DB_HOST, and *_test DB name")
class MySQLIntegrationTests(unittest.TestCase):
    def setUp(self):
        db_name = os.getenv("HEARTHCORE_DB_NAME", "").strip()
        if not db_name.endswith("_test"):
            raise RuntimeError(f"Refusing to truncate non-test database: {db_name!r}")
        run_sql(
            "TRUNCATE TABLE companion_weight_overrides;\n"
            "TRUNCATE TABLE companion_proposals;\n"
            "TRUNCATE TABLE companion_work_orders;\n"
            "TRUNCATE TABLE companion_bank_requests;\n"
            "TRUNCATE TABLE companion_events;\n"
        )

    def test_apply_plan_monotonic_revision_and_reconcile(self):
        p = profile("brannoc")
        snap = snapshot(json.loads((ROOT / "examples" / "brannoc.json").read_text(encoding="utf-8")), "brannoc")
        plan = fallback(p, snap)

        apply_plan_to_db(101, "Brannoc", plan, snap, "anthropic", plan_id=10, now_ts=1_800_000_000, char_profile=p)
        self.assertEqual(fetch_remote_max_revision(), 10)

        # Lower revision MUST NOT overwrite revision 10!
        apply_plan_to_db(101, "Brannoc", plan, snap, "fallback", plan_id=5, now_ts=1_800_000_005, char_profile=p)
        rows = run_sql("SELECT MAX(revision) FROM companion_weight_overrides WHERE guid = 101;", expected_cols=1)
        self.assertEqual(int(rows[0][0]), 10)

        # Verify reconcile detects clean state vs orphan drift
        with tempfile.TemporaryDirectory() as tmp:
            rep = reconcile_state(db_path=Path(tmp) / "brain.db", now_ts=1_800_000_010)
            self.assertTrue(rep["ok"])

    def test_quartermaster_atomic_manual_preservation_and_order_cap(self):
        run_sql(
            "INSERT INTO companion_bank_requests (item_entry, requested_by, min_count, note) "
            "VALUES (2770, 'WonkyGM', 150, 'Manual GM priority');"
        )
        plan = {
            "strategy_summary": "CI MySQL atomic test",
            "work_orders": [
                {"item_entry": 2840, "name": "Copper Bar", "quantity": 10, "priority": 1, "category": "bar", "skill_id": 186, "reason": "test1"},
                {"item_entry": 2841, "name": "Bronze Bar", "quantity": 10, "priority": 1, "category": "bar", "skill_id": 186, "reason": "test2"},
            ],
            "bank_requests": [
                {"item_entry": 2770, "name": "Copper Ore", "min_count": 40, "note": "QM attempt"},
                {"item_entry": 2447, "name": "Peacebloom", "min_count": 60, "note": "Herb need"},
            ],
        }
        state = {"characters": [{"guid": 102, "name": "Kelda", "level": 28, "skills": {165: (50, 75)}}]}

        added, qm_banks = apply_quartermaster_plan(plan, state, target_open_orders=1)
        self.assertEqual(added, 1, "Transaction must cap open orders at target_open_orders=1")
        self.assertEqual(qm_banks, 1)

        gm_row = run_sql(
            "SELECT requested_by, min_count, note FROM companion_bank_requests WHERE item_entry = 2770;",
            expected_cols=3,
        )
        self.assertEqual(gm_row[0], ["WonkyGM", "150", "Manual GM priority"])

    def test_tsv_parser_handles_null_tab_newline_cr_and_u2028(self):
        run_sql(
            "INSERT INTO companion_events (guid, event_type, summary, partner_guid, zone_id, created_at) "
            "VALUES (101, 'test', CONCAT('a', CHAR(9), 'b', CHAR(10), 'c', CHAR(13), 'd', UNHEX('E280A8'), 'e'), 0, 12, 1800000000);"
        )
        rows = run_sql("SELECT guid, summary, NULL FROM companion_events LIMIT 1;", expected_cols=3)
        self.assertEqual(rows[0][0], "101")
        self.assertEqual(rows[0][1], "a\tb\nc\rd\u2028e")
        self.assertIsNone(rows[0][2])


if __name__ == "__main__":
    unittest.main()
