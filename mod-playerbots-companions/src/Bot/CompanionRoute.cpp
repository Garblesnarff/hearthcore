/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "CompanionRoute.h"

#include <algorithm>
#include <cmath>
#include <initializer_list>
#include <list>
#include <mutex>
#include <unordered_map>
#include <vector>

#include "CellImpl.h"
#include "CompanionBotMgr.h"
#include "CompanionLease.h"
#include "CompanionQuest.h"
#include "CompanionTrip.h"
#include "Config.h"
#include "PlayerbotAIConfig.h"
#include "Creature.h"
#include "GameTime.h"
#include "GridNotifiers.h"
#include "GridNotifiersImpl.h"
#include "Log.h"
#include "Map.h"
#include "PathGenerator.h"
#include "Player.h"
#include "Timer.h"

namespace
{
constexpr uint32 CONFIG_REFRESH_MS = 10000;
constexpr float HORIZON_YD = 240.0f;      // route planned for this many yards of the direct path
constexpr float BLOCKER_VISIT_YD = 150.0f; // creature search radius around the bot (farther aggro: later replans)
constexpr float SAMPLE_YD = 10.0f;        // path sampling step
constexpr float ARRIVE_YD = 8.0f;         // detour waypoint reached
constexpr float LEG_END_YD = 6.0f;        // a validated path must end this close to its target
constexpr uint32 REPLAN_SEC = 20;         // a leg is re-planned at least this often
constexpr uint32 MAX_DETOURS_PER_LEG = 3; // detours chosen on one leg
constexpr uint32 MAX_CREATURES = 200;     // creatures considered per plan
constexpr float DETOUR_OFFSETS[2] = {1.5f, 2.5f};   // waypoint distance from the blocker, in aggro radii

struct Switches
{
    bool loaded = false;
    uint32 atMs = 0;
    bool enabled = true;
    bool roadEnabled = true;
    float maxFactor = 1.5f;
};

std::mutex gSwitchMutex;
Switches gSwitches;

void ReadSwitches(bool& enabled, bool& roadEnabled, float& maxFactor)
{
    uint32 const now = getMSTime();
    std::lock_guard<std::mutex> lock(gSwitchMutex);
    if (!gSwitches.loaded || getMSTimeDiff(gSwitches.atMs, now) >= CONFIG_REFRESH_MS)
    {
        gSwitches.enabled = sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.TripAggroRoute.Enabled", true, false);
        gSwitches.roadEnabled = sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.RoadRoute.Enabled", true, false);
        gSwitches.maxFactor =
            sConfigMgr->GetOption<float>("AiPlayerbot.Companion.TripAggroRoute.MaxDetourFactor", 1.5f, false);
        gSwitches.loaded = true;
        gSwitches.atMs = now;
    }
    enabled = gSwitches.enabled;
    roadEnabled = gSwitches.roadEnabled;
    maxFactor = gSwitches.maxFactor;
}

// Lightweight road waypoint graph for key Alliance starter/city travel corridors (Map 0 Elwynn/Dun Morogh,
// Map 1 Teldrassil, Map 530 Azuremyst). Keeps low-level companions on roads instead of cutting straight through
// hostile camps when walking to cities, trainers, or quest hubs.
struct RoadNode
{
    uint32 mapId;
    float x;
    float y;
    float z;
    int neighbors[4];   // -1 terminated
};

// Indices within gRoadNodes
static constexpr RoadNode gRoadNodes[] = {
    // --- Map 0: Elwynn Forest / Stormwind / Westfall / Redridge (0..9) ---
    {0, -8902.6f, -162.6f, 82.0f, {1, -1, -1, -1}},              // 0: Northshire Abbey
    {0, -9044.6f, -46.0f, 88.4f, {0, 2, -1, -1}},                // 1: Northshire Gate (Falkhaan)
    {0, -9250.0f, 15.0f, 65.0f, {1, 3, -1, -1}},                 // 2: Northshire-Goldshire Road Bend
    {0, -9465.5f, 74.0f, 56.8f, {2, 4, 6, 7}},                   // 3: Goldshire Crossroads (Marshal Dughan)
    {0, -9220.0f, 320.0f, 74.0f, {3, 5, -1, -1}},                // 4: Stormwind Road Midpoint
    {0, -8954.0f, 521.0f, 96.5f, {4, -1, -1, -1}},               // 5: Stormwind City Gates (Marcus Jonathan)
    {0, -9662.8f, 694.3f, 36.9f, {3, -1, -1, -1}},               // 6: Westfall Bridge (Deputy Rainer)
    {0, -9610.2f, -1032.1f, 41.3f, {3, 8, -1, -1}},              // 7: Eastvale Road (Guard Thomas)
    {0, -9469.8f, -1289.2f, 41.2f, {7, 9, -1, -1}},              // 8: Eastvale Logging Camp
    {0, -9411.5f, -2263.3f, 67.4f, {8, -1, -1, -1}},             // 9: Redridge Bridge (Guard Ashlock)

    // --- Map 0: Dun Morogh / Ironforge / Loch Modan (10..17) ---
    {0, -6236.7f, 331.1f, 382.9f, {11, -1, -1, -1}},             // 10: Coldridge Valley (Sten Stoutarm)
    {0, -6222.5f, 689.0f, 385.0f, {10, 12, -1, -1}},             // 11: Coldridge Pass (Talin Keeneye)
    {0, -5601.6f, -531.2f, 399.7f, {11, 13, 15, -1}},            // 12: Kharanos Crossroads (Innkeeper Belm)
    {0, -5250.0f, -700.0f, 445.0f, {12, 14, -1, -1}},            // 13: Ironforge Road Ramp
    {0, -5021.6f, -823.5f, 495.9f, {13, -1, -1, -1}},            // 14: Ironforge Gates
    {0, -5479.8f, -2447.1f, 400.7f, {12, 16, 17, -1}},           // 15: South Gate Pass (Barleybrew)
    {0, -6033.0f, -2495.3f, 310.1f, {15, -1, -1, -1}},           // 16: Loch Modan South Tunnel (Pebblebitty)
    {0, -5365.1f, -2938.8f, 326.8f, {15, -1, -1, -1}},           // 17: Thelsamar Road (Kadrell)

    // --- Map 1: Teldrassil / Dolanaar / Darnassus (18..23) ---
    {1, 10314.0f, 829.8f, 1326.5f, {19, -1, -1, -1}},            // 18: Shadowglen (Ilthalaine)
    {1, 10214.3f, 700.6f, 1360.7f, {18, 20, -1, -1}},            // 19: Shadowglen Road (Porthannius)
    {1, 9852.4f, 972.2f, 1305.5f, {19, 21, -1, -1}},             // 20: Dolanaar East (Amara)
    {1, 9811.2f, 963.3f, 1308.9f, {20, 22, -1, -1}},             // 21: Dolanaar Crossroads
    {1, 9875.0f, 1720.0f, 1315.0f, {21, 23, -1, -1}},            // 22: Darnassus Road Mid
    {1, 9948.0f, 2170.0f, 1328.0f, {22, -1, -1, -1}},            // 23: Darnassus Gates

    // --- Map 530: Azuremyst Isle / Azure Watch / Exodar (24..27) ---
    {530, -3962.0f, -13926.3f, 101.2f, {25, -1, -1, -1}},        // 24: Ammen Vale (Megelon)
    {530, -4039.4f, -13773.7f, 75.3f, {24, 26, -1, -1}},         // 25: Ammen Gate (Proenitus)
    {530, -4158.3f, -12483.1f, 45.5f, {25, 27, -1, -1}},         // 26: Azure Watch Crossroads
    {530, -4010.0f, -11885.0f, 15.0f, {26, -1, -1, -1}},         // 27: Exodar Approach Road
};

constexpr int NUM_ROAD_NODES = static_cast<int>(sizeof(gRoadNodes) / sizeof(gRoadNodes[0]));
constexpr float ROAD_MIN_LEG_YD = 160.0f;
constexpr float ROAD_ATTACH_MAX_YD = 420.0f;
constexpr float ROAD_ARRIVE_YD = 32.0f;
constexpr float ROAD_MAX_FACTOR = 1.65f;

float RoadDist2d(float x1, float y1, float x2, float y2)
{
    float const dx = x1 - x2;
    float const dy = y1 - y2;
    return std::sqrt(dx * dx + dy * dy);
}

// Finds the next road waypoint along the shortest road corridor between `start` and `target` on `mapId`.
// Returns true and sets `outWp` when a beneficial road corridor exists.
bool NextRoadWaypoint(uint32 mapId, G3D::Vector3 const& start, G3D::Vector3 const& target, G3D::Vector3& outWp)
{
    float const directDist = RoadDist2d(start.x, start.y, target.x, target.y);
    if (directDist < ROAD_MIN_LEG_YD)
        return false;

    int entry = -1;
    int exitNode = -1;
    float bestEntryDist = ROAD_ATTACH_MAX_YD;
    float bestExitDist = ROAD_ATTACH_MAX_YD;
    for (int i = 0; i < NUM_ROAD_NODES; ++i)
    {
        if (gRoadNodes[i].mapId != mapId)
            continue;
        float const ds = RoadDist2d(start.x, start.y, gRoadNodes[i].x, gRoadNodes[i].y);
        if (ds < bestEntryDist)
        {
            bestEntryDist = ds;
            entry = i;
        }
        float const dt = RoadDist2d(target.x, target.y, gRoadNodes[i].x, gRoadNodes[i].y);
        if (dt < bestExitDist)
        {
            bestExitDist = dt;
            exitNode = i;
        }
    }
    if (entry < 0 || exitNode < 0 || entry == exitNode)
        return false;

    // Tiny O(N^2) Dijkstra over <= 28 nodes (takes < 0.01 ms).
    float dist[NUM_ROAD_NODES];
    int prev[NUM_ROAD_NODES];
    bool vis[NUM_ROAD_NODES];
    for (int i = 0; i < NUM_ROAD_NODES; ++i)
    {
        dist[i] = 1e9f;
        prev[i] = -1;
        vis[i] = false;
    }
    dist[entry] = 0.0f;

    for (int iter = 0; iter < NUM_ROAD_NODES; ++iter)
    {
        int u = -1;
        for (int i = 0; i < NUM_ROAD_NODES; ++i)
            if (!vis[i] && (u < 0 || dist[i] < dist[u]))
                u = i;
        if (u < 0 || dist[u] >= 1e8f || u == exitNode)
            break;
        vis[u] = true;
        for (int v : gRoadNodes[u].neighbors)
        {
            if (v < 0 || v >= NUM_ROAD_NODES || vis[v])
                continue;
            float const w = RoadDist2d(gRoadNodes[u].x, gRoadNodes[u].y, gRoadNodes[v].x, gRoadNodes[v].y);
            if (dist[u] + w < dist[v])
            {
                dist[v] = dist[u] + w;
                prev[v] = u;
            }
        }
    }
    if (dist[exitNode] >= 1e8f)
        return false;

    float const totalViaRoad = bestEntryDist + dist[exitNode] + bestExitDist;
    if (totalViaRoad > directDist * ROAD_MAX_FACTOR)
        return false;

    // Reconstruct path [entry .. exitNode]
    std::vector<int> chain;
    for (int cur = exitNode; cur >= 0; cur = prev[cur])
        chain.push_back(cur);
    std::reverse(chain.begin(), chain.end());

    // Pick the first node on the road chain not already reached (and not behind us relative to the next hop).
    for (size_t i = 0; i < chain.size(); ++i)
    {
        RoadNode const& n = gRoadNodes[chain[i]];
        float const dToNode = RoadDist2d(start.x, start.y, n.x, n.y);
        if (dToNode <= ROAD_ARRIVE_YD)
            continue;
        // Skip leading node if we're already closer to the next road node than the segment length (avoids backtracking).
        if (i + 1 < chain.size())
        {
            RoadNode const& next = gRoadNodes[chain[i + 1]];
            float const segLen = RoadDist2d(n.x, n.y, next.x, next.y);
            float const dToNext = RoadDist2d(start.x, start.y, next.x, next.y);
            if (dToNext < segLen && dToNext < dToNode + 40.0f)
                continue;
        }
        outWp = G3D::Vector3(n.x, n.y, n.z);
        return true;
    }
    return false;
}

// Per-companion plan state, keyed by low guid. Only the leg (map + target) and the last plan are kept.
struct RouteState
{
    bool sameLeg = false;
    uint32 mapId = 0;
    G3D::Vector3 target;
    bool planned = false;
    uint32 planSec = 0;
    uint32 detours = 0;
    bool hasWp = false;
    G3D::Vector3 wp;
};

std::mutex gRouteMutex;
std::unordered_map<uint32, RouteState> gRoutes;

struct Blocker
{
    G3D::Vector3 pos;
    float radius;
};

struct Exposure
{
    float yards = 0.0f;
    uint32 mobs = 0;             // blockers whose aggro radius a sample of the path entered
    int firstSample = -1;        // first exposed sample
    int firstBlocker = -1;       // blocker of that sample
};

struct PlanOut
{
    float directLen = 0.0f;
    float directExp = 0.0f;
    uint32 mobs = 0;
    bool detour = false;
    G3D::Vector3 wp;
    float detourLen = 0.0f;     // best valid candidate (chosen one when detour = true)
    float detourExp = 0.0f;
};

uint32 NowSec()
{
    return static_cast<uint32>(GameTime::GetGameTime().count());
}

float Length(std::vector<G3D::Vector3> const& poly)
{
    float len = 0.0f;
    for (size_t i = 1; i < poly.size(); ++i)
        len += (poly[i] - poly[i - 1]).magnitude();
    return len;
}

float Dist2d(G3D::Vector3 const& a, G3D::Vector3 const& b)
{
    float dx = a.x - b.x, dy = a.y - b.y;
    return std::sqrt(dx * dx + dy * dy);
}

// Points every `step` yards along the polyline, starting at its first point.
void Sample(std::vector<G3D::Vector3> const& poly, float step, std::vector<G3D::Vector3>& out)
{
    out.clear();
    if (poly.empty())
        return;
    out.push_back(poly.front());
    float need = step;
    for (size_t i = 0; i + 1 < poly.size(); ++i)
    {
        G3D::Vector3 const& a = poly[i];
        G3D::Vector3 const& b = poly[i + 1];
        float const segLen = (b - a).magnitude();
        if (segLen <= 0.0f)
            continue;
        float pos = 0.0f;
        while (segLen - pos >= need)
        {
            pos += need;
            out.push_back(a + (b - a) * (pos / segLen));
            need = step;
        }
        need -= (segLen - pos);
    }
}

// The polyline up to `maxLen` yards.
std::vector<G3D::Vector3> Truncate(std::vector<G3D::Vector3> const& poly, float maxLen)
{
    std::vector<G3D::Vector3> out;
    if (poly.empty())
        return out;
    out.push_back(poly.front());
    float len = 0.0f;
    for (size_t i = 1; i < poly.size(); ++i)
    {
        float const seg = (poly[i] - poly[i - 1]).magnitude();
        if (len + seg >= maxLen && seg > 0.0f)
        {
            out.push_back(poly[i - 1] + (poly[i] - poly[i - 1]) * ((maxLen - len) / seg));
            return out;
        }
        len += seg;
        out.push_back(poly[i]);
    }
    return out;
}

Exposure Measure(std::vector<G3D::Vector3> const& samples, std::vector<Blocker> const& blockers)
{
    Exposure e;
    std::vector<bool> hit(blockers.size(), false);
    for (size_t s = 0; s < samples.size(); ++s)
    {
        bool inside = false;
        for (size_t b = 0; b < blockers.size(); ++b)
        {
            if ((samples[s] - blockers[b].pos).magnitude() <= blockers[b].radius)
            {
                inside = true;
                hit[b] = true;
                if (e.firstBlocker < 0)
                {
                    e.firstBlocker = static_cast<int>(b);
                    e.firstSample = static_cast<int>(s);
                }
            }
        }
        if (inside)
            e.yards += SAMPLE_YD;
    }
    for (bool h : hit)
        if (h)
            ++e.mobs;
    return e;
}

// Hostile, alive, non-civilian, non-passive creatures within range of the bot, with their aggro radius for the bot.
// The check stops accepting once MAX_CREATURES blockers are found, so the visit ends with the cap in the list.
struct BlockerCheck
{
    Player* bot;
    Acore::AnyUnitInObjectRangeCheck inRange;
    size_t& count;

    bool operator()(Unit* unit)
    {
        if (count >= MAX_CREATURES || !inRange(unit))
            return false;
        Creature* creature = unit->ToCreature();
        if (!creature || !creature->IsAlive() || creature->IsCivilian() || creature->IsNeutralToAll() ||
            creature->IsTotem() || !creature->IsHostileTo(bot) || creature->HasReactState(REACT_PASSIVE))
            return false;
        if (creature->GetAttackDistance(bot) <= 0.0f)
            return false;
        // The quest's own objective mobs are the work, not an obstacle: the trip fights them (TripPeace, TripAvoid).
        if (CompanionTrip::IsQuestObjectiveMob(bot, unit))
            return false;
        ++count;
        return true;
    }
};

void CollectBlockers(Player* bot, float range, std::vector<Blocker>& out)
{
    std::list<Unit*> units;
    size_t count = 0;
    BlockerCheck check{bot, Acore::AnyUnitInObjectRangeCheck(bot, range), count};
    Acore::UnitListSearcher<BlockerCheck> searcher(bot, units, check);
    Cell::VisitObjects(bot, searcher, range);

    out.clear();
    for (Unit* unit : units)
    {
        Creature* creature = unit->ToCreature();
        out.push_back({G3D::Vector3(creature->GetPositionX(), creature->GetPositionY(), creature->GetPositionZ()),
                       creature->GetAttackDistance(bot)});
    }
}

bool ValidLeg(PathGenerator const& path, G3D::Vector3 const& end)
{
    if (path.GetPathType() != PATHFIND_NORMAL || path.GetPath().empty())
        return false;
    G3D::Vector3 const& reached = path.GetActualEndPosition();
    return Dist2d(reached, end) <= LEG_END_YD;
}

// Plans the direct path to `target` (horizon), measures its aggro exposure, and, when exposed and allowed, looks for
// a detour. Returns true when the direct path met at least one aggro creature (the caller logs the plan then).
bool PlanLeg(Player* bot, G3D::Vector3 const& target, bool allowDetour, float maxFactor, PlanOut& out)
{
    out = PlanOut();
    G3D::Vector3 const start(bot->GetPositionX(), bot->GetPositionY(), bot->GetPositionZ());
    G3D::Vector3 pathTarget = target;
    float const totalDist2d = Dist2d(start, target);
    if (totalDist2d > HORIZON_YD)
    {
        float const frac = HORIZON_YD / totalDist2d;
        pathTarget.x = start.x + (target.x - start.x) * frac;
        pathTarget.y = start.y + (target.y - start.y) * frac;
        if (bot->GetMap()->IsGridLoaded(pathTarget.x, pathTarget.y))
        {
            float const hz = bot->GetMap()->GetHeight(bot->GetPhaseMask(), pathTarget.x, pathTarget.y, start.z);
            pathTarget.z = hz > -50000.0f ? hz : start.z + (target.z - start.z) * frac;
        }
        else
            return false;
    }
    else if (!bot->GetMap()->IsGridLoaded(pathTarget.x, pathTarget.y))
        return false;

    PathGenerator direct(bot);
    direct.CalculatePath(pathTarget.x, pathTarget.y, pathTarget.z, false);
    PathType const type = direct.GetPathType();
    if ((type & PATHFIND_NOPATH) || (type & PATHFIND_NOT_USING_PATH) || direct.GetPath().empty())
        return false;   // no navmesh here: MoveFarTo keeps its own fallbacks

    std::vector<G3D::Vector3> poly;
    poly.push_back(G3D::Vector3(bot->GetPositionX(), bot->GetPositionY(), bot->GetPositionZ()));
    for (G3D::Vector3 const& p : direct.GetPath())
        poly.push_back(p);
    std::vector<G3D::Vector3> horizon = Truncate(poly, HORIZON_YD);
    out.directLen = Length(horizon);
    if (horizon.size() < 2 || out.directLen < ARRIVE_YD)
        return false;
    G3D::Vector3 const goal = horizon.back();

    std::vector<Blocker> blockers;
    CollectBlockers(bot, BLOCKER_VISIT_YD, blockers);
    if (blockers.empty())
        return false;

    std::vector<G3D::Vector3> samples;
    Sample(horizon, SAMPLE_YD, samples);
    Exposure const base = Measure(samples, blockers);
    out.directExp = base.yards;
    out.mobs = base.mobs;
    if (base.mobs == 0 || base.firstBlocker < 0)
        return false;

    if (!allowDetour)
        return true;

    // Detour candidates around the first exposed creature, 1.5 and 2.5 aggro radii off the path, both sides.
    Blocker const& blocker = blockers[base.firstBlocker];
    G3D::Vector3 const& here = samples[base.firstSample];
    G3D::Vector3 const& ahead = samples[std::min(static_cast<size_t>(base.firstSample) + 1, samples.size() - 1)];
    float dirX = ahead.x - here.x, dirY = ahead.y - here.y;
    float const dirLen = std::sqrt(dirX * dirX + dirY * dirY);
    if (dirLen < 0.01f)
    {
        dirX = goal.x - bot->GetPositionX();
        dirY = goal.y - bot->GetPositionY();
    }
    float const dl = std::sqrt(dirX * dirX + dirY * dirY);
    if (dl < 0.01f)
        return true;
    float const perpX = -dirY / dl, perpY = dirX / dl;

    bool haveBest = false, haveOk = false;
    PlanOut best;   // best valid candidate, any length
    PlanOut bestOk; // best valid candidate within the length limit
    G3D::Vector3 bestOkWp;
    G3D::Vector3 bestWp;
    for (float offset : DETOUR_OFFSETS)
    {
        for (float side : {1.0f, -1.0f})
        {
            float const d = offset * blocker.radius * side;
            float const wx = blocker.pos.x + perpX * d;
            float const wy = blocker.pos.y + perpY * d;
            float const wz = bot->GetMap()->GetHeight(bot->GetPhaseMask(), wx, wy, blocker.pos.z);
            G3D::Vector3 const wp(wx, wy, wz > -50000.0f ? wz : blocker.pos.z);

            PathGenerator toWp(bot);
            toWp.CalculatePath(wp.x, wp.y, wp.z, false);
            if (!ValidLeg(toWp, wp))
                continue;
            PathGenerator wpToGoal(bot);
            wpToGoal.CalculatePath(wp.x, wp.y, wp.z, goal.x, goal.y, goal.z, false);
            if (!ValidLeg(wpToGoal, goal))
                continue;

            std::vector<G3D::Vector3> detourPoly;
            detourPoly.push_back(poly.front());
            for (G3D::Vector3 const& p : toWp.GetPath())
                detourPoly.push_back(p);
            for (G3D::Vector3 const& p : wpToGoal.GetPath())
                detourPoly.push_back(p);

            float const len = Length(detourPoly);
            std::vector<G3D::Vector3> detourSamples;
            Sample(detourPoly, SAMPLE_YD, detourSamples);
            float const exp = Measure(detourSamples, blockers).yards;

            if (!haveBest || exp < best.detourExp || (exp == best.detourExp && len < best.detourLen))
            {
                haveBest = true;
                best.detourLen = len;
                best.detourExp = exp;
                bestWp = wp;
            }
            if (len <= maxFactor * out.directLen && exp < out.directExp &&
                (!haveOk || exp < bestOk.detourExp || (exp == bestOk.detourExp && len < bestOk.detourLen)))
            {
                haveOk = true;
                bestOk.detourLen = len;
                bestOk.detourExp = exp;
                bestOkWp = wp;
            }
        }
    }

    if (haveOk)
    {
        out.detour = true;
        out.wp = bestOkWp;
        out.detourLen = bestOk.detourLen;
        out.detourExp = bestOk.detourExp;
    }
    else if (haveBest)
    {
        out.wp = bestWp;
        out.detourLen = best.detourLen;
        out.detourExp = best.detourExp;
    }
    return true;
}
}  // namespace

namespace CompanionRoute
{
bool NextPoint(Player* bot, uint32 mapId, float x, float y, float z, float& outX, float& outY, float& outZ)
{
    bool enabled = false;
    bool roadEnabled = true;
    float maxFactor = 1.5f;
    ReadSwitches(enabled, roadEnabled, maxFactor);
    if (!enabled || !bot || mapId != bot->GetMapId() || bot->IsInFlight())
        return false;

    uint32 const low = bot->GetGUID().GetCounter();
    CompanionQuest::Trip trip;
    std::string routeOwner;
    if (CompanionQuest::ActiveTrip(low, trip))
        routeOwner = trip.owner;
    else if (sPlayerbotAIConfig.companionTripAvoidDutyAndCraft && CompanionLease::Enabled())
    {
        CompanionLease::Info const li = CompanionLease::Current(low);
        if (li.active && (li.owner.rfind("duty:", 0) == 0 || li.owner.rfind("craft:", 0) == 0 ||
                          li.owner == "fish" || li.owner.rfind("gather:", 0) == 0))
            routeOwner = li.owner;
    }
    if (routeOwner.empty())
        return false;

    G3D::Vector3 const start(bot->GetPositionX(), bot->GetPositionY(), bot->GetPositionZ());
    G3D::Vector3 target(x, y, z);
    bool usingRoad = false;
    if (roadEnabled)
    {
        G3D::Vector3 roadWp;
        if (NextRoadWaypoint(mapId, start, target, roadWp))
        {
            target = roadWp;
            usingRoad = true;
        }
    }
    uint32 const now = NowSec();

    RouteState st;
    {
        std::lock_guard<std::mutex> lock(gRouteMutex);
        auto it = gRoutes.find(low);
        if (it != gRoutes.end() && it->second.sameLeg && it->second.mapId == mapId &&
            (it->second.target - target).magnitude() < 1.0f)
            st = it->second;
    }
    if (!st.sameLeg)
    {
        st = RouteState();
        st.sameLeg = true;
        st.mapId = mapId;
        st.target = target;
    }

    bool due = !st.planned || now - st.planSec >= REPLAN_SEC;
    if (st.hasWp && bot->GetExactDist2d(st.wp.x, st.wp.y) <= ARRIVE_YD)
    {
        st.hasWp = false;   // detour waypoint reached: plan again from here
        due = true;
    }
    if (!due)
    {
        if (st.hasWp)
        {
            outX = st.wp.x;
            outY = st.wp.y;
            outZ = st.wp.z;
            return true;
        }
        if (usingRoad)
        {
            outX = target.x;
            outY = target.y;
            outZ = target.z;
            return true;
        }
        return false;
    }

    PlanOut plan;
    bool const aggro = PlanLeg(bot, target, st.detours < MAX_DETOURS_PER_LEG, maxFactor, plan);
    st.planned = true;
    st.planSec = now;
    st.hasWp = plan.detour;
    if (plan.detour)
    {
        st.wp = plan.wp;
        ++st.detours;
    }

    {
        std::lock_guard<std::mutex> lock(gRouteMutex);
        gRoutes[low] = st;
    }

    if (aggro)
        LOG_INFO("playerbots",
                 "Companion {}: trip route aggro direct={:.1f} exposure={:.1f} chosen={} detourLen={:.1f} "
                 "detourExposure={:.1f} mobs={} lease={}",
                 bot->GetName(), plan.directLen, plan.directExp, plan.detour ? "detour" : "direct", plan.detourLen,
                 plan.detourExp, plan.mobs, routeOwner);

    if (!plan.detour)
    {
        if (usingRoad)
        {
            outX = target.x;
            outY = target.y;
            outZ = target.z;
            return true;
        }
        return false;
    }
    outX = plan.wp.x;
    outY = plan.wp.y;
    outZ = plan.wp.z;
    return true;
}
}  // namespace CompanionRoute
