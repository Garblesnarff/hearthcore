/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "CompanionQuest.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <limits>
#include <list>
#include <map>
#include <mutex>
#include <set>
#include <sstream>
#include <unordered_map>
#include <unordered_set>
#include <variant>

#include "CellImpl.h"
#include "CompanionBotMgr.h"
#include "CompanionBrain.h"
#include "CompanionDuty.h"
#include "CompanionLease.h"
#include "CompanionLife.h"
#include "CompanionTransport.h"
#include "CompanionTrip.h"
#include "Config.h"
#include "Creature.h"
#include "DBCStores.h"
#include "DatabaseEnv.h"
#include "GameObject.h"
#include "GameTime.h"
#include "GridNotifiers.h"
#include "GridNotifiersImpl.h"
#include "Item.h"
#include "ItemTemplate.h"
#include "Log.h"
#include "Map.h"
#include "MapMgr.h"
#include "NewRpgInfo.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "QuestDef.h"
#include "QuestPackets.h"
#include "SharedDefines.h"
#include "Spell.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include "WorldPacket.h"
#include "WorldSession.h"

namespace
{
constexpr uint32 COMPANION_MAPS[] = {0, 1, 530, 571};
constexpr float WALK_SPEED = 7.0f;          // yards per second on foot
constexpr float MOUNT_FACTOR = 1.6f;        // apprentice ground mount (+60%)
constexpr float FLIGHT_SPEED = 25.0f;       // yards per second over the straight node distance (splines are longer)
constexpr float PATH_FACTOR = 1.25f;        // ground path vs straight line
constexpr float FLIGHT_MIN_YARDS = 1200.0f; // shorter trips never fly
constexpr float FLIGHT_GAIN_SECONDS = 60.0f;// a flight must save this much over walking
constexpr float TRAVEL_DIVISOR = 15.0f;     // travel cost = seconds / 15
constexpr float TRAVEL_CAP = 80.0f;
constexpr uint32 DO_QUEST_RECHECK_SECONDS = 600;   // a quest worked here by new-rpg is re-checked this often
constexpr float TURNIN_BASE = 85.0f;    // a completed quest comes first (card: turn in, need high); was 60
constexpr float HUB_MAX_YARDS = 8000.0f;
constexpr size_t HUB_CANDIDATES = 40;
constexpr float POI_HERE_YARDS = 1500.0f;
constexpr uint32 NO_CROSS_MAP = std::numeric_limits<uint32>::max();
constexpr uint32 PICK_LOG_SECONDS = 300;
constexpr uint32 ABANDON_CHECK_SECONDS = 300;
constexpr uint32 MOUNT_LOG_SECONDS = 600;
constexpr uint32 MOUNT_RETRY_SEC = 10;          // between mount attempts
constexpr uint32 REP_RELOAD_MS = 600000;
constexpr uint32 SWITCH_RELOAD_MS = 30000;        // runtime switches re-read (reload config)
constexpr uint32 OWNER_LIVE_MS = 10000;           // owner online: copy of his live quest log
constexpr uint32 OWNER_DB_MS = 300000;            // owner offline: character_queststatus
constexpr uint32 OWNER_LOGOUT_DB_DELAY_MS = 60000;// after logout the DB save may lag: keep the live copy this long
constexpr uint32 SKIP_LOG_SECONDS = 600;          // abandon skip / accept skip lines per quest
constexpr size_t TOP_OPTIONS = 5;                 // options kept for "why"

// ---- configuration (world thread writes once, then publishes gConfigLoaded) ----
std::atomic<bool> gConfigLoaded{false};
std::atomic<bool> gEnabled{true};
std::atomic<bool> gMountEnabled{true};
std::atomic<bool> gAcceptGrey{true};
std::atomic<bool> gFlightEnabled{true};
std::atomic<bool> gAnyRaceVendor{true};
std::atomic<float> gHubRadius{150.0f};
std::atomic<float> gMountMinDistance{40.0f};
std::atomic<bool> gDoQuestSearch{true};
std::atomic<float> gDoQuestSearchYards{80.0f};
std::atomic<uint32> gDoQuestStallMinutes{6};
std::atomic<uint32> gEvalSeconds{30};
std::atomic<float> gWTurnIn{1.0f};
std::atomic<float> gWWork{1.0f};
std::atomic<float> gWHub{1.0f};
std::atomic<float> gWGrind{10.0f};
std::atomic<float> gWMount{1.0f};
std::atomic<float> gCompletionist{0.5f};
std::atomic<bool> gKeepOwnerQuests{true};
std::atomic<uint32> gFreeSlots{3};
std::atomic<uint32> gMountGoldReserve{1};
std::atomic<float> gGreyMinValue{25.0f};
std::atomic<float> gWaitBonusPerMin{1.5f};
std::atomic<float> gWaitBonusMax{60.0f};
std::atomic<bool> gCrossWork{true};
std::atomic<uint32> gCrossMinGoals{3};
std::atomic<float> gCrossPenalty{10.0f};
std::atomic<uint32> gCrossStayMaxMinutes{90};

// ---- owner quest log copy (world thread writes, map threads read under gOwnerMutex) ----
std::mutex gOwnerMutex;
std::unordered_set<uint32> gOwnerQuests;
std::atomic<bool> gOwnerQuestsKnown{false};
std::unordered_map<uint32, uint32> gRaceVendors;   // race id -> mount vendor creature entry (written before publish)

// ---- shared tables (world thread builds once, then publishes gTablesReady; read-only afterwards) ----
struct EnderSpot
{
    uint32 spawnId = 0;
    bool isGo = false;
    uint32 map = 0;
    uint32 zone = 0;
    float x = 0.0f, y = 0.0f, z = 0.0f;
    uint32 hostileMask = 0;
};

struct Hub
{
    uint32 map = 0;
    uint32 zone = 0;
    float x = 0.0f, y = 0.0f, z = 0.0f;
    std::vector<uint32> spots;   // indices into CompanionBotMgr::QuestGiverSpots()
};

struct MountOffer
{
    uint32 map = 0;
    uint32 zone = 0;
    uint32 spawnId = 0;
    uint32 entry = 0;
    float x = 0.0f, y = 0.0f, z = 0.0f;
    uint32 itemId = 0;
    uint32 spellId = 0;   // taught spell
    uint32 price = 0;     // list price (copper), before the reputation discount
    uint32 races = 0;     // item AllowableRace (0 = all)
    uint32 skillRank = 0; // riding skill needed
    uint32 hostileMask = 0;
};

std::atomic<bool> gTablesReady{false};
std::unordered_map<uint32, std::vector<EnderSpot>> gEnders;   // quest id -> ender spawns
std::vector<Hub> gHubs;
std::vector<MountOffer> gMountOffers;

std::mutex gRepMutex;
std::unordered_map<uint32, std::vector<std::pair<uint32, uint32>>> gRepGoals;   // low guid -> (faction, target rank)

// ---- per-bot state ----
struct QuestBotState
{
    uint32 lastEvalSec = 0;
    CompanionQuest::Trip trip;
    bool grindAllowed = false;
    std::string lastPick;
    uint32 lastPickLogSec = 0;
    std::vector<std::string> top;
    std::unordered_set<uint32> completeInLog;
    bool logInit = false;
    std::unordered_map<uint32, uint32> workFails;    // quest id -> failed work trips this session
    std::unordered_set<std::string> blockedLogged;   // "kind:id" already logged as blocked
    uint32 lastMountLogSec = 0;
    std::string mountStatus;
    uint32 lastMountTrySec = 0;
    uint32 lastAbandonCheckSec = 0;
    uint32 pendingAbandon = 0;                        // GM test hook (.companion abandon), taken on the map thread
    uint32 lastLogFullDropSec = 0;
    uint32 lastHereQuest = 0;                         // quest handed to new-rpg DO_QUEST, and when
    uint32 lastHereSec = 0;
    std::unordered_map<uint32, uint32> skipUntil;     // quest id -> second until which it is not offered as work
    uint32 turnIns = 0;
    uint32 doQuestId = 0;                             // new-rpg DO_QUEST quest seen, and since when
    uint32 doQuestSince = 0;
    std::unordered_map<uint64, uint32> skipLogSec;    // skip-line key -> second of the last line
    std::unordered_map<uint32, uint32> completeSince; // complete quest -> second first seen complete (B4)
    std::vector<std::string> blockedNow;              // blocked reasons of the last full evaluation (why)
    std::vector<std::string> completeLines;           // every complete quest: score or block reason (why)
    std::string lastBlock;                            // last reason the project did not start a trip
    uint32 lastBlockSec = 0;
    uint32 lastFullEvalSec = 0;
    uint32 logUsed = 0;
    // Map of the last cross-continent quest trip (D route stay), NO_CROSS_MAP when none. Not 0: map 0 is Eastern
    // Kingdoms, so a zero default made every companion on map 0 look like an arrival (stay from the first tick).
    uint32 crossToMap = NO_CROSS_MAP;
    uint32 crossArriveSec = 0;  // first evaluation on that map (0: not arrived yet)
    bool crossDoneLogged = false;   // "trip cross done" logged for this crossing
    std::unordered_map<uint32, std::pair<std::string, uint32>> crossLog;   // map -> (last result, second logged)
};

std::mutex gStateMutex;
std::unordered_map<uint32, QuestBotState> gStates;

uint32 NowSec()
{
    return static_cast<uint32>(GameTime::GetGameTime().count());
}

std::string Num(float value, int precision = 1)
{
    std::ostringstream out;
    out << std::fixed << std::setprecision(precision) << value;
    return out.str();
}

char const* KindName(CompanionQuest::TripKind kind)
{
    switch (kind)
    {
        case CompanionQuest::TripKind::TurnIn: return "turnin";
        case CompanionQuest::TripKind::Work: return "work";
        case CompanionQuest::TripKind::Hub: return "hub";
        case CompanionQuest::TripKind::Mount: return "mount";
        case CompanionQuest::TripKind::Travel: return "travel";
        default: return "none";
    }
}

uint32 ZoneAt(uint32 mapId, float x, float y, float z)
{
    Map* map = sMapMgr->FindBaseMap(mapId);
    return map ? map->GetZoneId(PHASEMASK_NORMAL, x, y, z) : 0;
}

bool OnCompanionMap(uint32 mapId)
{
    return std::find(std::begin(COMPANION_MAPS), std::end(COMPANION_MAPS), mapId) != std::end(COMPANION_MAPS);
}

float Dist2d(float ax, float ay, float bx, float by)
{
    float dx = ax - bx;
    float dy = ay - by;
    return std::sqrt(dx * dx + dy * dy);
}

// A zone is safe for a trip unless its mobs start more than 3 levels above the bot (lower zones are always fine).
bool ZoneSafe(uint32 zone, uint32 level)
{
    uint32 lo = 0;
    uint32 hi = 0;
    if (!sCompanionBotMgr.ZoneLevelRange(zone, lo, hi))
        return true;
    return lo <= level + 3;
}

// ---------------------------------------------------------------------------------------------------------------
// Configuration and tables (world thread)
// ---------------------------------------------------------------------------------------------------------------
float ReadFloat(char const* key, float fallback)
{
    float value = sConfigMgr->GetOption<float>(key, fallback);
    return std::isfinite(value) && value >= 0.0f ? value : fallback;
}

// Runtime switches (world thread, at load and every SWITCH_RELOAD_MS, so "reload config" applies them).
void LoadSwitches()
{
    auto readFloat = ReadFloat;
    gEnabled.store(sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.Quest.Enabled", true));
    gAcceptGrey.store(sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.Quest.AcceptGrey", true));
    gFlightEnabled.store(sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.Quest.Flight.Enabled", true));
    gHubRadius.store(readFloat("AiPlayerbot.Companion.Quest.HubRadius", 150.0f));
    gEvalSeconds.store(std::max(10u, sConfigMgr->GetOption<uint32>("AiPlayerbot.Companion.Quest.EvalSeconds", 30)));
    gMountEnabled.store(sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.Mount.Enabled", true));
    gMountMinDistance.store(readFloat("AiPlayerbot.Companion.Mount.MinDistance", 40.0f));
    gAnyRaceVendor.store(sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.Mount.AnyRaceVendor", true));
    gWTurnIn.store(readFloat("AiPlayerbot.Companion.Weight.QuestTurnIn", 1.0f));
    gWWork.store(readFloat("AiPlayerbot.Companion.Weight.QuestWork", 1.0f));
    gWHub.store(readFloat("AiPlayerbot.Companion.Weight.QuestHub", 1.0f));
    gWGrind.store(readFloat("AiPlayerbot.Companion.Weight.QuestGrind", 10.0f));
    gWMount.store(readFloat("AiPlayerbot.Companion.Weight.Mount", 1.0f));
    gCompletionist.store(std::min(1.0f, readFloat("AiPlayerbot.Companion.Weight.Completionist", 0.5f)));
    gKeepOwnerQuests.store(sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.Quest.KeepOwnerQuests", true));
    gFreeSlots.store(std::min(20u, sConfigMgr->GetOption<uint32>("AiPlayerbot.Companion.Quest.FreeSlots", 3)));
    gMountGoldReserve.store(sConfigMgr->GetOption<uint32>("AiPlayerbot.Companion.Quest.MountGoldReserve", 1));
    gGreyMinValue.store(readFloat("AiPlayerbot.Companion.Quest.GreyMinValue", 25.0f));
    gWaitBonusPerMin.store(readFloat("AiPlayerbot.Companion.Quest.TurnInWaitBonusPerMin", 1.5f));
    gWaitBonusMax.store(readFloat("AiPlayerbot.Companion.Quest.TurnInWaitBonusMax", 60.0f));
    gCrossWork.store(sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.Quest.CrossContinentWork", true));
    gCrossMinGoals.store(std::max(1u, sConfigMgr->GetOption<uint32>("AiPlayerbot.Companion.Quest.CrossMinGoals", 3)));
    gCrossPenalty.store(readFloat("AiPlayerbot.Companion.Quest.CrossPenalty", 10.0f));
    gCrossStayMaxMinutes.store(sConfigMgr->GetOption<uint32>("AiPlayerbot.Companion.Quest.CrossStayMaxMinutes", 90));
    gDoQuestSearch.store(sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.DoQuest.Search", true));
    gDoQuestSearchYards.store(readFloat("AiPlayerbot.Companion.DoQuest.SearchYards", 80.0f));
    gDoQuestStallMinutes.store(
        std::max(1u, sConfigMgr->GetOption<uint32>("AiPlayerbot.Companion.DoQuest.StallMinutes", 6)));
}

void LoadConfig()
{
    LoadSwitches();

    // "race:entry,race:entry": the mount vendor of each race (owner's rule: buy at the race's own vendor first).
    std::string raw =
        sConfigMgr->GetOption<std::string>("AiPlayerbot.Companion.Mount.RaceVendors", "1:384,3:1261,4:4730,11:17584");
    raw.erase(std::remove(raw.begin(), raw.end(), '"'), raw.end());
    std::stringstream in(raw);
    std::string token;
    while (std::getline(in, token, ','))
    {
        size_t colon = token.find(':');
        if (colon == std::string::npos)
            continue;
        try
        {
            uint32 race = static_cast<uint32>(std::stoul(token.substr(0, colon)));
            uint32 entry = static_cast<uint32>(std::stoul(token.substr(colon + 1)));
            gRaceVendors[race] = entry;
        }
        catch (std::exception const&)
        {
        }
    }
    LOG_INFO("playerbots", "Companion quest: enabled={} acceptGrey={} flight={} hubRadius={} mount={} minDistance={} "
             "anyRaceVendor={} raceVendors={} keepOwnerQuests={} freeSlots={} greyMinValue={} waitBonus={}/min max={}",
             gEnabled.load(), gAcceptGrey.load(), gFlightEnabled.load(), gHubRadius.load(), gMountEnabled.load(),
             gMountMinDistance.load(), gAnyRaceVendor.load(), raw, gKeepOwnerQuests.load(), gFreeSlots.load(),
             gGreyMinValue.load(), gWaitBonusPerMin.load(), gWaitBonusMax.load());
}

void LoadEnders()
{
    auto load = [](char const* sql, bool isGo)
    {
        QueryResult result = WorldDatabase.Query(sql);
        if (!result)
            return;
        do
        {
            Field* fields = result->Fetch();
            EnderSpot spot;
            spot.spawnId = fields[0].Get<uint32>();
            spot.isGo = isGo;
            spot.map = fields[1].Get<uint32>();
            spot.x = fields[2].Get<float>();
            spot.y = fields[3].Get<float>();
            spot.z = fields[4].Get<float>();
            uint32 questId = fields[5].Get<uint32>();
            uint32 entry = fields[6].Get<uint32>();
            if (!OnCompanionMap(spot.map) || !sObjectMgr->GetQuestTemplate(questId))
                continue;
            if (!isGo)
            {
                CreatureTemplate const* cInfo = sObjectMgr->GetCreatureTemplate(entry);
                FactionTemplateEntry const* faction =
                    cInfo ? sFactionTemplateStore.LookupEntry(cInfo->faction) : nullptr;
                spot.hostileMask = faction ? faction->hostileMask : 0;
            }
            spot.zone = ZoneAt(spot.map, spot.x, spot.y, spot.z);
            gEnders[questId].push_back(spot);
        } while (result->NextRow());
    };
    // Every ender (creature or game object) of every quest: a completed quest of any kind can be turned in.
    load("SELECT c.guid, c.map, c.position_x, c.position_y, c.position_z, cq.quest, c.id FROM creature c "
         "JOIN creature_questender cq ON cq.id = c.id",
         false);
    load("SELECT g.guid, g.map, g.position_x, g.position_y, g.position_z, gq.quest, g.id FROM gameobject g "
         "JOIN gameobject_questender gq ON gq.id = g.id",
         true);
}

void BuildHubs()
{
    std::vector<CompanionBotMgr::QuestGiverSpot> const& spots = sCompanionBotMgr.QuestGiverSpots();
    // Unique giver positions (one spawn gives several quests).
    struct GiverPos
    {
        uint32 map = 0;
        uint32 zone = 0;
        float x = 0.0f, y = 0.0f, z = 0.0f;
        std::vector<uint32> idx;
        bool used = false;
    };
    std::vector<uint32> order(spots.size());
    for (uint32 i = 0; i < order.size(); ++i)
        order[i] = i;
    std::sort(order.begin(), order.end(),
              [&spots](uint32 a, uint32 b)
              {
                  if (spots[a].map != spots[b].map)
                      return spots[a].map < spots[b].map;
                  if (spots[a].x != spots[b].x)
                      return spots[a].x < spots[b].x;
                  return spots[a].y < spots[b].y;
              });
    std::vector<GiverPos> givers;
    for (uint32 i : order)
    {
        CompanionBotMgr::QuestGiverSpot const& spot = spots[i];
        if (!givers.empty() && givers.back().map == spot.map && givers.back().x == spot.x && givers.back().y == spot.y)
        {
            givers.back().idx.push_back(i);
            continue;
        }
        GiverPos pos;
        pos.map = spot.map;
        pos.zone = spot.zoneId;
        pos.x = spot.x;
        pos.y = spot.y;
        pos.z = spot.z;
        pos.idx.push_back(i);
        givers.push_back(pos);
    }

    // Greedy clusters: the giver with the most quests is a hub centre; every free giver within the radius joins it.
    std::vector<uint32> byQuests(givers.size());
    for (uint32 i = 0; i < byQuests.size(); ++i)
        byQuests[i] = i;
    std::sort(byQuests.begin(), byQuests.end(),
              [&givers](uint32 a, uint32 b) { return givers[a].idx.size() > givers[b].idx.size(); });
    float const radius = gHubRadius.load();
    for (uint32 c : byQuests)
    {
        if (givers[c].used)
            continue;
        Hub hub;
        hub.map = givers[c].map;
        hub.zone = givers[c].zone;
        hub.x = givers[c].x;
        hub.y = givers[c].y;
        hub.z = givers[c].z;
        for (GiverPos& g : givers)
        {
            if (g.used || g.map != hub.map || Dist2d(g.x, g.y, hub.x, hub.y) > radius)
                continue;
            g.used = true;
            hub.spots.insert(hub.spots.end(), g.idx.begin(), g.idx.end());
        }
        gHubs.push_back(std::move(hub));
    }
}

void BuildMountOffers()
{
    for (uint32 mapId : COMPANION_MAPS)
    {
        for (CompanionBotMgr::DutySpot const& spot :
             sCompanionBotMgr.DutySpots(mapId, CompanionBotMgr::DutySpotKind::Vendor))
        {
            VendorItemData const* items = sObjectMgr->GetNpcVendorItemList(spot.entry);
            if (!items)
                continue;
            for (VendorItem const* vendorItem : items->m_items)
            {
                if (!vendorItem || vendorItem->ExtendedCost)
                    continue;   // gold only
                ItemTemplate const* proto = sObjectMgr->GetItemTemplate(vendorItem->item);
                if (!proto || proto->BuyPrice <= 0 || proto->RequiredSkill != SKILL_RIDING)
                    continue;
                uint32 taught = 0;
                for (auto const& spell : proto->Spells)
                    if (spell.SpellTrigger == ITEM_SPELLTRIGGER_LEARN_SPELL_ID && spell.SpellId > 0)
                        taught = static_cast<uint32>(spell.SpellId);
                SpellInfo const* info = taught ? sSpellMgr->GetSpellInfo(taught) : nullptr;
                if (!info || info->Effects[0].ApplyAuraName != SPELL_AURA_MOUNTED)
                    continue;
                if (info->Effects[1].ApplyAuraName == SPELL_AURA_MOD_INCREASE_MOUNTED_FLIGHT_SPEED ||
                    info->Effects[2].ApplyAuraName == SPELL_AURA_MOD_INCREASE_MOUNTED_FLIGHT_SPEED)
                    continue;   // ground mounts only
                MountOffer offer;
                offer.map = mapId;
                offer.zone = spot.zoneId;
                offer.spawnId = spot.spawnId;
                offer.entry = spot.entry;
                offer.x = spot.x;
                offer.y = spot.y;
                offer.z = spot.z;
                offer.itemId = proto->ItemId;
                offer.spellId = taught;
                offer.price = static_cast<uint32>(proto->BuyPrice);
                offer.races = proto->AllowableRace;
                offer.skillRank = proto->RequiredSkillRank;
                offer.hostileMask = spot.hostileMask;
                gMountOffers.push_back(offer);
            }
        }
    }
}

uint64 ProcessRssKB()
{
    std::ifstream status("/proc/self/status");
    std::string line;
    while (std::getline(status, line))
    {
        if (line.rfind("VmRSS:", 0) == 0)
        {
            std::istringstream in(line.substr(6));
            uint64 kb = 0;
            in >> kb;
            return kb;
        }
    }
    return 0;
}

void BuildTables()
{
    uint64 const rssBefore = ProcessRssKB();
    LoadEnders();
    BuildHubs();
    BuildMountOffers();

    // Approximate sizes: element bytes plus ~32 bytes per hash node / vector header.
    size_t endersBytes = 0;
    for (auto const& [quest, list] : gEnders)
        endersBytes += 32 + sizeof(quest) + sizeof(list) + list.capacity() * sizeof(EnderSpot);
    size_t hubsBytes = gHubs.capacity() * sizeof(Hub);
    for (Hub const& hub : gHubs)
        hubsBytes += hub.spots.capacity() * sizeof(uint32);
    size_t offersBytes = gMountOffers.capacity() * sizeof(MountOffer);
    size_t spotsBytes = sCompanionBotMgr.QuestGiverSpots().capacity() * sizeof(CompanionBotMgr::QuestGiverSpot);
    size_t zonesBytes = sCompanionBotMgr.ZoneLevelCount() * (32 + 3 * sizeof(uint32));
    size_t dutyBytes = 0;
    for (uint32 mapId : COMPANION_MAPS)
        for (uint8 kind = 0; kind < 4; ++kind)
            dutyBytes += sCompanionBotMgr.DutySpots(mapId, static_cast<CompanionBotMgr::DutySpotKind>(kind)).capacity() *
                         sizeof(CompanionBotMgr::DutySpot);
    size_t enderSpotsOld = sCompanionBotMgr.QuestEnderSpotCount() * sizeof(CompanionBotMgr::QuestGiverSpot);
    LOG_INFO("playerbots",
             "Companion tables: zones={} KB questGiverSpots={} KB oldEnderSpots={} KB questEnders={} KB dutySpots={} KB "
             "hubs={} KB mountVendors={} KB (counts: hubs={} quests-with-enders={} mountOffers={}) shared by all "
             "companions; rss before={} KB after={} KB",
             zonesBytes / 1024, spotsBytes / 1024, enderSpotsOld / 1024, endersBytes / 1024, dutyBytes / 1024,
             hubsBytes / 1024, offersBytes / 1024, gHubs.size(), gEnders.size(), gMountOffers.size(), rssBefore,
             ProcessRssKB());
}

void LoadRepGoals()
{
    // A SELECT on a missing table aborts the core, so probe information_schema first.
    QueryResult probe = CharacterDatabase.Query(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() "
        "AND table_name = 'companion_rep_goals'");
    std::unordered_map<uint32, std::vector<std::pair<uint32, uint32>>> goals;
    if (probe && probe->Fetch()[0].Get<uint64>() > 0)
    {
        QueryResult result = CharacterDatabase.Query("SELECT `guid`, `faction`, `target_rank` FROM `companion_rep_goals`");
        if (result)
        {
            do
            {
                Field* fields = result->Fetch();
                goals[fields[0].Get<uint32>()].emplace_back(fields[1].Get<uint32>(), fields[2].Get<uint32>());
            } while (result->NextRow());
        }
    }
    std::lock_guard<std::mutex> lock(gRepMutex);
    gRepGoals.swap(goals);
}

std::vector<std::pair<uint32, uint32>> RepGoals(uint32 low)
{
    std::lock_guard<std::mutex> lock(gRepMutex);
    auto it = gRepGoals.find(low);
    return it == gRepGoals.end() ? std::vector<std::pair<uint32, uint32>>() : it->second;
}

// ---------------------------------------------------------------------------------------------------------------
// Owner quest log copy (world thread)
// ---------------------------------------------------------------------------------------------------------------
void PublishOwnerQuests(std::unordered_set<uint32>&& quests, char const* source)
{
    size_t count = quests.size();
    bool changed = false;
    {
        std::lock_guard<std::mutex> lock(gOwnerMutex);
        changed = !gOwnerQuestsKnown.load() || quests != gOwnerQuests;
        gOwnerQuests = std::move(quests);
    }
    gOwnerQuestsKnown.store(true);
    if (changed)
        LOG_INFO("playerbots", "Companion owner quests: source={} count={}", source, count);
}

// Online: the live log every OWNER_LIVE_MS (world thread: the map threads are not running then). Logout: the last
// live copy is kept and character_queststatus is read OWNER_LOGOUT_DB_DELAY_MS later (the save is asynchronous).
// Offline: character_queststatus every OWNER_DB_MS. Status 0 rows are not in the log (Player::_LoadQuestStatus).
void UpdateOwnerQuests(uint32 elapsed)
{
    static uint32 timer = 0;
    static bool first = true;
    static bool wasOnline = false;
    uint32 const ownerLow = sCompanionBotMgr.OwnerLow();
    if (!ownerLow)
        return;
    timer += elapsed;
    Player* owner = sCompanionBotMgr.FindOwnerOnline();
    if (owner)
    {
        if (first || !wasOnline || timer >= OWNER_LIVE_MS)
        {
            std::unordered_set<uint32> quests;
            for (uint8 slot = 0; slot < MAX_QUEST_LOG_SIZE; ++slot)
                if (uint32 questId = owner->GetQuestSlotQuestId(slot))
                    quests.insert(questId);
            PublishOwnerQuests(std::move(quests), "online");
            timer = 0;
            first = false;
        }
        wasOnline = true;
        return;
    }
    if (wasOnline)
    {
        wasOnline = false;
        LOG_INFO("playerbots", "Companion owner quests: source=logout keep live copy, db read in {}s",
                 OWNER_LOGOUT_DB_DELAY_MS / 1000);
        timer = OWNER_DB_MS - OWNER_LOGOUT_DB_DELAY_MS;
        return;
    }
    if (!first && timer < OWNER_DB_MS)
        return;
    timer = 0;
    first = false;
    std::unordered_set<uint32> quests;
    QueryResult result =
        CharacterDatabase.Query("SELECT quest FROM character_queststatus WHERE guid = {} AND status <> 0", ownerLow);
    if (result)
    {
        do
            quests.insert(result->Fetch()[0].Get<uint32>());
        while (result->NextRow());
    }
    PublishOwnerQuests(std::move(quests), "db");
}

uint32 FreeLogSlots(Player* bot)
{
    uint32 free = 0;
    for (uint8 slot = 0; slot < MAX_QUEST_LOG_SIZE; ++slot)
        if (!bot->GetQuestSlotQuestId(slot))
            ++free;
    return free;
}

bool IsGreyFor(Player* bot, Quest const* quest)
{
    int32 const hideDiff = static_cast<int32>(sWorld->getIntConfig(CONFIG_QUEST_LOW_LEVEL_HIDE_DIFF));
    return static_cast<int32>(bot->GetLevel()) > bot->GetQuestLevel(quest) + hideDiff;
}

// Rate limit for the skip lines: true at most once per key per SKIP_LOG_SECONDS for this bot.
bool ShouldLogSkip(uint32 low, uint64 key, uint32 now)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    uint32& last = gStates[low].skipLogSec[key];
    if (last && now - last < SKIP_LOG_SECONDS)
        return false;
    last = now;
    return true;
}

// The quest project's own block reason for "why" (and the lease's block report).
void Block(uint32 low, std::string const& reason)
{
    CompanionLease::ReportBlock(low, "quest", reason);
    std::lock_guard<std::mutex> lock(gStateMutex);
    QuestBotState& st = gStates[low];
    st.lastBlock = reason;
    st.lastBlockSec = NowSec();
}

// A blocked reason of the current evaluation (shown by "why"; the log line is LogBlockedOnce).
void NoteBlocked(uint32 low, std::string const& line)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    std::vector<std::string>& blocked = gStates[low].blockedNow;
    if (blocked.size() < 12)
        blocked.push_back(line);
}

// ---------------------------------------------------------------------------------------------------------------
// Routes: walking (mounted when possible) or known flight paths
// ---------------------------------------------------------------------------------------------------------------
struct Route
{
    bool ok = false;
    bool flight = false;
    float seconds = 0.0f;
    float dist = 0.0f;   // straight line, bot to target
    float fmX = 0.0f, fmY = 0.0f, fmZ = 0.0f;
    std::vector<uint32> nodes;
    bool transport = false;   // boat / tram / portal / hearth legs (CompanionTransport); never with flight
    bool hearth = false;      // the legs use the hearthstone
    std::string legs;         // CompanionTransport::Plan::Describe()
};

class RoutePlanner
{
public:
    explicit RoutePlanner(Player* bot) : _bot(bot)
    {
        _speed = WALK_SPEED * (CompanionQuest::HasGroundMount(bot) && CompanionQuest::MountEnabled() ? MOUNT_FACTOR
                                                                                                    : 1.0f);
        if (!gFlightEnabled.load())
            return;
        uint32 const teamIdx = bot->GetTeamId() == TEAM_ALLIANCE ? 1 : 0;
        uint32 const mapId = bot->GetMapId();
        for (uint32 i = 0; i < sTaxiNodesStore.GetNumRows(); ++i)
        {
            TaxiNodesEntry const* node = sTaxiNodesStore.LookupEntry(i);
            if (!node || node->map_id != mapId || !node->MountCreatureID[teamIdx])
                continue;
            _usable[node->ID] = node;
        }
        // Start candidates: the two nearest flight masters of the bot's team (learned on the visit, like a player).
        std::vector<std::pair<float, TaxiNodesEntry const*>> near;
        for (auto const& [id, node] : _usable)
        {
            float d = Dist2d(bot->GetPositionX(), bot->GetPositionY(), node->x, node->y);
            if (d < 3000.0f)
                near.emplace_back(d, node);
        }
        std::sort(near.begin(), near.end(),
                  [](auto const& a, auto const& b) { return a.first < b.first; });
        for (size_t i = 0; i < near.size() && i < 2; ++i)
            AddStart(near[i].second, near[i].first);
    }

    // z and zone (0 = unknown) tell the landmass of the target (CompanionTransport).
    Route Plan(uint32 mapId, float x, float y, float z = 0.0f, uint32 zone = 0) const
    {
        Route route;
        bool const transport = CompanionTransport::Enabled() && CompanionTransport::Ready();
        if (transport && CompanionTransport::NeedsLegs(_bot, mapId, x, y, z, zone))
            return TransportRoute(mapId, x, y, z, zone);   // another map or landmass: boats, tram, portals
        if (mapId != _bot->GetMapId())
            return route;   // no transport between maps (Transport.Enabled = 0)
        Route walked = PlanSameLand(mapId, x, y);
        // Same landmass: a tram or the hearth route can still be faster than walking. A tram never beats a flight;
        // the hearth does when it saves Hearth.MinSavingSeconds over the flight too (CompanionTransport applies the
        // same saving, and the near-home rule, against walking).
        if (transport && walked.seconds > 180.0f)
        {
            Route legs = TransportRoute(mapId, x, y, z, zone);
            if (legs.ok && legs.transport && legs.seconds < walked.seconds &&
                (!walked.flight ||
                 (legs.hearth &&
                  legs.seconds + CompanionTransport::HearthMinSavingSeconds() <= walked.seconds)))
                return legs;
        }
        return walked;
    }

private:
    Route TransportRoute(uint32 mapId, float x, float y, float z, uint32 zone) const
    {
        Route route;
        CompanionTransport::Plan plan = CompanionTransport::PlanRoute(_bot, mapId, x, y, z, zone, _speed);
        if (!plan.ok)
            return route;
        route.ok = true;
        route.transport = !plan.legs.empty();
        route.hearth = plan.hearthUsed;
        route.legs = plan.Describe();
        route.seconds = plan.seconds;
        route.dist = mapId == _bot->GetMapId() ? Dist2d(_bot->GetPositionX(), _bot->GetPositionY(), x, y) : 0.0f;
        return route;
    }

    Route PlanSameLand(uint32 /*mapId*/, float x, float y) const
    {
        Route route;
        route.ok = true;
        route.dist = Dist2d(_bot->GetPositionX(), _bot->GetPositionY(), x, y);
        route.seconds = route.dist * PATH_FACTOR / _speed;
        if (route.dist < FLIGHT_MIN_YARDS)
            return route;

        float best = route.seconds - FLIGHT_GAIN_SECONDS;
        Start const* bestStart = nullptr;
        uint32 bestEnd = 0;
        for (Start const& start : _starts)
        {
            for (auto const& [nodeId, flown] : start.dist)
            {
                if (nodeId == start.node)
                    continue;
                TaxiNodesEntry const* end = _usable.at(nodeId);
                float sec = start.walkSec + flown / FLIGHT_SPEED + 15.0f +
                            Dist2d(end->x, end->y, x, y) * PATH_FACTOR / _speed;
                if (sec < best)
                {
                    best = sec;
                    bestStart = &start;
                    bestEnd = nodeId;
                }
            }
        }
        if (!bestStart)
            return route;
        route.flight = true;
        route.seconds = best;
        TaxiNodesEntry const* startNode = _usable.at(bestStart->node);
        route.fmX = startNode->x;
        route.fmY = startNode->y;
        route.fmZ = startNode->z;
        for (uint32 at = bestEnd; at; )
        {
            route.nodes.push_back(at);
            if (at == bestStart->node)
                break;
            auto prev = bestStart->prev.find(at);
            at = prev == bestStart->prev.end() ? 0 : prev->second;
        }
        std::reverse(route.nodes.begin(), route.nodes.end());
        if (route.nodes.size() < 2 || route.nodes.front() != bestStart->node)
        {
            route.flight = false;
            route.nodes.clear();
            route.seconds = route.dist * PATH_FACTOR / _speed;
        }
        return route;
    }

    struct Start
    {
        uint32 node = 0;
        float walkSec = 0.0f;
        std::unordered_map<uint32, float> dist;   // node -> flown yards
        std::unordered_map<uint32, uint32> prev;
    };

    // Dijkstra from a start node over taxi paths whose next node the bot knows.
    void AddStart(TaxiNodesEntry const* startNode, float walkYards)
    {
        Start start;
        start.node = startNode->ID;
        start.walkSec = walkYards * PATH_FACTOR / _speed;
        start.dist[start.node] = 0.0f;
        std::set<std::pair<float, uint32>> open;
        open.insert({0.0f, start.node});
        while (!open.empty())
        {
            auto [d, cur] = *open.begin();
            open.erase(open.begin());
            if (d > start.dist[cur])
                continue;
            auto from = sTaxiPathSetBySource.find(cur);
            if (from == sTaxiPathSetBySource.end())
                continue;
            TaxiNodesEntry const* curNode = _usable.at(cur);
            for (auto const& [to, path] : from->second)
            {
                auto toIt = _usable.find(to);
                if (!path || toIt == _usable.end() || !_bot->m_taxi.IsTaximaskNodeKnown(to))
                    continue;
                float nd = d + Dist2d(curNode->x, curNode->y, toIt->second->x, toIt->second->y);
                auto known = start.dist.find(to);
                if (known != start.dist.end() && known->second <= nd)
                    continue;
                start.dist[to] = nd;
                start.prev[to] = cur;
                open.insert({nd, to});
            }
        }
        _starts.push_back(std::move(start));
    }

    Player* _bot;
    float _speed = WALK_SPEED;
    std::unordered_map<uint32, TaxiNodesEntry const*> _usable;
    std::vector<Start> _starts;
};

float TravelCost(Route const& route, float travelW)
{
    return std::min(TRAVEL_CAP, route.seconds / TRAVEL_DIVISOR) * travelW;
}

// ---------------------------------------------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------------------------------------------
struct Option
{
    CompanionQuest::TripKind kind = CompanionQuest::TripKind::None;
    bool grind = false;
    bool here = false;   // work in the current zone through new-rpg DO_QUEST (no trip)
    uint32 questId = 0;
    uint32 zone = 0;
    uint32 map = 0;
    float x = 0.0f, y = 0.0f, z = 0.0f;
    uint32 spawnId = 0;
    bool isGo = false;
    uint32 itemId = 0;
    uint32 hubIndex = 0;
    std::vector<uint32> hubPlan;   // hub: quests to take, best value first (B3)
    uint32 waitMin = 0;            // turn-in: minutes since first seen complete (B4)
    float waitBonus = 0.0f;
    bool cross = false;            // work on another map (C route): scored by the per-continent batch
    bool drop = false;             // removed by the cross-continent rules
    Route route;
    float score = 0.0f;

    std::string Label() const
    {
        if (grind)
            return "grind";
        switch (kind)
        {
            case CompanionQuest::TripKind::TurnIn: return "turnin=" + std::to_string(questId);
            case CompanionQuest::TripKind::Work: return std::string(here ? "workhere=" : "work=") + std::to_string(questId);
            case CompanionQuest::TripKind::Hub: return "hub=" + std::to_string(zone);
            case CompanionQuest::TripKind::Mount: return "mount=" + std::to_string(itemId);
            case CompanionQuest::TripKind::Travel: return "travel=" + std::to_string(map);
            default: return "none";
        }
    }
};

void LogBlockedOnce(Player* bot, uint32 low, char const* kind, uint32 id, uint32 zone, uint32 map, char const* reason)
{
    std::string key = std::string(kind) + ":" + std::to_string(id) + ":" + reason;
    NoteBlocked(low, std::string(kind) + " " + std::to_string(id) + " to zone " + std::to_string(zone) + " map " +
                         std::to_string(map) + ": " + reason);
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        if (!gStates[low].blockedLogged.insert(key).second)
            return;
    }
    LOG_INFO("playerbots", "Companion {}: trip blocked kind={} id={} to={} map={} reason={}", bot->GetName(), kind, id,
             zone, map, reason);
}

// The work target of an incomplete quest: the open objective POI nearest by travel time on the bot's map; when none is
// there and allowOther, the nearest one on another companion map (C route: z unknown = 0, zone = the quest's own zone
// until the bot is there; another map's terrain is not read from this map thread). otherMap: the map of an open POI
// that was not taken (0: none). False when no POI is reachable.
bool WorkTarget(Player* bot, Quest const* quest, RoutePlanner const& planner, bool allowOther, Option& best,
                uint32& otherMap)
{
    otherMap = 0;
    QuestPOIVector const* pois = sObjectMgr->GetQuestPOIVector(quest->GetQuestId());
    QuestStatusMap const& statusMap = bot->getQuestStatusMap();
    auto statusIt = statusMap.find(quest->GetQuestId());
    if (!pois || statusIt == statusMap.end())
        return false;
    QuestStatusData const& qs = statusIt->second;
    uint32 const mapId = bot->GetMapId();
    bool found = false;
    std::vector<std::pair<uint32, std::pair<float, float>>> others;   // map, centroid
    for (QuestPOI const& poi : *pois)
    {
        if (poi.points.empty() || poi.ObjectiveIndex < 0)
            continue;
        int32 idx = poi.ObjectiveIndex;
        bool open = false;
        if (idx < QUEST_OBJECTIVES_COUNT)
            open = quest->RequiredNpcOrGo[idx] && qs.CreatureOrGOCount[idx] < quest->RequiredNpcOrGoCount[idx];
        else if (idx < QUEST_OBJECTIVES_COUNT + QUEST_ITEM_OBJECTIVES_COUNT)
            open = quest->RequiredItemId[idx - QUEST_OBJECTIVES_COUNT] &&
                   qs.ItemCount[idx - QUEST_OBJECTIVES_COUNT] < quest->RequiredItemCount[idx - QUEST_OBJECTIVES_COUNT];
        if (!open)
            continue;
        float px = 0.0f;
        float py = 0.0f;
        for (QuestPOIPoint const& point : poi.points)
        {
            px += point.x;
            py += point.y;
        }
        px /= poi.points.size();
        py /= poi.points.size();
        if (poi.MapId != mapId)
        {
            otherMap = poi.MapId;
            if (allowOther && OnCompanionMap(poi.MapId))
                others.push_back({poi.MapId, {px, py}});
            continue;
        }
        Route route = planner.Plan(mapId, px, py);
        if (!route.ok || (found && route.seconds >= best.route.seconds))
            continue;
        found = true;
        best.kind = CompanionQuest::TripKind::Work;
        best.questId = quest->GetQuestId();
        best.map = mapId;
        best.x = px;
        best.y = py;
        best.z = bot->GetPositionZ();
        best.route = route;
    }
    if (found)
    {
        best.zone = ZoneAt(mapId, best.x, best.y, bot->GetPositionZ());
        return true;
    }
    int32 const questZone = quest->GetZoneOrSort();
    uint32 const zone = questZone > 0 ? static_cast<uint32>(questZone) : 0;
    for (auto const& [poiMap, at] : others)
    {
        Route route = planner.Plan(poiMap, at.first, at.second, 0.0f, zone);
        if (!route.ok || (found && route.seconds >= best.route.seconds))
            continue;
        found = true;
        best.kind = CompanionQuest::TripKind::Work;
        best.questId = quest->GetQuestId();
        best.map = poiMap;
        best.x = at.first;
        best.y = at.second;
        best.z = 0.0f;   // unknown: read on the target map (CompanionQuest::SetTripPoint)
        best.zone = zone;
        best.cross = true;
        best.route = route;
    }
    if (found)
        otherMap = 0;
    return found;
}

// True when an objective creature of the quest is above maxLevel (minimum template level).
bool ObjectiveMobAbove(Quest const* quest, Player* bot, uint32 maxLevel)
{
    QuestStatusMap const& statusMap = bot->getQuestStatusMap();
    auto it = statusMap.find(quest->GetQuestId());
    for (int i = 0; i < QUEST_OBJECTIVES_COUNT; ++i)
    {
        int32 npcOrGo = quest->RequiredNpcOrGo[i];
        if (npcOrGo <= 0)
            continue;
        if (it != statusMap.end() && it->second.CreatureOrGOCount[i] >= quest->RequiredNpcOrGoCount[i])
            continue;
        CreatureTemplate const* cInfo = sObjectMgr->GetCreatureTemplate(static_cast<uint32>(npcOrGo));
        if (cInfo && cInfo->minlevel > maxLevel)
            return true;
    }
    return false;
}

void AbandonQuest(Player* bot, uint8 slot, uint32 questId, char const* reason, float value)
{
    WorldPacket packet(CMSG_QUESTLOG_REMOVE_QUEST);
    packet << slot;
    WorldPackets::Quest::QuestLogRemoveQuest removeQuest(std::move(packet));
    removeQuest.Read();
    bot->GetSession()->HandleQuestLogRemoveQuest(removeQuest);
    LOG_INFO("playerbots", "Companion {}: abandon quest={} reason={} value={}", bot->GetName(), questId, reason,
             Num(value));
}

// Turn-ins seen in the log (any path: this project, new-rpg, the stock quest strategy).
void DetectTurnIns(Player* bot, uint32 low)
{
    std::unordered_set<uint32> nowComplete;
    for (uint8 slot = 0; slot < MAX_QUEST_LOG_SIZE; ++slot)
    {
        uint32 questId = bot->GetQuestSlotQuestId(slot);
        if (questId && bot->GetQuestStatus(questId) == QUEST_STATUS_COMPLETE)
            nowComplete.insert(questId);
    }
    std::vector<uint32> turnedIn;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        QuestBotState& st = gStates[low];
        if (st.logInit)
        {
            for (uint32 questId : st.completeInLog)
                if (!nowComplete.count(questId) && bot->GetQuestRewardStatus(questId))
                    turnedIn.push_back(questId);
            st.turnIns += turnedIn.size();
        }
        st.logInit = true;
        // B4: when each complete quest was first seen complete (the waiting bonus of its turn-in).
        uint32 const now = NowSec();
        for (auto it = st.completeSince.begin(); it != st.completeSince.end();)
            it = nowComplete.count(it->first) ? std::next(it) : st.completeSince.erase(it);
        for (uint32 questId : nowComplete)
            st.completeSince.emplace(questId, now);
        st.completeInLog = std::move(nowComplete);
    }
    for (uint32 questId : turnedIn)
    {
        Quest const* quest = sObjectMgr->GetQuestTemplate(questId);
        int32 money = quest ? quest->GetRewOrReqMoney(bot->GetLevel()) : 0;
        LOG_INFO("playerbots", "Companion {}: quest turnin id={} zone={} gold={}", bot->GetName(), questId,
                 bot->GetZoneId(), money > 0 ? money : 0);
    }
}

// Every ABANDON_CHECK_SECONDS: status failed, objective mobs more than 10 levels above, three failed work trips.
void AbandonCheck(Player* bot, uint32 low, uint32 now)
{
    std::unordered_map<uint32, uint32> fails;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        QuestBotState& st = gStates[low];
        if (st.lastAbandonCheckSec && now - st.lastAbandonCheckSec < ABANDON_CHECK_SECONDS)
            return;
        st.lastAbandonCheckSec = now;
        fails = st.workFails;
    }
    for (uint8 slot = 0; slot < MAX_QUEST_LOG_SIZE; ++slot)
    {
        uint32 questId = bot->GetQuestSlotQuestId(slot);
        Quest const* quest = questId ? sObjectMgr->GetQuestTemplate(questId) : nullptr;
        if (!quest)
            continue;
        QuestStatus status = bot->GetQuestStatus(questId);
        char const* reason = nullptr;
        if (status == QUEST_STATUS_FAILED)
            reason = "status-failed";
        else if (status == QUEST_STATUS_INCOMPLETE && ObjectiveMobAbove(quest, bot, bot->GetLevel() + 10))
            reason = "mobs-too-high";
        else if (status == QUEST_STATUS_INCOMPLETE && fails[questId] >= 3)
            reason = "failed-3x";
        if (!reason)
            continue;
        if (CompanionQuest::KeepForOwner(bot, questId, reason))
            continue;   // B2: the owner has it too
        AbandonQuest(bot, slot, questId, reason, CompanionQuest::QuestValue(bot, quest));
        return;   // one per check: the slots shift
    }
}

// Best ground mount spell the bot knows (highest speed). 0 when none.
uint32 BestGroundMount(Player const* bot)
{
    uint32 best = 0;
    int32 bestSpeed = -1;
    for (auto const& [spellId, playerSpell] : bot->GetSpellMap())
    {
        if (!playerSpell || playerSpell->State == PLAYERSPELL_REMOVED || !playerSpell->Active)
            continue;
        SpellInfo const* info = sSpellMgr->GetSpellInfo(spellId);
        if (!info || info->IsPassive() || info->Effects[0].ApplyAuraName != SPELL_AURA_MOUNTED)
            continue;
        if (info->Effects[1].ApplyAuraName == SPELL_AURA_MOD_INCREASE_MOUNTED_FLIGHT_SPEED ||
            info->Effects[2].ApplyAuraName == SPELL_AURA_MOD_INCREASE_MOUNTED_FLIGHT_SPEED)
            continue;
        int32 speed = std::max(info->Effects[1].BasePoints, info->Effects[2].BasePoints);
        if (speed > bestSpeed)
        {
            bestSpeed = speed;
            best = spellId;
        }
    }
    return best;
}

bool HostileNear(Player* bot, float range)
{
    std::list<Unit*> units;
    Acore::AnyUnitInObjectRangeCheck check(bot, range);
    Acore::UnitListSearcher<Acore::AnyUnitInObjectRangeCheck> searcher(bot, units, check);
    Cell::VisitObjects(bot, searcher, range);
    for (Unit* unit : units)
    {
        if (!unit->IsAlive() || !unit->IsHostileTo(bot))
            continue;
        Creature* creature = unit->ToCreature();
        if (creature && creature->GetCreatureTemplate()->type == CREATURE_TYPE_CRITTER)
            continue;
        return true;
    }
    return false;
}

// Shared pre-purchase legality check for mount items (issue #68): Player::CanUseItem(ItemTemplate const*) checks
// faction, class, race, riding skill, spell, level, and holiday; reputation rank is checked explicitly here to match
// Player::CanUseItem(Item*) and Player::BuyItemFromVendorSlot.
InventoryResult CanUseMountTemplate(Player* bot, ItemTemplate const* proto)
{
    if (!bot || !proto)
        return EQUIP_ERR_ITEM_NOT_FOUND;
    InventoryResult const res = bot->CanUseItem(proto);
    if (res != EQUIP_ERR_OK)
        return res;
    if (proto->RequiredReputationFaction &&
        static_cast<uint32>(bot->GetReputationRank(proto->RequiredReputationFaction)) < proto->RequiredReputationRank)
        return EQUIP_ERR_CANT_EQUIP_REPUTATION;
    return EQUIP_ERR_OK;
}

uint64 MountReserveCopper()
{
    return static_cast<uint64>(gMountGoldReserve.load()) * 10000ull;
}

// Mount project status line (rate limited) and the mount option, if any.
void MountOption(Player* bot, uint32 low, RoutePlanner const& planner, float travelW, std::vector<Option>& out,
                 uint32 now)
{
    if (!CompanionQuest::MountEnabled())
        return;
    std::string status;
    uint32 const riding = bot->GetSkillValue(SKILL_RIDING);
    uint32 const have = BestGroundMount(bot);
    MountOffer const* pick = nullptr;
    float pickDist = 0.0f;
    if (have)
        status = "have spell=" + std::to_string(have);
    else if (!riding)
        status = "blocked reason=no-riding-skill (the train duty learns riding)";
    else
    {
        // Recover a mount item already sitting in bags (e.g. an earlier use-cast was delayed or interrupted):
        // never plan a vendor trip while holding an unlearned mount item; cast it directly once idle (issue #68).
        bool handledFromBag = false;
        for (MountOffer const& offer : gMountOffers)
        {
            if (offer.spellId && bot->HasSpell(offer.spellId))
                continue;
            Item* bagMount = bot->GetItemByEntry(offer.itemId);
            if (!bagMount || bot->CanUseItem(bagMount) != EQUIP_ERR_OK)
                continue;
            handledFromBag = true;
            bool const canCastNow = bot->IsAlive() && !bot->IsInCombat() && !bot->IsInFlight() && !bot->GetVehicle() &&
                                    !bot->IsNonMeleeSpellCast(false);
            bool cooldownOk = false;
            if (canCastNow)
            {
                std::lock_guard<std::mutex> lock(gStateMutex);
                QuestBotState& st = gStates[low];
                if (now - st.lastMountTrySec >= MOUNT_RETRY_SEC)
                {
                    st.lastMountTrySec = now;
                    cooldownOk = true;
                }
            }
            if (canCastNow && cooldownOk)
            {
                if (bot->isMoving())
                    bot->StopMoving();
                SpellCastTargets targets;
                targets.SetUnitTarget(bot);
                bot->CastItemUseSpell(bagMount, targets, 1, 0);
                status = "use-from-bag item=" + std::to_string(offer.itemId) + " spell=" +
                         std::to_string(offer.spellId);
            }
            else
            {
                status = "use-pending item=" + std::to_string(offer.itemId) + " spell=" +
                         std::to_string(offer.spellId);
            }
            break;
        }
        if (!handledFromBag)
        {
            uint32 const raceMask = bot->getRaceMask();
            bool const transportOn = CompanionTransport::Enabled() && CompanionTransport::Ready();
            uint32 const hostileBit = bot->GetTeamId() == TEAM_HORDE ? 4u : 2u;
            uint64 const reserveCopper = MountReserveCopper();
            auto raceIt = gRaceVendors.find(bot->getRace());
            uint32 const ownEntry = raceIt == gRaceVendors.end() ? 0 : raceIt->second;
            uint32 ownMap = 0;
            bool ownSeen = false;
            uint32 cheapest = 0;
            MountOffer const* own = nullptr;
            MountOffer const* any = nullptr;
            float ownDist = 0.0f;
            float anyDist = 0.0f;
            for (MountOffer const& offer : gMountOffers)
            {
                if ((offer.races && !(offer.races & raceMask)) || offer.skillRank > riding ||
                    (offer.hostileMask & hostileBit))
                    continue;
                if (offer.spellId && bot->HasSpell(offer.spellId))
                    continue;
                ItemTemplate const* proto = sObjectMgr->GetItemTemplate(offer.itemId);
                if (!proto || CanUseMountTemplate(bot, proto) != EQUIP_ERR_OK)
                    continue;
                if (!cheapest || offer.price < cheapest)
                    cheapest = offer.price;
                bool const isOwn = offer.entry == ownEntry;
                if (isOwn)
                {
                    ownSeen = true;
                    ownMap = offer.map;
                }
                // Conservative against list price before vendor reputation discount, so BuyMount never fails reserve.
                if ((offer.map != bot->GetMapId() && !transportOn) ||
                    static_cast<uint64>(offer.price) + reserveCopper > static_cast<uint64>(bot->GetMoney()) ||
                    !ZoneSafe(offer.zone, bot->GetLevel()))
                    continue;
                float d = Dist2d(bot->GetPositionX(), bot->GetPositionY(), offer.x, offer.y);
                if (transportOn && CompanionTransport::NeedsLegs(bot, offer.map, offer.x, offer.y, offer.z, offer.zone))
                {
                    // Across the sea (boat, tram, portal or hearth): compare by travel time, in walking yards.
                    Route route = planner.Plan(offer.map, offer.x, offer.y, offer.z, offer.zone);
                    if (!route.ok)
                        continue;
                    d = route.seconds * WALK_SPEED;
                }
                if (isOwn && (!own || d < ownDist))
                {
                    own = &offer;
                    ownDist = d;
                }
                if (!any || d < anyDist)
                {
                    any = &offer;
                    anyDist = d;
                }
            }
            if (own)
            {
                pick = own;
                pickDist = ownDist;
            }
            else if (any && gAnyRaceVendor.load())
            {
                pick = any;
                pickDist = anyDist;
            }

            if (!pick && !cheapest)
                status = "blocked reason=no-usable-mount-offer";
            else if (!pick && static_cast<uint64>(bot->GetMoney()) < static_cast<uint64>(cheapest) + reserveCopper)
                status = "save-gold need=" + std::to_string(static_cast<uint64>(cheapest) + reserveCopper) +
                         " have=" + std::to_string(bot->GetMoney());
            else if (pick)
                status = std::string("buy vendor=") + std::to_string(pick->entry) + " item=" +
                         std::to_string(pick->itemId) + " price=" + std::to_string(pick->price) + " gold=" +
                         std::to_string(bot->GetMoney()) +
                         (pick == own ? " own-race=1"
                                      : " own-race=0 own-vendor-map=" + std::to_string(ownMap) +
                                            (ownSeen ? " reason=no-transport" : " reason=no-own-vendor"));
            else
                status = "blocked reason=no-transport (no vendor on this map) gold=" + std::to_string(bot->GetMoney());
        }
    }

    bool logIt = false;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        QuestBotState& st = gStates[low];
        if (st.mountStatus != status || now - st.lastMountLogSec >= MOUNT_LOG_SECONDS)
        {
            st.mountStatus = status;
            st.lastMountLogSec = now;
            logIt = true;
        }
    }
    if (logIt)
        LOG_INFO("playerbots", "Companion {}: mount {} riding={}", bot->GetName(), status, riding);
    CompanionLease::ReportBlock(low, "mount", pick ? std::string() : status);

    if (!pick || !CompanionLease::MayTry(low, CompanionLease::SpawnKey(false, pick->spawnId)))
        return;
    Option option;
    option.kind = CompanionQuest::TripKind::Mount;
    option.zone = pick->zone;
    option.map = pick->map;
    option.x = pick->x;
    option.y = pick->y;
    option.z = pick->z;
    option.spawnId = pick->spawnId;
    option.itemId = pick->itemId;
    option.route = planner.Plan(pick->map, pick->x, pick->y, pick->z, pick->zone);
    (void)pickDist;
    if (!option.route.ok)
        return;
    float wMount = CompanionBrain::Weight(low, "mount", gWMount.load());
    // A mount speeds up every later trip: above turn-ins (85 + value) once the gold is there (run 1b, 2026-11-19:
    // Wren planned the purchase every 10 min for an hour at 85 and never went).
    option.score = 130.0f * wMount - TravelCost(option.route, travelW);
    out.push_back(option);
}
}  // namespace

// ---------------------------------------------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------------------------------------------
bool CompanionQuest::Enabled()
{
    return gConfigLoaded.load() && gEnabled.load();
}

bool CompanionQuest::MountEnabled()
{
    return gConfigLoaded.load() && gMountEnabled.load();
}

bool CompanionQuest::AcceptGrey()
{
    return Enabled() && gAcceptGrey.load();
}

float CompanionQuest::MountMinDistance()
{
    return gMountMinDistance.load();
}

bool CompanionQuest::DoQuestSearch()
{
    return gDoQuestSearch.load();
}

float CompanionQuest::DoQuestSearchYards()
{
    return gDoQuestSearchYards.load();
}

uint32 CompanionQuest::DoQuestStallMinutes()
{
    return gDoQuestStallMinutes.load();
}

void CompanionQuest::UpdateWorld(uint32 elapsed)
{
    static bool tablesBuilt = false;
    static bool repLoaded = false;
    static uint32 repTimer = 0;
    static uint32 switchTimer = 0;
    if (!gConfigLoaded.load())
    {
        LoadConfig();
        gConfigLoaded.store(true);
    }
    switchTimer += elapsed;
    if (switchTimer >= SWITCH_RELOAD_MS)
    {
        switchTimer = 0;
        LoadSwitches();
    }
    // The owner's quest log copy is kept even with the project off: the stock quest-log cleanup asks it too (B2).
    if (gKeepOwnerQuests.load())
        UpdateOwnerQuests(elapsed);
    if (!gEnabled.load() && !gMountEnabled.load())
        return;
    if (!tablesBuilt && sCompanionBotMgr.QuestGiverSpotsReady())
    {
        tablesBuilt = true;
        BuildTables();
        gTablesReady.store(true);
    }
    repTimer += elapsed;
    if (!repLoaded || repTimer >= REP_RELOAD_MS)
    {
        repTimer = 0;
        repLoaded = true;
        LoadRepGoals();
    }
}

float CompanionQuest::QuestValue(Player* bot, Quest const* quest)
{
    if (!bot || !quest)
        return 0.0f;
    uint32 const low = bot->GetGUID().GetCounter();

    // Reputation: raw quest reputation / 25; a faction on the bot's goal list below its target standing gets x3 + 15.
    float rep = 0.0f;
    std::vector<std::pair<uint32, uint32>> goals = RepGoals(low);
    for (uint8 i = 0; i < QUEST_REPUTATIONS_COUNT; ++i)
    {
        uint32 faction = quest->RewardFactionId[i];
        if (!faction)
            continue;
        float amount = 0.0f;
        if (quest->RewardFactionValueIdOverride[i])
            amount = quest->RewardFactionValueIdOverride[i] / 100.0f;
        else
        {
            uint32 row = (quest->RewardFactionValueId[i] < 0 ? 1 : 0) + 1;
            uint32 field = static_cast<uint32>(std::abs(quest->RewardFactionValueId[i]));
            if (QuestFactionRewEntry const* entry = sQuestFactionRewardStore.LookupEntry(row))
                if (field < 10)
                    amount = static_cast<float>(entry->QuestRewFactionValue[field]);
        }
        if (amount <= 0.0f)
            continue;
        float part = amount / 25.0f;
        for (auto const& [goalFaction, targetRank] : goals)
            if (goalFaction == faction && static_cast<uint32>(bot->GetReputationRank(faction)) < targetRank)
                part = part * 3.0f + 15.0f;
        rep += part;
    }

    // Items: vendor value of the reward items and the best choice item (1 per 50 copper, capped).
    float items = 0.0f;
    for (uint8 i = 0; i < QUEST_REWARDS_COUNT; ++i)
        if (quest->RewardItemId[i])
            if (ItemTemplate const* proto = sObjectMgr->GetItemTemplate(quest->RewardItemId[i]))
                items += proto->SellPrice / 50.0f;
    float bestChoice = 0.0f;
    for (uint8 i = 0; i < QUEST_REWARD_CHOICES_COUNT; ++i)
        if (quest->RewardChoiceItemId[i])
            if (ItemTemplate const* proto = sObjectMgr->GetItemTemplate(quest->RewardChoiceItemId[i]))
                bestChoice = std::max(bestChoice, proto->SellPrice / 50.0f);
    items = std::min(15.0f, items + bestChoice);

    // Gold: 1 per 2 silver, capped; doubled while the bot saves for a mount.
    int32 money = quest->GetRewOrReqMoney(bot->GetLevel());
    float gold = money > 0 ? std::min(25.0f, money / 200.0f) : 0.0f;
    if (MountEnabled() && !HasGroundMount(bot) && bot->GetMoney() < 10000)
        gold *= 2.0f;

    // Completion: the completionist trait (0..1) clears the quests of the zone the bot is in, grey or not.
    float completionist = std::clamp(CompanionBrain::RawWeight(low, "completionist", gCompletionist.load()), 0.0f, 1.0f);
    bool const sameZone = quest->GetZoneOrSort() > 0 && static_cast<uint32>(quest->GetZoneOrSort()) == bot->GetZoneId();
    float completion = completionist * 20.0f * (sameZone ? 1.5f : 1.0f);

    // XP: nothing at the XP cap (the owner's rule: the companions stay near the owner's level).
    float xp = 0.0f;
    if (!sCompanionBotMgr.IsXpCapped(bot))
        xp = std::min(30.0f, quest->XPValue(bot->GetLevel()) / 200.0f);

    return rep + items + gold + completion + xp;
}

bool CompanionQuest::HasGroundMount(Player const* bot)
{
    return bot && BestGroundMount(bot) != 0;
}

bool CompanionQuest::GrindAllowed(uint32 lowGuid)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gStates.find(lowGuid);
    return it == gStates.end() || it->second.grindAllowed;
}

bool CompanionQuest::ActiveTrip(uint32 lowGuid, Trip& out)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gStates.find(lowGuid);
    if (it == gStates.end() || !it->second.trip.active)
        return false;
    out = it->second.trip;
    return true;
}

void CompanionQuest::SetPhase(uint32 lowGuid, Phase phase)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gStates.find(lowGuid);
    if (it != gStates.end() && it->second.trip.active)
        it->second.trip.phase = phase;
}

void CompanionQuest::NoteArrival(uint32 lowGuid, uint32 nowSec)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gStates.find(lowGuid);
    if (it != gStates.end() && it->second.trip.active && !it->second.trip.arriveSec)
        it->second.trip.arriveSec = nowSec;
}

void CompanionQuest::NoteWorkHere(uint32 lowGuid, uint32 questId)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    QuestBotState& st = gStates[lowGuid];
    st.lastHereQuest = questId;
    st.lastHereSec = NowSec();
}

void CompanionQuest::SetTripPoint(uint32 lowGuid, float z, uint32 zone)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    Trip& trip = gStates[lowGuid].trip;
    if (!trip.active || !trip.zUnknown)
        return;
    trip.z = z;
    if (zone)
        trip.zone = zone;
    trip.zUnknown = false;
}

void CompanionQuest::NoteAccepted(uint32 lowGuid)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gStates.find(lowGuid);
    if (it != gStates.end() && it->second.trip.active)
        ++it->second.trip.accepted;
}

void CompanionQuest::EndTrip(PlayerbotAI* botAI, char const* result, bool success)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot)
        return;
    uint32 const low = bot->GetGUID().GetCounter();
    Trip trip;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        QuestBotState& st = gStates[low];
        if (!st.trip.active)
            return;
        trip = st.trip;
        st.trip = Trip();
        st.lastEvalSec = 0;   // decide again at the next sync
        // A crossing that failed before its arrival starts no stay.
        if (!success && trip.cross && !st.crossArriveSec)
            st.crossToMap = NO_CROSS_MAP;
        // A cross-map work trip that never reached its map failed on the crossing, not on the quest: it does not
        // count toward the failed-3x abandon.
        if (!success && trip.kind == TripKind::Work && !trip.zUnknown)
            ++st.workFails[trip.questId];
        // A failed turn-in trip restarts its waiting bonus, so a turn-in that keeps failing does not win every pick.
        if (!success && trip.kind == TripKind::TurnIn && st.completeSince.count(trip.questId))
            st.completeSince[trip.questId] = NowSec();
    }
    uint32 const now = NowSec();
    CompanionTransport::Reset(low);
    LOG_INFO("playerbots", "Companion {}: trip end kind={} id={} to={} result={} dur={}s", bot->GetName(),
             KindName(trip.kind), trip.questId, trip.zone, result, now > trip.startSec ? now - trip.startSec : 0);
    CompanionLease::End(low, trip.owner, success ? "done" : result,
                        success ? CompanionLease::Outcome::Success : CompanionLease::Outcome::Failure);
}

bool CompanionQuest::FreeLogSlot(Player* bot)
{
    if (!bot || sCompanionBotMgr.IsGroupedWithOwner(bot))
        return false;   // never drop a quest the owner may have shared while grouped
    for (uint8 slot = 0; slot < MAX_QUEST_LOG_SIZE; ++slot)
        if (!bot->GetQuestSlotQuestId(slot))
            return false;   // a free slot exists
    uint32 const now = NowSec();
    {
        // At most one log-full drop per 10 minutes, so a full log does not churn through quests.
        std::lock_guard<std::mutex> lock(gStateMutex);
        QuestBotState& st = gStates[bot->GetGUID().GetCounter()];
        if (st.lastLogFullDropSec && now - st.lastLogFullDropSec < 600)
            return false;
        st.lastLogFullDropSec = now;
    }
    // Lowest value first; a quest the owner has too is skipped (B2) and the next one is dropped instead.
    struct Candidate
    {
        float value;
        uint8 slot;
        uint32 questId;
    };
    std::vector<Candidate> candidates;
    for (uint8 slot = 0; slot < MAX_QUEST_LOG_SIZE; ++slot)
    {
        uint32 questId = bot->GetQuestSlotQuestId(slot);
        if (!questId)
            return false;   // a free slot exists
        QuestStatus status = bot->GetQuestStatus(questId);
        if (status == QUEST_STATUS_COMPLETE)
            continue;   // turn it in instead
        Quest const* quest = sObjectMgr->GetQuestTemplate(questId);
        candidates.push_back({QuestValue(bot, quest), slot, questId});
    }
    std::stable_sort(candidates.begin(), candidates.end(),
                     [](Candidate const& a, Candidate const& b) { return a.value < b.value; });
    for (Candidate const& candidate : candidates)
    {
        if (KeepForOwner(bot, candidate.questId, "log-full"))
            continue;
        AbandonQuest(bot, candidate.slot, candidate.questId, "log-full", candidate.value);
        return true;
    }
    return false;
}

bool CompanionQuest::OwnerHasQuest(Player* bot, uint32 questId)
{
    if (!questId)
        return false;
    {
        std::lock_guard<std::mutex> lock(gOwnerMutex);
        if (gOwnerQuests.count(questId))
            return true;
    }
    // On the bot's map the live log is read directly (same map thread); a quest taken in the last seconds counts.
    Player* owner = bot ? sCompanionBotMgr.FindOwnerOnlineFor(bot) : nullptr;
    return owner && owner->IsInWorld() && owner->FindMap() == bot->GetMap() &&
           owner->FindQuestSlot(questId) < MAX_QUEST_LOG_SIZE;   // in his log (not merely rewarded long ago)
}

// B2 guard: the reason a quest must be kept for the owner (owner-unknown, owner-shared), or nullptr.
static char const* OwnerGuardReason(Player* bot, uint32 questId)
{
    if (!bot || !gKeepOwnerQuests.load())
        return nullptr;
    if (!gOwnerQuestsKnown.load() && sCompanionBotMgr.OwnerConfigured())
        return "owner-unknown";   // fail closed until the owner's log has loaded once
    if (CompanionQuest::OwnerHasQuest(bot, questId))
        return "owner-shared";
    return nullptr;
}

// GM test hook: the companion's abandon path for one quest (AbandonQuest, behind the same B2 guard as the stock
// abandon). Refused quests are logged as "abandon refused ... reason=<owner-shared|owner-unknown>".
static void ForcedAbandon(Player* bot, uint32 low)
{
    uint32 questId = 0;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        QuestBotState& st = gStates[low];
        questId = st.pendingAbandon;
        st.pendingAbandon = 0;
    }
    if (!questId)
        return;
    uint8 slot = bot->FindQuestSlot(questId);
    if (slot >= MAX_QUEST_LOG_SIZE)
    {
        LOG_INFO("playerbots", "Companion {}: abandon refused quest={} reason=not-in-log why=gm-command",
                 bot->GetName(), questId);
        return;
    }
    if (char const* reason = OwnerGuardReason(bot, questId))
    {
        LOG_INFO("playerbots", "Companion {}: abandon refused quest={} reason={} why=gm-command", bot->GetName(),
                 questId, reason);
        return;
    }
    Quest const* quest = sObjectMgr->GetQuestTemplate(questId);
    AbandonQuest(bot, slot, questId, "gm-command", quest ? CompanionQuest::QuestValue(bot, quest) : 0.0f);
}

void CompanionQuest::RequestAbandon(uint32 lowGuid, uint32 questId)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    gStates[lowGuid].pendingAbandon = questId;
}

bool CompanionQuest::KeepForOwner(Player* bot, uint32 questId, char const* why)
{
    char const* reason = OwnerGuardReason(bot, questId);
    if (!reason)
        return false;
    uint32 const low = bot->GetGUID().GetCounter();
    if (ShouldLogSkip(low, (uint64(1) << 32) | questId, NowSec()))
        LOG_INFO("playerbots", "Companion {}: abandon skip quest={} reason={} why={}", bot->GetName(), questId, reason,
                 why ? why : "-");
    return true;
}

bool CompanionQuest::MayAccept(Player* bot, Quest const* quest)
{
    if (!bot || !quest || !Enabled())
        return true;
    uint32 const low = bot->GetGUID().GetCounter();
    uint32 const questId = quest->GetQuestId();
    uint32 const free = FreeLogSlots(bot);
    uint32 const keep = gFreeSlots.load();
    char const* reason = nullptr;
    float value = -1.0f;
    if (OwnerHasQuest(bot, questId))
    {
        if (!free)
            reason = "free-slots";   // a quest the owner has needs only one free slot
    }
    else if (free <= keep)
        reason = "free-slots";
    else if (IsGreyFor(bot, quest) && (value = QuestValue(bot, quest)) < gGreyMinValue.load())
        reason = "grey-low-value";
    else
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gStates.find(low);
        if (it != gStates.end() && it->second.trip.active && it->second.trip.kind == TripKind::Hub)
        {
            std::vector<uint32> const& plan = it->second.trip.hubPlan;
            if (std::find(plan.begin(), plan.end(), questId) == plan.end())
                reason = "not-in-hub-plan";
        }
    }
    if (!reason)
        return true;
    if (ShouldLogSkip(low, (uint64(2) << 32) | questId, NowSec()))
        LOG_INFO("playerbots", "Companion {}: accept skip quest={} reason={} free={} keep={} value={}", bot->GetName(),
                 questId, reason, free, keep, value < 0.0f ? std::string("-") : Num(value));
    return false;
}

namespace
{
constexpr uint32 MOUNT_RETRY_HOSTILE_SEC = 3;   // after a hostile-near block, with TripAvoid on
}  // namespace

bool CompanionQuest::TryMount(PlayerbotAI* botAI, float legDistance)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot || !MountEnabled() || bot->IsMounted() || legDistance < MountMinDistance() || bot->IsInCombat() ||
        !bot->IsOutdoors() || bot->IsInWater() || bot->IsInFlight())
        return false;
    uint32 const low = bot->GetGUID().GetCounter();
    uint32 const now = NowSec();
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        QuestBotState& st = gStates[low];
        if (now - st.lastMountTrySec < MOUNT_RETRY_SEC)
            return false;
        st.lastMountTrySec = now;
    }
    uint32 spell = BestGroundMount(bot);
    if (!spell)
        return false;
    // Trip peace (CompanionTrip.h): only a hostile within 10 yd blocks the mount on a leased trip (was 25 yd).
    float const hostileRange = CompanionTrip::MountHostileRange(botAI);
    if (HostileNear(bot, hostileRange))
    {
        CompanionTrip::NoteMountBlocked(botAI, hostileRange);
        // TripAvoid: a mounted companion can ride through attacks, so retry the mount after MOUNT_RETRY_HOSTILE_SEC
        // instead of the full 10 s (the hostile may have walked off; a walking leg pulls everything on the road).
        if (CompanionTrip::AvoidEnabled())
        {
            std::lock_guard<std::mutex> lock(gStateMutex);
            gStates[low].lastMountTrySec = now - (MOUNT_RETRY_SEC - MOUNT_RETRY_HOSTILE_SEC);
        }
        return false;
    }
    if (!botAI->CanCastSpell(spell, bot))
        return false;
    if (bot->isMoving())
        bot->StopMoving();
    bool const cast = botAI->CastSpell(spell, bot);
    if (cast)
        CompanionTrip::NoteMount(botAI, spell, legDistance);
    return cast;
}

bool CompanionQuest::BuyMount(PlayerbotAI* botAI, Creature* vendor, uint32 itemId)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot || !vendor)
        return false;
    VendorItemData const* items = sObjectMgr->GetNpcVendorItemList(vendor->GetEntry());
    ItemTemplate const* proto = sObjectMgr->GetItemTemplate(itemId);
    if (!items || !proto)
        return false;
    int32 slot = -1;
    VendorItem const* vendorItem = nullptr;
    for (uint32 i = 0; i < items->m_items.size(); ++i)
    {
        if (items->m_items[i] && items->m_items[i]->item == itemId)
        {
            slot = static_cast<int32>(i);
            vendorItem = items->m_items[i];
            break;
        }
    }
    if (slot < 0 || !vendorItem)
    {
        LOG_INFO("playerbots", "Companion {}: mount blocked reason=vendor-lacks-item vendor={} item={}", bot->GetName(),
                 vendor->GetEntry(), itemId);
        return false;
    }

    // Decide before paying (issue #68): inspect spells, reject already-known mounts, enforce full CanUseItem +
    // reputation requirements, and keep the configured gold reserve after purchase.
    uint32 taught = 0;
    bool hasUseSpell = (proto->Spells[0].SpellId == 483 || proto->Spells[0].SpellId == 55884);
    for (auto const& spell : proto->Spells)
    {
        if (spell.SpellTrigger == ITEM_SPELLTRIGGER_LEARN_SPELL_ID && spell.SpellId > 0)
            taught = static_cast<uint32>(spell.SpellId);
        if (spell.SpellTrigger == ITEM_SPELLTRIGGER_ON_USE && spell.SpellId > 0)
            hasUseSpell = true;
    }
    if (taught && bot->HasSpell(taught))
    {
        LOG_INFO("playerbots", "Companion {}: mount blocked reason=already-known item={} spell={}", bot->GetName(),
                 itemId, taught);
        return false;
    }
    if (!taught && !hasUseSpell)
    {
        LOG_INFO("playerbots", "Companion {}: mount blocked reason=no-use-spell item={}", bot->GetName(), itemId);
        return false;
    }
    if (InventoryResult const res = CanUseMountTemplate(bot, proto); res != EQUIP_ERR_OK)
    {
        LOG_INFO("playerbots", "Companion {}: mount blocked reason=cannot-use item={} res={}", bot->GetName(), itemId,
                 static_cast<uint32>(res));
        return false;
    }

    // Reuse a copy already sitting in bags rather than buying a duplicate; otherwise purchase from the vendor.
    uint32 cost = 0;
    Item* item = bot->GetItemByEntry(itemId);
    if (!item)
    {
        if (vendorItem->ExtendedCost != 0)
        {
            LOG_INFO("playerbots", "Companion {}: mount blocked reason=extended-cost vendor={} item={} extCost={}",
                     bot->GetName(), vendor->GetEntry(), itemId, vendorItem->ExtendedCost);
            return false;
        }
        // Match Player::BuyItemFromVendorSlot's reputation discount formula before checking gold reserve.
        uint64 const effectivePrice = static_cast<uint64>(
            std::floor(static_cast<double>(proto->BuyPrice) * bot->GetReputationPriceDiscount(vendor)));
        uint64 const reserveCopper = MountReserveCopper();
        if (static_cast<uint64>(bot->GetMoney()) < effectivePrice + reserveCopper)
        {
            LOG_INFO("playerbots",
                     "Companion {}: mount blocked reason=gold-reserve item={} price={} reserve={} gold={}",
                     bot->GetName(), itemId, effectivePrice, reserveCopper, bot->GetMoney());
            return false;
        }
        uint32 const moneyBefore = bot->GetMoney();
        uint32 const countBefore = bot->GetItemCount(itemId);
        bool const bought =
            bot->BuyItemFromVendorSlot(vendor->GetGUID(), static_cast<uint32>(slot), itemId, 1, NULL_BAG, NULL_SLOT);
        if (!bought || bot->GetItemCount(itemId) <= countBefore)
        {
            LOG_INFO("playerbots", "Companion {}: mount blocked reason=buy-failed vendor={} item={} gold={}",
                     bot->GetName(), vendor->GetEntry(), itemId, bot->GetMoney());
            return false;
        }
        cost = moneyBefore > bot->GetMoney() ? moneyBefore - bot->GetMoney() : 0;
        item = bot->GetItemByEntry(itemId);
    }

    if (!item)
    {
        LOG_INFO("playerbots", "Companion {}: mount blocked reason=item-missing item={}", bot->GetName(), itemId);
        return false;
    }
    if (InventoryResult const itemRes = bot->CanUseItem(item); itemRes != EQUIP_ERR_OK)
    {
        LOG_INFO("playerbots", "Companion {}: mount blocked reason=cannot-use-item item={} res={}", bot->GetName(),
                 itemId, static_cast<uint32>(itemRes));
        return false;
    }

    // Use the item through the stock item-use path (handles both learn-trigger mounts and non-learn on-use items,
    // enforcing level, class, skill, and reputation checks). If the cast has a cast time or is interrupted by GCD,
    // MountOption retries using the item directly from bags without buying again.
    if (cost > 0)
        CompanionDuty::RecordLedger(bot->GetGUID().GetCounter(), "mount", itemId, cost, -1, "mount project");
    if (bot->IsAlive() && !bot->IsInCombat() && !bot->IsInFlight() && !bot->GetVehicle() &&
        !bot->IsNonMeleeSpellCast(false))
    {
        if (bot->isMoving())
            bot->StopMoving();
        SpellCastTargets targets;
        targets.SetUnitTarget(bot);
        bot->CastItemUseSpell(item, targets, 1, 0);
    }

    bool const learnedNow = taught && bot->HasSpell(taught);
    LOG_INFO("playerbots", "Companion {}: mount {} item={} spell={} cost={} vendor={} gold={}", bot->GetName(),
             learnedNow ? "learned" : "use-pending", itemId, taught, cost, vendor->GetEntry(), bot->GetMoney());
    return true;
}

// Map thread. Takes the lease and starts the trip for the chosen option (Evaluate, or a forced GM trip).
static bool StartTrip(PlayerbotAI* botAI, Option chosen, std::string const& via, bool force)
{
    using TripKind = CompanionQuest::TripKind;
    using Phase = CompanionQuest::Phase;
    using Trip = CompanionQuest::Trip;
    Player* bot = botAI->GetBot();
    uint32 const low = bot->GetGUID().GetCounter();
    uint32 const now = NowSec();
    std::string nodesLegs;
    // Start the trip: the lease first.
    CompanionLease::Destination dest;
    std::string owner;
    switch (chosen.kind)
    {
        case TripKind::TurnIn:
        case TripKind::Work:
            dest.key = "quest:" + std::to_string(chosen.questId);
            owner = "quest:" + std::to_string(chosen.questId);
            break;
        case TripKind::Hub:
            dest.key = "hub:" + std::to_string(chosen.hubIndex);
            owner = "quest:hub:" + std::to_string(chosen.zone);
            break;
        case TripKind::Mount:
            dest.key = CompanionLease::SpawnKey(false, chosen.spawnId);
            owner = "mount:buy";
            break;
        case TripKind::Travel:
            dest.key = "point:" + std::to_string(chosen.map) + ":" + std::to_string(static_cast<int32>(chosen.x)) +
                       ":" + std::to_string(static_cast<int32>(chosen.y));
            owner = "travel:point";
            break;
        default:
            return false;
    }
    bool const crossMap = chosen.map != bot->GetMapId();
    if (chosen.kind == TripKind::Work && !crossMap)
    {
        // The objective point has no height: read it now for the chosen target only (another map's terrain is read
        // on arrival there, SetTripPoint).
        float h = bot->GetMap()->GetHeight(chosen.x, chosen.y, MAX_HEIGHT);
        if (h > INVALID_HEIGHT)
            chosen.z = h;
    }
    dest.mapId = chosen.map;
    dest.x = chosen.x;
    dest.y = chosen.y;
    dest.z = chosen.z;
    CompanionLease::Policy policy;
    policy.allowCorpses = true;
    policy.allowQuest = true;
    policy.allowRpg = false;   // new-rpg must not change status while the trip runs
    policy.nearYards = sPlayerbotAIConfig.companionLeaseNearYards;
    policy.cooldownOnSuccess = chosen.kind == TripKind::Hub;
    if (!CompanionLease::Begin(bot, owner, dest, 0, policy, chosen.score, force))
    {
        Block(low, "the lease refused " + owner + " (cooldown, budget or commit)");
        return false;
    }
    CompanionTransport::Reset(low);

    Trip trip;
    bool returnFrom = false;
    uint32 stayedBefore = 0;
    trip.active = true;
    trip.kind = chosen.kind;
    trip.owner = owner;
    trip.questId = chosen.questId;
    trip.zone = chosen.zone;
    trip.fromZone = bot->GetZoneId();
    trip.mapId = chosen.map;
    trip.x = chosen.x;
    trip.y = chosen.y;
    trip.z = chosen.z;
    trip.spawnId = chosen.spawnId;
    trip.spawnIsGo = chosen.isGo;
    trip.itemId = chosen.itemId;
    trip.flight = chosen.route.flight;
    trip.phase = trip.flight ? Phase::ToFlightMaster : Phase::ToTarget;
    trip.fmX = chosen.route.fmX;
    trip.fmY = chosen.route.fmY;
    trip.fmZ = chosen.route.fmZ;
    trip.nodes = chosen.route.nodes;
    trip.startSec = now;
    trip.score = chosen.score;
    trip.hubPlan = chosen.hubPlan;
    trip.cross = crossMap;
    trip.zUnknown = crossMap && chosen.kind == TripKind::Work;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        QuestBotState& st = gStates[low];
        st.trip = trip;
        if (crossMap)
        {
            // D route: leaving a continent the bot stayed on (its goals there ran out, or the safety max) by a
            // cross-map trip is the return; it is a normal trip, logged here.
            if (st.crossArriveSec && bot->GetMapId() == st.crossToMap)
                returnFrom = true;
            stayedBefore = st.crossArriveSec ? now - st.crossArriveSec : 0;
            // D route: only a cross-continent quest trip (work or turn-in) starts a stay on the map it reaches. Hub,
            // travel and mount trips do not, and neither does a return (the bot is then on a map it did not cross to).
            // C route hysteresis: the stay starts at the first evaluation on that map.
            bool const questTrip =
                (chosen.kind == TripKind::Work || chosen.kind == TripKind::TurnIn) && !returnFrom;
            st.crossToMap = questTrip ? chosen.map : NO_CROSS_MAP;
            st.crossArriveSec = 0;
            st.crossDoneLogged = false;
        }
    }
    if (returnFrom)
        LOG_INFO("playerbots", "Companion {}: trip cross return from={} to={} stayed={}s kind={} id={} score={}",
                 bot->GetName(), bot->GetMapId(), chosen.map, stayedBefore, KindName(chosen.kind), chosen.questId,
                 Num(chosen.score));
    botAI->rpgInfo.ChangeToIdle();
    Block(low, "");

    std::string by = trip.flight ? "flight"
                     : chosen.route.transport
                         ? "transport"
                         : (CompanionQuest::HasGroundMount(bot) && CompanionQuest::MountEnabled() ? "mount" : "walk");
    if (chosen.route.transport)
        nodesLegs = " legs=" + chosen.route.legs;
    std::string nodes = nodesLegs;
    if (trip.flight && !trip.nodes.empty())
        nodes = " nodes=" + std::to_string(trip.nodes.front()) + "-" + std::to_string(trip.nodes.back());
    LOG_INFO("playerbots",
             "Companion {}: trip start kind={} id={} from={} to={} by={} dist={} est={}s score={}{}{}{}{}",
             bot->GetName(), KindName(trip.kind), trip.questId, trip.fromZone, trip.zone, by,
             Num(chosen.route.dist, 0), Num(chosen.route.seconds, 0), Num(chosen.score), nodes,
             via.empty() ? "" : " via=" + via, force ? " forced=1" : "",
             crossMap ? " cross=" + std::to_string(bot->GetMapId()) + ">" + std::to_string(chosen.map) : "");
    if (trip.kind == TripKind::Hub)
    {
        std::string take;
        for (uint32 questId : trip.hubPlan)
            take += (take.empty() ? "" : ",") + std::to_string(questId);
        uint32 const free = FreeLogSlots(bot);
        LOG_INFO("playerbots", "Companion {}: hub plan zone={} free={} keep={} take={}", bot->GetName(), trip.zone,
                 free, gFreeSlots.load(), take.empty() ? "-" : take);
    }
    return true;
}

// Map thread. A forced trip from the GM command (.companion travel): the same trip and lease as a picked one, with
// force (a committed lease is replaced), score 999.
static void StartForced(PlayerbotAI* botAI, CompanionTransport::ForceRequest const& req)
{
    using TripKind = CompanionQuest::TripKind;
    using Kind = CompanionTransport::ForceRequest::Kind;
    Player* bot = botAI->GetBot();
    uint32 const low = bot->GetGUID().GetCounter();
    RoutePlanner planner(bot);
    Option option;
    option.score = 999.0f;
    std::string fail;
    if (req.kind == Kind::Quest)
    {
        auto it = gEnders.find(req.id);
        if (bot->GetQuestStatus(req.id) != QUEST_STATUS_COMPLETE)
            fail = "quest-not-complete";
        else if (it == gEnders.end())
            fail = "no-ender";
        else
        {
            uint32 const hostileBit = bot->GetTeamId() == TEAM_HORDE ? 4u : 2u;
            bool found = false;
            for (EnderSpot const& spot : it->second)
            {
                if (spot.hostileMask & hostileBit)
                    continue;
                Route route = planner.Plan(spot.map, spot.x, spot.y, spot.z, spot.zone);
                if (!route.ok || (found && route.seconds >= option.route.seconds))
                    continue;
                found = true;
                option.kind = TripKind::TurnIn;
                option.questId = req.id;
                option.zone = spot.zone;
                option.map = spot.map;
                option.x = spot.x;
                option.y = spot.y;
                option.z = spot.z;
                option.spawnId = spot.spawnId;
                option.isGo = spot.isGo;
                option.route = route;
            }
            if (!found)
                fail = "no-route";
        }
    }
    else if (req.kind == Kind::Work)
    {
        Quest const* quest = sObjectMgr->GetQuestTemplate(req.id);
        uint32 otherMap = 0;
        if (!quest || bot->GetQuestStatus(req.id) != QUEST_STATUS_INCOMPLETE)
            fail = "quest-not-incomplete";
        else if (!WorkTarget(bot, quest, planner, true, option, otherMap))
            fail = otherMap ? "no-route" : "no-open-poi";
        else
            option.score = 999.0f;
    }
    else if (req.kind == Kind::Vendor)
    {
        uint32 const riding = bot->GetSkillValue(SKILL_RIDING);
        uint64 const reserveCopper = MountReserveCopper();
        bool found = false;
        bool seen = false;
        uint32 bestPrice = 0;
        for (MountOffer const& offer : gMountOffers)
        {
            if (offer.entry != req.id)
                continue;
            seen = true;
            if ((offer.races && !(offer.races & bot->getRaceMask())) || offer.skillRank > riding)
                continue;
            if (offer.spellId && bot->HasSpell(offer.spellId))
                continue;
            ItemTemplate const* proto = sObjectMgr->GetItemTemplate(offer.itemId);
            if (!proto || CanUseMountTemplate(bot, proto) != EQUIP_ERR_OK)
                continue;
            if (!bot->HasItemCount(offer.itemId, 1) &&
                static_cast<uint64>(offer.price) + reserveCopper > static_cast<uint64>(bot->GetMoney()))
                continue;
            if (found && offer.price >= bestPrice)
                continue;   // the cheapest mount the bot can use
            Route route = planner.Plan(offer.map, offer.x, offer.y, offer.z, offer.zone);
            if (!route.ok)
                continue;
            found = true;
            option.kind = TripKind::Mount;
            option.zone = offer.zone;
            option.map = offer.map;
            option.x = offer.x;
            option.y = offer.y;
            option.z = offer.z;
            option.spawnId = offer.spawnId;
            option.itemId = offer.itemId;
            option.route = route;
            bestPrice = offer.price;
        }
        if (!found)
            fail = seen ? "no-usable-mount(race,riding,gold,route)" : "not-a-mount-vendor";
        else if (CompanionQuest::HasGroundMount(bot))
            fail = "has-mount";
    }
    else
    {
        option.kind = TripKind::Travel;
        option.map = req.mapId;
        option.x = req.x;
        option.y = req.y;
        option.z = req.z;
        option.zone = req.zone;
        option.route = planner.Plan(req.mapId, req.x, req.y, req.z, req.zone);
        if (!option.route.ok)
            fail = "no-route";
    }
    if (fail.empty())
    {
        CompanionQuest::Trip running;
        if (CompanionQuest::ActiveTrip(low, running))
            CompanionQuest::EndTrip(botAI, "replaced:forced", true);
        if (!StartTrip(botAI, option, "", true))
            fail = "lease-refused";
    }
    LOG_INFO("playerbots", "Companion {}: travel forced kind={} id={} to={}:{},{} result={} legs={} est={}s",
             bot->GetName(),
             req.kind == Kind::Quest    ? "quest"
             : req.kind == Kind::Work   ? "work"
             : req.kind == Kind::Vendor ? "vendor"
                                        : "point",
             req.id,
             option.map, Num(option.x, 0), Num(option.y, 0), fail.empty() ? "start" : "fail:" + fail,
             option.route.transport ? option.route.legs : std::string(option.route.flight ? "flight" : "walk"),
             Num(option.route.seconds, 0));
}

void CompanionQuest::Evaluate(PlayerbotAI* botAI)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot || !Enabled() || !gTablesReady.load())
        return;
    uint32 const low = bot->GetGUID().GetCounter();
    ForcedAbandon(bot, low);   // GM test hook: taken at the next evaluation, before any early return below
    static std::once_flag registered;
    std::call_once(registered, []()
                   {
                       CompanionLease::RegisterActivity("quest");
                       CompanionLease::RegisterActivity("mount");
                   });

    DetectTurnIns(bot, low);
    if (!bot->IsAlive() || bot->IsInCombat() || bot->IsInFlight() || !bot->GetMap() || bot->GetMap()->Instanceable())
        return;
    {
        CompanionTransport::ForceRequest forced;
        if (CompanionTransport::TakeForce(low, forced))
        {
            StartForced(botAI, forced);
            return;
        }
    }

    uint32 const now = NowSec();
    bool tripActive = false;
    std::string tripOwner;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        QuestBotState& st = gStates[low];
        if (st.lastEvalSec && now - st.lastEvalSec < gEvalSeconds.load())
            return;
        st.lastEvalSec = now;
        tripActive = st.trip.active;
        tripOwner = st.trip.owner;
    }
    if (tripActive)
    {
        if (CompanionLease::Holds(low, tripOwner))
        {
            Block(low, "running now: " + tripOwner);
            return;
        }
        EndTrip(botAI, "lease-ended", false);
    }
    if (CompanionDuty::HasActiveDuty(low))
    {
        Block(low, "a duty is running (its score beat the activity score + margin)");
        return;
    }
    bool urgentOnly = false;   // set while new-rpg works a quest: only a turn-in or the mount may take over
    // New-rpg already works an incomplete quest here (DO_QUEST): keep it until it ends.
    if (botAI->rpgInfo.GetStatus() == RPG_DO_QUEST)
    {
        auto const* doing = std::get_if<NewRpgInfo::DoQuest>(&botAI->rpgInfo.data);
        if (doing && bot->GetQuestStatus(doing->questId) == QUEST_STATUS_INCOMPLETE)
        {
            // Keep working it, but look again every DO_QUEST_RECHECK_SECONDS: a turn-in or the mount may take over
            // (run 1, 2026-11-19: Wren stayed in DO_QUEST all hour with quest 67 complete in her log and no mount).
            bool recheck = false;
            {
                std::lock_guard<std::mutex> lock(gStateMutex);
                QuestBotState& st = gStates[low];
                if (st.doQuestId != doing->questId)
                {
                    st.doQuestId = doing->questId;
                    st.doQuestSince = now;
                }
                recheck = now - st.doQuestSince >= DO_QUEST_RECHECK_SECONDS;
                if (recheck)
                    st.doQuestSince = now;   // next look in another DO_QUEST_RECHECK_SECONDS
            }
            if (!recheck)
            {
                Block(low, "working quest " + std::to_string(doing->questId) +
                                                              " here (new-rpg DO_QUEST)");
                return;
            }
            urgentOnly = true;
        }
    }
    // A quest handed to DO_QUEST that new-rpg dropped within two evaluations has no workable POI for it: do not
    // offer it as work for 15 minutes (else the bot would bounce between the project and an idle new-rpg).
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        QuestBotState& st = gStates[low];
        if (st.lastHereQuest && now - st.lastHereSec <= 2 * gEvalSeconds.load() + 10)
            st.skipUntil[st.lastHereQuest] = now + 900;
        st.lastHereQuest = 0;
    }
    AbandonCheck(bot, low, now);
    std::unordered_map<uint32, uint32> completeSince;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        QuestBotState& st = gStates[low];
        st.blockedNow.clear();
        st.completeLines.clear();
        st.lastFullEvalSec = now;
        st.logUsed = MAX_QUEST_LOG_SIZE - FreeLogSlots(bot);
        completeSince = st.completeSince;
    }
    std::vector<std::string> completeLines;

    uint32 const level = bot->GetLevel();
    uint32 const mapId = bot->GetMapId();
    uint32 const hostileBit = bot->GetTeamId() == TEAM_HORDE ? 4u : 2u;
    float const travelW = CompanionBrain::Weight(low, "travel", CompanionBrain::DutyDefault("travel"));
    float const wTurnIn = CompanionBrain::Weight(low, "quest_turnin", gWTurnIn.load());
    float const wWork = CompanionBrain::Weight(low, "quest_work", gWWork.load());
    float const wHub = CompanionBrain::Weight(low, "quest_hub", gWHub.load());
    float const wGrind = CompanionBrain::Weight(low, "quest_grind", gWGrind.load());
    RoutePlanner planner(bot);
    bool const transportOn = CompanionTransport::Enabled() && CompanionTransport::Ready();
    std::vector<Option> options;

    // a. Turn-ins: nearest ender (by travel time) of every complete quest.
    for (uint8 slot = 0; slot < MAX_QUEST_LOG_SIZE; ++slot)
    {
        uint32 questId = bot->GetQuestSlotQuestId(slot);
        if (!questId || bot->GetQuestStatus(questId) != QUEST_STATUS_COMPLETE)
            continue;
        Quest const* quest = sObjectMgr->GetQuestTemplate(questId);
        auto it = gEnders.find(questId);
        if (!quest || it == gEnders.end())
        {
            completeLines.push_back(std::to_string(questId) + " blocked: no ender spawn in the world DB");
            continue;
        }
        if (!CompanionLease::MayTry(low, "quest:" + std::to_string(questId)))
        {
            completeLines.push_back(std::to_string(questId) + " blocked: lease cooldown or retry budget (why " +
                                    "<name> quest:" + std::to_string(questId) + ")");
            continue;
        }
        Option best;
        bool found = false;
        uint32 otherMap = 0;
        uint32 otherZone = 0;
        uint32 unsafeZone = 0;
        for (EnderSpot const& spot : it->second)
        {
            if (spot.hostileMask & hostileBit)
                continue;
            if (spot.map != mapId && !transportOn)
            {
                otherMap = spot.map;
                otherZone = spot.zone;
                continue;
            }
            if (!ZoneSafe(spot.zone, level))
            {
                unsafeZone = spot.zone;
                continue;
            }
            Route route = planner.Plan(spot.map, spot.x, spot.y, spot.z, spot.zone);
            if (!route.ok && spot.map != mapId)
            {
                otherMap = spot.map;
                otherZone = spot.zone;
                continue;
            }
            if (!route.ok || (found && route.seconds >= best.route.seconds))
                continue;
            found = true;
            best.kind = TripKind::TurnIn;
            best.questId = questId;
            best.zone = spot.zone;
            best.map = spot.map;
            best.x = spot.x;
            best.y = spot.y;
            best.z = spot.z;
            best.spawnId = spot.spawnId;
            best.isGo = spot.isGo;
            best.route = route;
        }
        if (!found)
        {
            if (otherMap)
                LogBlockedOnce(bot, low, "turnin", questId, otherZone, otherMap, "no-transport");
            else if (unsafeZone)
                LogBlockedOnce(bot, low, "turnin", questId, unsafeZone, mapId, "zone-level");
            completeLines.push_back(std::to_string(questId) + " blocked: " +
                                    (otherMap ? "ender on map " + std::to_string(otherMap) + " (no-transport)"
                                              : unsafeZone ? "ender zone " + std::to_string(unsafeZone) +
                                                                 " too high (zone-level)"
                                                           : std::string("no friendly ender spawn")));
            continue;
        }
        float value = QuestValue(bot, quest);
        // B4: the waiting bonus, so a far ender (Kelda 161, Wetlands) is not starved by nearer turn-ins and hubs.
        auto since = completeSince.find(questId);
        best.waitMin = since != completeSince.end() && now > since->second ? (now - since->second) / 60 : 0;
        best.waitBonus = std::min(gWaitBonusMax.load(), best.waitMin * gWaitBonusPerMin.load());
        float const travel = TravelCost(best.route, travelW);
        best.score = (TURNIN_BASE + std::min(40.0f, value)) * wTurnIn + best.waitBonus - travel;
        completeLines.push_back(std::to_string(questId) + " score " + Num(best.score) + " = (base " +
                                Num(TURNIN_BASE, 0) + " + value " + Num(std::min(40.0f, value)) + ") x " +
                                Num(wTurnIn, 2) + " + waitBonus=" + Num(best.waitBonus) + " (waited " +
                                std::to_string(best.waitMin) + "m) - travel " + Num(travel) + " (" +
                                Num(best.route.seconds, 0) + "s" + (best.route.flight ? " by flight" : "") +
                                ", zone " + std::to_string(best.zone) + ")");
        options.push_back(best);
    }

    // b. Work: incomplete quests with an objective POI on this map (or, C route, on another companion map),
    // objective mobs at most 3 levels above.
    bool const crossWork = transportOn && gCrossWork.load();
    for (uint8 slot = 0; slot < MAX_QUEST_LOG_SIZE; ++slot)
    {
        uint32 questId = bot->GetQuestSlotQuestId(slot);
        if (!questId || bot->GetQuestStatus(questId) != QUEST_STATUS_INCOMPLETE ||
            botAI->lowPriorityQuest.count(questId))
            continue;
        Quest const* quest = sObjectMgr->GetQuestTemplate(questId);
        QuestPOIVector const* pois = quest ? sObjectMgr->GetQuestPOIVector(questId) : nullptr;
        if (!pois || ObjectiveMobAbove(quest, bot, level + 3))
            continue;
        if (!CompanionLease::MayTry(low, "quest:" + std::to_string(questId)))
        {
            NoteBlocked(low, "work " + std::to_string(questId) + ": lease cooldown or retry budget");
            continue;
        }
        {
            std::lock_guard<std::mutex> lock(gStateMutex);
            QuestBotState& st = gStates[low];
            auto skip = st.skipUntil.find(questId);
            if (skip != st.skipUntil.end() && now < skip->second)
                continue;
        }
        Option best;
        uint32 otherMap = 0;
        if (!WorkTarget(bot, quest, planner, crossWork, best, otherMap))
        {
            if (otherMap)
                LogBlockedOnce(bot, low, "work", questId, 0, otherMap, crossWork ? "no-route" : "no-transport");
            continue;
        }
        if (!ZoneSafe(best.zone, level))
            continue;
        best.here = !best.cross && best.zone == bot->GetZoneId() && best.route.dist < POI_HERE_YARDS;
        float value = QuestValue(bot, quest);
        float const base = (35.0f + std::min(30.0f, value * 0.7f)) * wWork;
        // A cross-map option keeps its base here; the batch rules below charge the crossing.
        best.score = best.cross ? base : base - TravelCost(best.route, travelW);
        options.push_back(best);
    }

    // b2. Cross-continent batching and hysteresis (C route, Quest.CrossContinentWork; D route stay rule).
    if (crossWork)
    {
        // here = the worthwhile goals on the bot's map: turn-ins and work options that beat the grind fallback.
        uint32 here = 0;
        float const grindScore = wGrind;
        std::map<uint32, std::pair<uint32, uint32>> there;   // map -> (work, turn-ins)
        for (Option const& option : options)
        {
            if (option.kind != TripKind::TurnIn && option.kind != TripKind::Work)
                continue;
            if (option.map == mapId)
            {
                if (option.score >= grindScore)
                    ++here;
            }
            else if (option.kind == TripKind::Work)
                ++there[option.map].first;
            else
                ++there[option.map].second;
        }
        // Stay (D route): the bot stays on the continent it crossed to until no worthwhile goals remain there, with
        // Quest.CrossStayMaxMinutes (90) as the safety max. Then the way back is a normal (batched) trip.
        uint32 stayedSec = 0;
        bool arrived = false;
        bool doneLog = false;
        {
            std::lock_guard<std::mutex> lock(gStateMutex);
            QuestBotState& st = gStates[low];
            if (st.crossToMap == mapId && !st.crossArriveSec)
                st.crossArriveSec = now;   // arrived by the cross-map trip
            if (st.crossToMap == mapId && st.crossArriveSec)
            {
                arrived = true;
                stayedSec = now - st.crossArriveSec;
                uint32 const stayMax = gCrossStayMaxMinutes.load() * 60;
                if (!st.crossDoneLogged && (!here || stayedSec >= stayMax))
                {
                    st.crossDoneLogged = true;
                    doneLog = true;
                }
            }
        }
        uint32 const stayMax = gCrossStayMaxMinutes.load() * 60;
        bool const staying = arrived && here && stayedSec < stayMax;
        if (doneLog)
            LOG_INFO("playerbots", "Companion {}: trip cross done map={} goalsLeft={} stayed={}s{}", bot->GetName(),
                     mapId, here, stayedSec, here ? " max=" + std::to_string(gCrossStayMaxMinutes.load()) : "");
        uint32 const minGoals = gCrossMinGoals.load();
        float const penalty = gCrossPenalty.load();
        for (auto const& [otherMap, counts] : there)
        {
            uint32 const goals = counts.first + counts.second;
            char const* result = staying ? "stay" : (here && goals < minGoals) ? "few" : "allow";
            bool const allow = result[0] == 'a';
            // Bundle (owner spec: do the goals there on one visit): the sum of the goals' own scores there, minus the
            // real crossing cost both ways and the penalty. Work goals then share it, one slot each, so a bundle
            // ranks against one local option by its average per goal.
            Option const* bestCross = nullptr;
            float crossSum = 0.0f;
            for (Option const& option : options)
            {
                if (option.map != otherMap || (option.kind != TripKind::TurnIn && option.kind != TripKind::Work))
                    continue;
                crossSum += std::max(0.0f, option.score);
                if (option.kind == TripKind::Work && (!bestCross || option.score > bestCross->score))
                    bestCross = &option;
            }
            float const crossing = bestCross ? 2.0f * TravelCost(bestCross->route, travelW) + penalty : penalty;
            float const bundle = goals ? (crossSum - crossing) / static_cast<float>(goals) : 0.0f;
            for (Option& option : options)
            {
                if (option.map != otherMap || (option.kind != TripKind::TurnIn && option.kind != TripKind::Work))
                    continue;
                if (option.kind == TripKind::TurnIn)
                {
                    // Turn-ins keep their own scoring; only the stay holds them back.
                    if (result[0] == 's')
                        option.drop = true;
                    continue;
                }
                if (!allow)
                    option.drop = true;
                else   // the goal's share of the bundle, less its own leg (the nearest goal of the bundle goes first)
                    option.score = bundle - TravelCost(option.route, travelW) / static_cast<float>(goals);
            }
            // The option the trip would take: the best scored cross option (work bundle share or turn-in).
            bestCross = nullptr;
            for (Option const& option : options)
                if (option.map == otherMap && !option.drop &&
                    (option.kind == TripKind::TurnIn || option.kind == TripKind::Work) &&
                    (!bestCross || option.score > bestCross->score))
                    bestCross = &option;
            if (!allow)
                NoteBlocked(low, "cross to map " + std::to_string(otherMap) + ": " + result + " (" +
                                     std::to_string(goals) + " goals there, " + std::to_string(here) + " here)");
            bool logIt = false;
            {
                std::lock_guard<std::mutex> lock(gStateMutex);
                auto& last = gStates[low].crossLog[otherMap];
                if (last.first != result || now - last.second >= PICK_LOG_SECONDS)
                {
                    last.first = result;
                    last.second = now;
                    logIt = true;
                }
            }
            if (logIt)
                LOG_INFO("playerbots",
                         "Companion {}: trip cross map={} from={} work={} turnin={} here={} result={} best={} score={} "
                         "travel={}s cost={} bundle={} sum={} minGoals={} stayLeft={}s",
                         bot->GetName(), otherMap, mapId, counts.first, counts.second, here, result,
                         bestCross ? bestCross->Label() : std::string("-"),
                         bestCross ? Num(bestCross->score) : std::string("-"),
                         bestCross ? Num(bestCross->route.seconds, 0) : std::string("-"), Num(crossing),
                         Num(bundle), Num(crossSum), minGoals, staying ? stayMax - stayedSec : 0);
        }
        options.erase(std::remove_if(options.begin(), options.end(), [](Option const& o) { return o.drop; }),
                      options.end());
    }

    // c. Hubs: givers with at least two quests the bot can take now.
    {
        std::vector<CompanionBotMgr::QuestGiverSpot> const& spots = sCompanionBotMgr.QuestGiverSpots();
        std::vector<std::pair<float, uint32>> nearHubs;
        for (uint32 i = 0; i < gHubs.size(); ++i)
        {
            Hub const& hub = gHubs[i];
            if (hub.map != mapId)
                continue;
            float d = Dist2d(bot->GetPositionX(), bot->GetPositionY(), hub.x, hub.y);
            if (d <= HUB_MAX_YARDS)
                nearHubs.emplace_back(d, i);
        }
        std::sort(nearHubs.begin(), nearHubs.end());
        if (nearHubs.size() > HUB_CANDIDATES)
            nearHubs.resize(HUB_CANDIDATES);
        uint32 const raceMask = bot->getRaceMask();
        int32 const hideDiff = static_cast<int32>(sWorld->getIntConfig(CONFIG_QUEST_LOW_LEVEL_HIDE_DIFF));
        // B3: a hub may fill the log only down to Quest.FreeSlots free slots; it takes its best quests first.
        uint32 const freeSlots = FreeLogSlots(bot);
        uint32 const keep = gFreeSlots.load();
        uint32 const budget = freeSlots > keep ? freeSlots - keep : 0;
        if (!budget)
        {
            NoteBlocked(low, "hubs: log has " + std::to_string(freeSlots) + " free slots, keeps " +
                                 std::to_string(keep) + " (Quest.FreeSlots)");
            nearHubs.clear();
        }
        float const greyMin = gGreyMinValue.load();
        for (auto const& [d, hubIndex] : nearHubs)
        {
            Hub const& hub = gHubs[hubIndex];
            if (!ZoneSafe(hub.zone, level) || !CompanionLease::MayTry(low, "hub:" + std::to_string(hubIndex)))
                continue;
            std::unordered_set<uint32> seen;
            std::vector<std::pair<float, uint32>> values;   // value, quest
            for (uint32 spotIndex : hub.spots)
            {
                CompanionBotMgr::QuestGiverSpot const& spot = spots[spotIndex];
                if ((spot.hostileMask & hostileBit) || !seen.insert(spot.questId).second)
                    continue;
                if (spot.minLevel > level || (spot.allowableRaces && !(spot.allowableRaces & raceMask)))
                    continue;
                Quest const* quest = sObjectMgr->GetQuestTemplate(spot.questId);
                if (!quest || quest->IsRepeatable() || quest->IsSeasonal() || quest->IsDailyOrWeekly())
                    continue;
                if (bot->GetQuestStatus(spot.questId) != QUEST_STATUS_NONE || bot->GetQuestRewardStatus(spot.questId))
                    continue;
                int32 questLevel = bot->GetQuestLevel(quest);
                if (questLevel > static_cast<int32>(level) + 3)
                    continue;
                if (!gAcceptGrey.load() && static_cast<int32>(level) > questLevel + hideDiff)
                    continue;
                if (!bot->CanTakeQuest(quest, false) || !bot->CanAddQuest(quest, false))
                    continue;
                // Same filters as the accept loop (NewRpgBaseAction::IsQuestCapableDoing), so the plan holds only
                // quests the bot will really take: no elite / dungeon / group quests.
                if (quest->GetType() != 0 || quest->GetSuggestedPlayers() >= 2)
                    continue;
                float value = QuestValue(bot, quest);
                if (static_cast<int32>(level) > questLevel + hideDiff && value < greyMin)
                    continue;   // B3: low-value grey quest
                values.emplace_back(value, spot.questId);
            }
            if (values.size() < 2)
                continue;
            std::sort(values.rbegin(), values.rend());
            if (values.size() > budget)
                values.resize(budget);
            float sum = 0.0f;
            for (size_t i = 0; i < values.size() && i < 8; ++i)
                sum += values[i].first;
            Option option;
            option.kind = TripKind::Hub;
            option.hubIndex = hubIndex;
            for (auto const& [value, questId] : values)
                option.hubPlan.push_back(questId);
            option.zone = hub.zone;
            option.map = hub.map;
            option.x = hub.x;
            option.y = hub.y;
            option.z = hub.z;
            option.route = planner.Plan(hub.map, hub.x, hub.y, hub.z, hub.zone);
            if (!option.route.ok)
                continue;
            float need = std::min(90.0f, 10.0f + sum * 0.5f + 3.0f * values.size());
            option.score = need * wHub - TravelCost(option.route, travelW) +
                           CompanionLife::NeighborhoodHubBonus(bot, hub.zone);
            options.push_back(option);
        }
    }

    // d. Mount project.
    MountOption(bot, low, planner, travelW, options, now);

    // e. Grind: the low fallback.
    {
        Option grind;
        grind.grind = true;
        grind.score = wGrind;
        options.push_back(grind);
    }

    std::stable_sort(options.begin(), options.end(),
                     [](Option const& a, Option const& b) { return a.score > b.score; });
    std::string top;
    std::vector<std::string> topLines;
    for (size_t i = 0; i < options.size() && i < TOP_OPTIONS; ++i)
    {
        if (i < 3)
            top += (i ? "," : "") + options[i].Label() + ":" + Num(options[i].score);
        std::string extra;
        if (options[i].kind == TripKind::TurnIn && !options[i].grind && options[i].waitMin)
            extra = ", waiting " + std::to_string(options[i].waitMin) + "m waitBonus=" + Num(options[i].waitBonus);
        if (options[i].kind == TripKind::Hub && !options[i].grind)
            extra = ", takes " + std::to_string(options[i].hubPlan.size());
        topLines.push_back(options[i].Label() + " score " + Num(options[i].score) + " (travel " +
                           Num(options[i].route.seconds, 0) + "s" + (options[i].route.flight ? " by flight" : "") +
                           extra + ")");
    }
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        QuestBotState& st = gStates[low];
        st.top = topLines;
        st.completeLines = completeLines;
    }

    // Plan one trip for several goals: a turn-in on the way to a farther trip target goes first.
    Option chosen = options.front();
    std::string via;
    if (!chosen.grind && !chosen.here && chosen.kind != TripKind::TurnIn)
    {
        float direct = Dist2d(bot->GetPositionX(), bot->GetPositionY(), chosen.x, chosen.y);
        for (Option const& option : options)
        {
            if (option.kind != TripKind::TurnIn || option.route.flight || option.route.transport ||
                chosen.route.transport || option.map != chosen.map)
                continue;
            float leg1 = Dist2d(bot->GetPositionX(), bot->GetPositionY(), option.x, option.y);
            float leg2 = Dist2d(option.x, option.y, chosen.x, chosen.y);
            if (leg1 + leg2 <= direct * 1.15f + 150.0f)
            {
                via = "turnin:" + std::to_string(option.questId) + " before " + chosen.Label();
                chosen = option;
                break;
            }
        }
    }

    if (urgentOnly && chosen.kind != TripKind::TurnIn && chosen.kind != TripKind::Mount)
    {
        // Nothing urgent beats the quest new-rpg works here: keep DO_QUEST.
        bool found = false;
        for (Option const& option : options)
            if (!option.grind && (option.kind == TripKind::TurnIn || option.kind == TripKind::Mount) &&
                !option.here && option.score >= wGrind)
            {
                chosen = option;
                via.clear();
                found = true;
                break;
            }
        if (!found)
            return;
    }

    std::string const pick = chosen.Label();
    bool logPick = false;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        QuestBotState& st = gStates[low];
        st.top = topLines;
        if (st.lastPick != pick || now - st.lastPickLogSec >= PICK_LOG_SECONDS)
        {
            st.lastPick = pick;
            st.lastPickLogSec = now;
            logPick = true;
        }
        st.grindAllowed = chosen.grind;
    }
    if (logPick)
        LOG_INFO("playerbots", "Companion {}: quest pick {} score={} top={}{}", bot->GetName(), pick,
                 Num(chosen.score), top, via.empty() ? "" : " via=" + via);

    if (chosen.grind)
    {
        Block(low, "grind wins: no turn-in, workable quest or hub scores above " +
                                                      Num(chosen.score));
        return;
    }
    if (chosen.here)
    {
        Quest const* quest = sObjectMgr->GetQuestTemplate(chosen.questId);
        if (quest)
        {
            botAI->rpgInfo.ChangeToDoQuest(chosen.questId, quest);
            NoteWorkHere(low, chosen.questId);
        }
        Block(low, "working quest " + std::to_string(chosen.questId) + " here");
        return;
    }

    StartTrip(botAI, chosen, via, false);
}

std::vector<std::string> CompanionQuest::WhyLines(uint32 lowGuid)
{
    std::vector<std::string> lines;
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gStates.find(lowGuid);
    if (it == gStates.end())
    {
        lines.push_back("Quest project: not evaluated yet.");
        return lines;
    }
    QuestBotState const& st = it->second;
    uint32 const now = NowSec();
    auto ago = [now](uint32 sec)
    { return sec && now >= sec ? std::to_string(now - sec) + "s ago" : std::string("never"); };
    size_t ownerCount = 0;
    {
        std::lock_guard<std::mutex> ownerLock(gOwnerMutex);
        ownerCount = gOwnerQuests.size();
    }
    lines.push_back(std::string("Quest project: ") + (Enabled() ? "on" : "off") + ", log " +
                    std::to_string(st.logUsed) + "/" + std::to_string(MAX_QUEST_LOG_SIZE) + ", keeps " +
                    std::to_string(gFreeSlots.load()) + " free, owner quests " +
                    (gOwnerQuestsKnown.load() ? std::to_string(ownerCount) : std::string("unknown")) +
                    ", last full evaluation " + ago(st.lastFullEvalSec));
    if (st.trip.active)
    {
        std::string plan;
        for (uint32 questId : st.trip.hubPlan)
            plan += (plan.empty() ? "" : ",") + std::to_string(questId);
        lines.push_back(std::string("Quest trip: ") + KindName(st.trip.kind) + " id " + std::to_string(st.trip.questId) +
                        " to zone " + std::to_string(st.trip.zone) + (st.trip.flight ? " (flight)" : "") +
                        ", score " + Num(st.trip.score) + ", started " + ago(st.trip.startSec) +
                        (plan.empty() ? "" : ", plan " + plan));
    }
    lines.push_back("Quest pick: " + (st.lastPick.empty() ? std::string("none") : st.lastPick) +
                    ", turn-ins this session " + std::to_string(st.turnIns));
    if (!st.top.empty())
        lines.push_back("Top options:");
    for (std::string const& line : st.top)
        lines.push_back("  " + line);
    if (!st.completeLines.empty())
        lines.push_back("Complete quests (turn-in score or block):");
    for (std::string const& line : st.completeLines)
        lines.push_back("  " + line);
    if (!st.blockedNow.empty())
        lines.push_back("Blocked:");
    for (std::string const& line : st.blockedNow)
        lines.push_back("  " + line);
    if (!st.lastBlock.empty())
        lines.push_back("Quest project waits: " + st.lastBlock + " (" + ago(st.lastBlockSec) + ")");
    if (!st.mountStatus.empty())
        lines.push_back("Mount: " + st.mountStatus);
    return lines;
}
