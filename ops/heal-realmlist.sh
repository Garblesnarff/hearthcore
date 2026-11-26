#!/usr/bin/env bash
# Auto-heals acore_auth.realmlist.flag=2 -> 0 ONLY when ac-worldserver is live and answering on port 8085.
# Safe during intentional stops/builds: exits 0 cleanly when port 8085 is closed OR maintenance flag exists.
#
# Maintenance override (Issue #75):
#   touch ~/hearthcore/runtime/maintenance.flag   # pause auto-heal during deliberate maintenance
#   rm -f ~/hearthcore/runtime/maintenance.flag   # resume auto-heal
set -euo pipefail

MAINT_FLAG="${HEARTHCORE_MAINTENANCE_FLAG:-$HOME/hearthcore/runtime/maintenance.flag}"
if [[ -f "$MAINT_FLAG" ]]; then
  echo "$(date '+%F %T') [heal-realmlist] Maintenance flag present ($MAINT_FLAG); skipping auto-heal."
  exit 0
fi

# 1. Require ac-worldserver running and returning the 44-byte WotLK auth challenge on 8085.
#    During deliberate stops/builds port 8085 is closed -> exit 0 cleanly (not an alertable error!).
if ! python3 - <<'EOF'
import socket, sys
try:
    s = socket.create_connection(("127.0.0.1", 8085), timeout=3)
    data = s.recv(128)
    s.close()
    if len(data) < 40:
        sys.exit(1)
except Exception:
    sys.exit(1)
EOF
then
  exit 0
fi

# 2. Only update if flag is currently 2 (stuck offline from boot ordering)
DB_CONTAINER="${HEARTHCORE_DB_CONTAINER:-ac-database}"
FLAG=$(docker exec "$DB_CONTAINER" sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -N -B -e "SELECT flag FROM acore_auth.realmlist WHERE id=1;"' 2>/dev/null || true)
if [[ "$FLAG" == "2" ]]; then
  docker exec "$DB_CONTAINER" sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -e "UPDATE acore_auth.realmlist SET flag=0 WHERE id=1 AND flag=2;"' 2>/dev/null
  echo "$(date '+%F %T') [heal-realmlist] ac-worldserver is live on 8085; reset realmlist.flag 2 -> 0"
fi
