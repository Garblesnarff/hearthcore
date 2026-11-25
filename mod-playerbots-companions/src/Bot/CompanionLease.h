/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANIONLEASE_H
#define PLAYERBOTS_COMPANIONLEASE_H

#include <string>
#include <vector>

#include "Define.h"

class Player;
class PlayerbotAI;
class WorldObject;

// Companion activity lease: ONE current activity owner per companion, in memory.
//
// An owner is a string: "duty:<duty>" for the phase 1 duties ("duty:bank", "duty:sell"), otherwise
// "<activity>[:<detail>]" ("quest:123", "craft:alchemy", "fish"). The activity (bank, sell, quest, craft, ...) is used
// by "why not <activity>" and by the destination statistics. Only the lease owner gives movement and interaction
// orders; every other
// system asks first (RequestInterrupt, MayTry, BlocksRpg) at its DECISION point (target pick, status change), never
// inside a movement action.
//
// Life cycle (owner code, map thread):
//   if (CompanionLease::Begin(bot, "duty:bank", dest, commitSeconds, policy, score)) { ...walk... }
//   each step: CompanionLease::Progress(low, -distanceToDest);   // larger value = closer to done
//   on finish: CompanionLease::End(low, "duty:bank", "done", CompanionLease::Outcome::Success);
//   each tick: if (!CompanionLease::Holds(low, "duty:bank")) -> the lease was ended for you (stall, timeout,
//              owner command, group): drop your own state, do NOT call End again (it is a no-op anyway).
//
// Rules (all only while AiPlayerbot.Companion.Lease.Enabled = 1; with 0 every gating call is a permissive no-op:
// Begin/RequestInterrupt/MayTry/Holds return true, BlocksRpg false; ReportBlock/WhyNot still work):
//   - Always-allowed interrupts: combat and death SUSPEND the lease (no end, no failure, no cooldown; the stall and
//     time limits do not count while suspended; it resumes after combat / resurrection). Owner command and group
//     with the owner END it with reason interrupt:<kind>, with no failure and no cooldown.
//   - Opportunities (node, corpse, rare mob, quest) are allowed only if the policy allows that kind, AND either the
//     target is within policy.nearYards, or (outside the commit window) gain > lease score + switch margin. An allowed
//     opportunity is a detour: the lease is kept, not ended.
//   - Stall: no Progress() improvement of policy.progressStep for Lease.StallSeconds (default 120) ends the lease with
//     reason stall: one failure on the destination and a cooldown of Lease.CooldownMinutes (default 15). The clock
//     stops while the lease is suspended and is never restarted by a suspension: the non-suspended time since the last
//     progress accumulates across every fight. On resume the best value is rebased to where the bot stands.
//   - Danger escape (AiPlayerbot.Companion.DangerEscape.Enabled, DangerLevelGap default 8): a fight that starts against
//     a unit at or above the companion's level + DangerLevelGap is a danger fight (the highest-level unit fighting the
//     bot). When two danger fights fall in the last 10 minutes, or one has lasted Lease.StallSeconds with no progress
//     since it began, the lease ends as a failure (reason danger) out of combat: the hearthstone item is used when it
//     is off cooldown (action=hearth), else the lease just ends (action=end). A due escape waits for a non-combat tick.
//     The stuck teleport is a private step of NewRpgBaseAction::MoveFarTo and is not used here, so action=teleport is
//     never logged.
//   - Timeout: a lease older than Lease.MaxMinutes (default 30, suspended time excluded) ends with reason timeout
//     (counted as a failure, like a stall).
//   - Retry budget: a destination that failed Lease.RetryBudget (default 3) times this session (worldserver run) is
//     skipped for the session: MayTry() returns false and Begin() refuses it.
//   - Success with policy.cooldownOnSuccess also puts the destination on cooldown (no failure counted): this keeps a
//     far duty from re-picking the same spawn in a loop.
//   - Commit: until commitUntil a different owner cannot take the lease without force (urgent work uses force).
//
// Threads: every function locks one internal mutex and never calls other companion systems while holding it, so it
// is safe to call under CompanionDuty's own lock. Begin/Progress/RequestInterrupt/End/Tick run on the companion's map
// thread; Current/WhyNot/StatusLine are also read by the GM command on the world thread.
//
// Log lines (Info, category playerbots, one line each, key=value):
//   Companion <Name>: lease start owner=<o> dest=<key> x=<x> y=<y> map=<m> commit=<s>s policy=<flags> score=<s>
//   Companion <Name>: lease end owner=<o> dest=<key> reason=<done|stall|timeout|need-gone|lease-gone|fail:<what>|
//                     interrupt:<kind>|replaced:<owner>> outcome=<success|failure|neutral> dur=<s>s paused=<s>s
//                     fails=<n>      (fails = failures of that destination this session)
//   Companion <Name>: lease interrupt owner=<o> by=<combat|death|owner-cmd|group|node|corpse|raremob|quest>
//                     allowed=<0|1> gain=<g> score=<s> dist=<d>
//   Companion <Name>: lease resume owner=<o> after=<kind> paused=<s>s
//   Companion <Name>: lease stall owner=<o> dest=<key> noProgressFor=<s>s fails=<n> cooldownUntil=<unix s|session>
//   Companion <Name>: lease refused owner=<o> dest=<key> reason=<cooldown|budget|commit> holder=<o|none>
//   Companion <Name>: lease pause-cap owner=<o> by=<combat|death> paused=<s>s   (reason pause-cap, a failure)
//   Companion <Name>: lease paused-tick owner=<o> by=<kind> dead=<0|1> inCombat=<0|1> avoiding=<0|1> attackers=<n>
//   Companion <Name>: danger fight owner=<o> attacker=<entry> attLevel=<l> botLevel=<l> fights=<n>
//   Companion <Name>: danger escape lease=<owner> attacker=<entry> attLevel=<l> botLevel=<l> fights=<n>
//                     action=<hearth|end>
class CompanionLease
{
public:
    // Interrupt kinds. Combat, Death, OwnerCommand and Group are always allowed.
    enum class Interrupt : uint8
    {
        Combat,
        Death,
        OwnerCommand,
        Group,
        Node,      // a gathering node (herb, ore, chest)
        Corpse,    // a corpse to loot or skin
        RareMob,   // a rare or elite worth killing
        Quest      // a quest objective or giver off the current route
    };

    enum class Outcome : uint8
    {
        Success,   // goal reached; cooldown only when policy.cooldownOnSuccess
        Failure,   // counts toward the retry budget and puts the destination on cooldown
        Neutral    // need gone, replaced or interrupted: no failure, no cooldown
    };

    struct Destination
    {
        std::string key;   // namespaced: "npc:<spawnId>", "go:<spawnId>", "quest:<id>", "node:<spawnId>", ...
        uint32 mapId = 0;
        float x = 0.0f, y = 0.0f, z = 0.0f;
    };

    struct Policy
    {
        bool allowNodes = false;
        bool allowCorpses = false;
        bool allowRareMobs = false;
        bool allowQuest = false;
        bool allowRpg = false;           // new-rpg status changes may run while this lease is held
        float nearYards = 20.0f;         // allowed opportunities within this range need no score test
        float progressStep = 10.0f;      // Progress() counts when the value beats the best so far by this much
        bool cooldownOnSuccess = false;  // see Outcome::Success
    };

    // Copy of a companion's lease, for readers.
    struct Info
    {
        bool active = false;
        std::string owner;
        Destination dest;
        Policy policy;
        float score = 0.0f;
        float margin = 0.0f;     // companion's switch margin, read at Begin
        uint32 startSec = 0;
        uint32 commitUntilSec = 0;
        uint32 lastProgressSec = 0;
        float progress = 0.0f;   // last value given to Progress()
        bool paused = false;
        std::string pausedBy;    // "combat" or "death"
        uint32 pausedSinceSec = 0;
        uint32 pausedTotalSec = 0;
    };

    // Any thread. AiPlayerbot.Companion.Lease.Enabled.
    static bool Enabled();
    // Lease.MaxPauseSeconds: a continuous suspension longer than this ends the lease (reason pause-cap); 0 = off.
    static uint32 MaxPauseSeconds();
    // Danger-death escape: true when the companion died in a danger fight or danger zone and should revive at homebind
    // instead of running back to its corpse.
    static bool NeedsDangerDeathEscape(uint32 lowGuid);
    static void ClearDangerDeathEscape(uint32 lowGuid);

    // Map thread. Takes the lease for owner toward dest. Same owner and same destination: keeps the running lease
    // (true, nothing logged). Refused (false, "lease refused" logged) when the destination is on cooldown or skipped
    // for the session, or when another owner's lease is inside its commit window and force is false. A different
    // running owner is ended with reason replaced:<owner> (neutral). score is the new owner's current score (used for
    // opportunity interrupts). commitSeconds 0 = AiPlayerbot.Companion.Lease.CommitSeconds.
    static bool Begin(Player* bot, std::string const& owner, Destination const& dest, uint32 commitSeconds,
                      Policy const& policy, float score, bool force = false);
    // Map thread. Reports progress; a larger value is better (e.g. minus the distance to the destination).
    static void Progress(uint32 lowGuid, float value);
    // A planned wait (dock wait for a boat, a crafting session at a station) counts as progress:
    // resets the stall clock without changing the best progress value. Call it every tick of the wait.
    static void KeepAlive(uint32 lowGuid);
    // A new progress scale starts (the last transport leg is done; the walk reports -distance from now on):
    // forget the best value so far and restart the stall clock.
    static void ResetProgress(uint32 lowGuid);
    // Map thread. Asks to interrupt the current lease. True when allowed (or no lease / lease disabled). See rules.
    static bool RequestInterrupt(Player* bot, Interrupt kind, float gain = 0.0f, float distance = 0.0f);
    // Any thread. Ends the lease if owner holds it (empty owner = whoever holds it). reason is a short code.
    static void End(uint32 lowGuid, std::string const& owner, char const* reason, Outcome outcome);
    // Map thread, from CompanionBotMgr::TickBot every sync. Suspends on combat / death, resumes after, and ends the
    // lease on stall or timeout.
    static void Tick(PlayerbotAI* botAI);

    // Any thread. Copy of the current lease (active = false when none).
    static Info Current(uint32 lowGuid);
    // Any thread. True when owner holds the active lease (true for anyone when the lease is disabled).
    static bool Holds(uint32 lowGuid, std::string const& owner);
    // Any thread. False while the destination is on cooldown or skipped for the session.
    static bool MayTry(uint32 lowGuid, std::string const& destKey);
    // Any thread. True while a lease is held whose policy does not allow new-rpg status changes.
    static bool BlocksRpg(uint32 lowGuid);

    // Destination keys for spawns.
    static std::string SpawnKey(bool isGo, uint32 spawnId);
    static std::string ObjectKey(WorldObject const* object);   // "npc:<spawn>", "go:<spawn>" or "guid:<low>"

    // "Why not". An activity is answerable once it is registered; bank, sell, repair, train, grind and gather are
    // built in. quest, craft and fish answer "not available in this build" until their code calls RegisterActivity.
    // Any thread.
    static void RegisterActivity(std::string const& activity);
    // Map thread (the activity's decision point). The current blocking condition of an activity for one companion,
    // e.g. "nothing to sell" or "score 12.0 <= activity 30.0 + margin 20.0". Empty text clears it.
    static void ReportBlock(uint32 lowGuid, std::string const& activity, std::string const& condition);
    // Any thread. Lines for "why not <activity>": blocking condition, last attempt, failure count, retry time.
    static std::vector<std::string> WhyNot(uint32 lowGuid, std::string const& activity);
    // Any thread. One line describing the current lease, for "why".
    static std::string StatusLine(uint32 lowGuid);
    // The activity names "why not" accepts.
    static std::vector<std::string> const& ActivityNames();
};

#endif
