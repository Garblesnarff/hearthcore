/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "GuildBankAction.h"

#include <map>
#include "AiObjectContext.h"
#include "Bag.h"
#include "CompanionBrain.h"
#include "CompanionClassStrategy.h"
#include "CompanionDuty.h"
#include "CompanionLife.h"
#include "GameObject.h"
#include "GameTime.h"
#include "GuildMgr.h"
#include "Item.h"
#include "ItemTemplate.h"
#include "ItemUsageValue.h"
#include "Log.h"
#include "ObjectMgr.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <mutex>
#include <set>
#include <unordered_map>

namespace
{
std::mutex gOrganizeMutex;
std::unordered_map<uint32, uint32> gLastOrganizeSec;   // guildId -> last auto-organize unix sec
constexpr uint32 ORGANIZE_COOLDOWN_SEC = 1800;         // auto-organize at most once per 30 min per guild
constexpr uint32 MAX_PURGE_PER_VISIT = 8;              // leave daily withdraw slots for crafting withdrawals
}  // namespace

bool GuildBankAction::Execute(Event event)
{
    std::string const text = event.getParam();
    if (text.empty())
        return false;

    if (!bot->GetGuildId() || (GetMaster() && GetMaster()->GetGuildId() != bot->GetGuildId()))
    {
        botAI->TellMaster("I'm not in your guild!");
        return false;
    }

    GuidVector gos = *botAI->GetAiObjectContext()->GetValue<GuidVector>("nearest game objects");
    for (GuidVector::iterator i = gos.begin(); i != gos.end(); ++i)
    {
        GameObject* go = botAI->GetGameObject(*i);
        if (!go || !bot->GetGameObjectIfCanInteractWith(go->GetGUID(), GAMEOBJECT_TYPE_GUILD_BANK))
            continue;

        return Execute(text, go);
    }

    botAI->TellMaster("Cannot find the guild bank nearby");
    return false;
}

bool GuildBankAction::Execute(std::string const text, GameObject* bank)
{
    bool result = true;

    std::vector<Item*> found = parseItems(text);
    if (found.empty())
        return false;

    for (std::vector<Item*>::iterator i = found.begin(); i != found.end(); i++)
    {
        Item* item = *i;
        if (item)
            result &= MoveFromCharToBank(item, bank);
    }

    return result;
}

bool GuildBankAction::MoveFromCharToBank(Item* item, GameObject* /*bank*/)
{
    uint32 playerSlot = item->GetSlot();
    uint32 playerBag = item->GetBagSlot();

    std::ostringstream out;

    Guild* guild = sGuildMgr->GetGuildById(bot->GetGuildId());
    // guild->SwapItems(bot, 0, playerSlot, 0, INVENTORY_SLOT_BAG_0, 0);

    // check source pos rights (item moved to bank)
    if (!guild->MemberHasTabRights(bot->GetGUID(), 0, GUILD_BANK_RIGHT_DEPOSIT_ITEM))
        out << "I can't put " << chat->FormatItem(item->GetTemplate())
            << " to guild bank. I have no rights to put items in the first guild bank tab";
    else
    {
        out << chat->FormatItem(item->GetTemplate()) << " put to guild bank";
        guild->SwapItemsWithInventory(bot, false, 0, 255, playerBag, playerSlot, 0);
    }

    botAI->TellMaster(out);

    return true;
}

bool GuildBankAction::RunCompanionBankDuty(GameObject* bank)
{
    uint32 const guildId = bot->GetGuildId();
    Guild* guild = guildId ? sGuildMgr->GetGuildById(guildId) : nullptr;
    if (!bank || !guild)
        return false;

    // Snapshot first: it lists the purchased tabs and the bank slots that hold items before this visit's moves.
    std::vector<CompanionDuty::BankTabUsage> tabs;
    std::vector<CompanionDuty::BankSlot> slots;
    if (!CompanionDuty::BankSnapshot(guildId, tabs, slots) || tabs.empty())
        return false;

    // Tabs this bot may deposit to (tabs with a free slot first, then ascending) and may withdraw from. A full tab is
    // tried last, so a full tab 0 never blocks the deposit.
    std::vector<uint8> depositTabs;
    std::set<uint8> viewTabs;
    std::vector<uint8> fullTabs;
    for (CompanionDuty::BankTabUsage const& tab : tabs)
    {
        if (guild->MemberHasTabRights(bot->GetGUID(), tab.tab, GUILD_BANK_RIGHT_DEPOSIT_ITEM))
        {
            if (tab.used < static_cast<uint32>(GUILD_BANK_MAX_SLOTS))
                depositTabs.push_back(tab.tab);
            else
                fullTabs.push_back(tab.tab);
        }
        if (guild->MemberHasTabRights(bot->GetGUID(), tab.tab, GUILD_BANK_RIGHT_VIEW_TAB))
            viewTabs.insert(tab.tab);
    }
    depositTabs.insert(depositTabs.end(), fullTabs.begin(), fullTabs.end());
    // Stock limit within this visit: the snapshot count plus what this visit deposited.
    std::map<uint32, uint64> stock;
    for (CompanionDuty::BankSlot const& bankSlot : slots)
        stock[bankSlot.entry] += bankSlot.count;

    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());

    // Deposit: BANK-class items from the backpack and the bags. Each item goes to the first deposit tab that takes it;
    // a full tab leaves the item in the bags and the next tab is tried.
    std::vector<Item*> bagItems;
    for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
        if (Item* item = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
            bagItems.push_back(item);
    for (uint8 bagSlot = INVENTORY_SLOT_BAG_START; bagSlot < INVENTORY_SLOT_BAG_END; ++bagSlot)
    {
        Bag* bag = static_cast<Bag*>(bot->GetItemByPos(INVENTORY_SLOT_BAG_0, bagSlot));
        if (!bag)
            continue;
        for (uint32 i = 0; i < bag->GetBagSize(); ++i)
            if (Item* item = bag->GetItemByPos(i))
                bagItems.push_back(item);
    }

    // Entries deposited in this visit are never withdrawn in it (no same-visit ping-pong).
    std::set<uint32> deposited;
    for (Item* item : bagItems)
    {
        if (CompanionBrain::ClassifyItem(botAI, item) != CompanionBrain::ItemClass::BANK)
            continue;

        // Read everything before the move: the item object is gone after it.
        ObjectGuid itemGuid = item->GetGUID();
        uint32 count = item->GetCount();
        uint32 entry = item->GetEntry();
        std::string name = item->GetTemplate() ? item->GetTemplate()->Name1 : "item";
        uint8 bagSlot = item->GetBagSlot();
        uint8 slot = item->GetSlot();
        uint64 limit = static_cast<uint64>(CompanionBrain::BankMaxStacks()) *
                       std::max<uint32>(1, item->GetTemplate() ? item->GetTemplate()->GetMaxStackSize() : 1);
        uint32 reqMin = 0;
        if (CompanionBrain::IsRequestedMat(entry, &reqMin) && reqMin > limit)
            limit = reqMin;
        if (item->GetTemplate() && item->GetTemplate()->Class == ITEM_CLASS_RECIPE)
            limit = 1;
        uint32 const pendingOrderNeed =
            (!item->IsSoulBound() && CompanionLife::IsGuildBankTransferable(item->GetTemplate()))
                ? CompanionLife::PendingDeliveryQty(bot->GetGUID().GetCounter(), entry)
                : 0;
        bool const pendingOrderDelivery = pendingOrderNeed > 0;
        if (!pendingOrderDelivery && stock[entry] >= limit)
            continue;   // the bank holds enough of it: the sell duty takes it at the next evaluation

        // Try preferred tab (Tab 0 = mats/gems/requested, Tab 1 = consumables/recipes/gear) first, then fallbacks.
        // For claimed guild work-order deliveries (Issue #27), restrict to tabs the requester can view & withdraw
        // from, and always split the deposit stack to the exact pending order remainder so the bot's own kept
        // consumables in the same stack are never stripped into the guild bank.
        uint8 const prefTab =
            CompanionBrain::PreferredBankTab(item->GetTemplate(), static_cast<uint8>(tabs.size()));
        std::vector<uint8> orderedTabs;
        orderedTabs.reserve(depositTabs.size());
        for (uint8 tab : depositTabs)
        {
            if (pendingOrderDelivery &&
                !CompanionLife::RequesterCanAccessBankTab(guild, bot->GetGUID().GetCounter(), entry, tab))
                continue;
            if (tab == prefTab)
                orderedTabs.push_back(tab);
        }
        for (uint8 tab : depositTabs)
        {
            if (pendingOrderDelivery &&
                !CompanionLife::RequesterCanAccessBankTab(guild, bot->GetGUID().GetCounter(), entry, tab))
                continue;
            if (tab != prefTab)
                orderedTabs.push_back(tab);
        }
        if (pendingOrderDelivery && orderedTabs.empty())
        {
            LOG_WARN("playerbots",
                     "[Companion] {} skipped work-order deposit for {} x{}: no guild bank tab accessible to requester",
                     bot->GetName(), name, count);
            continue;
        }

        uint32 const splitAmt = (pendingOrderDelivery && count > pendingOrderNeed) ? pendingOrderNeed : 0;
        uint8 takenTab = TAB_UNDEFINED;
        uint32 movedCount = 0;
        for (uint8 tab : orderedTabs)
        {
            uint32 const beforeEntryCount = bot->GetItemCount(entry, false);
            guild->SwapItemsWithInventory(bot, false, tab, NULL_SLOT, bagSlot, slot, splitAmt);
            uint32 const afterEntryCount = bot->GetItemCount(entry, false);
            if (beforeEntryCount > afterEntryCount)
            {
                movedCount = beforeEntryCount - afterEntryCount;
                takenTab = tab;
                break;
            }
        }

        if (takenTab != TAB_UNDEFINED && movedCount > 0)
        {
            deposited.insert(entry);
            stock[entry] += movedCount;
            LOG_INFO("playerbots", "[Companion] {} deposited {} x{} in guild bank tab {} (time {})", bot->GetName(),
                     name, movedCount, static_cast<uint32>(takenTab), now);
            CompanionDuty::RecordLedger(bot->GetGUID().GetCounter(), "deposit", entry, movedCount, takenTab,
                                        "bank duty");
            if (pendingOrderDelivery)
            {
                static std::atomic<uint64> sDepositCallbackSeq{1};
                uint64 const cbId = sDepositCallbackSeq.fetch_add(1, std::memory_order_relaxed);
                // SwapItemsWithInventory is atomic into takenTab; movedCount is the verified bag->tab delta.
                CompanionLife::OnBankDepositVerified(bot, entry, movedCount, movedCount, takenTab, cbId);
            }
        }
        else
            LOG_WARN("playerbots", "[Companion] {} could not deposit {} x{} in any guild bank tab", bot->GetName(),
                     name, count);
    }

    // Withdraw: trade goods this bot's professions use, capped at WithdrawCap stacks of each item in the bags. Any tab
    // the bot may view; never an entry deposited in this visit.
    uint64 const capStacks = CompanionBrain::WithdrawCapStacks();
    for (CompanionDuty::BankSlot const& bankSlot : slots)
    {
        if (!viewTabs.count(bankSlot.tab) || deposited.count(bankSlot.entry))
            continue;

        ItemUsage usage =
            botAI->GetAiObjectContext()->GetValue<ItemUsage>("item usage", std::to_string(bankSlot.entry))->Get();
        if (usage != ITEM_USAGE_SKILL)
            continue;

        ItemTemplate const* proto = sObjectMgr->GetItemTemplate(bankSlot.entry);
        if (!proto || proto->Class != ITEM_CLASS_TRADE_GOODS)
            continue;

        uint64 const room = static_cast<uint64>(proto->GetMaxStackSize()) * capStacks;
        uint32 const have = bot->GetItemCount(bankSlot.entry, false);
        if (have >= room)
            continue;
        // Protect guild-bank stock reserved for other requesters' delivered/partial work orders (Issue #27).
        uint32 const reservedForOthers =
            CompanionLife::ReservedBankQty(bankSlot.entry, bot->GetGUID().GetCounter());
        uint64 const availStock = stock[bankSlot.entry] > reservedForOthers
                                      ? (stock[bankSlot.entry] - reservedForOthers)
                                      : 0;
        if (availStock == 0)
            continue;
        uint32 const take = static_cast<uint32>(std::min<uint64>({bankSlot.count, room - have, availStock}));
        if (!take)
            continue;

        uint8 freeSlot = NULL_SLOT;
        for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
        {
            if (!bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
            {
                freeSlot = slot;
                break;
            }
        }
        if (freeSlot == NULL_SLOT)
            break;   // backpack full

        // A split when the bank stack is bigger than the room left (splitedAmount 0 moves the whole stack).
        guild->SwapItemsWithInventory(bot, true, bankSlot.tab, bankSlot.slot, INVENTORY_SLOT_BAG_0, freeSlot,
                                      take < bankSlot.count ? take : 0);

        uint32 const after = bot->GetItemCount(bankSlot.entry, false);
        uint32 const got = after > have ? after - have : 0;
        if (got)
        {
            stock[bankSlot.entry] = stock[bankSlot.entry] > got ? (stock[bankSlot.entry] - got) : 0;
            LOG_INFO("playerbots", "[Companion] {} withdrew item {} x{} from guild bank tab {} (time {})",
                     bot->GetName(), bankSlot.entry, got, static_cast<uint32>(bankSlot.tab), now);
            CompanionDuty::RecordLedger(bot->GetGUID().GetCounter(), "withdraw", bankSlot.entry, got, bankSlot.tab,
                                        "bank duty");
        }
        else
            LOG_WARN("playerbots", "[Companion] {} withdraw of item {} from guild bank tab {} slot {} moved nothing",
                     bot->GetName(), bankSlot.entry, static_cast<uint32>(bankSlot.tab),
                     static_cast<uint32>(bankSlot.slot));
    }

    // Class play consumables (CompanionClass::WantedConsumables, e.g. Minor Mana Oil, Smoked Sagefish): topped up to
    // the listed count when the bank has them.
    for (auto const& [entry, wanted] : CompanionClass::WantedConsumables(bot))
    {
        uint32 const have = bot->GetItemCount(entry, false);
        if (have < wanted && !deposited.count(entry))
            WithdrawForCompanion(entry, wanted - have, "consumables");
    }

    // Auto-organize guild bank (merge split stacks, sort Tab 0 mats vs Tab 1 consumables/recipes/gear, and pull
    // non-whitelisted clutter into free bag slots so the bot vendors it on its next sell visit).
    if (sPlayerbotAIConfig.companionBankAutoOrganize)
        OrganizeCompanionBank(false);

    // The bank changed: the snapshot is stale until the next refresh.
    CompanionDuty::InvalidateBank(guildId);
    return true;
}

bool GuildBankAction::OrganizeCompanionBank(bool force, uint32* outPurged, uint32* outMerged, uint32* outSorted)
{
    if (outPurged)
        *outPurged = 0;
    if (outMerged)
        *outMerged = 0;
    if (outSorted)
        *outSorted = 0;

    uint32 const guildId = bot ? bot->GetGuildId() : 0;
    Guild* guild = guildId ? sGuildMgr->GetGuildById(guildId) : nullptr;
    if (!guild)
        return false;

    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    if (!force)
    {
        std::lock_guard<std::mutex> lock(gOrganizeMutex);
        auto it = gLastOrganizeSec.find(guildId);
        if (it != gLastOrganizeSec.end() && now - it->second < ORGANIZE_COOLDOWN_SEC)
            return false;
        gLastOrganizeSec[guildId] = now;
    }

    std::vector<CompanionDuty::BankTabUsage> tabs;
    std::vector<CompanionDuty::BankSlot> slots;
    if (!CompanionDuty::BankSnapshot(guildId, tabs, slots) || tabs.empty())
        return false;

    uint8 const tabCount = static_cast<uint8>(tabs.size());
    struct Cell
    {
        uint32 entry = 0;
        uint32 count = 0;
    };
    std::vector<std::array<Cell, GUILD_BANK_MAX_SLOTS>> grid(tabCount);
    std::vector<bool> canView(tabCount, false);
    std::vector<bool> canDeposit(tabCount, false);
    for (CompanionDuty::BankTabUsage const& t : tabs)
    {
        if (t.tab >= tabCount)
            continue;
        canView[t.tab] = guild->MemberHasTabRights(bot->GetGUID(), t.tab, GUILD_BANK_RIGHT_VIEW_TAB);
        canDeposit[t.tab] = guild->MemberHasTabRights(bot->GetGUID(), t.tab, GUILD_BANK_RIGHT_DEPOSIT_ITEM);
    }
    for (CompanionDuty::BankSlot const& s : slots)
    {
        if (s.tab < tabCount && s.slot < GUILD_BANK_MAX_SLOTS)
            grid[s.tab][s.slot] = {s.entry, s.count};
    }

    auto findFreeBagSlot = [this](uint8& outBag, uint8& outSlot) -> bool
    {
        for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
            if (!bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
            {
                outBag = INVENTORY_SLOT_BAG_0;
                outSlot = slot;
                return true;
            }
        for (uint8 bagSlot = INVENTORY_SLOT_BAG_START; bagSlot < INVENTORY_SLOT_BAG_END; ++bagSlot)
        {
            Bag* bag = static_cast<Bag*>(bot->GetItemByPos(INVENTORY_SLOT_BAG_0, bagSlot));
            if (!bag)
                continue;
            for (uint32 i = 0; i < bag->GetBagSize(); ++i)
                if (!bag->GetItemByPos(static_cast<uint8>(i)))
                {
                    outBag = bagSlot;
                    outSlot = static_cast<uint8>(i);
                    return true;
                }
        }
        return false;
    };

    uint32 purged = 0;
    uint32 merged = 0;
    uint32 sorted = 0;

    // 1. Purge non-whitelisted clutter (and duplicate recipes / excess stacks over limit) into free bag slots so the
    // bot sells them to a vendor on its next SELL duty.
    std::map<uint32, uint64> keptStock;
    uint32 const maxPurge = force ? 24 : MAX_PURGE_PER_VISIT;
    for (uint8 tab = 0; tab < tabCount && purged < maxPurge; ++tab)
    {
        if (!canView[tab])
            continue;
        for (uint8 slot = 0; slot < GUILD_BANK_MAX_SLOTS && purged < maxPurge; ++slot)
        {
            Cell& cell = grid[tab][slot];
            if (!cell.entry)
                continue;
            ItemTemplate const* proto = sObjectMgr->GetItemTemplate(cell.entry);
            if (!proto)
                continue;

            uint32 const reservedQty = CompanionLife::ReservedBankQty(cell.entry, 0);
            bool unwanted = !CompanionBrain::IsBankWantedItem(proto);
            if (reservedQty > 0 && keptStock[cell.entry] < reservedQty)
            {
                // Keep reserved guild work-order stock intact so auto-organize never purges/vendors it (Issue #27).
                unwanted = false;
                keptStock[cell.entry] += cell.count;
            }
            else if (!unwanted)
            {
                uint32 reqMin = 0;
                bool const requested = CompanionBrain::IsRequestedMat(cell.entry, &reqMin);
                uint64 limit = static_cast<uint64>(CompanionBrain::BankMaxStacks()) *
                               std::max<uint32>(1, proto->GetMaxStackSize());
                if (proto->Class == ITEM_CLASS_RECIPE)
                    limit = 1;
                if (requested && reqMin > limit)
                    limit = reqMin;
                limit += reservedQty;
                if (keptStock[cell.entry] >= limit)
                    unwanted = true;
                else
                    keptStock[cell.entry] += cell.count;
            }
            if (!unwanted)
                continue;
            // Only pull items that a vendor will actually buy (or if force, clear unsellable junk too if needed).
            if (proto->SellPrice == 0 && !force)
                continue;
            if (bot->GetFreeInventorySpace() <= 2)
                break;

            uint8 toBag = NULL_BAG;
            uint8 toSlot = NULL_SLOT;
            if (!findFreeBagSlot(toBag, toSlot))
                break;

            uint32 const haveBefore = bot->GetItemCount(cell.entry, false);
            guild->SwapItemsWithInventory(bot, true, tab, slot, toBag, toSlot, 0);
            uint32 const haveAfter = bot->GetItemCount(cell.entry, false);
            if (haveAfter > haveBefore)
            {
                uint32 const movedCount = haveAfter - haveBefore;
                LOG_INFO("playerbots",
                         "[Companion] {} bank organize: pulled junk {} x{} from tab {} slot {} for vendoring",
                         bot->GetName(), proto->Name1, movedCount, static_cast<uint32>(tab), static_cast<uint32>(slot));
                CompanionDuty::RecordLedger(bot->GetGUID().GetCounter(), "withdraw", cell.entry, movedCount, tab,
                                            "organize-sell");
                cell.entry = 0;
                cell.count = 0;
                ++purged;
            }
        }
    }

    // 2. Consolidate partial stacks of the same item across all tabs (requires view+deposit rights).
    for (uint8 srcTab = 0; srcTab < tabCount; ++srcTab)
    {
        if (!canView[srcTab] || !canDeposit[srcTab])
            continue;
        for (uint8 srcSlot = 0; srcSlot < GUILD_BANK_MAX_SLOTS; ++srcSlot)
        {
            Cell& src = grid[srcTab][srcSlot];
            if (!src.entry)
                continue;
            ItemTemplate const* proto = sObjectMgr->GetItemTemplate(src.entry);
            uint32 const maxStack = proto ? std::max<uint32>(1, proto->GetMaxStackSize()) : 1;
            if (maxStack <= 1 || src.count >= maxStack)
                continue;

            // Find an earlier/preferred partial stack of the same item to merge into (never move reserved work-order
            // stock across tabs so it stays in the requester-accessible delivery tab, Issue #27).
            bool const reservedItem = CompanionLife::ReservedBankQty(src.entry, 0) > 0;
            for (uint8 dstTab = 0; dstTab < tabCount && src.count > 0; ++dstTab)
            {
                if (!canView[dstTab] || !canDeposit[dstTab] || (reservedItem && dstTab != srcTab))
                    continue;
                for (uint8 dstSlot = 0; dstSlot < GUILD_BANK_MAX_SLOTS && src.count > 0; ++dstSlot)
                {
                    if (dstTab == srcTab && dstSlot == srcSlot)
                        continue;
                    Cell& dst = grid[dstTab][dstSlot];
                    if (dst.entry != src.entry || dst.count >= maxStack)
                        continue;
                    uint32 const space = maxStack - dst.count;
                    uint32 const moveAmt = std::min(space, src.count);
                    guild->SwapItems(bot, srcTab, srcSlot, dstTab, dstSlot, moveAmt < src.count ? moveAmt : 0);
                    dst.count += moveAmt;
                    src.count -= moveAmt;
                    if (src.count == 0)
                        src.entry = 0;
                    ++merged;
                }
            }
        }
    }

    // 3. Sort items into preferred tabs (Tab 0 = profession mats/gems/requested, Tab 1 = consumables/recipes/gear).
    if (tabCount >= 2)
    {
        auto findEmptySlot = [&](uint8 tab) -> uint8
        {
            for (uint8 s = 0; s < GUILD_BANK_MAX_SLOTS; ++s)
                if (grid[tab][s].entry == 0)
                    return s;
            return NULL_SLOT;
        };

        for (uint8 srcTab = 0; srcTab < tabCount; ++srcTab)
        {
            if (!canView[srcTab] || !canDeposit[srcTab])
                continue;
            for (uint8 srcSlot = 0; srcSlot < GUILD_BANK_MAX_SLOTS; ++srcSlot)
            {
                Cell& src = grid[srcTab][srcSlot];
                if (!src.entry || CompanionLife::ReservedBankQty(src.entry, 0) > 0)
                    continue;
                ItemTemplate const* proto = sObjectMgr->GetItemTemplate(src.entry);
                uint8 const wantTab = CompanionBrain::PreferredBankTab(proto, tabCount);
                if (wantTab == srcTab || wantTab >= tabCount || !canDeposit[wantTab])
                    continue;
                uint8 const dstSlot = findEmptySlot(wantTab);
                if (dstSlot == NULL_SLOT)
                    continue;
                guild->SwapItems(bot, srcTab, srcSlot, wantTab, dstSlot, 0);
                grid[wantTab][dstSlot] = src;
                src = {0, 0};
                ++sorted;
            }
        }
    }

    if (purged || merged || sorted)
    {
        CompanionDuty::InvalidateBank(guildId);
        LOG_INFO("playerbots", "[Companion] {} bank organize summary: purged={} merged={} sorted={}", bot->GetName(),
                 purged, merged, sorted);
    }
    if (outPurged)
        *outPurged = purged;
    if (outMerged)
        *outMerged = merged;
    if (outSorted)
        *outSorted = sorted;
    return (purged + merged + sorted) > 0;
}

uint32 GuildBankAction::WithdrawForCompanion(uint32 entry, uint32 count, char const* note)
{
    uint32 const guildId = bot->GetGuildId();
    Guild* guild = guildId ? sGuildMgr->GetGuildById(guildId) : nullptr;
    if (!guild || !entry || !count)
        return 0;
    std::vector<CompanionDuty::BankTabUsage> tabs;
    std::vector<CompanionDuty::BankSlot> slots;
    if (!CompanionDuty::BankSnapshot(guildId, tabs, slots))
        return 0;

    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    uint32 moved = 0;
    for (CompanionDuty::BankSlot const& bankSlot : slots)
    {
        if (moved >= count)
            break;
        if (bankSlot.entry != entry ||
            !guild->MemberHasTabRights(bot->GetGUID(), bankSlot.tab, GUILD_BANK_RIGHT_VIEW_TAB))
            continue;

        // A free slot in the backpack or a bag.
        uint8 toBag = NULL_BAG;
        uint8 toSlot = NULL_SLOT;
        for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END && toSlot == NULL_SLOT; ++slot)
            if (!bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
            {
                toBag = INVENTORY_SLOT_BAG_0;
                toSlot = slot;
            }
        for (uint8 bagSlot = INVENTORY_SLOT_BAG_START; bagSlot < INVENTORY_SLOT_BAG_END && toSlot == NULL_SLOT;
             ++bagSlot)
        {
            Bag* bag = static_cast<Bag*>(bot->GetItemByPos(INVENTORY_SLOT_BAG_0, bagSlot));
            if (!bag)
                continue;
            for (uint32 i = 0; i < bag->GetBagSize() && toSlot == NULL_SLOT; ++i)
                if (!bag->GetItemByPos(static_cast<uint8>(i)))
                {
                    toBag = bagSlot;
                    toSlot = static_cast<uint8>(i);
                }
        }
        if (toSlot == NULL_SLOT)
            break;   // bags full

        uint32 const take = std::min(count - moved, bankSlot.count);
        uint32 const have = bot->GetItemCount(entry, false);
        guild->SwapItemsWithInventory(bot, true, bankSlot.tab, bankSlot.slot, toBag, toSlot,
                                      take < bankSlot.count ? take : 0);
        uint32 const after = bot->GetItemCount(entry, false);
        uint32 const got = after > have ? after - have : 0;
        if (!got)
        {
            LOG_WARN("playerbots", "[Companion] {} withdraw of item {} from guild bank tab {} slot {} moved nothing",
                     bot->GetName(), entry, static_cast<uint32>(bankSlot.tab), static_cast<uint32>(bankSlot.slot));
            continue;
        }
        moved += got;
        LOG_INFO("playerbots", "[Companion] {} withdrew item {} x{} from guild bank tab {} (time {}) for {}",
                 bot->GetName(), entry, got, static_cast<uint32>(bankSlot.tab), now, note);
        CompanionDuty::RecordLedger(bot->GetGUID().GetCounter(), "withdraw", entry, got, bankSlot.tab, note);
    }
    return moved;   // the caller invalidates the bank snapshot after its last move
}
