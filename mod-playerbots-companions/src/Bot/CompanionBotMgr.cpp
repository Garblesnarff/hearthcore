/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#include "CompanionBotMgr.h"

#include "CompanionBrain.h"
#include "CompanionCraft.h"
#include "CompanionClassStrategy.h"
#include "CompanionGatherStrategy.h"
#include "CompanionDuty.h"
#include "CompanionLease.h"
#include "CompanionLife.h"
#include "CompanionQuest.h"
#include "CompanionTransport.h"
#include "CompanionTrip.h"

#include "GameGraveyard.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <ctime>
#include <iomanip>
#include <iterator>
#include <sstream>

#include "CharacterCache.h"
#include "Creature.h"
#include "DBCStores.h"
#include "GameObject.h"
#include "GameObjectData.h"
#include "Map.h"
#include "MapMgr.h"
#include "ObjectMgr.h"
#include "QuestDef.h"
#include "Config.h"
#include "CharacterDatabase.h"
#include "DatabaseEnv.h"
#include "Group.h"
#include "Bag.h"
#include "Item.h"
#include "Chat.h"
#include "WorldPacket.h"
#include "ItemTemplate.h"
#include "GameTime.h"
#include "Log.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "RandomPlayerbotMgr.h"
#include "SharedDefines.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include "WorldDatabase.h"

namespace
{
// Maps companions travel on: zone level ranges and duty spawns are read for these only.
constexpr uint32 COMPANION_MAPS[] = {0, 1, 530, 571};

std::string Lower(std::string value)
{
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) { return std::tolower(c); });
    return value;
}

std::string Trim(std::string const& value)
{
    size_t first = value.find_first_not_of(" \t\"");
    if (first == std::string::npos)
        return "";
    size_t last = value.find_last_not_of(" \t\"");
    return value.substr(first, last - first + 1);
}
}  // namespace

CompanionBotMgr& CompanionBotMgr::instance()
{
    static CompanionBotMgr instance;
    return instance;
}

void CompanionBotMgr::LoadConfig()
{
    std::string const& raw = sPlayerbotAIConfig.companionBots;
    size_t start = 0;
    while (start <= raw.size())
    {
        size_t comma = raw.find(',', start);
        std::string token = Trim(raw.substr(start, comma == std::string::npos ? std::string::npos : comma - start));
        if (!token.empty())
        {
            _displayNames.push_back(token);
            _names.insert(Lower(token));
        }
        if (comma == std::string::npos)
            break;
        start = comma + 1;
    }

    _ownerName = Trim(sPlayerbotAIConfig.companionOwner);
    _ownerConfigured.store(!_ownerName.empty());
    _xpWaitForOwner.store(sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.XpCap.WaitForOwnerLevel", true));
    if (!_ownerName.empty())
    {
        ObjectGuid ownerGuid = sCharacterCache->GetCharacterGuidByName(_ownerName);
        if (ownerGuid)
        {
            _ownerLow.store(ownerGuid.GetCounter());
            _ownerGuildId.store(sCharacterCache->GetCharacterGuildIdByGuid(ownerGuid));
            _allOwnerLows.insert(ownerGuid.GetCounter());
        }
        else
            LOG_WARN("playerbots", "Companion owner '{}' was not found in the character cache", _ownerName);
    }

    // Per-companion owner override: AiPlayerbot.Companion.Owner.<Name> = "Logan" (defaults to CompanionOwner).
    for (std::string const& display : _displayNames)
    {
        std::string customOwner = Trim(sConfigMgr->GetOption<std::string>("AiPlayerbot.Companion.Owner." + display, "", false));
        if (customOwner.empty())
            continue;
        std::string const lowerBot = Lower(display);
        _botOwnerName[lowerBot] = customOwner;
        if (ObjectGuid og = sCharacterCache->GetCharacterGuidByName(customOwner))
        {
            _botOwnerLow[lowerBot] = og.GetCounter();
            _allOwnerLows.insert(og.GetCounter());
        }
    }

    // Per-companion armour types: AiPlayerbot.Companion.ArmorTypes.<Name> = "best" or a list like "leather,best".
    for (std::string const& display : _displayNames)
    {
        std::string value = sConfigMgr->GetOption<std::string>("AiPlayerbot.Companion.ArmorTypes." + display, "best");
        std::vector<std::string> tokens;
        size_t at = 0;
        while (at <= value.size())
        {
            size_t comma = value.find(',', at);
            std::string tok = Lower(Trim(value.substr(at, comma == std::string::npos ? std::string::npos : comma - at)));
            if (!tok.empty())
                tokens.push_back(tok);
            if (comma == std::string::npos)
                break;
            at = comma + 1;
        }
        if (tokens.empty())
            tokens.push_back("best");
        _armorTypes[Lower(display)] = tokens;
    }

    // B10: read the owner's level now (world thread, DB when he is offline), not one refresh interval later; until it
    // is known the XP cap stays closed (IsXpCapped).
    RefreshOwnerLevel();

    LOG_INFO("playerbots", "Companion bots: {} configured, owner '{}', level lead {}", _displayNames.size(),
             _ownerName, sPlayerbotAIConfig.companionLevelLead);
    _configLoaded.store(true);
}

void CompanionBotMgr::LoadClassQuests()
{
    // Start-up read on the world thread. Class quests are quests whose AllowableClasses names one class and that
    // reward a spell. MinLevel up to 80 covers every class quest a leveling companion can reach.
    _classQuestsLoaded = true;
    QueryResult result = WorldDatabase.Query(
        "SELECT a.AllowableClasses, q.ID, q.MinLevel, q.RewardSpell FROM quest_template q "
        "JOIN quest_template_addon a ON a.ID = q.ID "
        "WHERE q.RewardSpell > 0 AND a.AllowableClasses > 0 AND q.MinLevel <= 80 "
        "ORDER BY q.MinLevel, q.ID");
    if (!result)
    {
        LOG_WARN("playerbots", "Companion bots: no class quests found");
        return;
    }
    do
    {
        Field* fields = result->Fetch();
        ClassQuest entry;
        entry.classMask = fields[0].Get<uint32>();
        entry.questId = fields[1].Get<uint32>();
        entry.minLevel = fields[2].Get<uint32>();
        entry.rewardSpell = fields[3].Get<uint32>();
        _classQuests.push_back(entry);
    } while (result->NextRow());
    LOG_INFO("playerbots", "Companion bots: {} class quests loaded", _classQuests.size());
}

namespace
{
uint64 CoarseZoneKey(uint32 mapId, int32 cx, int32 cy)
{
    return (static_cast<uint64>(mapId & 0xFFFFu) << 40) |
           (static_cast<uint64>(static_cast<uint32>(cx + 32768) & 0xFFFFFu) << 20) |
           static_cast<uint64>(static_cast<uint32>(cy + 32768) & 0xFFFFFu);
}

int32 CoarseCoord(float v)
{
    return static_cast<int32>(std::floor(v / 128.0f));
}
}  // namespace

uint32 CompanionBotMgr::ZoneOf(uint32 mapId, float x, float y, float z)
{
    Map* map = sMapMgr->FindBaseMap(mapId);
    return map ? map->GetZoneId(PHASEMASK_NORMAL, x, y, z) : 0;
}

bool CompanionBotMgr::BuildZoneLevels()
{
    // Mob level range per zone, from hostile rank-0 creature spawns with loot that are not critters, on the maps
    // companions travel. Range = 10th..90th percentile of the template's (min + max) / 2. A zone with fewer than
    // 20 samples (cities, one-off areas) gets no range, so it fits any level.
    static constexpr size_t MIN_SAMPLES = 20;
    constexpr uint32 HOSTILE_BITS = 2 | 4;   // FactionTemplate hostileMask bits for Alliance and Horde

    std::unordered_map<uint32, std::vector<uint32>> samples;
    for (auto const& [guid, data] : sObjectMgr->GetAllCreatureData())
    {
        if (std::find(std::begin(COMPANION_MAPS), std::end(COMPANION_MAPS), static_cast<uint32>(data.mapid)) ==
            std::end(COMPANION_MAPS))
            continue;
        uint32 zone = ZoneOf(data.mapid, data.posX, data.posY, data.posZ);
        if (!zone)
            continue;
        _coarseZoneGrid.emplace(CoarseZoneKey(data.mapid, CoarseCoord(data.posX), CoarseCoord(data.posY)), zone);
        CreatureTemplate const* cInfo = sObjectMgr->GetCreatureTemplate(data.id);
        if (!cInfo || cInfo->rank != 0 || cInfo->lootid == 0 || cInfo->type == CREATURE_TYPE_CRITTER)
            continue;
        FactionTemplateEntry const* faction = sFactionTemplateStore.LookupEntry(cInfo->faction);
        if (!faction || !(faction->hostileMask & HOSTILE_BITS))
            continue;
        samples[zone].push_back((static_cast<uint32>(cInfo->minlevel) + cInfo->maxlevel) / 2);
    }

    size_t ranged = 0;
    for (auto& [zone, levels] : samples)
    {
        if (levels.size() < MIN_SAMPLES)
            continue;
        std::sort(levels.begin(), levels.end());
        size_t n = levels.size();
        _zoneLevels[zone] = ZoneRange{levels[n / 10], levels[(n * 9) / 10]};
        ++ranged;
    }
    LOG_INFO("playerbots", "[Companion] zone level table: {} zones with a range ({} zones sampled)", ranged,
             samples.size());
    return true;
}

void CompanionBotMgr::LoadQuestGiverSpots()
{
    // Start-up read on the world thread, once the base maps 0 and 1 exist (retried until they do). Solo normal quests
    // whose giver (or ender) is a creature spawn: where a companion walks when no quest giver of its level is in range.
    // Repeatable quests are not filtered here; the bot skips them.
    if (!sMapMgr->FindBaseMap(0) || !sMapMgr->FindBaseMap(1))
        return;
    _questGiverSpotsLoaded = true;

    auto loadSpots = [](char const* sql, bool ender, std::vector<QuestGiverSpot>& out, std::unordered_map<uint32,
                        std::vector<QuestGiverSpot>>* byQuest)
    {
        QueryResult result = WorldDatabase.Query(sql);
        if (!result)
            return;
        do
        {
            Field* fields = result->Fetch();
            QuestGiverSpot spot;
            spot.map = fields[0].Get<uint32>();
            spot.questLevel = fields[1].Get<uint32>();
            spot.x = fields[2].Get<float>();
            spot.y = fields[3].Get<float>();
            spot.z = fields[4].Get<float>();
            spot.questId = fields[5].Get<uint32>();
            uint32 entry = fields[6].Get<uint32>();

            Quest const* quest = sObjectMgr->GetQuestTemplate(spot.questId);
            CreatureTemplate const* cInfo = sObjectMgr->GetCreatureTemplate(entry);
            FactionTemplateEntry const* faction = cInfo ? sFactionTemplateStore.LookupEntry(cInfo->faction) : nullptr;
            if (!quest || !faction)
                continue;
            spot.minLevel = quest->GetMinLevel();
            spot.allowableRaces = quest->GetAllowableRaces();
            spot.hostileMask = faction->hostileMask;
            spot.zoneId = ZoneOf(spot.map, spot.x, spot.y, spot.z);

            if (byQuest)
                (*byQuest)[spot.questId].push_back(spot);
            if (!ender)
                out.push_back(spot);
        } while (result->NextRow());
    };

    // QuestType (quest_template.Method): 0 auto-complete, 1 disabled, 2 normal. Every quest but the disabled ones.
    // Starters: the creature's entry is creature.id (creature_queststarter.id is the template entry).
    loadSpots("SELECT c.map, q.QuestLevel, c.position_x, c.position_y, c.position_z, cq.quest, c.id FROM creature c "
              "JOIN creature_queststarter cq ON cq.id = c.id "
              "JOIN quest_template q ON q.ID = cq.quest "
              "WHERE q.QuestType <> 1 AND q.SuggestedGroupNum < 2 AND q.QuestLevel > 0",
              false, _questGiverSpots, nullptr);
    // Enders, keyed by quest, for pass 0 (a completed quest is turned in at its ender).
    std::vector<QuestGiverSpot> unusedEnders;
    loadSpots("SELECT c.map, q.QuestLevel, c.position_x, c.position_y, c.position_z, cq.quest, c.id FROM creature c "
              "JOIN creature_questender cq ON cq.id = c.id "
              "JOIN quest_template q ON q.ID = cq.quest "
              "WHERE q.QuestType <> 1 AND q.SuggestedGroupNum < 2 AND q.QuestLevel > 0",
              true, unusedEnders, &_questEnderSpots);

    BuildZoneLevels();
    LoadDutySpots();   // same start-up pass; reads the spawn caches, not the database

    // Publish last: every reader checks _questGiverSpotsReady first.
    _questGiverSpotsReady.store(true);
    LOG_INFO("playerbots", "Companion bots: {} quest-giver spawns loaded, {} quests with ender spawns",
             _questGiverSpots.size(), _questEnderSpots.size());
}

void CompanionBotMgr::LoadDutySpots()
{
    // Start-up on the world thread, from the in-memory spawn and template caches (no SQL). Creature spawns with a
    // vendor, repair or trainer flag on the template (trainers must have a Trainer entry), and guild bank game objects.
    // Same maps as the zone table. Zone and team are stored; reachability is decided per duty on the map thread.
    constexpr uint32 NPC_VENDOR_REPAIR_OR_TRAINER = UNIT_NPC_FLAG_VENDOR | UNIT_NPC_FLAG_REPAIR |
                                                    UNIT_NPC_FLAG_TRAINER | UNIT_NPC_FLAG_TRAINER_CLASS |
                                                    UNIT_NPC_FLAG_TRAINER_PROFESSION | UNIT_NPC_FLAG_INNKEEPER;
    auto onCompanionMap = [](uint32 mapId)
    { return std::find(std::begin(COMPANION_MAPS), std::end(COMPANION_MAPS), mapId) != std::end(COMPANION_MAPS); };

    auto fillZone = [](DutySpot& spot, uint32 mapId)
    {
        spot.zoneId = ZoneOf(mapId, spot.x, spot.y, spot.z);
        AreaTableEntry const* area = spot.zoneId ? sAreaTableStore.LookupEntry(spot.zoneId) : nullptr;
        spot.zoneTeam = area ? area->team : 0;
    };

    for (auto const& [guid, data] : sObjectMgr->GetAllCreatureData())
    {
        if (!onCompanionMap(data.mapid))
            continue;
        CreatureTemplate const* cInfo = sObjectMgr->GetCreatureTemplate(data.id);
        if (!cInfo || !(cInfo->npcflag & NPC_VENDOR_REPAIR_OR_TRAINER))
            continue;

        DutySpot spot;
        spot.spawnId = guid;
        spot.key = guid;
        spot.entry = data.id;
        spot.x = data.posX;
        spot.y = data.posY;
        spot.z = data.posZ;
        FactionTemplateEntry const* faction = sFactionTemplateStore.LookupEntry(cInfo->faction);
        spot.hostileMask = faction ? faction->hostileMask : 0;
        fillZone(spot, data.mapid);

        std::vector<DutySpot>& vendors = _dutySpots[static_cast<size_t>(DutySpotKind::Vendor)][data.mapid];
        std::vector<DutySpot>& repairers = _dutySpots[static_cast<size_t>(DutySpotKind::Repair)][data.mapid];
        std::vector<DutySpot>& trainers = _dutySpots[static_cast<size_t>(DutySpotKind::Trainer)][data.mapid];
        if (cInfo->npcflag & UNIT_NPC_FLAG_VENDOR)
            vendors.push_back(spot);
        if (cInfo->npcflag & UNIT_NPC_FLAG_REPAIR)
            repairers.push_back(spot);
        constexpr uint32 TRAINER_FLAGS =
            UNIT_NPC_FLAG_TRAINER | UNIT_NPC_FLAG_TRAINER_CLASS | UNIT_NPC_FLAG_TRAINER_PROFESSION;
        if ((cInfo->npcflag & TRAINER_FLAGS) && sObjectMgr->GetTrainer(data.id))
            trainers.push_back(spot);
        if (cInfo->npcflag & UNIT_NPC_FLAG_INNKEEPER)
            _dutySpots[static_cast<size_t>(DutySpotKind::Innkeeper)][data.mapid].push_back(spot);
    }

    for (auto const& [guid, data] : sObjectMgr->GetAllGOData())
    {
        if (!onCompanionMap(data.mapid))
            continue;
        GameObjectTemplate const* gInfo = sObjectMgr->GetGameObjectTemplate(data.id);
        if (!gInfo || gInfo->type != GAMEOBJECT_TYPE_GUILD_BANK)
            continue;

        DutySpot spot;
        spot.spawnId = guid;
        spot.key = static_cast<uint64>(guid) | (uint64(1) << 63);
        spot.entry = data.id;
        spot.isGo = true;
        spot.x = data.posX;
        spot.y = data.posY;
        spot.z = data.posZ;
        fillZone(spot, data.mapid);
        _dutySpots[static_cast<size_t>(DutySpotKind::Bank)][data.mapid].push_back(spot);
    }

    size_t counts[5] = {0, 0, 0, 0, 0};
    for (size_t kind = 0; kind < _dutySpots.size(); ++kind)
        for (auto const& byMap : _dutySpots[kind])
            counts[kind] += byMap.second.size();
    LOG_INFO("playerbots",
             "[Companion] duty spawns: {} vendors, {} repairers, {} trainers, {} guild banks, {} innkeepers",
             counts[0], counts[1], counts[2], counts[3], counts[4]);
    _dutySpotsReady.store(true);
}

std::vector<CompanionBotMgr::DutySpot> const& CompanionBotMgr::DutySpots(uint32 mapId, DutySpotKind kind) const
{
    static std::vector<DutySpot> const none;
    if (!_dutySpotsReady.load())
        return none;
    auto const& byMap = _dutySpots[static_cast<size_t>(kind)];
    auto it = byMap.find(mapId);
    return it == byMap.end() ? none : it->second;
}

std::vector<CompanionBotMgr::QuestGiverSpot> const& CompanionBotMgr::QuestGiverSpots() const
{
    static std::vector<QuestGiverSpot> const none;
    return _questGiverSpotsReady.load() ? _questGiverSpots : none;
}

size_t CompanionBotMgr::QuestEnderSpotCount() const
{
    if (!_questGiverSpotsReady.load())
        return 0;
    size_t count = 0;
    for (auto const& entry : _questEnderSpots)
        count += entry.second.size();
    return count;
}

bool CompanionBotMgr::ZoneLevelRange(uint32 zoneId, uint32& lo, uint32& hi) const
{
    auto it = _zoneLevels.find(zoneId);
    if (!zoneId || it == _zoneLevels.end())
        return false;
    lo = it->second.lo;
    hi = it->second.hi;
    return true;
}

bool CompanionBotMgr::ZoneFitsLevel(uint32 zoneId, uint32 level) const
{
    auto it = _zoneLevels.find(zoneId);
    if (!zoneId || it == _zoneLevels.end())
        return true;
    int32 lvl = static_cast<int32>(level);
    return lvl >= static_cast<int32>(it->second.lo) - 3 && lvl <= static_cast<int32>(it->second.hi) + 2;
}

uint32 CompanionBotMgr::CachedZoneAt(uint32 mapId, float x, float y) const
{
    if (!_questGiverSpotsReady.load())
        return 0;
    int32 const cx = CoarseCoord(x);
    int32 const cy = CoarseCoord(y);
    auto it = _coarseZoneGrid.find(CoarseZoneKey(mapId, cx, cy));
    if (it != _coarseZoneGrid.end())
        return it->second;
    for (int32 dx = -1; dx <= 1; ++dx)
    {
        for (int32 dy = -1; dy <= 1; ++dy)
        {
            if (dx == 0 && dy == 0)
                continue;
            auto nit = _coarseZoneGrid.find(CoarseZoneKey(mapId, cx + dx, cy + dy));
            if (nit != _coarseZoneGrid.end())
                return nit->second;
        }
    }
    return 0;
}

bool CompanionBotMgr::IsZoneTooDangerous(Player const* bot, uint32 zoneId) const
{
    if (!bot || !zoneId || !sPlayerbotAIConfig.companionZoneAvoidEnabled || !_questGiverSpotsReady.load())
        return false;
    auto it = _zoneLevels.find(zoneId);
    if (it == _zoneLevels.end())
        return false;
    uint32 const gap = std::max<uint32>(1, sPlayerbotAIConfig.companionDangerLevelGap);
    return it->second.lo >= bot->GetLevel() + gap;
}

bool CompanionBotMgr::IsPointTooDangerous(Player const* bot, uint32 mapId, float x, float y, float z) const
{
    if (!bot || !bot->IsInWorld() || !bot->GetMap() || bot->GetMapId() != mapId ||
        !sPlayerbotAIConfig.companionZoneAvoidEnabled || !_questGiverSpotsReady.load())
        return false;
    uint32 zone = CachedZoneAt(mapId, x, y);
    if (!zone && bot->GetMap()->IsGridLoaded(x, y))
        zone = bot->GetMap()->GetZoneId(PHASEMASK_NORMAL, x, y, z);
    return IsZoneTooDangerous(bot, zone);
}

bool CompanionBotMgr::IsPathTooDangerous(Player const* bot, uint32 mapId, float x, float y, float z) const
{
    (void)z;
    if (!bot || !bot->IsInWorld() || !bot->GetMap() || bot->GetMapId() != mapId ||
        !sPlayerbotAIConfig.companionZoneAvoidEnabled || !_questGiverSpotsReady.load())
        return false;
    float const sx = bot->GetPositionX();
    float const sy = bot->GetPositionY();
    constexpr int SAMPLES = 6;
    for (int i = 1; i <= SAMPLES; ++i)
    {
        float const t = static_cast<float>(i) / static_cast<float>(SAMPLES);
        float const px = sx + (x - sx) * t;
        float const py = sy + (y - sy) * t;
        uint32 const zone = CachedZoneAt(mapId, px, py);
        if (IsZoneTooDangerous(bot, zone))
            return true;
    }
    return false;
}

bool CompanionBotMgr::IsProtectedGuildMember(Player const* bot) const
{
    if (!bot)
        return false;
    if (IsCompanion(bot))
        return true;
    if (!sPlayerbotAIConfig.guildRosterProtectInventory)
        return false;
    uint32 const gid = bot->GetGuildId();
    if (!gid)
        return false;
    uint32 ownerGid = _ownerGuildId.load();
    if (!ownerGid)
    {
        if (Player* owner = FindOwnerOnline())
        {
            ownerGid = owner->GetGuildId();
            if (ownerGid)
                const_cast<CompanionBotMgr*>(this)->_ownerGuildId.store(ownerGid);
        }
    }
    return ownerGid != 0 && gid == ownerGid;
}

uint32 CompanionBotMgr::DestZone(Player const* bot, WorldLocation const& dest) const
{
    if (!bot)
        return 0;
    if (dest.GetMapId() == bot->GetMapId())
    {
        // Same map: the bot's own map is owned by this map thread, so it is read directly.
        if (!bot->IsInWorld())
            return 0;
        return bot->GetMap()->GetZoneId(PHASEMASK_NORMAL, dest.GetPositionX(), dest.GetPositionY(),
                                        dest.GetPositionZ());
    }
    // Another map: the zone stored with the quest-giver spot at this exact position (no terrain load on a map thread).
    for (QuestGiverSpot const& spot : _questGiverSpots)
    {
        if (spot.map == dest.GetMapId() && spot.x == dest.GetPositionX() && spot.y == dest.GetPositionY() &&
            spot.z == dest.GetPositionZ())
            return spot.zoneId;
    }
    return 0;
}

bool CompanionBotMgr::PickQuestGiverSpot(Player const* bot, WorldLocation& out) const
{
    if (!bot || !_questGiverSpotsReady.load())
        return false;

    int32 level = static_cast<int32>(bot->GetLevel());
    int32 hi = level + 3;   // NewRpgBaseAction::IsQuestCapableDoing: above this the bot will not take it
    int32 lo = level - 4;   // Quests.LowLevelHideDiff default: below this the quest is hidden from the bot
    int32 loWide = level - 6;
    uint32 botMap = bot->GetMapId();
    uint32 raceMask = bot->getRaceMask();
    uint32 hostileBit = bot->GetTeamId() == TEAM_HORDE ? 4u : 2u;

    // A spot the bot may walk to: zone fits, quest MinLevel and races fit, giver not hostile to the bot's team.
    auto usable = [&](QuestGiverSpot const& spot) -> bool
    {
        if (!ZoneFitsLevel(spot.zoneId, bot->GetLevel()))
            return false;
        if (static_cast<int32>(spot.minLevel) > level)
            return false;
        if (spot.allowableRaces && !(spot.allowableRaces & raceMask))
            return false;
        return !(spot.hostileMask & hostileBit);
    };

    // Nearest spot that passes the level window and the distance rule, on the given map.
    // Map -1 means any map other than botMap; the distance is then only used to order candidates.
    auto nearest = [&](int32 levelLo, float minDist, bool otherMapsOnly) -> QuestGiverSpot const*
    {
        QuestGiverSpot const* found = nullptr;
        float bestDist = 0.0f;
        for (QuestGiverSpot const& spot : _questGiverSpots)
        {
            if (otherMapsOnly ? spot.map == botMap : spot.map != botMap)
                continue;
            if (static_cast<int32>(spot.questLevel) < levelLo || static_cast<int32>(spot.questLevel) > hi)
                continue;
            float dx = spot.x - bot->GetPositionX();
            float dy = spot.y - bot->GetPositionY();
            float dist = std::sqrt(dx * dx + dy * dy);
            if (!otherMapsOnly && dist < minDist)   // already in range: the normal quest search handles these
                continue;
            if (!usable(spot))
                continue;
            if (!found || dist < bestDist)
            {
                found = &spot;
                bestDist = dist;
            }
        }
        return found;
    };

    int pass = 0;
    QuestGiverSpot const* best = nullptr;

    // Pass 0: a completed quest whose ender is on this map and in a zone that fits. Nearest ender wins.
    for (uint16 slot = 0; slot < MAX_QUEST_LOG_SIZE && !best; ++slot)
    {
        uint32 questId = bot->GetQuestSlotQuestId(slot);
        if (!questId || bot->GetQuestStatus(questId) != QUEST_STATUS_COMPLETE)
            continue;
        auto it = _questEnderSpots.find(questId);
        if (it == _questEnderSpots.end())
            continue;
        float bestDist = 0.0f;
        for (QuestGiverSpot const& spot : it->second)
        {
            if (spot.map != botMap || !ZoneFitsLevel(spot.zoneId, bot->GetLevel()))
                continue;
            float dist = bot->GetDistance(spot.x, spot.y, spot.z);
            if (!best || dist < bestDist)
            {
                best = &spot;
                bestDist = dist;
            }
        }
    }
    if (best)
        pass = 0;

    // Pass 1: same map, normal window, at least 150 yards away (walk).
    if (!best)
    {
        best = nearest(lo, 150.0f, false);
        pass = 1;
    }
    // Pass 2 (widened): same map, wider low end of the window, any distance (walk).
    if (!best)
    {
        best = nearest(loWide, 0.0f, false);
        pass = 2;
    }
    // Pass 3 (travel fallback): another map, normal window (location-only teleport in MoveFarTo).
    if (!best)
    {
        best = nearest(lo, 0.0f, true);
        pass = 3;
    }
    if (!best)
        return false;

    out = WorldLocation(best->map, best->x, best->y, best->z, 0.0f);

    // Log when the choice changes for this bot, not on every tick.
    std::ostringstream line;
    line << "pass " << pass << " -> map " << best->map << " zone " << best->zoneId << " (" << std::fixed
         << std::setprecision(0) << best->x << "," << best->y << ") quest level " << best->questLevel;
    {
        std::lock_guard<std::mutex> lock(_spotLogMutex);
        uint32 low = bot->GetGUID().GetCounter();
        auto it = _lastSpotChoice.find(low);
        if (it == _lastSpotChoice.end() || it->second != line.str())
        {
            _lastSpotChoice[low] = line.str();
            LOG_INFO("playerbots", "[Companion] {} quest spot {}", bot->GetName(), line.str());
        }
    }
    return true;
}

bool CompanionBotMgr::ShouldLogTeleportSkip(uint32 lowGuid, uint32 nowSec)
{
    std::lock_guard<std::mutex> lock(_questEventMutex);
    auto it = _lastTeleportSkipLog.find(lowGuid);
    if (it != _lastTeleportSkipLog.end() && nowSec - it->second < 600)
        return false;
    _lastTeleportSkipLog[lowGuid] = nowSec;
    return true;
}

bool CompanionBotMgr::ShouldLogXpBlock(uint32 lowGuid, uint32 nowSec)
{
    auto it = _lastXpBlockLog.find(lowGuid);
    if (it != _lastXpBlockLog.end() && nowSec - it->second < 600)
        return false;
    _lastXpBlockLog[lowGuid] = nowSec;
    return true;
}

bool CompanionBotMgr::ShouldLogTeleport(uint32 lowGuid, uint32 nowSec)
{
    std::lock_guard<std::mutex> lock(_questEventMutex);
    auto it = _lastTeleportLog.find(lowGuid);
    if (it != _lastTeleportLog.end() && nowSec - it->second < 600)
        return false;
    _lastTeleportLog[lowGuid] = nowSec;
    return true;
}

void CompanionBotMgr::NoteQuestAccept(uint32 lowGuid, uint32 questId, uint32 nowSec)
{
    std::lock_guard<std::mutex> lock(_questEventMutex);
    _lastAccept[lowGuid] = QuestEvent{questId, nowSec};
}

void CompanionBotMgr::NoteQuestTurnIn(uint32 lowGuid, uint32 questId, uint32 nowSec)
{
    std::lock_guard<std::mutex> lock(_questEventMutex);
    _lastTurnIn[lowGuid] = QuestEvent{questId, nowSec};
}

CompanionBotMgr::QuestEvent CompanionBotMgr::LastQuestAccept(uint32 lowGuid) const
{
    std::lock_guard<std::mutex> lock(_questEventMutex);
    auto it = _lastAccept.find(lowGuid);
    return it == _lastAccept.end() ? QuestEvent{} : it->second;
}

CompanionBotMgr::QuestEvent CompanionBotMgr::LastQuestTurnIn(uint32 lowGuid) const
{
    std::lock_guard<std::mutex> lock(_questEventMutex);
    auto it = _lastTurnIn.find(lowGuid);
    return it == _lastTurnIn.end() ? QuestEvent{} : it->second;
}

void CompanionBotMgr::Update(uint32 elapsed)
{
    if (!_configLoaded.load())
    {
        LoadConfig();
        return;
    }
    if (_names.empty())
        return;
    CompanionBrain::Update(elapsed);
    CompanionDuty::UpdateWorld(elapsed);
    CompanionQuest::UpdateWorld(elapsed);
    CompanionLife::UpdateWorld(elapsed);   // quest project tables (after the quest-giver spots are loaded)
    CompanionTransport::UpdateWorld(elapsed);   // dock / tram / portal tables once, boat schedule samples
    if (!_classQuestsLoaded)
        LoadClassQuests();
    if (!_questGiverSpotsLoaded)
        LoadQuestGiverSpots();

    _loginTimer += elapsed;
    _levelTimer += elapsed;
    _scheduleTimer += elapsed;
    _spellTimer += elapsed;

    if (!_schedulesLoaded || _scheduleTimer >= SCHEDULE_REFRESH_MS)
    {
        _scheduleTimer = 0;
        ReloadSchedules();
    }

    // Log in companions once the random-bot start-up has begun (and log out companions whose play shift ended).
    if (GameTime::GetUptime().count() >= 60 && _loginTimer >= LOGIN_RETRY_MS)
    {
        _loginTimer = 0;
        LoginMissingCompanions();
    }

    // Every refresh interval; every 5 s while the level is still unknown (the XP cap is closed until then).
    if (_levelTimer >= OWNER_LEVEL_REFRESH_MS || (OwnerLevelUnknown() && _levelTimer >= 5000))
    {
        _levelTimer = 0;
        _xpWaitForOwner.store(sConfigMgr->GetOption<bool>("AiPlayerbot.Companion.XpCap.WaitForOwnerLevel", true));
        RefreshOwnerLevel();
    }

    if (_spellTimer >= SPELL_PASS_MS)
    {
        _spellTimer = 0;
        SyncLevelSpells();
    }

    // Lightweight 3-second atomic live-position snapshot for Azeroth Watch (zero DB queries, <0.05 ms).
    static uint32 sLivePosTimer = 0;
    sLivePosTimer += elapsed;
    if (sLivePosTimer >= 3000)
    {
        sLivePosTimer = 0;
        std::string const logDir = sConfigMgr->GetOption<std::string>("LogsDir", "");
        if (!logDir.empty())
        {
            std::string const tmpPath = logDir + "/live_positions.json.tmp";
            std::string const outPath = logDir + "/live_positions.json";
            if (FILE* fp = std::fopen(tmpPath.c_str(), "w"))
            {
                std::string const wkEv = CompanionLife::ActiveWeeklyEventLabel();
                std::fprintf(fp, "{\"t\":%u,\"weekly_event\":\"%s\",\"units\":[",
                             static_cast<uint32>(GameTime::GetGameTime().count()), wkEv.c_str());
                bool first = true;
                HashMapHolder<Player>::MapType const& players = ObjectAccessor::GetPlayers();
                for (auto const& kv : players)
                {
                    Player* p = kv.second;
                    if (!p || !p->IsInWorld())
                        continue;
                    if (!first)
                        std::fputc(',', fp);
                    first = false;
                    std::fprintf(fp, "{\"g\":%u,\"m\":%u,\"z\":%u,\"x\":%.1f,\"y\":%.1f,\"hp\":%u}",
                                 p->GetGUID().GetCounter(), p->GetMapId(), p->GetZoneId(),
                                 p->GetPositionX(), p->GetPositionY(), p->IsAlive() ? p->GetHealth() : 0u);
                }
                std::fputs("]}\n", fp);
                std::fclose(fp);
                std::rename(tmpPath.c_str(), outPath.c_str());
            }
        }
    }
}

void CompanionBotMgr::ReloadSchedules()
{
    _schedulesLoaded = true;
    QueryResult probe = CharacterDatabase.Query(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() "
        "AND table_name = 'companion_schedule'");
    if (!probe || probe->Fetch()[0].Get<uint64>() == 0)
    {
        if (!_scheduleMissingLogged)
        {
            _scheduleMissingLogged = true;
            LOG_WARN("playerbots", "Companion schedules: table companion_schedule is missing (all companions always online)");
        }
        std::lock_guard<std::mutex> lock(_stateMutex);
        _schedules.clear();
        return;
    }

    std::unordered_map<std::string, ScheduleEntry> rows;
    if (QueryResult res = CharacterDatabase.Query(
            "SELECT `name`, `shift_group`, `start_hour`, `end_hour`, `days_mask`, `always_online` FROM `companion_schedule`"))
    {
        do
        {
            Field* f = res->Fetch();
            ScheduleEntry e;
            std::string const nm = Lower(f[0].Get<std::string>());
            e.shiftGroup = f[1].Get<std::string>();
            e.startHour = f[2].Get<uint8>();
            e.endHour = f[3].Get<uint8>();
            e.daysMask = f[4].Get<uint8>();
            e.alwaysOnline = f[5].Get<uint8>() != 0;
            rows[nm] = std::move(e);
        } while (res->NextRow());
    }

    std::lock_guard<std::mutex> lock(_stateMutex);
    _schedules.swap(rows);
}

bool CompanionBotMgr::IsScheduledOnline(std::string const& name, Player const* bot) const
{
    if (!sPlayerbotAIConfig.companionScheduleEnabled)
        return true;
    if (bot && IsGroupedWithOwner(bot))
        return true;

    ScheduleEntry entry;
    bool found = false;
    {
        std::lock_guard<std::mutex> lock(_stateMutex);
        auto it = _schedules.find(Lower(name));
        if (it != _schedules.end())
        {
            entry = it->second;
            found = true;
        }
    }
    if (!found || entry.alwaysOnline)
        return true;

    std::time_t const now = std::time(nullptr);
    std::tm localTm{};
    localtime_r(&now, &localTm);
    // daysMask names the day a shift starts. An overnight shift's after-midnight hours belong to the shift that
    // started on the previous weekday, so they consult the previous day's bit.
    uint8 const dayBit = static_cast<uint8>(1u << localTm.tm_wday);
    uint8 const prevDayBit = static_cast<uint8>(1u << ((localTm.tm_wday + 6) % 7));
    uint8 const hr = static_cast<uint8>(localTm.tm_hour);
    if (entry.startHour == entry.endHour)
        return (entry.daysMask & dayBit) != 0;
    if (entry.startHour < entry.endHour)
        return (entry.daysMask & dayBit) != 0 && hr >= entry.startHour && hr < entry.endHour;
    if (hr >= entry.startHour)
        return (entry.daysMask & dayBit) != 0;
    if (hr < entry.endHour)
        return (entry.daysMask & prevDayBit) != 0;
    return false;
}

std::string CompanionBotMgr::ScheduleSummary() const
{
    uint32 online = 0, scheduled = 0, total = static_cast<uint32>(_displayNames.size());
    for (std::string const& name : _displayNames)
    {
        ObjectGuid const guid = sCharacterCache->GetCharacterGuidByName(name);
        Player* p = guid ? ObjectAccessor::FindConnectedPlayer(guid) : nullptr;
        if (p)
            ++online;
        if (IsScheduledOnline(name, p))
            ++scheduled;
    }
    std::ostringstream oss;
    oss << "Companion schedules: enabled=" << (sPlayerbotAIConfig.companionScheduleEnabled ? 1 : 0)
        << " total=" << total << " scheduled_now=" << scheduled << " online_now=" << online;
    return oss.str();
}

void CompanionBotMgr::LoginMissingCompanions()
{
    for (std::string const& name : _displayNames)
    {
        ObjectGuid guid = sCharacterCache->GetCharacterGuidByName(name);
        if (!guid)
        {
            LOG_WARN("playerbots", "Companion bot '{}' does not exist", name);
            continue;
        }
        Player* connected = ObjectAccessor::FindConnectedPlayer(guid);
        bool const wantOnline = IsScheduledOnline(name, connected);
        if (!wantOnline)
        {
            if (connected && !IsGroupedWithOwner(connected) && !connected->IsInCombat() &&
                (!connected->GetMap() || !connected->GetMap()->Instanceable()))
            {
                LOG_INFO("playerbots", "[Companion] {}: schedule shift ended, logging out", name);
                sRandomPlayerbotMgr.LogoutPlayerBot(guid);
            }
            continue;
        }
        if (connected)
            continue;
        // Master account 0 means "no master": the bot is logged in as a standalone bot and is not in the random list.
        sRandomPlayerbotMgr.AddPlayerBot(guid, 0);
        LOG_INFO("playerbots", "Companion bot '{}' login requested", name);
    }
}

void CompanionBotMgr::RefreshOwnerLevel()
{
    auto readOwnerLevel = [](uint32 ownerLow, char const*& source) -> uint32
    {
        if (!ownerLow)
            return 0;
        if (Player* owner = ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(ownerLow)))
        {
            source = "online";
            return owner->GetLevel();
        }
        source = "db";
        QueryResult result = CharacterDatabase.Query("SELECT level FROM characters WHERE guid = {}", ownerLow);
        return result ? result->Fetch()[0].Get<uint32>() : 0;
    };

    uint32 ownerLow = _ownerLow.load();
    if (!ownerLow && !_ownerName.empty())
    {
        if (ObjectGuid ownerGuid = sCharacterCache->GetCharacterGuidByName(_ownerName))
        {
            ownerLow = ownerGuid.GetCounter();
            _ownerLow.store(ownerLow);
            std::lock_guard<std::mutex> lock(_stateMutex);
            _allOwnerLows.insert(ownerLow);
        }
    }

    // Resolve any custom per-companion owners that weren't in cache at boot.
    std::set<uint32> ownersToCheck;
    if (ownerLow)
        ownersToCheck.insert(ownerLow);
    {
        std::lock_guard<std::mutex> lock(_stateMutex);
        for (auto const& [lowerBot, ownerName] : _botOwnerName)
        {
            if (_botOwnerLow.find(lowerBot) == _botOwnerLow.end())
            {
                if (ObjectGuid og = sCharacterCache->GetCharacterGuidByName(ownerName))
                {
                    _botOwnerLow[lowerBot] = og.GetCounter();
                    _allOwnerLows.insert(og.GetCounter());
                }
            }
        }
        for (uint32 ol : _allOwnerLows)
            ownersToCheck.insert(ol);
    }

    for (uint32 ol : ownersToCheck)
    {
        char const* source = "online";
        uint32 const level = readOwnerLevel(ol, source);
        if (!level)
            continue;
        {
            std::lock_guard<std::mutex> lock(_stateMutex);
            _ownerLevels[ol] = level;
        }
        if (ol == ownerLow && !_ownerLevel.exchange(level))
            LOG_INFO("playerbots", "Companion XP cap: owner level known level={} source={} uptime={}s", level, source,
                     GameTime::GetUptime().count());
    }
}

void CompanionBotMgr::SyncLevelSpells()
{
    for (std::string const& name : _displayNames)
    {
        ObjectGuid guid = sCharacterCache->GetCharacterGuidByName(name);
        if (!guid)
            continue;
        Player* companion = ObjectAccessor::FindConnectedPlayer(guid);
        if (!companion || !companion->IsInWorld())
            continue;

        uint32 level = companion->GetLevel();
        auto it = _lastSpellPassLevel.find(guid.GetCounter());
        if (it != _lastSpellPassLevel.end() && it->second == level)
            continue;
        _lastSpellPassLevel[guid.GetCounter()] = level;
        EnsureClassSpells(companion);
    }
}

bool CompanionBotMgr::IsCompanionName(std::string const& name) const
{
    if (!_configLoaded.load() || name.empty())
        return false;
    return _names.count(Lower(name)) > 0;
}

bool CompanionBotMgr::IsCompanion(Player const* player) const
{
    return player && IsCompanionName(player->GetName());
}

namespace
{
// Body-armour inventory types (the armour-type rule applies to these). Cloaks, shields, rings, necks, trinkets,
// weapons and tabards are not body armour.
bool IsBodyArmorInvType(uint8 inv)
{
    switch (inv)
    {
        case INVTYPE_HEAD:
        case INVTYPE_SHOULDERS:
        case INVTYPE_BODY:
        case INVTYPE_CHEST:
        case INVTYPE_WAIST:
        case INVTYPE_LEGS:
        case INVTYPE_FEET:
        case INVTYPE_WRISTS:
        case INVTYPE_HANDS:
        case INVTYPE_ROBE:
            return true;
        default:
            return false;
    }
}

// Best armour subclass the class can wear at the level. 0 when the rule does not cover the class.
uint32 BestArmorSubclass(uint8 classId, uint32 level)
{
    switch (classId)
    {
        case CLASS_WARRIOR:
        case CLASS_PALADIN:
            return level < 40 ? ITEM_SUBCLASS_ARMOR_MAIL : ITEM_SUBCLASS_ARMOR_PLATE;
        case CLASS_HUNTER:
            return level < 40 ? ITEM_SUBCLASS_ARMOR_LEATHER : ITEM_SUBCLASS_ARMOR_MAIL;
        case CLASS_ROGUE:
            return ITEM_SUBCLASS_ARMOR_LEATHER;
        default:
            return 0;
    }
}

// Equipment slots an item can occupy, in preference order (rings, trinkets, one-hand weapons have two).
// A shield tank (TankKeepsShield) gets the main hand only for a one-hand weapon. FindEquipSlot returns a FREE slot
// first, so without this a one-hand weapon with an empty off hand would come back as the off hand.
std::vector<uint8> CandidateSlots(Player const* player, ItemTemplate const* proto)
{
    std::vector<uint8> slots;
    uint8 dst = const_cast<Player*>(player)->FindEquipSlot(proto, NULL_SLOT, true);
    if (dst == NULL_SLOT)
        return slots;
    if (proto->InventoryType == INVTYPE_WEAPON && sCompanionBotMgr.TankKeepsShield(player))
    {
        slots.push_back(EQUIPMENT_SLOT_MAINHAND);
        return slots;
    }
    slots.push_back(dst);
    switch (dst)
    {
        case EQUIPMENT_SLOT_FINGER1: slots.push_back(EQUIPMENT_SLOT_FINGER2); break;
        case EQUIPMENT_SLOT_TRINKET1: slots.push_back(EQUIPMENT_SLOT_TRINKET2); break;
        case EQUIPMENT_SLOT_MAINHAND:
            if (proto->InventoryType != INVTYPE_2HWEAPON)
                slots.push_back(EQUIPMENT_SLOT_OFFHAND);
            break;
        default: break;
    }
    return slots;
}
}  // namespace

bool CompanionBotMgr::SlotHoldsHeirloom(Player const* player, uint8 slot) const
{
    if (!player)
        return false;
    Item* equipped = player->GetItemByPos(INVENTORY_SLOT_BAG_0, slot);
    return equipped && equipped->GetTemplate()->Quality == ITEM_QUALITY_HEIRLOOM;
}

bool CompanionBotMgr::ArmorTypeAllowed(Player const* player, ItemTemplate const* proto) const
{
    if (!player || !proto)
        return false;
    if (proto->Quality == ITEM_QUALITY_HEIRLOOM)
        return true;   // heirloom stats scale with level; the owner's rule for heirlooms is "never replace one"
    if (proto->Class != ITEM_CLASS_ARMOR || !IsBodyArmorInvType(proto->InventoryType))
        return true;
    // The companion's list (AiPlayerbot.Companion.ArmorTypes.<Name>). The same keys are read by the best-gear script.
    std::vector<std::string> tokens = {"best"};
    auto it = _armorTypes.find(Lower(player->GetName()));
    if (it != _armorTypes.end())
        tokens = it->second;
    uint32 best = BestArmorSubclass(player->getClass(), player->GetLevel());
    for (std::string const& tok : tokens)
    {
        if (tok == "best")
        {
            if (best && proto->SubClass == best)
                return true;
        }
        else if (tok == "cloth" && proto->SubClass == ITEM_SUBCLASS_ARMOR_CLOTH)
            return true;
        else if (tok == "leather" && proto->SubClass == ITEM_SUBCLASS_ARMOR_LEATHER)
            return true;
        else if (tok == "mail" && proto->SubClass == ITEM_SUBCLASS_ARMOR_MAIL)
            return true;
        else if (tok == "plate" && proto->SubClass == ITEM_SUBCLASS_ARMOR_PLATE)
            return true;
    }
    return false;
}

bool CompanionBotMgr::CanEquip(Player* player, ItemTemplate const* proto) const
{
    if (!player || !proto)
        return false;
    if (!ArmorTypeAllowed(player, proto))
        return false;
    // Tank shield rule: no off-hand weapon or held item while the tank owns a shield. A shield may replace a weapon
    // (even an heirloom) in the off hand.
    bool const tankShield = TankKeepsShield(player);
    if (tankShield && IsOffhandNonShield(proto))
        return false;
    if (tankShield && IsShield(proto))
    {
        Item* off = player->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_OFFHAND);
        if (off && off->GetTemplate()->Class == ITEM_CLASS_WEAPON)
            return true;
    }
    // Never unequip an heirloom. Checked on every slot the item could take, so a robe cannot replace a chest heirloom.
    for (uint8 slot : CandidateSlots(player, proto))
    {
        if (SlotHoldsHeirloom(player, slot))
            return false;
    }
    return true;
}

bool CompanionBotMgr::IsShield(ItemTemplate const* proto)
{
    return proto && proto->Class == ITEM_CLASS_ARMOR && proto->SubClass == ITEM_SUBCLASS_ARMOR_SHIELD;
}

bool CompanionBotMgr::IsOffhandNonShield(ItemTemplate const* proto)
{
    return proto && !IsShield(proto) &&
           (proto->InventoryType == INVTYPE_WEAPONOFFHAND || proto->InventoryType == INVTYPE_HOLDABLE);
}

bool CompanionBotMgr::TankKeepsShield(Player const* player) const
{
    if (!player || !IsCompanion(player) || !CompanionCraft::AreaEnabled("TankShield"))
        return false;
    if (player->getClass() != CLASS_WARRIOR && player->getClass() != CLASS_PALADIN)
        return false;
    if (!player->HasSkill(SKILL_SHIELD) || !PlayerbotAI::IsTank(const_cast<Player*>(player), true))
        return false;
    // Owns a shield: equipped, in the backpack or in a bag (the bank does not count).
    Item* off = player->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_OFFHAND);
    if (off && IsShield(off->GetTemplate()))
        return true;
    for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
    {
        Item* item = player->GetItemByPos(INVENTORY_SLOT_BAG_0, slot);
        if (item && IsShield(item->GetTemplate()))
            return true;
    }
    for (uint8 bagSlot = INVENTORY_SLOT_BAG_START; bagSlot < INVENTORY_SLOT_BAG_END; ++bagSlot)
    {
        Bag* bag = player->GetBagByPos(bagSlot);
        if (!bag)
            continue;
        for (uint32 i = 0; i < bag->GetBagSize(); ++i)
        {
            Item* item = bag->GetItemByPos(i);
            if (item && IsShield(item->GetTemplate()))
                return true;
        }
    }
    return false;
}

uint32 CompanionBotMgr::OwnerLowFor(Player const* bot) const
{
    if (bot)
    {
        std::string const lowerBot = Lower(bot->GetName());
        std::lock_guard<std::mutex> lock(_stateMutex);
        auto it = _botOwnerLow.find(lowerBot);
        if (it != _botOwnerLow.end() && it->second != 0)
            return it->second;
    }
    return _ownerLow.load();
}

uint32 CompanionBotMgr::OwnerLevelFor(Player const* bot) const
{
    uint32 const ol = OwnerLowFor(bot);
    if (ol)
    {
        std::lock_guard<std::mutex> lock(_stateMutex);
        auto it = _ownerLevels.find(ol);
        if (it != _ownerLevels.end() && it->second > 0)
            return it->second;
    }
    return _ownerLevel.load();
}

Player* CompanionBotMgr::FindOwnerOnlineFor(Player const* bot) const
{
    uint32 const ol = OwnerLowFor(bot);
    if (!ol)
        return nullptr;
    return ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(ol));
}

bool CompanionBotMgr::IsOwner(Player const* player) const
{
    if (!player)
        return false;
    uint32 const low = player->GetGUID().GetCounter();
    if (_ownerLow.load() == low && low != 0)
        return true;
    std::lock_guard<std::mutex> lock(_stateMutex);
    return _allOwnerLows.count(low) > 0;
}

bool CompanionBotMgr::IsGroupedWithOwner(Player const* bot) const
{
    if (!bot)
        return false;
    Group const* group = bot->GetGroup();
    uint32 const ownerLow = OwnerLowFor(bot);
    return group && ownerLow && group->IsMember(ObjectGuid::Create<HighGuid::Player>(ownerLow));
}

bool CompanionBotMgr::IsInAuthorizedCoopGroup(Player const* bot) const
{
    if (!bot || !CompanionBrain::CoopEnabled())
        return false;
    Group const* group = bot->GetGroup();
    if (!group || group->isRaidGroup())
        return false;  // Phase II: strictly 5-player dungeon parties, NEVER autonomous raids!

    uint64 oppId = 0;
    uint32 targetMap = 0;
    uint32 expectedGroup = 0;
    if (!CompanionBrain::IsAuthorizedCoopMember(bot->GetGUID().GetCounter(), oppId, targetMap, expectedGroup))
        return false;

    // Never auto-control a group containing an unrecognized human player
    for (GroupReference const* itr = group->GetFirstMember(); itr != nullptr; itr = itr->next())
    {
        Player const* member = itr->GetSource();
        if (!member)
            continue;
        if (!IsCompanion(member))
            return false;
    }
    return true;
}

void CompanionBotMgr::RepairHeirlooms(Player* bot, std::vector<std::string>& report)
{
    if (!bot)
        return;

    std::vector<Item*> heirlooms;
    for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
    {
        Item* item = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot);
        if (item && item->GetTemplate()->Quality == ITEM_QUALITY_HEIRLOOM)
            heirlooms.push_back(item);
    }
    for (uint8 bagSlot = INVENTORY_SLOT_BAG_START; bagSlot < INVENTORY_SLOT_BAG_END; ++bagSlot)
    {
        Bag* bag = (Bag*)bot->GetItemByPos(INVENTORY_SLOT_BAG_0, bagSlot);
        if (!bag)
            continue;
        for (uint32 i = 0; i < bag->GetBagSize(); ++i)
        {
            Item* item = bag->GetItemByPos(i);
            if (item && item->GetTemplate()->Quality == ITEM_QUALITY_HEIRLOOM)
                heirlooms.push_back(item);
        }
    }

    if (heirlooms.empty())
    {
        report.push_back(bot->GetName() + ": no heirloom in the bags");
        return;
    }

    for (Item* item : heirlooms)
    {
        ItemTemplate const* proto = item->GetTemplate();
        std::string const name = proto->Name1;
        int target = -1;
        for (uint8 slot : CandidateSlots(bot, proto))
        {
            if (!SlotHoldsHeirloom(bot, slot))
            {
                target = slot;
                break;
            }
        }
        if (target < 0)
        {
            report.push_back(bot->GetName() + ": " + name + " stays in the bags (every slot it fits holds an heirloom)");
            continue;
        }

        uint16 src = (uint16(item->GetBagSlot()) << 8) | item->GetSlot();
        uint16 dst = (uint16(INVENTORY_SLOT_BAG_0) << 8) | uint16(target);
        bot->SwapItem(src, dst);
        Item* now = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, target);
        if (now == item)
            report.push_back(bot->GetName() + ": equipped " + name + " in slot " + std::to_string(target));
        else
            report.push_back(bot->GetName() + ": could not equip " + name + " in slot " + std::to_string(target));
    }
}

bool CompanionBotMgr::LeadModeOn(uint32 lowGuid) const
{
    std::lock_guard<std::mutex> lock(_stateMutex);
    auto it = _leadMode.find(lowGuid);
    if (it != _leadMode.end())
        return it->second;
    return sPlayerbotAIConfig.companionLeadDefault;
}

void CompanionBotMgr::SetLeadMode(uint32 lowGuid, bool on)
{
    std::lock_guard<std::mutex> lock(_stateMutex);
    _leadMode[lowGuid] = on;
}

void CompanionBotMgr::NoteGroupCombat(uint32 lowGuid, uint32 nowSec)
{
    std::lock_guard<std::mutex> lock(_stateMutex);
    _lastGroupCombat[lowGuid] = nowSec;
}

uint32 CompanionBotMgr::LastGroupCombat(uint32 lowGuid) const
{
    std::lock_guard<std::mutex> lock(_stateMutex);
    auto it = _lastGroupCombat.find(lowGuid);
    return it == _lastGroupCombat.end() ? 0 : it->second;
}

void CompanionBotMgr::ApplyLeadStrategies(PlayerbotAI* botAI)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot || !IsCompanion(bot))
        return;

    bool dungeon = bot->GetMap() && (bot->GetMap()->IsDungeon() || bot->GetMap()->IsRaid());
    bool groupAllowed = IsGroupedWithOwner(bot) || (bot->GetMap() && bot->GetMap()->IsDungeon() && IsInAuthorizedCoopGroup(bot));
    bool on = dungeon && groupAllowed && LeadModeOn(bot->GetGUID().GetCounter());
    bool tank = PlayerbotAI::IsTank(bot);
    bool wantLead = on && tank;
    bool wantAssist = on && !tank;

    auto sync = [botAI](char const* name, bool want, BotState state) {
        bool has = botAI->HasStrategy(name, state);
        if (want && !has)
            botAI->ChangeStrategy(std::string("+") + name, state);
        else if (!want && has)
            botAI->ChangeStrategy(std::string("-") + name, state);
    };
    sync("companion lead", wantLead, BOT_STATE_NON_COMBAT);
    sync("companion assist", wantAssist, BOT_STATE_NON_COMBAT);
    sync("companion assist", wantAssist, BOT_STATE_COMBAT);
    sync("companion chain", wantLead, BOT_STATE_COMBAT);
    CompanionClass::SyncStrategies(botAI);   // class play tweaks (CompanionClassStrategy.cpp)
    CompanionGather::SyncStrategies(botAI);  // phase 2: dungeon gathering, fishing weapon guard
}

bool CompanionBotMgr::SendProgressChat(PlayerbotAI* botAI, std::string const& text)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot || !IsGroupedWithOwner(bot))
        return false;   // not grouped: no progress lines

    uint32 now = static_cast<uint32>(GameTime::GetUptime().count());
    uint32 low = bot->GetGUID().GetCounter();
    {
        std::lock_guard<std::mutex> lock(_stateMutex);
        auto it = _lastPartyChat.find(low);
        if (it != _lastPartyChat.end() && now - it->second < sPlayerbotAIConfig.companionPartyChatSeconds)
            return false;
        _lastPartyChat[low] = now;
    }
    return botAI->SayToParty(text);
}

bool CompanionBotMgr::SendMilestone(PlayerbotAI* botAI, std::string const& type, std::string const& text)
{
    Player* bot = botAI ? botAI->GetBot() : nullptr;
    if (!bot || !IsCompanion(bot))
        return false;

    uint32 now = static_cast<uint32>(GameTime::GetUptime().count());
    std::string key = type + ":" + std::to_string(bot->GetGUID().GetCounter());
    {
        std::lock_guard<std::mutex> lock(_stateMutex);
        auto it = _lastMilestone.find(key);
        if (it != _lastMilestone.end() && now - it->second < sPlayerbotAIConfig.companionMilestoneSeconds)
            return false;
        _lastMilestone[key] = now;
    }

    if (IsGroupedWithOwner(bot))
        return botAI->SayToParty(text);

    Player* owner = FindOwnerOnlineFor(bot);
    if (!owner)
        return false;
    WorldPacket data;
    ChatHandler::BuildChatPacket(data, CHAT_MSG_WHISPER, LANG_UNIVERSAL, bot, nullptr, text);
    owner->SendDirectMessage(&data);
    return true;
}

bool CompanionBotMgr::IsXpCapped(Player const* player) const
{
    if (!player)
        return false;
    uint32 const ownerLevel = OwnerLevelFor(player);
    if (!ownerLevel)
        return _ownerConfigured.load() && _xpWaitForOwner.load();   // B10: closed until the owner's level is read
    return player->GetLevel() >= ownerLevel + sPlayerbotAIConfig.companionLevelLead;
}

Player* CompanionBotMgr::FindOwnerOnline() const
{
    uint32 ownerLow = _ownerLow.load();
    if (!ownerLow)
        return nullptr;
    return ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(ownerLow));
}

// Combat stuck (G1). The combat clock is per companion: the first TickBot sample that sees it in combat starts it, and
// any sample out of combat or dead resets it. When the clock passes CombatStuck.Seconds on a companion that is alive,
// not grouped with the owner and not in an instance or battleground, it clears combat (CombatStop, threat lists) and
// teleports to the nearest graveyard of its team when that graveyard is on its own map. At most one unstuck per 10
// minutes; three in an hour log a warning. Log: "Companion <Name>: combat stuck unstuck sec=<n> map=<m> from=x,y,z
// to=x,y,z zone=<z> attackers=<n> maxAttLevel=<n> action=<teleport|clear>". Companions only (TickBot).
void CompanionBotMgr::CheckCombatStuck(PlayerbotAI* botAI, Player* bot, bool groupedWithOwner, uint32 nowSec)
{
    uint32 const low = bot->GetGUID().GetCounter();
    bool const inCombat = bot->IsAlive() && bot->IsInCombat();
    bool const eligible = !groupedWithOwner && !bot->GetMap()->Instanceable() && !bot->InBattleground();
    uint32 combatSec = 0;
    {
        std::lock_guard<std::mutex> lock(_stateMutex);
        CombatStuckState& st = _combatStuck[low];
        if (!inCombat)
        {
            st.combatSince = 0;
            return;
        }
        if (!st.combatSince)
            st.combatSince = nowSec;
        if (!sPlayerbotAIConfig.companionCombatStuckEnabled || !eligible)
            return;
        combatSec = nowSec - st.combatSince;
        if (combatSec < sPlayerbotAIConfig.companionCombatStuckSeconds)
            return;
        if (st.lastUnstuck && nowSec - st.lastUnstuck < 600)
            return;
        st.lastUnstuck = nowSec;
        st.combatSince = 0;
        st.unstuckTimes.erase(std::remove_if(st.unstuckTimes.begin(), st.unstuckTimes.end(),
                                             [nowSec](uint32 t) { return nowSec - t >= 3600; }),
                              st.unstuckTimes.end());
        st.unstuckTimes.push_back(nowSec);
        if (st.unstuckTimes.size() >= 3)
            LOG_WARN("playerbots", "Companion {}: combat stuck unstuck {} times in the last hour (map={} zone={})",
                     bot->GetName(), st.unstuckTimes.size(), bot->GetMapId(), bot->GetZoneId());
    }

    // Attackers and their level, read before the fight is cleared.
    uint32 attackers = 0;
    uint32 maxAttLevel = 0;
    for (Unit* attacker : bot->getAttackers())
    {
        ++attackers;
        maxAttLevel = std::max<uint32>(maxAttLevel, attacker->GetLevel());
    }

    float const fromX = bot->GetPositionX(), fromY = bot->GetPositionY(), fromZ = bot->GetPositionZ();
    bot->GetThreatMgr().RemoveMeFromThreatLists();
    bot->GetThreatMgr().ClearAllThreat();
    bot->CombatStop(true);

    GraveyardStruct const* grave = sGraveyard->GetClosestGraveyard(bot, bot->GetTeamId());
    bool teleported = false;
    float toX = fromX, toY = fromY, toZ = fromZ;
    if (grave && grave->Map == bot->GetMapId())
    {
        toX = grave->x;
        toY = grave->y;
        toZ = grave->z;
        bot->RemoveAurasWithInterruptFlags(AURA_INTERRUPT_FLAG_TELEPORTED | AURA_INTERRUPT_FLAG_CHANGE_MAP);
        teleported = bot->TeleportTo(grave->Map, toX, toY, toZ, bot->GetOrientation());
    }

    LOG_INFO("playerbots",
             "Companion {}: combat stuck unstuck sec={} map={} from={:.1f},{:.1f},{:.1f} to={:.1f},{:.1f},{:.1f} "
             "zone={} attackers={} maxAttLevel={} action={}",
             bot->GetName(), combatSec, bot->GetMapId(), fromX, fromY, fromZ, toX, toY, toZ, bot->GetZoneId(),
             attackers, maxAttLevel, teleported ? "teleport" : "clear");
    if (teleported)
        SendMilestone(botAI, "unstuck", "I was stuck in a fight, so I left it and went to the nearest graveyard.");
}

void CompanionBotMgr::TickBot(PlayerbotAI* botAI)
{
    Player* bot = botAI->GetBot();
    if (!bot || !bot->IsInWorld())
        return;

    uint32 now = static_cast<uint32>(GameTime::GetUptime().count());
    if (now < botAI->GetCompanionNextSync())
        return;
    botAI->SetCompanionNextSync(now + SYNC_INTERVAL_SECONDS);

    Player* owner = FindOwnerOnlineFor(bot);
    Group* group = bot->GetGroup();
    // Membership is checked by the owner's GUID, not by his in-world state: a dungeon-finder teleport takes the
    // owner out of the world for a moment, and the companion must not read that as "not grouped".
    uint32 ownerLow = OwnerLowFor(bot);
    bool groupedWithOwner = group && ownerLow && group->IsMember(ObjectGuid::Create<HighGuid::Player>(ownerLow));

    // Combat stuck (G1) runs before the gates below, so the combat clock also runs for grouped companions.
    CheckCombatStuck(botAI, bot, groupedWithOwner, now);

    ApplyLeadStrategies(botAI);

    // Bags full: one milestone (rate limited) so the owner knows the companion is carrying loot it cannot store.
    uint32 freeSlots = 0;
    for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
        if (!bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
            ++freeSlots;
    for (uint8 bagSlot = INVENTORY_SLOT_BAG_START; bagSlot < INVENTORY_SLOT_BAG_END; ++bagSlot)
    {
        Bag* bag = (Bag*)bot->GetItemByPos(INVENTORY_SLOT_BAG_0, bagSlot);
        if (!bag)
            continue;
        for (uint32 i = 0; i < bag->GetBagSize(); ++i)
            if (!bag->GetItemByPos(i))
                ++freeSlots;
    }
    if (freeSlots == 0 && groupedWithOwner)
        SendMilestone(botAI, "bags", "My bags are full.");

    if (groupedWithOwner)
    {
        // The owner's group always wins: it ends any lease (no failure, no cooldown).
        CompanionLease::RequestInterrupt(bot, CompanionLease::Interrupt::Group);
        if (botAI->GetCompanionMode() != PlayerbotAI::COMPANION_FOLLOW)
        {
            botAI->SetMaster(owner);
            botAI->ChangeStrategy("+follow,-new rpg,-quest,-gather,-grind,-companion duty,-companion quest", BOT_STATE_NON_COMBAT);
            botAI->SetCompanionMode(PlayerbotAI::COMPANION_FOLLOW);
            CompanionDuty::CompleteDuty(bot->GetGUID().GetCounter(), "cleared: grouped with owner",
                                        CompanionDuty::DutyEnd::Interrupted, "interrupt:group");
        }
        return;
    }

    // Phase II (Card II-05): Explicit COMPANION_COOPERATIVE mode for authorized bot-only 5-player
    // dungeon groups (gated by AiPlayerbot.Companion.Coop.Enabled=0 by default). Owner groups ALWAYS
    // take precedence above; arbitrary/unrecognized groups are still disbanded below.
    if (group && IsInAuthorizedCoopGroup(bot))
    {
        CompanionLease::RequestInterrupt(bot, CompanionLease::Interrupt::Group);
        Player* leader = ObjectAccessor::FindConnectedPlayer(group->GetLeaderGUID());
        bool const isLeader = (leader == bot);
        if (botAI->GetCompanionMode() != PlayerbotAI::COMPANION_COOPERATIVE)
        {
            if (isLeader)
                botAI->ChangeStrategy("-follow,-stay,-new rpg,-quest,-gather,-grind,-companion duty,-companion quest",
                                      BOT_STATE_NON_COMBAT);
            else
                botAI->ChangeStrategy("+follow,-stay,-new rpg,-quest,-gather,-grind,-companion duty,-companion quest",
                                      BOT_STATE_NON_COMBAT);
            botAI->SetCompanionMode(PlayerbotAI::COMPANION_COOPERATIVE);
            CompanionDuty::CompleteDuty(bot->GetGUID().GetCounter(), "cleared: cooperative dungeon group",
                                        CompanionDuty::DutyEnd::Interrupted, "interrupt:coop_group");
        }
        if (!isLeader && leader && botAI->GetMaster() != leader)
            botAI->SetMaster(leader);
        return;
    }

    // Not with the owner (and not in an authorized cooperative group): any stray group ends.
    if (group)
    {
        LOG_WARN("playerbots", "[Companion] {} left its group: owner {} not a member (owner online: {})",
                 bot->GetName(), ownerLow, owner ? "yes" : "no");
        botAI->LeaveOrDisbandGroup();
    }

    // Autonomy keeps the owner as master while the owner is online, so the owner's "summon" and whispered commands
    // still reach the bot. Always heal strategy drift too (e.g. if ResetStrategies() or an earlier "stay"/"follow"
    // whisper stripped companion duty / companion quest / new rpg while the bot is ungrouped in autonomy mode).
    bool const needsAutonomyFix =
        (botAI->GetCompanionMode() != PlayerbotAI::COMPANION_AUTONOMY) ||
        !botAI->HasStrategy("companion duty", BOT_STATE_NON_COMBAT) ||
        !botAI->HasStrategy("companion quest", BOT_STATE_NON_COMBAT) ||
        !botAI->HasStrategy("new rpg", BOT_STATE_NON_COMBAT) ||
        botAI->HasStrategy("follow", BOT_STATE_NON_COMBAT) ||
        botAI->HasStrategy("stay", BOT_STATE_NON_COMBAT);
    if (needsAutonomyFix)
    {
        if (botAI->GetCompanionMode() == PlayerbotAI::COMPANION_AUTONOMY)
            LOG_INFO("playerbots",
                     "Companion {}: restored drifted autonomy strategies (-follow,-stay,+new rpg,+companion duty,+companion quest)",
                     bot->GetName());
        botAI->ChangeStrategy("-follow,-stay,+new rpg,+quest,+loot,+gather,+grind,+companion duty,+companion quest",
                              BOT_STATE_NON_COMBAT);
        botAI->SetCompanionMode(PlayerbotAI::COMPANION_AUTONOMY);
    }
    if (botAI->GetMaster() != owner)
        botAI->SetMaster(owner);

    // Motionless-companion unstuck watchdog: if an alive, out-of-combat autonomous companion has not moved >= 3 yd
    // for 15 minutes (and is not crafting or fishing), exit any vehicle (e.g. holiday chair) and teleport cleanly
    // to its homebind (or nearest graveyard) so it can never freeze on terrain/props for hours.
    if (bot->IsAlive() && !bot->IsInCombat() && bot->IsInWorld() && bot->GetMap() && !bot->GetMap()->Instanceable())
    {
        struct MotionWatch
        {
            uint32 mapId = 0;
            float x = 0.0f;
            float y = 0.0f;
            uint32 sinceSec = 0;
            uint32 lastUnstuckSec = 0;
        };
        static std::mutex sMotionMutex;
        static std::unordered_map<uint32, MotionWatch> sMotion;

        uint32 const low = bot->GetGUID().GetCounter();
        uint32 const nowSec = static_cast<uint32>(GameTime::GetGameTime().count());
        uint32 const idleLimit = std::max<uint32>(
            180, sConfigMgr->GetOption<uint32>("AiPlayerbot.Companion.IdleUnstuck.Seconds", 900));
        std::string const activeDuty = CompanionDuty::ActiveName(low);
        bool const legitStill = (activeDuty == "craft" || activeDuty == "fish");

        bool doUnstuck = false;
        uint32 stuckFor = 0;
        {
            std::lock_guard<std::mutex> lock(sMotionMutex);
            MotionWatch& w = sMotion[low];
            float const dx = bot->GetPositionX() - w.x;
            float const dy = bot->GetPositionY() - w.y;
            bool const moved = (w.sinceSec == 0) || (w.mapId != bot->GetMapId()) || (dx * dx + dy * dy >= 9.0f);
            if (moved || legitStill)
            {
                w.mapId = bot->GetMapId();
                w.x = bot->GetPositionX();
                w.y = bot->GetPositionY();
                w.sinceSec = nowSec;
            }
            else if (nowSec > w.sinceSec && nowSec - w.sinceSec >= idleLimit &&
                     (w.lastUnstuckSec == 0 || nowSec - w.lastUnstuckSec >= idleLimit))
            {
                stuckFor = nowSec - w.sinceSec;
                w.sinceSec = nowSec;
                w.lastUnstuckSec = nowSec;
                doUnstuck = true;
            }
        }
        if (doUnstuck)
        {
            if (bot->GetVehicle())
                bot->ExitVehicle();
            bot->GetMotionMaster()->Clear();
            bot->StopMoving();
            botAI->rpgInfo.ChangeToIdle();
            botAI->Reset(true);
            CompanionDuty::CompleteDuty(low, "cleared: idle-unstuck", CompanionDuty::DutyEnd::Interrupted,
                                        "idle-unstuck");
            CompanionLease::End(low, "", "idle-unstuck", CompanionLease::Outcome::Neutral);
            if (bot->m_homebindMapId != MAPID_INVALID)
            {
                bool const alreadyHome = (bot->GetMapId() == bot->m_homebindMapId) &&
                                         (bot->GetDistance2d(bot->m_homebindX, bot->m_homebindY) <= 15.0f);
                LOG_INFO("playerbots",
                         "Companion {}: idle-unstuck motionless {}s at map={} ({:.1f},{:.1f}) -> {} map={} ({:.1f},{:.1f})",
                         bot->GetName(), stuckFor, bot->GetMapId(), bot->GetPositionX(), bot->GetPositionY(),
                         alreadyHome ? "reset-at-homebind" : "homebind",
                         bot->m_homebindMapId, bot->m_homebindX, bot->m_homebindY);
                if (!alreadyHome)
                {
                    bot->RemoveAurasWithInterruptFlags(AURA_INTERRUPT_FLAG_TELEPORTED | AURA_INTERRUPT_FLAG_CHANGE_MAP);
                    bot->TeleportTo(bot->m_homebindMapId, bot->m_homebindX, bot->m_homebindY, bot->m_homebindZ, 0.0f);
                }
                return;
            }
            if (GraveyardStruct const* grave = sGraveyard->GetClosestGraveyard(bot, bot->GetTeamId()))
            {
                LOG_INFO("playerbots",
                         "Companion {}: idle-unstuck motionless {}s at map={} ({:.1f},{:.1f}) -> graveyard map={} ({:.1f},{:.1f})",
                         bot->GetName(), stuckFor, bot->GetMapId(), bot->GetPositionX(), bot->GetPositionY(),
                         grave->Map, grave->x, grave->y);
                bot->RemoveAurasWithInterruptFlags(AURA_INTERRUPT_FLAG_TELEPORTED | AURA_INTERRUPT_FLAG_CHANGE_MAP);
                bot->TeleportTo(grave->Map, grave->x, grave->y, grave->z, 0.0f);
                return;
            }
        }
    }

    // Lease first (combat / death pause, resume, stall, timeout), then the duty pick that may take a new lease.
    uint32 const s0 = getMSTime();
    CompanionLease::Tick(botAI);
    uint32 const s1 = getMSTime();
    CompanionTrip::CombatTick(botAI);   // mounted-in-combat safety net, "trip combat end" log
    uint32 const s2 = getMSTime();
    CompanionDuty::Evaluate(botAI);
    uint32 const s3 = getMSTime();
    // Quest project (quests before grinding, trips to other zones, mount): decides while no duty runs.
    CompanionQuest::Evaluate(botAI);
    CompanionLife::TickBot(botAI);
    uint32 const s4 = getMSTime();
    uint32 const tot = getMSTimeDiff(s0, s4);
    if (tot >= 200)
        LOG_WARN("playerbots", "[SlowTickBot] {} total={}ms lease={}ms combat={}ms duty={}ms quest={}ms",
                 bot->GetName(), tot, getMSTimeDiff(s0, s1), getMSTimeDiff(s1, s2), getMSTimeDiff(s2, s3),
                 getMSTimeDiff(s3, s4));
}

void CompanionBotMgr::EnsureClassSpells(Player* player)
{
    if (!player)
        return;

    uint32 classMask = 1u << (player->getClass() - 1);
    for (ClassQuest const& entry : _classQuests)
    {
        if (!(entry.classMask & classMask) || entry.minLevel > player->GetLevel())
            continue;

        // A reward spell that teaches other spells (SPELL_EFFECT_LEARN_SPELL) grants those spells. Otherwise the
        // reward spell is itself the class ability.
        std::vector<uint32> spells;
        if (SpellInfo const* info = sSpellMgr->GetSpellInfo(entry.rewardSpell))
        {
            for (auto const& effect : info->GetEffects())
                if (effect.Effect == SPELL_EFFECT_LEARN_SPELL && effect.TriggerSpell)
                    spells.push_back(effect.TriggerSpell);
        }
        if (spells.empty())
            spells.push_back(entry.rewardSpell);

        for (uint32 spellId : spells)
        {
            if (player->HasSpell(spellId))
            {
                LOG_DEBUG("playerbots", "Companion {} class quest {}: spell {} already known", player->GetName(),
                          entry.questId, spellId);
                continue;
            }
            player->learnSpell(spellId);
            LOG_INFO("playerbots", "Companion {} class quest {}: spell {} taught", player->GetName(), entry.questId,
                     spellId);
        }

        // Mark the quest rewarded so the bot never tries to do it.
        if (!player->GetQuestRewardStatus(entry.questId))
        {
            if (player->GetQuestStatus(entry.questId) != QUEST_STATUS_NONE)
                player->RemoveActiveQuest(entry.questId, false);
            player->SetRewardedQuest(entry.questId);
            LOG_INFO("playerbots", "Companion {} class quest {} marked rewarded", player->GetName(), entry.questId);
        }
    }
}
