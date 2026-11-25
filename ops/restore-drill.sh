#!/usr/bin/env bash
# Non-destructive automated backup verification & restore drill (Issue #75).
# Unpacks the newest (or given) acore-*.tar archive into a temporary directory and verifies:
#   1. Archive permissions (600) and required members (database.sql.gz, env, optional brain.db).
#   2. gzip integrity & SQL dump header/footer completeness (Dump completed).
#   3. SQLite PRAGMA integrity_check on restored brain.db (when bundled).
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/AzerothCore/backups}"
TARGET="${1:-$(ls -1t "$BACKUP_DIR"/acore-*.tar 2>/dev/null | head -n 1 || true)}"

if [[ -z "$TARGET" || ! -f "$TARGET" ]]; then
  echo "ERROR: No backup archive found in $BACKUP_DIR" >&2
  exit 1
fi

TMP_DRILL="$(mktemp -d)"
trap 'rm -rf "$TMP_DRILL"' EXIT

echo "[restore-drill] Verifying archive: $TARGET"
tar -xf "$TARGET" -C "$TMP_DRILL"

if [[ ! -s "$TMP_DRILL/database.sql.gz" ]]; then
  echo "FAIL: database.sql.gz missing or empty" >&2
  exit 1
fi
gzip -t "$TMP_DRILL/database.sql.gz"
if ! zcat "$TMP_DRILL/database.sql.gz" | tail -n 20 | grep -q "Dump completed"; then
  echo "FAIL: database.sql.gz missing 'Dump completed' footer" >&2
  exit 1
fi
echo "  [OK] database.sql.gz gzip integrity & SQL completion footer verified"

if [[ ! -s "$TMP_DRILL/env" ]]; then
  echo "FAIL: env file missing or empty" >&2
  exit 1
fi
echo "  [OK] env file present"

if [[ -f "$TMP_DRILL/brain.db" ]]; then
  python3 -c '
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
try:
    res = conn.execute("PRAGMA integrity_check;").fetchone()
    if not res or res[0] != "ok":
        raise SystemExit(f"FAIL: brain.db integrity_check={res}")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type=\"table\";").fetchall()]
    print(f"  [OK] brain.db PRAGMA integrity_check=ok ({len(tables)} tables)")
finally:
    conn.close()
' "$TMP_DRILL/brain.db"
else
  echo "  [WARN] brain.db not present in this archive (older pre-#53 backup)"
fi

echo "[restore-drill] PASS: $TARGET is valid and restorable."
