/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANIONBRAIN_H
#define PLAYERBOTS_COMPANIONBRAIN_H

#include <atomic>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

#include "Define.h"

class Item;
struct ItemTemplate;
class PlayerbotAI;

// Companion brain: bag item classes and per-companion weights (acore_characters.companion_weights).
//
// Threads:
//   Update() and the weight reload run on the world thread.
//   ClassifyItem() runs on the companion's own map thread (it reads that bot's item usage values).
//   Weight() and DutyDefault() run on map threads and only read data under _weightMutex.
class CompanionBrain
{
public:
    // KEEP stays in the bags. BANK goes to the guild bank. SELL goes to a vendor.
    enum class ItemClass : uint8
    {
        KEEP,
        BANK,
        SELL
    };

    // Hearthstone item entry (item_template 6948). Always KEEP: never sold, banked or destroyed by a companion.
    static constexpr uint32 ITEM_HEARTHSTONE = 6948;

    // Any thread (own map thread). Classifies one bag item for a companion. Rules are documented in the .cpp.
    static ItemClass ClassifyItem(PlayerbotAI* botAI, Item* item);

    // World thread. Call once per world tick. Loads defaults from config once, then re-reads the weight table at
    // the first call and every WEIGHT_REFRESH_MS.
    static void Update(uint32 elapsed);

    // Any thread. The companion's row for key, clamped to [0.5x, 1.5x] of defaultValue. defaultValue when the
    // companion has no row for key, or when defaultValue is not a finite non-negative number.
    static float Weight(uint32 lowGuid, std::string const& key, float defaultValue);

    // Any thread. The companion's row for key without the clamp (traits in 0..1 such as "completionist"); defaultValue
    // when the companion has no row for key.
    static float RawWeight(uint32 lowGuid, std::string const& key, float defaultValue);

    // Any thread. Default for a duty weight key (config AiPlayerbot.Companion.Weight.*, else the code default).
    static float DutyDefault(std::string const& key);

    // Any thread. AiPlayerbot.Companion.Bank.WithdrawCap: stacks of one trade good a companion may hold (default 1).
    static uint32 WithdrawCapStacks();
    // Any thread. AiPlayerbot.Companion.Bank.MaxStacksPerItem: guild bank stock limit per item, in stacks (default 3).
    // At the limit a BANK-class item becomes SELL (KEEP when unsellable; profession items are KEEP before that).
    static uint32 BankMaxStacks();
    // Any thread. AiPlayerbot.Companion.Duty.SearchRange: yards searched for duty targets (default 300).
    static float SearchRange();

    // Guild bank mat policy: true for standard WotLK 1-450 profession leveling-guide materials.
    static bool IsLevelingGuideMat(uint32 entry);
    // Any thread. True when entry is explicitly listed in acore_characters.companion_bank_requests.
    static bool IsRequestedMat(uint32 entry, uint32* outMinCount = nullptr);
    // Any thread. True when an item belongs in the guild bank under the active policy (strict or legacy).
    static bool IsBankWantedItem(ItemTemplate const* proto);
    // Preferred tab (0 = mats/gems/requested, 1 = consumables/recipes/gear) when tabCount >= 2.
    static uint8 PreferredBankTab(ItemTemplate const* proto, uint8 tabCount);
    // World or map thread. Reloads acore_characters.companion_bank_requests immediately (e.g. after GM command).
    static void ReloadBankRequests();
    struct BankRequest
    {
        uint32 itemEntry = 0;
        std::string requestedBy;
        uint32 minCount = 20;
        std::string note;
    };
    static std::vector<BankRequest> ListBankRequests();

    static constexpr uint32 WEIGHT_REFRESH_MS = 600000;
    static constexpr float WEIGHT_MIN_FACTOR = 0.5f;
    static constexpr float WEIGHT_MAX_FACTOR = 1.5f;

private:
    static bool IsProfessionTool(ItemTemplate const* proto);
    static void LoadDefaults();
    static void ReloadWeights();

    static std::mutex _weightMutex;                                                     // guards the two maps below
    static std::unordered_map<uint32, std::unordered_map<std::string, float>> _weights; // low guid -> key -> value
    static std::unordered_map<std::string, float> _dutyDefaults;                        // duty key -> default
    // World thread only:
    static uint32 _timer;
    static bool _defaultsLoaded;
    static bool _weightsLoaded;
    static bool _missingLogged;
    static std::atomic<uint32> _withdrawCapStacks;
    static std::atomic<float> _searchRange;
    static std::atomic<uint32> _bankMaxStacks;
};

#endif
