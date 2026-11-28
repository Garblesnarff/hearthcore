"""Issue #21: Native C++17 & wiring tests for bot-only cooperative dungeon lifecycle and leader execution."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "mod-playerbots-companions" / "src" / "Bot"
STRAT_DIR = ROOT / "mod-playerbots-companions" / "src" / "Ai" / "Base" / "Strategy"
ACTIONS_DIR = ROOT / "mod-playerbots-companions" / "src" / "Ai" / "Base" / "Actions"
SCRIPT_DIR = ROOT / "mod-playerbots-companions" / "src" / "Script"


CPP_HARNESS = r"""
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#include "CompanionCoopLifecycleLogic.h"

using namespace CompanionCoopLifecycle;

#define CHECK(expr)                                                                                  \
    do                                                                                               \
    {                                                                                                \
        if (!(expr))                                                                                 \
        {                                                                                            \
            std::cerr << "CHECK FAILED at line " << __LINE__ << ": " << #expr << "\n";               \
            std::exit(1);                                                                            \
        }                                                                                            \
    } while (0)

static MemberControlInput MakeStockadeMember(uint32_t botGuid, uint64_t oppId, uint32_t groupId,
                                             uint32_t nativeLeader, uint32_t tankLead,
                                             bool inDungeon = true, bool dead = false, bool combat = false,
                                             bool isLfg = true)
{
    MemberControlInput in;
    in.botGuid = botGuid;
    in.opportunityId = oppId;
    in.groupId = groupId;
    in.targetMapId = 34;
    in.isLfgGroup = isLfg;
    in.groupedWithOwner = false;
    in.hasHumanMember = false;
    in.botInTargetDungeon = inDungeon;
    in.botInCombat = combat;
    in.botDead = dead;
    in.groupInCombat = combat;
    in.totalMemberSlots = 5;
    in.nativeLeaderGuid = nativeLeader;
    in.electedTankLeadGuid = tankLead;
    in.memberSlotGuids = {101, 102, 103, 104, 105};
    return in;
}

int main()
{
    const uint32_t T0 = 100000;

    // 1. Supported dungeon routes (Stockade map 34 & Deadmines map 36 incl. Sneed 643); unsupported fail closed!
    {
        auto const* stk = LookupDungeonRoute(34);
        CHECK(stk != nullptr && stk->finalBossEntry == 1716 && stk->waypoints.size() == 4);
        auto const* dm = LookupDungeonRoute(36);
        CHECK(dm != nullptr && dm->finalBossEntry == 639 && dm->waypoints.size() == 6);
        CHECK(dm->waypoints[1].bossEntry == 643);  // Sneed (instance_encounters #162) opens Mast Room door
        CHECK(LookupDungeonRoute(33) == nullptr);  // SFK unsupported
        CHECK(LookupDungeonRoute(409) == nullptr); // Molten Core raid unsupported
    }

    // 2. P0-1 & owner-led isolation: targetMapId==0, oppId==0, groupId==0, owner-grouped, or human-member -> Ignore & NO leaked record!
    {
        LifecycleController ctrl;
        auto zeroMap = MakeStockadeMember(101, 901, 700, 101, 101);
        zeroMap.targetMapId = 0;
        CHECK(ctrl.EvaluateMember(zeroMap, T0).action == MemberAction::Ignore);
        CHECK(!ctrl.HasActiveRuns());
        CHECK(ctrl.GetRun(901) == nullptr);

        auto hum = MakeStockadeMember(101, 901, 700, 101, 101);
        hum.groupedWithOwner = true;
        CHECK(ctrl.EvaluateMember(hum, T0).action == MemberAction::Ignore);
        hum.groupedWithOwner = false;
        hum.hasHumanMember = true;
        CHECK(ctrl.EvaluateMember(hum, T0).action == MemberAction::Ignore);
        CHECK(!ctrl.HasActiveRuns());
    }

    // 3. P0-3 regression: mixed in/out-of-dungeon party where nativeLeader (103) != tank (101) MUST NOT flap leaderChanged!
    {
        LifecycleController ctrl;
        LifecycleConfig cfg;
        // 101, 102 inside dungeon (effectiveLeader = tank 101); 103, 104 outside dungeon (effectiveLeader = native 103)
        auto a1 = ctrl.EvaluateMember(MakeStockadeMember(101, 910, 710, 103, 101, true), T0, cfg);
        auto a2 = ctrl.EvaluateMember(MakeStockadeMember(103, 910, 710, 103, 0, false), T0, cfg);
        CHECK(!a1.leaderChanged && a1.effectiveLeaderGuid == 101);
        CHECK(!a2.leaderChanged && a2.effectiveLeaderGuid == 103);
        // Repeat ticks alternating between inside & outside members -> ZERO flapping!
        for (int i = 1; i <= 5; ++i)
        {
            auto r1 = ctrl.EvaluateMember(MakeStockadeMember(101, 910, 710, 103, 101, true), T0 + i, cfg);
            auto r2 = ctrl.EvaluateMember(MakeStockadeMember(103, 910, 710, 103, 0, false), T0 + i, cfg);
            CHECK(!r1.leaderChanged && !r2.leaderChanged);
        }
        CHECK(ctrl.GetRun(910)->leaderFailoverCount == 0);
    }

    // 4. Complete Stockade (map 34) bot-only lifecycle: native readiness -> read-only Peek vs mutating Next ->
    //    real boss kills & loot -> loot grace -> native exit!
    {
        LifecycleController ctrl;
        LifecycleConfig cfg;
        cfg.postBossLootGraceSec = 20;

        for (uint32_t g : {101u, 102u, 103u, 104u})
        {
            auto d = ctrl.EvaluateMember(MakeStockadeMember(g, 901, 700, 103, 101, true), T0, cfg);
            CHECK(d.action == MemberAction::ContinueRun && d.phase == RunPhase::WaitingForGroupEntry && !d.allowNewPulls);
            CHECK(d.effectiveLeaderGuid == 101 && d.isEffectiveLeader == (g == 101));
        }
        CHECK(!ctrl.IsGroupReadyForPull(700));

        auto d5 = ctrl.EvaluateMember(MakeStockadeMember(105, 901, 700, 103, 101, true), T0 + 2, cfg);
        CHECK(d5.phase == RunPhase::Traversing && d5.allowNewPulls);
        CHECK(ctrl.IsGroupReadyForPull(700));

        // P2: PeekProgressionWaypoint is strictly READ-ONLY (does NOT advance waypointIndex even at arrival dist!)
        float wx = 0, wy = 0, wz = 0;
        CHECK(ctrl.PeekProgressionWaypoint(700, 34, 159.5f, 1.2f, -25.6f, wx, wy, wz));
        CHECK(ctrl.GetRun(901)->waypointIndex == 0);  // Unmutated by Peek!
        CHECK(std::fabs(wx - 166.789f) < 0.01f);      // Skips arrived wp0 in read-only peek to show wp1
        // Execute calls NextProgressionWaypoint -> advances waypointIndex!
        CHECK(ctrl.NextProgressionWaypoint(700, 34, 159.5f, 1.2f, -25.6f, T0 + 10, wx, wy, wz));
        CHECK(ctrl.GetRun(901)->waypointIndex == 1);

        // Real boss kills & loot
        CHECK(!ctrl.RecordCreatureKill(700, 34, 1696, true, T0 + 60));
        CHECK(!ctrl.RecordCreatureKill(700, 34, 1663, true, T0 + 120));
        CHECK(ctrl.RecordLootItem(700, 34, 2168, 1, T0 + 125));
        CHECK(ctrl.RecordLootItem(700, 34, 2592, 3, T0 + 126));

        // Final boss Bazil Thredd (1716) killed + real loot -> CompletedLootGrace
        CHECK(ctrl.RecordCreatureKill(700, 34, 1716, true, T0 + 300));
        CHECK(ctrl.RecordLootItem(700, 34, 2942, 1, T0 + 305));
        CHECK(!ctrl.IsGroupReadyForPull(700));
        CHECK(ctrl.IsCompletedGroup(700));

        // P1-B: On native LFG group, holds in CompletedLootGrace until LFG_STATE_FINISHED_DUNGEON is observed
        // so bots NEVER trigger LFG_SPELL_DUNGEON_DESERTER on a real boss clear!
        CHECK(ctrl.EvaluateMember(MakeStockadeMember(101, 901, 700, 103, 101, true), T0 + 325, cfg).action ==
              MemberAction::ContinueRun);
        // World thread observes LFG_STATE_FINISHED_DUNGEON (returns true ONCE, false on duplicate poll — P2-2!)
        CHECK(ctrl.ObserveLfgDungeonFinished(700, T0 + 326));
        CHECK(!ctrl.ObserveLfgDungeonFinished(700, T0 + 328));
        auto exitDec = ctrl.EvaluateMember(MakeStockadeMember(101, 901, 700, 103, 101, true), T0 + 328, cfg);
        CHECK(exitDec.action == MemberAction::ExitCompleted);
        CHECK(exitDec.outcomeState == "completed");
        CHECK(exitDec.outcomeReason == "boss_killed:e1716:b3:l5");
        CHECK(exitDec.outcomeReason.size() <= 64);

        // P0-4 regression: disband during CompletedLootGrace force-claims completed outcome (never stuck in active!)
        LifecycleController ctrlGrace;
        for (uint32_t g : {101u, 102u, 103u, 104u, 105u})
            ctrlGrace.EvaluateMember(MakeStockadeMember(g, 920, 720, 101, 101, true), T0, cfg);
        CHECK(ctrlGrace.RecordCreatureKill(720, 34, 1716, true, T0 + 50));
        uint64_t fOpp = 0;
        std::string fState, fReason;
        CHECK(ctrlGrace.ForceClaimTerminalOnDisband(720, fOpp, fState, fReason));
        CHECK(fOpp == 920 && fState == "completed" && fReason == "boss_killed:e1716:b1:l0");
        CHECK(!ctrlGrace.ForceClaimTerminalOnDisband(720, fOpp, fState, fReason));  // Idempotent!
    }

    // 5. P1-2: ObserveLfgDungeonFinished requires real kill evidence AND NEVER invents finalBossEntryKilled!
    {
        LifecycleController ctrl;
        ctrl.RegisterOrRefreshRun(902, 701, 34, T0, true);
        CHECK(!ctrl.ObserveLfgDungeonFinished(701, T0 + 10));  // 0 kills -> rejected!
        ctrl.RecordCreatureKill(701, 34, 1715, false, T0 + 20);
        CHECK(ctrl.ObserveLfgDungeonFinished(701, T0 + 25));
        auto const* run = ctrl.GetRun(902);
        CHECK(run->finalBossEntryKilled == 0);  // NEVER fabricated!
        CHECK(FormatCompletionOutcomeReason(*run) == "lfg_finished:k1:l0");
    }

    // 6. Bounded wipe recovery, P1-3 stuck-combat stall cap & hard exit cap, P1-5 entry clock gating, and P2 orphan sweep!
    {
        LifecycleController ctrl;
        LifecycleConfig cfg;
        cfg.maxWipes = 2;
        cfg.combatStallCapSec = 100;
        cfg.stallTimeoutSec = 120;
        cfg.exitHardCapSec = 60;

        // P1-5: Pre-LFG assembly outside dungeon (isLfg=false, inDungeon=false) does NOT trip entry_timeout!
        auto pre = ctrl.EvaluateMember(MakeStockadeMember(101, 903, 702, 101, 101, false, false, false, false), T0, cfg);
        CHECK(pre.action == MemberAction::ContinueRun && pre.phase == RunPhase::WaitingForGroupEntry);
        auto preLate = ctrl.EvaluateMember(MakeStockadeMember(101, 903, 702, 101, 101, false, false, false, false), T0 + 500, cfg);
        CHECK(preLate.action == MemberAction::ContinueRun && preLate.phase == RunPhase::WaitingForGroupEntry);

        // Enter dungeon all 5 -> Traversing
        for (uint32_t g : {101u, 102u, 103u, 104u, 105u})
            ctrl.EvaluateMember(MakeStockadeMember(g, 903, 702, 101, 101, true, false, false, true), T0 + 510, cfg);
        CHECK(ctrl.IsGroupReadyForPull(702));

        // P1-3: Continuous stuck combat with ZERO kills STILL trips stall timeout once combatStallCap + stallTimeout passes!
        auto c1 = ctrl.EvaluateMember(MakeStockadeMember(101, 903, 702, 101, 101, true, false, true, true), T0 + 520, cfg);
        CHECK(c1.phase == RunPhase::Traversing);
        auto c2 = ctrl.EvaluateMember(MakeStockadeMember(101, 903, 702, 101, 101, true, false, true, true), T0 + 625, cfg);
        CHECK(c2.phase == RunPhase::Traversing);  // Past combatStallCapSec (100s), lastProgressAt frozen at T0+619!
        auto c3 = ctrl.EvaluateMember(MakeStockadeMember(101, 903, 702, 101, 101, true, false, true, true), T0 + 750, cfg);
        CHECK(c3.phase == RunPhase::AbortedExitPending && c3.action == MemberAction::ContinueRun);  // Held in combat until hard cap!
        // Past exitHardCapSec (60s) -> exits even if combat flag is stuck!
        auto c4 = ctrl.EvaluateMember(MakeStockadeMember(101, 903, 702, 101, 101, true, false, true, true), T0 + 820, cfg);
        CHECK(c4.action == MemberAction::ExitAborted && c4.outcomeReason == "run_stalled");

        // P2: SweepOrphanedGroups cleans up runs whose native group no longer exists!
        CHECK(ctrl.SweepOrphanedGroups({}) == 1);
        CHECK(!ctrl.HasActiveRuns());
    }

    std::cout << "ALL_ISSUE21_CPP_TESTS_PASSED\n";
    return 0;
}
"""


class Issue21CoopDungeonLifecycleTests(unittest.TestCase):
    def test_native_cpp17_coop_lifecycle_state_machine(self) -> None:
        gpp = shutil.which("g++")
        if not gpp:
            self.skipTest("g++ not installed")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "test_coop_lifecycle.cpp"
            bin_path = Path(tmp) / "test_coop_lifecycle"
            src.write_text(CPP_HARNESS, encoding="utf-8")
            comp = subprocess.run(
                [gpp, "-std=c++17", "-DNDEBUG", "-Wall", "-Wextra", "-Werror", f"-I{BOT_DIR}", str(src), "-o", str(bin_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(comp.returncode, 0, f"g++ compile failed:\n{comp.stderr}")
            run = subprocess.run([str(bin_path)], capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 0, f"binary failed:\n{run.stderr}\n{run.stdout}")
            self.assertIn("ALL_ISSUE21_CPP_TESTS_PASSED", run.stdout)

    def test_cpp_wiring_no_owner_online_dependency_and_owner_led_preservation(self) -> None:
        lead_cpp = (STRAT_DIR / "CompanionLeadStrategy.cpp").read_text(encoding="utf-8")
        cls_cpp = (STRAT_DIR / "CompanionClassStrategy.cpp").read_text(encoding="utf-8")
        mgr_cpp = (BOT_DIR / "CompanionBotMgr.cpp").read_text(encoding="utf-8")
        brain_cpp = (BOT_DIR / "CompanionBrain.cpp").read_text(encoding="utf-8")
        playerbots_cpp = (SCRIPT_DIR / "Playerbots.cpp").read_text(encoding="utf-8")
        revive_cpp = (ACTIONS_DIR / "ReviveFromCorpseAction.cpp").read_text(encoding="utf-8")

        # 1. Map-local ObjectAccessor::GetPlayer (P0-5 & P1-1), P1-A owner-led offline skip, and read-only Peek (P2)
        self.assertIn("ObjectAccessor::GetPlayer(*bot, slot.guid)", lead_cpp)
        self.assertIn("ObjectAccessor::GetPlayer(*tank, slot.guid)", lead_cpp)
        self.assertIn("ObjectAccessor::FindConnectedPlayer(slot.guid) != nullptr", lead_cpp)
        self.assertIn("ObjectAccessor::GetPlayer(*bot, slot.guid)", cls_cpp)
        self.assertIn("ObjectAccessor::GetPlayer(*bot, slot.guid)", brain_cpp)
        self.assertIn("CompanionBrain::PeekCoopProgressionWaypoint(", lead_cpp)
        self.assertIn("CompanionBrain::NextCoopProgressionWaypoint(", lead_cpp)
        self.assertIn("CompanionBrain::IsCoopGroupReadyForPull(group)", lead_cpp)

        # 2. CompanionBotMgr: checked isBoundMember + boundGroupId match (P0-2 & P2-1), dynamic leader sync, +loot!
        self.assertIn("bool const isBoundMember =", mgr_cpp)
        self.assertIn("boundGroupId == 0 || boundGroupId == curGroupId", mgr_cpp)
        self.assertIn("CompanionBrain::EvaluateCoopLifecycleMember(", mgr_cpp)
        self.assertIn("-companion quest,+loot", mgr_cpp)
        self.assertIn("coopDungeon", mgr_cpp)

        # 3. CompanionBrain + Playerbots: fast atomic gate (gHasActiveLifecycleRuns), roll loot hook,
        #    ForceClaimTerminalOnDisband (P0-4), gPendingCompletedOpps pruning (P0-6), orphan sweep (P2)!
        self.assertIn("PLAYERHOOK_ON_CREATURE_KILLED_BY_PET", playerbots_cpp)
        self.assertIn("PLAYERHOOK_ON_LOOT_ITEM", playerbots_cpp)
        self.assertIn("PLAYERHOOK_ON_GROUP_ROLL_REWARD_ITEM", playerbots_cpp)
        self.assertIn("gHasActiveLifecycleRuns", brain_cpp)
        self.assertIn("ForceClaimTerminalOnDisband(", brain_cpp)
        self.assertIn("gPendingCompletedOpps.erase(", brain_cpp)
        self.assertIn("SweepOrphanedGroups(", brain_cpp)
        self.assertIn("LFG_STATE_FINISHED_DUNGEON", brain_cpp)

        # 4. ReviveFromCorpseAction: cooperative dungeon recovery guarded ONLY while still in group!
        self.assertIn("bot->GetGroup() != nullptr &&", revive_cpp)


if __name__ == "__main__":
    unittest.main()
