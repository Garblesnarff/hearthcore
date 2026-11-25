/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANIONROUTE_H
#define PLAYERBOTS_COMPANIONROUTE_H

#include "Define.h"

class Player;

// Companion trip route (aggro-aware). Companions only, trips only; behind AiPlayerbot.Companion.TripAggroRoute.Enabled
// (default 1) and AiPlayerbot.Companion.TripAggroRoute.MaxDetourFactor (default 1.5), both re-read every 10 s.
//
// When a trip leg walks (CompanionQuestTravelAction::MoveLeg), the route is planned for the next horizon of the
// navmesh path to the leg target (up to RouteHorizonYards along the path). Points along that path are sampled every
// SampleYards; a sample is exposed when it is within the aggro radius (Creature::GetAttackDistance for the companion's
// level) of a hostile, alive, non-civilian, non-passive creature that would pull it. The exposure is the yards of
// sampled path inside any such radius.
//
// If the direct path is exposed, up to 4 detour waypoints are tried around the first exposed creature: 1.5 and 2.5
// aggro radii from it, left and right of the path. Each waypoint is validated with PathGenerator (bot -> waypoint and
// waypoint -> path point, both PATHFIND_NORMAL and ending at their target). A detour is chosen only when its length is
// less than MaxDetourFactor x the direct length AND its exposure is lower than the direct exposure. Otherwise the
// direct path is kept (the walk goes to the leg target, as before).
//
// Plans are cached per companion and leg: a new plan happens when the leg target changes, when the detour waypoint is
// reached, or every RouteReplanSeconds. A plan costs one creature search over the horizon and at most 9 PathGenerator
// calls, on the map thread; it is never run when the companion is not walking a trip leg.
//
// Creatures that are quest mobs of the companion (CompanionTrip::IsQuestObjectiveMob) are not blockers: the trip
// fights them, so a detour around the quest's own objective would only cost time.
//
// Road preference is not implemented: 3.3.5 has no road data on the server (the travel node tables are navigation
// graph nodes, not road geometry; TravelNodeMap is not loaded).
//
// Log line (Info, category playerbots), written when the plan saw at least one aggro creature on the path:
//   Companion <Name>: trip route aggro direct=<len> exposure=<yd> chosen=<direct|detour> detourLen=<len>
//                     detourExposure=<yd> mobs=<n> lease=<owner>
// len and exposure are yards, rounded to 1 yd. direct = the sampled horizon of the direct path; detourLen and
// detourExposure are the best valid detour candidate (0 when none was valid).
namespace CompanionRoute
{
// Walk target for the current trip leg. Returns true with (x, y, z) set to the waypoint to walk to instead of the leg
// target (a detour around aggro creatures). Returns false when the caller should walk to the leg target itself.
bool NextPoint(Player* bot, uint32 mapId, float x, float y, float z, float& outX, float& outY, float& outZ);
}  // namespace CompanionRoute

#endif  // PLAYERBOTS_COMPANIONROUTE_H
