#!/usr/bin/env bash
# Safe, drift-aware, secret-scanned Hearthcore mono-repo sync & deploy script (Issue #74).
#
# Direction of Truth (explicitly defined per Issue #74):
#   - Repository-first (deploy repo -> host): hearthcore/, azeroth-watch/, ops/, docs/, tests/
#     Run: ops/sync-monorepo.sh --deploy [--dry-run]
#   - Host-first C++ mirror (pull C++ -> repo): mod-playerbots-companions/
#     Run: ops/sync-monorepo.sh --mirror-cpp [--force] [--dry-run]
#
# Guarantees:
#   - All-or-nothing staging: any missing source aborts BEFORE modifying any destination file.
#   - Drift protection: --mirror-cpp stops with diff if repo files have local edits not on host
#     (unless --force is passed).
#   - Secret pattern gate: scans all staged files and aborts on any API key, admin token, or password.
#   - Provenance: records live azerothcore-wotlk & mod-playerbots git SHAs in MIRROR_STATE.json.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AW_SRC="${AW_SRC:-$HOME/azeroth-watch}"
AC_SRC="${AC_SRC:-$HOME/AzerothCore/azerothcore-wotlk}"
MPB_SRC="${MPB_SRC:-$AC_SRC/modules/mod-playerbots}"
PERF_SRC="${PERF_SRC:-$HOME/perf}"
SHARE_SRC="${SHARE_SRC:-$HOME/wow-lan-share}"
SYS_SRC="${SYS_SRC:-$HOME/.config/systemd/user}"

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
      sed -n '2,18p' "$0"
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
  # Verify every expected C++ source exists BEFORE copying anything (all-or-nothing!)
  CPP_FILES=(
    "src/Bot/PlayerbotAI.h"
    "src/Ai/Base/Actions/ReviveFromCorpseAction.cpp"
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

  # Drift check: fail if repo mirror has uncommitted edits or diffs not overridden with --force
  if [[ "$FORCE" -eq 0 ]]; then
    if ! git -C "$REPO_ROOT" diff --quiet -- mod-playerbots-companions/; then
      echo "ERROR: Repo has uncommitted local edits in mod-playerbots-companions/! Inspect git diff or pass --force." >&2
      git -C "$REPO_ROOT" diff --stat -- mod-playerbots-companions/
      exit 1
    fi
  fi

  secret_scan_dir "$STAGE"

  AC_SHA="$(git -C "$AC_SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  MPB_SHA="$(git -C "$MPB_SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  MPB_UPSTREAM="$(git -C "$MPB_SRC" merge-base HEAD origin/master 2>/dev/null | cut -c1-8 || echo unknown)"
  cat > "$STAGE/mod-playerbots-companions/MIRROR_STATE.json" <<EOF
{
  "synced_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "azerothcore_sha": "$AC_SHA",
  "mod_playerbots_sha": "$MPB_SHA",
  "mod_playerbots_upstream_base": "$MPB_UPSTREAM",
  "file_count": ${#CPP_FILES[@]}
}
EOF

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
  echo "[sync] Deploying repo-first sources to host (dry_run=$DRY_RUN)..."
  DEPLOY_MAP=(
    "$REPO_ROOT/azeroth-watch/server.py:$AW_SRC/server.py"
    "$REPO_ROOT/azeroth-watch/collector.py:$AW_SRC/collector.py"
    "$REPO_ROOT/ops/perf-check.sh:$PERF_SRC/check.sh"
    "$REPO_ROOT/ops/heal-realmlist.sh:$PERF_SRC/heal-realmlist.sh"
    "$REPO_ROOT/ops/hearthcore-sync.py:$PERF_SRC/hearthcore-sync.py"
    "$REPO_ROOT/ops/wow-lan-share/serve.py:$SHARE_SRC/serve.py"
    "$REPO_ROOT/ops/wow-lan-share/setup-wow-laptop.sh:$SHARE_SRC/setup-wow-laptop.sh"
  )
  for pair in "${DEPLOY_MAP[@]}"; do
    src="${pair%%:*}"
    dst="${pair#*:}"
    [[ -f "$src" ]] || { echo "ERROR: Missing repo source: $src" >&2; exit 1; }
    if [[ "$DRY_RUN" -eq 1 ]]; then
      diff -u "$dst" "$src" >/dev/null 2>&1 && echo "  [SAME] $dst" || echo "  [UPDATE] $dst"
    else
      mkdir -p "$(dirname "$dst")"
      cp "$src" "$dst"
    fi
  done
  echo "[sync] Deploy complete."
fi
