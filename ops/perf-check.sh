#!/usr/bin/env bash
# Verifies every Phase 1 + Phase 2 performance key against its expected value.
# Exits 0 if all keys match, 1 if any drifted (e.g. after a config rollback or setup.sh run).
set -uo pipefail

AC="/home/wonky/AzerothCore/azerothcore-wotlk"
ETC="$AC/env/dist/etc"
MOD="$ETC/modules"
WTF="/mnt/hdd/WoW335a/extracted/ChromieCraft_3.3.5a/WTF/Config.wtf"
WOWSH="/home/wonky/bin/wow.sh"
D3D9="/home/wonky/wine-wotlk/drive_c/windows/system32/d3d9.dll"
ENVF="/home/wonky/AzerothCore/.env"

DRIFT=0
check_kv() {
  local file="$1" key="$2" want="$3"
  local got
  got=$(awk -v k="$key" '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    {
      pos = index($0, "=")
      if (pos > 0) {
        lhs = substr($0, 1, pos - 1)
        rhs = substr($0, pos + 1)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", lhs)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", rhs)
        sub(/[[:space:]]*#.*$/, "", rhs)
        gsub(/[[:space:]]+$/, "", rhs)
        if (lhs == k) val = rhs
      }
    }
    END { print val }
  ' "$file" 2>/dev/null)
  if [[ "$got" == "$want" ]]; then
    printf "  [OK]    %-48s = %s\n" "$key" "$got"
  else
    printf "  [DIFF]  %-48s = %s  (expected: %s)\n" "$key" "${got:-<missing>}" "$want"
    DRIFT=$((DRIFT + 1))
  fi
}

check_grep() {
  local label="$1" file="$2" pattern="$3" want_desc="$4"
  if grep -qE "$pattern" "$file" 2>/dev/null; then
    printf "  [OK]    %-48s (%s)\n" "$label" "$want_desc"
  else
    printf "  [DIFF]  %-48s (expected: %s)\n" "$label" "$want_desc"
    DRIFT=$((DRIFT + 1))
  fi
}

echo "=== worldserver.conf ==="
check_kv "$ETC/worldserver.conf" "MapUpdate.Threads" "2"
check_kv "$ETC/worldserver.conf" "MinWorldUpdateTime" "10"
check_kv "$ETC/worldserver.conf" "Visibility.Distance.Continents" "70"
check_kv "$ETC/worldserver.conf" "Warden.Enabled" "0"
check_kv "$ETC/worldserver.conf" "Compression" "0"
check_kv "$ETC/worldserver.conf" "Log.Async.Enable" "0"
check_kv "$ETC/worldserver.conf" "Logger.playerbots" "4,Playerbots"
check_kv "$ETC/worldserver.conf" "MinRecordUpdateTimeDiff" "1"

echo "=== playerbots.conf ==="
check_kv "$MOD/playerbots.conf" "AiPlayerbot.MinRandomBots" "50"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.MaxRandomBots" "50"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.IterationsPerTick" "5"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.ReactDelay" "150"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.SightDistance" "100.0"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.RandomBotAllianceRatio" "100"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.RandomBotHordeRatio" "0"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.LevelBrackets.Dynamic.SyncFactions" "0"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.BotActiveAloneForceWhenInRadius" "110"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.BotActiveAloneForceWhenInZone" "0"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.botActiveAloneSmartScaleDiffLimitCeiling" "120"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.RandomBotMaps" "0,1"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.RandomBotAutoJoinBG" "0"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.RandomBotAutoJoinBGWSCount" "0"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.RandomBotAutoJoinBGABCount" "0"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.RandomBotAutoJoinBGEYCount" "0"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.RandomBotAutoJoinBGRatedArena2v2Count" "0"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.RandomBotAutoJoinBGRatedArena3v3Count" "0"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.RandomBotAutoJoinBGRatedArena5v5Count" "0"
check_kv "$MOD/playerbots.conf" "AiPlayerbot.RandomBotsPerInterval" "25"

echo "=== mod_ahbot.conf ==="
check_kv "$MOD/mod_ahbot.conf" "AuctionHouseBot.MinutesBetweenBuyCycle" "5"
check_kv "$MOD/mod_ahbot.conf" "AuctionHouseBot.MinutesBetweenSellCycle" "5"
check_kv "$MOD/mod_ahbot.conf" "AuctionHouseBot.ItemsPerCycle" "200"
check_kv "$MOD/mod_ahbot.conf" "AuctionHouseBot.ListingExpireTimeInSecondsMin" "14400"
check_kv "$MOD/mod_ahbot.conf" "AuctionHouseBot.Alliance.MinItems" "4000"
check_kv "$MOD/mod_ahbot.conf" "AuctionHouseBot.Alliance.MaxItems" "4000"
check_kv "$MOD/mod_ahbot.conf" "AuctionHouseBot.Horde.MinItems" "0"
check_kv "$MOD/mod_ahbot.conf" "AuctionHouseBot.Horde.MaxItems" "0"
check_kv "$MOD/mod_ahbot.conf" "AuctionHouseBot.Neutral.MinItems" "0"
check_kv "$MOD/mod_ahbot.conf" "AuctionHouseBot.Neutral.MaxItems" "0"
check_kv "$MOD/mod_ahbot.conf" "AuctionHouseBot.ListProportion.CategoryGem.QualityUncommon" "0"
check_kv "$MOD/mod_ahbot.conf" "AuctionHouseBot.ListProportion.CategoryGlyph.QualityNormal" "0"
check_kv "$MOD/mod_ahbot.conf" "AuctionHouseBot.ListProportion.CategoryGeneric.QualityNormal" "0"

echo "=== mod_battleground_bots.conf ==="
check_kv "$MOD/mod_battleground_bots.conf" "BattlegroundBots.Enable" "0"

echo "=== mysql-tuning.cnf ==="
check_kv "$AC/config/mysql-tuning.cnf" "innodb_buffer_pool_size" "256M"
check_kv "$AC/config/mysql-tuning.cnf" "innodb_buffer_pool_instances" "1"
check_kv "$AC/config/mysql-tuning.cnf" "innodb_log_buffer_size" "16M"
check_kv "$AC/config/mysql-tuning.cnf" "performance_schema" "OFF"
check_grep "skip-log-bin" "$AC/config/mysql-tuning.cnf" '^[[:space:]]*skip-log-bin' "present"
check_kv "$AC/config/mysql-tuning.cnf" "max_connections" "40"
check_kv "$AC/config/mysql-tuning.cnf" "table_open_cache" "400"
check_kv "$AC/config/mysql-tuning.cnf" "table_definition_cache" "400"
check_kv "$AC/config/mysql-tuning.cnf" "temptable_max_ram" "64M"

echo "=== docker-compose.override.yml & live containers ==="
check_grep "override cpuset 2-3" "$AC/docker-compose.override.yml" 'cpuset:[[:space:]]*"2-3"' 'cpuset: "2-3"'
for c in ac-worldserver ac-database ac-authserver; do
  live_cpu=$(docker inspect -f '{{.HostConfig.CpusetCpus}}' "$c" 2>/dev/null || echo "missing")
  if [[ "$live_cpu" == "2-3" ]]; then
    printf "  [OK]    %-48s = %s\n" "live $c CpusetCpus" "$live_cpu"
  else
    printf "  [DIFF]  %-48s = %s  (expected: 2-3)\n" "live $c CpusetCpus" "$live_cpu"
    DRIFT=$((DRIFT + 1))
  fi
done

echo "=== .env ==="
check_kv "$ENVF" "MAX_RANDOM_BOTS" "50"
check_kv "$ENVF" "MAP_UPDATE_THREADS" "2"
check_kv "$ENVF" "BOT_SMART_SCALE_CEILING" "120"
check_kv "$ENVF" "RANDOM_BOTS_PER_INTERVAL" "25"
check_kv "$ENVF" "VISIBILITY_DISTANCE_CONTINENTS" "70"
check_kv "$ENVF" "DB_BUFFER_POOL_SIZE" "256M"
check_kv "$ENVF" "DB_BUFFER_POOL_INSTANCES" "1"
check_kv "$ENVF" "AHBOT_MAX_ITEMS" "4000"
check_kv "$ENVF" "BGBOTS_ENABLE" "0"

echo "=== Client (wow.sh, Config.wtf, d3d9.dll) ==="
check_grep "wow.sh WINEDLLOVERRIDES" "$WOWSH" 'WINEDLLOVERRIDES="d3d9=n,b"' 'd3d9=n,b'
check_grep "wow.sh DXVK_STATE_CACHE_PATH" "$WOWSH" 'DXVK_STATE_CACHE_PATH=' 'SSD cache path'
if grep -q 'WINEFSYNC' "$WOWSH" 2>/dev/null; then
  printf "  [DIFF]  %-48s (WINEFSYNC still present; unsupported in stock wine 10.0)\n" "wow.sh WINEFSYNC"
  DRIFT=$((DRIFT + 1))
else
  printf "  [OK]    %-48s (not set)\n" "wow.sh WINEFSYNC removed"
fi
check_grep "Config.wtf maxFPS" "$WTF" '^SET maxFPS "60"' 'SET maxFPS "60"'
check_grep "Config.wtf maxFPSBk" "$WTF" '^SET maxFPSBk "15"' 'SET maxFPSBk "15"'
if grep -qE '^SET gxVSync "0"' "$WTF" 2>/dev/null; then
  printf "  [DIFF]  %-48s (gxVSync disabled; expected default 1)\n" "Config.wtf gxVSync"
  DRIFT=$((DRIFT + 1))
else
  printf "  [OK]    %-48s (enabled / WoW default 1)\n" "Config.wtf gxVSync"
fi
if grep -aqi "dxvk" "$D3D9" 2>/dev/null; then
  printf "  [OK]    %-48s = DXVK\n" "wine d3d9.dll"
else
  printf "  [DIFF]  %-48s = WineD3D  (expected: DXVK)\n" "wine d3d9.dll"
  DRIFT=$((DRIFT + 1))
fi

echo ""
if [[ "$DRIFT" -eq 0 ]]; then
  echo "SUMMARY: ALL KEYS MATCH (0 diffs)"
  exit 0
else
  echo "SUMMARY: $DRIFT key(s) differ from Phase 1/2 performance config!"
  exit 1
fi
