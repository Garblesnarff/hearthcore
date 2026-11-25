#!/usr/bin/env bash
# Sync live Hearthguard host sources into the public-safe Hearthcore mono-repo.
# Automatically excludes secrets (.env, .my.cnf, .db.env), runtime DBs, caches,
# and copyrighted Blizzard MPQ extracted artwork (.png/.jpg).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AW_SRC="${AW_SRC:-$HOME/azeroth-watch}"
MPB_SRC="${MPB_SRC:-$HOME/AzerothCore/azerothcore-wotlk/modules/mod-playerbots}"
PERF_SRC="${PERF_SRC:-$HOME/perf}"
SHARE_SRC="${SHARE_SRC:-$HOME/wow-lan-share}"
SYS_SRC="${SYS_SRC:-$HOME/.config/systemd/user}"

echo "[sync] 1/4 Azeroth Watch (source only; no MPQ maps/icons)..."
mkdir -p "$REPO_ROOT/azeroth-watch/static"
for f in server.py collector.py data_maps.py extract_maps.py kiosk.py launch-kiosk.sh worldmap_areas.json; do
  [[ -f "$AW_SRC/$f" ]] && cp -f "$AW_SRC/$f" "$REPO_ROOT/azeroth-watch/$f"
done
for f in index.html app.js app.css icon.svg; do
  [[ -f "$AW_SRC/static/$f" ]] && cp -f "$AW_SRC/static/$f" "$REPO_ROOT/azeroth-watch/static/$f"
done

echo "[sync] 2/4 mod-playerbots companion C++ mirror..."
mkdir -p "$REPO_ROOT/mod-playerbots-companions/src/Bot" \
         "$REPO_ROOT/mod-playerbots-companions/src/Ai/Base/Actions" \
         "$REPO_ROOT/mod-playerbots-companions/src/Ai/Base/Strategy" \
         "$REPO_ROOT/mod-playerbots-companions/src/Script"
cp -f "$MPB_SRC"/src/Bot/Companion*.{h,cpp} "$REPO_ROOT/mod-playerbots-companions/src/Bot/"
cp -f "$MPB_SRC/src/Bot/PlayerbotAI.h" "$REPO_ROOT/mod-playerbots-companions/src/Bot/"
cp -f "$MPB_SRC/src/Ai/Base/Actions/ReviveFromCorpseAction.cpp" "$REPO_ROOT/mod-playerbots-companions/src/Ai/Base/Actions/"
cp -f "$MPB_SRC/src/Ai/Base/Strategy/CompanionLeadStrategy.cpp" "$REPO_ROOT/mod-playerbots-companions/src/Ai/Base/Strategy/"
cp -f "$MPB_SRC/src/Ai/Base/Strategy/CompanionCraftStrategy.cpp" "$REPO_ROOT/mod-playerbots-companions/src/Ai/Base/Strategy/"
cp -f "$MPB_SRC/src/Script/PlayerbotCommandScript.cpp" "$REPO_ROOT/mod-playerbots-companions/src/Script/"

echo "[sync] 3/4 Operational scripts & LAN laptop installer..."
mkdir -p "$REPO_ROOT/ops/wow-lan-share" "$REPO_ROOT/ops/systemd"
cp -f "$PERF_SRC/check.sh" "$REPO_ROOT/ops/perf-check.sh"
cp -f "$PERF_SRC/heal-realmlist.sh" "$REPO_ROOT/ops/heal-realmlist.sh"
cp -f "$PERF_SRC/hearthcore-sync.py" "$REPO_ROOT/ops/hearthcore-sync.py"
cp -f "$SHARE_SRC/serve.py" "$REPO_ROOT/ops/wow-lan-share/serve.py"
cp -f "$SHARE_SRC/setup-wow-laptop.sh" "$REPO_ROOT/ops/wow-lan-share/setup-wow-laptop.sh"

echo "[sync] 4/4 systemd user units..."
for u in azeroth-realmlist-heal.service azeroth-realmlist-heal.timer \
         azeroth-watch.service \
         hearthcore-brain.service hearthcore-brain.timer \
         hearthcore-quartermaster.service hearthcore-quartermaster.timer \
         hearthcore-failure@.service \
         wow-lan-share.service; do
  [[ -f "$SYS_SRC/$u" ]] && cp -f "$SYS_SRC/$u" "$REPO_ROOT/ops/systemd/$u"
done

echo "[sync] Done! Run 'git status' and audit diff before committing."
