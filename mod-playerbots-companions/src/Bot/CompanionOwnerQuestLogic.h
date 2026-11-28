/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANIONOWNERQUEST_LOGIC_H
#define PLAYERBOTS_COMPANIONOWNERQUEST_LOGIC_H

#include <cstddef>
#include <cstdint>
#include <set>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

// Pure C++17 owner-keyed shared-quest protection & acceptance logic (Issue #30).
//
// Core invariants:
//   1. Quest snapshots are strictly keyed by each resolved owner's character low GUID
//      (never one shared global primary-owner set).
//   2. Two owners with disjoint quest logs (e.g. Wonky & Logan) never cross-contaminate
//      each other's companion quest protection or reserved log slots.
//   3. Per-owner online -> logout (keep live copy during async DB save delay) -> offline DB
//      lifecycle runs independently per owner.
//   4. On DB read failure, NEVER overwrite a snapshot with an empty set; transition to Stale
//      after maxConsecutiveDbFailures or staleAfterSec with exponential retry backoff.
//   5. While a bot's assigned owner is unresolved, unknown, or stale (and not live on the
//      bot's own map), fail closed ("owner-unknown") on ALL destructive cleanup/abandonment,
//      and preserve reserved log slots in MayAccept (never substituting the primary owner!).
//   6. Companions with NO configured owner (neither per-bot override nor primary owner) are
//      completely unaffected (AllowAbandon / no owner-shared override).
namespace CompanionOwnerQuest
{
enum class SnapshotState : std::uint8_t
{
    Unknown = 0,
    Fresh = 1,
    Stale = 2,
};

inline char const* SnapshotStateName(SnapshotState st)
{
    switch (st)
    {
        case SnapshotState::Unknown: return "unknown";
        case SnapshotState::Fresh:   return "fresh";
        case SnapshotState::Stale:   return "stale";
    }
    return "unknown";
}

enum class ProbeAction : std::uint8_t
{
    None = 0,
    ReadOnlineLog = 1,
    LogoutKeepLive = 2,
    QueryDb = 3,
};

enum class GuardDecision : std::uint8_t
{
    AllowAbandon = 0,
    KeepOwnerShared = 1,
    KeepOwnerUnknown = 2,
};

inline char const* GuardReasonText(GuardDecision d)
{
    switch (d)
    {
        case GuardDecision::AllowAbandon:     return nullptr;
        case GuardDecision::KeepOwnerShared:  return "owner-shared";
        case GuardDecision::KeepOwnerUnknown: return "owner-unknown";
    }
    return "owner-unknown";
}

struct OwnerQuestConfig
{
    std::uint32_t liveRefreshMs = 10000;
    std::uint32_t dbRefreshMs = 60000;
    std::uint32_t logoutDbDelayMs = 15000;
    std::uint32_t staleAfterSec = 300;
    std::uint32_t maxConsecutiveDbFailures = 2;
};

inline std::uint32_t ClampStaleSeconds(std::uint32_t staleAfterSec, std::uint32_t dbRefreshMs)
{
    if (staleAfterSec == 0)
        return 0;
    std::uint32_t const dbSec = dbRefreshMs / 1000u;
    std::uint32_t const minSafe = dbSec > 0 ? dbSec * 2u : 120u;
    return staleAfterSec < minSafe ? minSafe : staleAfterSec;
}

inline std::uint32_t ComputeDbRetryBackoffSec(std::uint32_t consecutiveFailures, std::uint32_t maxBackoffSec)
{
    std::uint32_t const cap = maxBackoffSec > 5u ? maxBackoffSec : 60u;
    if (consecutiveFailures <= 1u)
        return 5u;
    std::uint32_t delay = 5u;
    std::uint32_t const steps = consecutiveFailures - 1u;
    for (std::uint32_t i = 0; i < steps && delay < cap; ++i)
        delay = (delay * 2u > cap) ? cap : (delay * 2u);
    return delay;
}

struct OwnerQuestSnapshot
{
    std::uint32_t ownerLowGuid = 0;
    std::unordered_set<std::uint32_t> quests;
    bool known = false;
    bool wasOnline = false;
    bool waitingLogoutDb = false;
    bool lastDbFailed = false;
    std::uint32_t timerMs = 0;
    std::uint32_t lastSuccessAtSec = 0;
    std::uint32_t consecutiveDbFailures = 0;
    std::uint32_t nextDbRetryAtSec = 0;
};

struct AcceptDecision
{
    bool allowed = true;
    char const* rejectReason = nullptr;
};

inline AcceptDecision EvaluateMayAccept(bool ownerHasQuest,
                                        std::uint32_t freeSlots,
                                        std::uint32_t reservedFreeSlots,
                                        bool isGreyLowValue,
                                        bool outsideHubPlan)
{
    if (ownerHasQuest)
    {
        if (freeSlots == 0)
            return {false, "free-slots"};
        return {true, nullptr};
    }
    if (freeSlots <= reservedFreeSlots)
        return {false, "free-slots"};
    if (isGreyLowValue)
        return {false, "grey-low-value"};
    if (outsideHubPlan)
        return {false, "not-in-hub-plan"};
    return {true, nullptr};
}

class OwnerQuestRegistry
{
public:
    // Keep snapshots strictly synchronized with the current set of resolved configured owner GUIDs.
    // Evicts removed/deleted/renamed owners so stale snapshots can never linger.
    void SyncActiveOwners(std::set<std::uint32_t> const& activeOwnerLows)
    {
        for (auto it = _snapshots.begin(); it != _snapshots.end();)
        {
            if (activeOwnerLows.find(it->first) == activeOwnerLows.end())
                it = _snapshots.erase(it);
            else
                ++it;
        }
        for (std::uint32_t ownerLow : activeOwnerLows)
        {
            if (ownerLow == 0)
                continue;
            auto it = _snapshots.find(ownerLow);
            if (it == _snapshots.end())
            {
                OwnerQuestSnapshot snap;
                snap.ownerLowGuid = ownerLow;
                _snapshots.emplace(ownerLow, std::move(snap));
            }
        }
    }

    // Per-owner world-thread probe scheduler: decides whether to read live log, hold on logout, or query DB.
    ProbeAction PlanProbe(std::uint32_t ownerLow,
                          bool isOnline,
                          std::uint32_t elapsedMs,
                          std::uint32_t nowSec,
                          OwnerQuestConfig const& cfg)
    {
        if (ownerLow == 0)
            return ProbeAction::None;
        OwnerQuestSnapshot& snap = _snapshots[ownerLow];
        snap.ownerLowGuid = ownerLow;
        snap.timerMs += elapsedMs;

        if (isOnline)
        {
            bool const shouldRead = !snap.known || !snap.wasOnline || snap.timerMs >= cfg.liveRefreshMs;
            snap.wasOnline = true;
            snap.waitingLogoutDb = false;
            if (shouldRead)
            {
                snap.timerMs = 0;
                return ProbeAction::ReadOnlineLog;
            }
            return ProbeAction::None;
        }

        // Offline transition: keep the last live quest log while the async DB save completes.
        if (snap.wasOnline)
        {
            snap.wasOnline = false;
            snap.waitingLogoutDb = true;
            snap.timerMs = 0;
            return ProbeAction::LogoutKeepLive;
        }

        if (snap.waitingLogoutDb)
        {
            if (snap.timerMs >= cfg.logoutDbDelayMs)
            {
                snap.waitingLogoutDb = false;
                snap.timerMs = 0;
                return ProbeAction::QueryDb;
            }
            return ProbeAction::None;
        }

        if (snap.lastDbFailed)
        {
            if (nowSec >= snap.nextDbRetryAtSec)
            {
                snap.timerMs = 0;
                return ProbeAction::QueryDb;
            }
            return ProbeAction::None;
        }

        std::uint32_t const effStale = ClampStaleSeconds(cfg.staleAfterSec, cfg.dbRefreshMs);
        std::uint32_t const earlyRefreshAge = effStale > 0 ? (effStale / 2u) : 0u;
        bool const earlyDue = snap.known && earlyRefreshAge > 0 && nowSec > snap.lastSuccessAtSec &&
                              (nowSec - snap.lastSuccessAtSec) >= earlyRefreshAge;
        if (!snap.known || snap.timerMs >= cfg.dbRefreshMs || earlyDue)
        {
            snap.timerMs = 0;
            return ProbeAction::QueryDb;
        }
        return ProbeAction::None;
    }

    // Publish an online live quest-log snapshot for one owner. Returns true iff known/quest-set changed.
    bool RecordOnlineSnapshot(std::uint32_t ownerLow,
                              std::unordered_set<std::uint32_t> quests,
                              std::uint32_t nowSec)
    {
        if (ownerLow == 0)
            return false;
        OwnerQuestSnapshot& snap = _snapshots[ownerLow];
        snap.ownerLowGuid = ownerLow;
        bool const changed = !snap.known || snap.quests != quests;
        snap.quests = std::move(quests);
        snap.known = true;
        snap.wasOnline = true;
        snap.waitingLogoutDb = false;
        snap.lastDbFailed = false;
        snap.consecutiveDbFailures = 0;
        snap.lastSuccessAtSec = nowSec;
        snap.nextDbRetryAtSec = 0;
        return changed;
    }

    // Publish a confirmed DB snapshot for one offline owner (including genuinely empty quest logs when DB is healthy).
    bool RecordDbSnapshotSuccess(std::uint32_t ownerLow,
                                 std::unordered_set<std::uint32_t> quests,
                                 std::uint32_t nowSec)
    {
        if (ownerLow == 0)
            return false;
        OwnerQuestSnapshot& snap = _snapshots[ownerLow];
        snap.ownerLowGuid = ownerLow;
        bool const changed = !snap.known || snap.quests != quests;
        snap.quests = std::move(quests);
        snap.known = true;
        snap.waitingLogoutDb = false;
        snap.lastDbFailed = false;
        snap.consecutiveDbFailures = 0;
        snap.lastSuccessAtSec = nowSec;
        snap.nextDbRetryAtSec = 0;
        return changed;
    }

    // Record a DB outage/error without overwriting any previously known quest set with empty data.
    void RecordDbSnapshotFailure(std::uint32_t ownerLow,
                                 std::uint32_t nowSec,
                                 OwnerQuestConfig const& cfg)
    {
        if (ownerLow == 0)
            return;
        OwnerQuestSnapshot& snap = _snapshots[ownerLow];
        snap.ownerLowGuid = ownerLow;
        snap.lastDbFailed = true;
        ++snap.consecutiveDbFailures;
        std::uint32_t const maxBackoff = cfg.dbRefreshMs / 1000u;
        snap.nextDbRetryAtSec = nowSec + ComputeDbRetryBackoffSec(snap.consecutiveDbFailures, maxBackoff);
    }

    SnapshotState StateForOwner(std::uint32_t ownerLow,
                                std::uint32_t nowSec,
                                OwnerQuestConfig const& cfg) const
    {
        if (ownerLow == 0)
            return SnapshotState::Unknown;
        auto it = _snapshots.find(ownerLow);
        if (it == _snapshots.end() || !it->second.known)
            return SnapshotState::Unknown;
        OwnerQuestSnapshot const& snap = it->second;
        std::uint32_t const failLimit = cfg.maxConsecutiveDbFailures > 0 ? cfg.maxConsecutiveDbFailures : 1u;
        if (snap.lastDbFailed && snap.consecutiveDbFailures >= failLimit)
            return SnapshotState::Stale;
        std::uint32_t const effStale = ClampStaleSeconds(cfg.staleAfterSec, cfg.dbRefreshMs);
        if (effStale > 0 && nowSec > snap.lastSuccessAtSec && (nowSec - snap.lastSuccessAtSec) > effStale)
            return SnapshotState::Stale;
        return SnapshotState::Fresh;
    }

    std::size_t QuestCountForOwner(std::uint32_t ownerLow) const
    {
        auto it = _snapshots.find(ownerLow);
        return it == _snapshots.end() ? 0u : it->second.quests.size();
    }

    bool OwnerSnapshotHasQuest(std::uint32_t ownerLow, std::uint32_t questId) const
    {
        if (ownerLow == 0 || questId == 0)
            return false;
        auto it = _snapshots.find(ownerLow);
        return it != _snapshots.end() && it->second.known && it->second.quests.count(questId) > 0;
    }

    // Authoritative B2 abandon/cleanup guard for one companion.
    // Fails closed ("owner-unknown") whenever the bot has an assigned owner whose identity or quest
    // snapshot is unresolved/unknown/stale (and owner is not live on the bot's own map).
    GuardDecision EvaluateGuard(bool keepOwnerQuestsEnabled,
                                bool botHasAssignedOwner,
                                std::uint32_t resolvedOwnerLow,
                                bool ownerLiveOnSameMap,
                                bool liveOwnerHasQuest,
                                std::uint32_t questId,
                                std::uint32_t nowSec,
                                OwnerQuestConfig const& cfg) const
    {
        if (!keepOwnerQuestsEnabled || !botHasAssignedOwner)
            return GuardDecision::AllowAbandon;
        if (resolvedOwnerLow == 0)
            return GuardDecision::KeepOwnerUnknown;
        if (ownerLiveOnSameMap)
            return liveOwnerHasQuest ? GuardDecision::KeepOwnerShared : GuardDecision::AllowAbandon;
        SnapshotState const st = StateForOwner(resolvedOwnerLow, nowSec, cfg);
        if (st != SnapshotState::Fresh)
            return GuardDecision::KeepOwnerUnknown;
        return OwnerSnapshotHasQuest(resolvedOwnerLow, questId) ? GuardDecision::KeepOwnerShared
                                                                : GuardDecision::AllowAbandon;
    }

    // Authoritative owner-has-quest check for MayAccept & reserved slot usage.
    // Never falls back to primary owner; returns false when unresolved/unknown/stale so MayAccept
    // continues protecting reserved log slots!
    bool EvaluateOwnerHasQuest(bool botHasAssignedOwner,
                               std::uint32_t resolvedOwnerLow,
                               bool ownerLiveOnSameMap,
                               bool liveOwnerHasQuest,
                               std::uint32_t questId,
                               std::uint32_t nowSec,
                               OwnerQuestConfig const& cfg) const
    {
        if (!botHasAssignedOwner || resolvedOwnerLow == 0 || questId == 0)
            return false;
        if (ownerLiveOnSameMap)
            return liveOwnerHasQuest;
        if (StateForOwner(resolvedOwnerLow, nowSec, cfg) != SnapshotState::Fresh)
            return false;
        return OwnerSnapshotHasQuest(resolvedOwnerLow, questId);
    }

private:
    std::unordered_map<std::uint32_t, OwnerQuestSnapshot> _snapshots;
};

}  // namespace CompanionOwnerQuest

#endif  // PLAYERBOTS_COMPANIONOWNERQUEST_LOGIC_H
