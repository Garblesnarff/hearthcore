#!/usr/bin/env bash
# Nightly / on-demand backup for AzerothCore MySQL databases + WAL-safe Hearthcore brain.db (Issues #53 & #75).
# Produces one chmod-600 bundle per run: acore-YYYYmmdd-HHMMSS.tar (database.sql.gz + env + brain.db).
set -euo pipefail

AC_ROOT="${AC_ROOT:-$HOME/AzerothCore}"
AC_DIR="${AC_DIR:-$AC_ROOT/azerothcore-wotlk}"
BACKUP_DIR="${BACKUP_DIR:-$AC_ROOT/backups}"
BRAIN_DB="${HEARTHCORE_BRAIN_DB:-$HOME/hearthcore/runtime/brain.db}"
KEEP="${BACKUP_KEEP:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"

if [[ ! -f "$AC_DIR/.env" ]]; then
  echo "ERROR: $AC_DIR/.env not found." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source "$AC_DIR/.env"
set +a

mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/acore-$STAMP.tar"
STAGE="$BACKUP_DIR/.stage-$STAMP"
mkdir -p "$STAGE"
trap 'rm -rf "$STAGE"' EXIT

cd "$AC_DIR"
echo "[$(date)] Dumping databases -> $OUT"
docker compose exec -T ac-database \
  sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump -uroot --single-transaction --quick --routines --events --databases acore_auth acore_characters acore_world acore_playerbots' \
  | gzip > "$STAGE/database.sql.gz"

if [[ ! -s "$STAGE/database.sql.gz" ]]; then
  echo "ERROR: database dump is empty — aborting." >&2
  exit 1
fi

cp "$AC_DIR/.env" "$STAGE/env"
ARCHIVE_ITEMS=(database.sql.gz env)

if [[ -f "$BRAIN_DB" ]]; then
  python3 -c '
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=10.0)
dst = sqlite3.connect(sys.argv[2])
try:
    src.backup(dst)
finally:
    dst.close()
    src.close()
' "$BRAIN_DB" "$STAGE/brain.db"
  ARCHIVE_ITEMS+=(brain.db)
fi

tar -cf "$OUT" -C "$STAGE" "${ARCHIVE_ITEMS[@]}"
chmod 600 "$OUT"

echo "[$(date)] Backup OK ($(du -h "$OUT" | cut -f1)). Pruning to last $KEEP."
ls -1t "$BACKUP_DIR"/acore-*.tar 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
