/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANION_COOP_DRAIN_LOGIC_H
#define PLAYERBOTS_COMPANION_COOP_DRAIN_LOGIC_H

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

// Pure C++17 cooperative drain, safe owner recall, shift-end, and DB/worker outage state machine (Issue #22).
// Separates admission mode (Off / Draining / Enabled) from accepted native group ownership so toggling coop OFF,
// hitting run expiry, owner recall, shift end, or transient DB/worker outages NEVER arbitrarily disband a group
// mid-pull or fabricate failure memories — committed runs finish combat safely (within bounded grace) and release
// commitments ONLY when actual native group exit / disband occurs (or orphaned groups are swept).
namespace CompanionCoopDrain
{
inline constexpr std::uint32_t DEFAULT_DRAIN_GRACE_SEC = 600;
inline constexpr std::uint32_t DEFAULT_OUTAGE_GRACE_SEC = 180;
inline constexpr std::uint32_t DEFAULT_EXIT_RETRY_SEC = 15;
inline constexpr std::uint32_t DEFAULT_EXIT_STUCK_DEADLINE_SEC = 60;

// WotLK 3.3.5a LFG penalty spell IDs (LFGMgr.h)
inline constexpr std::uint32_t SPELL_LFG_DUNGEON_DESERTER = 71041;
inline constexpr std::uint32_t SPELL_LFG_DUNGEON_COOLDOWN = 71328;

enum class CoopMode : std::uint8_t
{
    Off = 0,
    Draining = 1,
    Enabled = 2,
};

inline CoopMode ParseCoopMode(bool enabledFlag, std::string const& modeRaw)
{
    std::string m;
    m.reserve(modeRaw.size());
    for (unsigned char c : modeRaw)
    {
        if (!std::isspace(c))
            m.push_back(static_cast<char>(std::tolower(c)));
    }
    if (m == "draining" || m == "drain")
        return CoopMode::Draining;
    if (m == "enabled" || m == "on" || m == "1" || m == "true" || m == "yes")
        return CoopMode::Enabled;
    if (m == "off" || m == "disabled" || m == "0" || m == "false" || m == "no")
        return CoopMode::Off;
    return enabledFlag ? CoopMode::Enabled : CoopMode::Off;
}

inline bool AllowsNewAdmissions(CoopMode mode)
{
    return mode == CoopMode::Enabled;
}

enum class DrainReason : std::uint8_t
{
    None = 0,
    ToggleOff = 1,
    DrainingMode = 2,
    RunExpired = 3,
    OwnerRecall = 4,
    ShiftEnd = 5,
    WorkerOutage = 6,
    MemberDeparted = 7,
};

inline char const* DrainOutcomeTag(DrainReason reason)
{
    switch (reason)
    {
        case DrainReason::None:
            return "";
        case DrainReason::ToggleOff:
            return "coop_toggled_off";
        case DrainReason::DrainingMode:
            return "coop_draining";
        case DrainReason::RunExpired:
            return "run_expired_drained";
        case DrainReason::OwnerRecall:
            return "owner_recall";
        case DrainReason::ShiftEnd:
            return "shift_end";
        case DrainReason::WorkerOutage:
            return "worker_outage_drained";
        case DrainReason::MemberDeparted:
            return "member_departed_drained";
    }
    return "coop_drained";
}

// Operational drains, recalls, shift-ends, toggle-offs, and DB/worker outages NEVER fabricate failure memories!
inline bool ShouldFabricateFailureMemory(DrainReason /*reason*/)
{
    return false;
}

enum class DrainAction : std::uint8_t
{
    NotOwned = 0,
    ContinueActive = 1,
    HoldInCombat = 2,
    ExitAtSafeBoundary = 3,
    ExitPending = 4,  // LeaveOrDisbandGroup() recently queued; awaiting actual OnRemoveMember/OnDisband event
};

struct ExitInitiatedInfo
{
    DrainReason reason = DrainReason::None;
    std::uint32_t firstInitiatedAt = 0;
    std::uint32_t lastSentAt = 0;
};

struct CommittedGroupOwnership
{
    std::uint64_t opportunityId = 0;
    std::uint32_t revision = 0;
    std::uint32_t groupId = 0;
    std::uint64_t groupToken = 0;
    std::uint32_t targetMapId = 0;
    bool isLfg = false;
    std::uint32_t committedAt = 0;
    std::uint32_t expiresAt = 0;
    // Group-wide drain reason & bounded grace deadline (NEVER set by a single member recall/shift-end!).
    DrainReason groupDrainReason = DrainReason::None;
    std::uint32_t drainStartedAt = 0;
    std::uint32_t drainDeadline = 0;
    std::unordered_set<std::uint32_t> memberGuids;
    std::unordered_map<std::uint32_t, DrainReason> memberDrainReasons;
    std::unordered_map<std::uint32_t, std::uint32_t> memberDrainDeadlines;
    std::unordered_map<std::uint32_t, ExitInitiatedInfo> exitInitiatedByMember;
};

struct MemberBoundaryInput
{
    std::uint32_t botGuid = 0;
    std::uint32_t groupId = 0;
    std::uint64_t groupToken = 0;
    bool hasHumanMember = false;    // owner OR any non-companion human player
    bool botInCombat = false;
    bool groupInCombat = false;
    bool botDead = false;
    bool inDungeonInstance = false;
    bool isLfgGroup = false;
    bool lfgDungeonFinished = false;
    bool hasLfgDeserter = false;    // SPELL_LFG_DUNGEON_DESERTER (71041)
    bool hasLfgCooldown = false;    // SPELL_LFG_DUNGEON_COOLDOWN (71328)
    bool shiftEnded = false;
    bool ownerRecalled = false;
};

struct DrainDecision
{
    DrainAction action = DrainAction::NotOwned;
    DrainReason reason = DrainReason::None;
    std::uint64_t opportunityId = 0;
    bool forcedByGraceDeadline = false;
    bool retryExit = false;
    bool exitStuckExceeded = false;
    bool expectLfgDeserterPenalty = false;
    bool hasExistingLfgPenalty = false;
    bool fabricateFailureMemory = false;  // ALWAYS false (Issue #22 invariant)
    std::string outcomeTag;
};

struct DisbandDrainResult
{
    std::uint64_t opportunityId = 0;
    bool wasDraining = false;
    DrainReason reason = DrainReason::None;
    std::vector<std::uint32_t> remainingMembers;
};

struct StaleGroupCleanup
{
    std::uint64_t opportunityId = 0;
    std::uint32_t groupId = 0;
    bool needsDbDrainWrite = false;  // true if group vanished/overran while DB row was still active/assembling
    DrainReason reason = DrainReason::None;
    std::vector<std::uint32_t> remainingMembers;
};

class DrainCoordinator
{
public:
    // Registers (or refreshes) accepted native group ownership. Independent of admission mode!
    // Clears any stale pending recall for newly committed members so past recalls never poison a new run.
    void RegisterCommittedGroup(std::uint64_t opportunityId, std::uint32_t revision, std::uint32_t groupId,
                                std::uint64_t groupToken, std::uint32_t targetMapId, bool isLfg,
                                std::vector<std::uint32_t> const& memberGuids, std::uint32_t nowSec,
                                std::uint32_t expiresAt)
    {
        if (opportunityId == 0 || groupId == 0 || memberGuids.empty())
            return;
        auto& rec = _ownedByOpp[opportunityId];
        bool const isNew = (rec.opportunityId == 0);
        rec.opportunityId = opportunityId;
        rec.revision = revision;
        rec.groupId = groupId;
        if (groupToken != 0)
            rec.groupToken = groupToken;
        rec.targetMapId = targetMapId;
        rec.isLfg = rec.isLfg || isLfg;
        if (isNew)
            rec.committedAt = nowSec;
        if (expiresAt > 0)
            rec.expiresAt = expiresAt;
        if (isNew || rec.memberGuids.empty())
        {
            rec.memberGuids.clear();
            for (std::uint32_t g : memberGuids)
            {
                if (g != 0)
                {
                    rec.memberGuids.insert(g);
                    if (isNew)
                        _pendingRecalls.erase(g);
                }
            }
        }
    }

    bool HasOwnedGroups() const { return !_ownedByOpp.empty(); }
    std::size_t OwnedGroupCount() const { return _ownedByOpp.size(); }

    bool IsOwnedOpportunity(std::uint64_t opportunityId) const
    {
        return _ownedByOpp.find(opportunityId) != _ownedByOpp.end();
    }

    std::vector<std::uint64_t> OwnedOpportunityIds() const
    {
        std::vector<std::uint64_t> out;
        out.reserve(_ownedByOpp.size());
        for (auto const& [oppId, _] : _ownedByOpp)
            out.push_back(oppId);
        return out;
    }

    std::vector<std::uint32_t> OwnedGroupIds() const
    {
        std::vector<std::uint32_t> out;
        out.reserve(_ownedByOpp.size());
        for (auto const& [_, rec] : _ownedByOpp)
            if (rec.groupId != 0)
                out.push_back(rec.groupId);
        return out;
    }

    // Pure read-only check (used by const IsInAuthorizedCoopGroup): true ONLY while an owned committed group is
    // actively draining/holding through combat or awaiting queued exit, without mutating state or bypassing
    // Issue #11's exact-roster checks for healthy active runs!
    bool IsHoldingForDrain(std::uint32_t botGuid, std::uint32_t groupId, std::uint64_t groupToken,
                           bool hasHumanMember, CoopMode mode, std::uint32_t nowSec) const
    {
        if (hasHumanMember || botGuid == 0 || groupId == 0)
            return false;
        CommittedGroupOwnership const* owned = FindOwnedGroupConst(botGuid, groupId, groupToken);
        if (!owned)
            return false;
        if (owned->exitInitiatedByMember.count(botGuid) > 0)
            return true;
        if (owned->groupDrainReason != DrainReason::None)
            return true;
        if (owned->memberDrainReasons.count(botGuid) > 0 || _pendingRecalls.count(botGuid) > 0)
            return true;
        if (mode != CoopMode::Enabled)
            return true;
        if (owned->expiresAt > 0 && nowSec >= owned->expiresAt)
            return true;
        return false;
    }

    // Triggers non-disruptive drain on ALL committed groups when admission mode flips to Off or Draining.
    void TriggerGlobalDrain(CoopMode mode, std::uint32_t nowSec,
                            std::uint32_t drainGraceSec = DEFAULT_DRAIN_GRACE_SEC)
    {
        if (mode == CoopMode::Enabled)
            return;
        DrainReason const reason = (mode == CoopMode::Draining) ? DrainReason::DrainingMode : DrainReason::ToggleOff;
        for (auto& [oppId, rec] : _ownedByOpp)
        {
            if (rec.groupDrainReason == DrainReason::None)
            {
                rec.groupDrainReason = reason;
                rec.drainStartedAt = nowSec;
                rec.drainDeadline = nowSec + std::max<std::uint32_t>(1, drainGraceSec);
            }
        }
    }

    // Records an explicit owner recall ONLY for a companion currently in an owned cooperative group!
    // Uses a PER-MEMBER deadline (NEVER sets the group-level drainDeadline, so an offline/stuck recalled member
    // can never sweep the remaining 4 healthy members out of ownership!).
    bool RequestOwnerRecall(std::uint32_t botGuid, std::uint32_t nowSec,
                            std::uint32_t drainGraceSec = DEFAULT_DRAIN_GRACE_SEC)
    {
        if (botGuid == 0)
            return false;
        bool matchedOwned = false;
        std::uint32_t const deadline = nowSec + std::max<std::uint32_t>(1, drainGraceSec);
        for (auto& [oppId, rec] : _ownedByOpp)
        {
            if (rec.memberGuids.count(botGuid) > 0)
            {
                matchedOwned = true;
                rec.memberDrainReasons[botGuid] = DrainReason::OwnerRecall;
                if (rec.memberDrainDeadlines.find(botGuid) == rec.memberDrainDeadlines.end())
                    rec.memberDrainDeadlines[botGuid] = deadline;
            }
        }
        if (matchedOwned)
            _pendingRecalls.insert(botGuid);
        return matchedOwned;
    }

    void ClearOwnerRecall(std::uint32_t botGuid) { _pendingRecalls.erase(botGuid); }

    bool HasPendingRecall(std::uint32_t botGuid) const { return _pendingRecalls.count(botGuid) > 0; }

    // Tracks transient DB / worker outages without wiping active committed group ownership!
    // Only once outage persists longer than outageGraceSec do committed groups enter WorkerOutage drain.
    void NoteDbOutage(std::uint32_t nowSec, std::uint32_t outageGraceSec = DEFAULT_OUTAGE_GRACE_SEC,
                      std::uint32_t drainGraceSec = DEFAULT_DRAIN_GRACE_SEC)
    {
        if (_outageSinceSec == 0)
            _outageSinceSec = nowSec;
        if (nowSec >= _outageSinceSec && (nowSec - _outageSinceSec) >= outageGraceSec)
        {
            for (auto& [oppId, rec] : _ownedByOpp)
            {
                if (rec.groupDrainReason == DrainReason::None)
                {
                    rec.groupDrainReason = DrainReason::WorkerOutage;
                    rec.drainStartedAt = nowSec;
                    rec.drainDeadline = nowSec + std::max<std::uint32_t>(1, drainGraceSec);
                }
            }
        }
    }

    void NoteDbHealthy() { _outageSinceSec = 0; }
    bool InDbOutage() const { return _outageSinceSec != 0; }

    // Periodic world-thread sweep (independent of member ticks!) that removes owned groups ONLY when:
    // (a) native group no longer exists in sGroupMgr (`liveGroupIds`), OR
    // (b) a DIRECT DB query (`SELECT id, state WHERE id IN (...)`) confirmed the row is closed (`dbClosedOppIds`)
    //     and no pending exit remains queued, OR
    // (c) a GROUP-LEVEL drain (`groupDrainReason != None`) overran its bounded `drainDeadline + deadlineOverrunSec`!
    // NOTE: Never infers closure from a time-filtered/LIMIT-50 reload query, so RunExpired groups hold safely
    // through combat across reload boundaries!
    std::vector<StaleGroupCleanup> SweepOrphanedOrExpiredGroups(
        std::unordered_set<std::uint64_t> const& dbClosedOppIds,
        std::unordered_set<std::uint32_t> const& liveGroupIds,
        std::unordered_set<std::uint64_t> const& pendingExitOppIds,
        std::uint32_t nowSec,
        std::uint32_t deadlineOverrunSec = DEFAULT_EXIT_STUCK_DEADLINE_SEC)
    {
        std::vector<StaleGroupCleanup> cleaned;
        for (auto it = _ownedByOpp.begin(); it != _ownedByOpp.end();)
        {
            std::uint64_t const oppId = it->first;
            CommittedGroupOwnership const& rec = it->second;
            bool const nativeGroupGone = (liveGroupIds.count(rec.groupId) == 0);
            bool const dbRowClosed = (dbClosedOppIds.count(oppId) > 0 && pendingExitOppIds.count(oppId) == 0);
            bool const groupDeadlineOverrun =
                (rec.groupDrainReason != DrainReason::None && rec.drainDeadline > 0 &&
                 nowSec >= rec.drainDeadline + deadlineOverrunSec);

            if (!nativeGroupGone && !dbRowClosed && !groupDeadlineOverrun)
            {
                ++it;
                continue;
            }

            StaleGroupCleanup item;
            item.opportunityId = oppId;
            item.groupId = rec.groupId;
            // Write neutral 'drained' cleanup ONLY when DB row is NOT already closed and group vanished/overran!
            item.needsDbDrainWrite = (dbClosedOppIds.count(oppId) == 0) && (nativeGroupGone || groupDeadlineOverrun);
            DrainReason r = rec.groupDrainReason;
            if (r == DrainReason::None && !rec.exitInitiatedByMember.empty())
                r = rec.exitInitiatedByMember.begin()->second.reason;
            if (r == DrainReason::None && !rec.memberDrainReasons.empty())
                r = rec.memberDrainReasons.begin()->second;
            if (r == DrainReason::None)
                r = DrainReason::RunExpired;
            item.reason = r;
            item.remainingMembers.assign(rec.memberGuids.begin(), rec.memberGuids.end());
            for (std::uint32_t g : rec.memberGuids)
                _pendingRecalls.erase(g);
            cleaned.push_back(std::move(item));
            it = _ownedByOpp.erase(it);
        }
        return cleaned;
    }

    // Evaluates whether a companion in a native group should continue active coop, hold through combat during
    // drain, exit cleanly at a safe boundary (or when bounded grace deadline expires), wait for an already-queued
    // leave packet (paced by exitRetrySec, respecting safeBoundary until stuck deadline!), or is not owned.
    DrainDecision EvaluateMember(CoopMode mode, MemberBoundaryInput const& in, std::uint32_t nowSec,
                                 std::uint32_t drainGraceSec = DEFAULT_DRAIN_GRACE_SEC,
                                 std::uint32_t exitRetrySec = DEFAULT_EXIT_RETRY_SEC,
                                 std::uint32_t exitStuckDeadlineSec = DEFAULT_EXIT_STUCK_DEADLINE_SEC)
    {
        DrainDecision dec;
        dec.fabricateFailureMemory = false;

        // Human-led or mixed-human groups are NEVER touched by cooperative drain!
        if (in.hasHumanMember || in.botGuid == 0 || in.groupId == 0)
            return dec;

        CommittedGroupOwnership* owned = FindOwnedGroup(in.botGuid, in.groupId, in.groupToken);
        if (!owned)
            return dec;

        dec.opportunityId = owned->opportunityId;
        dec.hasExistingLfgPenalty = (in.hasLfgDeserter || in.hasLfgCooldown);
        if (in.groupToken != 0 && owned->groupToken == 0)
            owned->groupToken = in.groupToken;
        if (in.isLfgGroup)
            owned->isLfg = true;

        bool const safeBoundary = (!in.botInCombat && !in.groupInCombat && !in.botDead);

        // If LeaveOrDisbandGroup() was already queued for this bot, wait for OnRemoveMember/OnDisband.
        // Retries are ALWAYS paced by exitRetrySec (never every tick!) and respect safeBoundary until
        // exitStuckDeadlineSec elapses so a bot pulled back into combat won't drop mid-pull prematurely!
        auto pendIt = owned->exitInitiatedByMember.find(in.botGuid);
        if (pendIt != owned->exitInitiatedByMember.end())
        {
            ExitInitiatedInfo const& info = pendIt->second;
            dec.reason = info.reason;
            dec.outcomeTag = DrainOutcomeTag(info.reason);
            bool const retryDue =
                (info.lastSentAt > 0 && nowSec >= info.lastSentAt + std::max<std::uint32_t>(1, exitRetrySec));
            bool const stuckExceeded =
                (info.firstInitiatedAt > 0 &&
                 nowSec >= info.firstInitiatedAt + std::max<std::uint32_t>(1, exitStuckDeadlineSec));
            if (retryDue)
            {
                if (!safeBoundary && !stuckExceeded)
                {
                    dec.action = DrainAction::HoldInCombat;
                    return dec;
                }
                dec.action = DrainAction::ExitAtSafeBoundary;
                dec.retryExit = true;
                dec.exitStuckExceeded = stuckExceeded;
                dec.forcedByGraceDeadline = (!safeBoundary && stuckExceeded);
                dec.expectLfgDeserterPenalty =
                    (in.inDungeonInstance && (in.isLfgGroup || owned->isLfg) && !in.lfgDungeonFinished && !in.hasLfgDeserter);
                return dec;
            }
            dec.action = DrainAction::ExitPending;
            return dec;
        }

        // Determine highest-priority drain reason (per-member recall/shift-end, then group drain / mode / expiry).
        DrainReason effReason = DrainReason::None;
        bool isMemberLevelDrain = false;
        if (in.ownerRecalled || _pendingRecalls.count(in.botGuid) > 0)
        {
            effReason = DrainReason::OwnerRecall;
            owned->memberDrainReasons[in.botGuid] = DrainReason::OwnerRecall;
            isMemberLevelDrain = true;
        }
        else
        {
            auto mit = owned->memberDrainReasons.find(in.botGuid);
            if (mit != owned->memberDrainReasons.end() && mit->second != DrainReason::None)
            {
                effReason = mit->second;
                isMemberLevelDrain = true;
            }
        }

        if (effReason == DrainReason::None && in.shiftEnded)
        {
            effReason = DrainReason::ShiftEnd;
            owned->memberDrainReasons[in.botGuid] = DrainReason::ShiftEnd;
            isMemberLevelDrain = true;
        }

        if (effReason == DrainReason::None)
        {
            if (owned->groupDrainReason != DrainReason::None)
            {
                effReason = owned->groupDrainReason;
            }
            else if (mode != CoopMode::Enabled)
            {
                effReason = (mode == CoopMode::Draining) ? DrainReason::DrainingMode : DrainReason::ToggleOff;
                owned->groupDrainReason = effReason;
            }
            else if (owned->expiresAt > 0 && nowSec >= owned->expiresAt)
            {
                effReason = DrainReason::RunExpired;
                owned->groupDrainReason = effReason;
            }
        }

        if (effReason == DrainReason::None)
        {
            dec.action = DrainAction::ContinueActive;
            return dec;
        }

        std::uint32_t effectiveDeadline = 0;
        if (isMemberLevelDrain)
        {
            auto& mDeadline = owned->memberDrainDeadlines[in.botGuid];
            if (mDeadline == 0)
                mDeadline = nowSec + std::max<std::uint32_t>(1, drainGraceSec);
            effectiveDeadline = mDeadline;
        }
        else
        {
            if (owned->drainStartedAt == 0)
            {
                owned->drainStartedAt = nowSec;
                owned->drainDeadline = nowSec + std::max<std::uint32_t>(1, drainGraceSec);
            }
            effectiveDeadline = owned->drainDeadline;
        }

        dec.reason = effReason;
        dec.outcomeTag = DrainOutcomeTag(effReason);
        dec.fabricateFailureMemory = ShouldFabricateFailureMemory(effReason);

        // Safe boundary gate with ENFORCED bounded grace deadline (Issue #22):
        // Hold through normal combat/wipe, but once effectiveDeadline elapses (e.g. stuck combat or permanent corpse),
        // force clean exit so drain and admission tokens can never wedge forever!
        bool const graceExpired = (effectiveDeadline > 0 && nowSec >= effectiveDeadline);
        if (!safeBoundary && !graceExpired)
        {
            dec.action = DrainAction::HoldInCombat;
            return dec;
        }

        dec.action = DrainAction::ExitAtSafeBoundary;
        dec.forcedByGraceDeadline = (!safeBoundary && graceExpired);
        dec.expectLfgDeserterPenalty =
            (in.inDungeonInstance && (in.isLfgGroup || owned->isLfg) && !in.lfgDungeonFinished && !in.hasLfgDeserter);
        return dec;
    }

    // Marks that LeaveOrDisbandGroup() was queued for botGuid (storing firstInitiatedAt + lastSentAt for bounded retry).
    bool MarkExitInitiated(std::uint64_t opportunityId, std::uint32_t botGuid, DrainReason reason, std::uint32_t nowSec)
    {
        auto it = _ownedByOpp.find(opportunityId);
        if (it == _ownedByOpp.end() || it->second.memberGuids.count(botGuid) == 0)
            return false;
        auto& info = it->second.exitInitiatedByMember[botGuid];
        info.reason = reason;
        if (info.firstInitiatedAt == 0)
            info.firstInitiatedAt = nowSec;
        info.lastSentAt = nowSec;
        return true;
    }

    // Called ONLY from GroupScript::OnRemoveMember when the bot ACTUALLY leaves the native group!
    // Classifies the removal as a drain (`outWasDrain=true`) ONLY if the group was already draining OR this member's
    // departure was initiated by drain logic (recall / shift-end / mode / expiry / outage). Healthy post-completion
    // or non-drain exits set `outWasDrain=false` so normal completion/disband outcomes are NEVER overwritten as 'drained'!
    bool OnActualMemberRemoved(std::uint32_t groupId, std::uint64_t groupToken, std::uint32_t botGuid,
                               std::uint32_t nowSec, std::uint32_t drainGraceSec, std::uint64_t& outOppId,
                               DrainReason& outReason, bool& outGroupFullyExited, bool& outWasDrain)
    {
        outOppId = 0;
        outReason = DrainReason::None;
        outGroupFullyExited = false;
        outWasDrain = false;
        _pendingRecalls.erase(botGuid);

        CommittedGroupOwnership* owned = FindOwnedGroup(botGuid, groupId, groupToken);
        if (!owned)
            return false;

        outOppId = owned->opportunityId;
        bool const memberInitiatedDrain =
            (owned->exitInitiatedByMember.count(botGuid) > 0 || owned->memberDrainReasons.count(botGuid) > 0);
        bool const groupDraining = (owned->groupDrainReason != DrainReason::None);

        if (auto initIt = owned->exitInitiatedByMember.find(botGuid); initIt != owned->exitInitiatedByMember.end())
            outReason = initIt->second.reason;
        else if (auto mit = owned->memberDrainReasons.find(botGuid); mit != owned->memberDrainReasons.end())
            outReason = mit->second;
        else if (groupDraining)
            outReason = owned->groupDrainReason;

        outWasDrain = (memberInitiatedDrain || groupDraining);

        owned->memberGuids.erase(botGuid);
        owned->memberDrainReasons.erase(botGuid);
        owned->memberDrainDeadlines.erase(botGuid);
        owned->exitInitiatedByMember.erase(botGuid);

        if (owned->memberGuids.empty())
        {
            _ownedByOpp.erase(outOppId);
            outGroupFullyExited = true;
        }
        else if (memberInitiatedDrain && !groupDraining)
        {
            // A member left due to owner recall or shift-end -> transition remaining 4 members into safe
            // MemberDeparted drain so they finish combat cleanly instead of being disbanded mid-pull!
            owned->groupDrainReason = DrainReason::MemberDeparted;
            owned->drainStartedAt = nowSec;
            owned->drainDeadline = nowSec + std::max<std::uint32_t>(1, drainGraceSec);
        }
        return true;
    }

    // Called from GroupScript::OnDisband: preserves drain reason & all remaining member GUIDs ONLY when the group
    // was actually draining so auto-disband closes draining runs as 'drained' while leaving healthy/completed runs
    // to their normal completion/disband path!
    std::vector<DisbandDrainResult> OnGroupDisbanded(std::uint32_t groupId, std::uint64_t groupToken)
    {
        std::vector<DisbandDrainResult> results;
        if (groupId == 0)
            return results;
        for (auto it = _ownedByOpp.begin(); it != _ownedByOpp.end();)
        {
            bool const matchId = (it->second.groupId == groupId);
            bool const matchTok = (groupToken == 0 || it->second.groupToken == 0 || it->second.groupToken == groupToken);
            if (matchId && matchTok)
            {
                DisbandDrainResult res;
                res.opportunityId = it->first;
                DrainReason r = it->second.groupDrainReason;
                if (r == DrainReason::None && !it->second.exitInitiatedByMember.empty())
                    r = it->second.exitInitiatedByMember.begin()->second.reason;
                if (r == DrainReason::None && !it->second.memberDrainReasons.empty())
                    r = it->second.memberDrainReasons.begin()->second;
                res.wasDraining = (r != DrainReason::None);
                res.reason = r;
                res.remainingMembers.assign(it->second.memberGuids.begin(), it->second.memberGuids.end());
                for (std::uint32_t g : it->second.memberGuids)
                    _pendingRecalls.erase(g);
                results.push_back(std::move(res));
                it = _ownedByOpp.erase(it);
            }
            else
                ++it;
        }
        return results;
    }

private:
    CommittedGroupOwnership* FindOwnedGroup(std::uint32_t botGuid, std::uint32_t groupId, std::uint64_t groupToken)
    {
        for (auto& [oppId, rec] : _ownedByOpp)
        {
            if (rec.groupId != groupId)
                continue;
            if (groupToken != 0 && rec.groupToken != 0 && rec.groupToken != groupToken)
                continue;
            if (rec.memberGuids.count(botGuid) > 0)
                return &rec;
        }
        return nullptr;
    }

    CommittedGroupOwnership const* FindOwnedGroupConst(std::uint32_t botGuid, std::uint32_t groupId,
                                                       std::uint64_t groupToken) const
    {
        for (auto const& [oppId, rec] : _ownedByOpp)
        {
            if (rec.groupId != groupId)
                continue;
            if (groupToken != 0 && rec.groupToken != 0 && rec.groupToken != groupToken)
                continue;
            if (rec.memberGuids.count(botGuid) > 0)
                return &rec;
        }
        return nullptr;
    }

    std::unordered_map<std::uint64_t, CommittedGroupOwnership> _ownedByOpp;
    std::unordered_set<std::uint32_t> _pendingRecalls;
    std::uint32_t _outageSinceSec = 0;
};

}  // namespace CompanionCoopDrain

#endif
