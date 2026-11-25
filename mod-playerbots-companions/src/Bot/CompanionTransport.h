/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANIONTRANSPORT_H
#define PLAYERBOTS_COMPANIONTRANSPORT_H

#include <functional>
#include <string>
#include <vector>

#include "Define.h"

class Player;
class PlayerbotAI;

// Companion dock-to-dock travel (B9), the Dalaran hearth route (B13 c, d) and the same-continent hearth (C route,
// rules revised in D route). Behind AiPlayerbot.Companion.Transport.Enabled (boats, tram, Rut'theran portal) and
// AiPlayerbot.Companion.Hearth.Enabled (hearthstone to Dalaran + Silver Enclave portal, and to any home bind), both
// default 1, re-read every 10 s (reload config works).
//
// Tables (world thread, once, from UpdateWorld):
//   boats   every row of the world `transports` table whose game object is an MO_TRANSPORT with a taxi path and a
//           transport template; the stops are the taxi path nodes with a delay (the exact loop of
//           TravelNodeMap::generateTransportNodes, which is not called: it would fill the global node map that
//           random bots read). Each stop gets a standing point on the pier (nearest creature / object spawn above the
//           water within 120 yd) and its arrival/departure time from the transport template key frames.
//           Boats named "(..., Horde ...)" are Horde only, "(..., Alliance ...)" Alliance only, the rest neutral.
//   tram    the Deeprun Tram area triggers (areatrigger_teleport rows whose target map is 369, and the exits on 369).
//   portal  same-map area trigger teleports between two landmasses (Rut'theran <-> Darnassus).
//   dalaran Silver Enclave portals (game objects 190960, 191008, 191006, 191007, type 22); the destination is the
//           object's spell (following a force-cast / trigger effect) in spell_target_position.
// A landmass is a piece of land that can be walked: Eastern Kingdoms, Kalimdor, Teldrassil (top), Rut'theran,
// Feathermoon (Sardor Isle), Azuremyst/Bloodmyst, the rest of map 530, Northrend, Dalaran, the tram.
//
// Route (map thread): Dijkstra from the bot over the leg ends; walking only within a landmass; a leg costs its
// expected wait (boats: their real period / 2, at most 180 s) + crossing time; the hearth leg (10 s cast + load) only
// when the bot has item 6948, it is off cooldown and the bot is not in combat, an instance, a battleground or a
// dungeon group, and either
//   - the home bind is Dalaran (map 571, zone 4395, Alliance): the Silver Enclave portal route (B13), rule "dalaran",
//     unchanged; or
//   - any other home bind (AiPlayerbot.Companion.Hearth.SameContinent, default 1): the hearth leg is kept only when
//     every rule passes, in this order (the first that fails names the rule):
//       saving-low  the route with the hearth is less than Hearth.MinSavingSeconds (180, minimum 180) faster than the
//                   best route without it (walk / boats / tram / portals; the quest planner also compares flights);
//       need-*      the companion's plan needs the stone within the hearthstone cooldown (below);
//       ok          otherwise.
//     Needs (HearthNeed): need-inn when a hearth (bind) duty is running, or the GM test need "inn";
//     the destination is far from the bind (another map, or more than Hearth.NeedReturnYards from it: the bot would
//     need the stone to get back) and then need-craft when a craft session is planned in the home city (CompanionCraft
//     run, read with CompanionCraft::GetRun, no new query), else need-return. The GM test need "craft" / "return"
//     forces those too. A destination near the bind is "ok": the bot ends near its inn.
//   The cooldown is read from the spell (item 6948 -> SpellInfo RecoveryTime / CategoryRecoveryTime, else the item
//   template spell cooldown): HearthCooldownSeconds(), never hard-coded. A planned craft session is pending, so it is
//   always within the cooldown; the cooldown is logged with every decision.
// Duty walks never hearth (Step allowHearth = false, PlanRoute allowHearth = false for duty scoring).
//
// Execution (map thread, Step): walk or ride (the caller's move function) to the departure point; boats: wait at the
// dock until the boat arrives by its real schedule (sampled from the live transport on the world thread), at most
// 180 s, CompanionLease::KeepAlive every tick; then TeleportTo the arrival dock. Tram: the entrance area trigger
// (HandleAreaTriggerOpcode, the stock handler) to map 369, a 30-90 s ride wait, platform to platform, then the exit
// trigger. Rut'theran portal: the area trigger. Hearth: the item is used like a player uses it
// (Player::CastItemUseSpell), then the bot walks to the Silver Enclave portal and uses it (GameObject::Use).
// No other teleport.
//
// Log lines (Info, category playerbots):
//   Companion transport: dock boat=<entry> name=<ship> stop=<n> map=<m> at=<x>,<y> stand=<x>,<y>,<z> area=<name>
//                        land=<l> arrive=<ms> depart=<ms> period=<ms>
//   Companion transport: dock pair boat=<entry> from=<dock> to=<dock> ride=<s>s team=<alliance|horde|both>
//   Companion transport: tram|portal|dalaran ... ; Companion transport: tables legs=<n> ...
//   Companion <Name>: transport plan owner=<o> legs=<kind:id,...> est=<s>s
//   Companion <Name>: transport depart kind=<boat|tram|portal> id=<entry|trigger> from=<dock> to=<dock> wait=<s>
//                     sched=<real|random|portal>
//   Companion <Name>: transport arrive kind=<k> id=<i> from=<dock> to=<dock> waited=<s>s map=<m> zone=<z>
//   Companion <Name>: transport fail owner=<o> reason=<no-route|...>
//   Companion <Name>: transport hearth use=<0|1>
//                     rule=<ok|dalaran|only-route|saving-low|need-craft|need-inn|need-return>
//                     saving=<s|all>s cooldown=<s>s hearth=<s>s nohearth=<s|none>s homeDist=<yd|other-map>
//                     minSaving=<s> needReturn=<yd> home=<map>:<zone> from=<map>:<zone> to=<map>:<zone> owner=<o>
//                     (once per trip plan, when the hearth route was cheaper than any route without it; use=0: a rule
//                     kept the stone and the route without it is taken)
//   Companion <Name>: hearth cast item=6948 spell=<id> home=<map>:<zone> reason=<rule> cooldown=<s>s
//                     (every use, with the reason)
//   Companion <Name>: hearth arrive map=<m> zone=<z> dur=<s>s
//   Companion <Name>: hearth portal entry=<id> to=<map>:<x>,<y> result=<used|no-object>
//   Companion <Name>: hearth skip reason=<...>   (forced trips / why only)
// GM test forms: .companion hearth <name> need <craft|inn|return|none> [minutes] (default 30) forces that need for
// the companion's hearth decisions; .companion hearth <name> status shows the cooldown and the forced need.
class CompanionTransport
{
public:
    enum class Drive : uint8
    {
        NotNeeded,   // the target is on the bot's landmass and walking is best: the caller moves as before
        Busy,        // a leg is running (walk to the dock, wait, teleport): the caller does nothing else this tick
        Failed       // no route (or a leg failed twice): the caller ends its trip
    };

    // Move function of the caller (walk / ride on the bot's map to x, y, z; dist = distance to it). True when a move
    // was issued (or is running).
    using MoveFn = std::function<bool(uint32 mapId, float x, float y, float z, float dist)>;

    // A planned route: total seconds (walk + waits + crossings + walk), ok = reachable, legs = leg indices.
    struct Plan
    {
        bool ok = false;
        float seconds = 0.0f;
        std::vector<uint32> legs;
        // The hearth decision (filled when the route with the hearth leg was cheaper than any route without it).
        bool hearthChecked = false;
        bool hearthUsed = false;
        float hearthSeconds = 0.0f;     // the route with the hearth leg
        float noHearthSeconds = -1.0f;  // the best route without it (-1: none)
        float homeDist = -1.0f;    // destination to the home bind (-1: another map)
        float hearthCooldown = 0.0f;  // the hearthstone cooldown read from the spell (seconds)
        std::string hearthRule;    // ok | dalaran | only-route | saving-low | need-craft | need-inn | need-return
        std::string Describe() const;
    };

    // A forced trip from the GM command (.companion travel): taken by CompanionQuest::Evaluate on the map thread.
    struct ForceRequest
    {
        enum class Kind : uint8
        {
            Point,
            Quest,
            Vendor,
            Work   // an incomplete quest's objective area (nearest open objective POI, any companion map)
        };
        Kind kind = Kind::Point;
        uint32 mapId = 0;
        float x = 0.0f, y = 0.0f, z = 0.0f;
        uint32 zone = 0;   // filled on the world thread for Point
        uint32 id = 0;     // quest id or creature entry
    };

    // Any thread.
    static bool Enabled();
    static bool HearthEnabled();
    static bool HearthSameContinent();
    static float HearthMinSavingSeconds();
    // The hearthstone cooldown of item 6948, read from its spell (RecoveryTime / CategoryRecoveryTime, else the item
    // template spell cooldown). Any thread (DBC and item templates are read-only).
    static float HearthCooldownSeconds();
    // GM test need ("craft", "inn", "return"; "" = none), forced for minutes. Any thread.
    static void SetTestNeed(uint32 lowGuid, std::string const& need, uint32 minutes);
    static std::string TestNeed(uint32 lowGuid);
    static bool Ready();

    // World thread (CompanionBotMgr::Update): config every 10 s, tables once, boat schedule samples every 20 s.
    static void UpdateWorld(uint32 elapsed);

    // Any thread (tables are read-only once Ready). Landmass of a point; zone 0 = unknown (map default).
    static uint32 Landmass(uint32 mapId, uint32 zone, float x, float y, float z);
    // Map thread. Landmass of the bot itself.
    static uint32 BotLandmass(Player* bot);

    // Map thread. Cheapest route from the bot to the target. ok = false: no way there. legs empty: walk directly
    // (the caller's own planner also compares flights). speed = the caller's ground speed in yards per second.
    // allowHearth = false: no hearth leg (duty scoring, like the duty walk).
    static Plan PlanRoute(Player* bot, uint32 mapId, float x, float y, float z, uint32 zone, float speed,
                          bool allowHearth = true);
    // Map thread. True when the target is on another map or landmass than the bot (a leg is needed).
    static bool NeedsLegs(Player* bot, uint32 mapId, float x, float y, float z, uint32 zone);

    // Map thread (travel actions). Drives the bot's legs toward the target for the lease owner. See Drive.
    // allowHearth = false: no hearth leg (duty walks: a hearth to Dalaran would strand a same-map duty).
    static Drive Step(PlayerbotAI* botAI, std::string const& owner, uint32 mapId, float x, float y, float z,
                      uint32 zone, MoveFn const& move, bool allowHearth = true);
    // Any thread. Drops the bot's leg state (trip ended).
    static void Reset(uint32 lowGuid);

    // World thread (GM command) puts, map thread (CompanionQuest::Evaluate) takes.
    static void RequestForce(uint32 lowGuid, ForceRequest const& request);
    static bool TakeForce(uint32 lowGuid, ForceRequest& out);

    // Any thread. Lines for "why" / the GM command.
    static std::vector<std::string> WhyLines(uint32 lowGuid);
    // World thread (GM command). The dock table, one line each.
    static std::vector<std::string> TableLines();
};

#endif
