/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "CompanionLease.h"

#include <algorithm>
#include <cctype>
#include <iomanip>
#include <mutex>
#include <set>
#include <sstream>
#include <unordered_map>
#include <vector>

#include "CompanionBotMgr.h"
#include "CompanionBrain.h"
#include "CompanionTrip.h"
#include "Creature.h"
#include "GameObject.h"
#include "GameTime.h"
#include "Item.h"
#include "Log.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "Spell.h"
#include "SpellInfo.h"

namespace
{
// Interrupt lines with allowed=0 (and opportunity lines) are logged at most once per kind per this many seconds.
constexpr uint32 INTERRUPT_LOG_SECONDS = 30;
constexpr uint32 REFUSED_LOG_SECONDS = 60;
constexpr size_t WHY_NOT_DESTINATIONS = 3;
// Danger escape (F1): fights against units at or above bot level + DangerLevelGap, counted in this window.
constexpr uint32 DANGER_WINDOW_SEC = 600;
constexpr size_t DANGER_FIGHTS = 2;
constexpr uint32 ITEM_HEARTHSTONE = 6948;   // item_template entry of the hearthstone (CompanionTransport)

struct DestStats
{
    std::string activity;
    uint32 lastAttemptSec = 0;
    uint32 fails = 0;
    uint32 cooldownUntilSec = 0;
    std::string lastReason;
};

struct BlockNote
{
    std::string text;
    uint32 atSec = 0;
};

struct LeaseState
{
    std::string name;   // bot name, for the log lines
    CompanionLease::Info info;
    bool hasBest = false;
    float bestProgress = 0.0f;
    std::unordered_map<std::string, DestStats> dests;         // destination key -> statistics (session)
    std::unordered_map<std::string, BlockNote> blocks;        // activity -> last reported blocking condition
    std::unordered_map<std::string, uint32> lastLogSec;       // rate-limit key -> second of the last line
    bool inFight = false;                                     // in combat on the last tick (a fight starts at the edge)
    bool wasDead = false;                                     // dead on the last tick (corpse-camp detection)
    std::vector<uint32> deathSecs;                            // death timestamps during this lease (rolling 10m)
    std::vector<uint32> dangerSecs;                           // start seconds of the danger fights in the window
    uint32 dangerEntry = 0;                                   // attacker entry of the last danger fight
    uint32 dangerAttLevel = 0;
    uint32 progressEvents = 0;                                // improvements reported by Progress() (this lease)
    uint32 dangerProgressMark = 0;                            // progressEvents when the first danger fight began
    bool dangerDeathEscape = false;                           // died in a danger fight or danger zone: revive safely
};

std::mutex gLeaseMutex;                                 // everything below
std::unordered_map<uint32, LeaseState> gLeases;         // low guid -> lease state
std::set<std::string> gRegistered;                      // activities registered by later code (quest, craft, fish)

uint32 NowSec() { return static_cast<uint32>(GameTime::GetGameTime().count()); }

uint32 StallSeconds() { return std::max<uint32>(10, sPlayerbotAIConfig.companionLeaseStallSeconds); }
uint32 CooldownSeconds() { return sPlayerbotAIConfig.companionLeaseCooldownMinutes * 60; }
uint32 RetryBudget() { return std::max<uint32>(1, sPlayerbotAIConfig.companionLeaseRetryBudget); }
uint32 MaxSeconds() { return std::max<uint32>(1, sPlayerbotAIConfig.companionLeaseMaxMinutes) * 60; }
bool DangerEscapeOn() { return sPlayerbotAIConfig.companionDangerEscapeEnabled; }
bool PauseDiag() { return sPlayerbotAIConfig.companionLeasePauseDiag; }
uint32 DangerLevelGap() { return sPlayerbotAIConfig.companionDangerLevelGap; }

// "duty:bank" -> "bank"; "quest:123" -> "quest"; "fish" -> "fish".
std::string ActivityOf(std::string const& owner)
{
    size_t const colon = owner.find(':');
    if (colon == std::string::npos)
        return owner;
    if (owner.compare(0, colon, "duty") == 0)
    {
        size_t const next = owner.find(':', colon + 1);
        return owner.substr(colon + 1, next == std::string::npos ? std::string::npos : next - colon - 1);
    }
    return owner.substr(0, colon);
}

char const* KindName(CompanionLease::Interrupt kind)
{
    switch (kind)
    {
        case CompanionLease::Interrupt::Combat:
            return "combat";
        case CompanionLease::Interrupt::Death:
            return "death";
        case CompanionLease::Interrupt::OwnerCommand:
            return "owner-cmd";
        case CompanionLease::Interrupt::Group:
            return "group";
        case CompanionLease::Interrupt::Node:
            return "node";
        case CompanionLease::Interrupt::Corpse:
            return "corpse";
        case CompanionLease::Interrupt::RareMob:
            return "raremob";
        case CompanionLease::Interrupt::Quest:
            return "quest";
    }
    return "unknown";
}

char const* OutcomeName(CompanionLease::Outcome outcome)
{
    switch (outcome)
    {
        case CompanionLease::Outcome::Success:
            return "success";
        case CompanionLease::Outcome::Failure:
            return "failure";
        case CompanionLease::Outcome::Neutral:
            return "neutral";
    }
    return "neutral";
}

std::string PolicyText(CompanionLease::Policy const& policy)
{
    std::ostringstream out;
    bool any = false;
    auto add = [&out, &any](bool on, char const* name)
    {
        if (!on)
            return;
        out << (any ? "," : "") << name;
        any = true;
    };
    add(policy.allowNodes, "node");
    add(policy.allowCorpses, "corpse");
    add(policy.allowRareMobs, "raremob");
    add(policy.allowQuest, "quest");
    add(policy.allowRpg, "rpg");
    if (!any)
        out << "none";
    out << ",near=" << static_cast<uint32>(policy.nearYards);
    return out.str();
}

// True at most once per key per seconds. Caller holds gLeaseMutex.
bool ShouldLog(LeaseState& st, std::string const& key, uint32 now, uint32 seconds)
{
    auto it = st.lastLogSec.find(key);
    if (it != st.lastLogSec.end() && now - it->second < seconds)
        return false;
    st.lastLogSec[key] = now;
    return true;
}

// Ends the active lease. Caller holds gLeaseMutex and checked info.active.
void EndLocked(LeaseState& st, std::string const& reason, CompanionLease::Outcome outcome, uint32 now)
{
    CompanionLease::Info& info = st.info;
    uint32 fails = 0;
    if (outcome == CompanionLease::Outcome::Failure && !info.owner.empty())
    {
        // Owner cooldown (no budget): checked by callers that pick among several destinations of one owner.
        st.dests["owner:" + info.owner].cooldownUntilSec = now + CooldownSeconds();
    }
    if (!info.dest.key.empty())
    {
        DestStats& dest = st.dests[info.dest.key];
        dest.activity = ActivityOf(info.owner);
        dest.lastReason = reason;
        if (outcome == CompanionLease::Outcome::Failure)
        {
            ++dest.fails;
            dest.cooldownUntilSec = now + CooldownSeconds();
        }
        else if (outcome == CompanionLease::Outcome::Success && info.policy.cooldownOnSuccess)
            dest.cooldownUntilSec = now + CooldownSeconds();
        fails = dest.fails;
    }

    uint32 const pausedNow = info.paused && now > info.pausedSinceSec ? now - info.pausedSinceSec : 0;
    uint32 const paused = info.pausedTotalSec + pausedNow;
    uint32 const dur = now > info.startSec ? now - info.startSec : 0;
    LOG_INFO("playerbots", "Companion {}: lease end owner={} dest={} reason={} outcome={} dur={}s paused={}s fails={}",
             st.name, info.owner, info.dest.key.empty() ? "none" : info.dest.key, reason, OutcomeName(outcome), dur,
             paused, fails);

    info.active = false;
    info.paused = false;
    info.pausedBy.clear();
    st.hasBest = false;
}

// Ends the lease for no progress: one failure and a cooldown on the destination. Caller holds gLeaseMutex.
void StallLocked(LeaseState& st, uint32 now)
{
    std::string const owner = st.info.owner;
    std::string const key = st.info.dest.key;
    uint32 const noProgress = now > st.info.lastProgressSec ? now - st.info.lastProgressSec : 0;
    EndLocked(st, "stall", CompanionLease::Outcome::Failure, now);

    uint32 fails = 0;
    std::string until = "none";
    if (!key.empty())
    {
        DestStats const& dest = st.dests[key];
        fails = dest.fails;
        until = fails >= RetryBudget() ? std::string("session") : std::to_string(dest.cooldownUntilSec);
    }
    LOG_INFO("playerbots", "Companion {}: lease stall owner={} dest={} noProgressFor={}s fails={} cooldownUntil={}",
             st.name, owner, key.empty() ? "none" : key, noProgress, fails, until);
}

// Suspends the lease (combat, death). Caller holds gLeaseMutex.
void PauseLocked(LeaseState& st, CompanionLease::Interrupt kind, uint32 now)
{
    CompanionLease::Info& info = st.info;
    char const* const kName = KindName(kind);
    if (info.paused)
    {
        if (info.pausedBy != kName)
            info.pausedBy = kName;
        return;
    }
    info.paused = true;
    info.pausedBy = kName;
    info.pausedSinceSec = now;
    LOG_INFO("playerbots", "Companion {}: lease interrupt owner={} by={} allowed=1 gain=0.0 score={:.1f} dist=0.0",
             st.name, info.owner, info.pausedBy, info.score);
}

// Ends a suspension: the stall clock restarts, the commit window moves on by the paused time, and the progress
// baseline is dropped (the bot may stand somewhere else after a fight or a corpse run). Caller holds gLeaseMutex.
void ResumeLocked(LeaseState& st, uint32 now)
{
    CompanionLease::Info& info = st.info;
    if (!info.paused)
        return;
    uint32 const paused = now > info.pausedSinceSec ? now - info.pausedSinceSec : 0;
    info.pausedTotalSec += paused;
    info.commitUntilSec += paused;
    // The stall clock is paused with the lease and not restarted: the pause moves the last-progress mark forward by
    // the paused time, so the non-paused time since the last progress accumulates across every fight (F1; before, the
    // resume and the next progress report restarted it each fight, and a repeating fight hid the stall). The best
    // value is rebased to where the bot stands after the fight, so only an improvement from there counts as progress.
    info.lastProgressSec += paused;
    if (st.hasBest)
        st.bestProgress = info.progress;
    LOG_INFO("playerbots", "Companion {}: lease resume owner={} after={} paused={}s", st.name, info.owner,
             info.pausedBy, paused);
    info.paused = false;
    info.pausedBy.clear();
}

std::string Lower(std::string text)
{
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) { return std::tolower(c); });
    return text;
}

bool BuiltIn(std::string const& activity)
{
    return activity == "bank" || activity == "sell" || activity == "repair" || activity == "train" ||
           activity == "hearth" || activity == "grind" || activity == "gather";
}

bool IsDuty(std::string const& activity)
{
    return activity == "bank" || activity == "sell" || activity == "repair" || activity == "train" ||
           activity == "hearth";
}
}  // namespace

bool CompanionLease::Enabled() { return sPlayerbotAIConfig.companionLeaseEnabled; }

uint32 CompanionLease::MaxPauseSeconds() { return sPlayerbotAIConfig.companionLeaseMaxPauseSeconds; }

bool CompanionLease::NeedsDangerDeathEscape(uint32 lowGuid)
{
    std::lock_guard<std::mutex> lock(gLeaseMutex);
    auto it = gLeases.find(lowGuid);
    return it != gLeases.end() && it->second.dangerDeathEscape;
}

void CompanionLease::ClearDangerDeathEscape(uint32 lowGuid)
{
    std::lock_guard<std::mutex> lock(gLeaseMutex);
    auto it = gLeases.find(lowGuid);
    if (it != gLeases.end())
        it->second.dangerDeathEscape = false;
}

bool CompanionLease::Begin(Player* bot, std::string const& owner, Destination const& dest, uint32 commitSeconds,
                           Policy const& policy, float score, bool force)
{
    if (!bot || !Enabled())
        return true;

    uint32 const low = bot->GetGUID().GetCounter();
    uint32 const now = NowSec();
    uint32 const commit = commitSeconds ? commitSeconds : sPlayerbotAIConfig.companionLeaseCommitSeconds;
    // Read before the lease lock: the brain has its own mutex.
    float const margin = CompanionBrain::Weight(low, "switch_margin", CompanionBrain::DutyDefault("switch_margin"));

    std::lock_guard<std::mutex> lock(gLeaseMutex);
    LeaseState& st = gLeases[low];
    st.name = bot->GetName();
    Info& info = st.info;

    if (info.active && info.owner == owner && info.dest.key == dest.key)
    {
        info.score = score;
        return true;
    }

    char const* refused = nullptr;
    if (!dest.key.empty())
    {
        auto it = st.dests.find(dest.key);
        if (it != st.dests.end() && it->second.fails >= RetryBudget())
            refused = "budget";
        else if (it != st.dests.end() && now < it->second.cooldownUntilSec)
            refused = "cooldown";
    }
    if (!refused && info.active && !force && info.owner != owner && now < info.commitUntilSec)
        refused = "commit";
    // After the commit window another activity takes the lease only when it beats the holder's score by the switch
    // margin (card: "the gain beats the current score by the switch margin"); the same activity may re-plan freely.
    // Run 1 (2026-11-19) showed quest trips replaced by duties scoring lower or barely higher (bank 71.5 over a hub
    // trip at 72.6), the main source of owner flips.
    if (!refused && info.active && !force && ActivityOf(info.owner) != ActivityOf(owner) &&
        score < info.score + margin)
        refused = "margin";
    if (refused)
    {
        if (ShouldLog(st, std::string("refused:") + owner + ":" + refused, now, REFUSED_LOG_SECONDS))
            LOG_INFO("playerbots", "Companion {}: lease refused owner={} dest={} reason={} holder={}", st.name, owner,
                     dest.key.empty() ? "none" : dest.key, refused, info.active ? info.owner : "none");
        return false;
    }

    if (info.active)
        EndLocked(st, "replaced:" + owner, Outcome::Neutral, now);

    info = Info();
    info.active = true;
    info.owner = owner;
    info.dest = dest;
    info.policy = policy;
    info.score = score;
    info.margin = margin;
    info.startSec = now;
    info.commitUntilSec = now + commit;
    info.lastProgressSec = now;
    st.hasBest = false;
    st.bestProgress = 0.0f;
    st.dangerSecs.clear();
    st.deathSecs.clear();
    st.inFight = false;
    st.wasDead = false;
    st.progressEvents = 0;

    if (!dest.key.empty())
    {
        DestStats& stats = st.dests[dest.key];
        stats.activity = ActivityOf(owner);
        stats.lastAttemptSec = now;
    }

    LOG_INFO("playerbots",
             "Companion {}: lease start owner={} dest={} x={:.1f} y={:.1f} map={} commit={}s policy={} score={:.1f}",
             st.name, owner, dest.key.empty() ? "none" : dest.key, dest.x, dest.y, dest.mapId, commit,
             PolicyText(policy), score);
    return true;
}

void CompanionLease::Progress(uint32 lowGuid, float value)
{
    if (!Enabled())
        return;

    std::lock_guard<std::mutex> lock(gLeaseMutex);
    auto it = gLeases.find(lowGuid);
    if (it == gLeases.end() || !it->second.info.active || it->second.info.paused)
        return;

    LeaseState& st = it->second;
    st.info.progress = value;
    if (!st.hasBest || value >= st.bestProgress + st.info.policy.progressStep)
    {
        st.hasBest = true;
        st.bestProgress = value;
        st.info.lastProgressSec = NowSec();
        ++st.progressEvents;
    }
}

void CompanionLease::KeepAlive(uint32 lowGuid)
{
    if (!Enabled())
        return;

    std::lock_guard<std::mutex> lock(gLeaseMutex);
    auto it = gLeases.find(lowGuid);
    if (it == gLeases.end() || !it->second.info.active || it->second.info.paused)
        return;
    it->second.info.lastProgressSec = NowSec();
}

void CompanionLease::ResetProgress(uint32 lowGuid)
{
    if (!Enabled())
        return;

    std::lock_guard<std::mutex> lock(gLeaseMutex);
    auto it = gLeases.find(lowGuid);
    if (it == gLeases.end() || !it->second.info.active)
        return;
    it->second.hasBest = false;
    it->second.info.lastProgressSec = NowSec();
}

bool CompanionLease::RequestInterrupt(Player* bot, Interrupt kind, float gain, float distance)
{
    if (!bot || !Enabled())
        return true;

    uint32 const low = bot->GetGUID().GetCounter();
    uint32 const now = NowSec();

    std::lock_guard<std::mutex> lock(gLeaseMutex);
    auto it = gLeases.find(low);
    if (it == gLeases.end() || !it->second.info.active)
        return true;

    LeaseState& st = it->second;
    Info& info = st.info;
    switch (kind)
    {
        case Interrupt::Combat:
        case Interrupt::Death:
            PauseLocked(st, kind, now);
            return true;
        case Interrupt::OwnerCommand:
        case Interrupt::Group:
            LOG_INFO("playerbots", "Companion {}: lease interrupt owner={} by={} allowed=1 gain={:.1f} score={:.1f} "
                     "dist={:.1f}", st.name, info.owner, KindName(kind), gain, info.score, distance);
            EndLocked(st, std::string("interrupt:") + KindName(kind), Outcome::Neutral, now);
            return true;
        default:
            break;
    }

    bool kindAllowed = false;
    switch (kind)
    {
        case Interrupt::Node:
            kindAllowed = info.policy.allowNodes;
            break;
        case Interrupt::Corpse:
            kindAllowed = info.policy.allowCorpses;
            break;
        case Interrupt::RareMob:
            kindAllowed = info.policy.allowRareMobs;
            break;
        case Interrupt::Quest:
            kindAllowed = info.policy.allowQuest;
            break;
        default:
            break;
    }
    bool const allowed = kindAllowed && (distance <= info.policy.nearYards ||
                                         (now >= info.commitUntilSec && gain > info.score + info.margin));
    if (ShouldLog(st, std::string("interrupt:") + KindName(kind) + (allowed ? ":1" : ":0"), now,
                  INTERRUPT_LOG_SECONDS))
        LOG_INFO("playerbots", "Companion {}: lease interrupt owner={} by={} allowed={} gain={:.1f} score={:.1f} "
                 "dist={:.1f}", st.name, info.owner, KindName(kind), allowed ? 1 : 0, gain, info.score, distance);
    return allowed;
}

void CompanionLease::End(uint32 lowGuid, std::string const& owner, char const* reason, Outcome outcome)
{
    if (!Enabled())
        return;

    std::lock_guard<std::mutex> lock(gLeaseMutex);
    auto it = gLeases.find(lowGuid);
    if (it == gLeases.end() || !it->second.info.active)
        return;
    if (!owner.empty() && it->second.info.owner != owner)
        return;
    EndLocked(it->second, reason ? reason : "done", outcome, NowSec());
}

namespace
{
// Danger (F1): drops the fight window entries older than DANGER_WINDOW_SEC. Caller holds gLeaseMutex.
void PruneDangerLocked(LeaseState& st, uint32 now)
{
    while (!st.dangerSecs.empty() && now >= st.dangerSecs.front() && now - st.dangerSecs.front() >= DANGER_WINDOW_SEC)
        st.dangerSecs.erase(st.dangerSecs.begin());
}

// Danger (F1): the escape is due when two danger fights are in the window, or when the first one has lasted
// StallSeconds. Caller holds gLeaseMutex.
bool DangerDueLocked(LeaseState& st, uint32 now)
{
    PruneDangerLocked(st, now);
    if (st.dangerSecs.empty())
        return false;
    if (st.dangerSecs.size() >= DANGER_FIGHTS)
        return true;
    // One danger fight: due only when it has lasted StallSeconds with no progress since it began.
    return now >= st.dangerSecs.front() && now - st.dangerSecs.front() >= StallSeconds() &&
           st.progressEvents == st.dangerProgressMark;
}

// Danger (F1): a fight that started against a unit at or above the bot's level + DangerLevelGap. Caller holds
// gLeaseMutex and checked info.active.
void RecordDangerLocked(LeaseState& st, Unit* attacker, uint32 botLevel, uint32 now)
{
    if (!attacker || attacker->GetLevel() < botLevel + DangerLevelGap())
        return;
    PruneDangerLocked(st, now);
    if (st.dangerSecs.empty())
        st.dangerProgressMark = st.progressEvents;
    st.dangerSecs.push_back(now);
    st.dangerEntry = attacker->GetEntry();
    st.dangerAttLevel = attacker->GetLevel();
    LOG_INFO("playerbots", "Companion {}: danger fight owner={} attacker={} attLevel={} botLevel={} fights={}",
             st.name, st.info.owner, st.dangerEntry, st.dangerAttLevel, botLevel, st.dangerSecs.size());
}

// The unit the bot fights: the highest-level one among its victim and its attackers.
Unit* CurrentAttacker(Player* bot)
{
    Unit* best = bot->GetVictim();
    for (Unit* attacker : bot->getAttackers())
        if (attacker && (!best || attacker->GetLevel() > best->GetLevel()))
            best = attacker;
    return best;
}

}  // namespace

void CompanionLease::Tick(PlayerbotAI* botAI)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot || !Enabled())
        return;

    uint32 const low = bot->GetGUID().GetCounter();
    bool const dead = !bot->IsAlive();
    // A quest-trip ride-through (TripAvoid) keeps the trip moving in combat: no combat pause. Read before the lock.
    bool const avoiding = CompanionTrip::Avoiding(low);
    bool const combat = bot->IsInCombat() && !avoiding;
    bool const suspended = dead || combat;
    uint32 const now = NowSec();

    std::unique_lock<std::mutex> lock(gLeaseMutex);
    auto it = gLeases.find(low);
    if (it == gLeases.end() || !it->second.info.active)
        return;

    LeaseState& st = it->second;
    st.name = bot->GetName();
    // Danger (F1): counted once per fight, at its start (the edge of combat). Runs before any early return.
    bool const fightStart = combat && !st.inFight;
    st.inFight = combat;
    if (fightStart && DangerEscapeOn())
        RecordDangerLocked(st, CurrentAttacker(bot), bot->GetLevel(), now);

    // Corpse-camp escape: if a companion dies >= 2 times within 10 minutes on the same lease (e.g. a low-level caster
    // reviving on top of a Defias Cutpurse along a walk lease), trigger Spirit Healer safe revive and cool down the
    // lease instead of re-dying in a loop.
    bool const deathStart = dead && !st.wasDead;
    st.wasDead = dead;
    if (deathStart)
    {
        while (!st.deathSecs.empty() && now >= st.deathSecs.front() && now - st.deathSecs.front() >= DANGER_WINDOW_SEC)
            st.deathSecs.erase(st.deathSecs.begin());
        st.deathSecs.push_back(now);
        if (st.deathSecs.size() >= 2)
        {
            st.dangerDeathEscape = true;
            std::string const escapedOwner = st.info.owner;
            LOG_INFO("playerbots",
                     "Companion {}: corpse-camp escape owner={} deaths={} (safe revive + lease cooldown)",
                     st.name, escapedOwner, st.deathSecs.size());
            EndLocked(st, "corpse-camp-escape", Outcome::Failure, now);
            return;
        }
    }

    if (suspended)
    {
        if (dead && (!st.dangerSecs.empty() || sCompanionBotMgr.IsZoneTooDangerous(bot, bot->GetZoneId())))
            st.dangerDeathEscape = true;
        PauseLocked(st, dead ? Interrupt::Death : Interrupt::Combat, now);
    }
    else
        ResumeLocked(st, now);

    // Danger escape (F1): ends the lease as a failure. The hearthstone item is used only when the bot is alive and out
    // of combat; otherwise the lease just ends (action=end).
    if (DangerEscapeOn() && DangerDueLocked(st, now))
    {
        std::string const owner = st.info.owner;
        uint32 const fights = static_cast<uint32>(st.dangerSecs.size());
        uint32 const entry = st.dangerEntry;
        uint32 const attLevel = st.dangerAttLevel;
        EndLocked(st, "danger", Outcome::Failure, now);
        st.dangerSecs.clear();
        lock.unlock();

        // The hearthstone item is used like the transport's hearth leg (its spell, its item cooldown).
        Item* stone = bot->GetItemByEntry(ITEM_HEARTHSTONE);
        uint32 const stoneSpell = stone ? stone->GetTemplate()->Spells[0].SpellId : 0;
        bool const hearth = !suspended && stoneSpell && !bot->HasSpellCooldown(stoneSpell) &&
                            !bot->HasSpellItemCooldown(stoneSpell, ITEM_HEARTHSTONE);
        if (hearth)
        {
            SpellCastTargets targets;
            targets.SetUnitTarget(bot);
            bot->CastItemUseSpell(stone, targets, 1, 0);
        }
        LOG_INFO("playerbots", "Companion {}: danger escape lease={} attacker={} attLevel={} botLevel={} fights={} "
                 "action={}", bot->GetName(), owner, entry, attLevel, bot->GetLevel(), fights,
                 hearth ? "hearth" : "end");
        return;
    }

    if (st.info.paused)
    {
        // Pause cap (F1): a suspension longer than Lease.MaxPauseSeconds ends the lease, even if no resume ever fires.
        uint32 const cap = CompanionLease::MaxPauseSeconds();
        uint32 const pausedFor = now > st.info.pausedSinceSec ? now - st.info.pausedSinceSec : 0;
        if (cap && pausedFor >= cap)
        {
            LOG_INFO("playerbots", "Companion {}: lease pause-cap owner={} by={} paused={}s", st.name, st.info.owner,
                     st.info.pausedBy, pausedFor);
            EndLocked(st, "pause-cap", Outcome::Failure, now);
            return;
        }
        // Pause diagnostic (once per 60 s): why the suspension does not end.
        if (PauseDiag() && ShouldLog(st, "paused-tick", now, 60))
            LOG_INFO("playerbots", "Companion {}: lease paused-tick owner={} by={} dead={} inCombat={} avoiding={} "
                     "attackers={}", st.name, st.info.owner, st.info.pausedBy, dead ? 1 : 0,
                     bot->IsInCombat() ? 1 : 0, avoiding ? 1 : 0, bot->getAttackers().size());
        return;
    }

    Info const& info = st.info;
    if (now > info.lastProgressSec && now - info.lastProgressSec >= StallSeconds())
    {
        StallLocked(st, now);
        return;
    }
    uint32 const age = now > info.startSec ? now - info.startSec : 0;
    if (age > info.pausedTotalSec && age - info.pausedTotalSec >= MaxSeconds())
        EndLocked(st, "timeout", Outcome::Failure, now);
}

CompanionLease::Info CompanionLease::Current(uint32 lowGuid)
{
    std::lock_guard<std::mutex> lock(gLeaseMutex);
    auto it = gLeases.find(lowGuid);
    return it == gLeases.end() ? Info() : it->second.info;
}

bool CompanionLease::Holds(uint32 lowGuid, std::string const& owner)
{
    if (!Enabled())
        return true;

    std::lock_guard<std::mutex> lock(gLeaseMutex);
    auto it = gLeases.find(lowGuid);
    return it != gLeases.end() && it->second.info.active && it->second.info.owner == owner;
}

bool CompanionLease::MayTry(uint32 lowGuid, std::string const& destKey)
{
    if (!Enabled() || destKey.empty())
        return true;

    uint32 const now = NowSec();
    std::lock_guard<std::mutex> lock(gLeaseMutex);
    auto it = gLeases.find(lowGuid);
    if (it == gLeases.end())
        return true;
    auto dest = it->second.dests.find(destKey);
    if (dest == it->second.dests.end())
        return true;
    return dest->second.fails < RetryBudget() && now >= dest->second.cooldownUntilSec;
}

bool CompanionLease::BlocksRpg(uint32 lowGuid)
{
    if (!Enabled())
        return false;

    std::lock_guard<std::mutex> lock(gLeaseMutex);
    auto it = gLeases.find(lowGuid);
    return it != gLeases.end() && it->second.info.active && !it->second.info.policy.allowRpg;
}

std::string CompanionLease::SpawnKey(bool isGo, uint32 spawnId)
{
    return std::string(isGo ? "go:" : "npc:") + std::to_string(spawnId);
}

std::string CompanionLease::ObjectKey(WorldObject const* object)
{
    if (!object)
        return std::string();
    if (Creature const* creature = object->ToCreature())
        return SpawnKey(false, creature->GetSpawnId());
    if (GameObject const* go = object->ToGameObject())
        return SpawnKey(true, go->GetSpawnId());
    return "guid:" + std::to_string(object->GetGUID().GetCounter());
}

void CompanionLease::RegisterActivity(std::string const& activity)
{
    std::lock_guard<std::mutex> lock(gLeaseMutex);
    gRegistered.insert(Lower(activity));
}

void CompanionLease::ReportBlock(uint32 lowGuid, std::string const& activity, std::string const& condition)
{
    std::lock_guard<std::mutex> lock(gLeaseMutex);
    LeaseState& st = gLeases[lowGuid];
    if (condition.empty())
    {
        st.blocks.erase(activity);
        return;
    }
    BlockNote& note = st.blocks[activity];
    note.text = condition;
    note.atSec = NowSec();
}

std::vector<std::string> const& CompanionLease::ActivityNames()
{
    static std::vector<std::string> const names = {"bank",  "sell",  "repair", "train",  "hearth",
                                                   "quest", "grind", "craft",  "fish",   "gather"};
    return names;
}

std::vector<std::string> CompanionLease::WhyNot(uint32 lowGuid, std::string const& rawActivity)
{
    std::vector<std::string> lines;
    std::string activity = Lower(rawActivity);
    size_t const first = activity.find_first_not_of(' ');
    activity = first == std::string::npos ? std::string() : activity.substr(first);
    if (activity.rfind("not ", 0) == 0)
        activity = activity.substr(4);
    size_t const last = activity.find_last_not_of(' ');
    activity = last == std::string::npos ? std::string() : activity.substr(0, last + 1);

    std::vector<std::string> const& names = ActivityNames();
    if (std::find(names.begin(), names.end(), activity) == names.end())
    {
        lines.push_back("No such activity: " + activity +
                        ". Try: bank, sell, repair, train, hearth, quest, grind, craft, fish, gather.");
        return lines;
    }

    uint32 const now = NowSec();
    std::lock_guard<std::mutex> lock(gLeaseMutex);
    if (!BuiltIn(activity) && !gRegistered.count(activity))
    {
        lines.push_back("Why not " + activity + ": not available in this build.");
        return lines;
    }

    LeaseState const* st = nullptr;
    auto stIt = gLeases.find(lowGuid);
    if (stIt != gLeases.end())
        st = &stIt->second;
    Info const lease = st ? st->info : Info();
    bool const otherOwner = lease.active && ActivityOf(lease.owner) != activity;

    // 1. Blocking condition: the activity's own report, else a built-in answer.
    std::ostringstream cond;
    cond << "Why not " << activity << ": ";
    auto note = st ? st->blocks.find(activity) : std::unordered_map<std::string, BlockNote>::const_iterator();
    if (st && note != st->blocks.end())
        cond << note->second.text << " (reported " << (now > note->second.atSec ? now - note->second.atSec : 0)
             << "s ago)";
    else if (lease.active && ActivityOf(lease.owner) == activity)
        cond << "it is the current activity (" << lease.owner << ")";
    else if (activity == "grind")
    {
        if (otherOwner)
            cond << "lease " << lease.owner << " holds the companion; grinding waits until it ends";
        else
            cond << "not blocked by a lease (new rpg picks grind by weight when no quest fits)";
    }
    else if (activity == "gather")
    {
        if (otherOwner && lease.policy.allowNodes)
            cond << "lease " << lease.owner << " allows nodes only within "
                 << static_cast<uint32>(lease.policy.nearYards) << " yd";
        else if (otherOwner)
            cond << "lease " << lease.owner << " allows no gathering detour";
        else
            cond << "not blocked by a lease (needs a gathering skill and a node in sight)";
    }
    else if (IsDuty(activity))
        cond << "not scored yet (duties are scored every 30 s, autonomy only)";
    else
        cond << "no report yet";
    lines.push_back(cond.str());

    if (otherOwner)
    {
        std::ostringstream out;
        out << "Lease held by " << lease.owner;
        if (now < lease.commitUntilSec)
            out << " (committed for " << lease.commitUntilSec - now << "s more)";
        if (lease.paused)
            out << ", paused by " << lease.pausedBy;
        lines.push_back(out.str());
    }

    // 2. Destinations of this activity: last attempt, failures, when it may try again.
    std::vector<std::pair<std::string, DestStats>> dests;
    if (st)
        for (auto const& entry : st->dests)
            if (entry.second.activity == activity)
                dests.push_back(entry);
    std::sort(dests.begin(), dests.end(),
              [](auto const& a, auto const& b) { return a.second.lastAttemptSec > b.second.lastAttemptSec; });

    uint32 totalFails = 0;
    for (auto const& entry : dests)
        totalFails += entry.second.fails;
    if (dests.empty())
    {
        lines.push_back("Last attempt: none this session. Failures: 0. May try again: now.");
        return lines;
    }

    std::ostringstream summary;
    summary << "Failures this session: " << totalFails << " over " << dests.size() << " destination(s), budget "
            << RetryBudget() << " per destination.";
    lines.push_back(summary.str());
    for (size_t i = 0; i < dests.size() && i < WHY_NOT_DESTINATIONS; ++i)
    {
        DestStats const& dest = dests[i].second;
        std::ostringstream out;
        out << (i == 0 ? "Last attempt: " : "Earlier: ") << dests[i].first << " "
            << (now > dest.lastAttemptSec ? now - dest.lastAttemptSec : 0) << "s ago, failures " << dest.fails
            << ", last end " << (dest.lastReason.empty() ? "running" : dest.lastReason) << ", may try again: ";
        if (dest.fails >= RetryBudget())
            out << "not this session";
        else if (now < dest.cooldownUntilSec)
            out << "in " << dest.cooldownUntilSec - now << "s";
        else
            out << "now";
        lines.push_back(out.str());
    }
    return lines;
}

std::string CompanionLease::StatusLine(uint32 lowGuid)
{
    if (!Enabled())
        return "Lease: off (AiPlayerbot.Companion.Lease.Enabled = 0).";

    Info const info = Current(lowGuid);
    if (!info.active)
        return "Lease: none (questing and rpg).";

    uint32 const now = NowSec();
    std::ostringstream out;
    out << "Lease: " << info.owner << " to " << (info.dest.key.empty() ? "none" : info.dest.key) << " for "
        << (now > info.startSec ? now - info.startSec : 0) << "s";
    if (now < info.commitUntilSec)
        out << ", committed " << info.commitUntilSec - now << "s more";
    if (info.paused)
        out << ", paused by " << info.pausedBy;
    else
        out << ", no progress for " << (now > info.lastProgressSec ? now - info.lastProgressSec : 0) << "s";
    out << ".";
    return out.str();
}
