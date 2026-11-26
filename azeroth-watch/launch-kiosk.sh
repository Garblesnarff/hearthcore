#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AW_PORT="${AZEROTH_WATCH_PORT:-8787}"

# Ensure the lightweight collector daemon is running on cores 0-1
if ! curl -sf "http://127.0.0.1:${AW_PORT}/api/health" >/dev/null 2>&1; then
  systemctl --user start azeroth-watch.service 2>/dev/null || \
    nohup taskset -c 0-1 nice -n 15 python3 "${SCRIPT_DIR}/server.py" >/tmp/azeroth-watch.log 2>&1 &
  for _ in $(seq 1 20); do
    curl -sf "http://127.0.0.1:${AW_PORT}/api/health" >/dev/null 2>&1 && break
    sleep 0.25
  done
fi

exec taskset -c 0-1 nice -n 15 python3 "${SCRIPT_DIR}/kiosk.py" "$@"
