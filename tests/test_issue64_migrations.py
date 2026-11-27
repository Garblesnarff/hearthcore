"""Unit tests for versioned MySQL migrations runner (Issue #64)."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_migrations_module():
    path = ROOT / "ops" / "sql" / "apply_migrations.py"
    spec = importlib.util.spec_from_file_location("apply_migrations", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Issue64MigrationsUnitTests(unittest.TestCase):
    def test_apply_migrations_dry_run_idempotency_and_drift_detection(self):
        mod = _load_migrations_module()
        with tempfile.TemporaryDirectory() as tmp:
            mdir = Path(tmp) / "migrations"
            mdir.mkdir()
            m1 = mdir / "001_init.sql"
            m2 = mdir / "002_next.sql"
            m1.write_text("CREATE TABLE IF NOT EXISTS t1 (id INT PRIMARY KEY);\n", encoding="utf-8")
            m2.write_text("CREATE TABLE IF NOT EXISTS t2 (id INT PRIMARY KEY);\n", encoding="utf-8")

            applied_rows: dict[str, str] = {}
            executed: list[str] = []

            def fake_sql(sql: str):
                executed.append(sql)
                if "SELECT version, sha256 FROM companion_schema_migrations" in sql:
                    return [[v, h] for v, h in sorted(applied_rows.items())]
                if "INSERT INTO companion_schema_migrations" in sql:
                    marker = "VALUES ('"
                    part = sql.split(marker, 1)[1]
                    ver, rest = part.split("', '", 1)
                    sha = rest.split("', ", 1)[0]
                    applied_rows[ver] = sha
                return []

            # Dry run reports pending without mutating
            pending = mod.apply_migrations(mdir, sql_exec=fake_sql, dry_run=True)
            self.assertEqual(pending, ["001_init.sql", "002_next.sql"])
            self.assertEqual(applied_rows, {})

            # First real run applies both in sorted order
            first = mod.apply_migrations(mdir, sql_exec=fake_sql)
            self.assertEqual(first, ["001_init.sql", "002_next.sql"])
            self.assertEqual(sorted(applied_rows.keys()), ["001_init.sql", "002_next.sql"])

            # Second run is a clean no-op
            second = mod.apply_migrations(mdir, sql_exec=fake_sql)
            self.assertEqual(second, [])

            # Mutating an already-applied migration raises checksum drift error
            m1.write_text("CREATE TABLE IF NOT EXISTS t1 (id BIGINT PRIMARY KEY);\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as ctx:
                mod.apply_migrations(mdir, sql_exec=fake_sql)
            self.assertIn("checksum drift", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
