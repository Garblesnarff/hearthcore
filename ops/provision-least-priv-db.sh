#!/usr/bin/env bash
# Provision least-privilege MySQL users (hearthcore_brain + azeroth_watch) and chmod-600
# MySQL client option files inside ac-database's persistent volume (Issues #62 & #75).
# Zero passwords ever appear on argv, stdout, or in git!
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_CONTAINER="${HEARTHCORE_DB_CONTAINER:-ac-database}"
SQL_TEMPLATE="$REPO_ROOT/ops/sql/create_least_priv_users.sql"
BRAIN_CNF="/var/lib/mysql/.hearthcore_brain.cnf"
WATCH_CNF="/var/lib/mysql/.azeroth_watch.cnf"

[[ -f "$SQL_TEMPLATE" ]] || { echo "ERROR: Missing $SQL_TEMPLATE" >&2; exit 1; }

# Generate 32-char alphanumeric random passwords (safe for MySQL option files without escaping)
BRAIN_PW="$(python3 -c 'import secrets, string; a=string.ascii_letters+string.digits; print("".join(secrets.choice(a) for _ in range(32)))')"
WATCH_PW="$(python3 -c 'import secrets, string; a=string.ascii_letters+string.digits; print("".join(secrets.choice(a) for _ in range(32)))')"

# Apply SQL template via stdin (passwords never appear in process argv!)
{
  printf "SET @brain_pw = '%s';\n" "$BRAIN_PW"
  printf "SET @watch_pw = '%s';\n" "$WATCH_PW"
  cat "$SQL_TEMPLATE"
} | docker exec -i "$DB_CONTAINER" sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -u root'

# Write chmod 600 option files inside container persistent volume (via stdin, never argv!)
printf "[client]\nuser=hearthcore_brain\npassword=%s\nhost=localhost\n" "$BRAIN_PW" \
  | docker exec -i "$DB_CONTAINER" sh -c "umask 077 && cat > '$BRAIN_CNF' && chmod 600 '$BRAIN_CNF'"

printf "[client]\nuser=azeroth_watch\npassword=%s\nhost=localhost\n" "$WATCH_PW" \
  | docker exec -i "$DB_CONTAINER" sh -c "umask 077 && cat > '$WATCH_CNF' && chmod 600 '$WATCH_CNF'"

# Smoke-test both least-privilege accounts immediately
docker exec "$DB_CONTAINER" mysql --defaults-extra-file="$BRAIN_CNF" -N -B -e "SELECT CURRENT_USER();" >/dev/null
docker exec "$DB_CONTAINER" mysql --defaults-extra-file="$WATCH_CNF" -N -B -e "SELECT CURRENT_USER();" >/dev/null

echo "[provision-db] Provisioned hearthcore_brain & azeroth_watch successfully."
echo "  Set in hearthcore/.env:"
echo "    HEARTHCORE_DB_CONTAINER_DEFAULTS_FILE=$BRAIN_CNF"
echo "    AZEROTH_WATCH_DB_CONTAINER_DEFAULTS_FILE=$WATCH_CNF"
