/*
 * This file is part of the mod-playerbots module for AzerothCore. See AUTHORS file for Copyright
 * information; released under GNU GPL v2 license, redistribute/modify under version 2 of the License,
 * or (at your option) any later version.
 */

#ifndef PLAYERBOTS_COMPANIONDUTY_H
#define PLAYERBOTS_COMPANIONDUTY_H

#include <string>
#include <vector>

#include "Define.h"
#include "ObjectGuid.h"

class Creature;
class GameObject;
class PlayerbotAI;
class Player;
class WorldObject;

// Companion duties: SELL, REPAIR, BANK and TRAIN (phase 2 adds CRAFT and FISH, see CompanionCraft). Each duty has a
// need (0..100) and a travel cost; the score is
// need x weight - travel x travel weight. Scores are recomputed at most every EVAL_SECONDS per companion, only in
// autonomy mode (not grouped with the owner), out of combat, alive, and outside instances.
//
// Threads:
//   Evaluate(), RunActiveDuty() and HasActiveDuty() run on the companion's map thread.
//   WhyLines() is read by the GM command (world thread) and the owner's chat command (map thread); it copies under
//   _stateMutex.
//   UpdateWorld() runs on the world thread and refreshes the guild bank snapshot; BankSnapshot() reads it under
//   _bankMutex.
class CompanionDuty
{
public:
    static constexpr uint32 EVAL_SECONDS = 30;
    // Legacy duty timeout, used only with AiPlayerbot.Companion.Lease.Enabled = 0 (the lease's stall rule replaces it).
    static constexpr uint32 TIMEOUT_SECONDS = 300;
    // Lowest equipped item under this durability percent: repair wins over the travel cost and every other duty.
    static constexpr float URGENT_REPAIR_PCT = 10.0f;
    static constexpr uint32 RING_SIZE = 50;
    static constexpr uint32 BANK_REFRESH_SECONDS = 60;

    struct Score
    {
        std::string name;   // sell, repair, bank, train, activity
        float need = 0.0f;
        float travel = 0.0f;
        float score = 0.0f;
    };

    struct BankSlot
    {
        uint8 tab = 0;
        uint8 slot = 0;
        uint32 entry = 0;
        uint32 count = 0;
    };

    struct BankTabUsage
    {
        uint8 tab = 0;
        uint32 used = 0;   // occupied slots at the last snapshot
    };

    // A walk target for a duty whose spawn is not in range: the spawn's position and identity. valid = false when
    // there is none. Walks are on the companion's own map only; a duty never teleports.
    struct DutyPoint
    {
        bool valid = false;
        uint32 mapId = 0;
        float x = 0.0f, y = 0.0f, z = 0.0f;
        uint32 spawnId = 0;
        bool isGo = false;
        uint64 key = 0;   // spawn key, for the cooldown after the duty ends
        uint32 zone = 0;  // spawn zone (landmass test for boats / portals, CompanionTransport)
    };

    // Walk and search limits for a far duty. The search runs within FAR_SEARCH_YARDS of the bot; a walk that finds
    // nothing within FAR_ARRIVE_YARDS of the point ends the duty. With the lease on, the lease's stall rule, cooldown
    // and retry budget (CompanionLease) apply. With the lease off (legacy), a far duty times out after
    // FAR_TIMEOUT_SECONDS and its spawn cools down for FAR_COOLDOWN_SECONDS.
    static constexpr uint32 FAR_TIMEOUT_SECONDS = 900;
    static constexpr uint32 FAR_COOLDOWN_SECONDS = 1800;
    static constexpr float FAR_SEARCH_YARDS = 60.0f;
    static constexpr float FAR_ARRIVE_YARDS = 20.0f;

    // Map thread (TickBot, autonomy only). Scores and picks or keeps the duty. Throttled to EVAL_SECONDS.
    static void Evaluate(PlayerbotAI* botAI);
    // Map thread (trigger). True while a duty is active.
    static bool HasActiveDuty(uint32 lowGuid);
    // Any thread. Name of the active duty ("" when none).
    static std::string ActiveName(uint32 lowGuid);
    // Any thread. The next Evaluate runs without waiting for EVAL_SECONDS (GM command).
    static void EvaluateSoon(uint32 lowGuid);
    // Map thread (action). The active duty's target: the object when known, else the far point (target empty).
    // Clears the duty and returns false when its lease was ended (stall, timeout, owner command), or on the legacy
    // timeout with the lease off.
    static bool ActiveTarget(uint32 lowGuid, std::string& name, ObjectGuid& target, DutyPoint& point);
    // Map thread (action). Records the spawn a far duty found within reach.
    static void BindTarget(uint32 lowGuid, ObjectGuid target);
    // Map thread (action). The spawn of the point nearest the bot, searched within FAR_SEARCH_YARDS of the bot.
    // Null when none is in range. Matches by spawn id, so a spawn with another entry (multispawn) still counts.
    static WorldObject* FindSpawnNear(PlayerbotAI* botAI, DutyPoint const& point);
    // Map thread (action). Performs the duty at target (already in range). True when the duty is finished.
    static bool PerformDuty(PlayerbotAI* botAI, std::string const& name, WorldObject* target);
    // How a duty ended. Interrupted (grouped, instanced, owner command) never puts a cooldown on the target.
    enum class DutyEnd : uint8
    {
        Done,
        Failed,
        Interrupted
    };
    // Any thread. Ends the duty, with a choice line in the ring buffer, and ends its lease ("duty:<name>") with
    // reason (a short code such as "done", "fail:target-gone", "interrupt:group"). Combat and death do not end a
    // duty: it pauses and resumes after.
    static void CompleteDuty(uint32 lowGuid, std::string const& choice, DutyEnd how = DutyEnd::Done,
                             char const* reason = "done");
    // Any thread. Lines for "why": activity, top three scores with terms, last two choices, the lease line.
    static std::vector<std::string> WhyLines(uint32 lowGuid);
    // Map thread (owner's chat command). Sends each line to the owner: party chat when grouped, else a whisper.
    static void ReplyToOwner(PlayerbotAI* botAI, std::vector<std::string> const& lines);

    // Any thread. Writes one row to companion_ledger (async, never blocks). action and note must be code literals;
    // item and count are integers; tab < 0 stores NULL (not a guild bank move).
    static void RecordLedger(uint32 lowGuid, char const* action, uint32 item, uint32 count, int tab, char const* note);

    // World thread. Refreshes the guild bank snapshot every BANK_REFRESH_SECONDS.
    static void UpdateWorld(uint32 elapsed);
    // Any thread. Snapshot of a guild's purchased tabs (ascending tab id, with used slots) and every item in them.
    // False when the guild has no snapshot yet.
    static bool BankSnapshot(uint32 guildId, std::vector<BankTabUsage>& tabs, std::vector<BankSlot>& slots);
    // Any thread. Item count of one entry in a guild's bank snapshot (0 when no snapshot). No copy.
    static uint64 BankStock(uint32 guildId, uint32 entry);
    // Any thread. Drops a guild's snapshot after the bot moved items, until the next refresh.
    static void InvalidateBank(uint32 guildId);
};

#endif
