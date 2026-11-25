/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANIONQUEST_H
#define PLAYERBOTS_COMPANIONQUEST_H

#include <string>
#include <vector>

#include "Define.h"

class Creature;
class Player;
class PlayerbotAI;
class Quest;

// Companion quest project: questing before grinding, with travel to other zones (walk, mount, known flight paths),
// and the mount project. Behind AiPlayerbot.Companion.Quest.Enabled (whole project) and
// AiPlayerbot.Companion.Mount.Enabled (mount purchase and mounting for travel); random bots are never touched.
//
// Every QuestEvalSeconds (autonomy only, out of combat, no duty running) the companion scores its options, best first:
//   turnin  a COMPLETE quest in the log, ender spawn (creature or game object) on the bot's map, any zone
//   work    an INCOMPLETE quest whose objective POI is on the bot's map (any zone), objective mobs <= level + 3
//   hub     a cluster of quest givers (within HubRadius) with at least two quests the bot can take
//   mount   no mount yet, enough gold: the race's mount vendor on the bot's map (else any vendor that sells it)
//   grind   the low fallback (weight quest_grind)
// score = need x weight - travel x travel weight; travel = estimated seconds / 15, capped at 80, by the cheapest of
// walking (mounted when the bot has a mount) and known flight paths (only nodes the bot knows; the start flight master
// is learned on the visit). Targets on another map or landmass go by boat, tram, portal or the Dalaran hearth route
// (CompanionTransport, AiPlayerbot.Companion.Transport.Enabled); with it off they are blocked:
// "trip blocked ... reason=no-transport".
// A trip (turnin, work, hub, mount) takes the activity lease with owner "quest:<id>", "quest:hub:<zone>" or
// "mount:buy" and policy allowRpg = false, so new-rpg cannot change status while it runs; the stall, cooldown and retry
// budget of the lease apply. The trip is executed by the "companion quest travel" action (CompanionQuestStrategy.cpp),
// which reuses NewRpgBaseAction::MoveFarTo and the stock quest-giver interaction (turn in + accept).
// "work here" (POI in the bot's zone) and "grind" do not take a lease: they set the new-rpg state (DO_QUEST) or allow
// GO_GRIND for the next new-rpg IDLE decision.
//
// Threads: UpdateWorld() on the world thread builds the shared tables (hubs, quest enders, mount vendors, rep goals)
// once and publishes them; Evaluate() and the trip accessors run on the companion's map thread; the per-bot state is
// guarded by one mutex (WhyLines is read by the GM command).
//
// Log lines (Info, category playerbots):
//   Companion <Name>: quest pick <kind>=<id|zone> score=<s> top=<kind:id:score,...>
//   Companion <Name>: trip start kind=<turnin|work|hub|mount> id=<q|0> from=<zone> to=<zone> by=<walk|mount|flight>
//                     dist=<yd> est=<s>s score=<s> [nodes=<a>-<b> | legs=<kind:id>...] [via=turnin:<id>]
//                     [forced=1] [cross=<fromMap>><toMap>]
//   Companion <Name>: trip end kind=<k> id=<q> to=<zone> result=<arrived|turned-in|accepted:<n>|bought|stall|...>
//                     dur=<s>s
//   Companion <Name>: trip blocked kind=<k> id=<q> to=<zone> map=<m> reason=<no-transport|zone-level|no-path>
//   Companion <Name>: trip flight from=<node> to=<node> nodes=<n> result=<ok|fail>
//   Companion <Name>: quest turnin id=<q> zone=<z> gold=<c>   (detected from the log, any path)
//   Companion <Name>: quest accept id=<q> level=<l> zone=<z>  (Playerbots.cpp hook)
//   Companion <Name>: abandon quest=<q> reason=<log-full|mobs-too-high|failed-3x|status-failed|...> value=<v>
//   Companion <Name>: abandon skip quest=<q> reason=<owner-shared|owner-unknown> [why=<reason it would drop>]
//   Companion <Name>: abandon refused quest=<q> reason=<owner-shared|owner-unknown|not-in-log> why=gm-command
//   Companion <Name>: abandon quest=<q> reason=gm-command value=<v>   (GM test hook, when not refused)
//   Companion <Name>: accept skip quest=<q> reason=<free-slots|grey-low-value|not-in-hub-plan> ... (rate limited)
//   Companion <Name>: hub plan zone=<z> free=<n> keep=<k> take=<q:value,...>
//   Companion owner quests: source=<online|db|logout> count=<n>
//
// Owner-shared quests (B2): a quest the owner (AiPlayerbot.CompanionOwner) has in his log is never abandoned, grouped
// or not. The world thread keeps a copy of his log: the live log every 10 s while he is online (and at logout), else
// character_queststatus (status <> 0) every 5 minutes. Until the copy has loaded once, nothing is abandoned.
// Log pressure (B3): companions accept a quest only while more than Quest.FreeSlots log slots are free (a quest the
// owner has needs one free slot); grey quests below Quest.GreyMinValue are skipped; a hub trip takes only its plan:
// the best (free - FreeSlots) quests of the hub by value, best first.
// Waiting turn-ins (B4): a complete quest gains Quest.TurnInWaitBonusPerMin per minute since it was first seen
// complete this session (capped at Quest.TurnInWaitBonusMax), so a far ender is not starved by nearer goals.
// Cross-continent work (C route, AiPlayerbot.Companion.Quest.CrossContinentWork, default 1): an incomplete quest
// whose open objective POIs are all on another companion map (0, 1, 530, 571) is offered as a work trip routed by
// boat / tram / portal / hearth (CompanionTransport). Batching per continent (map), so a companion does not
// ping-pong across the sea:
//   goals(m) = cross work options + turn-in options whose ender is on map m; here = the worthwhile work + turn-in
//   options on the bot's own map.
//   - "stay" (D route): after the bot arrived on a map by a cross-map trip, it stays there while it has worthwhile
//     goals (work or turn-in options that score at least the grind weight; "here"), up to
//     Quest.CrossStayMaxMinutes (90, the safety max). While staying, no work or turn-in on another map is offered.
//     When the goals run out (or the max is hit) the stay ends: "trip cross done map=<m> goalsLeft=<n> stayed=<s>s [max=<min>]" is logged once, and the way
//     back is a normal batched trip ("trip cross return from=<m> to=<m2> stayed=<s>s" when it starts).
//   - "few": with here > 0, a map is crossed to only when goals(m) >= Quest.CrossMinGoals (3).
//   - "allow": cross work score = (35 + min(30, value x 0.7)) x quest_work weight - travel / goals(m) -
//     Quest.CrossPenalty (10): the crossing is charged once for the batch, and the penalty makes the bot finish its
//     own continent first. The travel is the real route (PlanRoute legs: boat waits, tram, portal, hearth if the
//     hearth rules allow it).
//   The target height is unknown until the bot is on the target map (read there, first tick after the legs).
//   Companion <Name>: trip cross map=<m> from=<map> work=<n> turnin=<n> here=<n> result=<allow|few|stay>
//                     best=<label|-> score=<s|-> travel=<s>s cost=<c> minGoals=<n> stayLeft=<s>s
//   (once per map when the result changes, else at most every 5 minutes)
//   Companion <Name>: trip cross done map=<m> goalsLeft=<n> stayed=<s>s [max=<min>]     (once per crossing)
//   Companion <Name>: trip cross return from=<m> to=<m2> stayed=<s>s kind=<k> id=<q> score=<s>
//   Companion <Name>: mount <have|learned|bought|save-gold|blocked> ... reason=...
//   Companion tables: zones=<KB> questGiverSpots=<KB> questEnders=<KB> dutySpots=<KB> hubs=<KB> mountVendors=<KB>
class CompanionQuest
{
public:
    enum class TripKind : uint8
    {
        None,
        TurnIn,
        Work,
        Hub,
        Mount,
        Travel   // a forced trip to a point (GM command .companion travel)
    };

    // Phases of a trip with a flight: walk to the flight master, fly, walk to the target.
    enum class Phase : uint8
    {
        ToFlightMaster,
        Flying,
        ToTarget
    };

    struct Trip
    {
        bool active = false;
        TripKind kind = TripKind::None;
        std::string owner;          // lease owner
        uint32 questId = 0;         // turnin / work
        uint32 zone = 0;            // destination zone
        uint32 fromZone = 0;
        uint32 mapId = 0;
        float x = 0.0f, y = 0.0f, z = 0.0f;
        uint32 spawnId = 0;         // turnin ender / mount vendor spawn (0 for work and hub)
        bool spawnIsGo = false;
        uint32 itemId = 0;          // mount item to buy
        bool flight = false;
        Phase phase = Phase::ToTarget;
        float fmX = 0.0f, fmY = 0.0f, fmZ = 0.0f;   // start flight master (taxi node position)
        std::vector<uint32> nodes;  // taxi route, start first
        uint32 startSec = 0;
        uint32 arriveSec = 0;       // first second within reach of the target (0 = not yet)
        uint32 accepted = 0;        // quests accepted at a hub
        std::vector<uint32> hubPlan;// hub: the quests to take, best value first (B3)
        float score = 0.0f;
        bool cross = false;         // the target is on another map than the trip's start
        bool zUnknown = false;      // cross-map work: z (and the real zone) are read on arrival on the target map
    };

    // Any thread.
    static bool Enabled();
    static bool MountEnabled();
    static bool AcceptGrey();
    static float MountMinDistance();
    // new-rpg DO_QUEST search for companions (AiPlayerbot.Companion.DoQuest.*).
    static bool DoQuestSearch();
    static float DoQuestSearchYards();
    static uint32 DoQuestStallMinutes();

    // World thread, every world tick (CompanionBotMgr::Update). Reads the config once, builds the shared tables once
    // the quest-giver spots are loaded, and reloads companion_rep_goals every 10 minutes.
    static void UpdateWorld(uint32 elapsed);

    // Map thread, from CompanionBotMgr::TickBot (autonomy only), after CompanionDuty::Evaluate.
    static void Evaluate(PlayerbotAI* botAI);

    // Map thread (trigger / action). Copy of the active trip. False when none.
    static bool ActiveTrip(uint32 lowGuid, Trip& out);
    static void SetPhase(uint32 lowGuid, Phase phase);
    static void NoteArrival(uint32 lowGuid, uint32 nowSec);
    // Map thread (action), on the target map: the height and zone of a cross-map work target.
    static void SetTripPoint(uint32 lowGuid, float z, uint32 zone);
    static void NoteAccepted(uint32 lowGuid);
    // Map thread. A quest was handed to new-rpg DO_QUEST; if new-rpg drops it at once, it is not offered for 15 min.
    static void NoteWorkHere(uint32 lowGuid, uint32 questId);
    // Map thread (action). Ends the trip and its lease. result is a short code for the log.
    static void EndTrip(PlayerbotAI* botAI, char const* result, bool success);

    // Map thread. Quest value for this bot (reputation goals, items, gold, completionist, XP when not capped).
    static float QuestValue(Player* bot, Quest const* quest);
    // Map thread. True while the last pick of the project was grind (new-rpg may choose GO_GRIND).
    static bool GrindAllowed(uint32 lowGuid);
    // Map thread (OrganizeQuestLog for companions). Drops the lowest-value incomplete quest when the log is full.
    // Never a complete quest. True when a quest was dropped.
    static bool FreeLogSlot(Player* bot);

    // Map thread (travel action). Mounts for a leg longer than MountMinDistance when no hostile is near and the bot
    // knows a ground mount spell. True when a mount cast started.
    static bool TryMount(PlayerbotAI* botAI, float legDistance);
    // Map thread (travel action). Buys the trip's mount item at the vendor (in interaction range) and learns it.
    static bool BuyMount(PlayerbotAI* botAI, Creature* vendor, uint32 itemId);
    // Any thread. True when the bot knows a ground mount spell.
    static bool HasGroundMount(Player const* bot);

    // World thread (CompanionQuest::UpdateWorld). Keeps the copy of the owner's quest log.
    // Map thread. True when the companion must keep the quest for the owner (he has it, or his log is not known yet);
    // logs "abandon skip" (rate limited). why is the reason the quest would be dropped (for the log line).
    static bool KeepForOwner(Player* bot, uint32 questId, char const* why);
    // GM test hook (.companion abandon <name> <questId>): the next quest evaluation runs the companion's abandon path
    // for the quest (refused and logged "abandon refused ... reason=owner-shared" when the owner has it too).
    static void RequestAbandon(uint32 lowGuid, uint32 questId);
    // Any thread. True when the owner has the quest in his log (copy, or the live log when he is on the bot's map).
    static bool OwnerHasQuest(Player* bot, uint32 questId);
    // Map thread (NewRpgBaseAction::IsQuestWorthDoing for companions). False when accepting the quest would break the
    // log-pressure rules (free slots, grey low value, the hub plan). Shared by HasQuestToAcceptOrReward and the accept.
    static bool MayAccept(Player* bot, Quest const* quest);

    // Any thread. "why" lines of the quest project (GM command / owner chat may append them).
    static std::vector<std::string> WhyLines(uint32 lowGuid);
};

#endif
