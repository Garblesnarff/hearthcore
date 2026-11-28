/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANION_COOP_LIFECYCLE_LOGIC_H
#define PLAYERBOTS_COMPANION_COOP_LIFECYCLE_LOGIC_H

#include "CompanionLfgBridgeLogic.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

// Pure C++17 native bot-only cooperative dungeon lifecycle & leader execution state machine (Issue #21).
// Zero AzerothCore header dependencies so CompanionBrain.cpp, CompanionBotMgr.cpp, CompanionLeadStrategy.cpp,
// CompanionClassStrategy.cpp, Playerbots.cpp, and standalone native C++17 tests enforce identical invariants.
//
// Core invariants:
// - Native RDF teleport & native group/instance execution only; NO encounter bypasses, NO injected loot,
//   NO model/LLM calls in combat.
// - Owner-led / human-grouped runs are strictly untouched (every evaluation checks !groupedWithOwner && !hasHuman).
// - Native group readiness: ALL 5 members must be online, in-world, alive, and on the target dungeon map before
//   pulling or traversing. Entry timeout clock starts ONLY once in LFG group or target dungeon (bounded 180s).
// - Dynamic leader & follower control: per-member leader tracking prevents mixed in/out-of-dungeon flapping;
//   effective leader has NO conflicting solo movement and keeps +loot; followers keep +follow,+loot and re-bind
//   master cleanly on leader failover.
// - In-instance progression: verified stock dungeon boss waypoints (Stockade map 34, Deadmines map 36 including
//   Sneed's Shredder door boss) guide the tank between cleared corridors. Peek is strictly read-only; only Execute
//   advances waypointIndex.
// - Honest completion evidence: separates FinalBossKill vs LfgFinished (never fabricates a boss entry!); enforces
//   bounded post-boss loot grace + bounded hard-cap exit timeout (no infinite combat/dead wedge) and force-claims
//   terminal DB outcomes on disband during loot grace.
// - Bounded wipe & stall recovery: debounced party wipes (max 3), wipe recovery timeout (300s for Deadmines ghost
//   run), and combat-stall cap (300s without kills) prevent infinite corpse loops or stuck-combat wedges.
namespace CompanionCoopLifecycle
{
inline constexpr std::uint32_t DEFAULT_ENTRY_TIMEOUT_SEC = 180;
inline constexpr std::uint32_t DEFAULT_POST_BOSS_LOOT_GRACE_SEC = 20;
inline constexpr std::uint32_t DEFAULT_MAX_COOP_WIPES = 3;
inline constexpr std::uint32_t DEFAULT_WIPE_RECOVERY_TIMEOUT_SEC = 300;
inline constexpr std::uint32_t DEFAULT_STALL_TIMEOUT_SEC = 600;
inline constexpr std::uint32_t DEFAULT_COMBAT_STALL_CAP_SEC = 300;
inline constexpr std::uint32_t DEFAULT_EXIT_HARD_CAP_SEC = 120;
inline constexpr std::uint32_t DEFAULT_EXIT_RETRY_SEC = 15;
inline constexpr float WAYPOINT_ARRIVAL_DIST = 14.0f;
inline constexpr std::uint32_t EXPECTED_COOP_PARTY_SIZE = 5;

enum class RunPhase : std::uint8_t
{
    WaitingForGroupEntry = 0,  // Waiting for all 5 native LFG members to load into the target dungeon map
    Traversing = 1,            // Active dungeon clear (pulling packs + advancing along verified wing waypoints)
    WipeRecovery = 2,          // Party wiped; bounded recovery back to all-alive in dungeon
    CompletedLootGrace = 3,    // Final boss dead / LFG finished; brief out-of-combat grace for real boss loot
    CompletedExitPending = 4,  // Ready to leave natively (TeleportToEntryPoint) & persist state='completed'
    AbortedExitPending = 5,    // Bounded failure (entry timeout / max wipes / stall): exit cleanly & persist 'aborted'
};

enum class CompletionSource : std::uint8_t
{
    None = 0,
    FinalBossKill = 1,
    LfgFinished = 2,
};

struct DungeonWaypoint
{
    float x = 0.0f;
    float y = 0.0f;
    float z = 0.0f;
    std::uint32_t bossEntry = 0;  // Optional boss creature_template entry at this waypoint (0 = corridor)
    char const* label = "";
};

struct DungeonRouteSpec
{
    std::uint32_t mapId = 0;
    std::uint32_t finalBossEntry = 0;
    char const* slug = "";
    std::vector<DungeonWaypoint> waypoints;
};

// Verified real AzerothCore 3.3.5a creature.sql coordinates for Phase II pilot stock dungeons (matching the exact
// Issue #20 supported dungeon allowlist: Stockade map 34 & Deadmines map 36). Unsupported maps fail closed!
inline DungeonRouteSpec const* LookupDungeonRoute(std::uint32_t mapId)
{
    static DungeonRouteSpec const kStockade = {
        34,
        1716,  // Bazil Thredd (final boss)
        "stockade_n",
        {
            {159.582f, 1.25311f, -25.6062f, 1696, "Targorr the Dread"},
            {166.789f, 134.787f, -33.9396f, 1663, "Dextren Ward"},
            {142.644f, -71.8536f, -34.8562f, 1666, "Kam Deepfury"},
            {89.5518f, -136.922f, -33.9396f, 1716, "Bazil Thredd"},
        },
    };
    static DungeonRouteSpec const kDeadmines = {
        36,
        639,  // Edwin VanCleef (final boss, instance_encounters lastEncounterDungeon=6)
        "deadmines_n",
        {
            {-192.915f, -448.211f, 54.4339f, 644, "Rhahk'Zor"},
            {-289.453f, -513.009f, 49.6785f, 643, "Sneed"},
            {-177.362f, -574.458f, 19.3113f, 1763, "Gilnid"},
            {-22.8471f, -797.283f, 20.3745f, 646, "Mr. Smite"},
            {-59.6200f, -820.132f, 41.6134f, 647, "Captain Greenskin"},
            {-87.3690f, -819.895f, 39.3004f, 639, "Edwin VanCleef"},
        },
    };
    if (mapId == 34)
        return &kStockade;
    if (mapId == 36)
        return &kDeadmines;
    return nullptr;
}

struct MemberControlInput
{
    std::uint32_t botGuid = 0;
    std::uint64_t opportunityId = 0;
    std::uint32_t groupId = 0;
    std::uint32_t targetMapId = 0;
    std::uint32_t lfgDungeonId = 0;              // Picks the wing on multi-wing maps (0 = unknown)
    bool isLfgGroup = false;
    bool groupedWithOwner = false;
    bool hasHumanMember = false;
    bool botInTargetDungeon = false;
    bool botInCombat = false;
    bool botDead = false;
    bool groupInCombat = false;
    std::uint32_t totalMemberSlots = 0;
    std::uint32_t nativeLeaderGuid = 0;
    std::uint32_t electedTankLeadGuid = 0;       // Alive map-local tank elected by CompanionLead::GroupLead (0 if none)
    std::vector<std::uint32_t> memberSlotGuids;  // Current group member low GUIDs (from Group::GetMemberSlots)
};

struct MemberSelfReport
{
    bool inTargetDungeon = false;
    bool alive = true;
    bool inCombat = false;
    std::uint32_t updatedAt = 0;
};

enum class MemberAction : std::uint8_t
{
    Ignore = 0,         // Human/owner group or invalid/missing run
    ContinueRun = 1,    // Stay in COMPANION_COOPERATIVE and apply leader/follower strategies
    ExitCompleted = 2,  // Final boss killed / LFG finished + loot grace elapsed + safe boundary -> LeaveOrDisbandGroup!
    ExitAborted = 3,    // Bounded failure (entry timeout / max wipes / stall) at safe boundary -> LeaveOrDisbandGroup!
};

struct MemberControlDecision
{
    MemberAction action = MemberAction::Ignore;
    RunPhase phase = RunPhase::WaitingForGroupEntry;
    bool isEffectiveLeader = false;
    std::uint32_t effectiveLeaderGuid = 0;
    bool wantFollowStrategy = false;
    bool wantLootStrategy = true;
    bool stripSoloMovement = true;
    bool allowNewPulls = false;
    bool leaderChanged = false;
    std::uint64_t opportunityId = 0;
    std::string outcomeState;   // "completed" or "aborted" when exiting
    std::string outcomeReason;  // compact <= 64 char token safe for both reason_code(64) & outcome_reason(120)
};

struct RunProgressRecord
{
    std::uint64_t opportunityId = 0;
    std::uint32_t groupId = 0;
    std::uint32_t targetMapId = 0;
    std::uint32_t lfgDungeonId = 0;
    RunPhase phase = RunPhase::WaitingForGroupEntry;
    std::uint32_t entryClockStartedAt = 0;  // Starts ONLY once LFG group formed or any member enters dungeon (P1-5)
    std::uint32_t enteredDungeonAt = 0;
    std::uint32_t lastProgressAt = 0;
    std::uint32_t combatStartedAt = 0;      // Bounds stuck-combat progress refresh (P1-3)
    std::uint32_t terminalEnteredAt = 0;    // Bounds ExitCompleted / ExitAborted hard cap (P1-3)
    std::uint32_t lastAuthoritativeDungeonLeader = 0;
    std::uint32_t leaderFailoverCount = 0;
    std::unordered_map<std::uint32_t, std::uint32_t> lastLeaderByMember;  // P0-3: per-member leader prevents flapping!
    std::size_t waypointIndex = 0;
    bool lastAllAliveInDungeon = false;

    // Real in-instance combat, boss kill, and loot evidence (NEVER fabricated!)
    std::uint32_t creatureKills = 0;
    std::unordered_set<std::uint32_t> killedBossEntries;
    std::uint32_t finalBossEntryKilled = 0;
    CompletionSource completionSource = CompletionSource::None;
    bool lfgDungeonFinished = false;
    std::uint32_t completedAt = 0;
    std::uint32_t lootItemCount = 0;
    std::vector<std::uint32_t> lootItemSample;  // Bounded sample (max 16) for audit logging

    // Bounded wipe tracking (1 wipe per all-dead episode)
    std::uint32_t wipeCount = 0;
    bool inActiveWipeEpisode = false;
    std::uint32_t wipeStartedAt = 0;
    std::string abortReason;

    // Per-member self-reported status (thread-safe across map boundaries, including ghosts at outside graveyards!)
    std::unordered_map<std::uint32_t, MemberSelfReport> memberReports;

    // Per-member exit retry pacing so LeaveOrDisbandGroup isn't spammed every tick
    std::unordered_map<std::uint32_t, std::uint32_t> exitInitiatedAtByMember;
    bool completionDbQueued = false;
};

struct LifecycleConfig
{
    std::uint32_t entryTimeoutSec = DEFAULT_ENTRY_TIMEOUT_SEC;
    std::uint32_t postBossLootGraceSec = DEFAULT_POST_BOSS_LOOT_GRACE_SEC;
    std::uint32_t maxWipes = DEFAULT_MAX_COOP_WIPES;
    std::uint32_t wipeRecoveryTimeoutSec = DEFAULT_WIPE_RECOVERY_TIMEOUT_SEC;
    std::uint32_t stallTimeoutSec = DEFAULT_STALL_TIMEOUT_SEC;
    std::uint32_t combatStallCapSec = DEFAULT_COMBAT_STALL_CAP_SEC;
    std::uint32_t exitHardCapSec = DEFAULT_EXIT_HARD_CAP_SEC;
    std::uint32_t exitRetrySec = DEFAULT_EXIT_RETRY_SEC;
};

// Compact, SQL-safe (alphanumeric + ':' + '_' + '-', strictly <= 64 chars) honest completion token (P1-2 & Must-Verify #8).
inline std::string FormatCompletionOutcomeReason(RunProgressRecord const& rec)
{
    if (rec.completionSource == CompletionSource::FinalBossKill && rec.finalBossEntryKilled != 0)
    {
        return "boss_killed:e" + std::to_string(rec.finalBossEntryKilled) +
               ":b" + std::to_string(rec.killedBossEntries.size()) +
               ":l" + std::to_string(rec.lootItemCount);
    }
    return "lfg_finished:k" + std::to_string(rec.creatureKills) +
           ":l" + std::to_string(rec.lootItemCount);
}

class LifecycleController
{
public:
    bool HasActiveRuns() const { return !_runs.empty(); }

    void RegisterOrRefreshRun(std::uint64_t opportunityId, std::uint32_t groupId, std::uint32_t targetMapId,
                              std::uint32_t nowSec, bool startEntryClock = false,
                              std::uint32_t lfgDungeonId = 0)
    {
        if (opportunityId == 0 || groupId == 0 || targetMapId == 0)
            return;
        auto& rec = _runs[opportunityId];
        if (rec.opportunityId == 0)
        {
            rec.opportunityId = opportunityId;
            rec.groupId = groupId;
            rec.targetMapId = targetMapId;
            rec.phase = RunPhase::WaitingForGroupEntry;
            rec.entryClockStartedAt = startEntryClock ? nowSec : 0;
            rec.lastProgressAt = nowSec;
        }
        else
        {
            if (rec.groupId != 0 && rec.groupId != groupId)
                _oppByGroup.erase(rec.groupId);
            rec.groupId = groupId;
            rec.targetMapId = targetMapId;
            if (startEntryClock && rec.entryClockStartedAt == 0)
                rec.entryClockStartedAt = nowSec;
        }
        if (lfgDungeonId != 0)
            rec.lfgDungeonId = lfgDungeonId;
        _oppByGroup[groupId] = opportunityId;
    }

    // Records real creature kill in an active cooperative dungeon instance (zero bypassed encounters!).
    // Returns true if this kill completed the dungeon's final boss.
    bool RecordCreatureKill(std::uint32_t groupId, std::uint32_t mapId, std::uint32_t creatureEntry,
                            bool isDungeonBoss, std::uint32_t nowSec)
    {
        auto git = _oppByGroup.find(groupId);
        if (git == _oppByGroup.end())
            return false;
        auto rit = _runs.find(git->second);
        if (rit == _runs.end())
            return false;
        auto& rec = rit->second;
        if (rec.targetMapId != mapId || creatureEntry == 0)
            return false;

        ++rec.creatureKills;
        rec.lastProgressAt = nowSec;
        rec.combatStartedAt = nowSec;  // Real kill resets stuck-combat stall timer!
        DungeonRouteSpec const* spec = LookupDungeonRoute(mapId);
        bool const isKnownRouteBoss = (spec && spec->finalBossEntry == creatureEntry) ||
                                      (spec && std::any_of(spec->waypoints.begin(), spec->waypoints.end(),
                                                           [creatureEntry](DungeonWaypoint const& wp) {
                                                               return wp.bossEntry == creatureEntry;
                                                           }));
        // Dungeons without a scripted route take their final boss from the supported-dungeon table. 0 (unknown
        // or ambiguous wing) leaves completion to the native LFG_STATE_FINISHED_DUNGEON observation.
        std::uint32_t const finalBoss =
            spec ? spec->finalBossEntry : CompanionLfgBridge::LookupDungeonFinalBoss(mapId, rec.lfgDungeonId);
        if (isDungeonBoss || isKnownRouteBoss)
            rec.killedBossEntries.insert(creatureEntry);

        if (finalBoss != 0 && creatureEntry == finalBoss)
        {
            rec.finalBossEntryKilled = creatureEntry;
            rec.completionSource = CompletionSource::FinalBossKill;
            if (rec.phase != RunPhase::CompletedLootGrace && rec.phase != RunPhase::CompletedExitPending &&
                rec.phase != RunPhase::AbortedExitPending)
            {
                rec.phase = RunPhase::CompletedLootGrace;
                rec.completedAt = nowSec;
            }
            return true;
        }
        return false;
    }

    // Records real loot acquired natively (direct loot OR group roll reward) inside the cooperative dungeon!
    bool RecordLootItem(std::uint32_t groupId, std::uint32_t mapId, std::uint32_t itemEntry, std::uint32_t count,
                        std::uint32_t nowSec)
    {
        auto git = _oppByGroup.find(groupId);
        if (git == _oppByGroup.end())
            return false;
        auto rit = _runs.find(git->second);
        if (rit == _runs.end())
            return false;
        auto& rec = rit->second;
        if (rec.targetMapId != mapId || itemEntry == 0 || count == 0)
            return false;
        rec.lootItemCount += count;
        if (rec.lootItemSample.size() < 16)
            rec.lootItemSample.push_back(itemEntry);
        rec.lastProgressAt = nowSec;
        return true;
    }

    // Called from WORLD THREAD when sLFGMgr->GetState(groupGuid) == LFG_STATE_FINISHED_DUNGEON.
    // P1-2: NEVER invents finalBossEntryKilled! Uses honest CompletionSource::LfgFinished if boss kill wasn't observed.
    bool ObserveLfgDungeonFinished(std::uint32_t groupId, std::uint32_t nowSec)
    {
        auto git = _oppByGroup.find(groupId);
        if (git == _oppByGroup.end())
            return false;
        auto rit = _runs.find(git->second);
        if (rit == _runs.end())
            return false;
        auto& rec = rit->second;
        bool const wasFinished = rec.lfgDungeonFinished;
        rec.lfgDungeonFinished = true;
        if (rec.creatureKills == 0 && rec.finalBossEntryKilled == 0)
            return false;  // Require real in-instance combat/kill evidence
        if (rec.completionSource == CompletionSource::None)
            rec.completionSource = CompletionSource::LfgFinished;
        if (rec.phase != RunPhase::CompletedLootGrace && rec.phase != RunPhase::CompletedExitPending &&
            rec.phase != RunPhase::AbortedExitPending)
        {
            rec.phase = RunPhase::CompletedLootGrace;
            rec.completedAt = nowSec;
            return true;
        }
        return !wasFinished;
    }

    // Records real player death from PlayerScript::OnPlayerKilledByCreature (works even after ghost release!).
    void RecordMemberDeath(std::uint32_t groupId, std::uint32_t botGuid, std::uint32_t nowSec)
    {
        auto git = _oppByGroup.find(groupId);
        if (git == _oppByGroup.end() || botGuid == 0)
            return;
        auto rit = _runs.find(git->second);
        if (rit == _runs.end())
            return;
        auto& rep = rit->second.memberReports[botGuid];
        rep.alive = false;
        rep.inCombat = false;
        rep.updatedAt = nowSec;
    }

    // Evaluates cooperative group readiness, leader/follower control, wipe recovery, loot grace, and native exit.
    MemberControlDecision EvaluateMember(MemberControlInput const& in, std::uint32_t nowSec,
                                         LifecycleConfig const& cfg = LifecycleConfig{})
    {
        MemberControlDecision dec;
        // P0-1: Strict owner-led / human-group isolation AND fail-closed on missing opportunityId/groupId/targetMapId!
        if (in.groupedWithOwner || in.hasHumanMember || in.botGuid == 0 || in.opportunityId == 0 || in.groupId == 0 ||
            in.targetMapId == 0)
            return dec;

        bool const shouldStartEntryClock = (in.isLfgGroup || in.botInTargetDungeon);
        RegisterOrRefreshRun(in.opportunityId, in.groupId, in.targetMapId, nowSec, shouldStartEntryClock,
                             in.lfgDungeonId);
        auto runIt = _runs.find(in.opportunityId);
        if (runIt == _runs.end())
            return dec;
        RunProgressRecord& rec = runIt->second;

        // Update this member's own self-report (ZERO cross-map-thread Player* dereferences!).
        rec.memberReports[in.botGuid] = MemberSelfReport{
            in.botInTargetDungeon,
            !in.botDead,
            in.botInCombat,
            nowSec,
        };

        // Prune stale reports for members no longer in the group
        if (!in.memberSlotGuids.empty())
        {
            std::unordered_set<std::uint32_t> liveSet(in.memberSlotGuids.begin(), in.memberSlotGuids.end());
            for (auto it = rec.memberReports.begin(); it != rec.memberReports.end();)
            {
                if (liveSet.count(it->first) == 0)
                    it = rec.memberReports.erase(it);
                else
                    ++it;
            }
        }

        // Aggregate party readiness / wipe / combat from fresh self-reports (within 30s).
        std::uint32_t const expectedMembers =
            !in.memberSlotGuids.empty() ? static_cast<std::uint32_t>(in.memberSlotGuids.size()) : in.totalMemberSlots;
        std::uint32_t onlineInDungeonCount = 0;
        std::uint32_t aliveInDungeonCount = 0;
        std::uint32_t deadMemberCount = 0;
        bool anyMemberInCombat = in.groupInCombat || in.botInCombat;
        for (auto const& [guid, rep] : rec.memberReports)
        {
            if (rep.updatedAt > 0 && nowSec > rep.updatedAt + 30)
                continue;
            if (rep.inCombat)
                anyMemberInCombat = true;
            if (!rep.alive)
                ++deadMemberCount;
            if (rep.inTargetDungeon)
            {
                ++onlineInDungeonCount;
                if (rep.alive)
                    ++aliveInDungeonCount;
            }
        }

        // P0-3: Per-member leader tracking prevents mixed in/out-of-dungeon flapping when native leader != tank!
        std::uint32_t const effectiveLeader =
            (in.botInTargetDungeon && in.electedTankLeadGuid != 0) ? in.electedTankLeadGuid : in.nativeLeaderGuid;
        if (effectiveLeader != 0)
        {
            auto lit = rec.lastLeaderByMember.find(in.botGuid);
            if (lit != rec.lastLeaderByMember.end() && lit->second != effectiveLeader)
                dec.leaderChanged = true;
            rec.lastLeaderByMember[in.botGuid] = effectiveLeader;

            // Count run-level failovers ONLY when in-dungeon authoritative leader changes!
            if (in.botInTargetDungeon)
            {
                if (rec.lastAuthoritativeDungeonLeader != 0 && rec.lastAuthoritativeDungeonLeader != effectiveLeader)
                    ++rec.leaderFailoverCount;
                rec.lastAuthoritativeDungeonLeader = effectiveLeader;
            }
        }

        dec.opportunityId = in.opportunityId;
        dec.effectiveLeaderGuid = effectiveLeader;
        dec.isEffectiveLeader = (effectiveLeader != 0 && effectiveLeader == in.botGuid);
        dec.wantFollowStrategy = !dec.isEffectiveLeader && (effectiveLeader != 0);
        dec.wantLootStrategy = true;
        dec.stripSoloMovement = true;

        bool const fullPartyInDungeon = (expectedMembers >= EXPECTED_COOP_PARTY_SIZE &&
                                         onlineInDungeonCount >= expectedMembers);
        bool const allAliveInDungeon = fullPartyInDungeon && (aliveInDungeonCount >= expectedMembers) &&
                                       (deadMemberCount == 0);
        bool const allDeadWipe = (expectedMembers >= EXPECTED_COOP_PARTY_SIZE &&
                                  deadMemberCount >= expectedMembers);
        rec.lastAllAliveInDungeon = allAliveInDungeon;

        // P1-3: Bound how long continuous combat without a kill can refresh lastProgressAt (prevents stuck-combat wedge!)
        if (anyMemberInCombat)
        {
            if (rec.combatStartedAt == 0)
                rec.combatStartedAt = nowSec;
            if (nowSec < rec.combatStartedAt + std::max<std::uint32_t>(30, cfg.combatStallCapSec))
                rec.lastProgressAt = nowSec;
        }
        else
        {
            rec.combatStartedAt = 0;
        }

        // 1. State transitions based on current phase
        if (rec.phase == RunPhase::WaitingForGroupEntry)
        {
            if (fullPartyInDungeon)
            {
                rec.phase = RunPhase::Traversing;
                rec.enteredDungeonAt = nowSec;
                rec.lastProgressAt = nowSec;
            }
            else if (rec.entryClockStartedAt > 0 &&
                     nowSec >= rec.entryClockStartedAt + std::max<std::uint32_t>(30, cfg.entryTimeoutSec))
            {
                rec.phase = RunPhase::AbortedExitPending;
                rec.abortReason = "entry_timeout";
                rec.terminalEnteredAt = nowSec;
            }
        }
        else if (rec.phase == RunPhase::Traversing)
        {
            if (allDeadWipe && !rec.inActiveWipeEpisode)
            {
                rec.inActiveWipeEpisode = true;
                ++rec.wipeCount;
                rec.wipeStartedAt = nowSec;
                if (rec.wipeCount >= std::max<std::uint32_t>(1, cfg.maxWipes))
                {
                    rec.phase = RunPhase::AbortedExitPending;
                    rec.abortReason = "wipe_limit_exceeded";
                    rec.terminalEnteredAt = nowSec;
                }
                else
                {
                    rec.phase = RunPhase::WipeRecovery;
                }
            }
            else if (nowSec >= rec.lastProgressAt + std::max<std::uint32_t>(60, cfg.stallTimeoutSec))
            {
                rec.phase = RunPhase::AbortedExitPending;
                rec.abortReason = "run_stalled";
                rec.terminalEnteredAt = nowSec;
            }
        }
        else if (rec.phase == RunPhase::WipeRecovery)
        {
            if (allAliveInDungeon)
            {
                rec.inActiveWipeEpisode = false;
                rec.wipeStartedAt = 0;
                rec.phase = RunPhase::Traversing;
                rec.lastProgressAt = nowSec;
            }
            else if (rec.wipeStartedAt > 0 &&
                     nowSec >= rec.wipeStartedAt + std::max<std::uint32_t>(30, cfg.wipeRecoveryTimeoutSec))
            {
                rec.phase = RunPhase::AbortedExitPending;
                rec.abortReason = "wipe_recovery_timeout";
                rec.terminalEnteredAt = nowSec;
            }
        }
        else if (rec.phase == RunPhase::CompletedLootGrace)
        {
            // P1-B: On native LFG groups, wait for core LFG_STATE_FINISHED_DUNGEON before leaving so members NEVER
            // trigger LFG_SPELL_DUNGEON_DESERTER on a successful boss clear (bounded by hardGraceCap!).
            bool const graceDone = (nowSec >= rec.completedAt + cfg.postBossLootGraceSec);
            bool const lfgSafe = (!in.isLfgGroup || rec.lfgDungeonFinished);
            bool const hardGraceCap = (nowSec >= rec.completedAt + cfg.postBossLootGraceSec +
                                                 std::max<std::uint32_t>(30, cfg.exitHardCapSec));
            if ((graceDone && lfgSafe && !anyMemberInCombat) || hardGraceCap)
            {
                rec.phase = RunPhase::CompletedExitPending;
                rec.terminalEnteredAt = nowSec;
            }
        }

        dec.phase = rec.phase;

        // 2. Exit decisions (Completed or Bounded Abort — safe boundary out of combat, with bounded hard cap P1-3!)
        if (rec.phase == RunPhase::CompletedExitPending || rec.phase == RunPhase::AbortedExitPending)
        {
            bool const isComp = (rec.phase == RunPhase::CompletedExitPending);
            dec.outcomeState = isComp ? "completed" : "aborted";
            dec.outcomeReason = isComp ? FormatCompletionOutcomeReason(rec) : rec.abortReason;
            dec.allowNewPulls = false;

            if (rec.terminalEnteredAt == 0)
                rec.terminalEnteredAt = nowSec;
            bool const exitHardCapped = (nowSec >= rec.terminalEnteredAt + std::max<std::uint32_t>(30, cfg.exitHardCapSec));

            // Respect safe boundary out of combat for BOTH Completed & Aborted exits until hard cap expires!
            bool const unsafeNow = isComp ? (anyMemberInCombat || in.botDead) : anyMemberInCombat;
            if (unsafeNow && !exitHardCapped)
            {
                dec.action = MemberAction::ContinueRun;
                return dec;
            }

            // Pace LeaveOrDisbandGroup retries so we don't spam CMSG_GROUP_DISBAND every tick
            auto eit = rec.exitInitiatedAtByMember.find(in.botGuid);
            if (eit != rec.exitInitiatedAtByMember.end() &&
                nowSec < eit->second + std::max<std::uint32_t>(1, cfg.exitRetrySec))
            {
                dec.action = MemberAction::ContinueRun;
                return dec;
            }
            rec.exitInitiatedAtByMember[in.botGuid] = nowSec ? nowSec : 1;
            dec.action = isComp ? MemberAction::ExitCompleted : MemberAction::ExitAborted;
            return dec;
        }

        // Pulls are allowed ONLY during Traversing when all 5 members are alive & in dungeon!
        dec.allowNewPulls = (rec.phase == RunPhase::Traversing && allAliveInDungeon);
        dec.action = MemberAction::ContinueRun;
        return dec;
    }

    // P2: Uses BOTH phase == Traversing AND lastAllAliveInDungeon so pulls halt immediately on any member death!
    bool IsGroupReadyForPull(std::uint32_t groupId) const
    {
        auto git = _oppByGroup.find(groupId);
        if (git == _oppByGroup.end())
            return false;
        auto rit = _runs.find(git->second);
        if (rit == _runs.end())
            return false;
        return rit->second.phase == RunPhase::Traversing && rit->second.lastAllAliveInDungeon;
    }

    // P2: Strictly READ-ONLY peek for Trigger::IsActive / Action::isUseful (zero state mutation!).
    bool PeekProgressionWaypoint(std::uint32_t groupId, std::uint32_t mapId, float curX, float curY, float curZ,
                                 float& outX, float& outY, float& outZ) const
    {
        auto git = _oppByGroup.find(groupId);
        if (git == _oppByGroup.end())
            return false;
        auto rit = _runs.find(git->second);
        if (rit == _runs.end())
            return false;
        RunProgressRecord const& rec = rit->second;
        if (rec.phase != RunPhase::Traversing || rec.targetMapId != mapId)
            return false;
        DungeonRouteSpec const* spec = LookupDungeonRoute(mapId);
        if (!spec || spec->waypoints.empty())
            return false;

        std::size_t idx = rec.waypointIndex;
        while (idx < spec->waypoints.size())
        {
            DungeonWaypoint const& wp = spec->waypoints[idx];
            if (wp.bossEntry != 0 && rec.killedBossEntries.count(wp.bossEntry) > 0)
            {
                ++idx;
                continue;
            }
            float const dx = wp.x - curX;
            float const dy = wp.y - curY;
            float const dz = wp.z - curZ;
            if (std::sqrt(dx * dx + dy * dy + dz * dz) <= WAYPOINT_ARRIVAL_DIST)
            {
                ++idx;
                continue;
            }
            outX = wp.x;
            outY = wp.y;
            outZ = wp.z;
            return true;
        }
        return false;
    }

    // Mutating advancement called ONLY from CompanionLeadPullAction::Execute!
    bool NextProgressionWaypoint(std::uint32_t groupId, std::uint32_t mapId, float curX, float curY, float curZ,
                                 std::uint32_t nowSec, float& outX, float& outY, float& outZ)
    {
        auto git = _oppByGroup.find(groupId);
        if (git == _oppByGroup.end())
            return false;
        auto rit = _runs.find(git->second);
        if (rit == _runs.end())
            return false;
        RunProgressRecord& rec = rit->second;
        if (rec.phase != RunPhase::Traversing || rec.targetMapId != mapId)
            return false;

        DungeonRouteSpec const* spec = LookupDungeonRoute(mapId);
        if (!spec || spec->waypoints.empty())
            return false;

        while (rec.waypointIndex < spec->waypoints.size())
        {
            DungeonWaypoint const& wp = spec->waypoints[rec.waypointIndex];
            if (wp.bossEntry != 0 && rec.killedBossEntries.count(wp.bossEntry) > 0)
            {
                ++rec.waypointIndex;
                rec.lastProgressAt = nowSec;
                continue;
            }
            float const dx = wp.x - curX;
            float const dy = wp.y - curY;
            float const dz = wp.z - curZ;
            if (std::sqrt(dx * dx + dy * dy + dz * dz) <= WAYPOINT_ARRIVAL_DIST)
            {
                ++rec.waypointIndex;
                rec.lastProgressAt = nowSec;
                continue;
            }
            outX = wp.x;
            outY = wp.y;
            outZ = wp.z;
            return true;
        }
        return false;
    }

    // Marks the run's DB terminal outcome write as queued (once per opportunity).
    bool ClaimTerminalDbWrite(std::uint64_t opportunityId, std::string& outState, std::string& outReason)
    {
        auto it = _runs.find(opportunityId);
        if (it == _runs.end() || it->second.completionDbQueued)
            return false;
        if (it->second.phase == RunPhase::CompletedExitPending)
        {
            it->second.completionDbQueued = true;
            outState = "completed";
            outReason = FormatCompletionOutcomeReason(it->second);
            return true;
        }
        if (it->second.phase == RunPhase::AbortedExitPending)
        {
            it->second.completionDbQueued = true;
            outState = "aborted";
            outReason = it->second.abortReason.empty() ? "coop_run_aborted" : it->second.abortReason;
            return true;
        }
        return false;
    }

    // P0-4: Force-claims any unclaimed CompletedLootGrace / CompletedExitPending / AbortedExitPending outcome
    // right before RemoveGroup on native group disband so a disband during loot grace NEVER leaves DB stuck in 'active'!
    bool ForceClaimTerminalOnDisband(std::uint32_t groupId, std::uint64_t& outOppId, std::string& outState,
                                     std::string& outReason)
    {
        outOppId = 0;
        outState.clear();
        outReason.clear();
        auto git = _oppByGroup.find(groupId);
        if (git == _oppByGroup.end())
            return false;
        auto rit = _runs.find(git->second);
        if (rit == _runs.end())
            return false;
        auto& rec = rit->second;
        if (rec.phase == RunPhase::CompletedLootGrace)
            rec.phase = RunPhase::CompletedExitPending;
        if (rec.completionDbQueued)
            return false;
        if (rec.phase == RunPhase::CompletedExitPending)
        {
            rec.completionDbQueued = true;
            outOppId = rec.opportunityId;
            outState = "completed";
            outReason = FormatCompletionOutcomeReason(rec);
            return true;
        }
        if (rec.phase == RunPhase::AbortedExitPending)
        {
            rec.completionDbQueued = true;
            outOppId = rec.opportunityId;
            outState = "aborted";
            outReason = rec.abortReason.empty() ? "coop_run_aborted" : rec.abortReason;
            return true;
        }
        return false;
    }

    bool IsCompletedGroup(std::uint32_t groupId, std::uint64_t* outOppId = nullptr) const
    {
        auto git = _oppByGroup.find(groupId);
        if (git == _oppByGroup.end())
            return false;
        auto rit = _runs.find(git->second);
        if (rit == _runs.end())
            return false;
        if (rit->second.phase == RunPhase::CompletedLootGrace ||
            rit->second.phase == RunPhase::CompletedExitPending)
        {
            if (outOppId)
                *outOppId = rit->second.opportunityId;
            return true;
        }
        return false;
    }

    std::vector<std::uint32_t> ActiveGroupIds() const
    {
        std::vector<std::uint32_t> out;
        out.reserve(_runs.size());
        for (auto const& [_, rec] : _runs)
        {
            if (rec.groupId != 0 && !rec.lfgDungeonFinished && rec.phase != RunPhase::CompletedExitPending &&
                rec.phase != RunPhase::AbortedExitPending)
                out.push_back(rec.groupId);
        }
        return out;
    }

    // P2: Sweep runs whose native group no longer exists (prevents unbounded growth if a disband hook was missed!)
    std::size_t SweepOrphanedGroups(std::unordered_set<std::uint32_t> const& liveGroupIds)
    {
        std::size_t removed = 0;
        for (auto it = _runs.begin(); it != _runs.end();)
        {
            if (it->second.groupId == 0 || liveGroupIds.count(it->second.groupId) == 0)
            {
                _oppByGroup.erase(it->second.groupId);
                it = _runs.erase(it);
                ++removed;
            }
            else
            {
                ++it;
            }
        }
        return removed;
    }

    RunProgressRecord const* GetRun(std::uint64_t opportunityId) const
    {
        auto it = _runs.find(opportunityId);
        return (it != _runs.end()) ? &it->second : nullptr;
    }

    void RemoveGroup(std::uint32_t groupId)
    {
        auto git = _oppByGroup.find(groupId);
        if (git == _oppByGroup.end())
            return;
        _runs.erase(git->second);
        _oppByGroup.erase(git);
    }

    void Clear()
    {
        _runs.clear();
        _oppByGroup.clear();
    }

private:
    std::unordered_map<std::uint64_t, RunProgressRecord> _runs;
    std::unordered_map<std::uint32_t, std::uint64_t> _oppByGroup;
};

}  // namespace CompanionCoopLifecycle

#endif
