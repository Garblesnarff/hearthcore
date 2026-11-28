/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "CompanionClassStrategy.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <initializer_list>
#include <mutex>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

#include "Action.h"
#include "AiObjectContext.h"
#include "CharmInfo.h"
#include "CompanionBotMgr.h"
#include "CompanionLeadStrategy.h"
#include "Config.h"
#include "Creature.h"
#include "CreatureAI.h"
#include "DBCStores.h"
#include "Group.h"
#include "Log.h"
#include "Map.h"
#include "MovementActions.h"
#include "ObjectMgr.h"
#include "Pet.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "Playerbots.h"
#include "SpellAuras.h"
#include "SpellMgr.h"
#include "Timer.h"
#include "Trigger.h"
#include "UseItemAction.h"

namespace
{
constexpr uint32 CONFIG_REFRESH_MS = 10000;      // config values are re-read at most this often
constexpr float HEAL_RANGE = 35.0f;              // Flash of Light and Gift of the Naaru reach
constexpr float BLESSING_RANGE = 28.0f;          // small blessings reach 30 yards
constexpr float OPENER_RANGE = 30.0f;            // the rogue opens on a tank target inside this range
constexpr uint32 OPENER_GIVE_UP_MS = 8000;       // the opener hands over to the stock rotation after this long
constexpr uint32 OIL_RETRY_MS = 20000;           // an oil is applied at most this often
constexpr uint32 FOOD_RETRY_MS = 120000;         // food is eaten at most this often (Well Fed lasts far longer)
constexpr uint32 MIN_MANA_PCT_FOR_BUFFS = 15;    // blessings need this much mana
constexpr float BURST_RADIUS = 12.0f;            // units this close to the rogue count for Blade Flurry
constexpr uint32 BURST_MIN_UNITS = 2;
constexpr float DEFAULT_HEAL_BELOW_PCT = 40.0f;
constexpr float DEFAULT_BACKUP_HEAL_BELOW_PCT = 65.0f;
constexpr float DEFAULT_GIFT_BELOW_PCT = 70.0f;
constexpr uint32 DEFAULT_OWNER_MIN_MANA_PCT = 10;
constexpr uint32 DEFAULT_BLESSING_RECAST_MS = 60000;
constexpr uint32 DEFAULT_READY_WAIT_SECONDS = 20;
constexpr float DEFAULT_MEND_PET_BELOW_PCT = 50.0f;
constexpr float MELEE_BEHIND_OFFSET = 0.5f;
// Stock "companion assist" runs at 55.0f; the rogue opener must beat it.
constexpr float OPENER_RELEVANCE = 58.0f;

// Defaults of the per-companion lists (the same text is in conf/playerbots.conf.dist).
constexpr char const* DEFAULT_ALDRIC_BLESSINGS = "Brannoc:kings,Kelda:kings,Kelda.pet:kings,Wren:kings,Aldric:wisdom,Wonky:wisdom";
constexpr char const* DEFAULT_ALDRIC_CONSUMABLES = "Minor Mana Oil:1,Smoked Sagefish:5";

// Items with a switch AiPlayerbot.Companion.Class.<Item>.Enabled are read through ItemEnabled().

struct CachedOption
{
    uint32 atMs = 0;
    std::string value;
};
std::mutex gOptionMutex;
std::unordered_map<std::string, CachedOption> gOptions;

std::string Lower(std::string text)
{
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) { return std::tolower(c); });
    return text;
}

std::string Trim(std::string const& text)
{
    size_t first = text.find_first_not_of(" \t\r\n");
    if (first == std::string::npos)
        return "";
    size_t last = text.find_last_not_of(" \t\r\n");
    return text.substr(first, last - first + 1);
}

std::vector<std::string> SplitList(std::string const& text, char separator)
{
    std::vector<std::string> parts;
    size_t at = 0;
    while (at <= text.size())
    {
        size_t next = text.find(separator, at);
        std::string part = Trim(text.substr(at, next == std::string::npos ? std::string::npos : next - at));
        if (!part.empty())
            parts.push_back(part);
        if (next == std::string::npos)
            break;
        at = next + 1;
    }
    return parts;
}

// Config values cached for CONFIG_REFRESH_MS: triggers call these every tick.
std::string RawOption(std::string const& key, std::string const& fallback)
{
    uint32 now = getMSTime();
    std::lock_guard<std::mutex> lock(gOptionMutex);
    auto it = gOptions.find(key);
    if (it != gOptions.end() && getMSTimeDiff(it->second.atMs, now) < CONFIG_REFRESH_MS)
        return it->second.value;
    CachedOption entry;
    entry.atMs = now;
    entry.value = sConfigMgr->GetOption<std::string>(key, fallback);
    gOptions[key] = entry;
    return entry.value;
}

bool BoolOption(std::string const& key, bool fallback)
{
    std::string value = Lower(Trim(RawOption(key, fallback ? "1" : "0")));
    return !(value.empty() || value == "0" || value == "false" || value == "no" || value == "off");
}

float FloatOption(std::string const& key, float fallback)
{
    std::string value = RawOption(key, std::to_string(fallback));
    try
    {
        return std::stof(value);
    }
    catch (...)
    {
        return fallback;
    }
}

bool SpellNameIs(SpellInfo const* info, char const* name)
{
    return info && info->SpellName[0] && Lower(info->SpellName[0]) == Lower(name);
}

bool IsCompanionBot(Player* bot) { return bot && sCompanionBotMgr.IsCompanion(bot); }

bool InInstance(Player* bot)
{
    Map* map = bot ? bot->GetMap() : nullptr;
    return map && (map->IsDungeon() || map->IsRaid());
}

// The companion's own name decides which per-companion default list applies (Aldric only).
std::string PerCompanionOption(Player* bot, char const* key, char const* aldricDefault)
{
    std::string name = bot->GetName();
    char const* fallback = Lower(name) == "aldric" ? aldricDefault : "";
    return RawOption("AiPlayerbot.Companion." + name + "." + key, fallback);
}

// --- Revenge glyph ---------------------------------------------------------------------------------------------

// The buff of the equipped Glyph of Revenge: the spell its proc effect triggers. Proof (DBC): GlyphProperties 501 is
// spell 58364 "Glyph of Revenge", aura 42 (proc trigger spell) with TriggerSpell 58363, whose aura 108
// (SPELLMOD_COST) is -101%: the next Heroic Strike costs no rage. Read at run time from the equipped glyph.
uint32 RevengeGlyphBuff(Player* bot)
{
    for (uint8 slot = 0; slot < MAX_GLYPH_SLOT_INDEX; ++slot)
    {
        uint32 glyphId = bot->GetGlyph(slot);
        if (!glyphId)
            continue;
        GlyphPropertiesEntry const* glyph = sGlyphPropertiesStore.LookupEntry(glyphId);
        if (!glyph)
            continue;
        SpellInfo const* info = sSpellMgr->GetSpellInfo(glyph->SpellId);
        if (!SpellNameIs(info, "Glyph of Revenge"))
            continue;
        for (SpellEffectInfo const& effect : info->Effects)
            if (effect.TriggerSpell)
                return effect.TriggerSpell;
    }
    return 0;
}

// --- Paladin heals, Gift of the Naaru, blessings ---------------------------------------------------------------

// The owner (the healer) is online, alive, grouped with the bot on the same map and has mana.
bool HealerCovers(Player* bot)
{
    Player* owner = sCompanionBotMgr.FindOwnerOnlineFor(bot);
    if (!owner || !owner->IsAlive() || owner->GetMapId() != bot->GetMapId() || !bot->GetGroup() ||
        owner->GetGroup() != bot->GetGroup())
        return false;
    uint32 maxMana = owner->GetMaxPower(POWER_MANA);
    if (!maxMana)
        return false;
    uint32 minPct = static_cast<uint32>(FloatOption("AiPlayerbot.Companion.Class.PaladinHeal.OwnerMinManaPct",
                                                    static_cast<float>(DEFAULT_OWNER_MIN_MANA_PCT)));
    return owner->GetPower(POWER_MANA) * 100 / maxMana >= minPct;
}

// The lowest-health group member under the heal threshold: 40% while the healer covers, 65% when it does not.
Player* HealTarget(Player* bot)
{
    float below = HealerCovers(bot)
                      ? FloatOption("AiPlayerbot.Companion.Class.PaladinHeal.BelowPct", DEFAULT_HEAL_BELOW_PCT)
                      : FloatOption("AiPlayerbot.Companion.Class.PaladinHeal.BackupBelowPct",
                                    DEFAULT_BACKUP_HEAL_BELOW_PCT);
    Player* best = nullptr;
    float bestPct = 101.0f;
    auto consider = [&](Player* member) {
        if (!member || !member->IsAlive() || member->GetMapId() != bot->GetMapId())
            return;
        if (member != bot && (bot->GetDistance(member) > HEAL_RANGE || !bot->IsWithinLOSInMap(member)))
            return;
        float pct = member->GetHealthPct();
        if (pct < below && pct < bestPct)
        {
            best = member;
            bestPct = pct;
        }
    };
    Group* group = bot->GetGroup();
    if (!group)
        consider(bot);
    else
        for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
            consider(itr->GetSource());
    return best;
}

// A group member under GiftBelowPct that is a tank (Brannoc), without our Gift already on it.
Player* GiftTarget(PlayerbotAI* botAI, Player* bot)
{
    Group* group = bot->GetGroup();
    if (!group || !botAI->HasSpell("gift of the naaru"))
        return nullptr;
    float below = FloatOption("AiPlayerbot.Companion.Class.GiftOfTheNaaru.BelowPct", DEFAULT_GIFT_BELOW_PCT);
    for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
    {
        Player* member = itr->GetSource();
        if (!member || member == bot || !member->IsAlive() || member->GetMapId() != bot->GetMapId() ||
            !PlayerbotAI::IsTank(member))
            continue;
        if (member->GetHealthPct() >= below || bot->GetDistance(member) > HEAL_RANGE ||
            !bot->IsWithinLOSInMap(member))
            continue;
        if (botAI->GetAura("gift of the naaru", member, true))
            continue;
        return member;
    }
    return nullptr;
}

// Blessing target tokens: a player name, "<name>.pet", "self", "owner" or "tank".
Unit* ResolveBlessingTarget(Player* bot, std::string const& token)
{
    if (token == "self" || token == Lower(bot->GetName()))
        return bot;
    if (token == "owner")
        return sCompanionBotMgr.FindOwnerOnlineFor(bot);

    bool wantPet = false;
    std::string name = token;
    if (name.size() > 4 && name.compare(name.size() - 4, 4, ".pet") == 0)
    {
        wantPet = true;
        name = name.substr(0, name.size() - 4);
    }

    Group* group = bot->GetGroup();
    if (!group)
        return nullptr;
    for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
    {
        Player* member = itr->GetSource();
        if (!member)
            continue;
        bool match = name == "tank" ? (member != bot && PlayerbotAI::IsTank(member)) : Lower(member->GetName()) == name;
        if (!match)
            continue;
        return wantPet ? static_cast<Unit*>(member->GetPet()) : static_cast<Unit*>(member);
    }
    return nullptr;
}

struct BlessingWork
{
    Unit* target = nullptr;
    std::string spell;
};

// First entry of the list whose target lacks the blessing (ours) or has under BlessingRecastMs left. One blessing
// per target: the first list entry for a target wins.
BlessingWork FindBlessingWork(PlayerbotAI* botAI, Player* bot)
{
    BlessingWork work;
    std::string list = PerCompanionOption(bot, "Blessings", DEFAULT_ALDRIC_BLESSINGS);
    if (list.empty())
        return work;
    uint32 maxMana = bot->GetMaxPower(POWER_MANA);
    if (maxMana && bot->GetPower(POWER_MANA) * 100 / maxMana < MIN_MANA_PCT_FOR_BUFFS)
        return work;

    int32 recastMs = static_cast<int32>(FloatOption("AiPlayerbot.Companion.Class.Blessings.RecastBelowMs",
                                                    static_cast<float>(DEFAULT_BLESSING_RECAST_MS)));
    std::set<Unit*> seen;
    for (std::string const& entry : SplitList(list, ','))
    {
        size_t colon = entry.find(':');
        if (colon == std::string::npos)
            continue;
        std::string targetToken = Lower(Trim(entry.substr(0, colon)));
        std::string blessing = Lower(Trim(entry.substr(colon + 1)));
        if (targetToken.empty() || blessing.empty())
            continue;
        Unit* unit = ResolveBlessingTarget(bot, targetToken);
        if (!unit || !unit->IsAlive() || !seen.insert(unit).second)
            continue;
        std::string spell = "blessing of " + blessing;
        if (!botAI->HasSpell(spell))
            continue;   // only blessings he knows
        if (unit->GetMapId() != bot->GetMapId() || bot->GetDistance(unit) > BLESSING_RANGE ||
            !bot->IsWithinLOSInMap(unit))
            continue;
        Aura* aura = botAI->GetAura(spell, unit, true);
        if (aura && (aura->GetDuration() < 0 || aura->GetDuration() > recastMs))
            continue;
        work.target = unit;
        work.spell = spell;
        return work;
    }
    return work;
}

// --- Consumables -----------------------------------------------------------------------------------------------

struct ConsumableEntry
{
    uint32 itemId = 0;
    uint32 count = 0;
};

// Consumable item ids by lower-case name, built once from the item templates.
uint32 ConsumableIdByName(std::string const& lowerName)
{
    static std::once_flag built;
    static std::unordered_map<std::string, uint32> byName;
    std::call_once(built, [] {
        ItemTemplateContainer const* store = sObjectMgr->GetItemTemplateStore();
        if (!store)
            return;
        for (auto const& pair : *store)
            if (pair.second.Class == ITEM_CLASS_CONSUMABLE)
                byName[Lower(pair.second.Name1)] = pair.first;
    });
    auto it = byName.find(lowerName);
    return it == byName.end() ? 0 : it->second;
}

// "Minor Mana Oil:1,Smoked Sagefish:5" -> item ids and wanted counts. Unknown names are skipped.
std::vector<ConsumableEntry> ParseConsumables(Player* bot)
{
    std::vector<ConsumableEntry> entries;
    std::string list = PerCompanionOption(bot, "Consumables", DEFAULT_ALDRIC_CONSUMABLES);
    for (std::string const& part : SplitList(list, ','))
    {
        std::string name = part;
        uint32 count = 1;
        size_t colon = part.rfind(':');
        if (colon != std::string::npos)
        {
            name = Trim(part.substr(0, colon));
            try
            {
                count = static_cast<uint32>(std::stoul(Trim(part.substr(colon + 1))));
            }
            catch (...)
            {
                count = 1;
            }
        }
        uint32 id = ConsumableIdByName(Lower(name));
        if (id)
            entries.push_back({id, count});
    }
    return entries;
}

bool IsFoodTemplate(ItemTemplate const* proto)
{
    return proto && proto->Class == ITEM_CLASS_CONSUMABLE && proto->SubClass == ITEM_SUBCLASS_FOOD;
}

// A weapon enchant item (oil, stone): one of its spells has the temporary item enchant effect.
bool IsWeaponEnchantTemplate(ItemTemplate const* proto)
{
    if (!proto || proto->Class != ITEM_CLASS_CONSUMABLE)
        return false;
    for (uint8 i = 0; i < MAX_ITEM_PROTO_SPELLS; ++i)
    {
        SpellInfo const* info = proto->Spells[i].SpellId ? sSpellMgr->GetSpellInfo(proto->Spells[i].SpellId) : nullptr;
        if (!info)
            continue;
        for (SpellEffectInfo const& effect : info->Effects)
            if (effect.Effect == SPELL_EFFECT_ENCHANT_ITEM_TEMPORARY)
                return true;
    }
    return false;
}

bool IsEating(Player* bot)
{
    return bot->getStandState() == UNIT_STAND_STATE_SIT && bot->HasAuraType(SPELL_AURA_MOD_REGEN);
}

struct UseTimes
{
    uint32 oilMs = 0;
    uint32 foodMs = 0;
};
std::mutex gUseMutex;
std::unordered_map<uint32, UseTimes> gUseTimes;   // low guid -> last oil and food use

bool RecentlyUsed(Player* bot, bool food, uint32 retryMs)
{
    std::lock_guard<std::mutex> lock(gUseMutex);
    auto it = gUseTimes.find(bot->GetGUID().GetCounter());
    if (it == gUseTimes.end())
        return false;
    uint32 last = food ? it->second.foodMs : it->second.oilMs;
    return last && getMSTimeDiff(last, getMSTime()) < retryMs;
}

void NoteUsed(Player* bot, bool food)
{
    std::lock_guard<std::mutex> lock(gUseMutex);
    UseTimes& times = gUseTimes[bot->GetGUID().GetCounter()];
    (food ? times.foodMs : times.oilMs) = getMSTime();
}

// Food to eat now: listed, in the bags, no Well Fed (or Brain Food) buff, not eating, not eaten recently.
Item* FoodToEat(PlayerbotAI* botAI, Player* bot)
{
    if (bot->IsInCombat() || IsEating(bot) || RecentlyUsed(bot, true, FOOD_RETRY_MS) ||
        botAI->HasAura("well fed", bot) || botAI->HasAura("brain food", bot))
        return nullptr;
    for (ConsumableEntry const& entry : ParseConsumables(bot))
    {
        ItemTemplate const* proto = sObjectMgr->GetItemTemplate(entry.itemId);
        if (!IsFoodTemplate(proto) || proto->RequiredLevel > bot->GetLevel())
            continue;
        if (Item* item = bot->GetItemByEntry(entry.itemId))
            return item;
    }
    return nullptr;
}

// Oil to apply now: the listed weapon enchant in the bags with the highest required level the bot can use, only
// when the main hand has no temporary enchant.
Item* OilToApply(Player* bot, Item** weaponOut)
{
    if (bot->IsInCombat() || RecentlyUsed(bot, false, OIL_RETRY_MS))
        return nullptr;
    Item* weapon = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_MAINHAND);
    if (!weapon || weapon->GetEnchantmentId(TEMP_ENCHANTMENT_SLOT) != 0)
        return nullptr;
    Item* best = nullptr;
    uint32 bestLevel = 0;
    for (ConsumableEntry const& entry : ParseConsumables(bot))
    {
        ItemTemplate const* proto = sObjectMgr->GetItemTemplate(entry.itemId);
        if (!IsWeaponEnchantTemplate(proto) || proto->RequiredLevel > bot->GetLevel())
            continue;
        Item* item = bot->GetItemByEntry(entry.itemId);
        if (item && (!best || proto->RequiredLevel >= bestLevel))
        {
            best = item;
            bestLevel = proto->RequiredLevel;
        }
    }
    if (best && weaponOut)
        *weaponOut = weapon;
    return best;
}

// --- Pet and diamond -------------------------------------------------------------------------------------------

std::mutex gPetMutex;
std::set<uint32> gDiamondActive;   // low guids of hunters whose pet is currently sent at the diamond unit

bool PetHasAutocast(Pet* pet, uint32 spellId)
{
    return std::find(pet->m_autospells.begin(), pet->m_autospells.end(), spellId) != pet->m_autospells.end();
}

// Sends the pet at the unit, like PetAttackAction does.
void PetAttackUnit(Pet* pet, Unit* target)
{
    CharmInfo* charmInfo = pet->GetCharmInfo();
    if (!charmInfo || !pet->ToCreature() || !pet->ToCreature()->AI())
        return;
    pet->ClearUnitState(UNIT_STATE_FOLLOW);
    pet->AttackStop();
    pet->SetTarget(target->GetGUID());
    charmInfo->SetIsCommandAttack(true);
    charmInfo->SetIsAtStay(false);
    charmInfo->SetIsFollowing(false);
    charmInfo->SetIsCommandFollow(false);
    charmInfo->SetIsReturning(false);
    pet->ToCreature()->AI()->AttackStart(target);
}

// The pet gives up the diamond unit: back on the hunter's own target, or to her side.
void PetReturn(Pet* pet, Player* bot)
{
    Unit* mine = bot->GetVictim();
    if (mine && mine->IsAlive())
    {
        PetAttackUnit(pet, mine);
        return;
    }
    CharmInfo* charmInfo = pet->GetCharmInfo();
    pet->AttackStop();
    if (charmInfo)
    {
        charmInfo->SetIsCommandAttack(false);
        charmInfo->SetIsReturning(true);
    }
}

// Growl autocast: off in dungeons and raids unless the diamond unit lives; on elsewhere. True when it differs now.
bool GrowlMismatch(Pet* pet, bool wantGrowl)
{
    for (auto const& pair : pet->m_spells)
    {
        if (pair.second.state == PETSPELL_REMOVED)
            continue;
        SpellInfo const* info = sSpellMgr->GetSpellInfo(pair.first);
        if (SpellNameIs(info, "Growl") && info->IsAutocastable() && PetHasAutocast(pet, pair.first) != wantGrowl)
            return true;
    }
    return false;
}

// The pet's Furious Howl (any rank it knows), or 0.
uint32 FuriousHowlId(Pet* pet)
{
    for (auto const& pair : pet->m_spells)
    {
        if (pair.second.state == PETSPELL_REMOVED)
            continue;
        if (SpellNameIs(sSpellMgr->GetSpellInfo(pair.first), "Furious Howl"))
            return pair.first;
    }
    return 0;
}

bool DiamondWanted()
{
    return CompanionClass::ItemEnabled("DiamondTank");
}

bool WantGrowl(PlayerbotAI* botAI, Player* bot)
{
    return !InInstance(bot) || (DiamondWanted() && CompanionClass::DiamondUnit(botAI) != nullptr);
}

// --- Rogue -----------------------------------------------------------------------------------------------------

// A hostile unit that the group has engaged: the lead assist target, else a tank member's victim.
Unit* EngagedGroupTarget(PlayerbotAI* botAI, Player* bot)
{
    if (Unit* assist = CompanionLead::AssistTarget(botAI))
        return assist;
    Group* group = bot->GetGroup();
    if (!group)
        return nullptr;
    for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
    {
        Player* member = itr->GetSource();
        if (!member || member == bot || !member->IsAlive() || !PlayerbotAI::IsTank(member))
            continue;
        Unit* victim = member->GetVictim();
        if (victim && victim->IsAlive() && victim->IsCreature() && bot->IsHostileTo(victim) &&
            bot->GetDistance(victim) <= OPENER_RANGE && member->IsInCombat())
            return victim;
    }
    return nullptr;
}

std::mutex gOpenerMutex;
std::unordered_map<uint32, uint32> gOpenerStart;   // low guid -> ms of the first opener tick on this fight

// Units fighting the group close to the rogue.
uint32 NearbyAttackers(PlayerbotAI* botAI, Player* bot)
{
    uint32 count = 0;
    GuidVector attackers = botAI->GetAiObjectContext()->GetValue<GuidVector>("attackers")->Get();
    for (ObjectGuid guid : attackers)
    {
        Unit* unit = botAI->GetUnit(guid);
        if (unit && unit->IsAlive() && bot->GetDistance(unit) <= BURST_RADIUS)
            ++count;
    }
    return count;
}

// --- Triggers --------------------------------------------------------------------------------------------------

// Common guard: a living companion with the master switch on. Check() holds the item rule.
class ClassTrigger : public Trigger
{
public:
    ClassTrigger(PlayerbotAI* botAI, std::string const& name, int32 checkIntervalMs = 1)
        : Trigger(botAI, name, checkIntervalMs)
    {
    }

    bool IsActive() override
    {
        Player* player = botAI->GetBot();
        if (!player || !player->IsAlive() || !IsCompanionBot(player) || !CompanionClass::MasterEnabled())
            return false;
        return Check(player);
    }

protected:
    virtual bool Check(Player* player) = 0;
};

class FreeHeroicStrikeTrigger : public ClassTrigger
{
public:
    FreeHeroicStrikeTrigger(PlayerbotAI* botAI) : ClassTrigger(botAI, "companion free heroic strike") {}

protected:
    bool Check(Player* player) override
    {
        if (player->getClass() != CLASS_WARRIOR || !CompanionClass::ItemEnabled("RevengeGlyph"))
            return false;
        uint32 buff = RevengeGlyphBuff(player);
        if (!buff || !player->HasAura(buff))
            return false;
        Unit* victim = player->GetVictim();
        return victim && victim->IsAlive() && player->IsWithinMeleeRange(victim);
    }
};

class PaladinHealTrigger : public ClassTrigger
{
public:
    PaladinHealTrigger(PlayerbotAI* botAI) : ClassTrigger(botAI, "companion paladin heal") {}

protected:
    bool Check(Player* player) override
    {
        return player->getClass() == CLASS_PALADIN && CompanionClass::ItemEnabled("PaladinHeal") &&
               HealTarget(player) != nullptr;
    }
};

class GiftTrigger : public ClassTrigger
{
public:
    GiftTrigger(PlayerbotAI* botAI) : ClassTrigger(botAI, "companion gift of the naaru", 1000) {}

protected:
    bool Check(Player* player) override
    {
        if (!CompanionClass::ItemEnabled("GiftOfTheNaaru"))
            return false;
        Player* target = GiftTarget(botAI, player);
        return target && botAI->CanCastSpell("gift of the naaru", target);
    }
};

class BlessingTrigger : public ClassTrigger
{
public:
    BlessingTrigger(PlayerbotAI* botAI) : ClassTrigger(botAI, "companion blessing needed", 2000) {}

protected:
    bool Check(Player* player) override
    {
        if (player->getClass() != CLASS_PALADIN || !CompanionClass::ItemEnabled("Blessings") ||
            player->IsInCombat() || player->IsMounted())
            return false;
        return FindBlessingWork(botAI, player).target != nullptr;
    }
};

class ConsumableTrigger : public ClassTrigger
{
public:
    ConsumableTrigger(PlayerbotAI* botAI) : ClassTrigger(botAI, "companion consumable needed", 2000) {}

protected:
    bool Check(Player* player) override
    {
        if (!CompanionClass::ItemEnabled("Consumables") || player->IsInCombat() || player->IsMounted())
            return false;
        return FoodToEat(botAI, player) != nullptr || OilToApply(player, nullptr) != nullptr;
    }
};

class PetControlTrigger : public ClassTrigger
{
public:
    PetControlTrigger(PlayerbotAI* botAI) : ClassTrigger(botAI, "companion pet control", 500) {}

protected:
    bool Check(Player* player) override
    {
        if (player->getClass() != CLASS_HUNTER)
            return false;
        Pet* pet = player->GetPet();
        if (!pet || !pet->IsAlive())
            return false;
        bool growl = CompanionClass::ItemEnabled("PetGrowl");
        if (growl && GrowlMismatch(pet, WantGrowl(botAI, player)))
            return true;
        if (!DiamondWanted())
            return false;
        bool tracked;
        {
            std::lock_guard<std::mutex> lock(gPetMutex);
            tracked = gDiamondActive.count(player->GetGUID().GetCounter()) > 0;
        }
        Unit* diamond = CompanionClass::DiamondUnit(botAI);
        if (diamond && player->IsInCombat())
            return pet->GetVictim() != diamond;
        return tracked && !diamond;
    }
};

class FuriousHowlTrigger : public ClassTrigger
{
public:
    FuriousHowlTrigger(PlayerbotAI* botAI) : ClassTrigger(botAI, "companion furious howl", 1000) {}

protected:
    bool Check(Player* player) override
    {
        if (player->getClass() != CLASS_HUNTER || !player->IsInCombat() || !CompanionClass::ItemEnabled("FuriousHowl"))
            return false;
        Pet* pet = player->GetPet();
        if (!pet || !pet->IsAlive() || pet->IsNonMeleeSpellCast(false))
            return false;
        uint32 spellId = FuriousHowlId(pet);
        return spellId && !pet->HasSpellCooldown(spellId);
    }
};

class RogueStealthUpTrigger : public ClassTrigger
{
public:
    RogueStealthUpTrigger(PlayerbotAI* botAI) : ClassTrigger(botAI, "companion rogue stealth up", 2000) {}

protected:
    bool Check(Player* player) override
    {
        if (player->getClass() != CLASS_ROGUE || !CompanionClass::ItemEnabled("RogueOpener") || !InInstance(player) ||
            !player->GetGroup() || player->IsInCombat() || player->IsMounted() ||
            player->HasAuraType(SPELL_AURA_MOD_STEALTH))
            return false;
        return botAI->HasSpell("stealth") && botAI->CanCastSpell("stealth", player);
    }
};

class RogueOpenerTrigger : public ClassTrigger
{
public:
    RogueOpenerTrigger(PlayerbotAI* botAI) : ClassTrigger(botAI, "companion rogue opener") {}

protected:
    bool Check(Player* player) override
    {
        if (player->getClass() != CLASS_ROGUE || !CompanionClass::ItemEnabled("RogueOpener") || !InInstance(player) ||
            player->IsInCombat() || !player->HasAuraType(SPELL_AURA_MOD_STEALTH))
        {
            std::lock_guard<std::mutex> lock(gOpenerMutex);
            gOpenerStart.erase(player->GetGUID().GetCounter());
            return false;
        }
        return EngagedGroupTarget(botAI, player) != nullptr;
    }
};

class RogueFinisherTrigger : public ClassTrigger
{
public:
    RogueFinisherTrigger(PlayerbotAI* botAI) : ClassTrigger(botAI, "companion rogue finisher") {}

protected:
    bool Check(Player* player) override
    {
        if (player->getClass() != CLASS_ROGUE || !CompanionClass::ItemEnabled("RogueRotation"))
            return false;
        return AI_VALUE2(uint8, "combo", "current target") >= 5;
    }
};

// Blade Flurry and Adrenaline Rush: only when trained, not already on, and two or more units fight next to the rogue.
class RogueBurstTrigger : public ClassTrigger
{
public:
    RogueBurstTrigger(PlayerbotAI* botAI, std::string const& triggerName, char const* spellName)
        : ClassTrigger(botAI, triggerName, 1000), _spellName(spellName)
    {
    }

protected:
    bool Check(Player* player) override
    {
        if (player->getClass() != CLASS_ROGUE || !player->IsInCombat() || !CompanionClass::ItemEnabled("RogueBurst") ||
            !botAI->HasSpell(_spellName) || botAI->HasAura(_spellName, player))
            return false;
        return NearbyAttackers(botAI, player) >= BURST_MIN_UNITS && botAI->CanCastSpell(_spellName, player);
    }

private:
    std::string _spellName;
};

// --- Actions ---------------------------------------------------------------------------------------------------

class PaladinHealAction : public Action
{
public:
    PaladinHealAction(PlayerbotAI* botAI) : Action(botAI, "companion paladin heal") {}

    bool Execute(Event /*event*/) override
    {
        Player* target = HealTarget(bot);
        if (!target || !botAI->CanCastSpell("flash of light", target))
            return false;
        bool cast = botAI->CastSpell("flash of light", target);
        if (cast)
            LOG_INFO("playerbots", "Companion {}: heal target={} hp={} healer_covers={}", bot->GetName(),
                     target->GetName(), static_cast<uint32>(target->GetHealthPct()), HealerCovers(bot) ? 1 : 0);
        return cast;
    }
};

class GiftAction : public Action
{
public:
    GiftAction(PlayerbotAI* botAI) : Action(botAI, "companion gift of the naaru") {}

    bool Execute(Event /*event*/) override
    {
        Player* target = GiftTarget(botAI, bot);
        if (!target || !botAI->CanCastSpell("gift of the naaru", target))
            return false;
        bool cast = botAI->CastSpell("gift of the naaru", target);
        if (cast)
            LOG_INFO("playerbots", "Companion {}: gift target={} hp={}", bot->GetName(), target->GetName(),
                     static_cast<uint32>(target->GetHealthPct()));
        return cast;
    }
};

class BlessAction : public Action
{
public:
    BlessAction(PlayerbotAI* botAI) : Action(botAI, "companion bless") {}

    bool Execute(Event /*event*/) override
    {
        BlessingWork work = FindBlessingWork(botAI, bot);
        if (!work.target || !botAI->CanCastSpell(work.spell, work.target))
            return false;
        bool cast = botAI->CastSpell(work.spell, work.target);
        if (cast)
            LOG_INFO("playerbots", "Companion {}: bless target={} spell='{}'", bot->GetName(), work.target->GetName(),
                     work.spell);
        return cast;
    }
};

class UseConsumableAction : public UseItemAction
{
public:
    UseConsumableAction(PlayerbotAI* botAI) : UseItemAction(botAI, "companion use consumable") {}

    // UseItemAction::isPossible counts items named like the action; this action picks its own items.
    bool isPossible() override { return true; }

    bool Execute(Event /*event*/) override
    {
        if (bot->IsInCombat())
            return false;
        if (Item* food = FoodToEat(botAI, bot))
        {
            std::string name = food->GetTemplate()->Name1;
            // UseItem sets the next check delay for the length of the meal.
            if (UseItem(food, ObjectGuid::Empty, nullptr))
            {
                NoteUsed(bot, true);
                LOG_INFO("playerbots", "Companion {}: consumable use item='{}' kind=food", bot->GetName(), name);
                return true;
            }
        }
        Item* weapon = nullptr;
        if (Item* oil = OilToApply(bot, &weapon))
        {
            std::string name = oil->GetTemplate()->Name1;
            if (UseItem(oil, ObjectGuid::Empty, weapon))
            {
                NoteUsed(bot, false);
                LOG_INFO("playerbots", "Companion {}: consumable use item='{}' kind=weapon", bot->GetName(), name);
                return true;
            }
        }
        return false;
    }
};

class PetControlAction : public Action
{
public:
    PetControlAction(PlayerbotAI* botAI) : Action(botAI, "companion pet control") {}

    bool Execute(Event /*event*/) override
    {
        Pet* pet = bot->GetPet();
        if (!pet || !pet->IsAlive())
            return false;
        bool acted = false;
        uint32 low = bot->GetGUID().GetCounter();

        // Growl autocast, and Furious Howl autocast stays on.
        bool changeGrowl = CompanionClass::ItemEnabled("PetGrowl");
        bool wantGrowl = changeGrowl && WantGrowl(botAI, bot);
        for (auto const& pair : pet->m_spells)
        {
            if (pair.second.state == PETSPELL_REMOVED)
                continue;
            SpellInfo const* info = sSpellMgr->GetSpellInfo(pair.first);
            if (!info || !info->IsAutocastable())
                continue;
            if (changeGrowl && SpellNameIs(info, "Growl") && PetHasAutocast(pet, pair.first) != wantGrowl)
            {
                pet->ToggleAutocast(info, wantGrowl);
                acted = true;
                LOG_INFO("playerbots", "Companion {}: pet growl={} instance={}", bot->GetName(), wantGrowl ? "on" : "off",
                         InInstance(bot) ? 1 : 0);
            }
            else if (CompanionClass::ItemEnabled("FuriousHowl") && SpellNameIs(info, "Furious Howl") &&
                     !PetHasAutocast(pet, pair.first))
            {
                pet->ToggleAutocast(info, true);
                acted = true;
            }
        }

        // Diamond: the pet holds the off-tank target while it lives; the hunter keeps her own target.
        if (DiamondWanted())
        {
            Unit* diamond = CompanionClass::DiamondUnit(botAI);
            bool tracked;
            {
                std::lock_guard<std::mutex> lock(gPetMutex);
                tracked = gDiamondActive.count(low) > 0;
            }
            if (diamond && bot->IsInCombat())
            {
                if (pet->GetVictim() != diamond)
                {
                    PetAttackUnit(pet, diamond);
                    acted = true;
                }
                if (!tracked)
                {
                    std::lock_guard<std::mutex> lock(gPetMutex);
                    gDiamondActive.insert(low);
                    LOG_INFO("playerbots", "Companion {}: pet diamond target={} growl=on", bot->GetName(),
                             diamond->GetName());
                }
            }
            else if (tracked && !diamond)
            {
                {
                    std::lock_guard<std::mutex> lock(gPetMutex);
                    gDiamondActive.erase(low);
                }
                PetReturn(pet, bot);
                acted = true;
                LOG_INFO("playerbots", "Companion {}: pet diamond over growl={}", bot->GetName(),
                         InInstance(bot) ? "off" : "on");
            }
        }
        return acted;
    }
};

class FuriousHowlAction : public Action
{
public:
    FuriousHowlAction(PlayerbotAI* botAI) : Action(botAI, "companion furious howl") {}

    bool Execute(Event /*event*/) override
    {
        Pet* pet = bot->GetPet();
        if (!pet || !pet->IsAlive() || pet->IsNonMeleeSpellCast(false))
            return false;
        uint32 spellId = FuriousHowlId(pet);
        if (!spellId || pet->HasSpellCooldown(spellId))
            return false;
        pet->CastSpell(pet, spellId, false);
        LOG_INFO("playerbots", "Companion {}: pet furious howl spell={}", bot->GetName(), spellId);
        return true;
    }
};

class RogueStealthUpAction : public Action
{
public:
    RogueStealthUpAction(PlayerbotAI* botAI) : Action(botAI, "companion rogue stealth up") {}

    bool Execute(Event /*event*/) override
    {
        if (!botAI->CanCastSpell("stealth", bot))
            return false;
        return botAI->CastSpell("stealth", bot);
    }
};

// Stealthed in a dungeon, the tank has engaged a unit: walk behind it and open with Garrote (known), else Cheap
// Shot, else Sinister Strike. After OPENER_GIVE_UP_MS the stock rotation takes over.
class RogueOpenerAction : public MovementAction
{
public:
    RogueOpenerAction(PlayerbotAI* botAI) : MovementAction(botAI, "companion rogue opener") {}

    bool Execute(Event /*event*/) override
    {
        Unit* target = EngagedGroupTarget(botAI, bot);
        if (!target)
            return false;

        uint32 low = bot->GetGUID().GetCounter();
        uint32 now = getMSTime();
        {
            std::lock_guard<std::mutex> lock(gOpenerMutex);
            auto it = gOpenerStart.find(low);
            if (it == gOpenerStart.end())
                gOpenerStart[low] = now;
            else if (getMSTimeDiff(it->second, now) > OPENER_GIVE_UP_MS)
                return false;
        }

        context->GetValue<Unit*>("current target")->Set(target);

        bool behind = !target->HasInArc(static_cast<float>(M_PI), bot);
        if (!behind || !bot->IsWithinMeleeRange(target))
        {
            float x = 0.0f;
            float y = 0.0f;
            float z = 0.0f;
            float angle = Position::NormalizeOrientation(target->GetOrientation() + static_cast<float>(M_PI));
            target->GetNearPoint(bot, x, y, z, 0.0f, MELEE_BEHIND_OFFSET, angle);
            MoveTo(bot->GetMapId(), x, y, z);
            return true;   // hold the opener until in position
        }

        char const* spell = botAI->HasSpell("garrote")
                                ? "garrote"
                                : (botAI->HasSpell("cheap shot") ? "cheap shot" : "sinister strike");
        if (botAI->CanCastSpell(spell, target) && botAI->CastSpell(spell, target))
            LOG_INFO("playerbots", "Companion {}: opener spell='{}' target={}", bot->GetName(), spell, target->GetName());
        return true;   // out of energy: wait for it
    }
};
}  // namespace

// --- Public API ------------------------------------------------------------------------------------------------

namespace CompanionClass
{
bool MasterEnabled() { return BoolOption("AiPlayerbot.Companion.Class.Enabled", true); }

bool ItemEnabled(char const* item)
{
    return MasterEnabled() && BoolOption(std::string("AiPlayerbot.Companion.Class.") + item + ".Enabled", true);
}

float MendPetBelowPct()
{
    return FloatOption("AiPlayerbot.Companion.Class.MendPet.BelowPct", DEFAULT_MEND_PET_BELOW_PCT);
}

void SyncStrategies(PlayerbotAI* botAI)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!IsCompanionBot(bot))
        return;

    bool on = MasterEnabled();
    auto sync = [botAI](char const* name, bool want, BotState state) {
        bool has = botAI->HasStrategy(name, state);
        if (want && !has)
            botAI->ChangeStrategy(std::string("+") + name, state);
        else if (!want && has)
            botAI->ChangeStrategy(std::string("-") + name, state);
    };
    sync("companion class nc", on, BOT_STATE_NON_COMBAT);
    sync("companion class cb", on, BOT_STATE_COMBAT);

    if (bot->getClass() != CLASS_PALADIN)
        return;
    // Retribution Aura always on: the stock "baoe" strategy has the aura trigger in both states.
    if (ItemEnabled("RetributionAura"))
    {
        sync("baoe", true, BOT_STATE_NON_COMBAT);
        sync("baoe", true, BOT_STATE_COMBAT);
    }
    // The blessing list replaces the stock party blessings (Might, Wisdom, Kings, Sanctuary on party).
    if (ItemEnabled("Blessings") && !PerCompanionOption(bot, "Blessings", DEFAULT_ALDRIC_BLESSINGS).empty())
        for (char const* stock : {"bmight", "bwisdom", "bkings", "bsanc"})
            sync(stock, false, BOT_STATE_NON_COMBAT);
}

bool IsKeepItem(Player* bot, uint32 itemId)
{
    if (!bot || !itemId || !ItemEnabled("Consumables"))
        return false;
    for (ConsumableEntry const& entry : ParseConsumables(bot))
        if (entry.itemId == itemId)
            return true;
    return false;
}

std::vector<std::pair<uint32, uint32>> WantedConsumables(Player* bot)
{
    std::vector<std::pair<uint32, uint32>> wanted;
    if (!bot || !ItemEnabled("Consumables"))
        return wanted;
    for (ConsumableEntry const& entry : ParseConsumables(bot))
        wanted.emplace_back(entry.itemId, entry.count);
    return wanted;
}

bool BlockPullForConsumables(PlayerbotAI* tankAI)
{
    static std::mutex waitMutex;
    static std::unordered_map<uint32, uint32> waitStart;   // tank low guid -> ms the wait began

    Player* tank = tankAI ? tankAI->GetBot() : nullptr;
    if (!tank || !tank->GetGroup())
        return false;
    uint32 low = tank->GetGUID().GetCounter();

    bool needs = false;
    if (ItemEnabled("Consumables"))
    {
        for (GroupReference* itr = tank->GetGroup()->GetFirstMember(); itr != nullptr; itr = itr->next())
        {
            Player* member = itr->GetSource();
            if (!member || member == tank || !member->IsAlive() || member->IsInCombat() ||
                member->GetMapId() != tank->GetMapId() || !IsCompanionBot(member))
                continue;
            PlayerbotAI* memberAI = GET_PLAYERBOT_AI(member);
            if (memberAI && (IsEating(member) || FoodToEat(memberAI, member)))
                needs = true;
        }
    }

    std::lock_guard<std::mutex> lock(waitMutex);
    if (!needs)
    {
        waitStart.erase(low);
        return false;
    }
    uint32 now = getMSTime();
    auto it = waitStart.find(low);
    if (it == waitStart.end())
    {
        waitStart[low] = now;
        return true;
    }
    uint32 waitMs = static_cast<uint32>(FloatOption("AiPlayerbot.Companion.Class.Consumables.ReadyWaitSeconds",
                                                    static_cast<float>(DEFAULT_READY_WAIT_SECONDS)) *
                                        1000.0f);
    return getMSTimeDiff(it->second, now) < waitMs;
}

Unit* DiamondUnit(PlayerbotAI* botAI)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot || bot->getClass() != CLASS_HUNTER || !IsCompanionBot(bot) || !bot->GetPet())
        return nullptr;
    Group* group = bot->GetGroup();
    if (!group)
        return nullptr;
    // Read like the skull (CompanionLead SkullUnit): the group's raid icon slot holds the unit's guid.
    ObjectGuid guid = group->GetTargetIcon(DIAMOND_ICON);
    if (guid.IsEmpty())
        return nullptr;
    Unit* unit = botAI->GetUnit(guid);
    if (!unit || !unit->IsAlive() || !unit->IsInWorld() || unit->GetMapId() != bot->GetMapId() ||
        !bot->IsHostileTo(unit) || bot->GetDistance(unit) > sPlayerbotAIConfig.sightDistance)
        return nullptr;
    return unit;
}

bool IsOffTankUnit(PlayerbotAI* botAI, Unit* unit)
{
    return unit && ItemEnabled("DiamondTank") && DiamondUnit(botAI) == unit;
}
}  // namespace CompanionClass

// --- Strategies ------------------------------------------------------------------------------------------------

void CompanionClassNonCombatStrategy::InitTriggers(std::vector<TriggerNode*>& triggers)
{
    Player* bot = botAI->GetBot();
    if (!bot)
        return;

    // Any companion: listed consumables (weapon oil, food) when the buff is missing.
    triggers.push_back(new TriggerNode(
        "companion consumable needed", { NextAction("companion use consumable", ACTION_NORMAL + 4.0f) }));

    switch (bot->getClass())
    {
        case CLASS_PALADIN:
            triggers.push_back(new TriggerNode(
                "companion paladin heal", { NextAction("companion paladin heal", ACTION_CRITICAL_HEAL) }));
            triggers.push_back(new TriggerNode(
                "companion gift of the naaru", { NextAction("companion gift of the naaru", ACTION_CRITICAL_HEAL - 1.0f) }));
            triggers.push_back(new TriggerNode(
                "companion blessing needed", { NextAction("companion bless", ACTION_NORMAL + 3.0f) }));
            break;
        case CLASS_HUNTER:
            triggers.push_back(new TriggerNode(
                "companion pet control", { NextAction("companion pet control", ACTION_NORMAL + 6.0f) }));
            break;
        case CLASS_ROGUE:
            triggers.push_back(new TriggerNode(
                "companion rogue stealth up", { NextAction("companion rogue stealth up", ACTION_HIGH) }));
            triggers.push_back(new TriggerNode(
                "companion rogue opener", { NextAction("companion rogue opener", OPENER_RELEVANCE) }));
            break;
        default:
            break;
    }
}

void CompanionClassCombatStrategy::InitTriggers(std::vector<TriggerNode*>& triggers)
{
    Player* bot = botAI->GetBot();
    if (!bot)
        return;

    switch (bot->getClass())
    {
        case CLASS_WARRIOR:
            // Above the tank rotation (shield slam, devastate, thunder clap at ACTION_HIGH + 1 and + 2).
            triggers.push_back(new TriggerNode(
                "companion free heroic strike", { NextAction("heroic strike", ACTION_HIGH + 10.0f) }));
            break;
        case CLASS_PALADIN:
            triggers.push_back(new TriggerNode(
                "companion paladin heal", { NextAction("companion paladin heal", ACTION_CRITICAL_HEAL) }));
            triggers.push_back(new TriggerNode(
                "companion gift of the naaru", { NextAction("companion gift of the naaru", ACTION_CRITICAL_HEAL - 1.0f) }));
            break;
        case CLASS_HUNTER:
            triggers.push_back(new TriggerNode(
                "companion pet control", { NextAction("companion pet control", ACTION_HIGH + 6.0f) }));
            triggers.push_back(new TriggerNode(
                "companion furious howl", { NextAction("companion furious howl", ACTION_HIGH + 5.0f) }));
            break;
        case CLASS_ROGUE:
            // Eviscerate at 5 points, above the stock Rupture (ACTION_HIGH + 1).
            triggers.push_back(new TriggerNode(
                "companion rogue finisher", { NextAction("eviscerate", ACTION_HIGH + 1.5f) }));
            triggers.push_back(new TriggerNode(
                "companion blade flurry", { NextAction("blade flurry", ACTION_HIGH + 4.0f) }));
            triggers.push_back(new TriggerNode(
                "companion adrenaline rush", { NextAction("adrenaline rush", ACTION_HIGH + 2.0f) }));
            break;
        default:
            break;
    }
}

// --- Factories -------------------------------------------------------------------------------------------------

Trigger* CreateCompanionClassTrigger(PlayerbotAI* botAI, std::string const& name)
{
    if (name == "companion free heroic strike")
        return new FreeHeroicStrikeTrigger(botAI);
    if (name == "companion paladin heal")
        return new PaladinHealTrigger(botAI);
    if (name == "companion gift of the naaru")
        return new GiftTrigger(botAI);
    if (name == "companion blessing needed")
        return new BlessingTrigger(botAI);
    if (name == "companion consumable needed")
        return new ConsumableTrigger(botAI);
    if (name == "companion pet control")
        return new PetControlTrigger(botAI);
    if (name == "companion furious howl")
        return new FuriousHowlTrigger(botAI);
    if (name == "companion rogue stealth up")
        return new RogueStealthUpTrigger(botAI);
    if (name == "companion rogue opener")
        return new RogueOpenerTrigger(botAI);
    if (name == "companion rogue finisher")
        return new RogueFinisherTrigger(botAI);
    if (name == "companion blade flurry")
        return new RogueBurstTrigger(botAI, name, "blade flurry");
    if (name == "companion adrenaline rush")
        return new RogueBurstTrigger(botAI, name, "adrenaline rush");
    return nullptr;
}

Action* CreateCompanionClassAction(PlayerbotAI* botAI, std::string const& name)
{
    if (name == "companion paladin heal")
        return new PaladinHealAction(botAI);
    if (name == "companion gift of the naaru")
        return new GiftAction(botAI);
    if (name == "companion bless")
        return new BlessAction(botAI);
    if (name == "companion use consumable")
        return new UseConsumableAction(botAI);
    if (name == "companion pet control")
        return new PetControlAction(botAI);
    if (name == "companion furious howl")
        return new FuriousHowlAction(botAI);
    if (name == "companion rogue stealth up")
        return new RogueStealthUpAction(botAI);
    if (name == "companion rogue opener")
        return new RogueOpenerAction(botAI);
    return nullptr;
}
