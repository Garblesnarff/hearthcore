"""Issue #12: Native C++17 + Python unit & concurrency tests for admission flags, resource limits, and group caps."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hearthcore.identity import persona_id_for_slug
from hearthcore.opportunities import (
    OpportunityCoordinator,
    admit_cooperative_opportunity,
    admit_live_cooperative_opportunity,
    check_resource_headroom,
    evaluate_persisted_headroom,
    run_shadow_match_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "mod-playerbots-companions" / "src" / "Bot"
ACTIONS_DIR = ROOT / "mod-playerbots-companions" / "src" / "Ai" / "Base" / "Actions"
BRAIN_CPP = BOT_DIR / "CompanionBrain.cpp"
MGR_CPP = BOT_DIR / "CompanionBotMgr.cpp"
LFG_CPP = ACTIONS_DIR / "LfgActions.cpp"
THRESHOLDS_DOC = ROOT / "docs" / "PHASE_II_STAGING_THRESHOLDS.md"


CPP_HARNESS = r"""
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <sstream>
#include <string>
#include <unordered_set>
#include <vector>

#include "CompanionCoopAdmissionLogic.h"

using namespace CompanionCoopAdmission;

#define CHECK(expr)                                                                                  \
    do                                                                                               \
    {                                                                                                \
        if (!(expr))                                                                                 \
        {                                                                                            \
            std::cerr << "CHECK FAILED at line " << __LINE__ << ": " << #expr << "\n";               \
            std::exit(1);                                                                            \
        }                                                                                            \
    } while (0)

static AdmissionConfig MakeCfg(bool enabled = true)
{
    AdmissionConfig c;
    c.coopEnabled = enabled;
    c.enabledDungeonMaps = ParseEnabledMapList("34, 36, 43");
    c.maxManagedGroups = 1;
    c.maxTotalInstances = 4;
    c.maxOnlinePopulation = 45;
    c.maxTelemetryAgeSec = 30;
    c.tokenTtlSec = 180;
    return c;
}

static TelemetrySnapshot MakeHealthySnap(uint32_t ts)
{
    TelemetrySnapshot s;
    s.valid = true;
    s.sampledAt = ts;
    s.hasMemAvail = true;
    s.memAvailMib = 900.0;
    s.hasTickP99 = true;
    s.tickP99Ms = 20.0;
    s.hasSwapIo = true;
    s.swapIoPagesPerSec = 2.0;
    s.hasCpuPsi = true;
    s.cpuPsiSomeAvg10 = 3.0;
    s.hasInstances = true;
    s.activeInstances = 1;  // e.g. 1 existing human dungeon instance
    s.hasPopulation = true;
    s.onlinePopulation = 20;
    return s;
}

int main()
{
    const uint32_t T0 = 20000;

    // 0. Pure C++17 /proc & Python-compatible JSON headroom_state parsers!
    {
        std::istringstream mem("MemTotal:        8000000 kB\nMemFree:          500000 kB\nMemAvailable:    1048576 kB\n");
        double mib = 0.0;
        CHECK(ParseMemAvailableMib(mem, mib) && std::abs(mib - 1024.0) < 0.01);
        std::istringstream memBad("MemTotal: 8000000 kB\n");
        CHECK(!ParseMemAvailableMib(memBad, mib));

        std::istringstream vm("nr_free_pages 1234\npswpin 400\npswpout 600\n");
        uint64_t pages = 0;
        CHECK(ParseVmstatSwapPages(vm, pages) && pages == 1000);
        std::istringstream vmMissing("pswpin 400\n");
        CHECK(!ParseVmstatSwapPages(vmMissing, pages));

        std::istringstream psi("some avg10=12.34 avg60=5.00 avg300=1.00 total=999\nfull avg10=0.00\n");
        double avg10 = 0.0;
        CHECK(ParseCpuPsiSomeAvg10(psi, avg10) && std::abs(avg10 - 12.34) < 0.001);
        std::istringstream psiBad("some avg10=nan avg60=0\n");
        CHECK(!ParseCpuPsiSomeAvg10(psiBad, avg10));

        // Round-trip Python-compatible JSON headroom state + reject corrupt JSON
        std::string jTrue = FormatHeadroomStateJson(true, 12345, {"low_ram_headroom"});
        std::string jFalse = FormatHeadroomStateJson(false, 12346, {});
        bool p = false;
        CHECK(ParseHeadroomPausedJson(jTrue, p) && p == true);
        CHECK(ParseHeadroomPausedJson(jFalse, p) && p == false);
        CHECK(!ParseHeadroomPausedJson("{corrupt", p));
        CHECK(!ParseHeadroomPausedJson("{\"paused\": false", p));  // truncated/torn JSON MUST fail closed!
        CHECK(!ParseHeadroomPausedJson("{\"paused\": \"true\"}", p));
    }

    // 1. TickRingBuffer fails closed below MIN_SAMPLES, computes exact p99 after wrap-around.
    {
        TickRingBuffer ring;
        double p99 = 0.0;
        for (int i = 0; i < 19; ++i)
            ring.Record(15);
        CHECK(!ring.HasEnoughSamples());
        CHECK(!ring.Percentile(99.0, p99));

        ring.Record(15);
        CHECK(ring.HasEnoughSamples());
        CHECK(ring.Percentile(99.0, p99) && p99 == 15.0);

        for (int i = 0; i < 300; ++i)
            ring.Record(i == 299 ? 42 : 12);
        CHECK(ring.Count() == TICK_RING_CAPACITY);
        CHECK(ring.Percentile(99.0, p99) && p99 >= 12.0 && p99 <= 42.0);
    }

    // 2. ParseEnabledMapList strict parsing.
    {
        CHECK(ParseEnabledMapList("").empty());
        auto m = ParseEnabledMapList("34, 36, bad, 0, -5, 43");
        CHECK(m.size() == 3 && m.count(34) == 1 && m.count(36) == 1 && m.count(43) == 1);
    }

    // 3. Disabled flag, unsupported dungeon, unenrolled member, forced mass login all rejected.
    {
        AdmissionController ctrl;
        std::unordered_set<uint64_t> none;
        auto snap = MakeHealthySnap(T0);

        auto dOff = ctrl.TryAdmitNewQueueOrProposal(MakeCfg(false), snap, 1, 34, none, none, true, 0, T0);
        CHECK(!dOff.admitted && dOff.reason == "coop_disabled");

        auto dUnsup = ctrl.TryAdmitNewQueueOrProposal(MakeCfg(true), snap, 1, 409, none, none, true, 0, T0);
        CHECK(!dUnsup.admitted && dUnsup.reason == "unsupported_or_disabled_dungeon");

        auto dUnenr = ctrl.TryAdmitNewQueueOrProposal(MakeCfg(true), snap, 1, 34, none, none, false, 0, T0);
        CHECK(!dUnenr.admitted && dUnenr.reason == "unenrolled_member");

        auto dLogin = ctrl.TryAdmitNewQueueOrProposal(MakeCfg(true), snap, 1, 34, none, none, true, 3, T0);
        CHECK(!dLogin.admitted && dLogin.reason == "forced_mass_login_forbidden");
    }

    // 4. Missing, stale, future-skewed, NaN, Inf, negative telemetry ALL fail closed!
    {
        AdmissionController ctrl;
        auto cfg = MakeCfg(true);
        std::unordered_set<uint64_t> none;
        const double kNan = std::numeric_limits<double>::quiet_NaN();
        const double kInf = std::numeric_limits<double>::infinity();

        TelemetrySnapshot empty;
        CHECK(!ctrl.TryAdmitNewQueueOrProposal(cfg, empty, 1, 34, none, none, true, 0, T0).admitted);

        auto stale = MakeHealthySnap(T0 - 31);
        CHECK(ctrl.TryAdmitNewQueueOrProposal(cfg, stale, 1, 34, none, none, true, 0, T0).reason == "stale_telemetry");
        auto future = MakeHealthySnap(T0 + 5);
        CHECK(ctrl.TryAdmitNewQueueOrProposal(cfg, future, 1, 34, none, none, true, 0, T0).reason == "stale_telemetry");

        auto sNan = MakeHealthySnap(T0);
        sNan.memAvailMib = kNan;
        CHECK(ctrl.TryAdmitNewQueueOrProposal(cfg, sNan, 1, 34, none, none, true, 0, T0).reason == "missing_or_invalid_ram_telemetry");

        auto sInf = MakeHealthySnap(T0);
        sInf.tickP99Ms = kInf;
        CHECK(ctrl.TryAdmitNewQueueOrProposal(cfg, sInf, 1, 34, none, none, true, 0, T0).reason == "missing_or_invalid_tick_telemetry");

        auto sNeg = MakeHealthySnap(T0);
        sNeg.swapIoPagesPerSec = -1.0;
        CHECK(ctrl.TryAdmitNewQueueOrProposal(cfg, sNeg, 1, 34, none, none, true, 0, T0).reason == "missing_or_invalid_swap_telemetry");

        auto sPsiNan = MakeHealthySnap(T0);
        sPsiNan.cpuPsiSomeAvg10 = kNan;
        CHECK(ctrl.TryAdmitNewQueueOrProposal(cfg, sPsiNan, 1, 34, none, none, true, 0, T0).reason == "missing_or_invalid_cpu_psi_telemetry");
    }

    // 5. Persisted hysteresis across RAM, tick p99, swap I/O, and CPU PSI.
    {
        AdmissionController ctrl;
        auto cfg = MakeCfg(true);
        std::vector<std::string> reasons;

        auto sLow = MakeHealthySnap(T0);
        sLow.memAvailMib = 480.0;
        CHECK(!ctrl.EvaluateHeadroom(cfg, sLow, T0, reasons) && ctrl.IsHeadroomPaused());

        auto sMid = MakeHealthySnap(T0 + 5);
        sMid.memAvailMib = 580.0;
        CHECK(!ctrl.EvaluateHeadroom(cfg, sMid, T0 + 5, reasons) && ctrl.IsHeadroomPaused());

        auto sSwapMid = MakeHealthySnap(T0 + 10);
        sSwapMid.memAvailMib = 700.0;
        sSwapMid.swapIoPagesPerSec = 15.0;
        CHECK(!ctrl.EvaluateHeadroom(cfg, sSwapMid, T0 + 10, reasons) && ctrl.IsHeadroomPaused());

        auto sRec = MakeHealthySnap(T0 + 15);
        sRec.memAvailMib = 700.0;
        sRec.tickP99Ms = 30.0;
        sRec.swapIoPagesPerSec = 8.0;
        sRec.cpuPsiSomeAvg10 = 8.0;
        CHECK(ctrl.EvaluateHeadroom(cfg, sRec, T0 + 15, reasons) && !ctrl.IsHeadroomPaused());

        AdmissionController restarted;
        restarted.LoadHysteresisState(true);
        CHECK(!restarted.EvaluateHeadroom(cfg, sMid, T0 + 20, reasons) && restarted.IsHeadroomPaused());
    }

    // 6. Atomic group/assembling/instance/population caps, committed token pruning, & committed run safety!
    {
        AdmissionController ctrl;
        auto cfg = MakeCfg(true);
        std::unordered_set<uint64_t> committed;
        std::unordered_set<uint64_t> assembling;
        auto snap = MakeHealthySnap(T0);

        // Other assembling group counts toward group cap!
        assembling.insert(99);
        auto aBlockedByAsm = ctrl.TryAdmitNewQueueOrProposal(cfg, snap, 10, 34, committed, assembling, true, 0, T0);
        CHECK(!aBlockedByAsm.admitted && aBlockedByAsm.reason == "group_cap_reached");
        assembling.clear();

        // First proposal admitted (1/1 managed groups); second concurrent proposal blocked by group_cap_reached!
        auto a1 = ctrl.TryAdmitNewQueueOrProposal(cfg, snap, 10, 34, committed, assembling, true, 0, T0);
        CHECK(a1.admitted && a1.tokenId > 0 && a1.effectiveGroups == 1);
        auto a2 = ctrl.TryAdmitNewQueueOrProposal(cfg, snap, 11, 36, committed, assembling, true, 0, T0 + 1);
        CHECK(!a2.admitted && a2.reason == "group_cap_reached");

        // Mark opp 10 committed; drop host RAM below 512 MiB AND toggle coopEnabled=false:
        // Committed opp 10 STILL passes safely ("already_committed") while new opp 12 is blocked!
        CHECK(ctrl.MarkCommitted(10));
        committed.insert(10);
        auto lowRam = MakeHealthySnap(T0 + 5);
        lowRam.memAvailMib = 300.0;
        auto a1Keep = ctrl.TryAdmitNewQueueOrProposal(MakeCfg(false), lowRam, 10, 34, committed, assembling, true, 0, T0 + 5);
        CHECK(a1Keep.admitted && a1Keep.reason == "already_committed");
        auto aNewBlocked = ctrl.TryAdmitNewQueueOrProposal(cfg, lowRam, 12, 36, committed, assembling, true, 0, T0 + 5);
        CHECK(!aNewBlocked.admitted && aNewBlocked.reason == "low_ram_headroom");

        // Normal completion: ReloadCoopMemberships prunes committed token when opp 10 disappears from liveOppIds!
        CHECK(ctrl.PruneCommittedTokensNotIn({}) == 1);
        CHECK(!ctrl.HasToken(10) && ctrl.ActiveTokenCount() == 0);
        committed.clear();
        cfg.maxManagedGroups = 3;

        auto instFull = MakeHealthySnap(T0 + 20);
        instFull.activeInstances = 4;
        CHECK(ctrl.TryAdmitNewQueueOrProposal(cfg, instFull, 20, 34, committed, assembling, true, 0, T0 + 20).reason == "instance_cap_reached");

        auto inst3 = MakeHealthySnap(T0 + 21);
        inst3.activeInstances = 3;
        CHECK(ctrl.TryAdmitNewQueueOrProposal(cfg, inst3, 21, 34, committed, assembling, true, 0, T0 + 21).admitted);
        CHECK(ctrl.TryAdmitNewQueueOrProposal(cfg, inst3, 22, 36, committed, assembling, true, 0, T0 + 22).reason == "instance_cap_reached");

        // Uncommitted token 21 expires after tokenTtlSec (180s), freeing the slot!
        auto inst3Later = MakeHealthySnap(T0 + 205);
        inst3Later.activeInstances = 3;
        CHECK(ctrl.TryAdmitNewQueueOrProposal(cfg, inst3Later, 22, 36, committed, assembling, true, 0, T0 + 205).admitted);
        CHECK(ctrl.ReleaseToken(22));

        // Population cap (45 >= 45) blocks new admission!
        auto popFull = MakeHealthySnap(T0 + 210);
        popFull.onlinePopulation = 45;
        CHECK(ctrl.TryAdmitNewQueueOrProposal(cfg, popFull, 23, 34, committed, assembling, true, 0, T0 + 210).reason == "population_cap_reached");
    }

    std::cout << "ALL_ISSUE12_CPP_TESTS_PASSED\n";
    return 0;
}
"""


def _seed_party(coord: OpportunityCoordinator, dungeon_id: str, members: list[tuple[str, int, str]], *, now: int, is_shadow: bool = False) -> int:
    opp = coord.create_opportunity(dungeon_id=dungeon_id, creator_guid=members[0][1], is_shadow=is_shadow, now_ts=now)
    for slug, guid, role in members:
        ok = coord.express_interest(
            opportunity_id=opp["id"],
            expected_revision=1,
            persona_id=persona_id_for_slug(slug),
            character_guid=guid,
            character_slug=slug,
            role=role,
            reason_code="volunteer",
            available_until=now + 3600,
            now_ts=now,
        )
        assert ok
    return opp["id"]


class Issue12AdmissionLimitsTests(unittest.TestCase):
    def test_native_cpp17_admission_controller_and_parsers(self) -> None:
        gpp = shutil.which("g++")
        if not gpp:
            self.skipTest("g++ not installed")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "test_coop_admission.cpp"
            bin_path = Path(tmp) / "test_coop_admission"
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
            self.assertIn("ALL_ISSUE12_CPP_TESTS_PASSED", run.stdout)

    def test_headroom_rejects_nan_inf_stale_swap_cpu_instances_population_and_separates_cap_from_pause(self) -> None:
        now = 1_800_000_000
        # NaN & Inf & negative telemetry MUST fail closed!
        r_nan = check_resource_headroom(mem_avail_mib=math.nan, tick_p99_ms=20.0, active_managed_groups=0)
        self.assertFalse(r_nan["admit_ok"])
        self.assertTrue(r_nan["headroom_paused"])
        self.assertIn("invalid_ram_telemetry", r_nan["reasons"])

        r_inf = check_resource_headroom(mem_avail_mib=800.0, tick_p99_ms=math.inf, active_managed_groups=0)
        self.assertFalse(r_inf["admit_ok"])
        self.assertIn("invalid_tick_telemetry", r_inf["reasons"])

        # Normal group cap hit MUST block admission WITHOUT flipping hardware headroom hysteresis to paused!
        with tempfile.TemporaryDirectory() as tmp:
            hr_path = Path(tmp) / "headroom.json"
            r_cap = evaluate_persisted_headroom(
                hr_path, mem_avail_mib=560.0, tick_p99_ms=20.0, active_managed_groups=1, max_managed_groups=1, now_ts=now
            )
            self.assertFalse(r_cap["admit_ok"])
            self.assertTrue(r_cap["cap_blocked"])
            self.assertFalse(r_cap["headroom_paused"])
            self.assertFalse(json.loads(hr_path.read_text(encoding="utf-8"))["paused"])
            # When the 1 group finishes, 560 MiB (>= 512 MiB) admits cleanly without needing 640 MiB recovery!
            r_after = evaluate_persisted_headroom(
                hr_path, mem_avail_mib=560.0, tick_p99_ms=20.0, active_managed_groups=0, max_managed_groups=1, now_ts=now + 10
            )
            self.assertTrue(r_after["admit_ok"])

            # Unwritable / read-only path fails closed with warning instead of raising OSError!
            ro_blocker = Path(tmp) / "not_a_dir"
            ro_blocker.write_text("x", encoding="utf-8")
            r_ro = evaluate_persisted_headroom(
                ro_blocker / "headroom.json", mem_avail_mib=900.0, tick_p99_ms=15.0, active_managed_groups=0, now_ts=now + 20
            )
            self.assertFalse(r_ro["admit_ok"])
            self.assertIn("headroom_state_unwritable", r_ro["reasons"])

        # Stale telemetry fails closed
        r_stale = check_resource_headroom(
            mem_avail_mib=900.0, tick_p99_ms=18.0, active_managed_groups=0,
            telemetry_ts=now - 600, now_ts=now, max_telemetry_age_sec=300,
        )
        self.assertFalse(r_stale["admit_ok"])
        self.assertTrue(any(x.startswith("stale_telemetry:") for x in r_stale["reasons"]))

        # Extended telemetry fails closed on missing fields, high swap, high CPU PSI, human instances, population!
        r_ext_miss = check_resource_headroom(
            mem_avail_mib=900.0, tick_p99_ms=18.0, active_managed_groups=0,
            require_extended_telemetry=True,
        )
        self.assertFalse(r_ext_miss["admit_ok"])
        for expected in ("missing_telemetry_timestamp", "missing_swap_telemetry", "missing_cpu_psi_telemetry", "missing_instance_telemetry", "missing_population_telemetry"):
            self.assertIn(expected, r_ext_miss["reasons"])

        r_caps = check_resource_headroom(
            mem_avail_mib=900.0, tick_p99_ms=18.0, active_managed_groups=0,
            pending_proposals=1, active_instances=2, human_instances=1, max_total_instances=4,
            swap_io_pages_per_sec=30.0, cpu_psi_some_avg10=25.0, cpu_throttled_pct=20.0,
            online_population=45, max_online_population=45,
            telemetry_ts=now - 5, now_ts=now, require_extended_telemetry=True,
        )
        self.assertFalse(r_caps["admit_ok"])
        self.assertTrue(any(x.startswith("group_cap_reached:") for x in r_caps["reasons"]))
        self.assertTrue(any(x.startswith("instance_cap_reached:4/4") for x in r_caps["reasons"]))
        self.assertTrue(any(x.startswith("high_swap_io:") for x in r_caps["reasons"]))
        self.assertTrue(any(x.startswith("high_cpu_psi:") for x in r_caps["reasons"]))
        self.assertTrue(any(x.startswith("high_cpu_throttle:") for x in r_caps["reasons"]))
        self.assertTrue(any(x.startswith("population_cap_reached:45/45") for x in r_caps["reasons"]))

    def test_concurrent_live_proposals_default_caps_and_shadow_nan_safety(self) -> None:
        now = 1_800_000_000
        party_a = [("tank_a", 301, "tank"), ("heal_a", 302, "healer"), ("dps_a1", 303, "damage"), ("dps_a2", 304, "damage"), ("dps_a3", 305, "damage")]
        party_b = [("tank_b", 401, "tank"), ("heal_b", 402, "healer"), ("dps_b1", 403, "damage"), ("dps_b2", 404, "damage"), ("dps_b3", 405, "damage")]

        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            db_p = tp / "brain.db"
            hr_p = tp / "headroom.json"
            coord = OpportunityCoordinator(db_p)
            oid_a = _seed_party(coord, "stockade_n", party_a, now=now, is_shadow=False)
            oid_b = _seed_party(coord, "deadmines_n", party_b, now=now, is_shadow=False)

            common_kwargs = dict(
                expected_revision=1,
                state_path=hr_p,
                mem_avail_mib=900.0,
                tick_p99_ms=20.0,
                swap_io_pages_per_sec=1.5,
                cpu_psi_some_avg10=2.0,
                active_managed_groups=0,
                active_instances=1,
                online_population=20,
                telemetry_ts=now - 5,
                max_managed_groups=1,
                now_ts=now,
            )

            # 1. Default admit_cooperative_opportunity enforces coop_disabled flag by default!
            with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_ENABLED": "0", "HEARTHCORE_COOP_DUNGEONS": "stockade_n,deadmines_n"}):
                r_def_off = admit_cooperative_opportunity(
                    coord, oid_a, expected_revision=1, state_path=hr_p,
                    mem_avail_mib=900.0, tick_p99_ms=20.0, active_managed_groups=0, now_ts=now,
                )
                self.assertFalse(r_def_off["ok"])
                self.assertEqual(r_def_off["reason"], "coop_disabled")

                r_off = admit_live_cooperative_opportunity(coord, oid_a, **common_kwargs)
                self.assertFalse(r_off["ok"])
                self.assertEqual(r_off["reason"], "coop_disabled")

            # 2. Enabled coop, disabled dungeon rejects live admission!
            with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_ENABLED": "1", "HEARTHCORE_COOP_DUNGEONS": "deadmines_n"}):
                r_dun = admit_live_cooperative_opportunity(coord, oid_a, **common_kwargs)
                self.assertFalse(r_dun["ok"])
                self.assertEqual(r_dun["reason"], "dungeon_disabled_live:stockade_n")

            # 3. Forced mass login rejected + concurrent disjoint parties capped atomically!
            with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_ENABLED": "1", "HEARTHCORE_COOP_DUNGEONS": "stockade_n,deadmines_n"}):
                r_login = admit_live_cooperative_opportunity(coord, oid_a, additional_logins_needed=2, **common_kwargs)
                self.assertFalse(r_login["ok"])
                self.assertEqual(r_login["reason"], "forced_mass_login_forbidden")

                with ThreadPoolExecutor(max_workers=2) as ex:
                    fa = ex.submit(admit_live_cooperative_opportunity, coord, oid_a, **common_kwargs)
                    fb = ex.submit(admit_live_cooperative_opportunity, coord, oid_b, **common_kwargs)
                    ra, rb = fa.result(), fb.result()

                winners = [r for r in (ra, rb) if r["ok"]]
                losers = [r for r in (ra, rb) if not r["ok"]]
                self.assertEqual(len(winners), 1, f"Expected exactly 1 winner, got ra={ra}, rb={rb}")
                self.assertEqual(len(losers), 1)
                self.assertEqual(losers[0]["reason"], "group_cap_reached")

                # 4. Direct reserve_party() ALSO enforces DEFAULT_MAX_GROUPS=1 for disjoint live party!
                party_c = [("tank_c", 501, "tank"), ("heal_c", 502, "healer"), ("dps_c1", 503, "damage"), ("dps_c2", 504, "damage"), ("dps_c3", 505, "damage")]
                oid_c = _seed_party(coord, "stockade_n", party_c, now=now, is_shadow=False)
                r_direct = coord.reserve_party(oid_c, expected_revision=1, now_ts=now + 1)
                self.assertFalse(r_direct["ok"])
                self.assertEqual(r_direct["reason"], "group_cap_reached")

                # 5. Shadow match snapshot reflects 1 real pending live group AND handles NaN/bool vitals without crashing!
                sh = run_shadow_match_snapshot(
                    {"companions": {}, "work_orders": []},
                    db_path=db_p,
                    runtime_dir=tp / "rt",
                    vitals={"mem_avail_gib": 1.0, "tick_p99": 20.0, "observed_at": "2027-01-15T08:00:00Z"},
                    now_ts=1_800_000_005,
                )
                self.assertEqual(sh["headroom"]["pending_proposals"], 1)
                self.assertEqual(sh["headroom"]["effective_groups"], 1)
                self.assertFalse(sh["headroom"]["admit_ok"])
                self.assertTrue(any(r.startswith("group_cap_reached:") for r in sh["headroom"]["reasons"]))

                sh_nan = run_shadow_match_snapshot(
                    {"companions": {}, "work_orders": []},
                    db_path=db_p,
                    runtime_dir=tp / "rt2",
                    vitals={"mem_avail_gib": math.nan, "tick_p99": True, "active_managed_groups": math.nan, "human_instances": True},
                    now_ts=now,
                )
                self.assertFalse(sh_nan["headroom"]["admit_ok"])
                self.assertIn("invalid_ram_telemetry", sh_nan["headroom"]["reasons"])
                self.assertIn("invalid_tick_telemetry", sh_nan["headroom"]["reasons"])

    def test_wiring_invariants_and_thresholds_doc(self) -> None:
        brain = BRAIN_CPP.read_text(encoding="utf-8")
        mgr = MGR_CPP.read_text(encoding="utf-8")
        lfg = LFG_CPP.read_text(encoding="utf-8")
        doc = THRESHOLDS_DOC.read_text(encoding="utf-8")

        self.assertIn('#include "CompanionCoopAdmissionLogic.h"', brain)
        self.assertIn("gTickRing.Record(elapsed);", brain)
        self.assertIn("SampleCoopHostTelemetry(", brain)
        self.assertIn("ParseMemAvailableMib(", brain)
        self.assertIn("ParseVmstatSwapPages(", brain)
        self.assertIn("ParseCpuPsiSomeAvg10(", brain)
        self.assertIn("FormatHeadroomStateJson(", brain)
        self.assertIn("ParseHeadroomPausedJson(", brain)
        self.assertIn("FlushHysteresisPersistenceOnWorldThread();", brain)
        self.assertIn("rawLfgDungeonEntry & 0x00FFFFFFu", brain)
        self.assertIn("sMapMgr->GetNumInstances(", brain)
        self.assertIn("sWorldSessionMgr->GetPlayerCount()", brain)
        self.assertIn("TryAdmitCoopQueue(", brain)
        self.assertIn("TryAdmitCompanionLfgJoin(", brain)
        self.assertIn("TryAdmitCompanionLfgProposal(", brain)
        self.assertIn("PruneCommittedTokensNotIn(", brain)
        self.assertIn("gAdmissionBlockedOpps", brain)

        # Native LFG queue join & proposal accept are gated for non-owner-grouped companions!
        self.assertIn("CompanionBrain::TryAdmitCompanionLfgJoin(", lfg)
        # LfgActions routes proposals through EvaluateCompanionLfgProposal (which calls TryAdmitCompanionLfgProposal!)
        self.assertIn("CompanionBrain::EvaluateCompanionLfgProposal(", lfg)
        self.assertIn("TryAdmitCompanionLfgProposal(", brain)

        # No emergency combat/coop logout; non-coop startup unaffected (0 = unlimited when coop OFF)
        self.assertIn("!IsInAuthorizedCoopGroup(connected)", mgr)
        self.assertIn("AiPlayerbot.Companion.MaxLoginsPerPass", mgr)
        self.assertIn("CompanionBrain::CoopEnabled() ? 2u : 0u", mgr)

        # Thresholds doc documents real Acer i5-7400 limits
        for token in ("512 MiB", "640 MiB", "45.0 ms", "38.0 ms", "25.0 pages/s", "20.0 %", "MaxManagedGroups", "MaxTotalInstances", "companion_coop_headroom.json"):
            self.assertIn(token, doc)


if __name__ == "__main__":
    unittest.main()
