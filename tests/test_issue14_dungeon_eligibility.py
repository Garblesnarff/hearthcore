"""Comprehensive tests for Issue #14: actual schedules, identity, live role capability, and entry eligibility."""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hearthcore.dungeons import (
    DUNGEON_BY_ID,
    DUNGEON_BY_LFG_ID,
    LFG_ROLE_DAMAGE,
    LFG_ROLE_HEALER,
    LFG_ROLE_TANK,
    classify_level_band,
    compute_schedule_remaining_minutes,
    estimate_travel_and_prep_minutes,
    evaluate_live_role_capability,
    faction_from_race,
    lfg_mask_to_roles,
    roles_to_lfg_mask,
)
from hearthcore.identity import PersonaRegistry, persona_id_for_slug
from hearthcore.opportunities import OpportunityCoordinator, run_shadow_match_snapshot


def _verified_comp(
    slug: str,
    guid: int,
    *,
    class_id: int,
    race_id: int = 1,
    level: int = 24,
    spec_tab: int | None = 0,
    known_spells: list[int] | None = None,
    native_role_mask: int | None = None,
    has_shield: bool = False,
    has_main_hand: bool = True,
    broken_slots: int = 0,
    min_durability_pct: float = 100.0,
    shift_group: str = "early-birds",
    schedule: dict | None = None,
    locked_maps: list[int] | None = None,
    in_group: bool | None = False,
    in_combat: bool | None = False,
    in_lfg_queue: bool | None = False,
    in_lfg_queue_dungeons: list[int] | None = None,
    is_dead: bool | None = False,
    map_id: int = 0,
    zone_id: int = 1519,
    willing_dungeons: list[str] | dict | None = None,
    native_lfg_eligible: bool | None = True,
    native_lfg_lock_reason: str = "",
    xp_eligible: bool | None = True,
    online: bool = True,
    persona_id: str | None = None,
    available_minutes: int | None = None,
    telemetry_observed_at: int | None = None,
) -> dict:
    sched = schedule if schedule is not None else {
        "shift_group": shift_group,
        "start_hour": 0,
        "end_hour": 24,
        "days_mask": 127,
        "always_online": True,
    }
    comp: dict = {
        "guid": guid,
        "name": slug.capitalize(),
        "slug": slug,
        "level": level,
        "online": online,
        "shift_group": shift_group,
        "race_id": race_id,
        "class_id": class_id,
        "faction": faction_from_race(race_id),
        "map_id": map_id,
        "zone_id": zone_id,
        "schedule": sched,
        "locked_maps": [] if locked_maps is None else locked_maps,
        "in_group": in_group,
        "xp_eligible": xp_eligible,
        "willing_dungeons": [] if willing_dungeons is None else willing_dungeons,
        "live_telemetry": {
            "class_id": class_id,
            "spec_tab": spec_tab,
            "known_spells": [] if known_spells is None else list(known_spells),
            "equipped": {
                "has_main_hand": has_main_hand,
                "has_shield": has_shield,
                "broken_slots": broken_slots,
                "min_durability_pct": min_durability_pct,
            },
            "native_role_mask": native_role_mask,
            "native_lfg_eligible": native_lfg_eligible,
            "native_lfg_lock_reason": native_lfg_lock_reason,
            "in_combat": in_combat,
            "in_lfg_queue": in_lfg_queue,
            "in_lfg_queue_dungeons": [] if in_lfg_queue_dungeons is None else list(in_lfg_queue_dungeons),
            "is_dead": is_dead,
        },
    }
    if persona_id is not None:
        comp["persona_id"] = persona_id
    if available_minutes is not None:
        comp["available_minutes"] = available_minutes
    if telemetry_observed_at is not None:
        comp["telemetry_observed_at"] = telemetry_observed_at
    return comp


def _full_stockade_roster(*, willing: bool = True, dungeon: str = "stockade_n") -> dict[str, dict]:
    wd = [dungeon] if willing else []
    return {
        "ironbulwark": _verified_comp(
            "ironbulwark", 201, class_id=2, race_id=3, spec_tab=1,
            known_spells=[25780, 635], native_role_mask=LFG_ROLE_TANK, has_shield=True,
            willing_dungeons=wd,
        ),
        "aelindra": _verified_comp(
            "aelindra", 202, class_id=5, race_id=4, spec_tab=1,
            known_spells=[2061, 2054], native_role_mask=LFG_ROLE_HEALER,
            willing_dungeons=wd,
        ),
        "alaric": _verified_comp(
            "alaric", 203, class_id=8, race_id=1, spec_tab=2,
            native_role_mask=LFG_ROLE_DAMAGE, willing_dungeons=wd,
        ),
        "pipwick": _verified_comp(
            "pipwick", 204, class_id=9, race_id=7, spec_tab=0,
            native_role_mask=LFG_ROLE_DAMAGE, willing_dungeons=wd,
        ),
        "nightstep": _verified_comp(
            "nightstep", 205, class_id=4, race_id=4, spec_tab=1,
            native_role_mask=LFG_ROLE_DAMAGE, willing_dungeons=wd,
        ),
    }


def _file_sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Issue14DungeonEligibilityTests(unittest.TestCase):
    def test_lfg_queue_ids_and_legal_vs_recommended_level_bands(self):
        stock = DUNGEON_BY_ID["stockade_n"]
        dm = DUNGEON_BY_ID["deadmines_n"]
        self.assertEqual(stock.lfg_dungeon_id, 12)
        self.assertEqual(dm.lfg_dungeon_id, 6)
        self.assertIs(DUNGEON_BY_LFG_ID[12], stock)
        self.assertIs(DUNGEON_BY_LFG_ID[6], dm)

        # Deadmines Lv 12: legal portal entry (>=10), NOT LFG queue eligible (<15), NOT recommended (<17)
        b12 = classify_level_band(12, dm)
        self.assertTrue(b12["legal_entry"])
        self.assertFalse(b12["lfg_queue_eligible"])
        self.assertFalse(b12["in_recommended_band"])
        self.assertEqual(b12["band_status"], "legal_below_lfg_queue")

        # Stockade Lv 17: legal & LFG queueable (15..34), below recommended (22..30)
        b17 = classify_level_band(17, stock)
        self.assertTrue(b17["legal_entry"])
        self.assertTrue(b17["lfg_queue_eligible"])
        self.assertFalse(b17["in_recommended_band"])
        self.assertEqual(b17["band_status"], "lfg_under_recommended")

        # Stockade Lv 25: recommended
        b25 = classify_level_band(25, stock)
        self.assertTrue(b25["in_recommended_band"])
        self.assertEqual(b25["band_status"], "recommended")

        # Role mask roundtrip
        mask = roles_to_lfg_mask(["tank", "damage"])
        self.assertEqual(mask, LFG_ROLE_TANK | LFG_ROLE_DAMAGE)
        self.assertEqual(lfg_mask_to_roles(mask), ["tank", "damage"])

    def test_actual_schedule_remaining_minutes_daytime_overnight_and_missing(self):
        rem, reason = compute_schedule_remaining_minutes(None, now_ts=1_800_000_000)
        self.assertIsNone(rem)
        self.assertEqual(reason, "missing_schedule_telemetry")

        wed_0930 = int(datetime(2026, 11, 25, 9, 30, tzinfo=timezone.utc).timestamp())
        sched_early = {"start_hour": 5, "end_hour": 10, "days_mask": 127, "always_online": False}
        rem_e, st_e = compute_schedule_remaining_minutes(sched_early, now_ts=wed_0930, tz=timezone.utc)
        self.assertEqual(rem_e, 30)
        self.assertEqual(st_e, "in_scheduled_shift")

        # Overnight shift 22:00 -> 04:00 active ONLY on Tuesday (bit 1<<2 = 4)
        wed_0215 = int(datetime(2026, 11, 25, 2, 15, tzinfo=timezone.utc).timestamp())
        sched_night_tue = {"start_hour": 22, "end_hour": 4, "days_mask": (1 << 2), "always_online": False}
        rem_n, st_n = compute_schedule_remaining_minutes(sched_night_tue, now_ts=wed_0215, tz=timezone.utc)
        self.assertEqual(rem_n, 105)
        self.assertEqual(st_n, "in_overnight_shift")

        wed_2300 = int(datetime(2026, 11, 25, 23, 0, tzinfo=timezone.utc).timestamp())
        rem_out, st_out = compute_schedule_remaining_minutes(sched_night_tue, now_ts=wed_2300, tz=timezone.utc)
        self.assertEqual(rem_out, 0)
        self.assertEqual(st_out, "outside_scheduled_shift")

    def test_live_role_capability_blocks_profile_fallback_and_baseline_spell_loopholes(self):
        # Missing live telemetry fails closed
        r_unk = evaluate_live_role_capability({"class": "warrior", "primary_role": "tank"}, level=24, live_telemetry=None)
        self.assertFalse(r_unk["known"])
        self.assertEqual(r_unk["viable_roles"], [])

        # P0-2 regression: bridge-shaped Lv24 Ret Paladin with spec_tab=None, native_role_mask=None, baseline Holy Light (635)
        # MUST fail closed (`known=False`, `missing_live_spec`) — NEVER infer healer or profile role!
        r_ret_unk = evaluate_live_role_capability(
            {"class": "paladin", "primary_role": "damage", "primary_spec": "Retribution"},
            level=24,
            live_telemetry={
                "class_id": 2,
                "spec_tab": None,
                "native_role_mask": None,
                "known_spells": [635],
                "equipped": {"has_main_hand": True, "has_shield": False, "broken_slots": 0},
            },
        )
        self.assertFalse(r_ret_unk["known"])
        self.assertEqual(r_ret_unk["viable_roles"], [])
        self.assertIn("missing_live_spec", r_ret_unk["reasons"])

        # P0-2 regression: Lv15 Paladin with shield and NO Righteous Fury CANNOT tank!
        r_pal15 = evaluate_live_role_capability(
            {"class": "paladin", "primary_role": "tank"},
            level=15,
            live_telemetry={
                "class_id": 2,
                "spec_tab": 1,
                "native_role_mask": LFG_ROLE_TANK | LFG_ROLE_DAMAGE,
                "known_spells": [635],  # no 25780 Righteous Fury!
                "equipped": {"has_main_hand": True, "has_shield": True, "broken_slots": 0},
            },
        )
        self.assertNotIn("tank", r_pal15["viable_roles"])
        self.assertIn("tank:missing_paladin_tank_spells", r_pal15["reasons"])

        # P0-2 regression: Lv24 Shadow Priest with baseline Lesser Heal (2050) and native_role_mask=DAMAGE
        # MUST NOT become a flex healer!
        r_spriest = evaluate_live_role_capability(
            {"class": "priest", "primary_role": "healer"},
            level=24,
            live_telemetry={
                "class_id": 5,
                "spec_tab": 2,
                "native_role_mask": LFG_ROLE_DAMAGE,
                "known_spells": [2050],
                "equipped": {"has_main_hand": True, "has_shield": False, "broken_slots": 0},
            },
        )
        self.assertEqual(r_spriest["primary_role"], "damage")
        self.assertEqual(r_spriest["flex_roles"], [])

        # Mage can NEVER tank or heal even if corrupted native_role_mask claims TANK
        r_mage = evaluate_live_role_capability(
            {"class": "mage"},
            level=24,
            live_telemetry={"class_id": 8, "native_role_mask": LFG_ROLE_TANK},
        )
        self.assertTrue(r_mage["known"])
        self.assertEqual(r_mage["viable_roles"], [])
        self.assertIn("native_role_mask_illegal_for_class", r_mage["reasons"])

        # Protection Warrior WITHOUT shield cannot tank
        r_noshield = evaluate_live_role_capability(
            {"class": "warrior", "primary_role": "tank"},
            level=24,
            live_telemetry={
                "class_id": 1,
                "spec_tab": 2,
                "known_spells": [71, 355],
                "equipped": {"has_main_hand": True, "has_shield": False, "broken_slots": 0},
                "native_role_mask": LFG_ROLE_TANK,
            },
        )
        self.assertNotIn("tank", r_noshield["viable_roles"])
        self.assertIn("tank:missing_tank_shield", r_noshield["reasons"])

        # Cross-continent travel + broken gear prep needs
        stock = DUNGEON_BY_ID["stockade_n"]
        tp = estimate_travel_and_prep_minutes(
            stock,
            current_map_id=1,
            current_zone_id=331,
            equipped={"has_main_hand": True, "has_shield": True, "broken_slots": 1, "min_durability_pct": 0.0},
        )
        self.assertGreaterEqual(tp["travel_minutes"], 25)
        self.assertIn("broken_gear_needs_repair", tp["blocking_prep_needs"])
        self.assertIn("cross_continent_travel", tp["prep_needs"])

    def test_p0_1_bridge_shaped_unknown_live_state_fails_closed(self):
        """P0-1 regression: bridge-shaped companions with None for combat/queue/alive/native_lfg MUST NOT claim feasible!"""
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            # Test each unknown live-state field individually on an otherwise complete willing roster
            for label, field_key, expected_reason in (
                ("unknown_combat", "in_combat", "unknown_combat_status"),
                ("unknown_queue", "in_lfg_queue", "unknown_lfg_queue_status"),
                ("unknown_alive", "is_dead", "unknown_alive_status"),
                ("unknown_native_lfg", "native_lfg_eligible", "unknown_native_lfg_eligibility"),
                ("unknown_equipment", "equipped", "unknown_equipment_telemetry"),
            ):
                roster = _full_stockade_roster(willing=True)
                roster["alaric"]["live_telemetry"][field_key] = None
                res = run_shadow_match_snapshot(
                    {"companions": roster, "work_orders": [], "telemetry_observed_at": now},
                    db_path=tp / "none.db",
                    runtime_dir=tp / f"rt_{label}",
                    vitals={"mem_avail_gib": 1.0, "tick_p99": 20.0},
                    now_ts=now,
                )
                stock = next(d for d in res["shadow_dungeons"] if d["dungeon_id"] == "stockade_n")
                self.assertFalse(stock["feasible_now"], f"{label} MUST NOT claim feasible!")
                self.assertGreaterEqual(
                    stock["excluded_reasons"].get(expected_reason, 0), 1,
                    f"Expected {expected_reason} in {stock['excluded_reasons']}",
                )
                self.assertGreaterEqual(stock["unknown_telemetry_count"], 1)

            # Full verified roster with ZERO volunteers -> roster_feasible_now=True, feasible_now=False!
            roster_no_vol = _full_stockade_roster(willing=False)
            res_nv = run_shadow_match_snapshot(
                {"companions": roster_no_vol, "work_orders": [], "telemetry_observed_at": now},
                db_path=tp / "none.db",
                runtime_dir=tp / "rt_novol",
                vitals={"mem_avail_gib": 1.0, "tick_p99": 20.0},
                now_ts=now,
            )
            stock_nv = next(d for d in res_nv["shadow_dungeons"] if d["dungeon_id"] == "stockade_n")
            self.assertEqual(stock_nv["stages"]["eligible"]["count"], 5)
            self.assertEqual(stock_nv["stages"]["available"]["count"], 5)
            self.assertEqual(stock_nv["stages"]["willing"]["count"], 0)
            self.assertEqual(stock_nv["stages"]["queued"]["count"], 0)
            self.assertEqual(stock_nv["stages"]["matched"]["count"], 0)
            self.assertTrue(stock_nv["roster_feasible_now"])
            self.assertFalse(stock_nv["feasible_now"])

    def test_baseline_control_and_every_ac1_rejection_reason(self):
        now = int(datetime(2026, 11, 25, 9, 30, tzinfo=timezone.utc).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            # P2 control test: unmodified baseline MUST be feasible_now=True!
            base_res = run_shadow_match_snapshot(
                {"companions": _full_stockade_roster(willing=True), "work_orders": [], "telemetry_observed_at": now},
                db_path=tp / "none.db",
                runtime_dir=tp / "rt_base",
                vitals={"mem_avail_gib": 1.0, "tick_p99": 20.0},
                now_ts=now,
                tz=timezone.utc,
            )
            base_stock = next(d for d in base_res["shadow_dungeons"] if d["dungeon_id"] == "stockade_n")
            self.assertTrue(base_stock["feasible_now"], f"Baseline control must be feasible: {base_stock}")

            cases = [
                ("wrong_faction_horde", lambda r: r["ironbulwark"].update({"race_id": 2, "faction": "horde"}), "faction_mismatch"),
                ("locked_map", lambda r: r["ironbulwark"].update({"locked_maps": [34]}), "instance_locked"),
                ("unknown_lockouts", lambda r: r["ironbulwark"].update({"locked_maps": None}), "unknown_lockout_telemetry"),
                ("short_shift_30m", lambda r: r["ironbulwark"].update({
                    "schedule": {"start_hour": 5, "end_hour": 10, "days_mask": 127, "always_online": False},
                }), "insufficient_schedule_window"),
                ("in_combat", lambda r: r["aelindra"]["live_telemetry"].update({"in_combat": True}), "in_combat"),
                ("already_in_group", lambda r: r["alaric"].update({"in_group": True}), "already_in_group"),
                ("native_lfg_ineligible", lambda r: r["pipwick"]["live_telemetry"].update({
                    "native_lfg_eligible": False, "native_lfg_lock_reason": "deserter",
                }), "native_lfg_ineligible"),
                ("broken_gear", lambda r: r["nightstep"]["live_telemetry"]["equipped"].update({"broken_slots": 1}), "prep_required"),
                ("level_lead_blocked", lambda r: r["nightstep"].update({"xp_eligible": False}), "level_lead_cap_blocked"),
                ("offline", lambda r: r["nightstep"].update({"online": False}), "offline"),
                ("protected_core", lambda r: r["ironbulwark"].update({"shift_group": "core-wonky"}), "protected_core_excluded"),
            ]
            for label, mutate, expected_key in cases:
                roster = _full_stockade_roster(willing=True)
                mutate(roster)
                res = run_shadow_match_snapshot(
                    {"companions": roster, "work_orders": [], "telemetry_observed_at": now},
                    db_path=tp / "none.db",
                    runtime_dir=tp / f"rt_{label}",
                    vitals={"mem_avail_gib": 1.0, "tick_p99": 20.0},
                    now_ts=now,
                    tz=timezone.utc,
                )
                stock = next(d for d in res["shadow_dungeons"] if d["dungeon_id"] == "stockade_n")
                self.assertFalse(stock["feasible_now"], f"Case {label} MUST NOT be feasible!")
                self.assertGreaterEqual(
                    stock["excluded_reasons"].get(expected_key, 0), 1,
                    f"Case {label} expected {expected_key} in {stock['excluded_reasons']}",
                )

            # Stale telemetry (> max_telemetry_age_sec) MUST reject everyone
            res_stale = run_shadow_match_snapshot(
                {"companions": _full_stockade_roster(willing=True), "work_orders": [], "telemetry_observed_at": now - 600},
                db_path=tp / "none.db",
                runtime_dir=tp / "rt_stale",
                vitals={"mem_avail_gib": 1.0, "tick_p99": 20.0},
                now_ts=now,
            )
            stock_stale = next(d for d in res_stale["shadow_dungeons"] if d["dungeon_id"] == "stockade_n")
            self.assertFalse(stock_stale["feasible_now"])
            self.assertEqual(stock_stale["stages"]["eligible"]["count"], 0)
            self.assertGreaterEqual(stock_stale["excluded_reasons"].get("stale_or_missing_telemetry", 0), 5)

    def test_p0_3_live_coordinator_admit_candidate_enforces_ac1(self):
        """P0-3 regression: OpportunityCoordinator.admit_candidate rejects wrong role, Horde, locked, short shift, core, disabled."""
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp:
            db_p = Path(tmp) / "opp.db"
            reg = PersonaRegistry(db_p)
            reg.sync_from_companions({
                "ironbulwark": {"guid": 201, "slug": "ironbulwark", "race_id": 3, "class_id": 2, "shift_group": "early-birds", "online": True},
            }, realm_id=1, now_ts=now)
            pid_iron = reg.persona_for_guid(201).persona_id
            coord = OpportunityCoordinator(db_p)
            opp_shadow = coord.create_opportunity(dungeon_id="stockade_n", creator_guid=201, is_shadow=True, now_ts=now)
            opp_live = coord.create_opportunity(dungeon_id="stockade_n", creator_guid=201, is_shadow=False, now_ts=now)

            good_tank = _verified_comp(
                "ironbulwark", 201, class_id=2, race_id=3, spec_tab=1,
                known_spells=[25780, 635], native_role_mask=LFG_ROLE_TANK, has_shield=True,
                telemetry_observed_at=now,
            )
            # 1. Live admission rejected when HEARTHCORE_COOP_DUNGEONS does not enable stockade_n!
            r_live = coord.admit_candidate(
                opportunity_id=opp_live["id"], expected_revision=1,
                persona_id=pid_iron, character_guid=201,
                character_slug="ironbulwark", role="tank", reason_code="vol",
                candidate=good_tank, now_ts=now,
            )
            self.assertFalse(r_live["ok"])
            self.assertIn("dungeon_disabled", r_live["reason"])

            # 2. Shadow admission rejects wrong role (tank trying to join as healer when native_role_mask=TANK)
            r_role = coord.admit_candidate(
                opportunity_id=opp_shadow["id"], expected_revision=1,
                persona_id=pid_iron, character_guid=201,
                character_slug="ironbulwark", role="healer", reason_code="vol",
                candidate=good_tank, now_ts=now,
            )
            self.assertFalse(r_role["ok"])
            self.assertIn("wrong_role", r_role["reason"])

            # 3. Rejects Horde, locked map, short shift (30m), and protected core
            bad_variants = [
                ("horde", {**good_tank, "race_id": 2, "faction": "horde"}, "faction_mismatch"),
                ("locked", {**good_tank, "locked_maps": [34]}, "instance_locked"),
                ("short_shift", {**good_tank, "available_minutes": 30}, "insufficient_schedule_window"),
                ("core", {**good_tank, "shift_group": "core-wonky"}, "protected_core_excluded"),
            ]
            for label, cand, expected in bad_variants:
                res = coord.admit_candidate(
                    opportunity_id=opp_shadow["id"], expected_revision=1,
                    persona_id=pid_iron, character_guid=201,
                    character_slug="ironbulwark", role="tank", reason_code="vol",
                    candidate=cand, now_ts=now,
                )
                self.assertFalse(res["ok"], f"{label} must be rejected")
                self.assertIn(expected, res["reason"])

            # 4. Valid candidate succeeds!
            r_ok = coord.admit_candidate(
                opportunity_id=opp_shadow["id"], expected_revision=1,
                persona_id=pid_iron, character_guid=201,
                character_slug="ironbulwark", role="tank", reason_code="vol",
                candidate=good_tank, now_ts=now,
            )
            self.assertTrue(r_ok["ok"])

    def test_p1_volunteer_role_respect_unnested_funnel_party_shift_and_mixed_faction(self):
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)

            # P1-1: Shadow Priest who CAN flex heal, but explicitly volunteered ONLY as "damage" -> MUST NOT be assigned healer!
            roster_vr = _full_stockade_roster(willing=True)
            roster_vr["aelindra"] = _verified_comp(
                "aelindra", 202, class_id=5, race_id=4, spec_tab=2,
                known_spells=[2061, 2054], native_role_mask=LFG_ROLE_DAMAGE | LFG_ROLE_HEALER,
                willing_dungeons={"stockade_n": ["damage"]},  # Explicitly chose damage ONLY!
            )
            res_vr = run_shadow_match_snapshot(
                {"companions": roster_vr, "work_orders": [], "telemetry_observed_at": now},
                db_path=tp / "none.db", runtime_dir=tp / "rt_vr",
                vitals={"mem_avail_gib": 1.0, "tick_p99": 20.0}, now_ts=now,
            )
            stock_vr = next(d for d in res_vr["shadow_dungeons"] if d["dungeon_id"] == "stockade_n")
            self.assertFalse(stock_vr["feasible_now"], "Volunteer who chose damage ONLY must NEVER be forced into healer!")
            self.assertEqual(stock_vr["matched_counts"]["healer"], 0)

            # P1-3: Party-level shift window! Member A (alaric) has 60m left & 3m travel (individually passes 30+3+10=43m),
            # while Member B (ironbulwark) is cross-continent (Kalimdor map_id=1 -> 25m travel)!
            # Party needs 30 + 25 + 10 = 65m -> alaric's 60m is NOT enough for the joint party!
            roster_ps = _full_stockade_roster(willing=True)
            roster_ps["alaric"]["available_minutes"] = 60
            roster_ps["ironbulwark"]["map_id"] = 1  # Kalimdor -> 25m travel!
            roster_ps["ironbulwark"]["available_minutes"] = 120
            res_ps = run_shadow_match_snapshot(
                {"companions": roster_ps, "work_orders": [], "telemetry_observed_at": now},
                db_path=tp / "none.db", runtime_dir=tp / "rt_ps",
                vitals={"mem_avail_gib": 1.0, "tick_p99": 20.0}, now_ts=now,
            )
            stock_ps = next(d for d in res_ps["shadow_dungeons"] if d["dungeon_id"] == "stockade_n")
            self.assertFalse(stock_ps["feasible_now"], "Party shift window must account for slowest traveler!")
            self.assertGreaterEqual(stock_ps["excluded_reasons"].get("party_shift_window_insufficient", 0), 1)

            # P2: Mixed-faction Deadmines (faction='both') MUST NOT form a party mixing Alliance and Horde!
            roster_mf = _full_stockade_roster(willing=True, dungeon="deadmines_n")
            roster_mf["nightstep"]["race_id"] = 2
            roster_mf["nightstep"]["faction"] = "horde"
            res_mf = run_shadow_match_snapshot(
                {"companions": roster_mf, "work_orders": [], "telemetry_observed_at": now},
                db_path=tp / "none.db", runtime_dir=tp / "rt_mf",
                vitals={"mem_avail_gib": 1.0, "tick_p99": 20.0}, now_ts=now,
            )
            dm_mf = next(d for d in res_mf["shadow_dungeons"] if d["dungeon_id"] == "deadmines_n")
            self.assertFalse(dm_mf["feasible_now"], "Mixed Alliance/Horde party MUST NOT be matched!")
            self.assertEqual(dm_mf["stages"]["matched"]["count"], 4)

    def test_flexible_roles_dedup_unnested_queued_readonly_hash_and_fail_closed_db(self):
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            db_p = tp / "brain.db"

            reg = PersonaRegistry(db_p)
            reg.sync_from_companions({
                "ironbulwark": {"guid": 201, "shift_group": "early-birds"},
                "aelindra": {"guid": 202, "shift_group": "early-birds"},
                "alaric": {"guid": 203, "shift_group": "early-birds"},
                "pipwick": {"guid": 204, "shift_group": "early-birds"},
                "nightstep": {"guid": 205, "shift_group": "prime-time"},
                "kraghammer": {"guid": 206, "shift_group": "prime-time"},
                "solara": {"guid": 207, "shift_group": "prime-time"},
            }, now_ts=now)
            pid_iron = reg.persona_for_guid(201).persona_id
            pid_ael = reg.persona_for_guid(202).persona_id
            pid_alaric = reg.persona_for_guid(203).persona_id
            pid_pip = reg.persona_for_guid(204).persona_id
            pid_krag = reg.persona_for_guid(206).persona_id
            pid_sol = reg.persona_for_guid(207).persona_id
            # Active alt `vespera` under same persona as `pipwick`
            reg.register_alt(
                persona_id=pid_pip,
                slug="vespera",
                character_guid=299,
                slot_kind="alt",
                now_ts=now,
            )
            # P1-6: Inactive alt `lyraeth` (active=0) under same persona as `alaric` -> MUST NOT mint new persona!
            reg.register_alt(
                persona_id=pid_alaric,
                slug="lyraeth",
                character_guid=298,
                slot_kind="alt",
                now_ts=now,
            )
            with sqlite3.connect(db_p) as conn:
                conn.execute("UPDATE persona_characters SET active = 0 WHERE character_guid = 298")
                conn.commit()

            # Live opportunity: ironbulwark (tank), aelindra (healer), and kraghammer (damage, in_combat -> P1-2 test!)
            # Plus an EXPIRED interest (P1-5 test!) for `solara` that MUST NOT count as queued/willing!
            coord = OpportunityCoordinator(db_p)
            opp = coord.create_opportunity(dungeon_id="stockade_n", creator_guid=201, is_shadow=False, now_ts=now)
            coord.express_interest(
                opportunity_id=opp["id"], expected_revision=1,
                persona_id=pid_iron, character_guid=201,
                character_slug="ironbulwark", role="tank", reason_code="vol",
                available_until=now + 3600, now_ts=now,
            )
            coord.express_interest(
                opportunity_id=opp["id"], expected_revision=1,
                persona_id=pid_ael, character_guid=202,
                character_slug="aelindra", role="healer", reason_code="vol",
                available_until=now + 3600, now_ts=now,
            )
            coord.express_interest(
                opportunity_id=opp["id"], expected_revision=1,
                persona_id=pid_krag, character_guid=206,
                character_slug="kraghammer", role="damage", reason_code="vol",
                available_until=now + 3600, now_ts=now,
            )
            # Insert an expired interest row directly (available_until <= now) to test P1-5 TTL filter
            with sqlite3.connect(db_p) as conn:
                conn.execute(
                    "INSERT INTO opportunity_interests (opportunity_id, opportunity_revision, persona_id, realm_id, "
                    "character_guid, incarnation, character_slug, role, state, reason_code, available_until, "
                    "reserved_until, confirmed_at, ack_token, created_at, updated_at) "
                    "VALUES (?, 1, ?, 1, 207, 1, 'solara', 'damage', 'interested', 'old', ?, 0, 0, '', ?, ?)",
                    (opp["id"], pid_sol, now - 10, now - 500, now - 500),
                )
                conn.commit()

            # Hash brain.db BEFORE shadow pass to prove strict read-only isolation!
            sha_before = _file_sha256(db_p)

            roster = {
                "ironbulwark": _verified_comp(
                    "ironbulwark", 201, class_id=2, race_id=3, spec_tab=1,
                    known_spells=[25780, 635], native_role_mask=LFG_ROLE_TANK | LFG_ROLE_DAMAGE, has_shield=True,
                ),
                "aelindra": _verified_comp(
                    "aelindra", 202, class_id=5, race_id=4, spec_tab=2,
                    known_spells=[2061, 2054], native_role_mask=LFG_ROLE_DAMAGE | LFG_ROLE_HEALER,
                ),
                "alaric": _verified_comp(
                    "alaric", 203, class_id=8, race_id=1, spec_tab=2,
                    native_role_mask=LFG_ROLE_DAMAGE, willing_dungeons=["stockade_n"],
                ),
                "lyraeth": _verified_comp(
                    "lyraeth", 298, class_id=8, race_id=1, spec_tab=2,
                    native_role_mask=LFG_ROLE_DAMAGE, willing_dungeons=["stockade_n"],
                ),
                "pipwick": _verified_comp(
                    "pipwick", 204, class_id=9, race_id=7, spec_tab=0,
                    native_role_mask=LFG_ROLE_DAMAGE, willing_dungeons=["stockade_n"],
                ),
                "vespera": _verified_comp(
                    "vespera", 299, class_id=9, race_id=1, spec_tab=0,
                    native_role_mask=LFG_ROLE_DAMAGE, willing_dungeons=["stockade_n"],
                ),
                "nightstep": _verified_comp(
                    "nightstep", 205, class_id=4, race_id=4, spec_tab=1,
                    native_role_mask=LFG_ROLE_DAMAGE, willing_dungeons=["stockade_n"],
                ),
                "kraghammer": _verified_comp(
                    "kraghammer", 206, class_id=1, race_id=3, spec_tab=0,
                    known_spells=[], native_role_mask=LFG_ROLE_DAMAGE,
                    in_combat=True,  # Queued in opp AND in_combat -> P1-2 un-nested check!
                ),
                "solara": _verified_comp(
                    "solara", 207, class_id=8, race_id=1, spec_tab=2,
                    native_role_mask=LFG_ROLE_DAMAGE,  # Expired interest -> MUST NOT be willing or queued!
                ),
            }

            res = run_shadow_match_snapshot(
                {"companions": roster, "work_orders": [], "telemetry_observed_at": now},
                db_path=db_p,
                runtime_dir=tp / "rt",
                vitals={"mem_avail_gib": 1.0, "tick_p99": 20.0},
                now_ts=now,
            )
            self.assertEqual(_file_sha256(db_p), sha_before, "Shadow pass MUST NEVER mutate brain.db!")

            stock = next(d for d in res["shadow_dungeons"] if d["dungeon_id"] == "stockade_n")
            self.assertTrue(res["db_ok"])
            self.assertEqual(stock["excluded_reasons"].get("duplicate_persona"), 1)
            self.assertEqual(stock["excluded_reasons"].get("inactive_persona_character"), 1)

            # 7 distinct eligible personas (ironbulwark, aelindra, alaric, pipwick, nightstep, kraghammer, solara)
            self.assertEqual(stock["stages"]["eligible"]["count"], 7)
            # 6 available (kraghammer excluded for in_combat)
            self.assertEqual(stock["stages"]["available"]["count"], 6)
            # P1-2 un-nested check: kraghammer STILL appears in `willing` (6) and `queued` (3), NOT in `available`!
            self.assertEqual(stock["stages"]["willing"]["count"], 6)
            self.assertIn(pid_krag, stock["stages"]["queued"]["personas"])
            self.assertNotIn(pid_krag, stock["stages"]["available"]["personas"])
            # P1-5 expired interest check: solara is NOT in willing or queued!
            self.assertNotIn(pid_sol, stock["stages"]["willing"]["personas"])
            self.assertNotIn(pid_sol, stock["stages"]["queued"]["personas"])
            # Matched strictly from willing_and_available (5), with aelindra flexing cleanly into healer!
            self.assertTrue(stock["feasible_now"])
            self.assertEqual(stock["stages"]["matched"]["count"], 5)
            self.assertEqual(stock["matched_counts"], {"tank": 1, "healer": 1, "damage": 3})
            self.assertEqual(len(set(stock["stages"]["matched"]["personas"])), 5)
            self.assertEqual(len(stock["used_flex_roles"]), 1)
            self.assertEqual(stock["used_flex_roles"][0]["slug"], "aelindra")
            self.assertEqual(stock["used_flex_roles"][0]["assigned_role"], "healer")

            # P1-4: Corrupt / unreadable brain.db MUST fail closed (`db_ok=False`, `feasible_now=False`)!
            corrupt_db = tp / "corrupt.db"
            corrupt_db.write_bytes(b"not a valid sqlite database")
            res_bad = run_shadow_match_snapshot(
                {"companions": _full_stockade_roster(willing=True), "work_orders": [], "telemetry_observed_at": now},
                db_path=corrupt_db,
                runtime_dir=tp / "rt_bad",
                vitals={"mem_avail_gib": 1.0, "tick_p99": 20.0},
                now_ts=now,
            )
            self.assertFalse(res_bad["db_ok"])
            self.assertTrue(res_bad["db_errors"])
            stock_bad = next(d for d in res_bad["shadow_dungeons"] if d["dungeon_id"] == "stockade_n")
            self.assertFalse(stock_bad["feasible_now"])
            self.assertFalse(stock_bad["roster_feasible_now"])


if __name__ == "__main__":
    unittest.main()
