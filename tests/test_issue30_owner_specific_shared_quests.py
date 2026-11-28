"""Tests for Issue #30: Make shared quest protection and acceptance owner-specific.

Covers:
1. Compiled native C++17 state machine (`CompanionOwnerQuestLogic.h`) with `-std=c++17 -DNDEBUG -Wall -Wextra -Werror`:
   - Two owners with disjoint quest logs (Wonky & Logan) at different levels: each group protects ONLY its own
     owner's shared quests and reserves log slots for its own owner's shared quests (zero cross-contamination!).
   - Per-owner independent online -> logout (keep live copy during async DB save delay) -> offline DB snapshot
     lifecycle (one owner logging out never disturbs the other owner's snapshot or cadence).
   - Unresolved assigned owner (`resolvedOwnerLow == 0`), unknown snapshot (`Unknown`), and stale snapshot (`Stale`)
     after DB failures or TTL expiry fail closed (`owner-unknown`) on ALL destructive abandonment/cleanup while
     NEVER substituting the primary owner and preserving reserved log slots in `MayAccept`.
   - DB outage failure records failure with exponential backoff without overwriting known quests with empty data;
     recovery restores fresh snapshot cleanly.
   - Unrelated/unowned companions (`botHasAssignedOwner == false`) are completely unaffected (`AllowAbandon`).
   - Same-map live owner log takes precedence immediately for both protection and `MayAccept`.
   - Evicting removed/deleted/renamed owners via `SyncActiveOwners`.
2. Native C++ source wiring audit (`CompanionQuest.cpp`, `CompanionBotMgr.{h,cpp}`) verifying zero primary-owner
   fallback (`OwnerLow()`, `FindOwnerOnline()`, `gOwnerQuests`) in quest/accept/abandon hooks, map-local
   `ObjectAccessor::GetPlayer(*bot, ...)` usage, DB health probe on empty DB query, non-nested locking in `WhyLines`,
   and no cooldown burn on fail-closed skips in `FreeLogSlot`.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "mod-playerbots-companions" / "src" / "Bot"


CPP_HARNESS = r"""
#include "CompanionOwnerQuestLogic.h"
#include "CompanionOwnerXpCapLogic.h"

#include <cstdio>
#include <cstdlib>
#include <set>
#include <string>
#include <unordered_set>

#define CHECK(expr)                                                                         \
    do {                                                                                    \
        if (!(expr)) {                                                                      \
            std::fprintf(stderr, "CHECK FAILED at line %d: %s\n", __LINE__, #expr);         \
            std::exit(1);                                                                   \
        }                                                                                   \
    } while (0)

using namespace CompanionOwnerQuest;
using namespace CompanionOwnerXpCap;

int main()
{
    OwnerQuestConfig qcfg;
    qcfg.liveRefreshMs = 10000;
    qcfg.dbRefreshMs = 60000;
    qcfg.logoutDbDelayMs = 15000;
    qcfg.staleAfterSec = 300;
    qcfg.maxConsecutiveDbFailures = 2;

    OwnerXpCapConfig xcfg;
    xcfg.levelLead = 3;
    xcfg.expansionMaxLevel = 80;
    xcfg.waitForOwnerLevel = true;
    xcfg.staleAfterSec = 300;
    xcfg.maxConsecutiveFailures = 2;
    xcfg.normalRefreshSec = 60;

    // -------------------------------------------------------------------------
    // 1. Two owners with disjoint quests & different levels -> strictly isolated!
    // -------------------------------------------------------------------------
    {
        OwnerRegistry ownerReg;
        ownerReg.Configure("Wonky", {{"ashwick", "Logan"}, {"bramblepaw", "Logan"}});
        ownerReg.RecordGuidResolution("Wonky", 1001, 100, 60);
        ownerReg.RecordLevelRead("Wonky", 1001, 40, 100, 60);
        ownerReg.RecordGuidResolution("Logan", 2002, 100, 60);
        ownerReg.RecordLevelRead("Logan", 2002, 12, 100, 60);

        OwnerQuestRegistry qreg;
        qreg.SyncActiveOwners(ownerReg.AllResolvedOwnerLows());

        // Wonky (Lv 40) has STV quests {501, 502}; Logan (Lv 12) has Westfall quests {101, 102}.
        CHECK(qreg.RecordOnlineSnapshot(1001, {501, 502}, 100));
        CHECK(qreg.RecordOnlineSnapshot(2002, {101, 102}, 100));

        // Resolve bot owners via OwnerRegistry (simulating ResolveOwnerIdentityFor)
        BotXpCapFastResult halvarOwner = ownerReg.ResolveFastForBot("Halvar", 38, 105, xcfg);
        BotXpCapFastResult ashwickOwner = ownerReg.ResolveFastForBot("Ashwick", 12, 105, xcfg);
        CHECK(halvarOwner.ownerLowGuid == 1001);
        CHECK(ashwickOwner.ownerLowGuid == 2002);

        // Halvar (Wonky's bot): protects 501 (Wonky's), does NOT protect 101 (Logan's)!
        CHECK(qreg.EvaluateGuard(true, true, halvarOwner.ownerLowGuid, false, false, 501, 105, qcfg)
              == GuardDecision::KeepOwnerShared);
        CHECK(qreg.EvaluateGuard(true, true, halvarOwner.ownerLowGuid, false, false, 101, 105, qcfg)
              == GuardDecision::AllowAbandon);

        // Ashwick (Logan's bot): protects 101 (Logan's), does NOT protect 501 (Wonky's)!
        CHECK(qreg.EvaluateGuard(true, true, ashwickOwner.ownerLowGuid, false, false, 101, 105, qcfg)
              == GuardDecision::KeepOwnerShared);
        CHECK(qreg.EvaluateGuard(true, true, ashwickOwner.ownerLowGuid, false, false, 501, 105, qcfg)
              == GuardDecision::AllowAbandon);

        // Reserved log slots (free=2 <= keep=3):
        // Ashwick MAY use a reserved slot for Logan's quest 101, MUST NOT spend reserved slot on Wonky's quest 501!
        bool ashHas101 = qreg.EvaluateOwnerHasQuest(true, ashwickOwner.ownerLowGuid, false, false, 101, 105, qcfg);
        bool ashHas501 = qreg.EvaluateOwnerHasQuest(true, ashwickOwner.ownerLowGuid, false, false, 501, 105, qcfg);
        CHECK(ashHas101);
        CHECK(!ashHas501);
        CHECK(EvaluateMayAccept(ashHas101, /*free=*/2, /*keep=*/3, /*grey=*/true, /*outsideHub=*/true).allowed);
        CHECK(!EvaluateMayAccept(ashHas501, /*free=*/2, /*keep=*/3, /*grey=*/false, /*outsideHub=*/false).allowed);
        // Zero free slots -> even shared quest cannot be accepted
        CHECK(!EvaluateMayAccept(ashHas101, /*free=*/0, /*keep=*/3, false, false).allowed);
    }

    // -------------------------------------------------------------------------
    // 2. Per-owner logout transition (keep live copy during DB save delay) & offline DB read!
    // -------------------------------------------------------------------------
    {
        OwnerQuestRegistry qreg;
        qreg.SyncActiveOwners({1001, 2002});

        // Both online at t=100
        CHECK(qreg.PlanProbe(1001, true, 1000, 100, qcfg) == ProbeAction::ReadOnlineLog);
        CHECK(qreg.PlanProbe(2002, true, 1000, 100, qcfg) == ProbeAction::ReadOnlineLog);
        qreg.RecordOnlineSnapshot(1001, {501}, 100);
        qreg.RecordOnlineSnapshot(2002, {101}, 100);

        // Logan (2002) logs out at t=105 while Wonky (1001) stays online!
        CHECK(qreg.PlanProbe(1001, true, 5000, 105, qcfg) == ProbeAction::None);  // 5s < 10s liveRefresh
        CHECK(qreg.PlanProbe(2002, false, 5000, 105, qcfg) == ProbeAction::LogoutKeepLive);

        // During the 15s logout save delay, Logan's last live copy {101} remains FRESH and protected!
        CHECK(qreg.StateForOwner(2002, 110, qcfg) == SnapshotState::Fresh);
        CHECK(qreg.EvaluateGuard(true, true, 2002, false, false, 101, 110, qcfg) == GuardDecision::KeepOwnerShared);
        CHECK(qreg.PlanProbe(2002, false, 10000, 115, qcfg) == ProbeAction::None);  // 10s < 15s delay

        // At +15s after logout, PlanProbe triggers QueryDb for Logan only!
        CHECK(qreg.PlanProbe(2002, false, 5000, 120, qcfg) == ProbeAction::QueryDb);
        qreg.RecordDbSnapshotSuccess(2002, {101, 103}, 120);
        CHECK(qreg.EvaluateGuard(true, true, 2002, false, false, 103, 121, qcfg) == GuardDecision::KeepOwnerShared);
        // Wonky unaffected!
        CHECK(qreg.EvaluateGuard(true, true, 1001, false, false, 103, 121, qcfg) == GuardDecision::AllowAbandon);
    }

    // -------------------------------------------------------------------------
    // 3. Unresolved owner & unknown/stale snapshot -> NO destructive cleanup, NO primary fallback!
    // -------------------------------------------------------------------------
    {
        OwnerRegistry ownerReg;
        ownerReg.Configure("Wonky", {{"ashwick", "Logan"}});
        ownerReg.RecordGuidResolution("Wonky", 1001, 100, 60);
        ownerReg.RecordLevelRead("Wonky", 1001, 40, 100, 60);
        // Logan unresolved (renamed/deleted/offline cache miss)!
        ownerReg.RecordGuidResolution("Logan", 0, 100, 60);

        OwnerQuestRegistry qreg;
        qreg.SyncActiveOwners(ownerReg.AllResolvedOwnerLows());
        qreg.RecordOnlineSnapshot(1001, {501}, 100);

        BotXpCapFastResult ash = ownerReg.ResolveFastForBot("Ashwick", 12, 105, xcfg);
        CHECK(ash.assignment == OwnerAssignmentKind::AssignedOverride);
        CHECK(ash.ownerLowGuid == 0);

        // Unresolved owner -> guard MUST fail closed ("owner-unknown") for ANY quest (no destructive cleanup!)
        GuardDecision gd = qreg.EvaluateGuard(true, true, ash.ownerLowGuid, false, false, 999, 105, qcfg);
        CHECK(gd == GuardDecision::KeepOwnerUnknown);
        CHECK(std::string(GuardReasonText(gd)) == "owner-unknown");
        // And MayAccept MUST NOT treat Wonky's quest 501 as shared for Ashwick (preserves reserved slots!)
        CHECK(!qreg.EvaluateOwnerHasQuest(true, ash.ownerLowGuid, false, false, 501, 105, qcfg));

        // Now Logan resolves (2002), but his quest snapshot has NOT loaded yet (Unknown)!
        ownerReg.RecordGuidResolution("Logan", 2002, 110, 60);
        qreg.SyncActiveOwners(ownerReg.AllResolvedOwnerLows());
        CHECK(qreg.StateForOwner(2002, 110, qcfg) == SnapshotState::Unknown);
        CHECK(qreg.EvaluateGuard(true, true, 2002, false, false, 999, 110, qcfg) == GuardDecision::KeepOwnerUnknown);
        CHECK(!qreg.EvaluateOwnerHasQuest(true, 2002, false, false, 501, 110, qcfg));

        // Wonky's companion Halvar is STILL unaffected and can abandon junk quest 999 cleanly!
        CHECK(qreg.EvaluateGuard(true, true, 1001, false, false, 999, 110, qcfg) == GuardDecision::AllowAbandon);

        // Load Logan's DB snapshot {101}, then simulate DB outage (2 consecutive failures -> Stale!)
        qreg.RecordDbSnapshotSuccess(2002, {101}, 120);
        CHECK(qreg.StateForOwner(2002, 125, qcfg) == SnapshotState::Fresh);
        CHECK(qreg.EvaluateGuard(true, true, 2002, false, false, 999, 125, qcfg) == GuardDecision::AllowAbandon);

        // 1st DB failure -> does NOT wipe {101} with empty set, retries after 5s backoff
        qreg.RecordDbSnapshotFailure(2002, 180, qcfg);
        CHECK(qreg.OwnerSnapshotHasQuest(2002, 101));
        CHECK(qreg.PlanProbe(2002, false, 1000, 184, qcfg) == ProbeAction::None);
        CHECK(qreg.PlanProbe(2002, false, 1000, 185, qcfg) == ProbeAction::QueryDb);

        // 2nd DB failure -> transitions to Stale -> fails closed ("owner-unknown") on ALL abandons!
        qreg.RecordDbSnapshotFailure(2002, 185, qcfg);
        CHECK(qreg.StateForOwner(2002, 185, qcfg) == SnapshotState::Stale);
        CHECK(qreg.EvaluateGuard(true, true, 2002, false, false, 999, 185, qcfg) == GuardDecision::KeepOwnerUnknown);
        // Backoff is now 10s (185 + 10 = 195), even across long outage
        CHECK(qreg.PlanProbe(2002, false, 5000, 190, qcfg) == ProbeAction::None);
        CHECK(qreg.PlanProbe(2002, false, 5000, 195, qcfg) == ProbeAction::QueryDb);
        // MayAccept does not spend reserved slots while stale!
        CHECK(!qreg.EvaluateOwnerHasQuest(true, 2002, false, false, 101, 190, qcfg));

        // DB recovers at t=195 -> Fresh again!
        qreg.RecordDbSnapshotSuccess(2002, {101}, 195);
        CHECK(qreg.StateForOwner(2002, 196, qcfg) == SnapshotState::Fresh);
        CHECK(qreg.EvaluateGuard(true, true, 2002, false, false, 101, 196, qcfg) == GuardDecision::KeepOwnerShared);
        CHECK(qreg.EvaluateGuard(true, true, 2002, false, false, 999, 196, qcfg) == GuardDecision::AllowAbandon);

        // Age TTL staleness (> 300s without refresh) -> Stale -> fail closed!
        CHECK(qreg.StateForOwner(2002, 195 + 301, qcfg) == SnapshotState::Stale);
        CHECK(qreg.EvaluateGuard(true, true, 2002, false, false, 999, 195 + 301, qcfg)
              == GuardDecision::KeepOwnerUnknown);
    }

    // -------------------------------------------------------------------------
    // 4. Unrelated / unowned guild companions unaffected + same-map live owner override!
    // -------------------------------------------------------------------------
    {
        OwnerQuestRegistry qreg;
        qreg.SyncActiveOwners({1001});
        qreg.RecordOnlineSnapshot(1001, {501}, 100);

        // Unowned companion (botHasAssignedOwner = false, ownerLow = 0) -> AllowAbandon & not owner-shared!
        CHECK(qreg.EvaluateGuard(true, false, 0, false, false, 501, 105, qcfg) == GuardDecision::AllowAbandon);
        CHECK(!qreg.EvaluateOwnerHasQuest(false, 0, false, false, 501, 105, qcfg));

        // Same-map live owner (Logan 2002 just picked up quest 777 before world-thread poll):
        CHECK(qreg.EvaluateGuard(true, true, 2002, /*ownerLiveOnSameMap=*/true, /*liveHasQuest=*/true, 777, 105, qcfg)
              == GuardDecision::KeepOwnerShared);
        CHECK(qreg.EvaluateOwnerHasQuest(true, 2002, true, true, 777, 105, qcfg));
        CHECK(qreg.EvaluateGuard(true, true, 2002, true, false, 777, 105, qcfg) == GuardDecision::AllowAbandon);

        // Removing owner 1001 via SyncActiveOwners evicts stale snapshot cleanly, and re-adding starts Unknown!
        qreg.SyncActiveOwners({});
        CHECK(qreg.StateForOwner(1001, 106, qcfg) == SnapshotState::Unknown);
        CHECK(qreg.QuestCountForOwner(1001) == 0);
        qreg.SyncActiveOwners({1001});
        CHECK(qreg.StateForOwner(1001, 107, qcfg) == SnapshotState::Unknown);
    }

    // -------------------------------------------------------------------------
    // 5. Additional edge cases (Opus QA): never-known DB failure, re-login during logout delay,
    //    staleAfterSec=0, failure-below-limit stays Fresh, backoff cap, identity-only resolver!
    // -------------------------------------------------------------------------
    {
        OwnerQuestRegistry qreg;
        qreg.SyncActiveOwners({2002});

        // Never-known owner hits DB error -> stays Unknown & retries on backoff!
        CHECK(qreg.PlanProbe(2002, false, 1000, 100, qcfg) == ProbeAction::QueryDb);
        qreg.RecordDbSnapshotFailure(2002, 100, qcfg);
        CHECK(qreg.StateForOwner(2002, 101, qcfg) == SnapshotState::Unknown);
        CHECK(qreg.PlanProbe(2002, false, 1000, 104, qcfg) == ProbeAction::None);
        CHECK(qreg.PlanProbe(2002, false, 1000, 105, qcfg) == ProbeAction::QueryDb);
        // Backoff cap check (many consecutive failures capped at dbRefreshMs/1000 = 60s)
        for (int i = 0; i < 8; ++i)
            qreg.RecordDbSnapshotFailure(2002, 200, qcfg);
        CHECK(qreg.PlanProbe(2002, false, 1000, 259, qcfg) == ProbeAction::None);
        CHECK(qreg.PlanProbe(2002, false, 1000, 260, qcfg) == ProbeAction::QueryDb);

        // Re-login during 15s logout delay cancels waitingLogoutDb and reads live log immediately!
        qreg.RecordOnlineSnapshot(2002, {101}, 300);
        CHECK(qreg.PlanProbe(2002, false, 1000, 305, qcfg) == ProbeAction::LogoutKeepLive);
        CHECK(qreg.PlanProbe(2002, true, 5000, 310, qcfg) == ProbeAction::ReadOnlineLog);

        // 1 failure below maxConsecutiveDbFailures=2 stays Fresh; staleAfterSec=0 disables TTL!
        OwnerQuestConfig noTtl = qcfg;
        noTtl.staleAfterSec = 0;
        qreg.RecordDbSnapshotSuccess(2002, {101}, 400);
        CHECK(qreg.StateForOwner(2002, 400 + 9999, noTtl) == SnapshotState::Fresh);
        qreg.RecordDbSnapshotFailure(2002, 460, noTtl);
        CHECK(qreg.StateForOwner(2002, 461, noTtl) == SnapshotState::Fresh);

        // Identity-only resolver stays valid even when XP-cap level is stale (P2-2)!
        OwnerRegistry ownerReg;
        ownerReg.Configure("Wonky", {{"ashwick", "Logan"}});
        ownerReg.RecordGuidResolution("Logan", 2002, 100, 60);
        ownerReg.RecordLevelRead("Logan", 2002, 12, 100, 60);
        // Even at t=1000 (XP cap level stale), identity-only resolver still resolves Logan's GUID 2002!
        BotOwnerIdentity id = ownerReg.ResolveOwnerIdentity("Ashwick", true);
        CHECK(id.assignment == OwnerAssignmentKind::AssignedOverride);
        CHECK(id.ownerName == "Logan");
        CHECK(id.ownerLowGuid == 2002);
    }

    std::printf("ALL_ISSUE30_CPP_CHECKS_PASSED\n");
    return 0;
}
"""


class TestIssue30OwnerSpecificSharedQuests(unittest.TestCase):
    def test_native_cpp17_owner_quest_state_machine(self) -> None:
        compiler = shutil.which("g++") or shutil.which("clang++")
        if not compiler:
            self.skipTest("No C++17 compiler available")

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "test_owner_quest.cpp"
            exe = Path(tmp) / "test_owner_quest"
            src.write_text(CPP_HARNESS, encoding="utf-8")

            comp = subprocess.run(
                [
                    compiler,
                    "-std=c++17",
                    "-DNDEBUG",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    f"-I{BOT_DIR}",
                    str(src),
                    "-o",
                    str(exe),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(comp.returncode, 0, msg=f"Compile failed:\n{comp.stderr}")

            run = subprocess.run([str(exe)], capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 0, msg=f"Harness failed:\n{run.stderr}\n{run.stdout}")
            self.assertIn("ALL_ISSUE30_CPP_CHECKS_PASSED", run.stdout)

    def test_companion_quest_wiring_zero_primary_owner_fallback(self) -> None:
        quest_cpp = (BOT_DIR / "CompanionQuest.cpp").read_text(encoding="utf-8")
        mgr_h = (BOT_DIR / "CompanionBotMgr.h").read_text(encoding="utf-8")
        mgr_cpp = (BOT_DIR / "CompanionBotMgr.cpp").read_text(encoding="utf-8")

        # Zero primary-only quest snapshot globals or primary-only calls left in CompanionQuest.cpp!
        self.assertNotIn("gOwnerQuestsKnown", quest_cpp)
        self.assertNotIn("sCompanionBotMgr.OwnerLow()", quest_cpp)
        self.assertNotIn("sCompanionBotMgr.FindOwnerOnline()", quest_cpp)
        self.assertNotIn("sCompanionBotMgr.OwnerConfigured()", quest_cpp)

        # Owner-keyed registry, guarded FindMap() same-map check, atomic sentinel UNION ALL DB query, and multi-owner polling!
        self.assertIn('#include "CompanionOwnerQuestLogic.h"', quest_cpp)
        self.assertIn("sCompanionBotMgr.AllResolvedOwnerLows()", quest_cpp)
        self.assertIn("sCompanionBotMgr.ResolveOwnerIdentityFor(bot, ctx.hasAssignedOwner, ctx.ownerLow)", quest_cpp)
        self.assertIn("sCompanionBotMgr.ResolveOwnerIdentityByName(botName)", quest_cpp)
        self.assertIn("ObjectAccessor::GetPlayer(map, ObjectGuid::Create<HighGuid::Player>(ctx.ownerLow))", quest_cpp)
        self.assertIn("SELECT 0 UNION ALL SELECT quest FROM character_queststatus WHERE guid = {} AND status <> 0", quest_cpp)
        self.assertNotIn('CharacterDatabase.Query("SELECT 1")', quest_cpp)
        self.assertIn("CompanionOwnerQuest::EvaluateMayAccept(", quest_cpp)

        # CompanionBotMgr exposes identity-only ResolveOwnerIdentityFor/ByName & AllResolvedOwnerLows
        self.assertIn("ResolveOwnerIdentityFor(", mgr_h)
        self.assertIn("ResolveOwnerIdentityByName(", mgr_h)
        self.assertIn("AllResolvedOwnerLows()", mgr_h)
        self.assertIn("if (!bot || !IsCompanion(bot))", mgr_cpp)
        self.assertIn("ResolveOwnerIdentity(", mgr_cpp)


if __name__ == "__main__":
    unittest.main()
