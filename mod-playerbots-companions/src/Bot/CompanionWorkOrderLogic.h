/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANION_WORK_ORDER_LOGIC_H
#define PLAYERBOTS_COMPANION_WORK_ORDER_LOGIC_H

#include <algorithm>
#include <cstdint>
#include <deque>
#include <functional>
#include <string>
#include <unordered_set>
#include <vector>

// Pure C++17 work-order claim, production, legal bank delivery, reservation, and proposal-completion state machine
// (Issue #27). Zero AzerothCore dependencies so both CompanionLife.cpp and standalone native unit tests execute
// the exact same verification logic.
namespace CompanionWorkOrderLogic
{
inline constexpr std::uint8_t TAB_NONE = 255;
inline constexpr std::size_t MAX_CALLBACK_CACHE = 2048;
inline constexpr std::uint32_t DELIVERY_RESERVE_TTL_SEC = 172800;   // 48h reservation window for delivered orders

struct OrderState
{
    std::uint64_t id = 0;
    std::uint32_t requesterGuid = 0;
    std::string requesterName;
    std::uint32_t crafterGuid = 0;
    std::string crafterName;
    std::uint64_t attemptId = 0;
    std::uint32_t itemEntry = 0;
    std::string itemName;
    std::uint32_t quantity = 1;         // requested quantity
    std::uint32_t producedQty = 0;      // verified crafted quantity for this attempt
    std::uint32_t transferredQty = 0;   // verified quantity removed from crafter bags into guild bank
    std::uint32_t receivedQty = 0;      // verified quantity received into requester-accessible guild bank tab
    std::uint8_t deliveryTab = TAB_NONE;
    std::string story;
    std::string status = "open";        // open | claimed | delivered | expired
    std::uint32_t revision = 1;
    std::uint32_t createdAt = 0;
    std::uint32_t updatedAt = 0;
};

struct ProposalState
{
    std::uint64_t proposalId = 0;
    std::uint32_t crafterGuid = 0;
    std::uint64_t orderId = 0;
    std::uint32_t itemEntry = 0;
    std::uint32_t snapshotRev = 1;
    std::string status;
    std::uint32_t expiresAt = 0;
};

struct ActionResult
{
    bool ok = false;
    bool duplicate = false;
    bool stateChanged = false;
    bool completedNow = false;   // true ONLY on the single atomic claimed -> delivered transition
    std::uint64_t orderId = 0;
    std::uint64_t attemptId = 0;
    std::uint32_t expectedRevision = 0;
    std::uint32_t newRevision = 0;
    std::uint32_t creditedQty = 0;
    std::uint32_t creditedReceivedQty = 0;
    std::string previousStatus;
    OrderState order;
    std::string reason;
};

class BoundedIdSet
{
public:
    bool Contains(std::uint64_t id) const { return id != 0 && _set.count(id) > 0; }

    void Insert(std::uint64_t id)
    {
        if (id == 0 || !_set.insert(id).second)
            return;
        _fifo.push_back(id);
        while (_fifo.size() > MAX_CALLBACK_CACHE)
        {
            _set.erase(_fifo.front());
            _fifo.pop_front();
        }
    }

    void Clear()
    {
        _set.clear();
        _fifo.clear();
    }

private:
    std::unordered_set<std::uint64_t> _set;
    std::deque<std::uint64_t> _fifo;
};

// Validates whether a native proposal may transition to 'completed' upon verified order delivery.
inline bool CanCompleteProposal(ProposalState const& prop, std::uint64_t completedOrderId, std::uint32_t crafterGuid,
                                std::uint32_t nowSec)
{
    if (prop.proposalId == 0 || completedOrderId == 0 || crafterGuid == 0)
        return false;
    if (prop.crafterGuid != crafterGuid || prop.orderId != completedOrderId)
        return false;
    if (prop.status != "accepted" && prop.status != "executing")
        return false;
    if (prop.expiresAt <= nowSec)
        return false;
    return true;
}

class Tracker
{
public:
    void LoadOrders(std::vector<OrderState> orders, std::uint64_t maxPersistedAttemptId = 0)
    {
        if (maxPersistedAttemptId >= _nextAttemptId)
            _nextAttemptId = maxPersistedAttemptId + 1;
        for (OrderState const& wo : orders)
            if (wo.attemptId >= _nextAttemptId)
                _nextAttemptId = wo.attemptId + 1;
        _orders = std::move(orders);
    }

    // Restores authoritative DB state for an order when a guarded DB CAS update did not match (e.g. expiry race).
    void RollbackOrReplaceOrder(OrderState const& authoritative)
    {
        if (authoritative.attemptId >= _nextAttemptId)
            _nextAttemptId = authoritative.attemptId + 1;
        for (OrderState& wo : _orders)
        {
            if (wo.id == authoritative.id)
            {
                wo = authoritative;
                return;
            }
        }
        _orders.push_back(authoritative);
    }

    std::vector<OrderState> const& Orders() const { return _orders; }
    std::vector<OrderState>& OrdersMutable() { return _orders; }

    void ResetCallbacks()
    {
        _craftCallbacks.Clear();
        _deliveryCallbacks.Clear();
    }

    bool HasOpenOrderFor(std::uint32_t itemEntry, std::uint32_t crafterGuid) const
    {
        if (!itemEntry || !crafterGuid)
            return false;
        for (OrderState const& wo : _orders)
        {
            if (wo.id == 0 || wo.itemEntry != itemEntry || wo.requesterGuid == crafterGuid)
                continue;
            if (wo.status == "open" && wo.crafterGuid == 0)
                return true;
            if (wo.status == "claimed" && wo.crafterGuid == crafterGuid && wo.producedQty < wo.quantity)
                return true;
        }
        return false;
    }

    bool HasOpenOrderById(std::uint64_t orderId, std::uint32_t crafterGuid, std::uint32_t& outItemEntry) const
    {
        outItemEntry = 0;
        if (!orderId || !crafterGuid)
            return false;
        for (OrderState const& wo : _orders)
        {
            if (wo.id != orderId || wo.requesterGuid == crafterGuid)
                continue;
            if ((wo.status == "open" && wo.crafterGuid == 0) ||
                (wo.status == "claimed" && wo.crafterGuid == crafterGuid && wo.receivedQty < wo.quantity))
            {
                outItemEntry = wo.itemEntry;
                return true;
            }
        }
        return false;
    }

    // Returns exact remaining produced-but-undelivered quantity for this crafter's claimed order.
    std::uint32_t PendingDeliveryQty(std::uint32_t crafterGuid, std::uint32_t itemEntry,
                                     std::uint32_t* outRequesterGuid = nullptr) const
    {
        if (outRequesterGuid)
            *outRequesterGuid = 0;
        if (!crafterGuid || !itemEntry)
            return 0;
        for (OrderState const& wo : _orders)
        {
            if (wo.id == 0 || wo.status != "claimed" || wo.crafterGuid != crafterGuid || wo.itemEntry != itemEntry)
                continue;
            if (wo.producedQty > wo.transferredQty && wo.receivedQty < wo.quantity)
            {
                if (outRequesterGuid)
                    *outRequesterGuid = wo.requesterGuid;
                return std::min(wo.producedQty - wo.transferredQty, wo.quantity - wo.receivedQty);
            }
        }
        return 0;
    }

    bool HasPendingDeliveryFor(std::uint32_t crafterGuid, std::uint32_t itemEntry,
                               std::uint32_t* outRequesterGuid = nullptr) const
    {
        return PendingDeliveryQty(crafterGuid, itemEntry, outRequesterGuid) > 0;
    }

    // Quantity of itemEntry currently reserved in the guild bank for requesters of active partial or recent
    // delivered work orders. Prevents bank auto-organize/sell/withdraw from stripping delivered order stock.
    std::uint32_t ReservedBankQty(std::uint32_t itemEntry, std::uint32_t nowSec,
                                  std::uint32_t excludeRequesterGuid = 0) const
    {
        if (!itemEntry)
            return 0;
        std::uint64_t total = 0;
        for (OrderState const& wo : _orders)
        {
            if (wo.id == 0 || wo.itemEntry != itemEntry || wo.receivedQty == 0)
                continue;
            if (excludeRequesterGuid && wo.requesterGuid == excludeRequesterGuid)
                continue;
            if (wo.status == "claimed")
                total += wo.receivedQty;
            else if (wo.status == "delivered" &&
                     (nowSec == 0 || wo.updatedAt + DELIVERY_RESERVE_TTL_SEC >= nowSec))
                total += wo.receivedQty;
        }
        return total > UINT32_MAX ? UINT32_MAX : static_cast<std::uint32_t>(total);
    }

    ActionResult ClaimOrder(std::uint64_t orderId, std::uint32_t crafterGuid, std::string const& crafterName,
                            bool itemIsTransferable, bool recipientInSameGuild, std::uint32_t nowSec)
    {
        ActionResult res;
        if (orderId == 0)
        {
            res.reason = "invalid_order_id";
            return res;
        }
        if (crafterGuid == 0)
        {
            res.reason = "invalid_crafter";
            return res;
        }
        if (!itemIsTransferable)
        {
            res.reason = "bound_item";
            return res;
        }
        if (!recipientInSameGuild)
        {
            res.reason = "recipient_missing";
            return res;
        }
        OrderState* target = FindById(orderId);
        if (!target)
        {
            res.reason = "no_matching_order";
            return res;
        }
        if (target->requesterGuid == crafterGuid)
        {
            res.reason = "self_order";
            return res;
        }
        if (target->status == "claimed")
        {
            if (target->crafterGuid == crafterGuid)
            {
                res.ok = true;
                res.stateChanged = false;
                res.orderId = target->id;
                res.attemptId = target->attemptId;
                res.expectedRevision = target->revision;
                res.newRevision = target->revision;
                res.previousStatus = target->status;
                res.order = *target;
                res.reason = "already_claimed_by_self";
                return res;
            }
            res.reason = "claimed_by_other";
            return res;
        }
        if (target->status != "open" || target->crafterGuid != 0)
        {
            res.reason = "order_not_open";
            return res;
        }

        res.previousStatus = target->status;
        res.expectedRevision = target->revision;
        target->status = "claimed";
        target->crafterGuid = crafterGuid;
        target->crafterName = crafterName;
        target->attemptId = NextAttemptId();
        target->revision += 1;
        target->updatedAt = nowSec;

        res.ok = true;
        res.stateChanged = true;
        res.orderId = target->id;
        res.attemptId = target->attemptId;
        res.newRevision = target->revision;
        res.order = *target;
        res.reason = "claimed";
        return res;
    }

    // Records verified crafted output toward an eligible order. NEVER marks delivered; crafting into own bags
    // is production only and requires verified guild-bank transfer & accessible receipt before delivery completes.
    ActionResult RecordProduction(std::uint32_t crafterGuid, std::string const& crafterName, std::uint32_t itemEntry,
                                  std::uint32_t producedDelta, std::uint64_t preferredOrderId, bool itemIsTransferable,
                                  std::function<bool(std::uint32_t)> const& recipientInSameGuildFn,
                                  std::uint64_t callbackId, std::uint32_t nowSec)
    {
        ActionResult res;
        if (!crafterGuid || !itemEntry)
        {
            res.reason = "invalid_args";
            return res;
        }
        if (producedDelta == 0)
        {
            res.reason = "zero_quantity";
            return res;
        }
        if (!itemIsTransferable)
        {
            res.reason = "bound_item";
            return res;
        }
        if (_craftCallbacks.Contains(callbackId))
        {
            res.ok = true;
            res.duplicate = true;
            res.reason = "duplicate_callback";
            return res;
        }

        OrderState* target = nullptr;
        if (preferredOrderId > 0)
        {
            OrderState* pref = FindById(preferredOrderId);
            if (!pref || pref->itemEntry != itemEntry)
            {
                res.reason = "preferred_order_unavailable";
                return res;
            }
            if (pref->requesterGuid == crafterGuid)
            {
                res.reason = "self_order";
                return res;
            }
            if (pref->status == "claimed" && pref->crafterGuid != crafterGuid)
            {
                res.reason = "claimed_by_other";
                return res;
            }
            if ((pref->status == "open" && pref->crafterGuid == 0) ||
                (pref->status == "claimed" && pref->crafterGuid == crafterGuid))
            {
                target = pref;
            }
            else
            {
                res.reason = "order_not_open";
                return res;
            }
        }
        else
        {
            // 1. Prefer an active order already claimed by THIS crafter that still needs production.
            for (OrderState& wo : _orders)
            {
                if (wo.id > 0 && wo.status == "claimed" && wo.crafterGuid == crafterGuid && wo.itemEntry == itemEntry &&
                    wo.requesterGuid != crafterGuid && wo.producedQty < wo.quantity)
                {
                    target = &wo;
                    break;
                }
            }
            // 2. Otherwise claim an unclaimed open order (NEVER overwrite another crafter's claim).
            if (!target)
            {
                for (OrderState& wo : _orders)
                {
                    if (wo.id > 0 && wo.status == "open" && wo.crafterGuid == 0 && wo.itemEntry == itemEntry &&
                        wo.requesterGuid != crafterGuid)
                    {
                        target = &wo;
                        break;
                    }
                }
            }
        }

        if (!target)
        {
            res.reason = "no_matching_order";
            return res;
        }
        if (!recipientInSameGuildFn || !recipientInSameGuildFn(target->requesterGuid))
        {
            res.reason = "recipient_missing";
            return res;
        }

        std::uint32_t const remaining =
            target->quantity > target->producedQty ? (target->quantity - target->producedQty) : 0;
        if (remaining == 0)
        {
            res.ok = true;
            res.stateChanged = false;
            res.orderId = target->id;
            res.attemptId = target->attemptId;
            res.expectedRevision = target->revision;
            res.newRevision = target->revision;
            res.previousStatus = target->status;
            res.order = *target;
            res.reason = "already_produced_full";
            return res;
        }

        std::uint32_t const credited = std::min(remaining, producedDelta);
        res.previousStatus = target->status;
        res.expectedRevision = target->revision;
        if (target->status == "open")
        {
            target->status = "claimed";
            target->crafterGuid = crafterGuid;
            target->crafterName = crafterName;
            target->attemptId = NextAttemptId();
        }
        target->producedQty += credited;
        target->revision += 1;
        target->updatedAt = nowSec;
        _craftCallbacks.Insert(callbackId);

        res.ok = true;
        res.stateChanged = true;
        res.completedNow = false;   // crafting for oneself is NEVER delivery
        res.orderId = target->id;
        res.attemptId = target->attemptId;
        res.newRevision = target->revision;
        res.creditedQty = credited;
        res.order = *target;
        res.reason = target->producedQty >= target->quantity ? "produced_ready_for_delivery" : "produced_partial";
        return res;
    }

    // Records verified legal guild-bank deposit toward THIS crafter's claimed order, tracking transferredQty
    // (bag removal) and receivedQty (accessible bank tab receipt) separately.
    ActionResult RecordBankDelivery(std::uint32_t crafterGuid, std::uint32_t itemEntry, std::uint32_t transferredDelta,
                                    std::uint32_t receivedDelta, std::uint8_t bankTab, bool itemIsTransferable,
                                    std::function<bool(std::uint32_t)> const& recipientInSameGuildFn,
                                    std::function<bool(std::uint32_t, std::uint8_t)> const& recipientCanAccessTabFn,
                                    std::uint64_t callbackId, std::uint32_t nowSec)
    {
        ActionResult res;
        if (!crafterGuid || !itemEntry)
        {
            res.reason = "invalid_args";
            return res;
        }
        if (transferredDelta == 0)
        {
            res.reason = "zero_quantity";
            return res;
        }
        if (bankTab == TAB_NONE)
        {
            res.reason = "bank_full";
            return res;
        }
        if (!itemIsTransferable)
        {
            res.reason = "bound_item";
            return res;
        }
        if (_deliveryCallbacks.Contains(callbackId))
        {
            res.ok = true;
            res.duplicate = true;
            res.reason = "duplicate_callback";
            return res;
        }

        // Match the first active claimed order with produced items still awaiting delivery (matches PendingDeliveryQty).
        OrderState* target = nullptr;
        bool foundClaimedWithoutProd = false;
        for (OrderState& wo : _orders)
        {
            if (wo.id > 0 && wo.status == "claimed" && wo.crafterGuid == crafterGuid && wo.itemEntry == itemEntry &&
                wo.requesterGuid != crafterGuid && wo.receivedQty < wo.quantity)
            {
                if (wo.producedQty > wo.transferredQty)
                {
                    target = &wo;
                    break;
                }
                foundClaimedWithoutProd = true;
            }
        }
        if (!target)
        {
            res.reason = foundClaimedWithoutProd ? "nothing_produced_to_deliver" : "no_claimed_order";
            return res;
        }
        if (!recipientInSameGuildFn || !recipientInSameGuildFn(target->requesterGuid))
        {
            res.reason = "recipient_missing";
            return res;
        }
        if (!recipientCanAccessTabFn || !recipientCanAccessTabFn(target->requesterGuid, bankTab))
        {
            res.reason = "tab_inaccessible";
            return res;
        }

        std::uint32_t const deliverable = target->producedQty - target->transferredQty;
        std::uint32_t const remTransfer =
            target->quantity > target->transferredQty ? (target->quantity - target->transferredQty) : 0;
        std::uint32_t const creditT = std::min({transferredDelta, deliverable, remTransfer});
        if (creditT == 0)
        {
            res.reason = "nothing_remaining";
            return res;
        }

        std::uint32_t const newTransferred = target->transferredQty + creditT;
        std::uint32_t const remReceive =
            target->quantity > target->receivedQty ? (target->quantity - target->receivedQty) : 0;
        std::uint32_t const maxReceivable =
            newTransferred > target->receivedQty ? (newTransferred - target->receivedQty) : 0;
        std::uint32_t const creditR = std::min({receivedDelta, remReceive, maxReceivable});

        res.previousStatus = target->status;
        res.expectedRevision = target->revision;
        target->transferredQty = newTransferred;
        target->receivedQty += creditR;
        target->deliveryTab = bankTab;
        target->revision += 1;
        target->updatedAt = nowSec;

        bool const fullyDelivered = target->producedQty >= target->quantity &&
                                    target->transferredQty >= target->quantity &&
                                    target->receivedQty >= target->quantity;
        if (fullyDelivered)
            target->status = "delivered";

        _deliveryCallbacks.Insert(callbackId);

        res.ok = true;
        res.stateChanged = true;
        res.completedNow = fullyDelivered;
        res.orderId = target->id;
        res.attemptId = target->attemptId;
        res.newRevision = target->revision;
        res.creditedQty = creditT;
        res.creditedReceivedQty = creditR;
        res.order = *target;
        res.reason = fullyDelivered ? "delivered_complete" : "delivered_partial";
        return res;
    }

private:
    OrderState* FindById(std::uint64_t orderId)
    {
        for (OrderState& wo : _orders)
            if (wo.id == orderId)
                return &wo;
        return nullptr;
    }

    std::uint64_t NextAttemptId()
    {
        if (_nextAttemptId == 0)
            _nextAttemptId = 1;
        return _nextAttemptId++;
    }

    std::vector<OrderState> _orders;
    std::uint64_t _nextAttemptId = 1;
    BoundedIdSet _craftCallbacks;
    BoundedIdSet _deliveryCallbacks;
};

}  // namespace CompanionWorkOrderLogic

#endif
