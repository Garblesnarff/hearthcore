/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANIONOWNERXPCAP_LOGIC_H
#define PLAYERBOTS_COMPANIONOWNERXPCAP_LOGIC_H

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <map>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

// Pure C++17 companion owner resolution & XP-cap state machine (Issue #31).
//
// Core invariants:
//   1. Explicitly distinguish:
//        - None / Unconfigured (no per-bot override and no primary owner configured)
//        - PrimaryDefault (ordinary guild companion inheriting primary CompanionOwner)
//        - AssignedOverride (explicit AiPlayerbot.Companion.Owner.<Name> secondary owner)
//      and resolution states:
//        - Unconfigured
//        - ResolvedFresh
//        - UnresolvedAssignment (configured owner missing/renamed/deleted in cache or DB)
//        - LevelUnknown (GUID resolved, level never successfully read yet)
//        - StaleLevel (previously known level exceeded staleness TTL or consecutive refresh failures)
//   2. A companion with an explicit AssignedOverride MUST NEVER inherit the primary owner's
//      GUID or level when its assigned owner is unresolved, unknown, or stale.
//   3. While an applicable owner's identity or level is unresolved/stale (or unknown under
//      waitForOwnerLevel=true / AssignedOverride), fail closed on XP cap (zero accidental XP grants).
//      Note: a single transient DB read failure keeps a previously fresh level valid while scheduling
//      an immediate 5s fast retry; reaching maxConsecutiveFailures (default 2) or staleAfterSec
//      immediately transitions to StaleLevel and fails closed regardless of waitForOwnerLevel.
//   4. Always enforce the server expansion cap (CONFIG_MAX_PLAYER_LEVEL) alongside owner+lead.
namespace CompanionOwnerXpCap
{
inline std::string LowerAscii(std::string s)
{
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

inline std::string TrimAscii(std::string const& value)
{
    size_t const first = value.find_first_not_of(" \t\r\n\"");
    if (first == std::string::npos)
        return "";
    size_t const last = value.find_last_not_of(" \t\r\n\"");
    return value.substr(first, last - first + 1);
}

inline std::string EscapeJsonString(std::string const& s)
{
    std::string out;
    out.reserve(s.size() + 8);
    for (unsigned char c : s)
    {
        switch (c)
        {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b";  break;
            case '\f': out += "\\f";  break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:
                if (c < 0x20)
                {
                    char buf[8];
                    static char const hex[] = "0123456789abcdef";
                    buf[0] = '\\';
                    buf[1] = 'u';
                    buf[2] = '0';
                    buf[3] = '0';
                    buf[4] = hex[(c >> 4) & 0xF];
                    buf[5] = hex[c & 0xF];
                    buf[6] = '\0';
                    out += buf;
                }
                else
                {
                    out.push_back(static_cast<char>(c));
                }
                break;
        }
    }
    return out;
}

enum class OwnerAssignmentKind : std::uint8_t
{
    None = 0,
    PrimaryDefault = 1,
    AssignedOverride = 2,
};

enum class OwnerResolutionState : std::uint8_t
{
    Unconfigured = 0,
    ResolvedFresh = 1,
    UnresolvedAssignment = 2,
    LevelUnknown = 3,
    StaleLevel = 4,
};

inline char const* ResolutionStateName(OwnerResolutionState st)
{
    switch (st)
    {
        case OwnerResolutionState::Unconfigured:         return "unconfigured";
        case OwnerResolutionState::ResolvedFresh:        return "resolved_fresh";
        case OwnerResolutionState::UnresolvedAssignment: return "unresolved_assignment";
        case OwnerResolutionState::LevelUnknown:         return "level_unknown";
        case OwnerResolutionState::StaleLevel:           return "stale_level";
    }
    return "unknown";
}

struct OwnerXpCapConfig
{
    std::uint32_t levelLead = 3;
    std::uint32_t expansionMaxLevel = 80;
    bool waitForOwnerLevel = true;
    std::uint32_t staleAfterSec = 300;
    std::uint32_t maxConsecutiveFailures = 2;
    std::uint32_t normalRefreshSec = 60;
};

inline std::uint32_t ClampStaleSeconds(std::uint32_t configuredStaleSec, std::uint32_t normalRefreshSec)
{
    if (configuredStaleSec == 0)
        return 0;  // Explicitly disabled age-based staleness.
    std::uint32_t const minSafe = normalRefreshSec > 0 ? normalRefreshSec * 2u : 120u;
    return configuredStaleSec < minSafe ? minSafe : configuredStaleSec;
}

struct OwnerRecord
{
    std::string displayName;
    std::uint32_t resolvedLowGuid = 0;
    std::uint32_t level = 0;
    std::uint32_t lastSuccessAtSec = 0;
    std::uint32_t lastProbeAtSec = 0;
    std::uint32_t nextProbeAtSec = 0;
    std::uint32_t consecutiveFailures = 0;
    bool guidResolved = false;
    bool lastRefreshFailed = false;
    bool everProbed = false;
};

struct BotXpCapResolution
{
    OwnerAssignmentKind assignment = OwnerAssignmentKind::None;
    OwnerResolutionState state = OwnerResolutionState::Unconfigured;
    std::string ownerName;
    std::uint32_t ownerLowGuid = 0;
    std::uint32_t ownerLevel = 0;
    std::uint32_t capLevel = 0;
    bool xpCapped = false;
    bool failClosed = false;
    bool expansionCapped = false;
};

// Allocation-free hot-path projection for map-thread checks (IsXpCapped / OwnerLowFor / OwnerLevelFor).
struct BotXpCapFastResult
{
    OwnerAssignmentKind assignment = OwnerAssignmentKind::None;
    OwnerResolutionState state = OwnerResolutionState::Unconfigured;
    std::uint32_t ownerLowGuid = 0;
    std::uint32_t ownerLevel = 0;
    std::uint32_t capLevel = 0;
    bool xpCapped = false;
    bool failClosed = false;
    bool expansionCapped = false;
};

inline bool IsRecordStale(OwnerRecord const& rec, std::uint32_t nowSec, OwnerXpCapConfig const& cfg)
{
    if (rec.level == 0)
        return false;  // Handled explicitly as LevelUnknown.
    std::uint32_t const failLimit = cfg.maxConsecutiveFailures > 0 ? cfg.maxConsecutiveFailures : 1u;
    if (rec.lastRefreshFailed && rec.consecutiveFailures >= failLimit)
        return true;
    std::uint32_t const effStale = ClampStaleSeconds(cfg.staleAfterSec, cfg.normalRefreshSec);
    if (effStale > 0 && nowSec > rec.lastSuccessAtSec && (nowSec - rec.lastSuccessAtSec) > effStale)
        return true;
    return false;
}

inline std::uint32_t ComputeRetryBackoffSec(std::uint32_t consecutiveFailures, std::uint32_t maxBackoffSec)
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

class OwnerRegistry
{
public:
    // Synchronize primary owner & per-bot overrides (startup or config reload).
    // Preserves fresh OwnerRecords for owners that remain configured; drops removed owners;
    // initializes newly configured owners as UnresolvedAssignment (fail-closed until refreshed).
    void Configure(std::string const& primaryOwnerName,
                   std::unordered_map<std::string, std::string> const& botOwnerOverrides)
    {
        std::string const trimmedPrimary = TrimAscii(primaryOwnerName);
        std::string const nextPrimaryKey = LowerAscii(trimmedPrimary);

        std::unordered_map<std::string, std::string> nextOverrides;
        std::map<std::string, std::string> wantedOwners;  // lower owner key -> display name

        if (!nextPrimaryKey.empty())
            wantedOwners[nextPrimaryKey] = trimmedPrimary;

        for (auto const& [rawBot, rawOwner] : botOwnerOverrides)
        {
            std::string const botKey = LowerAscii(TrimAscii(rawBot));
            std::string const ownerDisplay = TrimAscii(rawOwner);
            if (botKey.empty() || ownerDisplay.empty())
                continue;
            std::string const ownerKey = LowerAscii(ownerDisplay);
            nextOverrides[botKey] = ownerKey;
            if (wantedOwners.find(ownerKey) == wantedOwners.end())
                wantedOwners[ownerKey] = ownerDisplay;
        }

        std::map<std::string, OwnerRecord> nextRecords;
        for (auto const& [ownerKey, displayName] : wantedOwners)
        {
            auto it = _owners.find(ownerKey);
            if (it != _owners.end())
            {
                OwnerRecord rec = it->second;
                rec.displayName = displayName;
                nextRecords[ownerKey] = std::move(rec);
            }
            else
            {
                OwnerRecord rec;
                rec.displayName = displayName;
                nextRecords[ownerKey] = std::move(rec);
            }
        }

        _primaryOwnerName = trimmedPrimary;
        _primaryOwnerKey = nextPrimaryKey;
        _botOverrides = std::move(nextOverrides);
        _owners = std::move(nextRecords);
    }

    // Returns every configured owner display name (primary + all per-bot overrides) in deterministic order.
    std::vector<std::string> ConfiguredOwnerNames() const
    {
        std::vector<std::string> out;
        out.reserve(_owners.size());
        for (auto const& [key, rec] : _owners)
            out.push_back(rec.displayName);
        return out;
    }

    // Returns ONLY owners currently due for a probe (respects per-owner exponential retry backoff for
    // failing/unresolved owners and normal refresh cadence for healthy owners; forces all if forceAll=true).
    std::vector<std::string> OwnersDueForProbe(std::uint32_t nowSec,
                                               OwnerXpCapConfig const& cfg,
                                               bool forceAll = false) const
    {
        std::vector<std::string> out;
        out.reserve(_owners.size());
        std::uint32_t const effStale = ClampStaleSeconds(cfg.staleAfterSec, cfg.normalRefreshSec);
        std::uint32_t const earlyRefreshAge = effStale > 0 ? (effStale / 2u) : 0u;
        for (auto const& [key, rec] : _owners)
        {
            if (forceAll || !rec.everProbed)
            {
                out.push_back(rec.displayName);
                continue;
            }
            if (nowSec >= rec.nextProbeAtSec)
            {
                out.push_back(rec.displayName);
                continue;
            }
            if (earlyRefreshAge > 0 && rec.level > 0 && !rec.lastRefreshFailed &&
                nowSec > rec.lastSuccessAtSec && (nowSec - rec.lastSuccessAtSec) >= earlyRefreshAge)
            {
                out.push_back(rec.displayName);
                continue;
            }
        }
        return out;
    }

    // Record live/cache GUID lookup result for an owner name.
    // - resolvedLowGuid == 0: owner renamed/deleted/missing -> immediately clears GUID & level (UnresolvedAssignment)
    //   and schedules per-owner exponential backoff (5s -> 10s -> 20s -> 40s -> 60s).
    // - resolvedLowGuid changed: different character now holds this name -> immediately clears old level (LevelUnknown).
    void RecordGuidResolution(std::string const& ownerName,
                              std::uint32_t resolvedLowGuid,
                              std::uint32_t nowSec = 0,
                              std::uint32_t maxBackoffSec = 60)
    {
        std::string const key = LowerAscii(TrimAscii(ownerName));
        auto it = _owners.find(key);
        if (it == _owners.end())
            return;
        OwnerRecord& rec = it->second;
        rec.everProbed = true;
        rec.lastProbeAtSec = nowSec;
        if (resolvedLowGuid == 0)
        {
            rec.guidResolved = false;
            rec.resolvedLowGuid = 0;
            rec.level = 0;
            rec.lastSuccessAtSec = 0;
            rec.lastRefreshFailed = true;
            ++rec.consecutiveFailures;
            rec.nextProbeAtSec = nowSec + ComputeRetryBackoffSec(rec.consecutiveFailures, maxBackoffSec);
            return;
        }
        if (!rec.guidResolved || rec.resolvedLowGuid != resolvedLowGuid)
        {
            rec.guidResolved = true;
            rec.resolvedLowGuid = resolvedLowGuid;
            rec.level = 0;
            rec.lastSuccessAtSec = 0;
            rec.lastRefreshFailed = false;
            rec.consecutiveFailures = 0;
            rec.nextProbeAtSec = nowSec;
        }
    }

    // Record the outcome of reading an owner's level (online or DB).
    // - resolvedLowGuid == 0 -> marks UnresolvedAssignment immediately with per-owner backoff.
    // - level > 0 -> marks ResolvedFresh, resets failure counters, schedules next normal refresh.
    // - level == 0 -> records read failure (keeps LevelUnknown if never read, or trips StaleLevel after limit)
    //   with per-owner exponential retry backoff.
    void RecordLevelRead(std::string const& ownerName,
                         std::uint32_t resolvedLowGuid,
                         std::uint32_t level,
                         std::uint32_t nowSec,
                         std::uint32_t normalRefreshSec = 60)
    {
        std::string const key = LowerAscii(TrimAscii(ownerName));
        auto it = _owners.find(key);
        if (it == _owners.end())
            return;
        if (resolvedLowGuid == 0)
        {
            RecordGuidResolution(ownerName, 0, nowSec, normalRefreshSec);
            return;
        }
        OwnerRecord& rec = it->second;
        rec.everProbed = true;
        rec.lastProbeAtSec = nowSec;
        if (!rec.guidResolved || rec.resolvedLowGuid != resolvedLowGuid)
        {
            rec.guidResolved = true;
            rec.resolvedLowGuid = resolvedLowGuid;
            rec.level = 0;
            rec.lastSuccessAtSec = 0;
        }
        if (level > 0)
        {
            rec.level = level;
            rec.lastSuccessAtSec = nowSec;
            rec.consecutiveFailures = 0;
            rec.lastRefreshFailed = false;
            rec.nextProbeAtSec = nowSec + (normalRefreshSec > 0 ? normalRefreshSec : 60u);
        }
        else
        {
            rec.lastRefreshFailed = true;
            ++rec.consecutiveFailures;
            rec.nextProbeAtSec = nowSec + ComputeRetryBackoffSec(rec.consecutiveFailures, normalRefreshSec);
        }
    }

    bool HasPrimaryOwner() const { return !_primaryOwnerKey.empty(); }
    std::string PrimaryOwnerName() const { return _primaryOwnerName; }

    bool HasOverrideForBot(std::string const& botName) const
    {
        return _botOverrides.find(LowerAscii(TrimAscii(botName))) != _botOverrides.end();
    }

    std::uint32_t PrimaryOwnerLow() const
    {
        if (_primaryOwnerKey.empty())
            return 0;
        auto it = _owners.find(_primaryOwnerKey);
        if (it == _owners.end() || !it->second.guidResolved)
            return 0;
        return it->second.resolvedLowGuid;
    }

    std::uint32_t PrimaryOwnerLevel(std::uint32_t nowSec, OwnerXpCapConfig const& cfg) const
    {
        if (_primaryOwnerKey.empty())
            return 0;
        auto it = _owners.find(_primaryOwnerKey);
        if (it == _owners.end() || !it->second.guidResolved || it->second.resolvedLowGuid == 0 ||
            it->second.level == 0 || IsRecordStale(it->second, nowSec, cfg))
        {
            return 0;
        }
        return it->second.level;
    }

    // True iff at least one configured owner is due for an early/retry probe RIGHT NOW (respects backoff!).
    bool AnyOwnerDueForProbe(std::uint32_t nowSec, OwnerXpCapConfig const& cfg) const
    {
        std::uint32_t const effStale = ClampStaleSeconds(cfg.staleAfterSec, cfg.normalRefreshSec);
        std::uint32_t const earlyRefreshAge = effStale > 0 ? (effStale / 2u) : 0u;
        for (auto const& [key, rec] : _owners)
        {
            if (!rec.everProbed || nowSec >= rec.nextProbeAtSec)
                return true;
            if (earlyRefreshAge > 0 && rec.level > 0 && !rec.lastRefreshFailed &&
                nowSec > rec.lastSuccessAtSec && (nowSec - rec.lastSuccessAtSec) >= earlyRefreshAge)
            {
                return true;
            }
        }
        return false;
    }

    std::set<std::uint32_t> AllResolvedOwnerLows() const
    {
        std::set<std::uint32_t> lows;
        for (auto const& [key, rec] : _owners)
        {
            if (rec.guidResolved && rec.resolvedLowGuid != 0)
                lows.insert(rec.resolvedLowGuid);
        }
        return lows;
    }

    bool IsAnyResolvedOwnerLow(std::uint32_t lowGuid) const
    {
        if (lowGuid == 0)
            return false;
        for (auto const& [key, rec] : _owners)
        {
            if (rec.guidResolved && rec.resolvedLowGuid == lowGuid)
                return true;
        }
        return false;
    }

    OwnerResolutionState OwnerStateForName(std::string const& ownerName,
                                           std::uint32_t nowSec,
                                           OwnerXpCapConfig const& cfg) const
    {
        std::string const key = LowerAscii(TrimAscii(ownerName));
        if (key.empty())
            return OwnerResolutionState::Unconfigured;
        auto it = _owners.find(key);
        if (it == _owners.end())
            return OwnerResolutionState::Unconfigured;
        OwnerRecord const& rec = it->second;
        if (!rec.guidResolved || rec.resolvedLowGuid == 0)
            return OwnerResolutionState::UnresolvedAssignment;
        if (rec.level == 0)
            return OwnerResolutionState::LevelUnknown;
        if (IsRecordStale(rec, nowSec, cfg))
            return OwnerResolutionState::StaleLevel;
        return OwnerResolutionState::ResolvedFresh;
    }

    // Fast-path projection avoiding return-value owner-name copying on hot map-thread calls
    // (IsXpCapped / OwnerLowFor / OwnerLevelFor).
    BotXpCapFastResult ResolveFastForBot(std::string const& botName,
                                         std::uint32_t botLevel,
                                         std::uint32_t nowSec,
                                         OwnerXpCapConfig const& cfg,
                                         std::string* outOwnerName = nullptr) const
    {
        BotXpCapFastResult res;
        bool const expCapped = (cfg.expansionMaxLevel > 0 && botLevel >= cfg.expansionMaxLevel);
        res.expansionCapped = expCapped;

        std::string const botKey = LowerAscii(TrimAscii(botName));
        std::string const* ownerKeyPtr = nullptr;
        auto ovIt = _botOverrides.find(botKey);
        if (ovIt != _botOverrides.end())
        {
            res.assignment = OwnerAssignmentKind::AssignedOverride;
            ownerKeyPtr = &ovIt->second;
        }
        else if (!_primaryOwnerKey.empty())
        {
            res.assignment = OwnerAssignmentKind::PrimaryDefault;
            ownerKeyPtr = &_primaryOwnerKey;
        }
        else
        {
            res.assignment = OwnerAssignmentKind::None;
            res.state = OwnerResolutionState::Unconfigured;
            res.capLevel = cfg.expansionMaxLevel;
            res.xpCapped = expCapped;
            res.failClosed = false;
            return res;
        }

        // WaitForOwnerLevel only controls initial startup wait on PrimaryDefault before first read;
        // explicit AssignedOverride, UnresolvedAssignment (after probe), repeated LevelUnknown failures,
        // and StaleLevel ALWAYS fail closed!
        bool const failClosedOnInitialUnknown =
            (res.assignment == OwnerAssignmentKind::AssignedOverride) ? true : cfg.waitForOwnerLevel;
        std::uint32_t const failLimit = cfg.maxConsecutiveFailures > 0 ? cfg.maxConsecutiveFailures : 1u;

        auto recIt = _owners.find(*ownerKeyPtr);
        if (recIt == _owners.end())
        {
            res.state = OwnerResolutionState::UnresolvedAssignment;
            res.capLevel = expCapped ? cfg.expansionMaxLevel : 0;
            res.failClosed = true;
            res.xpCapped = true;
            return res;
        }

        OwnerRecord const& rec = recIt->second;
        if (outOwnerName)
            *outOwnerName = rec.displayName;

        if (!rec.guidResolved || rec.resolvedLowGuid == 0)
        {
            res.state = OwnerResolutionState::UnresolvedAssignment;
            res.ownerLowGuid = 0;
            res.ownerLevel = 0;
            res.capLevel = expCapped ? cfg.expansionMaxLevel : 0;
            res.failClosed = failClosedOnInitialUnknown || rec.everProbed;
            res.xpCapped = expCapped || res.failClosed;
            return res;
        }

        res.ownerLowGuid = rec.resolvedLowGuid;

        if (rec.level == 0)
        {
            res.state = OwnerResolutionState::LevelUnknown;
            res.ownerLevel = 0;
            res.capLevel = expCapped ? cfg.expansionMaxLevel : 0;
            res.failClosed = failClosedOnInitialUnknown || (rec.consecutiveFailures >= failLimit);
            res.xpCapped = expCapped || res.failClosed;
            return res;
        }

        if (IsRecordStale(rec, nowSec, cfg))
        {
            // StaleLevel ALWAYS fails closed (never regresses to uncapped XP when waitForOwnerLevel=false).
            res.state = OwnerResolutionState::StaleLevel;
            res.ownerLevel = 0;
            res.capLevel = expCapped ? cfg.expansionMaxLevel : 0;
            res.failClosed = true;
            res.xpCapped = true;
            return res;
        }

        res.state = OwnerResolutionState::ResolvedFresh;
        res.ownerLevel = rec.level;
        std::uint64_t rawCap =
            static_cast<std::uint64_t>(rec.level) + static_cast<std::uint64_t>(cfg.levelLead);
        if (cfg.expansionMaxLevel > 0 && rawCap > cfg.expansionMaxLevel)
            rawCap = cfg.expansionMaxLevel;
        res.capLevel = static_cast<std::uint32_t>(rawCap);
        res.failClosed = false;
        res.xpCapped = expCapped || (botLevel >= res.capLevel);
        return res;
    }

    // Full resolution including owner display name (for logging, `.companion status`, and live_positions.json).
    BotXpCapResolution ResolveForBot(std::string const& botName,
                                     std::uint32_t botLevel,
                                     std::uint32_t nowSec,
                                     OwnerXpCapConfig const& cfg) const
    {
        BotXpCapResolution res;
        BotXpCapFastResult const fast = ResolveFastForBot(botName, botLevel, nowSec, cfg, &res.ownerName);
        res.assignment = fast.assignment;
        res.state = fast.state;
        res.ownerLowGuid = fast.ownerLowGuid;
        res.ownerLevel = fast.ownerLevel;
        res.capLevel = fast.capLevel;
        res.xpCapped = fast.xpCapped;
        res.failClosed = fast.failClosed;
        res.expansionCapped = fast.expansionCapped;
        return res;
    }

private:
    std::string _primaryOwnerName;
    std::string _primaryOwnerKey;
    std::unordered_map<std::string, std::string> _botOverrides;  // lower bot name -> lower owner key
    std::map<std::string, OwnerRecord> _owners;                  // lower owner key -> OwnerRecord
};

}  // namespace CompanionOwnerXpCap

#endif  // PLAYERBOTS_COMPANIONOWNERXPCAP_LOGIC_H
