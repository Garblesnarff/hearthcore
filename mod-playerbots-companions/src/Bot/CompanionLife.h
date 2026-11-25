/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANION_LIFE_H
#define PLAYERBOTS_COMPANION_LIFE_H

#include "Define.h"
#include "ObjectGuid.h"

#include <string>
#include <vector>

class Creature;
class Player;
class PlayerbotAI;
class Quest;

// Guild Life v1: personality profiles, real Event Memory (rolling 5-10 events/bot), pair relationships,
// guild work orders with stories, owner session availability (AVAILABLE / BUSY / COMMITTED),
// contextual emotes (wave back, cheer level-ups, sit at inn), and kid-safe activity proposals.
class CompanionLife
{
public:
    enum class Availability : uint8
    {
        Available = 0,
        Busy = 1,
        Committed = 2,
    };

    struct Profile
    {
        uint32 guid = 0;
        std::string name;
        std::string archetype;
        std::string culture;
        std::string flaw;
        std::string bio;
    };

    struct EventRecord
    {
        std::string type;
        std::string summary;
        uint32 partnerGuid = 0;
        uint32 zoneId = 0;
        uint32 atSec = 0;
    };

    struct Relation
    {
        uint32 otherGuid = 0;
        uint32 runsTogether = 0;
        uint32 wipes = 0;
        uint32 helpGiven = 0;
        uint32 lootContests = 0;
        int32 affinity = 0;
        std::string lastEvent;
        uint32 updatedAt = 0;
    };

    struct WorkOrder
    {
        uint64 id = 0;
        uint32 requesterGuid = 0;
        std::string requesterName;
        uint32 crafterGuid = 0;
        std::string crafterName;
        uint32 itemEntry = 0;
        std::string itemName;
        uint32 quantity = 1;
        std::string story;
        std::string status;
        uint32 createdAt = 0;
        uint32 updatedAt = 0;
    };

    // World thread: periodic refresh of profiles, events, relationships, and work orders.
    static void UpdateWorld(uint32 elapsed);
    // World thread (or GM command): force reload from DB.
    static void ReloadAll();

    // Map thread: runs from CompanionBotMgr::TickBot (every 10s, O(1) fast).
    // Handles inn sitting and owner activity proposals.
    static void TickBot(PlayerbotAI* botAI);

    // Owner session awareness: returns AVAILABLE / BUSY / COMMITTED and a short truthful reason.
    static Availability GetAvailability(Player* bot, std::string& detail);
    static char const* AvailabilityName(Availability a);

    // Real event hooks (never hallucinated — strictly from real in-game events).
    static void OnLevelUp(Player* player);
    static void OnQuestComplete(Player* player, Quest const* quest);
    static void OnSkillMilestone(Player* player, uint32 skillId, uint32 newValue);
    static void OnCraftMade(Player* bot, uint32 itemEntry, std::string const& itemName, uint32 skillId);
    static void OnCreatureKill(Player* killer, Creature* killed);
    static void OnPlayerDeath(Player* victim, Creature* killer);

    // Contextual emote reaction when an owner (Wonky / Logan) waves, cheers, salutes, or hugs nearby.
    static void OnTextEmote(Player* bot, ObjectGuid senderGuid, uint32 textEmote);

    // Owner whisper handler for Guild Life queries ("busy", "news", "story", "no", "later").
    // Returns true if handled.
    static bool HandleOwnerWhisper(PlayerbotAI* botAI, Player* sender, std::string const& message);

    // Create a guild work order with a story (deduplicated per requester + itemEntry).
    static bool CreateWorkOrder(uint32 requesterGuid, std::string const& requesterName, uint32 itemEntry,
                                uint32 quantity, std::string const& story);

    // Guild Life v2: autonomous work-order priority, neighbourhood presence, and weekly guild events.
    static bool HasOpenWorkOrderFor(uint32 itemEntry, uint32 crafterGuid);
    static bool HasOpenWorkOrderById(uint64 orderId, uint32 crafterGuid, uint32& outItemEntry);
    static float NeighborhoodHubBonus(Player* bot, uint32 hubZoneId);
    static float WeeklyFishingBonus();
    static float WeeklyCraftBonus();
    static std::string ActiveWeeklyEventLabel();
    static uint32 TriggerWorkOrderPulse();

    // Read-only summaries for GM commands (.companion life ...).
    static std::vector<std::string> StatusLines(std::string const& botNameFilter);
    static std::vector<std::string> EventLines(std::string const& botName);
    static std::vector<std::string> RelationLines(std::string const& botNameFilter);
    static std::vector<std::string> WorkOrderLines();
    static std::vector<std::string> WeeklyEventLines();
};

#endif
