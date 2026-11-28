"""Issue #20: Native C++17 + Python tests for voluntary dungeon goals -> native WotLK RDF/LFG execution bridge."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hearthcore.bridge import run_cycle
from hearthcore.identity import persona_id_for_slug
from hearthcore.native_lfg_bridge import (
    build_mysql_publish_statements,
    build_reconcile_snapshot_from_mysql,
    project_mysql_authority_to_sqlite,
    publish_confirmed_opportunity_to_mysql,
    sync_coop_native_lfg_bridge,
)
from hearthcore.opportunities import OpportunityCoordinator

ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "mod-playerbots-companions" / "src" / "Bot"
ACTIONS_DIR = ROOT / "mod-playerbots-companions" / "src" / "Ai" / "Base" / "Actions"
SCRIPT_DIR = ROOT / "mod-playerbots-companions" / "src" / "Script"


CPP_HARNESS = r"""
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#include "CompanionLfgBridgeLogic.h"

using namespace CompanionCoopAuth;
using namespace CompanionLfgBridge;

#define CHECK(expr)                                                                                  \
    do                                                                                               \
    {                                                                                                \
        if (!(expr))                                                                                 \
        {                                                                                            \
            std::cerr << "CHECK FAILED at line " << __LINE__ << ": " << #expr << "\n";               \
            std::exit(1);                                                                            \
        }                                                                                            \
    } while (0)

static AuthorizedRun MakeStockadeRun(uint64_t oppId, uint32_t rev, uint32_t nowSec)
{
    AuthorizedRun run;
    run.opportunityId = oppId;
    run.revision = rev;
    run.targetMapId = 34;  // The Stockade (lfgDungeonId = 12)
    run.difficulty = 0;
    run.runState = "assembling";
    run.assemblyDeadline = nowSec + 300;
    run.expiresAt = nowSec + 3600;
    run.isShadow = false;
    run.boundToNativeGroup = false;
    run.requiredRoles = {1, 1, 3};
    run.roster = {
        {101, "tank", "accepted", rev, true},
        {102, "healer", "accepted", rev, true},
        {103, "damage", "accepted", rev, true},
        {104, "damage", "accepted", rev, true},
        {105, "damage", "accepted", rev, true},
    };
    return run;
}

static JoinEligibilityInput MakeBotJoinInput(uint32_t guid, uint8_t cls, uint8_t lvl = 24)
{
    JoinEligibilityInput in;
    in.botGuid = guid;
    in.botLevel = lvl;
    in.botClass = cls;
    in.isAlliance = true;
    in.nativeLfgState = NATIVE_LFG_STATE_NONE;
    return in;
}

int main()
{
    const uint32_t T0 = 60000;

    // 1. Supported dungeon catalog & fail-closed rejection of unsupported/heroic/random dungeons.
    {
        auto const* stk = ResolveSupportedLfgDungeon(34, 0);
        CHECK(stk != nullptr && stk->lfgDungeonId == 12 && stk->allianceOnly);
        auto const* dm = ResolveSupportedLfgDungeon(36, 0);
        CHECK(dm != nullptr && dm->lfgDungeonId == 6 && !dm->allianceOnly);
        CHECK(ResolveSupportedLfgDungeon(34, 1) == nullptr);   // Heroic rejected
        CHECK(ResolveSupportedLfgDungeon(33, 0) == nullptr);   // SFK unsupported
        CHECK(ResolveSupportedLfgDungeon(48, 0) == nullptr);   // BFD unsupported
        CHECK(LookupSupportedLfgDungeonById(12u | (1u << 24)) != nullptr);
        CHECK(LookupSupportedLfgDungeonById(258u | (6u << 24)) == nullptr);  // Random dungeon rejected
    }

    // 2. Core WoW class role legality.
    {
        CHECK(IsClassLegalForRoleMask(CLASS_ID_WARRIOR, ROLE_MASK_TANK));
        CHECK(IsClassLegalForRoleMask(CLASS_ID_WARRIOR, ROLE_MASK_DAMAGE));
        CHECK(!IsClassLegalForRoleMask(CLASS_ID_WARRIOR, ROLE_MASK_HEALER));
        CHECK(IsClassLegalForRoleMask(CLASS_ID_PRIEST, ROLE_MASK_HEALER));
        CHECK(!IsClassLegalForRoleMask(CLASS_ID_PRIEST, ROLE_MASK_TANK));
        CHECK(IsClassLegalForRoleMask(CLASS_ID_MAGE, ROLE_MASK_DAMAGE));
        CHECK(!IsClassLegalForRoleMask(CLASS_ID_MAGE, ROLE_MASK_TANK));
        CHECK(!IsClassLegalForRoleMask(CLASS_ID_MAGE, ROLE_MASK_HEALER));
        CHECK(IsClassLegalForRoleMask(CLASS_ID_PALADIN, ROLE_MASK_TANK));
        CHECK(IsClassLegalForRoleMask(CLASS_ID_PALADIN, ROLE_MASK_HEALER));
        CHECK(!IsClassLegalForRoleMask(CLASS_ID_PALADIN, ROLE_MASK_TANK | ROLE_MASK_HEALER));
    }

    // 3. Coop OFF regression (P0-2): companions NEVER join or leave an owner's queue when coop is OFF & untracked!
    {
        std::vector<AuthorizedRun> runs = {MakeStockadeRun(501, 2, T0)};
        LfgBridgeController ctrl;
        CHECK(!ctrl.HasAnyTrackedState());
        auto joinOff = ctrl.EvaluateJoinEligibility(false, runs, MakeBotJoinInput(101, CLASS_ID_WARRIOR), T0 + 1);
        CHECK(!joinOff.canJoin && joinOff.reason == "coop_disabled");
        // Untracked companion in owner's LFG queue MUST NOT be told to leave!
        auto leaveUntracked = ctrl.EvaluateLeaveQueue(false, runs, 101, NATIVE_LFG_STATE_QUEUED, true, false, false, false, T0 + 1);
        CHECK(!leaveUntracked.shouldLeave);
    }

    // 4. Five independently willing bots queue specific Stockade (lfgId=12) natively; Queued event emitted ONLY
    //    after core confirms ALL 5 in LFG_STATE_QUEUED (P1-5) + live DBC override (P2-5)!
    {
        std::vector<AuthorizedRun> runs = {MakeStockadeRun(501, 2, T0)};
        LfgBridgeController ctrl;
        const std::pair<uint32_t, uint8_t> party[5] = {
            {101, CLASS_ID_WARRIOR},
            {102, CLASS_ID_PRIEST},
            {103, CLASS_ID_MAGE},
            {104, CLASS_ID_HUNTER},
            {105, CLASS_ID_ROGUE},
        };
        for (int i = 0; i < 5; ++i)
        {
            auto [guid, cls] = party[i];
            auto in = MakeBotJoinInput(guid, cls);
            auto dec = ctrl.EvaluateJoinEligibility(true, runs, in, T0 + 1);
            CHECK(dec.canJoin && dec.opportunityId == 501 && dec.lfgDungeonId == 12 && dec.targetMapId == 34);
            ctrl.MarkJoinPacketSent(guid, dec.opportunityId, dec.lfgDungeonId, T0 + 1);
            CHECK(ctrl.HasTrackedBot(guid));

            // Anti-double-queue inside in-flight window
            auto dup = ctrl.EvaluateJoinEligibility(true, runs, in, T0 + 5);
            CHECK(!dup.canJoin && dup.reason == "join_packet_in_flight");

            // Already in native LFG queue
            in.nativeLfgState = NATIVE_LFG_STATE_QUEUED;
            auto dupNative = ctrl.EvaluateJoinEligibility(true, runs, in, T0 + 20);
            CHECK(!dupNative.canJoin && dupNative.reason == "already_in_native_lfg");

            // P1-5: ObserveNativeQueued emits Queued ONLY when the 5th member is confirmed queued!
            auto qEv = ctrl.ObserveNativeQueued(guid, T0 + 2, runs);
            if (i < 4)
                CHECK(qEv.kind == LfgLifecycleEventKind::None);
            else
                CHECK(qEv.kind == LfgLifecycleEventKind::Queued && qEv.nativeQueueId == "lfgq:501:12");
        }

        // Duplicate ObserveNativeQueued emits nothing
        CHECK(ctrl.ObserveNativeQueued(101, T0 + 3, runs).kind == LfgLifecycleEventKind::None);

        // Live DBC level bounds check (P2-5): DBC says minLevel=26 -> level 24 rejected!
        auto dbcBlocked = MakeBotJoinInput(101, CLASS_ID_WARRIOR, 24);
        dbcBlocked.dbcMinLevel = 26;
        dbcBlocked.dbcMaxLevel = 32;
        CHECK(ctrl.EvaluateJoinEligibility(true, runs, dbcBlocked, T0 + 25).reason == "level_out_of_lfg_band");

        // Locked dungeon, Horde in Stockade, wrong class role, Deserter
        auto bad = MakeBotJoinInput(101, CLASS_ID_WARRIOR);
        bad.lockedLfgDungeonIds.insert(12);
        CHECK(!ctrl.EvaluateJoinEligibility(true, runs, bad, T0 + 25).canJoin);
        auto horde = MakeBotJoinInput(101, CLASS_ID_WARRIOR);
        horde.isAlliance = false;
        CHECK(ctrl.EvaluateJoinEligibility(true, runs, horde, T0 + 25).reason == "faction_ineligible");
        auto wrongCls = MakeBotJoinInput(101, CLASS_ID_MAGE, 24);
        CHECK(ctrl.EvaluateJoinEligibility(true, runs, wrongCls, T0 + 25).reason == "class_ineligible_for_role");
        auto deserter = MakeBotJoinInput(101, CLASS_ID_WARRIOR);
        deserter.hasLfgDeserter = true;
        CHECK(ctrl.EvaluateJoinEligibility(true, runs, deserter, T0 + 25).reason == "lfg_deserter_active");
    }

    // 5. Bot-only LFG queue match gate (exact 5 confirmed companions only; assemblyDeadline respected).
    {
        std::vector<AuthorizedRun> runs = {MakeStockadeRun(501, 2, T0)};
        auto ok = CanMatchBotOnlyProposal(true, runs, {101, 102, 103, 104, 105}, T0 + 10);
        CHECK(ok.allowed && ok.opportunityId == 501 && ok.lfgDungeonId == 12);
        CHECK(!CanMatchBotOnlyProposal(false, runs, {101, 102, 103, 104, 105}, T0 + 10).allowed);
        CHECK(!CanMatchBotOnlyProposal(true, runs, {101, 102, 103, 104, 999}, T0 + 10).allowed);
        CHECK(!CanMatchBotOnlyProposal(true, runs, {101, 102, 103, 104, 105}, T0 + 305).allowed);  // past assemblyDeadline
        runs[0].isShadow = true;
        CHECK(!CanMatchBotOnlyProposal(true, runs, {101, 102, 103, 104, 105}, T0 + 10).allowed);
    }

    // 6. Proposal acceptance at safe boundary, combat/unsafe-boundary deferral (P2-3), UNCONDITIONAL hard timeout
    //    even after combat ends at t=45s (P1-2!), and full cancelOpportunity flag on decline/failure (P1-1, P1-4)!
    {
        std::vector<AuthorizedRun> runs = {MakeStockadeRun(501, 2, T0)};
        LfgBridgeController ctrl;
        uint32_t const typedStockade = 12u | (1u << 24);
        for (uint32_t g : {101u, 102u, 103u, 104u, 105u})
            ctrl.MarkJoinPacketSent(g, 501, 12, T0);

        // Non-initiating state -> ClearStoredOnly
        ProposalInput fin;
        fin.botGuid = 101;
        fin.proposalId = 77;
        fin.rawDungeonEntry = typedStockade;
        fin.proposalState = PROPOSAL_STATE_FAILED;
        CHECK(ctrl.EvaluateProposal(true, runs, fin, T0 + 10).action == ProposalAction::ClearStoredOnly);

        // Bot 101 in unsafe boundary (taxi/flight/casting) -> DeferInCombat -> clears -> Accept!
        ProposalInput p1;
        p1.botGuid = 101;
        p1.proposalId = 77;
        p1.rawDungeonEntry = typedStockade;
        p1.proposalState = PROPOSAL_STATE_INITIATING;
        p1.inUnsafeBoundary = true;
        auto d1 = ctrl.EvaluateProposal(true, runs, p1, T0 + 10, 25, 35);
        CHECK(d1.action == ProposalAction::DeferInCombat && d1.reason == "defer_unsafe_boundary");

        p1.inUnsafeBoundary = false;
        p1.rawDungeonEntry = 0;
        auto d2 = ctrl.EvaluateProposal(true, runs, p1, T0 + 18, 25, 35);
        CHECK(d2.action == ProposalAction::Accept && d2.opportunityId == 501 && d2.lfgDungeonId == 12);
        auto accEv = ctrl.MarkProposalAccepted(101, d2.opportunityId, 77, d2.lfgDungeonId);
        CHECK(accEv.kind == LfgLifecycleEventKind::ProposalAccepted && accEv.proposalId == 77);
        CHECK(ctrl.EvaluateProposal(true, runs, p1, T0 + 19, 25, 35).action == ProposalAction::Ignore);

        // P1-2 & P2-B: Bot 102 enters combat at T0+10, combat ends LATE at T0+45 (>= 35s hard timeout) -> MUST
        // ClearStoredOnly, NEVER Accept; AND a second INITIATING update for proposal 77 stays tombstoned!
        ProposalInput p2;
        p2.botGuid = 102;
        p2.proposalId = 77;
        p2.rawDungeonEntry = typedStockade;
        p2.proposalState = PROPOSAL_STATE_INITIATING;
        p2.inCombat = true;
        CHECK(ctrl.EvaluateProposal(true, runs, p2, T0 + 10, 25, 35).action == ProposalAction::DeferInCombat);
        p2.inCombat = false;
        p2.rawDungeonEntry = 0;
        auto dExpired = ctrl.EvaluateProposal(true, runs, p2, T0 + 45, 25, 35);
        CHECK(dExpired.action == ProposalAction::ClearStoredOnly && dExpired.reason == "proposal_hard_timeout_expired");
        (void)ctrl.OnProposalDeclinedOrFailed(102, 77, false, dExpired.reason, T0 + 45, 30);
        // Second INITIATING update for the SAME expired proposalId 77 MUST NOT reset firstSeen or Accept (P2-B)!
        p2.rawDungeonEntry = typedStockade;
        auto dTomb = ctrl.EvaluateProposal(true, runs, p2, T0 + 46, 25, 35);
        CHECK(dTomb.action == ProposalAction::ClearStoredOnly && dTomb.reason == "proposal_hard_timeout_expired");

        // P2-A: Bot 102 lands in LFG_STATE_NONE after failure/timeout -> PruneIfIdleInNone clears tracking!
        std::vector<AuthorizedRun> emptyRuns;
        CHECK(ctrl.HasTrackedBot(102));
        CHECK(ctrl.PruneIfIdleInNone(102, emptyRuns, T0 + 46));
        CHECK(!ctrl.HasTrackedBot(102));

        // Bot 103 in combat past 25s (< 35s hard timeout) -> Decline with cancelOpportunity=true!
        ProposalInput p3;
        p3.botGuid = 103;
        p3.proposalId = 77;
        p3.rawDungeonEntry = typedStockade;
        p3.inCombat = true;
        CHECK(ctrl.EvaluateProposal(true, runs, p3, T0 + 10, 25, 35).action == ProposalAction::DeferInCombat);
        auto dTimeout = ctrl.EvaluateProposal(true, runs, p3, T0 + 35, 25, 36);
        CHECK(dTimeout.action == ProposalAction::Decline && dTimeout.reason == "combat_defer_timeout");
        auto decEv = ctrl.OnProposalDeclinedOrFailed(103, 77, true, dTimeout.reason, T0 + 35, 30);
        CHECK(decEv.kind == LfgLifecycleEventKind::ProposalDeclined && decEv.cancelOpportunity);

        // ProposalFailed emits ProposalFailed with cancelOpportunity=true (P1-4!)
        // AND P1 regression: non-decline proposal failure KEEPS opportunityId tracked so remaining 4 bots
        // re-added to LFG_STATE_QUEUED by core RemoveProposal leave cleanly when the run is cancelled!
        auto failEv = ctrl.OnProposalDeclinedOrFailed(104, 77, false, "native_proposal_failed", T0 + 36, 30);
        CHECK(failEv.kind == LfgLifecycleEventKind::ProposalFailed && failEv.cancelOpportunity);
        CHECK(ctrl.HasTrackedBot(104));
        std::vector<AuthorizedRun> cancelledRuns = runs;
        cancelledRuns[0].runState = "cancelled";
        auto afterFailLeave = ctrl.EvaluateLeaveQueue(true, cancelledRuns, 104, NATIVE_LFG_STATE_QUEUED,
                                                      false, false, false, false, T0 + 38);
        CHECK(afterFailLeave.shouldLeave && afterFailLeave.reason == "opportunity_closed_or_expired");
        // Force-release bypasses in-flight leave guard (P2-6: logout / owner group join never drops cancel event!)
        auto forcedLeave = ctrl.OnQueueCancelled(104, "bot_offline", T0 + 38, false, true);
        CHECK(forcedLeave.kind == LfgLifecycleEventKind::QueueCancelled && forcedLeave.cancelOpportunity);
    }

    // 7. P1-1 (incomplete roster & assemblyDeadline leave), P1-7 (stale queue requeue), P2-2 (duplicate leave suppression), P2-7 (ClearBot)!
    {
        std::vector<AuthorizedRun> runs = {MakeStockadeRun(501, 2, T0)};
        LfgBridgeController ctrl;
        for (uint32_t g : {101u, 102u, 103u, 104u, 105u})
        {
            ctrl.MarkJoinPacketSent(g, 501, 12, T0);
            ctrl.ObserveNativeQueued(g, T0 + 1, runs);
        }

        // Healthy queued bot stays queued at T0 + 10
        auto stay = ctrl.EvaluateLeaveQueue(true, runs, 101, NATIVE_LFG_STATE_QUEUED, false, false, false, false, T0 + 10);
        CHECK(!stay.shouldLeave);

        // P1-7: Queued >= 90s with no proposal -> stale_queue_requeue!
        auto stale = ctrl.EvaluateLeaveQueue(true, runs, 101, NATIVE_LFG_STATE_QUEUED, false, false, false, false, T0 + 95);
        CHECK(stale.shouldLeave && stale.isStaleRequeue && stale.reason == "stale_queue_requeue");
        auto staleEv = ctrl.OnQueueCancelled(101, stale.reason, T0 + 95, true);
        CHECK(staleEv.kind == LfgLifecycleEventKind::None);  // Does NOT cancel opportunity on stale requeue!
        // P2-2: Duplicate leave suppressed during leaveSentAt in-flight window!
        auto dupLeave = ctrl.EvaluateLeaveQueue(true, runs, 101, NATIVE_LFG_STATE_QUEUED, false, false, false, false, T0 + 97);
        CHECK(!dupLeave.shouldLeave);

        // P1-1: Past assemblyDeadline -> remaining bots leave cleanly!
        auto expLeave = ctrl.EvaluateLeaveQueue(true, runs, 102, NATIVE_LFG_STATE_QUEUED, false, false, false, false, T0 + 305);
        CHECK(expLeave.shouldLeave && expLeave.reason == "assembly_deadline_expired");

        // P1-1: One member released in roster -> other 4 bots see incomplete roster & leave cleanly!
        runs[0].roster[4].interestState = "released";
        auto incLeave = ctrl.EvaluateLeaveQueue(true, runs, 103, NATIVE_LFG_STATE_QUEUED, false, false, false, false, T0 + 20);
        CHECK(incLeave.shouldLeave && incLeave.reason == "roster_incomplete_after_member_release");

        // P2-7: ClearBot prunes tracking completely
        ctrl.ClearBot(103);
        CHECK(!ctrl.HasTrackedBot(103));
    }

    std::cout << "ALL_ISSUE20_CPP_TESTS_PASSED\n";
    return 0;
}
"""


class Issue20NativeRdfBridgeTests(unittest.TestCase):
    def test_native_cpp17_lfg_bridge(self) -> None:
        gpp = shutil.which("g++")
        if not gpp:
            self.skipTest("g++ not installed")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "test_lfg_bridge.cpp"
            bin_path = Path(tmp) / "test_lfg_bridge"
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
            self.assertIn("ALL_ISSUE20_CPP_TESTS_PASSED", run.stdout)

    def _make_confirmed_opp(self, coord: OpportunityCoordinator, dungeon_id: str, is_shadow: bool, t0: int) -> dict:
        party = [
            ("t1", 101, "tank"),
            ("h1", 102, "healer"),
            ("d1", 103, "damage"),
            ("d2", 104, "damage"),
            ("d3", 105, "damage"),
        ]
        opp = coord.create_opportunity(dungeon_id=dungeon_id, creator_guid=101, is_shadow=is_shadow, now_ts=t0)
        for slug, guid, role in party:
            self.assertTrue(
                coord.express_interest(
                    opportunity_id=opp["id"],
                    expected_revision=1,
                    persona_id=persona_id_for_slug(slug),
                    character_guid=guid,
                    character_slug=slug,
                    role=role,
                    reason_code="voluntary",
                    available_until=t0 + 3600,
                    now_ts=t0,
                )
            )
        res = coord.reserve_party(opp["id"], expected_revision=1, now_ts=t0 + 1)
        self.assertTrue(res["ok"])
        rev = res["revision"]
        for slug, guid, _ in party:
            c = coord.confirm_member(
                opp["id"],
                expected_revision=rev,
                persona_id=persona_id_for_slug(slug),
                character_guid=guid,
                accepted=True,
                now_ts=t0 + 5,
            )
            self.assertTrue(c["ok"])
        final = coord.get_opportunity(opp["id"])
        assert final is not None
        return final

    def test_python_mysql_publish_gates_and_authoritative_projection(self) -> None:
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp:
            coord = OpportunityCoordinator(Path(tmp) / "brain.db")

            # 1. Live OFF blocks publish fail-closed!
            opp_live = self._make_confirmed_opp(coord, "stockade_n", False, now)
            with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_ENABLED": "0", "HEARTHCORE_COOP_MODE": "off", "HEARTHCORE_COOP_DUNGEONS": "stockade_n"}):
                ok, reason, stmts = build_mysql_publish_statements(opp_live, now_ts=now + 10)
                self.assertFalse(ok)
                self.assertEqual(reason, "coop_disabled_or_draining")
                self.assertEqual(stmts, [])

            # 2. Shadow opp blocked even when coop enabled; live confirmed Stockade publishes transactionally
            #    with native_queue_id='' (P1-5) and correlation_key guard (P1-10)!
            opp_shadow = self._make_confirmed_opp(coord, "stockade_n", True, now + 20)
            with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_ENABLED": "1", "HEARTHCORE_COOP_MODE": "enabled", "HEARTHCORE_COOP_DUNGEONS": "stockade_n"}):
                ok, reason, _ = build_mysql_publish_statements(opp_shadow, now_ts=now + 25)
                self.assertFalse(ok)
                self.assertEqual(reason, "shadow_opportunity_blocked")

                ok, reason, stmts = build_mysql_publish_statements(opp_live, now_ts=now + 30)
                self.assertTrue(ok, reason)
                self.assertEqual(stmts[0], "START TRANSACTION;")
                self.assertEqual(stmts[-1], "COMMIT;")
                self.assertIn("o.correlation_key =", stmts[2])
                self.assertNotIn("lfgq:", stmts[1])  # P1-5: NEVER fabricate queue ID at publish time!

                # P2-4: Unmigrated DB (include_lfg_columns=False) MUST fail closed!
                ok_unmig, reason_unmig, _ = build_mysql_publish_statements(opp_live, now_ts=now + 30, include_lfg_columns=False)
                self.assertFalse(ok_unmig)
                self.assertEqual(reason_unmig, "lfg_columns_missing")

                batches: list[str] = []
                def _pub_sql(q: str):
                    batches.append(q)
                    if q.startswith("SELECT o.correlation_key"):
                        return [[opp_live["correlation_key"], "5"]]
                    return []
                pub = publish_confirmed_opportunity_to_mysql(coord, opp_live["id"], _pub_sql, now_ts=now + 30)
                self.assertTrue(pub["ok"])
                self.assertEqual(pub["lfg_dungeon_id"], 12)
                self.assertEqual(pub["native_queue_id"], "")
                self.assertEqual(len(batches), 2)
                self.assertIn("START TRANSACTION;", batches[0])
                self.assertIn("COMMIT;", batches[0])

                # P2-3: Readback detects when correlation guard blocked interest inserts!
                pub_blocked = publish_confirmed_opportunity_to_mysql(
                    coord, opp_live["id"],
                    lambda q: [["opp:foreign:r1", "0"]] if q.startswith("SELECT o.correlation_key") else [],
                    now_ts=now + 30,
                )
                self.assertFalse(pub_blocked["ok"])
                self.assertIn("publish_correlation_or_interest_mismatch", pub_blocked["reason"])

            # 3. Published row with native_queue_id='' does NOT prematurely flip SQLite confirmed -> active (P1-5)!
            #    AND P2-2: foreign correlation_key row with same ID is strictly ignored!
            oid = int(opp_live["id"])
            ckey = str(opp_live["correlation_key"])
            p_foreign = project_mysql_authority_to_sqlite(
                coord,
                [{
                    "id": oid, "is_shadow": 0, "state": "completed", "group_guid": 999,
                    "native_queue_id": f"lfgq:{oid}:12", "native_proposal_id": 99,
                    "native_run_id": "lfgr:999:1:34", "correlation_key": "opp:unrelated:foreign",
                    "outcome_reason": "boss_killed",
                }],
                now_ts=now + 34,
            )
            self.assertEqual(p_foreign["outcomes_applied"], 0)
            self.assertEqual(coord.get_opportunity(oid)["state"], "confirmed")
            p0 = project_mysql_authority_to_sqlite(
                coord,
                [{
                    "id": oid, "is_shadow": 0, "state": "assembling", "group_guid": 0,
                    "native_queue_id": "", "native_proposal_id": 0,
                    "native_run_id": "", "correlation_key": ckey, "outcome_reason": "published_for_native_rdf",
                }],
                now_ts=now + 35,
            )
            self.assertEqual(p0["handoffs_applied"], 0)
            cur = coord.get_opportunity(oid)
            assert cur is not None
            self.assertEqual(cur["state"], "confirmed")

            # 4. Once C++ confirms all 5 queued & proposal pops -> in_instance -> completed!
            p1 = project_mysql_authority_to_sqlite(
                coord,
                [{
                    "id": oid, "is_shadow": 0, "state": "assembling", "group_guid": 0,
                    "native_queue_id": f"lfgq:{oid}:12", "native_proposal_id": 77,
                    "native_run_id": "", "correlation_key": ckey, "outcome_reason": "",
                }],
                now_ts=now + 40,
            )
            self.assertEqual(p1["handoffs_applied"], 1)
            cur = coord.get_opportunity(oid)
            assert cur is not None
            self.assertEqual(cur["state"], "active")
            self.assertEqual(cur["native_state"], "proposal_sent")

            p2 = project_mysql_authority_to_sqlite(
                coord,
                [{
                    "id": oid, "is_shadow": 0, "state": "active", "group_guid": 700,
                    "native_queue_id": f"lfgq:{oid}:12", "native_proposal_id": 77,
                    "native_run_id": "lfgr:700:9001:34", "correlation_key": ckey, "outcome_reason": "",
                }],
                now_ts=now + 60,
            )
            self.assertEqual(p2["handoffs_applied"], 1)
            cur = coord.get_opportunity(oid)
            assert cur is not None
            self.assertEqual(cur["native_state"], "in_instance")
            self.assertEqual(cur["native_run_id"], "lfgr:700:9001:34")

            p3 = project_mysql_authority_to_sqlite(
                coord,
                [{
                    "id": oid, "is_shadow": 0, "state": "completed", "group_guid": 700,
                    "native_queue_id": f"lfgq:{oid}:12", "native_proposal_id": 77,
                    "native_run_id": "lfgr:700:9001:34", "correlation_key": ckey, "outcome_reason": "boss_killed",
                }],
                now_ts=now + 600,
            )
            self.assertEqual(p3["outcomes_applied"], 1)
            cur = coord.get_opportunity(oid)
            assert cur is not None
            self.assertEqual(cur["state"], "completed")
            self.assertTrue(all(m["state"] == "completed" for m in cur["members"]))

            # 5. P2-6: Active SQLite row whose authoritative MySQL row was cancelled after queue confirmation
            #    resolves cleanly as drained (NOT dangling/aborted)!
            opp_cancel = self._make_confirmed_opp(coord, "stockade_n", False, now + 700)
            oid2 = int(opp_cancel["id"])
            project_mysql_authority_to_sqlite(
                coord,
                [{
                    "id": oid2, "is_shadow": 0, "state": "assembling", "group_guid": 0,
                    "native_queue_id": f"lfgq:{oid2}:12", "native_proposal_id": 88,
                    "native_run_id": "", "correlation_key": opp_cancel["correlation_key"], "outcome_reason": "",
                }],
                now_ts=now + 720,
            )
            self.assertEqual(coord.get_opportunity(oid2)["state"], "active")
            res_c = project_mysql_authority_to_sqlite(
                coord,
                [{
                    "id": oid2, "is_shadow": 0, "state": "cancelled", "group_guid": 0,
                    "native_queue_id": f"lfgq:{oid2}:12", "native_proposal_id": 88,
                    "native_run_id": "", "correlation_key": opp_cancel["correlation_key"],
                    "outcome_reason": "combat_defer_timeout",
                }],
                now_ts=now + 750,
            )
            self.assertEqual(res_c["outcomes_applied"], 1)
            self.assertEqual(res_c["reconcile"]["aborted_missing_native"], 0)
            self.assertEqual(coord.get_opportunity(oid2)["state"], "drained")

    def test_runtime_bridge_run_cycle_publishes_and_projects_coop_opportunities(self) -> None:
        """P0-3: Verify real bridge.run_cycle(apply=True) publishes confirmed opps and projects MySQL outcomes!"""
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            db_path = tp / "brain.db"
            rt_dir = tp / "runtime"
            coord = OpportunityCoordinator(db_path)
            opp = self._make_confirmed_opp(coord, "stockade_n", False, now)
            oid = int(opp["id"])
            ckey = str(opp["correlation_key"])

            executed_sql: list[str] = []
            mysql_state: dict[str, list[list[str]]] = {"rows": []}

            def fake_sql(query: str):
                executed_sql.append(query)
                q = query.strip()
                if "information_schema.columns" in q:
                    return [["5"]]
                if q.startswith("SELECT id, state, group_guid, is_shadow"):
                    return list(mysql_state["rows"])
                if q.startswith("SELECT o.correlation_key"):
                    return [[ckey, "5"]] if mysql_state["rows"] else []
                if "START TRANSACTION;" in q and "INSERT INTO companion_opportunities" in q:
                    mysql_state["rows"] = [
                        [str(oid), "assembling", "0", "0", "published_for_native_rdf", "12", "", "0", "", ckey]
                    ]
                    return []
                return []

            empty_world = {"companions": {}, "observed_at": now, "owner_level": 35, "vault": {}, "orders": []}

            # Cycle 1 with coop OFF: MUST NOT publish to MySQL!
            with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_ENABLED": "0", "HEARTHCORE_COOP_MODE": "off", "HEARTHCORE_COOP_DUNGEONS": "stockade_n"}):
                out_off = run_cycle(
                    provider="fallback",
                    apply=True,
                    db_path=db_path,
                    runtime_dir=rt_dir,
                    world_ctx_override=empty_world,
                    sql_exec=fake_sql,
                    now_ts_override=now + 10,
                    remote_max_rev_override=0,
                )
            self.assertEqual(out_off["coop_sync"]["published"], 0)
            self.assertEqual(mysql_state["rows"], [])

            # Cycle 2 with coop ENABLED: publishes confirmed Stockade opp via real run_cycle!
            with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_ENABLED": "1", "HEARTHCORE_COOP_MODE": "enabled", "HEARTHCORE_COOP_DUNGEONS": "stockade_n"}):
                out_on = run_cycle(
                    provider="fallback",
                    apply=True,
                    db_path=db_path,
                    runtime_dir=rt_dir,
                    world_ctx_override=empty_world,
                    sql_exec=fake_sql,
                    now_ts_override=now + 20,
                    remote_max_rev_override=0,
                )
            self.assertTrue(out_on["coop_sync"]["ok"])
            self.assertEqual(out_on["coop_sync"]["published"], 1)

            # Cycle 3: MySQL advances to completed -> run_cycle projects outcome back into SQLite even when coop OFF!
            mysql_state["rows"] = [
                [str(oid), "completed", "700", "0", "boss_killed", "12", f"lfgq:{oid}:12", "77", "lfgr:700:9001:34", ckey]
            ]
            with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_ENABLED": "0", "HEARTHCORE_COOP_MODE": "off"}):
                out_proj = run_cycle(
                    provider="fallback",
                    apply=True,
                    db_path=db_path,
                    runtime_dir=rt_dir,
                    world_ctx_override=empty_world,
                    sql_exec=fake_sql,
                    now_ts_override=now + 400,
                    remote_max_rev_override=0,
                )
            self.assertEqual(out_proj["coop_sync"]["outcomes"], 1)
            self.assertEqual(coord.get_opportunity(oid)["state"], "completed")

    def test_native_wiring_and_coop_off_safety(self) -> None:
        playerbots_cpp = (SCRIPT_DIR / "Playerbots.cpp").read_text(encoding="utf-8")
        lfg_cpp = (ACTIONS_DIR / "LfgActions.cpp").read_text(encoding="utf-8")
        brain_cpp = (BOT_DIR / "CompanionBrain.cpp").read_text(encoding="utf-8")
        mgr_cpp = (BOT_DIR / "CompanionBotMgr.cpp").read_text(encoding="utf-8")
        sync_sh = (ROOT / "ops" / "sync-monorepo.sh").read_text(encoding="utf-8")
        audit_doc = (ROOT / "docs" / "PHASE_II_NATIVE_LFG_AUDIT.md").read_text(encoding="utf-8")
        mig_sql = (ROOT / "ops" / "sql" / "migrations" / "2026_11_27_issue20_native_lfg_columns.sql").read_text(encoding="utf-8")

        # P0-1/P0-3: No MariaDB-only syntax, no nonexistent LFG_LOCKSTATUS_NONE or dbc->Faction, no dangling isCoopCompanion!
        self.assertNotIn("ADD COLUMN IF NOT EXISTS", mig_sql)
        self.assertIn("PREPARE hc_stmt FROM @hc_sql", mig_sql)
        self.assertIn("missing Issue #20 native LFG columns", brain_cpp)
        self.assertNotIn("LFG_LOCKSTATUS_NONE", brain_cpp)
        self.assertNotIn("dbc->Faction", brain_cpp)
        self.assertNotIn("if (isCoopCompanion)\n    {\n        if (bot->GetGroup()", lfg_cpp)
        # P1-6 + P1 defence-in-depth: Mixed human + tracked/solo companion combos rejected in Playerbots.cpp
        self.assertIn("hasSoloCompanion", playerbots_cpp)

        # P0-2 & P1-9: CompanionBotMgr::TickBot has ZERO sLFGMgr calls or CMSG_LFG_LEAVE in groupedWithOwner!
        self.assertIn("CompanionBrain::OnCompanionJoinedOwnerGroup(", mgr_cpp)
        self.assertNotIn("CMSG_LFG_LEAVE", mgr_cpp)
        self.assertNotIn("CMSG_LFG_JOIN", mgr_cpp)
        self.assertIn("TickCoopLfgOnWorldThread()", brain_cpp)
        self.assertIn("SendCompanionLfgJoin(", brain_cpp)

        # P1-6: Mixed human + tracked coop bot combos rejected in Playerbots.cpp
        self.assertIn("CompanionBrain::IsCoopLfgTrackedBot(", playerbots_cpp)
        self.assertIn("CompanionBrain::AllowBotOnlyLfgProposal(", playerbots_cpp)

        # P1-3 & P1-4: RunEntered persisted after binds; ProposalFailed persisted; admission token released on cancel
        self.assertIn("ProposalFailed", brain_cpp)
        self.assertIn("runEnteredEvents", brain_cpp)
        self.assertIn("LFG_STATE_PROPOSAL", brain_cpp)

        # P2-3: No blanket ClearUnitState(UNIT_STATE_ALL_STATE) or botAI->Reset() on cooperative accept!
        accept_idx = lfg_cpp.index("handleCompanionProposal")
        accept_end = lfg_cpp.index("bool const isCoopCompanion", accept_idx)
        coop_accept_code = "\n".join(
            ln for ln in lfg_cpp[accept_idx:accept_end].splitlines() if not ln.lstrip().startswith("//")
        )
        self.assertNotIn("ClearUnitState(UNIT_STATE_ALL_STATE)", coop_accept_code)
        self.assertNotIn("botAI->Reset()", coop_accept_code)

        self.assertIn("src/Script/Playerbots.cpp", sync_sh)
        self.assertIn("OnPlayerbotCheckLFGQueue", audit_doc)


if __name__ == "__main__":
    unittest.main()
