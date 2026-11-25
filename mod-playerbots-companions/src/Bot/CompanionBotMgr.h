/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANIONBOTMGR_H
#define PLAYERBOTS_COMPANIONBOTMGR_H

#include <array>
#include <atomic>
#include <mutex>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

#include "ObjectGuid.h"
#include "Position.h"

class Item;
struct ItemTemplate;
class Player;
class PlayerbotAI;

// Companion bots: a fixed set of named characters (AiPlayerbot.CompanionBots) that stay online with no master,
// level by questing when alone, follow the owner (AiPlayerbot.CompanionOwner) when grouped, never get randomized,
// and gain no XP once they reach owner level + AiPlayerbot.CompanionLevelLead.
//
// Threads:
//   Update(), owner-level refresh, login and class-spell passes run on the world thread.
//   IsCompanion(), IsXpCapped() and TickBot() run on map threads and only read state that the world
//   thread writes through atomics or fixed-at-init data.
class CompanionBotMgr
{
public:
    static CompanionBotMgr& instance();

    // World thread. Call once per world tick.
    void Update(uint32 elapsed);

    // Any thread. Case-insensitive name match against AiPlayerbot.CompanionBots.
    bool IsCompanion(Player const* player) const;
    bool IsCompanionName(std::string const& name) const;

    // Any thread. True when the companion may gain no XP (level >= owner level + lead). While an owner is configured
    // and his level is not known yet, also true (AiPlayerbot.Companion.XpCap.WaitForOwnerLevel, default 1).
    bool IsXpCapped(Player const* player) const;
    // Any thread. True while an owner is configured but his level has not been read yet.
    bool OwnerConfigured() const { return _ownerConfigured.load(); }
    bool OwnerLevelUnknown() const { return _ownerConfigured.load() && _ownerLevel.load() == 0; }
    // Any thread. Owner character low GUID, 0 when unknown.
    uint32 OwnerLow() const { return _ownerLow.load(); }

    // Any thread. False when equipping proto would replace an equipped heirloom (quality 7) in any slot it can take.
    // Heirlooms themselves may go on an empty slot or over a non-heirloom. Also applies the armour-type rule.
    bool CanEquip(Player* player, ItemTemplate const* proto) const;
    // Any thread. Body armour must be the best type the class wears at the level (warrior/paladin: mail under 40,
    // plate from 40; hunter: leather under 40, mail from 40; rogue: leather). Heirlooms are exempt (they scale).
    bool ArmorTypeAllowed(Player const* player, ItemTemplate const* proto) const;
    // Any thread. True when the equipment slot holds an heirloom (quality 7).
    bool SlotHoldsHeirloom(Player const* player, uint8 slot) const;
    // Bot's own map thread. Tank shield rule (AiPlayerbot.Companion.TankShield.Enabled, default 1): true for a
    // companion shield tank (warrior or paladin, Protection by talents, knows the Shield skill) that owns a shield
    // (equipped or in the bags). Such a companion never puts a weapon or held item in the off hand, and a shield may
    // replace a weapon (even an heirloom) that sits there.
    bool TankKeepsShield(Player const* player) const;
    // Any thread. Item is a shield (armour subclass shield).
    static bool IsShield(ItemTemplate const* proto);
    // Any thread. Item goes only into the off hand and is not a shield (off-hand weapon or held-in-off-hand item).
    static bool IsOffhandNonShield(ItemTemplate const* proto);
    // World thread (GM command). Equips heirlooms that sit in the bags, but only into slots that hold no heirloom.
    // Appends one line per heirloom to report.
    void RepairHeirlooms(Player* bot, std::vector<std::string>& report);
    // Any thread. True when the player is the configured owner.
    bool IsOwner(Player const* player) const;
    // Any thread. True when the low GUID is the configured owner's.
    bool IsOwnerLow(uint32 lowGuid) const { return _ownerLow.load() != 0 && lowGuid == _ownerLow.load(); }
    // Any thread. True when the companion is in a group with the owner.
    bool IsGroupedWithOwner(Player const* bot) const;

    // Map thread, once per companion sync interval. Chooses follow or autonomy strategies and leaves stray groups.
    void TickBot(PlayerbotAI* botAI);

    // Any thread, read-only after load. Used when no quest giver is in range, so the companion walks toward one.
    // Pass 0: a COMPLETE quest in the bot's log whose ender spawn is on the bot's map (walk).
    // Pass 1: nearest spawn on the bot's map, quest level in (level - 4 .. level + 3), at least 150 yards away.
    // Pass 2 (widened): nearest spawn on the bot's map, quest level in (level - 6 .. level + 3), any distance.
    // Pass 3 (travel fallback): nearest spawn on any other map, quest level in (level - 4 .. level + 3).
    // Pass 3 is a location-only teleport, performed by MoveFarTo. Every pass also needs the spot's zone to fit the
    // level, the quest's MinLevel to be at most the level, the quest's races to include the bot's, and the giver not
    // to be hostile to the bot's team. False when nothing fits or the list is not loaded.
    bool PickQuestGiverSpot(Player const* bot, WorldLocation& out) const;

    // Any thread, read-only after load. True when the zone's mob level range fits the level: lo - 3 <= level <= hi + 2.
    // A zone with no range (unknown, or too few samples) fits.
    bool ZoneFitsLevel(uint32 zoneId, uint32 level) const;
    // Any thread. The zone's level range (lo, hi) when it has one.
    bool ZoneLevelRange(uint32 zoneId, uint32& lo, uint32& hi) const;
    // Any thread (read-only after load). O(1) coarse 128-yd grid lookup of zoneId built once at startup (never loads
    // map/vmap tiles from disk). Returns 0 if unknown.
    uint32 CachedZoneAt(uint32 mapId, float x, float y) const;
    // Any thread (read-only after load). True when ZoneAvoid is enabled and zoneId's 10th-percentile mob level is at
    // least bot->GetLevel() + DangerLevelGap (e.g. Burning Steppes or Winterspring for a level-27..31 companion).
    bool IsZoneTooDangerous(Player const* bot, uint32 zoneId) const;
    // Map thread (same map as bot). True when the point lies in a zone too dangerous for bot (uses CachedZoneAt).
    bool IsPointTooDangerous(Player const* bot, uint32 mapId, float x, float y, float z) const;
    // Map thread (same map as bot). Samples points along the straight line from bot to (x,y,z) via CachedZoneAt
    // (never triggers Map::GetZoneId tile loads) and returns true if any sample zone is too dangerous for bot.
    bool IsPathTooDangerous(Player const* bot, uint32 mapId, float x, float y, float z) const;
    // Any thread. True for configured companions AND (when GuildRoster.ProtectInventory=1) any bot in the owner's
    // guild so RandomPlayerbotMgr::Refresh/Randomize never wipes guild-roster bags, gear, or mats.
    bool IsProtectedGuildMember(Player const* bot) const;
    uint32 OwnerGuildId() const { return _ownerGuildId.load(); }
    // Any thread. Zone of a destination for the companion's level check: the bot's own map is read directly, another
    // map uses the zone stored with the matching quest-giver spot. 0 when unknown.
    uint32 DestZone(Player const* bot, WorldLocation const& dest) const;

    // Duty spawns: vendors, repairers, trainers and guild banks on the companion maps, read from the spawn and template
    // caches at load (no SQL). Read-only after load. A duty walks to one of these when none of its kind is in range.
    enum class DutySpotKind : uint8
    {
        Vendor = 0,
        Repair = 1,
        Trainer = 2,
        Bank = 3,
        Innkeeper = 4,   // B13: the bind (spell 3286) creates a hearthstone when the companion has none
    };
    struct DutySpot
    {
        uint64 key = 0;             // spawn id, with bit 63 set for a game object
        uint32 spawnId = 0;         // creature or game object spawn guid (low part)
        uint32 entry = 0;           // creature or game object template entry
        bool isGo = false;
        float x = 0.0f, y = 0.0f, z = 0.0f;
        uint32 zoneId = 0;          // 0 = unknown
        uint32 zoneTeam = 0;        // AreaTable team: AREATEAM_NONE, AREATEAM_ALLY, AREATEAM_HORDE or AREATEAM_ANY
        uint32 hostileMask = 0;     // creature faction hostileMask (0 for game objects)
    };
    // Any thread. Spawns of a kind on a map. Empty before the load or for a map with none.
    std::vector<DutySpot> const& DutySpots(uint32 mapId, DutySpotKind kind) const;

    // Quest-giver spawns (one row per quest per spawn), read at load. Shared by the quest project (CompanionQuest
    // builds its hubs from them). Read-only after load.
    struct QuestGiverSpot
    {
        uint32 map = 0;
        uint32 questId = 0;
        uint32 questLevel = 0;
        uint32 minLevel = 0;         // quest MinLevel
        uint32 allowableRaces = 0;   // quest AllowableRaces mask, 0 = all
        uint32 hostileMask = 0;      // giver faction hostileMask (bit 4 = hostile to Horde, bit 2 = to Alliance)
        uint32 zoneId = 0;           // 0 = unknown
        float x = 0.0f, y = 0.0f, z = 0.0f;
    };
    // Any thread. True once the quest-giver spots, enders, zone ranges and duty spots are loaded.
    bool QuestGiverSpotsReady() const { return _questGiverSpotsReady.load(); }
    // Any thread. Every quest-giver spot (empty before the load).
    std::vector<QuestGiverSpot> const& QuestGiverSpots() const;
    // Any thread. Table sizes for the memory line.
    size_t ZoneLevelCount() const { return _questGiverSpotsReady.load() ? _zoneLevels.size() : 0; }
    size_t QuestEnderSpotCount() const;
    // Any thread, rate limited (once per companion per 10 minutes): the teleport-skipped line.
    bool ShouldLogTeleportSkip(uint32 lowGuid, uint32 nowSec);

    // World thread. True at most once per companion per 10 minutes, so the XP-cap proof line is not spammed.
    bool ShouldLogXpBlock(uint32 lowGuid, uint32 nowSec);
    // Any thread. True at most once per companion per 10 minutes, for the location-only teleport line.
    bool ShouldLogTeleport(uint32 lowGuid, uint32 nowSec);

    // Any thread. Last quest accept and turn-in seen since the worldserver started (for .companion status).
    struct QuestEvent
    {
        uint32 questId = 0;
        uint32 atSec = 0;
    };
    void NoteQuestAccept(uint32 lowGuid, uint32 questId, uint32 nowSec);
    void NoteQuestTurnIn(uint32 lowGuid, uint32 questId, uint32 nowSec);
    QuestEvent LastQuestAccept(uint32 lowGuid) const;
    QuestEvent LastQuestTurnIn(uint32 lowGuid) const;

    // Configured companion names as written in AiPlayerbot.CompanionBots.
    std::vector<std::string> CompanionNames() const { return _displayNames; }

    // World thread. Learns every class-quest spell the companion's class has at or below its level and marks the
    // class quests rewarded so the bot never tries them. Logs one line per spell.
    void EnsureClassSpells(Player* player);

    uint32 GetOwnerLevel() const { return _ownerLevel.load(); }

    // Companion lead mode (dungeon pulls with the owner). Per companion; the default is AiPlayerbot.Companion.Lead.Default.
    // Any thread.
    bool LeadModeOn(uint32 lowGuid) const;
    void SetLeadMode(uint32 lowGuid, bool on);
    // Map thread. Records the last time a group member of the companion was in combat (ready-check pause).
    void NoteGroupCombat(uint32 lowGuid, uint32 nowSec);
    uint32 LastGroupCombat(uint32 lowGuid) const;
    // Map thread. Adds or removes the lead (tank) or assist (damage) strategies to match the companion's state:
    // lead mode on, grouped with the owner, inside a dungeon or raid.
    void ApplyLeadStrategies(PlayerbotAI* botAI);

    // Combat stuck (G1). Runs first in TickBot, so the continuous-combat clock also runs for grouped companions.
    void CheckCombatStuck(PlayerbotAI* botAI, Player* bot, bool groupedWithOwner, uint32 nowSec);

    // Companion chat. Progress lines go to party chat, at most one per companion per
    // AiPlayerbot.Companion.Chat.PartyIntervalSeconds; progress lines are dropped when not grouped with the owner.
    bool SendProgressChat(PlayerbotAI* botAI, std::string const& text);
    // Milestones (level up, profession rank, gear upgrade, bags full, location-only teleport): party chat when grouped,
    // whispered to the owner when not. At most one per type per companion per
    // AiPlayerbot.Companion.Chat.MilestoneIntervalSeconds.
    bool SendMilestone(PlayerbotAI* botAI, std::string const& type, std::string const& text);

    // Any thread. The default owner if online, else nullptr.
    Player* FindOwnerOnline() const;
    // Any thread. Per-companion owner helpers (supports AiPlayerbot.Companion.Owner.<Name>, e.g. Logan's core 4).
    uint32 OwnerLowFor(Player const* bot) const;
    uint32 OwnerLevelFor(Player const* bot) const;
    Player* FindOwnerOnlineFor(Player const* bot) const;
    // Companion play schedules (acore_characters.companion_schedule).
    struct ScheduleEntry
    {
        std::string shiftGroup = "always";
        uint8 startHour = 0;
        uint8 endHour = 24;
        uint8 daysMask = 127;
        bool alwaysOnline = true;
    };
    void ReloadSchedules();
    bool IsScheduledOnline(std::string const& name, Player const* bot = nullptr) const;
    std::string ScheduleSummary() const;

    static constexpr uint32 SYNC_INTERVAL_SECONDS = 10;
    static constexpr uint32 LOGIN_RETRY_MS = 30000;
    static constexpr uint32 OWNER_LEVEL_REFRESH_MS = 60000;
    static constexpr uint32 SCHEDULE_REFRESH_MS = 300000;
    static constexpr uint32 SPELL_PASS_MS = 15000;

private:
    CompanionBotMgr() = default;

    struct ClassQuest
    {
        uint32 classMask;   // AllowableClasses bit: 1 << (classId - 1)
        uint32 questId;
        uint32 minLevel;
        uint32 rewardSpell;
    };

    void LoadConfig();
    void LoadClassQuests();
    void LoadQuestGiverSpots();
    void LoadDutySpots();
    bool BuildZoneLevels();
    static uint32 ZoneOf(uint32 mapId, float x, float y, float z);
    void LoginMissingCompanions();
    void RefreshOwnerLevel();
    void SyncLevelSpells();

    std::atomic<bool> _configLoaded{false};     // published after the fields below are filled
    bool _classQuestsLoaded = false;
    std::set<std::string> _names;               // lower-case; written before _configLoaded is set
    std::vector<std::string> _displayNames;     // as configured, for login by name
    std::string _ownerName;
    std::atomic<uint32> _ownerLow{0};           // owner character low GUID, 0 when unknown
    std::atomic<uint32> _ownerGuildId{0};       // owner character guild id, 0 when unknown
    std::atomic<uint32> _ownerLevel{0};
    std::unordered_map<std::string, std::string> _botOwnerName; // lower bot name -> configured owner name (if custom)
    std::unordered_map<std::string, uint32> _botOwnerLow;       // lower bot name -> resolved owner low guid
    std::set<uint32> _allOwnerLows;                             // all configured owner low guids (Wonky, Logan, ...)
    std::unordered_map<uint32, uint32> _ownerLevels;            // owner low guid -> cached level (under _stateMutex)
    std::unordered_map<std::string, ScheduleEntry> _schedules;  // lower bot name -> play schedule (under _stateMutex)
    bool _schedulesLoaded = false;
    bool _scheduleMissingLogged = false;
    uint32 _scheduleTimer = 0;
    std::atomic<bool> _ownerConfigured{false};      // AiPlayerbot.CompanionOwner is set
    std::atomic<bool> _xpWaitForOwner{true};        // AiPlayerbot.Companion.XpCap.WaitForOwnerLevel
    std::vector<ClassQuest> _classQuests;
    // Armour types per companion (lower-case name -> tokens: cloth, leather, mail, plate, best). Read at load.
    std::unordered_map<std::string, std::vector<std::string>> _armorTypes;
    struct ZoneRange
    {
        uint32 lo = 0;
        uint32 hi = 0;
    };
    std::vector<QuestGiverSpot> _questGiverSpots;   // written once on the world thread
    std::unordered_map<uint32, std::vector<QuestGiverSpot>> _questEnderSpots;   // quest id -> ender spawns
    std::unordered_map<uint32, ZoneRange> _zoneLevels;                         // zone id -> mob level range
    std::unordered_map<uint64, uint32> _coarseZoneGrid;                        // (map, cx, cy) 128-yd cell -> zoneId
    std::array<std::unordered_map<uint32, std::vector<DutySpot>>, 5> _dutySpots;   // kind -> map -> spawns
    std::atomic<bool> _dutySpotsReady{false};   // published after _dutySpots is filled
    std::atomic<bool> _questGiverSpotsReady{false}; // published after the spots, enders and zones are filled
    bool _questGiverSpotsLoaded = false;
    mutable std::mutex _spotLogMutex;                                // PickQuestGiverSpot runs on map threads
    mutable std::unordered_map<uint32, std::string> _lastSpotChoice; // low guid -> last logged choice
    std::unordered_map<uint32, uint32> _lastTeleportSkipLog;        // low guid -> seconds of last skip line
    std::unordered_map<uint32, uint32> _lastSpellPassLevel;   // low guid -> level of last class-spell pass
    std::unordered_map<uint32, uint32> _lastXpBlockLog;       // low guid -> seconds of last XP-cap log line
    std::unordered_map<uint32, uint32> _lastTeleportLog;      // low guid -> seconds of last teleport log line
    mutable std::mutex _questEventMutex;                      // quest events come from map threads
    mutable std::mutex _stateMutex;                           // lead mode, group combat and chat rate limits
    std::unordered_map<uint32, bool> _leadMode;               // low guid -> lead mode (explicit owner choice)
    std::unordered_map<uint32, uint32> _lastGroupCombat;      // low guid -> seconds a group member was last in combat
    std::unordered_map<uint32, uint32> _lastPartyChat;        // low guid -> seconds of last progress line
    std::unordered_map<std::string, uint32> _lastMilestone;   // "type:low guid" -> seconds of last milestone
    std::unordered_map<uint32, QuestEvent> _lastAccept;       // low guid -> last accept
    std::unordered_map<uint32, QuestEvent> _lastTurnIn;       // low guid -> last turn-in
    struct CombatStuckState
    {
        uint32 combatSince = 0;               // seconds (uptime) of the first sample of the current combat, 0 = none
        uint32 lastUnstuck = 0;               // seconds of the last unstuck (rate limit: 10 minutes)
        std::vector<uint32> unstuckTimes;     // seconds of unstucks in the last hour (warning at three)
    };
    std::unordered_map<uint32, CombatStuckState> _combatStuck;   // low guid -> combat clock (under _stateMutex)
    uint32 _loginTimer = 0;
    uint32 _levelTimer = 0;
    uint32 _spellTimer = 0;
    uint32 _loginWarned = 0;
};

#define sCompanionBotMgr CompanionBotMgr::instance()

#endif
