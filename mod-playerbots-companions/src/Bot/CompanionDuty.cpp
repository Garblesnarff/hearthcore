/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "CompanionDuty.h"

#include <atomic>

#include <algorithm>
#include <cmath>
#include <deque>
#include <iomanip>
#include <list>
#include <mutex>
#include <set>
#include <sstream>
#include <unordered_map>

#include "Bag.h"
#include "CellImpl.h"
#include "Chat.h"
#include "CompanionBotMgr.h"
#include "CompanionBrain.h"
#include "CompanionCraft.h"
#include "CompanionLease.h"
#include "CompanionTransport.h"
#include "CharacterCache.h"
#include "Creature.h"
#include "DBCEnums.h"
#include "DatabaseEnv.h"
#include "GameObject.h"
#include "GameTime.h"
#include "GridNotifiers.h"
#include "GridNotifiersImpl.h"
#include "GuildMgr.h"
#include "Item.h"
#include "ItemPackets.h"
#include "ItemTemplate.h"
#include "Log.h"
#include "Map.h"
#include "NearestGameObjects.h"
#include "ObjectAccessor.h"
#include "ObjectMgr.h"
#include "Playerbots.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "PlayerbotFactory.h"
#include "Trainer.h"
#include "TrainerAction.h"
#include "GuildBankAction.h"
#include "WorldPacket.h"
#include "WorldSession.h"

namespace
{
using DutyKind = CompanionBotMgr::DutySpotKind;

// Trainers checked, nearest first, for a far TRAIN candidate and in range. Profession trainers (Cooking, Fishing, First
// Aid, new ranks) are rarely among the nearest three, so phase 2 checks more (only when the train signature changes).
constexpr size_t FAR_TRAINER_CHECKS = 25;
constexpr size_t NEAR_TRAINER_CHECKS = 8;

struct Choice
{
    uint32 atSec = 0;
    std::string text;
    std::vector<CompanionDuty::Score> top;
};

struct DutyState
{
    uint32 lastEvalSec = 0;
    bool active = false;
    std::string dutyName;
    ObjectGuid target;
    CompanionDuty::DutyPoint point;                     // last far point of the duty (kept for the cooldown)
    bool far = false;                                   // started at a far point: long timeout until it ends
    uint32 startSec = 0;
    std::vector<CompanionDuty::Score> top;              // last evaluation, best first
    std::deque<Choice> ring;                            // last RING_SIZE choices
    std::unordered_map<uint64, uint32> cooldownUntil;   // spawn key -> second a far walk there may start again
    uint32 trainCheckLevel = 0;                         // train signature (CompanionCraft::TrainSignature: level,
                                                        // profession caps, missing secondary skills) of the last
                                                        // trainer arrival or empty far search
    bool noTabLogged = false;
    bool noRightsLogged = false;
    bool bankFullLogged = false;
};

struct GuildBankSnapshot
{
    std::vector<CompanionDuty::BankTabUsage> tabs;   // purchased tabs, ascending
    std::vector<CompanionDuty::BankSlot> slots;      // every item in those tabs
};

// One candidate duty for this evaluation. valid = need > 0 and a target exists (an object in range, or a far point).
struct Candidate
{
    CompanionDuty::Score score;
    bool valid = false;
    ObjectGuid target;
    CompanionDuty::DutyPoint point;
    CompanionLease::Destination dest;   // lease destination: the object's spawn key and position, or the far point
};

std::mutex gStateMutex;                                   // DutyState map: map threads and the GM command
std::unordered_map<uint32, DutyState> gStates;             // low guid -> state
std::mutex gBankMutex;                                    // guild bank snapshots: world thread and map threads
std::unordered_map<uint32, GuildBankSnapshot> gBanks;     // guild id -> tab snapshot
std::atomic<bool> gBankRefreshNow{false};                // set by InvalidateBank (any thread)
uint32 gBankTimerMs = 0;                                  // world thread only

uint32 NowSec()
{
    return static_cast<uint32>(GameTime::GetGameTime().count());
}

bool Eligible(Player* bot)
{
    return sCompanionBotMgr.IsCompanion(bot) && !sCompanionBotMgr.IsGroupedWithOwner(bot) && bot->IsInWorld() &&
           bot->IsAlive() && !bot->IsInCombat() && bot->GetMap() && !bot->GetMap()->Instanceable();
}

// General-purpose backpack and container bag items (excludes Quivers/Ammo Pouches/Soul Bags from slot totals so
// Hunters and Warlocks still detect full regular bags accurately).
void CountBags(Player* bot, std::vector<Item*>& items, uint32& used, uint32& total)
{
    used = 0;
    total = 0;
    for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
    {
        ++total;
        if (Item* item = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
        {
            ++used;
            items.push_back(item);
        }
    }
    for (uint8 bagSlot = INVENTORY_SLOT_BAG_START; bagSlot < INVENTORY_SLOT_BAG_END; ++bagSlot)
    {
        Bag* bag = static_cast<Bag*>(bot->GetItemByPos(INVENTORY_SLOT_BAG_0, bagSlot));
        if (!bag)
            continue;
        ItemTemplate const* bProto = bag->GetTemplate();
        bool const isGeneralBag =
            bProto && bProto->Class == ITEM_CLASS_CONTAINER && bProto->SubClass == ITEM_SUBCLASS_CONTAINER;
        for (uint32 i = 0; i < bag->GetBagSize(); ++i)
        {
            if (isGeneralBag)
                ++total;
            if (Item* item = bag->GetItemByPos(i))
            {
                if (isGeneralBag)
                    ++used;
                items.push_back(item);
            }
        }
    }
}

// Counts matching projectile ammo (arrows for bows/crossbows, bullets for guns) for a Hunter companion.
uint32 CountHunterAmmo(Player* bot, uint32& outSubClass)
{
    outSubClass = 0;
    if (!bot || bot->getClass() != CLASS_HUNTER)
        return 9999;
    Item* ranged = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_RANGED);
    if (!ranged || !ranged->GetTemplate())
        return 9999;
    uint32 const rSub = ranged->GetTemplate()->SubClass;
    if (rSub == ITEM_SUBCLASS_WEAPON_GUN)
        outSubClass = ITEM_SUBCLASS_BULLET;
    else if (rSub == ITEM_SUBCLASS_WEAPON_BOW || rSub == ITEM_SUBCLASS_WEAPON_CROSSBOW)
        outSubClass = ITEM_SUBCLASS_ARROW;
    else
        return 9999;

    uint32 total = 0;
    uint8 const lvl = bot->GetLevel();
    auto countItem = [&](Item* it)
    {
        if (!it || !it->GetTemplate())
            return;
        ItemTemplate const* p = it->GetTemplate();
        if (p->Class == ITEM_CLASS_PROJECTILE && p->SubClass == outSubClass && p->RequiredLevel <= lvl)
            total += it->GetCount();
    };
    for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
        countItem(bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot));
    for (uint8 bagSlot = INVENTORY_SLOT_BAG_START; bagSlot < INVENTORY_SLOT_BAG_END; ++bagSlot)
        if (Bag* bag = static_cast<Bag*>(bot->GetItemByPos(INVENTORY_SLOT_BAG_0, bagSlot)))
            for (uint32 i = 0; i < bag->GetBagSize(); ++i)
                countItem(bag->GetItemByPos(i));
    return total;
}

// Lowest durability percent over the equipped items (100 when nothing is equipped or breakable).
float DurabilityPct(Player* bot)
{
    float minPct = 100.0f;
    for (uint8 slot = EQUIPMENT_SLOT_START; slot < EQUIPMENT_SLOT_END; ++slot)
    {
        Item* item = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot);
        if (!item)
            continue;
        uint32 maxDur = item->GetUInt32Value(ITEM_FIELD_MAXDURABILITY);
        if (!maxDur)
            continue;
        float pct = 100.0f * item->GetUInt32Value(ITEM_FIELD_DURABILITY) / maxDur;
        minPct = std::min(minPct, pct);
    }
    return minPct;
}

// Creatures on the bot's map within range that carry one of the flags, nearest first.
void CollectNpcs(Player* bot, std::vector<uint32> const& flags, float range, std::vector<Creature*>& out)
{
    std::list<Unit*> units;
    Acore::AnyUnitInObjectRangeCheck check(bot, range);
    Acore::UnitListSearcher<Acore::AnyUnitInObjectRangeCheck> searcher(bot, units, check);
    Cell::VisitObjects(bot, searcher, range);

    for (Unit* unit : units)
    {
        Creature* creature = unit->ToCreature();
        if (!creature || !creature->IsAlive() || creature->IsHostileTo(bot))
            continue;
        for (uint32 flag : flags)
        {
            if (creature->HasNpcFlag(static_cast<NPCFlags>(flag)))
            {
                out.push_back(creature);
                break;
            }
        }
    }
    std::sort(out.begin(), out.end(),
              [bot](Creature* a, Creature* b) { return bot->GetDistance(a) < bot->GetDistance(b); });
}

// Nearest game object of type GUILD_BANK within range.
GameObject* NearestGuildBank(Player* bot, float range)
{
    std::list<GameObject*> targets;
    AnyGameObjectInObjectRangeCheck check(bot, range);
    Acore::GameObjectListSearcher<AnyGameObjectInObjectRangeCheck> searcher(bot, targets, check);
    Cell::VisitObjects(bot, searcher, range);

    GameObject* best = nullptr;
    float bestDist = 0.0f;
    for (GameObject* go : targets)
    {
        if (go->GetGoType() != GAMEOBJECT_TYPE_GUILD_BANK)
            continue;
        float dist = bot->GetDistance(go);
        if (!best || dist < bestDist)
        {
            best = go;
            bestDist = dist;
        }
    }
    return best;
}

// True when the trainer teaches at least one spell this bot may learn and can afford at the given discount (same
// rules as RpgTrainAction). Works on the entry, so a far trainer (no creature object) is checked the same way.
bool TrainerCanTeach(Player* bot, uint32 entry, float discount)
{
    Trainer::Trainer* trainer = sObjectMgr->GetTrainer(entry);
    if (!trainer || !trainer->IsTrainerValidForPlayer(bot))
        return false;

    for (auto& spell : trainer->GetSpells())
    {
        Trainer::Spell const* trainerSpell = trainer->GetSpell(spell.SpellId);
        if (!trainerSpell)
            continue;
        if (!PlayerbotFactory::IsTrainerSpellAllowedForBot(bot, trainer, trainerSpell))
            continue;
        if (!trainer->CanTeachSpell(bot, trainerSpell))
            continue;
        if (bot->GetMoney() < static_cast<uint32>(std::floor(trainerSpell->MoneyCost * discount)))
            continue;
        return true;
    }
    return false;
}

// Travel cost: yards / 20, capped at 100. Another map is 100.
float TravelCost(Player* bot, WorldObject const* target)
{
    if (target->GetMapId() != bot->GetMapId())
        return 100.0f;
    return std::min(100.0f, bot->GetDistance(target) / 20.0f);
}

void Fill(Candidate& candidate, char const* name, float need, float weight, float travelWeight,
          WorldObject const* target, Player* bot)
{
    candidate.score = CompanionDuty::Score();
    candidate.score.name = name;
    candidate.score.need = need;
    candidate.valid = false;
    candidate.target = ObjectGuid();
    candidate.point = CompanionDuty::DutyPoint();
    if (need <= 0.0f || !target)
        return;
    candidate.score.travel = TravelCost(bot, target);
    candidate.score.score = need * weight - candidate.score.travel * travelWeight;
    candidate.valid = true;
    candidate.target = target->GetGUID();
    candidate.dest.key = CompanionLease::ObjectKey(target);
    candidate.dest.mapId = target->GetMapId();
    candidate.dest.x = target->GetPositionX();
    candidate.dest.y = target->GetPositionY();
    candidate.dest.z = target->GetPositionZ();
}

// Same as Fill, for a far point. The point is on the bot's map (FarSpawns only returns that map).
void FillFar(Candidate& candidate, char const* name, float need, float weight, float travelWeight,
             CompanionDuty::DutyPoint const& point, Player* bot)
{
    candidate.score = CompanionDuty::Score();
    candidate.score.name = name;
    candidate.score.need = need;
    candidate.valid = false;
    candidate.target = ObjectGuid();
    candidate.point = CompanionDuty::DutyPoint();
    if (need <= 0.0f || !point.valid)
        return;
    candidate.score.travel = std::min(100.0f, bot->GetDistance(point.x, point.y, point.z) / 20.0f);
    if (CompanionTransport::Enabled() && CompanionTransport::Ready() &&
        CompanionTransport::NeedsLegs(bot, point.mapId, point.x, point.y, point.z, point.zone))
    {
        // Another landmass on this map (Teldrassil, Rut'theran, Feathermoon, Azuremyst): by boat or portal, in
        // walking yards; unreachable points are not offered. No hearth leg: the duty walk never uses one.
        CompanionTransport::Plan plan =
            CompanionTransport::PlanRoute(bot, point.mapId, point.x, point.y, point.z, point.zone, 7.0f, false);
        if (!plan.ok)
            return;
        candidate.score.travel = std::min(100.0f, plan.seconds * 7.0f / 20.0f);
    }
    candidate.score.score = need * weight - candidate.score.travel * travelWeight;
    candidate.valid = true;
    candidate.point = point;
    candidate.dest.key = CompanionLease::SpawnKey(point.isGo, point.spawnId);
    candidate.dest.mapId = point.mapId;
    candidate.dest.x = point.x;
    candidate.dest.y = point.y;
    candidate.dest.z = point.z;
}

// Logs a per-bot condition once. Returns true only the first time for the given flag.
bool FirstLog(uint32 low, bool DutyState::*flag)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    bool& logged = gStates[low].*flag;
    if (logged)
        return false;
    logged = true;
    return true;
}

// The guild bank duty needs a snapshot and a tab with deposit rights that has a free slot.
bool BankReady(Player* bot, uint32 low)
{
    uint32 guildId = bot->GetGuildId();
    if (!guildId)
        return false;

    std::vector<CompanionDuty::BankTabUsage> tabs;
    std::vector<CompanionDuty::BankSlot> slots;
    if (!CompanionDuty::BankSnapshot(guildId, tabs, slots))
        return false;   // snapshot pending, or invalidated after a move
    if (tabs.empty())
    {
        if (FirstLog(low, &DutyState::noTabLogged))
            LOG_WARN("playerbots", "[Companion] {} has no guild bank tab, BANK duty off", bot->GetName());
        return false;
    }

    Guild* guild = sGuildMgr->GetGuildById(guildId);
    bool hasRights = false;
    bool hasViewRights = false;
    for (CompanionDuty::BankTabUsage const& tab : tabs)
    {
        if (guild && guild->MemberHasTabRights(bot->GetGUID(), tab.tab, GUILD_BANK_RIGHT_VIEW_TAB))
            hasViewRights = true;
        if (!guild || !guild->MemberHasTabRights(bot->GetGUID(), tab.tab, GUILD_BANK_RIGHT_DEPOSIT_ITEM))
            continue;
        hasRights = true;
        if (tab.used < static_cast<uint32>(GUILD_BANK_MAX_SLOTS))
            return true;
    }
    if (!hasRights)
    {
        if (FirstLog(low, &DutyState::noRightsLogged))
            LOG_WARN("playerbots", "[Companion] {} has no rights on any guild bank tab, BANK duty off", bot->GetName());
        return false;
    }
    // Break full-bank deadlock: when AutoOrganize is on and the bot has bag space to pull non-whitelisted clutter
    // from viewable bank tabs for vendoring, keep BANK duty active so the bank self-cleans.
    if (sPlayerbotAIConfig.companionBankAutoOrganize && hasViewRights && bot->GetFreeInventorySpace() >= 3)
    {
        for (CompanionDuty::BankSlot const& s : slots)
        {
            if (!guild->MemberHasTabRights(bot->GetGUID(), s.tab, GUILD_BANK_RIGHT_VIEW_TAB))
                continue;
            ItemTemplate const* proto = sObjectMgr->GetItemTemplate(s.entry);
            if (proto && proto->SellPrice > 0 && !CompanionBrain::IsBankWantedItem(proto))
                return true;
        }
    }
    if (FirstLog(low, &DutyState::bankFullLogged))
        LOG_WARN("playerbots", "[Companion] {} guild bank full: no tab with deposit rights has a free slot, "
                 "BANK duty off", bot->GetName());
    return false;
}

// Sells every SELL-class item to the vendor, with the same packet the player sell path uses. Returns the number of
// SELL-class items the vendor did not take.
uint32 SellAtVendor(Player* bot, PlayerbotAI* botAI, Creature* vendor)
{
    uint32 unsold = 0;
    std::vector<Item*> items;
    uint32 used = 0;
    uint32 total = 0;
    CountBags(bot, items, used, total);

    for (Item* item : items)
    {
        if (CompanionBrain::ClassifyItem(botAI, item) != CompanionBrain::ItemClass::SELL)
            continue;

        // Read everything before the sale: the item object is gone after it.
        ObjectGuid itemGuid = item->GetGUID();
        uint32 count = item->GetCount();
        uint32 entry = item->GetEntry();
        std::string name = item->GetTemplate() ? item->GetTemplate()->Name1 : "item";

        WorldPacket p(CMSG_SELL_ITEM);
        p << vendor->GetGUID() << itemGuid << count;
        WorldPackets::Item::SellItem nicePacket(std::move(p));
        nicePacket.Read();
        bot->GetSession()->HandleSellItemOpcode(nicePacket);

        if (!bot->GetItemByGuid(itemGuid))
        {
            LOG_INFO("playerbots", "[Companion] {} sold {} x{} to {}", bot->GetName(), name, count,
                     vendor->GetName());
            CompanionDuty::RecordLedger(bot->GetGUID().GetCounter(), "sell", entry, count, -1, "vendor duty");
        }
        else
        {
            ++unsold;
            LOG_WARN("playerbots", "[Companion] {} could not sell {} x{} to {}", bot->GetName(), name, count,
                     vendor->GetName());
        }
    }
    return unsold;
}

// If the companion still has empty bag slots (e.g. fresh level 1-15 with only the 16-slot backpack) and this vendor
// sells affordable regular bags (6+ slots), buy and equip them so bag crunches stop happening.
void TryBuyStarterBags(Player* bot, Creature* vendor)
{
    if (!bot || !vendor || !bot->GetNPCIfCanInteractWith(vendor->GetGUID(), UNIT_NPC_FLAG_VENDOR))
        return;
    VendorItemData const* vItems = vendor->GetVendorItems();
    if (!vItems || vItems->Empty())
        return;

    float const discount = bot->GetReputationPriceDiscount(vendor);
    for (uint8 bagSlot = INVENTORY_SLOT_BAG_START; bagSlot < INVENTORY_SLOT_BAG_END; ++bagSlot)
    {
        if (bot->GetItemByPos(INVENTORY_SLOT_BAG_0, bagSlot))
            continue;

        uint32 bestSlot = 0;
        uint32 bestItem = 0;
        uint32 bestSize = 0;
        uint32 bestPrice = 0;
        for (uint32 i = 0; i < vItems->GetItemCount(); ++i)
        {
            VendorItem const* vi = vItems->GetItem(i);
            if (!vi || vi->ExtendedCost != 0)
                continue;
            ItemTemplate const* proto = sObjectMgr->GetItemTemplate(vi->item);
            if (!proto || proto->Class != ITEM_CLASS_CONTAINER || proto->SubClass != ITEM_SUBCLASS_CONTAINER ||
                proto->ContainerSlots < 6 || proto->BuyPrice == 0)
                continue;
            uint32 const price = std::max<uint32>(1, static_cast<uint32>(std::floor(proto->BuyPrice * discount)));
            // Keep at least half our purse for class training / repairs.
            if (bot->GetMoney() < price * 2)
                continue;
            if (proto->ContainerSlots > bestSize || (proto->ContainerSlots == bestSize && price < bestPrice))
            {
                bestSlot = i;
                bestItem = vi->item;
                bestSize = proto->ContainerSlots;
                bestPrice = price;
            }
        }
        if (!bestItem)
            break;

        if (bot->BuyItemFromVendorSlot(vendor->GetGUID(), bestSlot, bestItem, 1, INVENTORY_SLOT_BAG_0, bagSlot))
        {
            ItemTemplate const* proto = sObjectMgr->GetItemTemplate(bestItem);
            LOG_INFO("playerbots", "[Companion] {} bought {} ({}-slot bag) from {} for {}c", bot->GetName(),
                     proto ? proto->Name1 : "bag", bestSize, vendor->GetName(), bestPrice);
            CompanionDuty::RecordLedger(bot->GetGUID().GetCounter(), "buy", bestItem, 1, -1, "starter bag");
        }
        else
            break;
    }
}

// Restocks Hunter bullets/arrows at any vendor that carries them whenever ammo is below 400.
void TryBuyHunterAmmo(Player* bot, Creature* vendor)
{
    if (!bot || !vendor || bot->getClass() != CLASS_HUNTER ||
        !bot->GetNPCIfCanInteractWith(vendor->GetGUID(), UNIT_NPC_FLAG_VENDOR))
        return;
    uint32 wantSub = 0;
    uint32 haveAmmo = CountHunterAmmo(bot, wantSub);
    if (!wantSub || haveAmmo >= 400)
        return;

    VendorItemData const* vItems = vendor->GetVendorItems();
    if (!vItems || vItems->Empty())
        return;

    float const discount = bot->GetReputationPriceDiscount(vendor);
    uint8 const lvl = bot->GetLevel();
    uint32 bestSlot = 0;
    uint32 bestItem = 0;
    uint32 bestReqLvl = 0;
    uint32 bestStack = 200;
    uint32 bestPrice = 0;
    for (uint32 i = 0; i < vItems->GetItemCount(); ++i)
    {
        VendorItem const* vi = vItems->GetItem(i);
        if (!vi || vi->ExtendedCost != 0)
            continue;
        ItemTemplate const* proto = sObjectMgr->GetItemTemplate(vi->item);
        if (!proto || proto->Class != ITEM_CLASS_PROJECTILE || proto->SubClass != wantSub ||
            proto->RequiredLevel > lvl || proto->BuyPrice == 0)
            continue;
        uint32 const stack = std::max<uint32>(1, proto->BuyCount);
        uint32 const price = std::max<uint32>(1, static_cast<uint32>(std::floor(proto->BuyPrice * discount)));
        if (bot->GetMoney() < price)
            continue;
        if (!bestItem || proto->RequiredLevel > bestReqLvl ||
            (proto->RequiredLevel == bestReqLvl && proto->ItemLevel > sObjectMgr->GetItemTemplate(bestItem)->ItemLevel))
        {
            bestSlot = i;
            bestItem = vi->item;
            bestReqLvl = proto->RequiredLevel;
            bestStack = stack;
            bestPrice = price;
        }
    }
    if (!bestItem)
        return;

    uint32 bought = 0;
    for (int tries = 0; tries < 3 && haveAmmo < 600 && bot->GetMoney() >= bestPrice; ++tries)
    {
        if (!bot->BuyItemFromVendorSlot(vendor->GetGUID(), bestSlot, bestItem, 1, NULL_BAG, NULL_SLOT))
            break;
        haveAmmo += bestStack;
        bought += bestStack;
    }
    if (bought > 0)
    {
        bot->SetAmmo(bestItem);
        ItemTemplate const* proto = sObjectMgr->GetItemTemplate(bestItem);
        LOG_INFO("playerbots", "[Companion] {} restocked ammo {} x{} from {} (now {})", bot->GetName(),
                 proto ? proto->Name1 : "ammo", bought, vendor->GetName(), haveAmmo);
        CompanionDuty::RecordLedger(bot->GetGUID().GetCounter(), "buy", bestItem, bought, -1, "hunter ammo");
    }
}

// Repairs equipped items at a repair NPC. Same calls as RepairAllAction, without the chat line. The cost is the money
// the purse lost across the calls, not their return values.
bool RepairAt(Player* bot, Creature* npc)
{
    if (!bot->GetNPCIfCanInteractWith(npc->GetGUID(), UNIT_NPC_FLAG_REPAIR))
        return false;

    float discountMod = bot->GetReputationPriceDiscount(npc);
    uint32 const moneyBefore = bot->GetMoney();
    bot->DurabilityRepair(EQUIPMENT_SLOT_MAINHAND, true, discountMod, false);
    bot->DurabilityRepair(EQUIPMENT_SLOT_RANGED, true, discountMod, false);
    bot->DurabilityRepair(EQUIPMENT_SLOT_OFFHAND, true, discountMod, false);
    bot->DurabilityRepairAll(true, discountMod, false);
    uint32 const moneyAfter = bot->GetMoney();
    uint32 const totalCost = moneyBefore > moneyAfter ? moneyBefore - moneyAfter : 0;

    LOG_INFO("playerbots", "[Companion] {} repaired at {} for {} copper", bot->GetName(), npc->GetName(), totalCost);
    if (totalCost)
        CompanionDuty::RecordLedger(bot->GetGUID().GetCounter(), "repair", 0, totalCost, -1, "repair duty");
    return true;
}

// True while a far walk to the spawn key is on cooldown. Caller holds gStateMutex.
bool CoolingDown(DutyState const& st, uint64 key, uint32 now)
{
    auto it = st.cooldownUntil.find(key);
    return it != st.cooldownUntil.end() && now < it->second;
}

// Spawns of a kind on the bot's map that a far walk may target, nearest first. Excludes spawns closer than minDist
// (the in-range search covers those), zones outside the bot's level window or team, creatures hostile to the bot's
// team, and spawns on cooldown.
std::vector<CompanionBotMgr::DutySpot const*> FarSpawns(Player* bot, uint32 low, DutyKind kind, float minDist)
{
    std::vector<CompanionBotMgr::DutySpot const*> spots;
    uint32 const now = NowSec();
    bool const horde = bot->GetTeamId() == TEAM_HORDE;
    uint32 const hostileBit = horde ? 4u : 2u;   // FactionTemplate hostileMask bit for the bot's team
    uint32 const team = horde ? AREATEAM_HORDE : AREATEAM_ALLY;
    bool const leaseOn = CompanionLease::Enabled();

    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gStates.find(low);
        DutyState const* st = it != gStates.end() ? &it->second : nullptr;
        for (CompanionBotMgr::DutySpot const& spot : sCompanionBotMgr.DutySpots(bot->GetMapId(), kind))
        {
            if (bot->GetDistance(spot.x, spot.y, spot.z) < minDist)
                continue;
            if (!sCompanionBotMgr.ZoneFitsLevel(spot.zoneId, bot->GetLevel()))
                continue;
            if (sCompanionBotMgr.IsZoneTooDangerous(bot, spot.zoneId))
                continue;
            if (!CompanionTransport::NeedsLegs(bot, bot->GetMapId(), spot.x, spot.y, spot.z, spot.zoneId) &&
                sCompanionBotMgr.IsPathTooDangerous(bot, bot->GetMapId(), spot.x, spot.y, spot.z))
                continue;
            if ((spot.zoneTeam == AREATEAM_ALLY || spot.zoneTeam == AREATEAM_HORDE) && spot.zoneTeam != team)
                continue;
            if (spot.hostileMask & hostileBit)
                continue;
            // Lease on: the lease's cooldown and session skip. Lease off: the legacy far cooldown.
            bool const blocked = leaseOn
                                     ? !CompanionLease::MayTry(low, CompanionLease::SpawnKey(spot.isGo, spot.spawnId))
                                     : (st && CoolingDown(*st, spot.key, now));
            if (blocked)
                continue;
            spots.push_back(&spot);
        }
    }

    std::sort(spots.begin(), spots.end(),
              [bot](CompanionBotMgr::DutySpot const* a, CompanionBotMgr::DutySpot const* b)
              { return bot->GetDistance(a->x, a->y, a->z) < bot->GetDistance(b->x, b->y, b->z); });
    return spots;
}

CompanionDuty::DutyPoint PointAt(Player* bot, CompanionBotMgr::DutySpot const& spot)
{
    CompanionDuty::DutyPoint point;
    point.valid = true;
    point.mapId = bot->GetMapId();
    point.x = spot.x;
    point.y = spot.y;
    point.z = spot.z;
    point.spawnId = spot.spawnId;
    point.isGo = spot.isGo;
    point.key = spot.key;
    point.zone = spot.zoneId;
    return point;
}

// Nearest far spawn of a kind, as a point. Invalid when none.
CompanionDuty::DutyPoint FarPoint(Player* bot, uint32 low, DutyKind kind, float minDist)
{
    std::vector<CompanionBotMgr::DutySpot const*> spots = FarSpawns(bot, low, kind, minDist);
    return spots.empty() ? CompanionDuty::DutyPoint() : PointAt(bot, *spots.front());
}

// Far TRAIN candidate. The search runs only while a train duty is active, or when the bot's level is above the level
// of the last check (an arrival at a trainer, or a search that found nothing). Among the nearest FAR_TRAINER_CHECKS
// far trainers, the first that teaches a spell the bot can learn now at full price is the candidate.
CompanionDuty::DutyPoint FarTrainer(Player* bot, uint32 low, bool trainActive, float minDist)
{
    // Phase 2: the signature also changes when a profession reaches its rank cap or a secondary skill is missing, so
    // a level-capped companion still searches again.
    uint32 const level = CompanionCraft::TrainSignature(bot);
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        DutyState const& st = gStates[low];
        if (!trainActive && level == st.trainCheckLevel)
            return CompanionDuty::DutyPoint();
    }

    CompanionDuty::DutyPoint point;
    std::vector<CompanionBotMgr::DutySpot const*> spots = FarSpawns(bot, low, DutyKind::Trainer, minDist);
    for (size_t i = 0; i < spots.size() && i < FAR_TRAINER_CHECKS && !point.valid; ++i)
        if (TrainerCanTeach(bot, spots[i]->entry, 1.0f))   // full price: never cheaper than the real visit
            point = PointAt(bot, *spots[i]);
    // B12: class spells are learned only at a class trainer, which may lie beyond the nearest FAR_TRAINER_CHECKS
    // trainers: while one is learnable, every further class trainer of the map is checked too.
    if (!point.valid && CompanionCraft::AreaEnabled("Train") && CompanionCraft::ClassSpellsPending(bot) > 0)
        for (size_t i = FAR_TRAINER_CHECKS; i < spots.size() && !point.valid; ++i)
        {
            Trainer::Trainer const* trainer = sObjectMgr->GetTrainer(spots[i]->entry);
            if (trainer && trainer->GetTrainerType() == Trainer::Type::Class &&
                TrainerCanTeach(bot, spots[i]->entry, 1.0f))
                point = PointAt(bot, *spots[i]);
        }

    if (!point.valid && !trainActive)
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        gStates[low].trainCheckLevel = level;
    }
    return point;
}

// Records that the bot was at a trainer (or checked) at its current level, so a far TRAIN is not searched again
// until the level moves on.
void NoteTrainLevel(uint32 low, uint32 level)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    gStates[low].trainCheckLevel = level;
}

// First creature of the list (nearest first) that the lease lets the bot try. Null when none.
Creature* FirstAllowed(uint32 low, std::vector<Creature*> const& creatures)
{
    for (Creature* creature : creatures)
        if (CompanionLease::MayTry(low, CompanionLease::ObjectKey(creature)))
            return creature;
    return nullptr;
}

// Takes the lease "duty:<name>" for a candidate. A duty allows a node or corpse detour within Lease.NearYards, no
// quest and no new-rpg status change. A far duty also cools its spawn down on success, so the same spawn is not
// picked in a loop; so does a bank visit (a deposit that silently failed would otherwise re-pick the same bank after
// every snapshot refresh). force: urgent repair takes over another owner's commit window.
// Lease owner of a duty: "duty:<name>", except craft ("craft:<skill>") and fish ("fish"), which own their lease.
std::string LeaseOwnerOf(uint32 low, std::string const& duty)
{
    if (duty == "craft" || duty == "fish" || duty == "gather")
        return CompanionCraft::LeaseOwner(low, duty);
    return "duty:" + duty;
}

bool IsSelfRunDuty(std::string const& duty)
{
    return duty == "craft" || duty == "fish" || duty == "gather";
}

bool TakeLease(Player* bot, Candidate const& candidate, bool force)
{
    CompanionLease::Policy policy;
    policy.allowNodes = true;
    policy.allowCorpses = true;
    policy.nearYards = sPlayerbotAIConfig.companionLeaseNearYards;
    policy.cooldownOnSuccess =
        !IsSelfRunDuty(candidate.score.name) &&
        (candidate.point.valid || candidate.score.name == "bank" || candidate.score.name == "train");
    return CompanionLease::Begin(bot, LeaseOwnerOf(bot->GetGUID().GetCounter(), candidate.score.name), candidate.dest,
                                 0, policy, candidate.score.score, force);
}

// Candidate from a craft or fish offer: the first walk target (or the bot itself when the work starts here).
void FillOffer(Candidate& candidate, char const* name, CompanionCraft::Offer const& offer, float weight,
               float travelWeight, Player* bot)
{
    candidate.score = CompanionDuty::Score();
    candidate.score.name = name;
    candidate.score.need = offer.valid ? offer.need : 0.0f;
    candidate.valid = false;
    candidate.target = ObjectGuid();
    candidate.point = CompanionDuty::DutyPoint();
    if (!offer.valid || offer.need <= 0.0f)
        return;
    bool const walk = !offer.here && offer.first.valid;
    if (walk && offer.first.mapId != bot->GetMapId())
        return;
    candidate.score.travel =
        walk ? std::min(100.0f, bot->GetDistance(offer.first.x, offer.first.y, offer.first.z) / 20.0f) : 0.0f;
    // B6: a craft session pays its one trip with CraftSession.TravelFactor (1 for everything else).
    candidate.score.score = offer.need * weight - candidate.score.travel * travelWeight * offer.travelFactor;
    candidate.valid = true;
    candidate.dest.key = offer.key;
    candidate.dest.mapId = bot->GetMapId();
    candidate.dest.x = walk ? offer.first.x : bot->GetPositionX();
    candidate.dest.y = walk ? offer.first.y : bot->GetPositionY();
    candidate.dest.z = walk ? offer.first.z : bot->GetPositionZ();
}

// B13 hearthstone need. 80 when the switch is on, the companion owns no hearthstone (bags or bank) and an innkeeper
// bind may move its home: the home is on the bot's current map and not in Northrend (map 571: the owner sets the
// Dalaran home there with the "home" command, and it must stay). 0 otherwise.
constexpr float HEARTH_NEED = 80.0f;
constexpr uint32 MAP_NORTHREND = 571;

float HearthNeed(Player* bot)
{
    if (!CompanionCraft::AreaEnabled("Hearthstone") || bot->HasItemCount(CompanionBrain::ITEM_HEARTHSTONE, 1, true))
        return 0.0f;
    if (bot->m_homebindMapId != bot->GetMapId() || bot->m_homebindMapId == MAP_NORTHREND)
        return 0.0f;
    return HEARTH_NEED;
}

std::string Num(float value)
{
    std::ostringstream out;
    out << std::fixed << std::setprecision(1) << value;
    return out.str();
}

// Reports a duty's blocking condition for "why not <duty>".
void ReportDuty(uint32 low, Candidate const& candidate, std::string const& noNeed, std::string const& activeName,
                std::string const& refusedName, float activity, float margin)
{
    std::string const& name = candidate.score.name;
    std::string condition;
    if (name == activeName)
        condition = "running now";
    else if (candidate.score.need <= 0.0f)
        condition = noNeed;
    else if (!candidate.valid)
        condition = "need " + Num(candidate.score.need) +
                    " but no target in range or far (targets on cooldown, skipped this session, wrong level or team)";
    else if (name == refusedName)
        condition = "the lease refused it (destination on cooldown, or another activity's commit window)";
    else if (!activeName.empty())
        condition = "duty " + activeName + " is running; a switch needs a score above it + margin " + Num(margin) +
                    " (score " + Num(candidate.score.score) + ")";
    else
        condition = "score " + Num(candidate.score.score) + " (need " + Num(candidate.score.need) + ", travel " +
                    Num(candidate.score.travel) + ") <= activity " + Num(activity) + " + margin " + Num(margin);
    CompanionLease::ReportBlock(low, name, condition);
}

// Starts a duty from a candidate. Caller holds gStateMutex.
void Adopt(DutyState& st, Candidate const& candidate, uint32 now)
{
    st.active = true;
    st.dutyName = candidate.score.name;
    st.target = candidate.target;
    st.point = candidate.point;
    st.far = candidate.point.valid;
    st.startSec = now;
}

// Ends the duty without a cooldown (no walk was made, or the need is gone). Caller holds gStateMutex.
void EndDutyState(DutyState& st)
{
    st.active = false;
    st.dutyName.clear();
    st.target = ObjectGuid();
    st.point = CompanionDuty::DutyPoint();
    st.far = false;
}

std::string ScoreLine(CompanionDuty::Score const& score)
{
    std::ostringstream out;
    out << std::fixed << std::setprecision(1) << score.name << ": score " << score.score << " (need " << score.need
        << ", travel " << score.travel << ")";
    return out.str();
}
}  // namespace

void CompanionDuty::Evaluate(PlayerbotAI* botAI)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot)
        return;

    uint32 low = bot->GetGUID().GetCounter();
    uint32 now = NowSec();
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        DutyState& st = gStates[low];
        if (st.lastEvalSec && now - st.lastEvalSec < EVAL_SECONDS)
            return;
        st.lastEvalSec = now;
    }

    if (!Eligible(bot))
    {
        // Combat and death pause the duty: it is kept (no cooldown, no failure) and resumes afterwards.
        bool const outdoorCompanion = sCompanionBotMgr.IsCompanion(bot) && !sCompanionBotMgr.IsGroupedWithOwner(bot) &&
                                      bot->IsInWorld() && bot->GetMap() && !bot->GetMap()->Instanceable();
        if (outdoorCompanion && (!bot->IsAlive() || bot->IsInCombat()))
            return;
        CompleteDuty(low, "cleared: not eligible (grouped or instanced)", DutyEnd::Interrupted, "interrupt:instance");
        return;
    }

    bool const leaseOn = CompanionLease::Enabled();

    // Weights: per companion, clamped by CompanionBrain::Weight.
    float sellW = CompanionBrain::Weight(low, "sell", CompanionBrain::DutyDefault("sell"));
    float repairW = CompanionBrain::Weight(low, "repair", CompanionBrain::DutyDefault("repair"));
    float bankW = CompanionBrain::Weight(low, "bank", CompanionBrain::DutyDefault("bank"));
    float trainW = CompanionBrain::Weight(low, "train", CompanionBrain::DutyDefault("train"));
    float travelW = CompanionBrain::Weight(low, "travel", CompanionBrain::DutyDefault("travel"));
    float margin = CompanionBrain::Weight(low, "switch_margin", CompanionBrain::DutyDefault("switch_margin"));
    float activity = CompanionBrain::Weight(low, "activity", CompanionBrain::DutyDefault("activity"));

    // Bag need. Fullness: 0 at 50% used, 100 at 100%. Each SELL item adds 1 (cap 100). Each BANK item adds 5.
    std::vector<Item*> items;
    uint32 used = 0;
    uint32 total = 0;
    CountBags(bot, items, used, total);
    uint32 sellCount = 0;
    uint32 bankCount = 0;
    for (Item* item : items)
    {
        CompanionBrain::ItemClass cls = CompanionBrain::ClassifyItem(botAI, item);
        if (cls == CompanionBrain::ItemClass::SELL)
            ++sellCount;
        else if (cls == CompanionBrain::ItemClass::BANK)
            ++bankCount;
    }
    float fullPct = total ? 100.0f * used / total : 0.0f;
    float fullNeed = std::clamp((fullPct - 50.0f) * 2.0f, 0.0f, 100.0f);
    // A sell or bank need only exists when there is something to sell or bank; fullness alone never creates one.
    // Exception: a Hunter low on ammo (< 100) actively visits a vendor to restock bullets/arrows.
    float sellNeed = sellCount ? std::min(100.0f, fullNeed + sellCount) : 0.0f;
    if (bot->getClass() == CLASS_HUNTER && bot->GetMoney() >= 20)
    {
        uint32 ammoSub = 0;
        if (CountHunterAmmo(bot, ammoSub) < 100 && ammoSub != 0)
            sellNeed = std::max(sellNeed, 75.0f);
    }
    bool bankOk = BankReady(bot, low);
    float bankNeed = bankOk && bankCount ? std::min(100.0f, bankCount * 5.0f + fullNeed) : 0.0f;
    // If AutoOrganize is on and the guild bank is >= 85% full with purgeable clutter, boost bankNeed so a companion
    // visits the bank to pull junk and vendor it.
    if (bankOk && bankNeed < 75.0f && sPlayerbotAIConfig.companionBankAutoOrganize && bot->GetFreeInventorySpace() >= 3)
    {
        std::vector<CompanionDuty::BankTabUsage> bTabs;
        std::vector<CompanionDuty::BankSlot> bSlots;
        if (bot->GetGuildId() && CompanionDuty::BankSnapshot(bot->GetGuildId(), bTabs, bSlots) && !bTabs.empty())
        {
            uint32 bUsed = 0;
            uint32 bCap = static_cast<uint32>(bTabs.size()) * GUILD_BANK_MAX_SLOTS;
            for (CompanionDuty::BankTabUsage const& t : bTabs)
                bUsed += t.used;
            if (bCap > 0 && bUsed * 100 / bCap >= 85)
            {
                Guild* g = sGuildMgr->GetGuildById(bot->GetGuildId());
                for (CompanionDuty::BankSlot const& s : bSlots)
                {
                    if (!g || !g->MemberHasTabRights(bot->GetGUID(), s.tab, GUILD_BANK_RIGHT_VIEW_TAB))
                        continue;
                    ItemTemplate const* proto = sObjectMgr->GetItemTemplate(s.entry);
                    if (proto && proto->SellPrice > 0 && !CompanionBrain::IsBankWantedItem(proto))
                    {
                        bankNeed = 75.0f;
                        break;
                    }
                }
            }
        }
    }

    // Repair need: 0 above 60% durability, 100 at 0%. Durability is the LOWEST equipped item (not the average): under
    // URGENT_REPAIR_PCT the repair ignores the travel cost and wins over every other duty and the activity.
    float const durPct = DurabilityPct(bot);
    float repairNeed = std::clamp((60.0f - durPct) * 100.0f / 60.0f, 0.0f, 100.0f);
    bool const urgentRepair = durPct < URGENT_REPAIR_PCT;
    float const repairTravelW = urgentRepair ? 0.0f : travelW;

    // In range first (the normal search). A spawn further away is a far point: the duty walks there on the same map.
    // Targets the lease put on cooldown or skipped for the session are left out.
    float range = CompanionBrain::SearchRange();
    Candidate sell;
    Candidate repair;
    Candidate bank;
    Candidate train;

    if (sellNeed > 0.0f)
    {
        std::vector<Creature*> vendors;
        CollectNpcs(bot, {UNIT_NPC_FLAG_VENDOR}, range, vendors);
        if (Creature* vendor = FirstAllowed(low, vendors))
            Fill(sell, "sell", sellNeed, sellW, travelW, vendor, bot);
        else
            FillFar(sell, "sell", sellNeed, sellW, travelW, FarPoint(bot, low, DutyKind::Vendor, range), bot);
    }
    else
        Fill(sell, "sell", 0.0f, sellW, travelW, nullptr, bot);

    if (repairNeed > 0.0f)
    {
        std::vector<Creature*> repairers;
        CollectNpcs(bot, {UNIT_NPC_FLAG_REPAIR}, range, repairers);
        if (Creature* repairer = FirstAllowed(low, repairers))
            Fill(repair, "repair", repairNeed, repairW, repairTravelW, repairer, bot);
        else
            FillFar(repair, "repair", repairNeed, repairW, repairTravelW, FarPoint(bot, low, DutyKind::Repair, range),
                    bot);
    }
    else
        Fill(repair, "repair", 0.0f, repairW, travelW, nullptr, bot);

    if (bankNeed > 0.0f)
    {
        GameObject* bankObject = NearestGuildBank(bot, range);
        if (bankObject && CompanionLease::MayTry(low, CompanionLease::ObjectKey(bankObject)))
            Fill(bank, "bank", bankNeed, bankW, travelW, bankObject, bot);
        else
            FillFar(bank, "bank", bankNeed, bankW, travelW, FarPoint(bot, low, DutyKind::Bank, range), bot);
    }
    else
        Fill(bank, "bank", 0.0f, bankW, travelW, nullptr, bot);

    bool trainActive = false;
    bool craftActive = false;
    bool fishActive = false;
    bool gatherActive = false;
    uint32 trainCheckLevel = 0;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        DutyState const& st = gStates[low];
        trainActive = st.active && st.dutyName == "train";
        craftActive = st.active && st.dutyName == "craft";
        fishActive = st.active && st.dutyName == "fish";
        gatherActive = st.active && st.dutyName == "gather";
        trainCheckLevel = st.trainCheckLevel;
    }

    // Train need: 100 when one of the nearest three trainers in range teaches something this bot can learn now.
    // Otherwise a far trainer (see FarTrainer) is the candidate.
    std::vector<Creature*> trainers;
    CollectNpcs(bot, {UNIT_NPC_FLAG_TRAINER, UNIT_NPC_FLAG_TRAINER_CLASS, UNIT_NPC_FLAG_TRAINER_PROFESSION}, range,
                trainers);
    Creature* pick = nullptr;
    for (size_t i = 0; i < trainers.size() && i < NEAR_TRAINER_CHECKS && !pick; ++i)
        if (CompanionLease::MayTry(low, CompanionLease::ObjectKey(trainers[i])) &&
            TrainerCanTeach(bot, trainers[i]->GetEntry(), bot->GetReputationPriceDiscount(trainers[i])))
            pick = trainers[i];
    if (pick)
        Fill(train, "train", 100.0f, trainW, travelW, pick, bot);
    else
    {
        CompanionDuty::DutyPoint farTrainer = FarTrainer(bot, low, trainActive, range);
        if (farTrainer.valid)
            FillFar(train, "train", 100.0f, trainW, travelW, farTrainer, bot);
        else
            Fill(train, "train", 0.0f, trainW, travelW, nullptr, bot);
    }

    // B13: HEARTH. The legitimate source of a hearthstone is the innkeeper bind (CMSG_BINDER_ACTIVATE ->
    // WorldSession::SendBindPoint -> spell 3286 Bind: SPELL_EFFECT_BIND + SPELL_EFFECT_CREATE_ITEM 6948).
    Candidate hearth;
    float const hearthW = CompanionBrain::Weight(low, "hearth", CompanionBrain::DutyDefault("hearth"));
    float const hearthNeed = HearthNeed(bot);
    if (hearthNeed > 0.0f)
    {
        std::vector<Creature*> innkeepers;
        CollectNpcs(bot, {UNIT_NPC_FLAG_INNKEEPER}, range, innkeepers);
        if (Creature* innkeeper = FirstAllowed(low, innkeepers))
            Fill(hearth, "hearth", hearthNeed, hearthW, travelW, innkeeper, bot);
        else
            FillFar(hearth, "hearth", hearthNeed, hearthW, travelW, FarPoint(bot, low, DutyKind::Innkeeper, range),
                    bot);
    }
    else
        Fill(hearth, "hearth", 0.0f, hearthW, travelW, nullptr, bot);

    // Phase 2: CRAFT, FISH and active GATHER-FARM (CompanionCraft). Their runner action does the walking and the work.
    float const craftW = CompanionBrain::Weight(low, "craft", CompanionBrain::DutyDefault("craft"));
    float const fishW = CompanionBrain::Weight(low, "fish", CompanionBrain::DutyDefault("fish"));
    float const gatherW = CompanionBrain::Weight(low, "gather", CompanionBrain::DutyDefault("gather"));
    uint32 const d0 = getMSTime();
    CompanionCraft::Offer const craftOffer = CompanionCraft::EvaluateCraft(bot, craftActive);
    uint32 const d1 = getMSTime();
    CompanionCraft::Offer const fishOffer = CompanionCraft::EvaluateFish(bot, fishActive);
    uint32 const d2 = getMSTime();
    CompanionCraft::Offer const gatherOffer = CompanionCraft::EvaluateGatherFarm(bot, gatherActive);
    uint32 const d3 = getMSTime();
    if (getMSTimeDiff(d0, d3) >= 200)
        LOG_WARN("playerbots", "[SlowDuty] {} craft={}ms fish={}ms gather={}ms", bot->GetName(),
                 getMSTimeDiff(d0, d1), getMSTimeDiff(d1, d2), getMSTimeDiff(d2, d3));
    Candidate craft;
    Candidate fish;
    Candidate gather;
    FillOffer(craft, "craft", craftOffer, craftW, travelW, bot);
    FillOffer(fish, "fish", fishOffer, fishW, travelW, bot);
    FillOffer(gather, "gather", gatherOffer, gatherW, travelW, bot);

    Candidate* duties[] = {&sell, &repair, &bank, &train, &craft, &fish, &gather, &hearth};
    Candidate* best = nullptr;
    std::vector<Score> scores;
    // Every duty is listed, valid or not (an invalid one has need 0 and travel 0), so "why" shows the full picture.
    // Only valid duties can be chosen.
    for (Candidate* candidate : duties)
    {
        scores.push_back(candidate->score);
        if (candidate->valid && (!best || candidate->score.score > best->score.score))
            best = candidate;
    }
    // Urgent repair always wins: no travel term, no margin, and it may take over a committed lease.
    Candidate* urgent = urgentRepair && repair.valid ? &repair : nullptr;
    if (urgent)
        best = urgent;
    Score activityScore;
    activityScore.name = "activity";
    activityScore.score = activity;
    scores.push_back(activityScore);
    std::sort(scores.begin(), scores.end(), [](Score const& a, Score const& b) { return a.score > b.score; });
    if (scores.size() > 3)
        scores.resize(3);

    // Start, keep or switch. A duty only starts (or replaces the active one) when it beats the current score by the
    // switch margin, and (lease on) when the lease takes it. The lease refuses a destination on cooldown or skipped
    // for the session, and a switch inside another owner's commit window.
    std::string text;
    std::string logLine;
    std::string activeName;
    std::string refusedName;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        DutyState& st = gStates[low];
        if (!st.active)
        {
            if (best && (best == urgent || best->score.score > activity + margin))
            {
                if (!leaseOn || TakeLease(bot, *best, best == urgent))
                {
                    Adopt(st, *best, now);
                    text = "start " + st.dutyName + (st.far ? " (far)" : "") + (best == urgent ? " (urgent)" : "");
                }
                else
                {
                    refusedName = best->score.name;
                    text = "refused " + refusedName + " (lease)";
                }
            }
            else
                text = "keep activity";
        }
        else
        {
            Candidate* current = nullptr;
            for (Candidate* candidate : duties)
                if (candidate->score.name == st.dutyName)
                    current = candidate;

            if (leaseOn && !CompanionLease::Holds(low, LeaseOwnerOf(low, st.dutyName)))
            {
                // The lease ended it (stall, timeout, owner command): the lease line says why.
                text = "end " + st.dutyName + " (lease ended)";
                EndDutyState(st);
            }
            else if (!current || !current->valid)
            {
                text = "end " + st.dutyName + " (need gone or no target)";
                if (leaseOn)
                    CompanionLease::End(low, LeaseOwnerOf(low, st.dutyName), "need-gone",
                                        CompanionLease::Outcome::Neutral);
                EndDutyState(st);
            }
            else if (best && best != current &&
                     (best == urgent || best->score.score > current->score.score + margin))
            {
                if (!leaseOn || TakeLease(bot, *best, best == urgent))
                {
                    Adopt(st, *best, now);
                    text = "switch to " + st.dutyName + (st.far ? " (far)" : "") + (best == urgent ? " (urgent)" : "");
                }
                else
                {
                    refusedName = best->score.name;
                    text = "keep " + st.dutyName + " (lease refused " + refusedName + ")";
                }
            }
            else if (!leaseOn)
            {
                st.target = current->target;
                if (current->point.valid)
                    st.point = current->point;
                text = "keep " + st.dutyName;
            }
            else
                text = "keep " + st.dutyName;   // lease on: the destination stays fixed while the lease runs
        }

        if (st.active)
            activeName = st.dutyName;
        st.top = scores;
        st.ring.push_back(Choice{now, text, scores});
        while (st.ring.size() > RING_SIZE)
            st.ring.pop_front();
        logLine = text;
    }

    if (logLine.rfind("keep", 0) != 0 || logLine.find("refused") != std::string::npos)
        LOG_INFO("playerbots", "[Companion] {} duty decision: {}", bot->GetName(), logLine);

    // "why not <duty>": the blocking condition of each duty, from this evaluation.
    std::string const bankNoNeed = bankOk ? std::string("nothing to bank (no BANK-class item in the bags)")
                                          : std::string("guild bank not ready (no guild, no snapshot yet, no tab with "
                                                        "deposit rights, or every such tab full)");
    std::string const sellNoNeed = "nothing to sell (no SELL-class item in the bags)";
    std::string const repairNoNeed = "durability fine (lowest equipped item " + Num(durPct) + "%, repair below 60%)";
    std::string trainNoNeed =
        "no trainer teaches a spell the bot can learn and afford now (far trainers checked at level " +
        std::to_string(trainCheckLevel) + ")";
    for (std::string const& line : CompanionCraft::RankBlockedLines(bot))   // B8: waits for a skill rank
        trainNoNeed += "; " + line;
    ReportDuty(low, sell, sellNoNeed, activeName, refusedName, activity, margin);
    ReportDuty(low, repair, repairNoNeed, activeName, refusedName, activity, margin);
    ReportDuty(low, bank, bankNoNeed, activeName, refusedName, activity, margin);
    ReportDuty(low, train, trainNoNeed, activeName, refusedName, activity, margin);
    ReportDuty(low, craft, craftOffer.reason.empty() ? std::string("no need") : craftOffer.reason, activeName,
               refusedName, activity, margin);
    ReportDuty(low, fish, fishOffer.reason.empty() ? std::string("no need") : fishOffer.reason, activeName,
               refusedName, activity, margin);
    ReportDuty(low, hearth,
               "has a hearthstone, switch off, or the home is on another map or in Northrend (owner's Dalaran home)",
               activeName, refusedName, activity, margin);
    if (logLine.rfind("start craft", 0) == 0 || logLine.rfind("switch to craft", 0) == 0)
        LOG_INFO("playerbots", "Companion {}: craft plan {} score={:.1f}", bot->GetName(), craftOffer.summary,
                 craft.score.score);
    if (logLine.rfind("start fish", 0) == 0 || logLine.rfind("switch to fish", 0) == 0)
        LOG_INFO("playerbots", "Companion {}: fish plan {} score={:.1f}", bot->GetName(), fishOffer.summary,
                 fish.score.score);
}

std::string CompanionDuty::ActiveName(uint32 lowGuid)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gStates.find(lowGuid);
    return it != gStates.end() && it->second.active ? it->second.dutyName : std::string();
}

void CompanionDuty::EvaluateSoon(uint32 lowGuid)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    gStates[lowGuid].lastEvalSec = 0;
}

bool CompanionDuty::HasActiveDuty(uint32 lowGuid)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gStates.find(lowGuid);
    return it != gStates.end() && it->second.active;
}

bool CompanionDuty::ActiveTarget(uint32 lowGuid, std::string& name, ObjectGuid& target, DutyPoint& point)
{
    bool const leaseOn = CompanionLease::Enabled();
    std::string ended;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gStates.find(lowGuid);
        if (it == gStates.end() || !it->second.active)
            return false;
        DutyState& st = it->second;
        // Lease on: the lease's stall and time limits decide; the duty ends once its lease is gone. Lease off: the
        // legacy timeouts.
        bool const keep = leaseOn ? CompanionLease::Holds(lowGuid, LeaseOwnerOf(lowGuid, st.dutyName))
                                  : NowSec() - st.startSec <= (st.far ? FAR_TIMEOUT_SECONDS : TIMEOUT_SECONDS);
        if (keep)
        {
            name = st.dutyName;
            target = st.target;
            point = st.point;
            return true;
        }
        ended = st.dutyName;
    }
    if (leaseOn)
        CompleteDuty(lowGuid, "ended " + ended + " (lease ended)", DutyEnd::Interrupted, "lease-gone");
    else
        CompleteDuty(lowGuid, "failed " + ended + " (timeout)", DutyEnd::Failed, "timeout");
    return false;
}

void CompanionDuty::BindTarget(uint32 lowGuid, ObjectGuid target)
{
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gStates.find(lowGuid);
    if (it != gStates.end() && it->second.active)
        it->second.target = target;
}

void CompanionDuty::CompleteDuty(uint32 lowGuid, std::string const& choice, DutyEnd how, char const* reason)
{
    bool const leaseOn = CompanionLease::Enabled();
    std::string owner;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gStates.find(lowGuid);
        if (it == gStates.end() || !it->second.active)
            return;
        DutyState& st = it->second;
        uint32 const now = NowSec();
        // Lease off (legacy): a far spawn is not walked to again for FAR_COOLDOWN_SECONDS after a finished or failed
        // duty, so the same nearest spawn is not picked every evaluation. Lease on: the lease applies its own
        // cooldown below. An interrupted duty never cools its spawn down.
        if (!leaseOn && how != DutyEnd::Interrupted && st.far && st.point.valid)
            st.cooldownUntil[st.point.key] = now + FAR_COOLDOWN_SECONDS;
        owner = LeaseOwnerOf(lowGuid, st.dutyName);
        EndDutyState(st);
        st.ring.push_back(Choice{now, choice, st.top});
        while (st.ring.size() > RING_SIZE)
            st.ring.pop_front();
        LOG_INFO("playerbots", "[Companion] duty ended for guid {}: {}", lowGuid, choice);
    }

    if (leaseOn)
    {
        CompanionLease::Outcome const outcome = how == DutyEnd::Done     ? CompanionLease::Outcome::Success
                                                : how == DutyEnd::Failed ? CompanionLease::Outcome::Failure
                                                                         : CompanionLease::Outcome::Neutral;
        CompanionLease::End(lowGuid, owner, reason, outcome);
    }
}

bool CompanionDuty::PerformDuty(PlayerbotAI* botAI, std::string const& name, WorldObject* target)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot || !target)
        return false;

    if (name == "sell")
    {
        Creature* vendor = target->ToCreature();
        if (!vendor)
            return false;
        uint32 const unsold = SellAtVendor(bot, botAI, vendor);
        TryBuyStarterBags(bot, vendor);
        TryBuyHunterAmmo(bot, vendor);
        // A vendor that also repairs: repair in the same visit.
        if (vendor->HasNpcFlag(UNIT_NPC_FLAG_REPAIR))
            RepairAt(bot, vendor);
        // Items left unsold keep the sell need up: report a failure, so the lease cools this vendor down and its
        // retry budget applies (no re-sell loop at the same vendor).
        return unsold == 0;
    }
    if (name == "repair")
    {
        Creature* npc = target->ToCreature();
        return npc && RepairAt(bot, npc);
    }
    if (name == "train")
    {
        Creature* trainer = target->ToCreature();
        if (!trainer)
            return false;
        TrainerAction(botAI).LearnAt(trainer);
        // Recorded whatever was learned, so a trainer visit is not repeated until the level moves on.
        NoteTrainLevel(bot->GetGUID().GetCounter(), CompanionCraft::TrainSignature(bot));
        return true;
    }
    if (name == "bank")
    {
        GameObject* bank = target->ToGameObject();
        return bank && GuildBankAction(botAI).RunCompanionBankDuty(bank);
    }
    if (name == "hearth")
    {
        // Same path as a player's "Make this inn your home" (WorldSession::HandleBinderActivateOpcode): the interact
        // check, then SendBindPoint, where the innkeeper casts spell 3286 (bind + create item 6948) on the bot.
        Creature* innkeeper = target->ToCreature();
        if (!innkeeper || !bot->GetNPCIfCanInteractWith(innkeeper->GetGUID(), UNIT_NPC_FLAG_INNKEEPER) ||
            bot->GetMap()->Instanceable())
            return false;
        uint32 const oldMap = bot->m_homebindMapId;
        uint32 const oldArea = bot->m_homebindAreaId;
        bot->GetSession()->SendBindPoint(innkeeper);
        bool const got = bot->HasItemCount(CompanionBrain::ITEM_HEARTHSTONE, 1, true);
        LOG_INFO("playerbots",
                 "Companion {}: hearthstone got={} via=bind npc={} home old=map{}:area{} new=map{}:area{}",
                 bot->GetName(), got ? 1 : 0, innkeeper->GetEntry(), oldMap, oldArea, bot->m_homebindMapId,
                 bot->m_homebindAreaId);
        return got;
    }
    return false;
}

WorldObject* CompanionDuty::FindSpawnNear(PlayerbotAI* botAI, DutyPoint const& point)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot || !point.valid)
        return nullptr;

    if (point.isGo)
    {
        std::list<GameObject*> targets;
        AnyGameObjectInObjectRangeCheck check(bot, FAR_SEARCH_YARDS);
        Acore::GameObjectListSearcher<AnyGameObjectInObjectRangeCheck> searcher(bot, targets, check);
        Cell::VisitObjects(bot, searcher, FAR_SEARCH_YARDS);

        GameObject* best = nullptr;
        float bestDist = 0.0f;
        for (GameObject* go : targets)
        {
            if (go->GetSpawnId() != point.spawnId || go->GetGoType() != GAMEOBJECT_TYPE_GUILD_BANK)
                continue;
            float dist = bot->GetDistance(go);
            if (!best || dist < bestDist)
            {
                best = go;
                bestDist = dist;
            }
        }
        return best;
    }

    std::list<Unit*> units;
    Acore::AnyUnitInObjectRangeCheck check(bot, FAR_SEARCH_YARDS);
    Acore::UnitListSearcher<Acore::AnyUnitInObjectRangeCheck> searcher(bot, units, check);
    Cell::VisitObjects(bot, searcher, FAR_SEARCH_YARDS);

    Creature* best = nullptr;
    float bestDist = 0.0f;
    for (Unit* unit : units)
    {
        Creature* creature = unit->ToCreature();
        if (!creature || creature->GetSpawnId() != point.spawnId || !creature->IsAlive())
            continue;
        float dist = bot->GetDistance(creature);
        if (!best || dist < bestDist)
        {
            best = creature;
            bestDist = dist;
        }
    }
    return best;
}

std::vector<std::string> CompanionDuty::WhyLines(uint32 lowGuid)
{
    std::vector<std::string> lines;
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gStates.find(lowGuid);
    if (it == gStates.end())
    {
        lines.push_back("Activity: questing and rpg (not scored yet).");
        lines.push_back(CompanionLease::StatusLine(lowGuid));
        return lines;
    }

    DutyState const& st = it->second;
    lines.push_back(st.active ? "Activity: duty " + st.dutyName + (st.far ? " (far)" : "")
                              : std::string("Activity: questing and rpg (no duty)."));
    if (st.top.empty())
        lines.push_back("Scores: not scored yet (every 30 s, autonomy only).");
    for (Score const& score : st.top)
        lines.push_back(ScoreLine(score));

    uint32 now = NowSec();
    size_t shown = 0;
    for (auto ring = st.ring.rbegin(); ring != st.ring.rend() && shown < 2; ++ring, ++shown)
    {
        std::ostringstream out;
        out << (shown == 0 ? "Last choice: " : "Previous choice: ") << ring->text << " ("
            << (now > ring->atSec ? now - ring->atSec : 0) << "s ago)";
        lines.push_back(out.str());
    }
    if (shown == 0)
        lines.push_back("Choices: none yet.");
    lines.push_back(CompanionLease::StatusLine(lowGuid));
    return lines;
}

void CompanionDuty::RecordLedger(uint32 lowGuid, char const* action, uint32 item, uint32 count, int tab,
                                 char const* note)
{
    // Asynchronous write: safe on map threads. Every value is a literal or an integer, so nothing needs escaping.
    std::string tabText = tab < 0 ? std::string("NULL") : std::to_string(tab);
    CharacterDatabase.Execute(
        "INSERT INTO companion_ledger (guid, action, item, count, tab, note) VALUES ({}, '{}', {}, {}, {}, '{}')",
        lowGuid, action, item, count, tabText, note);
}

void CompanionDuty::ReplyToOwner(PlayerbotAI* botAI, std::vector<std::string> const& lines)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot)
        return;

    // Owner commands always get a reply: party chat when grouped, otherwise a whisper (not rate limited).
    if (sCompanionBotMgr.IsGroupedWithOwner(bot))
    {
        for (std::string const& line : lines)
            botAI->SayToParty(line);
        return;
    }
    Player* owner = sCompanionBotMgr.FindOwnerOnline();
    if (!owner)
        return;
    for (std::string const& line : lines)
    {
        WorldPacket data;
        ChatHandler::BuildChatPacket(data, CHAT_MSG_WHISPER, LANG_UNIVERSAL, bot, nullptr, line);
        owner->SendDirectMessage(&data);
    }
}

void CompanionDuty::UpdateWorld(uint32 elapsed)
{
    CompanionCraft::LoadWorldData();   // B8: milling / prospecting loot tables, once (world thread)
    gBankTimerMs += elapsed;
    // An invalidated snapshot (after a withdraw or deposit) is rebuilt on the next world tick, so a craft
    // session's next withdraw does not find it missing.
    if (gBankTimerMs < BANK_REFRESH_SECONDS * 1000 && !gBankRefreshNow.exchange(false))
        return;
    gBankTimerMs = 0;

    std::set<uint32> guilds;
    for (std::string const& name : sCompanionBotMgr.CompanionNames())
    {
        Player* companion = ObjectAccessor::FindPlayerByName(name);
        if (companion && companion->GetGuildId())
            guilds.insert(companion->GetGuildId());
    }

    for (uint32 guildId : guilds)
    {
        GuildBankSnapshot snapshot;

        // Every purchased tab, so a full tab 0 does not hide the free ones.
        QueryResult tabRows =
            CharacterDatabase.Query("SELECT TabId FROM guild_bank_tab WHERE guildid = {} ORDER BY TabId", guildId);
        if (tabRows)
        {
            do
            {
                CompanionDuty::BankTabUsage tab;
                tab.tab = static_cast<uint8>(tabRows->Fetch()[0].Get<uint32>());
                snapshot.tabs.push_back(tab);
            } while (tabRows->NextRow());
        }

        QueryResult items = CharacterDatabase.Query(
            "SELECT gbi.TabId, gbi.SlotId, ii.itemEntry, ii.count FROM guild_bank_item gbi "
            "JOIN item_instance ii ON ii.guid = gbi.item_guid "
            "WHERE gbi.guildid = {}",
            guildId);
        if (items)
        {
            do
            {
                Field* fields = items->Fetch();
                CompanionDuty::BankSlot slot;
                slot.tab = static_cast<uint8>(fields[0].Get<uint32>());
                slot.slot = static_cast<uint8>(fields[1].Get<uint32>());
                slot.entry = fields[2].Get<uint32>();
                slot.count = fields[3].Get<uint32>();
                for (CompanionDuty::BankTabUsage& tab : snapshot.tabs)
                {
                    if (tab.tab == slot.tab)
                    {
                        ++tab.used;
                        break;
                    }
                }
                snapshot.slots.push_back(slot);
            } while (items->NextRow());
        }

        std::lock_guard<std::mutex> lock(gBankMutex);
        gBanks[guildId] = snapshot;
    }
}

bool CompanionDuty::BankSnapshot(uint32 guildId, std::vector<BankTabUsage>& tabs, std::vector<BankSlot>& slots)
{
    std::lock_guard<std::mutex> lock(gBankMutex);
    auto it = gBanks.find(guildId);
    if (it == gBanks.end())
        return false;
    tabs = it->second.tabs;
    slots = it->second.slots;
    return true;
}

uint64 CompanionDuty::BankStock(uint32 guildId, uint32 entry)
{
    std::lock_guard<std::mutex> lock(gBankMutex);
    auto it = gBanks.find(guildId);
    if (it == gBanks.end())
        return 0;
    uint64 count = 0;
    for (BankSlot const& slot : it->second.slots)
        if (slot.entry == entry)
            count += slot.count;
    return count;
}

void CompanionDuty::InvalidateBank(uint32 guildId)
{
    {
        std::lock_guard<std::mutex> lock(gBankMutex);
        gBanks.erase(guildId);
    }
    gBankRefreshNow.store(true);
}
