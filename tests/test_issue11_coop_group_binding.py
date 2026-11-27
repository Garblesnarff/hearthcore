"""Issue #11: Native C++17 unit tests + wiring checks for exact cooperative group & roster binding."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "mod-playerbots-companions" / "src" / "Bot"
SCRIPT_DIR = ROOT / "mod-playerbots-companions" / "src" / "Script"
COOP_HDR = BOT_DIR / "CompanionCoopAuthLogic.h"
BRAIN_CPP = BOT_DIR / "CompanionBrain.cpp"
MGR_CPP = BOT_DIR / "CompanionBotMgr.cpp"
CMD_SCRIPT_CPP = SCRIPT_DIR / "PlayerbotCommandScript.cpp"


CPP_HARNESS = r"""
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#include "CompanionCoopAuthLogic.h"

using namespace CompanionCoopAuth;

// Side-effect-safe assertion macro (works identically even under -DNDEBUG).
#define CHECK(expr)                                                                                  \
    do                                                                                               \
    {                                                                                                \
        if (!(expr))                                                                                 \
        {                                                                                            \
            std::cerr << "CHECK FAILED at line " << __LINE__ << ": " << #expr << "\n";               \
            std::exit(1);                                                                            \
        }                                                                                            \
    } while (0)

static AuthorizedRun MakeRun(uint64_t oppId, uint32_t rev, uint32_t mapId, uint32_t groupId,
                             std::string state, uint32_t assemblyDeadline, uint32_t expiresAt,
                             std::vector<uint32_t> guids, bool shadow = false,
                             std::string rolesJson = R"({"tank":1,"healer":1,"damage":3})",
                             uint8_t difficulty = 0)
{
    AuthorizedRun r;
    r.opportunityId = oppId;
    r.revision = rev;
    r.targetMapId = mapId;
    r.difficulty = difficulty;
    r.expectedGroupId = groupId;
    r.boundToNativeGroup = (groupId != 0);
    r.runState = std::move(state);
    r.assemblyDeadline = assemblyDeadline;
    r.expiresAt = expiresAt;
    r.isShadow = shadow;
    CHECK(ParseRoleRequirements(rolesJson, r.requiredRoles));
    static const char* kRoles[] = {"tank", "healer", "damage", "damage", "damage"};
    for (size_t i = 0; i < guids.size(); ++i)
    {
        AuthorizedMember m;
        m.characterGuid = guids[i];
        m.role = kRoles[i < 5 ? i : 2];
        m.interestState = (r.runState == "active") ? "active" : "accepted";
        m.interestRevision = rev;
        m.enrolledCoop = true;
        r.roster.push_back(m);
    }
    return r;
}

static NativeGroupSnapshot MakeSnap(uint32_t groupId, uint64_t groupToken, uint32_t leaderGuid,
                                    std::vector<uint32_t> guids, bool isLfg = false, uint8_t diff = 0)
{
    NativeGroupSnapshot s;
    s.groupId = groupId;
    s.groupToken = groupToken;
    s.leaderGuid = leaderGuid;
    s.isLfg = isLfg;
    s.dungeonDifficulty = diff;
    for (uint32_t g : guids)
    {
        NativeGroupMember m;
        m.guid = g;
        m.online = true;
        m.isCompanion = true;
        m.isOwner = false;
        s.members.push_back(m);
    }
    return s;
}

int main()
{
    const uint32_t T0 = 10000;
    const std::vector<uint32_t> R1 = {101, 102, 103, 104, 105};
    const std::vector<uint32_t> R2 = {201, 202, 203, 204, 205};

    // 0. Strict roles_json parser verification.
    {
        RoleRequirements req;
        CHECK(ParseRoleRequirements(R"({"tank":1,"healer":1,"damage":3})", req));
        CHECK(req.tank == 1 && req.healer == 1 && req.damage == 3 && req.Total() == 5);
        CHECK(ParseRoleRequirements(R"( { "damage" : 2 , "tank" : 1 , "healer" : 1 } )", req));
        CHECK(req.Total() == 4);
        CHECK(!ParseRoleRequirements("", req));
        CHECK(!ParseRoleRequirements("{}", req));
        CHECK(!ParseRoleRequirements(R"({"tank":1,"healer":1,"damage":4})", req));  // > 5
        CHECK(!ParseRoleRequirements(R"({"tank":0,"healer":0,"damage":0})", req));  // == 0
        CHECK(!ParseRoleRequirements(R"({"tank":1,"unknown":1})", req));            // unknown key
    }

    // 1. Authorized bot in unrelated group is rejected; coop disabled fails closed.
    {
        Registry reg;
        reg.ReloadRuns({MakeRun(1, 2, 34, 500, "active", T0 + 180, T0 + 3600, R1)});
        auto unrelated = MakeSnap(999, 10, 101, {101, 301, 302});
        auto dOff = reg.Authorize(false, 101, 0, 0, 0, false, false, false, unrelated, T0);
        CHECK(!dOff.authorized && dOff.reason == "coop_disabled");

        auto dUnrel = reg.Authorize(true, 101, 0, 0, 0, false, false, false, unrelated, T0);
        CHECK(!dUnrel.authorized && !dUnrel.assemblyInProgress && dUnrel.reason == "group_id_mismatch");

        auto stray = MakeSnap(500, 11, 101, {101, 102, 103, 104, 999});
        auto dStray = reg.Authorize(true, 101, 0, 0, 0, false, false, false, stray, T0);
        CHECK(!dStray.authorized && dStray.reason == "unapproved_member");
    }

    // 2. Offline human or offline unapproved bot in MemberSlots CANNOT bypass guard!
    {
        Registry reg;
        reg.ReloadRuns({MakeRun(1, 2, 34, 500, "active", T0 + 180, T0 + 3600, R1)});
        auto snap = MakeSnap(500, 11, 101, {101, 102, 103, 104, 888});
        snap.members[4].online = false;
        snap.members[4].isCompanion = false;  // offline human
        auto dHuman = reg.Authorize(true, 101, 34, 77, 0, true, false, false, snap, T0);
        CHECK(!dHuman.authorized && dHuman.reason == "offline_non_companion_member");

        snap.members[4].isCompanion = true;   // offline unapproved companion bot
        auto dUnappBot = reg.Authorize(true, 101, 34, 77, 0, true, false, false, snap, T0);
        CHECK(!dUnappBot.authorized && dUnappBot.reason == "unapproved_member");
    }

    // 3. Reserved-only, partial DB roster (2 accepted + 3 reserved), bad role composition, shadow,
    //    unenrolled, revision mismatch, expired all fail closed!
    {
        auto snap = MakeSnap(500, 11, 101, R1);

        // Partial DB roster: roles_json requires 5, only 2 have accepted so far -> NEVER readyToBind!
        {
            Registry reg;
            auto partialDb = MakeRun(10, 1, 36, 0, "assembling", T0 + 180, T0 + 3600, {101, 102});
            reg.ReloadRuns({partialDb});
            auto twoSnap = MakeSnap(700, 20, 101, {101, 102});
            twoSnap.inviteeCount = 3;
            auto d = reg.Authorize(true, 101, 0, 0, 0, false, false, false, twoSnap, T0 + 30);
            CHECK(!d.authorized && !d.readyToBind && d.assemblyInProgress && d.reason == "assembly_in_progress");
            twoSnap.inviteeCount = 0;
            std::string why;
            CHECK(!reg.BindNativeGroup(10, 1, twoSnap, 0, T0 + 30, true, false, &why));
            CHECK(why == "roster_incomplete_for_roles");
        }
        // Wrong role composition (2 tanks, 0 healers, 3 damage vs {"tank":1,"healer":1,"damage":3})
        {
            Registry reg;
            auto badComp = MakeRun(10, 1, 36, 0, "assembling", T0 + 180, T0 + 3600, R1);
            badComp.roster[1].role = "tank";  // 2 tanks, 0 healers
            reg.ReloadRuns({badComp});
            auto fullSnap = MakeSnap(700, 20, 101, R1);
            auto d = reg.Authorize(true, 101, 0, 0, 0, false, false, false, fullSnap, T0 + 30);
            CHECK(!d.authorized && !d.readyToBind && d.reason == "role_composition_mismatch");
        }
        // Reserved-only row MUST NEVER authorize
        {
            Registry reg;
            auto run = MakeRun(1, 1, 34, 500, "active", T0 + 180, T0 + 3600, R1);
            run.roster[0].interestState = "reserved";
            reg.ReloadRuns({run});
            auto d = reg.Authorize(true, 101, 0, 0, 0, false, false, false, snap, T0);
            CHECK(!d.authorized && d.reason == "reserved_only");
            auto d2 = reg.Authorize(true, 102, 0, 0, 0, false, false, false, snap, T0);
            CHECK(!d2.authorized && d2.reason == "roster_entry_invalid");
        }
        // Shadow run rejected
        {
            Registry reg;
            reg.ReloadRuns({MakeRun(1, 1, 34, 500, "active", T0 + 180, T0 + 3600, R1, /*shadow=*/true)});
            CHECK(reg.Authorize(true, 101, 0, 0, 0, false, false, false, snap, T0).reason == "shadow_run");
        }
        // Unenrolled persona rejected
        {
            Registry reg;
            auto run = MakeRun(1, 1, 34, 500, "active", T0 + 180, T0 + 3600, R1);
            run.roster[0].enrolledCoop = false;
            reg.ReloadRuns({run});
            CHECK(reg.Authorize(true, 101, 0, 0, 0, false, false, false, snap, T0).reason == "not_enrolled");
        }
        // Revision mismatch rejected
        {
            Registry reg;
            auto run = MakeRun(1, 3, 34, 500, "active", T0 + 180, T0 + 3600, R1);
            run.roster[0].interestRevision = 2;
            reg.ReloadRuns({run});
            CHECK(reg.Authorize(true, 101, 0, 0, 0, false, false, false, snap, T0).reason == "revision_mismatch");
        }
        // Expired rejected
        {
            Registry reg;
            reg.ReloadRuns({MakeRun(1, 1, 34, 500, "active", T0 + 180, T0 - 1, R1)});
            CHECK(reg.Authorize(true, 101, 0, 0, 0, false, false, false, snap, T0).reason == "expired");
        }
    }

    // 4. Bounded assembly -> pre-made bind -> explicit native LFG group rebind -> difficulty/map/instance check!
    {
        Registry reg;
        reg.ReloadRuns({MakeRun(10, 1, 36, 0, "assembling", T0 + 180, T0 + 3600, R1, false,
                                R"({"tank":1,"healer":1,"damage":3})", /*difficulty=*/1)});

        // 3/5 joined, 2 pending invites (outside dungeon): assemblyInProgress=true, NO coop control yet.
        auto partial = MakeSnap(700, 100, 101, {101, 102, 103}, /*isLfg=*/false, 1);
        partial.inviteeCount = 2;
        auto dPart = reg.Authorize(true, 101, 0, 0, 0, false, false, false, partial, T0 + 60);
        CHECK(!dPart.authorized && dPart.assemblyInProgress && !dPart.readyToBind && dPart.reason == "assembly_in_progress");

        // Extra invitee (3 + 3 > 5) fails closed
        partial.inviteeCount = 3;
        CHECK(reg.Authorize(true, 101, 0, 0, 0, false, false, false, partial, T0 + 60).reason == "invitee_pending_unverified");
        partial.inviteeCount = 2;

        // Early dungeon entry before bind fails closed
        CHECK(reg.Authorize(true, 101, 36, 91, 1, true, false, false, partial, T0 + 60).reason == "dungeon_entry_before_bind");

        // Past assemblyDeadline -> timeout
        auto dTimeout = reg.Authorize(true, 101, 0, 0, 0, false, false, false, partial, T0 + 181);
        CHECK(!dTimeout.authorized && !dTimeout.assemblyInProgress && dTimeout.reason == "assembly_timeout");

        // Full 5/5 pre-made party -> readyToBind -> BindNativeGroup
        auto premade = MakeSnap(700, 100, 101, R1, /*isLfg=*/false, 1);
        auto dReady = reg.Authorize(true, 101, 0, 0, 0, false, false, false, premade, T0 + 90);
        CHECK(!dReady.authorized && dReady.readyToBind && !dReady.isLfgRebind && dReady.reason == "ready_to_bind");

        uint64_t const genBeforeBind = reg.BindGeneration();
        std::string bindReason;
        bool const bound1 = reg.BindNativeGroup(10, 1, premade, 0, T0 + 90, true, false, &bindReason);
        CHECK(bound1 && bindReason == "bound");
        CHECK(reg.BindGeneration() > genBeforeBind);

        // Stale DB reload captured BEFORE the bind MUST NOT wipe the fresh bind!
        bool const staleApplied = reg.ReloadRuns({MakeRun(10, 1, 36, 0, "assembling", T0 + 180, T0 + 3600, R1)}, genBeforeBind);
        CHECK(!staleApplied);
        CHECK(reg.Runs()[0].boundToNativeGroup && reg.Runs()[0].expectedGroupId == 700);

        // Now RDF pops: pre-made group 700 dissolves FIRST (firing GroupScript::OnDisband), transitioning
        // opp #10 back to bounded 'assembling' (60s rebind grace), and native LFG group 705 (token 101) forms!
        std::vector<Registry::DisbandTransition> premadeDisband;
        CHECK(reg.OnGroupDisbanded(700, 100, T0 + 95, &premadeDisband));
        CHECK(premadeDisband.size() == 1 && premadeDisband[0].opportunityId == 10 && !premadeDisband[0].aborted);
        CHECK(reg.Runs()[0].runState == "assembling" && !reg.Runs()[0].boundToNativeGroup);

        auto lfgSnap = MakeSnap(705, 101, 101, R1, /*isLfg=*/true, 1);
        auto dRebind = reg.Authorize(true, 101, 0, 0, 0, false, false, false, lfgSnap, T0 + 100);
        CHECK(!dRebind.authorized && dRebind.readyToBind && dRebind.reason == "ready_to_bind");

        bool const boundLfg = reg.BindNativeGroup(10, 1, lfgSnap, 0, T0 + 100, true, /*allowLfgRebind=*/true, &bindReason);
        CHECK(boundLfg && bindReason == "bound");
        CHECK(reg.Runs()[0].isLfgBound && reg.Runs()[0].expectedGroupId == 705);

        // Once isLfgBound == true, a second group ID change is REJECTED (group_id_mismatch)!
        auto lfgSnap2 = MakeSnap(706, 102, 101, R1, /*isLfg=*/true, 1);
        CHECK(reg.Authorize(true, 101, 0, 0, 0, false, false, false, lfgSnap2, T0 + 105).reason == "group_id_mismatch");

        // Enter target Heroic (1) dungeon map 36, pin instance 91; Normal (0) difficulty rejected!
        bool const pinned = reg.PinRuntimeBindings(10, 1, 705, 101, 101, false, true, 36, 91);
        CHECK(pinned);
        CHECK(reg.Authorize(true, 101, 36, 91, 1, true, false, false, lfgSnap, T0 + 120).authorized);
        CHECK(reg.Authorize(true, 101, 36, 91, 0, true, false, false, lfgSnap, T0 + 120).reason == "difficulty_mismatch");
        CHECK(reg.Authorize(true, 101, 34, 91, 1, true, false, false, lfgSnap, T0 + 120).reason == "map_mismatch");
        CHECK(reg.Authorize(true, 101, 36, 92, 1, true, false, false, lfgSnap, T0 + 120).reason == "instance_mismatch");

        // When the LFG/in-dungeon group 705 disbands, opp #10 transitions to terminal 'aborted' and frees
        // all 5 bots from _memberToRunIdx (zero zombie lockout!).
        std::vector<Registry::DisbandTransition> lfgDisband;
        CHECK(reg.OnGroupDisbanded(705, 101, T0 + 500, &lfgDisband));
        CHECK(lfgDisband.size() == 1 && lfgDisband[0].opportunityId == 10 && lfgDisband[0].aborted);
        CHECK(reg.Runs()[0].runState == "aborted");
        CHECK(reg.Authorize(true, 101, 0, 0, 0, false, false, false, lfgSnap, T0 + 501).reason == "not_authorized_member");
    }

    // 5. Owner-led groups ALWAYS take precedence (online or offline owner); raids/BGs/BFs/6+ forbidden!
    {
        Registry reg;
        reg.ReloadRuns({MakeRun(1, 1, 34, 500, "active", T0 + 180, T0 + 3600, R1)});
        auto snap = MakeSnap(500, 11, 101, R1);

        auto withOwner = snap;
        withOwner.members[4].guid = 1;
        withOwner.members[4].isOwner = true;
        withOwner.members[4].isCompanion = false;
        CHECK(reg.Authorize(true, 101, 0, 0, 0, false, false, false, withOwner, T0).reason == "owner_group_precedence");
        withOwner.members[4].online = false;
        CHECK(reg.Authorize(true, 101, 0, 0, 0, false, false, false, withOwner, T0).reason == "owner_group_precedence");

        auto raidSnap = snap;
        raidSnap.isRaid = true;
        CHECK(reg.Authorize(true, 101, 34, 1, 0, true, false, false, raidSnap, T0).reason == "raid_forbidden");
        CHECK(reg.Authorize(true, 101, 409, 1, 0, false, true, false, snap, T0).reason == "raid_forbidden");

        auto bgSnap = snap;
        bgSnap.isBg = true;
        CHECK(reg.Authorize(true, 101, 0, 0, 0, false, false, false, bgSnap, T0).reason == "bg_or_bf_forbidden");
        CHECK(reg.Authorize(true, 101, 489, 1, 0, false, false, true, snap, T0).reason == "bg_or_bf_forbidden");

        auto bigSnap = snap;
        bigSnap.members.push_back(NativeGroupMember{106, true, true, false});
        CHECK(reg.Authorize(true, 101, 0, 0, 0, false, false, false, bigSnap, T0).reason == "group_too_large");
    }

    // 6. Reconnect + leader failover re-pin, real group-ID reuse + GroupScript OnDisband cleanup,
    //    mixed opportunity, mid-run membership changes, and ambiguous multi-run membership.
    {
        Registry reg;
        auto run1 = MakeRun(1, 1, 34, 500, "active", T0 + 180, T0 + 3600, R1);
        run1.expectedGroupToken = 41;
        run1.expectedLeaderGuid = 101;
        auto run2 = MakeRun(2, 1, 36, 0, "assembling", T0 + 180, T0 + 3600, R2);
        reg.ReloadRuns({run1, run2});

        auto snap = MakeSnap(500, 41, 101, R1);
        CHECK(reg.Authorize(true, 101, 34, 77, 0, true, false, false, snap, T0).authorized);

        // 6a. DPS disconnect & reconnect stays authorized.
        snap.members[3].online = false;
        CHECK(reg.Authorize(true, 101, 34, 77, 0, true, false, false, snap, T0 + 5).authorized);
        snap.members[3].online = true;
        CHECK(reg.Authorize(true, 101, 34, 77, 0, true, false, false, snap, T0 + 10).authorized);

        // 6b. Online leader hijack while 101 is ONLINE -> rejected (leader_mismatch)!
        snap.leaderGuid = 102;
        CHECK(reg.Authorize(true, 102, 34, 77, 0, true, false, false, snap, T0 + 11).reason == "leader_mismatch");

        // 6c. Real leader disconnect failover: 101 goes offline -> AC rotates leader to 102 -> PinRuntimeBindings
        //     re-pins expectedLeaderGuid=102 -> when 101 RECONNECTS while 102 still leads, group STAYS AUTHORIZED!
        snap.members[0].online = false;
        snap.leaderGuid = 102;
        auto dFailover = reg.Authorize(true, 102, 34, 77, 0, true, false, false, snap, T0 + 12);
        CHECK(dFailover.authorized && dFailover.leaderFailover);
        bool const repinned = reg.PinRuntimeBindings(1, 1, 500, 41, 102, dFailover.leaderFailover, false, 34, 77);
        CHECK(repinned);
        snap.members[0].online = true;  // 101 reconnects, 102 still leads -> MUST NOT DISBAND!
        auto dAfterReconnect = reg.Authorize(true, 101, 34, 77, 0, true, false, false, snap, T0 + 14);
        CHECK(dAfterReconnect.authorized && !dAfterReconnect.leaderFailover);

        // 6d. Real Group ID reuse via monotonic GroupScript token: counter 500 reused by a NEW Group (token 42)!
        auto reused = snap;
        reused.groupToken = 42;
        CHECK(reg.Authorize(true, 101, 34, 77, 0, true, false, false, reused, T0 + 15).reason == "group_id_reused");

        // 6e. GroupScript::OnDisband clears run1's binding (aborted since instance 77 was pinned) so freed
        //     groupId 500 (new token 42) can be bound by run2 AND run1's members are NOT trapped as zombies!
        std::vector<Registry::DisbandTransition> clearedOpps;
        bool const disbanded = reg.OnGroupDisbanded(500, 41, T0 + 15, &clearedOpps);
        CHECK(disbanded && clearedOpps.size() == 1 && clearedOpps[0].opportunityId == 1 && clearedOpps[0].aborted);
        CHECK(!reg.Runs()[0].boundToNativeGroup && reg.Runs()[0].expectedGroupId == 0 && reg.Runs()[0].runState == "aborted");

        auto run2Reuses500 = MakeSnap(500, 42, 201, R2);
        std::string why;
        bool const bound2 = reg.BindNativeGroup(2, 1, run2Reuses500, 0, T0 + 16, true, false, &why);
        CHECK(bound2 && why == "bound");
        CHECK(reg.Authorize(true, 201, 36, 88, 0, true, false, false, run2Reuses500, T0 + 17).authorized);

        // 6f. Mixed opportunity (run2 vs active run3), mid-run member leave, pending invitee, ambiguous runs.
        Registry regMix;
        auto mix1 = MakeRun(1, 1, 34, 500, "active", T0 + 180, T0 + 3600, R1);
        mix1.expectedGroupToken = 41;
        auto mix2 = MakeRun(2, 1, 36, 501, "active", T0 + 180, T0 + 3600, R2);
        mix2.expectedGroupToken = 42;
        regMix.ReloadRuns({mix1, mix2});
        auto mixed = MakeSnap(501, 42, 201, R2);
        mixed.members[4].guid = 105;  // belongs to active run1
        CHECK(regMix.Authorize(true, 201, 36, 88, 0, true, false, false, mixed, T0 + 20).reason == "mixed_opportunity_member");

        auto leftSnap = run2Reuses500;
        leftSnap.members.pop_back();
        CHECK(reg.Authorize(true, 201, 36, 88, 0, true, false, false, leftSnap, T0 + 25).reason == "roster_incomplete_for_active");

        auto invSnap = run2Reuses500;
        invSnap.inviteeCount = 1;
        CHECK(reg.Authorize(true, 201, 36, 88, 0, true, false, false, invSnap, T0 + 26).reason == "invitee_pending_unverified");

        Registry ambig;
        ambig.ReloadRuns({
            MakeRun(1, 1, 34, 500, "active", T0 + 180, T0 + 3600, R1),
            MakeRun(2, 1, 36, 501, "active", T0 + 180, T0 + 3600, {101, 202, 203, 204, 205}),
        });
        CHECK(ambig.Authorize(true, 101, 0, 0, 0, false, false, false, snap, T0).reason == "ambiguous_multiple_runs");
        uint64_t o = 0;
        uint32_t m = 0, g = 0;
        CHECK(!ambig.LookupBoundMember(101, T0, o, m, g));
    }

    std::cout << "ALL_ISSUE11_CPP_TESTS_PASSED\n";
    return 0;
}
"""


class Issue11CoopGroupBindingTests(unittest.TestCase):
    def test_native_cpp17_coop_auth_engine(self) -> None:
        gpp = shutil.which("g++")
        if not gpp:
            self.skipTest("g++ not installed")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "test_coop_auth.cpp"
            bin_path = Path(tmp) / "test_coop_auth"
            src.write_text(CPP_HARNESS, encoding="utf-8")
            # Compile with -DNDEBUG too so we prove zero side-effect assertions!
            comp = subprocess.run(
                [gpp, "-std=c++17", "-DNDEBUG", "-Wall", "-Wextra", "-Werror", f"-I{BOT_DIR}", str(src), "-o", str(bin_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(comp.returncode, 0, f"g++ compile failed:\n{comp.stderr}")
            run = subprocess.run([str(bin_path)], capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 0, f"binary failed:\n{run.stderr}\n{run.stdout}")
            self.assertIn("ALL_ISSUE11_CPP_TESTS_PASSED", run.stdout)

    def test_wiring_invariants_in_brain_botmgr_and_groupscript(self) -> None:
        brain = BRAIN_CPP.read_text(encoding="utf-8")
        mgr = MGR_CPP.read_text(encoding="utf-8")
        script = CMD_SCRIPT_CPP.read_text(encoding="utf-8")

        # 1. Coop flag remains safe default OFF.
        self.assertIn('GetOption<bool>("AiPlayerbot.Companion.Coop.Enabled", false, false)', brain)

        # 2. ReloadCoopMemberships NEVER selects 'reserved', filters expiry in SQL, limits by opportunities,
        #    loads roles_json, and uses generation guard so stale reloads never wipe fresh binds.
        reload_idx = brain.index("void CompanionBrain::ReloadCoopMemberships()")
        reload_end = brain.index("bool CompanionBrain::IsAuthorizedCoopMember", reload_idx)
        reload_body = brain[reload_idx:reload_end]
        reload_code = "\n".join(line for line in reload_body.splitlines() if not line.lstrip().startswith("//"))
        self.assertNotIn("'reserved'", reload_code)
        self.assertIn("i.state IN ('accepted', 'active')", reload_code)
        self.assertIn("i.opportunity_revision = o.revision", reload_code)
        self.assertIn("o.roles_json", reload_code)
        self.assertIn("LIMIT 50", reload_code)
        self.assertIn("ParseRoleRequirements", reload_code)
        self.assertIn("ReloadRuns(std::move(nextRuns), startGen)", reload_code)

        # 3. AuthorizeCoopGroup performs ZERO DB calls on map threads; world-thread FlushCoopGroupMutations
        #    does the single-statement atomic guarded UPDATE + expiry-filtered NOT EXISTS conflict check.
        auth_idx = brain.index("bool CompanionBrain::AuthorizeCoopGroup(")
        auth_end = brain.index("float CompanionBrain::Weight(", auth_idx)
        auth_body = brain[auth_idx:auth_end]
        self.assertNotIn("CharacterDatabase.", auth_body)
        self.assertIn("gPendingCoopBinds[dec.opportunityId]", auth_body)

        flush_idx = brain.index("void CompanionBrain::FlushCoopGroupMutations()")
        flush_end = brain.index("void CompanionBrain::ReloadCoopMemberships()", flush_idx)
        flush_body = brain[flush_idx:flush_end]
        self.assertIn("AND NOT EXISTS", flush_body)
        self.assertIn("recruit_until + estimated_minutes * 60", flush_body)
        self.assertIn("sGroupMgr->GetGroupByGUID(req.snap.groupId)", flush_body)
        self.assertIn("rolled back DB", flush_body)
        self.assertIn("gCoopRegistry.BindNativeGroup", flush_body)
        self.assertIn("FlushCoopGroupMutations();", brain[brain.index("void CompanionBrain::Update("):brain.index("bool CompanionBrain::IsLevelingGuideMat")])

        # 4. CompanionBotMgr inspects ALL MemberSlots (online AND offline), avoids cross-map-thread player reads,
        #    passes GroupTokenFor + GetDungeonDifficulty, keeps IsInAuthorizedCoopGroup pure read-only!
        snap_idx = mgr.index("BuildCoopGroupSnapshot(")
        snap_end = mgr.index("bool CompanionBotMgr::IsInAuthorizedCoopGroup(", snap_idx)
        snap_body = mgr[snap_idx:snap_end]
        self.assertIn("group->GetMemberSlots()", snap_body)
        self.assertNotIn("GetFirstMember", snap_body)
        self.assertNotIn("GetMapId", snap_body)
        self.assertIn("CompanionBrain::GroupTokenFor(group)", snap_body)
        self.assertIn("group->GetDungeonDifficulty()", snap_body)
        self.assertIn("AuthorizeCoopGroup(bot, snap, /*allowQueueBind=*/false)", mgr)
        self.assertIn("bot->GetMap()->IsNonRaidDungeon() && IsInAuthorizedCoopGroup(bot)", mgr)

        # 5. GroupScript hooks OnCreate + OnDisband to track monotonic group tokens & clear freed group_guid!
        self.assertIn("class CompanionCoopGroupScript : public GroupScript", script)
        self.assertIn("CompanionBrain::OnNativeGroupCreated(group)", script)
        self.assertIn("CompanionBrain::OnNativeGroupDisbanded(group)", script)
        self.assertIn("new CompanionCoopGroupScript();", script)


if __name__ == "__main__":
    unittest.main()
