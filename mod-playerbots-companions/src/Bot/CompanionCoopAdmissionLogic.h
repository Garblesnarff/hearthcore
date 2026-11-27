/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANION_COOP_ADMISSION_LOGIC_H
#define PLAYERBOTS_COMPANION_COOP_ADMISSION_LOGIC_H

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <istream>
#include <sstream>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

// Pure C++17 native cooperative admission, resource-limit, telemetry-parser, and group-cap controller (Issue #12).
// Zero AzerothCore dependencies so CompanionBrain.cpp, CompanionBotMgr.cpp, LfgActions.cpp, and standalone
// native C++17 tests execute the exact same parsers, fail-closed telemetry, persisted JSON hysteresis, and
// atomic resource-token rules.
namespace CompanionCoopAdmission
{
inline constexpr std::uint32_t DEFAULT_MAX_MANAGED_GROUPS = 1;
inline constexpr std::uint32_t DEFAULT_MAX_TOTAL_INSTANCES = 4;
inline constexpr std::uint32_t DEFAULT_MAX_ONLINE_POPULATION = 45;
inline constexpr std::uint32_t DEFAULT_MAX_TELEMETRY_AGE_SEC = 30;
inline constexpr std::uint32_t DEFAULT_TOKEN_TTL_SEC = 180;
inline constexpr std::size_t TICK_RING_CAPACITY = 256;
inline constexpr std::size_t TICK_MIN_SAMPLES = 20;
inline constexpr char const* DEFAULT_HEADROOM_STATE_FILE = "companion_coop_headroom.json";

struct AdmissionConfig
{
    bool coopEnabled = false;  // Safe default OFF!
    std::unordered_set<std::uint32_t> enabledDungeonMaps;
    std::uint32_t maxManagedGroups = DEFAULT_MAX_MANAGED_GROUPS;
    std::uint32_t maxTotalInstances = DEFAULT_MAX_TOTAL_INSTANCES;
    std::uint32_t maxOnlinePopulation = DEFAULT_MAX_ONLINE_POPULATION;
    double minAvailRamMib = 512.0;
    double recoverAvailRamMib = 640.0;
    double maxTickP99Ms = 45.0;
    double recoverTickP99Ms = 38.0;
    double maxSwapIoPagesPerSec = 25.0;
    double recoverSwapIoPagesPerSec = 10.0;
    double maxCpuPsiSomeAvg10 = 20.0;
    double recoverCpuPsiSomeAvg10 = 10.0;
    std::uint32_t maxTelemetryAgeSec = DEFAULT_MAX_TELEMETRY_AGE_SEC;
    std::uint32_t tokenTtlSec = DEFAULT_TOKEN_TTL_SEC;
};

struct TelemetrySnapshot
{
    bool valid = false;
    std::uint32_t sampledAt = 0;
    bool hasMemAvail = false;
    double memAvailMib = 0.0;
    bool hasTickP99 = false;
    double tickP99Ms = 0.0;
    bool hasSwapIo = false;
    double swapIoPagesPerSec = 0.0;
    bool hasCpuPsi = false;
    double cpuPsiSomeAvg10 = 0.0;
    bool hasInstances = false;
    std::uint32_t activeInstances = 0;  // ALL live instances (managed + ordinary/human dungeon/BG/arena!)
    bool hasPopulation = false;
    std::uint32_t onlinePopulation = 0;
};

struct AdmissionToken
{
    std::uint64_t tokenId = 0;
    std::uint64_t opportunityId = 0;
    std::uint32_t targetMapId = 0;
    std::uint32_t issuedAt = 0;
    std::uint32_t expiresAt = 0;
    bool committed = false;  // once committed, existing combat/run continues safely regardless of headroom pause!
};

struct AdmissionDecision
{
    bool admitted = false;
    bool headroomPaused = true;
    std::uint64_t tokenId = 0;
    std::uint32_t effectiveGroups = 0;
    std::uint32_t effectiveInstances = 0;
    std::string reason;
    std::vector<std::string> detailReasons;
};

inline bool IsValidNonNeg(double v)
{
    return std::isfinite(v) && v >= 0.0;
}

// Pure C++17 /proc/meminfo parser -> MemAvailable in MiB.
inline bool ParseMemAvailableMib(std::istream& in, double& outMib)
{
    std::string key;
    std::uint64_t kb = 0;
    std::string rest;
    while (in >> key >> kb)
    {
        std::getline(in, rest);
        if (key == "MemAvailable:")
        {
            outMib = static_cast<double>(kb) / 1024.0;
            return IsValidNonNeg(outMib);
        }
    }
    return false;
}

// Pure C++17 /proc/vmstat parser -> cumulative (pswpin + pswpout) pages.
inline bool ParseVmstatSwapPages(std::istream& in, std::uint64_t& outTotalPages)
{
    std::string key;
    std::uint64_t val = 0;
    bool gotIn = false;
    bool gotOut = false;
    std::uint64_t pIn = 0;
    std::uint64_t pOut = 0;
    while (in >> key >> val)
    {
        if (key == "pswpin")
        {
            pIn = val;
            gotIn = true;
        }
        else if (key == "pswpout")
        {
            pOut = val;
            gotOut = true;
        }
        if (gotIn && gotOut)
        {
            outTotalPages = pIn + pOut;
            return true;
        }
    }
    return false;
}

// Pure C++17 /proc/pressure/cpu parser -> "some avg10=<pct>".
inline bool ParseCpuPsiSomeAvg10(std::istream& in, double& outAvg10)
{
    std::string line;
    while (std::getline(in, line))
    {
        if (line.rfind("some ", 0) == 0)
        {
            std::size_t pos = line.find("avg10=");
            if (pos == std::string::npos)
                return false;
            try
            {
                std::size_t consumed = 0;
                double v = std::stod(line.substr(pos + 6), &consumed);
                if (consumed > 0 && IsValidNonNeg(v))
                {
                    outAvg10 = v;
                    return true;
                }
            }
            catch (...)
            {
                return false;
            }
            return false;
        }
    }
    return false;
}

// Python-compatible headroom_state.json parser: reads `"paused": true|false` inside a complete JSON object `{...}`.
// Rejects truncated/torn files (missing opening '{' or closing '}') so corrupt state ALWAYS fails CLOSED!
inline bool ParseHeadroomPausedJson(std::string const& raw, bool& outPaused)
{
    std::size_t openBrace = raw.find('{');
    std::size_t closeBrace = raw.rfind('}');
    if (openBrace == std::string::npos || closeBrace == std::string::npos || closeBrace <= openBrace)
        return false;
    std::size_t keyPos = raw.find("\"paused\"", openBrace);
    if (keyPos == std::string::npos || keyPos >= closeBrace)
        return false;
    std::size_t i = keyPos + 8;
    while (i < closeBrace && std::isspace(static_cast<unsigned char>(raw[i])))
        ++i;
    if (i >= closeBrace || raw[i] != ':')
        return false;
    ++i;
    while (i < closeBrace && std::isspace(static_cast<unsigned char>(raw[i])))
        ++i;
    if (i + 4 <= closeBrace && raw.compare(i, 4, "true") == 0)
    {
        std::size_t end = i + 4;
        if (end < closeBrace && std::isalnum(static_cast<unsigned char>(raw[end])))
            return false;
        outPaused = true;
        return true;
    }
    if (i + 5 <= closeBrace && raw.compare(i, 5, "false") == 0)
    {
        std::size_t end = i + 5;
        if (end < closeBrace && std::isalnum(static_cast<unsigned char>(raw[end])))
            return false;
        outPaused = false;
        return true;
    }
    return false;
}

inline std::string FormatHeadroomStateJson(bool paused, std::uint32_t updatedAt,
                                           std::vector<std::string> const& reasons)
{
    std::ostringstream oss;
    oss << "{\n  \"paused\": " << (paused ? "true" : "false")
        << ",\n  \"updated_at\": " << updatedAt
        << ",\n  \"reasons\": [";
    for (std::size_t idx = 0; idx < reasons.size(); ++idx)
    {
        if (idx > 0)
            oss << ", ";
        oss << '"';
        for (char c : reasons[idx])
        {
            if (c == '"' || c == '\\')
                oss << '\\' << c;
            else if (static_cast<unsigned char>(c) >= 0x20)
                oss << c;
        }
        oss << '"';
    }
    oss << "]\n}\n";
    return oss.str();
}

// Fixed-capacity ring buffer tracking real worldserver tick durations (ms) on the world thread.
// Fails closed (HasEnoughSamples() == false) until TICK_MIN_SAMPLES ticks have been recorded.
class TickRingBuffer
{
public:
    void Record(std::uint32_t elapsedMs)
    {
        _samples[_writeIdx] = static_cast<double>(elapsedMs);
        _writeIdx = (_writeIdx + 1) % TICK_RING_CAPACITY;
        if (_count < TICK_RING_CAPACITY)
            ++_count;
    }

    void Reset()
    {
        _writeIdx = 0;
        _count = 0;
    }

    std::size_t Count() const { return _count; }
    bool HasEnoughSamples() const { return _count >= TICK_MIN_SAMPLES; }

    bool Percentile(double pct, double& outMs) const
    {
        if (!HasEnoughSamples() || !std::isfinite(pct) || pct < 0.0 || pct > 100.0)
            return false;
        std::vector<double> sorted(_samples.begin(), _samples.begin() + static_cast<std::ptrdiff_t>(_count));
        std::sort(sorted.begin(), sorted.end());
        double const rawIdx = (pct / 100.0) * static_cast<double>(sorted.size() - 1);
        std::size_t const idx = std::min(sorted.size() - 1, static_cast<std::size_t>(std::llround(rawIdx)));
        outMs = sorted[idx];
        return std::isfinite(outMs);
    }

private:
    std::array<double, TICK_RING_CAPACITY> _samples{};
    std::size_t _writeIdx = 0;
    std::size_t _count = 0;
};

// Strict parser for comma-separated integer map IDs (e.g. "34,36,43"). Empty string -> empty allowlist (all closed).
inline std::unordered_set<std::uint32_t> ParseEnabledMapList(std::string const& raw)
{
    std::unordered_set<std::uint32_t> out;
    std::size_t pos = 0;
    while (pos < raw.size())
    {
        std::size_t comma = raw.find(',', pos);
        std::string tok = raw.substr(pos, comma == std::string::npos ? std::string::npos : comma - pos);
        std::size_t first = tok.find_first_not_of(" \t\r\n");
        if (first != std::string::npos)
        {
            std::size_t last = tok.find_last_not_of(" \t\r\n");
            tok = tok.substr(first, last - first + 1);
            bool allDigits = !tok.empty();
            std::uint64_t val = 0;
            for (char c : tok)
            {
                if (c < '0' || c > '9')
                {
                    allDigits = false;
                    break;
                }
                val = val * 10 + static_cast<std::uint64_t>(c - '0');
                if (val > 0xFFFFFFFFu)
                {
                    allDigits = false;
                    break;
                }
            }
            if (allDigits && val > 0)
                out.insert(static_cast<std::uint32_t>(val));
        }
        if (comma == std::string::npos)
            break;
        pos = comma + 1;
    }
    return out;
}

// Serialized native admission controller (caller holds gAdmissionMutex).
// Guards ONLY new automated queue admissions and proposal acceptances — NEVER interrupts existing human
// gameplay, existing combat, or already-committed cooperative groups.
class AdmissionController
{
public:
    void LoadHysteresisState(bool paused) { _headroomPaused = paused; }
    bool IsHeadroomPaused() const { return _headroomPaused; }

    void UpdateTelemetry(TelemetrySnapshot const& snap)
    {
        _lastTelemetry = snap;
    }

    TelemetrySnapshot const& LastTelemetry() const { return _lastTelemetry; }

    std::size_t ActiveTokenCount() const { return _tokens.size(); }

    void ExpireStaleTokens(std::uint32_t nowSec)
    {
        for (auto it = _tokens.begin(); it != _tokens.end();)
        {
            if (!it->second.committed && it->second.expiresAt <= nowSec)
                it = _tokens.erase(it);
            else
                ++it;
        }
    }

    // Frees committed tokens whose opportunity is no longer present in active/assembling DB runs (prevents token leak!).
    std::size_t PruneCommittedTokensNotIn(std::unordered_set<std::uint64_t> const& liveOppIds)
    {
        std::size_t removed = 0;
        for (auto it = _tokens.begin(); it != _tokens.end();)
        {
            if (it->second.committed && liveOppIds.count(it->first) == 0)
            {
                it = _tokens.erase(it);
                ++removed;
            }
            else
                ++it;
        }
        return removed;
    }

    // Marks an already-admitted opportunity as committed (protects it from TTL expiry; existing runs continue
    // safely even if host headroom subsequently pauses new admissions).
    bool MarkCommitted(std::uint64_t opportunityId)
    {
        auto it = _tokens.find(opportunityId);
        if (it == _tokens.end())
            return false;
        it->second.committed = true;
        return true;
    }

    bool ReleaseToken(std::uint64_t opportunityId)
    {
        return _tokens.erase(opportunityId) > 0;
    }

    bool HasToken(std::uint64_t opportunityId) const
    {
        return _tokens.find(opportunityId) != _tokens.end();
    }

    bool HasCommittedToken(std::uint64_t opportunityId) const
    {
        auto it = _tokens.find(opportunityId);
        return it != _tokens.end() && it->second.committed;
    }

    // Evaluates telemetry & updates persisted hysteresis state (without consuming a token or conflating cap hits).
    // Fails CLOSED on missing, stale, clock-skewed, negative, or NaN/Inf telemetry.
    bool EvaluateHeadroom(AdmissionConfig const& cfg, TelemetrySnapshot const& snap, std::uint32_t nowSec,
                          std::vector<std::string>& outReasons)
    {
        outReasons.clear();
        if (!snap.valid || snap.sampledAt == 0)
        {
            outReasons.push_back("missing_telemetry_snapshot");
        }
        else if (nowSec < snap.sampledAt || (nowSec - snap.sampledAt) > cfg.maxTelemetryAgeSec)
        {
            outReasons.push_back("stale_telemetry");
        }

        double const ramThresh = _headroomPaused ? cfg.recoverAvailRamMib : cfg.minAvailRamMib;
        double const tickThresh = _headroomPaused ? cfg.recoverTickP99Ms : cfg.maxTickP99Ms;
        double const swapThresh = _headroomPaused ? cfg.recoverSwapIoPagesPerSec : cfg.maxSwapIoPagesPerSec;
        double const cpuThresh = _headroomPaused ? cfg.recoverCpuPsiSomeAvg10 : cfg.maxCpuPsiSomeAvg10;

        if (!snap.hasMemAvail || !IsValidNonNeg(snap.memAvailMib))
            outReasons.push_back("missing_or_invalid_ram_telemetry");
        else if (snap.memAvailMib < ramThresh)
            outReasons.push_back("low_ram_headroom");

        if (!snap.hasTickP99 || !IsValidNonNeg(snap.tickP99Ms))
            outReasons.push_back("missing_or_invalid_tick_telemetry");
        else if (snap.tickP99Ms > tickThresh)
            outReasons.push_back("high_tick_p99");

        if (!snap.hasSwapIo || !IsValidNonNeg(snap.swapIoPagesPerSec))
            outReasons.push_back("missing_or_invalid_swap_telemetry");
        else if (snap.swapIoPagesPerSec > swapThresh)
            outReasons.push_back("high_swap_io");

        if (!snap.hasCpuPsi || !IsValidNonNeg(snap.cpuPsiSomeAvg10))
            outReasons.push_back("missing_or_invalid_cpu_psi_telemetry");
        else if (snap.cpuPsiSomeAvg10 > cpuThresh)
            outReasons.push_back("high_cpu_psi");

        if (!snap.hasInstances)
            outReasons.push_back("missing_instance_telemetry");

        if (!snap.hasPopulation)
            outReasons.push_back("missing_population_telemetry");

        bool const ok = outReasons.empty();
        _headroomPaused = !ok;
        return ok;
    }

    // Atomically checks ALL flags, dungeon allowlist, enrollment, no-forced-login rule, fresh fail-closed
    // telemetry + hysteresis, AND group/instance/population caps, minting an uncommitted resource token on success.
    // activeOrAssemblingOppIds MUST include ALL other active AND assembling live runs!
    // If opportunityId ALREADY holds a live token (e.g. proposal accept after queue join, or committed run),
    // returns admitted=true without blocking existing runs!
    AdmissionDecision TryAdmitNewQueueOrProposal(AdmissionConfig const& cfg, TelemetrySnapshot const& snap,
                                                 std::uint64_t opportunityId, std::uint32_t targetMapId,
                                                 std::unordered_set<std::uint64_t> const& committedOppIds,
                                                 std::unordered_set<std::uint64_t> const& assemblingOppIds,
                                                 bool allMembersEnrolled, std::uint32_t additionalLoginsNeeded,
                                                 std::uint32_t nowSec)
    {
        AdmissionDecision dec;
        ExpireStaleTokens(nowSec);

        if (opportunityId == 0)
        {
            dec.headroomPaused = _headroomPaused;
            dec.reason = "invalid_opportunity_id";
            return dec;
        }

        // Already-committed runs continue safely even if Coop.Enabled is toggled OFF or headroom pauses!
        auto existingIt = _tokens.find(opportunityId);
        if (existingIt != _tokens.end() && existingIt->second.committed)
        {
            dec.admitted = true;
            dec.headroomPaused = _headroomPaused;
            dec.tokenId = existingIt->second.tokenId;
            dec.reason = "already_committed";
            return dec;
        }
        if (committedOppIds.count(opportunityId) > 0)
        {
            AdmissionToken tok{++_nextTokenId, opportunityId, targetMapId, nowSec, nowSec + cfg.tokenTtlSec, true};
            _tokens[opportunityId] = tok;
            dec.admitted = true;
            dec.headroomPaused = _headroomPaused;
            dec.tokenId = tok.tokenId;
            dec.reason = "already_committed";
            return dec;
        }

        // New (uncommitted) admissions strictly require coopEnabled=true!
        if (!cfg.coopEnabled)
        {
            dec.headroomPaused = _headroomPaused;
            dec.reason = "coop_disabled";
            return dec;
        }
        if (existingIt != _tokens.end())
        {
            dec.admitted = true;
            dec.headroomPaused = _headroomPaused;
            dec.tokenId = existingIt->second.tokenId;
            dec.reason = "already_admitted";
            return dec;
        }

        if (targetMapId == 0 || cfg.enabledDungeonMaps.count(targetMapId) == 0)
        {
            dec.headroomPaused = _headroomPaused;
            dec.reason = "unsupported_or_disabled_dungeon";
            return dec;
        }
        if (!allMembersEnrolled)
        {
            dec.headroomPaused = _headroomPaused;
            dec.reason = "unenrolled_member";
            return dec;
        }
        if (additionalLoginsNeeded > 0)
        {
            dec.headroomPaused = _headroomPaused;
            dec.reason = "forced_mass_login_forbidden";
            return dec;
        }

        // Fail-closed telemetry + persisted hysteresis check (NEVER interrupts existing combat/committed groups).
        bool const hrOk = EvaluateHeadroom(cfg, snap, nowSec, dec.detailReasons);
        dec.headroomPaused = _headroomPaused;
        if (!hrOk)
        {
            dec.reason = dec.detailReasons.empty() ? "headroom_blocked" : dec.detailReasons.front();
            return dec;
        }

        // Count ALL other committed groups + other assembling groups + other uncommitted pending tokens (deduped!).
        std::unordered_set<std::uint64_t> otherActiveGroups = committedOppIds;
        std::unordered_set<std::uint64_t> otherPendingOpps;
        for (std::uint64_t aid : assemblingOppIds)
        {
            if (aid != opportunityId && otherActiveGroups.count(aid) == 0)
            {
                otherActiveGroups.insert(aid);
                otherPendingOpps.insert(aid);
            }
        }
        for (auto const& [oid, tok] : _tokens)
        {
            if (oid == opportunityId)
                continue;
            otherActiveGroups.insert(oid);
            if (!tok.committed && committedOppIds.count(oid) == 0)
                otherPendingOpps.insert(oid);
        }

        std::uint32_t const effectiveGroups = static_cast<std::uint32_t>(otherActiveGroups.size());
        // Every uncommitted pending proposal reserves 1 instance slot on top of ALL live instances (human + managed).
        std::uint32_t const effectiveInstances = snap.activeInstances + static_cast<std::uint32_t>(otherPendingOpps.size());
        dec.effectiveGroups = effectiveGroups;
        dec.effectiveInstances = effectiveInstances;

        if (effectiveGroups >= cfg.maxManagedGroups)
        {
            dec.reason = "group_cap_reached";
            dec.detailReasons.push_back(dec.reason);
            return dec;
        }
        if (effectiveInstances >= cfg.maxTotalInstances)
        {
            dec.reason = "instance_cap_reached";
            dec.detailReasons.push_back(dec.reason);
            return dec;
        }
        if (snap.onlinePopulation >= cfg.maxOnlinePopulation)
        {
            dec.reason = "population_cap_reached";
            dec.detailReasons.push_back(dec.reason);
            return dec;
        }

        AdmissionToken tok{
            ++_nextTokenId,
            opportunityId,
            targetMapId,
            nowSec,
            nowSec + std::max<std::uint32_t>(1, cfg.tokenTtlSec),
            false,
        };
        _tokens[opportunityId] = tok;
        dec.admitted = true;
        dec.tokenId = tok.tokenId;
        dec.effectiveGroups = effectiveGroups + 1;
        dec.effectiveInstances = effectiveInstances + 1;
        dec.reason = "admitted";
        return dec;
    }

private:
    bool _headroomPaused = false;
    std::uint64_t _nextTokenId = 0;
    TelemetrySnapshot _lastTelemetry;
    std::unordered_map<std::uint64_t, AdmissionToken> _tokens;
};

}  // namespace CompanionCoopAdmission

#endif
