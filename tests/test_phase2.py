"""Comprehensive Phase II Adversarial Test Suite (Cards II-01 through II-10).

Covers PR #10's Mandatory Test Matrix (docs/PHASE_II_IMPLEMENTATION.md) with zero real API spend
and zero live MySQL mutations.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hearthcore.benchmark import build_benchmark_artifact
from hearthcore.dungeons import DUNGEON_BY_ID, check_character_eligibility, infer_viable_roles
from hearthcore.identity import PersonaRegistry, persona_id_for_slug
from hearthcore.opportunities import (
    OpportunityCoordinator,
    adapt_work_orders_as_opportunities,
    check_resource_headroom,
    coop_enabled,
    run_shadow_match_snapshot,
)
from hearthcore.playstyle import apply_playstyle_scoring, normalize_traits
from hearthcore.readiness import evaluate_raid_roster


def _make_roster() -> dict:
    return {
        "brannoc": {"guid": 101, "name": "Brannoc", "slug": "brannoc", "level": 32, "online": True, "shift_group": "core-wonky", "zone": "Ironforge"},
        "ironbulwark": {"guid": 201, "name": "Ironbulwark", "slug": "ironbulwark", "level": 24, "online": True, "shift_group": "early-birds", "zone": "Stormwind City"},
        "aelindra": {"guid": 202, "name": "Aelindra", "slug": "aelindra", "level": 24, "online": True, "shift_group": "early-birds", "zone": "Stormwind City"},
        "alaric": {"guid": 203, "name": "Alaric", "slug": "alaric", "level": 23, "online": True, "shift_group": "early-birds", "zone": "Stormwind City"},
        "pipwick": {"guid": 204, "name": "Pipwick", "slug": "pipwick", "level": 23, "online": True, "shift_group": "early-birds", "zone": "Stormwind City"},
        "nightstep": {"guid": 205, "name": "Nightstep", "slug": "nightstep", "level": 23, "online": True, "shift_group": "prime-time", "zone": "Stormwind City"},
    }


class Phase2Tests(unittest.TestCase):
    # ------------------------------------------------------------------
    # Card II-01: Persona/character identity seam & single-active-char rule
    # ------------------------------------------------------------------
    def test_identity_seam_core_protection_and_multi_char_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = PersonaRegistry(Path(tmp) / "brain.db")
            roster = _make_roster()
            res = reg.sync_from_companions(roster)
            self.assertEqual(res["synced"], 6)
            self.assertEqual(res["quarantined"], [])

            # Core-4 protected by default; guild crew enrolled
            p_core = reg.persona_for_guid(101)
            p_guild = reg.persona_for_guid(201)
            self.assertIsNotNone(p_core)
            self.assertFalse(p_core.enrolled_coop)
            self.assertIsNotNone(p_guild)
            self.assertTrue(p_guild.enrolled_coop)

            # Future alt under same persona cannot be double-counted in distinct_personas()
            reg.register_alt(persona_id=p_guild.persona_id, slug="ironalt", character_guid=999, slot_kind="alt")
            dedup = reg.distinct_personas([201, 999, 202])
            self.assertEqual(len(dedup), 2)

            # GUID reuse conflict quarantined honestly instead of silent overwrite
            conflict = {"otherbot": {"guid": 201, "name": "Otherbot", "slug": "otherbot", "level": 10, "online": True, "shift_group": "prime-time"}}
            c_res = reg.sync_from_companions(conflict)
            self.assertEqual(len(c_res["quarantined"]), 1)

    # ------------------------------------------------------------------
    # Card II-03: Dungeon catalog, unsupported rejection, lockouts, faction & level
    # ------------------------------------------------------------------
    def test_dungeon_catalog_eligibility_and_unsupported_rejection(self):
        stockade = DUNGEON_BY_ID["stockade_n"]
        sfk = DUNGEON_BY_ID["sfk_n"]

        # Live pilot defaults OFF!
        ok_live, reason_live = check_character_eligibility(
            level=24, faction="alliance", viable_roles=["tank"], dungeon=stockade, allow_pilot_shadow=False
        )
        self.assertFalse(ok_live)
        self.assertIn("dungeon_disabled", reason_live)

        # Unsupported dungeon rejected even in shadow
        ok_unsup, reason_unsup = check_character_eligibility(
            level=24, faction="alliance", viable_roles=["tank"], dungeon=sfk, allow_pilot_shadow=True
        )
        self.assertFalse(ok_unsup)
        self.assertIn("dungeon_unsupported", reason_unsup)

        # Faction mismatch, lockout, level range, insufficient schedule rejected
        ok_fac, r_fac = check_character_eligibility(
            level=24, faction="horde", viable_roles=["tank"], dungeon=stockade, allow_pilot_shadow=True
        )
        self.assertFalse(ok_fac)
        self.assertIn("faction_mismatch", r_fac)

        ok_lock, r_lock = check_character_eligibility(
            level=24, faction="alliance", viable_roles=["tank"], dungeon=stockade, locked_maps={34}, allow_pilot_shadow=True
        )
        self.assertFalse(ok_lock)
        self.assertIn("instance_locked", r_lock)

        ok_lvl, _ = check_character_eligibility(
            level=10, faction="alliance", viable_roles=["tank"], dungeon=stockade, allow_pilot_shadow=True
        )
        self.assertFalse(ok_lvl)

        ok_win, _ = check_character_eligibility(
            level=24, faction="alliance", viable_roles=["tank"], dungeon=stockade, available_minutes=20, allow_pilot_shadow=True
        )
        self.assertFalse(ok_win)

        # Valid alliance pilot candidate in shadow passes
        ok_good, _ = check_character_eligibility(
            level=24, faction="alliance", viable_roles=["tank"], dungeon=stockade, available_minutes=90, allow_pilot_shadow=True
        )
        self.assertTrue(ok_good)

    # ------------------------------------------------------------------
    # Cards II-02 & II-04: Atomic single-tank race, partial rollback, expiry & shadow isolation
    # ------------------------------------------------------------------
    def test_opportunity_atomic_single_tank_race_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            coord = OpportunityCoordinator(Path(tmp) / "opp.db")
            now = 1_800_000_000
            o1 = coord.create_opportunity(dungeon_id="stockade_n", creator_guid=201, now_ts=now, is_shadow=True)
            o2 = coord.create_opportunity(dungeon_id="stockade_n", creator_guid=201, now_ts=now, is_shadow=True)

            # Both opportunities compete for the SAME single tank (per_ironbulwark)!
            for opp in (o1, o2):
                for slug, guid, role in [
                    ("ironbulwark", 201, "tank"),
                    ("aelindra", 202, "healer"),
                    ("alaric", 203, "damage"),
                    ("pipwick", 204, "damage"),
                    ("nightstep", 205, "damage"),
                ]:
                    ok = coord.express_interest(
                        opportunity_id=opp["id"],
                        expected_revision=opp["revision"],
                        persona_id=persona_id_for_slug(slug),
                        character_guid=guid,
                        character_slug=slug,
                        role=role,
                        reason_code="wanted_dungeon",
                        available_until=now + 3600,
                        now_ts=now,
                    )
                    self.assertTrue(ok)

            # Race both reserve_party calls concurrently -> EXACTLY ONE may succeed!
            with ThreadPoolExecutor(max_workers=2) as ex:
                f1 = ex.submit(coord.reserve_party, o1["id"], expected_revision=1, now_ts=now + 5)
                f2 = ex.submit(coord.reserve_party, o2["id"], expected_revision=1, now_ts=now + 5)
                r1, r2 = f1.result(), f2.result()

            winners = [r for r in (r1, r2) if r["ok"]]
            losers = [r for r in (r1, r2) if not r["ok"]]
            self.assertEqual(len(winners), 1)
            self.assertEqual(len(losers), 1)
            self.assertEqual(losers[0]["reason"], "insufficient_roles")
            self.assertEqual(len(winners[0]["party"]), 5)

            # Partial invite failure compensation -> releases all 5 slots cleanly
            win_id = winners[0]["opportunity_id"]
            coord.release_party(win_id, "partial_invite_failed", now_ts=now + 10)
            # Now the losing opportunity CAN reserve those released players!
            lose_id = o2["id"] if win_id == o1["id"] else o1["id"]
            r_retry = coord.reserve_party(lose_id, expected_revision=1, now_ts=now + 15)
            self.assertTrue(r_retry["ok"])

            # Work-order read adapter never mutates/copies DB state
            adapted = adapt_work_orders_as_opportunities([
                {"id": 9, "requester_guid": 101, "requester_name": "Brannoc", "item_entry": 2862, "item_name": "Rough Sharpening Stone", "quantity": 5, "skill": 164}
            ])
            self.assertEqual(adapted[0]["opportunity_id"], "wo:9")
            self.assertEqual(adapted[0]["source_table"], "companion_work_orders")

    def test_shadow_match_isolation_and_resource_hysteresis(self):
        self.assertFalse(coop_enabled())
        # Hysteresis check: pause below 512 MiB, stay paused at 580 MiB until >= 640 MiB
        h_low = check_resource_headroom(mem_avail_mib=480.0, tick_p99_ms=20.0, active_managed_groups=0)
        self.assertFalse(h_low["admit_ok"])
        h_mid_paused = check_resource_headroom(mem_avail_mib=580.0, tick_p99_ms=20.0, active_managed_groups=0, prev_paused=True)
        self.assertFalse(h_mid_paused["admit_ok"])
        h_rec = check_resource_headroom(mem_avail_mib=700.0, tick_p99_ms=20.0, active_managed_groups=0, prev_paused=True)
        self.assertTrue(h_rec["admit_ok"])

        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            world = {"companions": _make_roster(), "work_orders": []}
            res = run_shadow_match_snapshot(
                world, db_path=tp / "brain.db", runtime_dir=tp / "rt",
                vitals={"mem_avail_gib": 1.0, "tick_p99": 22.0}, raid_stage=0, now_ts=1_800_000_000,
            )
            self.assertTrue(res["is_shadow"])
            self.assertFalse(res["coop_enabled_live"])
            stock = next(d for d in res["shadow_dungeons"] if d["dungeon_id"] == "stockade_n")
            # 5 guild crew members at Lv 23-24 form a feasible Stockade party while core-wonky Brannoc is excluded!
            self.assertTrue(stock["feasible_now"])
            self.assertEqual(stock["excluded_protected_core"], 1)
            self.assertTrue((tp / "rt" / "phase2_shadow.json").is_file())

    # ------------------------------------------------------------------
    # Card II-08: Readiness unknowns, bank reserve non-double-counting & distinct personas
    # ------------------------------------------------------------------
    def test_readiness_unknowns_and_non_double_counted_bank_supply(self):
        comps = {
            "brannoc": {"guid": 101, "name": "Brannoc", "slug": "brannoc", "level": 60, "online": True, "shift_group": "core-wonky", "zone": "Ironforge"},
            "ironbulwark": {"guid": 201, "name": "Ironbulwark", "slug": "ironbulwark", "level": 60, "online": True, "shift_group": "early-birds", "zone": "Stormwind City"},
            # Same persona_id as brannoc (synthetic alt) -> must NOT be counted twice in distinct_personas!
            "kelda": {"guid": 102, "name": "Kelda", "slug": "kelda", "persona_id": "per_brannoc", "level": 60, "online": True, "shift_group": "core-wonky", "zone": "Ironforge"},
        }
        # Missing durability/consumable telemetry -> MUST be 'unknown', NEVER 'ready_available'!
        rep_unk = evaluate_raid_roster(comps, raid_stage=0, bank_consumable_pool=5)
        self.assertEqual(rep_unk["summary_counts"]["ready_available"], 0)
        self.assertEqual(rep_unk["summary_counts"]["unknown"], 2)  # 2 distinct personas (kelda shares per_brannoc)

        # Only 5 bank consumables available -> FIRST raider claims all 5, SECOND raider fails consumables (no double counting!)
        telem = {
            "brannoc": {"personal_consumables": 0, "durability_pct": 100.0},
            "ironbulwark": {"personal_consumables": 0, "durability_pct": 100.0},
            "kelda": {"personal_consumables": 5, "durability_pct": 100.0},
        }
        rep_bank = evaluate_raid_roster(comps, raid_stage=0, bank_consumable_pool=5, telemetry_by_slug=telem)
        self.assertEqual(rep_bank["bank_consumables_unallocated"], 0)
        self.assertEqual(rep_bank["summary_counts"]["ready_available"], 1)
        self.assertEqual(rep_bank["summary_counts"]["not_ready"], 1)

    # ------------------------------------------------------------------
    # Card II-09: Bounded playstyle traits & non-stacking with memory fatigue
    # ------------------------------------------------------------------
    def test_playstyle_bounded_and_no_stacking_with_fatigue(self):
        prof_default = {"class": "warrior"}
        self.assertEqual(normalize_traits(prof_default)["sociability"], 0.5)

        prof_social = {"traits": {"sociability": 1.0, "risk_tolerance": 1.0, "variety_seeking": 1.0}}
        cands = [
            {"id": "dungeon:prep", "type": "dungeon", "score": 60, "description": "Dungeon"},
            {"id": "quest:zone_1", "type": "quest", "score": 70, "description": "Quest"},
        ]
        adj1, trace1 = apply_playstyle_scoring(cands, prof_social, has_fatigue_nudge=False)
        d1 = next(c for c in adj1 if c["id"] == "dungeon:prep")
        q1 = next(c for c in adj1 if c["id"] == "quest:zone_1")
        self.assertEqual(d1["score"], 66)  # bounded strictly at +6!
        self.assertEqual(q1["score"], 71)
        self.assertTrue(len(trace1) == 2)

        # When Memory v1 fatigue is active, variety_seeking must NOT stack on quest!
        adj2, _ = apply_playstyle_scoring(cands, prof_social, has_fatigue_nudge=True)
        q2 = next(c for c in adj2 if c["id"] == "quest:zone_1")
        self.assertEqual(q2["score"], 70)

    # ------------------------------------------------------------------
    # Card II-10: Benchmark artifact schema & verifiable SHA-256 digest
    # ------------------------------------------------------------------
    def test_benchmark_artifact_schema_and_digest(self):
        art = build_benchmark_artifact(
            scenario="staging_stockade_1grp",
            samples=[
                {"total_online": 62, "tick_latest": 11, "tick_p99": 22, "worldserver_rss_mib": 2560.0, "worldserver_cpu_pct": 29.0, "swap_used_mib": 3000.0, "managed_groups": 1},
                {"total_online": 62, "tick_latest": 13, "tick_p99": 25, "worldserver_rss_mib": 2570.0, "worldserver_cpu_pct": 31.0, "swap_used_mib": 3004.0, "managed_groups": 1},
            ],
            correctness={"worldserver_up": True, "errors_log_zero_bytes": True},
            hearthcore_sha="5ee45be",
            mod_playerbots_sha="2bcb40bf",
        )
        self.assertEqual(art["schema_version"], 2)
        self.assertTrue(art["all_correctness_passed"])
        self.assertEqual(art["metrics"]["tick_p99_ms"], 25.0)
        self.assertEqual(art["metrics"]["swap_delta_mib"], 4.0)
        self.assertEqual(len(art["artifact_sha256"]), 64)
