/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "CompanionCraft.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <iomanip>
#include <limits>
#include <mutex>
#include <set>
#include <sstream>
#include <unordered_map>

#include "CompanionBotMgr.h"
#include "DatabaseEnv.h"
#include "Trainer.h"
#include "CompanionBrain.h"
#include "CompanionDuty.h"
#include "CompanionLease.h"
#include "CompanionLife.h"
#include "CompanionTransport.h"
#include "Config.h"
#include "DBCEnums.h"
#include "DBCStores.h"
#include "FishingAction.h"
#include "GameObject.h"
#include "GameTime.h"
#include "GuildMgr.h"
#include "Item.h"
#include "ItemTemplate.h"
#include "Log.h"
#include "Map.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "PlayerbotAIConfig.h"
#include "Playerbots.h"
#include "SharedDefines.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include "Timer.h"

namespace
{
constexpr uint32 OPTION_REFRESH_MS = 60000;      // config values are re-read at most this often
constexpr uint32 RESERVE_SECONDS = 1800;         // a guild bank reservation is released after 30 minutes
constexpr uint32 DEFAULT_BATCH = 20;             // crafts per batch (Craft.BatchSize)
constexpr uint32 SECONDS_PER_DAY = 86400;
constexpr uint32 COPPER_PER_GOLD = 10000;
constexpr float FISH_HERE_MIN_YARDS = 10.0f;     // stock fishing: water 10..20 yd in front of the bot
constexpr float FISH_HERE_MAX_YARDS = 40.0f;
constexpr float FISH_HERE_STEP_YARDS = 5.0f;
constexpr uint32 SKILL_STEP = 75;                // one profession rank (Apprentice 75, Journeyman 150, ...)
constexpr uint32 TARGET_STACK = 5;               // Milling / Prospecting take 5 of one stack (Spell::CheckCast)
constexpr uint32 MIN_FREE_SLOTS_TARGET = 4;      // free bag slots before a mill / prospect (unlooted results are lost)
constexpr float FORCED_NEED = 150.0f;            // need of a GM-forced craft (AiPlayerbot.Companion.Craft.ForcedNeed)

// Profession skill lines a companion may have (craft and gather, primary and secondary).
constexpr uint32 PROFESSION_SKILLS[] = {SKILL_ALCHEMY,       SKILL_BLACKSMITHING,  SKILL_ENCHANTING,
                                        SKILL_ENGINEERING,   SKILL_HERBALISM,      SKILL_INSCRIPTION,
                                        SKILL_JEWELCRAFTING, SKILL_LEATHERWORKING, SKILL_MINING,
                                        SKILL_SKINNING,      SKILL_TAILORING,      SKILL_COOKING,
                                        SKILL_FIRST_AID,     SKILL_FISHING};
constexpr uint32 SECONDARY_SKILLS[] = {SKILL_COOKING, SKILL_FIRST_AID, SKILL_FISHING};

// Skill lines whose CREATE_ITEM recipes the CRAFT duty uses. Enchanting is left out (its spells target an item).
bool IsCraftSkill(uint32 skillId)
{
    switch (skillId)
    {
        case SKILL_ALCHEMY:
        case SKILL_BLACKSMITHING:
        case SKILL_ENGINEERING:
        case SKILL_INSCRIPTION:
        case SKILL_JEWELCRAFTING:
        case SKILL_LEATHERWORKING:
        case SKILL_MINING:   // smelting
        case SKILL_TAILORING:
        case SKILL_COOKING:
        case SKILL_FIRST_AID:
            return true;
        default:
            return false;
    }
}

struct CachedOption
{
    uint32 atMs = 0;
    std::string value;
};

std::mutex gOptionMutex;
std::unordered_map<std::string, CachedOption> gOptions;

std::string RawOption(std::string const& key, std::string const& fallback)
{
    uint32 const now = getMSTime();
    std::lock_guard<std::mutex> lock(gOptionMutex);
    auto it = gOptions.find(key);
    if (it != gOptions.end() && getMSTimeDiff(it->second.atMs, now) < OPTION_REFRESH_MS)
        return it->second.value;
    CachedOption entry;
    entry.atMs = now;
    entry.value = sConfigMgr->GetOption<std::string>(key, fallback, false);
    gOptions[key] = entry;
    return entry.value;
}

struct Reservation
{
    uint32 guildId = 0;
    uint32 entry = 0;
    uint32 count = 0;
    uint32 untilSec = 0;
};

struct CraftBotState
{
    CompanionCraft::Run craft;
    CompanionCraft::Run fish;
    CompanionCraft::Run gather;
    std::vector<Reservation> reservations;
    uint32 spendDay = 0;
    uint32 spent = 0;
    bool weaponsSaved = false;
    std::vector<CompanionCraft::GatherNeed> gatherNeeds;   // Craft.GatherForNeed, from the last evaluation
    uint32 gatherSec = 0;
    std::string gatherLog;                                 // last logged need (log on change)
    ObjectGuid savedMain;
    ObjectGuid savedOff;
    CompanionCraft::Force force = CompanionCraft::Force::None;   // GM test hook, pending until the run starts
    std::string forceCity;
    uint32 forceUntil = 0;
};

std::mutex gMutex;                                // gBots
std::unordered_map<uint32, CraftBotState> gBots;       // low guid -> state

// Static spawn indexes per map, built once on first use from the in-memory spawn and vendor caches.
struct VendorSpawn
{
    uint32 entry = 0;
    uint32 spawnId = 0;
    float x = 0.0f, y = 0.0f, z = 0.0f;
    uint32 zoneId = 0;
    uint32 zoneTeam = 0;
    uint32 hostileMask = 0;
};

struct GoSpawn
{
    uint32 entry = 0;
    uint32 spawnId = 0;
    float x = 0.0f, y = 0.0f, z = 0.0f;
    uint32 focusId = 0;   // spell focus only
    uint32 zoneId = 0;    // cached from CompanionBotMgr::CachedZoneAt (zero runtime tile loads)
};

struct GatherNodeInfo
{
    uint32 goEntry = 0;
    uint32 skillId = 0;
    uint32 reqSkill = 0;
};

struct MapIndex
{
    bool vendorsBuilt = false;
    bool gosBuilt = false;
    std::vector<VendorSpawn> vendors;
    std::unordered_map<uint32, std::vector<uint32>> itemVendors;   // item -> indexes into vendors
    std::unordered_map<uint32, std::vector<uint32>> categoryItems; // TotemCategory -> sold tool items
    std::vector<uint32> fishingPoles;                               // sold fishing poles
    std::vector<GoSpawn> focuses;
    std::vector<GoSpawn> fishingHoles;
    std::unordered_map<uint32, std::vector<GoSpawn>> gatherSpawns; // GO entry -> world spawns on this map
};

std::mutex gIndexMutex;
std::unordered_map<uint32, MapIndex> gIndexes;   // map id -> index

uint32 NowSec()
{
    return static_cast<uint32>(GameTime::GetGameTime().count());
}

// Builds the vendor part of a map's index. Caller holds gIndexMutex. Not built (retried later) while the duty spawn
// list is not loaded yet.
void BuildVendors(MapIndex& index, uint32 mapId)
{
    std::vector<CompanionBotMgr::DutySpot> const& spots =
        sCompanionBotMgr.DutySpots(mapId, CompanionBotMgr::DutySpotKind::Vendor);
    if (spots.empty())
        return;
    index.vendorsBuilt = true;
    for (CompanionBotMgr::DutySpot const& spot : spots)
    {
        VendorItemData const* list = sObjectMgr->GetNpcVendorItemList(spot.entry);
        if (!list)
            continue;
        uint32 const at = static_cast<uint32>(index.vendors.size());
        bool any = false;
        for (VendorItem const* item : list->m_items)
        {
            // Unlimited, gold-priced items only (thread, flux, vials, salt, spices, tools, fishing poles).
            if (!item || item->maxcount != 0 || item->ExtendedCost != 0)
                continue;
            ItemTemplate const* proto = sObjectMgr->GetItemTemplate(item->item);
            if (!proto || proto->BuyPrice <= 0)
                continue;
            std::vector<uint32>& sellers = index.itemVendors[item->item];
            if (sellers.empty())
            {
                if (proto->TotemCategory)
                    index.categoryItems[proto->TotemCategory].push_back(item->item);
                if (proto->Class == ITEM_CLASS_WEAPON && proto->SubClass == ITEM_SUBCLASS_WEAPON_FISHING_POLE)
                    index.fishingPoles.push_back(item->item);
            }
            sellers.push_back(at);
            any = true;
        }
        if (!any)
            continue;
        VendorSpawn vendor;
        vendor.entry = spot.entry;
        vendor.spawnId = spot.spawnId;
        vendor.x = spot.x;
        vendor.y = spot.y;
        vendor.z = spot.z;
        vendor.zoneId = spot.zoneId;
        vendor.zoneTeam = spot.zoneTeam;
        vendor.hostileMask = spot.hostileMask;
        index.vendors.push_back(vendor);
    }
}

// Builds the spell focus, fishing pool, and gatherable Mining/Herbalism node part of a map's index. Caller holds gIndexMutex.
void BuildGos(MapIndex& index, uint32 mapId)
{
    index.gosBuilt = true;
    for (auto const& [guid, data] : sObjectMgr->GetAllGOData())
    {
        if (data.mapid != mapId)
            continue;
        GameObjectTemplate const* info = sObjectMgr->GetGameObjectTemplate(data.id);
        if (!info)
            continue;
        if (info->type == GAMEOBJECT_TYPE_SPELL_FOCUS || info->type == GAMEOBJECT_TYPE_FISHINGHOLE)
        {
            GoSpawn spawn;
            spawn.entry = data.id;
            spawn.spawnId = guid;
            spawn.x = data.posX;
            spawn.y = data.posY;
            spawn.z = data.posZ;
            if (info->type == GAMEOBJECT_TYPE_SPELL_FOCUS)
            {
                spawn.focusId = info->spellFocus.focusId;
                index.focuses.push_back(spawn);
            }
            else
                index.fishingHoles.push_back(spawn);
            continue;
        }
        if (info->type == GAMEOBJECT_TYPE_CHEST && info->GetLockId() > 0)
        {
            LockEntry const* lock = sLockStore.LookupEntry(info->GetLockId());
            if (!lock)
                continue;
            bool isGather = false;
            for (uint8 i = 0; i < MAX_LOCK_CASE && !isGather; ++i)
            {
                if (lock->Type[i] == LOCK_KEY_SKILL)
                {
                    uint32 const sk = SkillByLockType(LockType(lock->Index[i]));
                    if (sk == SKILL_MINING || sk == SKILL_HERBALISM)
                        isGather = true;
                }
            }
            if (isGather)
            {
                GoSpawn spawn;
                spawn.entry = data.id;
                spawn.spawnId = guid;
                spawn.x = data.posX;
                spawn.y = data.posY;
                spawn.z = data.posZ;
                spawn.zoneId = sCompanionBotMgr.CachedZoneAt(mapId, data.posX, data.posY);
                index.gatherSpawns[data.id].push_back(spawn);
            }
        }
    }
}

// The index of a map, built as needed. Only a complete index is returned (it is never changed afterwards, so it is
// read without the lock); before the duty spawns are loaded an empty index is returned and the build is retried.
MapIndex const* IndexFor(uint32 mapId)
{
    static MapIndex const empty;
    std::lock_guard<std::mutex> lock(gIndexMutex);
    MapIndex& index = gIndexes[mapId];
    if (!index.vendorsBuilt)
        BuildVendors(index, mapId);
    if (!index.vendorsBuilt)
        return &empty;
    if (!index.gosBuilt)
        BuildGos(index, mapId);
    return &index;
}

// Distance from the session city (when planning from one) or from the bot.
float DistFrom(Player* bot, CompanionCraft::Origin const* at, float x, float y, float z)
{
    if (at && at->valid)
    {
        float const dx = at->x - x;
        float const dy = at->y - y;
        float const dz = at->z - z;
        return std::sqrt(dx * dx + dy * dy + dz * dz);
    }
    return bot->GetDistance(x, y, z);
}

// Inside the session city (always true without one).
bool InCity(CompanionCraft::Origin const* at, float x, float y, float z)
{
    if (!at || !at->valid)
        return true;
    float const dx = at->x - x;
    float const dy = at->y - y;
    float const dz = at->z - z;
    return dx * dx + dy * dy + dz * dz <= at->radius * at->radius;
}

// Free backpack and bag slots.
uint32 FreeSlots(Player* bot)
{
    return bot ? bot->GetFreeInventorySpace() : 0;
}

// The ore a known Mining spell smelts into item (Copper Bar -> Copper Ore); the item itself for a mined stone (Metal &
// Stone trade goods, which a node yields directly); 0 otherwise (a cooking reagent or a drop has no mining node, so a
// need for it must not send the companion to a mining node). Smelting spells are craft spells of the Mining skill
// line with the product as CREATE_ITEM.
uint32 SmeltSource(Player* bot, uint32 item)
{
    for (auto const& [spellId, playerSpell] : bot->GetSpellMap())
    {
        if (!playerSpell || playerSpell->State == PLAYERSPELL_REMOVED || !playerSpell->Active)
            continue;
        SpellInfo const* info = sSpellMgr->GetSpellInfo(spellId);
        if (!info)
            continue;
        bool product = false;
        for (uint8 i = 0; i < MAX_SPELL_EFFECTS && !product; ++i)
            product = info->Effects[i].Effect == SPELL_EFFECT_CREATE_ITEM && info->Effects[i].ItemType == item;
        if (!product)
            continue;
        SkillLineAbilityMapBounds const bounds = sSpellMgr->GetSkillLineAbilityMapBounds(spellId);
        bool mining = false;
        for (auto it = bounds.first; it != bounds.second; ++it)
            mining = mining || (it->second && it->second->SkillLine == SKILL_MINING);
        if (!mining)
            continue;
        for (uint8 i = 0; i < MAX_SPELL_REAGENTS; ++i)
            if (info->Reagent[i] > 0)
                return static_cast<uint32>(info->Reagent[i]);
    }
    ItemTemplate const* proto = sObjectMgr->GetItemTemplate(item);
    if (proto && proto->Class == ITEM_CLASS_TRADE_GOODS &&
        (proto->SubClass == ITEM_SUBCLASS_METAL_STONE ||
         (proto->SubClass == ITEM_SUBCLASS_HERB && bot->HasSkill(SKILL_HERBALISM))))
        return item;
    return 0;
}

// B8: results of milling / prospecting per item entry (world thread load, read-only afterwards).
// Gather-farm: itemEntry -> Mining/Herbalism GameObject entries that drop it.
std::mutex gLootMutex;
bool gLootLoaded = false;
std::unordered_map<uint32, std::vector<uint32>> gMillLoot;
std::unordered_map<uint32, std::vector<uint32>> gProspectLoot;
std::unordered_map<uint32, std::vector<GatherNodeInfo>> gGatherNodesByItem;

// B8/B12: tradeskill trainer spells per map (from the trainers spawned there), for the rank-blocked rule.
struct RankSpell
{
    uint32 spellId = 0;
    uint32 skill = 0;
    uint32 rank = 0;
    uint32 level = 0;
    std::array<uint32, 3> abilities = {};
};
std::mutex gRankMutex;
std::unordered_map<uint32, std::vector<RankSpell>> gRankSpells;   // map id -> spells (built once per map)

std::vector<RankSpell> const& RankSpellsFor(uint32 mapId)
{
    static std::vector<RankSpell> const none;
    std::lock_guard<std::mutex> lock(gRankMutex);
    auto it = gRankSpells.find(mapId);
    if (it != gRankSpells.end())
        return it->second;
    std::vector<CompanionBotMgr::DutySpot> const& spots =
        sCompanionBotMgr.DutySpots(mapId, CompanionBotMgr::DutySpotKind::Trainer);
    if (spots.empty())
        return none;   // not loaded yet: built on a later call
    std::vector<RankSpell>& list = gRankSpells[mapId];
    std::set<uint32> seenTrainers;
    std::set<uint32> seenSpells;
    for (CompanionBotMgr::DutySpot const& spot : spots)
    {
        if (!seenTrainers.insert(spot.entry).second)
            continue;
        Trainer::Trainer const* trainer = sObjectMgr->GetTrainer(spot.entry);
        if (!trainer || trainer->GetTrainerType() != Trainer::Type::Tradeskill)
            continue;
        for (Trainer::Spell const& spell : trainer->GetSpells())
        {
            if (!spell.ReqSkillLine || !spell.ReqSkillRank || !seenSpells.insert(spell.SpellId).second)
                continue;
            RankSpell rs;
            rs.spellId = spell.SpellId;
            rs.skill = spell.ReqSkillLine;
            rs.rank = spell.ReqSkillRank;
            rs.level = spell.ReqLevel;
            rs.abilities = spell.ReqAbility;
            list.push_back(rs);
        }
    }
    return list;
}

// The bot knows a trainer spell (the spell itself or every spell it teaches).
bool KnowsTrainerSpell(Player* bot, uint32 spellId)
{
    if (bot->HasSpell(spellId))
        return true;
    SpellInfo const* info = sSpellMgr->GetSpellInfo(spellId);
    if (!info)
        return false;
    bool teaches = false;
    for (uint8 i = 0; i < MAX_SPELL_EFFECTS; ++i)
    {
        if (info->Effects[i].Effect != SPELL_EFFECT_LEARN_SPELL)
            continue;
        teaches = true;
        if (!bot->HasSpell(info->Effects[i].TriggerSpell))
            return false;
    }
    return teaches;
}

// A trainer spell that waits only for the skill rank: skill known and below the rank, level and abilities met.
bool WaitsForRank(Player* bot, RankSpell const& rs)
{
    if (!bot->HasSkill(rs.skill) || bot->GetBaseSkillValue(rs.skill) >= rs.rank || bot->GetLevel() < rs.level)
        return false;
    for (uint32 ability : rs.abilities)
        if (ability && !bot->HasSpell(ability))
            return false;
    if (!bot->IsSpellFitByClassAndRace(rs.spellId))
        return false;
    return !KnowsTrainerSpell(bot, rs.spellId);
}

// A vendor the bot may walk to: zone fits its level, not the other team's zone, not hostile to its team (the same
// filter as the far duty spawns).
bool VendorUsable(Player* bot, VendorSpawn const& vendor)
{
    bool const horde = bot->GetTeamId() == TEAM_HORDE;
    uint32 const hostileBit = horde ? 4u : 2u;
    uint32 const team = horde ? AREATEAM_HORDE : AREATEAM_ALLY;
    if (!sCompanionBotMgr.ZoneFitsLevel(vendor.zoneId, bot->GetLevel()))
        return false;
    if ((vendor.zoneTeam == AREATEAM_ALLY || vendor.zoneTeam == AREATEAM_HORDE) && vendor.zoneTeam != team)
        return false;
    return !(vendor.hostileMask & hostileBit);
}

// Unit price (copper per item) of an item at any usable vendor of the bot's map, 0 when no vendor sells it.
uint32 VendorUnitPrice(Player* bot, MapIndex const* index, uint32 entry, CompanionCraft::Origin const* at = nullptr)
{
    if (!index)
        return 0;
    auto it = index->itemVendors.find(entry);
    if (it == index->itemVendors.end())
        return 0;
    bool usable = false;
    for (uint32 slot : it->second)
    {
        VendorSpawn const& vendor = index->vendors[slot];
        if (VendorUsable(bot, vendor) && InCity(at, vendor.x, vendor.y, vendor.z))
        {
            usable = true;
            break;
        }
    }
    ItemTemplate const* proto = sObjectMgr->GetItemTemplate(entry);
    if (!usable || !proto || proto->BuyPrice <= 0)
        return 0;
    uint32 const lot = std::max<uint32>(1, proto->BuyCount);
    return std::max<uint32>(1, static_cast<uint32>(proto->BuyPrice) / lot);
}

CompanionCraft::Color ColorOf(uint32 value, uint32 yellowAt, uint32 greyAt)
{
    // Player::UpdateCraftSkill / CraftSkillGainChance: orange up to the yellow threshold (TrivialSkillLineRankLow),
    // falling linearly to grey at TrivialSkillLineRankHigh; the green threshold is the midpoint.
    if (greyAt <= yellowAt)
        return value < greyAt ? CompanionCraft::Color::Orange : CompanionCraft::Color::Grey;
    if (value >= greyAt)
        return CompanionCraft::Color::Grey;
    if (value <= yellowAt)
        return CompanionCraft::Color::Orange;
    if (value >= (yellowAt + greyAt) / 2)
        return CompanionCraft::Color::Green;
    return CompanionCraft::Color::Yellow;
}

std::string SkillName(uint32 skillId)
{
    SkillLineEntry const* entry = sSkillLineStore.LookupEntry(skillId);
    std::string name = entry && entry->name[0] ? entry->name[0] : std::to_string(skillId);
    return name;
}

// "first-aid", "blacksmithing": the lease owner detail.
std::string SkillKey(uint32 skillId)
{
    std::string key = SkillName(skillId);
    for (char& c : key)
        c = c == ' ' ? '-' : static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return key;
}

std::string SpellName(SpellInfo const* info)
{
    return info && info->SpellName[0] ? std::string(info->SpellName[0]) : std::string("?");
}

float NeedFromGap(uint32 gap, char const* perPointKey, float perPointFallback)
{
    return std::min(100.0f, static_cast<float>(gap) * CompanionCraft::Option(perPointKey, perPointFallback));
}

// A result the party can use: consumables (food, potions, elixirs, flasks, bandages, armor kits, sharpening stones),
// trade goods (bars, leather, parts, inks, pigments) and gems.
bool PartyUse(ItemTemplate const* proto)
{
    if (!proto)
        return false;
    if (proto->Class == ITEM_CLASS_TRADE_GOODS || proto->Class == ITEM_CLASS_GEM)
        return true;
    if (proto->Class != ITEM_CLASS_CONSUMABLE)
        return false;
    switch (proto->SubClass)
    {
        case ITEM_SUBCLASS_FOOD:
        case ITEM_SUBCLASS_POTION:
        case ITEM_SUBCLASS_ELIXIR:
        case ITEM_SUBCLASS_FLASK:
        case ITEM_SUBCLASS_BANDAGE:
        case ITEM_SUBCLASS_ITEM_ENHANCEMENT:
            return true;
        default:
            return false;
    }
}

// A known spell that summons a spell focus game object with focusId (e.g. Basic Campfire for a cooking fire), whose
// reagents and tools the bot has. 0 when none.
uint32 SummonFocusSpell(Player* bot, uint32 focusId)
{
    for (auto const& [spellId, playerSpell] : bot->GetSpellMap())
    {
        if (!playerSpell || playerSpell->State == PLAYERSPELL_REMOVED || !playerSpell->Active)
            continue;
        SpellInfo const* info = sSpellMgr->GetSpellInfo(spellId);
        if (!info)
            continue;
        bool summons = false;
        for (uint8 i = 0; i < MAX_SPELL_EFFECTS; ++i)
        {
            if (info->Effects[i].Effect != SPELL_EFFECT_TRANS_DOOR &&
                info->Effects[i].Effect != SPELL_EFFECT_SUMMON_OBJECT_WILD)
                continue;
            GameObjectTemplate const* go = sObjectMgr->GetGameObjectTemplate(info->Effects[i].MiscValue);
            if (go && go->type == GAMEOBJECT_TYPE_SPELL_FOCUS && go->spellFocus.focusId == focusId)
                summons = true;
        }
        if (!summons)
            continue;
        bool ready = true;
        for (uint8 i = 0; i < MAX_SPELL_REAGENTS && ready; ++i)
            if (info->Reagent[i] > 0 && info->ReagentCount[i] &&
                !bot->HasItemCount(static_cast<uint32>(info->Reagent[i]), info->ReagentCount[i]))
                ready = false;
        for (uint8 i = 0; i < 2 && ready; ++i)
        {
            if (info->Totem[i] && !bot->HasItemCount(info->Totem[i], 1))
                ready = false;
            if (info->TotemCategory[i] && !bot->HasItemTotemCategory(info->TotemCategory[i]))
                ready = false;
        }
        if (ready)
            return spellId;
    }
    return 0;
}

// Cheapest sold item of a tool category on the map (0 when none).
uint32 ToolForCategory(MapIndex const* index, uint32 category)
{
    if (!index)
        return 0;
    auto it = index->categoryItems.find(category);
    if (it == index->categoryItems.end())
        return 0;
    uint32 best = 0;
    int32 bestPrice = std::numeric_limits<int32>::max();
    for (uint32 item : it->second)
    {
        ItemTemplate const* proto = sObjectMgr->GetItemTemplate(item);
        if (proto && proto->BuyPrice < bestPrice)
        {
            best = item;
            bestPrice = proto->BuyPrice;
        }
    }
    return best;
}

std::string Num(float value)
{
    std::ostringstream out;
    out << std::fixed << std::setprecision(1) << value;
    return out.str();
}

void PruneReservations(CraftBotState& st, uint32 now, std::vector<std::pair<uint32, uint32>>& expired)
{
    for (auto it = st.reservations.begin(); it != st.reservations.end();)
    {
        if (now >= it->untilSec)
        {
            expired.emplace_back(it->entry, it->count);
            it = st.reservations.erase(it);
        }
        else
            ++it;
    }
}

}  // namespace

bool CompanionCraft::AreaEnabled(char const* area)
{
    std::string const value = RawOption(std::string("AiPlayerbot.Companion.") + area + ".Enabled", "1");
    return !(value.empty() || value == "0" || value == "false" || value == "no" || value == "off");
}

float CompanionCraft::Option(char const* key, float fallback)
{
    std::string const value = RawOption(key, std::to_string(fallback));
    try
    {
        float const parsed = std::stof(value);
        return std::isfinite(parsed) ? parsed : fallback;
    }
    catch (...)
    {
        return fallback;
    }
}

CompanionCraft::Color CompanionCraft::ColorFor(uint32 value, uint32 yellowAt, uint32 greyAt)
{
    return ColorOf(value, yellowAt, greyAt);
}

char const* CompanionCraft::ColorName(Color color)
{
    switch (color)
    {
        case Color::Orange:
            return "orange";
        case Color::Yellow:
            return "yellow";
        case Color::Green:
            return "green";
        default:
            return "grey";
    }
}

void CompanionCraft::RegisterActivities()
{
    static std::once_flag once;
    std::call_once(once,
                   []()
                   {
                       CompanionLease::RegisterActivity("craft");
                       CompanionLease::RegisterActivity("fish");
                       CompanionLease::RegisterActivity("gather");
                   });
}

uint32 CompanionCraft::SkillGap(Player* bot, uint32 skillId)
{
    if (!bot || !bot->HasSkill(skillId))
        return 0;
    uint32 const value = bot->GetPureSkillValue(skillId);
    uint32 const max = bot->GetPureMaxSkillValue(skillId);
    uint32 const target = std::min<uint32>(5 * bot->GetLevel(), max);
    return target > value ? target - value : 0;
}

// --- material flow -----------------------------------------------------------------------------------------------

uint32 CompanionCraft::BankAvailable(Player* bot, uint32 entry)
{
    uint32 const guildId = bot ? bot->GetGuildId() : 0;
    if (!guildId)
        return 0;
    Guild* guild = sGuildMgr->GetGuildById(guildId);
    if (!guild)
        return 0;
    std::vector<CompanionDuty::BankTabUsage> tabs;
    std::vector<CompanionDuty::BankSlot> slots;
    if (!CompanionDuty::BankSnapshot(guildId, tabs, slots))
        return 0;

    uint32 total = 0;
    for (CompanionDuty::BankSlot const& slot : slots)
        if (slot.entry == entry && guild->MemberHasTabRights(bot->GetGUID(), slot.tab, GUILD_BANK_RIGHT_VIEW_TAB))
            total += slot.count;

    uint32 const low = bot->GetGUID().GetCounter();
    uint32 const now = NowSec();
    uint32 reserved = 0;
    std::lock_guard<std::mutex> lock(gMutex);
    for (auto const& [other, st] : gBots)
    {
        if (other == low)
            continue;
        for (Reservation const& res : st.reservations)
            if (res.guildId == guildId && res.entry == entry && now < res.untilSec)
                reserved += res.count;
    }
    return total > reserved ? total - reserved : 0;
}

void CompanionCraft::Reserve(Player* bot, uint32 entry, uint32 count)
{
    if (!bot || !entry || !count || !bot->GetGuildId())
        return;
    uint32 const low = bot->GetGUID().GetCounter();
    {
        std::lock_guard<std::mutex> lock(gMutex);
        Reservation res;
        res.guildId = bot->GetGuildId();
        res.entry = entry;
        res.count = count;
        res.untilSec = NowSec() + RESERVE_SECONDS;
        gBots[low].reservations.push_back(res);
    }
    CompanionDuty::RecordLedger(low, "reserve", entry, count, -1, "craft plan");
    LOG_INFO("playerbots", "Companion {}: reserve item={} count={} until=+{}s", bot->GetName(), entry, count,
             RESERVE_SECONDS);
}

void CompanionCraft::Release(uint32 lowGuid, uint32 entry, char const* note)
{
    uint32 released = 0;
    std::vector<std::pair<uint32, uint32>> expired;
    {
        std::lock_guard<std::mutex> lock(gMutex);
        CraftBotState& st = gBots[lowGuid];
        PruneReservations(st, NowSec(), expired);
        for (auto it = st.reservations.begin(); it != st.reservations.end();)
        {
            if (it->entry == entry)
            {
                released += it->count;
                it = st.reservations.erase(it);
            }
            else
                ++it;
        }
    }
    for (auto const& [item, count] : expired)
        CompanionDuty::RecordLedger(lowGuid, "release", item, count, -1, "expired");
    if (released)
        CompanionDuty::RecordLedger(lowGuid, "release", entry, released, -1, note);
}

void CompanionCraft::ReleaseAll(uint32 lowGuid, char const* note)
{
    std::vector<Reservation> all;
    {
        std::lock_guard<std::mutex> lock(gMutex);
        auto it = gBots.find(lowGuid);
        if (it == gBots.end())
            return;
        all.swap(it->second.reservations);
    }
    for (Reservation const& res : all)
        CompanionDuty::RecordLedger(lowGuid, "release", res.entry, res.count, -1, note);
}

uint32 CompanionCraft::SpendLeft(Player* bot)
{
    if (!bot)
        return 0;
    float gold = Option("AiPlayerbot.Companion.Craft.MaxVendorSpend", 5.0f);
    if (gold < 0.0f)
        gold = 0.0f;
    float cap = gold * COPPER_PER_GOLD;
    if (bot->GetLevel() < 20)
        cap = cap * bot->GetLevel() / 20.0f;
    uint32 const day = NowSec() / SECONDS_PER_DAY;
    uint32 spent = 0;
    {
        std::lock_guard<std::mutex> lock(gMutex);
        CraftBotState& st = gBots[bot->GetGUID().GetCounter()];
        if (st.spendDay != day)
        {
            st.spendDay = day;
            st.spent = 0;
        }
        spent = st.spent;
    }
    uint32 const capCopper = static_cast<uint32>(cap);
    return capCopper > spent ? capCopper - spent : 0;
}

void CompanionCraft::NoteSpend(uint32 lowGuid, uint32 copper)
{
    uint32 const day = NowSec() / SECONDS_PER_DAY;
    std::lock_guard<std::mutex> lock(gMutex);
    CraftBotState& st = gBots[lowGuid];
    if (st.spendDay != day)
    {
        st.spendDay = day;
        st.spent = 0;
    }
    st.spent += copper;
}

CompanionCraft::Place CompanionCraft::NearestBank(Player* bot, Origin const* at)
{
    Place place;
    if (!bot)
        return place;
    if (at && at->valid)
        return at->bank;   // the session city's own guild bank
    float best = std::numeric_limits<float>::max();
    for (CompanionBotMgr::DutySpot const& spot :
         sCompanionBotMgr.DutySpots(bot->GetMapId(), CompanionBotMgr::DutySpotKind::Bank))
    {
        bool const horde = bot->GetTeamId() == TEAM_HORDE;
        uint32 const team = horde ? AREATEAM_HORDE : AREATEAM_ALLY;
        if ((spot.zoneTeam == AREATEAM_ALLY || spot.zoneTeam == AREATEAM_HORDE) && spot.zoneTeam != team)
            continue;
        // Zone danger avoidance: skip cities in zones far above bot's level (e.g. Winterspring at level 27) and
        // ground walks whose path crosses a danger zone (e.g. Burning Steppes on the way to Ironforge).
        if (sCompanionBotMgr.IsZoneTooDangerous(bot, spot.zoneId))
            continue;
        if (!CompanionTransport::NeedsLegs(bot, bot->GetMapId(), spot.x, spot.y, spot.z, spot.zoneId) &&
            sCompanionBotMgr.IsPathTooDangerous(bot, bot->GetMapId(), spot.x, spot.y, spot.z))
            continue;
        if (!CompanionLease::MayTry(bot->GetGUID().GetCounter(), CompanionLease::SpawnKey(true, spot.spawnId)))
            continue;
        float const dist = bot->GetDistance(spot.x, spot.y, spot.z);
        if (dist >= best)
            continue;
        best = dist;
        place.valid = true;
        place.isGo = true;
        place.mapId = bot->GetMapId();
        place.x = spot.x;
        place.y = spot.y;
        place.z = spot.z;
        place.spawnId = spot.spawnId;
        place.entry = spot.entry;
    }
    return place;
}

CompanionCraft::Place CompanionCraft::NearestVendorFor(Player* bot, std::vector<uint32> const& items, Origin const* at)
{
    Place place;
    if (!bot || items.empty())
        return place;
    MapIndex const* index = IndexFor(bot->GetMapId());
    auto first = index->itemVendors.find(items.front());
    if (first == index->itemVendors.end())
        return place;

    // Prefer the nearest vendor that sells every item; else the nearest that sells the first one.
    float bestAll = std::numeric_limits<float>::max();
    float bestFirst = std::numeric_limits<float>::max();
    VendorSpawn const* all = nullptr;
    VendorSpawn const* any = nullptr;
    uint32 const low = bot->GetGUID().GetCounter();
    for (uint32 slot : first->second)
    {
        VendorSpawn const& vendor = index->vendors[slot];
        if (!VendorUsable(bot, vendor) || !InCity(at, vendor.x, vendor.y, vendor.z) ||
            !CompanionLease::MayTry(low, CompanionLease::SpawnKey(false, vendor.spawnId)))
            continue;
        float const dist = DistFrom(bot, at, vendor.x, vendor.y, vendor.z);
        if (dist < bestFirst)
        {
            bestFirst = dist;
            any = &vendor;
        }
        bool sellsAll = true;
        for (size_t i = 1; i < items.size() && sellsAll; ++i)
            sellsAll = VendorSlot(vendor.entry, items[i]) >= 0;
        if (sellsAll && dist < bestAll)
        {
            bestAll = dist;
            all = &vendor;
        }
    }
    VendorSpawn const* pick = all ? all : any;
    if (!pick)
        return place;
    place.valid = true;
    place.isGo = false;
    place.mapId = bot->GetMapId();
    place.x = pick->x;
    place.y = pick->y;
    place.z = pick->z;
    place.spawnId = pick->spawnId;
    place.entry = pick->entry;
    return place;
}

CompanionCraft::Place CompanionCraft::NearestFocus(Player* bot, uint32 focusId, Origin const* at)
{
    Place place;
    if (!bot || !focusId)
        return place;
    MapIndex const* index = IndexFor(bot->GetMapId());
    float const maxYards =
        at && at->valid ? at->radius : Option("AiPlayerbot.Companion.Craft.StationYards", 1500.0f);
    float best = maxYards;
    for (GoSpawn const& spawn : index->focuses)
    {
        if (spawn.focusId != focusId)
            continue;
        float const dist = DistFrom(bot, at, spawn.x, spawn.y, spawn.z);
        if (dist >= best)
            continue;
        if (!CompanionLease::MayTry(bot->GetGUID().GetCounter(), CompanionLease::SpawnKey(true, spawn.spawnId)))
            continue;
        best = dist;
        place.valid = true;
        place.isGo = true;
        place.mapId = bot->GetMapId();
        place.x = spawn.x;
        place.y = spawn.y;
        place.z = spawn.z;
        place.spawnId = spawn.spawnId;
        place.entry = spawn.entry;
    }
    return place;
}

int32 CompanionCraft::VendorSlot(uint32 vendorEntry, uint32 item)
{
    VendorItemData const* list = sObjectMgr->GetNpcVendorItemList(vendorEntry);
    if (!list)
        return -1;
    for (size_t i = 0; i < list->m_items.size(); ++i)
    {
        VendorItem const* vendorItem = list->m_items[i];
        if (vendorItem && vendorItem->item == item && vendorItem->maxcount == 0 && vendorItem->ExtendedCost == 0)
            return static_cast<int32>(i);
    }
    return -1;
}

bool CompanionCraft::HasFishingPole(Player* bot)
{
    if (!bot)
        return false;
    Item* mainHand = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_MAINHAND);
    auto isPole = [](Item* item)
    {
        ItemTemplate const* proto = item ? item->GetTemplate() : nullptr;
        return proto && proto->Class == ITEM_CLASS_WEAPON && proto->SubClass == ITEM_SUBCLASS_WEAPON_FISHING_POLE;
    };
    if (isPole(mainHand))
        return true;
    for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
        if (isPole(bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot)))
            return true;
    for (uint8 bagSlot = INVENTORY_SLOT_BAG_START; bagSlot < INVENTORY_SLOT_BAG_END; ++bagSlot)
        if (Bag* bag = bot->GetBagByPos(bagSlot))
            for (uint32 i = 0; i < bag->GetBagSize(); ++i)
                if (isPole(bag->GetItemByPos(i)))
                    return true;
    return false;
}

uint32 CompanionCraft::FishingPoleItem(Player* bot)
{
    if (!bot)
        return 0;
    MapIndex const* index = IndexFor(bot->GetMapId());
    uint32 best = 0;
    int32 bestPrice = std::numeric_limits<int32>::max();
    for (uint32 item : index->fishingPoles)
    {
        ItemTemplate const* proto = sObjectMgr->GetItemTemplate(item);
        if (!proto || proto->RequiredLevel > bot->GetLevel() || proto->BuyPrice > static_cast<int32>(bot->GetMoney()))
            continue;
        if (proto->RequiredSkill && bot->GetSkillValue(proto->RequiredSkill) < proto->RequiredSkillRank)
            continue;
        if (proto->BuyPrice < bestPrice)
        {
            best = item;
            bestPrice = proto->BuyPrice;
        }
    }
    return best;
}

// --- recipe choice -----------------------------------------------------------------------------------------------

CompanionCraft::Recipe CompanionCraft::ChooseRecipe(Player* bot, std::string& reason, Origin const* at,
                                                    std::vector<Recipe>* perSkill, Force forced,
                                                    std::vector<GatherNeed>* gatherWant)
{
    Recipe best;
    reason.clear();
    if (perSkill)
        perSkill->clear();
    if (!bot || !bot->IsInWorld())
    {
        reason = "not in world";
        return best;
    }

    uint32 const low = bot->GetGUID().GetCounter();
    MapIndex const* index = IndexFor(bot->GetMapId());
    uint32 const spendLeft = std::min<uint32>(SpendLeft(bot), bot->GetMoney());
    uint32 const batch = std::max<uint32>(
        1, static_cast<uint32>(Option("AiPlayerbot.Companion.Craft.BatchSize", static_cast<float>(DEFAULT_BATCH))));
    bool const smithGap = SkillGap(bot, SKILL_BLACKSMITHING) > 0 || SkillGap(bot, SKILL_JEWELCRAFTING) > 0 ||
                          SkillGap(bot, SKILL_ENGINEERING) > 0;
    float const rankBonus = Option("AiPlayerbot.Companion.Craft.RankTargetBonus", 20.0f);

    uint32 known = 0;
    uint32 gapless = 0;
    uint32 grey = 0;
    uint32 noMaterials = 0;
    uint32 noTool = 0;
    uint32 noStation = 0;
    uint32 skipped = 0;
    std::string firstMissing;

    // Guild bank counts, read once per entry in this call (BankAvailable copies the snapshot).
    std::unordered_map<uint32, uint32> bankMemo;
    auto bankOf = [&bankMemo, bot](uint32 entry) -> uint32
    {
        auto it = bankMemo.find(entry);
        if (it != bankMemo.end())
            return it->second;
        uint32 const count = BankAvailable(bot, entry);
        bankMemo[entry] = count;
        return count;
    };
    // B8: reagents that blocked a recipe with a skill gap (no bags, bank or vendor stock): milling and prospecting
    // run only when their results are among them.
    std::set<uint32> missing;
    std::vector<std::pair<uint32, bool>> feeders;   // known Milling (true) / Prospecting (false) spells
    std::unordered_map<uint32, Recipe> bestOfSkill;
    bool const forcedFeeder = forced == Force::Mill || forced == Force::Prospect;

    auto consider = [&best, &bestOfSkill](Recipe const& r)
    {
        if (!r.valid)
            return;
        if (!best.valid || r.rank > best.rank)
            best = r;
        auto it = bestOfSkill.find(r.skillId);
        if (it == bestOfSkill.end() || r.rank > it->second.rank)
            bestOfSkill[r.skillId] = r;
    };
    auto needOf = [bot, rankBonus](uint32 gap, uint32 skillId)
    {
        float need = NeedFromGap(gap, "AiPlayerbot.Companion.Craft.NeedPerPoint", 2.0f);
        if (need > 0.0f && rankBonus > 0.0f && RankBlocked(bot, skillId))
            need = std::min(100.0f, need + rankBonus);
        return need;
    };

    for (auto const& [spellId, playerSpell] : bot->GetSpellMap())
    {
        if (!playerSpell || playerSpell->State == PLAYERSPELL_REMOVED || !playerSpell->Active)
            continue;
        SpellInfo const* info = sSpellMgr->GetSpellInfo(spellId);
        if (!info)
            continue;
        uint32 product = 0;
        for (uint8 i = 0; i < MAX_SPELL_EFFECTS; ++i)
        {
            if (info->Effects[i].Effect == SPELL_EFFECT_MILLING)
                feeders.emplace_back(spellId, true);
            else if (info->Effects[i].Effect == SPELL_EFFECT_PROSPECTING)
                feeders.emplace_back(spellId, false);
            else if (info->Effects[i].Effect == SPELL_EFFECT_CREATE_ITEM && info->Effects[i].ItemType && !product)
                product = info->Effects[i].ItemType;
        }
        if (forcedFeeder)
            continue;   // GM test hook: only the feeder below
        if (!product)
            continue;
        SkillLineAbilityEntry const* ability = nullptr;
        SkillLineAbilityMapBounds const bounds = sSpellMgr->GetSkillLineAbilityMapBounds(spellId);
        for (auto it = bounds.first; it != bounds.second; ++it)
            if (it->second && IsCraftSkill(it->second->SkillLine))
            {
                ability = it->second;
                break;
            }
        if (!ability || !bot->HasSkill(ability->SkillLine))
            continue;
        ++known;

        uint32 const skillId = ability->SkillLine;
        uint32 const value = bot->GetPureSkillValue(skillId);
        Color const color = ColorOf(value, ability->TrivialSkillLineRankLow, ability->TrivialSkillLineRankHigh);
        bool const smelting = skillId == SKILL_MINING;
        uint32 const gap = SkillGap(bot, skillId);
        // Smelting also runs at no mining gap or grey when a smithing skill has a gap (the bars feed it).
        if (smelting ? (color == Color::Grey || gap == 0) && !smithGap : gap == 0)
        {
            ++gapless;
            continue;
        }
        if (color == Color::Grey && !smelting)
        {
            ++grey;
            continue;
        }
        if ((at && at->skip.count(spellId)) || !CompanionLease::MayTry(low, "recipe:" + std::to_string(spellId)))
        {
            ++skipped;
            continue;
        }

        Recipe r;
        r.spellId = spellId;
        r.spellName = SpellName(info);
        r.skillId = skillId;
        r.skillName = SkillName(skillId);
        r.color = color;
        r.skillValue = value;
        r.skillMax = bot->GetPureMaxSkillValue(skillId);
        r.yellowAt = ability->TrivialSkillLineRankLow;
        r.greyAt = ability->TrivialSkillLineRankHigh;
        r.product = product;
        r.smelting = smelting;
        uint32 const smithGapNow =
            smelting ? std::max(SkillGap(bot, SKILL_BLACKSMITHING), SkillGap(bot, SKILL_JEWELCRAFTING)) : 0u;
        r.gap = std::max(gap, smithGapNow);
        r.focusId = info->RequiresSpellFocus;

        // Consumed materials: the most crafts from bags + bank (vendor items are unlimited).
        uint32 crafts = batch;
        bool materialsOk = true;
        for (uint8 i = 0; i < MAX_SPELL_REAGENTS; ++i)
        {
            if (info->Reagent[i] <= 0 || !info->ReagentCount[i])
                continue;
            Material m;
            m.entry = static_cast<uint32>(info->Reagent[i]);
            m.perCraft = info->ReagentCount[i];
            m.inBags = bot->GetItemCount(m.entry, false);
            uint32 const bank = bankOf(m.entry);
            m.vendorPrice = VendorUnitPrice(bot, index, m.entry, at);
            m.fromBank = bank;   // upper bound, trimmed below
            if (!m.vendorPrice)
            {
                uint32 const cap = (m.inBags + bank) / m.perCraft;
                if (cap < crafts)
                    crafts = cap;
                if (!cap)
                {
                    materialsOk = false;
                    missing.insert(m.entry);
                    if (gatherWant)
                        gatherWant->push_back({skillId, m.entry, SmeltSource(bot, m.entry)});
                    if (firstMissing.empty())
                        firstMissing = r.spellName + " needs item " + std::to_string(m.entry);
                }
            }
            r.materials.push_back(m);
        }
        if (!materialsOk || !crafts)
        {
            ++noMaterials;
            continue;
        }

        // Tools (not consumed): owned, or bought.
        bool toolsOk = true;
        for (uint8 i = 0; i < 2 && toolsOk; ++i)
        {
            uint32 tool = 0;
            if (info->Totem[i] && !bot->HasItemCount(info->Totem[i], 1))
                tool = info->Totem[i];
            else if (info->TotemCategory[i] && !bot->HasItemTotemCategory(info->TotemCategory[i]))
                tool = ToolForCategory(index, info->TotemCategory[i]);
            else
                continue;
            uint32 const price = tool ? VendorUnitPrice(bot, index, tool, at) : 0;
            if (!price)
            {
                toolsOk = false;
                break;
            }
            Material m;
            m.entry = tool;
            m.perCraft = 1;
            m.fromVendor = 1;
            m.vendorPrice = price;
            m.tool = true;
            r.materials.push_back(m);
        }
        if (!toolsOk)
        {
            ++noTool;
            continue;
        }

        // Plan the batch: bags first, then the bank, then vendors; shrink it to the spend left.
        auto plan = [&r](uint32 n) -> uint32
        {
            uint32 cost = 0;
            for (Material& m : r.materials)
            {
                if (m.tool)
                {
                    cost += m.vendorPrice;
                    continue;
                }
                uint32 const want = n * m.perCraft;
                uint32 const fromBags = std::min(want, m.inBags);
                uint32 const bankLeft = want - fromBags;
                m.fromBank = std::min(bankLeft, m.fromBank);
                m.fromVendor = want - fromBags - m.fromBank;
                cost += m.fromVendor * m.vendorPrice;
            }
            return cost;
        };
        std::vector<uint32> bankCaps;
        for (Material const& m : r.materials)
            bankCaps.push_back(m.fromBank);
        uint32 cost = 0;
        while (crafts > 0)
        {
            for (size_t i = 0; i < r.materials.size(); ++i)
                if (!r.materials[i].tool)
                    r.materials[i].fromBank = bankCaps[i];
            cost = plan(crafts);
            if (cost <= spendLeft)
                break;
            --crafts;
        }
        if (!crafts)
        {
            ++noMaterials;
            if (firstMissing.empty())
                firstMissing = r.spellName + " needs vendor items over the daily spend left (" +
                               std::to_string(spendLeft) + " copper)";
            continue;
        }
        r.crafts = crafts;
        r.vendorCost = cost;

        // Station: a focus spawn, or a known spell that summons one.
        if (r.focusId)
        {
            r.campfireSpell = SummonFocusSpell(bot, r.focusId);
            if (!r.campfireSpell)
            {
                r.station = NearestFocus(bot, r.focusId, at);
                if (!r.station.valid)
                {
                    ++noStation;
                    continue;
                }
            }
        }

        // Where the materials come from.
        bool usesBank = false;
        std::vector<uint32> buy;
        for (Material const& m : r.materials)
        {
            if (m.fromBank)
                usesBank = true;
            if (m.fromVendor)
                buy.push_back(m.entry);
        }
        if (usesBank)
        {
            r.bank = NearestBank(bot, at);
            if (!r.bank.valid)
            {
                ++noMaterials;
                continue;
            }
        }
        if (!buy.empty())
        {
            r.vendor = NearestVendorFor(bot, buy, at);
            if (!r.vendor.valid)
            {
                ++noMaterials;
                continue;
            }
        }
        r.source = std::string("bags") + (usesBank ? "+bank" : "") + (buy.empty() ? "" : "+vendor");

        ItemPosCountVec dest;
        if (bot->CanStoreNewItem(NULL_BAG, NULL_SLOT, dest, product, 1) != EQUIP_ERR_OK)
        {
            ++noMaterials;
            if (firstMissing.empty())
                firstMissing = "bags full";
            continue;
        }

        r.partyUse = PartyUse(sObjectMgr->GetItemTemplate(product));
        uint32 colorScore = color == Color::Orange ? 3 : color == Color::Yellow ? 2 : color == Color::Green ? 1 : 0;
        bool oreInBags = false;
        bool allGuideMats = true;
        uint32 totalReagents = 0;
        for (Material const& m : r.materials)
        {
            if (m.tool)
                continue;
            if (m.inBags >= m.perCraft)
                oreInBags = true;
            totalReagents += m.perCraft;
            if (m.fromVendor == 0 && !CompanionBrain::IsLevelingGuideMat(m.entry) &&
                !CompanionBrain::IsRequestedMat(m.entry))
                allGuideMats = false;
        }
        r.rank = colorScore * 100.0f + (r.partyUse ? 20.0f : 0.0f) + r.gap * 0.1f + std::min(crafts, 10u) * 0.5f;
        // Guild Life v2: prioritize crafting items that fulfill an open guildmate work order (+Craft Night bonus).
        if (CompanionLife::HasOpenWorkOrderFor(product, bot->GetGUID().GetCounter()))
            r.rank += 180.0f;
        r.rank += CompanionLife::WeeklyCraftBonus();
        if (sPlayerbotAIConfig.companionCraftGuideAligned)
        {
            if (allGuideMats)
                r.rank += 40.0f;
            r.rank -= std::min(30.0f, static_cast<float>(totalReagents) * 3.0f);
        }
        if (smelting && oreInBags)
            r.rank += 1000.0f;   // Brannoc smelts his ore before he crafts
        r.need = needOf(r.gap, skillId);
        r.valid = r.need > 0.0f;
        consider(r);
    }

    // B8: Milling and Prospecting (item-targeted). A 5-stack of a herb / ore from the bags or the guild bank whose
    // results (milling_loot_template / prospecting_loot_template) a blocked recipe needs.
    for (auto const& [spellId, milling] : feeders)
    {
        if (!AreaEnabled(milling ? "Mill" : "Prospect"))
            continue;
        if (forcedFeeder ? forced != (milling ? Force::Mill : Force::Prospect) : missing.empty())
            continue;
        SpellInfo const* info = sSpellMgr->GetSpellInfo(spellId);
        uint32 skillId = milling ? SKILL_INSCRIPTION : SKILL_JEWELCRAFTING;
        SkillLineAbilityMapBounds const bounds = sSpellMgr->GetSkillLineAbilityMapBounds(spellId);
        for (auto it = bounds.first; it != bounds.second; ++it)
            if (it->second && IsCraftSkill(it->second->SkillLine))
                skillId = it->second->SkillLine;
        if (!info || !bot->HasSkill(skillId))
            continue;
        uint32 const gap = SkillGap(bot, skillId);
        if (!gap)
            continue;
        ++known;
        if ((at && at->skip.count(spellId)) || !CompanionLease::MayTry(low, "recipe:" + std::to_string(spellId)))
        {
            ++skipped;
            continue;
        }
        if (FreeSlots(bot) < MIN_FREE_SLOTS_TARGET)
        {
            ++noMaterials;
            if (firstMissing.empty())
                firstMissing = SpellName(info) + " needs " + std::to_string(MIN_FREE_SLOTS_TARGET) + " free bag slots";
            continue;
        }
        uint32 const skillValue = bot->GetSkillValue(skillId);
        uint32 bestItem = 0;
        uint32 bestCasts = 0;
        uint32 bestBags = 0;
        uint32 bestBank = 0;
        std::vector<uint32> bestLoot;
        std::vector<uint32> candidates;
        {
            std::lock_guard<std::mutex> lock(gLootMutex);
            for (auto const& [item, results] : milling ? gMillLoot : gProspectLoot)
                for (uint32 result : results)
                    if (forcedFeeder || missing.count(result))
                    {
                        candidates.push_back(item);
                        break;
                    }
        }
        for (uint32 item : candidates)
        {
            ItemTemplate const* proto = sObjectMgr->GetItemTemplate(item);
            if (!proto || !proto->HasFlag(milling ? ITEM_FLAG_IS_MILLABLE : ITEM_FLAG_IS_PROSPECTABLE) ||
                proto->RequiredSkillRank > skillValue)
                continue;
            uint32 const inBags = bot->GetItemCount(item, false);
            uint32 const bank = bankOf(item);
            uint32 const casts = (inBags + bank) / TARGET_STACK;
            if (casts > bestCasts)
            {
                bestItem = item;
                bestCasts = casts;
                bestBags = inBags;
                bestBank = bank;
            }
        }
        if (!bestItem)
        {
            ++noMaterials;
            if (firstMissing.empty())
                firstMissing = SpellName(info) + " has no 5 of a " + (milling ? "herb" : "ore") +
                               " whose results a recipe needs (bags + guild bank)";
            continue;
        }
        Recipe r;
        r.spellId = spellId;
        r.spellName = SpellName(info);
        r.skillId = skillId;
        r.skillName = SkillName(skillId);
        r.color = Color::Green;   // no skill-up from the cast itself (SkillChance.Milling/Prospecting)
        r.skillValue = bot->GetPureSkillValue(skillId);
        r.skillMax = bot->GetPureMaxSkillValue(skillId);
        r.targetItem = true;
        r.lootItems = TargetLoot(bestItem, milling);
        r.crafts = std::min(batch, bestCasts);
        Material m;
        m.entry = bestItem;
        m.perCraft = TARGET_STACK;
        m.inBags = bestBags;
        uint32 const want = r.crafts * TARGET_STACK;
        m.fromBank = want > bestBags ? std::min(bestBank, want - bestBags) : 0;
        r.materials.push_back(m);
        if (m.fromBank)
        {
            r.bank = NearestBank(bot, at);
            if (!r.bank.valid)
            {
                ++noMaterials;
                continue;
            }
        }
        r.source = m.fromBank ? "bags+bank" : "bags";
        r.gap = gap;
        r.rank = 900.0f + std::min(r.crafts, 10u) * 0.5f;   // results first, then the recipes that use them
        r.need = needOf(gap, skillId);
        r.valid = r.need > 0.0f;
        consider(r);
    }

    if (perSkill)
        for (auto const& [skill, r] : bestOfSkill)
            perSkill->push_back(r);

    if (!best.valid)
    {
        std::ostringstream out;
        if (!known)
            out << "no known crafting recipe with a skill (train a crafting profession)";
        else
            out << "no recipe can be made now: " << known << " known, " << gapless << " at cap or no gap, " << grey
                << " grey, " << noMaterials << " without materials (bags, bank, vendor), " << noTool
                << " without tool, " << noStation << " without station, " << skipped << " skipped after failures"
                << (firstMissing.empty() ? "" : "; e.g. " + firstMissing);
        reason = out.str();
    }
    return best;
}

CompanionCraft::Origin CompanionCraft::ChooseCity(Player* bot, std::string& name, char const* only)
{
    Origin city;
    name.clear();
    // City name of a bank spawn: its AreaTable name with underscores (bank:<spawn> when no zone is known).
    auto cityNameOf = [bot](CompanionBotMgr::DutySpot const& spot) -> std::string
    {
        uint32 zone = spot.zoneId;
        if (!zone && bot->GetMap())
            zone = bot->GetMap()->GetZoneId(PHASEMASK_NORMAL, spot.x, spot.y, spot.z);
        AreaTableEntry const* area = zone ? sAreaTableStore.LookupEntry(zone) : nullptr;
        std::string text = area && area->area_name[0] && area->area_name[0][0] ? std::string(area->area_name[0])
                                                                               : "bank:" + std::to_string(spot.spawnId);
        for (char& c : text)
            if (c == ' ')
                c = '_';
        return text;
    };
    auto lowerKey = [](std::string text)
    {
        for (char& c : text)
            c = c == ' ' ? '_' : static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        return text;
    };
    std::string const wanted = only ? lowerKey(only) : std::string();
    if (!bot || !bot->IsInWorld() || !AreaEnabled("CraftSession"))
        return city;
    float const radius = std::max(50.0f, Option("AiPlayerbot.Companion.CraftSession.CityYards", 700.0f));
    uint32 const low = bot->GetGUID().GetCounter();

    // Stations the bot's recipes with a gap need (a summoned campfire counts as present).
    std::set<uint32> foci;
    for (auto const& [spellId, playerSpell] : bot->GetSpellMap())
    {
        if (!playerSpell || playerSpell->State == PLAYERSPELL_REMOVED || !playerSpell->Active)
            continue;
        SpellInfo const* info = sSpellMgr->GetSpellInfo(spellId);
        if (!info || !info->RequiresSpellFocus)
            continue;
        SkillLineAbilityMapBounds const bounds = sSpellMgr->GetSkillLineAbilityMapBounds(spellId);
        for (auto it = bounds.first; it != bounds.second; ++it)
        {
            SkillLineAbilityEntry const* ability = it->second;
            if (!ability || !IsCraftSkill(ability->SkillLine) || !SkillGap(bot, ability->SkillLine))
                continue;
            if (ColorOf(bot->GetPureSkillValue(ability->SkillLine), ability->TrivialSkillLineRankLow,
                        ability->TrivialSkillLineRankHigh) == Color::Grey)
                continue;
            if (!SummonFocusSpell(bot, info->RequiresSpellFocus))
                foci.insert(info->RequiresSpellFocus);
        }
    }

    MapIndex const* index = IndexFor(bot->GetMapId());
    bool const horde = bot->GetTeamId() == TEAM_HORDE;
    uint32 const team = horde ? AREATEAM_HORDE : AREATEAM_ALLY;
    int32 bestFoci = -1;
    float bestDist = std::numeric_limits<float>::max();
    CompanionBotMgr::DutySpot const* pick = nullptr;
    for (CompanionBotMgr::DutySpot const& spot :
         sCompanionBotMgr.DutySpots(bot->GetMapId(), CompanionBotMgr::DutySpotKind::Bank))
    {
        if ((spot.zoneTeam == AREATEAM_ALLY || spot.zoneTeam == AREATEAM_HORDE) && spot.zoneTeam != team)
            continue;
        // Zone danger avoidance: skip cities in zones far above bot's level (e.g. Winterspring at level 27) and
        // ground walks whose path crosses a danger zone (e.g. Burning Steppes on the way to Ironforge).
        if (sCompanionBotMgr.IsZoneTooDangerous(bot, spot.zoneId))
            continue;
        if (!CompanionTransport::NeedsLegs(bot, bot->GetMapId(), spot.x, spot.y, spot.z, spot.zoneId) &&
            sCompanionBotMgr.IsPathTooDangerous(bot, bot->GetMapId(), spot.x, spot.y, spot.z))
            continue;
        // A failed session cools down its trip key or its work key (the work part has its own lease), and the owner
        // (any bank): a stalled walk to one bank must not restart at the next bank of the city (run 5: four stalls
        // at four bank spawns, each on a fresh key).
        std::string const key = "session:" + std::to_string(spot.spawnId);
        if (!CompanionLease::MayTry(low, key) || !CompanionLease::MayTry(low, key + ":work") ||
            !CompanionLease::MayTry(low, "owner:craft:session"))
            continue;
        // The session trip walks (plain MoveTo): a city across water (Darnassus from Kalimdor) is unreachable.
        if (CompanionTransport::NeedsLegs(bot, bot->GetMapId(), spot.x, spot.y, spot.z, spot.zoneId))
            continue;
        Origin probe;
        probe.valid = true;
        probe.x = spot.x;
        probe.y = spot.y;
        probe.z = spot.z;
        probe.radius = radius;
        bool vendor = false;
        for (VendorSpawn const& v : index->vendors)
            if (VendorUsable(bot, v) && InCity(&probe, v.x, v.y, v.z))
            {
                vendor = true;
                break;
            }
        if (!vendor)
            continue;
        if (!wanted.empty() && lowerKey(cityNameOf(spot)) != wanted)
            continue;
        int32 found = 0;
        for (uint32 focus : foci)
            for (GoSpawn const& g : index->focuses)
                if (g.focusId == focus && InCity(&probe, g.x, g.y, g.z))
                {
                    ++found;
                    break;
                }
        float const dist = bot->GetDistance(spot.x, spot.y, spot.z);
        if (found > bestFoci || (found == bestFoci && dist < bestDist))
        {
            bestFoci = found;
            bestDist = dist;
            pick = &spot;
        }
    }
    if (!pick)
        return city;

    city.valid = true;
    city.mapId = bot->GetMapId();
    city.x = pick->x;
    city.y = pick->y;
    city.z = pick->z;
    city.radius = radius;
    city.bank.valid = true;
    city.bank.isGo = true;
    city.bank.mapId = city.mapId;
    city.bank.x = pick->x;
    city.bank.y = pick->y;
    city.bank.z = pick->z;
    city.bank.spawnId = pick->spawnId;
    city.bank.entry = pick->entry;
    name = cityNameOf(*pick);
    return city;
}

CompanionCraft::Offer CompanionCraft::EvaluateCraft(Player* bot, bool active)
{
    RegisterActivities();
    Offer offer;
    if (!bot)
        return offer;
    uint32 const low = bot->GetGUID().GetCounter();
    if (!AreaEnabled("Craft"))
    {
        offer.reason = "off (AiPlayerbot.Companion.Craft.Enabled = 0)";
        return offer;
    }

    bool dropped = false;
    Force force = Force::None;   // GM test hook (RequestForce), pending until the run starts or it expires
    std::string forceCity;
    {
        std::lock_guard<std::mutex> lock(gMutex);
        CraftBotState& st = gBots[low];
        if (active && st.craft.valid)
        {
            // Running: keep the plan (no recompute while materials are being consumed). A forced request is done.
            st.force = Force::None;
            offer.here = true;
            if (st.craft.session)
            {
                offer.need = st.craft.sessionNeed;
                offer.key = "session:" + std::to_string(st.craft.city.bank.spawnId);
                offer.summary = "running session city=" + st.craft.cityName + " crafts " +
                                std::to_string(st.craft.sessionCrafts);
            }
            else
            {
                offer.need = st.craft.recipe.need;
                offer.key = "recipe:" + std::to_string(st.craft.recipe.spellId);
                offer.summary = "running " + st.craft.recipe.spellName + " " + std::to_string(st.craft.crafted) +
                                "/" + std::to_string(st.craft.recipe.crafts);
            }
            offer.valid = offer.need > 0.0f;
            return offer;
        }
        if (st.force != Force::None && NowSec() > st.forceUntil)
            st.force = Force::None;
        force = st.force;
        forceCity = st.forceCity;
        dropped = st.craft.started;
        st.craft = Run();
    }
    if (dropped)
        ReleaseAll(low, "run ended");

    // Forced GM requests: a feeder (mill / prospect) replaces the local recipe, a session replaces the session test.
    bool const forcedSession = force == Force::Session;
    bool const forcedFeeder = force == Force::Mill || force == Force::Prospect;
    float const forcedNeed = Option("AiPlayerbot.Companion.Craft.ForcedNeed", FORCED_NEED);

    std::string reason;
    // Craft.GatherForNeed: the reagents the known recipes lack (gathered in the open world, within Gather.NeedRange).
    std::vector<GatherNeed> gatherNeeds;
    bool const gatherOn =
        AreaEnabled("Craft") && Option("AiPlayerbot.Companion.Craft.GatherForNeed", 1.0f) > 0.5f;
    Recipe recipe = ChooseRecipe(bot, reason, nullptr, nullptr, forcedFeeder ? force : Force::None,
                                 gatherOn ? &gatherNeeds : nullptr);
    if (!active)
    {
        std::string text;
        for (GatherNeed const& n : gatherNeeds)
            text += (text.empty() ? "" : ";") + std::string("skill=") + std::to_string(n.skill) + " item=" +
                    std::to_string(n.blocked) + " source=" + std::to_string(n.source);
        std::lock_guard<std::mutex> lock(gMutex);
        CraftBotState& st = gBots[bot->GetGUID().GetCounter()];
        st.gatherNeeds = gatherNeeds;
        st.gatherSec = NowSec();
        if (!text.empty() && text != st.gatherLog)
            LOG_INFO("playerbots", "Companion {}: craft gather-for-need want {} (no gatherable stock now)",
                     bot->GetName(), text);
        st.gatherLog = text;
    }

    // B6: a crafting session in a city (one trip, everything crafted there) when it scores above the local plan.
    // BatchValue (CraftSession.BatchValue, default on): the local plan is worth its batch only (crafts / BatchSize of
    // the recipe need), so a x1 or x2 local craft does not beat a whole session in the city.
    std::string sessionNote;
    if (AreaEnabled("CraftSession") && !forcedFeeder && (forcedSession || bot->GetLevel() >= 8))
    {
        std::string cityName;
        Origin city = ChooseCity(bot, cityName, forcedSession && !forceCity.empty() ? forceCity.c_str() : nullptr);
        if (!city.valid)
            sessionNote = forcedSession && !forceCity.empty()
                              ? "session: no city named " + forceCity
                              : "session: no city (guild bank with a vendor) on this map";
        else
        {
            std::vector<Recipe> per;
            std::string cityReason;
            ChooseRecipe(bot, cityReason, &city, &per);
            float sessionNeed = 0.0f;
            std::ostringstream skills;
            for (Recipe const& r : per)
            {
                sessionNeed += r.need;
                skills << (skills.tellp() > 0 ? "," : "") << SkillKey(r.skillId) << ":" << r.skillValue << "/"
                       << r.skillMax;
            }
            sessionNeed = std::min(sessionNeed, Option("AiPlayerbot.Companion.CraftSession.MaxNeed", 150.0f));
            float const minNeed = Option("AiPlayerbot.Companion.CraftSession.MinNeed", 60.0f);
            float const factor = std::clamp(Option("AiPlayerbot.Companion.CraftSession.TravelFactor", 0.25f), 0.0f,
                                            1.0f);
            float const craftW = CompanionBrain::Weight(low, "craft", CompanionBrain::DutyDefault("craft"));
            float const travelW = CompanionBrain::Weight(low, "travel", CompanionBrain::DutyDefault("travel"));
            float const cityTravel = std::min(100.0f, bot->GetDistance(city.x, city.y, city.z) / 20.0f);
            float const sessionScore = sessionNeed * craftW - cityTravel * travelW * factor;
            bool const batchValue = Option("AiPlayerbot.Companion.CraftSession.BatchValue", 1.0f) > 0.5f;
            float const batch =
                std::max(1.0f, Option("AiPlayerbot.Companion.Craft.BatchSize", static_cast<float>(DEFAULT_BATCH)));
            float localScore = -std::numeric_limits<float>::max();
            float localShare = 1.0f;
            if (recipe.valid)
            {
                Place const& first = recipe.bank.valid     ? recipe.bank
                                     : recipe.vendor.valid ? recipe.vendor
                                                           : recipe.station;
                float const localTravel =
                    first.valid ? std::min(100.0f, bot->GetDistance(first.x, first.y, first.z) / 20.0f) : 0.0f;
                localShare = batchValue ? std::min(1.0f, static_cast<float>(recipe.crafts) / batch) : 1.0f;
                localScore = recipe.need * localShare * craftW - localTravel * travelW;
            }
            std::string const localText = recipe.valid
                                              ? Num(localScore) + (batchValue ? " (x" + std::to_string(recipe.crafts) +
                                                                                    " of " + Num(batch) + ")"
                                                                              : std::string())
                                              : std::string("none");
            if (per.empty())
                sessionNote = "session " + cityName + ": " + cityReason;
            else if (!forcedSession && sessionNeed < minNeed)
                sessionNote = "session " + cityName + ": need " + Num(sessionNeed) + " < CraftSession.MinNeed " +
                              Num(minNeed);
            else if (!forcedSession && sessionScore <= localScore)
                sessionNote = "session " + cityName + " score " + Num(sessionScore) + " <= local recipe " + localText;
            else
            {
                Run run;
                run.valid = true;
                run.duty = "craft";
                run.session = true;
                run.phase = PHASE_TRAVEL;
                run.city = city;
                run.cityName = cityName;
                run.sessionNeed = sessionNeed;
                SaveRun(low, run);

                offer.need = forcedSession ? forcedNeed : sessionNeed;
                offer.valid = true;
                offer.first = city.bank;
                offer.here = bot->GetDistance(city.x, city.y, city.z) <= CompanionDuty::FAR_SEARCH_YARDS;
                offer.key = "session:" + std::to_string(city.bank.spawnId);
                offer.travelFactor = factor;
                offer.summary = std::string(forcedSession ? "forced " : "") + "session city=" + cityName +
                                " need=" + Num(sessionNeed) + " skills=" + skills.str() + " travel=" +
                                Num(cityTravel) + "x" + Num(factor) + " (local " + localText + ")";
                return offer;
            }
        }
    }
    if (forcedSession)
    {
        offer.reason = "forced session: " + sessionNote;
        return offer;
    }

    if (!recipe.valid)
    {
        offer.reason = reason + (sessionNote.empty() ? "" : "; " + sessionNote);
        if (forcedFeeder)
            offer.reason = std::string("forced ") + (force == Force::Mill ? "mill" : "prospect") + ": " + reason;
        return offer;
    }

    Run run;
    run.valid = true;
    run.duty = "craft";
    run.recipe = recipe;
    if (recipe.bank.valid)
    {
        run.step = STEP_BANK;
        offer.first = recipe.bank;
    }
    else if (recipe.vendor.valid)
    {
        run.step = STEP_VENDOR;
        offer.first = recipe.vendor;
    }
    else if (recipe.station.valid)
    {
        run.step = STEP_STATION;
        offer.first = recipe.station;
    }
    else
    {
        run.step = recipe.campfireSpell ? STEP_STATION : STEP_WORK;
        offer.here = true;
    }
    SaveRun(low, run);

    offer.need = forcedFeeder ? forcedNeed : recipe.need;
    offer.valid = offer.need > 0.0f;
    offer.key = "recipe:" + std::to_string(recipe.spellId);
    std::ostringstream out;
    out << (forcedFeeder ? "forced " : "") << recipe.spellName << " (" << recipe.skillName << " " << recipe.skillValue
        << "/" << recipe.skillMax << ", " << ColorName(recipe.color) << ", gap " << recipe.gap << ") x"
        << recipe.crafts << " from " << recipe.source;
    if (!sessionNote.empty())
        out << "; " << sessionNote;
    offer.summary = out.str();
    return offer;
}

CompanionCraft::Offer CompanionCraft::EvaluateFish(Player* bot, bool active)
{
    RegisterActivities();
    Offer offer;
    if (!bot)
        return offer;
    uint32 const low = bot->GetGUID().GetCounter();
    if (!AreaEnabled("Fish"))
    {
        offer.reason = "off (AiPlayerbot.Companion.Fish.Enabled = 0)";
        return offer;
    }
    if (!bot->HasSkill(SKILL_FISHING))
    {
        offer.reason = "Fishing not learned (the TRAIN duty learns it at a fishing trainer)";
        return offer;
    }
    uint32 const gap = SkillGap(bot, SKILL_FISHING);
    uint32 const fishSkill = bot->GetPureSkillValue(SKILL_FISHING);
    bool const wantSagefish = CompanionBrain::IsRequestedMat(21071) && fishSkill >= 75;
    bool const wantGreaterSagefish = CompanionBrain::IsRequestedMat(21153) && fishSkill >= 175;
    bool const guildFishNeed = wantSagefish || wantGreaterSagefish;
    if (!gap && !guildFishNeed)
    {
        offer.reason = "no Fishing gap (" + std::to_string(fishSkill) + "/" +
                       std::to_string(bot->GetPureMaxSkillValue(SKILL_FISHING)) + ", level target " +
                       std::to_string(5 * bot->GetLevel()) + ")";
        return offer;
    }

    {
        std::lock_guard<std::mutex> lock(gMutex);
        CraftBotState& st = gBots[low];
        if (active && st.fish.valid)
        {
            float contNeed = NeedFromGap(gap, "AiPlayerbot.Companion.Fish.NeedPerPoint", 1.0f);
            if (guildFishNeed)
                contNeed = std::max(contNeed, 55.0f);
            offer.need = contNeed;
            offer.valid = offer.need > 0.0f;
            offer.here = true;
            offer.key = st.fish.pool ? CompanionLease::SpawnKey(true, st.fish.water.spawnId) : "fish:here";
            offer.summary = "running, casts " + std::to_string(st.fish.casts);
            return offer;
        }
        st.fish = Run();
    }

    Run run;
    run.valid = true;
    run.duty = "fish";
    float need = NeedFromGap(gap, "AiPlayerbot.Companion.Fish.NeedPerPoint", 1.0f);
    if (guildFishNeed)
        need = std::max(need, 55.0f);
    if (bot->HasSkill(SKILL_COOKING) && SkillGap(bot, SKILL_COOKING) > 0)
        need = std::min(100.0f, need + Option("AiPlayerbot.Companion.Fish.CookingBonus", 10.0f));
    float const wkFish = CompanionLife::WeeklyFishingBonus();
    if (wkFish > 0.0f)
        need = std::min(100.0f, need + wkFish);

    // Pole first.
    if (!HasFishingPole(bot))
    {
        run.poleItem = FishingPoleItem(bot);
        if (run.poleItem)
            run.poleVendor = NearestVendorFor(bot, {run.poleItem});
        if (!run.poleVendor.valid)
        {
            offer.reason = "no fishing pole, and no usable vendor on this map sells one the bot can afford";
            return offer;
        }
    }

    // Water: prefer Sagefish Schools when Quartermaster requested Raw Sagefish, else nearest fishing pool
    // within Fish.SearchYards (catches 100%), else water next to the bot.
    MapIndex const* index = IndexFor(bot->GetMapId());
    float const searchYards = Option("AiPlayerbot.Companion.Fish.SearchYards", 600.0f);
    float const sageSearchYards = std::max(searchYards, 1800.0f);
    float bestScore = searchYards;
    for (GoSpawn const& hole : index->fishingHoles)
    {
        bool const isSagePool = (hole.entry == 180656 && wantSagefish) ||
                                (hole.entry == 180663 && wantSagefish) ||
                                (hole.entry == 180684 && wantGreaterSagefish);
        float const dist = bot->GetDistance(hole.x, hole.y, hole.z);
        float const maxD = isSagePool ? sageSearchYards : searchYards;
        if (dist >= maxD || !CompanionLease::MayTry(low, CompanionLease::SpawnKey(true, hole.spawnId)))
            continue;
        if (isSagePool)
        {
            uint32 const hz = hole.zoneId ? hole.zoneId : sCompanionBotMgr.CachedZoneAt(bot->GetMapId(), hole.x, hole.y);
            if (sCompanionBotMgr.IsZoneTooDangerous(bot, hz) ||
                CompanionTransport::NeedsLegs(bot, bot->GetMapId(), hole.x, hole.y, hole.z, hz) ||
                sCompanionBotMgr.IsPathTooDangerous(bot, bot->GetMapId(), hole.x, hole.y, hole.z))
                continue;
        }
        float const score = isSagePool ? (dist - 900.0f) : dist;
        if (score >= bestScore)
            continue;
        bestScore = score;
        run.water.valid = true;
        run.water.isGo = true;
        run.water.mapId = bot->GetMapId();
        run.water.x = hole.x;
        run.water.y = hole.y;
        run.water.z = hole.z;
        run.water.spawnId = hole.spawnId;
        run.water.entry = hole.entry;
        run.pool = true;
        run.chancePct = 100;
    }

    uint32 zone = 0;
    uint32 area = 0;
    bot->GetZoneAndAreaId(zone, area);
    int32 zoneSkill = sObjectMgr->GetFishingBaseSkillLevel(area);
    if (!zoneSkill)
        zoneSkill = sObjectMgr->GetFishingBaseSkillLevel(zone);
    uint32 hereChance = 0;
    if (zoneSkill)
    {
        // GameObject::Use (fishing bobber): chance = (skill / (zone skill + 95))^2, at least 1%, 100% at no-miss skill.
        int32 const noMiss = zoneSkill + 95;
        int32 const skill = bot->GetSkillValue(SKILL_FISHING);
        if (noMiss <= 0 || skill >= noMiss)
            hereChance = 100;
        else
            hereChance = std::max<uint32>(
                1, static_cast<uint32>(std::pow(static_cast<double>(skill) / noMiss, 2.0) * 100.0));
    }
    uint32 const minChance = static_cast<uint32>(Option("AiPlayerbot.Companion.Fish.MinCatchPct", 10.0f));
    if (!run.water.valid)
    {
        if (hereChance < minChance)
        {
            offer.reason = "no fishing pool within " + std::to_string(static_cast<uint32>(searchYards)) +
                           " yd, and the catch chance here is " + std::to_string(hereChance) + "% (zone skill " +
                           std::to_string(zoneSkill) + ") < Fish.MinCatchPct " + std::to_string(minChance) + "%";
            return offer;
        }
        WorldPosition water =
            FindWaterRadial(bot, bot->GetPositionX(), bot->GetPositionY(), bot->GetPositionZ(), bot->GetMap(),
                            bot->GetPhaseMask(), FISH_HERE_MIN_YARDS, FISH_HERE_MAX_YARDS, FISH_HERE_STEP_YARDS, false,
                            16);
        if (!water.IsValid())
        {
            offer.reason = "no fishing pool within " + std::to_string(static_cast<uint32>(searchYards)) +
                           " yd and no water within " + std::to_string(static_cast<uint32>(FISH_HERE_MAX_YARDS)) +
                           " yd";
            return offer;
        }
        run.water.valid = true;
        run.water.mapId = bot->GetMapId();
        run.water.x = bot->GetPositionX();
        run.water.y = bot->GetPositionY();
        run.water.z = bot->GetPositionZ();
        run.chancePct = hereChance;
        run.zoneSkill = static_cast<uint32>(std::max(0, zoneSkill));
    }

    run.step = run.poleVendor.valid ? STEP_VENDOR : STEP_STATION;
    SaveRun(low, run);

    offer.need = need;
    offer.valid = need > 0.0f;
    offer.first = run.poleVendor.valid ? run.poleVendor : run.water;
    offer.here = !run.poleVendor.valid && !run.pool;
    offer.key = run.pool ? CompanionLease::SpawnKey(true, run.water.spawnId) : "fish:here";
    std::ostringstream out;
    out << "Fishing " << bot->GetPureSkillValue(SKILL_FISHING) << "/" << bot->GetPureMaxSkillValue(SKILL_FISHING)
        << " gap " << gap << ", " << (run.pool ? "pool" : "water here") << " chance " << run.chancePct << "%"
        << (run.poleVendor.valid ? ", buys pole " + std::to_string(run.poleItem) : "");
    offer.summary = out.str();
    return offer;
}

namespace
{
// Map guild bank requests for smelted bars to their raw gatherable ore entry.
uint32 MapBankRequestToGatherItem(uint32 reqEntry)
{
    switch (reqEntry)
    {
        case 2840:  return 2770;   // Copper Bar -> Copper Ore
        case 2841:  return 2771;   // Bronze Bar -> Tin Ore (Copper Ore is also requested separately)
        case 2842:  return 2775;   // Silver Bar -> Silver Ore
        case 3575:  return 2772;   // Iron Bar -> Iron Ore
        case 3577:  return 2776;   // Gold Bar -> Gold Ore
        case 3860:  return 3858;   // Mithril Bar -> Mithril Ore
        case 6037:  return 7911;   // Truesilver Bar -> Truesilver Ore
        case 12359: return 10620;  // Thorium Bar -> Thorium Ore
        default:    return reqEntry;
    }
}

// Verified high-density Alliance farming zones from live acore_world node counts.
// Returns an effective-distance bonus (in yards) so gatherers prefer rich farming clusters over isolated nodes.
float PrimeFarmZoneBonus(uint32 gatherItem, uint32 zoneId)
{
    switch (gatherItem)
    {
        case 2770:  // Copper Ore
        case 2835:  // Rough Stone
            if (zoneId == 1 || zoneId == 12 || zoneId == 40 || zoneId == 38 ||
                zoneId == 148 || zoneId == 3524 || zoneId == 3525)
                return 450.0f;
            break;
        case 2771:  // Tin Ore
        case 2836:  // Coarse Stone
            if (zoneId == 44 || zoneId == 10 || zoneId == 40 || zoneId == 11 ||
                zoneId == 38 || zoneId == 267 || zoneId == 331 || zoneId == 3525)
                return 450.0f;
            break;
        case 2772:  // Iron Ore
        case 2838:  // Heavy Stone
            if (zoneId == 33 || zoneId == 45 || zoneId == 3 || zoneId == 36 || zoneId == 331)
                return 450.0f;
            break;
        case 3858:  // Mithril Ore
        case 7912:  // Solid Stone
            if (zoneId == 47 || zoneId == 440 || zoneId == 405 || zoneId == 3 || zoneId == 51 || zoneId == 33)
                return 450.0f;
            break;
        case 10620: // Thorium Ore
        case 12365: // Dense Stone
            if (zoneId == 490 || zoneId == 139 || zoneId == 46 || zoneId == 618)
                return 500.0f;
            break;
        case 2447:  // Peacebloom
        case 765:   // Silverleaf
        case 2449:  // Earthroot
            if (zoneId == 141 || zoneId == 1 || zoneId == 12 || zoneId == 3524 || zoneId == 3525)
                return 450.0f;
            break;
        case 785:   // Mageroyal
        case 2450:  // Briarthorn
        case 2453:  // Bruiseweed
        case 3820:  // Stranglekelp
            if (zoneId == 40 || zoneId == 11 || zoneId == 10 || zoneId == 44 ||
                zoneId == 38 || zoneId == 148 || zoneId == 331 || zoneId == 3525)
                return 450.0f;
            break;
        case 3355:  // Wild Steelbloom
        case 3356:  // Kingsblood
        case 3357:  // Liferoot
        case 3821:  // Goldthorn
            if (zoneId == 33 || zoneId == 11 || zoneId == 45 || zoneId == 15 || zoneId == 357 || zoneId == 331)
                return 450.0f;
            break;
        case 8838:  // Sungrass
        case 13464: // Golden Sansam
        case 13465: // Mountain Silversage
        case 13463: // Dreamfoil
            if (zoneId == 490 || zoneId == 357 || zoneId == 47 || zoneId == 139 || zoneId == 16 || zoneId == 618)
                return 450.0f;
            break;
        default:
            break;
    }
    return 0.0f;
}
}  // namespace

CompanionCraft::Place CompanionCraft::NextGatherSpot(Player* bot, uint32 gatherItem, uint32 gatherSkill,
                                                     std::set<uint32> const& skipSpawns)
{
    Place best;
    if (!bot || !gatherItem || !gatherSkill)
        return best;
    uint32 const skillVal = bot->GetPureSkillValue(gatherSkill);
    std::vector<uint32> entries;
    {
        std::lock_guard<std::mutex> lock(gLootMutex);
        auto it = gGatherNodesByItem.find(gatherItem);
        if (it != gGatherNodesByItem.end())
        {
            for (GatherNodeInfo const& info : it->second)
                if (info.skillId == gatherSkill && skillVal >= info.reqSkill)
                    entries.push_back(info.goEntry);
        }
    }
    if (entries.empty())
        return best;

    uint32 const mapId = bot->GetMapId();
    MapIndex const* index = IndexFor(mapId);
    if (!index)
        return best;
    uint32 const low = bot->GetGUID().GetCounter();

    // First pass: count matching nodes per zone on this map so bots favor dense farming zones.
    std::unordered_map<uint32, uint32> zoneDensity;
    for (uint32 goEntry : entries)
    {
        auto sit = index->gatherSpawns.find(goEntry);
        if (sit == index->gatherSpawns.end())
            continue;
        for (GoSpawn const& sp : sit->second)
        {
            if (sp.zoneId)
                ++zoneDensity[sp.zoneId];
        }
    }

    float bestScore = std::numeric_limits<float>::max();
    for (uint32 goEntry : entries)
    {
        auto sit = index->gatherSpawns.find(goEntry);
        if (sit == index->gatherSpawns.end())
            continue;
        for (GoSpawn const& sp : sit->second)
        {
            float const d = bot->GetDistance(sp.x, sp.y, sp.z);
            if (d > 2600.0f)
                continue;
            if (skipSpawns.count(sp.spawnId))
                continue;
            if (!CompanionLease::MayTry(low, "gather:" + std::to_string(sp.spawnId)))
                continue;
            uint32 const zone = sp.zoneId ? sp.zoneId : sCompanionBotMgr.CachedZoneAt(mapId, sp.x, sp.y);
            if (sCompanionBotMgr.IsZoneTooDangerous(bot, zone))
                continue;
            if (CompanionTransport::NeedsLegs(bot, mapId, sp.x, sp.y, sp.z, zone))
                continue;
            if (sCompanionBotMgr.IsPathTooDangerous(bot, mapId, sp.x, sp.y, sp.z))
                continue;

            uint32 const zNodes = zoneDensity.count(zone) ? zoneDensity[zone] : 0u;
            float const effScore = d - std::min(static_cast<float>(zNodes) * 6.0f, 600.0f) -
                                   PrimeFarmZoneBonus(gatherItem, zone);
            if (effScore >= bestScore)
                continue;
            bestScore = effScore;
            best.valid = true;
            best.isGo = true;
            best.mapId = mapId;
            best.x = sp.x;
            best.y = sp.y;
            best.z = sp.z;
            best.spawnId = sp.spawnId;
            best.entry = sp.entry;
        }
    }
    return best;
}

CompanionCraft::Offer CompanionCraft::EvaluateGatherFarm(Player* bot, bool active)
{
    RegisterActivities();
    Offer offer;
    if (!bot || !sPlayerbotAIConfig.companionGatherFarmEnabled || !AreaEnabled("Gather") || !AreaEnabled("Craft"))
        return offer;
    if (!bot->HasSkill(SKILL_MINING) && !bot->HasSkill(SKILL_HERBALISM))
        return offer;

    uint32 const low = bot->GetGUID().GetCounter();
    {
        std::lock_guard<std::mutex> lock(gMutex);
        CraftBotState& st = gBots[low];
        if (active && st.gather.valid)
        {
            offer.valid = true;
            offer.here = true;
            offer.need = 85.0f;
            offer.key = "gather:" + std::to_string(st.gather.gatherItem);
            offer.summary = "farming item " + std::to_string(st.gather.gatherItem) + " (" +
                            std::to_string(bot->GetItemCount(st.gather.gatherItem, false)) + "/" +
                            std::to_string(st.gather.gatherTargetCount) + ")";
            return offer;
        }
        st.gather = Run();
    }

    if (FreeSlots(bot) < 3)
    {
        offer.reason = "need at least 3 free bag slots";
        return offer;
    }

    std::vector<GatherNeed> needs = GatherNeeds(low);
    bool const useGuildReqs = sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.GatherFarm.GuildRequests", true, false);
    if (useGuildReqs)
    {
        std::set<uint32> seenSources;
        for (GatherNeed const& n : needs)
            seenSources.insert(n.source);
        // Append Quartermaster / guild bank gathering requests so Miners & Herbalists actively supply the 40-char vault.
        for (CompanionBrain::BankRequest const& br : CompanionBrain::ListBankRequests())
        {
            uint32 const gatherItem = MapBankRequestToGatherItem(br.itemEntry);
            if (!gatherItem || !seenSources.insert(gatherItem).second)
                continue;
            GatherNeed gn;
            gn.blocked = br.itemEntry;
            gn.source = gatherItem;
            gn.skill = 0;  // marker: guild bank request (needScore handled below)
            needs.push_back(gn);
        }
    }
    if (needs.empty())
        return offer;

    uint32 const cfgBatch = std::max<uint32>(20, sPlayerbotAIConfig.companionGatherFarmBatchSize);
    uint32 const bagScaled = FreeSlots(bot) * 8;
    uint32 const targetCount = std::clamp(std::max(cfgBatch, bagScaled), cfgBatch, 80u);
    for (GatherNeed const& need : needs)
    {
        if (!need.source)
            continue;
        if (bot->GetItemCount(need.source, false) >= targetCount)
            continue;

        uint32 gatherSkill = 0;
        std::vector<uint32> matchingEntries;
        {
            std::lock_guard<std::mutex> lock(gLootMutex);
            auto it = gGatherNodesByItem.find(need.source);
            if (it == gGatherNodesByItem.end())
                continue;
            for (GatherNodeInfo const& info : it->second)
            {
                if (!bot->HasSkill(info.skillId))
                    continue;
                if (bot->GetPureSkillValue(info.skillId) < info.reqSkill)
                    continue;
                if (!gatherSkill)
                    gatherSkill = info.skillId;
                if (info.skillId == gatherSkill)
                    matchingEntries.push_back(info.goEntry);
            }
            // Also opportunistically harvest any other nodes of the same gathering profession along the route
            // (e.g. grabbing nearby Tin/Silver veins while farming Copper Ore for raid/guild bank stock).
            if (gatherSkill)
            {
                uint32 const curSkillVal = bot->GetPureSkillValue(gatherSkill);
                std::set<uint32> seen(matchingEntries.begin(), matchingEntries.end());
                for (auto const& kv : gGatherNodesByItem)
                {
                    for (GatherNodeInfo const& info : kv.second)
                    {
                        if (info.skillId == gatherSkill && curSkillVal >= info.reqSkill &&
                            seen.insert(info.goEntry).second)
                            matchingEntries.push_back(info.goEntry);
                    }
                }
            }
        }
        if (!gatherSkill || matchingEntries.empty())
            continue;

        std::string const leaseKey = "gather:" + std::to_string(need.source);
        if (!CompanionLease::MayTry(low, leaseKey))
            continue;

        Place const spot = NextGatherSpot(bot, need.source, gatherSkill, {});
        if (!spot.valid)
            continue;

        bool const isGuildReq = (need.skill == 0);
        uint32 const gap = isGuildReq ? std::max<uint32>(20, SkillGap(bot, gatherSkill))
                                      : std::max<uint32>(10, SkillGap(bot, need.skill));
        float const needScore = isGuildReq ? std::min(88.0f, 52.0f + static_cast<float>(gap) * 0.8f)
                                           : std::min(100.0f, static_cast<float>(gap) * 1.5f);

        Run run;
        run.valid = true;
        run.duty = "gather";
        run.step = STEP_STATION;
        run.gatherSkill = gatherSkill;
        run.gatherItem = need.source;
        run.gatherTargetCount = targetCount;
        run.gatherStartCount = bot->GetItemCount(need.source, false);
        run.gatherSpot = spot;
        run.gatherNodeEntries = std::move(matchingEntries);
        SaveRun(low, run);

        offer.valid = true;
        offer.need = needScore;
        offer.first = spot;
        offer.here = bot->GetDistance(spot.x, spot.y, spot.z) <= 60.0f;
        offer.key = leaseKey;
        offer.travelFactor = 0.25f;
        offer.summary = isGuildReq
                            ? ("farm guild-requested item " + std::to_string(need.source))
                            : ("farm item " + std::to_string(need.source) + " for skill " + std::to_string(need.skill));
        return offer;
    }
    return offer;
}

std::string CompanionCraft::LeaseOwner(uint32 lowGuid, std::string const& duty)
{
    if (duty == "fish")
        return "fish";
    if (duty == "gather")
    {
        std::lock_guard<std::mutex> lock(gMutex);
        auto it = gBots.find(lowGuid);
        if (it != gBots.end() && it->second.gather.valid)
            return "gather:" + std::to_string(it->second.gather.gatherItem);
        return "gather:farm";
    }
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gBots.find(lowGuid);
    if (it == gBots.end() || !it->second.craft.valid)
        return "craft:unknown";
    if (it->second.craft.session)
        return "craft:session";   // constant for the whole session (the recipe changes inside it)
    return "craft:" + SkillKey(it->second.craft.recipe.skillId);
}

std::vector<CompanionCraft::GatherNeed> CompanionCraft::GatherNeeds(uint32 lowGuid)
{
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gBots.find(lowGuid);
    if (it == gBots.end() || NowSec() - it->second.gatherSec > 120)
        return {};
    return it->second.gatherNeeds;
}

bool CompanionCraft::IsKnownReagent(Player* bot, uint32 entry)
{
    if (!bot || !entry || !AreaEnabled("Craft"))
        return false;
    for (auto const& [spellId, playerSpell] : bot->GetSpellMap())
    {
        if (!playerSpell || playerSpell->State == PLAYERSPELL_REMOVED || !playerSpell->Active)
            continue;
        SpellInfo const* info = sSpellMgr->GetSpellInfo(spellId);
        if (!info)
            continue;
        bool uses = false;
        for (uint8 i = 0; i < MAX_SPELL_REAGENTS && !uses; ++i)
            uses = info->Reagent[i] > 0 && static_cast<uint32>(info->Reagent[i]) == entry;
        if (!uses)
            continue;
        SkillLineAbilityMapBounds const bounds = sSpellMgr->GetSkillLineAbilityMapBounds(spellId);
        for (auto it = bounds.first; it != bounds.second; ++it)
            if (it->second && IsCraftSkill(it->second->SkillLine) && bot->HasSkill(it->second->SkillLine) &&
                bot->GetPureSkillValue(it->second->SkillLine) < bot->GetPureMaxSkillValue(it->second->SkillLine))
                return true;
    }
    return false;
}

void CompanionCraft::RequestForce(uint32 lowGuid, Force kind, std::string const& city)
{
    {
        std::lock_guard<std::mutex> lock(gMutex);
        CraftBotState& st = gBots[lowGuid];
        st.force = kind;
        st.forceCity = city;
        st.forceUntil = NowSec() + 600;
    }
    char const* name = kind == Force::Session ? "session" : kind == Force::Mill ? "mill" : "prospect";
    LOG_INFO("playerbots", "[Companion] craft force kind={} city={} by GM command (lowGuid={})", name,
             city.empty() ? "-" : city, lowGuid);
}

bool CompanionCraft::GetRun(uint32 lowGuid, std::string const& duty, Run& out)
{
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gBots.find(lowGuid);
    if (it == gBots.end())
        return false;
    Run const& run = duty == "fish" ? it->second.fish : (duty == "gather" ? it->second.gather : it->second.craft);
    if (!run.valid)
        return false;
    out = run;
    return true;
}

void CompanionCraft::SaveRun(uint32 lowGuid, Run const& run)
{
    std::lock_guard<std::mutex> lock(gMutex);
    CraftBotState& st = gBots[lowGuid];
    if (run.duty == "fish")
        st.fish = run;
    else if (run.duty == "gather")
        st.gather = run;
    else
        st.craft = run;
}

void CompanionCraft::ClearRun(uint32 lowGuid, std::string const& duty)
{
    {
        std::lock_guard<std::mutex> lock(gMutex);
        CraftBotState& st = gBots[lowGuid];
        if (duty == "fish")
            st.fish = Run();
        else if (duty == "gather")
            st.gather = Run();
        else
            st.craft = Run();
    }
    if (duty != "fish" && duty != "gather")
        ReleaseAll(lowGuid, "run ended");
}

// --- hooks -------------------------------------------------------------------------------------------------------

bool CompanionCraft::IsPlannedMaterial(uint32 lowGuid, uint32 entry)
{
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gBots.find(lowGuid);
    if (it == gBots.end() || !it->second.craft.valid)
        return false;
    for (Material const& m : it->second.craft.recipe.materials)
        if (m.entry == entry)
            return true;
    if (it->second.craft.session && it->second.craft.started && it->second.craft.phase < PHASE_DEPOSIT)
    {
        // B6: pigments, inks, gems, bars and other trade goods feed the next recipe of the session; they are
        // deposited at its end.
        ItemTemplate const* proto = sObjectMgr->GetItemTemplate(entry);
        if (proto && (proto->Class == ITEM_CLASS_TRADE_GOODS || proto->Class == ITEM_CLASS_GEM))
            return true;
    }
    return false;
}

bool CompanionCraft::OverKeepStacks(Player* bot, ItemTemplate const* proto)
{
    if (!bot || !proto || proto->Class != ITEM_CLASS_CONSUMABLE ||
        (proto->SubClass != ITEM_SUBCLASS_FOOD && proto->SubClass != ITEM_SUBCLASS_BANDAGE))
        return false;
    if (!AreaEnabled("Craft"))
        return false;
    float const stacks = std::max(0.0f, Option("AiPlayerbot.Companion.Craft.KeepStacks", 1.0f));
    uint32 const keep = static_cast<uint32>(stacks * proto->GetMaxStackSize());
    return bot->GetItemCount(proto->ItemId, false) > keep;
}

uint32 CompanionCraft::TrainSignature(Player* bot)
{
    if (!bot)
        return 0;
    uint32 capped = 0;
    uint32 sumMax = 0;
    for (uint32 skill : PROFESSION_SKILLS)
    {
        if (!bot->HasSkill(skill))
            continue;
        uint32 const max = bot->GetPureMaxSkillValue(skill);
        sumMax += max;
        if (bot->GetPureSkillValue(skill) >= max)
            ++capped;
    }
    uint32 missing = 0;
    uint32 ranksReached = 0;
    uint32 classPending = 0;
    if (AreaEnabled("Train"))
    {
        for (uint32 skill : SECONDARY_SKILLS)
            if (!bot->HasSkill(skill))
                ++missing;
        // B8: a profession reaching a trainer spell's ReqSkillRank (Prospecting needs Jewelcrafting 20) changes the
        // signature, so the far trainer search runs again.
        for (RankSpell const& rs : RankSpellsFor(bot->GetMapId()))
            if (bot->HasSkill(rs.skill) && bot->GetBaseSkillValue(rs.skill) >= rs.rank)
                ++ranksReached;
        // B12: a class trainer spell becoming learnable (or learned) changes it too.
        classPending = ClassSpellsPending(bot);
    }
    uint32 sig = bot->GetLevel() * 100000 + capped * 1000 + missing * 100 + (sumMax / SKILL_STEP) % 100;
    sig = sig * 31u + ranksReached;
    sig = sig * 31u + classPending;
    return sig;
}

bool CompanionCraft::RankBlocked(Player* bot, uint32 skillId)
{
    if (!bot || !AreaEnabled("Train"))
        return false;
    for (RankSpell const& rs : RankSpellsFor(bot->GetMapId()))
        if (rs.skill == skillId && WaitsForRank(bot, rs))
            return true;
    return false;
}

std::vector<std::string> CompanionCraft::RankBlockedLines(Player* bot)
{
    std::vector<std::string> lines;
    if (!bot)
        return lines;
    // Lowest rank per skill first: that is the next trainer visit the CRAFT duty works toward.
    std::unordered_map<uint32, RankSpell const*> next;
    for (RankSpell const& rs : RankSpellsFor(bot->GetMapId()))
    {
        if (!WaitsForRank(bot, rs))
            continue;
        auto it = next.find(rs.skill);
        if (it == next.end() || rs.rank < it->second->rank)
            next[rs.skill] = &rs;
    }
    for (auto const& [skill, rs] : next)
    {
        SpellInfo const* info = sSpellMgr->GetSpellInfo(rs->spellId);
        std::ostringstream out;
        out << SpellName(info) << " (" << rs->spellId << ") needs " << SkillName(skill) << " " << rs->rank
            << " (have " << bot->GetBaseSkillValue(skill) << "): CRAFT levels it first";
        lines.push_back(out.str());
    }
    return lines;
}

uint32 CompanionCraft::ClassSpellsPending(Player* bot)
{
    if (!bot)
        return 0;
    std::vector<Trainer::Trainer const*> const* trainers = nullptr;
    try
    {
        trainers = &sObjectMgr->GetClassTrainers(bot->getClass());
    }
    catch (std::out_of_range const&)
    {
        return 0;
    }
    std::set<uint32> pending;
    for (Trainer::Trainer const* trainer : *trainers)
    {
        if (!trainer || !trainer->IsTrainerValidForPlayer(bot))
            continue;
        for (Trainer::Spell const& spell : trainer->GetSpells())
            if (trainer->CanTeachSpell(bot, &spell))
                pending.insert(spell.SpellId);
    }
    return static_cast<uint32>(pending.size());
}

void CompanionCraft::LoadWorldData()
{
    {
        std::lock_guard<std::mutex> lock(gLootMutex);
        if (gLootLoaded)
            return;
    }
    // World thread, once: milling_loot_template / prospecting_loot_template (Entry = herb / ore, Item = result, or
    // Reference -> reference_loot_template rows, one level deep).
    auto load = [](char const* table, std::unordered_map<uint32, std::vector<uint32>>& out)
    {
        std::unordered_map<uint32, std::vector<uint32>> refs;   // reference id -> entries using it
        if (QueryResult rows = WorldDatabase.Query("SELECT Entry, Item, Reference FROM {}", table))
        {
            do
            {
                Field* f = rows->Fetch();
                uint32 const entry = f[0].Get<uint32>();
                uint32 const item = f[1].Get<uint32>();
                int32 const reference = f[2].Get<int32>();
                if (reference > 0)
                    refs[static_cast<uint32>(reference)].push_back(entry);
                else if (item)
                    out[entry].push_back(item);
            } while (rows->NextRow());
        }
        if (refs.empty())
            return;
        std::ostringstream ids;
        for (auto const& [ref, entries] : refs)
            ids << (ids.tellp() > 0 ? "," : "") << ref;
        if (QueryResult rows = WorldDatabase.Query(
                "SELECT Entry, Item FROM reference_loot_template WHERE Reference = 0 AND Entry IN ({})", ids.str()))
        {
            do
            {
                Field* f = rows->Fetch();
                auto it = refs.find(f[0].Get<uint32>());
                if (it == refs.end())
                    continue;
                for (uint32 entry : it->second)
                    out[entry].push_back(f[1].Get<uint32>());
            } while (rows->NextRow());
        }
    };
    std::unordered_map<uint32, std::vector<uint32>> mill;
    std::unordered_map<uint32, std::vector<uint32>> prospect;
    load("milling_loot_template", mill);
    load("prospecting_loot_template", prospect);

    // Gather-farm lookup: map each mined ore/stone and gathered herb item -> Mining/Herbalism GameObject entries.
    std::unordered_map<uint32, std::vector<GatherNodeInfo>> gatherNodes;
    if (QueryResult rows = WorldDatabase.Query(
            "SELECT gt.entry, gt.data0, l.Item FROM gameobject_template gt "
            "JOIN gameobject_loot_template l ON l.Entry = gt.data1 "
            "WHERE gt.type = 3 AND gt.data0 > 0 AND gt.data1 > 0 AND l.Reference = 0 AND l.Item > 0"))
    {
        std::set<std::pair<uint32, uint32>> seen;
        do
        {
            Field* f = rows->Fetch();
            uint32 const goEntry = f[0].Get<uint32>();
            uint32 const lockId = f[1].Get<uint32>();
            uint32 const itemEntry = f[2].Get<uint32>();
            LockEntry const* lock = sLockStore.LookupEntry(lockId);
            if (!lock)
                continue;
            uint32 skillId = 0;
            uint32 reqSkill = 0;
            for (uint8 i = 0; i < MAX_LOCK_CASE && !skillId; ++i)
            {
                if (lock->Type[i] == LOCK_KEY_SKILL)
                {
                    uint32 const sk = SkillByLockType(LockType(lock->Index[i]));
                    if (sk == SKILL_MINING || sk == SKILL_HERBALISM)
                    {
                        skillId = sk;
                        reqSkill = lock->Skill[i];
                    }
                }
            }
            if (!skillId)
                continue;
            if (seen.insert({itemEntry, goEntry}).second)
                gatherNodes[itemEntry].push_back({goEntry, skillId, reqSkill});
        } while (rows->NextRow());
    }

    std::lock_guard<std::mutex> lock(gLootMutex);
    gMillLoot.swap(mill);
    gProspectLoot.swap(prospect);
    gGatherNodesByItem.swap(gatherNodes);
    gLootLoaded = true;
    LOG_INFO("playerbots", "[Companion] craft loot tables: {} millable, {} prospectable, {} gatherable items",
             gMillLoot.size(), gProspectLoot.size(), gGatherNodesByItem.size());
}

std::vector<uint32> CompanionCraft::TargetLoot(uint32 itemEntry, bool milling)
{
    std::lock_guard<std::mutex> lock(gLootMutex);
    auto const& table = milling ? gMillLoot : gProspectLoot;
    auto it = table.find(itemEntry);
    return it == table.end() ? std::vector<uint32>() : it->second;
}

void CompanionCraft::SaveWeapons(uint32 lowGuid, ObjectGuid mainHand, ObjectGuid offHand)
{
    std::lock_guard<std::mutex> lock(gMutex);
    CraftBotState& st = gBots[lowGuid];
    if (st.weaponsSaved)
        return;   // keep the first (real) weapons
    st.weaponsSaved = true;
    st.savedMain = mainHand;
    st.savedOff = offHand;
}

bool CompanionCraft::SavedWeapons(uint32 lowGuid, ObjectGuid& mainHand, ObjectGuid& offHand)
{
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gBots.find(lowGuid);
    if (it == gBots.end() || !it->second.weaponsSaved)
        return false;
    mainHand = it->second.savedMain;
    offHand = it->second.savedOff;
    return true;
}

void CompanionCraft::ClearSavedWeapons(uint32 lowGuid)
{
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gBots.find(lowGuid);
    if (it == gBots.end())
        return;
    it->second.weaponsSaved = false;
    it->second.savedMain = ObjectGuid();
    it->second.savedOff = ObjectGuid();
}

// --- reports -----------------------------------------------------------------------------------------------------

std::vector<std::string> CompanionCraft::SkillLines(Player* bot)
{
    std::vector<std::string> lines;
    if (!bot)
        return lines;
    lines.push_back(bot->GetName() + " level " + std::to_string(bot->GetLevel()) + ", money " +
                    std::to_string(bot->GetMoney()) + " c, vendor spend left today " +
                    std::to_string(SpendLeft(bot)) + " c, fishing pole: " + (HasFishingPole(bot) ? "yes" : "no"));
    for (uint32 skill : PROFESSION_SKILLS)
    {
        if (!bot->HasSkill(skill))
        {
            lines.push_back(SkillName(skill) + " (" + std::to_string(skill) + "): not learned");
            continue;
        }
        uint32 const gap = SkillGap(bot, skill);
        std::ostringstream out;
        out << SkillName(skill) << " (" << skill << "): " << bot->GetPureSkillValue(skill) << "/"
            << bot->GetPureMaxSkillValue(skill) << " (with bonus " << bot->GetSkillValue(skill) << "/"
            << bot->GetMaxSkillValue(skill) << ") gap=" << gap;
        lines.push_back(out.str());
    }
    lines.push_back("Train signature " + std::to_string(TrainSignature(bot)) + ", class trainer spells learnable now " +
                    std::to_string(ClassSpellsPending(bot)));
    for (std::string const& line : RankBlockedLines(bot))
        lines.push_back("Train waits: " + line);
    return lines;
}

std::vector<std::string> CompanionCraft::CraftLines(Player* bot)
{
    std::vector<std::string> lines;
    if (!bot)
        return lines;
    uint32 const low = bot->GetGUID().GetCounter();
    std::string reason;
    // B6: the session city and what the bot would craft there.
    {
        std::string cityName;
        Origin const city = ChooseCity(bot, cityName);
        if (!city.valid)
            lines.push_back(std::string("Session: ") + (AreaEnabled("CraftSession") ? "no city on this map" : "off"));
        else
        {
            std::vector<Recipe> per;
            std::string cityReason;
            ChooseRecipe(bot, cityReason, &city, &per);
            std::ostringstream out;
            out << "Session: city=" << cityName << " bank spawn " << city.bank.spawnId << " ("
                << Num(bot->GetDistance(city.x, city.y, city.z)) << " yd):";
            float need = 0.0f;
            for (Recipe const& p : per)
            {
                need += p.need;
                out << " " << p.spellName << " (" << p.skillName << " need " << Num(p.need) << ")";
            }
            if (per.empty())
                out << " nothing: " << cityReason;
            else
                out << "; need " << Num(need);
            lines.push_back(out.str());
        }
    }
    Recipe const r = ChooseRecipe(bot, reason);
    if (!r.valid)
    {
        lines.push_back("No recipe here: " + reason);
        return lines;
    }
    float const weight = CompanionBrain::Weight(low, "craft", CompanionBrain::DutyDefault("craft"));
    float const travelW = CompanionBrain::Weight(low, "travel", CompanionBrain::DutyDefault("travel"));
    Place const& first = r.bank.valid ? r.bank : r.vendor.valid ? r.vendor : r.station;
    float const travel = first.valid ? std::min(100.0f, bot->GetDistance(first.x, first.y, first.z) / 20.0f) : 0.0f;
    std::ostringstream head;
    head << "Recipe: " << r.spellName << " (spell " << r.spellId << ") skill=" << r.skillName << " " << r.skillValue
         << "/" << r.skillMax << " color=" << ColorName(r.color) << " (yellow at " << r.yellowAt << ", grey at "
         << r.greyAt << ") crafts=" << r.crafts << " product=" << r.product << (r.partyUse ? " (party use)" : "")
         << (r.smelting ? " smelting" : "");
    lines.push_back(head.str());
    std::ostringstream src;
    src << "Materials from " << r.source << ", vendor cost " << r.vendorCost << " c (spend left "
        << SpendLeft(bot) << " c)";
    lines.push_back(src.str());
    for (Material const& m : r.materials)
    {
        ItemTemplate const* proto = sObjectMgr->GetItemTemplate(m.entry);
        std::ostringstream out;
        out << "  " << (m.tool ? "tool " : "") << m.entry << " " << (proto ? proto->Name1 : "?") << ": per craft "
            << m.perCraft << ", bags " << m.inBags << ", bank " << m.fromBank << ", vendor " << m.fromVendor
            << (m.vendorPrice ? " at " + std::to_string(m.vendorPrice) + " c" : "");
        lines.push_back(out.str());
    }
    std::ostringstream station;
    station << "Station: "
            << (!r.focusId           ? std::string("none")
                : r.campfireSpell    ? "focus " + std::to_string(r.focusId) + " by spell " +
                                        std::to_string(r.campfireSpell)
                : "focus " + std::to_string(r.focusId) + " at spawn " + std::to_string(r.station.spawnId) + " (" +
                                        Num(bot->GetDistance(r.station.x, r.station.y, r.station.z)) + " yd)");
    if (r.bank.valid)
        station << "; bank spawn " << r.bank.spawnId;
    if (r.vendor.valid)
        station << "; vendor " << r.vendor.entry << " spawn " << r.vendor.spawnId;
    lines.push_back(station.str());
    std::ostringstream score;
    score << "Score " << Num(r.need * weight - travel * travelW) << " = need " << Num(r.need) << " (gap " << r.gap
          << ") x weight " << Num(weight) << " - travel " << Num(travel) << " x " << Num(travelW)
          << "; recipe rank " << Num(r.rank);
    lines.push_back(score.str());
    return lines;
}
