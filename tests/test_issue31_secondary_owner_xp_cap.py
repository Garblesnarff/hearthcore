"""Tests for Issue #31: Prevent secondary-owner level & identity fallback from applying wrong XP cap.

Covers:
1. Compiled native C++17 state machine (`CompanionOwnerXpCapLogic.h`) with `-std=c++17 -DNDEBUG -Wall -Wextra -Werror`:
   - Distinguishes no override (`None`/`PrimaryDefault`), resolved assigned owner (`AssignedOverride`),
     unresolved assignment (`UnresolvedAssignment`), unread level (`LevelUnknown`), and stale level (`StaleLevel`).
   - Secondary owner offline / cache miss / unread / stale NEVER falls back to primary owner GUID or level.
   - Fails closed on applicable XP cap while unknown/unresolved/stale (including StaleLevel when waitForOwnerLevel=false).
   - Two owners at different levels enforce independent caps concurrently.
   - Owner rename / deletion / same-name recreation with new GUID / config reassignment & removal.
   - Recovery from unresolved/stale -> fresh without accidental XP grants.
   - Per-owner exponential retry backoff (`OwnersDueForProbe`), safe staleness clamping (`ClampStaleSeconds`),
     and early pre-staleness refresh at `staleAfterSec / 2`.
   - Expansion cap (`CONFIG_MAX_PLAYER_LEVEL`) clamping and enforcement for both owned & unconfigured bots.
   - Allocation-free `ResolveFastForBot` equivalence and JSON escaping safety.
2. Native C++ source wiring audit (`CompanionBotMgr.{h,cpp}`, `Playerbots.cpp`, `PlayerbotCommandScript.cpp`).
3. End-to-end `live_positions.json` round-trip (`fetch_world_context`), offline `playerbots.conf` per-bot
   owner overrides, shift-group binding conflict isolation, Issue #23 `fail_closed` mode, and Issue #24
   `plan_companion_gear_ambitions` / `resolve_bracket` cap & fail-closed enforcement.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import hearthcore.bridge as bridge_mod
from hearthcore.bridge import (
    _xp_cap_prof_catchup_state,
    authoritative_cap_params_for_companion,
    discover_owner_bindings,
    fetch_world_context,
    load_per_bot_owner_overrides,
)
from hearthcore.gear_ambition import (
    CatalogItem,
    ItemSource,
    plan_character_gear_ambitions,
    plan_companion_gear_ambitions,
    resolve_bracket,
    resolve_horizon_brackets,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "mod-playerbots-companions" / "src" / "Bot"
SCRIPT_DIR = REPO_ROOT / "mod-playerbots-companions" / "src" / "Script"


CPP_HARNESS = r"""
#include "CompanionOwnerXpCapLogic.h"

#include <cstdio>
#include <cstdlib>
#include <string>
#include <unordered_map>
#include <vector>

#define CHECK(expr)                                                                         \
    do {                                                                                    \
        if (!(expr)) {                                                                      \
            std::fprintf(stderr, "CHECK FAILED at line %d: %s\n", __LINE__, #expr);         \
            std::exit(1);                                                                   \
        }                                                                                   \
    } while (0)

using namespace CompanionOwnerXpCap;

int main()
{
    OwnerXpCapConfig cfg;
    cfg.levelLead = 3;
    cfg.expansionMaxLevel = 80;
    cfg.waitForOwnerLevel = true;
    cfg.staleAfterSec = 300;
    cfg.maxConsecutiveFailures = 2;
    cfg.normalRefreshSec = 60;

    // -------------------------------------------------------------------------
    // 1. Secondary owner cache miss / offline unresolved NEVER falls back to primary!
    // -------------------------------------------------------------------------
    {
        OwnerRegistry reg;
        reg.Configure("Wonky", {{"ashwick", "Logan"}, {"bramblepaw", "Logan"}});

        reg.RecordGuidResolution("Wonky", 1001, 100, 60);
        reg.RecordLevelRead("Wonky", 1001, 35, 100, 60);

        // Secondary owner Logan is NOT resolved yet (cache miss / offline).
        reg.RecordGuidResolution("Logan", 0, 100, 60);

        // At t=105, Logan is due for 5s retry, Wonky is NOT due yet (nextProbe=160)!
        CHECK(reg.AnyOwnerDueForProbe(105, cfg));
        std::vector<std::string> due105 = reg.OwnersDueForProbe(105, cfg, false);
        CHECK(due105.size() == 1 && due105[0] == "Logan");

        // Ordinary guild bot (no override) -> PrimaryDefault -> Wonky (Lv 35, cap 38).
        BotXpCapResolution halvar = reg.ResolveForBot("Halvar", 34, 105, cfg);
        BotXpCapFastResult halvarFast = reg.ResolveFastForBot("Halvar", 34, 105, cfg);
        CHECK(halvar.assignment == OwnerAssignmentKind::PrimaryDefault);
        CHECK(halvar.state == OwnerResolutionState::ResolvedFresh);
        CHECK(halvar.ownerName == "Wonky");
        CHECK(halvar.ownerLowGuid == 1001 && halvarFast.ownerLowGuid == 1001);
        CHECK(halvar.ownerLevel == 35 && halvarFast.ownerLevel == 35);
        CHECK(halvar.capLevel == 38 && halvarFast.capLevel == 38);
        CHECK(!halvar.xpCapped && !halvarFast.xpCapped);
        CHECK(!halvar.failClosed);

        BotXpCapResolution halvarAtCap = reg.ResolveForBot("Halvar", 38, 105, cfg);
        CHECK(halvarAtCap.xpCapped);
        CHECK(!halvarAtCap.failClosed);

        // Secondary-owner bot Ashwick (Lv 12) MUST NEVER inherit Wonky's GUID (1001) or level (35)!
        BotXpCapResolution ashwick = reg.ResolveForBot("Ashwick", 12, 105, cfg);
        CHECK(ashwick.assignment == OwnerAssignmentKind::AssignedOverride);
        CHECK(ashwick.state == OwnerResolutionState::UnresolvedAssignment);
        CHECK(ashwick.ownerName == "Logan");
        CHECK(ashwick.ownerLowGuid == 0);
        CHECK(ashwick.ownerLevel == 0);
        CHECK(ashwick.capLevel == 0);
        CHECK(ashwick.xpCapped);
        CHECK(ashwick.failClosed);

        // Even if waitForOwnerLevel=false, explicit AssignedOverride with unresolved owner STILL fails closed!
        OwnerXpCapConfig noWait = cfg;
        noWait.waitForOwnerLevel = false;
        BotXpCapResolution ashwickNoWait = reg.ResolveForBot("Ashwick", 12, 105, noWait);
        CHECK(ashwickNoWait.state == OwnerResolutionState::UnresolvedAssignment);
        CHECK(ashwickNoWait.ownerLowGuid == 0);
        CHECK(ashwickNoWait.ownerLevel == 0);
        CHECK(ashwickNoWait.xpCapped);
        CHECK(ashwickNoWait.failClosed);
    }

    // -------------------------------------------------------------------------
    // 2. Secondary owner GUID resolved but level unread (LevelUnknown) -> fail closed, no fallback!
    // -------------------------------------------------------------------------
    {
        OwnerRegistry reg;
        reg.Configure("Wonky", {{"ashwick", "Logan"}});
        reg.RecordGuidResolution("Wonky", 1001, 100, 60);
        reg.RecordLevelRead("Wonky", 1001, 40, 100, 60);

        reg.RecordGuidResolution("Logan", 2002, 100, 60);
        reg.RecordLevelRead("Logan", 2002, 0, 100, 60);

        BotXpCapResolution ashwick = reg.ResolveForBot("Ashwick", 15, 105, cfg);
        CHECK(ashwick.assignment == OwnerAssignmentKind::AssignedOverride);
        CHECK(ashwick.state == OwnerResolutionState::LevelUnknown);
        CHECK(ashwick.ownerLowGuid == 2002);
        CHECK(ashwick.ownerLevel == 0);
        CHECK(ashwick.capLevel == 0);
        CHECK(ashwick.xpCapped);
        CHECK(ashwick.failClosed);
        CHECK(reg.AnyOwnerDueForProbe(105, cfg));

        // Now Logan's offline DB level (12) is read -> cap becomes 15 cleanly!
        reg.RecordLevelRead("Logan", 2002, 12, 110, 60);
        CHECK(!reg.AnyOwnerDueForProbe(115, cfg));

        BotXpCapResolution ashwick15 = reg.ResolveForBot("Ashwick", 15, 110, cfg);
        CHECK(ashwick15.state == OwnerResolutionState::ResolvedFresh);
        CHECK(ashwick15.ownerLowGuid == 2002);
        CHECK(ashwick15.ownerLevel == 12);
        CHECK(ashwick15.capLevel == 15);
        CHECK(ashwick15.xpCapped);
        CHECK(!ashwick15.failClosed);

        BotXpCapResolution ashwick14 = reg.ResolveForBot("Ashwick", 14, 110, cfg);
        CHECK(!ashwick14.xpCapped);
        CHECK(ashwick14.capLevel == 15);

        // Wonky's ordinary guild bot still uses Wonky Lv 40 -> cap 43.
        BotXpCapResolution halvar = reg.ResolveForBot("Halvar", 42, 110, cfg);
        CHECK(halvar.ownerLowGuid == 1001);
        CHECK(halvar.ownerLevel == 40);
        CHECK(halvar.capLevel == 43);
        CHECK(!halvar.xpCapped);
    }

    // -------------------------------------------------------------------------
    // 3. Stale level detection (consecutive DB failures + TTL) AND P1-4 waitForOwnerLevel=false!
    // -------------------------------------------------------------------------
    {
        OwnerRegistry reg;
        reg.Configure("Wonky", {{"ashwick", "Logan"}});
        reg.RecordGuidResolution("Wonky", 1001, 100, 60);
        reg.RecordLevelRead("Wonky", 1001, 40, 100, 60);
        reg.RecordGuidResolution("Logan", 2002, 100, 60);
        reg.RecordLevelRead("Logan", 2002, 12, 100, 60);

        // 1st transient DB read failure -> stays fresh (schedules 5s retry) until failure limit (2).
        reg.RecordLevelRead("Logan", 2002, 0, 160, 60);
        CHECK(reg.AnyOwnerDueForProbe(165, cfg));
        BotXpCapResolution afterOneFail = reg.ResolveForBot("Ashwick", 14, 160, cfg);
        CHECK(afterOneFail.state == OwnerResolutionState::ResolvedFresh);
        CHECK(!afterOneFail.xpCapped);

        // 2nd consecutive DB failure -> trips StaleLevel immediately and fails closed!
        reg.RecordLevelRead("Logan", 2002, 0, 165, 60);
        BotXpCapResolution afterTwoFails = reg.ResolveForBot("Ashwick", 14, 165, cfg);
        CHECK(afterTwoFails.state == OwnerResolutionState::StaleLevel);
        CHECK(afterTwoFails.ownerLowGuid == 2002);
        CHECK(afterTwoFails.ownerLevel == 0);
        CHECK(afterTwoFails.capLevel == 0);
        CHECK(afterTwoFails.xpCapped);
        CHECK(afterTwoFails.failClosed);

        // P2-1: Exponential backoff on failing owner (2nd failure => +10s => 175)
        reg.RecordLevelRead("Wonky", 1001, 40, 165, 60);  // Refresh Wonky -> nextProbe=225
        CHECK(!reg.AnyOwnerDueForProbe(170, cfg));        // Logan nextProbe=175, Wonky=225 -> neither due!
        CHECK(reg.AnyOwnerDueForProbe(175, cfg));         // Logan due at 175!
        std::vector<std::string> due175 = reg.OwnersDueForProbe(175, cfg, false);
        CHECK(due175.size() == 1 && due175[0] == "Logan");

        // B-1: During a long DB outage (age >= staleAfterSec/2 = 150s), retry backoff MUST NOT be canceled!
        reg.RecordLevelRead("Wonky", 1001, 40, 260, 60);  // Wonky healthy (nextProbe=320)
        // Simulate 4th consecutive failure for Logan at t=260 (160s after lastSuccess=100 >= 150s): backoff = 40s -> nextProbe=300
        reg.RecordLevelRead("Logan", 2002, 0, 220, 60);
        reg.RecordLevelRead("Logan", 2002, 0, 260, 60);
        CHECK(!reg.AnyOwnerDueForProbe(270, cfg));        // Logan nextProbe=300, Wonky=320 -> NOT due every 5s!
        CHECK(reg.OwnersDueForProbe(270, cfg, false).empty());
        CHECK(reg.AnyOwnerDueForProbe(300, cfg));         // Logan due right at backoff target 300!

        // Recovery via successful DB read restores fresh level cleanly without accidental grant at Lv 15.
        reg.RecordLevelRead("Logan", 2002, 12, 300, 60);
        BotXpCapResolution recovered15 = reg.ResolveForBot("Ashwick", 15, 300, cfg);
        CHECK(recovered15.state == OwnerResolutionState::ResolvedFresh);
        CHECK(recovered15.xpCapped);
        BotXpCapResolution recovered14 = reg.ResolveForBot("Ashwick", 14, 300, cfg);
        CHECK(!recovered14.xpCapped);

        // P1-4: PrimaryDefault + StaleLevel + waitForOwnerLevel=false MUST STILL fail closed (no regression!)
        OwnerXpCapConfig noWait = cfg;
        noWait.waitForOwnerLevel = false;
        BotXpCapResolution primaryStaleNoWait = reg.ResolveForBot("Halvar", 42, 260 + 301, noWait);
        CHECK(primaryStaleNoWait.assignment == OwnerAssignmentKind::PrimaryDefault);
        CHECK(primaryStaleNoWait.state == OwnerResolutionState::StaleLevel);
        CHECK(primaryStaleNoWait.ownerLevel == 0);
        CHECK(primaryStaleNoWait.xpCapped);
        CHECK(primaryStaleNoWait.failClosed);
        CHECK(reg.PrimaryOwnerLevel(260 + 301, cfg) == 0);

        // B-4: Boot-time LevelUnknown with waitForOwnerLevel=false fails closed once consecutiveFailures >= limit!
        OwnerRegistry regBoot;
        regBoot.Configure("Wonky", {});
        regBoot.RecordGuidResolution("Wonky", 1001, 10, 60);
        regBoot.RecordLevelRead("Wonky", 1001, 0, 10, 60);
        regBoot.RecordLevelRead("Wonky", 1001, 0, 15, 60);
        BotXpCapResolution bootFailClosed = regBoot.ResolveForBot("Halvar", 10, 15, noWait);
        CHECK(bootFailClosed.state == OwnerResolutionState::LevelUnknown);
        CHECK(bootFailClosed.failClosed && bootFailClosed.xpCapped);
    }

    // -------------------------------------------------------------------------
    // 4. V6: ClampStaleSeconds & early pre-staleness probe at staleAfterSec / 2
    // -------------------------------------------------------------------------
    {
        CHECK(ClampStaleSeconds(0, 60) == 0);
        CHECK(ClampStaleSeconds(30, 60) == 120);   // clamped >= 2 * normalRefreshSec
        CHECK(ClampStaleSeconds(300, 60) == 300);

        OwnerRegistry reg;
        reg.Configure("Wonky", {});
        reg.RecordGuidResolution("Wonky", 1001, 100, 300);
        reg.RecordLevelRead("Wonky", 1001, 40, 100, 300);  // nextProbe = 400, staleAfter = 300
        CHECK(!reg.AnyOwnerDueForProbe(249, cfg));         // age = 149 < 150
        CHECK(reg.AnyOwnerDueForProbe(250, cfg));          // age = 150 >= staleAfter/2 -> early refresh!
    }

    // -------------------------------------------------------------------------
    // 5. Owner rename / deletion & same-name recreation with new GUID -> zero accidental XP grant!
    // -------------------------------------------------------------------------
    {
        OwnerRegistry reg;
        reg.Configure("Wonky", {{"ashwick", "Logan"}});
        reg.RecordGuidResolution("Wonky", 1001, 100, 60);
        reg.RecordLevelRead("Wonky", 1001, 40, 100, 60);
        reg.RecordGuidResolution("Logan", 2002, 100, 60);
        reg.RecordLevelRead("Logan", 2002, 25, 100, 60);
        CHECK(reg.IsAnyResolvedOwnerLow(2002));

        // Logan is renamed or deleted -> GUID lookup returns 0 -> IMMEDIATE UnresolvedAssignment!
        reg.RecordGuidResolution("Logan", 0, 105, 60);
        CHECK(!reg.IsAnyResolvedOwnerLow(2002));
        BotXpCapResolution deleted = reg.ResolveForBot("Ashwick", 10, 105, cfg);
        CHECK(deleted.state == OwnerResolutionState::UnresolvedAssignment);
        CHECK(deleted.ownerLowGuid == 0);
        CHECK(deleted.ownerLevel == 0);
        CHECK(deleted.xpCapped);
        CHECK(deleted.failClosed);

        // Brand-new Lv 1 character created with name "Logan" (new GUID 9999).
        reg.RecordGuidResolution("Logan", 2002, 110, 60);
        reg.RecordLevelRead("Logan", 2002, 25, 110, 60);
        reg.RecordGuidResolution("Logan", 9999, 111, 60);
        BotXpCapResolution recreatedUnread = reg.ResolveForBot("Ashwick", 10, 111, cfg);
        CHECK(recreatedUnread.state == OwnerResolutionState::LevelUnknown);
        CHECK(recreatedUnread.ownerLowGuid == 9999);
        CHECK(recreatedUnread.ownerLevel == 0);
        CHECK(recreatedUnread.xpCapped);
        CHECK(recreatedUnread.failClosed);

        reg.RecordLevelRead("Logan", 9999, 1, 112, 60);
        BotXpCapResolution recreatedRead = reg.ResolveForBot("Ashwick", 10, 112, cfg);
        CHECK(recreatedRead.state == OwnerResolutionState::ResolvedFresh);
        CHECK(recreatedRead.ownerLevel == 1);
        CHECK(recreatedRead.capLevel == 4);
        CHECK(recreatedRead.xpCapped);
    }

    // -------------------------------------------------------------------------
    // 6. Config change: reassign owner, remove override, clear primary!
    // -------------------------------------------------------------------------
    {
        OwnerRegistry reg;
        reg.Configure("Wonky", {{"ashwick", "Logan"}});
        reg.RecordGuidResolution("Wonky", 1001, 100, 60);
        reg.RecordLevelRead("Wonky", 1001, 40, 100, 60);
        reg.RecordGuidResolution("Logan", 2002, 100, 60);
        reg.RecordLevelRead("Logan", 2002, 12, 100, 60);

        reg.Configure("Wonky", {{"ashwick", "Alex"}});
        CHECK(!reg.IsAnyResolvedOwnerLow(2002));
        CHECK(reg.IsAnyResolvedOwnerLow(1001));

        BotXpCapResolution reassigned = reg.ResolveForBot("Ashwick", 10, 105, cfg);
        CHECK(reassigned.assignment == OwnerAssignmentKind::AssignedOverride);
        CHECK(reassigned.state == OwnerResolutionState::UnresolvedAssignment);
        CHECK(reassigned.ownerName == "Alex");
        CHECK(reassigned.ownerLowGuid == 0);
        CHECK(reassigned.xpCapped);
        CHECK(reassigned.failClosed);

        reg.RecordGuidResolution("Alex", 3003, 110, 60);
        reg.RecordLevelRead("Alex", 3003, 20, 110, 60);
        BotXpCapResolution alexOk = reg.ResolveForBot("Ashwick", 22, 110, cfg);
        CHECK(alexOk.state == OwnerResolutionState::ResolvedFresh);
        CHECK(alexOk.ownerLowGuid == 3003);
        CHECK(alexOk.capLevel == 23);
        CHECK(!alexOk.xpCapped);

        reg.Configure("Wonky", {});
        CHECK(!reg.IsAnyResolvedOwnerLow(3003));
        BotXpCapResolution reverted = reg.ResolveForBot("Ashwick", 42, 115, cfg);
        CHECK(reverted.assignment == OwnerAssignmentKind::PrimaryDefault);
        CHECK(reverted.ownerName == "Wonky");
        CHECK(reverted.capLevel == 43);
        CHECK(!reverted.xpCapped);

        reg.Configure("", {{"ashwick", "Logan"}});
        BotXpCapResolution ordinaryNoPrimary = reg.ResolveForBot("Halvar", 45, 120, cfg);
        CHECK(ordinaryNoPrimary.assignment == OwnerAssignmentKind::None);
        CHECK(ordinaryNoPrimary.state == OwnerResolutionState::Unconfigured);
        CHECK(ordinaryNoPrimary.capLevel == 80);
        CHECK(!ordinaryNoPrimary.xpCapped);
        CHECK(!ordinaryNoPrimary.failClosed);

        BotXpCapResolution ordinaryMax = reg.ResolveForBot("Halvar", 80, 120, cfg);
        CHECK(ordinaryMax.xpCapped);
        CHECK(ordinaryMax.expansionCapped);

        BotXpCapResolution secondaryNoPrimary = reg.ResolveForBot("Ashwick", 10, 120, cfg);
        CHECK(secondaryNoPrimary.assignment == OwnerAssignmentKind::AssignedOverride);
        CHECK(secondaryNoPrimary.state == OwnerResolutionState::UnresolvedAssignment);
        CHECK(secondaryNoPrimary.xpCapped);
        CHECK(secondaryNoPrimary.failClosed);
    }

    // -------------------------------------------------------------------------
    // 7. Expansion cap clamping & JSON escaping safety
    // -------------------------------------------------------------------------
    {
        OwnerRegistry reg;
        reg.Configure("Wonky", {});
        reg.RecordGuidResolution("Wonky", 1001, 100, 60);
        reg.RecordLevelRead("Wonky", 1001, 79, 100, 60);

        BotXpCapResolution clamped80 = reg.ResolveForBot("Halvar", 79, 105, cfg);
        CHECK(clamped80.capLevel == 80 && !clamped80.xpCapped);
        BotXpCapResolution at80 = reg.ResolveForBot("Halvar", 80, 105, cfg);
        CHECK(at80.capLevel == 80 && at80.xpCapped && at80.expansionCapped);

        OwnerXpCapConfig vanillaCfg = cfg;
        vanillaCfg.expansionMaxLevel = 60;
        reg.RecordLevelRead("Wonky", 1001, 59, 106, 60);
        BotXpCapResolution clamped60 = reg.ResolveForBot("Halvar", 60, 106, vanillaCfg);
        CHECK(clamped60.capLevel == 60 && clamped60.xpCapped && clamped60.expansionCapped);

        CHECK(EscapeJsonString("Logan") == "Logan");
        CHECK(EscapeJsonString("O\"Brien\\Jr\nX") == "O\\\"Brien\\\\Jr\\nX");
    }

    std::printf("ALL_ISSUE31_CPP_CHECKS_PASSED\n");
    return 0;
}
"""


class TestIssue31SecondaryOwnerXpCap(unittest.TestCase):
    def test_native_cpp17_owner_xp_cap_state_machine(self) -> None:
        compiler = shutil.which("g++") or shutil.which("clang++")
        if not compiler:
            self.skipTest("No C++17 compiler available")

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "test_owner_xp_cap.cpp"
            exe = Path(tmp) / "test_owner_xp_cap"
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
            self.assertIn("ALL_ISSUE31_CPP_CHECKS_PASSED", run.stdout)

    def test_native_cpp_source_wiring_and_thread_safety_invariants(self) -> None:
        mgr_h = (BOT_DIR / "CompanionBotMgr.h").read_text(encoding="utf-8")
        mgr_cpp = (BOT_DIR / "CompanionBotMgr.cpp").read_text(encoding="utf-8")
        pb_cpp = (SCRIPT_DIR / "Playerbots.cpp").read_text(encoding="utf-8")
        cmd_cpp = (SCRIPT_DIR / "PlayerbotCommandScript.cpp").read_text(encoding="utf-8")

        # Lock-free atomic getters preserved (V3), _ownerName immutable after LoadConfig (V4)
        self.assertIn('#include "CompanionOwnerXpCapLogic.h"', mgr_h)
        self.assertIn("uint32 GetOwnerLevel() const { return _ownerLevel.load(); }", mgr_h)
        self.assertIn("bool OwnerLevelUnknown() const { return _ownerConfigured.load() && _ownerLevel.load() == 0; }", mgr_h)
        self.assertNotIn("_ownerName = primaryName;", mgr_cpp)

        # Hot paths use allocation-free ResolveFastForBot; DB probe checks name + deleteDate IS NULL (P2-4)
        self.assertIn("ResolveFastForBot(bot->GetName(), bot->GetLevel(), nowSec, cfg).ownerLowGuid", mgr_cpp)
        self.assertIn("ResolveFastForBot(bot->GetName(), bot->GetLevel(), nowSec, cfg).ownerLevel", mgr_cpp)
        self.assertIn("ResolveFastForBot(player->GetName(), player->GetLevel(), nowSec, cfg).xpCapped", mgr_cpp)
        self.assertIn("SELECT level, name, deleteDate FROM characters WHERE guid = {}", mgr_cpp)
        self.assertIn("OwnersDueForProbe(probeStartSec, cfg, forceAll || primaryOwnerChanged)", mgr_cpp)
        self.assertIn("primaryDefinitiveUnresolved", mgr_cpp)
        self.assertIn("!primaryTransientDbError", mgr_cpp)
        self.assertIn("ClampStaleSeconds(", mgr_cpp)
        self.assertIn("CONFIG_MAX_PLAYER_LEVEL", mgr_cpp)
        # Silent config reads (showLogs=false, P2-2)
        self.assertIn('"AiPlayerbot.Companion.XpCap.StaleSeconds", 300, false', mgr_cpp)
        self.assertIn('"AiPlayerbot.Companion.XpCap.MaxConsecutiveFailures", 2, false', mgr_cpp)
        # Authoritative per-companion export in live_positions.json for #23/#24
        for field in (r'\"xp_eligible\":', r'\"xp_cap_level\":', r'\"xp_cap_state\":', r'\"xp_fail_closed\":', r'\"xp_owner_override\":'):
            self.assertIn(field, mgr_cpp)

        # OnPlayerGiveXP & .companion status use per-companion ResolveXpCapFor
        self.assertIn("sCompanionBotMgr.ResolveXpCapFor(player)", pb_cpp)
        self.assertIn("sCompanionBotMgr.ResolveXpCapFor(p)", cmd_cpp)
        self.assertNotIn("sCompanionBotMgr.GetOwnerLevel()", pb_cpp)
        self.assertIn("sCompanionBotMgr.ExpansionMaxLevel()", cmd_cpp)

    def test_end_to_end_live_positions_roundtrip_group_bindings_and_offline_overrides(self) -> None:
        # 1. P1-3(a), P1-3(b), and conflicting explicit owners in discover_owner_bindings
        comps_mixed = {
            "halvar": {"shift_group": "day-crew", "owner_name": "Wonky", "xp_owner_override": False},
            "ashwick_in_day": {"shift_group": "day-crew", "owner_name": "Logan", "xp_owner_override": True},
            "stray_in_core": {"shift_group": "core-logan", "owner_name": "Wonky", "xp_owner_override": False},
            "bramblepaw": {"shift_group": "core-logan", "owner_name": "Logan", "xp_owner_override": True},
            "conflict_a": {"shift_group": "core-alex", "owner_name": "Alex", "xp_owner_override": True},
            "conflict_b": {"shift_group": "core-alex", "owner_name": "Sam", "xp_owner_override": True},
        }
        bindings = discover_owner_bindings(comps_mixed)
        # (a) Neither primary-default nor per-bot override pollutes day-crew!
        self.assertNotIn("day-crew", bindings)
        # (b) Primary-default Wonky in core-logan does NOT overwrite core-logan -> logan!
        self.assertEqual(bindings.get("core-logan"), "logan")
        # Conflicting explicit overrides in core-alex drop the shared binding cleanly!
        self.assertNotIn("core-alex", bindings)
        # B-2: Downstream _xp_cap_prof_catchup_state MUST NOT re-synthesize `alex` from sg[5:] when conflicted!
        conflicted_offline = {
            "guid": 410,
            "name": "ConflictedBot",
            "slug": "conflictedbot",
            "level": 25,
            "shift_group": "core-alex",
            "skills": {171: (20, 75)},
        }
        conflict_ctx = {
            "companions": comps_mixed,
            "owner_levels": {"alex": 20, "sam": 20},
            "companion_level_lead": 3,
        }
        st_conf = _xp_cap_prof_catchup_state(conflicted_offline, conflict_ctx)
        self.assertEqual(st_conf["mode"], "none")
        self.assertEqual(st_conf["cap_level"], 0)

        # 2. End-to-end live_positions.json -> fetch_world_context (P2-6) + offline playerbots.conf override (V1)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            live_pos = tmp_path / "live_positions.json"
            pb_conf = tmp_path / "playerbots.conf"
            qm_path = tmp_path / "quartermaster.json"

            pb_conf.write_text(
                'AiPlayerbot.CompanionOwner = "Wonky"\n'
                "AiPlayerbot.CompanionLevelLead = 3\n"
                'AiPlayerbot.Companion.Owner.Ashwick = "Logan"\n'
                'AiPlayerbot.Companion.Owner.OfflineBot = "MissingOwner"\n',
                encoding="utf-8",
            )
            self.assertEqual(
                load_per_bot_owner_overrides(pb_conf),
                {"ashwick": "logan", "offlinebot": "missingowner"},
            )

            # Exact wire format emitted by CompanionBotMgr.cpp fprintf (including escaped owner name!)
            live_pos.write_text(
                json.dumps(
                    {
                        "t": 1700000000,
                        "weekly_event": 'Brewfest "2026"',
                        "units": [
                            {
                                "g": 110,
                                "m": 0,
                                "z": 12,
                                "x": -8900.5,
                                "y": -130.2,
                                "hp": 420,
                                "xp_eligible": False,
                                "xp_cap_level": 0,
                                "xp_cap_state": "unresolved_assignment",
                                "xp_fail_closed": True,
                                "xp_owner_override": True,
                                "owner_name": 'O"Brien\\Jr',
                            },
                            {
                                "g": 250,
                                "m": 0,
                                "z": 33,
                                "x": -12000.0,
                                "y": 100.0,
                                "hp": 1100,
                                "xp_eligible": True,
                                "xp_cap_level": 43,
                                "xp_cap_state": "resolved_fresh",
                                "xp_fail_closed": False,
                                "xp_owner_override": False,
                                "owner_name": "Wonky",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            def fake_run_sql(sql: str) -> list[list[str]]:
                s = " ".join(sql.split())
                if "FROM companion_schedule cs" in s and "JOIN characters c" in s:
                    return [
                        ["110", "Ashwick", "12", "12", "0", "5000", "1", "core-logan", "100", "1", "4", "0", "24", "127", "1"],
                        ["250", "Halvar", "33", "33", "0", "12000", "1", "day-crew", "500", "1", "1", "6", "18", "127", "0"],
                        ["310", "OfflineBot", "14", "12", "0", "2000", "0", "day-crew", "50", "1", "8", "0", "24", "127", "0"],
                    ]
                if "SELECT name, level FROM characters WHERE LOWER(name) IN" in s:
                    # Only Wonky (Lv 40) exists in DB; MissingOwner and O"Brien\Jr do not!
                    return [["Wonky", "40"]]
                return []

            with mock.patch.object(bridge_mod, "LIVE_POS_FILE", live_pos), \
                 mock.patch.object(bridge_mod, "QUARTERMASTER_PATH", qm_path), \
                 mock.patch.object(bridge_mod, "run_sql", side_effect=fake_run_sql), \
                 mock.patch.dict(os.environ, {"HEARTHCORE_PLAYERBOTS_CONF": str(pb_conf), "HEARTHCORE_COMPANION_LEVEL_LEAD": ""}):
                world_ctx = fetch_world_context()

            # Ashwick parsed directly from live_positions.json -> fail_closed (P2-3: NOT false at_cap!)
            ash = world_ctx["companions"]["ashwick"]
            self.assertEqual(ash["owner_name"], 'O"Brien\\Jr')
            self.assertIs(ash["xp_eligible"], False)
            self.assertIs(ash["xp_fail_closed"], True)
            self.assertEqual(ash["xp_cap_state"], "unresolved_assignment")
            self.assertFalse(ash["level_lead_state"]["at_level_cap"])
            self.assertTrue(ash["level_lead_state"]["xp_fail_closed"])
            self.assertIs(ash["level_lead_state"]["xp_eligible"], False)
            st_ash = _xp_cap_prof_catchup_state(ash, world_ctx)
            self.assertEqual(st_ash["mode"], "fail_closed")
            self.assertTrue(st_ash["fail_closed"])
            self.assertFalse(st_ash["at_cap"])

            # OfflineBot has NO live_positions entry, has playerbots.conf override "MissingOwner" -> fails closed (V1)!
            off = world_ctx["companions"]["offlinebot"]
            self.assertTrue(off["level_lead_state"]["xp_fail_closed"])
            self.assertIs(off["level_lead_state"]["xp_eligible"], False)
            self.assertEqual(off["level_lead_state"]["owner_cap_level"], 0)

            # Halvar (online day-crew) parsed cleanly with authoritative xp_cap_level=43
            hal = world_ctx["companions"]["halvar"]
            self.assertEqual(hal["xp_cap_level"], 43)
            self.assertEqual(authoritative_cap_params_for_companion(hal), {"xp_cap_level": 43, "xp_fail_closed": False})
            self.assertEqual(authoritative_cap_params_for_companion(ash), {"xp_cap_level": None, "xp_fail_closed": True})

    def test_issue24_gear_ambition_bracket_clamping_and_fail_closed(self) -> None:
        # 1. Standard secondary-owner cap (Lv 14 bot, Logan cap 15 < vanilla 60)
        b_logan = resolve_bracket(14, raid_stage=0, xp_cap_level=15)
        self.assertEqual(b_logan["server_level_cap"], 60)
        self.assertEqual(b_logan["effective_level_cap"], 15)
        self.assertEqual(b_logan["max_level"], 15)
        self.assertTrue(b_logan["xp_cap_authoritative"])
        self.assertFalse(b_logan["xp_fail_closed"])

        # 2. P1-1: Bot ALREADY above cap (Lv 10 bot, recreated Lv 1 owner -> cap 4) stays in 10-19 bracket!
        b_above = resolve_bracket(10, raid_stage=0, xp_cap_level=4)
        self.assertEqual(b_above["bracket_id"], "10-19")
        self.assertEqual(b_above["effective_level_cap"], 10)
        self.assertEqual(b_above["max_level"], 10)

        # 3. P1-2: Fail-closed (`xp_fail_closed=True` or `xp_cap_level=0`) caps at bot's current level (current bracket only!)
        h_fc = resolve_horizon_brackets(14, raid_stage=0, brackets=("current", "next", "cap"), xp_cap_level=0, xp_fail_closed=True)
        self.assertEqual([h["bracket_id"] for h in h_fc], ["10-19"])
        self.assertEqual(h_fc[0]["effective_level_cap"], 14)
        self.assertTrue(h_fc[0]["xp_cap_authoritative"])
        self.assertTrue(h_fc[0]["xp_fail_closed"])

        # 4. Production `plan_companion_gear_ambitions` forwards fail-closed & authoritative cap automatically!
        catalog = {
            9001: CatalogItem(
                entry=9001,
                name="Lv14 Tunic",
                item_level=16,
                required_level=14,
                quality=2,
                inventory_type=5,
                item_class=4,
                item_subclass=2,
                armor=65,
                stats={"agility": 5.0, "stamina": 4.0},
                sources=(
                    ItemSource(
                        source_kind="quest_reward",
                        source_id=501,
                        source_name="Test Quest",
                        faction="alliance",
                        evidence_table="acore_world.quest_template",
                        evidence_key=("quest", 501),
                        min_level=10,
                    ),
                ),
            ),
            9002: CatalogItem(
                entry=9002,
                name="Lv18 Tunic",
                item_level=22,
                required_level=18,
                quality=3,
                inventory_type=5,
                item_class=4,
                item_subclass=2,
                armor=90,
                stats={"agility": 10.0, "stamina": 9.0},
                sources=(
                    ItemSource(
                        source_kind="quest_reward",
                        source_id=502,
                        source_name="Higher Quest",
                        faction="alliance",
                        evidence_table="acore_world.quest_template",
                        evidence_key=("quest", 502),
                        min_level=16,
                    ),
                ),
            ),
        }
        # Capped at 15 via companion dict: Lv14 Tunic attainable, Lv18 Tunic excluded!
        comp_capped = {
            "class_id": 4,
            "race_id": 1,
            "faction": "alliance",
            "level": 14,
            "xp_cap_level": 15,
            "xp_eligible": True,
            "xp_fail_closed": False,
        }
        plan = plan_companion_gear_ambitions(
            comp_capped,
            catalog=catalog,
            role="damage",
            equipped_items={"chest": None},
            raid_stage=0,
            brackets=("current", "next", "cap"),
            now_ts=1700000000,
        )
        self.assertTrue(plan["ok"], msg=plan)
        self.assertTrue(plan["xp_cap_authoritative"])
        self.assertFalse(plan["xp_fail_closed"])
        self.assertEqual(plan["bracket"]["effective_level_cap"], 15)
        self.assertIn(9001, [t["item_entry"] for t in plan["slots"]["chest"]["attainable"]])
        self.assertNotIn(9002, [t["item_entry"] for t in plan["slots"]["chest"]["aspirational"]])

        # Fail-closed companion dict (xp_cap_level=0, xp_fail_closed=True): caps at Lv 14, NO next/cap horizon!
        comp_fc = {
            "class_id": 4,
            "race_id": 1,
            "faction": "alliance",
            "level": 14,
            "xp_cap_level": 0,
            "xp_eligible": False,
            "xp_fail_closed": True,
            "xp_cap_state": "unresolved_assignment",
        }
        plan_fc = plan_companion_gear_ambitions(
            comp_fc,
            catalog=catalog,
            role="damage",
            equipped_items={"chest": None},
            raid_stage=0,
            brackets=("current", "next", "cap"),
            now_ts=1700000000,
        )
        self.assertTrue(plan_fc["ok"])
        self.assertTrue(plan_fc["xp_cap_authoritative"])
        self.assertTrue(plan_fc["xp_fail_closed"])
        self.assertEqual(plan_fc["bracket"]["effective_level_cap"], 14)
        self.assertEqual(list(plan_fc["bracket_plans"].keys()), ["current"])

        # Missing class_id fails closed cleanly (never silently defaults to Warrior!)
        plan_missing_cls = plan_companion_gear_ambitions(
            {"level": 14, "faction": "alliance"},
            catalog=catalog,
            role="damage",
            equipped_items={"chest": None},
            now_ts=1700000000,
        )
        self.assertFalse(plan_missing_cls["ok"])
        self.assertEqual(plan_missing_cls["reason"], "missing_class_id")


if __name__ == "__main__":
    unittest.main()
