/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_GUILDBANKACTION_H
#define PLAYERBOTS_GUILDBANKACTION_H

#include <set>
#include <vector>

#include "CompanionDuty.h"
#include "InventoryAction.h"

class GameObject;
class Item;
class PlayerbotAI;

class GuildBankAction : public InventoryAction
{
public:
    GuildBankAction(PlayerbotAI* botAI) : InventoryAction(botAI, "guild bank") {}

    bool Execute(Event event) override;

    // Companion duty at a guild bank object. Deposits BANK-class items, then withdraws trade goods the bot's own
    // professions use (SKILL), up to AiPlayerbot.Companion.Bank.WithdrawCap stacks of each. Logs every move.
    bool RunCompanionBankDuty(GameObject* bank);
    // Guild bank organizer: consolidates split stacks, sorts Tab 0 (mats/gems/requested) vs Tab 1 (consumables/recipes/gear),
    // and withdraws non-whitelisted clutter into the bot's free bag slots so the bot vendors it on its next sell visit.
    bool OrganizeCompanionBank(bool force = false, uint32* outPurged = nullptr, uint32* outMerged = nullptr,
                               uint32* outSorted = nullptr);
    // Companion (craft duty, consumables): withdraws up to count of entry from the guild bank tabs the bot may view,
    // into free bag slots, at a guild bank the bot stands at. Returns the number of items moved. Logs and writes a
    // companion_ledger row "withdraw" with note (a code literal).
    // The caller calls CompanionDuty::InvalidateBank after its last move (the snapshot is read here).
    uint32 WithdrawForCompanion(uint32 entry, uint32 count, char const* note);
    // True when the bank snapshot holds a recipe, bag or gear upgrade this companion would take, and it has not tried
    // in the last CompanionBankPickup::RETRY_SECONDS. Switch: AiPlayerbot.Companion.Bank.Pickup (default 1).
    bool HasPickup();

private:
    uint8 PickupKind(CompanionDuty::BankSlot const& bankSlot);   // CompanionBankPickup::Kind as uint8
    void RunPickup(std::vector<CompanionDuty::BankSlot> const& slots, std::set<uint8> const& viewTabs,
                   std::set<uint32> const& deposited);
    bool Execute(std::string const text, GameObject* bank);
    bool MoveFromCharToBank(Item* item, GameObject* bank);
};

#endif
