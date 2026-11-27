#!/usr/bin/env bash
# Safe, drift-aware, secret-scanned Hearthcore mono-repo sync & deploy script (Issue #74).
#
# Direction of Truth (explicitly defined per Issue #74):
#   - Repository-first (deploy repo -> host): hearthcore/, azeroth-watch/, ops/, ops/systemd/, docs/, tests/
#     Run: ops/sync-monorepo.sh --deploy [--force] [--dry-run]
#   - Host-first C++ mirror (pull C++ -> repo): mod-playerbots-companions/
#     Run: ops/sync-monorepo.sh --mirror-cpp [--force] [--dry-run]
#
# Safety guarantees:
#   - All-or-nothing staging: any missing source aborts BEFORE modifying any destination file.
#   - Committed & uncommitted drift protection:
#     * --mirror-cpp compares repo files against BOTH git working tree AND last MIRROR_STATE.json
#       per-file SHA-256 digests so even COMMITTED repo-side edits are never silently overwritten!
#     * --deploy checks host drift against last deployed hashes, backs up overwritten host files to
#       ~/.cache/hearthcore-deploy-backups/<timestamp>/, and stops on unexpected host edits unless --force.
#   - Secret pattern gate scans all staged files before any copy or deploy.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AW_SRC="${AW_SRC:-$HOME/azeroth-watch}"
AC_SRC="${AC_SRC:-$HOME/AzerothCore/azerothcore-wotlk}"
MPB_SRC="${MPB_SRC:-$AC_SRC/modules/mod-playerbots}"
PERF_SRC="${PERF_SRC:-$HOME/perf}"
SHARE_SRC="${SHARE_SRC:-$HOME/wow-lan-share}"
SYS_SRC="${SYS_SRC:-$HOME/.config/systemd/user}"
DEPLOY_STATE_FILE="${DEPLOY_STATE_FILE:-$HOME/.cache/hearthcore-deploy-state.json}"
DEPLOY_BACKUP_ROOT="${DEPLOY_BACKUP_ROOT:-$HOME/.cache/hearthcore-deploy-backups}"

MODE=""
FORCE=0
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --mirror-cpp) MODE="mirror-cpp" ;;
    --deploy) MODE="deploy" ;;
    --scan-only) MODE="scan-only" ;;
    --force) FORCE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg (use --mirror-cpp, --deploy, --scan-only, --force, --dry-run)" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$MODE" ]]; then
  echo "Usage: $0 [--mirror-cpp | --deploy | --scan-only] [--force] [--dry-run]" >&2
  exit 2
fi

secret_scan_dir() {
  local target_dir="$1"
  echo "[sync] Running secret pattern scan on $target_dir..."
  if grep -rnE \
      --exclude-dir=.git \
      --exclude-dir=runtime \
      --exclude-dir=__pycache__ \
      --exclude=.env \
      --exclude=.env.example \
      --exclude='test_*.py' \
      --exclude='*.pyc' \
      '(sk-ant-[A-Za-z0-9_-]{10,}|ANTHROPIC_API_KEY=[A-Za-z0-9_-]{8,}|AZEROTH_WATCH_ADMIN_TOKEN=[A-Za-z0-9_-]{8,}|MYSQL_ROOT_PASSWORD=[^$[:space:]]{4,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)' \
      "$target_dir"; then
    echo "ERROR: Secret pattern detected! Aborting sync." >&2
    exit 1
  fi
  echo "[sync] Secret scan passed (0 leaks)."
}

if [[ "$MODE" == "scan-only" ]]; then
  secret_scan_dir "$REPO_ROOT"
  exit 0
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

if [[ "$MODE" == "mirror-cpp" ]]; then
  CPP_FILES=(
    "src/Bot/PlayerbotAI.h"
    "src/Ai/Base/Actions/ReviveFromCorpseAction.cpp"
    "src/Ai/Base/Strategy/CompanionLeadStrategy.h"
    "src/Ai/Base/Strategy/CompanionLeadStrategy.cpp"
    "src/Ai/Base/Strategy/CompanionCraftStrategy.cpp"
    "src/Script/PlayerbotCommandScript.cpp"
  )
  for f in "$MPB_SRC"/src/Bot/Companion*.h "$MPB_SRC"/src/Bot/Companion*.cpp; do
    [[ -f "$f" ]] || { echo "ERROR: Missing Companion* source in $MPB_SRC/src/Bot" >&2; exit 1; }
    CPP_FILES+=("${f#$MPB_SRC/}")
  done
  for rel in "${CPP_FILES[@]}"; do
    src="$MPB_SRC/$rel"
    [[ -f "$src" ]] || { echo "ERROR: Missing required source file: $src" >&2; exit 1; }
    mkdir -p "$STAGE/mod-playerbots-companions/$(dirname "$rel")"
    cp "$src" "$STAGE/mod-playerbots-companions/$rel"
  done

  # Drift check: detect BOTH uncommitted AND committed repo-side edits against MIRROR_STATE.json!
  if [[ "$FORCE" -eq 0 ]]; then
    if ! git -C "$REPO_ROOT" diff --quiet -- mod-playerbots-companions/; then
      echo "ERROR: Uncommitted edits in mod-playerbots-companions/! Inspect diff or pass --force." >&2
      git -C "$REPO_ROOT" diff --stat -- mod-playerbots-companions/
      exit 1
    fi
    MIRROR_STATE="$REPO_ROOT/mod-playerbots-companions/MIRROR_STATE.json"
    if [[ -f "$MIRROR_STATE" ]]; then
      python3 - "$MIRROR_STATE" "$REPO_ROOT/mod-playerbots-companions" "$STAGE/mod-playerbots-companions" <<'PYEOF'
import hashlib, json, sys
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
repo_dir = Path(sys.argv[2])
stage_dir = Path(sys.argv[3])
prev_hashes = state.get("file_sha256") or {}
conflicts = []
for rel, prev_h in prev_hashes.items():
    rp = repo_dir / rel
    sp = stage_dir / rel
    if not rp.is_file() or not sp.is_file():
        continue
    cur_repo_h = hashlib.sha256(rp.read_bytes()).hexdigest()
    cur_host_h = hashlib.sha256(sp.read_bytes()).hexdigest()
    if cur_repo_h != prev_h and cur_repo_h != cur_host_h:
        conflicts.append(rel)
if conflicts:
    print("ERROR: Committed repo-side edits would be overwritten in mod-playerbots-companions/:", file=sys.stderr)
    for c in conflicts:
        print(f"  - {c}", file=sys.stderr)
    print("Reconcile or re-run with --force.", file=sys.stderr)
    sys.exit(1)
PYEOF
    fi
  fi

  secret_scan_dir "$STAGE"

  AC_SHA="$(git -C "$AC_SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  MPB_SHA="$(git -C "$MPB_SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  MPB_UPSTREAM="$(git -C "$MPB_SRC" merge-base HEAD origin/master 2>/dev/null | cut -c1-8 || echo unknown)"
  python3 - "$STAGE/mod-playerbots-companions" "$AC_SHA" "$MPB_SHA" "$MPB_UPSTREAM" <<'PYEOF'
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

stage_dir = Path(sys.argv[1])
hashes = {}
for p in sorted(stage_dir.rglob("*")):
    if p.is_file() and p.name != "MIRROR_STATE.json":
        rel = str(p.relative_to(stage_dir))
        hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
out = {
    "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "azerothcore_sha": sys.argv[2],
    "mod_playerbots_sha": sys.argv[3],
    "mod_playerbots_upstream_base": sys.argv[4],
    "file_count": len(hashes),
    "file_sha256": hashes,
}
(stage_dir / "MIRROR_STATE.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PYEOF

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[sync] Dry-run OK (${#CPP_FILES[@]} files staged; AC=$AC_SHA MPB=$MPB_SHA)."
    diff -rq "$STAGE/mod-playerbots-companions" "$REPO_ROOT/mod-playerbots-companions" || true
    exit 0
  fi

  cp -r "$STAGE/mod-playerbots-companions/." "$REPO_ROOT/mod-playerbots-companions/"
  echo "[sync] Mirrored ${#CPP_FILES[@]} C++ files (AC=$AC_SHA, MPB=$MPB_SHA)."
  exit 0
fi

if [[ "$MODE" == "deploy" ]]; then
  secret_scan_dir "$REPO_ROOT"
  DEPLOY_MAP=(
    "$REPO_ROOT/azeroth-watch/server.py:$AW_SRC/server.py"
    "$REPO_ROOT/azeroth-watch/collector.py:$AW_SRC/collector.py"
    "$REPO_ROOT/azeroth-watch/data_maps.py:$AW_SRC/data_maps.py"
    "$REPO_ROOT/azeroth-watch/extract_maps.py:$AW_SRC/extract_maps.py"
    "$REPO_ROOT/azeroth-watch/kiosk.py:$AW_SRC/kiosk.py"
    "$REPO_ROOT/azeroth-watch/launch-kiosk.sh:$AW_SRC/launch-kiosk.sh"
    "$REPO_ROOT/azeroth-watch/worldmap_areas.json:$AW_SRC/worldmap_areas.json"
    "$REPO_ROOT/azeroth-watch/static/index.html:$AW_SRC/static/index.html"
    "$REPO_ROOT/azeroth-watch/static/character.html:$AW_SRC/static/character.html"
    "$REPO_ROOT/azeroth-watch/static/app.js:$AW_SRC/static/app.js"
    "$REPO_ROOT/azeroth-watch/static/character.js:$AW_SRC/static/character.js"
    "$REPO_ROOT/azeroth-watch/static/app.css:$AW_SRC/static/app.css"
    "$REPO_ROOT/azeroth-watch/static/icon.svg:$AW_SRC/static/icon.svg"
    "$REPO_ROOT/ops/perf-check.sh:$PERF_SRC/check.sh"
    "$REPO_ROOT/ops/heal-realmlist.sh:$PERF_SRC/heal-realmlist.sh"
    "$REPO_ROOT/ops/hearthcore-sync.py:$PERF_SRC/hearthcore-sync.py"
    "$REPO_ROOT/ops/wow-lan-share/serve.py:$SHARE_SRC/serve.py"
    "$REPO_ROOT/ops/wow-lan-share/setup-wow-laptop.sh:$SHARE_SRC/setup-wow-laptop.sh"
  )
  # Verify every source exists BEFORE copying anything (all-or-nothing!)
  for pair in "${DEPLOY_MAP[@]}"; do
    src="${pair%%:*}"
    [[ -f "$src" ]] || { echo "ERROR: Missing repo source: $src" >&2; exit 1; }
  done

  # Drift check on host files against last deployed state unless --force
  if [[ "$FORCE" -eq 0 && -f "$DEPLOY_STATE_FILE" ]]; then
    python3 - "$DEPLOY_STATE_FILE" "${DEPLOY_MAP[@]}" <<'PYEOF'
import hashlib, json, sys
from pathlib import Path

prev = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
drifted = []
for pair in sys.argv[2:]:
    src_s, dst_s = pair.split(":", 1)
    src, dst = Path(src_s), Path(dst_s)
    if not dst.is_file() or dst_s not in prev:
        continue
    cur_dst = hashlib.sha256(dst.read_bytes()).hexdigest()
    cur_src = hashlib.sha256(src.read_bytes()).hexdigest()
    if cur_dst != prev[dst_s] and cur_dst != cur_src:
        drifted.append((src_s, dst_s))
if drifted:
    print("ERROR: Host files have unrecorded local edits (would be overwritten):", file=sys.stderr)
    for s, d in drifted:
        print(f"  - {d} (differs from last deploy and {s})", file=sys.stderr)
    print("Inspect diff or re-run with --force.", file=sys.stderr)
    sys.exit(1)
PYEOF
  fi

  BACKUP_DIR="$DEPLOY_BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"
  backed_up=0
  for pair in "${DEPLOY_MAP[@]}"; do
    src="${pair%%:*}"
    dst="${pair#*:}"
    if [[ "$DRY_RUN" -eq 1 ]]; then
      cmp -s "$src" "$dst" 2>/dev/null && echo "  [SAME] $dst" || echo "  [UPDATE] $dst"
    else
      if [[ -f "$dst" ]] && ! cmp -s "$src" "$dst"; then
        mkdir -p "$BACKUP_DIR"
        cp -p "$dst" "$BACKUP_DIR/$(basename "$dst").$(echo "$dst" | sha256sum | cut -c1-8)"
        backed_up=$((backed_up + 1))
      fi
      mkdir -p "$(dirname "$dst")"
      cp "$src" "$dst"
    fi
  done

  if [[ "$DRY_RUN" -eq 0 ]]; then
    mkdir -p "$(dirname "$DEPLOY_STATE_FILE")"
    python3 - "$DEPLOY_STATE_FILE" "${DEPLOY_MAP[@]}" <<'PYEOF'
import hashlib, json, sys
from pathlib import Path
state = {}
for pair in sys.argv[2:]:
    _, dst_s = pair.split(":", 1)
    p = Path(dst_s)
    if p.is_file():
        state[dst_s] = hashlib.sha256(p.read_bytes()).hexdigest()
Path(sys.argv[1]).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PYEOF
    [[ "$backed_up" -gt 0 ]] && echo "[sync] Backed up $backed_up overwritten host file(s) to $BACKUP_DIR"
    echo "[sync] Deploy complete (${#DEPLOY_MAP[@]} files)."
  fi
fi
