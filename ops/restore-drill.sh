#!/usr/bin/env bash
# Non-destructive automated backup verification & REAL restore drill (Issue #75).
#
# Usage:
#   ops/restore-drill.sh [--mysql-restore-test] [path/to/acore-*.tar]
#
# Always verifies:
#   1. Archive unpacks cleanly and contains database.sql.gz + env (+ optional brain.db).
#   2. gzip integrity & SQL dump completion footer (`Dump completed`).
#   3. SQLite PRAGMA integrity_check AND readable tables/row counts on restored brain.db.
# With --mysql-restore-test:
#   4. Creates a throwaway MySQL database (`hearthcore_restore_drill_<pid>`), actually restores
#      database.sql.gz into that isolated database (rewriting CREATE DATABASE / USE statements
#      so live acore_* databases are NEVER touched), verifies restored tables > 0, and drops
#      the throwaway database on exit.
set -euo pipefail

MYSQL_RESTORE_TEST=0
TARGET=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mysql-restore-test) MYSQL_RESTORE_TEST=1; shift ;;
    -h|--help)
      echo "Usage: $0 [--mysql-restore-test] [path/to/acore-*.tar]"
      exit 0
      ;;
    *) TARGET="$1"; shift ;;
  esac
done

BACKUP_DIR="${BACKUP_DIR:-$HOME/AzerothCore/backups}"
if [[ -z "$TARGET" ]]; then
  TARGET="$(ls -1t "$BACKUP_DIR"/acore-*.tar 2>/dev/null | head -n 1 || true)"
fi

if [[ -z "$TARGET" || ! -f "$TARGET" ]]; then
  echo "ERROR: No backup archive found in $BACKUP_DIR" >&2
  exit 1
fi

TMP_DRILL="$(mktemp -d)"
DRILL_DB="hearthcore_restore_drill_$$"
MYSQL_CLEANUP=0

run_mysql_admin() {
  local sql_in="$1"
  if [[ -n "${HEARTHCORE_DB_HOST:-}" ]]; then
    local host="${HEARTHCORE_DB_HOST}"
    local port="${HEARTHCORE_DB_PORT:-3306}"
    local user="${HEARTHCORE_DB_USER:-root}"
    MYSQL_PWD="${HEARTHCORE_DB_PASSWORD:-}" mysql -h"$host" -P"$port" -u"$user" -N -B <<<"$sql_in"
  else
    local container="${HEARTHCORE_DB_CONTAINER:-ac-database}"
    docker exec -i "$container" sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -u root -N -B' <<<"$sql_in"
  fi
}

cleanup() {
  if [[ "$MYSQL_CLEANUP" -eq 1 ]]; then
    run_mysql_admin "DROP DATABASE IF EXISTS \`${DRILL_DB}\`;" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP_DRILL"
}
trap cleanup EXIT

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
    plan_cnt = conn.execute("SELECT COUNT(*) FROM plans;").fetchone()[0] if "plans" in tables else 0
    print(f"  [OK] brain.db restored & queried: integrity=ok, tables={len(tables)}, plans={plan_cnt}")
finally:
    conn.close()
' "$TMP_DRILL/brain.db"
else
  echo "  [WARN] brain.db not present in this archive (older pre-#53 backup)"
fi

if [[ "$MYSQL_RESTORE_TEST" -eq 1 ]]; then
  echo "  [..] Running REAL MySQL restore into throwaway DB \`${DRILL_DB}\`..."
  MYSQL_CLEANUP=1
  run_mysql_admin "DROP DATABASE IF EXISTS \`${DRILL_DB}\`; CREATE DATABASE \`${DRILL_DB}\`;"

  # Stream gzip SQL while stripping multi-DB CREATE DATABASE / USE statements so ALL tables restore
  # safely inside the isolated throwaway DB (never touching live acore_* schemas!).
  if [[ -n "${HEARTHCORE_DB_HOST:-}" ]]; then
    zcat "$TMP_DRILL/database.sql.gz" \
      | sed -E '/^CREATE DATABASE /Id; /^USE `/Id; /^DROP DATABASE /Id' \
      | MYSQL_PWD="${HEARTHCORE_DB_PASSWORD:-}" mysql -h"${HEARTHCORE_DB_HOST}" -P"${HEARTHCORE_DB_PORT:-3306}" -u"${HEARTHCORE_DB_USER:-root}" "$DRILL_DB"
  else
    zcat "$TMP_DRILL/database.sql.gz" \
      | sed -E '/^CREATE DATABASE /Id; /^USE `/Id; /^DROP DATABASE /Id' \
      | docker exec -i "${HEARTHCORE_DB_CONTAINER:-ac-database}" sh -c "MYSQL_PWD=\"\$MYSQL_ROOT_PASSWORD\" mysql -u root '${DRILL_DB}'"
  fi

  TABLE_COUNT="$(run_mysql_admin "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = '${DRILL_DB}';" | tr -d '[:space:]')"
  if [[ -z "$TABLE_COUNT" || "$TABLE_COUNT" -lt 1 ]]; then
    echo "FAIL: MySQL restore drill into ${DRILL_DB} produced 0 tables!" >&2
    exit 1
  fi
  echo "  [OK] MySQL restore drill verified: ${TABLE_COUNT} table(s) restored cleanly into ${DRILL_DB} (dropping throwaway DB on exit)"
fi

echo "[restore-drill] PASS: $TARGET is valid and restorable."
