"""Issue #22: Native C++17 + Python unit tests for cooperative drain, safe owner recall, shift-end & outage handling."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hearthcore.identity import persona_id_for_slug
from hearthcore.opportunities import (
    NativeReconcileSnapshot,
    OpportunityCoordinator,
    coop_enabled,
    coop_mode,
)

ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "mod-playerbots-companions" / "src" / "Bot"
ACTIONS_DIR = ROOT / "mod-playerbots-companions" / "src" / "Ai" / "Base" / "Actions"
SCRIPT_DIR = ROOT / "mod-playerbots-companions" / "src" / "Script"
BRAIN_CPP = BOT_DIR / "CompanionBrain.cpp"
MGR_CPP = BOT_DIR / "CompanionBotMgr.cpp"
LFG_CPP = ACTIONS_DIR / "LfgActions.cpp"
CMD_SCRIPT_CPP = SCRIPT_DIR / "PlayerbotCommandScript.cpp"


CPP_HARNESS = r"""
#include <cstdlib>
#include <iostream>
#include <string>
#include <unordered_set>
#include <vector>

#include "CompanionCoopDrainLogic.h"

using namespace CompanionCoopDrain;

#define CHECK(expr)                                                                                  \
    do                                                                                               \
    {                                                                                                \
        if (!(expr))                                                                                 \
        {                                                                                            \
            std::cerr << "CHECK FAILED at line " << __LINE__ << ": " << #expr << "\n";               \
            std::exit(1);                                                                            \
        }                                                                                            \
    } while (0)

static MemberBoundaryInput MakeInput(uint32_t botGuid, uint32_t groupId, uint64_t token)
{
    MemberBoundaryInput in;
    in.botGuid = botGuid;
    in.groupId = groupId;
    in.groupToken = token;
    in.hasHumanMember = false;
    in.botInCombat = false;
    in.groupInCombat = false;
    in.botDead = false;
    in.inDungeonInstance = true;
    in.isLfgGroup = true;
    in.lfgDungeonFinished = false;
    return in;
}

int main()
{
    const uint32_t T0 = 50000;
    const std::vector<uint32_t> PARTY = {101, 102, 103, 104, 105};

    // 1. 3-state CoopMode parsing (including yes/no parity with Python) & admission separation.
    {
        CHECK(ParseCoopMode(false, "") == CoopMode::Off);
        CHECK(ParseCoopMode(true, "") == CoopMode::Enabled);
        CHECK(ParseCoopMode(true, "draining") == CoopMode::Draining);
        CHECK(ParseCoopMode(true, "off") == CoopMode::Off);
        CHECK(ParseCoopMode(false, "yes") == CoopMode::Enabled);
        CHECK(ParseCoopMode(true, "no") == CoopMode::Off);
        CHECK(AllowsNewAdmissions(CoopMode::Enabled));
        CHECK(!AllowsNewAdmissions(CoopMode::Draining));
        CHECK(!AllowsNewAdmissions(CoopMode::Off));
    }

    // 2. Toggle OFF during combat -> HoldInCombat -> safe boundary ExitAtSafeBoundary -> ExitPending with
    //    bounded retry/stuck deadline -> actual GroupScript::OnRemoveMember & auto-disband OnGroupDisbanded!
    {
        DrainCoordinator dc;
        dc.RegisterCommittedGroup(10, 1, 700, 41, 36, true, PARTY, T0, T0 + 3600);
        CHECK(!dc.IsHoldingForDrain(101, 700, 41, false, CoopMode::Enabled, T0 + 5));

        dc.TriggerGlobalDrain(CoopMode::Off, T0 + 100, 600);
        CHECK(dc.IsHoldingForDrain(101, 700, 41, false, CoopMode::Off, T0 + 105));

        auto inCombat = MakeInput(101, 700, 41);
        inCombat.botInCombat = true;
        inCombat.groupInCombat = true;
        auto d1 = dc.EvaluateMember(CoopMode::Off, inCombat, T0 + 105, 600);
        CHECK(d1.action == DrainAction::HoldInCombat && d1.reason == DrainReason::ToggleOff);
        CHECK(!d1.forcedByGraceDeadline && !d1.fabricateFailureMemory);

        // Dead bot mid-wipe holds until alive OR bounded grace deadline expires!
        auto deadIn = MakeInput(101, 700, 41);
        deadIn.botDead = true;
        CHECK(dc.EvaluateMember(CoopMode::Off, deadIn, T0 + 115, 600).action == DrainAction::HoldInCombat);

        // Combat ends -> safe boundary reached -> ExitAtSafeBoundary!
        auto safeIn = MakeInput(101, 700, 41);
        auto dExit = dc.EvaluateMember(CoopMode::Off, safeIn, T0 + 120, 600);
        CHECK(dExit.action == DrainAction::ExitAtSafeBoundary);
        CHECK(dExit.reason == DrainReason::ToggleOff && dExit.outcomeTag == "coop_toggled_off");
        CHECK(dExit.expectLfgDeserterPenalty && !dExit.fabricateFailureMemory);

        // Finished LFG dungeon reports expectLfgDeserterPenalty == false!
        auto safeFinished = safeIn;
        safeFinished.lfgDungeonFinished = true;
        CHECK(!dc.EvaluateMember(CoopMode::Off, safeFinished, T0 + 120, 600).expectLfgDeserterPenalty);

        // P1-3 + P2: MarkExitInitiated keeps ownership alive as ExitPending, retries after exitRetrySec (15s),
        // respects safeBoundary before exitStuckDeadlineSec (60s), and stays paced by exitRetrySec even after 60s!
        CHECK(dc.MarkExitInitiated(10, 101, DrainReason::ToggleOff, T0 + 120));
        CHECK(dc.EvaluateMember(CoopMode::Off, safeIn, T0 + 125, 600, 15, 60).action == DrainAction::ExitPending);
        // Pulled back into combat before stuck deadline -> holds in combat instead of retrying mid-pull!
        auto combatRetry = safeIn;
        combatRetry.botInCombat = true;
        CHECK(dc.EvaluateMember(CoopMode::Off, combatRetry, T0 + 135, 600, 15, 60).action == DrainAction::HoldInCombat);
        // Safe boundary -> retry due at +15s
        auto dRetry = dc.EvaluateMember(CoopMode::Off, safeIn, T0 + 135, 600, 15, 60);
        CHECK(dRetry.action == DrainAction::ExitAtSafeBoundary && dRetry.retryExit && !dRetry.exitStuckExceeded);
        CHECK(dc.MarkExitInitiated(10, 101, DrainReason::ToggleOff, T0 + 135));
        CHECK(dc.EvaluateMember(CoopMode::Off, safeIn, T0 + 145, 600, 15, 60).action == DrainAction::ExitPending);
        // Past 60s stuck deadline -> retries even if in combat, AND after re-marking stays paced (ExitPending at +1s)!
        auto dStuck = dc.EvaluateMember(CoopMode::Off, combatRetry, T0 + 180, 600, 15, 60);
        CHECK(dStuck.action == DrainAction::ExitAtSafeBoundary && dStuck.retryExit && dStuck.exitStuckExceeded);
        CHECK(dStuck.forcedByGraceDeadline);
        CHECK(dc.MarkExitInitiated(10, 101, DrainReason::ToggleOff, T0 + 180));
        CHECK(dc.EvaluateMember(CoopMode::Off, combatRetry, T0 + 181, 600, 15, 60).action == DrainAction::ExitPending);
        CHECK(dc.IsOwnedOpportunity(10));

        // Actual OnRemoveMember fires for first 3 members; then auto-disband fires when 4th leaves!
        uint64_t outOpp = 0;
        DrainReason outReason = DrainReason::None;
        bool full = false;
        bool wasDrain = false;
        for (size_t i = 0; i < 3; ++i)
        {
            CHECK(dc.OnActualMemberRemoved(700, 41, PARTY[i], T0 + 185, 600, outOpp, outReason, full, wasDrain));
            CHECK(outOpp == 10 && outReason == DrainReason::ToggleOff && !full && wasDrain);
        }
        auto disbandRes = dc.OnGroupDisbanded(700, 41);
        CHECK(disbandRes.size() == 1 && disbandRes[0].opportunityId == 10);
        CHECK(disbandRes[0].wasDraining && disbandRes[0].reason == DrainReason::ToggleOff);
        CHECK(disbandRes[0].remainingMembers.size() == 2);
        CHECK(!dc.IsOwnedOpportunity(10));
    }

    // 3. ENFORCED bounded grace deadline: stuck combat / permanent corpse exits cleanly once drainDeadline hits!
    {
        DrainCoordinator dc;
        dc.RegisterCommittedGroup(15, 1, 705, 46, 36, true, PARTY, T0, T0 + 3600);
        dc.TriggerGlobalDrain(CoopMode::Draining, T0 + 100, /*drainGraceSec=*/60);

        auto stuckCombat = MakeInput(101, 705, 46);
        stuckCombat.botInCombat = true;
        stuckCombat.groupInCombat = true;
        CHECK(dc.EvaluateMember(CoopMode::Draining, stuckCombat, T0 + 159, 60).action == DrainAction::HoldInCombat);
        auto dForced = dc.EvaluateMember(CoopMode::Draining, stuckCombat, T0 + 160, 60);
        CHECK(dForced.action == DrainAction::ExitAtSafeBoundary && dForced.forcedByGraceDeadline);
        CHECK(!dForced.fabricateFailureMemory && dForced.outcomeTag == "coop_draining");
    }

    // 4. P1-5: Normal exit from a healthy non-draining run is NOT misclassified as a drain!
    {
        DrainCoordinator dc;
        dc.RegisterCommittedGroup(25, 1, 708, 48, 36, true, PARTY, T0, T0 + 3600);
        uint64_t outOpp = 0;
        DrainReason outReason = DrainReason::None;
        bool full = false;
        bool wasDrain = true;
        // All 5 leave after normal dungeon completion -> wasDrain MUST be false!
        for (size_t i = 0; i < 4; ++i)
        {
            CHECK(dc.OnActualMemberRemoved(708, 48, PARTY[i], T0 + 500, 600, outOpp, outReason, full, wasDrain));
            CHECK(outOpp == 25 && !wasDrain && !full);
            CHECK(outReason == DrainReason::None);
        }
        // Remaining member is STILL NOT marked as draining (group wasn't flipped to MemberDeparted)!
        CHECK(!dc.IsHoldingForDrain(105, 708, 48, false, CoopMode::Enabled, T0 + 501));
        auto disbandRes = dc.OnGroupDisbanded(708, 48);
        CHECK(disbandRes.size() == 1 && !disbandRes[0].wasDraining);
    }

    // 5. P2-1 + Owner recall & shift end:
    //    - Recall for an IDLE bot (not in any owned group) returns false and NEVER arms a stale recall!
    //    - Recall for a bot in an owned group returns true, leaves at safe boundary, and transitions remaining 4
    //      to MemberDeparted safe drain!
    {
        DrainCoordinator dc;
        CHECK(!dc.RequestOwnerRecall(101, T0 + 10, 600));
        CHECK(!dc.HasPendingRecall(101));

        // Now bind 101 into a new group -> must continue active (NOT killed by prior idle recall!)
        dc.RegisterCommittedGroup(30, 1, 702, 43, 36, true, PARTY, T0 + 20, T0 + 3600);
        auto m101Healthy = MakeInput(101, 702, 43);
        CHECK(dc.EvaluateMember(CoopMode::Enabled, m101Healthy, T0 + 25, 600).action == DrainAction::ContinueActive);

        // Now recall 101 WHILE in owned group -> returns true!
        CHECK(dc.RequestOwnerRecall(101, T0 + 50, 600));
        auto m101Combat = MakeInput(101, 702, 43);
        m101Combat.groupInCombat = true;
        CHECK(dc.EvaluateMember(CoopMode::Enabled, m101Combat, T0 + 55, 600).action == DrainAction::HoldInCombat);

        m101Combat.groupInCombat = false;
        auto dRec = dc.EvaluateMember(CoopMode::Enabled, m101Combat, T0 + 60, 600);
        CHECK(dRec.action == DrainAction::ExitAtSafeBoundary && dRec.reason == DrainReason::OwnerRecall);
        CHECK(dc.MarkExitInitiated(30, 101, DrainReason::OwnerRecall, T0 + 60));

        uint64_t outOpp = 0;
        DrainReason outReason = DrainReason::None;
        bool full = false;
        bool wasDrain = false;
        CHECK(dc.OnActualMemberRemoved(702, 43, 101, T0 + 61, 600, outOpp, outReason, full, wasDrain));
        CHECK(outOpp == 30 && outReason == DrainReason::OwnerRecall && !full && wasDrain);
        CHECK(!dc.HasPendingRecall(101));

        // Remaining member 102 in combat HOLDS safely under MemberDeparted!
        auto m102Combat = MakeInput(102, 702, 43);
        m102Combat.botInCombat = true;
        m102Combat.groupInCombat = true;
        CHECK(dc.IsHoldingForDrain(102, 702, 43, false, CoopMode::Enabled, T0 + 62));
        auto d102Hold = dc.EvaluateMember(CoopMode::Enabled, m102Combat, T0 + 62, 600);
        CHECK(d102Hold.action == DrainAction::HoldInCombat && d102Hold.reason == DrainReason::MemberDeparted);
    }

    // 6. P1 + P2: SweepOrphanedOrExpiredGroups uses DIRECT dbClosedOppIds (NEVER time-filtered reload absence!):
    //    - RunExpired active group (NOT in dbClosedOppIds, native group alive) survives sweep & holds in combat!
    //    - Single-member recall deadline NEVER sweeps the 4 healthy members out of ownership!
    //    - Vanished native group -> swept with needsDbDrainWrite=true!
    //    - Confirmed DB-closed row -> swept with needsDbDrainWrite=false!
    //    - Group-level drainDeadline overrun -> swept with needsDbDrainWrite=true!
    {
        DrainCoordinator dc2;
        dc2.RegisterCommittedGroup(61, 1, 801, 51, 36, true, PARTY, T0, T0 + 3600);  // native group vanished
        dc2.RegisterCommittedGroup(62, 1, 802, 52, 36, true, PARTY, T0, T0 + 3600);  // confirmed DB-closed
        dc2.RegisterCommittedGroup(63, 1, 803, 53, 36, true, PARTY, T0, T0 + 40);    // RunExpired in DB, still active!
        dc2.RegisterCommittedGroup(65, 1, 805, 55, 36, true, PARTY, T0, T0 + 3600);  // 1 member recalled & offline
        CHECK(dc2.RequestOwnerRecall(101, T0 + 10, /*drainGraceSec=*/30));

        // Even past T0 + 200 (way past recall grace+60s AND past opp 63's expiresAt=T0+40), neither 63 nor 65 is swept!
        std::unordered_set<uint64_t> dbClosed = {62};                 // ONLY 62 confirmed closed in DB
        std::unordered_set<uint32_t> liveGroups = {802, 803, 805};    // 801 vanished from sGroupMgr
        auto cleaned = dc2.SweepOrphanedOrExpiredGroups(dbClosed, liveGroups, {}, T0 + 200, 60);
        CHECK(cleaned.size() == 2);
        CHECK(dc2.IsOwnedOpportunity(63) && dc2.IsOwnedOpportunity(65));
        CHECK(!dc2.IsOwnedOpportunity(61) && !dc2.IsOwnedOpportunity(62));
        for (auto const& c : cleaned)
        {
            if (c.opportunityId == 61)
                CHECK(c.needsDbDrainWrite);
            else if (c.opportunityId == 62)
                CHECK(!c.needsDbDrainWrite);
            else
                CHECK(false);
        }
        // Opp 63 still holds safely through combat on RunExpired!
        auto m63 = MakeInput(102, 803, 53);
        m63.botInCombat = true;
        auto d63 = dc2.EvaluateMember(CoopMode::Enabled, m63, T0 + 205, 600);
        CHECK(d63.action == DrainAction::HoldInCombat && d63.reason == DrainReason::RunExpired);

        // Group-level drainDeadline overrun sweep (deadline T0+40, overrun=60 -> swept at T0+100 even if group exists!)
        DrainCoordinator dc;
        dc.RegisterCommittedGroup(64, 1, 804, 54, 36, true, PARTY, T0, T0 + 3600);
        dc.TriggerGlobalDrain(CoopMode::Draining, T0 + 10, /*drainGraceSec=*/30);
        auto overrun = dc.SweepOrphanedOrExpiredGroups({}, {804}, {}, T0 + 100, 60);
        CHECK(overrun.size() == 1 && overrun[0].needsDbDrainWrite && !dc.HasOwnedGroups());
    }

    // 7. DB / worker outage during combat + Human groups untouched!
    {
        DrainCoordinator dc;
        dc.RegisterCommittedGroup(40, 1, 703, 44, 34, true, PARTY, T0, T0 + 3600);
        dc.NoteDbOutage(T0 + 10, /*outageGraceSec=*/180, 600);
        auto m = MakeInput(101, 703, 44);
        CHECK(dc.EvaluateMember(CoopMode::Enabled, m, T0 + 100, 600).action == DrainAction::ContinueActive);

        dc.NoteDbOutage(T0 + 200, /*outageGraceSec=*/180, 600);
        m.botInCombat = true;
        CHECK(dc.EvaluateMember(CoopMode::Enabled, m, T0 + 201, 600).action == DrainAction::HoldInCombat);
        m.botInCombat = false;
        auto dOut = dc.EvaluateMember(CoopMode::Enabled, m, T0 + 205, 600);
        CHECK(dOut.action == DrainAction::ExitAtSafeBoundary && dOut.reason == DrainReason::WorkerOutage);
        CHECK(dOut.outcomeTag == "worker_outage_drained" && !dOut.fabricateFailureMemory);

        auto hum = MakeInput(101, 703, 44);
        hum.hasHumanMember = true;
        CHECK(!dc.IsHoldingForDrain(101, 703, 44, true, CoopMode::Off, T0 + 210));
        CHECK(dc.EvaluateMember(CoopMode::Off, hum, T0 + 210, 600).action == DrainAction::NotOwned);
    }

    std::cout << "ALL_ISSUE22_CPP_TESTS_PASSED\n";
    return 0;
}
"""


class Issue22CoopDrainTests(unittest.TestCase):
    def test_native_cpp17_drain_coordinator(self) -> None:
        gpp = shutil.which("g++")
        if not gpp:
            self.skipTest("g++ not installed")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "test_coop_drain.cpp"
            bin_path = Path(tmp) / "test_coop_drain"
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
            self.assertIn("ALL_ISSUE22_CPP_TESTS_PASSED", run.stdout)

    def test_python_3state_coop_mode_drained_outcome_and_reconcile(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HEARTHCORE_COOP_MODE", None)
            os.environ.pop("HEARTHCORE_COOP_ENABLED", None)
            self.assertEqual(coop_mode(), "off")
            self.assertFalse(coop_enabled())

        with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_ENABLED": "1", "HEARTHCORE_COOP_MODE": "draining"}):
            self.assertEqual(coop_mode(), "draining")
            self.assertFalse(coop_enabled(), "Draining mode MUST stop new admissions!")

        with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_ENABLED": "0", "HEARTHCORE_COOP_MODE": "enabled"}):
            self.assertEqual(coop_mode(), "enabled")
            self.assertTrue(coop_enabled())

        now = 1_800_000_000
        party = [("t1", 601, "tank"), ("h1", 602, "healer"), ("d1", 603, "damage"), ("d2", 604, "damage"), ("d3", 605, "damage")]
        with tempfile.TemporaryDirectory() as tmp:
            coord = OpportunityCoordinator(Path(tmp) / "brain.db")

            def _make_active_run(gid: int, run_id: str, t0: int) -> int:
                opp = coord.create_opportunity(dungeon_id="stockade_n", creator_guid=601, is_shadow=False, now_ts=t0)
                for slug, guid, role in party:
                    self.assertTrue(
                        coord.express_interest(
                            opportunity_id=opp["id"], expected_revision=1, persona_id=persona_id_for_slug(slug),
                            character_guid=guid, character_slug=slug, role=role, reason_code="vol",
                            available_until=t0 + 3600, now_ts=t0,
                        )
                    )
                res = coord.reserve_party(opp["id"], expected_revision=1, now_ts=t0 + 1)
                self.assertTrue(res["ok"])
                rev = res["revision"]
                for slug, guid, _ in party:
                    c = coord.confirm_member(opp["id"], expected_revision=rev, persona_id=persona_id_for_slug(slug), character_guid=guid, accepted=True, now_ts=t0 + 5)
                    self.assertTrue(c["ok"])
                nh = coord.record_native_handoff(opp["id"], expected_revision=c["revision"], native_state="in_instance", group_id=gid, native_run_id=run_id, now_ts=t0 + 10)
                self.assertTrue(nh["ok"])
                return int(opp["id"])

            # 1. Neutral 'drained' outcome works even while HEARTHCORE_COOP_MODE=draining (coop_enabled()==False)!
            opp1 = _make_active_run(700, "run_700", now)
            with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_MODE": "draining", "HEARTHCORE_COOP_ENABLED": "0"}):
                self.assertFalse(coop_enabled())
                dr = coord.record_native_outcome(opp1, native_run_id="run_700", outcome="drained", outcome_reason="coop_draining", now_ts=now + 50)
                self.assertTrue(dr["ok"])
                self.assertEqual(dr["state"], "drained")
                opp_after = coord.get_opportunity(opp1)
                assert opp_after is not None
                self.assertTrue(all(m["state"] == "released" for m in opp_after["members"]))
                dr_dup = coord.record_native_outcome(opp1, native_run_id="run_700", outcome="drained", outcome_reason="coop_draining", now_ts=now + 55)
                self.assertTrue(dr_dup["ok"] and dr_dup["idempotent"])

            # 2. reconcile_native_state maps drained_outcomes neutrally (NOT aborted) even in Off mode!
            opp2 = _make_active_run(701, "run_701", now + 100)
            with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_MODE": "off", "HEARTHCORE_COOP_ENABLED": "0"}):
                rec = coord.reconcile_native_state(
                    NativeReconcileSnapshot(
                        authoritative=True,
                        active_group_ids=frozenset(),
                        active_queue_ids=frozenset(),
                        active_run_ids=frozenset(),
                        drained_outcomes={opp2: "coop_toggled_off"},
                    ),
                    now_ts=now + 150,
                )
                self.assertEqual(rec["drained_reconciled"], 1)
                self.assertEqual(rec["aborted_missing_native"], 0)
                opp2_after = coord.get_opportunity(opp2)
                assert opp2_after is not None
                self.assertEqual(opp2_after["state"], "drained")
                self.assertEqual(opp2_after["outcome_reason"], "coop_toggled_off")
                self.assertTrue(all(m["state"] == "released" for m in opp2_after["members"]))

    def test_wiring_invariants_in_brain_botmgr_lfg_and_commandscript(self) -> None:
        brain = BRAIN_CPP.read_text(encoding="utf-8")
        mgr = MGR_CPP.read_text(encoding="utf-8")
        lfg = LFG_CPP.read_text(encoding="utf-8")
        script = CMD_SCRIPT_CPP.read_text(encoding="utf-8")

        self.assertIn('#include "CompanionCoopDrainLogic.h"', brain)
        self.assertIn("CompanionBrain::GetCoopMode()", brain)
        self.assertIn("IsCoopBootProbeDone()", brain)
        self.assertIn("gCoopDbHealthy", brain)
        self.assertIn("gPendingDrainedOpps", brain)
        self.assertIn("SweepOrphanedOrExpiredGroups(", brain)
        self.assertIn("SELECT id, state FROM companion_opportunities WHERE id IN (", brain)
        self.assertIn("COOP_EXIT_MAX_RETRIES", brain)
        self.assertIn("restoreDeferred(", brain)
        self.assertIn("gDrainCoordinator.TriggerGlobalDrain(", brain)
        self.assertIn("gDrainCoordinator.NoteDbOutage(", brain)
        self.assertIn("gDrainCoordinator.IsHoldingForDrain(", brain)
        self.assertIn("gDrainCoordinator.EvaluateMember(", brain)
        self.assertIn("gDrainCoordinator.MarkExitInitiated(", brain)
        self.assertIn("gDrainCoordinator.OnActualMemberRemoved(", brain)
        self.assertIn("bot->GetMap()->GetPlayers()", brain)
        self.assertIn("SPELL_LFG_DUNGEON_DESERTER", brain)
        self.assertIn("LFG_STATE_FINISHED_DUNGEON", brain)
        # P1-2: DB probe MUST execute before FlushCoopGroupMutations in ReloadCoopMemberships!
        reload_body = brain.split("void CompanionBrain::ReloadCoopMemberships()", 1)[1]
        self.assertLess(
            reload_body.index("SELECT COUNT(*) FROM information_schema.tables"),
            reload_body.index("FlushCoopGroupMutations();"),
        )

        # TickBot waits for boot probe, evaluates cooperative drain BEFORE stray-group disband, marks exit initiated.
        self.assertIn("CompanionBrain::IsCoopBootProbeDone()", mgr)
        self.assertIn("IsCoopGroupHoldingForDrain(", mgr)
        self.assertIn("DrainAction::HoldInCombat", mgr)
        self.assertIn("DrainAction::ExitPending", mgr)
        self.assertIn("DrainAction::ExitAtSafeBoundary", mgr)
        self.assertIn("CompanionBrain::MarkCoopExitInitiated(", mgr)
        self.assertIn("CompanionBrain::ClearCoopOwnerRecall(", mgr)

        # GroupScript hooks OnRemoveMember (guarding GROUP_REMOVEMETHOD_KICK on LFG) & .companion recall wires owner recall!
        self.assertIn("GROUPHOOK_ON_REMOVE_MEMBER", script)
        self.assertIn("GROUP_REMOVEMETHOD_KICK", script)
        self.assertIn("CompanionBrain::OnNativeGroupMemberRemoved(", script)
        self.assertIn("HandleCompanionRecallCommand", script)
        self.assertIn("CompanionBrain::RequestCoopOwnerRecall(", script)

        # Pending LFG queue cancelled cleanly on world thread when coop mode switches to Off/Draining
        self.assertIn("EvaluateLeaveQueue(", brain)
        self.assertIn("TickCoopLfgOnWorldThread()", brain)


if __name__ == "__main__":
    unittest.main()
