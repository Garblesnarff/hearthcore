/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "CompanionTrip.h"
#include "CompanionTransport.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <iomanip>
#include <limits>
#include <mutex>
#include <sstream>
#include <unordered_map>

#include "CompanionCraft.h"
#include "CompanionDuty.h"
#include "CompanionLease.h"
#include "CompanionQuest.h"
#include "Config.h"
#include "DBCStores.h"
#include "DatabaseEnv.h"
#include "GameObject.h"
#include "GameTime.h"
#include "Group.h"
#include "Item.h"
#include "ItemTemplate.h"
#include "Log.h"
#include "Map.h"
#include "MapMgr.h"
#include "ObjectMgr.h"
#include "Opcodes.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "Random.h"
#include "Spell.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include "Transport.h"
#include "TransportMgr.h"
#include "WorldPacket.h"
#include "WorldSession.h"

namespace
{
// ---- landmasses ----
enum Land : uint32
{
    LAND_NONE = 0,
    LAND_EK = 1,
    LAND_KALIMDOR = 2,
    LAND_TELDRASSIL = 3,
    LAND_RUTTHERAN = 4,
    LAND_FEATHERMOON = 5,
    LAND_AZUREMYST = 6,
    LAND_OUTLAND = 7,
    LAND_NORTHREND = 8,
    LAND_DALARAN = 9,
    LAND_TRAM = 10,
    LAND_OTHER_BASE = 1000
};

// Zone and area ids proved with AreaTable.dbc (tools, 2026-11-20): Teldrassil 141, Darnassus 1657 (Rut'theran
// Village 702 is a sub-area of 141), Feralas 357 (Feathermoon Stronghold 1116 and Sardor Isle 1120 are its sub-areas),
// Azuremyst Isle 3524, Bloodmyst Isle 3525, The Exodar 3557, Dalaran 4395, Deeprun Tram map 369.
constexpr uint32 ZONE_TELDRASSIL = 141;
constexpr uint32 ZONE_DARNASSUS = 1657;
constexpr uint32 ZONE_FERALAS = 357;
constexpr uint32 AREA_FEATHERMOON = 1116;
constexpr uint32 AREA_SARDOR_ISLE = 1120;
constexpr uint32 ZONE_AZUREMYST = 3524;
constexpr uint32 ZONE_BLOODMYST = 3525;
constexpr uint32 ZONE_EXODAR = 3557;
constexpr uint32 ZONE_DALARAN = 4395;
constexpr uint32 MAP_TRAM = 369;
constexpr float TELDRASSIL_TOP_Z = 300.0f;   // Rut'theran is at sea level, the top of Teldrassil above 1000

// Silver Enclave portals (Dalaran, map 571), from the task text and checked in the world DB gameobject /
// gameobject_template (type 22 spellcaster, faction 1732): Stormwind, Ironforge, Darnassus, Exodar.
constexpr uint32 DALARAN_PORTALS[] = {190960, 191008, 191006, 191007};
constexpr uint32 ITEM_HEARTHSTONE = 6948;
constexpr uint32 HEARTH_LEG = std::numeric_limits<uint32>::max();

constexpr float WALK_SPEED = 7.0f;
constexpr float MOUNT_FACTOR = 1.6f;
constexpr float PATH_FACTOR = 1.25f;
constexpr uint32 MAX_WAIT_SECONDS = 180;
constexpr float DOCK_REACH_YARDS = 6.0f;
constexpr float ANCHOR_MAX_YARDS = 120.0f;
constexpr float HEARTH_SECONDS = 20.0f;   // 10 s cast + loading
constexpr float LEG_PROGRESS = 1000000.0f;

enum class LegKind : uint8
{
    Boat,
    Tram,
    Portal,    // same-map area trigger between two landmasses (Rut'theran <-> Darnassus)
    Dalaran,   // Silver Enclave portal object
    Hearth
};

char const* KindName(LegKind kind)
{
    switch (kind)
    {
        case LegKind::Boat: return "boat";
        case LegKind::Tram: return "tram";
        case LegKind::Portal: return "portal";
        case LegKind::Dalaran: return "dalaran";
        case LegKind::Hearth: return "hearth";
    }
    return "?";
}

struct Point
{
    uint32 map = 0;
    float x = 0.0f, y = 0.0f, z = 0.0f;
    uint32 zone = 0;
    uint32 land = LAND_NONE;
    std::string name;
};

struct Leg
{
    LegKind kind = LegKind::Boat;
    uint32 id = 0;            // boat entry, entrance trigger, portal trigger or portal object entry
    Point from, to;
    uint8 team = 0;           // 0 both, 1 alliance, 2 horde
    float rideSec = 0.0f;
    float expectWait = 0.0f;
    // boat schedule (ms in the transport period); period 0 = unknown
    uint32 period = 0;
    uint32 arriveMs = 0;      // the boat reaches the departure dock
    uint32 departMs = 0;      // and leaves it
    // tram
    uint32 exitTrigger = 0;
    Point tramIn, tramOut;    // map 369: platform after the entrance, exit trigger at the far platform
};

// ---- config ----
std::atomic<bool> gEnabled{true};
std::atomic<bool> gHearthEnabled{true};
std::atomic<bool> gHearthSameContinent{true};
std::atomic<float> gHearthMinSaving{180.0f};
std::atomic<float> gHearthNeedReturn{1500.0f};
std::atomic<bool> gConfigRead{false};
uint32 gConfigTimer = 0;

// ---- tables (world thread builds once, then publishes gReady; read-only afterwards) ----
std::atomic<bool> gReady{false};
std::vector<Leg> gLegs;
// Feathermoon: the two stand points of the ferry; a Feralas point nearer the island one is on the island.
bool gFeatherKnown = false;
float gFeatherIslandX = 0.0f, gFeatherIslandY = 0.0f, gFeatherCoastX = 0.0f, gFeatherCoastY = 0.0f;
float gRutX = 0.0f, gRutY = 0.0f;   // Rut'theran portal trigger, for points without a height
bool gRutKnown = false;
std::vector<std::string> gTableLines;

// ---- boat schedule samples (world thread writes, map threads read) ----
struct Sample
{
    uint32 progress = 0;   // path progress % period at sampleMs
    uint64 sampleMs = 0;
    uint32 period = 0;
};
std::mutex gSampleMutex;
std::unordered_map<uint32, Sample> gSamples;
uint32 gSampleTimer = 0;

// ---- per-bot leg state ----
enum class Phase : uint8
{
    ToDeparture,
    Waiting,
    Casting,
    InTram,
    TramExit,
    Arriving
};

struct Run
{
    std::string owner;
    uint32 leaseStart = 0;
    uint32 map = 0;
    float x = 0.0f, y = 0.0f, z = 0.0f;
    bool planned = false;
    bool failed = false;
    bool noHearth = false;
    std::vector<uint32> legs;
    size_t idx = 0;
    Phase phase = Phase::ToDeparture;
    uint64 phaseStartMs = 0;
    uint64 waitUntilMs = 0;
    uint64 nearSinceMs = 0;
    uint64 combatWaitSinceMs = 0;
    uint64 triggerWalkSinceMs = 0;
    bool departLogged = false;   // one depart line per leg (a fight may pull the bot off the dock and back)
    uint32 waitSec = 0;
    uint32 fails = 0;
    uint32 replans = 0;
    uint32 startSec = 0;
    Point home;   // hearth leg arrival (the home bind)
    std::string planText;
    std::string hearthRule;   // the rule of the hearth decision of this plan (reported with the cast)
};
std::mutex gRunMutex;
std::unordered_map<uint32, Run> gRuns;
std::unordered_map<uint32, CompanionTransport::ForceRequest> gForce;

uint64 NowMs()
{
    return static_cast<uint64>(GameTime::GetGameTimeMS().count());
}

uint32 NowSec()
{
    return static_cast<uint32>(GameTime::GetGameTime().count());
}

float Dist2d(float ax, float ay, float bx, float by)
{
    float dx = ax - bx;
    float dy = ay - by;
    return std::sqrt(dx * dx + dy * dy);
}

std::string Num(float value, int precision = 0)
{
    std::ostringstream out;
    out << std::fixed << std::setprecision(precision) << value;
    return out.str();
}

// Log values must not contain spaces (the test splits key=value).
std::string Token(std::string text)
{
    for (char& c : text)
        if (c == ' ' || c == '=' || c == ',')
            c = '_';
    return text.empty() ? std::string("?") : text;
}

std::string AreaName(uint32 areaId)
{
    AreaTableEntry const* area = sAreaTableStore.LookupEntry(areaId);
    if (!area || !area->area_name[0])
        return "area" + std::to_string(areaId);
    return Token(area->area_name[0]);
}

void ReadConfig()
{
    gEnabled.store(sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.Transport.Enabled", true));
    gHearthEnabled.store(sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.Hearth.Enabled", true));
    gHearthSameContinent.store(sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.Hearth.SameContinent", true));
    float saving = sConfigMgr->GetOption<float>("AiPlayerbot.Companion.Hearth.MinSavingSeconds", 180.0f);
    float needReturn = sConfigMgr->GetOption<float>("AiPlayerbot.Companion.Hearth.NeedReturnYards", 1500.0f);
    // At least 180 s: the trip planner only looks at legs for walks over 180 s, and Step skips the route search
    // below that; a smaller saving could make the two disagree.
    gHearthMinSaving.store(std::isfinite(saving) ? std::max(180.0f, saving) : 180.0f);
    gHearthNeedReturn.store(std::isfinite(needReturn) && needReturn >= 0.0f ? needReturn : 1500.0f);
    gConfigRead.store(true);
}

// World thread only (creates grids on the base map, like CompanionQuest's ZoneAt).
void ZoneArea(uint32 mapId, float x, float y, float z, uint32& zone, uint32& area)
{
    zone = area = 0;
    if (Map* map = sMapMgr->FindBaseMap(mapId))
        map->GetZoneAndAreaId(PHASEMASK_NORMAL, zone, area, x, y, z);
}

Point MakePoint(uint32 mapId, float x, float y, float z)
{
    Point p;
    p.map = mapId;
    p.x = x;
    p.y = y;
    p.z = z;
    uint32 area = 0;
    ZoneArea(mapId, x, y, z, p.zone, area);
    p.name = AreaName(area ? area : p.zone);
    return p;
}

// Standing point for a boat stop (the stop is the ship's position on the water): the nearest creature or object
// spawn above the water within ANCHOR_MAX_YARDS. False when there is none.
bool FindAnchor(uint32 mapId, float sx, float sy, float sz, float& ox, float& oy, float& oz)
{
    float best = ANCHOR_MAX_YARDS;
    bool found = false;
    auto consider = [&](float x, float y, float z)
    {
        if (z < sz + 1.0f || z > sz + 40.0f)
            return;
        float d = Dist2d(sx, sy, x, y);
        if (d < best)
        {
            best = d;
            ox = x;
            oy = y;
            oz = z;
            found = true;
        }
    };
    for (auto const& [spawnId, data] : sObjectMgr->GetAllCreatureData())
        if (data.mapid == mapId && std::fabs(data.posX - sx) < ANCHOR_MAX_YARDS &&
            std::fabs(data.posY - sy) < ANCHOR_MAX_YARDS)
            consider(data.posX, data.posY, data.posZ);
    for (auto const& [spawnId, data] : sObjectMgr->GetAllGOData())
    {
        if (data.mapid != mapId || std::fabs(data.posX - sx) >= ANCHOR_MAX_YARDS ||
            std::fabs(data.posY - sy) >= ANCHOR_MAX_YARDS)
            continue;
        GameObjectTemplate const* goInfo = sObjectMgr->GetGameObjectTemplate(data.id);
        if (!goInfo || goInfo->type == GAMEOBJECT_TYPE_TRANSPORT || goInfo->type == GAMEOBJECT_TYPE_MO_TRANSPORT ||
            goInfo->type == GAMEOBJECT_TYPE_FISHINGHOLE)
            continue;
        consider(data.posX, data.posY, data.posZ);
    }
    return found;
}

bool CompanionMap(uint32 mapId)
{
    return mapId == 0 || mapId == 1 || mapId == 530 || mapId == 571;
}

void LoadBoats()
{
    QueryResult result = WorldDatabase.Query("SELECT guid, entry, name FROM transports ORDER BY guid");
    if (!result)
    {
        LOG_INFO("playerbots", "Companion transport: no rows in transports");
        return;
    }
    do
    {
        Field* fields = result->Fetch();
        uint32 const entry = fields[1].Get<uint32>();
        std::string const name = fields[2].Get<std::string>();
        GameObjectTemplate const* goInfo = sObjectMgr->GetGameObjectTemplate(entry);
        TransportTemplate const* tInfo = sTransportMgr->GetTransportTemplate(entry);
        if (!goInfo || goInfo->type != GAMEOBJECT_TYPE_MO_TRANSPORT || !tInfo ||
            name.find("Gunship") != std::string::npos)
        {
            LOG_INFO("playerbots", "Companion transport: skip transport entry={} name={} reason={}", entry, Token(name),
                     !goInfo ? "no-template" : (goInfo->type != GAMEOBJECT_TYPE_MO_TRANSPORT ? "not-mo-transport"
                                                : (!tInfo ? "no-transport-template" : "gunship")));
            continue;
        }
        uint32 const pathId = goInfo->moTransport.taxiPathId;
        if (pathId >= sTaxiPathNodesByPath.size() || sTaxiPathNodesByPath[pathId].empty())
            continue;
        uint8 const team = name.find("Horde") != std::string::npos      ? 2
                           : name.find("Alliance") != std::string::npos ? 1
                                                                         : 0;
        std::string ship = name;
        size_t q1 = name.find("(\"");
        size_t q2 = name.find("\")");
        if (q1 != std::string::npos && q2 != std::string::npos && q2 > q1)
            ship = name.substr(q1 + 2, q2 - q1 - 2);

        // TravelNodeMap::generateTransportNodes: the stops are the path nodes with a delay.
        struct Stop
        {
            Point stand;
            float sx = 0.0f, sy = 0.0f;
            uint32 arrive = 0, depart = 0;
            bool timed = false;
        };
        std::vector<Stop> stops;
        bool bad = false;
        for (TaxiPathNodeEntry const* node : sTaxiPathNodesByPath[pathId])
        {
            if (!node || node->delay == 0)
                continue;
            Stop stop;
            stop.sx = node->x;
            stop.sy = node->y;
            float ax = 0.0f, ay = 0.0f, az = 0.0f;
            if (!CompanionMap(node->mapid) || !FindAnchor(node->mapid, node->x, node->y, node->z, ax, ay, az))
            {
                LOG_INFO("playerbots", "Companion transport: skip stop boat={} map={} at={},{} reason={}", entry,
                         node->mapid, Num(node->x), Num(node->y),
                         CompanionMap(node->mapid) ? "no-standing-point" : "map");
                bad = true;
                break;
            }
            stop.stand = MakePoint(node->mapid, ax, ay, az);
            for (KeyFrame const& frame : tInfo->keyFrames)
                if (frame.Node == node)
                {
                    stop.arrive = frame.ArriveTime;
                    stop.depart = frame.DepartureTime;
                    stop.timed = true;
                    break;
                }
            stops.push_back(stop);
        }
        if (bad || stops.size() < 2)
            continue;
        uint32 const period = tInfo->pathTime;
        for (size_t i = 0; i < stops.size(); ++i)
            LOG_INFO("playerbots",
                     "Companion transport: dock boat={} name={} stop={} map={} at={},{} stand={},{},{} area={} "
                     "arrive={} depart={} period={}",
                     entry, Token(ship), i, stops[i].stand.map, Num(stops[i].sx), Num(stops[i].sy),
                     Num(stops[i].stand.x, 1), Num(stops[i].stand.y, 1), Num(stops[i].stand.z, 1), stops[i].stand.name,
                     stops[i].arrive, stops[i].depart, period);
        // One leg from every stop to the next one (and from the last back to the first).
        for (size_t i = 0; i < stops.size(); ++i)
        {
            Stop const& a = stops[i];
            Stop const& b = stops[(i + 1) % stops.size()];
            Leg leg;
            leg.kind = LegKind::Boat;
            leg.id = entry;
            leg.from = a.stand;
            leg.to = b.stand;
            leg.team = team;
            if (a.timed && b.timed && period)
            {
                leg.period = period;
                leg.arriveMs = a.arrive;
                leg.departMs = a.depart;
                leg.rideSec = static_cast<float>((b.arrive + period - a.depart) % period) / 1000.0f;
                leg.expectWait = std::min<float>(MAX_WAIT_SECONDS, period / 2000.0f);
            }
            else
            {
                leg.rideSec = 120.0f;
                leg.expectWait = 105.0f;
            }
            gLegs.push_back(leg);
            gTableLines.push_back("boat " + std::to_string(entry) + " " + ship + ": " + a.stand.name + " -> " +
                                  b.stand.name + " ride " + Num(leg.rideSec) + "s period " +
                                  Num(period / 1000.0f) + "s");
            if (b.stand.zone == ZONE_FERALAS && a.stand.zone == ZONE_FERALAS)
            {
                uint32 zone = 0, area = 0;
                ZoneArea(b.stand.map, b.stand.x, b.stand.y, b.stand.z, zone, area);
                if (area == AREA_FEATHERMOON || area == AREA_SARDOR_ISLE)
                {
                    gFeatherKnown = true;
                    gFeatherIslandX = b.stand.x;
                    gFeatherIslandY = b.stand.y;
                    gFeatherCoastX = a.stand.x;
                    gFeatherCoastY = a.stand.y;
                }
            }
        }
    } while (result->NextRow());
}

void LoadTriggers()
{
    QueryResult result = WorldDatabase.Query("SELECT ID, target_map FROM areatrigger_teleport");
    if (!result)
        return;
    std::vector<uint32> entrances, exits, sameMap;
    do
    {
        Field* fields = result->Fetch();
        uint32 const id = fields[0].Get<uint32>();
        uint32 const targetMap = fields[1].Get<uint32>();
        AreaTrigger const* at = sObjectMgr->GetAreaTrigger(id);
        if (!at)
            continue;
        if (targetMap == MAP_TRAM && at->map == 0)
            entrances.push_back(id);
        else if (at->map == MAP_TRAM && targetMap == 0)
            exits.push_back(id);
        else if (at->map == targetMap && CompanionMap(at->map))
            sameMap.push_back(id);
    } while (result->NextRow());

    // Tram: enter at one city, leave by the exit at the far platform.
    for (uint32 enter : entrances)
    {
        AreaTrigger const* at = sObjectMgr->GetAreaTrigger(enter);
        AreaTriggerTeleport const* tp = sObjectMgr->GetAreaTriggerTeleport(enter);
        uint32 bestExit = 0;
        float bestDist = 0.0f;
        for (uint32 exit : exits)
        {
            AreaTrigger const* ex = sObjectMgr->GetAreaTrigger(exit);
            float d = Dist2d(ex->x, ex->y, tp->target_X, tp->target_Y);
            if (d > bestDist)
            {
                bestDist = d;
                bestExit = exit;
            }
        }
        if (!bestExit || bestDist < 500.0f)
            continue;
        AreaTrigger const* ex = sObjectMgr->GetAreaTrigger(bestExit);
        AreaTriggerTeleport const* exTp = sObjectMgr->GetAreaTriggerTeleport(bestExit);
        if (!exTp)
            continue;
        Leg leg;
        leg.kind = LegKind::Tram;
        leg.id = enter;
        leg.exitTrigger = bestExit;
        leg.from = MakePoint(at->map, at->x, at->y, at->z);
        leg.to = MakePoint(exTp->target_mapId, exTp->target_X, exTp->target_Y, exTp->target_Z);
        leg.tramIn.map = MAP_TRAM;
        leg.tramIn.x = tp->target_X;
        leg.tramIn.y = tp->target_Y;
        leg.tramIn.z = tp->target_Z;
        leg.tramOut.map = MAP_TRAM;
        leg.tramOut.x = ex->x;
        leg.tramOut.y = ex->y;
        leg.tramOut.z = ex->z;
        leg.expectWait = 60.0f;
        leg.rideSec = 15.0f;
        gLegs.push_back(leg);
        gTableLines.push_back("tram " + std::to_string(enter) + "->" + std::to_string(bestExit) + ": " +
                              leg.from.name + " -> " + leg.to.name);
        LOG_INFO("playerbots", "Companion transport: tram enter={} exit={} from={} to={} inside={},{}->{},{}", enter,
                 bestExit, leg.from.name, leg.to.name, Num(leg.tramIn.x), Num(leg.tramIn.y), Num(leg.tramOut.x),
                 Num(leg.tramOut.y));
    }
}

void LoadPortals()
{
    QueryResult result = WorldDatabase.Query("SELECT ID FROM areatrigger_teleport");
    if (!result)
        return;
    do
    {
        uint32 const id = result->Fetch()[0].Get<uint32>();
        AreaTrigger const* at = sObjectMgr->GetAreaTrigger(id);
        AreaTriggerTeleport const* tp = sObjectMgr->GetAreaTriggerTeleport(id);
        if (!at || !tp || at->map != tp->target_mapId || !CompanionMap(at->map))
            continue;
        if (Dist2d(at->x, at->y, tp->target_X, tp->target_Y) < 300.0f)
            continue;
        Point from = MakePoint(at->map, at->x, at->y, at->z);
        Point to = MakePoint(tp->target_mapId, tp->target_X, tp->target_Y, tp->target_Z);
        from.land = CompanionTransport::Landmass(from.map, from.zone, from.x, from.y, from.z);
        to.land = CompanionTransport::Landmass(to.map, to.zone, to.x, to.y, to.z);
        if (from.land == to.land)
            continue;
        Leg leg;
        leg.kind = LegKind::Portal;
        leg.id = id;
        leg.from = from;
        leg.to = to;
        leg.expectWait = 3.0f;
        gLegs.push_back(leg);
        gTableLines.push_back("portal trigger " + std::to_string(id) + ": " + from.name + " -> " + to.name);
        LOG_INFO("playerbots", "Companion transport: portal trigger={} from={} to={} land={}->{}", id, from.name,
                 to.name, from.land, to.land);
    } while (result->NextRow());
}

// Destination of a spell: spell_target_position of the spell or of the spell it force-casts / triggers.
bool SpellDestination(uint32 spellId, uint32& map, float& x, float& y, float& z)
{
    for (int depth = 0; depth < 3 && spellId; ++depth)
    {
        if (SpellTargetPosition const* pos = sSpellMgr->GetSpellTargetPosition(spellId, EFFECT_0))
        {
            map = pos->target_mapId;
            x = pos->target_X;
            y = pos->target_Y;
            z = pos->target_Z;
            return true;
        }
        SpellInfo const* info = sSpellMgr->GetSpellInfo(spellId);
        if (!info)
            return false;
        uint32 next = 0;
        for (uint8 i = 0; i < MAX_SPELL_EFFECTS && !next; ++i)
            if (info->Effects[i].Effect == SPELL_EFFECT_FORCE_CAST ||
                info->Effects[i].Effect == SPELL_EFFECT_TRIGGER_SPELL)
                next = info->Effects[i].TriggerSpell;
        spellId = next;
    }
    return false;
}

void LoadDalaran()
{
    for (uint32 entry : DALARAN_PORTALS)
    {
        GameObjectTemplate const* goInfo = sObjectMgr->GetGameObjectTemplate(entry);
        if (!goInfo || goInfo->type != GAMEOBJECT_TYPE_SPELLCASTER)
        {
            LOG_INFO("playerbots", "Companion transport: dalaran portal={} skipped reason=template", entry);
            continue;
        }
        GameObjectData const* spawn = nullptr;
        for (auto const& [spawnId, data] : sObjectMgr->GetAllGOData())
            if (data.id == entry && data.mapid == 571)
            {
                spawn = &data;
                break;
            }
        uint32 map = 0;
        float x = 0.0f, y = 0.0f, z = 0.0f;
        if (!spawn || !SpellDestination(goInfo->spellcaster.spellId, map, x, y, z) || !CompanionMap(map))
        {
            LOG_INFO("playerbots", "Companion transport: dalaran portal={} skipped reason={}", entry,
                     spawn ? "no-destination" : "no-spawn");
            continue;
        }
        Leg leg;
        leg.kind = LegKind::Dalaran;
        leg.id = entry;
        leg.team = 1;   // Silver Enclave
        leg.from = MakePoint(571, spawn->posX, spawn->posY, spawn->posZ);
        leg.to = MakePoint(map, x, y, z);
        leg.expectWait = 3.0f;
        gLegs.push_back(leg);
        gTableLines.push_back("dalaran portal " + std::to_string(entry) + ": " + leg.from.name + " -> " +
                              leg.to.name);
        LOG_INFO("playerbots", "Companion transport: dalaran portal={} spell={} at={},{},{} to={}:{},{} area={}", entry,
                 goInfo->spellcaster.spellId, Num(spawn->posX), Num(spawn->posY), Num(spawn->posZ), map, Num(x), Num(y),
                 leg.to.name);
    }
}

void LoadTables()
{
    // Rut'theran: the trigger whose target is Darnassus is in Rut'theran (used for points without a height).
    {
        QueryResult result = WorldDatabase.Query("SELECT ID FROM areatrigger_teleport");
        if (result)
        {
            do
            {
                uint32 const id = result->Fetch()[0].Get<uint32>();
                AreaTrigger const* at = sObjectMgr->GetAreaTrigger(id);
                AreaTriggerTeleport const* tp = sObjectMgr->GetAreaTriggerTeleport(id);
                if (!at || !tp || at->map != 1 || tp->target_mapId != 1 || at->z > TELDRASSIL_TOP_Z ||
                    tp->target_Z < TELDRASSIL_TOP_Z)
                    continue;
                uint32 zone = 0, area = 0;
                ZoneArea(1, tp->target_X, tp->target_Y, tp->target_Z, zone, area);
                if (zone != ZONE_DARNASSUS)
                    continue;
                // Below the tree, teleporting to Darnassus: the Rut'theran portal (trigger 542 in this DB).
                gRutKnown = true;
                gRutX = at->x;
                gRutY = at->y;
            } while (result->NextRow());
        }
    }
    LoadBoats();
    LoadTriggers();
    LoadPortals();
    LoadDalaran();
    for (Leg& leg : gLegs)
    {
        leg.from.land = CompanionTransport::Landmass(leg.from.map, leg.from.zone, leg.from.x, leg.from.y, leg.from.z);
        leg.to.land = CompanionTransport::Landmass(leg.to.map, leg.to.zone, leg.to.x, leg.to.y, leg.to.z);
        if (leg.kind == LegKind::Boat)
            LOG_INFO("playerbots",
                     "Companion transport: dock pair boat={} from={} to={} ride={}s team={} fromLand={} toLand={} "
                     "fromZone={} toZone={}",
                     leg.id, leg.from.name, leg.to.name, Num(leg.rideSec),
                     leg.team == 1 ? "alliance" : (leg.team == 2 ? "horde" : "both"), leg.from.land, leg.to.land,
                     leg.from.zone, leg.to.zone);
    }
    LOG_INFO("playerbots", "Companion transport: tables legs={} feathermoon={} ruttheran={}", gLegs.size(),
             gFeatherKnown ? 1 : 0, gRutKnown ? 1 : 0);
}

void SampleBoats()
{
    uint64 const now = NowMs();
    std::lock_guard<std::mutex> lock(gSampleMutex);
    for (uint32 mapId : {0u, 1u, 530u, 571u})
    {
        Map* map = sMapMgr->FindBaseMap(mapId);
        if (!map)
            continue;
        for (Transport* transport : map->GetAllTransports())
        {
            MotionTransport* motion = transport ? transport->ToMotionTransport() : nullptr;
            if (!motion || !motion->GetPeriod())
                continue;
            Sample& sample = gSamples[motion->GetEntry()];
            sample.period = motion->GetPeriod();
            sample.progress = motion->GetPathProgress() % sample.period;
            sample.sampleMs = now;
        }
    }
}

// Seconds until the boat of leg is at its departure dock (real schedule), else a random wait. At most 180 s.
uint32 BoatWait(Leg const& leg, std::string& sched, uint32& real)
{
    real = 0;
    if (leg.period)
    {
        Sample sample;
        bool have = false;
        {
            std::lock_guard<std::mutex> lock(gSampleMutex);
            auto it = gSamples.find(leg.id);
            if (it != gSamples.end() && it->second.period == leg.period)
            {
                sample = it->second;
                have = true;
            }
        }
        if (have)
        {
            uint64 const now = NowMs();
            uint32 const p = static_cast<uint32>((sample.progress + (now - sample.sampleMs)) % leg.period);
            uint32 waitMs = 0;
            if (p < leg.arriveMs || p >= leg.departMs)
                waitMs = (leg.arriveMs + leg.period - p) % leg.period;
            real = waitMs / 1000;
            sched = "real";
            return std::max<uint32>(3, std::min<uint32>(MAX_WAIT_SECONDS, real));
        }
    }
    sched = "random";
    return urand(30, MAX_WAIT_SECONDS);
}

uint32 HomeZone(Player* bot)
{
    AreaTableEntry const* area = sAreaTableStore.LookupEntry(bot->m_homebindAreaId);
    return area ? (area->zone ? area->zone : area->ID) : 0;
}

bool HomeIsDalaran(Player* bot)
{
    return bot->m_homebindMapId == 571 && HomeZone(bot) == ZONE_DALARAN;
}

// The hearth leg may be planned: the stone is usable now and the home bind is one the route knows (Dalaran, or any
// home with Hearth.SameContinent). The "won't need the hearth soon" rule for a non-Dalaran home is in Route.
bool HearthReady(Player* bot, std::string& why)
{
    if (!gHearthEnabled.load())
        return why = "disabled", false;
    bool const dalaran = HomeIsDalaran(bot);
    if (!dalaran && !gHearthSameContinent.load())
        return why = "home-" + std::to_string(bot->m_homebindMapId) + ":" + std::to_string(HomeZone(bot)), false;
    if (dalaran && bot->GetTeamId() != TEAM_ALLIANCE)
        return why = "team", false;   // the Silver Enclave portals
    if (bot->IsInCombat())
        return why = "combat", false;
    Map* map = bot->GetMap();
    if (!map || map->Instanceable() || map->IsBattlegroundOrArena() || bot->InBattleground())
        return why = "instance", false;
    if (Group* group = bot->GetGroup())
        if (group->isLFGGroup() || group->isBGGroup() || map->IsDungeon())
            return why = "dungeon-group", false;
    Item* item = bot->GetItemByEntry(ITEM_HEARTHSTONE);
    if (!item)
        return why = "no-item", false;
    uint32 const spell = item->GetTemplate()->Spells[0].SpellId;
    if (!spell)
        return why = "no-spell", false;
    if (bot->HasSpellCooldown(spell) || bot->HasSpellItemCooldown(spell, ITEM_HEARTHSTONE))
        return why = "cooldown", false;
    return true;
}

Point HomePoint(Player* bot)
{
    Point p;
    p.map = bot->m_homebindMapId;
    p.x = bot->m_homebindX;
    p.y = bot->m_homebindY;
    p.z = bot->m_homebindZ;
    p.zone = HomeZone(bot);
    p.land = CompanionTransport::Landmass(p.map, p.zone, p.x, p.y, p.z);
    p.name = p.zone == ZONE_DALARAN ? std::string("Dalaran") : AreaName(p.zone);
    return p;
}

// GM test needs (.companion hearth <name> need ...): lowGuid -> need and the second it expires.
std::mutex gNeedMutex;
std::unordered_map<uint32, std::pair<std::string, uint32>> gTestNeeds;

// The hearthstone cooldown in seconds, read from the stone's spell at run time (never a constant).
float HearthCooldown()
{
    ItemTemplate const* proto = sObjectMgr->GetItemTemplate(ITEM_HEARTHSTONE);
    if (!proto)
        return 0.0f;
    uint32 const spell = proto->Spells[0].SpellId;
    SpellInfo const* info = spell ? sSpellMgr->GetSpellInfo(spell) : nullptr;
    uint32 ms = info ? std::max(info->RecoveryTime, info->CategoryRecoveryTime) : 0;
    if (!ms && proto->Spells[0].SpellCooldown > 0)
        ms = static_cast<uint32>(proto->Spells[0].SpellCooldown);
    return ms / 1000.0f;
}

// The "need" that keeps the stone for a later trip (the rule after saving-low). Empty = no need, the hearth may be
// used. Map thread. A destination near the bind is no need: the bot ends near its inn. A far destination (another
// map, or more than Hearth.NeedReturnYards from the bind) needs the stone to get back: need-craft when a craft
// session is planned in the home city (CompanionCraft run, read only), else need-return. A running hearth (bind)
// duty is need-inn. GM test needs (craft, inn, return) apply first, to any destination.
std::string HearthNeed(Player* bot, Point const& target, Point const& home)
{
    uint32 const low = bot->GetGUID().GetCounter();
    std::string const forced = CompanionTransport::TestNeed(low);
    if (forced == "craft")
        return "need-craft";
    if (forced == "inn")
        return "need-inn";
    if (forced == "return")
        return "need-return";
    if (CompanionDuty::ActiveName(low) == "hearth")
        return "need-inn";
    bool const far = target.map != home.map || Dist2d(home.x, home.y, target.x, target.y) > gHearthNeedReturn.load();
    if (!far)
        return std::string();
    CompanionCraft::Run craft;
    if (CompanionCraft::GetRun(low, "craft", craft) && craft.session && craft.city.valid &&
        craft.city.mapId == home.map && Dist2d(craft.city.x, craft.city.y, home.x, home.y) <= craft.city.radius)
        return "need-craft";
    return "need-return";
}

struct LegView
{
    uint32 index = 0;   // gLegs index or HEARTH_LEG
    Point const* from = nullptr;
    Point const* to = nullptr;
    float cost = 0.0f;
};

// Dijkstra from start to target over the legs (walking only within a landmass).
CompanionTransport::Plan Search(Point const& start, Point const& target, std::vector<LegView> const& legs, float speed)
{
    CompanionTransport::Plan plan;
    // Nodes: 0 start, 1 target, 2 + 2k departure of leg k, 3 + 2k arrival of leg k.
    size_t const n = 2 + 2 * legs.size();
    auto node = [&](size_t i) -> Point const&
    {
        if (i == 0)
            return start;
        if (i == 1)
            return target;
        LegView const& view = legs[(i - 2) / 2];
        return (i % 2 == 0) ? *view.from : *view.to;
    };
    auto walk = [&](Point const& a, Point const& b) -> float
    {
        if (a.map != b.map || a.land != b.land)
            return -1.0f;
        return Dist2d(a.x, a.y, b.x, b.y) * PATH_FACTOR / speed;
    };
    std::vector<float> dist(n, std::numeric_limits<float>::max());
    std::vector<int> prev(n, -1);
    std::vector<bool> done(n, false);
    dist[0] = 0.0f;
    for (size_t round = 0; round < n; ++round)
    {
        size_t u = n;
        for (size_t i = 0; i < n; ++i)
            if (!done[i] && dist[i] < std::numeric_limits<float>::max() && (u == n || dist[i] < dist[u]))
                u = i;
        if (u == n || u == 1)
            break;
        done[u] = true;
        auto relax = [&](size_t v, float cost)
        {
            if (cost < 0.0f || done[v])
                return;
            if (dist[u] + cost < dist[v])
            {
                dist[v] = dist[u] + cost;
                prev[v] = static_cast<int>(u);
            }
        };
        if (u >= 2 && u % 2 == 0)
        {
            // a departure: only its own crossing
            relax(u + 1, legs[(u - 2) / 2].cost);
            continue;
        }
        Point const& here = node(u);
        relax(1, walk(here, target));
        for (size_t k = 0; k < legs.size(); ++k)
        {
            if (legs[k].index == HEARTH_LEG && u != 0)
                continue;   // the hearthstone is used where the bot stands
            relax(2 + 2 * k, legs[k].index == HEARTH_LEG ? 0.0f : walk(here, *legs[k].from));
        }
    }
    if (dist[1] == std::numeric_limits<float>::max())
        return plan;
    plan.ok = true;
    plan.seconds = dist[1];
    std::vector<uint32> order;
    for (int at = 1; at > 0; at = prev[at])
        if (at >= 2 && at % 2 == 1)
            order.push_back(legs[(at - 2) / 2].index);
    std::reverse(order.begin(), order.end());
    plan.legs = order;
    return plan;
}

bool UsesHearth(CompanionTransport::Plan const& plan)
{
    return std::find(plan.legs.begin(), plan.legs.end(), HEARTH_LEG) != plan.legs.end();
}

CompanionTransport::Plan Route(Player* bot, Point const& target, float speed, bool allowHearth, Point& home)
{
    Point start;
    start.map = bot->GetMapId();
    start.x = bot->GetPositionX();
    start.y = bot->GetPositionY();
    start.z = bot->GetPositionZ();
    start.land = CompanionTransport::BotLandmass(bot);

    uint8 const team = bot->GetTeamId() == TEAM_ALLIANCE ? 1 : 2;
    std::vector<LegView> legs;
    for (uint32 i = 0; i < gLegs.size(); ++i)
    {
        Leg const& leg = gLegs[i];
        if (leg.team && leg.team != team)
            continue;
        LegView view;
        view.index = i;
        view.from = &leg.from;
        view.to = &leg.to;
        view.cost = leg.expectWait + leg.rideSec;
        legs.push_back(view);
    }
    std::string why;
    if (!allowHearth || !HearthReady(bot, why))
        return Search(start, target, legs, speed);

    home = HomePoint(bot);
    std::vector<LegView> withHearth = legs;
    LegView hearth;
    hearth.index = HEARTH_LEG;
    hearth.from = &start;
    hearth.to = &home;
    hearth.cost = HEARTH_SECONDS;
    withHearth.push_back(hearth);
    CompanionTransport::Plan plan = Search(start, target, withHearth, speed);
    if (!plan.ok || !UsesHearth(plan))
        return plan;

    // The hearth route is the cheapest: the rules decide whether the stone is spent on it (Route comment).
    CompanionTransport::Plan without = Search(start, target, legs, speed);
    plan.hearthChecked = true;
    plan.hearthSeconds = plan.seconds;
    plan.noHearthSeconds = without.ok ? without.seconds : -1.0f;
    plan.homeDist = target.map == home.map ? Dist2d(home.x, home.y, target.x, target.y) : -1.0f;
    plan.hearthCooldown = HearthCooldown();
    bool keep = true;
    std::string need;
    if (HomeIsDalaran(bot))
        plan.hearthRule = "dalaran";   // the B13 Silver Enclave route, unchanged
    else if (!without.ok)
        plan.hearthRule = "only-route";   // no way there without the stone
    else if (without.seconds - plan.seconds < gHearthMinSaving.load())
    {
        plan.hearthRule = "saving-low";
        keep = false;
    }
    else if (!(need = HearthNeed(bot, target, home)).empty())
    {
        plan.hearthRule = need;
        keep = false;
    }
    else
        plan.hearthRule = "ok";
    plan.hearthUsed = keep;
    if (keep)
        return plan;
    without.hearthChecked = true;
    without.hearthUsed = false;
    without.hearthSeconds = plan.seconds;
    without.noHearthSeconds = without.seconds;
    without.homeDist = plan.homeDist;
    without.hearthCooldown = plan.hearthCooldown;
    without.hearthRule = plan.hearthRule;
    return without;
}

Leg const* LegAt(uint32 index)
{
    return index < gLegs.size() ? &gLegs[index] : nullptr;
}

void StoreRun(uint32 low, Run const& run)
{
    std::lock_guard<std::mutex> lock(gRunMutex);
    gRuns[low] = run;
}

constexpr uint64 COMBAT_WAIT_MAX_MS = 120000;
constexpr uint64 TRIGGER_WALK_MAX_MS = 15000;

enum class TriggerStep : uint8
{
    Walking,
    Fired,
    Unreachable,
    Invalid,
    Refused
};

bool FireTrigger(Player* bot, uint32 triggerId)
{
    if (!bot || !bot->IsAlive() || bot->IsInCombat() || bot->IsBeingTeleported())
        return false;
    AreaTrigger const* at = sObjectMgr->GetAreaTrigger(triggerId);
    if (!at || !bot->IsInAreaTriggerRadius(at))
        return false;
    bool const wasTeleporting = bot->IsBeingTeleported();
    uint32 const mapBefore = bot->GetMapId();
    float const xBefore = bot->GetPositionX();
    float const yBefore = bot->GetPositionY();
    WorldPacket packet(CMSG_AREATRIGGER);
    packet << triggerId;
    packet.rpos(0);
    bot->GetSession()->HandleAreaTriggerOpcode(packet);
    // Confirm the stock handler actually accepted the trigger (started a new teleport or moved the bot) rather than
    // refusing access due to level, quest, or item requirements (issue #80).
    return (!wasTeleporting && bot->IsBeingTeleported()) || bot->GetMapId() != mapBefore ||
           Dist2d(bot->GetPositionX(), bot->GetPositionY(), xBefore, yBefore) > 10.0f;
}

// Walk the companion onto an area trigger before firing the stock handler (issue #80). Never teleports on refusal.
TriggerStep WalkAndFireTrigger(Player* bot, uint32 triggerId, uint64 nowMs, uint64& walkSinceMs)
{
    if (!bot || bot->IsBeingTeleported())
        return TriggerStep::Walking;
    AreaTrigger const* at = sObjectMgr->GetAreaTrigger(triggerId);
    if (!at || at->map != bot->GetMapId())
        return TriggerStep::Invalid;
    if (!bot->IsInAreaTriggerRadius(at))
    {
        if (!walkSinceMs)
            walkSinceMs = nowMs;
        if (nowMs - walkSinceMs > TRIGGER_WALK_MAX_MS)
            return TriggerStep::Unreachable;
        if (bot->GetMotionMaster()->GetCurrentMovementGeneratorType() != POINT_MOTION_TYPE)
            bot->GetMotionMaster()->MovePoint(0, at->x, at->y, at->z);
        return TriggerStep::Walking;
    }
    walkSinceMs = 0;
    bot->StopMoving();
    return FireTrigger(bot, triggerId) ? TriggerStep::Fired : TriggerStep::Refused;
}

void Dismount(PlayerbotAI* botAI, Player* bot)
{
    botAI->RemoveShapeshift();
    if (bot->IsMounted())
    {
        CompanionTrip::SelfDismount scope(bot, "transport");
        bot->RemoveAurasByType(SPELL_AURA_MOUNTED);
    }
}

// The only teleports the transport system performs (issue #80). Every one names an owner-approved reason and is
// logged with its leg and positions; the owner-approved stuck teleport lives separately in upstream
// NewRpgBaseAction::MoveFarTo. A stock area trigger or portal that refuses the companion is NOT an approved reason:
// the companion walks onto the trigger and fails the leg if the stock handler does not fire.
enum class TeleportReason : uint8
{
    BoatDock,  // dock-to-dock boat move (owner-approved)
    TramRide,  // platform-to-platform tram move (owner-approved, issue #80)
};

char const* TeleportReasonName(TeleportReason reason)
{
    switch (reason)
    {
        case TeleportReason::BoatDock:
            return "boat-dock";
        case TeleportReason::TramRide:
            return "tram-ride";
    }
    return "unknown";
}

bool Teleport(Player* bot, Point const& p, TeleportReason reason, uint32 legId)
{
    // Safety backstop inside the single Teleport() helper (issue #80): never teleport while dead or in combat.
    if (!bot || !bot->IsAlive() || bot->IsInCombat())
    {
        LOG_WARN("playerbots", "Companion {}: transport teleport refused leg={} reason={} alive={} combat={}",
                 bot ? bot->GetName() : "null", legId, TeleportReasonName(reason),
                 bot && bot->IsAlive() ? 1 : 0, bot && bot->IsInCombat() ? 1 : 0);
        return false;
    }
    LOG_INFO("playerbots",
             "Companion {}: transport teleport leg={} reason={} from={}:{:.1f},{:.1f},{:.1f} "
             "to={}:{:.1f},{:.1f},{:.1f}",
             bot->GetName(), legId, TeleportReasonName(reason), bot->GetMapId(), bot->GetPositionX(),
             bot->GetPositionY(), bot->GetPositionZ(), p.map, p.x, p.y, p.z);
    bot->RemoveAurasWithInterruptFlags(AURA_INTERRUPT_FLAG_TELEPORTED | AURA_INTERRUPT_FLAG_CHANGE_MAP);
    return bot->TeleportTo(p.map, p.x, p.y, p.z, bot->GetOrientation());
}
}  // namespace

// ---------------------------------------------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------------------------------------------
std::string CompanionTransport::Plan::Describe() const
{
    std::string out;
    for (uint32 index : legs)
    {
        if (!out.empty())
            out += ">";
        if (index == HEARTH_LEG)
            out += "hearth:6948";
        else if (Leg const* leg = LegAt(index))
            out += std::string(KindName(leg->kind)) + ":" + std::to_string(leg->id);
    }
    return out.empty() ? std::string("walk") : out;
}

bool CompanionTransport::Enabled()
{
    return gConfigRead.load() && gEnabled.load();
}

bool CompanionTransport::HearthEnabled()
{
    return gConfigRead.load() && gHearthEnabled.load();
}

bool CompanionTransport::HearthSameContinent()
{
    return HearthEnabled() && gHearthSameContinent.load();
}

float CompanionTransport::HearthMinSavingSeconds()
{
    return gHearthMinSaving.load();
}

float CompanionTransport::HearthCooldownSeconds()
{
    return HearthCooldown();
}

void CompanionTransport::SetTestNeed(uint32 lowGuid, std::string const& need, uint32 minutes)
{
    std::lock_guard<std::mutex> lock(gNeedMutex);
    if (need != "craft" && need != "inn" && need != "return")
    {
        gTestNeeds.erase(lowGuid);
        return;
    }
    gTestNeeds[lowGuid] = {need, NowSec() + minutes * 60};
}

std::string CompanionTransport::TestNeed(uint32 lowGuid)
{
    std::lock_guard<std::mutex> lock(gNeedMutex);
    auto it = gTestNeeds.find(lowGuid);
    if (it == gTestNeeds.end())
        return std::string();
    if (NowSec() >= it->second.second)
    {
        gTestNeeds.erase(it);
        return std::string();
    }
    return it->second.first;
}

bool CompanionTransport::Ready()
{
    return gReady.load();
}

void CompanionTransport::UpdateWorld(uint32 elapsed)
{
    gConfigTimer += elapsed;
    if (!gConfigRead.load() || gConfigTimer >= 10000)
    {
        gConfigTimer = 0;
        ReadConfig();
    }
    if (!gReady.load())
    {
        uint32 const startMs = getMSTime();
        LoadTables();
        gReady.store(true);
        LOG_INFO("playerbots", "Companion transport: loaded in {} ms (enabled={} hearth={})",
                 GetMSTimeDiffToNow(startMs), gEnabled.load() ? 1 : 0, gHearthEnabled.load() ? 1 : 0);
        SampleBoats();
        return;
    }
    gSampleTimer += elapsed;
    if (gSampleTimer >= 20000)
    {
        gSampleTimer = 0;
        SampleBoats();
    }
}

uint32 CompanionTransport::Landmass(uint32 mapId, uint32 zone, float x, float y, float z)
{
    switch (mapId)
    {
        case 0:
            return LAND_EK;
        case MAP_TRAM:
            return LAND_TRAM;
        case 1:
        {
            bool teldrassil = zone == ZONE_TELDRASSIL || zone == ZONE_DARNASSUS;
            if (!teldrassil && gRutKnown && Dist2d(x, y, gRutX, gRutY) < 2500.0f && x > gRutX - 400.0f)
                teldrassil = true;   // the Teldrassil island area (north of Darkshore), whatever the terrain zone
            if (teldrassil)
            {
                bool const heightKnown = z != 0.0f;
                if (heightKnown ? z < TELDRASSIL_TOP_Z : (gRutKnown && Dist2d(x, y, gRutX, gRutY) < 250.0f))
                    return LAND_RUTTHERAN;
                return LAND_TELDRASSIL;
            }
            if (zone == ZONE_FERALAS && gFeatherKnown &&
                Dist2d(x, y, gFeatherIslandX, gFeatherIslandY) < Dist2d(x, y, gFeatherCoastX, gFeatherCoastY))
                return LAND_FEATHERMOON;
            return LAND_KALIMDOR;
        }
        case 530:
            if (zone == ZONE_AZUREMYST || zone == ZONE_BLOODMYST || zone == ZONE_EXODAR || (!zone && y < -10000.0f))
                return LAND_AZUREMYST;
            return LAND_OUTLAND;
        case 571:
            if (zone == ZONE_DALARAN)
                return LAND_DALARAN;
            return LAND_NORTHREND;
        default:
            return LAND_OTHER_BASE + mapId;
    }
}

uint32 CompanionTransport::BotLandmass(Player* bot)
{
    return Landmass(bot->GetMapId(), bot->GetZoneId(), bot->GetPositionX(), bot->GetPositionY(),
                    bot->GetPositionZ());
}

CompanionTransport::Plan CompanionTransport::PlanRoute(Player* bot, uint32 mapId, float x, float y, float z,
                                                       uint32 zone, float speed, bool allowHearth)
{
    if (!bot || !Ready() || !Enabled())
        return Plan();
    Point target;
    target.map = mapId;
    target.x = x;
    target.y = y;
    target.z = z;
    target.zone = zone;
    target.land = Landmass(mapId, zone, x, y, z);
    Point home;
    return Route(bot, target, speed > 0.0f ? speed : WALK_SPEED, allowHearth, home);
}

bool CompanionTransport::NeedsLegs(Player* bot, uint32 mapId, float x, float y, float z, uint32 zone)
{
    return bot && (bot->GetMapId() != mapId || BotLandmass(bot) != Landmass(mapId, zone, x, y, z));
}

void CompanionTransport::Reset(uint32 lowGuid)
{
    std::lock_guard<std::mutex> lock(gRunMutex);
    gRuns.erase(lowGuid);
}

CompanionTransport::Drive CompanionTransport::Step(PlayerbotAI* botAI, std::string const& owner, uint32 mapId,
                                                   float x, float y, float z, uint32 zone, MoveFn const& move,
                                                   bool allowHearth)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot)
        return Drive::Failed;
    if (!Ready() || !Enabled())
        return bot->GetMapId() != mapId ? Drive::Failed : Drive::NotNeeded;
    if (!bot->IsInWorld())
        return Drive::Busy;

    uint32 const low = bot->GetGUID().GetCounter();
    if (bot->IsBeingTeleported())
    {
        CompanionLease::KeepAlive(low);
        return Drive::Busy;
    }
    uint32 const leaseStart = CompanionLease::Current(low).startSec;
    uint64 const nowMs = NowMs();
    Run run;
    bool hadRun = false;
    {
        std::lock_guard<std::mutex> lock(gRunMutex);
        auto it = gRuns.find(low);
        if (it != gRuns.end())
        {
            run = it->second;
            hadRun = true;
        }
    }

    // No transport phase advances while dead or in combat (issue #80):
    // - Dead: abandon the leg neutrally (no failure/cooldown) and clear run state so revive logic runs unimpeded.
    // - In combat: suspend the lease and hold phase up to COMBAT_WAIT_MAX_MS before abandoning neutrally.
    if (!bot->IsAlive())
    {
        if (hadRun)
        {
            LOG_INFO("playerbots", "Companion {}: transport abandoned owner={} reason=dead leg={}", bot->GetName(),
                     owner, run.idx);
            Reset(low);
        }
        if (hadRun || CompanionLease::Holds(low, owner))
            CompanionLease::End(low, owner, "transport:dead", CompanionLease::Outcome::Neutral);
        return Drive::Busy;
    }
    if (bot->IsInCombat())
    {
        CompanionLease::RequestInterrupt(bot, CompanionLease::Interrupt::Combat);
        if (hadRun)
        {
            CompanionLease::KeepAlive(low);
            if (!run.combatWaitSinceMs)
                run.combatWaitSinceMs = nowMs;
            if (nowMs - run.combatWaitSinceMs > COMBAT_WAIT_MAX_MS)
            {
                LOG_INFO("playerbots", "Companion {}: transport abandoned owner={} reason=combat-timeout leg={}",
                         bot->GetName(), owner, run.idx);
                Reset(low);
                CompanionLease::End(low, owner, "transport:combat-timeout", CompanionLease::Outcome::Neutral);
                return Drive::Busy;
            }
            StoreRun(low, run);
        }
        return Drive::Busy;
    }

    bool const same = hadRun && run.owner == owner && run.leaseStart == leaseStart && run.map == mapId &&
                      std::fabs(run.x - x) < 1.0f && std::fabs(run.y - y) < 1.0f;
    if (!same)
    {
        run = Run();
        run.owner = owner;
        run.leaseStart = leaseStart;
        run.map = mapId;
        run.x = x;
        run.y = y;
        run.z = z;
        run.noHearth = !allowHearth;
    }
    run.combatWaitSinceMs = 0;
    uint32 const nowSec = NowSec();
    Point target;
    target.map = mapId;
    target.x = x;
    target.y = y;
    target.z = z;
    target.zone = zone;
    target.land = Landmass(mapId, zone, x, y, z);

    if (!run.planned && bot->GetMapId() == mapId && BotLandmass(bot) == target.land &&
        Dist2d(bot->GetPositionX(), bot->GetPositionY(), x, y) * PATH_FACTOR / WALK_SPEED <= 180.0f)
    {
        // Same landmass and a short walk (the trip planner never chooses legs for it): no route search.
        run.planned = true;
        run.legs.clear();   // a re-plan may land here: drop the old legs
        run.idx = 0;
        run.phase = Phase::ToDeparture;
        StoreRun(low, run);
        return Drive::NotNeeded;
    }
    if (!run.planned)
    {
        // Once per trip (owner + lease + target): the cheapest legs, or none (walk).
        float const speed = WALK_SPEED * (CompanionQuest::HasGroundMount(bot) && CompanionQuest::MountEnabled()
                                              ? MOUNT_FACTOR
                                              : 1.0f);
        Point home;
        Plan plan = Route(bot, target, speed, !run.noHearth, home);
        run.planned = true;
        run.home = home;
        run.idx = 0;
        run.phase = Phase::ToDeparture;
        run.fails = 0;
        if (!plan.ok)
        {
            run.failed = true;
            StoreRun(low, run);
            LOG_INFO("playerbots", "Companion {}: transport fail owner={} reason=no-route from={}:{} to={}:{}",
                     bot->GetName(), owner, bot->GetMapId(), BotLandmass(bot), mapId, target.land);
            return Drive::Failed;
        }
        run.legs = plan.legs;
        run.planText = plan.Describe();
        run.hearthRule = plan.hearthChecked ? plan.hearthRule : std::string();
        if (plan.hearthChecked)
        {
            float const saving = plan.noHearthSeconds >= 0.0f ? plan.noHearthSeconds - plan.hearthSeconds : -1.0f;
            LOG_INFO("playerbots",
                     "Companion {}: transport hearth use={} rule={} saving={}s cooldown={}s hearth={}s nohearth={}s "
                     "homeDist={} minSaving={} needReturn={} home={}:{} from={}:{} to={}:{} owner={}",
                     bot->GetName(), plan.hearthUsed ? 1 : 0, plan.hearthRule,
                     saving >= 0.0f ? Num(saving) : std::string("all"), Num(plan.hearthCooldown),
                     Num(plan.hearthSeconds),
                     plan.noHearthSeconds >= 0.0f ? Num(plan.noHearthSeconds) : std::string("none"),
                     plan.homeDist >= 0.0f ? Num(plan.homeDist) : std::string("other-map"),
                     Num(gHearthMinSaving.load()), Num(gHearthNeedReturn.load()), run.home.map, run.home.zone,
                     bot->GetMapId(), bot->GetZoneId(), mapId, zone, owner);
        }
        if (!run.legs.empty())
        {
            run.startSec = nowSec;
            LOG_INFO("playerbots", "Companion {}: transport plan owner={} legs={} est={}s from={}:{} to={}:{}",
                     bot->GetName(), owner, run.planText, Num(plan.seconds), bot->GetMapId(), BotLandmass(bot), mapId,
                     target.land);
        }
    }
    if (run.failed)
    {
        StoreRun(low, run);
        return Drive::Failed;
    }

    auto replan = [&](char const* reason, bool dropHearth) -> Drive
    {
        LOG_INFO("playerbots", "Companion {}: transport replan owner={} reason={} leg={}", bot->GetName(), owner,
                 reason, run.idx);
        if (++run.replans > 3)
        {
            run.failed = true;
            StoreRun(low, run);
            LOG_INFO("playerbots", "Companion {}: transport fail owner={} reason={}", bot->GetName(), owner, reason);
            return Drive::Failed;
        }
        run.planned = false;
        run.legs.clear();
        run.idx = 0;
        run.phase = Phase::ToDeparture;
        run.nearSinceMs = 0;
        run.triggerWalkSinceMs = 0;
        if (dropHearth)
            run.noHearth = true;
        StoreRun(low, run);
        return Drive::Busy;
    };

    auto failLeg = [&](char const* reason) -> Drive
    {
        run.failed = true;
        StoreRun(low, run);
        LOG_INFO("playerbots", "Companion {}: transport fail owner={} reason={} leg={}", bot->GetName(), owner, reason,
                 run.idx);
        return Drive::Failed;
    };

    if (run.idx >= run.legs.size())
    {
        if (bot->GetMapId() != mapId || BotLandmass(bot) != target.land)
        {
            if (run.legs.empty() && bot->GetMapId() == mapId)
            {
                // Planned as a walk but the bot's landmass reads otherwise (zone border): walk on.
                StoreRun(low, run);
                return Drive::NotNeeded;
            }
            return replan("not-on-target-land", false);
        }
        StoreRun(low, run);
        return Drive::NotNeeded;
    }

    uint32 const legsLeft = static_cast<uint32>(run.legs.size() - run.idx);
    uint32 const index = run.legs[run.idx];
    Leg const* leg = LegAt(index);
    bool const hearth = index == HEARTH_LEG;
    if (!hearth && !leg)
        return replan("bad-leg", false);
    LegKind const kind = hearth ? LegKind::Hearth : leg->kind;
    Point const& from = hearth ? run.home : leg->from;
    Point const& to = hearth ? run.home : leg->to;

    auto finishLeg = [&]()
    {
        uint32 const waited = run.phaseStartMs ? static_cast<uint32>((nowMs - run.phaseStartMs) / 1000) : 0;
        if (kind == LegKind::Hearth)
            LOG_INFO("playerbots", "Companion {}: hearth arrive map={} zone={} dur={}s", bot->GetName(),
                     bot->GetMapId(), bot->GetZoneId(), waited);
        else
            LOG_INFO("playerbots",
                     "Companion {}: transport arrive from={} to={} waited={}s kind={} id={} map={} zone={} trip={}s",
                     bot->GetName(), from.name, to.name, run.waitSec, KindName(kind), leg->id, bot->GetMapId(),
                     bot->GetZoneId(), nowSec - run.startSec);
        ++run.idx;
        run.phase = Phase::ToDeparture;
        run.phaseStartMs = 0;
        run.nearSinceMs = 0;
        run.triggerWalkSinceMs = 0;
        run.departLogged = false;
        run.fails = 0;
        if (legsLeft > 1)
            CompanionLease::Progress(low, -(LEG_PROGRESS * (legsLeft - 1)));
        else
            CompanionLease::ResetProgress(low);   // the trip's own walk reports -distance from here on
        StoreRun(low, run);
        return Drive::Busy;
    };

    switch (run.phase)
    {
        case Phase::ToDeparture:
        {
            if (kind == LegKind::Hearth)
            {
                std::string why;
                if (!HearthReady(bot, why))
                {
                    LOG_INFO("playerbots", "Companion {}: hearth skip reason={}", bot->GetName(), why);
                    return replan("hearth-not-ready", true);
                }
                Item* item = bot->GetItemByEntry(ITEM_HEARTHSTONE);
                uint32 const spell = item->GetTemplate()->Spells[0].SpellId;
                bot->StopMoving();
                bot->GetMotionMaster()->Clear();
                Dismount(botAI, bot);
                // Used like a player uses the item (WorldSession::HandleUseItemOpcode ends here).
                SpellCastTargets targets;
                targets.SetUnitTarget(bot);
                bot->CastItemUseSpell(item, targets, 1, 0);
                LOG_INFO("playerbots",
                         "Companion {}: hearth cast item={} spell={} home={}:{} from={}:{} reason={} cooldown={}s",
                         bot->GetName(), ITEM_HEARTHSTONE, spell, run.home.map, run.home.zone, bot->GetMapId(),
                         bot->GetZoneId(), run.hearthRule.empty() ? std::string("route") : run.hearthRule,
                         Num(HearthCooldown()));
                run.phase = Phase::Casting;
                run.phaseStartMs = nowMs;
                CompanionLease::KeepAlive(low);
                StoreRun(low, run);
                return Drive::Busy;
            }
            if (bot->GetMapId() != from.map || BotLandmass(bot) != from.land)
                return replan("off-route", false);
            float const d = Dist2d(bot->GetPositionX(), bot->GetPositionY(), from.x, from.y);
            CompanionLease::Progress(low, -(LEG_PROGRESS * legsLeft + d));
            float const reach = kind == LegKind::Tram || kind == LegKind::Portal ? 3.0f : DOCK_REACH_YARDS;
            if (!run.triggerWalkSinceMs && d > reach)
            {
                run.nearSinceMs = d < 25.0f ? (run.nearSinceMs ? run.nearSinceMs : nowMs) : 0;
                // Near the point for 30 s without reaching it (no path to the last yards): close enough.
                if (!(run.nearSinceMs && nowMs - run.nearSinceMs > 30000))
                {
                    StoreRun(low, run);
                    move(from.map, from.x, from.y, from.z, d);
                    return Drive::Busy;
                }
            }
            if (!run.triggerWalkSinceMs)
            {
                bot->StopMoving();
                Dismount(botAI, bot);
                run.phaseStartMs = nowMs;
            }
            if (kind == LegKind::Boat)
            {
                std::string sched;
                uint32 real = 0;
                run.waitSec = BoatWait(*leg, sched, real);
                run.waitUntilMs = nowMs + run.waitSec * 1000ull;
                run.phase = Phase::Waiting;
                if (!run.departLogged)
                    LOG_INFO("playerbots",
                             "Companion {}: transport depart from={} to={} wait={} kind=boat id={} sched={} real={}",
                             bot->GetName(), from.name, to.name, run.waitSec, leg->id, sched, real);
                else
                    LOG_INFO("playerbots", "Companion {}: transport rewait from={} wait={} kind=boat id={} sched={}",
                             bot->GetName(), from.name, run.waitSec, leg->id, sched);
                run.departLogged = true;
            }
            else if (kind == LegKind::Tram)
            {
                TriggerStep const step = WalkAndFireTrigger(bot, leg->id, nowMs, run.triggerWalkSinceMs);
                if (step == TriggerStep::Walking)
                {
                    CompanionLease::KeepAlive(low);
                    StoreRun(low, run);
                    return Drive::Busy;
                }
                if (step == TriggerStep::Unreachable)
                    return replan("tram-trigger-unreachable", false);
                if (step == TriggerStep::Invalid)
                    return failLeg("tram-trigger-data");
                if (step == TriggerStep::Refused)
                    return failLeg("tram-trigger-refused");
                run.waitSec = urand(30, 90);
                LOG_INFO("playerbots",
                         "Companion {}: transport depart from={} to={} wait={} kind=tram id={} sched=random",
                         bot->GetName(), from.name, to.name, run.waitSec, leg->id);
                run.phase = Phase::InTram;
                run.waitUntilMs = 0;
            }
            else if (kind == LegKind::Portal)
            {
                TriggerStep const step = WalkAndFireTrigger(bot, leg->id, nowMs, run.triggerWalkSinceMs);
                if (step == TriggerStep::Walking)
                {
                    CompanionLease::KeepAlive(low);
                    StoreRun(low, run);
                    return Drive::Busy;
                }
                if (step == TriggerStep::Unreachable)
                    return replan("portal-trigger-unreachable", false);
                if (step == TriggerStep::Invalid)
                    return failLeg("portal-trigger-data");
                if (step == TriggerStep::Refused)
                    return failLeg("portal-trigger-refused");
                run.waitSec = 0;
                LOG_INFO("playerbots",
                         "Companion {}: transport depart from={} to={} wait=0 kind=portal id={} sched=portal",
                         bot->GetName(), from.name, to.name, leg->id);
                run.phase = Phase::Arriving;
            }
            else   // Dalaran portal object
            {
                GameObject* go = bot->FindNearestGameObject(leg->id, 15.0f);
                if (!go)
                {
                    LOG_INFO("playerbots", "Companion {}: hearth portal entry={} to={}:{},{} result=no-object",
                             bot->GetName(), leg->id, to.map, Num(to.x), Num(to.y));
                    return replan("portal-missing", true);
                }
                run.waitSec = 0;
                LOG_INFO("playerbots", "Companion {}: hearth portal entry={} to={}:{},{} area={} result=used",
                         bot->GetName(), leg->id, to.map, Num(to.x), Num(to.y), to.name);
                go->Use(bot);
                run.phase = Phase::Arriving;
            }
            CompanionLease::KeepAlive(low);
            StoreRun(low, run);
            return Drive::Busy;
        }
        case Phase::Waiting:
        {
            CompanionLease::KeepAlive(low);
            if (bot->GetMapId() != from.map ||
                Dist2d(bot->GetPositionX(), bot->GetPositionY(), from.x, from.y) > 30.0f)
            {
                run.phase = Phase::ToDeparture;   // pulled away (a fight): back to the dock, then a new wait
                StoreRun(low, run);
                return Drive::Busy;
            }
            if (nowMs < run.waitUntilMs)
            {
                StoreRun(low, run);
                return Drive::Busy;
            }
            if (!Teleport(bot, to, TeleportReason::BoatDock, leg->id))
            {
                if (++run.fails >= 3)
                    return replan("teleport-failed", false);
                StoreRun(low, run);
                return Drive::Busy;
            }
            run.phase = Phase::Arriving;
            StoreRun(low, run);
            return Drive::Busy;
        }
        case Phase::Casting:
        {
            CompanionLease::KeepAlive(low);
            // Landed at the home bind (the inn, or Dalaran for the Silver Enclave route).
            if (bot->GetMapId() == run.home.map &&
                (Dist2d(bot->GetPositionX(), bot->GetPositionY(), run.home.x, run.home.y) < 60.0f ||
                 (run.home.zone == ZONE_DALARAN && bot->GetZoneId() == ZONE_DALARAN)))
                return finishLeg();
            if (bot->IsNonMeleeSpellCast(false) || nowMs - run.phaseStartMs < 15000)
            {
                StoreRun(low, run);
                return Drive::Busy;
            }
            LOG_INFO("playerbots", "Companion {}: hearth skip reason=cast-failed", bot->GetName());
            return replan("hearth-cast-failed", true);
        }
        case Phase::InTram:
        {
            CompanionLease::KeepAlive(low);
            if (bot->GetMapId() != MAP_TRAM)
            {
                if (nowMs - run.phaseStartMs > 30000)
                    return replan("tram-enter-failed", false);
                StoreRun(low, run);
                return Drive::Busy;
            }
            if (!run.waitUntilMs)
                run.waitUntilMs = nowMs + run.waitSec * 1000ull;
            if (nowMs >= run.waitUntilMs)
            {
                // The ride: platform to platform on map 369, then the exit trigger.
                if (!Teleport(bot, leg->tramOut, TeleportReason::TramRide, leg->id))
                {
                    if (++run.fails >= 3)
                        return replan("tram-teleport-failed", false);
                    StoreRun(low, run);
                    return Drive::Busy;
                }
                run.phase = Phase::TramExit;
                run.nearSinceMs = nowMs;
                run.triggerWalkSinceMs = 0;
            }
            StoreRun(low, run);
            return Drive::Busy;
        }
        case Phase::TramExit:
        {
            CompanionLease::KeepAlive(low);
            if (bot->GetMapId() == MAP_TRAM)
            {
                // Walk onto the stock exit trigger and fire it; never bypass with a fallback teleport (issue #80).
                TriggerStep const step = WalkAndFireTrigger(bot, leg->exitTrigger, nowMs, run.triggerWalkSinceMs);
                if (step == TriggerStep::Invalid)
                    return failLeg("tram-exit-data");
                if (step == TriggerStep::Refused)
                    return failLeg("tram-exit-refused");
                if (step == TriggerStep::Unreachable || nowMs - run.nearSinceMs > 60000)
                    return failLeg("tram-exit-failed");
            }
            run.phase = bot->GetMapId() == MAP_TRAM ? Phase::TramExit : Phase::Arriving;
            StoreRun(low, run);
            return Drive::Busy;
        }
        case Phase::Arriving:
        {
            CompanionLease::KeepAlive(low);
            if (bot->GetMapId() == to.map && Dist2d(bot->GetPositionX(), bot->GetPositionY(), to.x, to.y) < 60.0f)
                return finishLeg();
            if (nowMs - run.phaseStartMs > 60000 + run.waitSec * 1000ull)
                return replan("arrive-timeout", kind == LegKind::Dalaran);
            StoreRun(low, run);
            return Drive::Busy;
        }
    }
    StoreRun(low, run);
    return Drive::Busy;
}

void CompanionTransport::RequestForce(uint32 lowGuid, ForceRequest const& request)
{
    std::lock_guard<std::mutex> lock(gRunMutex);
    gForce[lowGuid] = request;
}

bool CompanionTransport::TakeForce(uint32 lowGuid, ForceRequest& out)
{
    std::lock_guard<std::mutex> lock(gRunMutex);
    auto it = gForce.find(lowGuid);
    if (it == gForce.end())
        return false;
    out = it->second;
    gForce.erase(it);
    return true;
}

std::vector<std::string> CompanionTransport::WhyLines(uint32 lowGuid)
{
    std::vector<std::string> lines;
    std::string const need = TestNeed(lowGuid);
    lines.push_back("Hearth: cooldown " + Num(HearthCooldown()) + "s, test need " + (need.empty() ? "none" : need) +
                    " (.companion hearth <name> need <craft|inn|return|none> [minutes])");
    std::lock_guard<std::mutex> lock(gRunMutex);
    if (gForce.count(lowGuid))
        lines.push_back("Transport: forced trip queued (taken at the next quest evaluation).");
    auto it = gRuns.find(lowGuid);
    if (it == gRuns.end() || it->second.legs.empty())
    {
        lines.push_back(std::string("Transport: ") + (Enabled() ? "no legs running" : "disabled"));
        return lines;
    }
    Run const& run = it->second;
    lines.push_back("Transport: " + run.owner + " legs " + run.planText + " at leg " + std::to_string(run.idx + 1) +
                    "/" + std::to_string(run.legs.size()) + (run.failed ? " (failed)" : ""));
    return lines;
}

std::vector<std::string> CompanionTransport::TableLines()
{
    if (!Ready())
        return {"Transport tables not loaded yet."};
    return gTableLines;
}
