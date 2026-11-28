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

class Group;
class Item;
struct ItemTemplate;
class Player;
class PlayerbotAI;
namespace CompanionCoopAuth
{
struct NativeGroupSnapshot;
}
namespace CompanionCoopDrain
{
enum class CoopMode : uint8;
enum class DrainReason : uint8;
struct DrainDecision;
}
namespace CompanionLfgBridge
{
struct JoinDecision;
struct ProposalDecision;
}

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
    // Updates the in-memory cache to match a write that was just queued (async DB write).
    static void UpsertBankRequest(BankRequest const& req);
    static void RemoveBankRequest(uint32 itemEntry);

    // Native proposal binding vertical slice (Issue #3 / #27): returns true if this companion currently
    // has a C++-validated executing guild work-order proposal.
    static bool ActiveOrderProposal(uint32 lowGuid, uint64& outProposalId, uint64& outOrderId, uint32& outItemEntry,
                                    uint32* outSnapshotRev = nullptr);
    static bool MarkProposalCompleted(uint32 lowGuid, uint64 proposalId, uint32 progressDelta,
                                      uint64 expectedOrderId = 0, uint32 expectedSnapshotRev = 0);
    static void ReloadProposals();

    // Phase II Cooperative Mode (Cards II-05/II-06, Issues #11, #12, #22, gated by AiPlayerbot.Companion.Coop.Enabled = 0).
    static CompanionCoopDrain::CoopMode GetCoopMode();
    static bool CoopEnabled();
    static bool IsAuthorizedCoopMember(uint32 lowGuid, uint64& outOppId, uint32& outTargetMap, uint32& outGroupId);
    // Any thread (pure in-memory, never blocks map threads on DB I/O). When allowQueueBind=true (TickBot),
    // ready-to-bind groups are queued for atomic world-thread DB commit and outAssemblyInProgress is set true.
    static bool AuthorizeCoopGroup(Player const* bot, CompanionCoopAuth::NativeGroupSnapshot const& snap,
                                   bool allowQueueBind = false, bool* outAssemblyInProgress = nullptr,
                                   std::string* outReason = nullptr);
    // Issue #22: Cooperative drain, safe owner recall, shift-end & outage handling (NEVER disbands mid-pull!).
    static bool IsCoopBootProbeDone();
    static bool RequestCoopOwnerRecall(uint32 botGuid);
    static void ClearCoopOwnerRecall(uint32 botGuid);
    static bool IsCoopGroupHoldingForDrain(Player const* bot, Group const* group);
    static CompanionCoopDrain::DrainDecision EvaluateCoopDrain(Player const* bot, Group const* group, bool shiftEnded,
                                                               bool ownerRecalled);
    static void MarkCoopExitInitiated(uint64 opportunityId, uint32 botGuid, CompanionCoopDrain::DrainReason reason);
    // GroupScript lifecycle hooks & monotonic token lookup (real group-ID reuse, disband & actual exit release).
    static uint64 GroupTokenFor(Group const* group);
    static uint64 OnNativeGroupCreated(Group const* group);
    static void OnNativeGroupMemberRemoved(Group const* group, uint32 memberLowGuid);
    static void OnNativeGroupDisbanded(Group const* group);
    // Issue #12: Native admission flags, fail-closed resource limits, persisted hysteresis & atomic caps.
    static bool TryAdmitCoopQueue(uint64 opportunityId, uint32 targetMapId, bool allMembersEnrolled,
                                  uint32 additionalLoginsNeeded = 0, std::string* outReason = nullptr);
    static bool TryAdmitCompanionLfgJoin(Player const* bot, std::vector<uint32> const& targetMapIds,
                                         std::string* outReason = nullptr);
    static bool TryAdmitCompanionLfgProposal(Player const* bot, uint32 proposalId, uint32 rawLfgDungeonEntry,
                                             std::string* outReason = nullptr);
    static void ReleaseCoopAdmissionToken(uint64 opportunityId);
    static bool CoopHeadroomPaused();
    // Issue #20: Native WotLK RDF/LFG voluntary queue bridge (MySQL + LFGMgr authoritative, world-thread driven).
    static bool IsCoopLfgTrackedBot(uint32 botGuid);
    static bool AllowBotOnlyLfgProposal(std::vector<uint32> const& candidateGuids);
    static bool TryBuildCompanionLfgJoin(Player const* bot, bool shiftEnded,
                                         CompanionLfgBridge::JoinDecision& outDecision);
    static bool SendCompanionLfgJoin(Player* bot, PlayerbotAI* botAI, bool shiftEnded);
    static void MarkCompanionLfgJoinSent(uint32 botGuid, uint64 opportunityId, uint32 lfgDungeonId);
    static bool ShouldCompanionLeaveLfgQueue(Player const* bot, bool shiftEnded, std::string* outReason = nullptr);
    static void OnCompanionLfgQueueLeft(uint32 botGuid, std::string const& reason, bool isStaleRequeue = false);
    static void OnCompanionJoinedOwnerGroup(uint32 botGuid);
    static void OnCompanionLogout(uint32 botGuid);
    static CompanionLfgBridge::ProposalDecision EvaluateCompanionLfgProposal(Player const* bot, uint32 proposalId,
                                                                             uint32 rawDungeonEntry, uint8 proposalState,
                                                                             bool shiftEnded);
    static void OnCompanionLfgProposalAccepted(uint32 botGuid, uint64 opportunityId, uint32 proposalId,
                                               uint32 lfgDungeonId);
    static void OnCompanionLfgProposalDeclined(uint32 botGuid, uint32 proposalId, std::string const& reason);
    static void OnCompanionLfgProposalFinished(uint32 botGuid, uint32 proposalId, uint8 proposalState);
    // World thread only: drives cooperative LFG queue/leave/deferred-proposal lifecycle safely with LFGMgr,
    // flushes queued binds/disbands/outcomes atomically to MySQL, and reloads memberships.
    static void TickCoopLfgOnWorldThread();
    static void FlushCoopGroupMutations();
    static void ReloadCoopMemberships();

    static constexpr uint32 WEIGHT_REFRESH_MS = 300000;
    static constexpr uint32 PROPOSAL_REFRESH_MS = 60000;
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
    static uint32 _proposalTimer;
    static bool _defaultsLoaded;
    static bool _weightsLoaded;
    static bool _missingLogged;
    static std::atomic<uint32> _withdrawCapStacks;
    static std::atomic<float> _searchRange;
    static std::atomic<uint32> _bankMaxStacks;
};

#endif
