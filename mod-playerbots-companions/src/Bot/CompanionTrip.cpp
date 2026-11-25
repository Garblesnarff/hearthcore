/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "CompanionTrip.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <mutex>
#include <string>
#include <unordered_map>

#include "CompanionBotMgr.h"
#include "CompanionLease.h"
#include "CompanionQuest.h"
#include "Config.h"
#include "Creature.h"
#include "Group.h"
#include "Log.h"
#include "LootMgr.h"
#include "ObjectAccessor.h"
#include "ObjectMgr.h"
#include "PathGenerator.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "Playerbots.h"
#include "QuestDef.h"
#include "SpellAuraDefines.h"
#include "SpellAuraEffects.h"
#include "SpellAuras.h"
#include "SpellInfo.h"
#include "Timer.h"

namespace
{
constexpr uint32 CONFIG_REFRESH_MS = 10000;
constexpr uint32 DAZE_SPELL = 1604;             // "Dazed": Unit::DealMeleeDamage daze roll
constexpr uint32 CAUSE_WINDOW_MS = 1000;        // dismount cause: last hit / last hostile aura within 1 s
constexpr uint32 HIT_WINDOW_MS = 3000;          // "still hitting": dealt damage within 3 s (or in melee range)
constexpr uint32 QUEST_VERDICT_MS = 2000;       // an attacker's quest-mob verdict (quest-log scan) is reused this long
constexpr uint32 ASSIST_LOG_MS = 10000;         // assist refusal log, per companion
constexpr float AVOID_MOVE_YARDS = 5.0f;        // stuck: this much real movement ...
constexpr uint32 AVOID_STUCK_MS = 4000;         // ... within this time
constexpr uint8 AVOID_NONE = 0;
constexpr uint8 AVOID_RIDING = 1;
constexpr uint8 AVOID_FOUGHT = 2;               // latched until out of combat

struct Switches
{
    uint32 atMs = 0;
    bool loaded = false;
    bool peace = true;
    bool approach = true;
    bool questLoot = false;
    bool combatDismount = true;
    float questYards = 30.0f;
    bool avoid = true;
    float avoidMinHealthPct = 50.0f;
    uint32 avoidCaughtSeconds = 10;
    bool noAssist = true;
    bool dismountLog = true;
};

std::mutex gSwitchMutex;
Switches gSwitches;

Switches ReadSwitches()
{
    uint32 const now = getMSTime();
    std::lock_guard<std::mutex> lock(gSwitchMutex);
    if (!gSwitches.loaded || getMSTimeDiff(gSwitches.atMs, now) >= CONFIG_REFRESH_MS)
    {
        gSwitches.peace = sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.TripPeace.Enabled", true, false);
        gSwitches.questYards = sConfigMgr->GetOption<float>("AiPlayerbot.Companion.TripPeace.QuestYards", 30.0f, false);
        gSwitches.questLoot = sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.TripPeace.QuestLoot", false, false);
        gSwitches.approach = sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.DutyApproach.Enabled", true, false);
        gSwitches.combatDismount =
            sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.CombatDismount.Enabled", true, false);
        gSwitches.avoid = sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.TripAvoid.Enabled", true, false);
        gSwitches.avoidMinHealthPct =
            sConfigMgr->GetOption<float>("AiPlayerbot.Companion.TripAvoid.MinHealthPct", 50.0f, false);
        gSwitches.avoidCaughtSeconds =
            sConfigMgr->GetOption<uint32>("AiPlayerbot.Companion.TripAvoid.CaughtSeconds", 10, false);
        gSwitches.noAssist = sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.TripAvoid.NoAssist", true, false);
        gSwitches.dismountLog = sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.DismountLog", true, false);
        gSwitches.atMs = now;
        gSwitches.loaded = true;
    }
    return gSwitches;
}

// One attacker of the current fight that has dealt damage to the companion (or was seen attacking it).
struct Hitter
{
    ObjectGuid guid;
    uint32 firstMs = 0;   // first hit (or first sight) in this fight
    uint32 lastMs = 0;    // last damage dealt to the companion (0 = none)
    uint32 questCheckMs = 0;   // when questMob was computed (0 = never); AvoidActive owns the two quest fields
    bool questMob = false;     // a quest mob of the companion (IsQuestMob) at questCheckMs
};
constexpr size_t HITTER_SLOTS = 8;

// Per companion: the trip-combat state of the current fight plus the last hits and hostile aura (dismount cause).
struct TripLogState
{
    ObjectGuid lastTarget;
    uint32 lastMountBlockMs = 0;
    uint32 lastAssistLogMs = 0;
    uint32 fightStartMs = 0;   // 0 = no fight tracked
    bool fightMounted = false;
    uint32 fightDismounts = 0;
    // Trip combat avoidance (ride-through) for the current fight.
    uint8 avoid = AVOID_NONE;
    uint32 avoidStartMs = 0;
    uint32 avoidMoveMs = 0;    // last time the companion had moved AVOID_MOVE_YARDS from (avoidX, avoidY)
    float avoidX = 0.0f;
    float avoidY = 0.0f;
    Hitter hitters[HITTER_SLOTS];
    // Dismount cause: the last damage taken and the last hostile (negative) aura applied to the companion.
    uint32 lastHitMs = 0;
    uint32 lastHitEntry = 0;
    uint32 lastAuraMs = 0;
    uint32 lastAuraSpell = 0;
    bool lastAuraHostile = false;
};
std::mutex gLogMutex;
std::unordered_map<uint32, TripLogState> gLogStates;

// Depth of CompanionTrip::SelfDismount scopes on this thread: a mount removal inside one is logged by the scope.
thread_local uint32 gSelfDismountDepth = 0;

bool IsCompanionBot(Player* bot)
{
    PlayerbotAI* botAI = bot ? GET_PLAYERBOT_AI(bot) : nullptr;
    return botAI && botAI->IsCompanion();
}

bool IsOurSide(Player* bot, PlayerbotAI* botAI, Unit* victim)
{
    if (!victim)
        return false;
    if (victim == bot)
        return true;
    if (victim->GetOwnerGUID() == bot->GetGUID())
        return true;   // our pet
    if (Player* master = botAI->GetMaster())
        if (victim == master || victim->GetOwnerGUID() == master->GetGUID())
            return true;
    if (Group* group = bot->GetGroup())
    {
        if (Player* player = victim->ToPlayer())
            return group->IsMember(player->GetGUID());
        if (Unit* owner = victim->GetOwner())
            if (Player* ownerPlayer = owner->ToPlayer())
                return group->IsMember(ownerPlayer->GetGUID());
    }
    return false;
}

// An incomplete kill objective of a quest in the log, for this creature entry.
bool QuestKillNeeded(Player* bot, uint32 entry)
{
    for (auto const& [questId, status] : bot->getQuestStatusMap())
    {
        if (status.Status != QUEST_STATUS_INCOMPLETE)
            continue;
        Quest const* quest = sObjectMgr->GetQuestTemplate(questId);
        if (!quest || quest->GetQuestLevel() > static_cast<int32>(bot->GetLevel()) + 5)
            continue;
        for (int j = 0; j < QUEST_OBJECTIVES_COUNT; ++j)
        {
            int32 const required = quest->RequiredNpcOrGo[j];
            if (required > 0 && uint32(required) == entry && quest->RequiredNpcOrGoCount[j] &&
                status.CreatureOrGOCount[j] < quest->RequiredNpcOrGoCount[j])
                return true;
        }
    }
    return false;
}

// "quest": an incomplete kill objective of a quest in the log; "quest-loot": the creature drops an item a quest in
// the log still needs. Same rules as GrindTargetValue::needForQuest, split by kind.
char const* QuestReason(Player* bot, Unit* unit)
{
    Creature* creature = unit->ToCreature();
    if (!creature)
        return nullptr;
    if (QuestKillNeeded(bot, creature->GetEntry()))
        return "quest";
    if (!ReadSwitches().questLoot)
        return nullptr;
    if (CreatureTemplate const* data = creature->GetCreatureTemplate())
        if (data->lootid && LootTemplates_Creature.HaveQuestLootForPlayer(data->lootid, bot))
            return "quest-loot";
    return nullptr;
}

// Trip avoidance: a mob that is a quest mob of this companion (kill objective, or a loot item a quest still needs),
// whatever the TripPeace.QuestLoot switch says. The companion fights those.
bool IsQuestMob(Player* bot, Unit* unit)
{
    Creature* creature = unit->ToCreature();
    if (!creature)
        return false;
    if (QuestKillNeeded(bot, creature->GetEntry()))
        return true;
    if (CreatureTemplate const* data = creature->GetCreatureTemplate())
        if (data->lootid && LootTemplates_Creature.HaveQuestLootForPlayer(data->lootid, bot))
            return true;
    return false;
}

bool InAttackers(PlayerbotAI* botAI, Unit* unit)
{
    GuidVector const attackers = botAI->GetAiObjectContext()->GetValue<GuidVector>("attackers")->Get();
    for (ObjectGuid const& guid : attackers)
        if (guid == unit->GetGUID())
            return true;
    return false;
}

Hitter* FindHitter(TripLogState& st, ObjectGuid const& guid)
{
    for (Hitter& h : st.hitters)
        if (h.guid == guid)
            return &h;
    return nullptr;
}

// Finds or adds the hitter. A full table evicts the entry that was hit longest ago. Caller holds gLogMutex.
Hitter* AddHitter(TripLogState& st, ObjectGuid const& guid, uint32 now)
{
    if (Hitter* h = FindHitter(st, guid))
        return h;
    Hitter* slot = nullptr;
    for (Hitter& h : st.hitters)
    {
        if (!h.guid)
        {
            slot = &h;
            break;
        }
        if (!slot || h.lastMs < slot->lastMs)
            slot = &h;
    }
    slot->guid = guid;
    slot->firstMs = now;
    slot->lastMs = 0;
    slot->questCheckMs = 0;
    slot->questMob = false;
    return slot;
}

// Copies only the quest verdict of a hitter computed by AvoidActive into the live table. The hit times stay the live
// ones, which NoteDamage may have advanced meanwhile. Caller holds gLogMutex.
void MergeQuestVerdict(TripLogState& live, Hitter const& seen)
{
    Hitter* hitter = FindHitter(live, seen.guid);
    if (!hitter)
        hitter = AddHitter(live, seen.guid, seen.firstMs);
    hitter->questCheckMs = seen.questCheckMs;
    hitter->questMob = seen.questMob;
}

// Prints one dismount line. Logged when the companion holds a trip lease or is in combat.
void LogDismount(Player* bot, char const* cause, uint32 spell, uint32 attacker, char const* via)
{
    if (!CompanionTrip::DismountLogEnabled())
        return;
    uint32 const low = bot->GetGUID().GetCounter();
    CompanionLease::Info const lease = CompanionLease::Current(low);
    bool const inCombat = bot->IsInCombat();
    if (!lease.active && !inCombat)
        return;
    std::string const viaText = via ? std::string(" via=") + via : std::string();
    LOG_INFO("playerbots", "Companion {}: dismount cause={} spell={} attacker={} lease={} inCombat={}{}",
             bot->GetName(), cause, spell, attacker, lease.active ? lease.owner : "none", inCombat ? 1 : 0, viaText);
}
}  // namespace

bool CompanionTrip::PeaceEnabled() { return ReadSwitches().peace; }

float CompanionTrip::PeaceQuestYards() { return ReadSwitches().questYards; }

bool CompanionTrip::ApproachEnabled() { return ReadSwitches().approach; }

bool CompanionTrip::CombatDismountEnabled() { return ReadSwitches().combatDismount; }

bool CompanionTrip::AvoidEnabled() { return ReadSwitches().avoid; }

bool CompanionTrip::IsQuestObjectiveMob(Player* bot, Unit* unit) { return bot && unit && IsQuestMob(bot, unit); }

bool CompanionTrip::NoAssistEnabled() { return ReadSwitches().noAssist; }

bool CompanionTrip::DismountLogEnabled() { return ReadSwitches().dismountLog; }

bool CompanionTrip::Avoiding(uint32 lowGuid)
{
    std::lock_guard<std::mutex> lock(gLogMutex);
    auto it = gLogStates.find(lowGuid);
    return it != gLogStates.end() && it->second.avoid == AVOID_RIDING;
}

bool CompanionTrip::AvoidActive(PlayerbotAI* botAI)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot || !botAI->IsCompanion())
        return false;
    uint32 const low = bot->GetGUID().GetCounter();
    uint32 const now = getMSTime();

    TripLogState st;
    {
        std::lock_guard<std::mutex> lock(gLogMutex);
        auto it = gLogStates.find(low);
        if (it != gLogStates.end())
            st = it->second;
    }

    // Out of combat (or dead): a running ride-through ends here, and the hitters of the fight are forgotten.
    if (!bot->IsInCombat() || !bot->IsAlive())
    {
        {
            std::lock_guard<std::mutex> lock(gLogMutex);
            auto it = gLogStates.find(low);
            if (it != gLogStates.end())
            {
                it->second.avoid = AVOID_NONE;
                std::fill(std::begin(it->second.hitters), std::end(it->second.hitters), Hitter());
            }
        }
        if (st.avoid == AVOID_RIDING)
        {
            bool const alive = bot->IsAlive();
            CompanionLease::Info const lease = CompanionLease::Current(low);
            LOG_INFO("playerbots", "Companion {}: trip combat avoid end result={} reason={} dur={} lease={}",
                     bot->GetName(), alive ? "escaped" : "died", alive ? "out-of-combat" : "dead",
                     getMSTimeDiff(st.avoidStartMs, now) / 1000, lease.active ? lease.owner : "none");
        }
        return false;
    }
    if (st.avoid == AVOID_FOUGHT)
        return false;   // latched: this fight is fought

    Switches const sw = ReadSwitches();
    char const* why = nullptr;
    bool silent = false;   // not a travel-lease situation at all: no avoid log line
    CompanionQuest::Trip trip;
    std::string leaseOwner;
    bool const hasQuestTrip = CompanionQuest::ActiveTrip(low, trip) && CompanionLease::Holds(low, trip.owner);
    bool hasDutyOrCraftTrip = false;
    if (hasQuestTrip)
        leaseOwner = trip.owner;
    else if (sPlayerbotAIConfig.companionTripAvoidDutyAndCraft && CompanionLease::Enabled())
    {
        CompanionLease::Info const li = CompanionLease::Current(low);
        if (li.active && (li.owner.rfind("duty:", 0) == 0 || li.owner.rfind("craft:", 0) == 0 ||
                          li.owner == "fish" || li.owner.rfind("gather:", 0) == 0))
        {
            hasDutyOrCraftTrip = true;
            leaseOwner = li.owner;
        }
    }
    if (!sw.avoid || !CompanionLease::Enabled() || bot->InBattleground() || bot->InArena() ||
        (!hasQuestTrip && !hasDutyOrCraftTrip))
    {
        why = "trip-ended";
        silent = st.avoid == AVOID_NONE;
    }
    else if (bot->IsInFlight() || (hasQuestTrip && trip.phase == CompanionQuest::Phase::Flying))
    {
        why = "flight";
        silent = st.avoid == AVOID_NONE;
    }

    // Owner's rule: ride on while mounted; fight only when dismounted, below MinHealthPct, or caught by a quest mob or
    // by the same mob hitting for CaughtSeconds. Checked on the start tick too.
    if (!why && !bot->IsMounted())
        why = "dismounted";
    else if (!why && bot->GetHealthPct() < sw.avoidMinHealthPct)
        why = "health";

    uint32 attackers = 0;
    Unit* first = nullptr;
    for (Unit* attacker : bot->getAttackers())
    {
        if (!attacker || !attacker->IsAlive())
            continue;
        ++attackers;
        if (!first)
            first = attacker;
        Hitter* hitter = AddHitter(st, attacker->GetGUID(), now);
        // Quest mob: the quest-log scan runs at most once per attacker per QUEST_VERDICT_MS.
        if (!why)
        {
            if (!hitter->questCheckMs || getMSTimeDiff(hitter->questCheckMs, now) >= QUEST_VERDICT_MS)
            {
                hitter->questMob = IsQuestMob(bot, attacker);
                hitter->questCheckMs = now;
            }
            if (hitter->questMob)
                why = "quest";
        }
        // Caught: the SAME mob, from its first hit on this fight, still hitting (in melee range, or damage dealt
        // within HIT_WINDOW_MS) CaughtSeconds later.
        bool const hitting = attacker->IsWithinMeleeRange(bot) ||
                             (hitter->lastMs && getMSTimeDiff(hitter->lastMs, now) <= HIT_WINDOW_MS);
        if (!why && st.avoid == AVOID_RIDING && hitter->firstMs && hitting &&
            getMSTimeDiff(hitter->firstMs, now) >= sw.avoidCaughtSeconds * 1000)
            why = "caught";
    }

    // Stuck: never stand still in combat. AVOID_MOVE_YARDS of real movement within AVOID_STUCK_MS while riding.
    if (st.avoid == AVOID_RIDING)
    {
        float const dx = bot->GetPositionX() - st.avoidX;
        float const dy = bot->GetPositionY() - st.avoidY;
        if (dx * dx + dy * dy >= AVOID_MOVE_YARDS * AVOID_MOVE_YARDS)
        {
            st.avoidX = bot->GetPositionX();
            st.avoidY = bot->GetPositionY();
            st.avoidMoveMs = now;
        }
        else if (!why && getMSTimeDiff(st.avoidMoveMs, now) >= AVOID_STUCK_MS)
            why = "stuck";
    }

    uint8 const before = st.avoid;
    {
        std::lock_guard<std::mutex> lock(gLogMutex);
        TripLogState& cur = gLogStates[low];
        if (why)
            cur.avoid = AVOID_FOUGHT;
        else if (before == AVOID_NONE)
        {
            cur.avoid = AVOID_RIDING;
            cur.avoidStartMs = now;
            cur.avoidMoveMs = now;
            cur.avoidX = bot->GetPositionX();
            cur.avoidY = bot->GetPositionY();
        }
        else
        {
            cur.avoidMoveMs = st.avoidMoveMs;
            cur.avoidX = st.avoidX;
            cur.avoidY = st.avoidY;
        }
        for (Hitter const& seen : st.hitters)
            if (seen.guid)
                MergeQuestVerdict(cur, seen);
    }

    if (!why && before == AVOID_NONE)
        LOG_INFO("playerbots", "Companion {}: trip combat avoid start attackers={} hp={:.0f} target={} lease={}",
                 bot->GetName(), attackers, bot->GetHealthPct(), first ? first->GetEntry() : 0, leaseOwner);
    else if (why && before == AVOID_RIDING)
    {
        LOG_INFO("playerbots", "Companion {}: trip combat avoid end result=fought reason={} dur={} lease={}",
                 bot->GetName(), why, getMSTimeDiff(st.avoidStartMs, now) / 1000,
                 leaseOwner.empty() ? "none" : leaseOwner);
        // Fight now: dismount at once instead of waiting for an Attack that passes its checks or the 10-s tick.
        if (first && bot->IsMounted())
            DismountForCombat(botAI, first, "avoid-fallback");
    }
    else if (why && !silent)
        LOG_INFO("playerbots",
                 "Companion {}: trip combat avoid skip reason={} attackers={} hp={:.0f} mounted={} lease={}",
                 bot->GetName(), why, attackers, bot->GetHealthPct(), bot->IsMounted() ? 1 : 0, leaseOwner);
    return !why;
}

bool CompanionTrip::AssistRefused(PlayerbotAI* botAI, Unit* target)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot || !target || !botAI->IsCompanion() || !NoAssistEnabled() || !AvoidEnabled())
        return false;
    Unit* victimOfTarget = target->GetVictim();
    Player* victim = victimOfTarget ? victimOfTarget->ToPlayer() : nullptr;
    if (!victim || victim == bot || !IsCompanionBot(victim) || !Avoiding(victim->GetGUID().GetCounter()))
        return false;
    uint32 const now = getMSTime();
    {
        std::lock_guard<std::mutex> lock(gLogMutex);
        TripLogState& st = gLogStates[bot->GetGUID().GetCounter()];
        if (st.lastAssistLogMs && getMSTimeDiff(st.lastAssistLogMs, now) < ASSIST_LOG_MS)
            return true;
        st.lastAssistLogMs = now;
    }
    LOG_INFO("playerbots", "Companion {}: trip combat assist refused target={} victim={}", bot->GetName(),
             target->GetEntry(), victim->GetName());
    return true;
}

bool CompanionTrip::PeaceActive(PlayerbotAI* botAI)
{
    if (!botAI || !botAI->IsCompanion() || !PeaceEnabled() || !CompanionLease::Enabled())
        return false;
    Player* bot = botAI->GetBot();
    if (!bot || bot->InBattleground() || bot->InArena())
        return false;
    // Active is enough: a paused lease (combat) keeps the gate, attackers stay allowed.
    return CompanionLease::Current(bot->GetGUID().GetCounter()).active;
}

char const* CompanionTrip::PeaceReason(PlayerbotAI* botAI, Unit* unit)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot || !unit)
        return nullptr;
    if (IsOurSide(bot, botAI, unit->GetVictim()) || InAttackers(botAI, unit))
        return "attacked";
    if (bot->GetDistance(unit) > PeaceQuestYards())
        return nullptr;
    return QuestReason(bot, unit);
}

void CompanionTrip::NoteAttack(PlayerbotAI* botAI, Unit* target, char const* via)
{
    if (!botAI || !target || !botAI->IsCompanion() || !PeaceEnabled())
        return;
    Player* bot = botAI->GetBot();
    if (!bot)
        return;
    uint32 const low = bot->GetGUID().GetCounter();
    CompanionLease::Info const lease = CompanionLease::Current(low);
    if (!lease.active)
        return;
    {
        std::lock_guard<std::mutex> lock(gLogMutex);
        TripLogState& st = gLogStates[low];
        if (!st.fightStartMs)
        {
            st.fightStartMs = getMSTime();
            st.fightMounted = bot->IsMounted();
            st.fightDismounts = 0;
        }
        if (st.lastTarget == target->GetGUID())
            return;
        st.lastTarget = target->GetGUID();
    }
    char const* reason = PeaceReason(botAI, target);
    LOG_INFO("playerbots",
             "Companion {}: trip combat start target={} name=\"{}\" reason={} via={} lease={} dist={:.1f} mounted={}",
             bot->GetName(), target->GetEntry(), target->GetName(), reason ? reason : "other", via ? via : "?",
             lease.owner, bot->GetDistance(target), bot->IsMounted() ? 1 : 0);
}

CompanionTrip::SelfDismount::SelfDismount(Player* bot, char const* via, uint32 attackerEntry)
    : _bot(bot), _via(via), _attacker(attackerEntry), _wasMounted(bot && bot->IsMounted())
{
    if (_wasMounted)
    {
        auto const& effects = bot->GetAuraEffectsByType(SPELL_AURA_MOUNTED);
        if (!effects.empty())
            _spell = effects.front()->GetId();
    }
    ++gSelfDismountDepth;
}

CompanionTrip::SelfDismount::~SelfDismount()
{
    --gSelfDismountDepth;
    if (_wasMounted && _bot && !_bot->IsMounted() && IsCompanionBot(_bot))
        LogDismount(_bot, "self", _spell, _attacker, _via);
}

bool CompanionTrip::DismountForCombat(PlayerbotAI* botAI, Unit* target, char const* via)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot || !botAI->IsCompanion() || !CombatDismountEnabled() || !bot->IsMounted())
        return false;
    // Never off a taxi, a vehicle or a flying mount in the air.
    if (bot->IsInFlight() || bot->HasUnitState(UNIT_STATE_IN_FLIGHT) || bot->GetVehicle() || bot->IsFlying())
        return false;
    if (bot->isMoving())
        bot->StopMoving();
    {
        SelfDismount scope(bot, via, target ? target->GetEntry() : 0);
        bot->RemoveAurasByType(SPELL_AURA_MOUNTED);   // the aura handler dismounts (and brings a hunter pet back)
    }
    uint32 const low = bot->GetGUID().GetCounter();
    {
        std::lock_guard<std::mutex> lock(gLogMutex);
        ++gLogStates[low].fightDismounts;
    }
    CompanionLease::Info const lease = CompanionLease::Current(low);
    LOG_INFO("playerbots", "Companion {}: trip combat dismount via={} target={} dist={:.1f} lease={}",
             bot->GetName(), via ? via : "?", target ? target->GetEntry() : 0,
             target ? bot->GetDistance(target) : 0.0f, lease.active ? lease.owner : "none");
    return true;
}

void CompanionTrip::NoteDamage(Unit* attacker, Unit* victim)
{
    if (!attacker || !victim || attacker == victim)
        return;
    Player* bot = victim->ToPlayer();
    if (!bot || !IsCompanionBot(bot))
        return;
    uint32 const now = getMSTime();
    std::lock_guard<std::mutex> lock(gLogMutex);
    TripLogState& st = gLogStates[bot->GetGUID().GetCounter()];
    Hitter* hitter = AddHitter(st, attacker->GetGUID(), now);
    hitter->lastMs = now;
    st.lastHitMs = now;
    st.lastHitEntry = attacker->GetEntry();
}

void CompanionTrip::NoteAuraApplied(Unit* unit, Aura const* aura)
{
    Player* bot = unit ? unit->ToPlayer() : nullptr;
    if (!bot || !aura || !aura->GetSpellInfo() || aura->GetSpellInfo()->IsPositive() || !IsCompanionBot(bot))
        return;
    ObjectGuid const caster = aura->GetCasterGUID();
    uint32 const now = getMSTime();
    std::lock_guard<std::mutex> lock(gLogMutex);
    TripLogState& st = gLogStates[bot->GetGUID().GetCounter()];
    st.lastAuraMs = now;
    st.lastAuraSpell = aura->GetSpellInfo()->Id;
    st.lastAuraHostile = !caster.IsEmpty() && caster != bot->GetGUID();
}

void CompanionTrip::NoteMountRemoved(Unit* unit, AuraRemoveMode removeMode)
{
    Player* bot = unit ? unit->ToPlayer() : nullptr;
    if (!bot || gSelfDismountDepth || !DismountLogEnabled() || !IsCompanionBot(bot))
        return;
    // Causes from the removal mode first: cancel, expiry and death are not an attack on the mount.
    if (removeMode == AURA_REMOVE_BY_CANCEL || removeMode == AURA_REMOVE_BY_EXPIRE ||
        removeMode == AURA_REMOVE_BY_DEATH)
    {
        LogDismount(bot, "other", 0, 0, nullptr);
        return;
    }
    uint32 const now = getMSTime();
    TripLogState st;
    {
        std::lock_guard<std::mutex> lock(gLogMutex);
        auto it = gLogStates.find(bot->GetGUID().GetCounter());
        if (it != gLogStates.end())
            st = it->second;
    }
    bool const recentAura = st.lastAuraMs && getMSTimeDiff(st.lastAuraMs, now) <= CAUSE_WINDOW_MS;
    bool const recentHit = st.lastHitMs && getMSTimeDiff(st.lastHitMs, now) <= CAUSE_WINDOW_MS;
    if (removeMode == AURA_REMOVE_BY_ENEMY_SPELL)
        LogDismount(bot, "spell", recentAura ? st.lastAuraSpell : 0, 0, nullptr);
    else if (recentAura && st.lastAuraSpell == DAZE_SPELL)
        LogDismount(bot, "daze", DAZE_SPELL, recentHit ? st.lastHitEntry : 0, nullptr);
    else if (recentAura && st.lastAuraHostile)
        LogDismount(bot, "spell", st.lastAuraSpell, recentHit ? st.lastHitEntry : 0, nullptr);
    else if (recentHit)
        LogDismount(bot, "damage", 0, st.lastHitEntry, nullptr);
    else
        LogDismount(bot, "other", 0, 0, nullptr);
}

void CompanionTrip::CombatTick(PlayerbotAI* botAI)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot || !botAI->IsCompanion())
        return;
    uint32 const low = bot->GetGUID().GetCounter();
    // Ride-through (TripAvoid): ends a run once out of combat; while it runs, no safety-net dismount.
    if (AvoidActive(botAI))
        return;
    if (bot->IsInCombat())
    {
        // Safety net: attacked while mounted and no attack action dismounted us (e.g. no attack target picked).
        if (bot->IsMounted() && bot->IsAlive() && !bot->getAttackers().empty())
        {
            Unit* attacker = *bot->getAttackers().begin();
            DismountForCombat(botAI, attacker, "tick");
        }
        return;
    }
    uint32 startMs = 0;
    bool mounted = false;
    uint32 dismounts = 0;
    {
        std::lock_guard<std::mutex> lock(gLogMutex);
        auto it = gLogStates.find(low);
        if (it == gLogStates.end() || !it->second.fightStartMs)
            return;
        startMs = it->second.fightStartMs;
        mounted = it->second.fightMounted;
        dismounts = it->second.fightDismounts;
        it->second.fightStartMs = 0;
        it->second.fightDismounts = 0;
        it->second.lastTarget = ObjectGuid::Empty;
    }
    CompanionLease::Info const lease = CompanionLease::Current(low);
    LOG_INFO("playerbots", "Companion {}: trip combat end dur={} mounted={} dismounts={} lease={}", bot->GetName(),
             getMSTimeDiff(startMs, getMSTime()) / 1000, mounted ? 1 : 0, dismounts,
             lease.active ? lease.owner : "none");
}

float CompanionTrip::MountHostileRange(PlayerbotAI* botAI) { return PeaceActive(botAI) ? 10.0f : 25.0f; }

void CompanionTrip::NoteMount(PlayerbotAI* botAI, uint32 spell, float leg)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot)
        return;
    CompanionLease::Info const lease = CompanionLease::Current(bot->GetGUID().GetCounter());
    LOG_INFO("playerbots", "Companion {}: trip mount spell={} leg={:.0f} lease={}", bot->GetName(), spell, leg,
             lease.active ? lease.owner : "none");
}

void CompanionTrip::NoteMountBlocked(PlayerbotAI* botAI, float range)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot)
        return;
    uint32 const low = bot->GetGUID().GetCounter();
    uint32 const now = getMSTime();
    {
        std::lock_guard<std::mutex> lock(gLogMutex);
        TripLogState& st = gLogStates[low];
        if (st.lastMountBlockMs && getMSTimeDiff(st.lastMountBlockMs, now) < 60000)
            return;
        st.lastMountBlockMs = now;
    }
    CompanionLease::Info const lease = CompanionLease::Current(low);
    LOG_INFO("playerbots", "Companion {}: trip mount blocked reason=hostile-near range={:.0f} lease={}",
             bot->GetName(), range, lease.active ? lease.owner : "none");
}

void CompanionTrip::ApproachPoint(Player* bot, WorldObject* target, float& x, float& y, float& z)
{
    x = target->GetPositionX();
    y = target->GetPositionY();
    z = target->GetPositionZ();
    if (!bot)
        return;
    float const o = target->GetOrientation();
    float px = x + std::cos(o) * 1.5f;
    float py = y + std::sin(o) * 1.5f;
    float pz = z;
    bot->UpdateAllowedPositionZ(px, py, pz);
    if (std::fabs(pz - z) > 1.5f)
        return;
    if (!target->IsWithinLOS(px, py, pz + 1.0f))
        return;
    x = px;
    y = py;
    z = pz;
}

bool CompanionTrip::CheckApproach(Player* bot, WorldObject* target, float x, float y, float z, float& gap,
                                  uint32& pathType)
{
    PathGenerator path(bot);
    path.CalculatePath(x, y, z, false);
    pathType = path.GetPathType();
    G3D::Vector3 const& end = path.GetActualEndPosition();
    gap = target->GetExactDist(end.x, end.y, end.z);
    if (pathType == PATHFIND_BLANK || (pathType & PATHFIND_NOPATH))
        return false;
    if (pathType & PATHFIND_NOT_USING_PATH)
        return true;   // no mmaps here: nothing to judge
    if (gap <= INTERACTION_DISTANCE)
        return true;
    // Incomplete or truncated: still fine while the path end gets 5 yd closer to the target than the bot is
    // (as MoveFarTo judges it); a dead end at a wall or under the floor does not.
    return gap + 5.0f < bot->GetExactDist(target);
}
