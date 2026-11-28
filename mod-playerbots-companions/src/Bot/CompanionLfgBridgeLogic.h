/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANION_LFG_BRIDGE_LOGIC_H
#define PLAYERBOTS_COMPANION_LFG_BRIDGE_LOGIC_H

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "CompanionCoopAuthLogic.h"

// Pure C++17 native WotLK 3.3.5a Dungeon Finder (RDF/LFG) execution bridge for voluntary cooperative dungeon runs
// (Issue #20). Zero AzerothCore header dependencies so CompanionBrain.cpp, LfgActions.cpp, Playerbots.cpp,
// CompanionBotMgr.cpp, and standalone native C++17 tests compile and enforce identical rules.
//
// Core invariants:
// - Native WotLK RDF/LFG is primary; MySQL + native LFGMgr is authoritative (SQLite is read projection only).
// - Unsupported dungeons, heroics, raids, and random dungeons fail CLOSED.
// - Core class role eligibility, level bands, faction, Deserter/Cooldown auras, and locked dungeons are preserved.
// - No double-queueing (checks native LFG state AND bounded in-flight join/leave windows).
// - Safe-boundary proposal acceptance: defers in combat / flight / vehicle / cast / fall up to
//   DEFAULT_PROPOSAL_COMBAT_DEFER_SEC (25s) and enforces an unconditional hard cutoff at
//   DEFAULT_PROPOSAL_HARD_TIMEOUT_SEC (35s < 40s native LFG_TIME_PROPOSAL) so expired proposals are NEVER accepted.
// - Bot-only LFG queue matching (PlayerbotsScript::OnPlayerbotCheckLFGQueue) permits ONLY exact 5-member confirmed
//   cooperative rosters under CoopEnabled() == true; mixed combos involving any bridge-tracked coop bot + human or
//   random bot are rejected so a coop opportunity roster can never be split.
namespace CompanionLfgBridge
{
inline constexpr std::uint32_t DEFAULT_JOIN_INFLIGHT_SEC = 15;
inline constexpr std::uint32_t DEFAULT_LEAVE_INFLIGHT_SEC = 10;
inline constexpr std::uint32_t DEFAULT_PROPOSAL_COMBAT_DEFER_SEC = 25;
inline constexpr std::uint32_t DEFAULT_PROPOSAL_HARD_TIMEOUT_SEC = 35;  // strictly < 40s LFG_TIME_PROPOSAL
inline constexpr std::uint32_t DEFAULT_DECLINE_BACKOFF_SEC = 30;
inline constexpr std::uint32_t DEFAULT_STALE_QUEUE_REQUEUE_SEC = 90;

// WotLK 3.3.5a LFG role masks (LFG.h)
inline constexpr std::uint8_t ROLE_MASK_NONE = 0x00;
inline constexpr std::uint8_t ROLE_MASK_LEADER = 0x01;
inline constexpr std::uint8_t ROLE_MASK_TANK = 0x02;
inline constexpr std::uint8_t ROLE_MASK_HEALER = 0x04;
inline constexpr std::uint8_t ROLE_MASK_DAMAGE = 0x08;

// WotLK 3.3.5a LFG states (LFG.h)
inline constexpr std::uint8_t NATIVE_LFG_STATE_NONE = 0;
inline constexpr std::uint8_t NATIVE_LFG_STATE_ROLECHECK = 1;
inline constexpr std::uint8_t NATIVE_LFG_STATE_QUEUED = 2;
inline constexpr std::uint8_t NATIVE_LFG_STATE_PROPOSAL = 3;
inline constexpr std::uint8_t NATIVE_LFG_STATE_DUNGEON = 5;
inline constexpr std::uint8_t NATIVE_LFG_STATE_FINISHED_DUNGEON = 6;

// WotLK 3.3.5a LfgProposalState (LFGMgr.h)
inline constexpr std::uint8_t PROPOSAL_STATE_INITIATING = 0;
inline constexpr std::uint8_t PROPOSAL_STATE_FAILED = 1;
inline constexpr std::uint8_t PROPOSAL_STATE_SUCCESS = 2;

// WotLK 3.3.5a Class IDs (SharedDefines.h)
inline constexpr std::uint8_t CLASS_ID_WARRIOR = 1;
inline constexpr std::uint8_t CLASS_ID_PALADIN = 2;
inline constexpr std::uint8_t CLASS_ID_HUNTER = 3;
inline constexpr std::uint8_t CLASS_ID_ROGUE = 4;
inline constexpr std::uint8_t CLASS_ID_PRIEST = 5;
inline constexpr std::uint8_t CLASS_ID_DEATH_KNIGHT = 6;
inline constexpr std::uint8_t CLASS_ID_SHAMAN = 7;
inline constexpr std::uint8_t CLASS_ID_MAGE = 8;
inline constexpr std::uint8_t CLASS_ID_WARLOCK = 9;
inline constexpr std::uint8_t CLASS_ID_DRUID = 11;

struct SupportedLfgDungeon
{
    std::uint32_t lfgDungeonId = 0;   // WotLK 3.3.5a LFGDungeons.dbc ID
    std::uint32_t mapId = 0;
    std::uint8_t difficulty = 0;      // 0 = Normal 5-player, 1 = Heroic 5-player (raids >= 2 rejected)
    std::uint8_t minLevel = 15;       // fallback if DBC bounds not supplied
    std::uint8_t maxLevel = 80;
    std::uint8_t faction = 0;         // 0 = both, 1 = Alliance only, 2 = Horde only
    std::uint8_t expansion = 0;       // 0 = Classic, 1 = TBC, 2 = WotLK
    std::uint32_t finalBossEntry = 0; // instance_encounters.creditEntry (0 = spell/event credit via LFG_STATE_FINISHED_DUNGEON)
    char const* slug = "";
};

// Complete WotLK 3.3.5a 5-player RDF dungeon table (60 Normal + 32 Heroic = 92 dungeons) matching hearthcore/dungeons.py.
// Raids, battlegrounds, random dungeon IDs, and seasonal holiday events are excluded (fail closed -> nullptr).
// Live C++ callers also supply DBC bounds from sLFGDungeonStore so level rules follow the installed server DBC.
inline std::vector<SupportedLfgDungeon> const& SupportedDungeonTable()
{
    static std::vector<SupportedLfgDungeon> const kTable = {
        {12, 34, 0, 15, 34, 1, 0, 1716, "stockade_n"},
        {6, 36, 0, 15, 28, 0, 0, 639, "deadmines_n"},
        {1, 43, 0, 15, 25, 0, 0, 3654, "wailing_caverns_n"},
        {4, 389, 0, 15, 21, 2, 0, 11520, "ragefire_chasm_n"},
        {8, 33, 0, 16, 26, 0, 0, 4275, "sfk_n"},
        {10, 48, 0, 19, 29, 0, 0, 0, "bfd_n"},
        {16, 47, 0, 22, 32, 0, 0, 4421, "razorfen_kraul_n"},
        {14, 90, 0, 23, 33, 0, 0, 7800, "gnomeregan_n"},
        {18, 189, 0, 27, 37, 0, 0, 4543, "scarlet_monastery_graveyard_n"},
        {165, 189, 0, 30, 40, 0, 0, 6487, "scarlet_monastery_library_n"},
        {20, 129, 0, 32, 42, 0, 0, 7358, "razorfen_downs_n"},
        {163, 189, 0, 32, 42, 0, 0, 3975, "scarlet_monastery_armory_n"},
        {22, 70, 0, 35, 45, 0, 0, 2748, "uldaman_n"},
        {164, 189, 0, 35, 45, 0, 0, 3977, "scarlet_monastery_cathedral_n"},
        {272, 349, 0, 39, 49, 0, 0, 12236, "maraudon_purple_crystals_n"},
        {24, 209, 0, 41, 51, 0, 0, 7267, "zulfarrak_n"},
        {26, 349, 0, 41, 51, 0, 0, 12258, "maraudon_orange_crystals_n"},
        {273, 349, 0, 43, 53, 0, 0, 12201, "maraudon_pristine_waters_n"},
        {28, 109, 0, 45, 55, 0, 0, 5709, "sunken_temple_n"},
        {30, 230, 0, 47, 57, 0, 0, 9018, "blackrock_depths_prison_n"},
        {276, 230, 0, 51, 61, 0, 0, 9019, "blackrock_depths_upper_city_n"},
        {34, 429, 0, 53, 63, 0, 0, 11492, "dire_maul_east_n"},
        {2, 289, 0, 55, 65, 0, 0, 1853, "scholomance_n"},
        {32, 229, 0, 55, 65, 0, 0, 9568, "lower_blackrock_spire_n"},
        {36, 429, 0, 55, 65, 0, 0, 11486, "dire_maul_west_n"},
        {38, 429, 0, 55, 65, 0, 0, 11501, "dire_maul_north_n"},
        {40, 329, 0, 55, 65, 0, 0, 10813, "stratholme_main_gate_n"},
        {274, 329, 0, 55, 65, 0, 0, 10440, "stratholme_service_entrance_n"},
        {136, 543, 0, 57, 67, 0, 1, 17537, "hellfire_ramparts_n"},
        {137, 542, 0, 59, 68, 0, 1, 0, "blood_furnace_n"},
        {140, 547, 0, 60, 69, 0, 1, 17942, "slave_pens_n"},
        {146, 546, 0, 61, 70, 0, 1, 17882, "underbog_n"},
        {148, 557, 0, 62, 71, 0, 1, 18344, "mana_tombs_n"},
        {149, 558, 0, 63, 72, 0, 1, 18373, "auchenai_crypts_n"},
        {170, 560, 0, 64, 73, 0, 1, 18096, "escape_from_durnholde_n"},
        {150, 556, 0, 65, 73, 0, 1, 18473, "sethekk_halls_n"},
        {138, 540, 0, 67, 75, 0, 1, 16808, "shattered_halls_n"},
        {147, 545, 0, 67, 75, 0, 1, 17798, "steamvault_n"},
        {151, 555, 0, 67, 75, 0, 1, 18708, "shadow_labyrinth_n"},
        {172, 554, 0, 67, 75, 0, 1, 19220, "mechanar_n"},
        {173, 553, 0, 67, 75, 0, 1, 17977, "botanica_n"},
        {171, 269, 0, 68, 75, 0, 1, 17881, "black_morass_n"},
        {174, 552, 0, 68, 75, 0, 1, 20912, "arcatraz_n"},
        {198, 585, 0, 68, 75, 0, 1, 0, "magisters_terrace_n"},
        {178, 558, 1, 70, 75, 0, 1, 18373, "auchenai_crypts_h"},
        {179, 557, 1, 70, 75, 0, 1, 18344, "mana_tombs_h"},
        {180, 556, 1, 70, 75, 0, 1, 18473, "sethekk_halls_h"},
        {181, 555, 1, 70, 75, 0, 1, 18708, "shadow_labyrinth_h"},
        {182, 269, 1, 70, 75, 0, 1, 17881, "black_morass_h"},
        {183, 560, 1, 70, 75, 0, 1, 18096, "escape_from_durnholde_h"},
        {184, 547, 1, 70, 75, 0, 1, 17942, "slave_pens_h"},
        {185, 545, 1, 70, 75, 0, 1, 17798, "steamvault_h"},
        {186, 546, 1, 70, 75, 0, 1, 17882, "underbog_h"},
        {187, 542, 1, 70, 75, 0, 1, 0, "blood_furnace_h"},
        {188, 543, 1, 70, 75, 0, 1, 17537, "hellfire_ramparts_h"},
        {189, 540, 1, 70, 75, 0, 1, 16808, "shattered_halls_h"},
        {190, 552, 1, 70, 75, 0, 1, 20912, "arcatraz_h"},
        {191, 553, 1, 70, 75, 0, 1, 17977, "botanica_h"},
        {192, 554, 1, 70, 75, 0, 1, 19220, "mechanar_h"},
        {201, 585, 1, 70, 75, 0, 1, 0, "magisters_terrace_h"},
        {202, 574, 0, 68, 80, 0, 2, 23954, "utgarde_keep_n"},
        {225, 576, 0, 69, 80, 0, 2, 26723, "nexus_n"},
        {204, 601, 0, 70, 80, 0, 2, 0, "azjol_nerub_n"},
        {218, 619, 0, 71, 80, 0, 2, 29311, "ahnkahet_the_old_kingdom_n"},
        {214, 600, 0, 72, 80, 0, 2, 0, "draktharon_keep_n"},
        {220, 608, 0, 73, 80, 0, 2, 31134, "violet_hold_n"},
        {216, 604, 0, 74, 80, 0, 2, 0, "gundrak_n"},
        {208, 599, 0, 75, 80, 0, 2, 27978, "halls_of_stone_n"},
        {203, 575, 0, 77, 80, 0, 2, 26861, "utgarde_pinnacle_n"},
        {206, 578, 0, 77, 80, 0, 2, 27656, "oculus_n"},
        {207, 602, 0, 77, 80, 0, 2, 28923, "halls_of_lightning_n"},
        {209, 595, 0, 77, 80, 0, 2, 0, "culling_of_stratholme_n"},
        {245, 650, 0, 80, 80, 0, 2, 0, "trial_of_the_champion_n"},
        {251, 632, 0, 80, 80, 0, 2, 36502, "forge_of_souls_n"},
        {253, 658, 0, 80, 80, 0, 2, 36658, "pit_of_saron_n"},
        {255, 668, 0, 80, 80, 0, 2, 0, "halls_of_reflection_n"},
        {205, 575, 1, 80, 80, 0, 2, 26861, "utgarde_pinnacle_h"},
        {210, 595, 1, 80, 80, 0, 2, 0, "culling_of_stratholme_h"},
        {211, 578, 1, 80, 80, 0, 2, 27656, "oculus_h"},
        {212, 602, 1, 80, 80, 0, 2, 28923, "halls_of_lightning_h"},
        {213, 599, 1, 80, 80, 0, 2, 27978, "halls_of_stone_h"},
        {215, 600, 1, 80, 80, 0, 2, 0, "draktharon_keep_h"},
        {217, 604, 1, 80, 80, 0, 2, 0, "gundrak_h"},
        {219, 619, 1, 80, 80, 0, 2, 29311, "ahnkahet_the_old_kingdom_h"},
        {221, 608, 1, 80, 80, 0, 2, 31134, "violet_hold_h"},
        {226, 576, 1, 80, 80, 0, 2, 26723, "nexus_h"},
        {241, 601, 1, 80, 80, 0, 2, 0, "azjol_nerub_h"},
        {242, 574, 1, 80, 80, 0, 2, 23954, "utgarde_keep_h"},
        {249, 650, 1, 80, 80, 0, 2, 0, "trial_of_the_champion_h"},
        {252, 632, 1, 80, 80, 0, 2, 36502, "forge_of_souls_h"},
        {254, 658, 1, 80, 80, 0, 2, 36658, "pit_of_saron_h"},
        {256, 668, 1, 80, 80, 0, 2, 0, "halls_of_reflection_h"},
    };
    return kTable;
}

inline SupportedLfgDungeon const* LookupSupportedLfgDungeonById(std::uint32_t rawDungeonEntry)
{
    std::uint32_t const lfgId = rawDungeonEntry & 0x00FFFFFFu;
    if (lfgId == 0)
        return nullptr;
    for (auto const& d : SupportedDungeonTable())
    {
        if (d.lfgDungeonId == lfgId)
            return &d;
    }
    return nullptr;
}

// Resolve a supported 5-player dungeon by (mapId, difficulty) and optional preferredLfgDungeonId.
// Multi-wing maps (Scarlet Monastery 189, Maraudon 349, BRD 230, Dire Maul 429, Stratholme 329) REQUIRE
// preferredLfgDungeonId > 0 so companions never queue the wrong wing; single-wing maps resolve uniquely.
inline SupportedLfgDungeon const* ResolveSupportedLfgDungeon(std::uint32_t mapId,
                                                             std::uint8_t difficulty,
                                                             std::uint32_t preferredLfgDungeonId = 0)
{
    if (difficulty > 1)
        return nullptr;  // Raids (>= 2) are never queued by 5-player cooperative RDF
    if (preferredLfgDungeonId != 0)
    {
        SupportedLfgDungeon const* exact = LookupSupportedLfgDungeonById(preferredLfgDungeonId);
        if (exact && exact->mapId == mapId && exact->difficulty == difficulty)
            return exact;
        return nullptr;
    }
    SupportedLfgDungeon const* match = nullptr;
    for (auto const& d : SupportedDungeonTable())
    {
        if (d.mapId == mapId && d.difficulty == difficulty)
        {
            if (match != nullptr)
                return nullptr;  // Ambiguous multi-wing map without explicit lfgDungeonId -> fail closed!
            match = &d;
        }
    }
    return match;
}

// Returns the final boss creature entry for a supported 5-player dungeon (0 if spell/event-credit or unknown).
inline std::uint32_t LookupDungeonFinalBoss(std::uint32_t mapId, std::uint32_t lfgDungeonId = 0)
{
    if (lfgDungeonId != 0)
    {
        if (SupportedLfgDungeon const* exact = LookupSupportedLfgDungeonById(lfgDungeonId))
        {
            if (exact->mapId == mapId)
                return exact->finalBossEntry;
        }
        return 0;
    }
    std::uint32_t boss = 0;
    for (auto const& d : SupportedDungeonTable())
    {
        if (d.mapId != mapId)
            continue;
        if (d.finalBossEntry == 0)
            continue;
        if (boss != 0 && boss != d.finalBossEntry)
            return 0;  // Multi-wing map with distinct wing bosses requires explicit lfgDungeonId!
        boss = d.finalBossEntry;
    }
    return boss;
}

// Expands comma-separated AiPlayerbot.Companion.Coop.EnabledDungeonMaps tokens supporting numeric map IDs
// PLUS safe expansion keywords: "all" / "*", "classic", "tbc", "wotlk" (NEVER admits raids or unknown maps).
inline std::unordered_set<std::uint32_t> ParseSupportedDungeonMapFilter(std::string const& raw)
{
    std::unordered_set<std::uint32_t> out;
    std::unordered_set<std::uint32_t> validMaps;
    for (auto const& d : SupportedDungeonTable())
        validMaps.insert(d.mapId);

    size_t start = 0;
    while (start <= raw.size())
    {
        size_t comma = raw.find(',', start);
        std::string tok = raw.substr(start, comma == std::string::npos ? std::string::npos : comma - start);
        size_t f = tok.find_first_not_of(" \t\r\n\"");
        if (f != std::string::npos)
        {
            size_t l = tok.find_last_not_of(" \t\r\n\"");
            tok = tok.substr(f, l - f + 1);
            std::transform(tok.begin(), tok.end(), tok.begin(),
                           [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
            if (tok == "all" || tok == "*")
            {
                for (auto const& d : SupportedDungeonTable())
                    out.insert(d.mapId);
            }
            else if (tok == "classic")
            {
                for (auto const& d : SupportedDungeonTable())
                    if (d.expansion == 0)
                        out.insert(d.mapId);
            }
            else if (tok == "tbc")
            {
                for (auto const& d : SupportedDungeonTable())
                    if (d.expansion == 1)
                        out.insert(d.mapId);
            }
            else if (tok == "wotlk")
            {
                for (auto const& d : SupportedDungeonTable())
                    if (d.expansion == 2)
                        out.insert(d.mapId);
            }
            else
            {
                char* endp = nullptr;
                unsigned long v = std::strtoul(tok.c_str(), &endp, 10);
                if (endp && *endp == '\0' && v > 0 && v <= 0xFFFFFFFFul)
                {
                    std::uint32_t const mapId = static_cast<std::uint32_t>(v);
                    if (validMaps.count(mapId) > 0)
                        out.insert(mapId);
                }
            }
        }
        if (comma == std::string::npos)
            break;
        start = comma + 1;
    }
    return out;
}

inline std::uint8_t ParseCoopRoleMask(std::string const& role)
{
    if (role == "tank")
        return ROLE_MASK_TANK;
    if (role == "healer")
        return ROLE_MASK_HEALER;
    if (role == "damage")
        return ROLE_MASK_DAMAGE;
    return ROLE_MASK_NONE;
}

// Preserves core WoW 3.3.5a class role legality (e.g. Mage cannot queue Tank/Healer, Warrior cannot queue Healer).
inline bool IsClassLegalForRoleMask(std::uint8_t classId, std::uint8_t roleMask)
{
    std::uint8_t const combatRole = static_cast<std::uint8_t>(roleMask & (ROLE_MASK_TANK | ROLE_MASK_HEALER | ROLE_MASK_DAMAGE));
    if (combatRole == ROLE_MASK_NONE)
        return false;
    // Require exactly one combat role bit for deterministic party composition.
    if ((combatRole & (combatRole - 1)) != 0)
        return false;
    if (combatRole == ROLE_MASK_DAMAGE)
        return (classId >= CLASS_ID_WARRIOR && classId <= CLASS_ID_WARLOCK) || classId == CLASS_ID_DRUID;
    if (combatRole == ROLE_MASK_TANK)
        return classId == CLASS_ID_WARRIOR || classId == CLASS_ID_PALADIN || classId == CLASS_ID_DEATH_KNIGHT ||
               classId == CLASS_ID_DRUID;
    if (combatRole == ROLE_MASK_HEALER)
        return classId == CLASS_ID_PALADIN || classId == CLASS_ID_PRIEST || classId == CLASS_ID_SHAMAN ||
               classId == CLASS_ID_DRUID;
    return false;
}

inline std::string FormatNativeQueueId(std::uint64_t opportunityId, std::uint32_t lfgDungeonId)
{
    return "lfgq:" + std::to_string(opportunityId) + ":" + std::to_string(lfgDungeonId);
}

inline std::string FormatNativeProposalId(std::uint32_t proposalId)
{
    return "lfgp:" + std::to_string(proposalId);
}

inline std::string FormatNativeRunId(std::uint32_t groupId, std::uint32_t instanceId, std::uint32_t mapId)
{
    return "lfgr:" + std::to_string(groupId) + ":" + std::to_string(instanceId) + ":" + std::to_string(mapId);
}

struct JoinEligibilityInput
{
    std::uint32_t botGuid = 0;
    std::uint8_t botLevel = 0;
    std::uint8_t botClass = 0;
    bool isAlliance = true;
    std::uint8_t nativeLfgState = NATIVE_LFG_STATE_NONE;
    bool inGroup = false;
    bool groupedWithOwner = false;
    bool inCombat = false;
    bool isDead = false;
    bool inBattlegroundOrQueue = false;
    bool inInstance = false;
    bool hasLfgDeserter = false;
    bool hasLfgCooldown = false;
    bool ownerRecalled = false;
    bool shiftEnded = false;
    std::unordered_set<std::uint32_t> lockedLfgDungeonIds;
    // Optional live DBC level bounds from sLFGDungeonStore (0 = use SupportedLfgDungeon fallback)
    std::uint8_t dbcMinLevel = 0;
    std::uint8_t dbcMaxLevel = 0;
};

struct JoinDecision
{
    bool canJoin = false;
    std::uint64_t opportunityId = 0;
    std::uint32_t revision = 0;
    std::uint32_t lfgDungeonId = 0;
    std::uint32_t targetMapId = 0;
    std::uint8_t roleMask = ROLE_MASK_NONE;
    std::string nativeQueueId;
    std::string reason;
};

enum class ProposalAction : std::uint8_t
{
    Ignore = 0,          // Not a coop companion proposal (e.g. human/owner group or already accepted)
    ClearStoredOnly = 1, // Non-initiating state (SUCCESS / FAILED) or expired past hard timeout: clear stored only
    DeferInCombat = 2,   // Wait for safe boundary (combat/flight/vehicle/cast/fall, bounded < 40s native timeout)
    Accept = 3,          // Safe & authorized: send CMSG_LFG_PROPOSAL_RESULT true
    Decline = 4,         // Blocked / recalled / dead / defer-timeout: send CMSG_LFG_PROPOSAL_RESULT false & release
};

struct ProposalInput
{
    std::uint32_t botGuid = 0;
    std::uint32_t proposalId = 0;
    std::uint32_t rawDungeonEntry = 0;
    std::uint8_t proposalState = PROPOSAL_STATE_INITIATING;
    bool groupedWithOwner = false;
    bool hasHumanMember = false;
    bool inCombat = false;
    bool inUnsafeBoundary = false;  // InFlight (taxi), Vehicle, NonMeleeSpellCast, or Falling
    bool isDead = false;
    bool hasLfgDeserter = false;
    bool hasLfgCooldown = false;
    bool ownerRecalled = false;
    bool shiftEnded = false;
    bool admissionAllowed = true;
    std::string admissionReason;
};

struct ProposalDecision
{
    ProposalAction action = ProposalAction::Ignore;
    std::uint64_t opportunityId = 0;
    std::uint32_t proposalId = 0;
    std::uint32_t lfgDungeonId = 0;
    std::uint32_t targetMapId = 0;
    std::string nativeQueueId;
    std::string nativeProposalId;
    std::string reason;
};

struct LeaveQueueDecision
{
    bool shouldLeave = false;
    bool isStaleRequeue = false;
    std::string reason;
};

struct BotOnlyMatchResult
{
    bool allowed = false;
    std::uint64_t opportunityId = 0;
    std::uint32_t targetMapId = 0;
    std::uint32_t lfgDungeonId = 0;
    std::string reason;
};

enum class LfgLifecycleEventKind : std::uint8_t
{
    None = 0,
    Queued = 1,
    QueueCancelled = 2,
    ProposalAccepted = 3,
    ProposalDeclined = 4,
    ProposalFailed = 5,
    RunEntered = 6,
};

struct LfgLifecycleEvent
{
    LfgLifecycleEventKind kind = LfgLifecycleEventKind::None;
    std::uint64_t opportunityId = 0;
    std::uint32_t botGuid = 0;
    std::uint32_t lfgDungeonId = 0;
    std::uint32_t proposalId = 0;
    std::uint32_t groupId = 0;
    std::uint32_t instanceId = 0;
    std::string nativeQueueId;
    std::string nativeRunId;
    std::string reason;
    bool cancelOpportunity = false;
};

struct BotQueueTracking
{
    std::uint64_t opportunityId = 0;
    std::uint32_t lfgDungeonId = 0;
    std::uint32_t joinSentAt = 0;
    std::uint32_t confirmedQueuedAt = 0;
    std::uint32_t leaveSentAt = 0;
    std::uint32_t activeProposalId = 0;
    std::uint32_t proposalFirstSeenAt = 0;
    std::uint32_t proposalRawDungeon = 0;
    std::uint32_t lastExpiredProposalId = 0;
    bool proposalAccepted = false;
    std::uint32_t declineBackoffUntil = 0;
};

// Pure helper: verifies exact confirmed role composition (tank/healer/damage counts match roles_json,
// every member is enrolled, confirmed accepted/active, matching run revision, and unique GUID).
inline bool ValidateConfirmedRoleComposition(std::vector<CompanionCoopAuth::AuthorizedMember> const& roster,
                                             CompanionCoopAuth::RoleRequirements const& req,
                                             std::uint32_t expectedRevision)
{
    if (!req.IsValid() || roster.size() != req.Total())
        return false;
    std::uint32_t tanks = 0;
    std::uint32_t healers = 0;
    std::uint32_t dps = 0;
    std::unordered_set<std::uint32_t> seen;
    for (auto const& m : roster)
    {
        if (m.characterGuid == 0 || !seen.insert(m.characterGuid).second)
            return false;
        if (!m.enrolledCoop || !CompanionCoopAuth::IsConfirmedInterestState(m.interestState) ||
            m.interestRevision != expectedRevision)
            return false;
        if (m.role == "tank")
            ++tanks;
        else if (m.role == "healer")
            ++healers;
        else if (m.role == "damage")
            ++dps;
        else
            return false;
    }
    return tanks == req.tank && healers == req.healer && dps == req.damage;
}

// Pure helper: verifies an assembling/active run has an exact, fully-confirmed, enrolled 5-member roster
// for a supported WotLK dungeon and matches a candidate bot-only 5-GUID LFG queue proposal.
inline BotOnlyMatchResult CanMatchBotOnlyProposal(bool coopEnabled,
                                                  std::vector<CompanionCoopAuth::AuthorizedRun> const& runs,
                                                  std::vector<std::uint32_t> const& candidateGuids,
                                                  std::uint32_t nowSec)
{
    BotOnlyMatchResult res;
    if (!coopEnabled)
    {
        res.reason = "coop_disabled";
        return res;
    }
    if (candidateGuids.size() != CompanionCoopAuth::MAX_COOP_PARTY_SIZE)
    {
        res.reason = "not_5_bot_guids";
        return res;
    }
    std::unordered_set<std::uint32_t> candSet;
    for (std::uint32_t g : candidateGuids)
    {
        if (g == 0 || !candSet.insert(g).second)
        {
            res.reason = "invalid_or_duplicate_candidate_guid";
            return res;
        }
    }

    for (auto const& run : runs)
    {
        if (run.isShadow || !CompanionCoopAuth::IsActiveRunState(run.runState) || run.expiresAt <= nowSec)
            continue;
        if (run.boundToNativeGroup && run.expectedGroupId != 0)
            continue;  // Already bound to a live native group (do not match a second LFG group!)
        if (run.assemblyDeadline > 0 && nowSec >= run.assemblyDeadline)
            continue;
        SupportedLfgDungeon const* spec = ResolveSupportedLfgDungeon(run.targetMapId, run.difficulty, run.lfgDungeonId);
        if (!spec)
            continue;
        if (run.roster.size() != CompanionCoopAuth::MAX_COOP_PARTY_SIZE)
            continue;
        if (!ValidateConfirmedRoleComposition(run.roster, run.requiredRoles, run.revision))
            continue;

        bool exactMatch = true;
        for (auto const& m : run.roster)
        {
            if (!m.enrolledCoop || !CompanionCoopAuth::IsConfirmedInterestState(m.interestState) ||
                m.interestRevision != run.revision || candSet.count(m.characterGuid) == 0)
            {
                exactMatch = false;
                break;
            }
        }
        if (exactMatch)
        {
            res.allowed = true;
            res.opportunityId = run.opportunityId;
            res.targetMapId = run.targetMapId;
            res.lfgDungeonId = spec->lfgDungeonId;
            res.reason = "authorized_coop_5bot_match";
            return res;
        }
    }
    res.reason = "no_matching_authorized_coop_roster";
    return res;
}

class LfgBridgeController
{
public:
    // Evaluates whether an individual companion should send CMSG_LFG_JOIN right now for its voluntary dungeon goal.
    // Prevents double queueing, enforces supported-dungeon allowlist, role/class legality, level/faction, and locks!
    JoinDecision EvaluateJoinEligibility(bool coopEnabled, std::vector<CompanionCoopAuth::AuthorizedRun> const& runs,
                                         JoinEligibilityInput const& in, std::uint32_t nowSec,
                                         std::uint32_t joinInflightSec = DEFAULT_JOIN_INFLIGHT_SEC) const
    {
        JoinDecision dec;
        if (!coopEnabled)
        {
            dec.reason = "coop_disabled";
            return dec;
        }
        if (in.botGuid == 0)
        {
            dec.reason = "invalid_bot";
            return dec;
        }
        if (in.groupedWithOwner || in.inGroup)
        {
            dec.reason = in.groupedWithOwner ? "grouped_with_owner" : "already_in_group";
            return dec;
        }
        if (in.nativeLfgState != NATIVE_LFG_STATE_NONE)
        {
            dec.reason = "already_in_native_lfg";
            return dec;
        }
        if (in.ownerRecalled)
        {
            dec.reason = "owner_recalled";
            return dec;
        }
        if (in.shiftEnded)
        {
            dec.reason = "shift_ended";
            return dec;
        }
        if (in.inCombat || in.isDead || in.inBattlegroundOrQueue || in.inInstance)
        {
            dec.reason = "not_safe_to_queue";
            return dec;
        }
        if (in.hasLfgDeserter)
        {
            dec.reason = "lfg_deserter_active";
            return dec;
        }
        if (in.hasLfgCooldown)
        {
            dec.reason = "lfg_cooldown_active";
            return dec;
        }

        // Anti-double-queue guard (async CMSG_LFG_JOIN/CMSG_LFG_LEAVE in flight or recent decline backoff)
        if (auto it = _byBot.find(in.botGuid); it != _byBot.end())
        {
            if (it->second.joinSentAt > 0 && nowSec < it->second.joinSentAt + joinInflightSec)
            {
                dec.reason = "join_packet_in_flight";
                return dec;
            }
            if (it->second.leaveSentAt > 0 && nowSec < it->second.leaveSentAt + DEFAULT_LEAVE_INFLIGHT_SEC)
            {
                dec.reason = "leave_packet_in_flight";
                return dec;
            }
            if (it->second.declineBackoffUntil > nowSec)
            {
                dec.reason = "recent_decline_backoff";
                return dec;
            }
        }

        // Find this bot's authorized assembling opportunity with a complete confirmed 5-member roster.
        for (auto const& run : runs)
        {
            if (run.isShadow || run.runState != "assembling" || run.boundToNativeGroup || run.expiresAt <= nowSec)
                continue;
            if (run.assemblyDeadline > 0 && nowSec >= run.assemblyDeadline)
                continue;
            CompanionCoopAuth::AuthorizedMember const* self = nullptr;
            bool allConfirmed = (run.roster.size() == CompanionCoopAuth::MAX_COOP_PARTY_SIZE);
            for (auto const& m : run.roster)
            {
                if (m.characterGuid == in.botGuid)
                    self = &m;
                if (!m.enrolledCoop || !CompanionCoopAuth::IsConfirmedInterestState(m.interestState) ||
                    m.interestRevision != run.revision)
                    allConfirmed = false;
            }
            if (!self)
                continue;
            if (!allConfirmed || !ValidateConfirmedRoleComposition(run.roster, run.requiredRoles, run.revision))
            {
                dec.reason = "roster_not_complete_or_unconfirmed";
                return dec;
            }

            SupportedLfgDungeon const* spec = ResolveSupportedLfgDungeon(run.targetMapId, run.difficulty, run.lfgDungeonId);
            if (!spec)
            {
                dec.reason = "unsupported_dungeon";
                return dec;
            }
            if ((spec->faction == 1 && !in.isAlliance) || (spec->faction == 2 && in.isAlliance))
            {
                dec.reason = "faction_ineligible";
                return dec;
            }
            std::uint8_t const minLvl = (in.dbcMinLevel > 0) ? in.dbcMinLevel : spec->minLevel;
            std::uint8_t const maxLvl = (in.dbcMaxLevel > 0) ? in.dbcMaxLevel : spec->maxLevel;
            if (in.botLevel < minLvl || in.botLevel > maxLvl)
            {
                dec.reason = "level_out_of_lfg_band";
                return dec;
            }
            if (in.lockedLfgDungeonIds.count(spec->lfgDungeonId) > 0)
            {
                dec.reason = "dungeon_locked_in_lfgmgr";
                return dec;
            }

            std::uint8_t const roleMask = ParseCoopRoleMask(self->role);
            if (!IsClassLegalForRoleMask(in.botClass, roleMask))
            {
                dec.reason = "class_ineligible_for_role";
                return dec;
            }

            dec.canJoin = true;
            dec.opportunityId = run.opportunityId;
            dec.revision = run.revision;
            dec.lfgDungeonId = spec->lfgDungeonId;
            dec.targetMapId = spec->mapId;
            dec.roleMask = roleMask;
            dec.nativeQueueId = FormatNativeQueueId(run.opportunityId, spec->lfgDungeonId);
            dec.reason = "eligible_native_rdf_join";
            return dec;
        }

        dec.reason = "no_assembling_opportunity";
        return dec;
    }

    // Records in-flight CMSG_LFG_JOIN ONLY; does NOT write native_queue_id to MySQL until core confirms LFG_STATE_QUEUED!
    void MarkJoinPacketSent(std::uint32_t botGuid, std::uint64_t opportunityId,
                            std::uint32_t lfgDungeonId, std::uint32_t nowSec)
    {
        auto& st = _byBot[botGuid];
        st.opportunityId = opportunityId;
        st.lfgDungeonId = lfgDungeonId;
        st.joinSentAt = nowSec;
        st.confirmedQueuedAt = 0;
        st.leaveSentAt = 0;
        st.activeProposalId = 0;
        st.proposalFirstSeenAt = 0;
        st.proposalRawDungeon = 0;
        st.proposalAccepted = false;
    }

    // Called once core LFGMgr confirms sLFGMgr->GetState(bot) == LFG_STATE_QUEUED. Emits Queued ONLY once per opp
    // when all 5 confirmed members of that opportunity have been confirmed queued (or returns empty event until then).
    LfgLifecycleEvent ObserveNativeQueued(std::uint32_t botGuid, std::uint32_t nowSec,
                                          std::vector<CompanionCoopAuth::AuthorizedRun> const& runs)
    {
        LfgLifecycleEvent ev;
        auto it = _byBot.find(botGuid);
        if (it == _byBot.end() || it->second.opportunityId == 0)
            return ev;
        if (it->second.confirmedQueuedAt != 0)
            return ev;  // Already observed confirmed queued
        it->second.confirmedQueuedAt = nowSec ? nowSec : 1;

        std::uint64_t const oppId = it->second.opportunityId;
        if (_recordedQueuedOpps.count(oppId) > 0)
            return ev;

        // Verify ALL 5 confirmed members of this opportunity are confirmed in LFG_STATE_QUEUED before writing opp queue ID!
        for (auto const& run : runs)
        {
            if (run.opportunityId != oppId || run.isShadow || run.roster.size() != CompanionCoopAuth::MAX_COOP_PARTY_SIZE)
                continue;
            bool allConfirmedQueued = true;
            for (auto const& m : run.roster)
            {
                auto mit = _byBot.find(m.characterGuid);
                if (mit == _byBot.end() || mit->second.opportunityId != oppId || mit->second.confirmedQueuedAt == 0)
                {
                    allConfirmedQueued = false;
                    break;
                }
            }
            if (allConfirmedQueued)
            {
                _recordedQueuedOpps.insert(oppId);
                ev.kind = LfgLifecycleEventKind::Queued;
                ev.opportunityId = oppId;
                ev.botGuid = botGuid;
                ev.lfgDungeonId = it->second.lfgDungeonId;
                ev.nativeQueueId = FormatNativeQueueId(oppId, it->second.lfgDungeonId);
                ev.reason = "all_5_confirmed_native_lfg_queued";
            }
            break;
        }
        return ev;
    }

    // Evaluates whether a currently queued companion should leave the native LFG queue cleanly!
    // Checks: full roster completeness (P1-1), assemblyDeadline (P1-1), stale-queue requeue (P1-7),
    // and duplicate-leave suppression (P2-2).
    LeaveQueueDecision EvaluateLeaveQueue(bool coopEnabled, std::vector<CompanionCoopAuth::AuthorizedRun> const& runs,
                                          std::uint32_t botGuid, std::uint8_t nativeLfgState, bool groupedWithOwner,
                                          bool ownerRecalled, bool shiftEnded, bool inBgOrInstance,
                                          std::uint32_t nowSec,
                                          std::uint32_t staleRequeueSec = DEFAULT_STALE_QUEUE_REQUEUE_SEC) const
    {
        LeaveQueueDecision dec;
        if (nativeLfgState != NATIVE_LFG_STATE_QUEUED)
            return dec;

        // Only manage bots that are actually tracked by the cooperative LFG bridge (P0-2: NEVER touch human/owner queues!)
        auto trackIt = _byBot.find(botGuid);
        if (trackIt == _byBot.end() || trackIt->second.opportunityId == 0)
            return dec;
        // Suppress duplicate CMSG_LFG_LEAVE packets while a previous leave is still in flight (P2-2)
        if (trackIt->second.leaveSentAt > 0 && nowSec < trackIt->second.leaveSentAt + DEFAULT_LEAVE_INFLIGHT_SEC)
            return dec;

        if (groupedWithOwner)
        {
            dec.shouldLeave = true;
            dec.reason = "grouped_with_owner";
            return dec;
        }
        if (!coopEnabled)
        {
            dec.shouldLeave = true;
            dec.reason = "coop_disabled_or_draining";
            return dec;
        }
        if (ownerRecalled)
        {
            dec.shouldLeave = true;
            dec.reason = "owner_recalled";
            return dec;
        }
        if (shiftEnded)
        {
            dec.shouldLeave = true;
            dec.reason = "shift_ended";
            return dec;
        }
        if (inBgOrInstance)
        {
            dec.shouldLeave = true;
            dec.reason = "entered_bg_or_instance";
            return dec;
        }

        // Verify bot's opportunity is STILL active, within assemblyDeadline, AND has a complete 5-member roster (P1-1)!
        bool foundHealthyOpp = false;
        std::string failReason = "opportunity_no_longer_active";
        for (auto const& run : runs)
        {
            if (run.opportunityId != trackIt->second.opportunityId)
                continue;
            if (run.isShadow || !CompanionCoopAuth::IsActiveRunState(run.runState) || run.expiresAt <= nowSec)
            {
                failReason = "opportunity_closed_or_expired";
                break;
            }
            if (!run.boundToNativeGroup && run.assemblyDeadline > 0 && nowSec >= run.assemblyDeadline)
            {
                failReason = "assembly_deadline_expired";
                break;
            }
            if (!ValidateConfirmedRoleComposition(run.roster, run.requiredRoles, run.revision))
            {
                failReason = "roster_incomplete_after_member_release";
                break;
            }
            for (auto const& m : run.roster)
            {
                if (m.characterGuid == botGuid && m.enrolledCoop &&
                    CompanionCoopAuth::IsConfirmedInterestState(m.interestState) && m.interestRevision == run.revision)
                {
                    foundHealthyOpp = true;
                    break;
                }
            }
            break;
        }
        if (!foundHealthyOpp)
        {
            dec.shouldLeave = true;
            dec.reason = failReason;
            return dec;
        }

        // P1-7: If queued >= staleRequeueSec without receiving a proposal (e.g. transient headroom rejection cached
        // in LFGQueue), leave & re-join cleanly to trigger a fresh LFGQueue::FindNewGroups pass!
        std::uint32_t const queuedSince =
            trackIt->second.confirmedQueuedAt ? trackIt->second.confirmedQueuedAt : trackIt->second.joinSentAt;
        if (staleRequeueSec > 0 && queuedSince > 0 && trackIt->second.activeProposalId == 0 &&
            nowSec >= queuedSince + staleRequeueSec)
        {
            dec.shouldLeave = true;
            dec.isStaleRequeue = true;
            dec.reason = "stale_queue_requeue";
            return dec;
        }
        return dec;
    }

    // Legacy bool wrapper for convenience
    bool ShouldLeaveQueue(bool coopEnabled, std::vector<CompanionCoopAuth::AuthorizedRun> const& runs,
                          std::uint32_t botGuid, std::uint8_t nativeLfgState, bool groupedWithOwner,
                          bool ownerRecalled, bool shiftEnded, bool inBgOrInstance, std::uint32_t nowSec,
                          std::string* outReason = nullptr) const
    {
        LeaveQueueDecision const d = EvaluateLeaveQueue(coopEnabled, runs, botGuid, nativeLfgState, groupedWithOwner,
                                                        ownerRecalled, shiftEnded, inBgOrInstance, nowSec);
        if (outReason)
            *outReason = d.reason;
        return d.shouldLeave;
    }

    LfgLifecycleEvent OnQueueCancelled(std::uint32_t botGuid, std::string const& reason, std::uint32_t nowSec = 0,
                                       bool isStaleRequeue = false, bool forceRelease = false)
    {
        LfgLifecycleEvent ev;
        auto it = _byBot.find(botGuid);
        if (it == _byBot.end() || it->second.opportunityId == 0)
            return ev;
        // Duplicate-leave guard (P2-2); bypassed when forceRelease=true (e.g. logout / owner group join!)
        if (!forceRelease && nowSec > 0 && it->second.leaveSentAt > 0 &&
            nowSec < it->second.leaveSentAt + DEFAULT_LEAVE_INFLIGHT_SEC)
            return ev;

        std::uint64_t const oppId = it->second.opportunityId;
        std::uint32_t const lfgId = it->second.lfgDungeonId;
        it->second.joinSentAt = 0;
        it->second.confirmedQueuedAt = 0;
        it->second.leaveSentAt = nowSec ? nowSec : 1;
        it->second.activeProposalId = 0;
        it->second.proposalFirstSeenAt = 0;
        it->second.proposalRawDungeon = 0;
        it->second.proposalAccepted = false;

        if (isStaleRequeue)
        {
            // Keep opportunity intact & clear recorded-queued flag so fresh re-join confirms cleanly (P1-7)
            _recordedQueuedOpps.erase(oppId);
            return ev;
        }

        // Real cancellation (recall / shift / coop off / owner group / offline): release bot & cancel opp (P1-1)!
        it->second.opportunityId = 0;
        it->second.lfgDungeonId = 0;
        _recordedQueuedOpps.erase(oppId);

        ev.kind = LfgLifecycleEventKind::QueueCancelled;
        ev.opportunityId = oppId;
        ev.botGuid = botGuid;
        ev.lfgDungeonId = lfgId;
        ev.nativeQueueId = FormatNativeQueueId(oppId, lfgId);
        ev.reason = reason.empty() ? "queue_cancelled" : reason;
        ev.cancelOpportunity = (reason != "opportunity_closed_or_expired" && reason != "opportunity_no_longer_active" &&
                                reason != "roster_incomplete_after_member_release");
        return ev;
    }

    // Evaluates a native LFG proposal (SMSG_LFG_PROPOSAL_UPDATE or world-thread retry of a deferred proposal).
    // Enforces safe-boundary deferral (combat/flight/vehicle/cast/fall up to combatDeferSec) AND an unconditional
    // hard timeout (hardTimeoutSec < 40s native timeout) so expired proposals are NEVER accepted (P1-2, P2-3)!
    ProposalDecision EvaluateProposal(bool coopEnabled, std::vector<CompanionCoopAuth::AuthorizedRun> const& runs,
                                      ProposalInput const& in, std::uint32_t nowSec,
                                      std::uint32_t combatDeferSec = DEFAULT_PROPOSAL_COMBAT_DEFER_SEC,
                                      std::uint32_t hardTimeoutSec = DEFAULT_PROPOSAL_HARD_TIMEOUT_SEC)
    {
        ProposalDecision dec;
        dec.proposalId = in.proposalId;
        if (in.botGuid == 0 || in.proposalId == 0)
        {
            dec.action = ProposalAction::Ignore;
            dec.reason = "invalid_proposal_input";
            return dec;
        }
        // Real human / owner-grouped companions use stock LFG flow untouched!
        if (in.groupedWithOwner || in.hasHumanMember)
        {
            dec.action = ProposalAction::Ignore;
            dec.reason = "human_or_owner_group";
            return dec;
        }
        // Non-initiating proposal packets (FAILED=1, SUCCESS=2) MUST ONLY clear stored state, never reply!
        if (in.proposalState != PROPOSAL_STATE_INITIATING)
        {
            dec.action = ProposalAction::ClearStoredOnly;
            dec.reason = (in.proposalState == PROPOSAL_STATE_SUCCESS) ? "proposal_succeeded" : "proposal_failed";
            if (auto it = _byBot.find(in.botGuid); it != _byBot.end())
            {
                dec.opportunityId = it->second.opportunityId;
                dec.lfgDungeonId = it->second.lfgDungeonId;
            }
            return dec;
        }

        // Resolve stored raw dungeon entry if this is a deferred re-evaluation where packet isn't attached.
        std::uint32_t rawEntry = in.rawDungeonEntry;
        auto& track = _byBot[in.botGuid];
        if (rawEntry == 0 && track.activeProposalId == in.proposalId)
            rawEntry = track.proposalRawDungeon;

        // If we already sent CMSG_LFG_PROPOSAL_RESULT(true) for this exact proposalId, do not re-send!
        if (track.activeProposalId == in.proposalId && track.proposalAccepted)
        {
            dec.action = ProposalAction::Ignore;
            dec.opportunityId = track.opportunityId;
            dec.reason = "proposal_already_accepted";
            return dec;
        }

        // P2-B: Tombstone guard — once a proposalId has hit hard timeout, NEVER re-evaluate or accept it if
        // another INITIATING packet arrives in the 35..40s window!
        if (track.lastExpiredProposalId == in.proposalId)
        {
            dec.action = ProposalAction::ClearStoredOnly;
            dec.opportunityId = track.opportunityId;
            dec.lfgDungeonId = track.lfgDungeonId;
            dec.nativeProposalId = FormatNativeProposalId(in.proposalId);
            dec.reason = "proposal_hard_timeout_expired";
            return dec;
        }

        // P1-2: Unconditional hard cutoff (even if combat just ended at t=45s!) — NEVER accept an expired proposal!
        if (track.activeProposalId == in.proposalId && track.proposalFirstSeenAt > 0 &&
            nowSec >= track.proposalFirstSeenAt + std::max<std::uint32_t>(1, hardTimeoutSec))
        {
            track.lastExpiredProposalId = in.proposalId;
            dec.action = ProposalAction::ClearStoredOnly;
            dec.opportunityId = track.opportunityId;
            dec.lfgDungeonId = track.lfgDungeonId;
            dec.nativeProposalId = FormatNativeProposalId(in.proposalId);
            dec.reason = "proposal_hard_timeout_expired";
            return dec;
        }

        auto rejectWith = [&](std::string const& why, std::uint64_t oppId = 0) -> ProposalDecision
        {
            dec.action = ProposalAction::Decline;
            dec.opportunityId = oppId ? oppId : track.opportunityId;
            dec.nativeProposalId = FormatNativeProposalId(in.proposalId);
            dec.reason = why;
            return dec;
        };

        if (!coopEnabled)
            return rejectWith("coop_disabled_or_draining");
        if (in.ownerRecalled)
            return rejectWith("owner_recalled");
        if (in.shiftEnded)
            return rejectWith("shift_ended");
        if (in.isDead)
            return rejectWith("bot_dead");
        if (in.hasLfgDeserter)
            return rejectWith("lfg_deserter_active");
        if (in.hasLfgCooldown)
            return rejectWith("lfg_cooldown_active");

        // Validate proposal dungeon is a supported specific WotLK dungeon (never random/heroic/unsupported!)
        SupportedLfgDungeon const* spec = LookupSupportedLfgDungeonById(rawEntry);
        if (!spec)
            return rejectWith("unsupported_or_unknown_proposal_dungeon");

        // Match against bot's authorized assembling/active opportunity & verify map + full roster!
        std::uint64_t matchedOppId = 0;
        for (auto const& run : runs)
        {
            if (run.isShadow || !CompanionCoopAuth::IsActiveRunState(run.runState) || run.expiresAt <= nowSec)
                continue;
            if (!run.boundToNativeGroup && run.assemblyDeadline > 0 && nowSec >= run.assemblyDeadline)
                continue;
            if (run.targetMapId != spec->mapId || run.difficulty != spec->difficulty)
                continue;
            if (run.lfgDungeonId != 0 && run.lfgDungeonId != spec->lfgDungeonId)
                continue;
            if (!ValidateConfirmedRoleComposition(run.roster, run.requiredRoles, run.revision))
                continue;
            for (auto const& m : run.roster)
            {
                if (m.characterGuid == in.botGuid && m.enrolledCoop &&
                    CompanionCoopAuth::IsConfirmedInterestState(m.interestState) && m.interestRevision == run.revision)
                {
                    matchedOppId = run.opportunityId;
                    break;
                }
            }
            if (matchedOppId != 0)
                break;
        }
        if (matchedOppId == 0)
            return rejectWith("no_authorized_opportunity_for_proposal");

        if (!in.admissionAllowed)
            return rejectWith(in.admissionReason.empty() ? "admission_gate_blocked" : in.admissionReason, matchedOppId);

        // Record first-seen timestamp & raw dungeon for this proposalId (supports safe-boundary deferral).
        if (track.activeProposalId != in.proposalId)
        {
            track.opportunityId = matchedOppId;
            track.lfgDungeonId = spec->lfgDungeonId;
            track.activeProposalId = in.proposalId;
            track.proposalFirstSeenAt = nowSec;
            track.proposalRawDungeon = rawEntry;
            track.proposalAccepted = false;
        }

        // Safe-boundary gate: if in combat OR unsafe boundary (taxi/vehicle/casting/falling), defer up to
        // combatDeferSec (25s < 40s native timeout), then decline cleanly (P2-3)!
        if (in.inCombat || in.inUnsafeBoundary)
        {
            if (nowSec < track.proposalFirstSeenAt + std::max<std::uint32_t>(1, combatDeferSec))
            {
                dec.action = ProposalAction::DeferInCombat;
                dec.opportunityId = matchedOppId;
                dec.lfgDungeonId = spec->lfgDungeonId;
                dec.targetMapId = spec->mapId;
                dec.nativeQueueId = FormatNativeQueueId(matchedOppId, spec->lfgDungeonId);
                dec.nativeProposalId = FormatNativeProposalId(in.proposalId);
                dec.reason = in.inCombat ? "defer_in_combat" : "defer_unsafe_boundary";
                return dec;
            }
            return rejectWith(in.inCombat ? "combat_defer_timeout" : "unsafe_boundary_defer_timeout", matchedOppId);
        }

        dec.action = ProposalAction::Accept;
        dec.opportunityId = matchedOppId;
        dec.lfgDungeonId = spec->lfgDungeonId;
        dec.targetMapId = spec->mapId;
        dec.nativeQueueId = FormatNativeQueueId(matchedOppId, spec->lfgDungeonId);
        dec.nativeProposalId = FormatNativeProposalId(in.proposalId);
        dec.reason = "accepted_at_safe_boundary";
        return dec;
    }

    LfgLifecycleEvent MarkProposalAccepted(std::uint32_t botGuid, std::uint64_t opportunityId,
                                           std::uint32_t proposalId, std::uint32_t lfgDungeonId)
    {
        auto& st = _byBot[botGuid];
        st.opportunityId = opportunityId;
        st.lfgDungeonId = lfgDungeonId;
        st.activeProposalId = proposalId;
        st.proposalAccepted = true;

        LfgLifecycleEvent ev;
        ev.kind = LfgLifecycleEventKind::ProposalAccepted;
        ev.opportunityId = opportunityId;
        ev.botGuid = botGuid;
        ev.lfgDungeonId = lfgDungeonId;
        ev.proposalId = proposalId;
        ev.nativeQueueId = FormatNativeQueueId(opportunityId, lfgDungeonId);
        ev.reason = "proposal_accepted";
        return ev;
    }

    LfgLifecycleEvent OnProposalDeclinedOrFailed(std::uint32_t botGuid, std::uint32_t proposalId, bool wasDecline,
                                                 std::string const& reason, std::uint32_t nowSec,
                                                 std::uint32_t backoffSec = DEFAULT_DECLINE_BACKOFF_SEC)
    {
        LfgLifecycleEvent ev;
        auto it = _byBot.find(botGuid);
        if (it == _byBot.end() || it->second.opportunityId == 0)
            return ev;
        if (proposalId != 0 && it->second.activeProposalId != 0 && it->second.activeProposalId != proposalId)
            return ev;

        std::uint64_t const oppId = it->second.opportunityId;
        ev.kind = wasDecline ? LfgLifecycleEventKind::ProposalDeclined : LfgLifecycleEventKind::ProposalFailed;
        ev.opportunityId = oppId;
        ev.botGuid = botGuid;
        ev.lfgDungeonId = it->second.lfgDungeonId;
        ev.proposalId = proposalId ? proposalId : it->second.activeProposalId;
        ev.nativeQueueId = FormatNativeQueueId(oppId, it->second.lfgDungeonId);
        ev.reason = reason.empty() ? (wasDecline ? "proposal_declined" : "proposal_failed") : reason;
        ev.cancelOpportunity = true;

        // Clear proposal state cleanly; on non-decline proposal failure (wasDecline=false), core RemoveProposal
        // re-adds the remaining willing bots to LFG_STATE_QUEUED, so KEEP opportunityId intact! On the next world
        // tick, EvaluateLeaveQueue sees LFG_STATE_QUEUED + cancelled/incomplete run and sends CMSG_LFG_LEAVE so
        // ALL 4 remaining bots exit the native queue cleanly (P1 fix!).
        _recordedQueuedOpps.erase(oppId);
        it->second.activeProposalId = 0;
        it->second.proposalFirstSeenAt = 0;
        it->second.proposalRawDungeon = 0;
        it->second.proposalAccepted = false;
        if (wasDecline)
        {
            // Declining bot is removed from queue by core RemoveProposal; clear its opportunity tracking & backoff.
            it->second.opportunityId = 0;
            it->second.lfgDungeonId = 0;
            it->second.joinSentAt = 0;
            it->second.confirmedQueuedAt = 0;
            it->second.leaveSentAt = 0;
            it->second.declineBackoffUntil = nowSec + backoffSec;
        }
        return ev;
    }

    void OnProposalSucceeded(std::uint32_t botGuid, std::uint32_t proposalId)
    {
        auto it = _byBot.find(botGuid);
        if (it == _byBot.end())
            return;
        if (proposalId == 0 || it->second.activeProposalId == proposalId)
        {
            _recordedQueuedOpps.erase(it->second.opportunityId);
            it->second.opportunityId = 0;
            it->second.lfgDungeonId = 0;
            it->second.joinSentAt = 0;
            it->second.confirmedQueuedAt = 0;
            it->second.leaveSentAt = 0;
            it->second.activeProposalId = 0;
            it->second.proposalFirstSeenAt = 0;
            it->second.proposalRawDungeon = 0;
            it->second.proposalAccepted = false;
        }
    }

    bool HasTrackedProposal(std::uint32_t botGuid, std::uint32_t& outProposalId, std::uint32_t& outRawDungeon) const
    {
        auto it = _byBot.find(botGuid);
        if (it == _byBot.end() || it->second.activeProposalId == 0)
            return false;
        outProposalId = it->second.activeProposalId;
        outRawDungeon = it->second.proposalRawDungeon;
        return true;
    }

    bool HasTrackedBot(std::uint32_t botGuid) const
    {
        auto it = _byBot.find(botGuid);
        return it != _byBot.end() && (it->second.opportunityId != 0 || it->second.activeProposalId != 0);
    }

    bool HasAnyTrackedState() const
    {
        for (auto const& [guid, st] : _byBot)
        {
            if (st.opportunityId != 0 || st.activeProposalId != 0)
                return true;
        }
        return false;
    }

    std::vector<std::uint32_t> TrackedBotGuids() const
    {
        std::vector<std::uint32_t> out;
        out.reserve(_byBot.size());
        for (auto const& [guid, st] : _byBot)
        {
            if (st.opportunityId != 0 || st.activeProposalId != 0)
                out.push_back(guid);
        }
        return out;
    }

    // P2-A: Prunes stale tracking when core LFGMgr reports LFG_STATE_NONE (e.g. after a 40s native timeout,
    // hard-timeout expiry, or missed SUCCESS update) and no join packet is in flight!
    bool PruneIfIdleInNone(std::uint32_t botGuid, std::vector<CompanionCoopAuth::AuthorizedRun> const& runs,
                           std::uint32_t nowSec, std::uint32_t joinInflightSec = DEFAULT_JOIN_INFLIGHT_SEC)
    {
        auto it = _byBot.find(botGuid);
        if (it == _byBot.end())
            return false;
        if (it->second.joinSentAt > 0 && nowSec < it->second.joinSentAt + joinInflightSec)
            return false;  // CMSG_LFG_JOIN still in flight; do not prune prematurely
        std::uint64_t const oppId = it->second.opportunityId;
        bool stillAssembling = false;
        if (oppId != 0)
        {
            for (auto const& run : runs)
            {
                if (run.opportunityId == oppId && !run.isShadow && run.runState == "assembling" &&
                    !run.boundToNativeGroup && run.expiresAt > nowSec &&
                    (run.assemblyDeadline == 0 || nowSec < run.assemblyDeadline))
                {
                    stillAssembling = true;
                    break;
                }
            }
        }
        if (!stillAssembling || it->second.activeProposalId == 0)
        {
            if (oppId != 0)
                _recordedQueuedOpps.erase(oppId);
            it->second.opportunityId = 0;
            it->second.lfgDungeonId = 0;
            it->second.joinSentAt = 0;
            it->second.confirmedQueuedAt = 0;
            it->second.leaveSentAt = 0;
            it->second.activeProposalId = 0;
            it->second.proposalFirstSeenAt = 0;
            it->second.proposalRawDungeon = 0;
            it->second.proposalAccepted = false;
            if (it->second.declineBackoffUntil <= nowSec && it->second.lastExpiredProposalId == 0)
                _byBot.erase(it);
            return true;
        }
        return false;
    }

    void ClearBot(std::uint32_t botGuid)
    {
        auto it = _byBot.find(botGuid);
        if (it != _byBot.end())
        {
            if (it->second.opportunityId != 0)
                _recordedQueuedOpps.erase(it->second.opportunityId);
            _byBot.erase(it);
        }
    }

    void ClearOpportunity(std::uint64_t opportunityId)
    {
        if (opportunityId == 0)
            return;
        _recordedQueuedOpps.erase(opportunityId);
        for (auto it = _byBot.begin(); it != _byBot.end();)
        {
            if (it->second.opportunityId == opportunityId)
                it = _byBot.erase(it);
            else
                ++it;
        }
    }

    void Clear()
    {
        _byBot.clear();
        _recordedQueuedOpps.clear();
    }

private:
    std::unordered_map<std::uint32_t, BotQueueTracking> _byBot;
    std::unordered_set<std::uint64_t> _recordedQueuedOpps;
};

}  // namespace CompanionLfgBridge

#endif
