/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "CompanionLife.h"

#include "CharacterCache.h"
#include "Chat.h"
#include "CompanionBotMgr.h"
#include "CompanionBrain.h"
#include "CompanionDuty.h"
#include "CompanionLease.h"
#include "CompanionWorkOrderLogic.h"
#include "Config.h"
#include "Creature.h"
#include "DBCStores.h"
#include "DatabaseEnv.h"
#include "GameTime.h"
#include "Group.h"
#include "Guild.h"
#include "GuildMgr.h"
#include "Log.h"
#include "Map.h"
#include "ObjectAccessor.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "QuestDef.h"
#include "SharedDefines.h"
#include "StringFormat.h"
#include "WorldPacket.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <ctime>
#include <deque>
#include <mutex>
#include <sstream>
#include <unordered_map>
#include <unordered_set>

namespace
{
constexpr uint32 REFRESH_INTERVAL_MS = 300000;      // 5 min DB sync
constexpr uint32 AUTO_ORDER_INTERVAL_MS = 900000;   // 15 min autonomous guild work-order check
constexpr uint32 PAIR_ADVENTURE_COOLDOWN_SEC = 1800;// 30 min cooldown per pair for shared zone quest bonds
constexpr size_t MAX_EVENTS_PER_BOT = 10;           // rolling real event memory (5-10 events)
constexpr uint32 EMOTE_COOLDOWN_SEC = 45;           // per-bot emote reply cooldown
constexpr uint32 CHEER_COOLDOWN_SEC = 60;           // per-bot level-up cheer cooldown
constexpr uint32 CRAFT_EVENT_COOLDOWN_SEC = 600;    // avoid spamming 20 identical smelt events
constexpr uint32 PROPOSAL_COOLDOWN_SEC = 1800;      // 30 min between activity proposals per owner
constexpr uint32 PROPOSAL_DECLINE_COOLDOWN_SEC = 3600; // 60 min after owner says "no" / "later"
constexpr uint32 MAX_PROPOSALS_PER_SESSION = 2;     // max 1-2 activity proposals per owner session
constexpr uint32 OWNER_SESSION_RESET_SEC = 10800;   // 3 hours offline/gap resets proposal session counter

std::mutex gLifeMutex;
std::mutex gCreateOrderMutex;
bool gLoaded = false;
uint32 gRefreshTimerMs = 0;
uint32 gAutoOrderTimerMs = 0;
bool gInitialOrderPulseDone = false;

std::unordered_map<uint32, CompanionLife::Profile> gProfiles;                  // low guid -> profile
std::unordered_map<std::string, uint32> gGuidByLowerName;                      // lower name -> low guid
std::unordered_map<uint32, std::deque<CompanionLife::EventRecord>> gEvents;    // low guid -> newest first (<=10)
std::unordered_map<uint64, CompanionLife::Relation> gRelations;                // packed(lo, hi) -> pair relation
CompanionWorkOrderLogic::Tracker gOrderTracker;                                // verified work-order state machine
bool gOrderVerificationCols = false;                                           // true when migration 003 cols exist
bool gOrderMigrationWarned = false;
// Cached guild_bank_right rows: key = (guildId << 16) | (tabId << 8) | rankId -> {gbright, slotPerDay}
std::unordered_map<uint64, std::pair<uint8, int32>> gBankTabRights;

std::unordered_map<uint32, uint32> gLastEmoteSec;          // bot low guid -> last emote reply time
std::unordered_map<uint32, uint32> gLastCheerSec;          // bot low guid -> last level-up cheer time
std::unordered_map<uint64, uint32> gLastCraftEventSec;     // (botGuid<<32)|itemEntry -> last craft event time
std::unordered_map<uint64, uint32> gLastPairBondSec;       // pair key -> last shared zone adventure bond
std::unordered_map<uint32, uint32> gInnIdleSinceSec;       // bot low guid -> when resting-idle started

struct OwnerProposalState
{
    uint32 sessionStartSec = 0;
    uint32 lastProposalSec = 0;
    uint32 cooldownUntilSec = 0;
    uint32 proposalsSent = 0;
    uint32 lastProposerGuid = 0;
};
std::unordered_map<uint32, OwnerProposalState> gOwnerProposals;   // owner low guid -> session state

std::string LowerStr(std::string s)
{
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

std::string TrimStr(std::string s)
{
    size_t a = 0;
    while (a < s.size() && std::isspace(static_cast<unsigned char>(s[a])))
        ++a;
    size_t b = s.size();
    while (b > a && std::isspace(static_cast<unsigned char>(s[b - 1])))
        --b;
    return s.substr(a, b - a);
}

uint32 NowSec()
{
    return static_cast<uint32>(GameTime::GetGameTime().count());
}

uint64 PairKey(uint32 a, uint32 b)
{
    uint32 const lo = std::min(a, b);
    uint32 const hi = std::max(a, b);
    return (static_cast<uint64>(lo) << 32) | static_cast<uint64>(hi);
}

std::string ZoneNameOf(uint32 zoneId)
{
    if (AreaTableEntry const* area = zoneId ? sAreaTableStore.LookupEntry(zoneId) : nullptr)
        if (area->area_name[0] && area->area_name[0][0])
            return area->area_name[0];
    return "Azeroth";
}

std::string SkillNameOf(uint32 skillId)
{
    if (SkillLineEntry const* line = skillId ? sSkillLineStore.LookupEntry(skillId) : nullptr)
        if (line->name[0] && line->name[0][0])
            return line->name[0];
    return "Profession";
}

std::string NameOfGuid(uint32 lowGuid)
{
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        auto it = gProfiles.find(lowGuid);
        if (it != gProfiles.end() && !it->second.name.empty())
            return it->second.name;
    }
    std::string nm;
    if (sCharacterCache->GetCharacterNameByGuid(ObjectGuid::Create<HighGuid::Player>(lowGuid), nm))
        return nm;
    return "Guid#" + std::to_string(lowGuid);
}

std::string SqlEsc(std::string s)
{
    CharacterDatabase.EscapeString(s);
    return s;
}

void SendWhisper(Player* from, Player* to, std::string const& text)
{
    if (!from || !to || text.empty())
        return;
    WorldPacket data;
    ChatHandler::BuildChatPacket(data, CHAT_MSG_WHISPER, LANG_UNIVERSAL, from, to, text);
    to->SendDirectMessage(&data);
}

void RecordEventInternal(uint32 guid, std::string const& type, std::string const& summary, uint32 partnerGuid,
                         uint32 zoneId)
{
    if (!guid || summary.empty())
        return;
    uint32 const now = NowSec();
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        auto& q = gEvents[guid];
        q.push_front(CompanionLife::EventRecord{type, summary, partnerGuid, zoneId, now});
        while (q.size() > MAX_EVENTS_PER_BOT)
            q.pop_back();
    }

    // Async insert (zero map-thread stall). Decouple durable autobiographical history from the
    // 12-event UI window (Issue #4): NEVER prune milestones; retain up to 250 routine events per
    // character and prune asynchronously only every 25 inserts.
    CharacterDatabase.Execute(
        "INSERT INTO companion_events (`guid`, `event_type`, `summary`, `partner_guid`, `zone_id`, `created_at`) "
        "VALUES ({}, '{}', '{}', {}, {}, {})",
        guid, SqlEsc(type), SqlEsc(summary), partnerGuid, zoneId, now);

    static std::mutex sPruneMutex;
    static std::unordered_map<uint32, uint32> sInsertCounts;
    bool shouldPrune = false;
    {
        std::lock_guard<std::mutex> plock(sPruneMutex);
        uint32& cnt = sInsertCounts[guid];
        ++cnt;
        if (cnt >= 25)
        {
            cnt = 0;
            shouldPrune = true;
        }
    }
    if (shouldPrune)
    {
        CharacterDatabase.Execute(
            "DELETE FROM companion_events WHERE `guid` = {} "
            "AND `event_type` NOT IN ('level_up', 'skill_milestone', 'boss_kill', 'work_order', 'dungeon', 'reputation') "
            "AND `id` NOT IN (SELECT id FROM (SELECT id FROM companion_events WHERE `guid` = {} "
            "AND `event_type` NOT IN ('level_up', 'skill_milestone', 'boss_kill', 'work_order', 'dungeon', 'reputation') "
            "ORDER BY `created_at` DESC, `id` DESC LIMIT 250) t)",
            guid, guid);
    }
}

void BumpRelationInternal(uint32 guidA, uint32 guidB, uint32 addRuns, uint32 addWipes, uint32 addHelp,
                          uint32 addLoot, int32 addAffinity, std::string const& lastEvent)
{
    if (!guidA || !guidB || guidA == guidB)
        return;
    uint32 const lo = std::min(guidA, guidB);
    uint32 const hi = std::max(guidA, guidB);
    uint64 const key = PairKey(lo, hi);
    uint32 const now = NowSec();

    CompanionLife::Relation snap;
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        CompanionLife::Relation& r = gRelations[key];
        r.otherGuid = hi;
        r.runsTogether += addRuns;
        r.wipes += addWipes;
        r.helpGiven += addHelp;
        r.lootContests += addLoot;
        r.affinity += addAffinity;
        r.lastEvent = lastEvent;
        r.updatedAt = now;
        snap = r;
    }

    CharacterDatabase.Execute(
        "INSERT INTO companion_relationships (`guid_a`, `guid_b`, `runs_together`, `wipes`, `help_given`, "
        "`loot_contests`, `affinity`, `last_event`, `updated_at`) "
        "VALUES ({}, {}, {}, {}, {}, {}, {}, '{}', {}) "
        "ON DUPLICATE KEY UPDATE `runs_together` = {}, `wipes` = {}, `help_given` = {}, "
        "`loot_contests` = {}, `affinity` = {}, `last_event` = '{}', `updated_at` = {}",
        lo, hi, snap.runsTogether, snap.wipes, snap.helpGiven, snap.lootContests, snap.affinity,
        SqlEsc(snap.lastEvent), now, snap.runsTogether, snap.wipes, snap.helpGiven, snap.lootContests, snap.affinity,
        SqlEsc(snap.lastEvent), now);
}

bool TableExists(char const* tableName)
{
    QueryResult res = CharacterDatabase.Query(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = '{}'",
        tableName);
    return res && res->Fetch()[0].Get<uint64>() > 0;
}

bool HasWorkOrderVerificationCols()
{
    QueryResult res = CharacterDatabase.Query(
        "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() "
        "AND table_name = 'companion_work_orders' "
        "AND column_name IN ('attempt_id', 'produced_qty', 'transferred_qty', 'received_qty', 'delivery_tab', 'revision')");
    return res && res->Fetch()[0].Get<uint64>() >= 6;
}

CompanionLife::WorkOrder ToPublicOrder(CompanionWorkOrderLogic::OrderState const& s)
{
    CompanionLife::WorkOrder wo;
    wo.id = s.id;
    wo.requesterGuid = s.requesterGuid;
    wo.requesterName = s.requesterName;
    wo.crafterGuid = s.crafterGuid;
    wo.crafterName = s.crafterName;
    wo.attemptId = s.attemptId;
    wo.itemEntry = s.itemEntry;
    wo.itemName = s.itemName;
    wo.quantity = s.quantity;
    wo.producedQty = s.producedQty;
    wo.transferredQty = s.transferredQty;
    wo.receivedQty = s.receivedQty;
    wo.deliveryTab = s.deliveryTab;
    wo.story = s.story;
    wo.status = s.status;
    wo.revision = s.revision;
    wo.createdAt = s.createdAt;
    wo.updatedAt = s.updatedAt;
    return wo;
}

uint64 BankRightKey(uint32 guildId, uint8 tabId, uint8 rankId)
{
    return (static_cast<uint64>(guildId) << 16) | (static_cast<uint64>(tabId) << 8) | static_cast<uint64>(rankId);
}

bool HasAccessibleWithdrawRightsLocked(Guild const* guild, uint32 memberGuid, uint8 bankTab)
{
    if (!guild || !memberGuid || bankTab == CompanionWorkOrderLogic::TAB_NONE)
        return false;
    ObjectGuid const og = ObjectGuid::Create<HighGuid::Player>(memberGuid);
    Guild::Member const* member = guild->GetMember(og);
    if (!member)
        return false;
    if (!guild->MemberHasTabRights(og, bankTab, GUILD_BANK_RIGHT_VIEW_TAB))
        return false;
    uint8 const rankId = member->GetRankId();
    if (rankId == 0)
        return true;   // Guild Master always has full tab view & withdraw rights
    auto it = gBankTabRights.find(BankRightKey(guild->GetId(), bankTab, rankId));
    if (it == gBankTabRights.end())
        return false;
    // Require VIEW_TAB right AND non-zero daily withdraw slots (-1 = unlimited, >0 = slots/day; 0 = view-only).
    return (it->second.first & GUILD_BANK_RIGHT_VIEW_TAB) != 0 && it->second.second != 0;
}

std::string BuildWorkOrderUpdateSql(CompanionWorkOrderLogic::ActionResult const& res, uint32 nowSec)
{
    CompanionWorkOrderLogic::OrderState const& wo = res.order;
    if (res.previousStatus == "open")
    {
        return Acore::StringFormat(
            "UPDATE companion_work_orders SET crafter_guid = {}, crafter_name = '{}', attempt_id = {}, "
            "produced_qty = {}, transferred_qty = {}, received_qty = {}, delivery_tab = {}, status = '{}', "
            "revision = {}, updated_at = {} "
            "WHERE id = {} AND status = 'open' AND crafter_guid = 0 AND revision = {}",
            wo.crafterGuid, SqlEsc(wo.crafterName), wo.attemptId, wo.producedQty, wo.transferredQty,
            wo.receivedQty, static_cast<uint32>(wo.deliveryTab), SqlEsc(wo.status), wo.revision, nowSec,
            wo.id, res.expectedRevision);
    }
    return Acore::StringFormat(
        "UPDATE companion_work_orders SET produced_qty = {}, transferred_qty = {}, received_qty = {}, "
        "delivery_tab = {}, status = '{}', revision = {}, updated_at = {} "
        "WHERE id = {} AND status = 'claimed' AND crafter_guid = {} AND attempt_id = {} AND revision = {}",
        wo.producedQty, wo.transferredQty, wo.receivedQty, static_cast<uint32>(wo.deliveryTab),
        SqlEsc(wo.status), wo.revision, nowSec, wo.id, wo.crafterGuid, wo.attemptId, res.expectedRevision);
}

void PersistWorkOrderTransitionSync(CompanionWorkOrderLogic::ActionResult const& res, uint32 nowSec)
{
    if (!res.stateChanged || res.orderId == 0)
        return;
    // Always use synchronous DirectExecute for every work-order mutation (claim, produce, partial/full delivery)
    // so writes are strictly ordered on the DB and can never race out-of-order against a later delivery CAS.
    CharacterDatabase.DirectExecute(BuildWorkOrderUpdateSql(res, nowSec));
}

// Re-reads the authoritative DB row after a guarded CAS update; on mismatch (e.g. concurrent expiry) restores
// DB truth into gOrderTracker and returns false. On DB read error returns false without keeping optimistic state.
bool ConfirmWorkOrderRowFromDb(CompanionWorkOrderLogic::ActionResult const& res, uint32 expectedCrafterGuid)
{
    QueryResult chk = CharacterDatabase.Query(
        "SELECT `status`, `crafter_guid`, `attempt_id`, `produced_qty`, `transferred_qty`, `received_qty`, "
        "`delivery_tab`, `revision`, `updated_at` FROM `companion_work_orders` WHERE `id` = {}",
        res.orderId);
    if (!chk)
    {
        LOG_WARN("playerbots",
                 "[GuildLife] Work order #{} confirm query returned no row (expected status={} rev={}); "
                 "skipping completion side effects.",
                 res.orderId, res.order.status, res.newRevision);
        return false;
    }

    Field* f = chk->Fetch();
    CompanionWorkOrderLogic::OrderState dbRow = res.order;
    dbRow.status = f[0].Get<std::string>();
    dbRow.crafterGuid = f[1].Get<uint32>();
    dbRow.attemptId = f[2].Get<uint64>();
    dbRow.producedQty = f[3].Get<uint32>();
    dbRow.transferredQty = f[4].Get<uint32>();
    dbRow.receivedQty = f[5].Get<uint32>();
    dbRow.deliveryTab = f[6].Get<uint8>();
    dbRow.revision = f[7].Get<uint32>();
    dbRow.updatedAt = f[8].Get<uint32>();

    bool const matched = (dbRow.status == res.order.status && dbRow.crafterGuid == expectedCrafterGuid &&
                          dbRow.attemptId == res.attemptId && dbRow.revision == res.newRevision);
    if (!matched)
    {
        {
            std::lock_guard<std::mutex> lock(gLifeMutex);
            gOrderTracker.RollbackOrReplaceOrder(dbRow);
        }
        LOG_WARN("playerbots",
                 "[GuildLife] Work order #{} CAS mismatch (db status={} rev={} vs expected status={} rev={}); "
                 "resynced memory from DB.",
                 res.orderId, dbRow.status, dbRow.revision, res.order.status, res.newRevision);
    }
    return matched;
}

bool WarnIfMissingVerificationSchema(bool hasVerificationCols)
{
    if (hasVerificationCols)
        return true;
    if (!gOrderMigrationWarned)
    {
        gOrderMigrationWarned = true;
        LOG_ERROR("playerbots",
                  "[GuildLife] companion_work_orders missing Issue #27 verification columns (run migration 003); "
                  "failing closed on work-order claims/deliveries.");
    }
    return false;
}

std::string LatestEventSummary(uint32 guid)
{
    std::lock_guard<std::mutex> lock(gLifeMutex);
    auto it = gEvents.find(guid);
    if (it != gEvents.end() && !it->second.empty())
        return it->second.front().summary;
    return "";
}

uint32 FindGuidByNameLocked(std::string const& name)
{
    auto it = gGuidByLowerName.find(LowerStr(name));
    if (it != gGuidByLowerName.end())
        return it->second;
    return 0;
}

// Weekly Hearthguard Guild Events (server local time):
//   Wednesday 18:00-22:00 -> Craft & Supply Night (more work orders + craft priority)
//   Friday    18:00-22:00 -> Fishing Night (+35 fishing need)
//   Sunday    13:00-21:00 -> Sunday Dungeon & Adventure Day (wider neighbourhood budget + dungeon proposals)
enum class WeeklyEventKind : uint8
{
    None = 0,
    CraftNight = 1,
    FishingNight = 2,
    SundayAdventure = 3,
};

WeeklyEventKind CurrentWeeklyEvent()
{
    if (!sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.GuildLife.WeeklyEvents.Enabled", true, false))
        return WeeklyEventKind::None;
    std::time_t const tt = std::time(nullptr);
    std::tm tmBuf{};
    localtime_r(&tt, &tmBuf);
    int const wday = tmBuf.tm_wday;  // 0=Sun, 3=Wed, 5=Fri
    int const hour = tmBuf.tm_hour;
    if (wday == 3 && hour >= 18 && hour < 22)
        return WeeklyEventKind::CraftNight;
    if (wday == 5 && hour >= 18 && hour < 22)
        return WeeklyEventKind::FishingNight;
    if (wday == 0 && hour >= 13 && hour < 21)
        return WeeklyEventKind::SundayAdventure;
    return WeeklyEventKind::None;
}

struct AutoOrderTemplate
{
    uint32 itemEntry;
    uint32 qty;
    uint8 minLvl;
    uint8 maxLvl;
    uint32 crafterSkill;   // profession that produces it
    bool meleeOnly;
    bool manaOnly;
    char const* storyFmt;  // "{req} <storyFmt>"
};

// Curated Classic/TBC craftable staples that Hearthguard crafters genuinely make at each tier.
static constexpr AutoOrderTemplate gAutoOrderCatalog[] = {
    // Alchemy potions & elixirs
    {118,  5,  1, 18, SKILL_ALCHEMY,        false, false, "needs Minor Healing Potions for safe solo questing"},
    {2455, 5,  5, 22, SKILL_ALCHEMY,        false, true,  "asked for Minor Mana Potions to keep spells flowing"},
    {2454, 4,  1, 20, SKILL_ALCHEMY,        true,  false, "wants Elixirs of Lion's Strength for tougher fights"},
    {858,  5, 12, 32, SKILL_ALCHEMY,        false, false, "is stocking Lesser Healing Potions for upcoming dungeons"},
    {3385, 5, 14, 35, SKILL_ALCHEMY,        false, true,  "needs Lesser Mana Potions before the next dungeon run"},
    {929,  5, 21, 45, SKILL_ALCHEMY,        false, false, "requested Healing Potions for Hearthguard raid prep"},
    // First Aid bandages
    {1251, 10, 1, 18, SKILL_FIRST_AID,      false, false, "is running low on Linen Bandages out in the wilds"},
    {3530, 10, 15, 35, SKILL_FIRST_AID,     false, false, "asked for Wool Bandages for group adventures"},
    {6450, 10, 28, 50, SKILL_FIRST_AID,     false, false, "is stocking Silk Bandages for Scarlet Monastery runs"},
    // Blacksmithing / Mining bars & stones
    {2840, 10, 1, 22, SKILL_MINING,         false, false, "needs Copper Bars for the Hearthguard forge"},
    {2862, 5,  1, 18, SKILL_BLACKSMITHING,  true,  false, "wants Rough Sharpening Stones to keep blades keen"},
    {2841, 10, 15, 38, SKILL_MINING,        false, false, "is gathering Bronze Bars for guild blacksmithing"},
    {2863, 5, 12, 32, SKILL_BLACKSMITHING,  true,  false, "needs Coarse Sharpening Stones before heading out"},
    {3575, 10, 25, 50, SKILL_MINING,        false, false, "asked for Iron Bars to build up the guild vault"},
    // Tailoring bags
    {4238, 1,  1, 18, SKILL_TAILORING,      false, false, "hopes a tailor can stitch a Linen Bag for extra loot"},
    {4240, 1, 12, 30, SKILL_TAILORING,      false, false, "asked a guild tailor for a roomy Woolen Bag"},
    // Leatherworking armor kits
    {2304, 4,  1, 20, SKILL_LEATHERWORKING, false, false, "wants Light Armor Kits to reinforce leveling gear"},
    {2313, 4, 15, 38, SKILL_LEATHERWORKING, false, false, "asked for Medium Armor Kits for dungeon prep"},
    // Cooking staples
    {2681, 10, 1, 16, SKILL_COOKING,        false, false, "is craving Roasted Boar Meat for the road"},
    {6888, 10, 1, 16, SKILL_COOKING,        false, false, "asked the guild cooks for Herb Baked Eggs"},
    {6890, 10, 10, 28, SKILL_COOKING,       false, false, "is packing Smoked Bear Meat for a long quest trek"},
    {4592, 10, 10, 30, SKILL_COOKING,       false, false, "wants cooked Longjaw Mud Snappers for dungeon food"},
};

void MaybeBumpNearbyGuildBonds(Player* actor, std::string const& reason)
{
    if (!actor || !actor->IsInWorld() || !sCompanionBotMgr.IsCompanion(actor))
        return;
    uint32 const low = actor->GetGUID().GetCounter();
    uint32 const now = NowSec();
    for (std::string const& name : sCompanionBotMgr.CompanionNames())
    {
        ObjectGuid const og = sCharacterCache->GetCharacterGuidByName(name);
        if (!og || og == actor->GetGUID())
            continue;
        Player* other = ObjectAccessor::FindConnectedPlayer(og);
        if (!other || !other->IsInWorld() || !other->IsAlive())
            continue;
        if (other->GetMapId() != actor->GetMapId() || other->GetDistance(actor) > 100.0f)
            continue;

        uint64 const key = PairKey(low, og.GetCounter());
        bool due = false;
        {
            std::lock_guard<std::mutex> lock(gLifeMutex);
            auto& last = gLastPairBondSec[key];
            if (last == 0 || now - last >= PAIR_ADVENTURE_COOLDOWN_SEC)
            {
                last = now;
                due = true;
            }
        }
        if (due)
            BumpRelationInternal(low, og.GetCounter(), 1, 0, 0, 0, 2, reason);
    }
}
}  // namespace

void CompanionLife::ReloadAll()
{
    gLoaded = true;
    if (!TableExists("companion_profiles"))
        return;

    std::unordered_map<uint32, Profile> profiles;
    std::unordered_map<std::string, uint32> byName;
    if (QueryResult res = CharacterDatabase.Query(
            "SELECT `guid`, `name`, `archetype`, `culture`, `flaw`, `bio` FROM `companion_profiles`"))
    {
        do
        {
            Field* f = res->Fetch();
            Profile p;
            p.guid = f[0].Get<uint32>();
            p.name = f[1].Get<std::string>();
            p.archetype = f[2].Get<std::string>();
            p.culture = f[3].Get<std::string>();
            p.flaw = f[4].Get<std::string>();
            p.bio = f[5].Get<std::string>();
            byName[LowerStr(p.name)] = p.guid;
            profiles[p.guid] = std::move(p);
        } while (res->NextRow());
    }

    std::unordered_map<uint32, std::deque<EventRecord>> events;
    if (TableExists("companion_events"))
    {
        if (QueryResult res = CharacterDatabase.Query(
                "SELECT `guid`, `event_type`, `summary`, `partner_guid`, `zone_id`, `created_at` "
                "FROM `companion_events` ORDER BY `created_at` DESC, `id` DESC"))
        {
            do
            {
                Field* f = res->Fetch();
                uint32 const guid = f[0].Get<uint32>();
                auto& q = events[guid];
                if (q.size() >= MAX_EVENTS_PER_BOT)
                    continue;
                q.push_back(EventRecord{f[1].Get<std::string>(), f[2].Get<std::string>(), f[3].Get<uint32>(),
                                        f[4].Get<uint32>(), f[5].Get<uint32>()});
            } while (res->NextRow());
        }
    }

    std::unordered_map<uint64, Relation> rels;
    if (TableExists("companion_relationships"))
    {
        if (QueryResult res = CharacterDatabase.Query(
                "SELECT `guid_a`, `guid_b`, `runs_together`, `wipes`, `help_given`, `loot_contests`, "
                "`affinity`, `last_event`, `updated_at` FROM `companion_relationships`"))
        {
            do
            {
                Field* f = res->Fetch();
                uint32 const a = f[0].Get<uint32>();
                uint32 const b = f[1].Get<uint32>();
                Relation r;
                r.otherGuid = std::max(a, b);
                r.runsTogether = f[2].Get<uint32>();
                r.wipes = f[3].Get<uint32>();
                r.helpGiven = f[4].Get<uint32>();
                r.lootContests = f[5].Get<uint32>();
                r.affinity = f[6].Get<int32>();
                r.lastEvent = f[7].Get<std::string>();
                r.updatedAt = f[8].Get<uint32>();
                rels[PairKey(a, b)] = std::move(r);
            } while (res->NextRow());
        }
    }

    std::unordered_map<uint64, std::pair<uint8, int32>> bankRights;
    if (QueryResult br = CharacterDatabase.Query("SELECT guildid, TabId, rid, gbright, SlotPerDay FROM guild_bank_right"))
    {
        do
        {
            Field* f = br->Fetch();
            uint32 const gid = f[0].Get<uint32>();
            uint8 const tabId = f[1].Get<uint8>();
            uint8 const rankId = f[2].Get<uint8>();
            uint8 const gbright = f[3].Get<uint8>();
            int32 const slotsPerDay = f[4].Get<int32>();
            bankRights[BankRightKey(gid, tabId, rankId)] = {gbright, slotsPerDay};
        } while (br->NextRow());
    }

    std::vector<CompanionWorkOrderLogic::OrderState> orders;
    bool hasVerificationCols = false;
    uint64 maxAttemptId = 0;
    if (TableExists("companion_work_orders"))
    {
        uint32 const now = NowSec();
        hasVerificationCols = HasWorkOrderVerificationCols();
        if (now > 172800)
        {
            // Synchronous expiry UPDATE before SELECT so loaded state never predates expiry (Issue #27).
            if (hasVerificationCols)
                CharacterDatabase.DirectExecute(Acore::StringFormat(
                    "UPDATE companion_work_orders SET status = 'expired', revision = revision + 1, updated_at = {} "
                    "WHERE status IN ('open', 'claimed') AND created_at < {}",
                    now, now - 172800));
            else
                CharacterDatabase.DirectExecute(Acore::StringFormat(
                    "UPDATE companion_work_orders SET status = 'expired', updated_at = {} "
                    "WHERE status IN ('open', 'claimed') AND created_at < {}",
                    now, now - 172800));
        }

        if (hasVerificationCols)
        {
            if (QueryResult mres =
                    CharacterDatabase.Query("SELECT COALESCE(MAX(`attempt_id`), 0) FROM `companion_work_orders`"))
                maxAttemptId = mres->Fetch()[0].Get<uint64>();
        }

        // Load ALL active (open/claimed) orders without truncation, plus the latest 25 historical orders
        // (delivered/expired) for reservations & status display (Issue #27).
        auto loadQuery = [&](char const* whereClause, char const* limitClause)
        {
            QueryResult res =
                hasVerificationCols
                    ? CharacterDatabase.Query(
                          "SELECT `id`, `requester_guid`, `requester_name`, `crafter_guid`, `crafter_name`, "
                          "`attempt_id`, `item_entry`, `item_name`, `quantity`, `produced_qty`, "
                          "`transferred_qty`, `received_qty`, `delivery_tab`, `story`, `status`, `revision`, "
                          "`created_at`, `updated_at` "
                          "FROM `companion_work_orders` WHERE {} ORDER BY `id` DESC {}",
                          whereClause, limitClause)
                    : CharacterDatabase.Query(
                          "SELECT `id`, `requester_guid`, `requester_name`, `crafter_guid`, `crafter_name`, "
                          "`item_entry`, `item_name`, `quantity`, `story`, `status`, `created_at`, `updated_at` "
                          "FROM `companion_work_orders` WHERE {} ORDER BY `id` DESC {}",
                          whereClause, limitClause);
            if (!res)
                return;
            do
            {
                Field* f = res->Fetch();
                CompanionWorkOrderLogic::OrderState wo;
                if (hasVerificationCols)
                {
                    wo.id = f[0].Get<uint64>();
                    wo.requesterGuid = f[1].Get<uint32>();
                    wo.requesterName = f[2].Get<std::string>();
                    wo.crafterGuid = f[3].Get<uint32>();
                    wo.crafterName = f[4].Get<std::string>();
                    wo.attemptId = f[5].Get<uint64>();
                    wo.itemEntry = f[6].Get<uint32>();
                    wo.itemName = f[7].Get<std::string>();
                    wo.quantity = f[8].Get<uint32>();
                    wo.producedQty = f[9].Get<uint32>();
                    wo.transferredQty = f[10].Get<uint32>();
                    wo.receivedQty = f[11].Get<uint32>();
                    wo.deliveryTab = f[12].Get<uint8>();
                    wo.story = f[13].Get<std::string>();
                    wo.status = f[14].Get<std::string>();
                    wo.revision = std::max<uint32>(1u, f[15].Get<uint32>());
                    wo.createdAt = f[16].Get<uint32>();
                    wo.updatedAt = f[17].Get<uint32>();
                }
                else
                {
                    wo.id = f[0].Get<uint64>();
                    wo.requesterGuid = f[1].Get<uint32>();
                    wo.requesterName = f[2].Get<std::string>();
                    wo.crafterGuid = f[3].Get<uint32>();
                    wo.crafterName = f[4].Get<std::string>();
                    wo.itemEntry = f[5].Get<uint32>();
                    wo.itemName = f[6].Get<std::string>();
                    wo.quantity = f[7].Get<uint32>();
                    wo.story = f[8].Get<std::string>();
                    wo.status = f[9].Get<std::string>();
                    wo.createdAt = f[10].Get<uint32>();
                    wo.updatedAt = f[11].Get<uint32>();
                }
                orders.push_back(std::move(wo));
            } while (res->NextRow());
        };
        loadQuery("`status` IN ('open', 'claimed')", "");
        loadQuery("`status` NOT IN ('open', 'claimed')", "LIMIT 25");
    }

    uint32 const now = NowSec();
    std::lock_guard<std::mutex> lock(gLifeMutex);
    // Preserve any in-memory higher-revision order progress whose async UPDATE may still be in flight.
    std::unordered_map<uint64, CompanionWorkOrderLogic::OrderState> prevById;
    for (CompanionWorkOrderLogic::OrderState const& prev : gOrderTracker.Orders())
        if (prev.id > 0)
            prevById[prev.id] = prev;
    for (CompanionWorkOrderLogic::OrderState& loaded : orders)
    {
        auto it = prevById.find(loaded.id);
        if (it != prevById.end() && it->second.revision > loaded.revision &&
            it->second.updatedAt + 60 >= now && loaded.status != "expired")
            loaded = it->second;
    }
    gProfiles.swap(profiles);
    gGuidByLowerName.swap(byName);
    gEvents.swap(events);
    gRelations.swap(rels);
    gBankTabRights.swap(bankRights);
    gOrderVerificationCols = hasVerificationCols;
    gOrderTracker.LoadOrders(std::move(orders), maxAttemptId);
}

void CompanionLife::UpdateWorld(uint32 elapsed)
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled)
        return;
    gRefreshTimerMs += elapsed;
    if (!gLoaded || gRefreshTimerMs >= REFRESH_INTERVAL_MS)
    {
        gRefreshTimerMs = 0;
        ReloadAll();
    }

    // Guild Life v2: autonomous story work orders between online guildmates (initial pulse at uptime >= 90s, then every 15m).
    gAutoOrderTimerMs += elapsed;
    bool const autoOrdersOn = sPlayerbotAIConfig.companionGuildLifeWorkOrders &&
                              sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.GuildLife.AutoWorkOrders", true, false);
    if (autoOrdersOn && GameTime::GetUptime().count() >= 90 &&
        (!gInitialOrderPulseDone || gAutoOrderTimerMs >= AUTO_ORDER_INTERVAL_MS))
    {
        gAutoOrderTimerMs = 0;
        gInitialOrderPulseDone = true;
        TriggerWorkOrderPulse();
    }
}

char const* CompanionLife::AvailabilityName(Availability a)
{
    switch (a)
    {
        case Availability::Available: return "AVAILABLE";
        case Availability::Busy: return "BUSY";
        case Availability::Committed: return "COMMITTED";
    }
    return "AVAILABLE";
}

CompanionLife::Availability CompanionLife::GetAvailability(Player* bot, std::string& detail)
{
    detail.clear();
    if (!bot || !bot->IsInWorld())
    {
        detail = "offline";
        return Availability::Committed;
    }
    if (!bot->IsAlive())
    {
        detail = "recovering my corpse";
        return Availability::Committed;
    }
    if (bot->GetMap() && bot->GetMap()->Instanceable())
    {
        detail = "in a dungeon run";
        return Availability::Committed;
    }
    if (bot->IsInCombat())
    {
        detail = "in a fight";
        return Availability::Committed;
    }
    if (bot->IsNonMeleeSpellCast(false))
    {
        detail = "casting";
        return Availability::Committed;
    }

    uint32 const low = bot->GetGUID().GetCounter();
    CompanionLease::Info const li = CompanionLease::Current(low);
    if (li.active)
    {
        if (li.owner.rfind("craft:", 0) == 0)
        {
            detail = "finishing a crafting session in " + ZoneNameOf(bot->GetZoneId());
            return Availability::Busy;
        }
        if (li.owner.rfind("gather:", 0) == 0)
        {
            detail = "gathering crafting mats in " + ZoneNameOf(bot->GetZoneId());
            return Availability::Busy;
        }
        if (li.owner.rfind("duty:", 0) == 0)
        {
            detail = "finishing a town errand (" + li.owner.substr(5) + ") in " + ZoneNameOf(bot->GetZoneId());
            return Availability::Busy;
        }
        if (li.owner.rfind("quest:", 0) == 0)
        {
            detail = "working on a quest in " + ZoneNameOf(bot->GetZoneId());
            return Availability::Busy;
        }
    }

    detail = "free in " + ZoneNameOf(bot->GetZoneId());
    return Availability::Available;
}

void CompanionLife::TickBot(PlayerbotAI* botAI)
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !botAI)
        return;
    Player* bot = botAI->GetBot();
    if (!bot || !bot->IsInWorld() || !bot->IsAlive())
        return;

    uint32 const low = bot->GetGUID().GetCounter();
    uint32 const now = NowSec();

    // 1. Contextual emote: sit at the inn when resting and idle.
    if (sPlayerbotAIConfig.companionGuildLifeEmotes)
    {
        bool const restingIdle = bot->HasFlag(PLAYER_FLAGS, PLAYER_FLAGS_RESTING) && !bot->IsInCombat() &&
                                 !bot->isMoving() && !bot->IsMounted() && !bot->IsNonMeleeSpellCast(false) &&
                                 !sCompanionBotMgr.IsGroupedWithOwner(bot);
        if (restingIdle)
        {
            std::string dummy;
            if (GetAvailability(bot, dummy) == Availability::Available)
            {
                bool shouldSit = false;
                {
                    std::lock_guard<std::mutex> lock(gLifeMutex);
                    auto& since = gInnIdleSinceSec[low];
                    if (since == 0)
                        since = now;
                    else if (now - since >= 20)
                        shouldSit = true;
                }
                if (shouldSit && bot->getStandState() == UNIT_STAND_STATE_STAND)
                    bot->SetStandState(UNIT_STAND_STATE_SIT);
            }
        }
        else
        {
            std::lock_guard<std::mutex> lock(gLifeMutex);
            gInnIdleSinceSec.erase(low);
        }
    }

    // 2. Activity proposals to online owner (Wonky or Logan): max 2 per session, 30-min cooldown, near owner's level.
    if (sPlayerbotAIConfig.companionGuildLifeProposals && !sCompanionBotMgr.IsGroupedWithOwner(bot) && !bot->IsInCombat())
    {
        Player* owner = sCompanionBotMgr.FindOwnerOnlineFor(bot);
        if (owner && owner->IsInWorld() && owner->IsAlive() && !owner->IsInCombat() &&
            (!owner->GetMap() || !owner->GetMap()->Instanceable()))
        {
            int32 const lvlDiff = std::abs(static_cast<int32>(bot->GetLevel()) - static_cast<int32>(owner->GetLevel()));
            if (lvlDiff <= 5)
            {
                std::string detail;
                if (GetAvailability(bot, detail) == Availability::Available)
                {
                    uint32 const ownerLow = owner->GetGUID().GetCounter();
                    bool sendNow = false;
                    {
                        std::lock_guard<std::mutex> lock(gLifeMutex);
                        OwnerProposalState& st = gOwnerProposals[ownerLow];
                        if (st.sessionStartSec == 0 || (st.lastProposalSec && now - st.lastProposalSec > OWNER_SESSION_RESET_SEC))
                        {
                            st.sessionStartSec = now;
                            st.proposalsSent = 0;
                        }
                        // Wait at least 5 minutes into owner's session before the first proposal.
                        if (st.proposalsSent < MAX_PROPOSALS_PER_SESSION && now >= st.cooldownUntilSec &&
                            now - st.sessionStartSec >= 300 &&
                            (st.lastProposalSec == 0 || now - st.lastProposalSec >= PROPOSAL_COOLDOWN_SEC))
                        {
                            st.lastProposalSec = now;
                            st.cooldownUntilSec = now + PROPOSAL_COOLDOWN_SEC;
                            ++st.proposalsSent;
                            st.lastProposerGuid = low;
                            sendNow = true;
                        }
                    }

                    if (sendNow)
                    {
                        std::string const zone = ZoneNameOf(owner->GetZoneId());
                        WeeklyEventKind const wk = CurrentWeeklyEvent();
                        std::string msg;
                        if (wk == WeeklyEventKind::SundayAdventure && owner->GetLevel() >= 15)
                            msg = "Happy Sunday Dungeon Day, " + owner->GetName() +
                                  "! Hearthguard is rallying for dungeons — want to group up? Say 'no' anytime!";
                        else if (wk == WeeklyEventKind::FishingNight)
                            msg = "Hey " + owner->GetName() +
                                  "! It's Hearthguard Fishing Night — want to cast a line or quest together in " +
                                  zone + "?";
                        else if (owner->GetLevel() <= 14)
                            msg = "Hi " + owner->GetName() + "! I am free right now. Want to do quests together in " +
                                  zone + "? Say 'no' if you are busy!";
                        else if (bot->GetLevel() >= 15 && (now % 3 == 0))
                            msg = "Hey " + owner->GetName() +
                                  "! I am free if you want to run a dungeon together. Say 'no' anytime!";
                        else if (now % 2 == 0)
                            msg = "Hey " + owner->GetName() + "! Want to go fishing or quest in " + zone +
                                  " together? No rush!";
                        else
                            msg = "Hey " + owner->GetName() + "! I am free in " + ZoneNameOf(bot->GetZoneId()) +
                                  " — want to team up? Just invite me or say 'no'!";

                        SendWhisper(bot, owner, msg);
                        LOG_INFO("playerbots", "[GuildLife] {} proposed activity to {} (owner lvl {}, bot lvl {})",
                                 bot->GetName(), owner->GetName(), owner->GetLevel(), bot->GetLevel());
                    }
                }
            }
        }
    }
}

void CompanionLife::OnLevelUp(Player* player)
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !player || !player->IsInWorld())
        return;

    uint32 const low = player->GetGUID().GetCounter();
    uint32 const lvl = player->GetLevel();
    std::string const zone = ZoneNameOf(player->GetZoneId());

    if (sCompanionBotMgr.IsCompanion(player))
    {
        RecordEventInternal(low, "level_up", "Reached level " + std::to_string(lvl) + " in " + zone, 0,
                            player->GetZoneId());
        MaybeBumpNearbyGuildBonds(player, "Leveled up together in " + zone);
    }

    // Contextual emote: nearby guild companions cheer when Wonky, Logan, or a guildmate levels up!
    if (!sPlayerbotAIConfig.companionGuildLifeEmotes)
        return;
    bool const isOwner = sCompanionBotMgr.IsOwner(player);
    bool const isGuildie = player->GetGuildId() != 0 && player->GetGuildId() == sCompanionBotMgr.OwnerGuildId();
    if (!isOwner && !isGuildie)
        return;

    uint32 const now = NowSec();
    for (std::string const& name : sCompanionBotMgr.CompanionNames())
    {
        ObjectGuid const guid = sCharacterCache->GetCharacterGuidByName(name);
        if (!guid || guid == player->GetGUID())
            continue;
        Player* comp = ObjectAccessor::FindConnectedPlayer(guid);
        if (!comp || !comp->IsInWorld() || !comp->IsAlive() || comp->IsInCombat())
            continue;
        if (comp->GetMapId() != player->GetMapId() || comp->GetDistance(player) > 40.0f)
            continue;

        bool canCheer = false;
        {
            std::lock_guard<std::mutex> lock(gLifeMutex);
            auto& last = gLastCheerSec[comp->GetGUID().GetCounter()];
            if (last == 0 || now - last >= CHEER_COOLDOWN_SEC)
            {
                last = now;
                canCheer = true;
            }
        }
        if (!canCheer)
            continue;

        comp->HandleEmoteCommand(EMOTE_ONESHOT_CHEER);
        if (isOwner)
            comp->Say("Way to go, " + player->GetName() + "! Level " + std::to_string(lvl) + "!", LANG_UNIVERSAL);
    }
}

void CompanionLife::OnQuestComplete(Player* player, Quest const* quest)
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !player || !quest || !sCompanionBotMgr.IsCompanion(player))
        return;
    std::string title = quest->GetTitle();
    if (title.size() > 80)
        title.resize(80);
    RecordEventInternal(player->GetGUID().GetCounter(), "quest_done", "Finished quest " + title, 0,
                        player->GetZoneId());
    MaybeBumpNearbyGuildBonds(player, "Questing together in " + ZoneNameOf(player->GetZoneId()));
}

void CompanionLife::OnSkillMilestone(Player* player, uint32 skillId, uint32 newValue)
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !player || !sCompanionBotMgr.IsCompanion(player))
        return;
    // Record meaningful milestones (every 25 points) so memory stays rich and crisp.
    if (newValue == 0 || (newValue % 25) != 0)
        return;
    RecordEventInternal(player->GetGUID().GetCounter(), "skill_up",
                        "Reached " + SkillNameOf(skillId) + " " + std::to_string(newValue), 0, player->GetZoneId());
}

bool CompanionLife::ClaimWorkOrder(uint64 orderId, Player* crafter, uint64& outAttemptId)
{
    outAttemptId = 0;
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !sPlayerbotAIConfig.companionGuildLifeWorkOrders ||
        !crafter || !sCompanionBotMgr.IsCompanion(crafter) || !orderId)
        return false;

    uint32 const low = crafter->GetGUID().GetCounter();
    uint32 const now = NowSec();
    Guild* guild = crafter->GetGuildId() ? sGuildMgr->GetGuildById(crafter->GetGuildId()) : nullptr;

    uint32 itemEntry = 0;
    uint32 requesterGuid = 0;
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        for (CompanionWorkOrderLogic::OrderState const& wo : gOrderTracker.Orders())
        {
            if (wo.id == orderId)
            {
                itemEntry = wo.itemEntry;
                requesterGuid = wo.requesterGuid;
                break;
            }
        }
    }
    ItemTemplate const* proto = itemEntry ? sObjectMgr->GetItemTemplate(itemEntry) : nullptr;
    bool const isTransferable = IsGuildBankTransferable(proto);
    bool const recipientOk =
        guild && requesterGuid && guild->GetMember(ObjectGuid::Create<HighGuid::Player>(requesterGuid)) != nullptr;

    CompanionWorkOrderLogic::ActionResult res;
    bool hasVerificationCols = false;
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        hasVerificationCols = gOrderVerificationCols;
        if (!WarnIfMissingVerificationSchema(hasVerificationCols))
            return false;
        res = gOrderTracker.ClaimOrder(orderId, low, crafter->GetName(), isTransferable, recipientOk, now);
    }
    if (!res.ok)
    {
        LOG_INFO("playerbots", "[GuildLife] Work order #{} claim refused for {}: reason={}", orderId,
                 crafter->GetName(), res.reason);
        return false;
    }
    outAttemptId = res.attemptId;
    if (res.stateChanged)
    {
        PersistWorkOrderTransitionSync(res, now);
        if (!ConfirmWorkOrderRowFromDb(res, low))
            return false;
        LOG_INFO("playerbots", "[GuildLife] Work order #{} claimed by {} attempt={} rev={}", res.orderId,
                 crafter->GetName(), res.attemptId, res.newRevision);
    }
    return true;
}

void CompanionLife::OnCraftMade(Player* bot, uint32 itemEntry, std::string const& itemName, uint32 skillId,
                                uint32 producedCount, uint64 callbackEventId)
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !bot || !sCompanionBotMgr.IsCompanion(bot))
        return;
    if (producedCount == 0)
        return;
    uint32 const low = bot->GetGUID().GetCounter();
    uint32 const now = NowSec();
    ItemTemplate const* proto = sObjectMgr->GetItemTemplate(itemEntry);
    std::string name = itemName;
    if (name.empty())
        name = proto ? proto->Name1 : ("item #" + std::to_string(itemEntry));

    // Rate-limit routine craft events per (bot, item) to 1 per 10 min.
    bool recordCraft = false;
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        uint64 const key = (static_cast<uint64>(low) << 32) | static_cast<uint64>(itemEntry);
        auto& last = gLastCraftEventSec[key];
        if (last == 0 || now - last >= CRAFT_EVENT_COOLDOWN_SEC)
        {
            last = now;
            recordCraft = true;
        }
    }
    if (recordCraft)
        RecordEventInternal(low, "craft_done", "Crafted " + name + " (" + SkillNameOf(skillId) + ")", 0,
                            bot->GetZoneId());

    // Track verified production against an eligible claimed/open guild work order (Issue #27).
    // Crafting into own bags is production ONLY — NEVER marks delivered, NEVER completes the proposal, and
    // NEVER bumps help/relationship memory until verified accessible guild-bank receipt occurs.
    if (!sPlayerbotAIConfig.companionGuildLifeWorkOrders)
        return;

    bool const itemIsTransferable = IsGuildBankTransferable(proto);
    Guild* guild = bot->GetGuildId() ? sGuildMgr->GetGuildById(bot->GetGuildId()) : nullptr;
    auto recipientInSameGuild = [guild](uint32 reqGuid) -> bool {
        return guild && reqGuid && guild->GetMember(ObjectGuid::Create<HighGuid::Player>(reqGuid)) != nullptr;
    };

    uint64 propId = 0;
    uint64 preferredOrderId = 0;
    uint32 propItemEntry = 0;
    bool const hasPropOrder = CompanionBrain::ActiveOrderProposal(low, propId, preferredOrderId, propItemEntry) &&
                              propItemEntry == itemEntry;

    CompanionWorkOrderLogic::ActionResult res;
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        if (!WarnIfMissingVerificationSchema(gOrderVerificationCols))
            return;
        res = gOrderTracker.RecordProduction(low, bot->GetName(), itemEntry, producedCount,
                                             hasPropOrder ? preferredOrderId : 0, itemIsTransferable,
                                             recipientInSameGuild, callbackEventId, now);
    }
    if (res.stateChanged)
    {
        PersistWorkOrderTransitionSync(res, now);
        if (ConfirmWorkOrderRowFromDb(res, low))
            LOG_INFO("playerbots",
                     "[GuildLife] Work order #{} production recorded: {} crafted {} +{} ({}/{}) attempt={} rev={} status={}",
                     res.orderId, bot->GetName(), name, res.creditedQty, res.order.producedQty, res.order.quantity,
                     res.attemptId, res.newRevision, res.reason);
    }
}

void CompanionLife::OnBankDepositVerified(Player* bot, uint32 itemEntry, uint32 transferredCount, uint32 receivedCount,
                                          uint8 bankTab, uint64 callbackEventId)
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !sPlayerbotAIConfig.companionGuildLifeWorkOrders || !bot ||
        !sCompanionBotMgr.IsCompanion(bot) || !itemEntry || transferredCount == 0)
        return;

    uint32 const low = bot->GetGUID().GetCounter();
    uint32 const now = NowSec();
    ItemTemplate const* proto = sObjectMgr->GetItemTemplate(itemEntry);
    bool const itemIsTransferable = IsGuildBankTransferable(proto);
    std::string const name = proto ? proto->Name1 : ("item #" + std::to_string(itemEntry));

    Guild* guild = bot->GetGuildId() ? sGuildMgr->GetGuildById(bot->GetGuildId()) : nullptr;
    auto recipientInSameGuild = [guild](uint32 reqGuid) -> bool {
        return guild && reqGuid && guild->GetMember(ObjectGuid::Create<HighGuid::Player>(reqGuid)) != nullptr;
    };
    auto recipientCanAccessTab = [guild](uint32 reqGuid, uint8 tab) -> bool {
        return HasAccessibleWithdrawRightsLocked(guild, reqGuid, tab);
    };

    CompanionWorkOrderLogic::ActionResult res;
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        if (!WarnIfMissingVerificationSchema(gOrderVerificationCols))
            return;
        res = gOrderTracker.RecordBankDelivery(low, itemEntry, transferredCount, receivedCount, bankTab,
                                               itemIsTransferable, recipientInSameGuild, recipientCanAccessTab,
                                               callbackEventId, now);
    }
    if (!res.ok)
    {
        if (res.reason != "no_claimed_order" && res.reason != "nothing_produced_to_deliver")
            LOG_WARN("playerbots",
                     "[GuildLife] Work order bank delivery not credited for {} item={} xfer={} recv={} tab={}: reason={}",
                     bot->GetName(), itemEntry, transferredCount, receivedCount, static_cast<uint32>(bankTab),
                     res.reason);
        return;
    }
    if (!res.stateChanged)
        return;

    // Persist synchronously and confirm the guarded CAS row in DB on BOTH partial and full delivery (Issue #27).
    PersistWorkOrderTransitionSync(res, now);
    if (!ConfirmWorkOrderRowFromDb(res, low))
        return;
    if (!res.completedNow)
    {
        LOG_INFO("playerbots",
                 "[GuildLife] Work order #{} partial bank delivery: {} deposited {} xfer=+{} recv=+{} (received {}/{}) tab={} rev={}",
                 res.orderId, bot->GetName(), name, res.creditedQty, res.creditedReceivedQty, res.order.receivedQty,
                 res.order.quantity, static_cast<uint32>(bankTab), res.newRevision);
        return;
    }

    // DB-verified full delivery into requester-accessible guild bank tab: complete proposal conditionally and record
    // authentic relationship/memory events strictly once (Issue #27).
    uint64 propId = 0;
    uint64 propOrderId = 0;
    uint32 propItemEntry = 0;
    uint32 propSnapshotRev = 0;
    if (CompanionBrain::ActiveOrderProposal(low, propId, propOrderId, propItemEntry, &propSnapshotRev) &&
        propId > 0 && propOrderId == res.orderId)
    {
        CompanionBrain::MarkProposalCompleted(low, propId, res.order.receivedQty, res.orderId, propSnapshotRev);
    }

    RecordEventInternal(low, "work_order",
                        "Delivered " + name + " x" + std::to_string(res.order.receivedQty) + " for " +
                            res.order.requesterName + " (guild work order)",
                        res.order.requesterGuid, bot->GetZoneId());
    RecordEventInternal(res.order.requesterGuid, "work_order",
                        bot->GetName() + " delivered " + name + " x" + std::to_string(res.order.receivedQty) +
                            " to guild bank for my work order",
                        low, bot->GetZoneId());
    BumpRelationInternal(low, res.order.requesterGuid, 0, 0, 1, 0, 5,
                         bot->GetName() + " delivered " + name + " x" + std::to_string(res.order.receivedQty) +
                             " for " + res.order.requesterName);
    LOG_INFO("playerbots",
             "[GuildLife] Work order #{} verified delivered: {} deposited {} x{} for {} in tab {} (attempt={} rev={})",
             res.orderId, bot->GetName(), name, res.order.receivedQty, res.order.requesterName,
             static_cast<uint32>(bankTab), res.attemptId, res.newRevision);
}

void CompanionLife::OnCreatureKill(Player* killer, Creature* killed)
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !killer || !killed)
        return;
    if (!killed->IsDungeonBoss() && !killed->isWorldBoss())
        return;

    Group* group = killer->GetGroup();
    std::vector<Player*> comps;
    if (group)
    {
        for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
        {
            Player* m = itr->GetSource();
            if (m && m->IsInWorld() && sCompanionBotMgr.IsCompanion(m) && m->GetMapId() == killed->GetMapId())
                comps.push_back(m);
        }
    }
    else if (sCompanionBotMgr.IsCompanion(killer))
        comps.push_back(killer);

    if (comps.empty())
        return;

    std::string const bossName = killed->GetName();
    for (Player* c : comps)
        RecordEventInternal(c->GetGUID().GetCounter(), "boss_kill", "Defeated " + bossName, 0, c->GetZoneId());

    for (size_t i = 0; i < comps.size(); ++i)
        for (size_t j = i + 1; j < comps.size(); ++j)
            BumpRelationInternal(comps[i]->GetGUID().GetCounter(), comps[j]->GetGUID().GetCounter(), 1, 0, 0, 0, 4,
                                 "Defeated " + bossName + " together");
}

void CompanionLife::OnPlayerDeath(Player* victim, Creature* killer)
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !victim || !sCompanionBotMgr.IsCompanion(victim))
        return;
    uint32 const low = victim->GetGUID().GetCounter();
    std::string const killerName = killer ? killer->GetName() : "a monster";
    RecordEventInternal(low, "death", "Fell to " + killerName + " in " + ZoneNameOf(victim->GetZoneId()), 0,
                        victim->GetZoneId());

    // If in an instance and another guild companion is also dead, record shared wipe hardship.
    Group* group = victim->GetGroup();
    if (group && victim->GetMap() && victim->GetMap()->Instanceable())
    {
        for (GroupReference* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
        {
            Player* m = itr->GetSource();
            if (m && m != victim && !m->IsAlive() && sCompanionBotMgr.IsCompanion(m))
                BumpRelationInternal(low, m->GetGUID().GetCounter(), 0, 1, 0, 0, 1,
                                     "Survived a tough wipe against " + killerName);
        }
    }
}

void CompanionLife::OnTextEmote(Player* bot, ObjectGuid senderGuid, uint32 textEmote)
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !sPlayerbotAIConfig.companionGuildLifeEmotes)
        return;
    if (!bot || !bot->IsInWorld() || !bot->IsAlive() || bot->IsInCombat() || !sCompanionBotMgr.IsCompanion(bot))
        return;

    Player* sender = ObjectAccessor::FindConnectedPlayer(senderGuid);
    if (!sender || sender == bot || !sCompanionBotMgr.IsOwner(sender))
        return;
    if (bot->GetMapId() != sender->GetMapId() || bot->GetDistance(sender) > 25.0f)
        return;

    uint32 replyEmote = 0;
    std::string greeting;
    switch (textEmote)
    {
        case TEXT_EMOTE_WAVE:
        case TEXT_EMOTE_HELLO:
            replyEmote = EMOTE_ONESHOT_WAVE;
            greeting = "Hi " + sender->GetName() + "!";
            break;
        case TEXT_EMOTE_SALUTE:
            replyEmote = EMOTE_ONESHOT_SALUTE;
            greeting = "Ready when you are, " + sender->GetName() + "!";
            break;
        case TEXT_EMOTE_CHEER:
        case TEXT_EMOTE_APPLAUD:
        case TEXT_EMOTE_CONGRATULATE:
            replyEmote = EMOTE_ONESHOT_CHEER;
            greeting = "Thanks, " + sender->GetName() + "!";
            break;
        case TEXT_EMOTE_HUG:
        case TEXT_EMOTE_BOW:
        case TEXT_EMOTE_THANK:
            replyEmote = EMOTE_ONESHOT_BOW;
            greeting = "Good to see you, " + sender->GetName() + "!";
            break;
        default:
            return;
    }

    uint32 const low = bot->GetGUID().GetCounter();
    uint32 const now = NowSec();
    bool canSay = false;
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        auto& last = gLastEmoteSec[low];
        if (last == 0 || now - last >= EMOTE_COOLDOWN_SEC)
        {
            last = now;
            canSay = true;
        }
    }

    bot->HandleEmoteCommand(replyEmote);
    if (canSay)
    {
        std::string const ev = LatestEventSummary(low);
        if (!ev.empty())
            bot->Say(greeting + " Earlier I " + LowerStr(ev.substr(0, 1)) + ev.substr(1) + ".", LANG_UNIVERSAL);
        else
            bot->Say(greeting, LANG_UNIVERSAL);
    }
}

bool CompanionLife::HandleOwnerWhisper(PlayerbotAI* botAI, Player* sender, std::string const& message)
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !botAI || !sender)
        return false;
    Player* bot = botAI->GetBot();
    if (!bot || !sCompanionBotMgr.IsCompanion(bot) || !sCompanionBotMgr.IsOwner(sender))
        return false;

    std::string const lower = LowerStr(TrimStr(message));
    if (lower == "no" || lower == "later" || lower == "not now")
    {
        uint32 const ownerLow = sender->GetGUID().GetCounter();
        uint32 const now = NowSec();
        bool wasProposer = false;
        {
            std::lock_guard<std::mutex> lock(gLifeMutex);
            auto it = gOwnerProposals.find(ownerLow);
            if (it != gOwnerProposals.end() && it->second.lastProposerGuid == bot->GetGUID().GetCounter() &&
                now - it->second.lastProposalSec < 600)
            {
                it->second.cooldownUntilSec = now + PROPOSAL_DECLINE_COOLDOWN_SEC;
                wasProposer = true;
            }
        }
        if (wasProposer)
        {
            SendWhisper(bot, sender, "No problem at all! Have fun and ping me anytime.");
            return true;
        }
        return false;
    }

    if (lower == "busy" || lower == "available" || lower == "free" || lower == "what are you doing")
    {
        std::string detail;
        Availability const av = GetAvailability(bot, detail);
        std::string reply = "I am " + std::string(AvailabilityName(av)) + " — " + detail + ".";
        if (av == Availability::Busy)
            reply += " Invite me or say 'summon' if you need me right now!";
        SendWhisper(bot, sender, reply);
        return true;
    }

    if (lower == "news" || lower == "story" || lower == "events")
    {
        uint32 const low = bot->GetGUID().GetCounter();
        std::string arch;
        std::string ev;
        {
            std::lock_guard<std::mutex> lock(gLifeMutex);
            auto pit = gProfiles.find(low);
            if (pit != gProfiles.end())
                arch = pit->second.archetype;
            auto eit = gEvents.find(low);
            if (eit != gEvents.end() && !eit->second.empty())
                ev = eit->second.front().summary;
        }
        std::string detail;
        Availability const av = GetAvailability(bot, detail);
        std::string reply = "[" + (arch.empty() ? std::string("Guildmate") : arch) + " | " +
                            std::string(AvailabilityName(av)) + "] Currently " + detail + ".";
        if (!ev.empty())
            reply += " Latest memory: " + ev + ".";
        else
            reply += " Just getting started on my adventures!";
        SendWhisper(bot, sender, reply);
        return true;
    }

    return false;
}

bool CompanionLife::CreateWorkOrder(uint32 requesterGuid, std::string const& requesterName, uint32 itemEntry,
                                    uint32 quantity, std::string const& story)
{
    if (!requesterGuid || !itemEntry || !quantity)
        return false;
    ItemTemplate const* proto = sObjectMgr->GetItemTemplate(itemEntry);
    if (!IsGuildBankTransferable(proto))
        return false;   // soulbound/quest/conjured items cannot be transferred via guild work orders (Issue #27)
    std::string const itemName = proto->Name1;
    std::string finalStory = story;
    if (finalStory.empty())
        finalStory = requesterName + " needs " + itemName + " x" + std::to_string(quantity) + " for leveling up.";

    // Serialize duplicate-check + INSERT + ID lookup so concurrent callers cannot insert duplicate rows or share an ID.
    std::lock_guard<std::mutex> createLock(gCreateOrderMutex);
    uint32 const now = NowSec();
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        for (CompanionWorkOrderLogic::OrderState const& wo : gOrderTracker.Orders())
            if ((wo.status == "open" || wo.status == "claimed") && wo.requesterGuid == requesterGuid &&
                wo.itemEntry == itemEntry)
                return false;   // already open
    }

    // Synchronous insert on world/GM thread so the order immediately carries a stable DB id > 0 (Issue #27).
    CharacterDatabase.DirectExecute(Acore::StringFormat(
        "INSERT INTO companion_work_orders (`requester_guid`, `requester_name`, `crafter_guid`, `crafter_name`, "
        "`item_entry`, `item_name`, `quantity`, `story`, `status`, `created_at`, `updated_at`) "
        "VALUES ({}, '{}', 0, '', {}, '{}', {}, '{}', 'open', {}, {})",
        requesterGuid, SqlEsc(requesterName), itemEntry, SqlEsc(itemName), quantity, SqlEsc(finalStory), now, now));

    uint64 insertedId = 0;
    if (QueryResult idRes = CharacterDatabase.Query(
            "SELECT `id` FROM `companion_work_orders` WHERE `requester_guid` = {} AND `item_entry` = {} "
            "AND `status` = 'open' ORDER BY `id` DESC LIMIT 1",
            requesterGuid, itemEntry))
        insertedId = idRes->Fetch()[0].Get<uint64>();
    if (insertedId == 0)
        return false;

    CompanionWorkOrderLogic::OrderState wo;
    wo.id = insertedId;
    wo.requesterGuid = requesterGuid;
    wo.requesterName = requesterName;
    wo.itemEntry = itemEntry;
    wo.itemName = itemName;
    wo.quantity = quantity;
    wo.story = finalStory;
    wo.status = "open";
    wo.revision = 1;
    wo.createdAt = now;
    wo.updatedAt = now;
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        auto& orders = gOrderTracker.OrdersMutable();
        orders.insert(orders.begin(), std::move(wo));
    }
    LOG_INFO("playerbots", "[GuildLife] New work order #{}: {}", insertedId, finalStory);
    return true;
}

bool CompanionLife::HasOpenWorkOrderFor(uint32 itemEntry, uint32 crafterGuid)
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !sPlayerbotAIConfig.companionGuildLifeWorkOrders || !itemEntry)
        return false;
    std::lock_guard<std::mutex> lock(gLifeMutex);
    return gOrderTracker.HasOpenOrderFor(itemEntry, crafterGuid);
}

bool CompanionLife::HasOpenWorkOrderById(uint64 orderId, uint32 crafterGuid, uint32& outItemEntry)
{
    outItemEntry = 0;
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !sPlayerbotAIConfig.companionGuildLifeWorkOrders || !orderId)
        return false;
    std::lock_guard<std::mutex> lock(gLifeMutex);
    return gOrderTracker.HasOpenOrderById(orderId, crafterGuid, outItemEntry);
}

bool CompanionLife::HasPendingDeliveryFor(uint32 crafterGuid, uint32 itemEntry)
{
    return PendingDeliveryQty(crafterGuid, itemEntry) > 0;
}

uint32 CompanionLife::PendingDeliveryQty(uint32 crafterGuid, uint32 itemEntry)
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !sPlayerbotAIConfig.companionGuildLifeWorkOrders ||
        !crafterGuid || !itemEntry)
        return 0;
    std::lock_guard<std::mutex> lock(gLifeMutex);
    return gOrderTracker.PendingDeliveryQty(crafterGuid, itemEntry);
}

uint32 CompanionLife::ReservedBankQty(uint32 itemEntry, uint32 excludeRequesterGuid)
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !sPlayerbotAIConfig.companionGuildLifeWorkOrders || !itemEntry)
        return 0;
    uint32 const now = NowSec();
    std::lock_guard<std::mutex> lock(gLifeMutex);
    return gOrderTracker.ReservedBankQty(itemEntry, now, excludeRequesterGuid);
}

bool CompanionLife::IsGuildBankTransferable(ItemTemplate const* proto)
{
    if (!proto)
        return false;
    if (proto->Bonding == BIND_WHEN_PICKED_UP || proto->Bonding == BIND_QUEST_ITEM)
        return false;
    if ((proto->Flags & ITEM_FLAG_CONJURED) != 0 || proto->Class == ITEM_CLASS_QUEST)
        return false;
    return true;
}

bool CompanionLife::RequesterCanAccessBankTab(Guild const* guild, uint32 crafterGuid, uint32 itemEntry, uint8 bankTab)
{
    if (!guild || !crafterGuid || !itemEntry || bankTab == CompanionWorkOrderLogic::TAB_NONE)
        return false;
    std::lock_guard<std::mutex> lock(gLifeMutex);
    uint32 requesterGuid = 0;
    if (!gOrderTracker.HasPendingDeliveryFor(crafterGuid, itemEntry, &requesterGuid) || !requesterGuid)
        return false;
    return HasAccessibleWithdrawRightsLocked(guild, requesterGuid, bankTab);
}

float CompanionLife::NeighborhoodHubBonus(Player* bot, uint32 hubZoneId)
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !bot || !hubZoneId)
        return 0.0f;
    if (!sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.GuildLife.Neighborhood.Enabled", true, false))
        return 0.0f;

    Player* owner = sCompanionBotMgr.FindOwnerOnlineFor(bot);
    if (!owner || !owner->IsInWorld() || (owner->GetMap() && owner->GetMap()->Instanceable()))
        return 0.0f;
    if (owner->GetZoneId() != hubZoneId)
        return 0.0f;

    int32 const lvlDiff = std::abs(static_cast<int32>(bot->GetLevel()) - static_cast<int32>(owner->GetLevel()));
    if (lvlDiff > 5)
        return 0.0f;

    uint32 budget = sConfigMgr->GetOption<uint32>("AiPlayerbot.Companion.GuildLife.Neighborhood.Budget", 4, false);
    if (CurrentWeeklyEvent() == WeeklyEventKind::SundayAdventure)
        budget += 2;

    uint32 othersInZone = 0;
    for (std::string const& name : sCompanionBotMgr.CompanionNames())
    {
        ObjectGuid const og = sCharacterCache->GetCharacterGuidByName(name);
        if (!og || og == bot->GetGUID())
            continue;
        Player* comp = ObjectAccessor::FindConnectedPlayer(og);
        if (comp && comp->IsInWorld() && comp->GetZoneId() == hubZoneId)
            ++othersInZone;
    }
    if (othersInZone >= budget)
        return 0.0f;
    return bot->GetZoneId() == hubZoneId ? 14.0f : 28.0f;
}

float CompanionLife::WeeklyFishingBonus()
{
    return CurrentWeeklyEvent() == WeeklyEventKind::FishingNight ? 35.0f : 0.0f;
}

float CompanionLife::WeeklyCraftBonus()
{
    return CurrentWeeklyEvent() == WeeklyEventKind::CraftNight ? 25.0f : 0.0f;
}

std::string CompanionLife::ActiveWeeklyEventLabel()
{
    switch (CurrentWeeklyEvent())
    {
        case WeeklyEventKind::CraftNight:      return "Craft & Supply Night (Wed 6-10 PM)";
        case WeeklyEventKind::FishingNight:    return "Hearthguard Fishing Night (Fri 6-10 PM)";
        case WeeklyEventKind::SundayAdventure: return "Sunday Dungeon & Adventure Day (Sun 1-9 PM)";
        default:                               return "";
    }
}

uint32 CompanionLife::TriggerWorkOrderPulse()
{
    if (!sPlayerbotAIConfig.companionGuildLifeEnabled || !sPlayerbotAIConfig.companionGuildLifeWorkOrders)
        return 0;

    uint32 const targetOpen = CurrentWeeklyEvent() == WeeklyEventKind::CraftNight ? 7u : 4u;
    uint32 openCount = 0;
    std::unordered_set<uint32> openRequesters;
    std::unordered_set<uint32> openItems;
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        for (CompanionWorkOrderLogic::OrderState const& wo : gOrderTracker.Orders())
        {
            if (wo.status == "open" || wo.status == "claimed")
            {
                ++openCount;
                openRequesters.insert(wo.requesterGuid);
                openItems.insert(wo.itemEntry);
            }
        }
    }
    if (openCount >= targetOpen)
        return 0;

    // Collect online companions & which crafting skills exist among currently online guildmates.
    std::vector<Player*> onlineComps;
    std::unordered_set<uint32> onlineSkills;
    for (std::string const& name : sCompanionBotMgr.CompanionNames())
    {
        ObjectGuid const og = sCharacterCache->GetCharacterGuidByName(name);
        if (!og)
            continue;
        Player* p = ObjectAccessor::FindConnectedPlayer(og);
        if (!p || !p->IsInWorld())
            continue;
        onlineComps.push_back(p);
        for (uint32 sk : {uint32(SKILL_ALCHEMY), uint32(SKILL_BLACKSMITHING), uint32(SKILL_MINING),
                          uint32(SKILL_TAILORING), uint32(SKILL_LEATHERWORKING), uint32(SKILL_FIRST_AID),
                          uint32(SKILL_COOKING)})
        {
            if (p->HasSkill(sk))
                onlineSkills.insert(sk);
        }
    }
    if (onlineComps.size() < 2)
        return 0;

    uint32 const now = NowSec();
    size_t const startIdx = now % onlineComps.size();
    size_t const catSize = sizeof(gAutoOrderCatalog) / sizeof(gAutoOrderCatalog[0]);
    uint32 created = 0;

    for (size_t step = 0; step < onlineComps.size() && openCount < targetOpen; ++step)
    {
        Player* req = onlineComps[(startIdx + step) % onlineComps.size()];
        uint32 const reqGuid = req->GetGUID().GetCounter();
        if (openRequesters.count(reqGuid))
            continue;

        uint8 const lvl = req->GetLevel();
        bool const usesMana = req->getPowerType() == POWER_MANA;
        bool const isMelee = req->getClass() == CLASS_WARRIOR || req->getClass() == CLASS_ROGUE ||
                             req->getClass() == CLASS_PALADIN || req->getClass() == CLASS_DEATH_KNIGHT;

        // Pick a craftable item whose crafter skill is present on ANOTHER online guildie.
        size_t const cStart = (now + reqGuid) % catSize;
        for (size_t ci = 0; ci < catSize; ++ci)
        {
            AutoOrderTemplate const& tpl = gAutoOrderCatalog[(cStart + ci) % catSize];
            if (lvl < tpl.minLvl || lvl > tpl.maxLvl)
                continue;
            if (tpl.manaOnly && !usesMana)
                continue;
            if (tpl.meleeOnly && !isMelee)
                continue;
            if (openItems.count(tpl.itemEntry))
                continue;
            if (!onlineSkills.count(tpl.crafterSkill))
                continue;
            // Ensure at least one *other* online guildie holds that skill (not self-ordering).
            bool otherCanCraft = false;
            for (Player* p : onlineComps)
                if (p != req && p->HasSkill(tpl.crafterSkill))
                {
                    otherCanCraft = true;
                    break;
                }
            if (!otherCanCraft)
                continue;

            std::string const story = req->GetName() + " " + std::string(tpl.storyFmt) + ".";
            if (CreateWorkOrder(reqGuid, req->GetName(), tpl.itemEntry, tpl.qty, story))
            {
                openRequesters.insert(reqGuid);
                openItems.insert(tpl.itemEntry);
                ++openCount;
                ++created;
                break;
            }
        }
    }
    return created;
}

std::vector<std::string> CompanionLife::WeeklyEventLines()
{
    std::vector<std::string> out;
    std::string const active = ActiveWeeklyEventLabel();
    out.push_back("Hearthguard Weekly Calendar (server local time):");
    out.push_back("  Active now: " + (active.empty() ? std::string("None (normal guild shifts)") : active));
    out.push_back("  - Wed 18:00-22:00: Craft & Supply Night (+25 craft rank, 7 active work orders)");
    out.push_back("  - Fri 18:00-22:00: Hearthguard Fishing Night (+35 fishing priority)");
    out.push_back("  - Sun 13:00-21:00: Sunday Dungeon & Adventure Day (+2 neighbourhood budget, dungeon invites)");
    return out;
}

std::vector<std::string> CompanionLife::StatusLines(std::string const& botNameFilter)
{
    std::vector<std::string> out;
    std::string const filter = LowerStr(TrimStr(botNameFilter));

    if (!filter.empty())
    {
        uint32 guid = 0;
        Profile prof;
        std::deque<EventRecord> evs;
        {
            std::lock_guard<std::mutex> lock(gLifeMutex);
            guid = FindGuidByNameLocked(filter);
            auto pit = gProfiles.find(guid);
            if (pit != gProfiles.end())
                prof = pit->second;
            auto eit = gEvents.find(guid);
            if (eit != gEvents.end())
                evs = eit->second;
        }
        if (!guid)
        {
            out.push_back("No Guild Life profile found for '" + botNameFilter + "'.");
            return out;
        }
        Player* online = ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(guid));
        std::string detail;
        Availability const av = GetAvailability(online, detail);
        out.push_back(prof.name + " [" + prof.archetype + " / " + prof.culture + "] — " +
                      std::string(AvailabilityName(av)) + " (" + detail + ")");
        if (!prof.flaw.empty())
            out.push_back("  Quirk: " + prof.flaw + " | Bio: " + prof.bio);
        out.push_back("  Recent real events (" + std::to_string(evs.size()) + "):");
        size_t shown = 0;
        for (EventRecord const& e : evs)
        {
            if (shown++ >= 5)
                break;
            out.push_back("    - [" + e.type + "] " + e.summary);
        }
        return out;
    }

    uint32 avail = 0, busy = 0, comm = 0, offline = 0;
    size_t profCount = 0, eventCount = 0, relCount = 0, openOrders = 0;
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        profCount = gProfiles.size();
        for (auto const& [g, q] : gEvents)
            eventCount += q.size();
        relCount = gRelations.size();
        for (CompanionWorkOrderLogic::OrderState const& wo : gOrderTracker.Orders())
            if (wo.status == "open" || wo.status == "claimed")
                ++openOrders;
    }
    for (std::string const& name : sCompanionBotMgr.CompanionNames())
    {
        ObjectGuid const og = sCharacterCache->GetCharacterGuidByName(name);
        Player* p = og ? ObjectAccessor::FindConnectedPlayer(og) : nullptr;
        if (!p)
        {
            ++offline;
            continue;
        }
        std::string d;
        switch (GetAvailability(p, d))
        {
            case Availability::Available: ++avail; break;
            case Availability::Busy: ++busy; break;
            case Availability::Committed: ++comm; break;
        }
    }
    std::string const wk = ActiveWeeklyEventLabel();
    out.push_back("Guild Life v2: enabled=" + std::string(sPlayerbotAIConfig.companionGuildLifeEnabled ? "1" : "0") +
                  " profiles=" + std::to_string(profCount) + " events=" + std::to_string(eventCount) +
                  " relations=" + std::to_string(relCount) + " open_orders=" + std::to_string(openOrders));
    out.push_back("Availability: AVAILABLE=" + std::to_string(avail) + " BUSY=" + std::to_string(busy) +
                  " COMMITTED=" + std::to_string(comm) + " offline=" + std::to_string(offline));
    out.push_back("Weekly Event: " + (wk.empty() ? std::string("None (Wed Craft / Fri Fishing / Sun Dungeon)") : wk));
    return out;
}

std::vector<std::string> CompanionLife::EventLines(std::string const& botName)
{
    std::vector<std::string> out;
    uint32 guid = 0;
    std::deque<EventRecord> evs;
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        guid = FindGuidByNameLocked(botName);
        if (guid)
        {
            auto it = gEvents.find(guid);
            if (it != gEvents.end())
                evs = it->second;
        }
    }
    if (!guid)
    {
        out.push_back("Unknown companion '" + botName + "'.");
        return out;
    }
    out.push_back("Event memory for " + NameOfGuid(guid) + " (" + std::to_string(evs.size()) + " events):");
    for (EventRecord const& e : evs)
        out.push_back("  - [" + e.type + "] " + e.summary);
    return out;
}

std::vector<std::string> CompanionLife::RelationLines(std::string const& botNameFilter)
{
    std::vector<std::string> out;
    uint32 filterGuid = 0;
    std::vector<std::pair<uint64, Relation>> copy;
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        if (!botNameFilter.empty())
            filterGuid = FindGuidByNameLocked(botNameFilter);
        for (auto const& kv : gRelations)
        {
            uint32 const a = static_cast<uint32>(kv.first >> 32);
            uint32 const b = static_cast<uint32>(kv.first & 0xFFFFFFFFu);
            if (filterGuid && a != filterGuid && b != filterGuid)
                continue;
            copy.push_back(kv);
        }
    }
    std::sort(copy.begin(), copy.end(),
              [](auto const& x, auto const& y) { return x.second.affinity > y.second.affinity; });
    if (copy.empty())
    {
        out.push_back("No pair relationships recorded yet (dungeon bosses, wipes, and work orders populate them).");
        return out;
    }
    out.push_back("Companion pair relationships (" + std::to_string(copy.size()) + "):");
    size_t shown = 0;
    for (auto const& [k, r] : copy)
    {
        if (shown++ >= 15)
            break;
        uint32 const a = static_cast<uint32>(k >> 32);
        uint32 const b = static_cast<uint32>(k & 0xFFFFFFFFu);
        out.push_back("  " + NameOfGuid(a) + " <-> " + NameOfGuid(b) + " | affinity=" + std::to_string(r.affinity) +
                      " runs=" + std::to_string(r.runsTogether) + " help=" + std::to_string(r.helpGiven) +
                      " wipes=" + std::to_string(r.wipes) + " (" + r.lastEvent + ")");
    }
    return out;
}

std::vector<std::string> CompanionLife::WorkOrderLines()
{
    std::vector<std::string> out;
    std::vector<WorkOrder> copy;
    {
        std::lock_guard<std::mutex> lock(gLifeMutex);
        copy.reserve(gOrderTracker.Orders().size());
        for (CompanionWorkOrderLogic::OrderState const& wo : gOrderTracker.Orders())
            copy.push_back(ToPublicOrder(wo));
    }
    if (copy.empty())
    {
        out.push_back("No guild work orders yet. Use '.companion life order <requester> <itemId> [qty]' to create one.");
        return out;
    }
    out.push_back("Guild work orders (" + std::to_string(copy.size()) + "):");
    size_t shown = 0;
    for (WorkOrder const& wo : copy)
    {
        if (shown++ >= 15)
            break;
        out.push_back("  #" + std::to_string(wo.id) + " [" + wo.status + " " + std::to_string(wo.producedQty) + "/" +
                      std::to_string(wo.receivedQty) + "/" + std::to_string(wo.quantity) + "] " + wo.story +
                      (wo.crafterName.empty() ? "" : " (by " + wo.crafterName + ")"));
    }
    return out;
}
