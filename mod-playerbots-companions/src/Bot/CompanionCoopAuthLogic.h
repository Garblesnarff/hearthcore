/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANION_COOP_AUTH_LOGIC_H
#define PLAYERBOTS_COMPANION_COOP_AUTH_LOGIC_H

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

// Pure C++17 cooperative group & roster authorization engine (Issue #11).
// Zero AzerothCore dependencies so CompanionBrain.cpp, CompanionBotMgr.cpp, and standalone native C++17 tests
// execute the exact same authorization, group-binding, role-composition, and fail-closed verification rules.
namespace CompanionCoopAuth
{
inline constexpr std::size_t MAX_COOP_PARTY_SIZE = 5;
inline constexpr std::uint32_t DEFAULT_ASSEMBLY_GRACE_SEC = 180;
inline constexpr std::uint32_t LFG_REBIND_GRACE_SEC = 60;

inline bool IsValidCoopRole(std::string const& role)
{
    return role == "tank" || role == "healer" || role == "damage";
}

inline bool IsConfirmedInterestState(std::string const& state)
{
    // NEVER authorize from a 'reserved'-only row (Issue #11).
    return state == "accepted" || state == "active";
}

inline bool IsActiveRunState(std::string const& state)
{
    return state == "assembling" || state == "active";
}

struct RoleRequirements
{
    std::uint32_t tank = 0;
    std::uint32_t healer = 0;
    std::uint32_t damage = 0;

    std::uint32_t Total() const { return tank + healer + damage; }
    bool IsValid() const
    {
        std::uint32_t const total = Total();
        return total >= 1 && total <= MAX_COOP_PARTY_SIZE;
    }
};

// Strict parser for companion_opportunities.roles_json (e.g. {"tank":1,"healer":1,"damage":3}).
// Fails closed on malformed JSON, unknown keys, negative values, or headcount outside [1..5].
inline bool ParseRoleRequirements(std::string const& json, RoleRequirements& out)
{
    out = RoleRequirements{};
    std::size_t i = 0;
    auto skipWs = [&]()
    {
        while (i < json.size() && std::isspace(static_cast<unsigned char>(json[i])))
            ++i;
    };
    skipWs();
    if (i >= json.size() || json[i] != '{')
        return false;
    ++i;
    skipWs();
    if (i >= json.size() || json[i] == '}')
        return false;

    bool seenAny = false;
    while (i < json.size())
    {
        skipWs();
        if (i >= json.size() || json[i] != '"')
            return false;
        ++i;
        std::size_t const keyStart = i;
        while (i < json.size() && json[i] != '"')
            ++i;
        if (i >= json.size())
            return false;
        std::string const key = json.substr(keyStart, i - keyStart);
        ++i;  // closing quote
        skipWs();
        if (i >= json.size() || json[i] != ':')
            return false;
        ++i;
        skipWs();
        if (i >= json.size() || !std::isdigit(static_cast<unsigned char>(json[i])))
            return false;
        std::uint32_t val = 0;
        while (i < json.size() && std::isdigit(static_cast<unsigned char>(json[i])))
        {
            val = val * 10 + static_cast<std::uint32_t>(json[i] - '0');
            if (val > MAX_COOP_PARTY_SIZE)
                return false;
            ++i;
        }
        if (key == "tank")
            out.tank = val;
        else if (key == "healer")
            out.healer = val;
        else if (key == "damage")
            out.damage = val;
        else
            return false;
        seenAny = true;

        skipWs();
        if (i < json.size() && json[i] == ',')
        {
            ++i;
            continue;
        }
        if (i < json.size() && json[i] == '}')
        {
            ++i;
            skipWs();
            return seenAny && i == json.size() && out.IsValid();
        }
        return false;
    }
    return false;
}

struct AuthorizedMember
{
    std::uint32_t characterGuid = 0;
    std::string role;
    std::string interestState;        // must be "accepted" or "active"
    std::uint32_t interestRevision = 0;
    bool enrolledCoop = false;
};

struct AuthorizedRun
{
    std::uint64_t opportunityId = 0;
    std::uint32_t revision = 0;
    std::uint32_t targetMapId = 0;
    std::uint32_t expectedInstanceId = 0;   // 0 until bound on verified dungeon entry
    std::uint8_t difficulty = 0;            // 0 = Normal, 1 = Heroic
    std::uint32_t expectedGroupId = 0;      // 0 only during bounded assembly before native group bind
    std::uint64_t expectedGroupToken = 0;   // monotonic GroupScript lifecycle token (catches real group ID reuse!)
    std::uint32_t expectedLeaderGuid = 0;   // 0 until bound, must belong to roster
    std::string runState;                   // "assembling" | "active"
    std::uint32_t assemblyDeadline = 0;     // bounded proposal-to-group transition deadline
    std::uint32_t expiresAt = 0;
    bool isShadow = false;
    bool boundToNativeGroup = false;
    bool isLfgBound = false;
    RoleRequirements requiredRoles;         // parsed from companion_opportunities.roles_json
    std::vector<AuthorizedMember> roster;
};

struct NativeGroupMember
{
    std::uint32_t guid = 0;
    bool online = false;
    bool isCompanion = false;
    bool isOwner = false;
};

struct NativeGroupSnapshot
{
    std::uint32_t groupId = 0;
    std::uint64_t groupToken = 0;           // monotonic lifecycle token assigned on GroupScript::OnCreate
    bool isRaid = false;
    bool isBg = false;
    bool isBf = false;
    bool isLfg = false;
    std::uint8_t dungeonDifficulty = 0;
    std::uint32_t leaderGuid = 0;
    std::uint32_t inviteeCount = 0;
    std::vector<NativeGroupMember> members; // ALL Group::MemberSlots (online AND offline!)
};

struct AuthDecision
{
    bool authorized = false;
    bool assemblyInProgress = false; // bounded partial assembly or queued bind (do not disband, do not grant control)
    bool readyToBind = false;        // exact full roster confirmed and awaits world-thread atomic DB+native bind
    bool isLfgRebind = false;        // explicit pre-made -> native LFG group ID transition
    bool leaderFailover = false;     // verified roster leader failover while pinned leader was offline
    std::uint64_t opportunityId = 0;
    std::uint32_t revision = 0;
    std::uint32_t targetMapId = 0;
    std::uint32_t previousGroupId = 0;
    std::uint32_t groupId = 0;
    std::string reason;
};

class Registry
{
public:
    std::uint64_t BindGeneration() const { return _bindGeneration; }

    // Reloads authorized runs from DB while preserving runtime-only native bindings (groupToken,
    // expectedInstanceId, expectedLeaderGuid, isLfgBound) ONLY when (opportunityId, revision, expectedGroupId)
    // still match the DB state. If expectedGen != _bindGeneration (a bind or disband committed while the DB
    // query was in flight), the stale reload is skipped so fresh binds are never wiped!
    bool ReloadRuns(std::vector<AuthorizedRun> nextRuns, std::uint64_t expectedGen)
    {
        if (expectedGen != _bindGeneration)
            return false;

        std::unordered_map<std::uint64_t, AuthorizedRun> prevByOpp;
        for (AuthorizedRun const& r : _runs)
            prevByOpp[r.opportunityId] = r;

        for (AuthorizedRun& r : nextRuns)
        {
            r.boundToNativeGroup = (r.expectedGroupId != 0);
            auto it = prevByOpp.find(r.opportunityId);
            if (it != prevByOpp.end() && it->second.revision == r.revision)
            {
                AuthorizedRun const& prev = it->second;
                if (r.boundToNativeGroup && prev.boundToNativeGroup && r.expectedGroupId == prev.expectedGroupId)
                {
                    if (r.expectedGroupToken == 0)
                        r.expectedGroupToken = prev.expectedGroupToken;
                    if (r.expectedLeaderGuid == 0)
                        r.expectedLeaderGuid = prev.expectedLeaderGuid;
                    r.isLfgBound = r.isLfgBound || prev.isLfgBound;
                    if (r.expectedInstanceId == 0)
                        r.expectedInstanceId = prev.expectedInstanceId;
                }
            }
        }

        _runs = std::move(nextRuns);
        RebuildIndexes();
        return true;
    }

    // Convenience overload for tests that reload synchronously without concurrent writers.
    void ReloadRuns(std::vector<AuthorizedRun> nextRuns)
    {
        (void)ReloadRuns(std::move(nextRuns), _bindGeneration);
    }

    void Clear()
    {
        _runs.clear();
        _memberToRunIdx.clear();
        _ambiguousMembers.clear();
    }

    std::vector<AuthorizedRun> const& Runs() const { return _runs; }

    struct DisbandTransition
    {
        std::uint64_t opportunityId = 0;
        bool aborted = false;  // true -> terminal aborted (LFG or in-dungeon run ended); false -> bounded rebind window
    };

    // Immediately handles GroupScript::OnDisband for a native group:
    // - If the run was already LFG-bound OR had entered a dungeon (expectedInstanceId != 0), marks it terminal
    //   "aborted" and removes its members from _memberToRunIdx so zero zombie lockouts occur!
    // - If a pre-made assembling/pre-dungeon group disbanded (e.g. dissolving into a newly popped native LFG group),
    //   transitions back to "assembling" with a short bounded LFG_REBIND_GRACE_SEC window so the new native LFG
    //   group can bind cleanly without hitting unbound_active_run, and times out cleanly if no LFG group forms.
    bool OnGroupDisbanded(std::uint32_t groupId, std::uint64_t groupToken, std::uint32_t nowSec,
                          std::vector<DisbandTransition>* outTransitions = nullptr)
    {
        if (outTransitions)
            outTransitions->clear();
        if (groupId == 0)
            return false;
        bool changed = false;
        for (AuthorizedRun& r : _runs)
        {
            if (!r.boundToNativeGroup || r.expectedGroupId != groupId)
                continue;
            if (groupToken != 0 && r.expectedGroupToken != 0 && r.expectedGroupToken != groupToken)
                continue;
            bool const terminalAbort = (r.isLfgBound || r.expectedInstanceId != 0);
            r.boundToNativeGroup = false;
            r.expectedGroupId = 0;
            r.expectedGroupToken = 0;
            r.expectedLeaderGuid = 0;
            r.expectedInstanceId = 0;
            r.isLfgBound = false;
            if (terminalAbort)
            {
                r.runState = "aborted";
            }
            else
            {
                r.runState = "assembling";
                std::uint32_t const rebindDeadline = nowSec + LFG_REBIND_GRACE_SEC;
                r.assemblyDeadline = (r.expiresAt > 0) ? std::min(r.expiresAt, rebindDeadline) : rebindDeadline;
            }
            if (outTransitions)
                outTransitions->push_back(DisbandTransition{r.opportunityId, terminalAbort});
            changed = true;
        }
        if (changed)
        {
            ++_bindGeneration;
            RebuildIndexes();
        }
        return changed;
    }

    // Finds a strictly confirmed, non-expired, non-shadow, bound membership for a single bot GUID.
    bool LookupBoundMember(std::uint32_t botGuid, std::uint32_t nowSec, std::uint64_t& outOppId,
                           std::uint32_t& outTargetMap, std::uint32_t& outGroupId) const
    {
        outOppId = 0;
        outTargetMap = 0;
        outGroupId = 0;
        if (_ambiguousMembers.count(botGuid) > 0)
            return false;
        auto it = _memberToRunIdx.find(botGuid);
        if (it == _memberToRunIdx.end())
            return false;
        AuthorizedRun const& run = _runs[it->second];
        if (run.isShadow || !IsActiveRunState(run.runState) || run.expiresAt <= nowSec)
            return false;
        if (!run.boundToNativeGroup || run.expectedGroupId == 0)
            return false;
        std::string roleErr;
        if (!ValidateRoleComposition(run, roleErr))
            return false;
        AuthorizedMember const* mem = FindRosterMember(run, botGuid);
        if (!mem || !mem->enrolledCoop || !IsConfirmedInterestState(mem->interestState) ||
            mem->interestRevision != run.revision || !IsValidCoopRole(mem->role))
            return false;
        outOppId = run.opportunityId;
        outTargetMap = run.targetMapId;
        outGroupId = run.expectedGroupId;
        return true;
    }

    // Explicitly binds a newly formed native (or LFG) group to an assembling run once exact full roster and
    // role composition are confirmed. Also supports explicit pre-made -> native LFG group rebind when
    // allowLfgRebind=true and !target->isLfgBound && snap.isLfg.
    bool BindNativeGroup(std::uint64_t opportunityId, std::uint32_t expectedRevision,
                         NativeGroupSnapshot const& snap, std::uint32_t instanceId, std::uint32_t nowSec,
                         bool promoteToActive = true, bool allowLfgRebind = false, std::string* outReason = nullptr)
    {
        auto setReason = [&](char const* r)
        {
            if (outReason)
                *outReason = r;
        };
        if (opportunityId == 0 || snap.groupId == 0)
        {
            setReason("invalid_bind_args");
            return false;
        }
        if (snap.isRaid || snap.isBg || snap.isBf || snap.members.size() > MAX_COOP_PARTY_SIZE)
        {
            setReason("invalid_group_type");
            return false;
        }
        if (snap.inviteeCount > 0)
        {
            setReason("invitee_pending_unverified");
            return false;
        }

        AuthorizedRun* target = nullptr;
        for (AuthorizedRun& r : _runs)
        {
            if (r.opportunityId == opportunityId)
                target = &r;
            else if (!r.isShadow && IsActiveRunState(r.runState) && r.expiresAt > nowSec && r.boundToNativeGroup)
            {
                if (r.expectedGroupId == snap.groupId ||
                    (snap.groupToken != 0 && r.expectedGroupToken != 0 && r.expectedGroupToken == snap.groupToken))
                {
                    setReason("group_already_bound_to_other_run");
                    return false;
                }
            }
        }
        if (!target)
        {
            setReason("run_not_found");
            return false;
        }
        if (target->isShadow)
        {
            setReason("shadow_run");
            return false;
        }
        if (target->revision != expectedRevision)
        {
            setReason("revision_mismatch");
            return false;
        }
        if (!IsActiveRunState(target->runState) || target->expiresAt <= nowSec)
        {
            setReason("run_inactive_or_expired");
            return false;
        }
        if (!target->boundToNativeGroup && target->assemblyDeadline > 0 && nowSec > target->assemblyDeadline)
        {
            setReason("assembly_timeout");
            return false;
        }

        bool const rebindingToLfg =
            allowLfgRebind && target->boundToNativeGroup && !target->isLfgBound && snap.isLfg &&
            target->expectedGroupId != snap.groupId;
        if (target->boundToNativeGroup && !rebindingToLfg)
        {
            if (target->expectedGroupId != snap.groupId)
            {
                setReason("already_bound_different_group");
                return false;
            }
            if (target->expectedGroupToken != 0 && snap.groupToken != 0 &&
                target->expectedGroupToken != snap.groupToken)
            {
                setReason("group_id_reused");
                return false;
            }
        }

        std::string roleErr;
        if (!ValidateRoleComposition(*target, roleErr))
        {
            setReason(roleErr.c_str());
            return false;
        }
        std::string rosterErr;
        if (!ValidateExactRoster(*target, snap, /*requireComplete*/ true, rosterErr))
        {
            setReason(rosterErr.c_str());
            return false;
        }

        target->boundToNativeGroup = true;
        target->expectedGroupId = snap.groupId;
        target->expectedGroupToken = snap.groupToken;
        if (target->expectedLeaderGuid == 0 || rebindingToLfg)
            target->expectedLeaderGuid = snap.leaderGuid;
        target->isLfgBound = target->isLfgBound || snap.isLfg;
        if (promoteToActive)
            target->runState = "active";
        if (instanceId != 0 && target->expectedInstanceId == 0)
            target->expectedInstanceId = instanceId;
        ++_bindGeneration;
        setReason(rebindingToLfg ? "rebound_lfg" : "bound");
        return true;
    }

    // Pins runtime-only native identifiers (groupToken, leader, LFG flag, concrete dungeon instance ID)
    // after a DB-reloaded bound run passes full Authorize verification. On verified offline leader failover
    // (leaderFailover=true), re-pins expectedLeaderGuid so the original leader reconnecting never causes a disband!
    bool PinRuntimeBindings(std::uint64_t opportunityId, std::uint32_t expectedRevision, std::uint32_t groupId,
                            std::uint64_t groupToken, std::uint32_t leaderGuid, bool leaderFailover, bool isLfg,
                            std::uint32_t dungeonMapId, std::uint32_t instanceId)
    {
        if (!opportunityId || !groupId)
            return false;
        for (AuthorizedRun& r : _runs)
        {
            if (r.opportunityId != opportunityId)
                continue;
            if (r.revision != expectedRevision || !r.boundToNativeGroup || r.expectedGroupId != groupId)
                return false;
            if (groupToken != 0)
            {
                if (r.expectedGroupToken != 0 && r.expectedGroupToken != groupToken)
                    return false;
                r.expectedGroupToken = groupToken;
            }
            if (leaderGuid != 0 && (r.expectedLeaderGuid == 0 || leaderFailover))
                r.expectedLeaderGuid = leaderGuid;
            if (isLfg)
                r.isLfgBound = true;
            if (dungeonMapId != 0 && instanceId != 0)
            {
                if (r.targetMapId != dungeonMapId)
                    return false;
                if (r.expectedInstanceId != 0 && r.expectedInstanceId != instanceId)
                    return false;
                r.expectedInstanceId = instanceId;
            }
            return true;
        }
        return false;
    }

    // Primary authorization gate (pure in-memory, non-blocking, safe on map threads).
    AuthDecision Authorize(bool coopEnabled, std::uint32_t botGuid, std::uint32_t botMapId,
                           std::uint32_t botInstanceId, std::uint8_t botDifficulty, bool botInNonRaidDungeon,
                           bool botInRaid, bool botInBgOrArena, NativeGroupSnapshot const& snap,
                           std::uint32_t nowSec) const
    {
        AuthDecision dec;
        if (!coopEnabled)
        {
            dec.reason = "coop_disabled";
            return dec;
        }
        if (botGuid == 0 || snap.groupId == 0)
        {
            dec.reason = "no_group";
            return dec;
        }
        // Strictly 5-player non-raid dungeon parties: NEVER autonomous raids, battlegrounds, arenas, or battlefields.
        if (snap.isRaid || botInRaid)
        {
            dec.reason = "raid_forbidden";
            return dec;
        }
        if (snap.isBg || snap.isBf || botInBgOrArena)
        {
            dec.reason = "bg_or_bf_forbidden";
            return dec;
        }
        if (snap.members.empty() || snap.members.size() > MAX_COOP_PARTY_SIZE)
        {
            dec.reason = "group_too_large";
            return dec;
        }
        // Owner-led groups ALWAYS take precedence over cooperative automation (including offline owner slots!).
        for (NativeGroupMember const& m : snap.members)
        {
            if (m.isOwner)
            {
                dec.reason = "owner_group_precedence";
                return dec;
            }
        }
        if (_ambiguousMembers.count(botGuid) > 0)
        {
            dec.reason = "ambiguous_multiple_runs";
            return dec;
        }

        auto it = _memberToRunIdx.find(botGuid);
        if (it == _memberToRunIdx.end())
        {
            dec.reason = "not_authorized_member";
            return dec;
        }
        AuthorizedRun const& run = _runs[it->second];
        dec.opportunityId = run.opportunityId;
        dec.revision = run.revision;
        dec.targetMapId = run.targetMapId;
        dec.previousGroupId = run.expectedGroupId;
        dec.groupId = snap.groupId;

        if (run.isShadow)
        {
            dec.reason = "shadow_run";
            return dec;
        }
        if (!IsActiveRunState(run.runState))
        {
            dec.reason = "run_not_active";
            return dec;
        }
        if (run.expiresAt <= nowSec)
        {
            dec.reason = "expired";
            return dec;
        }
        if (!run.requiredRoles.IsValid())
        {
            dec.reason = "invalid_role_requirements";
            return dec;
        }

        AuthorizedMember const* selfMem = FindRosterMember(run, botGuid);
        if (!selfMem)
        {
            dec.reason = "not_in_roster";
            return dec;
        }
        if (!selfMem->enrolledCoop)
        {
            dec.reason = "not_enrolled";
            return dec;
        }
        if (!IsConfirmedInterestState(selfMem->interestState))
        {
            dec.reason = selfMem->interestState == "reserved" ? "reserved_only" : "interest_not_confirmed";
            return dec;
        }
        if (selfMem->interestRevision != run.revision)
        {
            dec.reason = "revision_mismatch";
            return dec;
        }
        if (!IsValidCoopRole(selfMem->role))
        {
            dec.reason = "role_missing";
            return dec;
        }

        std::uint32_t const requiredSize = run.requiredRoles.Total();

        // Proposal-to-group bounded assembly transition (unbound):
        // NEVER bind or grant cooperative control until exact required headcount + role composition are confirmed!
        if (!run.boundToNativeGroup || run.expectedGroupId == 0)
        {
            if (run.runState != "assembling")
            {
                dec.reason = "unbound_active_run";
                return dec;
            }
            if (run.assemblyDeadline > 0 && nowSec > run.assemblyDeadline)
            {
                dec.reason = "assembly_timeout";
                return dec;
            }
            if (botInNonRaidDungeon)
            {
                dec.reason = "dungeon_entry_before_bind";
                return dec;
            }
            if (IsGroupBoundToOtherRun(run.opportunityId, snap.groupId, snap.groupToken, nowSec))
            {
                dec.reason = "group_already_bound_to_other_run";
                return dec;
            }
            std::string rosterErr;
            if (!ValidateExactRoster(run, snap, /*requireComplete*/ false, rosterErr))
            {
                dec.reason = rosterErr;
                return dec;
            }
            // If DB confirmed roster hasn't reached requiredSize yet (e.g. others still 'reserved'), OR native group
            // hasn't finished assembling all requiredSize slots, hold bounded assembly without binding!
            if (run.roster.size() < requiredSize || snap.members.size() < requiredSize)
            {
                if (snap.members.size() + snap.inviteeCount > requiredSize)
                {
                    dec.reason = "invitee_pending_unverified";
                    return dec;
                }
                dec.assemblyInProgress = true;
                dec.reason = "assembly_in_progress";
                return dec;
            }
            std::string roleErr;
            if (!ValidateRoleComposition(run, roleErr))
            {
                dec.reason = roleErr;
                return dec;
            }
            if (snap.inviteeCount > 0)
            {
                dec.reason = "invitee_pending_unverified";
                return dec;
            }
            dec.readyToBind = true;
            dec.assemblyInProgress = true;
            dec.reason = "ready_to_bind";
            return dec;
        }

        // Bound run: check explicit pre-made -> native LFG group rebind transition first.
        if (snap.groupId != run.expectedGroupId)
        {
            if (!run.isLfgBound && snap.isLfg && !botInNonRaidDungeon && snap.inviteeCount == 0 &&
                !IsGroupBoundToOtherRun(run.opportunityId, snap.groupId, snap.groupToken, nowSec))
            {
                std::string roleErr;
                std::string rosterErr;
                if (ValidateRoleComposition(run, roleErr) &&
                    ValidateExactRoster(run, snap, /*requireComplete*/ true, rosterErr))
                {
                    dec.readyToBind = true;
                    dec.isLfgRebind = true;
                    dec.assemblyInProgress = true;
                    dec.reason = "ready_to_rebind_lfg";
                    return dec;
                }
            }
            dec.reason = "group_id_mismatch";
            return dec;
        }
        // Real group ID reuse detection via GroupScript monotonic lifecycle token!
        if (run.expectedGroupToken != 0 && snap.groupToken != 0 && snap.groupToken != run.expectedGroupToken)
        {
            dec.reason = "group_id_reused";
            return dec;
        }
        if (IsGroupBoundToOtherRun(run.opportunityId, snap.groupId, snap.groupToken, nowSec))
        {
            dec.reason = "group_already_bound_to_other_run";
            return dec;
        }

        // Bound but still assembling with partial members: bounded assembly without coop control yet.
        if (run.runState == "assembling" && (run.roster.size() < requiredSize || snap.members.size() < requiredSize))
        {
            if (run.assemblyDeadline > 0 && nowSec > run.assemblyDeadline)
            {
                dec.reason = "assembly_timeout";
                return dec;
            }
            if (botInNonRaidDungeon)
            {
                dec.reason = "dungeon_entry_before_full_roster";
                return dec;
            }
            std::string rosterErr;
            if (!ValidateExactRoster(run, snap, /*requireComplete*/ false, rosterErr))
            {
                dec.reason = rosterErr;
                return dec;
            }
            if (snap.members.size() + snap.inviteeCount > requiredSize)
            {
                dec.reason = "invitee_pending_unverified";
                return dec;
            }
            dec.assemblyInProgress = true;
            dec.reason = "assembly_in_progress";
            return dec;
        }

        // Reject pending unverified invitees once full/bound.
        if (snap.inviteeCount > 0)
        {
            dec.reason = "invitee_pending_unverified";
            return dec;
        }

        // Verify exact role composition AND exact full roster across ALL native MemberSlots (online AND offline).
        std::string roleErr;
        if (!ValidateRoleComposition(run, roleErr))
        {
            dec.reason = roleErr;
            return dec;
        }
        std::string rosterErr;
        if (!ValidateExactRoster(run, snap, /*requireComplete*/ true, rosterErr))
        {
            dec.reason = rosterErr;
            return dec;
        }

        // Verify native group leader matches expectedLeaderGuid when pinned. If the pinned leader went offline
        // and AzerothCore auto-rotated leadership to another verified roster member, mark leaderFailover=true so
        // PinRuntimeBindings re-pins expectedLeaderGuid (preventing disband when the original leader reconnects!).
        if (run.expectedLeaderGuid != 0 && snap.leaderGuid != run.expectedLeaderGuid)
        {
            bool expectedLeaderOnline = false;
            for (NativeGroupMember const& nm : snap.members)
            {
                if (nm.guid == run.expectedLeaderGuid && nm.online)
                {
                    expectedLeaderOnline = true;
                    break;
                }
            }
            if (expectedLeaderOnline)
            {
                dec.reason = "leader_mismatch";
                return dec;
            }
            dec.leaderFailover = true;
        }

        // Verify dungeon difficulty, map, and instance binding when inside a dungeon.
        if (botInNonRaidDungeon)
        {
            if (run.targetMapId == 0 || botMapId != run.targetMapId)
            {
                dec.reason = "map_mismatch";
                return dec;
            }
            if (botDifficulty != run.difficulty || snap.dungeonDifficulty != run.difficulty)
            {
                dec.reason = "difficulty_mismatch";
                return dec;
            }
            if (run.expectedInstanceId != 0 && botInstanceId != 0 && botInstanceId != run.expectedInstanceId)
            {
                dec.reason = "instance_mismatch";
                return dec;
            }
        }

        dec.authorized = true;
        dec.reason = "ok";
        return dec;
    }

private:
    static AuthorizedMember const* FindRosterMember(AuthorizedRun const& run, std::uint32_t guid)
    {
        for (AuthorizedMember const& m : run.roster)
            if (m.characterGuid == guid)
                return &m;
        return nullptr;
    }

    static bool ValidateRoleComposition(AuthorizedRun const& run, std::string& outErr)
    {
        if (!run.requiredRoles.IsValid())
        {
            outErr = "invalid_role_requirements";
            return false;
        }
        if (run.roster.size() != run.requiredRoles.Total())
        {
            outErr = "roster_incomplete_for_roles";
            return false;
        }
        std::uint32_t tanks = 0;
        std::uint32_t healers = 0;
        std::uint32_t damages = 0;
        for (AuthorizedMember const& rm : run.roster)
        {
            if (rm.role == "tank")
                ++tanks;
            else if (rm.role == "healer")
                ++healers;
            else if (rm.role == "damage")
                ++damages;
            else
            {
                outErr = "role_missing";
                return false;
            }
        }
        if (tanks != run.requiredRoles.tank || healers != run.requiredRoles.healer ||
            damages != run.requiredRoles.damage)
        {
            outErr = "role_composition_mismatch";
            return false;
        }
        return true;
    }

    bool IsGroupBoundToOtherRun(std::uint64_t selfOppId, std::uint32_t groupId, std::uint64_t groupToken,
                                std::uint32_t nowSec) const
    {
        for (AuthorizedRun const& r : _runs)
        {
            if (r.opportunityId == selfOppId || r.isShadow || !IsActiveRunState(r.runState) || r.expiresAt <= nowSec)
                continue;
            if (!r.boundToNativeGroup)
                continue;
            if (r.expectedGroupId == groupId ||
                (groupToken != 0 && r.expectedGroupToken != 0 && r.expectedGroupToken == groupToken))
                return true;
        }
        return false;
    }

    bool ValidateExactRoster(AuthorizedRun const& run, NativeGroupSnapshot const& snap, bool requireComplete,
                             std::string& outErr) const
    {
        if (run.roster.empty() || run.roster.size() > MAX_COOP_PARTY_SIZE)
        {
            outErr = "invalid_roster_size";
            return false;
        }
        std::unordered_set<std::uint32_t> expectedGuids;
        for (AuthorizedMember const& rm : run.roster)
        {
            if (!rm.characterGuid || !rm.enrolledCoop || !IsConfirmedInterestState(rm.interestState) ||
                rm.interestRevision != run.revision || !IsValidCoopRole(rm.role))
            {
                outErr = "roster_entry_invalid";
                return false;
            }
            expectedGuids.insert(rm.characterGuid);
        }

        // Check EVERY member slot in the native group (online AND offline).
        std::unordered_set<std::uint32_t> seenNative;
        for (NativeGroupMember const& nm : snap.members)
        {
            if (nm.guid == 0)
            {
                outErr = "invalid_member_guid";
                return false;
            }
            if (nm.isOwner)
            {
                outErr = "owner_group_precedence";
                return false;
            }
            if (!nm.isCompanion)
            {
                outErr = nm.online ? "unapproved_human_member" : "offline_non_companion_member";
                return false;
            }
            if (!seenNative.insert(nm.guid).second)
            {
                outErr = "duplicate_native_member";
                return false;
            }
            if (expectedGuids.count(nm.guid) == 0)
            {
                auto otherIt = _memberToRunIdx.find(nm.guid);
                if (otherIt != _memberToRunIdx.end() && _runs[otherIt->second].opportunityId != run.opportunityId)
                    outErr = "mixed_opportunity_member";
                else
                    outErr = "unapproved_member";
                return false;
            }
        }

        if (snap.leaderGuid == 0 || expectedGuids.count(snap.leaderGuid) == 0 ||
            seenNative.count(snap.leaderGuid) == 0)
        {
            outErr = "leader_not_in_roster";
            return false;
        }

        if (requireComplete && seenNative.size() != expectedGuids.size())
        {
            outErr = "roster_incomplete_for_active";
            return false;
        }
        return true;
    }

    void RebuildIndexes()
    {
        _memberToRunIdx.clear();
        _ambiguousMembers.clear();
        for (std::size_t idx = 0; idx < _runs.size(); ++idx)
        {
            // Skip terminal/aborted runs so disbanded groups never leave zombie lockouts in _memberToRunIdx!
            if (!IsActiveRunState(_runs[idx].runState))
                continue;
            for (AuthorizedMember const& m : _runs[idx].roster)
            {
                if (m.characterGuid == 0)
                    continue;
                auto [it, inserted] = _memberToRunIdx.emplace(m.characterGuid, idx);
                if (!inserted && _runs[it->second].opportunityId != _runs[idx].opportunityId)
                    _ambiguousMembers.insert(m.characterGuid);
            }
        }
    }

    std::vector<AuthorizedRun> _runs;
    std::unordered_map<std::uint32_t, std::size_t> _memberToRunIdx;
    std::unordered_set<std::uint32_t> _ambiguousMembers;
    std::uint64_t _bindGeneration = 0;
};

}  // namespace CompanionCoopAuth

#endif
