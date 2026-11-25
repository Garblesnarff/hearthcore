#!/usr/bin/env bash
# Auto-heals acore_auth.realmlist.flag=2 -> 0 ONLY when ac-worldserver is live and answering on port 8085.
# Safe during intentional stops/builds: does nothing when port 8085 is closed.
set -euo pipefail

# 1. Require ac-worldserver running and returning the 44-byte WotLK auth challenge on 8085
python3 - <<'EOF'
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

# 2. Only update if flag is currently 2 (stuck offline from boot ordering)
FLAG=$(docker exec ac-database sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -B -e "SELECT flag FROM acore_auth.realmlist WHERE id=1;"' 2>/dev/null || true)
if [[ "$FLAG" == "2" ]]; then
  docker exec ac-database sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "UPDATE acore_auth.realmlist SET flag=0 WHERE id=1 AND flag=2;"' 2>/dev/null
  echo "$(date '+%F %T') [heal-realmlist] ac-worldserver is live on 8085; reset realmlist.flag 2 -> 0"
fi
