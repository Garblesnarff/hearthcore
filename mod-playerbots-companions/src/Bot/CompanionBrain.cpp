/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "CompanionBrain.h"
#include "CompanionClassStrategy.h"
#include "CompanionDuty.h"
#include "CompanionCraft.h"

#include <algorithm>
#include <cmath>
#include <unordered_set>

#include "Bag.h"
#include "Config.h"
#include "DatabaseEnv.h"
#include "Item.h"
#include "ItemTemplate.h"
#include "ItemUsageValue.h"
#include "Log.h"
#include "Playerbots.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"

namespace
{
// Hearthstone item entry. Never sold, banked or destroyed (B13).
constexpr uint32 ITEM_HEARTHSTONE_ENTRY = CompanionBrain::ITEM_HEARTHSTONE;

std::mutex gBankReqMutex;
std::unordered_map<uint32, CompanionBrain::BankRequest> gBankRequests;
bool gBankReqMissingLogged = false;

// WotLK 3.3.5a (1-450) profession leveling-guide materials whitelist (Mining/BS, Eng, JC, Herb/Alch, Inscription,
// Skinning/LW, Tailoring, Enchanting, First Aid, and guide-standard Cooking meats/fish).
std::unordered_set<uint32> const& LevelingGuideMats()
{
    static std::unordered_set<uint32> const kMats = {
        // Ores & Bars (Classic, TBC, WotLK)
        2770, 2840, 2771, 3576, 2841, 2775, 2842, 2772, 3575, 3859, 2776, 3577, 3858, 3860, 7911, 6037,
        10620, 12359, 11370, 11371, 12655, 23424, 23445, 23425, 23446, 23426, 23449, 23427, 23447, 23448,
        23573, 36909, 36916, 36912, 36913, 36910, 41163, 37663,
        // Stone, Flux, Coal, Blasting Powder & Engineering Parts
        2835, 2836, 2838, 7912, 12365, 3857, 2880, 3466, 18567,
        4357, 4364, 4377, 10505, 15992, 23781,
        4359, 4361, 4363, 4371, 4375, 4382, 4404, 4389, 4387, 10558, 10559, 10560, 10561, 15994, 16000,
        23782, 23783, 39681, 39682, 39683, 39690,
        // Elementals, Essences, Motes, Primals, Crystallized, Eternals
        7067, 7068, 7069, 7070, 7075, 7076, 7077, 7078, 7079, 7080, 7081, 7082, 12803, 12808, 12809,
        22572, 22573, 22574, 22575, 22576, 22577, 22578,
        21884, 21885, 21886, 22451, 22452, 22456, 22457,
        37700, 37701, 37702, 37703, 37704, 37705,
        35622, 35623, 35624, 35625, 35627, 36860,
        // Herbs (Classic, TBC, WotLK)
        2447, 765, 2449, 785, 2450, 2452, 3820, 2453, 3355, 3369, 3356, 3357, 3818, 3821, 3358, 3819,
        4625, 8831, 8836, 8838, 8839, 8845, 8846, 13464, 13463, 13465, 13466, 13467, 13468,
        22785, 22786, 22787, 22788, 22789, 22790, 22791, 22792, 22793, 22794,
        36901, 36903, 36904, 36905, 36906, 36907, 36908, 39970, 37921,
        // Alchemy Oils & Inscription Pigments / Inks
        6370, 6371, 13423,
        39151, 39334, 43103, 39338, 43104, 39339, 43105, 39340, 43106, 39341, 43107, 39342, 43108, 39343, 43109,
        37101, 39469, 39774, 43116, 43117, 43118, 43119, 43120, 43121, 43122, 43123, 43124, 43125, 43126, 43127,
        // Leather, Hides, Scales
        2934, 2318, 2319, 4234, 4304, 8170, 25649, 21887, 23793, 33567, 33568, 38425, 44128,
        783, 4232, 4235, 8169, 8171, 4231, 4233, 4236, 8172, 15407,
        8368, 8146, 8165, 25700, 38557, 38558, 38561,
        // Cloth, Bolts, Silks
        2589, 2996, 2592, 2997, 4306, 4305, 4338, 4339, 14047, 14048, 21877, 21840, 33470, 41510, 41511,
        3182, 4337, 10285, 14227, 14256, 14342,
        // Enchanting Dusts, Essences, Shards, Crystals, Pearls
        10940, 11083, 11137, 11176, 16204, 22445, 34054,
        10938, 10939, 10998, 11082, 11134, 11135, 11174, 11175, 16202, 16203, 22447, 22446, 34056, 34055,
        10978, 11084, 11138, 11139, 11177, 11178, 14343, 14344, 22448, 22449, 34053, 34052, 20725, 22450, 34057,
        5498, 5500, 7971, 13926,
        // Gems (Classic, TBC, WotLK uncut)
        774, 818, 1210, 1206, 1705, 1529, 3864, 7909, 7910, 12361, 12364, 12799, 12800,
        23077, 23079, 23112, 23117, 21929, 23107, 23436, 23437, 23438, 23439, 23440, 23441,
        36917, 36920, 36923, 36926, 36929, 36932, 36918, 36921, 36924, 36927, 36930, 36933,
        // Cooking leveling-guide meats & fish (standard 1-450 guide ingredients only)
        2672, 769, 2673, 3173, 3731, 12184, 12208, 12037, 12207, 7974,
        6291, 6289, 6308, 8365, 21071, 21153, 6358, 6359, 13422, 13759, 13760, 4603,
        27674, 27671, 27677, 27678, 27682, 27681, 27422, 27438, 27437, 27435,
        43013, 43012, 43011, 43010, 43009, 41810, 41809, 41813, 41806, 41807, 41814, 41805, 41802
    };
    return kMats;
}
}  // namespace

std::mutex CompanionBrain::_weightMutex;
std::unordered_map<uint32, std::unordered_map<std::string, float>> CompanionBrain::_weights;
// Code defaults. LoadDefaults() replaces them with the AiPlayerbot.Companion.Weight.* config values.
std::unordered_map<std::string, float> CompanionBrain::_dutyDefaults = {
    {"sell", 1.0f},
    {"repair", 1.0f},
    {"bank", 1.0f},
    {"train", 1.0f},
    {"craft", 1.0f},
    {"fish", 1.0f},
    {"gather", 1.0f},
    {"travel", 1.0f},
    {"hearth", 1.0f},   // B13 hearthstone duty (no config key; per-companion row in companion_weights works)
    {"switch_margin", 20.0f},
    {"activity", 30.0f},
};
uint32 CompanionBrain::_timer = 0;
bool CompanionBrain::_defaultsLoaded = false;
bool CompanionBrain::_weightsLoaded = false;
bool CompanionBrain::_missingLogged = false;
std::atomic<uint32> CompanionBrain::_withdrawCapStacks{1};
std::atomic<float> CompanionBrain::_searchRange{300.0f};
std::atomic<uint32> CompanionBrain::_bankMaxStacks{3};

// Item class rule (applied in this order; the first match wins; anything unmatched is KEEP):
//
// Protected, never SELL or BANK (checked first, from the item template):
//   1. Quality 7 (heirloom).
//   2. Entry 6948 (hearthstone).
//   3. Class QUEST, or Bonding BIND_QUEST_ITEM, or StartQuest != 0 (quest items).
//   4. Class PROJECTILE (ammo), QUIVER or CONTAINER.
//   5. Profession tool: TotemCategory != 0, or weapon subclass MISC or FISHING_POLE.
// Usage, from the bot's "item usage" value (qualifier "itemId,randomProperty"):
//   6. EQUIP, REPLACE (equipment upgrade candidate; this includes soulbound upgrades), QUEST, SKILL (needed by
//      this bot's professions), KEEP (needed amount), USE (consumable or key within need), AMMO or GUILD_TASK -> KEEP.
// Then:
//   7. SELL: grey (quality 0) with SellPrice > 0.
//   8. SELL: white (quality 1) with SellPrice > 0 and usage VENDOR.
//   9. KEEP: food, drink and bandages (consumable subclass FOOD or BANDAGE). With Craft.Enabled this is checked
//      before step 6: KEEP up to Craft.KeepStacks stacks, BANK above (not soulbound).
//  10. Soulbound items are never banked: anything that reaches here and is soulbound -> KEEP.
//  11. BANK: trade goods (ITEM_CLASS_TRADE_GOODS: herbs, ore, bars, leather, cloth, dusts, ...) and gems.
//  12. BANK: consumable potion, elixir or flask above the bot's own need (usage other than USE or KEEP).
//  13. BANK: armour or weapon of quality 2 or better, Bonding BIND_WHEN_EQUIPPED, not an upgrade (usage other
//      than EQUIP or REPLACE; any usage such as AH, DISENCHANT, NONE, BAD_EQUIP or BROKEN_EQUIP).
//  14. KEEP: everything else.
CompanionBrain::ItemClass CompanionBrain::ClassifyItem(PlayerbotAI* botAI, Item* item)
{
    if (!botAI || !item)
        return ItemClass::KEEP;
    ItemTemplate const* proto = item->GetTemplate();
    if (!proto)
        return ItemClass::KEEP;

    // 1-5: protected by the template alone. The hearthstone is KEEP before any switch or usage rule (B13a).
    if (proto->ItemId == ITEM_HEARTHSTONE_ENTRY || proto->Quality == ITEM_QUALITY_HEIRLOOM)
        return ItemClass::KEEP;
    if (proto->Class == ITEM_CLASS_QUEST || proto->Bonding == BIND_QUEST_ITEM || proto->StartQuest != 0)
        return ItemClass::KEEP;
    if (proto->Class == ITEM_CLASS_PROJECTILE || proto->Class == ITEM_CLASS_QUIVER ||
        proto->Class == ITEM_CLASS_CONTAINER)
        return ItemClass::KEEP;
    if (IsProfessionTool(proto))
        return ItemClass::KEEP;
    // Class play hook: this companion's consumables list (AiPlayerbot.Companion.<Name>.Consumables) always stays.
    if (CompanionClass::IsKeepItem(botAI->GetBot(), proto->ItemId))
        return ItemClass::KEEP;
    // Phase 2: a material of the companion's craft plan stays (no withdraw / deposit ping-pong).
    if (CompanionCraft::IsPlannedMaterial(botAI->GetBot()->GetGUID().GetCounter(), proto->ItemId))
        return ItemClass::KEEP;
    Player* const bot = botAI->GetBot();
    // Count only general-purpose backpack + container slots (exclude Quivers/Ammo Pouches/Soul Bags).
    uint32 generalFree = 0;
    if (bot)
    {
        for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
            if (!bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
                ++generalFree;
        for (uint8 bagSlot = INVENTORY_SLOT_BAG_START; bagSlot < INVENTORY_SLOT_BAG_END; ++bagSlot)
        {
            Bag* bag = static_cast<Bag*>(bot->GetItemByPos(INVENTORY_SLOT_BAG_0, bagSlot));
            if (!bag || !bag->GetTemplate() || bag->GetTemplate()->Class != ITEM_CLASS_CONTAINER ||
                bag->GetTemplate()->SubClass != ITEM_SUBCLASS_CONTAINER)
                continue;
            for (uint32 i = 0; i < bag->GetBagSize(); ++i)
                if (!bag->GetItemByPos(i))
                    ++generalFree;
        }
    }
    bool const bagsTight = bot && generalFree <= 2;

    // Non-mana classes (Warrior, Rogue, Death Knight) never need drinks: sell them instead of wasting bag slots.
    if (bot && bot->getPowerType() != POWER_MANA && proto->Class == ITEM_CLASS_CONSUMABLE &&
        proto->SubClass == ITEM_SUBCLASS_FOOD && proto->Spells[0].SpellCategory == 59 && proto->SellPrice > 0)
        return ItemClass::SELL;

    // Craft.KeepReagents (companions): a reagent of a known craft recipe that is not at its skill cap (ore for a smelt,
    // bars and stone for a blacksmith recipe) is not sold or banked, whatever the usage value says.
    // When bags are nearly full (<=2 free slots), only protect actual bank-wanted/leveling-guide mats so low-level
    // cooking scraps (e.g. spider legs, eggs, meats) don't deadlock quest turn-ins.
    if (botAI->IsCompanion() && CompanionCraft::Option("AiPlayerbot.Companion.Craft.KeepReagents", 1.0f) > 0.5f &&
        CompanionCraft::IsKnownReagent(bot, proto->ItemId))
    {
        if (!bagsTight || IsBankWantedItem(proto))
            return ItemClass::KEEP;
    }
    // Phase 2 (Craft.Enabled): food and bandages are kept up to Craft.KeepStacks stacks. With Bank.StrictPolicy=1,
    // excess ordinary food/bandages are SOLD to vendors rather than clogging the guild bank.
    if (proto->Class == ITEM_CLASS_CONSUMABLE &&
        (proto->SubClass == ITEM_SUBCLASS_FOOD || proto->SubClass == ITEM_SUBCLASS_BANDAGE) &&
        CompanionCraft::AreaEnabled("Craft"))
    {
        if (!CompanionCraft::OverKeepStacks(bot, proto))
            return ItemClass::KEEP;
        if (sPlayerbotAIConfig.companionBankStrictPolicy && !IsRequestedMat(proto->ItemId))
            return proto->SellPrice > 0 ? ItemClass::SELL : ItemClass::KEEP;
        return !item->IsSoulBound() ? ItemClass::BANK : ItemClass::KEEP;
    }

    // 6: usage. The qualifier carries the random property so enchanted equipment is judged with its stats.
    std::string const qualifier =
        std::to_string(proto->ItemId) + "," + std::to_string(item->GetItemRandomPropertyId());
    ItemUsage const usage = botAI->GetAiObjectContext()->GetValue<ItemUsage>("item usage", qualifier)->Get();
    if (usage == ITEM_USAGE_EQUIP || usage == ITEM_USAGE_REPLACE || usage == ITEM_USAGE_QUEST ||
        usage == ITEM_USAGE_KEEP || usage == ITEM_USAGE_AMMO || usage == ITEM_USAGE_GUILD_TASK)
        return ItemClass::KEEP;
    if (usage == ITEM_USAGE_SKILL || usage == ITEM_USAGE_USE)
    {
        // When bags are almost full, release ordinary white non-guide drops (common meats/starter weapons) so the
        // sell duty can actually clear bag space.
        if (!bagsTight || proto->Quality >= ITEM_QUALITY_UNCOMMON || proto->SellPrice == 0 ||
            IsBankWantedItem(proto) ||
            (proto->Class == ITEM_CLASS_CONSUMABLE &&
             (proto->SubClass == ITEM_SUBCLASS_FOOD || proto->SubClass == ITEM_SUBCLASS_BANDAGE ||
              proto->SubClass == ITEM_SUBCLASS_POTION)))
            return ItemClass::KEEP;
        return ItemClass::SELL;
    }

    // 7-8: SELL. SellPrice > 0 is required, otherwise the vendor will not take it and it stays in the bags.
    if (proto->Quality == ITEM_QUALITY_POOR && proto->SellPrice > 0)
        return ItemClass::SELL;
    if (proto->Quality == ITEM_QUALITY_NORMAL && proto->SellPrice > 0 &&
        (usage == ITEM_USAGE_VENDOR ||
         (bagsTight && (proto->Class == ITEM_CLASS_WEAPON || proto->Class == ITEM_CLASS_ARMOR))))
        return ItemClass::SELL;

    // 9: food, drink and bandages stay with the companion.
    bool const consumable = proto->Class == ITEM_CLASS_CONSUMABLE;
    if (consumable && (proto->SubClass == ITEM_SUBCLASS_FOOD || proto->SubClass == ITEM_SUBCLASS_BANDAGE))
        return ItemClass::KEEP;

    // 10: a soulbound item cannot be deposited, so it stays.
    if (item->IsSoulBound())
        return ItemClass::KEEP;

    // 11-13: BANK, unless strict bank policy rejects non-guide clutter (then SELL) or the guild bank already holds
    // BankMaxStacks stacks (or requested minCount) of the item.
    if (IsBankWantedItem(proto))
    {
        uint32 const guildId = botAI->GetBot()->GetGuildId();
        uint32 reqMin = 0;
        bool const requested = IsRequestedMat(proto->ItemId, &reqMin);
        uint64 limit = static_cast<uint64>(BankMaxStacks()) * std::max<uint32>(1, proto->GetMaxStackSize());
        if (proto->Class == ITEM_CLASS_RECIPE)
            limit = 1;
        if (requested && reqMin > limit)
            limit = reqMin;
        if (guildId && CompanionDuty::BankStock(guildId, proto->ItemId) >= limit)
            return proto->SellPrice > 0 ? ItemClass::SELL : ItemClass::KEEP;
        return ItemClass::BANK;
    }

    // Strict policy: vendor non-guide white trade goods and low-quality BoE gear instead of hoarding them.
    if (sPlayerbotAIConfig.companionBankStrictPolicy && proto->SellPrice > 0)
    {
        if (proto->Class == ITEM_CLASS_TRADE_GOODS && proto->Quality <= ITEM_QUALITY_NORMAL)
            return ItemClass::SELL;
        if ((proto->Class == ITEM_CLASS_ARMOR || proto->Class == ITEM_CLASS_WEAPON) &&
            proto->Quality >= ITEM_QUALITY_UNCOMMON && proto->Bonding == BIND_WHEN_EQUIPPED)
            return ItemClass::SELL;
    }

    // 14: default.
    return ItemClass::KEEP;
}

bool CompanionBrain::IsProfessionTool(ItemTemplate const* proto)
{
    if (proto->TotemCategory != 0)
        return true;
    return proto->Class == ITEM_CLASS_WEAPON &&
           (proto->SubClass == ITEM_SUBCLASS_WEAPON_MISC || proto->SubClass == ITEM_SUBCLASS_WEAPON_FISHING_POLE);
}

void CompanionBrain::Update(uint32 elapsed)
{
    if (!_defaultsLoaded)
        LoadDefaults();

    _timer += elapsed;
    if (_weightsLoaded && _timer < WEIGHT_REFRESH_MS)
        return;
    _timer = 0;
    _weightsLoaded = true;
    ReloadWeights();
    ReloadBankRequests();
}

bool CompanionBrain::IsLevelingGuideMat(uint32 entry)
{
    return LevelingGuideMats().count(entry) > 0;
}

bool CompanionBrain::IsRequestedMat(uint32 entry, uint32* outMinCount)
{
    std::lock_guard<std::mutex> lock(gBankReqMutex);
    auto it = gBankRequests.find(entry);
    if (it == gBankRequests.end())
        return false;
    if (outMinCount)
        *outMinCount = it->second.minCount;
    return true;
}

bool CompanionBrain::IsBankWantedItem(ItemTemplate const* proto)
{
    if (!proto)
        return false;
    if (IsRequestedMat(proto->ItemId))
        return true;

    bool const strict = sPlayerbotAIConfig.companionBankStrictPolicy;
    if (proto->Class == ITEM_CLASS_GEM)
        return true;
    if (proto->Class == ITEM_CLASS_TRADE_GOODS)
        return !strict || IsLevelingGuideMat(proto->ItemId);
    if (proto->Class == ITEM_CLASS_RECIPE && proto->Bonding != BIND_WHEN_PICKED_UP)
        return true;
    if (proto->Class == ITEM_CLASS_CONSUMABLE)
    {
        if (proto->SubClass == ITEM_SUBCLASS_POTION || proto->SubClass == ITEM_SUBCLASS_ELIXIR ||
            proto->SubClass == ITEM_SUBCLASS_FLASK)
            return true;
        return !strict && (proto->SubClass == ITEM_SUBCLASS_FOOD || proto->SubClass == ITEM_SUBCLASS_BANDAGE);
    }
    if ((proto->Class == ITEM_CLASS_ARMOR || proto->Class == ITEM_CLASS_WEAPON) &&
        proto->Bonding == BIND_WHEN_EQUIPPED)
    {
        uint32 const minQ = strict ? sPlayerbotAIConfig.companionBankMinBoeQuality : ITEM_QUALITY_UNCOMMON;
        return proto->Quality >= minQ;
    }
    return false;
}

uint8 CompanionBrain::PreferredBankTab(ItemTemplate const* proto, uint8 tabCount)
{
    if (!proto || tabCount < 2)
        return 0;
    // Tab 0: Profession leveling mats, gems, and explicitly requested mats.
    // Tab 1: Combat consumables (potions/elixirs/flasks), recipes/plans/patterns, and BoE gear.
    if (proto->Class == ITEM_CLASS_TRADE_GOODS || proto->Class == ITEM_CLASS_GEM || IsRequestedMat(proto->ItemId))
        return 0;
    return 1;
}

void CompanionBrain::ReloadBankRequests()
{
    QueryResult probe = CharacterDatabase.Query(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() "
        "AND table_name = 'companion_bank_requests'");
    if (!probe || probe->Fetch()[0].Get<uint64>() == 0)
    {
        if (!gBankReqMissingLogged)
        {
            gBankReqMissingLogged = true;
            LOG_WARN("playerbots", "Companion bank requests: table companion_bank_requests is missing");
        }
        std::lock_guard<std::mutex> lock(gBankReqMutex);
        gBankRequests.clear();
        return;
    }

    std::unordered_map<uint32, BankRequest> rows;
    QueryResult result = CharacterDatabase.Query(
        "SELECT `item_entry`, `requested_by`, `min_count`, `note` FROM `companion_bank_requests`");
    if (result)
    {
        do
        {
            Field* fields = result->Fetch();
            BankRequest req;
            req.itemEntry = fields[0].Get<uint32>();
            req.requestedBy = fields[1].Get<std::string>();
            req.minCount = fields[2].Get<uint32>();
            req.note = fields[3].Get<std::string>();
            rows[req.itemEntry] = std::move(req);
        } while (result->NextRow());
    }

    std::lock_guard<std::mutex> lock(gBankReqMutex);
    gBankRequests.swap(rows);
}

std::vector<CompanionBrain::BankRequest> CompanionBrain::ListBankRequests()
{
    std::lock_guard<std::mutex> lock(gBankReqMutex);
    std::vector<BankRequest> out;
    out.reserve(gBankRequests.size());
    for (auto const& [entry, req] : gBankRequests)
        out.push_back(req);
    std::sort(out.begin(), out.end(), [](BankRequest const& a, BankRequest const& b) { return a.itemEntry < b.itemEntry; });
    return out;
}

void CompanionBrain::LoadDefaults()
{
    _defaultsLoaded = true;
    // Non-finite or negative config values fall back to the code default.
    auto read = [](char const* option, float fallback)
    {
        float value = sConfigMgr->GetOption<float>(option, fallback);
        return std::isfinite(value) && value >= 0.0f ? value : fallback;
    };

    std::lock_guard<std::mutex> lock(_weightMutex);
    _dutyDefaults["sell"] = read("AiPlayerbot.Companion.Weight.Sell", 1.0f);
    _dutyDefaults["repair"] = read("AiPlayerbot.Companion.Weight.Repair", 1.0f);
    _dutyDefaults["bank"] = read("AiPlayerbot.Companion.Weight.Bank", 1.0f);
    _dutyDefaults["train"] = read("AiPlayerbot.Companion.Weight.Train", 1.0f);
    _dutyDefaults["craft"] = read("AiPlayerbot.Companion.Weight.Craft", 1.0f);
    _dutyDefaults["fish"] = read("AiPlayerbot.Companion.Weight.Fish", 1.0f);
    _dutyDefaults["gather"] = read("AiPlayerbot.Companion.Weight.Gather", 1.0f);
    _dutyDefaults["travel"] = read("AiPlayerbot.Companion.Weight.Travel", 1.0f);
    _dutyDefaults["switch_margin"] = read("AiPlayerbot.Companion.Weight.SwitchMargin", 20.0f);
    _dutyDefaults["activity"] = read("AiPlayerbot.Companion.Weight.Activity", 30.0f);
    _withdrawCapStacks.store(std::max(1u, sConfigMgr->GetOption<uint32>("AiPlayerbot.Companion.Bank.WithdrawCap", 1u)));
    _bankMaxStacks.store(std::max(1u, sConfigMgr->GetOption<uint32>("AiPlayerbot.Companion.Bank.MaxStacksPerItem", 3u)));
    float range = sConfigMgr->GetOption<float>("AiPlayerbot.Companion.Duty.SearchRange", 300.0f);
    _searchRange.store(std::isfinite(range) && range > 0.0f ? range : 300.0f);
}

uint32 CompanionBrain::WithdrawCapStacks()
{
    return _withdrawCapStacks.load();
}

uint32 CompanionBrain::BankMaxStacks()
{
    return _bankMaxStacks.load();
}

float CompanionBrain::SearchRange()
{
    return _searchRange.load();
}

void CompanionBrain::ReloadWeights()
{
    // A SELECT on a missing table makes the core abort (ER_NO_SUCH_TABLE), so probe information_schema first.
    QueryResult probe = CharacterDatabase.Query(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() "
        "AND table_name = 'companion_weights'");
    if (!probe || probe->Fetch()[0].Get<uint64>() == 0)
    {
        if (!_missingLogged)
        {
            _missingLogged = true;
            LOG_WARN("playerbots", "Companion weights: table companion_weights is missing, using default weights");
        }
        std::lock_guard<std::mutex> lock(_weightMutex);
        _weights.clear();
        return;
    }

    std::unordered_map<uint32, std::unordered_map<std::string, float>> rows;
    QueryResult result = CharacterDatabase.Query("SELECT `guid`, `key`, `value` FROM `companion_weights`");
    if (result)
    {
        do
        {
            Field* fields = result->Fetch();
            float const value = fields[2].Get<float>();
            if (!std::isfinite(value))
                continue;
            rows[fields[0].Get<uint32>()][fields[1].Get<std::string>()] = value;
        } while (result->NextRow());
    }

    std::lock_guard<std::mutex> lock(_weightMutex);
    _weights.swap(rows);
}

float CompanionBrain::Weight(uint32 lowGuid, std::string const& key, float defaultValue)
{
    if (!std::isfinite(defaultValue) || defaultValue < 0.0f)
        return defaultValue;

    std::lock_guard<std::mutex> lock(_weightMutex);
    auto guidIt = _weights.find(lowGuid);
    if (guidIt == _weights.end())
        return defaultValue;
    auto keyIt = guidIt->second.find(key);
    if (keyIt == guidIt->second.end())
        return defaultValue;

    float const low = defaultValue * WEIGHT_MIN_FACTOR;
    float const high = defaultValue * WEIGHT_MAX_FACTOR;
    return std::clamp(keyIt->second, low, high);
}

float CompanionBrain::RawWeight(uint32 lowGuid, std::string const& key, float defaultValue)
{
    std::lock_guard<std::mutex> lock(_weightMutex);
    auto guidIt = _weights.find(lowGuid);
    if (guidIt == _weights.end())
        return defaultValue;
    auto keyIt = guidIt->second.find(key);
    return keyIt == guidIt->second.end() ? defaultValue : keyIt->second;
}

float CompanionBrain::DutyDefault(std::string const& key)
{
    std::lock_guard<std::mutex> lock(_weightMutex);
    auto it = _dutyDefaults.find(key);
    return it == _dutyDefaults.end() ? 0.0f : it->second;
}
