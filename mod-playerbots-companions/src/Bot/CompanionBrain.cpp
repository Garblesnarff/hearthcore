/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "CompanionBrain.h"
#include "CompanionBotMgr.h"
#include "CompanionClassStrategy.h"
#include "CompanionCoopAdmissionLogic.h"
#include "CompanionCoopAuthLogic.h"
#include "CompanionCoopDrainLogic.h"
#include "CompanionDuty.h"
#include "CompanionCraft.h"
#include "CompanionLife.h"
#include "CompanionWorkOrderLogic.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <unordered_set>

#include "Bag.h"
#include "Config.h"
#include "DBCStores.h"
#include "DatabaseEnv.h"
#include "GameTime.h"
#include "Group.h"
#include "GroupMgr.h"
#include "Item.h"
#include "ItemTemplate.h"
#include "ItemUsageValue.h"
#include "LFGMgr.h"
#include "Log.h"
#include "Map.h"
#include "MapMgr.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "Playerbots.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "StringFormat.h"
#include "WorldSessionMgr.h"

namespace
{
// Hearthstone item entry. Never sold, banked or destroyed (B13).
constexpr uint32 ITEM_HEARTHSTONE_ENTRY = CompanionBrain::ITEM_HEARTHSTONE;

std::mutex gBankReqMutex;
std::unordered_map<uint32, CompanionBrain::BankRequest> gBankRequests;
bool gBankReqMissingLogged = false;

struct ActiveOrderProp
{
    uint64 proposalId = 0;
    uint64 orderId = 0;
    uint32 itemEntry = 0;
    uint32 snapshotRev = 1;
    uint32 expiresAt = 0;
    std::string status;
};
std::mutex gPropMutex;
std::unordered_map<uint32, ActiveOrderProp> gActiveOrderProps;  // lowGuid -> active validated work-order proposal
CompanionWorkOrderLogic::BoundedIdSet gCompletedPropIds;        // prevents stale ReloadProposals from re-inserting completed proposals
bool gPropMissingLogged = false;
bool gOverrideMissingLogged = false;

std::mutex gCoopMutex;
CompanionCoopAuth::Registry gCoopRegistry;  // Issue #11: exact native group & roster authorization registry
bool gCoopSchemaMissingLogged = false;

std::mutex gGroupTokenMutex;
std::unordered_map<Group const*, uint64> gGroupTokens;
uint64 gNextGroupToken = 1;

struct PendingCoopBind
{
    uint64 opportunityId = 0;
    uint32 revision = 0;
    uint32 previousGroupId = 0;
    uint32 instanceId = 0;
    bool isLfgRebind = false;
    CompanionCoopAuth::NativeGroupSnapshot snap;
};
struct PendingCoopDisband
{
    uint32 groupId = 0;
    bool hadDrainedOpp = false;
    std::vector<CompanionCoopAuth::Registry::DisbandTransition> transitions;
};
struct PendingCoopExit
{
    uint64 opportunityId = 0;
    std::vector<uint32> exitedMemberGuids;
    CompanionCoopDrain::DrainReason reason = CompanionCoopDrain::DrainReason::None;
    bool groupFullyExited = false;
    uint8 retryCount = 0;
};
constexpr uint8 COOP_EXIT_MAX_RETRIES = 5;
std::mutex gCoopQueueMutex;
std::unordered_map<uint64, PendingCoopBind> gPendingCoopBinds;  // oppId -> latest queued bind
std::vector<PendingCoopDisband> gPendingCoopDisbands;
std::vector<PendingCoopExit> gPendingCoopExits;
std::unordered_set<uint64> gPendingDrainedOpps;                 // retained until DB reload confirms opp is no longer active/assembling
std::unordered_map<uint64, uint32> gFailedBindCooldown;         // oppId -> unix time when retry allowed
std::unordered_map<uint64, uint32> gAdmissionBlockedOpps;       // oppId -> blocked until unix time (stops 15s retry loop)
constexpr uint32 COOP_BIND_FAIL_BACKOFF_SEC = 15;

// Issue #22: Accepted native group ownership & safe-boundary drain coordinator (strictly separated from admission mode).
std::mutex gDrainMutex;
CompanionCoopDrain::DrainCoordinator gDrainCoordinator;
std::atomic<bool> gCoopBootProbeDone{false};
std::atomic<bool> gCoopDbHealthy{true};

// Issue #12: Native admission controller, world-tick p99 ring buffer & host telemetry sampler.
std::mutex gAdmissionMutex;
CompanionCoopAdmission::AdmissionController gAdmissionController;
CompanionCoopAdmission::TickRingBuffer gTickRing;
uint32 gTelemetrySampleTimer = 0;
constexpr uint32 COOP_TELEMETRY_SAMPLE_MS = 5000;
uint64 gPrevSwapPages = 0;
uint32 gPrevSwapSampleSec = 0;
bool gHasPrevSwap = false;
bool gHysteresisLoaded = false;
bool gHysteresisPersistDirty = false;
bool gHysteresisPersistPaused = false;
uint32 gHysteresisPersistTs = 0;
std::vector<std::string> gHysteresisPersistReasons;

CompanionCoopAdmission::AdmissionConfig LoadNativeAdmissionConfig()
{
    CompanionCoopAdmission::AdmissionConfig cfg;
    cfg.coopEnabled = CompanionBrain::CoopEnabled();
    cfg.enabledDungeonMaps = CompanionCoopAdmission::ParseEnabledMapList(
        sConfigMgr->GetOption<std::string>("AiPlayerbot.Companion.Coop.EnabledDungeonMaps", "", false));
    cfg.maxManagedGroups = sConfigMgr->GetOption<uint32>(
        "AiPlayerbot.Companion.Coop.MaxManagedGroups", CompanionCoopAdmission::DEFAULT_MAX_MANAGED_GROUPS, false);
    cfg.maxTotalInstances = sConfigMgr->GetOption<uint32>(
        "AiPlayerbot.Companion.Coop.MaxTotalInstances", CompanionCoopAdmission::DEFAULT_MAX_TOTAL_INSTANCES, false);
    cfg.maxOnlinePopulation = sConfigMgr->GetOption<uint32>(
        "AiPlayerbot.Companion.Coop.MaxOnlinePopulation", CompanionCoopAdmission::DEFAULT_MAX_ONLINE_POPULATION, false);
    cfg.minAvailRamMib = sConfigMgr->GetOption<float>("AiPlayerbot.Companion.Coop.MinAvailRamMib", 512.0f, false);
    cfg.recoverAvailRamMib =
        sConfigMgr->GetOption<float>("AiPlayerbot.Companion.Coop.RecoverAvailRamMib", 640.0f, false);
    cfg.maxTickP99Ms = sConfigMgr->GetOption<float>("AiPlayerbot.Companion.Coop.MaxTickP99Ms", 45.0f, false);
    cfg.recoverTickP99Ms = sConfigMgr->GetOption<float>("AiPlayerbot.Companion.Coop.RecoverTickP99Ms", 38.0f, false);
    cfg.maxSwapIoPagesPerSec =
        sConfigMgr->GetOption<float>("AiPlayerbot.Companion.Coop.MaxSwapIoPagesPerSec", 25.0f, false);
    cfg.recoverSwapIoPagesPerSec =
        sConfigMgr->GetOption<float>("AiPlayerbot.Companion.Coop.RecoverSwapIoPagesPerSec", 10.0f, false);
    cfg.maxCpuPsiSomeAvg10 =
        sConfigMgr->GetOption<float>("AiPlayerbot.Companion.Coop.MaxCpuPsiSomeAvg10", 20.0f, false);
    cfg.recoverCpuPsiSomeAvg10 =
        sConfigMgr->GetOption<float>("AiPlayerbot.Companion.Coop.RecoverCpuPsiSomeAvg10", 10.0f, false);
    cfg.maxTelemetryAgeSec = sConfigMgr->GetOption<uint32>(
        "AiPlayerbot.Companion.Coop.MaxTelemetryAgeSec", CompanionCoopAdmission::DEFAULT_MAX_TELEMETRY_AGE_SEC, false);
    cfg.tokenTtlSec = sConfigMgr->GetOption<uint32>(
        "AiPlayerbot.Companion.Coop.AdmissionTokenTtlSec", CompanionCoopAdmission::DEFAULT_TOKEN_TTL_SEC, false);
    return cfg;
}

std::string NativeHeadroomStatePath()
{
    return sConfigMgr->GetOption<std::string>("AiPlayerbot.Companion.Coop.HeadroomStateFile",
                                              CompanionCoopAdmission::DEFAULT_HEADROOM_STATE_FILE, false);
}

bool PersistHysteresisStateOnWorldThread(bool paused, uint32 nowSec, std::vector<std::string> const& reasons)
{
    std::string const path = NativeHeadroomStatePath();
    if (path.empty())
        return true;
    std::string const tmp = path + ".tmp";
    std::ofstream out(tmp, std::ios::trunc);
    if (!out)
    {
        LOG_WARN("playerbots", "[CompanionCoop] Failed opening headroom state temp file '{}'", tmp);
        return false;
    }
    out << CompanionCoopAdmission::FormatHeadroomStateJson(paused, nowSec, reasons);
    out.flush();
    if (!out.good())
    {
        LOG_WARN("playerbots", "[CompanionCoop] Failed writing headroom state temp file '{}'", tmp);
        return false;
    }
    out.close();
    if (std::rename(tmp.c_str(), path.c_str()) != 0)
    {
        LOG_WARN("playerbots", "[CompanionCoop] Failed renaming headroom state '{}' -> '{}'", tmp, path);
        return false;
    }
    return true;
}

void FlushHysteresisPersistenceOnWorldThread()
{
    bool paused = false;
    uint32 ts = 0;
    std::vector<std::string> reasons;
    {
        std::lock_guard<std::mutex> lock(gAdmissionMutex);
        if (!gHysteresisPersistDirty)
            return;
        paused = gHysteresisPersistPaused;
        ts = gHysteresisPersistTs;
        reasons = gHysteresisPersistReasons;
        gHysteresisPersistDirty = false;
    }
    if (!PersistHysteresisStateOnWorldThread(paused, ts, reasons))
    {
        // Fail closed in memory if state file cannot be persisted!
        std::lock_guard<std::mutex> lock(gAdmissionMutex);
        gAdmissionController.LoadHysteresisState(true);
    }
}

void EnsureHysteresisLoaded()
{
    if (gHysteresisLoaded)
        return;
    gHysteresisLoaded = true;
    std::string const path = NativeHeadroomStatePath();
    if (path.empty())
        return;
    std::error_code ec;
    bool const exists = std::filesystem::exists(path, ec);
    if (ec)
    {
        LOG_WARN("playerbots", "[CompanionCoop] Unable to stat headroom state '{}'; failing closed (paused=true)", path);
        std::lock_guard<std::mutex> lock(gAdmissionMutex);
        gAdmissionController.LoadHysteresisState(true);
        return;
    }
    if (!exists)
        return;

    std::ifstream in(path);
    if (!in)
    {
        LOG_WARN("playerbots", "[CompanionCoop] Headroom state '{}' unreadable; failing closed (paused=true)", path);
        std::lock_guard<std::mutex> lock(gAdmissionMutex);
        gAdmissionController.LoadHysteresisState(true);
        return;
    }
    std::ostringstream ss;
    ss << in.rdbuf();
    bool paused = true;
    if (!CompanionCoopAdmission::ParseHeadroomPausedJson(ss.str(), paused))
    {
        LOG_WARN("playerbots", "[CompanionCoop] Corrupt headroom state '{}'; failing closed (paused=true)", path);
        paused = true;
    }
    std::lock_guard<std::mutex> lock(gAdmissionMutex);
    gAdmissionController.LoadHysteresisState(paused);
}

void SampleCoopHostTelemetry(uint32 nowSec)
{
    CompanionCoopAdmission::TelemetrySnapshot snap;
    snap.valid = true;
    snap.sampledAt = nowSec;

    // 1. /proc/meminfo -> MemAvailable (MiB)
    {
        std::ifstream mem("/proc/meminfo");
        double mib = 0.0;
        if (mem && CompanionCoopAdmission::ParseMemAvailableMib(mem, mib))
        {
            snap.hasMemAvail = true;
            snap.memAvailMib = mib;
        }
    }

    // 2. /proc/vmstat -> delta (pswpin + pswpout) / dt -> swap pages/sec
    {
        std::ifstream vm("/proc/vmstat");
        std::uint64_t totalPages = 0;
        if (vm && CompanionCoopAdmission::ParseVmstatSwapPages(vm, totalPages))
        {
            if (gHasPrevSwap && nowSec > gPrevSwapSampleSec && totalPages >= gPrevSwapPages)
            {
                double const dt = static_cast<double>(nowSec - gPrevSwapSampleSec);
                snap.hasSwapIo = true;
                snap.swapIoPagesPerSec = static_cast<double>(totalPages - gPrevSwapPages) / dt;
            }
            gPrevSwapPages = totalPages;
            gPrevSwapSampleSec = nowSec;
            gHasPrevSwap = true;
        }
    }

    // 3. /proc/pressure/cpu -> "some avg10=..."
    {
        std::ifstream psi("/proc/pressure/cpu");
        double avg10 = 0.0;
        if (psi && CompanionCoopAdmission::ParseCpuPsiSomeAvg10(psi, avg10))
        {
            snap.hasCpuPsi = true;
            snap.cpuPsiSomeAvg10 = avg10;
        }
    }

    // 4. World-thread tick p99 latency (ms)
    double p99 = 0.0;
    if (gTickRing.Percentile(99.0, p99))
    {
        snap.hasTickP99 = true;
        snap.tickP99Ms = p99;
    }

    // 5. Real active instance count (managed + ordinary/human dungeon/BG/arena instances) & online population
    uint32 dungeons = 0;
    uint32 bgs = 0;
    uint32 arenas = 0;
    sMapMgr->GetNumInstances(dungeons, bgs, arenas);
    snap.hasInstances = true;
    snap.activeInstances = dungeons + bgs + arenas;

    snap.hasPopulation = true;
    snap.onlinePopulation = sWorldSessionMgr->GetPlayerCount();

    CompanionCoopAdmission::AdmissionConfig const cfg = LoadNativeAdmissionConfig();
    std::vector<std::string> reasons;
    {
        std::lock_guard<std::mutex> lock(gAdmissionMutex);
        bool const prevPaused = gAdmissionController.IsHeadroomPaused();
        gAdmissionController.UpdateTelemetry(snap);
        (void)gAdmissionController.EvaluateHeadroom(cfg, snap, nowSec, reasons);
        bool const newPaused = gAdmissionController.IsHeadroomPaused();
        if (newPaused != prevPaused)
        {
            gHysteresisPersistPaused = newPaused;
            gHysteresisPersistTs = nowSec;
            gHysteresisPersistReasons = reasons;
            gHysteresisPersistDirty = true;
        }
    }
    FlushHysteresisPersistenceOnWorldThread();
}

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
uint32 CompanionBrain::_proposalTimer = 0;
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
    // Verified guild work-order delivery (Issue #27): a transferable item crafted for this companion's active
    // claimed work order must be classified BANK so RunCompanionBankDuty deposits it for verified receipt.
    if (Player* const orderBot = botAI->GetBot();
        orderBot && !item->IsSoulBound() && CompanionLife::IsGuildBankTransferable(proto) &&
        CompanionLife::HasPendingDeliveryFor(orderBot->GetGUID().GetCounter(), proto->ItemId))
        return ItemClass::BANK;
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

    // Issue #12: Record world tick duration (ms) and sample host/server telemetry on the world thread.
    gTickRing.Record(elapsed);
    if (CoopEnabled())
    {
        EnsureHysteresisLoaded();
        gTelemetrySampleTimer += elapsed;
        if (gTelemetrySampleTimer >= COOP_TELEMETRY_SAMPLE_MS)
        {
            gTelemetrySampleTimer = 0;
            SampleCoopHostTelemetry(static_cast<uint32>(GameTime::GetGameTime().count()));
        }
        FlushHysteresisPersistenceOnWorldThread();
    }

    FlushCoopGroupMutations();

    _timer += elapsed;
    _proposalTimer += elapsed;
    if (!_weightsLoaded || _timer >= WEIGHT_REFRESH_MS)
    {
        _timer = 0;
        _weightsLoaded = true;
        ReloadWeights();
        ReloadBankRequests();
        _proposalTimer = 0;
        ReloadProposals();
        ReloadCoopMemberships();
        return;
    }
    if (_proposalTimer >= PROPOSAL_REFRESH_MS)
    {
        _proposalTimer = 0;
        ReloadProposals();
        ReloadCoopMemberships();
    }
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

void CompanionBrain::UpsertBankRequest(BankRequest const& req)
{
    std::lock_guard<std::mutex> lock(gBankReqMutex);
    gBankRequests[req.itemEntry] = req;
}

void CompanionBrain::RemoveBankRequest(uint32 itemEntry)
{
    std::lock_guard<std::mutex> lock(gBankReqMutex);
    gBankRequests.erase(itemEntry);
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

    // 1. Load canonical persona & manual baseline weights from companion_weights (never overwritten by AI).
    std::unordered_map<uint32, std::unordered_map<std::string, float>> rows;
    std::unordered_map<uint32, std::unordered_set<std::string>> manualKeys;
    QueryResult result = CharacterDatabase.Query("SELECT `guid`, `key`, `value`, COALESCE(`source`, '') FROM `companion_weights`");
    if (result)
    {
        do
        {
            Field* fields = result->Fetch();
            float const value = fields[2].Get<float>();
            if (!std::isfinite(value))
                continue;
            uint32 const guid = fields[0].Get<uint32>();
            std::string const key = fields[1].Get<std::string>();
            std::string const src = fields[3].Get<std::string>();
            rows[guid][key] = value;
            if (src == "manual")
                manualKeys[guid].insert(key);
        } while (result->NextRow());
    }

    // 2. Layer active, non-expired temporary AI overrides from companion_weight_overrides (Issue #6).
    //    Expired/disabled overrides are ignored automatically -> bot falls back to canonical baseline
    //    within WEIGHT_REFRESH_MS even if Python planner stops. Manual baseline edits take precedence.
    bool const overridesEnabled = sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.Brain.OverridesEnabled", true, false);
    if (overridesEnabled)
    {
        QueryResult ovProbe = CharacterDatabase.Query(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() "
            "AND table_name = 'companion_weight_overrides'");
        if (ovProbe && ovProbe->Fetch()[0].Get<uint64>() > 0)
        {
            QueryResult ovRes = CharacterDatabase.Query(
                "SELECT `guid`, `key`, `value` FROM `companion_weight_overrides` "
                "WHERE `enabled` = 1 AND `expires_at` > UNIX_TIMESTAMP()");
            if (ovRes)
            {
                do
                {
                    Field* f = ovRes->Fetch();
                    float const val = f[2].Get<float>();
                    if (!std::isfinite(val))
                        continue;
                    uint32 const guid = f[0].Get<uint32>();
                    std::string const key = f[1].Get<std::string>();
                    auto mit = manualKeys.find(guid);
                    if (mit != manualKeys.end() && mit->second.count(key) > 0)
                        continue;  // Preserve manual configuration precedence
                    rows[guid][key] = std::clamp(val, WEIGHT_MIN_FACTOR, WEIGHT_MAX_FACTOR);
                } while (ovRes->NextRow());
            }
        }
        else if (!gOverrideMissingLogged)
        {
            gOverrideMissingLogged = true;
            LOG_INFO("playerbots", "Companion weights: companion_weight_overrides table absent, using baseline only");
        }
    }

    std::lock_guard<std::mutex> lock(_weightMutex);
    _weights.swap(rows);
}

void CompanionBrain::ReloadProposals()
{
    QueryResult probe = CharacterDatabase.Query(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() "
        "AND table_name = 'companion_proposals'");
    if (!probe || probe->Fetch()[0].Get<uint64>() == 0)
    {
        if (!gPropMissingLogged)
        {
            gPropMissingLogged = true;
            LOG_INFO("playerbots", "Companion proposals: table companion_proposals absent");
        }
        std::lock_guard<std::mutex> lock(gPropMutex);
        gActiveOrderProps.clear();
        return;
    }

    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    std::unordered_map<uint32, ActiveOrderProp> nextOrders;

    QueryResult res = CharacterDatabase.Query(
        "SELECT `id`, `guid`, `goal_type`, `target_id`, `advisory_only`, `status`, `expires_at`, `snapshot_rev` "
        "FROM `companion_proposals` "
        "WHERE `status` IN ('proposed', 'accepted', 'executing') "
        "ORDER BY `id` DESC LIMIT 80");
    if (res)
    {
        std::unordered_set<uint32> seenGuid;
        do
        {
            Field* f = res->Fetch();
            uint64 const propId = f[0].Get<uint64>();
            uint32 const guid = f[1].Get<uint32>();
            std::string const goalType = f[2].Get<std::string>();
            uint32 const targetId = f[3].Get<uint32>();
            bool const advisory = f[4].Get<uint8>() != 0;
            std::string const status = f[5].Get<std::string>();
            uint32 const expiresAt = f[6].Get<uint32>();
            uint32 const snapshotRev = f[7].Get<uint32>();

            // Skip proposals that were already completed natively before this DB read landed (Issue #27).
            {
                std::lock_guard<std::mutex> lock(gPropMutex);
                if (gCompletedPropIds.Contains(propId))
                    continue;
            }

            // Expire past-TTL proposals natively (even if Python planner is stopped)
            if (expiresAt <= now)
            {
                CharacterDatabase.Execute(
                    "UPDATE companion_proposals SET status = 'expired', outcome_reason = 'ttl_expired', "
                    "ended_at = {} WHERE id = {} AND status IN ('proposed', 'accepted', 'executing')",
                    now, propId);
                continue;
            }
            if (!seenGuid.insert(guid).second)
                continue;  // Only latest active proposal per companion

            if (advisory)
                continue;  // Advisory goals stay soft-weight only, never falsely marked executing

            Player* bot = ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(guid));
            if (bot && bot->GetGroup())
            {
                // Native group/owner commitment ALWAYS takes precedence (Issue #3)
                if (status == "accepted" || status == "executing")
                {
                    CharacterDatabase.Execute(
                        "UPDATE companion_proposals SET status = 'interrupted', outcome_reason = 'owner_group_priority', "
                        "ended_at = {} WHERE id = {} AND status IN ('accepted', 'executing')",
                        now, propId);
                }
                continue;
            }

            if (goalType == "guild_order")
            {
                uint32 itemEntry = 0;
                if (!CompanionLife::HasOpenWorkOrderById(targetId, guid, itemEntry) || itemEntry == 0)
                {
                    CharacterDatabase.Execute(
                        "UPDATE companion_proposals SET status = 'rejected', outcome_reason = 'order_unavailable', "
                        "ended_at = {} WHERE id = {} AND status IN ('proposed', 'accepted', 'executing')",
                        now, propId);
                    continue;
                }
                std::string effectiveStatus = status;
                if (status == "proposed")
                {
                    CharacterDatabase.Execute(
                        "UPDATE companion_proposals SET status = 'executing', outcome_reason = 'native_order_bound', "
                        "started_at = IF(started_at = 0, {}, started_at) "
                        "WHERE id = {} AND status = 'proposed' AND expires_at > {}",
                        now, propId, now);
                    effectiveStatus = "executing";
                }
                nextOrders[guid] = ActiveOrderProp{propId, targetId, itemEntry, snapshotRev, expiresAt, effectiveStatus};
            }
            else if (bot && status == "proposed")
            {
                CharacterDatabase.Execute(
                    "UPDATE companion_proposals SET status = 'accepted', outcome_reason = 'native_weight_bound', "
                    "started_at = IF(started_at = 0, {}, started_at) "
                    "WHERE id = {} AND status = 'proposed' AND expires_at > {}",
                    now, propId, now);
            }
        } while (res->NextRow());
    }

    std::lock_guard<std::mutex> lock(gPropMutex);
    // Filter out any proposal that completed while ReloadProposals was running.
    for (auto it = nextOrders.begin(); it != nextOrders.end();)
    {
        if (gCompletedPropIds.Contains(it->second.proposalId))
            it = nextOrders.erase(it);
        else
            ++it;
    }
    gActiveOrderProps.swap(nextOrders);
}

bool CompanionBrain::ActiveOrderProposal(uint32 lowGuid, uint64& outProposalId, uint64& outOrderId, uint32& outItemEntry,
                                         uint32* outSnapshotRev)
{
    outProposalId = 0;
    outOrderId = 0;
    outItemEntry = 0;
    if (outSnapshotRev)
        *outSnapshotRev = 0;
    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    std::lock_guard<std::mutex> lock(gPropMutex);
    auto it = gActiveOrderProps.find(lowGuid);
    if (it == gActiveOrderProps.end() || it->second.expiresAt <= now)
        return false;
    outProposalId = it->second.proposalId;
    outOrderId = it->second.orderId;
    outItemEntry = it->second.itemEntry;
    if (outSnapshotRev)
        *outSnapshotRev = it->second.snapshotRev;
    return true;
}

bool CompanionBrain::MarkProposalCompleted(uint32 lowGuid, uint64 proposalId, uint32 progressDelta,
                                           uint64 expectedOrderId, uint32 expectedSnapshotRev)
{
    if (!lowGuid || !proposalId || !progressDelta)
        return false;
    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    ActiveOrderProp prop;
    {
        std::lock_guard<std::mutex> lock(gPropMutex);
        auto it = gActiveOrderProps.find(lowGuid);
        if (it == gActiveOrderProps.end() || it->second.proposalId != proposalId)
            return false;
        prop = it->second;
        uint64 const orderIdToCheck = expectedOrderId ? expectedOrderId : prop.orderId;
        CompanionWorkOrderLogic::ProposalState pstate{
            prop.proposalId, lowGuid, prop.orderId, prop.itemEntry, prop.snapshotRev, prop.status, prop.expiresAt};
        if (!CompanionWorkOrderLogic::CanCompleteProposal(pstate, orderIdToCheck, lowGuid, now))
            return false;
        if (expectedSnapshotRev && prop.snapshotRev != expectedSnapshotRev)
            return false;
        gCompletedPropIds.Insert(proposalId);
        gActiveOrderProps.erase(it);
    }

    // Conditional SQL update (Issue #27): expired or interrupted proposals cannot be silently overwritten.
    uint64 const boundOrderId = expectedOrderId ? expectedOrderId : prop.orderId;
    uint32 const boundRev = expectedSnapshotRev ? expectedSnapshotRev : prop.snapshotRev;
    CharacterDatabase.Execute(
        "UPDATE companion_proposals SET status = 'completed', outcome_reason = 'order_delivered', "
        "ended_at = {}, progress_delta = {} "
        "WHERE id = {} AND guid = {} AND target_id = {} AND snapshot_rev = {} "
        "AND status IN ('accepted', 'executing') AND expires_at > {}",
        now, progressDelta, proposalId, lowGuid, boundOrderId, boundRev, now);
    return true;
}

CompanionCoopDrain::CoopMode CompanionBrain::GetCoopMode()
{
    // Issue #22: 3-state admission mode (Off / Draining / Enabled), safe default OFF.
    bool const enabledFlag = sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.Coop.Enabled", false, false);
    std::string const modeRaw = sConfigMgr->GetOption<std::string>("AiPlayerbot.Companion.Coop.Mode", "", false);
    return CompanionCoopDrain::ParseCoopMode(enabledFlag, modeRaw);
}

bool CompanionBrain::CoopEnabled()
{
    // Safe default OFF; returns true ONLY when mode == Enabled (Draining & Off block new admissions).
    return CompanionCoopDrain::AllowsNewAdmissions(GetCoopMode());
}

bool CompanionBrain::IsCoopBootProbeDone()
{
    return gCoopBootProbeDone.load();
}

bool CompanionBrain::RequestCoopOwnerRecall(uint32 botGuid)
{
    if (!botGuid)
        return false;
    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    uint32 const drainGraceSec = sConfigMgr->GetOption<uint32>(
        "AiPlayerbot.Companion.Coop.DrainGraceSeconds", CompanionCoopDrain::DEFAULT_DRAIN_GRACE_SEC, false);
    std::lock_guard<std::mutex> lock(gDrainMutex);
    return gDrainCoordinator.RequestOwnerRecall(botGuid, now, drainGraceSec);
}

void CompanionBrain::ClearCoopOwnerRecall(uint32 botGuid)
{
    if (!botGuid)
        return;
    std::lock_guard<std::mutex> lock(gDrainMutex);
    gDrainCoordinator.ClearOwnerRecall(botGuid);
}

bool CompanionBrain::IsCoopGroupHoldingForDrain(Player const* bot, Group const* group)
{
    if (!bot || !group)
        return false;
    bool hasHuman = false;
    for (Group::MemberSlot const& slot : group->GetMemberSlots())
    {
        if (sCompanionBotMgr.IsAnyOwnerLow(slot.guid.GetCounter()) ||
            !sCompanionBotMgr.IsCompanionSlot(slot.guid, slot.name))
        {
            hasHuman = true;
            break;
        }
    }
    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    CompanionCoopDrain::CoopMode const mode = GetCoopMode();
    std::lock_guard<std::mutex> lock(gDrainMutex);
    return gDrainCoordinator.IsHoldingForDrain(bot->GetGUID().GetCounter(), group->GetGUID().GetCounter(),
                                               GroupTokenFor(group), hasHuman, mode, now);
}

CompanionCoopDrain::DrainDecision CompanionBrain::EvaluateCoopDrain(Player const* bot, Group const* group,
                                                                    bool shiftEnded, bool ownerRecalled)
{
    CompanionCoopDrain::DrainDecision empty;
    if (!bot || !group)
        return empty;

    CompanionCoopDrain::MemberBoundaryInput in;
    in.botGuid = bot->GetGUID().GetCounter();
    in.groupId = group->GetGUID().GetCounter();
    in.groupToken = GroupTokenFor(group);
    in.botInCombat = bot->IsInCombat();
    in.botDead = bot->isDead();
    in.inDungeonInstance = (bot->GetMap() && bot->GetMap()->IsNonRaidDungeon());
    in.isLfgGroup = group->isLFGGroup();
    in.lfgDungeonFinished = (in.isLfgGroup && sLFGMgr->GetState(group->GetGUID()) == lfg::LFG_STATE_FINISHED_DUNGEON);
    in.hasLfgDeserter = bot->HasAura(CompanionCoopDrain::SPELL_LFG_DUNGEON_DESERTER);
    in.hasLfgCooldown = bot->HasAura(CompanionCoopDrain::SPELL_LFG_DUNGEON_COOLDOWN);
    in.shiftEnded = shiftEnded;
    in.ownerRecalled = ownerRecalled;

    // Inspect ALL MemberSlots for human presence (never touch human groups!).
    std::unordered_set<uint32> groupGuids;
    for (Group::MemberSlot const& slot : group->GetMemberSlots())
    {
        uint32 const low = slot.guid.GetCounter();
        if (sCompanionBotMgr.IsAnyOwnerLow(low) || !sCompanionBotMgr.IsCompanionSlot(slot.guid, slot.name))
        {
            in.hasHumanMember = true;
            break;
        }
        groupGuids.insert(low);
    }

    // Check combat ONLY across players on bot's OWN map (owned by current map thread — ZERO cross-map-thread read!).
    in.groupInCombat = in.botInCombat;
    if (!in.hasHumanMember && !in.groupInCombat && bot->GetMap())
    {
        for (auto const& ref : bot->GetMap()->GetPlayers())
        {
            Player const* peer = ref.GetSource();
            if (peer && groupGuids.count(peer->GetGUID().GetCounter()) > 0 && peer->IsInCombat())
            {
                in.groupInCombat = true;
                break;
            }
        }
    }

    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    uint32 const drainGraceSec = sConfigMgr->GetOption<uint32>(
        "AiPlayerbot.Companion.Coop.DrainGraceSeconds", CompanionCoopDrain::DEFAULT_DRAIN_GRACE_SEC, false);
    CompanionCoopDrain::CoopMode const mode = GetCoopMode();

    std::lock_guard<std::mutex> lock(gDrainMutex);
    return gDrainCoordinator.EvaluateMember(mode, in, now, drainGraceSec);
}

void CompanionBrain::MarkCoopExitInitiated(uint64 opportunityId, uint32 botGuid,
                                           CompanionCoopDrain::DrainReason reason)
{
    if (!opportunityId || !botGuid)
        return;
    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    std::lock_guard<std::mutex> lock(gDrainMutex);
    (void)gDrainCoordinator.MarkExitInitiated(opportunityId, botGuid, reason, now);
}

uint64 CompanionBrain::GroupTokenFor(Group const* group)
{
    if (!group)
        return 0;
    std::lock_guard<std::mutex> lock(gGroupTokenMutex);
    auto it = gGroupTokens.find(group);
    if (it != gGroupTokens.end())
        return it->second;
    uint64 const token = gNextGroupToken++;
    gGroupTokens.emplace(group, token);
    return token;
}

uint64 CompanionBrain::OnNativeGroupCreated(Group const* group)
{
    if (!group)
        return 0;
    std::lock_guard<std::mutex> lock(gGroupTokenMutex);
    uint64 const token = gNextGroupToken++;
    gGroupTokens[group] = token;
    return token;
}

bool CompanionBrain::TryAdmitCoopQueue(uint64 opportunityId, uint32 targetMapId, bool allMembersEnrolled,
                                       uint32 additionalLoginsNeeded, std::string* outReason)
{
    if (outReason)
        outReason->clear();

    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    std::unordered_set<uint64_t> committedOppIds;
    std::unordered_set<uint64_t> assemblingOppIds;
    {
        std::lock_guard<std::mutex> lock(gCoopMutex);
        for (auto const& r : gCoopRegistry.Runs())
        {
            if (r.isShadow || !CompanionCoopAuth::IsActiveRunState(r.runState) || r.expiresAt <= now)
                continue;
            if (r.boundToNativeGroup)
                committedOppIds.insert(r.opportunityId);
            else
                assemblingOppIds.insert(r.opportunityId);
        }
    }

    CompanionCoopAdmission::AdmissionConfig const cfg = LoadNativeAdmissionConfig();
    CompanionCoopAdmission::AdmissionDecision dec;
    {
        std::lock_guard<std::mutex> lock(gAdmissionMutex);
        bool const prevPaused = gAdmissionController.IsHeadroomPaused();
        dec = gAdmissionController.TryAdmitNewQueueOrProposal(
            cfg, gAdmissionController.LastTelemetry(), opportunityId, targetMapId, committedOppIds, assemblingOppIds,
            allMembersEnrolled, additionalLoginsNeeded, now);
        bool const newPaused = gAdmissionController.IsHeadroomPaused();
        if (newPaused != prevPaused)
        {
            // Queue persistence to world thread ONLY (single writer, zero interleaved temp files!).
            gHysteresisPersistPaused = newPaused;
            gHysteresisPersistTs = now;
            gHysteresisPersistReasons = dec.detailReasons;
            gHysteresisPersistDirty = true;
        }
    }
    if (outReason)
        *outReason = dec.reason;
    return dec.admitted;
}

namespace
{
bool ResolveBotCoopRunForAdmission(uint32 botGuid, uint32 nowSec, uint64& outOppId, uint32& outTargetMap,
                                   bool& outAllEnrolled, uint32& outOfflineCount)
{
    outOppId = 0;
    outTargetMap = 0;
    outAllEnrolled = false;
    outOfflineCount = 0;
    std::vector<uint32> rosterGuids;
    {
        std::lock_guard<std::mutex> lock(gCoopMutex);
        for (auto const& r : gCoopRegistry.Runs())
        {
            if (r.isShadow || !CompanionCoopAuth::IsActiveRunState(r.runState) || r.expiresAt <= nowSec)
                continue;
            bool foundSelf = false;
            bool allEnr = !r.roster.empty();
            for (auto const& rm : r.roster)
            {
                if (rm.characterGuid == botGuid)
                    foundSelf = true;
                if (!rm.enrolledCoop || !CompanionCoopAuth::IsConfirmedInterestState(rm.interestState) ||
                    rm.interestRevision != r.revision)
                    allEnr = false;
            }
            if (foundSelf)
            {
                outOppId = r.opportunityId;
                outTargetMap = r.targetMapId;
                outAllEnrolled = allEnr;
                for (auto const& rm : r.roster)
                    rosterGuids.push_back(rm.characterGuid);
                break;
            }
        }
    }
    if (!outOppId)
        return false;
    // Count offline roster members outside gCoopMutex (prevents forced mass logins for coop admission!).
    for (uint32 g : rosterGuids)
    {
        if (!ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(g)))
            ++outOfflineCount;
    }
    return true;
}
}  // namespace

bool CompanionBrain::TryAdmitCompanionLfgJoin(Player const* bot, std::vector<uint32> const& targetMapIds,
                                              std::string* outReason)
{
    if (outReason)
        outReason->clear();
    if (!bot)
    {
        if (outReason)
            *outReason = "invalid_bot";
        return false;
    }
    // Issue #22: Reject new LFG queue admission while native LFG Deserter or Cooldown penalty aura is active!
    if (bot->HasAura(CompanionCoopDrain::SPELL_LFG_DUNGEON_DESERTER))
    {
        if (outReason)
            *outReason = "lfg_deserter_active";
        return false;
    }
    if (bot->HasAura(CompanionCoopDrain::SPELL_LFG_DUNGEON_COOLDOWN))
    {
        if (outReason)
            *outReason = "lfg_cooldown_active";
        return false;
    }
    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    uint64 oppId = 0;
    uint32 runMap = 0;
    bool allEnrolled = false;
    uint32 offlineCount = 0;
    if (!ResolveBotCoopRunForAdmission(bot->GetGUID().GetCounter(), now, oppId, runMap, allEnrolled, offlineCount))
    {
        if (outReason)
            *outReason = CoopEnabled() ? "no_authorized_opportunity" : "coop_disabled";
        return false;
    }
    if (targetMapIds.empty() || runMap == 0)
    {
        if (outReason)
            *outReason = "empty_or_unknown_target_map";
        return false;
    }
    // Validate EVERY dungeon in the LFG join list (never allow extra or random dungeons to slip past!)
    for (uint32 mId : targetMapIds)
    {
        if (mId == 0 || mId != runMap)
        {
            if (outReason)
                *outReason = "map_mismatch";
            return false;
        }
    }
    return TryAdmitCoopQueue(oppId, runMap, allEnrolled, offlineCount, outReason);
}

bool CompanionBrain::TryAdmitCompanionLfgProposal(Player const* bot, uint32 /*proposalId*/, uint32 rawLfgDungeonEntry,
                                                  std::string* outReason)
{
    if (outReason)
        outReason->clear();
    if (!bot)
    {
        if (outReason)
            *outReason = "invalid_bot";
        return false;
    }
    // Issue #22: Reject new LFG queue admission while native LFG Deserter or Cooldown penalty aura is active!
    if (bot->HasAura(CompanionCoopDrain::SPELL_LFG_DUNGEON_DESERTER))
    {
        if (outReason)
            *outReason = "lfg_deserter_active";
        return false;
    }
    if (bot->HasAura(CompanionCoopDrain::SPELL_LFG_DUNGEON_COOLDOWN))
    {
        if (outReason)
            *outReason = "lfg_cooldown_active";
        return false;
    }
    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    uint64 oppId = 0;
    uint32 runMap = 0;
    bool allEnrolled = false;
    uint32 offlineCount = 0;
    if (!ResolveBotCoopRunForAdmission(bot->GetGUID().GetCounter(), now, oppId, runMap, allEnrolled, offlineCount))
    {
        if (outReason)
            *outReason = CoopEnabled() ? "no_authorized_opportunity" : "coop_disabled";
        return false;
    }
    // SMSG_LFG_PROPOSAL_UPDATE encodes dungeon as (id | type << 24); mask low 24 bits before DBC lookup!
    uint32 const dungeonId = rawLfgDungeonEntry & 0x00FFFFFFu;
    if (dungeonId == 0)
    {
        if (outReason)
            *outReason = "unknown_proposal_dungeon";
        return false;
    }
    LFGDungeonEntry const* entry = sLFGDungeonStore.LookupEntry(dungeonId);
    if (!entry || entry->TypeID == 6 /* LFG_TYPE_RANDOM */ || entry->MapID <= 0)
    {
        if (outReason)
            *outReason = "unsupported_or_random_proposal_dungeon";
        return false;
    }
    uint32 const propMap = static_cast<uint32>(entry->MapID);
    if (runMap == 0 || propMap != runMap)
    {
        if (outReason)
            *outReason = "proposal_map_mismatch";
        return false;
    }
    return TryAdmitCoopQueue(oppId, runMap, allEnrolled, offlineCount, outReason);
}

void CompanionBrain::ReleaseCoopAdmissionToken(uint64 opportunityId)
{
    if (!opportunityId)
        return;
    std::lock_guard<std::mutex> lock(gAdmissionMutex);
    (void)gAdmissionController.ReleaseToken(opportunityId);
}

bool CompanionBrain::CoopHeadroomPaused()
{
    std::lock_guard<std::mutex> lock(gAdmissionMutex);
    return gAdmissionController.IsHeadroomPaused();
}

void CompanionBrain::OnNativeGroupMemberRemoved(Group const* group, uint32 memberLowGuid)
{
    if (!group || !memberLowGuid)
        return;
    uint32 const groupId = group->GetGUID().GetCounter();
    uint64 const token = GroupTokenFor(group);
    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    uint32 const drainGraceSec = sConfigMgr->GetOption<uint32>(
        "AiPlayerbot.Companion.Coop.DrainGraceSeconds", CompanionCoopDrain::DEFAULT_DRAIN_GRACE_SEC, false);

    uint64 oppId = 0;
    CompanionCoopDrain::DrainReason reason = CompanionCoopDrain::DrainReason::None;
    bool groupFullyExited = false;
    bool wasDrain = false;
    bool wasOwned = false;
    {
        std::lock_guard<std::mutex> lock(gDrainMutex);
        wasOwned = gDrainCoordinator.OnActualMemberRemoved(groupId, token, memberLowGuid, now, drainGraceSec, oppId,
                                                           reason, groupFullyExited, wasDrain);
    }
    if (!wasOwned || !oppId)
        return;
    if (groupFullyExited)
        ReleaseCoopAdmissionToken(oppId);
    // Only record a 'drained' DB transition when this exit was actually a drain (mode/expiry/recall/shift/outage);
    // normal post-completion leaves leave the opportunity to its normal completion/disband path!
    if (!wasDrain)
        return;

    std::lock_guard<std::mutex> lock(gCoopQueueMutex);
    PendingCoopExit ex;
    ex.opportunityId = oppId;
    ex.exitedMemberGuids.push_back(memberLowGuid);
    ex.reason = reason;
    ex.groupFullyExited = groupFullyExited;
    gPendingCoopExits.push_back(std::move(ex));
    if (groupFullyExited)
        gPendingDrainedOpps.insert(oppId);
}

void CompanionBrain::OnNativeGroupDisbanded(Group const* group)
{
    if (!group)
        return;
    uint32 const groupId = group->GetGUID().GetCounter();
    uint64 token = 0;
    {
        std::lock_guard<std::mutex> lock(gGroupTokenMutex);
        auto it = gGroupTokens.find(group);
        if (it != gGroupTokens.end())
        {
            token = it->second;
            gGroupTokens.erase(it);
        }
    }
    if (!groupId)
        return;

    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    std::vector<CompanionCoopDrain::DisbandDrainResult> drainResults;
    {
        std::lock_guard<std::mutex> lock(gDrainMutex);
        drainResults = gDrainCoordinator.OnGroupDisbanded(groupId, token);
    }
    std::unordered_set<uint64> drainedOppIds;
    for (auto const& dr : drainResults)
    {
        ReleaseCoopAdmissionToken(dr.opportunityId);
        if (dr.wasDraining)
            drainedOppIds.insert(dr.opportunityId);
    }

    // Also include opportunities just drained by OnRemoveMember (e.g. LFG auto-disband right after last member left!)
    {
        std::lock_guard<std::mutex> lock(gCoopQueueMutex);
        for (uint64 oid : gPendingDrainedOpps)
            drainedOppIds.insert(oid);
    }

    if (!CoopEnabled() && drainResults.empty() && drainedOppIds.empty())
        return;

    std::vector<CompanionCoopAuth::Registry::DisbandTransition> transitions;
    {
        std::lock_guard<std::mutex> lock(gCoopMutex);
        (void)gCoopRegistry.OnGroupDisbanded(groupId, token, now, &transitions);
    }

    // Filter out opportunities already closed neutrally as 'drained' so disband cleanup NEVER overwrites them
    // as 'aborted' (covers BOTH 2nd-to-last-member auto-disband AND LFG last-member OnRemoveMember -> Disband!).
    bool hadDrainedOpp = false;
    std::vector<CompanionCoopAuth::Registry::DisbandTransition> nonDrainTransitions;
    for (auto const& tr : transitions)
    {
        if (drainedOppIds.count(tr.opportunityId) > 0)
            hadDrainedOpp = true;
        else
            nonDrainTransitions.push_back(tr);
    }
    if (!drainResults.empty())
    {
        for (auto const& dr : drainResults)
            if (dr.wasDraining)
                hadDrainedOpp = true;
    }

    std::lock_guard<std::mutex> lock(gCoopQueueMutex);
    for (auto const& dr : drainResults)
    {
        if (!dr.wasDraining)
            continue;
        PendingCoopExit ex;
        ex.opportunityId = dr.opportunityId;
        ex.exitedMemberGuids = dr.remainingMembers;
        ex.reason = dr.reason;
        ex.groupFullyExited = true;
        gPendingCoopExits.push_back(std::move(ex));
        gPendingDrainedOpps.insert(dr.opportunityId);
    }
    bool removedPending = false;
    for (auto it = gPendingCoopBinds.begin(); it != gPendingCoopBinds.end();)
    {
        if (it->second.snap.groupId == groupId)
        {
            it = gPendingCoopBinds.erase(it);
            removedPending = true;
        }
        else
            ++it;
    }
    if (!nonDrainTransitions.empty() || removedPending)
        gPendingCoopDisbands.push_back(PendingCoopDisband{groupId, hadDrainedOpp, std::move(nonDrainTransitions)});
}

void CompanionBrain::FlushCoopGroupMutations()
{
    CompanionCoopDrain::CoopMode const mode = GetCoopMode();
    std::vector<PendingCoopDisband> disbands;
    std::vector<PendingCoopBind> binds;
    std::vector<PendingCoopExit> exits;
    {
        std::lock_guard<std::mutex> lock(gCoopQueueMutex);
        // When mode != Enabled (Draining or Off), cancel all pending uncommitted binds immediately while
        // STILL retaining disbands and safe-boundary member exit commitment releases!
        if (!CompanionCoopDrain::AllowsNewAdmissions(mode))
        {
            gPendingCoopBinds.clear();
            gFailedBindCooldown.clear();
        }
        if (gPendingCoopDisbands.empty() && gPendingCoopBinds.empty() && gPendingCoopExits.empty())
            return;
        disbands.swap(gPendingCoopDisbands);
        exits.swap(gPendingCoopExits);
        binds.reserve(gPendingCoopBinds.size());
        for (auto& [oppId, req] : gPendingCoopBinds)
            binds.push_back(std::move(req));
        gPendingCoopBinds.clear();
    }

    // Issue #22: Defer exits, disbands AND binds while DB is unhealthy so no writes are lost during outages
    // and disbands can never race ahead of deferred drained exits!
    auto restoreDeferred = [&](std::vector<PendingCoopExit>& remExits)
    {
        std::lock_guard<std::mutex> lock(gCoopQueueMutex);
        if (!remExits.empty())
            gPendingCoopExits.insert(gPendingCoopExits.begin(), std::make_move_iterator(remExits.begin()),
                                     std::make_move_iterator(remExits.end()));
        if (!disbands.empty())
            gPendingCoopDisbands.insert(gPendingCoopDisbands.begin(), std::make_move_iterator(disbands.begin()),
                                        std::make_move_iterator(disbands.end()));
        if (CompanionCoopDrain::AllowsNewAdmissions(mode))
        {
            for (auto& b : binds)
                if (gPendingCoopBinds.find(b.opportunityId) == gPendingCoopBinds.end())
                    gPendingCoopBinds.emplace(b.opportunityId, std::move(b));
        }
    };

    if (!gCoopDbHealthy.load())
    {
        restoreDeferred(exits);
        return;
    }

    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    uint32 const graceSec = sConfigMgr->GetOption<uint32>("AiPlayerbot.Companion.Coop.ExitGraceMinutes", 10, false) * 60;

    // Issue #22: Flush BOTH partial and full member exits BEFORE disbands and verify DB persistence!
    // Distinguish real DB outage (`SELECT 1` fails) from missing/poisoned row (bounded COOP_EXIT_MAX_RETRIES) so
    // a single deleted/unconfirmable row can NEVER stall all subsequent disbands indefinitely!
    std::vector<PendingCoopExit> retryExits;
    std::unordered_set<uint64> confirmedDrainedOpps;
    bool dbFailedDuringExits = false;
    for (PendingCoopExit& ex : exits)
    {
        char const* tag = CompanionCoopDrain::DrainOutcomeTag(ex.reason);
        if (!tag || !*tag)
            tag = "coop_drained";
        if (ex.groupFullyExited)
        {
            // Release ALL remaining active/accepted/reserved member interest rows for this opportunity (also catches
            // any earlier partial exit whose interest row missed!).
            CharacterDatabase.DirectExecute(Acore::StringFormat(
                "UPDATE companion_opportunity_interests SET state = 'released', reason_code = '{}', "
                "reserved_until = 0, updated_at = {} "
                "WHERE opportunity_id = {} AND state IN ('reserved', 'accepted', 'active')",
                tag, now, ex.opportunityId));
            CharacterDatabase.DirectExecute(Acore::StringFormat(
                "UPDATE companion_opportunities SET state = 'drained', group_guid = 0, outcome_reason = '{}' "
                "WHERE id = {} AND is_shadow = 0 AND state IN ('assembling', 'active')",
                tag, ex.opportunityId));
            QueryResult confirm = CharacterDatabase.Query(Acore::StringFormat(
                "SELECT state FROM companion_opportunities WHERE id = {}", ex.opportunityId));
            bool persisted = false;
            if (confirm)
            {
                std::string const st = confirm->Fetch()[0].Get<std::string>();
                persisted = !CompanionCoopAuth::IsActiveRunState(st);
            }
            else if (CharacterDatabase.Query("SELECT 1"))
            {
                // DB is healthy, row simply does not exist (deleted/pruned): treat as closed, never wedge queue!
                persisted = true;
            }
            else
            {
                dbFailedDuringExits = true;
            }
            if (!persisted)
            {
                if (dbFailedDuringExits)
                {
                    gCoopDbHealthy.store(false);
                    retryExits.push_back(std::move(ex));
                }
                else if (ex.retryCount < COOP_EXIT_MAX_RETRIES)
                {
                    ++ex.retryCount;
                    retryExits.push_back(std::move(ex));
                }
                else
                {
                    LOG_WARN("playerbots",
                             "[CompanionCoop] Dropping unconfirmable drain exit for opp #{} after {} retries",
                             ex.opportunityId, COOP_EXIT_MAX_RETRIES);
                }
            }
            else
            {
                confirmedDrainedOpps.insert(ex.opportunityId);
                LOG_INFO("playerbots", "[CompanionCoop] Opportunity #{} drained cleanly on actual exit ({})",
                         ex.opportunityId, tag);
            }
        }
        else if (!ex.exitedMemberGuids.empty())
        {
            std::ostringstream inList;
            for (size_t i = 0; i < ex.exitedMemberGuids.size(); ++i)
            {
                if (i > 0)
                    inList << ",";
                inList << ex.exitedMemberGuids[i];
                CharacterDatabase.DirectExecute(Acore::StringFormat(
                    "UPDATE companion_opportunity_interests SET state = 'released', reason_code = '{}', "
                    "reserved_until = 0, updated_at = {} "
                    "WHERE opportunity_id = {} AND character_guid = {} AND state IN ('reserved', 'accepted', 'active')",
                    tag, now, ex.opportunityId, ex.exitedMemberGuids[i]));
            }
            // Verify partial member exit persisted (if DB failed mid-flush, re-queue for retry!).
            QueryResult pConfirm = CharacterDatabase.Query(Acore::StringFormat(
                "SELECT COUNT(*) FROM companion_opportunity_interests "
                "WHERE opportunity_id = {} AND character_guid IN ({}) AND state IN ('reserved', 'accepted', 'active')",
                ex.opportunityId, inList.str()));
            if (!pConfirm)
            {
                dbFailedDuringExits = true;
                gCoopDbHealthy.store(false);
                retryExits.push_back(std::move(ex));
            }
            else if (pConfirm->Fetch()[0].Get<uint64>() > 0 && ex.retryCount < COOP_EXIT_MAX_RETRIES)
            {
                ++ex.retryCount;
                retryExits.push_back(std::move(ex));
            }
        }
    }
    if (!retryExits.empty())
    {
        if (dbFailedDuringExits)
        {
            // Real DB outage: defer disbands AND binds too so nothing is dropped or raced!
            restoreDeferred(retryExits);
            return;
        }
        // DB is healthy (only an isolated row failed confirmation): re-queue ONLY retryExits without blocking
        // unrelated disbands or binds!
        std::lock_guard<std::mutex> lock(gCoopQueueMutex);
        gPendingCoopExits.insert(gPendingCoopExits.begin(), std::make_move_iterator(retryExits.begin()),
                                 std::make_move_iterator(retryExits.end()));
    }

    std::unordered_set<uint64> drainedGuard;
    {
        std::lock_guard<std::mutex> lock(gCoopQueueMutex);
        drainedGuard = gPendingDrainedOpps;
    }
    for (uint64 oid : confirmedDrainedOpps)
        drainedGuard.insert(oid);

    for (PendingCoopDisband const& d : disbands)
    {
        if (!d.groupId)
            continue;
        bool skippedAnyDrained = d.hadDrainedOpp;
        for (auto const& tr : d.transitions)
        {
            if (drainedGuard.count(tr.opportunityId) > 0)
            {
                skippedAnyDrained = true;
                continue;
            }
            if (tr.aborted)
            {
                ReleaseCoopAdmissionToken(tr.opportunityId);
                CharacterDatabase.DirectExecute(Acore::StringFormat(
                    "UPDATE companion_opportunities SET group_guid = 0, state = 'aborted', "
                    "outcome_reason = 'native_group_disbanded' "
                    "WHERE id = {} AND group_guid = {} AND is_shadow = 0 AND state IN ('assembling', 'active')",
                    tr.opportunityId, d.groupId));
            }
            else
            {
                // Pre-made group disbanded prior to dungeon entry (e.g. transitioning to native LFG group):
                // revert to 'assembling' with group_guid = 0 (bounded by LFG_REBIND_GRACE_SEC in registry).
                CharacterDatabase.DirectExecute(Acore::StringFormat(
                    "UPDATE companion_opportunities SET group_guid = 0, state = 'assembling' "
                    "WHERE id = {} AND group_guid = {} AND is_shadow = 0 AND state IN ('assembling', 'active')",
                    tr.opportunityId, d.groupId));
            }
        }
        // Fallback sweep for any remaining row still referencing this disbanded group_guid (no zombie active+0!),
        // skipped when this group belonged to a drained opportunity so drained rows are NEVER overwritten as aborted!
        if (!skippedAnyDrained)
        {
            CharacterDatabase.DirectExecute(Acore::StringFormat(
                "UPDATE companion_opportunities SET group_guid = 0, state = 'aborted', "
                "outcome_reason = 'native_group_disbanded' "
                "WHERE group_guid = {} AND is_shadow = 0 AND state IN ('assembling', 'active')",
                d.groupId));
        }
        LOG_INFO("playerbots", "[CompanionCoop] Cleared DB group_guid {} after native group disband", d.groupId);
    }

    for (PendingCoopBind const& req : binds)
    {
        if (!req.opportunityId || !req.snap.groupId)
            continue;

        // Verify the native Group still exists with the EXACT same monotonic lifecycle token before touching DB!
        Group const* liveGrp = sGroupMgr->GetGroupByGUID(req.snap.groupId);
        if (!liveGrp || GroupTokenFor(liveGrp) != req.snap.groupToken)
        {
            LOG_INFO("playerbots",
                     "[CompanionCoop] Dropped stale queued bind for opp #{} (group {} already disbanded)",
                     req.opportunityId, req.snap.groupId);
            continue;
        }

        // Issue #12: Enforce native admission flags, dungeon allowlist, enrollment, no forced mass logins,
        // fail-closed headroom + hysteresis, and atomic group/instance/population caps BEFORE committing a new
        // managed group! Already-committed groups pass through safely without disruption.
        uint32 runTargetMap = 0;
        bool runAllEnrolled = false;
        bool alreadyCommitted = false;
        std::vector<uint32> rosterGuids;
        {
            std::lock_guard<std::mutex> lock(gCoopMutex);
            for (auto const& r : gCoopRegistry.Runs())
            {
                if (r.opportunityId != req.opportunityId || r.revision != req.revision)
                    continue;
                runTargetMap = r.targetMapId;
                alreadyCommitted = r.boundToNativeGroup;
                runAllEnrolled = !r.roster.empty();
                for (auto const& rm : r.roster)
                {
                    rosterGuids.push_back(rm.characterGuid);
                    if (!rm.enrolledCoop || !CompanionCoopAuth::IsConfirmedInterestState(rm.interestState) ||
                        rm.interestRevision != r.revision)
                        runAllEnrolled = false;
                }
                break;
            }
        }
        uint32 offlineRosterCount = 0;
        for (uint32 g : rosterGuids)
        {
            if (!ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(g)))
                ++offlineRosterCount;
        }
        bool mintedNewToken = false;
        if (!alreadyCommitted)
        {
            std::string admitReason;
            if (!TryAdmitCoopQueue(req.opportunityId, runTargetMap, runAllEnrolled, offlineRosterCount, &admitReason))
            {
                std::lock_guard<std::mutex> lock(gCoopQueueMutex);
                gFailedBindCooldown[req.opportunityId] = now + COOP_BIND_FAIL_BACKOFF_SEC;
                gAdmissionBlockedOpps[req.opportunityId] = now + COOP_BIND_FAIL_BACKOFF_SEC;
                LOG_WARN("playerbots",
                         "[CompanionCoop] Admission gate blocked opp #{} (map {}, group {}): {}",
                         req.opportunityId, runTargetMap, req.snap.groupId, admitReason);
                continue;
            }
            mintedNewToken = true;
        }

        std::string const groupGuard =
            req.isLfgRebind && req.previousGroupId != 0
                ? Acore::StringFormat("(group_guid = 0 OR group_guid = {} OR group_guid = {})", req.previousGroupId,
                                      req.snap.groupId)
                : Acore::StringFormat("(group_guid = 0 OR group_guid = {})", req.snap.groupId);

        // Single atomic guarded UPDATE (Issue #11): checks revision, kind, non-shadow, non-expired, group_guid
        // ownership, AND verifies zero OTHER non-expired active/assembling opportunities own this group_guid.
        CharacterDatabase.DirectExecute(Acore::StringFormat(
            "UPDATE companion_opportunities SET group_guid = {}, state = 'active' "
            "WHERE id = {} AND revision = {} AND is_shadow = 0 AND kind = 'dungeon_run' "
            "AND state IN ('assembling', 'active') "
            "AND (recruit_until + estimated_minutes * 60 + {}) > {} "
            "AND {} "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM ("
            "    SELECT id FROM companion_opportunities "
            "    WHERE group_guid = {} AND id <> {} AND is_shadow = 0 "
            "    AND state IN ('assembling', 'active') "
            "    AND (recruit_until + estimated_minutes * 60 + {}) > {}"
            "  ) AS active_conflict"
            ")",
            req.snap.groupId, req.opportunityId, req.revision, graceSec, now, groupGuard, req.snap.groupId,
            req.opportunityId, graceSec, now));

        QueryResult confirm = CharacterDatabase.Query(Acore::StringFormat(
            "SELECT group_guid, state, revision FROM companion_opportunities WHERE id = {}", req.opportunityId));
        bool dbConfirmed = false;
        std::string dbState;
        if (confirm)
        {
            Field* cf = confirm->Fetch();
            uint32 const dbGroupId = cf[0].Get<uint32>();
            dbState = cf[1].Get<std::string>();
            uint32 const dbRev = cf[2].Get<uint32>();
            dbConfirmed =
                (dbGroupId == req.snap.groupId && dbRev == req.revision && CompanionCoopAuth::IsActiveRunState(dbState));
        }

        if (!dbConfirmed)
        {
            if (mintedNewToken)
                ReleaseCoopAdmissionToken(req.opportunityId);
            std::lock_guard<std::mutex> lock(gCoopQueueMutex);
            gFailedBindCooldown[req.opportunityId] = now + COOP_BIND_FAIL_BACKOFF_SEC;
            LOG_WARN("playerbots",
                     "[CompanionCoop] Atomic DB bind rejected for opportunity #{} (rev {}, group {})",
                     req.opportunityId, req.revision, req.snap.groupId);
            continue;
        }

        std::string bindReason;
        bool boundOk = false;
        {
            std::lock_guard<std::mutex> lock(gCoopMutex);
            boundOk = gCoopRegistry.BindNativeGroup(req.opportunityId, req.revision, req.snap, req.instanceId, now,
                                                    dbState == "active", req.isLfgRebind, &bindReason);
        }
        if (boundOk)
        {
            {
                std::lock_guard<std::mutex> lock(gAdmissionMutex);
                (void)gAdmissionController.MarkCommitted(req.opportunityId);
            }
            {
                // Register accepted native group ownership in DrainCoordinator (separated from admission mode!).
                std::vector<uint32_t> memGuids;
                memGuids.reserve(req.snap.members.size());
                for (auto const& nm : req.snap.members)
                    memGuids.push_back(nm.guid);
                uint32 runExpiresAt = 0;
                {
                    std::lock_guard<std::mutex> lock(gCoopMutex);
                    for (auto const& r : gCoopRegistry.Runs())
                    {
                        if (r.opportunityId == req.opportunityId)
                        {
                            runExpiresAt = r.expiresAt;
                            break;
                        }
                    }
                }
                std::lock_guard<std::mutex> dlock(gDrainMutex);
                gDrainCoordinator.RegisterCommittedGroup(req.opportunityId, req.revision, req.snap.groupId,
                                                         req.snap.groupToken, runTargetMap, req.snap.isLfg, memGuids,
                                                         now, runExpiresAt);
            }
            std::lock_guard<std::mutex> lock(gCoopQueueMutex);
            gFailedBindCooldown.erase(req.opportunityId);
            LOG_INFO("playerbots",
                     "[CompanionCoop] {} opportunity #{} (rev {}) to native {}group {} ({} members)",
                     req.isLfgRebind ? "Rebound" : "Bound", req.opportunityId, req.revision,
                     req.snap.isLfg ? "LFG " : "", req.snap.groupId, req.snap.members.size());
        }
        else
        {
            if (mintedNewToken)
                ReleaseCoopAdmissionToken(req.opportunityId);
            // Roll back DB group_guid so a rejected registry bind never leaves an orphaned group_guid in MySQL!
            CharacterDatabase.DirectExecute(Acore::StringFormat(
                "UPDATE companion_opportunities SET group_guid = {}, state = 'assembling' "
                "WHERE id = {} AND revision = {} AND group_guid = {}",
                req.previousGroupId, req.opportunityId, req.revision, req.snap.groupId));
            std::lock_guard<std::mutex> lock(gCoopQueueMutex);
            gFailedBindCooldown[req.opportunityId] = now + COOP_BIND_FAIL_BACKOFF_SEC;
            LOG_WARN("playerbots",
                     "[CompanionCoop] Registry bind rejected after DB update for opp #{} ({}); rolled back DB",
                     req.opportunityId, bindReason);
        }
    }
}

void CompanionBrain::ReloadCoopMemberships()
{
    CompanionCoopDrain::CoopMode const mode = GetCoopMode();
    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    uint32 const drainGraceSec = sConfigMgr->GetOption<uint32>(
        "AiPlayerbot.Companion.Coop.DrainGraceSeconds", CompanionCoopDrain::DEFAULT_DRAIN_GRACE_SEC, false);
    uint32 const outageGraceSec = sConfigMgr->GetOption<uint32>(
        "AiPlayerbot.Companion.Coop.OutageGraceSeconds", CompanionCoopDrain::DEFAULT_OUTAGE_GRACE_SEC, false);

    if (!CompanionCoopDrain::AllowsNewAdmissions(mode))
    {
        // Issue #22: Toggle OFF or Draining MUST NOT wipe already-committed groups mid-pull!
        // On startup (gCoopBootProbeDone == false), ALWAYS probe DB once even in Off mode so pre-existing
        // committed groups survive a restart and drain safely instead of being disbanded as strays!
        bool hasOwned = false;
        {
            std::lock_guard<std::mutex> lock(gDrainMutex);
            gDrainCoordinator.TriggerGlobalDrain(mode, now, drainGraceSec);
            hasOwned = gDrainCoordinator.HasOwnedGroups();
        }
        bool hasPendingWrites = false;
        {
            std::lock_guard<std::mutex> lock(gCoopQueueMutex);
            gPendingCoopBinds.clear();
            gFailedBindCooldown.clear();
            hasPendingWrites =
                (!gPendingCoopExits.empty() || !gPendingCoopDisbands.empty() || !gPendingDrainedOpps.empty());
        }
        if (mode == CompanionCoopDrain::CoopMode::Off && !hasOwned && !hasPendingWrites && gCoopBootProbeDone.load())
        {
            std::lock_guard<std::mutex> lock(gCoopMutex);
            if (!gCoopRegistry.Runs().empty())
                gCoopRegistry.Clear();
            return;
        }
    }

    // Issue #22 (P1-2): ALWAYS probe DB health BEFORE flushing mutations so exit/disband writes are NEVER
    // attempted into a just-failed DB and lost!
    QueryResult probe = CharacterDatabase.Query(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() "
        "AND table_name IN ('companion_opportunities', 'companion_opportunity_interests', "
        "'companion_personas', 'companion_persona_characters')");
    if (!probe)
    {
        // Real DB outage (COUNT(*) returned nullptr)! NEVER clear committed group ownership mid-pull;
        // mark DB unhealthy so exit/disband writes stay queued until recovery, and drain at safe boundary past grace.
        gCoopDbHealthy.store(false);
        std::lock_guard<std::mutex> lock(gDrainMutex);
        gDrainCoordinator.NoteDbOutage(now, outageGraceSec, drainGraceSec);
        LOG_WARN("playerbots", "[CompanionCoop] DB probe failed (transient outage); preserving committed groups");
        return;
    }
    gCoopBootProbeDone.store(true);
    gCoopDbHealthy.store(true);
    if (probe->Fetch()[0].Get<uint64>() < 4)
    {
        if (!gCoopSchemaMissingLogged)
        {
            gCoopSchemaMissingLogged = true;
            LOG_WARN("playerbots",
                     "[CompanionCoop] Cooperative authorization disabled: required Phase II tables missing");
        }
        bool hasOwned = false;
        {
            std::lock_guard<std::mutex> lock(gDrainMutex);
            hasOwned = gDrainCoordinator.HasOwnedGroups();
        }
        if (!hasOwned)
        {
            std::lock_guard<std::mutex> lock(gCoopMutex);
            gCoopRegistry.Clear();
        }
        return;
    }

    {
        std::lock_guard<std::mutex> lock(gDrainMutex);
        gDrainCoordinator.NoteDbHealthy();
    }
    // DB is confirmed healthy right now — flush pending exits, disbands (and binds if Enabled) before reading DB!
    FlushCoopGroupMutations();

    uint64 startGen = 0;
    {
        std::lock_guard<std::mutex> lock(gCoopMutex);
        startGen = gCoopRegistry.BindGeneration();
    }

    uint32 const graceSec = sConfigMgr->GetOption<uint32>("AiPlayerbot.Companion.Coop.ExitGraceMinutes", 10, false) * 60;
    uint32 const assemblyGraceSec = sConfigMgr->GetOption<uint32>(
        "AiPlayerbot.Companion.Coop.AssemblyGraceSeconds", CompanionCoopAuth::DEFAULT_ASSEMBLY_GRACE_SEC, false);

    // Issue #11 & #22: When mode != Enabled (Draining/Off), load ONLY already-bound active runs (for safe drain /
    // restart resilience) and NEVER load new assembling runs!
    std::string const stateFilter =
        CompanionCoopDrain::AllowsNewAdmissions(mode)
            ? "(state = 'assembling' OR (state = 'active' AND group_guid > 0))"
            : "(state = 'active' AND group_guid > 0)";

    std::unordered_map<uint64, CompanionCoopAuth::AuthorizedRun> runsByOpp;
    QueryResult res = CharacterDatabase.Query(Acore::StringFormat(
        "SELECT o.id, o.revision, o.dungeon_map_id, o.difficulty, o.group_guid, o.state, "
        "o.recruit_until, o.estimated_minutes, o.is_shadow, o.roles_json, "
        "i.character_guid, i.role, i.state, i.opportunity_revision, "
        "p.enrolled_coop, p.state, "
        "EXISTS(SELECT 1 FROM companion_persona_characters pc "
        "  WHERE pc.persona_id = i.persona_id AND pc.character_guid = i.character_guid AND pc.active = 1) "
        "FROM companion_opportunities o "
        "JOIN ("
        "  SELECT id FROM companion_opportunities "
        "  WHERE is_shadow = 0 AND kind = 'dungeon_run' "
        "  AND {} "
        "  AND (recruit_until + estimated_minutes * 60 + {}) > {} "
        "  ORDER BY id ASC LIMIT 50"
        ") active_ops ON active_ops.id = o.id "
        "JOIN companion_opportunity_interests i ON i.opportunity_id = o.id AND i.opportunity_revision = o.revision "
        "LEFT JOIN companion_personas p ON p.persona_id = i.persona_id "
        "WHERE i.state IN ('accepted', 'active') "
        "ORDER BY o.id ASC, i.character_guid ASC",
        stateFilter, graceSec, now));
    if (res)
    {
        do
        {
            Field* f = res->Fetch();
            uint64 const oppId = f[0].Get<uint64>();
            uint32 const rev = f[1].Get<uint32>();
            uint32 const mapId = f[2].Get<uint32>();
            uint8 const diff = f[3].Get<uint8>();
            uint32 const grpGuid = f[4].Get<uint32>();
            std::string const runState = f[5].Get<std::string>();
            uint32 const baseUntil = f[6].Get<uint32>();
            uint32 const estMin = f[7].Get<uint32>();
            bool const isShadow = f[8].Get<uint8>() != 0;
            std::string const rolesJson = f[9].Get<std::string>();
            uint32 const charGuid = f[10].Get<uint32>();
            std::string const role = f[11].Get<std::string>();
            std::string const interestState = f[12].Get<std::string>();
            uint32 const interestRev = f[13].Get<uint32>();
            bool const enrolledCoop = !f[14].IsNull() && f[14].Get<uint8>() == 1 &&
                                      !f[15].IsNull() && f[15].Get<std::string>() == "active" &&
                                      f[16].Get<uint64>() != 0;

            uint32 const expAt = baseUntil + estMin * 60 + graceSec;
            if (expAt <= now)
                continue;

            auto& run = runsByOpp[oppId];
            if (run.opportunityId == 0)
            {
                run.opportunityId = oppId;
                run.revision = rev;
                run.targetMapId = mapId;
                run.difficulty = diff;
                run.expectedGroupId = grpGuid;
                run.runState = runState;
                run.assemblyDeadline = baseUntil + assemblyGraceSec;
                run.expiresAt = expAt;
                run.isShadow = isShadow;
                run.boundToNativeGroup = (grpGuid != 0);
                if (!CompanionCoopAuth::ParseRoleRequirements(rolesJson, run.requiredRoles))
                    run.requiredRoles = CompanionCoopAuth::RoleRequirements{};
            }
            CompanionCoopAuth::AuthorizedMember mem;
            mem.characterGuid = charGuid;
            mem.role = role;
            mem.interestState = interestState;
            mem.interestRevision = interestRev;
            mem.enrolledCoop = enrolledCoop;
            run.roster.push_back(std::move(mem));
        } while (res->NextRow());
    }

    // Confirm gPendingDrainedOpps cleanup ONLY once DB query proves the row is no longer active/assembling
    // and no pending exit remains queued for that oppId!
    std::unordered_set<uint64> skipDrained;
    std::unordered_set<uint64_t> pendingExitOppIds;
    {
        std::lock_guard<std::mutex> lock(gCoopQueueMutex);
        for (auto const& ex : gPendingCoopExits)
            pendingExitOppIds.insert(ex.opportunityId);
        for (auto it = gPendingDrainedOpps.begin(); it != gPendingDrainedOpps.end();)
        {
            if (runsByOpp.find(*it) == runsByOpp.end() && pendingExitOppIds.count(*it) == 0)
                it = gPendingDrainedOpps.erase(it);
            else
                ++it;
        }
        skipDrained = gPendingDrainedOpps;
    }

    std::unordered_set<uint64_t> liveOppIds;
    std::unordered_set<uint32_t> liveGroupIds;
    std::vector<CompanionCoopAuth::AuthorizedRun> nextRuns;
    std::vector<PendingCoopExit> orphanCleanupExits;
    nextRuns.reserve(runsByOpp.size());

    for (auto& [oppId, run] : runsByOpp)
    {
        if (skipDrained.count(oppId) > 0)
            continue;  // Already drained in memory; awaiting confirmed DB persistence — never revive!

        // Issue #22 (P1-1): Verify any DB 'active' row with group_guid > 0 actually has a live native Group!
        // If the native group is already gone (e.g. crash/shutdown orphan), clean it up cleanly instead of
        // re-registering ownership and leaking its admission token forever!
        if (!run.isShadow && run.boundToNativeGroup && run.expectedGroupId != 0 && run.runState == "active")
        {
            Group const* nativeGrp = sGroupMgr->GetGroupByGUID(run.expectedGroupId);
            if (!nativeGrp)
            {
                std::vector<uint32> memGuids;
                memGuids.reserve(run.roster.size());
                for (auto const& m : run.roster)
                    memGuids.push_back(m.characterGuid);
                ReleaseCoopAdmissionToken(oppId);
                PendingCoopExit ex;
                ex.opportunityId = oppId;
                ex.exitedMemberGuids = std::move(memGuids);
                ex.reason = (mode == CompanionCoopDrain::CoopMode::Off) ? CompanionCoopDrain::DrainReason::ToggleOff
                                                                        : CompanionCoopDrain::DrainReason::RunExpired;
                ex.groupFullyExited = true;
                orphanCleanupExits.push_back(std::move(ex));
                LOG_INFO("playerbots",
                         "[CompanionCoop] Swept orphaned DB active opp #{} (native group {} no longer exists)",
                         oppId, run.expectedGroupId);
                continue;
            }
            liveGroupIds.insert(run.expectedGroupId);
            std::vector<uint32_t> memGuids;
            memGuids.reserve(run.roster.size());
            for (auto const& m : run.roster)
                memGuids.push_back(m.characterGuid);
            uint64 const liveToken = GroupTokenFor(nativeGrp);
            std::lock_guard<std::mutex> dlock(gDrainMutex);
            gDrainCoordinator.RegisterCommittedGroup(run.opportunityId, run.revision, run.expectedGroupId, liveToken,
                                                     run.targetMapId, nativeGrp->isLFGGroup() || run.isLfgBound,
                                                     memGuids, now, run.expiresAt);
        }
        liveOppIds.insert(oppId);
        nextRuns.push_back(std::move(run));
    }

    // Check liveness of ALL currently owned groups (world thread safe via sGroupMgr->GetGroupByGUID) AND query
    // their EXACT DB states directly (`WHERE id IN (...)`) — NEVER infer DB closure from the time-filtered/LIMIT-50
    // reload query, or RunExpired runs would get swept mid-pull right when their time window expires!
    std::vector<uint32_t> ownedGroupIds;
    std::vector<uint64_t> ownedOppIds;
    {
        std::lock_guard<std::mutex> dlock(gDrainMutex);
        ownedGroupIds = gDrainCoordinator.OwnedGroupIds();
        ownedOppIds = gDrainCoordinator.OwnedOpportunityIds();
    }
    for (uint32_t gid : ownedGroupIds)
    {
        if (sGroupMgr->GetGroupByGUID(gid) != nullptr)
            liveGroupIds.insert(gid);
    }
    std::unordered_set<uint64_t> dbClosedOppIds;
    if (!ownedOppIds.empty())
    {
        std::ostringstream idList;
        for (size_t i = 0; i < ownedOppIds.size(); ++i)
        {
            if (i > 0)
                idList << ",";
            idList << ownedOppIds[i];
        }
        std::unordered_map<uint64_t, std::string> exactDbStates;
        QueryResult stateRes = CharacterDatabase.Query(
            Acore::StringFormat("SELECT id, state FROM companion_opportunities WHERE id IN ({})", idList.str()));
        bool stateQueryOk = (stateRes != nullptr);
        if (stateRes)
        {
            do
            {
                Field* sf = stateRes->Fetch();
                exactDbStates[sf[0].Get<uint64>()] = sf[1].Get<std::string>();
            } while (stateRes->NextRow());
        }
        else if (CharacterDatabase.Query("SELECT 1"))
        {
            // DB is healthy and returned 0 rows (all owned IDs deleted from DB).
            stateQueryOk = true;
        }
        if (stateQueryOk)
        {
            for (uint64_t oid : ownedOppIds)
            {
                auto sit = exactDbStates.find(oid);
                if (sit == exactDbStates.end() || !CompanionCoopAuth::IsActiveRunState(sit->second))
                    dbClosedOppIds.insert(oid);
            }
        }
    }
    std::vector<CompanionCoopDrain::StaleGroupCleanup> staleCleaned;
    {
        std::lock_guard<std::mutex> dlock(gDrainMutex);
        staleCleaned = gDrainCoordinator.SweepOrphanedOrExpiredGroups(dbClosedOppIds, liveGroupIds, pendingExitOppIds, now);
        if (!CompanionCoopDrain::AllowsNewAdmissions(mode))
            gDrainCoordinator.TriggerGlobalDrain(mode, now, drainGraceSec);
    }
    for (auto const& item : staleCleaned)
    {
        ReleaseCoopAdmissionToken(item.opportunityId);
        if (item.needsDbDrainWrite)
        {
            PendingCoopExit ex;
            ex.opportunityId = item.opportunityId;
            ex.exitedMemberGuids = item.remainingMembers;
            ex.reason = item.reason;
            ex.groupFullyExited = true;
            orphanCleanupExits.push_back(std::move(ex));
        }
        LOG_INFO("playerbots", "[CompanionCoop] Swept stale owned opp #{} (group {}, db_write={})",
                 item.opportunityId, item.groupId, item.needsDbDrainWrite ? "yes" : "no");
    }
    if (!orphanCleanupExits.empty())
    {
        {
            std::lock_guard<std::mutex> lock(gCoopQueueMutex);
            for (auto& ex : orphanCleanupExits)
            {
                gPendingDrainedOpps.insert(ex.opportunityId);
                gPendingCoopExits.push_back(std::move(ex));
            }
        }
        FlushCoopGroupMutations();
    }

    bool reloaded = false;
    {
        std::lock_guard<std::mutex> lock(gCoopMutex);
        // Generation guard: if a bind or disband committed while this SQL read was running, do NOT clobber it!
        reloaded = gCoopRegistry.ReloadRuns(std::move(nextRuns), startGen);
    }
    if (reloaded)
    {
        // Prune committed tokens ONLY for runs that are neither in liveOppIds nor still owned/draining!
        std::unordered_set<uint64_t> keepOppIds = liveOppIds;
        {
            std::lock_guard<std::mutex> dlock(gDrainMutex);
            for (uint64_t oid : gDrainCoordinator.OwnedOpportunityIds())
                keepOppIds.insert(oid);
        }
        std::lock_guard<std::mutex> lock(gAdmissionMutex);
        (void)gAdmissionController.PruneCommittedTokensNotIn(keepOppIds);
    }
}

bool CompanionBrain::IsAuthorizedCoopMember(uint32 lowGuid, uint64& outOppId, uint32& outTargetMap, uint32& outGroupId)
{
    outOppId = 0;
    outTargetMap = 0;
    outGroupId = 0;
    if (!CoopEnabled() || !lowGuid)
        return false;
    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());
    std::lock_guard<std::mutex> lock(gCoopMutex);
    return gCoopRegistry.LookupBoundMember(lowGuid, now, outOppId, outTargetMap, outGroupId);
}

bool CompanionBrain::AuthorizeCoopGroup(Player const* bot, CompanionCoopAuth::NativeGroupSnapshot const& snap,
                                        bool allowQueueBind, bool* outAssemblyInProgress, std::string* outReason)
{
    if (outAssemblyInProgress)
        *outAssemblyInProgress = false;
    if (outReason)
        outReason->clear();
    if (!bot || !CoopEnabled())
    {
        if (outReason)
            *outReason = "coop_disabled";
        return false;
    }

    uint32 const botGuid = bot->GetGUID().GetCounter();
    uint32 const botMapId = bot->GetMapId();
    uint32 const botInstanceId = bot->GetInstanceId();
    Map const* map = bot->GetMap();
    uint8 const botDifficulty = map ? static_cast<uint8>(map->GetDifficulty()) : 0;
    bool const botInNonRaidDungeon = map && map->IsNonRaidDungeon();
    bool const botInRaid = map && map->IsRaid();
    bool const botInBgOrArena = map && map->IsBattlegroundOrArena();
    uint32 const now = static_cast<uint32>(GameTime::GetGameTime().count());

    CompanionCoopAuth::AuthDecision dec;
    {
        std::lock_guard<std::mutex> lock(gCoopMutex);
        dec = gCoopRegistry.Authorize(true, botGuid, botMapId, botInstanceId, botDifficulty, botInNonRaidDungeon,
                                      botInRaid, botInBgOrArena, snap, now);
        if (dec.authorized)
        {
            bool const pinned = gCoopRegistry.PinRuntimeBindings(
                dec.opportunityId, dec.revision, snap.groupId, snap.groupToken, snap.leaderGuid, dec.leaderFailover,
                snap.isLfg, botInNonRaidDungeon ? botMapId : 0, botInNonRaidDungeon ? botInstanceId : 0);
            if (!pinned)
            {
                if (outReason)
                    *outReason = "runtime_pin_mismatch";
                return false;
            }
        }
    }

    if (dec.readyToBind && allowQueueBind)
    {
        // Queue atomic DB bind for the world thread (ZERO blocking DB I/O on map threads!).
        // If this opportunity was already rejected by the admission gate, fail closed cleanly so TickBot
        // disbands the blocked unbound group instead of retrying every 15s forever.
        std::lock_guard<std::mutex> lock(gCoopQueueMutex);
        auto blkIt = gAdmissionBlockedOpps.find(dec.opportunityId);
        if (blkIt != gAdmissionBlockedOpps.end() && blkIt->second > now)
        {
            if (outAssemblyInProgress)
                *outAssemblyInProgress = false;
            if (outReason)
                *outReason = "admission_gate_blocked";
            return false;
        }
        auto failIt = gFailedBindCooldown.find(dec.opportunityId);
        if (failIt == gFailedBindCooldown.end() || failIt->second <= now)
        {
            PendingCoopBind req;
            req.opportunityId = dec.opportunityId;
            req.revision = dec.revision;
            req.previousGroupId = dec.previousGroupId;
            req.instanceId = botInNonRaidDungeon ? botInstanceId : 0;
            req.isLfgRebind = dec.isLfgRebind;
            req.snap = snap;
            gPendingCoopBinds[dec.opportunityId] = std::move(req);
        }
        if (outAssemblyInProgress)
            *outAssemblyInProgress = true;
        if (outReason)
            *outReason = dec.isLfgRebind ? "lfg_rebind_queued" : "bind_queued";
        return false;
    }

    if (outAssemblyInProgress)
        *outAssemblyInProgress = dec.assemblyInProgress;
    if (outReason)
        *outReason = dec.reason;
    return dec.authorized;
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
