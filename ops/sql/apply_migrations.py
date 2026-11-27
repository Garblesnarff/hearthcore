#!/usr/bin/env python3
"""Idempotent MySQL migration runner for ops/sql/migrations/*.sql (Issue #64)."""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hearthcore.bridge import run_sql

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def apply_migrations(migrations_dir: Path = MIGRATIONS_DIR, *, sql_exec=None, dry_run: bool = False) -> list[str]:
    runner = sql_exec if sql_exec is not None else run_sql
    runner(
        """
        CREATE TABLE IF NOT EXISTS `companion_schema_migrations` (
            `version` varchar(128) NOT NULL,
            `sha256` varchar(64) NOT NULL,
            `applied_at` int unsigned NOT NULL,
            PRIMARY KEY (`version`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
        """
    )
    rows = runner("SELECT version, sha256 FROM companion_schema_migrations ORDER BY version;")
    applied = {str(r[0]): str(r[1]) for r in rows if len(r) >= 2}

    newly_applied: list[str] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        ver = path.name
        sql_txt = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(sql_txt.encode("utf-8")).hexdigest()
        if ver in applied:
            if applied[ver] != digest:
                raise RuntimeError(f"Migration checksum drift for {ver}: recorded={applied[ver][:12]} current={digest[:12]}")
            continue
        if dry_run:
            newly_applied.append(ver)
            continue
        now = int(time.time())
        runner(
            sql_txt
            + f"\nINSERT INTO companion_schema_migrations (version, sha256, applied_at) VALUES ('{ver}', '{digest}', {now});\n"
        )
        newly_applied.append(ver)
    return newly_applied


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply versioned companion_* SQL migrations idempotently")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    done = apply_migrations(dry_run=args.dry_run)
    prefix = "Pending migrations" if args.dry_run else "Applied migrations"
    print(f"{prefix}: {done or ['(none — up to date)']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
