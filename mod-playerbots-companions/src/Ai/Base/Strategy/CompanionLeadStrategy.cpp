/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "CompanionLeadStrategy.h"
#include "CompanionClassStrategy.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <list>
#include <mutex>
#include <set>
#include <sstream>
#include <unordered_map>
#include <vector>

#include "AttackAction.h"
#include "AttackersValue.h"
#include "CellImpl.h"
#include "CompanionBotMgr.h"
#include "CompanionDuty.h"
#include "CompanionGatherStrategy.h"
#include "Creature.h"
#include "EncounterHelpers.h"
#include "Event.h"
#include "Group.h"
#include "GameTime.h"
#include "GridNotifiers.h"
#include "GridNotifiersImpl.h"
#include "Log.h"
#include "Map.h"
#include "MovementActions.h"
#include "Multiplier.h"
#include "ObjectAccessor.h"
#include "PathGenerator.h"
#include "Spell.h"
#include "SpellAuraEffects.h"
#include "SpellInfo.h"
#include "ThreatManager.h"
#include "Timer.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "Playerbots.h"
#include "Trigger.h"
#include "AiObjectContext.h"
#include "Action.h"

namespace
{
// Drinking detection by aura type rather than a fixed spell ID list (issue #86): matches any seated
// mana-regeneration drink aura (including food+drink refreshments and all Classic/TBC/WotLK ranks).
bool IsDrinking(Unit* unit)
{
    if (!unit)
        return false;
    for (AuraType type : {SPELL_AURA_MOD_POWER_REGEN, SPELL_AURA_OBS_MOD_POWER})
    {
        for (AuraEffect const* eff : unit->GetAuraEffectsByType(type))
        {
            if (!eff)
                continue;
            SpellInfo const* info = eff->GetSpellInfo();
            if (eff->GetMiscValue() == POWER_MANA && info &&
                (info->AuraInterruptFlags & AURA_INTERRUPT_FLAG_NOT_SEATED))
                return true;
        }
    }
    return false;
}

// A hostile, unengaged creature the tank may pull: not a critter, not fleeing or evading.
bool IsPullable(Player* bot, Unit* unit)
{
    if (!unit || !unit->IsAlive() || !unit->IsInWorld() || unit->GetMapId() != bot->GetMapId())
        return false;
    if (!unit->IsCreature() || unit->IsInCombat())
        return false;
    Creature* creature = unit->ToCreature();
    if (!creature || creature->IsCritter() || creature->IsInEvadeMode() || unit->HasUnitState(UNIT_STATE_FLEEING))
        return false;
    if (!bot->IsHostileTo(unit))
        return false;
    return AttackersValue::IsPossibleTarget(unit, bot);
}

// Engaged with the group: in combat with ANY group member, the owner included. A mob that attacks the healer counts.
bool EngagedWithGroup(Unit* unit, Group* group)
{
    for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
    {
        Player* member = itr->GetSource();
        if (member && unit->IsEngagedBy(member))
            return true;
    }
    return false;
}

// Skull target within range. For assist, the unit must be engaged with the group (see EngagedWithGroup). For the
// pull, it must be pullable (not yet engaged).
Unit* SkullUnit(PlayerbotAI* botAI, bool engaged)
{
    Player* bot = botAI->GetBot();
    Group* group = bot->GetGroup();
    if (!group)
        return nullptr;
    ObjectGuid guid = group->GetTargetIcon(CompanionLead::SKULL_ICON);
    if (guid.IsEmpty())
        return nullptr;
    Unit* unit = botAI->GetUnit(guid);
    if (!unit || !unit->IsAlive() || !unit->IsInWorld() || unit->GetMapId() != bot->GetMapId())
        return nullptr;
    if (bot->GetDistance(unit) > sPlayerbotAIConfig.companionLeadSkullRange || !bot->IsHostileTo(unit))
        return nullptr;
    if (engaged)
        return EngagedWithGroup(unit, group) ? unit : nullptr;
    return IsPullable(bot, unit) ? unit : nullptr;
}

// Lead mode applies in a dungeon/raid grouped with the owner, OR in a 5-player dungeon when in an
// authorized Phase II cooperative group (AiPlayerbot.Companion.Coop.Enabled=1).
bool LeadActive(Player* bot)
{
    if (!bot->GetMap() || !(bot->GetMap()->IsDungeon() || bot->GetMap()->IsRaid()))
        return false;
    CompanionBotMgr& mgr = sCompanionBotMgr;
    if (!mgr.IsCompanion(bot) || !mgr.LeadModeOn(bot->GetGUID().GetCounter()))
        return false;
    if (mgr.IsGroupedWithOwner(bot))
        return true;
    return bot->GetMap()->IsDungeon() && !bot->GetMap()->IsRaid() && mgr.IsInAuthorizedCoopGroup(bot);
}

// Per-companion state written from map threads: gStateMutex covers per-bot/kill-order tables below.
// gLeadMutex is kept strictly separate from gStateMutex so GroupLead/IsGroupLead can never self-deadlock.
std::mutex gStateMutex;
std::mutex gLeadMutex;
std::unordered_map<uint32, ObjectGuid> gGroupLead;   // group low guid -> elected lead tank guid
uint32 gLastLeadPruneMs = 0;

bool GroupInCombat(Group* group)
{
    if (!group)
        return false;
    for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
    {
        Player* member = itr->GetSource();
        if (member && member->IsInWorld() && member->IsInCombat())
            return true;
    }
    return false;
}

// Convenience wrapper inside this translation unit: returns the elected lead ONLY when alive (so callers like
// MeleeFollowTarget and AssistTarget never follow or assist a corpse).
Player* FindCompanionTank(Group* group, Player const* reference = nullptr)
{
    Player* lead = CompanionLead::GroupLead(group, reference);
    return (lead && lead->IsAlive()) ? lead : nullptr;
}

// Lead v2 (3c): healer is the group member with the heal role; if none, the owner.
Player* FindGroupHealer(Player* tank, Group* group)
{
    if (group)
    {
        for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
        {
            Player* member = itr->GetSource();
            if (!member || member == tank || !member->IsInWorld())
                continue;
            if (PlayerbotAI::IsHeal(member))
                return member;
        }
    }
    return sCompanionBotMgr.FindOwnerOnlineFor(tank);
}

// Lead v2 (2b): breakable crowd control that DPS must not break (stuns like HoJ/Kidney Shot are NOT excluded).
bool IsCrowdControlled(Unit* unit)
{
    return unit && (unit->IsPolymorphed() || unit->HasBreakableByDamageCrowdControlAura());
}

// Lead v2: soft healer tether state per companion tank (before a new pull only).
struct HealerTetherState
{
    uint32 waitingSinceMs = 0;
    bool announced = false;
    uint32 overrideSetMs = 0;
};
std::unordered_map<uint32, HealerTetherState> gHealerTether; // tank low guid -> tether state

// Ready check before a pull. Always: nobody in the group is in combat, every member is alive and above
// MinMemberHealthPct, the owner is not AFK or drinking and has at least MinHealerManaPct mana, and the PauseSeconds
// since the last group fight have passed. withRange adds: every member within GroupRange of the tank.
bool ReadyToPull(PlayerbotAI* botAI, bool withRange)
{
    Player* bot = botAI->GetBot();
    Group* group = bot->GetGroup();
    CompanionBotMgr& mgr = sCompanionBotMgr;
    if (!group || !LeadActive(bot))
        return false;

    bool const coopGroup = !mgr.IsGroupedWithOwner(bot) && mgr.IsInAuthorizedCoopGroup(bot);
    Player* owner = coopGroup ? FindGroupHealer(bot, group) : mgr.FindOwnerOnlineFor(bot);
    if (!owner)
        return false;

    Player* healer = sPlayerbotAIConfig.companionLeadHealerTetherEnabled ? FindGroupHealer(bot, group) : nullptr;
    uint32 now = static_cast<uint32>(GameTime::GetUptime().count());
    uint32 low = bot->GetGUID().GetCounter();
    bool ready = true;
    for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
    {
        Player* member = itr->GetSource();
        if (!member)
            continue;
        if (member->IsInCombat())
        {
            mgr.NoteGroupCombat(low, now);
            {
                std::lock_guard<std::mutex> lock(gStateMutex);
                auto it = gHealerTether.find(low);
                if (it != gHealerTether.end())
                    it->second.overrideSetMs = 0;
            }
            ready = false;
        }
        if (!member->IsAlive() || member->GetMapId() != bot->GetMapId() ||
            member->GetHealthPct() < sPlayerbotAIConfig.companionLeadMinMemberHealthPct)
            ready = false;
        if (PlayerbotAI::IsHeal(member) && IsDrinking(member))
            ready = false;
        // Phase 2: a companion gathering a node or skinning is not waited for (the group does not stop for it).
        // Lead v2: when HealerTether is on, the healer's distance/LOS is governed by the soft healer tether instead.
        if (withRange && member != healer && member->GetMapId() == bot->GetMapId() &&
            bot->GetDistance(member) > sPlayerbotAIConfig.companionLeadGroupRange &&
            !CompanionGather::IsGathering(member->GetGUID().GetCounter()))
            ready = false;
    }
    if (!ready)
        return false;

    if (owner->isAFK() || IsDrinking(owner))
        return false;
    // A companion that needs to eat (Smoked Sagefish, no Well Fed) holds the pull for up to 20 seconds.
    if (CompanionClass::BlockPullForConsumables(botAI))
        return false;
    uint32 maxMana = owner->GetMaxPower(POWER_MANA);
    if (maxMana > 0 && owner->GetPower(POWER_MANA) * 100 / maxMana < sPlayerbotAIConfig.companionLeadMinHealerManaPct)
        return false;

    uint32 lastFight = mgr.LastGroupCombat(low);
    if (lastFight && now - lastFight < sPlayerbotAIConfig.companionLeadPauseSeconds)
        return false;
    return true;
}

// --- Far pulls (step 3) -------------------------------------------------------------------------------------------

constexpr float PACK_RADIUS = 15.0f;        // a path passing this close to another pullable unit goes through a pack
constexpr uint32 FAR_RECHECK_MS = 2000;     // the far search runs at most this often per companion
constexpr size_t FAR_PATH_CHECKS = 6;       // nearest pullable units (straight line) that get a path check

struct FarCache
{
    uint32 atMs = 0;
    bool found = false;
    ObjectGuid guid;
};

// Per-companion state tables guarded by gStateMutex (declared above ReadyToPull).
std::unordered_map<uint32, FarCache> gFarCache;        // low guid -> last far search
std::unordered_map<uint32, uint32> gWaitLog;           // low guid -> seconds of the last "waiting for group" line
std::unordered_map<uint32, ObjectGuid> gLastAttack;    // low guid -> last attack target (empty = follow tank)
std::unordered_map<uint32, ObjectGuid> gNotYetSaid;    // low guid -> skull already refused with "Not yet."
std::unordered_map<uint32, ObjectGuid> gChainLogged;   // low guid -> skull already logged as a chain pull
std::unordered_map<uint32, ObjectGuid> gChainPulled;   // low guid -> unit the chain pull opened on

// Lead v2: per-DPS target persistence state.
struct PersistState
{
    ObjectGuid lockedGuid;
    uint32 unreachableSinceMs = 0;
    float minDistSeen = 0.0f;
    ObjectGuid lastSwitchedSkull;
};
std::unordered_map<uint32, PersistState> gPersist;          // DPS low guid -> target persistence state
std::unordered_map<ObjectGuid, uint32> gTankAttackStart;    // mob guid -> getMSTime when tank first attacked it

// --- Kill Order v1 (AiPlayerbot.Companion.Lead.KillOrder, default 0 until owner tests) ---------------------------
constexpr uint8 ORDER_ICONS[3] = {CompanionLead::SKULL_ICON, CompanionLead::CROSS_ICON, CompanionLead::CIRCLE_ICON};

struct MobOrderInfo
{
    uint32 firstHitMs = 0;
    bool sawHeal = false;
    bool sawCast = false;
    bool sawFlee = false;
    bool sawLowHp = false;
};

struct KillOrderState
{
    bool active = false;
    uint32 packId = 0;
    uint32 pullStartMs = 0;
    bool initialDone = false;
    uint32 nonDeathMoves = 0;   // mark moves without a death or new mob (capped <= 2 per pack)
    std::unordered_map<ObjectGuid, MobOrderInfo> mobs;
    std::set<ObjectGuid> lastAlive;
    std::vector<ObjectGuid> order;
    std::array<ObjectGuid, 8> botPlacedIcon = {};
    std::array<ObjectGuid, 8> ownerLockedIcon = {};
};

std::unordered_map<uint32, KillOrderState> gKillOrders;   // group low guid -> kill order state
uint32 gNextPackId = 1;

// Single mark-placement helper for lead mode (issue #86): keeps group target icons and botPlacedIcon ownership
// records in sync everywhere the lead sets or clears a raid icon. Both pull-time and UpdateKillOrder mark
// writes run on the single elected lead bot's update thread.
void SetLeadIcon(Group* group, Player* lead, uint8 icon, ObjectGuid targetGuid, KillOrderState* localState = nullptr)
{
    if (!group || !lead || icon >= 8)
        return;
    if (group->GetTargetIcon(icon) != targetGuid)
        group->SetTargetIcon(icon, lead->GetGUID(), targetGuid);
    if (localState)
    {
        localState->botPlacedIcon[icon] = targetGuid;
        return;
    }
    if (!sPlayerbotAIConfig.companionLeadKillOrder)
        return;
    uint32 const gid = group->GetGUID().GetCounter();
    std::lock_guard<std::mutex> lock(gStateMutex);
    gKillOrders[gid].botPlacedIcon[icon] = targetGuid;
}

bool IsCastingHeal(Unit* unit)
{
    for (CurrentSpellTypes t : {CURRENT_GENERIC_SPELL, CURRENT_CHANNELED_SPELL})
    {
        Spell* sp = unit->GetCurrentSpell(t);
        SpellInfo const* info = sp ? sp->GetSpellInfo() : nullptr;
        if (!info)
            continue;
        for (uint8 i = 0; i < MAX_SPELL_EFFECTS; ++i)
        {
            uint32 const eff = info->Effects[i].Effect;
            if (eff == SPELL_EFFECT_HEAL || eff == SPELL_EFFECT_HEAL_PCT || eff == SPELL_EFFECT_HEAL_MAX_HEALTH ||
                eff == SPELL_EFFECT_HEAL_MECHANICAL)
                return true;
            if (eff == SPELL_EFFECT_APPLY_AURA && info->Effects[i].ApplyAuraName == SPELL_AURA_PERIODIC_HEAL)
                return true;
        }
    }
    return false;
}

bool IsCastingAnySpell(Unit* unit)
{
    return unit->GetCurrentSpell(CURRENT_GENERIC_SPELL) != nullptr ||
           unit->GetCurrentSpell(CURRENT_CHANNELED_SPELL) != nullptr;
}

char const* OrderIconName(uint8 icon)
{
    if (icon == CompanionLead::SKULL_ICON)
        return "Skull";
    if (icon == CompanionLead::CROSS_ICON)
        return "X";
    if (icon == CompanionLead::CIRCLE_ICON)
        return "Circle";
    return "?";
}

void NoteTankAttack(Unit* target)
{
    if (!target)
        return;
    uint32 const now = getMSTime();
    ObjectGuid const guid = target->GetGUID();
    std::lock_guard<std::mutex> lock(gStateMutex);
    if (gTankAttackStart.size() > 256)
    {
        for (auto it = gTankAttackStart.begin(); it != gTankAttackStart.end();)
        {
            if (getMSTimeDiff(it->second, now) > 60000)
                it = gTankAttackStart.erase(it);
            else
                ++it;
        }
    }
    if (gTankAttackStart.find(guid) == gTankAttackStart.end())
        gTankAttackStart[guid] = now;
}

// Lead v2 (2c): true when unit is engaged with the group AND (has threat on tank, targets a group member, or
// TankEngageSeconds have elapsed since the tank started attacking it). Never true for an unengaged mob.
bool TankEngagedForDps(Player* tank, Unit* unit, Group* group, uint32 nowMs)
{
    if (!unit || !group || !unit->IsAlive())
        return false;
    bool const engagedGroup = EngagedWithGroup(unit, group);
    if (!engagedGroup && !unit->IsInCombat())
        return false;
    if (tank)
    {
        if (unit->GetThreatMgr().GetThreat(tank) > 0.0f || unit->GetVictim() == tank)
            return true;
    }
    if (Unit* mobVictim = unit->GetVictim())
        if (group->IsMember(mobVictim->GetGUID()))
            return true;
    uint32 startMs = 0;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gTankAttackStart.find(unit->GetGUID());
        if (it != gTankAttackStart.end())
            startMs = it->second;
    }
    uint32 const engageMs = sPlayerbotAIConfig.companionLeadTankEngageSeconds * IN_MILLISECONDS;
    if (startMs != 0 && unit->IsInCombat() && getMSTimeDiff(startMs, nowMs) >= engageMs)
        return true;
    return false;
}

// Lead v2 (3a): soft healer tether check before a NEW pull only. Returns true when pull is allowed to proceed.
bool CheckHealerTetherForNewPull(PlayerbotAI* botAI, Group* group)
{
    if (!sPlayerbotAIConfig.companionLeadHealerTetherEnabled)
        return true;
    Player* bot = botAI->GetBot();
    uint32 const low = bot->GetGUID().GetCounter();
    uint32 const now = getMSTime();

    // Engaged skull bypasses tether (already attacking the group).
    if (SkullUnit(botAI, true) != nullptr)
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        gHealerTether[low].waitingSinceMs = 0;
        gHealerTether[low].announced = false;
        return true;
    }

    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        HealerTetherState& st = gHealerTether[low];
        if (st.overrideSetMs != 0 && getMSTimeDiff(st.overrideSetMs, now) < 15000)
            return true;
        st.overrideSetMs = 0;
    }

    Player* healer = FindGroupHealer(bot, group);
    float const maxRange = sPlayerbotAIConfig.companionLeadHealerTetherRange;
    float const dist = (healer && healer->IsInWorld() && healer->GetMapId() == bot->GetMapId())
                           ? bot->GetDistance(healer)
                           : 999.0f;
    bool const inLos = healer && healer->IsAlive() && healer->IsInWorld() && healer->GetMapId() == bot->GetMapId() &&
                       bot->IsWithinLOSInMap(healer);
    if (healer && healer->IsAlive() && dist <= maxRange && inLos)
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        HealerTetherState& st = gHealerTether[low];
        st.waitingSinceMs = 0;
        st.announced = false;
        return true;
    }

    bool shouldAnnounce = false;
    bool firstWait = false;
    uint32 const waitMs = sPlayerbotAIConfig.companionLeadHealerTetherWaitSeconds * IN_MILLISECONDS;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        HealerTetherState& st = gHealerTether[low];
        if (st.waitingSinceMs == 0)
        {
            st.waitingSinceMs = now;
            st.announced = false;
            firstWait = true;
        }
        else if (!st.announced && getMSTimeDiff(st.waitingSinceMs, now) >= waitMs)
        {
            st.announced = true;
            shouldAnnounce = true;
        }
    }

    char const* healerName = healer ? healer->GetName().c_str() : "none";
    if (firstWait)
        LOG_INFO("playerbots", "[Companion] {} healer tether wait: healer={} dist={:.1f} los={}", bot->GetName(),
                 healerName, dist, inLos ? 1 : 0);
    if (shouldAnnounce)
    {
        botAI->SayToParty("Waiting for heals.");
        LOG_INFO("playerbots", "[Companion] {} healer tether: Waiting for heals. (healer={} dist={:.1f} los={})",
                 bot->GetName(), healerName, dist, inLos ? 1 : 0);
    }
    return false;
}

constexpr uint32 CHAIN_RECHECK_MS = 1000;   // the chain check (owner mana, health, engaged count) is cached this long

struct ChainCache
{
    uint32 atMs = 0;
    bool valid = false;
    std::string reason;   // empty when the chain pull is allowed
    uint32 engaged = 0;
    uint32 manaPct = 0;
};
std::unordered_map<uint32, ChainCache> gChainCache;

// Walking distance: the walk from the bot to the unit along the path, in yards.
float PathLength(Player* bot, Movement::PointsArray const& points)
{
    float len = 0.0f;
    float px = bot->GetPositionX();
    float py = bot->GetPositionY();
    for (auto const& point : points)
    {
        float dx = point.x - px;
        float dy = point.y - py;
        len += std::sqrt(dx * dx + dy * dy);
        px = point.x;
        py = point.y;
    }
    return len;
}

// The other pullable unit that a path point passes within PACK_RADIUS of, if any. One substitution, no recursion.
Unit* PackOnPath(Movement::PointsArray const& points, std::vector<Unit*> const& pullables, Unit* target)
{
    for (auto const& point : points)
    {
        for (Unit* other : pullables)
        {
            if (other == target)
                continue;
            float dx = other->GetPositionX() - point.x;
            float dy = other->GetPositionY() - point.y;
            if (std::sqrt(dx * dx + dy * dy) < PACK_RADIUS)
                return other;
        }
    }
    return nullptr;
}

// Pullable units within the search range, ranked by path distance (the 6 nearest in straight line get a path check).
Unit* FindFarPull(PlayerbotAI* botAI)
{
    Player* bot = botAI->GetBot();
    float range = sPlayerbotAIConfig.companionLeadSearchRange;

    std::list<Unit*> units;
    Acore::AnyUnitInObjectRangeCheck check(bot, range);
    Acore::UnitListSearcher<Acore::AnyUnitInObjectRangeCheck> searcher(bot, units, check);
    Cell::VisitObjects(bot, searcher, range);

    std::vector<Unit*> pullables;
    for (Unit* unit : units)
        if (IsPullable(bot, unit))
            pullables.push_back(unit);
    std::sort(pullables.begin(), pullables.end(),
              [bot](Unit* a, Unit* b) { return bot->GetDistance(a) < bot->GetDistance(b); });

    Unit* best = nullptr;
    float bestLen = 0.0f;
    float limit = range * 1.5f;
    size_t checks = std::min(FAR_PATH_CHECKS, pullables.size());
    for (size_t i = 0; i < checks; ++i)
    {
        Unit* candidate = pullables[i];
        PathGenerator path(bot);
        path.CalculatePath(candidate->GetPositionX(), candidate->GetPositionY(), candidate->GetPositionZ());
        if (path.GetPathType() & PATHFIND_NOPATH)
            continue;
        Movement::PointsArray const& points = path.GetPath();
        float len = PathLength(bot, points);
        if (len > limit)
            continue;
        Unit* target = candidate;
        if (Unit* pack = PackOnPath(points, pullables, candidate))
            target = pack;   // never walk through a pack: the nearer pack is pulled first
        if (!best || len < bestLen)
        {
            best = target;
            bestLen = len;
        }
    }
    return best;
}

// Cached far search: one real search per FAR_RECHECK_MS per companion, negative results included.
Unit* FarPull(PlayerbotAI* botAI)
{
    Player* bot = botAI->GetBot();
    uint32 low = bot->GetGUID().GetCounter();
    uint32 now = getMSTime();
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gFarCache.find(low);
        if (it != gFarCache.end() && getMSTimeDiff(it->second.atMs, now) < FAR_RECHECK_MS)
        {
            if (!it->second.found)
                return nullptr;
            Unit* cached = botAI->GetUnit(it->second.guid);
            if (IsPullable(bot, cached))
                return cached;
            it->second.found = false;   // dead or engaged since the search: nothing until the next search
            return nullptr;
        }
    }

    Unit* found = FindFarPull(botAI);
    std::lock_guard<std::mutex> lock(gStateMutex);
    FarCache& cache = gFarCache[low];
    cache.atMs = now;
    cache.found = found != nullptr;
    cache.guid = found ? found->GetGUID() : ObjectGuid();
    return found;
}

// True when a group member is more than GroupRange from the bot (the tank waits for the group before a far pull).
bool GroupMemberBehind(Player* bot)
{
    Group* group = bot->GetGroup();
    if (!group)
        return false;
    for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
    {
        Player* member = itr->GetSource();
        if (!member || member == bot)
            continue;
        if (CompanionGather::IsGathering(member->GetGUID().GetCounter()))
            continue;   // phase 2: a gatherer catches up by itself
        if (member->GetMapId() != bot->GetMapId() ||
            bot->GetDistance(member) > sPlayerbotAIConfig.companionLeadGroupRange)
            return true;
    }
    return false;
}

// --- Chain pulls (tank, in combat) --------------------------------------------------------------------------------

// The chain check: owner mana, member health, and the number of distinct units attacking the group. Cached per bot.
ChainCache CachedChainCheck(PlayerbotAI* botAI)
{
    Player* bot = botAI->GetBot();
    uint32 low = bot->GetGUID().GetCounter();
    uint32 now = getMSTime();
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gChainCache.find(low);
        if (it != gChainCache.end() && it->second.valid && getMSTimeDiff(it->second.atMs, now) < CHAIN_RECHECK_MS)
            return it->second;
    }

    ChainCache result;
    result.atMs = now;
    result.valid = true;
    Group* group = bot->GetGroup();
    CompanionBotMgr& mgr = sCompanionBotMgr;
    Player* owner = mgr.FindOwnerOnlineFor(bot);
    if (!group)
        result.reason = "not in a group";
    else if (!owner)
        result.reason = "owner offline";
    else
    {
        uint32 maxMana = owner->GetMaxPower(POWER_MANA);
        result.manaPct = maxMana ? owner->GetPower(POWER_MANA) * 100 / maxMana : 100;
        if (maxMana && result.manaPct < sPlayerbotAIConfig.companionChainMinHealerManaPct)
            result.reason = "owner mana " + std::to_string(result.manaPct) + "%";

        std::vector<Unit*> engaged;
        for (GroupReference* itr = group->GetFirstMember(); itr != nullptr && result.reason.empty(); itr = itr->next())
        {
            Player* member = itr->GetSource();
            if (!member)
                continue;
            if (static_cast<uint32>(member->GetHealthPct()) < sPlayerbotAIConfig.companionChainMinMemberHealthPct)
                result.reason = std::string(member->GetName()) + " below health";
            for (Unit* attacker : member->getAttackers())
                if (std::find(engaged.begin(), engaged.end(), attacker) == engaged.end())
                    engaged.push_back(attacker);
        }
        result.engaged = static_cast<uint32>(engaged.size());
        if (result.reason.empty() && result.engaged > sPlayerbotAIConfig.companionChainMaxEngaged)
            result.reason = std::to_string(result.engaged) + " units engaged";
    }

    std::lock_guard<std::mutex> lock(gStateMutex);
    gChainCache[low] = result;
    return result;
}

// After a chain pull: the unit the tank opened on, while it is engaged with the tank and the owner is over 10 yards
// away. The tank walks back to the owner while it fights.
Unit* ChainReturnUnit(PlayerbotAI* botAI)
{
    Player* bot = botAI->GetBot();
    uint32 low = bot->GetGUID().GetCounter();
    ObjectGuid guid;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gChainPulled.find(low);
        if (it == gChainPulled.end())
            return nullptr;
        guid = it->second;
    }
    Unit* unit = botAI->GetUnit(guid);
    if (!unit || !unit->IsAlive() || !unit->IsEngagedBy(bot))
        return nullptr;
    Player* owner = sCompanionBotMgr.FindOwnerOnlineFor(bot);
    if (!owner || bot->GetMapId() != owner->GetMapId() || bot->GetDistance(owner) <= 10.0f)
        return nullptr;
    return unit;
}

// Logs a refusal once per skull target.
void RefuseOnce(PlayerbotAI* botAI, Unit* skull, std::string const& reason)
{
    Player* bot = botAI->GetBot();
    uint32 low = bot->GetGUID().GetCounter();
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gNotYetSaid.find(low);
        if (it != gNotYetSaid.end() && it->second == skull->GetGUID())
            return;
        gNotYetSaid[low] = skull->GetGUID();
    }
    botAI->SayToParty("Not yet.");
    LOG_INFO("playerbots", "[Companion] {} refused chain pull {}: {}", bot->GetName(), skull->GetName(), reason);
}
}  // namespace

namespace CompanionLead
{
Player* GroupLead(Group* group, Player const* reference)
{
    if (!group)
        return nullptr;

    // Group-level eligibility (independent of caller's map so an out-of-instance member can never evict the active
    // dungeon lead from gGroupLead). LeadActive already requires dungeon/raid map, LeadModeOn, and authorized group;
    // IsCompanion is kept explicitly here as defense-in-depth.
    CompanionBotMgr& mgr = sCompanionBotMgr;
    auto eligible = [group, &mgr](Player* p) -> bool {
        return p && p->IsInWorld() && p->GetGroup() == group && mgr.IsCompanion(p) && LeadActive(p) &&
               PlayerbotAI::IsTank(p);
    };

    Map* const refMap = (reference && reference->IsInWorld()) ? reference->GetMap() : nullptr;
    auto matchRefMap = [refMap](Player* p) -> Player* {
        if (!p)
            return nullptr;
        return (!refMap || p->GetMap() == refMap) ? p : nullptr;
    };

    uint32 const gid = group->GetGUID().GetCounter();
    ObjectGuid cached;
    {
        std::lock_guard<std::mutex> lock(gLeadMutex);
        auto it = gGroupLead.find(gid);
        if (it != gGroupLead.end())
            cached = it->second;
    }

    Player* cur = !cached.IsEmpty() ? ObjectAccessor::FindConnectedPlayer(cached) : nullptr;
    bool const valid = eligible(cur);
    // Stability policy (issue #83): keep the current lead while alive OR while the group is in combat so lead
    // never flips mid-fight on a tank death and bounces back on resurrection.
    if (valid && (cur->IsAlive() || GroupInCombat(group)))
        return matchRefMap(cur);

    // Deterministic re-election out of combat (or when no valid lead exists): alive eligible tank with lowest GUID.
    Player* best = nullptr;
    uint32 bestLow = UINT32_MAX;
    for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
    {
        Player* member = itr->GetSource();
        if (!member || !member->IsAlive() || !eligible(member))
            continue;
        uint32 const low = member->GetGUID().GetCounter();
        if (!best || low < bestLow)
        {
            best = member;
            bestLow = low;
        }
    }

    // If no alive eligible tank exists (e.g. solo tank died out of combat) and the cached lead is still valid in
    // the group, retain it to avoid strategy churn and repeated "lead elected" log spam while resurrecting.
    if (!best && valid)
        return matchRefMap(cur);

    ObjectGuid const elected = best ? best->GetGUID() : ObjectGuid::Empty;
    uint32 const nowMs = getMSTime();
    std::vector<std::pair<uint32, ObjectGuid>> pruneSnapshot;
    {
        std::lock_guard<std::mutex> lock(gLeadMutex);
        if (elected.IsEmpty())
            gGroupLead.erase(gid);
        else
            gGroupLead[gid] = elected;
        if (gGroupLead.size() > 64 && getMSTimeDiff(gLastLeadPruneMs, nowMs) >= 60000)
        {
            gLastLeadPruneMs = nowMs;
            pruneSnapshot.assign(gGroupLead.begin(), gGroupLead.end());
        }
    }
    if (!pruneSnapshot.empty())
    {
        std::vector<std::pair<uint32, ObjectGuid>> staleEntries;
        for (auto const& [k, v] : pruneSnapshot)
        {
            if (k == gid)
                continue;
            Player* p = ObjectAccessor::FindConnectedPlayer(v);
            if (!p || !p->GetGroup() || p->GetGroup()->GetGUID().GetCounter() != k)
                staleEntries.emplace_back(k, v);
        }
        if (!staleEntries.empty())
        {
            std::lock_guard<std::mutex> lock(gLeadMutex);
            for (auto const& [k, v] : staleEntries)
                if (auto it = gGroupLead.find(k); it != gGroupLead.end() && it->second == v)
                    gGroupLead.erase(it);
        }
    }
    if (best && elected != cached)
        LOG_INFO("playerbots", "[Companion] group={} lead elected -> {}[{}]", gid, best->GetName(), bestLow);
    return matchRefMap(best);
}

bool IsGroupLead(Player* bot)
{
    if (!bot || !bot->GetGroup() || !LeadActive(bot))
        return false;
    return bot == GroupLead(bot->GetGroup(), bot);
}

bool GroupHasActiveFight(Group* group)
{
    if (!group)
        return false;
    for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
    {
        Player* member = itr->GetSource();
        if (!member || !member->IsInWorld())
            continue;
        for (Unit* attacker : member->getAttackers())
            if (attacker && attacker->IsAlive() && attacker->IsInWorld() && attacker->IsCreature())
                return true;
    }
    return false;
}

Player* MeleeFollowTarget(PlayerbotAI* botAI)
{
    if (!sPlayerbotAIConfig.companionLeadMeleeFollowTank || !botAI)
        return nullptr;
    Player* bot = botAI->GetBot();
    if (!bot || !LeadActive(bot) || IsGroupLead(bot) || PlayerbotAI::IsHeal(bot) ||
        !PlayerbotAI::IsMelee(bot))
        return nullptr;
    Player* tank = FindCompanionTank(bot->GetGroup(), bot);
    if (!tank || tank == bot || tank->GetMapId() != bot->GetMapId())
        return nullptr;
    return tank;
}

bool NotifyOwnerPullCommand(PlayerbotAI* botAI, Player* requester)
{
    if (!botAI || !sPlayerbotAIConfig.companionLeadHealerTetherEnabled)
        return false;
    Player* bot = botAI->GetBot();
    if (!bot || !LeadActive(bot) || !IsGroupLead(bot))
        return false;
    if (requester && !sCompanionBotMgr.IsOwner(requester))
        return false;

    uint32 const low = bot->GetGUID().GetCounter();
    uint32 const now = getMSTime();
    bool wasWaiting = false;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        HealerTetherState& st = gHealerTether[low];
        wasWaiting = (st.waitingSinceMs != 0);
        st.waitingSinceMs = 0;
        st.announced = false;
        st.overrideSetMs = now ? now : 1;
    }
    if (wasWaiting)
        LOG_INFO("playerbots", "[Companion] {} healer tether overridden by owner pull command", bot->GetName());
    return wasWaiting;
}

Unit* PickPullTarget(PlayerbotAI* botAI, bool* far)
{
    if (far)
        *far = false;
    Player* bot = botAI->GetBot();
    if (!bot || !IsGroupLead(bot) || !ReadyToPull(botAI, false))
        return nullptr;

    Unit* chosen = nullptr;
    bool chosenFar = false;

    // Steps 1 and 2 need the group within GroupRange of the tank.
    if (ReadyToPull(botAI, true))
    {
        // 1. Skull, anywhere within SkullRange.
        if (Unit* skull = SkullUnit(botAI, false))
            chosen = skull;

        // 1b. Phase 2: a unit guarding a node a companion wants to gather (CompanionGather), within SkullRange.
        if (!chosen)
            if (Unit* guard = CompanionGather::PriorityPull(botAI))
                if (IsPullable(bot, guard) && bot->GetDistance(guard) <= sPlayerbotAIConfig.companionLeadSkullRange)
                    chosen = guard;

        // 2. Nearest pullable unit in line of sight within PullRange, in front of the tank.
        if (!chosen)
        {
            float pullRange = sPlayerbotAIConfig.companionLeadPullRange;
            Unit* best = nullptr;
            float bestDist = pullRange;
            GuidVector possible = botAI->GetAiObjectContext()->GetValue<GuidVector>("possible targets")->Get();
            for (ObjectGuid guid : possible)
            {
                Unit* unit = botAI->GetUnit(guid);
                if (!IsPullable(bot, unit))
                    continue;
                float dist = bot->GetDistance(unit);
                if (dist > bestDist || !bot->HasInArc(static_cast<float>(M_PI) * 2.0f / 3.0f, unit) ||
                    !bot->IsWithinLOSInMap(unit))
                    continue;
                best = unit;
                bestDist = dist;
            }
            chosen = best;
        }
    }

    // 3. Far pull by path distance. The caller waits for the group and walks to it before pulling.
    if (!chosen)
    {
        chosen = FarPull(botAI);
        chosenFar = (chosen != nullptr);
    }

    if (!chosen)
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gHealerTether.find(bot->GetGUID().GetCounter());
        if (it != gHealerTether.end())
        {
            it->second.waitingSinceMs = 0;
            it->second.announced = false;
        }
        return nullptr;
    }

    // Lead v2 (3a): before initiating a NEW pull, require healer within range and LOS (soft 8s tether).
    if (!CheckHealerTetherForNewPull(botAI, bot->GetGroup()))
        return nullptr;

    if (far)
        *far = chosenFar;
    return chosen;
}

Unit* AssistTarget(PlayerbotAI* botAI, std::string* reason)
{
    if (reason)
        reason->clear();
    Player* bot = botAI->GetBot();
    if (!bot || !LeadActive(bot) || IsGroupLead(bot))
        return nullptr;

    Group* group = bot->GetGroup();
    if (!group)
        return nullptr;

    auto usable = [bot, botAI](Unit* unit) {
        return unit && unit->IsAlive() && unit->IsInWorld() && unit->GetMapId() == bot->GetMapId() &&
               unit->IsCreature() && bot->IsHostileTo(unit) &&
               bot->GetDistance(unit) <= sPlayerbotAIConfig.companionLeadSkullRange &&
               !CompanionClass::IsOffTankUnit(botAI, unit);   // the hunter's pet holds the diamond unit
    };

    // Lead v1 fallback when TargetPersistence switch is disabled.
    if (!sPlayerbotAIConfig.companionLeadTargetPersistence)
    {
        if (Unit* skull = SkullUnit(botAI, true))
        {
            if (reason)
                *reason = "skull";
            return skull;
        }
        if (Player* tank = FindCompanionTank(group, bot))
        {
            Unit* victim = tank->GetVictim();
            if (usable(victim))
            {
                if (reason)
                    *reason = "tank_target";
                return victim;
            }
        }
        Unit* nearest = nullptr;
        float nearestDist = 0.0f;
        for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
        {
            Player* member = itr->GetSource();
            if (!member)
                continue;
            for (Unit* attacker : member->getAttackers())
            {
                if (!usable(attacker))
                    continue;
                float dist = bot->GetDistance(attacker);
                if (!nearest || dist < nearestDist)
                {
                    nearest = attacker;
                    nearestDist = dist;
                }
            }
        }
        if (nearest && reason)
            *reason = "nearest_attacker";
        return nearest;
    }

    // Lead v2 (2a-2d): Target persistence for companion DPS in lead mode.
    uint32 const low = bot->GetGUID().GetCounter();
    uint32 const now = getMSTime();
    Player* tank = FindCompanionTank(group, bot);
    if (tank && tank->GetVictim() && tank->GetVictim()->IsCreature() && tank->IsHostileTo(tank->GetVictim()))
        NoteTankAttack(tank->GetVictim());

    PersistState st;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        st = gPersist[low];
    }

    Unit* cur = !st.lockedGuid.IsEmpty() ? botAI->GetUnit(st.lockedGuid) : nullptr;
    if (!cur && st.lockedGuid.IsEmpty())
    {
        if (Unit* v = bot->GetVictim(); usable(v) && EngagedWithGroup(v, group) && !IsCrowdControlled(v))
        {
            cur = v;
            st.lockedGuid = v->GetGUID();
        }
    }

    // Evaluate whether the current locked target is still valid to keep (alive, engaged, not CC'd, reachable).
    std::string dropReason;
    bool curValid = false;
    if (cur)
    {
        if (!cur->IsAlive() || !cur->IsInWorld() || cur->GetMapId() != bot->GetMapId())
            dropReason = "target_died";
        else if (IsCrowdControlled(cur))
            dropReason = "target_cc";
        else if (!usable(cur) || !EngagedWithGroup(cur, group))
            dropReason = "target_lost";
        else
        {
            float const reachDist =
                PlayerbotAI::IsRanged(bot) ? sPlayerbotAIConfig.spellDistance : (sPlayerbotAIConfig.meleeDistance + 5.0f);
            float const dist = bot->GetDistance(cur);
            bool const inReach = dist <= reachDist && bot->IsWithinLOSInMap(cur);
            if (inReach)
            {
                st.unreachableSinceMs = 0;
                st.minDistSeen = dist;
                curValid = true;
            }
            else
            {
                if (st.unreachableSinceMs == 0 || dist < st.minDistSeen - 2.0f)
                {
                    st.unreachableSinceMs = now;
                    st.minDistSeen = dist;
                }
                uint32 const oorMs = sPlayerbotAIConfig.companionLeadOutOfReachSeconds * IN_MILLISECONDS;
                if (getMSTimeDiff(st.unreachableSinceMs, now) > oorMs)
                    dropReason = "out_of_reach";
                else
                    curValid = true;
            }
        }
    }
    else if (!st.lockedGuid.IsEmpty())
    {
        dropReason = "target_died";
    }
    else
    {
        dropReason = "new_engage";
    }

    // Check skull: only usable when IN COMBAT with group and Brannoc has threat / engaged it (2b & 2c).
    ObjectGuid const skullGuid = group->GetTargetIcon(SKULL_ICON);
    Unit* engagedSkull = nullptr;
    if (!skullGuid.IsEmpty())
    {
        Unit* su = botAI->GetUnit(skullGuid);
        if (usable(su) && !IsCrowdControlled(su) && TankEngagedForDps(tank, su, group, now))
            engagedSkull = su;
    }

    // 2b: A NEW skull on a mob IN COMBAT with the group switches the DPS onto it.
    if (curValid && engagedSkull && engagedSkull->GetGUID() != cur->GetGUID() &&
        engagedSkull->GetGUID() != st.lastSwitchedSkull)
    {
        st.lockedGuid = engagedSkull->GetGUID();
        st.lastSwitchedSkull = engagedSkull->GetGUID();
        st.unreachableSinceMs = 0;
        st.minDistSeen = bot->GetDistance(engagedSkull);
        {
            std::lock_guard<std::mutex> lock(gStateMutex);
            gPersist[low] = st;
        }
        if (reason)
            *reason = "skull_moved";
        return engagedSkull;
    }

    // 2a: Keep current target while alive, engaged with group, not CC'd, and reachable.
    if (curValid)
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        gPersist[low] = st;
        if (reason)
            *reason = "persist";
        return cur;
    }

    // 2d: No valid current target -> (1) engaged skull, (2) Brannoc's engaged victim, (3) null -> upstream choice.
    st.lockedGuid = ObjectGuid();
    st.unreachableSinceMs = 0;
    st.minDistSeen = 0.0f;

    if (engagedSkull)
    {
        st.lockedGuid = engagedSkull->GetGUID();
        st.lastSwitchedSkull = engagedSkull->GetGUID();
        st.minDistSeen = bot->GetDistance(engagedSkull);
        {
            std::lock_guard<std::mutex> lock(gStateMutex);
            gPersist[low] = st;
        }
        if (reason)
            *reason = (dropReason == "new_engage") ? "skull" : (dropReason + ":skull");
        return engagedSkull;
    }

    // Kill Order v1 (point 5): with no current target, take the lowest (highest-priority) engaged mark (X, then Circle).
    if (sPlayerbotAIConfig.companionLeadKillOrder)
    {
        for (uint8 icon : {CROSS_ICON, CIRCLE_ICON})
        {
            ObjectGuid const markGuid = group->GetTargetIcon(icon);
            if (markGuid.IsEmpty())
                continue;
            Unit* mu = botAI->GetUnit(markGuid);
            if (!usable(mu) || IsCrowdControlled(mu) || !TankEngagedForDps(tank, mu, group, now))
                continue;
            st.lockedGuid = mu->GetGUID();
            st.minDistSeen = bot->GetDistance(mu);
            {
                std::lock_guard<std::mutex> lock(gStateMutex);
                gPersist[low] = st;
            }
            std::string const tag = (icon == CROSS_ICON) ? "cross" : "circle";
            if (reason)
                *reason = (dropReason == "new_engage") ? tag : (dropReason + ":" + tag);
            return mu;
        }
    }

    if (tank)
    {
        Unit* victim = tank->GetVictim();
        if (usable(victim) && !IsCrowdControlled(victim) && TankEngagedForDps(tank, victim, group, now))
        {
            st.lockedGuid = victim->GetGUID();
            st.minDistSeen = bot->GetDistance(victim);
            {
                std::lock_guard<std::mutex> lock(gStateMutex);
                gPersist[low] = st;
            }
            if (reason)
                *reason = (dropReason == "new_engage") ? "tank_target" : (dropReason + ":tank_target");
            return victim;
        }
    }

    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        gPersist[low] = st;
    }
    if (reason)
        *reason = GroupHasActiveFight(group) ? (dropReason + ":upstream") : "combat_end";
    return nullptr;
}

Unit* LeadDpsTarget(PlayerbotAI* botAI, bool* handled)
{
    if (handled)
        *handled = false;
    if (!botAI || !sPlayerbotAIConfig.companionLeadTargetPersistence)
        return nullptr;
    Player* bot = botAI->GetBot();
    if (!bot || !LeadActive(bot) || IsGroupLead(bot))
        return nullptr;
    if (handled)
        *handled = true;
    std::string reason;
    Unit* target = AssistTarget(botAI, &reason);
    if (target)
        NoteAttackTarget(botAI, target, reason);
    return target;
}

void LockFallbackTarget(PlayerbotAI* botAI, Unit* target)
{
    if (!botAI || !target || !sPlayerbotAIConfig.companionLeadTargetPersistence)
        return;
    Player* bot = botAI->GetBot();
    if (!bot || !LeadActive(bot) || IsGroupLead(bot))
        return;
    if (CompanionClass::IsOffTankUnit(botAI, target) || IsCrowdControlled(target))
        return;
    uint32 const low = bot->GetGUID().GetCounter();
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        PersistState& st = gPersist[low];
        if (st.lockedGuid != target->GetGUID())
        {
            st.lockedGuid = target->GetGUID();
            st.unreachableSinceMs = 0;
            st.minDistSeen = bot->GetDistance(target);
        }
    }
    NoteAttackTarget(botAI, target, "upstream");
}

bool IconHeldUnengaged(PlayerbotAI* botAI, ObjectGuid guid)
{
    Player* bot = botAI->GetBot();
    if (!bot || guid.IsEmpty() || !LeadActive(bot) || IsGroupLead(bot))
        return false;
    Group* group = bot->GetGroup();
    if (!group)
        return false;
    Unit* unit = botAI->GetUnit(guid);
    if (!unit || !unit->IsAlive())
        return false;
    if (sPlayerbotAIConfig.companionLeadTargetPersistence)
        return !TankEngagedForDps(FindCompanionTank(group, bot), unit, group, getMSTime());
    return !EngagedWithGroup(unit, group);
}

bool SkullHeldUnengaged(PlayerbotAI* botAI)
{
    Player* bot = botAI->GetBot();
    Group* group = bot ? bot->GetGroup() : nullptr;
    if (!group)
        return false;
    if (!IconHeldUnengaged(botAI, group->GetTargetIcon(SKULL_ICON)))
        return false;
    // Lead v2 (2c): if the current pack is still fighting, do NOT block DPS from finishing it.
    if (sPlayerbotAIConfig.companionLeadTargetPersistence && GroupHasActiveFight(group))
        return false;
    return true;
}

Unit* ChainTarget(PlayerbotAI* botAI, std::string* refusal)
{
    if (refusal)
        refusal->clear();
    Player* bot = botAI->GetBot();
    if (!bot || !sPlayerbotAIConfig.companionChainEnabled || !bot->IsInCombat() || !LeadActive(bot) ||
        !IsGroupLead(bot))
        return nullptr;
    Unit* skull = SkullUnit(botAI, false);
    if (!skull)
        return nullptr;
    ChainCache check = CachedChainCheck(botAI);
    if (refusal)
        *refusal = check.reason;
    return skull;
}

void NoteAttackTarget(PlayerbotAI* botAI, Unit* target, std::string const& reason)
{
    Player* bot = botAI->GetBot();
    if (!bot || !LeadActive(bot) || IsGroupLead(bot))
        return;
    uint32 low = bot->GetGUID().GetCounter();
    ObjectGuid guid = target ? target->GetGUID() : ObjectGuid();
    ObjectGuid previous;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gLastAttack.find(low);
        previous = it == gLastAttack.end() ? ObjectGuid() : it->second;
        if (previous == guid)
            return;
        gLastAttack[low] = guid;
    }
    if (!sPlayerbotAIConfig.companionLeadLogTargetChanges)
        return;
    // Old state: follow tank, or attack <name>[<lowguid>] while the previous unit is still known.
    std::string before = "follow tank";
    if (!previous.IsEmpty())
    {
        Unit* old = botAI->GetUnit(previous);
        before = std::string("attack ") + (old ? old->GetName() : "unknown") + "[" +
                 std::to_string(previous.GetCounter()) + "]";
    }
    std::string after = target ? (std::string("attack ") + target->GetName() + "[" +
                                  std::to_string(guid.GetCounter()) + "]")
                               : std::string("follow tank");
    Player* tank = FindCompanionTank(bot->GetGroup(), bot);
    float const tankDist = (tank && tank->GetMapId() == bot->GetMapId()) ? bot->GetDistance(tank) : -1.0f;
    std::string const why = reason.empty() ? (target ? "other" : "combat_end") : reason;
    LOG_INFO("playerbots", "[Companion] {} lead: {} -> {} reason={} tankDist={:.1f}", bot->GetName(), before, after,
             why, tankDist);
}

void ClearKillOrderIfIdle(PlayerbotAI* botAI)
{
    if (!botAI || !sPlayerbotAIConfig.companionLeadKillOrder)
        return;
    Player* bot = botAI->GetBot();
    Group* group = bot ? bot->GetGroup() : nullptr;
    if (!group || GroupHasActiveFight(group))
        return;
    uint32 const gid = group->GetGUID().GetCounter();
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gKillOrders.find(gid);
    if (it != gKillOrders.end() && it->second.active)
    {
        it->second.active = false;
        it->second.initialDone = false;
        it->second.nonDeathMoves = 0;
        it->second.mobs.clear();
        it->second.lastAlive.clear();
        it->second.order.clear();
        it->second.botPlacedIcon.fill(ObjectGuid::Empty);
        it->second.ownerLockedIcon.fill(ObjectGuid::Empty);
    }
}

void UpdateKillOrder(PlayerbotAI* botAI)
{
    if (!botAI || !sPlayerbotAIConfig.companionLeadKillOrder)
        return;
    Player* tank = botAI->GetBot();
    if (!tank || !LeadActive(tank) || !tank->IsAlive() || !IsGroupLead(tank))
        return;
    Group* group = tank->GetGroup();
    if (!group)
        return;

    uint32 const gid = group->GetGUID().GetCounter();
    uint32 const now = getMSTime();

    // Collect all live hostile creatures currently engaged with any group member, excluding Diamond & CC'd mobs.
    ObjectGuid const diamondGuid = group->GetTargetIcon(CompanionClass::DIAMOND_ICON);
    std::vector<Unit*> engaged;
    std::set<ObjectGuid> engagedSet;
    auto consider = [&](Unit* u)
    {
        if (!u || !u->IsAlive() || !u->IsInWorld() || u->GetMapId() != tank->GetMapId() || !u->IsCreature())
            return;
        if (!tank->IsHostileTo(u) || tank->GetDistance(u) > sPlayerbotAIConfig.companionLeadSkullRange)
            return;
        if (u->GetGUID() == diamondGuid || CompanionClass::IsOffTankUnit(botAI, u) || IsCrowdControlled(u))
            return;
        if (engagedSet.insert(u->GetGUID()).second)
            engaged.push_back(u);
    };
    for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
    {
        Player* member = itr->GetSource();
        if (!member || !member->IsInWorld() || member->GetMapId() != tank->GetMapId())
            continue;
        for (Unit* attacker : member->getAttackers())
            consider(attacker);
    }
    if (Unit* tv = tank->GetVictim())
        if (EngagedWithGroup(tv, group))
            consider(tv);

    if (engaged.empty())
    {
        ClearKillOrderIfIdle(botAI);
        return;
    }

    std::array<ObjectGuid, 8> curIcons = {};
    for (uint8 i = 0; i < curIcons.size(); ++i)
        curIcons[i] = group->GetTargetIcon(i);

    KillOrderState st;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        KillOrderState& live = gKillOrders[gid];
        if (!live.active)
        {
            // Keep only pull-time marks placed by the lead that still match what the group currently displays
            // (issue #86); stale records from cancelled pulls or previous packs are dropped.
            std::array<ObjectGuid, 8> carried = {};
            for (uint8 i = 0; i < carried.size(); ++i)
                if (!live.botPlacedIcon[i].IsEmpty() && curIcons[i] == live.botPlacedIcon[i])
                    carried[i] = live.botPlacedIcon[i];
            live = KillOrderState();
            live.botPlacedIcon = carried;
            live.active = true;
            live.packId = gNextPackId++;
            live.pullStartMs = now;
        }
        st = live;
    }

    // Track per-mob first tank hit time and sticky priority events (heal, cast, flee, low HP).
    bool allHaveThreat = true;
    bool newHeal = false;
    bool newCast = false;
    bool newFlee = false;
    bool newLowHp = false;
    bool newMob = false;
    bool mobDied = false;
    uint32 hitOrderSeq = 0;

    for (ObjectGuid const& prev : st.lastAlive)
        if (!engagedSet.count(prev))
            mobDied = true;

    for (Unit* u : engaged)
    {
        ObjectGuid const ug = u->GetGUID();
        if (st.initialDone && !st.lastAlive.count(ug))
            newMob = true;
        MobOrderInfo& mi = st.mobs[ug];
        bool const hasThreat = u->GetThreatMgr().GetThreat(tank) > 0.0f || tank->GetVictim() == u;
        if (hasThreat && mi.firstHitMs == 0)
            mi.firstHitMs = now + (hitOrderSeq++);
        if (!hasThreat)
            allHaveThreat = false;

        if (!mi.sawHeal && IsCastingHeal(u))
        {
            mi.sawHeal = true;
            newHeal = true;
        }
        if (!mi.sawCast && IsCastingAnySpell(u))
        {
            mi.sawCast = true;
            newCast = true;
        }
        if (!mi.sawFlee && u->HasUnitState(UNIT_STATE_FLEEING))
        {
            mi.sawFlee = true;
            newFlee = true;
        }
        if (!mi.sawLowHp && u->GetHealthPct() < 30.0f)
        {
            mi.sawLowHp = true;
            newLowHp = true;
        }
    }

    // Detect owner hand-placed marks (point 6): if a mark holds a live mob we didn't place there, lock it for the pack.
    bool ownerChanged = false;
    for (uint8 icon : ORDER_ICONS)
    {
        ObjectGuid const cur = group->GetTargetIcon(icon);
        if (!st.ownerLockedIcon[icon].IsEmpty())
        {
            Unit* lockedU = botAI->GetUnit(st.ownerLockedIcon[icon]);
            if (cur != st.ownerLockedIcon[icon] || !lockedU || !lockedU->IsAlive())
                st.ownerLockedIcon[icon] = ObjectGuid::Empty;
        }
        if (!cur.IsEmpty() && cur != st.botPlacedIcon[icon])
        {
            Unit* cu = botAI->GetUnit(cur);
            if (cu && cu->IsAlive() && st.ownerLockedIcon[icon] != cur)
            {
                st.ownerLockedIcon[icon] = cur;
                ownerChanged = true;
            }
        }
    }

    // Wait until initial threat on pack (all mobs hit once, or >= 2s after pull).
    if (!st.initialDone)
    {
        if (!allHaveThreat && getMSTimeDiff(st.pullStartMs, now) < 2000)
        {
            std::lock_guard<std::mutex> lock(gStateMutex);
            gKillOrders[gid] = st;
            return;
        }
    }

    std::string reason;
    bool const structuralChange = !st.initialDone || mobDied || newMob || ownerChanged;
    if (!st.initialDone)
        reason = "initial";
    else if (mobDied)
        reason = "mob_died";
    else if (newMob)
        reason = "new_mob";
    else if (ownerChanged)
        reason = "owner_mark";
    else if (newHeal)
        reason = "mob_heal";
    else if (newFlee)
        reason = "mob_flee";
    else if (newCast)
        reason = "mob_cast";
    else if (newLowHp)
        reason = "mob_lowhp";

    if (reason.empty())
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        gKillOrders[gid] = st;
        return;
    }

    // Anti-bounce cap (TEST rule): without a death, new mob, or owner mark, never move marks more than 2 times per pack.
    if (!structuralChange && st.nonDeathMoves >= 2)
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        gKillOrders[gid] = st;
        return;
    }

    // Score each engaged mob (higher = earlier). Ties: mob Brannoc hit first.
    struct ScoredMob
    {
        Unit* unit = nullptr;
        int32 score = 0;
        uint32 firstHitMs = UINT32_MAX;
        std::string tags;
    };
    std::vector<ScoredMob> scored;
    scored.reserve(engaged.size());
    for (Unit* u : engaged)
    {
        MobOrderInfo const& mi = st.mobs[u->GetGUID()];
        ScoredMob sm;
        sm.unit = u;
        sm.firstHitMs = mi.firstHitMs ? mi.firstHitMs : UINT32_MAX;
        auto addTag = [&sm](char const* t)
        {
            if (!sm.tags.empty())
                sm.tags += "+";
            sm.tags += t;
        };
        if (mi.sawHeal)
        {
            sm.score += 50;
            addTag("heal");
        }
        if (mi.sawFlee || u->HasUnitState(UNIT_STATE_FLEEING))
        {
            sm.score += 40;
            addTag("flee");
        }
        if (mi.sawCast)
        {
            sm.score += 30;
            addTag("cast");
        }
        if (u->GetHealthPct() < 30.0f)
        {
            sm.score += 20;
            addTag("lowhp");
        }
        if (Creature* cr = u->ToCreature(); cr && cr->isElite())
        {
            sm.score += 10;
            addTag("elite");
        }
        if (sm.tags.empty())
            sm.tags = "base";
        scored.push_back(std::move(sm));
    }

    std::sort(scored.begin(), scored.end(), [](ScoredMob const& a, ScoredMob const& b)
    {
        if (a.score != b.score)
            return a.score > b.score;
        if (a.firstHitMs != b.firstHitMs)
            return a.firstHitMs < b.firstHitMs;
        return a.unit->GetGUID().GetCounter() < b.unit->GetGUID().GetCounter();
    });

    // Build target icon assignments respecting owner-locked icons.
    std::set<ObjectGuid> assignedMobs;
    std::array<ObjectGuid, 8> wantIcon = {};
    for (uint8 icon : ORDER_ICONS)
    {
        if (!st.ownerLockedIcon[icon].IsEmpty())
        {
            wantIcon[icon] = st.ownerLockedIcon[icon];
            assignedMobs.insert(st.ownerLockedIcon[icon]);
        }
    }
    size_t idx = 0;
    for (uint8 icon : ORDER_ICONS)
    {
        if (!wantIcon[icon].IsEmpty())
            continue;
        while (idx < scored.size() && assignedMobs.count(scored[idx].unit->GetGUID()))
            ++idx;
        if (idx < scored.size())
        {
            wantIcon[icon] = scored[idx].unit->GetGUID();
            assignedMobs.insert(wantIcon[icon]);
            ++idx;
        }
    }

    // Check if any mark actually changes.
    bool marksChanged = false;
    for (uint8 icon : ORDER_ICONS)
        if (group->GetTargetIcon(icon) != wantIcon[icon])
            marksChanged = true;

    if (!st.initialDone || marksChanged)
    {
        if (st.initialDone && !structuralChange && marksChanged)
            ++st.nonDeathMoves;
        for (uint8 icon : ORDER_ICONS)
        {
            if (!st.ownerLockedIcon[icon].IsEmpty())
                continue;
            SetLeadIcon(group, tank, icon, wantIcon[icon], &st);
        }

        std::ostringstream oss;
        bool first = true;
        for (uint8 icon : ORDER_ICONS)
        {
            ObjectGuid const mg = wantIcon[icon];
            if (mg.IsEmpty())
                continue;
            if (!first)
                oss << ", ";
            first = false;
            Unit* mu = botAI->GetUnit(mg);
            int32 sc = 0;
            std::string tags = st.ownerLockedIcon[icon] == mg ? "owner" : "base";
            for (ScoredMob const& sm : scored)
                if (sm.unit->GetGUID() == mg)
                {
                    sc = sm.score;
                    if (st.ownerLockedIcon[icon] != mg)
                        tags = sm.tags;
                    break;
                }
            oss << OrderIconName(icon) << ":" << (mu ? mu->GetName() : "unknown") << "[" << mg.GetCounter() << "]="
                << sc << "(" << tags << ")";
        }
        LOG_INFO("playerbots", "[Companion] {} killorder pack={} reason={} moves={} engaged={} order=[{}]",
                 tank->GetName(), st.packId, reason, st.nonDeathMoves, engaged.size(), oss.str());
    }

    st.initialDone = true;
    st.lastAlive = engagedSet;
    st.order.clear();
    for (ScoredMob const& sm : scored)
        st.order.push_back(sm.unit->GetGUID());

    std::lock_guard<std::mutex> lock(gStateMutex);
    gKillOrders[gid] = st;
}

bool SetLeadFromOwner(PlayerbotAI* botAI, Player* from, bool on)
{
    Player* bot = botAI->GetBot();
    CompanionBotMgr& mgr = sCompanionBotMgr;
    if (!bot || !from || !mgr.IsOwner(from) || !mgr.IsCompanion(bot))
        return false;

    mgr.SetLeadMode(bot->GetGUID().GetCounter(), on);
    mgr.ApplyLeadStrategies(botAI);
    // Grouped: the tank answers in party chat (one line for the party). Not grouped: the owner whispered this
    // companion, so it answers by whisper.
    std::string const reply = on ? "Lead mode on." : "Lead mode off.";
    if (mgr.IsGroupedWithOwner(bot))
    {
        // Deterministic single party reply independent of chat-event delivery order or lead-on/off state:
        // only the lowest-GUID in-world companion tank in the group announces the toggle.
        Group* group = bot->GetGroup();
        Player* speaker = nullptr;
        uint32 bestLow = UINT32_MAX;
        if (group)
        {
            for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
            {
                Player* member = itr->GetSource();
                if (!member || !member->IsInWorld() || !mgr.IsCompanion(member) || !mgr.IsGroupedWithOwner(member) ||
                    !PlayerbotAI::IsTank(member))
                    continue;
                uint32 const low = member->GetGUID().GetCounter();
                if (!speaker || low < bestLow)
                {
                    speaker = member;
                    bestLow = low;
                }
            }
        }
        if (bot == speaker)
            botAI->SayToParty(reply);
    }
    else
        CompanionDuty::ReplyToOwner(botAI, {reply});
    return true;
}
}  // namespace CompanionLead

// --- Triggers and actions -------------------------------------------------------------------------------------------

namespace
{
class CompanionLeadReadyTrigger : public Trigger
{
public:
    CompanionLeadReadyTrigger(PlayerbotAI* botAI) : Trigger(botAI, "companion lead ready") {}
    bool IsActive() override
    {
        CompanionLead::ClearKillOrderIfIdle(botAI);
        return CompanionLead::PickPullTarget(botAI) != nullptr;
    }
};

class CompanionAssistReadyTrigger : public Trigger
{
public:
    CompanionAssistReadyTrigger(PlayerbotAI* botAI) : Trigger(botAI, "companion assist ready") {}
    bool IsActive() override
    {
        std::string reason;
        Unit* target = CompanionLead::AssistTarget(botAI, &reason);
        // Only clear last-attack log to "follow tank" when the group fight actually ended (not on upstream fallback).
        if (target || !CompanionLead::GroupHasActiveFight(botAI->GetBot()->GetGroup()))
            CompanionLead::NoteAttackTarget(botAI, target, reason);
        return target != nullptr;
    }
};

class CompanionChainReadyTrigger : public Trigger
{
public:
    CompanionChainReadyTrigger(PlayerbotAI* botAI) : Trigger(botAI, "companion chain ready") {}
    bool IsActive() override
    {
        CompanionLead::UpdateKillOrder(botAI);
        std::string refusal;
        Unit* skull = CompanionLead::ChainTarget(botAI, &refusal);
        return (skull && refusal.empty()) || ChainReturnUnit(botAI) != nullptr;
    }
};

// Pull: walk to the target, open with a Charge or a throw inside range, otherwise melee. Marks the skull when no
// skull is set. A far target (path distance beyond PullRange) is walked to first: the tank waits while a group
// member is more than GroupRange behind, and marks nothing until the target is inside PullRange.
class CompanionLeadPullAction : public AttackAction
{
public:
    CompanionLeadPullAction(PlayerbotAI* botAI) : AttackAction(botAI, "companion lead pull") {}

    bool Execute(Event /*event*/) override
    {
        Player* bot = botAI->GetBot();
        bool far = false;
        Unit* target = CompanionLead::PickPullTarget(botAI, &far);
        if (!target)
            return false;

        float pullRange = sPlayerbotAIConfig.companionLeadPullRange;
        if (far)
        {
            if (GroupMemberBehind(bot))
            {
                LogWaitingOnce(bot);
                return true;   // hold position while the group catches up
            }
            if (bot->GetDistance(target) > pullRange)
                return ReachCombatTo(target, pullRange - 5.0f);
        }

        if (Group* group = bot->GetGroup())
        {
            if (group->GetTargetIcon(CompanionLead::SKULL_ICON).IsEmpty())
                SetLeadIcon(group, bot, CompanionLead::SKULL_ICON, target->GetGUID());
        }
        context->GetValue<Unit*>("current target")->Set(target);
        NoteTankAttack(target);

        float dist = bot->GetDistance(target);
        if (dist > 25.0f)
            return ReachCombatTo(target, 20.0f);   // inside Charge range next tick
        // Charge needs Battle Stance. The protection strategy keeps the tank in Defensive Stance (aura 71), so switch
        // first; the next tick opens with the Charge.
        if (dist >= 8.0f && bot->HasAura(71) && botAI->CanCastSpell("battle stance", bot))
            return botAI->CastSpell("battle stance", bot);
        if (dist >= 8.0f && botAI->CanCastSpell("charge", target))
            return botAI->CastSpell("charge", target);
        return Attack(target);
    }
    bool isUseful() override { return CompanionLead::PickPullTarget(botAI) != nullptr; }

private:
    void LogWaitingOnce(Player* bot)
    {
        uint32 low = bot->GetGUID().GetCounter();
        uint32 now = static_cast<uint32>(GameTime::GetUptime().count());
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gWaitLog.find(low);
        if (it != gWaitLog.end() && now - it->second < 10)
            return;
        gWaitLog[low] = now;
        LOG_INFO("playerbots", "[Companion] {} waiting for group", bot->GetName());
    }
};

// Damage dealer: attacks the persisted lead target (or skull / tank's engaged victim).
class CompanionAssistAction : public AttackAction
{
public:
    CompanionAssistAction(PlayerbotAI* botAI) : AttackAction(botAI, "companion assist") {}

    bool Execute(Event /*event*/) override
    {
        std::string reason;
        Unit* target = CompanionLead::AssistTarget(botAI, &reason);
        if (!target)
            return false;
        CompanionLead::NoteAttackTarget(botAI, target, reason);
        context->GetValue<Unit*>("current target")->Set(target);
        return Attack(target);
    }
    bool isUseful() override
    {
        Unit* target = CompanionLead::AssistTarget(botAI);
        return target && botAI->GetBot()->GetVictim() != target;
    }
};

// Tank, in combat: pulls the skull target while the chain check passes, and walks back to the owner while the pulled
// unit fights the tank. A refused chain pull says "Not yet." once per skull target.
class CompanionChainPullAction : public AttackAction
{
public:
    CompanionChainPullAction(PlayerbotAI* botAI) : AttackAction(botAI, "companion chain pull") {}

    bool Execute(Event /*event*/) override
    {
        Player* bot = botAI->GetBot();
        std::string refusal;
        Unit* skull = CompanionLead::ChainTarget(botAI, &refusal);
        if (skull && !refusal.empty())
        {
            RefuseOnce(botAI, skull, refusal);
            return false;
        }
        if (!skull)
        {
            Unit* returning = ChainReturnUnit(botAI);
            Player* owner = sCompanionBotMgr.FindOwnerOnlineFor(bot);
            return returning && owner && MoveTo(owner, 8.0f);
        }

        uint32 low = bot->GetGUID().GetCounter();
        ChainCache check = CachedChainCheck(botAI);
        {
            std::lock_guard<std::mutex> lock(gStateMutex);
            auto it = gChainLogged.find(low);
            if (it == gChainLogged.end() || it->second != skull->GetGUID())
            {
                gChainLogged[low] = skull->GetGUID();
                LOG_INFO("playerbots", "[Companion] {} chain pull {} (engaged {}, owner mana {}%)", bot->GetName(),
                         skull->GetName(), check.engaged, check.manaPct);
            }
            gChainPulled[low] = skull->GetGUID();
        }

        float dist = bot->GetDistance(skull);
        if (dist > 25.0f)
            return ReachCombatTo(skull, 20.0f);
        context->GetValue<Unit*>("current target")->Set(skull);
        NoteTankAttack(skull);
        return Attack(skull);
    }

    bool isUseful() override
    {
        std::string refusal;
        Unit* skull = CompanionLead::ChainTarget(botAI, &refusal);
        if (skull)
            return refusal.empty();
        return ChainReturnUnit(botAI) != nullptr;
    }
};

class CompanionLeadOnAction : public Action
{
public:
    CompanionLeadOnAction(PlayerbotAI* botAI) : Action(botAI, "companion lead on") {}
    bool Execute(Event event) override
    {
        Player* from = event.getOwner() ? event.getOwner() : botAI->GetMaster();
        return CompanionLead::SetLeadFromOwner(botAI, from, true);
    }
};

class CompanionLeadOffAction : public Action
{
public:
    CompanionLeadOffAction(PlayerbotAI* botAI) : Action(botAI, "companion lead off") {}
    bool Execute(Event event) override
    {
        Player* from = event.getOwner() ? event.getOwner() : botAI->GetMaster();
        return CompanionLead::SetLeadFromOwner(botAI, from, false);
    }
};

// Non-lead tank in lead mode (issue #83): suppresses upstream tank-assist targeting and taunt wars against the
// group's elected lead tank. Still permits single-target peels on mobs actively hitting a non-tank party member
// (or explicit off-tank Diamond targets), and unlocks fully if the lead tank dies mid-fight.
class CompanionOffTankAssistMultiplier : public Multiplier
{
public:
    CompanionOffTankAssistMultiplier(PlayerbotAI* botAI) : Multiplier(botAI, "companion off-tank assist") {}

    float GetValue(Action* action) override
    {
        if (!action || !botAI)
            return 1.0f;
        Player* bot = botAI->GetBot();
        if (!bot || !LeadActive(bot) || !PlayerbotAI::IsTank(bot))
            return 1.0f;
        // Raid encounters rely on encounter-specific tank swaps via taunt; only suppress dungeon taunt-wars.
        if (bot->GetMap() && bot->GetMap()->IsRaid())
            return 1.0f;

        std::string const& name = action->getName();
        bool const isTankAssist = (name == "tank assist");
        bool const isAoeTaunt = (name == "challenging shout" || name == "challenging roar");
        bool const isTaunt = isAoeTaunt || EncounterHelpers::IsTauntAction(bot, action) ||
                             name == "taunt on snare target" || name == "heroic throw taunt" ||
                             name == "mocking blow";
        if (!isTankAssist && !isTaunt)
            return 1.0f;

        Group* group = bot->GetGroup();
        Player* lead = CompanionLead::GroupLead(group, bot);
        if (!lead || lead == bot || !lead->IsAlive())
            return 1.0f;   // we are the lead, or lead tank is down: unlock tank assist & taunts immediately

        if (isTankAssist)
            return 0.0f;

        if (isAoeTaunt)
            return 0.0f;   // never AoE-taunt the pack off a live lead tank

        Unit* target = action->GetTarget();
        if (!target)
            target = botAI->GetAiObjectContext()->GetValue<Unit*>("current target")->Get();
        if (!target)
            return 0.0f;

        // Friendly-targeted taunts (e.g. Righteous Defense): never peel off the lead tank.
        if (bot->IsFriendlyTo(target))
            return (target == lead || target == bot) ? 0.0f : 1.0f;

        // Already targeting us: no need to waste a taunt.
        if (target->GetVictim() == bot)
            return 0.0f;

        // Explicit off-tank mark (Diamond) may always be taunted off the lead after a pull.
        if (CompanionClass::IsOffTankUnit(botAI, target))
            return 1.0f;

        // Hostile-targeted taunts: never taunt the lead's current victim or a mob already targeting the lead.
        if (target == lead->GetVictim() || target->GetVictim() == lead)
            return 0.0f;

        // Allow peeling a loose mob actively hitting a non-tank party member.
        if (Unit* mobVictim = target->GetVictim();
            mobVictim && mobVictim->IsPlayer() && mobVictim != lead && mobVictim != bot && group &&
            group->IsMember(mobVictim->GetGUID()))
            return 1.0f;

        return 0.0f;
    }
};
}  // namespace

// Exposed for the context registries (StrategyContext.h, ActionContext.h, TriggerContext.h).
Trigger* CreateCompanionLeadReadyTrigger(PlayerbotAI* botAI) { return new CompanionLeadReadyTrigger(botAI); }
Trigger* CreateCompanionAssistReadyTrigger(PlayerbotAI* botAI) { return new CompanionAssistReadyTrigger(botAI); }
Trigger* CreateCompanionChainReadyTrigger(PlayerbotAI* botAI) { return new CompanionChainReadyTrigger(botAI); }
Action* CreateCompanionLeadPullAction(PlayerbotAI* botAI) { return new CompanionLeadPullAction(botAI); }
Action* CreateCompanionAssistAction(PlayerbotAI* botAI) { return new CompanionAssistAction(botAI); }
Action* CreateCompanionChainPullAction(PlayerbotAI* botAI) { return new CompanionChainPullAction(botAI); }
Action* CreateCompanionLeadOnAction(PlayerbotAI* botAI) { return new CompanionLeadOnAction(botAI); }
Action* CreateCompanionLeadOffAction(PlayerbotAI* botAI) { return new CompanionLeadOffAction(botAI); }

void CompanionLeadStrategy::InitTriggers(std::vector<TriggerNode*>& triggers)
{
    triggers.push_back(new TriggerNode("companion lead ready", { NextAction("companion lead pull", 55.0f) }));
}

void CompanionAssistStrategy::InitTriggers(std::vector<TriggerNode*>& triggers)
{
    triggers.push_back(new TriggerNode("companion assist ready", { NextAction("companion assist", 55.0f) }));
}

void CompanionAssistStrategy::InitMultipliers(std::vector<Multiplier*>& multipliers)
{
    multipliers.push_back(new CompanionOffTankAssistMultiplier(botAI));
}

void CompanionChainStrategy::InitTriggers(std::vector<TriggerNode*>& triggers)
{
    // Above the tank's normal combat actions (ACTION_HIGH), below taunt and other interrupts (ACTION_INTERRUPT).
    triggers.push_back(
        new TriggerNode("companion chain ready", { NextAction("companion chain pull", ACTION_HIGH + 5.0f) }));
}
