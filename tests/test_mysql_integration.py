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

from hearthcore.bridge import (
    BrainStore,
    apply_plan_to_db,
    fetch_remote_max_revision,
    fetch_world_context,
    reconcile_state,
    run_sql,
)
from hearthcore.core import ROOT, fallback, profile, snapshot
from hearthcore.memory import MemoryEngine
from hearthcore.quartermaster import apply_quartermaster_plan, fetch_40char_supply_state


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

    def test_least_privilege_users_enforce_grants_and_deny_forbidden_ops(self):
        import subprocess

        host = os.environ["HEARTHCORE_DB_HOST"]
        port = os.environ.get("HEARTHCORE_DB_PORT", "3306")
        root_pw = os.environ["HEARTHCORE_DB_PASSWORD"]
        env_root = dict(os.environ, MYSQL_PWD=root_pw)

        # Create minimal stub acore_characters/acore_auth/acore_world schemas on the throwaway CI MySQL
        stub_ddl = """
        CREATE DATABASE IF NOT EXISTS acore_characters;
        CREATE DATABASE IF NOT EXISTS acore_auth;
        CREATE DATABASE IF NOT EXISTS acore_world;
        CREATE TABLE IF NOT EXISTS acore_characters.characters (guid INT PRIMARY KEY, name VARCHAR(32));
        CREATE TABLE IF NOT EXISTS acore_characters.character_skills (guid INT, skill INT, value INT, max INT);
        CREATE TABLE IF NOT EXISTS acore_characters.character_queststatus_rewarded (guid INT, quest INT);
        CREATE TABLE IF NOT EXISTS acore_characters.guild_bank_item (guildid INT, item_guid INT);
        CREATE TABLE IF NOT EXISTS acore_characters.guild_member (guildid INT, guid INT);
        CREATE TABLE IF NOT EXISTS acore_characters.item_instance (guid INT, itemEntry INT, count INT);
        CREATE TABLE IF NOT EXISTS acore_auth.account (id INT PRIMARY KEY, username VARCHAR(32), verifier VARBINARY(32));
        CREATE TABLE IF NOT EXISTS acore_auth.realmlist (id INT PRIMARY KEY, name VARCHAR(32), address VARCHAR(32), port INT, flag INT);
        CREATE TABLE IF NOT EXISTS acore_world.item_template (entry INT PRIMARY KEY, name VARCHAR(64));
        CREATE TABLE IF NOT EXISTS acore_world.player_xp_for_level (level INT PRIMARY KEY, Experience INT);
        INSERT IGNORE INTO acore_auth.account (id, username, verifier) VALUES (1, 'Tester', UNHEX('AABBCC'));
        """
        schema_sql = (ROOT / "ops" / "sql" / "companion_schema.sql").read_text(encoding="utf-8")
        users_sql = (ROOT / "ops" / "sql" / "create_least_priv_users.sql").read_text(encoding="utf-8")
        brain_pw = "BrainTestPass1234567890!"
        watch_pw = "WatchTestPass1234567890!"

        # Discover exact client host IP seen by MySQL container (e.g. 172.18.0.1 in CI Docker bridge)
        client_host_res = subprocess.run(
            ["mysql", f"-h{host}", f"-P{port}", "-uroot", "-N", "-B", "-e", "SELECT SUBSTRING_INDEX(USER(), '@', -1);"],
            check=True,
            capture_output=True,
            env=env_root,
        )
        client_host = client_host_res.stdout.decode("utf-8").strip() or "127.0.0.1"

        subprocess.run(
            ["mysql", f"-h{host}", f"-P{port}", "-uroot"],
            input=(
                stub_ddl
                + "\nUSE acore_characters;\n"
                + schema_sql
                + f"\nSET @client_host='{client_host}'; SET @brain_pw='{brain_pw}'; SET @watch_pw='{watch_pw}';\n"
                + users_sql
            ).encode("utf-8"),
            check=True,
            capture_output=True,
            env=env_root,
        )

        def run_as(user: str, pw: str, sql: str):
            return subprocess.run(
                ["mysql", f"-h{host}", f"-P{port}", f"-u{user}", "-N", "-B"],
                input=sql.encode("utf-8"),
                capture_output=True,
                env=dict(os.environ, MYSQL_PWD=pw),
            )

        # 1. azeroth_watch CAN read allowed columns and CANNOT read account.verifier or write anything!
        r_ok = run_as("azeroth_watch", watch_pw, "SELECT id, username FROM acore_auth.account;")
        self.assertEqual(r_ok.returncode, 0, r_ok.stderr.decode())
        r_col = run_as("azeroth_watch", watch_pw, "SELECT verifier FROM acore_auth.account;")
        self.assertNotEqual(r_col.returncode, 0, "azeroth_watch MUST NOT read account.verifier!")
        r_wr = run_as("azeroth_watch", watch_pw, "INSERT INTO acore_characters.companion_bank_requests (item_entry, requested_by, min_count, note) VALUES (1, 'x', 1, 'x');")
        self.assertNotEqual(r_wr.returncode, 0, "azeroth_watch MUST be strictly SELECT-only!")

        # 2. hearthcore_brain CAN write companion_weight_overrides and CANNOT write characters or companion_weights!
        r_b_ok = run_as(
            "hearthcore_brain", brain_pw,
            "INSERT INTO acore_characters.companion_weight_overrides (guid, `key`, value, plan_id, revision, source, enabled, created_at, expires_at) "
            "VALUES (101, 'craft', 1.2, 1, 1, 'hearthcore_ai', 1, 1, 1900000000) ON DUPLICATE KEY UPDATE value=1.2;",
        )
        self.assertEqual(r_b_ok.returncode, 0, r_b_ok.stderr.decode())
        r_b_deny_chars = run_as("hearthcore_brain", brain_pw, "INSERT INTO acore_characters.characters (guid, name) VALUES (999, 'Hack');")
        self.assertNotEqual(r_b_deny_chars.returncode, 0, "hearthcore_brain MUST NOT write characters!")
        r_b_deny_canon = run_as("hearthcore_brain", brain_pw, "INSERT INTO acore_characters.companion_weights (guid, `key`, value, source) VALUES (101, 'craft', 1.5, 'manual');")
        self.assertNotEqual(r_b_deny_canon.returncode, 0, "hearthcore_brain MUST NOT write canonical companion_weights!")
        r_b_deny_ver = run_as("hearthcore_brain", brain_pw, "SELECT verifier FROM acore_auth.account;")
        self.assertNotEqual(r_b_deny_ver.returncode, 0, "hearthcore_brain MUST NOT read account.verifier!")

    def test_world_context_and_supply_state_reads_migrations_and_reconcile_drift(self):
        import importlib.util
        import subprocess

        host = os.environ["HEARTHCORE_DB_HOST"]
        port = os.environ.get("HEARTHCORE_DB_PORT", "3306")
        db_name = os.environ["HEARTHCORE_DB_NAME"]
        root_pw = os.environ["HEARTHCORE_DB_PASSWORD"]
        env_root = dict(os.environ, MYSQL_PWD=root_pw)

        # 1. Versioned migrations runner applies cleanly and is idempotent on second run
        mig_path = ROOT / "ops" / "sql" / "apply_migrations.py"
        spec = importlib.util.spec_from_file_location("apply_migrations", mig_path)
        assert spec and spec.loader
        mig_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig_mod)
        run_sql("DROP TABLE IF EXISTS companion_schema_migrations;")
        first = mig_mod.apply_migrations()
        self.assertGreaterEqual(len(first), 2)
        second = mig_mod.apply_migrations()
        self.assertEqual(second, [], "Second migration run must be a no-op")

        # 2. Populate realistic AzerothCore character/skill/bank/auth fixtures in the throwaway test DB
        seed_sql = f"""
        CREATE DATABASE IF NOT EXISTS acore_auth;
        CREATE TABLE IF NOT EXISTS acore_auth.account (
            id INT PRIMARY KEY, username VARCHAR(32), verifier VARBINARY(32)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        DROP TABLE IF EXISTS `{db_name}`.characters;
        CREATE TABLE `{db_name}`.characters (
            guid INT PRIMARY KEY, account INT NOT NULL, name VARCHAR(32) NOT NULL,
            level TINYINT UNSIGNED NOT NULL, zone INT NOT NULL, map INT NOT NULL,
            money INT NOT NULL, online TINYINT NOT NULL, xp INT NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        CREATE TABLE IF NOT EXISTS `{db_name}`.character_skills (
            guid INT NOT NULL, skill SMALLINT NOT NULL, value SMALLINT NOT NULL, max SMALLINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS `{db_name}`.character_queststatus_rewarded (guid INT NOT NULL, quest INT NOT NULL);
        CREATE TABLE IF NOT EXISTS `{db_name}`.guild_bank_item (guildid INT NOT NULL, item_guid INT NOT NULL);
        CREATE TABLE IF NOT EXISTS `{db_name}`.item_instance (guid INT NOT NULL, itemEntry INT NOT NULL, count INT NOT NULL);

        TRUNCATE TABLE `{db_name}`.characters;
        TRUNCATE TABLE `{db_name}`.character_skills;
        TRUNCATE TABLE `{db_name}`.character_queststatus_rewarded;
        TRUNCATE TABLE `{db_name}`.guild_bank_item;
        TRUNCATE TABLE `{db_name}`.item_instance;
        TRUNCATE TABLE `{db_name}`.companion_schedule;
        TRUNCATE TABLE `{db_name}`.companion_relationships;
        TRUNCATE TABLE `{db_name}`.companion_ledger;

        INSERT IGNORE INTO acore_auth.account (id, username, verifier) VALUES (1, 'WONKY', UNHEX('00')), (2, 'COMPBOT', UNHEX('00'));
        INSERT INTO `{db_name}`.characters VALUES
            (1, 1, 'Wonky', 30, 10, 0, 500000, 1, 12000),
            (101, 2, 'Brannoc', 33, 10, 0, 250000, 1, 0);
        INSERT INTO `{db_name}`.companion_schedule (name, shift_group, start_hour, end_hour, days_mask, always_online, note)
            VALUES ('Brannoc', 'core-wonky', 0, 24, 127, 1, 'Core tank');
        INSERT INTO `{db_name}`.character_skills VALUES (101, 164, 105, 150), (101, 186, 121, 150);
        INSERT INTO `{db_name}`.character_queststatus_rewarded VALUES (101, 1001), (101, 1002);
        INSERT INTO `{db_name}`.guild_bank_item VALUES (6, 9001);
        INSERT INTO `{db_name}`.item_instance VALUES (9001, 2770, 40);
        INSERT INTO `{db_name}`.companion_events (guid, event_type, summary, partner_guid, zone_id, created_at)
            VALUES (101, 'level_up', 'Brannoc reached 33', 0, 10, 1800000000);
        """
        subprocess.run(
            ["mysql", f"-h{host}", f"-P{port}", "-uroot"],
            input=seed_sql.encode("utf-8"),
            check=True,
            capture_output=True,
            env=env_root,
        )

        # 3. Exercise REAL fetch_world_context() & fetch_40char_supply_state() read paths against MySQL 8.4!
        with tempfile.TemporaryDirectory() as tmp:
            db_p = Path(tmp) / "brain.db"
            mem = MemoryEngine(db_p)
            wctx = fetch_world_context(mem=mem, now_ts=1_800_000_050)
            self.assertIn("brannoc", wctx["companions"])
            self.assertEqual(wctx["companions"]["brannoc"]["skills"][186], (121, 150))
            self.assertEqual(wctx["companions"]["brannoc"]["quests_rewarded"], 2)
            self.assertEqual(wctx["owner_levels"]["wonky"], 30)
            self.assertEqual(mem.journal_status()["total_events"], 1)

            sup_state = fetch_40char_supply_state()
            self.assertEqual(sup_state["vault_counts"][2770], 40)
            self.assertEqual(sup_state["supply"]["characters_analyzed"], 2)

            # 4. Exercise real reconcile_state() detecting orphan SQLite plan vs clean MySQL match!
            p = profile("brannoc")
            snap = snapshot(json.loads((ROOT / "examples" / "brannoc.json").read_text(encoding="utf-8")), "brannoc")
            plan = fallback(p, snap)
            store = BrainStore(db_p)
            pid = store.record_plan(
                now_ts=1_800_000_000, slug="brannoc", guid=101, source="fallback",
                model="fallback", effort="", plan=plan, primary_desc="Test",
                cpp_weights={"craft": 1.2}, meta={}, applied=True, error_note="",
                observed_at="2026-11-25T20:00:00Z",
            )
            drift_rep = reconcile_state(db_path=db_p, now_ts=1_800_000_060)
            self.assertFalse(drift_rep["ok"])
            self.assertEqual(len(drift_rep["orphan_plans"]), 1)

            apply_plan_to_db(101, "Brannoc", plan, snap, "fallback", plan_id=pid, now_ts=1_800_000_000, char_profile=p)
            clean_rep = reconcile_state(db_path=db_p, now_ts=1_800_000_060)
            self.assertTrue(clean_rep["ok"], f"Expected clean reconcile after apply, got: {clean_rep}")


if __name__ == "__main__":
    unittest.main()
