/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "CompanionCraftStrategy.h"

#include <algorithm>
#include <atomic>
#include <list>
#include <mutex>
#include <unordered_map>
#include <set>
#include <sstream>

#include "Action.h"
#include "Bag.h"
#include "DBCStores.h"
#include "LootMgr.h"
#include "CellImpl.h"
#include "CompanionBotMgr.h"
#include "CompanionCraft.h"
#include "CompanionDuty.h"
#include "CompanionLease.h"
#include "CompanionLife.h"
#include "CompanionQuest.h"
#include "CompanionRoute.h"
#include "CompanionTrip.h"
#include "Creature.h"
#include "Event.h"
#include "FishingAction.h"
#include "GameObject.h"
#include "GameTime.h"
#include "GridNotifiers.h"
#include "GridNotifiersImpl.h"
#include "GuildBankAction.h"
#include "Item.h"
#include "ItemPackets.h"
#include "ItemTemplate.h"
#include "Log.h"
#include "LootObjectStack.h"
#include "MovementActions.h"
#include "NearestGameObjects.h"
#include "ObjectMgr.h"
#include "Playerbots.h"
#include "PlayerbotAI.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include "Timer.h"
#include "Trigger.h"
#include "WorldPacket.h"
#include "WorldSession.h"

namespace
{
constexpr float STEP_PROGRESS = 10000.0f;   // lease progress per step
constexpr float CRAFT_PROGRESS = 10.0f;     // lease progress per craft (= the lease's progress step)
constexpr uint32 MAX_CRAFT_FAILURES = 3;    // casts that made nothing before the batch ends as a failure
constexpr float FOCUS_SEARCH_YARDS = 40.0f;
constexpr float POOL_STAND_YARDS = 30.0f;   // walk until this close to a fishing pool spawn, then look for a shore
constexpr uint32 WATER_CHECK_MS = 2000;
constexpr uint32 MAX_WATER_CHECKS = 15;
constexpr uint32 CAMPFIRE_RETRY_SECONDS = 10;
constexpr uint32 TARGET_STACK = 5;           // Milling / Prospecting take 5 of one stack
constexpr uint32 MIN_FREE_SLOTS_TARGET = 4;  // free slots kept for the results (unlooted results are lost)
constexpr uint32 TARGET_WAIT_SECONDS = 5;    // after an item-targeted cast: wait this long for the stack to drop
constexpr uint32 MAX_SESSION_FAILURES = 3;   // recipes in a row that made nothing before the session ends
// Skills whose values the session end line reports.
constexpr uint32 SESSION_SKILLS[] = {SKILL_ALCHEMY,     SKILL_BLACKSMITHING,  SKILL_ENGINEERING, SKILL_INSCRIPTION,
                                     SKILL_JEWELCRAFTING, SKILL_LEATHERWORKING, SKILL_MINING,     SKILL_TAILORING,
                                     SKILL_COOKING,     SKILL_FIRST_AID};

uint32 NowSec()
{
    return static_cast<uint32>(GameTime::GetGameTime().count());
}

bool IsPole(Item* item)
{
    ItemTemplate const* proto = item ? item->GetTemplate() : nullptr;
    return proto && proto->Class == ITEM_CLASS_WEAPON && proto->SubClass == ITEM_SUBCLASS_WEAPON_FISHING_POLE;
}

// Nearest spawned spell focus object with focusId within range.
GameObject* FindFocusNear(Player* bot, uint32 focusId, float range)
{
    std::list<GameObject*> gos;
    AnyGameObjectInObjectRangeCheck check(bot, range);
    Acore::GameObjectListSearcher<AnyGameObjectInObjectRangeCheck> searcher(bot, gos, check);
    Cell::VisitObjects(bot, searcher, range);
    GameObject* best = nullptr;
    float bestDist = 0.0f;
    for (GameObject* go : gos)
    {
        GameObjectTemplate const* info = go ? go->GetGOInfo() : nullptr;
        if (!info || info->type != GAMEOBJECT_TYPE_SPELL_FOCUS || info->spellFocus.focusId != focusId)
            continue;
        float const dist = bot->GetDistance(go);
        if (!best || dist < bestDist)
        {
            best = go;
            bestDist = dist;
        }
    }
    return best;
}

// A spawned fishing pool within POOL_STAND_YARDS + 10 of the bot.
bool PoolNear(Player* bot)
{
    float const range = POOL_STAND_YARDS + 10.0f;
    std::list<GameObject*> gos;
    AnyGameObjectInObjectRangeCheck check(bot, range);
    Acore::GameObjectListSearcher<AnyGameObjectInObjectRangeCheck> searcher(bot, gos, check);
    Cell::VisitObjects(bot, searcher, range);
    for (GameObject* go : gos)
        if (go && go->GetGoType() == GAMEOBJECT_TYPE_FISHINGHOLE)
            return true;
    return false;
}

// Distance within which a focus object counts for a cast (its spellFocus.dist), at least 2 yards.
float FocusReach(GameObject* go)
{
    GameObjectTemplate const* info = go->GetGOInfo();
    float const dist = info && info->spellFocus.dist ? static_cast<float>(info->spellFocus.dist) : 3.0f;
    return std::max(2.0f, dist - 1.0f);
}

class CompanionCraftActiveTrigger : public Trigger
{
public:
    CompanionCraftActiveTrigger(PlayerbotAI* botAI) : Trigger(botAI, "companion craft active") {}

    bool IsActive() override
    {
        Player* bot = botAI->GetBot();
        if (!bot || !bot->IsAlive() || !botAI->IsCompanion())
            return false;
        bool const avoiding = CompanionTrip::AvoidActive(botAI);
        if (bot->IsInCombat() && !avoiding)
            return false;
        std::string const name = CompanionDuty::ActiveName(bot->GetGUID().GetCounter());
        return name == "craft" || name == "fish" || name == "gather";
    }
};

class CompanionCraftAction : public MovementAction
{
public:
    CompanionCraftAction(PlayerbotAI* botAI) : MovementAction(botAI, "companion craft") {}

    bool Execute(Event /*event*/) override
    {
        Player* bot = botAI->GetBot();
        if (!bot || !bot->IsAlive())
            return false;
        bool const avoiding = CompanionTrip::AvoidActive(botAI);
        if (bot->IsInCombat() && !avoiding)
            return false;
        uint32 const low = bot->GetGUID().GetCounter();
        std::string const duty = CompanionDuty::ActiveName(low);
        if (duty != "craft" && duty != "fish" && duty != "gather")
            return false;

        CompanionCraft::Run run;
        if (!CompanionCraft::GetRun(low, duty, run))
        {
            Finish(low, duty, false, "fail:no-plan", "no plan");
            return false;
        }
        // The lease was ended for us (stall, timeout, owner command): ActiveTarget ends the duty.
        std::string name;
        ObjectGuid target;
        CompanionDuty::DutyPoint point;
        if (!CompanionDuty::ActiveTarget(low, name, target, point))
        {
            if (duty == "fish")
                CompanionRestoreWeapons(botAI);
            if (run.session && run.started)
                LogSessionEnd(bot, run, "lease-gone", "the lease ended the session");
            CompanionCraft::ClearRun(low, duty);
            return false;
        }

        bool const keep = duty == "fish"     ? RunFish(bot, low, run)
                          : duty == "gather" ? RunGatherFarm(bot, low, run)
                          : run.session      ? RunSession(bot, low, run)
                                             : RunCraft(bot, low, run);
        if (run.valid)
            CompanionCraft::SaveRun(low, run);
        return keep;
    }

private:
    // Ends the duty and the run. success: lease success; else failure (cooldown and retry budget on the destination).
    void Finish(uint32 low, std::string const& duty, bool success, char const* reason, std::string const& text)
    {
        CompanionDuty::CompleteDuty(low, (success ? "done " : "failed ") + duty + " (" + text + ")",
                                    success ? CompanionDuty::DutyEnd::Done : CompanionDuty::DutyEnd::Failed, reason);
        CompanionCraft::ClearRun(low, duty);
    }

    void Progress(uint32 low, CompanionCraft::Run const& run, float distance)
    {
        float const done = (run.crafted + run.casts) * CRAFT_PROGRESS;
        CompanionLease::Progress(low, run.step * STEP_PROGRESS + done - distance);
    }

    // Walks to a spawn (bank, vendor) and finds it. 1: in interaction range (found set), 0: walking, -1: failed.
    int ApproachSpawn(Player* bot, uint32 low, CompanionCraft::Run const& run, CompanionCraft::Place const& place,
                      WorldObject*& found)
    {
        found = nullptr;
        if (!place.valid || place.mapId != bot->GetMapId())
            return -1;
        float const dist = bot->GetDistance(place.x, place.y, place.z);
        if (dist > CompanionDuty::FAR_SEARCH_YARDS)
        {
            Progress(low, run, dist);
            if (!bot->IsInCombat() && CompanionQuest::TryMount(botAI, dist))
                return 0;
            float wx = place.x, wy = place.y, wz = place.z;
            CompanionRoute::NextPoint(bot, place.mapId, place.x, place.y, place.z, wx, wy, wz);
            MoveTo(place.mapId, wx, wy, wz);
            return 0;
        }
        CompanionDuty::DutyPoint point;
        point.valid = true;
        point.mapId = place.mapId;
        point.x = place.x;
        point.y = place.y;
        point.z = place.z;
        point.spawnId = place.spawnId;
        point.isGo = place.isGo;
        WorldObject* object = CompanionDuty::FindSpawnNear(botAI, point);
        if (!object)
        {
            if (dist > CompanionDuty::FAR_ARRIVE_YARDS)
            {
                Progress(low, run, dist);
                MoveTo(place.mapId, place.x, place.y, place.z);
                return 0;
            }
            return -1;
        }
        float const objectDist = bot->GetDistance(object);
        Progress(low, run, objectDist);
        if (objectDist > INTERACTION_DISTANCE)
        {
            MoveTo(object, INTERACTION_DISTANCE - 2.0f);
            return 0;
        }
        found = object;
        return 1;
    }

    // Buys count items of entry at the vendor, in lots of BuyCount, at most one stack per purchase. Returns the
    // items bought.
    uint32 Buy(Player* bot, Creature* vendor, uint32 entry, uint32 count)
    {
        ItemTemplate const* proto = sObjectMgr->GetItemTemplate(entry);
        int32 const slot = CompanionCraft::VendorSlot(vendor->GetEntry(), entry);
        if (!proto || slot < 0 || !count)
            return 0;
        uint32 const lotSize = std::max<uint32>(1, proto->BuyCount);
        uint32 const lotsPerBuy = std::max<uint32>(1, std::min<uint32>(255, proto->GetMaxStackSize() / lotSize));
        uint32 lots = (count + lotSize - 1) / lotSize;
        uint32 const before = bot->GetItemCount(entry, false);
        uint32 const moneyBefore = bot->GetMoney();
        while (lots > 0)
        {
            uint32 const now = std::min(lots, lotsPerBuy);
            if (!bot->BuyItemFromVendorSlot(vendor->GetGUID(), static_cast<uint32>(slot), entry,
                                            static_cast<uint8>(now), NULL_BAG, NULL_SLOT))
                break;
            lots -= now;
        }
        uint32 const after = bot->GetItemCount(entry, false);
        uint32 const moneyAfter = bot->GetMoney();
        uint32 const bought = after > before ? after - before : 0;
        uint32 const cost = moneyBefore > moneyAfter ? moneyBefore - moneyAfter : 0;
        uint32 const low = bot->GetGUID().GetCounter();
        if (cost)
            CompanionCraft::NoteSpend(low, cost);
        if (bought)
            CompanionDuty::RecordLedger(low, "buy", entry, bought, -1, "craft duty");
        LOG_INFO("playerbots", "Companion {}: craft buy item={} count={} cost={} vendor={}", bot->GetName(), entry,
                 bought, cost, vendor->GetEntry());
        return bought;
    }

    // --- craft session (B6) -----------------------------------------------------------------------------------------

    // One leased trip to the city, then every recipe there (bank withdraw, vendor, station, crafts), then a deposit.
    // The work part keeps the lease alive (CompanionLease::KeepAlive) and gets its own Lease.MaxMinutes.
    bool RunSession(Player* bot, uint32 low, CompanionCraft::Run& run)
    {
        uint32 const now = NowSec();
        if (!run.started)
        {
            run.started = true;
            run.startSec = now;
            run.skillStart.clear();
            for (uint32 skill : SESSION_SKILLS)
                if (bot->HasSkill(skill))
                    run.skillStart.emplace_back(skill, bot->GetPureSkillValue(skill));
            LOG_INFO("playerbots", "Companion {}: craft session start city={} need={:.1f} bank={} dist={:.0f}",
                     bot->GetName(), run.cityName, run.sessionNeed, run.city.bank.spawnId,
                     bot->GetDistance(run.city.x, run.city.y, run.city.z));
        }

        switch (run.phase)
        {
            case CompanionCraft::PHASE_TRAVEL:
            {
                if (run.city.mapId != bot->GetMapId())
                    return EndSession(bot, low, run, "fail:no-point", "city on another map");
                float const dist = bot->GetDistance(run.city.x, run.city.y, run.city.z);
                if (dist > CompanionDuty::FAR_SEARCH_YARDS)
                {
                    CompanionLease::Progress(low, -dist);
                    if (CompanionQuest::TryMount(botAI, dist))
                        return true;
                    MoveTo(run.city.mapId, run.city.x, run.city.y, run.city.z);
                    return true;
                }
                // Arrived. The work part takes a fresh lease under the same owner (the trip's lease time is not
                // charged to the crafting).
                CompanionLease::Info const info = CompanionLease::Current(low);
                if (info.active && info.owner == "craft:session")
                {
                    CompanionLease::Destination dest;
                    dest.key = "session:" + std::to_string(run.city.bank.spawnId) + ":work";
                    dest.mapId = bot->GetMapId();
                    dest.x = run.city.x;
                    dest.y = run.city.y;
                    dest.z = run.city.z;
                    CompanionLease::Begin(bot, "craft:session", dest, 0, info.policy, info.score);
                }
                float const minutes =
                    std::max(1.0f, CompanionCraft::Option("AiPlayerbot.Companion.CraftSession.Minutes", 25.0f));
                run.phase = CompanionCraft::PHASE_WORK;
                run.sessionEndSec = now + static_cast<uint32>(minutes * 60.0f);
                LOG_INFO("playerbots", "Companion {}: craft session arrive city={} travel={}s", bot->GetName(),
                         run.cityName, now - run.startSec);
                return true;
            }
            case CompanionCraft::PHASE_WORK:
            {
                CompanionLease::KeepAlive(low);
                if (run.recipeActive)
                    return RunCraft(bot, low, run);
                std::string why;
                if (now >= run.sessionEndSec)
                    why = "time up";
                else if (run.sessionFailures >= MAX_SESSION_FAILURES)
                    why = "recipes failed";
                else
                {
                    std::string reason;
                    CompanionCraft::Recipe const r = CompanionCraft::ChooseRecipe(bot, reason, &run.city);
                    if (r.valid)
                    {
                        BeginRecipe(bot, run, r);
                        return true;
                    }
                    why = "nothing left (" + reason + ")";
                }
                run.phase = CompanionCraft::PHASE_DEPOSIT;
                LOG_INFO("playerbots", "Companion {}: craft session deposit city={} after={}", bot->GetName(),
                         run.cityName, why);
                return true;
            }
            case CompanionCraft::PHASE_DEPOSIT:
            default:
            {
                CompanionLease::KeepAlive(low);
                WorldObject* object = nullptr;
                int const state = ApproachSpawn(bot, low, run, run.city.bank, object);
                if (state == 0)
                    return true;
                GameObject* bank = object ? object->ToGameObject() : nullptr;
                bool const deposited = state > 0 && bank && GuildBankAction(botAI).RunCompanionBankDuty(bank);
                CompanionDuty::InvalidateBank(bot->GetGuildId());
                return EndSession(bot, low, run, run.sessionCrafts > 0 ? "done" : "fail:nothing-made",
                                  deposited ? "deposited" : "no deposit");
            }
        }
    }

    // Starts the next recipe of a session: the same first step and log line as a single-recipe craft duty.
    void BeginRecipe(Player* bot, CompanionCraft::Run& run, CompanionCraft::Recipe const& r)
    {
        run.recipe = r;
        run.recipeActive = true;
        run.crafted = 0;
        run.failures = 0;
        run.castPending = false;
        run.campfireCast = false;
        run.stepStartSec = NowSec();
        run.skillBefore = bot->GetPureSkillValue(r.skillId);
        if (r.bank.valid)
            run.step = CompanionCraft::STEP_BANK;
        else if (r.vendor.valid)
            run.step = CompanionCraft::STEP_VENDOR;
        else if (r.station.valid || r.campfireSpell)
            run.step = CompanionCraft::STEP_STATION;
        else
            run.step = CompanionCraft::STEP_WORK;
        for (CompanionCraft::Material const& m : r.materials)
            if (m.fromBank)
                CompanionCraft::Reserve(bot, m.entry, m.fromBank);
        LOG_INFO("playerbots",
                 "Companion {}: craft start spell={} name=\"{}\" skill={} value={}/{} color={} crafts={} source={} "
                 "vendorCost={} session={}",
                 bot->GetName(), r.spellId, r.spellName, r.skillId, r.skillValue, r.skillMax,
                 CompanionCraft::ColorName(r.color), r.crafts, r.source, r.vendorCost, run.cityName);
    }

    void LogSessionEnd(Player* bot, CompanionCraft::Run const& run, char const* reason, std::string const& text)
    {
        uint32 total = 0;
        std::ostringstream detail;
        for (auto const& [skill, start] : run.skillStart)
        {
            uint32 const value = bot->GetPureSkillValue(skill);
            if (value == start)
                continue;
            total += value > start ? value - start : 0;
            SkillLineEntry const* entry = sSkillLineStore.LookupEntry(skill);
            detail << (detail.tellp() > 0 ? "," : "") << (entry && entry->name[0] ? entry->name[0] : "?") << ":"
                   << start << "->" << value;
        }
        uint32 const now = NowSec();
        LOG_INFO("playerbots",
                 "Companion {}: craft session end city={} crafts={} recipes={} skillups={} ({}) dur={}s reason={} ({})",
                 bot->GetName(), run.cityName, run.sessionCrafts + (run.recipeActive ? run.crafted : 0),
                 run.sessionRecipes, total, detail.tellp() > 0 ? detail.str() : std::string("none"),
                 now > run.startSec ? now - run.startSec : 0, reason, text);
    }

    bool EndSession(Player* bot, uint32 low, CompanionCraft::Run& run, char const* reason, std::string const& text)
    {
        LogSessionEnd(bot, run, reason, text);
        bool const success = run.sessionCrafts > 0;
        run.valid = false;   // not saved again
        Finish(low, "craft", success, success ? "done" : reason, text);
        return false;
    }

    // --- craft ------------------------------------------------------------------------------------------------------

    uint8 NextCraftStep(CompanionCraft::Recipe const& r, uint8 after)
    {
        if (after < CompanionCraft::STEP_VENDOR && r.vendor.valid)
            return CompanionCraft::STEP_VENDOR;
        if (after < CompanionCraft::STEP_STATION && r.focusId)
            return CompanionCraft::STEP_STATION;
        return CompanionCraft::STEP_WORK;
    }

    bool RunCraft(Player* bot, uint32 low, CompanionCraft::Run& run)
    {
        CompanionCraft::Recipe& r = run.recipe;
        uint32 const now = NowSec();
        if (!run.started)
        {
            run.started = true;
            run.startSec = now;
            run.stepStartSec = now;
            run.skillBefore = bot->GetPureSkillValue(r.skillId);
            for (CompanionCraft::Material const& m : r.materials)
                if (m.fromBank)
                    CompanionCraft::Reserve(bot, m.entry, m.fromBank);
            LOG_INFO("playerbots",
                     "Companion {}: craft start spell={} name=\"{}\" skill={} value={}/{} color={} crafts={} source={} "
                     "vendorCost={}",
                     bot->GetName(), r.spellId, r.spellName, r.skillId, r.skillValue, r.skillMax,
                     CompanionCraft::ColorName(r.color), r.crafts, r.source, r.vendorCost);
        }

        switch (run.step)
        {
            case CompanionCraft::STEP_BANK:
            {
                WorldObject* bank = nullptr;
                int const state = ApproachSpawn(bot, low, run, r.bank, bank);
                if (state == 0)
                    return true;
                if (state < 0)
                    return EndCraft(bot, low, run, false, "fail:target-gone", "bank not found");
                {
                    // The bank snapshot is rebuilt on the world thread after each move: wait for it (max 20 s),
                    // or the withdraw finds no slots and the recipe fails with "materials used up".
                    std::vector<CompanionDuty::BankTabUsage> tabs;
                    std::vector<CompanionDuty::BankSlot> slots;
                    if (!CompanionDuty::BankSnapshot(bot->GetGuildId(), tabs, slots) && now - run.stepStartSec < 20)
                    {
                        CompanionLease::KeepAlive(low);
                        return true;
                    }
                }
                GuildBankAction withdraw(botAI);
                for (CompanionCraft::Material& m : r.materials)
                {
                    if (!m.fromBank)
                        continue;
                    uint32 const got = withdraw.WithdrawForCompanion(m.entry, m.fromBank, "craft duty");
                    CompanionCraft::Release(low, m.entry, "withdrawn");
                    m.inBags += got;
                }
                CompanionDuty::InvalidateBank(bot->GetGuildId());
                run.step = NextCraftStep(r, CompanionCraft::STEP_BANK);
                run.stepStartSec = now;
                return true;
            }
            case CompanionCraft::STEP_VENDOR:
            {
                WorldObject* object = nullptr;
                int const state = ApproachSpawn(bot, low, run, r.vendor, object);
                if (state == 0)
                    return true;
                Creature* vendor = object ? object->ToCreature() : nullptr;
                if (state < 0 || !vendor)
                    return EndCraft(bot, low, run, false, "fail:target-gone", "vendor not found");
                for (CompanionCraft::Material& m : r.materials)
                    if (m.fromVendor)
                        m.inBags += Buy(bot, vendor, m.entry, m.fromVendor);
                run.step = NextCraftStep(r, CompanionCraft::STEP_VENDOR);
                run.stepStartSec = now;
                return true;
            }
            case CompanionCraft::STEP_STATION:
            {
                if (!r.focusId)
                {
                    run.step = CompanionCraft::STEP_WORK;
                    return true;
                }
                GameObject* focus = FindFocusNear(bot, r.focusId, FOCUS_SEARCH_YARDS);
                if (focus)
                {
                    float const dist = bot->GetDistance(focus);
                    Progress(low, run, dist);
                    if (dist > FocusReach(focus))
                    {
                        MoveTo(focus, FocusReach(focus) - 1.0f);
                        return true;
                    }
                    LOG_INFO("playerbots", "Companion {}: craft station focus={} by={} id={}", bot->GetName(),
                             r.focusId, run.campfireCast ? "spell" : "spawn",
                             run.campfireCast ? r.campfireSpell : focus->GetSpawnId());
                    run.step = CompanionCraft::STEP_WORK;
                    run.stepStartSec = now;
                    return true;
                }
                if (r.campfireSpell)
                {
                    if (run.campfireCast && now - run.stepStartSec < CAMPFIRE_RETRY_SECONDS)
                        return true;   // the fire appears after the cast
                    if (bot->isMoving())
                    {
                        bot->StopMoving();
                        return true;
                    }
                    if (run.campfireCast)
                        ++run.failures;
                    if (run.failures >= MAX_CRAFT_FAILURES)
                        return EndCraft(bot, low, run, false, "fail:perform", "campfire");
                    run.campfireCast = botAI->CastSpell(r.campfireSpell, bot) || run.campfireCast;
                    run.stepStartSec = now;
                    return true;
                }
                if (!r.station.valid || r.station.mapId != bot->GetMapId())
                    return EndCraft(bot, low, run, false, "fail:no-point", "no station");
                float const dist = bot->GetDistance(r.station.x, r.station.y, r.station.z);
                Progress(low, run, dist);
                if (dist <= CompanionDuty::FAR_ARRIVE_YARDS / 2.0f)
                    return EndCraft(bot, low, run, false, "fail:target-gone", "station not found");
                MoveTo(r.station.mapId, r.station.x, r.station.y, r.station.z);
                return true;
            }
            case CompanionCraft::STEP_WORK:
                return Work(bot, low, run);
            default:
                return EndCraft(bot, low, run, true, "done", "done");
        }
    }

    // A stack of entry with at least count items, nullptr when none.
    Item* FindStack(Player* bot, uint32 entry, uint32 count)
    {
        Item* found = nullptr;
        auto visit = [&](Item* item)
        {
            if (!found && item && item->GetEntry() == entry && item->GetCount() >= count)
                found = item;
        };
        for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
            visit(bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot));
        for (uint8 bagSlot = INVENTORY_SLOT_BAG_START; bagSlot < INVENTORY_SLOT_BAG_END; ++bagSlot)
            if (Bag* bag = bot->GetBagByPos(bagSlot))
                for (uint32 i = 0; i < bag->GetBagSize(); ++i)
                    visit(bag->GetItemByPos(i));
        return found;
    }

    // Moves one partial stack of entry onto another (Player::SwapItem fills the target stack). True when moved.
    bool MergeStacks(Player* bot, uint32 entry)
    {
        std::vector<Item*> stacks;
        auto visit = [&](Item* item)
        {
            if (item && item->GetEntry() == entry && item->GetCount() < item->GetMaxStackCount())
                stacks.push_back(item);
        };
        for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
            visit(bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot));
        for (uint8 bagSlot = INVENTORY_SLOT_BAG_START; bagSlot < INVENTORY_SLOT_BAG_END; ++bagSlot)
            if (Bag* bag = bot->GetBagByPos(bagSlot))
                for (uint32 i = 0; i < bag->GetBagSize(); ++i)
                    visit(bag->GetItemByPos(i));
        if (stacks.size() < 2)
            return false;
        std::sort(stacks.begin(), stacks.end(), [](Item* a, Item* b) { return a->GetCount() > b->GetCount(); });
        uint32 const before = stacks.front()->GetCount();
        bot->SwapItem(stacks.back()->GetPos(), stacks.front()->GetPos());
        return stacks.front()->GetCount() > before;
    }

    // B8: Milling / Prospecting. One cast per 5-stack with the stack as the item target; the results come in a loot
    // window, which is emptied here before it is released (the release destroys the 5 items, looted or not).
    bool WorkTarget(Player* bot, uint32 low, CompanionCraft::Run& run)
    {
        CompanionCraft::Recipe& r = run.recipe;
        uint32 const now = NowSec();
        if (bot->IsNonMeleeSpellCast(false) || r.materials.empty())
            return r.materials.empty() ? EndCraft(bot, low, run, false, "fail:no-plan", "no material") : true;
        uint32 const entry = r.materials.front().entry;
        if (run.castPending)
        {
            ObjectGuid const lootGuid = bot->GetLootGUID();
            if (lootGuid.IsItem())
            {
                if (Item* source = bot->GetItemByGuid(lootGuid))
                {
                    Loot& loot = source->loot;
                    for (size_t i = 0; i < loot.items.size(); ++i)
                    {
                        InventoryResult msg = EQUIP_ERR_OK;
                        bot->StoreLootItem(static_cast<uint8>(i), &loot, msg);
                    }
                }
                bot->GetSession()->DoLootRelease(lootGuid);
            }
            uint32 const left = bot->GetItemCount(entry, false);
            if (left + TARGET_STACK <= run.productBefore)
            {
                run.castPending = false;
                ++run.crafted;
                LOG_INFO("playerbots",
                         "Companion {}: craft made spell={} item={} n={}/{} skill={} value={}->{} color=target",
                         bot->GetName(), r.spellId, entry, run.crafted, r.crafts, r.skillId, run.skillBefore,
                         bot->GetPureSkillValue(r.skillId));
                Progress(low, run, 0.0f);
            }
            else if (now - run.castSec < TARGET_WAIT_SECONDS)
                return true;   // the loot window may come a moment later
            else
            {
                run.castPending = false;
                ++run.failures;
            }
        }

        if (run.crafted >= r.crafts)
            return EndCraft(bot, low, run, true, "done", "batch done");
        if (run.failures >= MAX_CRAFT_FAILURES)
            return EndCraft(bot, low, run, run.crafted > 0, run.crafted > 0 ? "done" : "fail:perform", "casts failed");
        if (bot->GetItemCount(entry, false) < TARGET_STACK)
            return EndCraft(bot, low, run, run.crafted > 0, run.crafted > 0 ? "done" : "fail:materials",
                            "materials used up");
        if (bot->GetFreeInventorySpace() < MIN_FREE_SLOTS_TARGET)
            return EndCraft(bot, low, run, run.crafted > 0, run.crafted > 0 ? "done" : "fail:bags", "bags full");
        if (bot->isMoving())
        {
            bot->StopMoving();
            return true;
        }

        Item* stack = FindStack(bot, entry, TARGET_STACK);
        if (!stack)
        {
            if (MergeStacks(bot, entry))
                return true;
            return EndCraft(bot, low, run, run.crafted > 0, run.crafted > 0 ? "done" : "fail:materials",
                            "no stack of 5");
        }
        // Stock looting ("store loot") takes these results too.
        std::set<uint32>& always = context->GetValue<std::set<uint32>&>("always loot list")->Get();
        for (uint32 item : r.lootItems)
            always.insert(item);
        run.productBefore = bot->GetItemCount(entry, false);
        run.skillBefore = bot->GetPureSkillValue(r.skillId);
        run.castSec = now;
        if (botAI->CastSpell(r.spellId, bot, stack))
            run.castPending = true;
        else
            ++run.failures;
        return true;
    }

    bool Work(Player* bot, uint32 low, CompanionCraft::Run& run)
    {
        CompanionCraft::Recipe& r = run.recipe;
        if (r.targetItem)
            return WorkTarget(bot, low, run);
        if (bot->IsNonMeleeSpellCast(false))
            return true;
        if (run.castPending)
        {
            run.castPending = false;
            uint32 const made = bot->GetItemCount(r.product, false);
            uint32 const skill = bot->GetPureSkillValue(r.skillId);
            if (made > run.productBefore)
            {
                static std::atomic<uint64> sCraftCallbackSeq{1};
                uint32 const producedDelta = made - run.productBefore;
                uint64 const cbId = sCraftCallbackSeq.fetch_add(1, std::memory_order_relaxed);
                ++run.crafted;
                CompanionCraft::Color const color = CompanionCraft::ColorFor(skill, r.yellowAt, r.greyAt);
                LOG_INFO("playerbots",
                         "Companion {}: craft made spell={} item={} delta={} n={}/{} skill={} value={}->{} color={}",
                         bot->GetName(), r.spellId, r.product, producedDelta, run.crafted, r.crafts, r.skillId,
                         run.skillBefore, skill, CompanionCraft::ColorName(color));
                CompanionLife::OnCraftMade(bot, r.product, "", r.skillId, producedDelta, cbId);
                run.productBefore = made;
                run.skillBefore = skill;
            }
            else
                ++run.failures;
            Progress(low, run, 0.0f);
        }

        if (run.crafted >= r.crafts)
            return EndCraft(bot, low, run, true, "done", "batch done");
        if (run.failures >= MAX_CRAFT_FAILURES)
            return EndCraft(bot, low, run, run.crafted > 0, run.crafted > 0 ? "done" : "fail:perform", "casts failed");
        uint32 const skillNow = bot->GetPureSkillValue(r.skillId);
        if (!r.smelting && CompanionCraft::ColorFor(skillNow, r.yellowAt, r.greyAt) == CompanionCraft::Color::Grey)
            return EndCraft(bot, low, run, true, "done", "recipe grey");
        for (CompanionCraft::Material const& m : r.materials)
            if (!m.tool && bot->GetItemCount(m.entry, false) < m.perCraft)
                return EndCraft(bot, low, run, run.crafted > 0, run.crafted > 0 ? "done" : "fail:materials",
                                "materials used up");
        if (bot->isMoving())
        {
            bot->StopMoving();
            return true;
        }

        run.productBefore = bot->GetItemCount(r.product, false);
        run.skillBefore = skillNow;
        if (botAI->CastSpell(r.spellId, bot))
            run.castPending = true;
        else
            ++run.failures;
        return true;
    }

    bool EndCraft(Player* bot, uint32 low, CompanionCraft::Run& run, bool success, char const* reason,
                  std::string const& text)
    {
        CompanionCraft::Recipe const& r = run.recipe;
        uint32 const skillStart = r.skillValue;
        LOG_INFO("playerbots", "Companion {}: craft end spell={} made={}/{} skill={} value={}->{} reason={} ({})",
                 bot->GetName(), r.spellId, run.crafted, r.crafts, r.skillId, skillStart,
                 bot->GetPureSkillValue(r.skillId), reason, text);
        if (run.session)
        {
            // Inside a session a recipe end is not a duty end: count it and pick the next recipe.
            run.sessionCrafts += run.crafted;
            ++run.sessionRecipes;
            if (!success || run.crafted == 0)
            {
                ++run.sessionFailures;
                run.city.skip.insert(r.spellId);
            }
            else
                run.sessionFailures = 0;
            if (run.failures >= MAX_CRAFT_FAILURES)
                run.city.skip.insert(r.spellId);
            CompanionCraft::ReleaseAll(low, "recipe done");
            run.recipeActive = false;
            run.recipe = CompanionCraft::Recipe();
            run.crafted = 0;
            run.failures = 0;
            run.castPending = false;
            CompanionLease::KeepAlive(low);
            return true;
        }
        run.valid = false;   // not saved again
        Finish(low, "craft", success, reason, text);
        return false;
    }

    // --- gather-farm (active farming for blocked craft mats: Mining / Herbalism) ----------------------------------

    GameObject* FindLiveGatherNode(Player* bot, std::vector<uint32> const& entries, float range)
    {
        std::list<GameObject*> gos;
        AnyGameObjectInObjectRangeCheck check(bot, range);
        Acore::GameObjectListSearcher<AnyGameObjectInObjectRangeCheck> searcher(bot, gos, check);
        Cell::VisitObjects(bot, searcher, range);
        GameObject* best = nullptr;
        float bestDist = 0.0f;
        for (GameObject* go : gos)
        {
            if (!go || !go->isSpawned() || go->getLootState() != GO_READY)
                continue;
            if (std::find(entries.begin(), entries.end(), go->GetEntry()) == entries.end())
                continue;
            LootObject loot(bot, go->GetGUID());
            if (loot.IsEmpty() || !loot.IsLootPossible(bot))
                continue;
            float const d = bot->GetDistance(go);
            if (!best || d < bestDist)
            {
                best = go;
                bestDist = d;
            }
        }
        return best;
    }

    bool EndGatherFarm(Player* bot, uint32 low, CompanionCraft::Run& run, bool success, char const* reason,
                       std::string const& text)
    {
        uint32 const have = bot->GetItemCount(run.gatherItem, false);
        LOG_INFO("playerbots",
                 "Companion {}: gather-farm end item={} have={}/{} nodes={} reason={} note={}",
                 bot->GetName(), run.gatherItem, have, run.gatherTargetCount, run.gatheredNodes, reason, text);
        Finish(low, "gather", success, reason, text);
        run.valid = false;
        return false;
    }

    bool RunGatherFarm(Player* bot, uint32 low, CompanionCraft::Run& run)
    {
        uint32 const now = NowSec();
        if (!run.started)
        {
            run.started = true;
            run.startSec = now;
            run.stepStartSec = now;
            uint32 const maxSec = std::max<uint32>(
                300, static_cast<uint32>(CompanionCraft::Option("AiPlayerbot.Companion.GatherFarm.MaxSeconds", 1800.0f)));
            run.endSec = now + maxSec;   // 30 minute default max per farming trip
            LOG_INFO("playerbots",
                     "Companion {}: gather-farm start item={} skill={} have={}/{} target=({:.1f},{:.1f},{:.1f})",
                     bot->GetName(), run.gatherItem, run.gatherSkill, run.gatherStartCount, run.gatherTargetCount,
                     run.gatherSpot.x, run.gatherSpot.y, run.gatherSpot.z);
        }

        uint32 const have = bot->GetItemCount(run.gatherItem, false);
        if (have >= run.gatherTargetCount)
            return EndGatherFarm(bot, low, run, true, "done", "batch gathered");
        if (bot->GetFreeInventorySpace() < 3)
            return EndGatherFarm(bot, low, run, have > run.gatherStartCount, "done", "bags nearly full");
        if (now >= run.endSec)
            return EndGatherFarm(bot, low, run, have > run.gatherStartCount, "done", "time cap reached");

        // Skinning catch-up hunting: travel to low-level beast cluster, pull skinnable beasts, and skin corpses.
        if (run.gatherSkill == SKILL_SKINNING)
        {
            if (!run.gatherSpot.valid || run.gatherSpot.mapId != bot->GetMapId())
                return EndGatherFarm(bot, low, run, have > run.gatherStartCount, "done", "left hunting map");

            if (!bot->IsInCombat())
            {
                // 1. Skin any nearby skinnable corpse first!
                std::list<Creature*> corpses;
                Acore::AnyUnitInObjectRangeCheck ccheck(bot, 70.0f);
                Acore::CreatureListSearcher<Acore::AnyUnitInObjectRangeCheck> csearcher(bot, corpses, ccheck);
                Cell::VisitObjects(bot, csearcher, 70.0f);
                Creature* skinTarget = nullptr;
                float skinDist = 999.0f;
                for (Creature* cr : corpses)
                {
                    if (!cr || cr->IsAlive() || !cr->HasUnitFlag(UNIT_FLAG_SKINNABLE))
                        continue;
                    LootObject loot(bot, cr->GetGUID());
                    if (loot.IsEmpty() || !loot.IsLootPossible(bot))
                        continue;
                    float const d = bot->GetDistance(cr);
                    if (d < skinDist)
                    {
                        skinDist = d;
                        skinTarget = cr;
                    }
                }
                if (skinTarget)
                {
                    CompanionLease::Progress(low, run.gatheredNodes * 500.0f - skinDist);
                    if (skinDist > INTERACTION_DISTANCE - 1.5f)
                    {
                        MoveTo(skinTarget, INTERACTION_DISTANCE - 2.0f);
                        return true;
                    }
                    LootObject loot(bot, skinTarget->GetGUID());
                    if (!loot.IsEmpty() && loot.IsLootPossible(bot))
                    {
                        context->GetValue<LootObject>("loot target")->Set(loot);
                        AI_VALUE(LootObjectStack*, "available loot")->Add(skinTarget->GetGUID());
                        if (botAI->DoSpecificAction("open loot", Event(), true))
                        {
                            ++run.gatheredNodes;
                            CompanionLease::KeepAlive(low);
                        }
                    }
                    return true;
                }

                // 2. If inside the beast hunting ground (<= 220 yd), pull nearest level-appropriate skinnable beast!
                float const spotDist = bot->GetDistance(run.gatherSpot.x, run.gatherSpot.y, run.gatherSpot.z);
                if (spotDist <= 220.0f)
                {
                    uint32 const curSkin = bot->GetPureSkillValue(SKILL_SKINNING);
                    uint32 const maxMobLvl = std::max<uint32>(10, curSkin / 5 + 4);
                    Creature* prey = nullptr;
                    float preyDist = 999.0f;
                    for (Creature* cr : corpses)
                    {
                        if (!cr || !cr->IsAlive() || cr->IsCritter() || cr->IsPet() || cr->IsTotem() ||
                            !bot->IsValidAttackTarget(cr) || bot->IsFriendlyTo(cr))
                            continue;
                        if (cr->GetCreatureType() != CREATURE_TYPE_BEAST)
                            continue;
                        if (cr->GetLevel() > maxMobLvl)
                            continue;
                        CreatureTemplate const* cinfo = cr->GetCreatureTemplate();
                        if (!cinfo || !cinfo->SkinLootId)
                            continue;
                        float const d = bot->GetDistance(cr);
                        if (d < preyDist)
                        {
                            preyDist = d;
                            prey = cr;
                        }
                    }
                    if (prey)
                    {
                        CompanionLease::KeepAlive(low);
                        context->GetValue<Unit*>("current target")->Set(prey);
                        if (preyDist > 25.0f)
                            MoveTo(prey, 15.0f);
                        else
                            botAI->DoSpecificAction("attack", Event(), true);
                        return true;
                    }
                }
            }

            float const dist = bot->GetDistance(run.gatherSpot.x, run.gatherSpot.y, run.gatherSpot.z);
            CompanionLease::Progress(low, run.gatheredNodes * 500.0f - dist);
            if (!bot->IsInCombat() && CompanionQuest::TryMount(botAI, dist))
                return true;
            float wx = run.gatherSpot.x, wy = run.gatherSpot.y, wz = run.gatherSpot.z;
            CompanionRoute::NextPoint(bot, run.gatherSpot.mapId, run.gatherSpot.x, run.gatherSpot.y, run.gatherSpot.z,
                                      wx, wy, wz);
            MoveTo(run.gatherSpot.mapId, wx, wy, wz);
            return true;
        }

        if (!run.gatherSpot.valid || run.gatherSpot.mapId != bot->GetMapId())
        {
            run.gatherSpot =
                CompanionCraft::NextGatherSpot(bot, run.gatherItem, run.gatherSkill, run.gatherSkipSpawns);
            if (!run.gatherSpot.valid)
                return EndGatherFarm(bot, low, run, have > run.gatherStartCount,
                                     have > run.gatherStartCount ? "done" : "fail:no-node", "no safe nodes left");
        }

        // Check for any live matching node within 100 yd first (opportunistic pickup along the cluster).
        if (!bot->IsInCombat())
        {
            if (GameObject* liveNode = FindLiveGatherNode(bot, run.gatherNodeEntries, 100.0f))
            {
                float const nd = bot->GetDistance(liveNode);
                CompanionLease::Progress(low, run.gatheredNodes * 500.0f - nd);
                if (nd > INTERACTION_DISTANCE - 2.0f)
                {
                    if (nd > 35.0f && CompanionQuest::TryMount(botAI, nd))
                        return true;
                    MoveTo(liveNode, INTERACTION_DISTANCE - 2.5f);
                    return true;
                }
                LootObject loot(bot, liveNode->GetGUID());
                if (!loot.IsEmpty() && loot.IsLootPossible(bot))
                {
                    context->GetValue<LootObject>("loot target")->Set(loot);
                    AI_VALUE(LootObjectStack*, "available loot")->Add(liveNode->GetGUID());
                    if (botAI->DoSpecificAction("open loot", Event(), true))
                    {
                        ++run.gatheredNodes;
                        run.gatherSkipSpawns.insert(liveNode->GetSpawnId());
                        CompanionLease::KeepAlive(low);
                        uint32 const nowHave = bot->GetItemCount(run.gatherItem, false);
                        LOG_INFO("playerbots", "Companion {}: gather-farm progress item={} have={}/{} nodes={}",
                                 bot->GetName(), run.gatherItem, nowHave, run.gatherTargetCount, run.gatheredNodes);
                    }
                    return true;
                }
            }
        }

        float const dist = bot->GetDistance(run.gatherSpot.x, run.gatherSpot.y, run.gatherSpot.z);
        if (dist <= 35.0f)
        {
            // Arrived at target spawn point and no live node was found here (on respawn timer): hop to next spawn!
            run.gatherSkipSpawns.insert(run.gatherSpot.spawnId);
            run.gatherSpot =
                CompanionCraft::NextGatherSpot(bot, run.gatherItem, run.gatherSkill, run.gatherSkipSpawns);
            if (!run.gatherSpot.valid)
                return EndGatherFarm(bot, low, run, have > run.gatherStartCount,
                                     have > run.gatherStartCount ? "done" : "fail:no-node", "cluster exhausted");
            CompanionLease::KeepAlive(low);
            return true;
        }

        CompanionLease::Progress(low, run.gatheredNodes * 500.0f - dist);
        if (!bot->IsInCombat() && CompanionQuest::TryMount(botAI, dist))
            return true;
        float wx = run.gatherSpot.x, wy = run.gatherSpot.y, wz = run.gatherSpot.z;
        CompanionRoute::NextPoint(bot, run.gatherSpot.mapId, run.gatherSpot.x, run.gatherSpot.y, run.gatherSpot.z, wx,
                                  wy, wz);
        MoveTo(run.gatherSpot.mapId, wx, wy, wz);
        return true;
    }

    // --- fish -------------------------------------------------------------------------------------------------------

    bool RunFish(Player* bot, uint32 low, CompanionCraft::Run& run)
    {
        uint32 const now = NowSec();
        if (!run.started)
        {
            run.started = true;
            run.startSec = now;
            run.stepStartSec = now;
            run.fishSkillStart = bot->GetPureSkillValue(SKILL_FISHING);
            LOG_INFO("playerbots", "Companion {}: fish start water={} chance={} skill={}/{}", bot->GetName(),
                     run.pool ? "pool:" + std::to_string(run.water.spawnId) : std::string("here"), run.chancePct,
                     bot->GetPureSkillValue(SKILL_FISHING), bot->GetPureMaxSkillValue(SKILL_FISHING));
        }

        switch (run.step)
        {
            case CompanionCraft::STEP_VENDOR:
            {
                WorldObject* object = nullptr;
                int const state = ApproachSpawn(bot, low, run, run.poleVendor, object);
                if (state == 0)
                    return true;
                Creature* vendor = object ? object->ToCreature() : nullptr;
                if (state < 0 || !vendor || !Buy(bot, vendor, run.poleItem, 1))
                    return EndFish(bot, low, run, false, "fail:perform", "no fishing pole bought");
                run.step = CompanionCraft::STEP_STATION;
                run.stepStartSec = now;
                return true;
            }
            case CompanionCraft::STEP_BANK:
            case CompanionCraft::STEP_STATION:
            {
                if (run.pool)
                {
                    if (run.water.mapId != bot->GetMapId())
                        return EndFish(bot, low, run, false, "fail:no-point", "pool on another map");
                    float const dist = bot->GetDistance(run.water.x, run.water.y, run.water.z);
                    Progress(low, run, dist);
                    if (dist > POOL_STAND_YARDS)
                    {
                        MoveTo(run.water.mapId, run.water.x, run.water.y, run.water.z);
                        return true;
                    }
                    // Pools rotate (pool_template): a pool spawn that is not up now is a failure (cooldown on it).
                    if (!PoolNear(bot))
                        return EndFish(bot, low, run, false, "fail:target-gone", "pool not spawned");
                }
                run.step = CompanionCraft::STEP_WORK;
                run.stepStartSec = now;
                run.endSec = now + static_cast<uint32>(std::max(1.0f, CompanionCraft::Option(
                                                                         "AiPlayerbot.Companion.Fish.Minutes", 15.0f)) *
                                                       60.0f);
                return true;
            }
            case CompanionCraft::STEP_WORK:
                return Fish(bot, low, run);
            default:
                return EndFish(bot, low, run, true, "done", "done");
        }
    }

    bool Fish(Player* bot, uint32 low, CompanionCraft::Run& run)
    {
        uint32 const now = NowSec();
        if (now >= run.endSec)
            return EndFish(bot, low, run, true, "done", "time up");
        if (!CompanionCraft::SkillGap(bot, SKILL_FISHING))
            return EndFish(bot, low, run, true, "done", "gap closed");

        UseBobberAction bobber(botAI);
        if (bobber.isUseful())
        {
            bobber.Execute(Event());
            return true;
        }
        if (bot->GetCurrentSpell(CURRENT_CHANNELED_SPELL))
            return true;   // waiting for a bite

        uint32 const nowMs = getMSTime();
        if (run.lastWaterCheckMs && getMSTimeDiff(run.lastWaterCheckMs, nowMs) < WATER_CHECK_MS)
            return true;
        run.lastWaterCheckMs = nowMs;

        // Stock fishing: "fishing spot" is set by MoveNearWaterAction::isPossible (here when water is in range).
        MoveNearWaterAction moveNear(botAI);
        bool const needsMove = moveNear.isPossible();
        FishingAction fishing(botAI);
        if (fishing.isUseful())
        {
            Item* mainHand = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_MAINHAND);
            if (!IsPole(mainHand))
            {
                Item* offHand = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_OFFHAND);
                CompanionCraft::SaveWeapons(low, mainHand ? mainHand->GetGUID() : ObjectGuid(),
                                            offHand ? offHand->GetGUID() : ObjectGuid());
            }
            if (fishing.Execute(Event()))
            {
                run.noWaterChecks = 0;
                if (IsPole(bot->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_MAINHAND)))
                {
                    ++run.casts;
                    Progress(low, run, 0.0f);
                    LOG_INFO("playerbots", "Companion {}: fish cast n={} skill={}", bot->GetName(), run.casts,
                             bot->GetPureSkillValue(SKILL_FISHING));
                }
                return true;
            }
        }
        if (needsMove && moveNear.isUseful())
        {
            moveNear.Execute(Event());
            return true;
        }
        if (++run.noWaterChecks > MAX_WATER_CHECKS)
            return EndFish(bot, low, run, run.casts > 0, run.casts > 0 ? "done" : "fail:no-water", "no water");
        return true;
    }

    bool EndFish(Player* bot, uint32 low, CompanionCraft::Run& run, bool success, char const* reason,
                 std::string const& text)
    {
        CompanionRestoreWeapons(botAI);
        LOG_INFO("playerbots", "Companion {}: fish end casts={} skill={}->{} reason={} ({})", bot->GetName(),
                 run.casts, run.fishSkillStart, bot->GetPureSkillValue(SKILL_FISHING), reason, text);
        run.valid = false;
        Finish(low, "fish", success, reason, text);
        return false;
    }
};
}  // namespace

bool CompanionRestoreWeapons(PlayerbotAI* botAI)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot)
        return false;
    uint32 const low = bot->GetGUID().GetCounter();
    ObjectGuid mainHand;
    ObjectGuid offHand;
    if (!CompanionCraft::SavedWeapons(low, mainHand, offHand))
        return false;

    uint32 mainEntry = 0;
    uint32 offEntry = 0;
    // B14: the equip is refused while a spell is cast or channelled (the fishing cast), and that was not seen: stop
    // the cast first and log whether each weapon is really in its slot afterwards (ok=0 means it stayed in the bags).
    bot->InterruptNonMeleeSpells(false);
    bool ok = true;
    auto equip = [bot, &ok](ObjectGuid guid, uint8 slot) -> uint32
    {
        if (guid.IsEmpty())
            return 0;
        Item* item = bot->GetItemByGuid(guid);
        if (!item || item->IsEquipped())
            return 0;
        uint32 const entry = item->GetEntry();
        WorldPacket packet(CMSG_AUTOEQUIP_ITEM_SLOT, 9);
        packet << guid << slot;
        WorldPackets::Item::AutoEquipItemSlot nicePacket(std::move(packet));
        nicePacket.Read();
        bot->GetSession()->HandleAutoEquipItemSlotOpcode(nicePacket);
        if (bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot) != item)
            ok = false;
        return entry;
    };
    // A refused equip keeps the saved weapons so the "companion weapons saved" trigger tries again, at most
    // RESTORE_TRIES times (then the normal equip upgrade takes over).
    constexpr uint32 RESTORE_TRIES = 5;
    static std::mutex triesMutex;
    static std::unordered_map<uint32, uint32> tries;
    mainEntry = equip(mainHand, EQUIPMENT_SLOT_MAINHAND);
    // Tank shield rule: a weapon saved from the off hand stays in the bags (the equip upgrade puts the shield back).
    Item* savedOff = offHand.IsEmpty() ? nullptr : bot->GetItemByGuid(offHand);
    if (!(savedOff && savedOff->GetTemplate()->Class == ITEM_CLASS_WEAPON && sCompanionBotMgr.TankKeepsShield(bot)))
        offEntry = equip(offHand, EQUIPMENT_SLOT_OFFHAND);
    uint32 attempt = 0;
    {
        std::lock_guard<std::mutex> lock(triesMutex);
        attempt = ok ? 0 : ++tries[low];
        if (ok || attempt >= RESTORE_TRIES)
            tries.erase(low);
    }
    LOG_INFO("playerbots", "Companion {}: weapons restored main={} off={} ok={} try={}", bot->GetName(), mainEntry,
             offEntry, ok ? 1 : 0, attempt);
    if (!ok && attempt < RESTORE_TRIES)
        return false;   // saved weapons kept: retried next tick
    CompanionCraft::ClearSavedWeapons(low);
    return true;
}

Trigger* CreateCompanionCraftActiveTrigger(PlayerbotAI* botAI) { return new CompanionCraftActiveTrigger(botAI); }
Action* CreateCompanionCraftAction(PlayerbotAI* botAI) { return new CompanionCraftAction(botAI); }
