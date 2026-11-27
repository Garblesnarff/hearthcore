"""Comprehensive tests for Issue #16: specific encounter/roster raid readiness requirements."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hearthcore.dungeons import LFG_ROLE_DAMAGE, LFG_ROLE_HEALER, LFG_ROLE_TANK
from hearthcore.identity import PersonaRegistry, persona_id_for_slug
from hearthcore.readiness import (
    RAID_BY_ID,
    evaluate_raid_roster,
    fetch_raid_readiness_telemetry,
    load_owner_attestations,
    resolve_raid_spec,
)


def _raider(
    slug: str,
    guid: int,
    *,
    class_id: int,
    spec_tab: int,
    native_role_mask: int,
    level: int = 70,
    known_spells: list[int] | None = None,
    has_shield: bool = False,
    online: bool = True,
    in_group: bool = False,
    group_id: int = 0,
    avg_ilvl: float = 110.0,
    filled_slots: int = 17,
    expected_slots: int = 17,
    durability_pct: float = 100.0,
    personal_consumables: int = 5,
    tactics_verified: bool | None = True,
    encounter_capabilities: dict | None = None,
    locked_instances: list | None = None,
    owned_items: list[int] | None = None,
    persona_id: str | None = None,
    now_ts: int = 1_800_000_000,
    live_observed_at: int | None = None,
    sql_observed_at: int | None = None,
) -> tuple[dict, dict]:
    comp: dict = {
        "guid": guid,
        "name": slug.capitalize(),
        "slug": slug,
        "level": level,
        "online": online,
        "shift_group": "early-birds",
        "zone": "Shattrath City",
        "in_group": in_group,
        "group_id": group_id,
        "locked_instances": [] if locked_instances is None else list(locked_instances),
        "telemetry_observed_at": now_ts if live_observed_at is None else live_observed_at,
    }
    if persona_id is not None:
        comp["persona_id"] = persona_id
    telem: dict = {
        "personal_consumables": personal_consumables,
        "durability_pct": durability_pct,
        "avg_ilvl": avg_ilvl,
        "filled_slots": filled_slots,
        "expected_slots": expected_slots,
        "tactics_verified": tactics_verified,
        "encounter_capabilities": encounter_capabilities,
        "owned_items": owned_items,
        "live_observed_at": now_ts if live_observed_at is None else live_observed_at,
        "sql_observed_at": now_ts if sql_observed_at is None else sql_observed_at,
        "live_telemetry": {
            "class_id": class_id,
            "spec_tab": spec_tab,
            "known_spells": [] if known_spells is None else list(known_spells),
            "equipped": {
                "has_main_hand": True,
                "has_shield": has_shield,
                "broken_slots": 0,
                "min_durability_pct": durability_pct,
                "avg_ilvl": avg_ilvl,
                "filled_slots": filled_slots,
                "expected_slots": expected_slots,
            },
            "native_role_mask": native_role_mask,
            "is_dead": False,
        },
    }
    return comp, telem


class Issue16RaidReadinessTests(unittest.TestCase):
    def test_separate_raid_definitions_sizes_stages_and_fail_closed_stage_resolution(self):
        expected = {
            "zg_20": (309, 0, 20, 0),
            "aq20_20": (509, 0, 20, 0),
            "mc_40": (409, 0, 40, 0),
            "bwl_40": (469, 0, 40, 0),
            "aq40_40": (531, 0, 40, 0),
            "karazhan_10": (532, 0, 10, 1),
            "gruul_25": (565, 0, 25, 1),
            "naxx_10": (533, 0, 10, 2),
            "naxx_25": (533, 1, 25, 2),
            "onyxia_10": (249, 0, 10, 2),
            "onyxia_25": (249, 1, 25, 2),
        }
        for rid, (mid, diff, size, stage) in expected.items():
            self.assertIn(rid, RAID_BY_ID)
            spec = RAID_BY_ID[rid]
            self.assertEqual(spec.map_id, mid)
            self.assertEqual(spec.difficulty, diff)
            self.assertEqual(spec.raid_size, size)
            self.assertEqual(spec.raid_stage_min, stage)
            self.assertEqual(sum(spec.roles_dict().values()), size)

        # Fail-closed resolution: unknown raid, missing both raid & stage, or unknown stage MUST raise!
        with self.assertRaises(ValueError):
            resolve_raid_spec("nonexistent_raid")
        with self.assertRaises(ValueError):
            resolve_raid_spec(None, raid_stage=None)
        with self.assertRaises(ValueError):
            resolve_raid_spec(None, raid_stage=99)

        # Stage defaults for 0, 1, 2
        self.assertEqual(resolve_raid_spec(None, raid_stage=0).raid_id, "mc_40")
        self.assertEqual(resolve_raid_spec(None, raid_stage=1).raid_id, "karazhan_10")
        self.assertEqual(resolve_raid_spec(None, raid_stage=2).raid_id, "naxx_10")

    def test_unknown_gear_empty_slots_access_tactics_per_source_freshness_and_lockout_expiry(self):
        now = 1_800_000_000
        c_base, t_base = _raider(
            "ironbulwark", 201, class_id=2, spec_tab=1, native_role_mask=LFG_ROLE_TANK,
            level=60, known_spells=[25780], has_shield=True, avg_ilvl=68.0,
            personal_consumables=8, owned_items=[15138], now_ts=now,
        )
        rep_ok = evaluate_raid_roster(
            {"ironbulwark": c_base}, raid_id="bwl_40", raid_stage=0,
            telemetry_by_slug={"ironbulwark": t_base}, allow_no_persona_db=True, now_ts=now,
        )
        self.assertEqual(rep_ok["summary_counts_obtained"]["ready_available"], 1)
        self.assertTrue(rep_ok["owner_initiated_only"])
        self.assertFalse(rep_ok["auto_raid_enabled"])
        self.assertTrue(rep_ok["read_only"])

        def _run_mutated(mutate_c, mutate_t, *, stage=0, access_db_ok=True):
            c = dict(c_base)
            t = {
                **t_base,
                "live_telemetry": {**t_base["live_telemetry"], "equipped": dict(t_base["live_telemetry"]["equipped"])},
            }
            mutate_c(c)
            mutate_t(t)
            return evaluate_raid_roster(
                {"ironbulwark": c}, raid_id="bwl_40", raid_stage=stage,
                telemetry_by_slug={"ironbulwark": t}, access_verified_against_db=access_db_ok,
                allow_no_persona_db=True, now_ts=now,
            )

        unknown_cases = [
            ("unknown_ilvl", lambda c: None, lambda t: (t.update({"avg_ilvl": None}), t["live_telemetry"]["equipped"].update({"avg_ilvl": None}))),
            ("unknown_durability", lambda c: None, lambda t: (t.update({"durability_pct": None}), t["live_telemetry"]["equipped"].update({"min_durability_pct": None}))),
            ("unknown_tactics", lambda c: None, lambda t: t.update({"tactics_verified": None})),
            ("unknown_lockouts", lambda c: c.update({"locked_instances": None}), lambda t: None),
            ("unknown_bwl_cloak", lambda c: None, lambda t: t.update({"owned_items": None})),
            ("unknown_live_role", lambda c: None, lambda t: t.update({"live_telemetry": None})),
            ("unknown_personal_consumables", lambda c: None, lambda t: t.update({"personal_consumables": None})),
            # P0-2: Fresh live telemetry + STALE SQL snapshot MUST be unknown!
            ("stale_sql_despite_fresh_live", lambda c: None, lambda t: t.update({"sql_observed_at": now - 900})),
            # Fresh SQL + STALE live snapshot MUST be unknown!
            ("stale_live_despite_fresh_sql", lambda c: c.update({"telemetry_observed_at": now - 900}), lambda t: t.update({"live_observed_at": now - 900})),
        ]
        for label, mc, mt in unknown_cases:
            rep = _run_mutated(mc, mt)
            self.assertEqual(rep["summary_counts_obtained"]["ready_available"], 0, f"{label} MUST NOT be ready!")
            self.assertEqual(rep["summary_counts_obtained"]["unknown"], 1, f"{label} MUST be unknown!")

        # P1-9: Unverified dungeon_access_template -> unknown!
        rep_noacc = _run_mutated(lambda c: None, lambda t: None, access_db_ok=False)
        self.assertEqual(rep_noacc["summary_counts_obtained"]["unknown"], 1)

        # P1-2: Empty equipment slots MUST fail even if avg_ilvl looks high!
        rep_empty = _run_mutated(
            lambda c: None,
            lambda t: (t.update({"filled_slots": 1, "expected_slots": 17}), t["live_telemetry"]["equipped"].update({"filled_slots": 1, "expected_slots": 17})),
        )
        self.assertEqual(rep_empty["summary_counts_obtained"]["not_ready"], 1)

        # P1-4: Unexpired BWL lockout fails; EXPIRED BWL lockout passes; different difficulty does not block!
        rep_locked = _run_mutated(lambda c: c.update({"locked_instances": [(469, 0, now + 3600)]}), lambda t: None)
        self.assertEqual(rep_locked["summary_counts_obtained"]["not_ready"], 1)
        rep_expired = _run_mutated(lambda c: c.update({"locked_instances": [(469, 0, now - 10)]}), lambda t: None)
        self.assertEqual(rep_expired["summary_counts_obtained"]["ready_available"], 1)
        rep_other_diff = _run_mutated(lambda c: c.update({"locked_instances": [(469, 1, now + 3600)]}), lambda t: None)
        self.assertEqual(rep_other_diff["summary_counts_obtained"]["ready_available"], 1)

    def test_p0_1_missing_persona_db_fails_closed_and_p1_8_bench_does_not_spend_bank(self):
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            # 1. Missing brain.db -> db_ok=False, feasible_now=False, feasible_with_potential_bank=False!
            c1, t1 = _raider("ironbulwark", 201, class_id=2, spec_tab=1, native_role_mask=LFG_ROLE_TANK, known_spells=[25780], has_shield=True, now_ts=now)
            c2, t2 = _raider("kelda", 202, class_id=5, spec_tab=1, native_role_mask=LFG_ROLE_HEALER, known_spells=[2061], now_ts=now)
            rep_missing = evaluate_raid_roster(
                {"ironbulwark": c1, "kelda": c2},
                raid_id="karazhan_10", raid_stage=1, bank_consumable_pool=20,
                telemetry_by_slug={"ironbulwark": t1, "kelda": t2},
                db_path=tp / "nonexistent.db", now_ts=now,
            )
            self.assertFalse(rep_missing["db_ok"])
            self.assertFalse(rep_missing["feasible_now"])
            self.assertFalse(rep_missing["feasible_with_potential_bank"])

            # 2. Bank of 10 with TWO same-persona characters needing 5 each -> ONLY 5 allocated, 5 unallocated!
            db_p = tp / "brain.db"
            reg = PersonaRegistry(db_p)
            reg.sync_from_companions({"ironbulwark": {"guid": 201, "shift_group": "early-birds"}}, now_ts=now)
            pid_iron = reg.persona_for_guid(201).persona_id
            reg.register_alt(persona_id=pid_iron, slug="kelda", character_guid=202, slot_kind="alt", now_ts=now)
            c1_0, t1_0 = _raider("ironbulwark", 201, class_id=2, spec_tab=1, native_role_mask=LFG_ROLE_TANK, known_spells=[25780], has_shield=True, personal_consumables=0, now_ts=now)
            c2_0, t2_0 = _raider("kelda", 202, class_id=5, spec_tab=1, native_role_mask=LFG_ROLE_HEALER, known_spells=[2061], personal_consumables=0, now_ts=now)
            rep_same = evaluate_raid_roster(
                {"ironbulwark": c1_0, "kelda": c2_0},
                raid_id="karazhan_10", raid_stage=1, bank_consumable_pool=10,
                telemetry_by_slug={"ironbulwark": t1_0, "kelda": t2_0},
                db_path=db_p, now_ts=now,
            )
            self.assertEqual(rep_same["bank_consumables_potential_allocated"], 5)
            self.assertEqual(rep_same["bank_consumables_unallocated"], 5)

    def test_p0_3_encounter_coverage_and_p1_3_owner_group_and_p1_6_human_validation(self):
        now = 1_800_000_000
        # Test owner_group_id: companion in owner's group (42) IS available; companion in group 99 is NOT!
        c_own, t_own = _raider(
            "ironbulwark", 201, class_id=2, spec_tab=1, native_role_mask=LFG_ROLE_TANK,
            known_spells=[25780], has_shield=True, in_group=True, group_id=42, now_ts=now,
        )
        c_oth, t_oth = _raider(
            "kelda", 202, class_id=5, spec_tab=1, native_role_mask=LFG_ROLE_HEALER,
            known_spells=[2061], in_group=True, group_id=99, now_ts=now,
        )
        rep_grp = evaluate_raid_roster(
            {"ironbulwark": c_own, "kelda": c_oth},
            raid_id="karazhan_10", raid_stage=1, bank_consumable_pool=0,
            telemetry_by_slug={"ironbulwark": t_own, "kelda": t_oth},
            owner_group_id=42, allow_no_persona_db=True, now_ts=now,
        )
        by_sl = {c["slug"]: c for c in rep_grp["characters"]}
        self.assertEqual(by_sl["ironbulwark"]["overall"], "ready_available")
        self.assertEqual(by_sl["kelda"]["overall"], "ready_but_unavailable")

        # P1-6: Human validation rejects invalid role, duplicate human, companion name collision, and overflow!
        rep_hum = evaluate_raid_roster(
            {"ironbulwark": c_own},
            raid_id="karazhan_10", raid_stage=1, bank_consumable_pool=0,
            telemetry_by_slug={"ironbulwark": t_own},
            human_slots=[
                {"name": "Wonky", "role": "tank"},
                {"name": "Wonky", "role": "tank"},          # duplicate!
                {"name": "Logan", "role": "tnak"},          # typo role!
                {"name": "Ironbulwark", "role": "damage"},  # collides with companion!
                {"name": "Human2", "role": "tank"},
                {"name": "Human3", "role": "tank"},         # overflow (Karazhan has only 2 tanks)!
            ],
            allow_no_persona_db=True, now_ts=now,
        )
        reasons = {r["reason"] for r in rep_hum["rejected_humans"]}
        self.assertIn("duplicate_human", reasons)
        self.assertIn("invalid_role:tnak", reasons)
        self.assertIn("collides_with_companion", reasons)
        self.assertIn("role_overflow:tank", reasons)
        self.assertFalse(rep_hum["feasible_now"])

        # P0-3: Gruul 25 requires 1 mage_tank_krosh! Even with 25 non-mage humans, missing mage_tank_krosh FAILS!
        non_mage_humans = (
            [{"name": f"HT{i}", "role": "tank", "encounter_capabilities": {"mage_tank_krosh": False}} for i in range(3)]
            + [{"name": f"HH{i}", "role": "healer", "encounter_capabilities": {"mage_tank_krosh": False}} for i in range(6)]
            + [{"name": f"HD{i}", "role": "damage", "encounter_capabilities": {"mage_tank_krosh": False}} for i in range(16)]
        )
        rep_gruul_fail = evaluate_raid_roster(
            {}, raid_id="gruul_25", raid_stage=1, human_slots=non_mage_humans,
            allow_no_persona_db=True, now_ts=now,
        )
        self.assertEqual(rep_gruul_fail["encounter_coverage"]["mage_tank_krosh"]["state"], "fail")
        self.assertFalse(rep_gruul_fail["feasible_now"])
        self.assertFalse(rep_gruul_fail["feasible_with_potential_bank"])

    def test_ac4_full_karazhan_unavailable_main_ready_alt_flex_humans_bench_pins_and_stage_transitions(self):
        """End-to-end AC3/AC4 scenario with real brain.db, obtained vs potential bank separation, bench, pins, stages 0/1/2."""
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp:
            db_p = Path(tmp) / "brain.db"
            reg = PersonaRegistry(db_p)
            reg.sync_from_companions({
                "brannoc": {"guid": 101, "shift_group": "core-wonky"},
                "kelda": {"guid": 102, "shift_group": "core-wonky"},
                "wren": {"guid": 103, "shift_group": "core-wonky"},
                "aelindra": {"guid": 202, "shift_group": "early-birds"},
                "alaric": {"guid": 203, "shift_group": "early-birds"},
                "pipwick": {"guid": 204, "shift_group": "early-birds"},
                "nightstep": {"guid": 205, "shift_group": "prime-time"},
                "solara": {"guid": 206, "shift_group": "prime-time"},
                "vespera": {"guid": 207, "shift_group": "prime-time"},
            }, now_ts=now)
            pid_brannoc = reg.persona_for_guid(101).persona_id
            reg.register_alt(persona_id=pid_brannoc, slug="ironbulwark", character_guid=201, slot_kind="alt", now_ts=now)
            sha_before = hashlib.sha256(db_p.read_bytes()).hexdigest()

            # Vespera (bench) needs 4 bank potions -> P1-8: bench MUST NOT subtract from bank_consumables_unallocated!
            specs = [
                ("brannoc", 101, 1, 2, LFG_ROLE_TANK, [71, 355], True, False, 5),
                ("ironbulwark", 201, 2, 1, LFG_ROLE_TANK, [25780], True, True, 2),  # Active roster needs 3 from bank!
                ("kelda", 102, 5, 1, LFG_ROLE_HEALER, [2061], False, True, 5),
                ("wren", 103, 11, 2, LFG_ROLE_HEALER, [5185], False, True, 5),
                ("aelindra", 202, 5, 2, LFG_ROLE_DAMAGE | LFG_ROLE_HEALER, [2061], False, True, 5),
                ("alaric", 203, 8, 2, LFG_ROLE_DAMAGE, [], False, True, 5),
                ("pipwick", 204, 9, 0, LFG_ROLE_DAMAGE, [], False, True, 5),
                ("nightstep", 205, 4, 1, LFG_ROLE_DAMAGE, [], False, True, 5),
                ("solara", 206, 8, 1, LFG_ROLE_DAMAGE, [], False, True, 5),
                ("vespera", 207, 9, 1, LFG_ROLE_DAMAGE, [], False, True, 1),  # Bench candidate needs 4 -> MUST NOT spend bank!
            ]
            comps: dict[str, dict] = {}
            telem: dict[str, dict] = {}
            for sl, g, cid, st, mask, sp, sh, on, cons in specs:
                c, t = _raider(
                    sl, g, class_id=cid, spec_tab=st, native_role_mask=mask,
                    known_spells=sp, has_shield=sh, online=on, personal_consumables=cons, now_ts=now,
                )
                comps[sl] = c
                telem[sl] = t

            humans = [
                {"name": "Wonky", "role": "tank", "status": "confirmed"},
                {"name": "Logan", "role": "damage", "status": "confirmed"},
            ]

            # 1. Stage 0 (Classic): Karazhan 10 locked!
            rep_s0 = evaluate_raid_roster(
                comps, raid_id="karazhan_10", raid_stage=0, bank_consumable_pool=8,
                telemetry_by_slug=telem, human_slots=humans, db_path=db_p, now_ts=now,
            )
            self.assertFalse(rep_s0["feasible_now"])
            self.assertFalse(rep_s0["feasible_with_potential_bank"])

            # 2. Stage 1 (TBC) with bank=8:
            #    - P1-1: feasible_now (obtained_only) MUST be False (ironbulwark only has 2/5 in bags)!
            #    - feasible_with_potential_bank MUST be True!
            #    - P1-8: Active roster allocates ONLY 3 (ironbulwark); bench `vespera` (needs 4) is viable against leftover 5
            #      WITHOUT subtracting from unallocated -> unallocated stays 5!
            rep_s1 = evaluate_raid_roster(
                comps, raid_id="karazhan_10", raid_stage=1, bank_consumable_pool=8,
                telemetry_by_slug=telem, human_slots=humans, db_path=db_p, now_ts=now,
            )
            self.assertEqual(hashlib.sha256(db_p.read_bytes()).hexdigest(), sha_before)
            self.assertFalse(rep_s1["feasible_now"], "P1-1: feasible_now MUST stay False when relying on undistributed bank stock!")
            self.assertFalse(rep_s1["feasible_obtained_only"])
            self.assertTrue(rep_s1["feasible_with_potential_bank"])
            self.assertEqual(rep_s1["bank_consumables_potential_allocated"], 3)
            self.assertEqual(rep_s1["bank_consumables_unallocated"], 5, "P1-8: bench MUST NOT reduce bank_consumables_unallocated!")
            self.assertEqual(rep_s1["bench_potential_consumables_need"], 4)
            self.assertEqual(len(rep_s1["viable_bench"]), 1)
            self.assertEqual(rep_s1["viable_bench"][0]["slug"], "vespera")

            # Verify ready alt `ironbulwark` replaced offline `brannoc`, and `aelindra` flexed to healer!
            active_comp = {m["slug"]: m for m in rep_s1["active_roster"] if m["kind"] == "companion"}
            self.assertIn("ironbulwark", active_comp)
            self.assertNotIn("brannoc", active_comp)
            self.assertTrue(active_comp["aelindra"]["used_flex"])
            self.assertEqual(active_comp["aelindra"]["assigned_role"], "healer")

            # 3. Stage 2 (WotLK): Stage-1 raid `karazhan_10` stays unlocked;
            #    Pinning `vespera` when she only has 1/5 potions and bank=0 MUST NOT silently unpin her!
            telem["ironbulwark"]["personal_consumables"] = 5
            rep_s2_unaff = evaluate_raid_roster(
                comps, raid_id="karazhan_10", raid_stage=2, bank_consumable_pool=0,
                telemetry_by_slug=telem, human_slots=humans,
                owner_pinned_roles={"vespera": "damage"},
                db_path=db_p, now_ts=now,
            )
            self.assertFalse(rep_s2_unaff["feasible_now"], "Pinned raider missing consumables must never be silently unpinned!")
            self.assertFalse(rep_s2_unaff["feasible_with_potential_bank"])

            # Once pinned `vespera` obtains 5/5 potions -> feasible_now=True AND vespera is in active_roster!
            telem["vespera"]["personal_consumables"] = 5
            rep_s2 = evaluate_raid_roster(
                comps, raid_id="karazhan_10", raid_stage=2, bank_consumable_pool=0,
                telemetry_by_slug=telem, human_slots=humans,
                owner_pinned_roles={"vespera": "damage"},  # P2-8: pin vespera into active DPS!
                db_path=db_p, now_ts=now,
            )
            self.assertTrue(rep_s2["feasible_now"])
            self.assertTrue(rep_s2["feasible_obtained_only"])
            active_s2 = {m["slug"] for m in rep_s2["active_roster"] if m["kind"] == "companion"}
            self.assertIn("vespera", active_s2, "P2-8: owner-pinned vespera must be in active roster!")

    def test_fetch_raid_readiness_telemetry_guild_scoping_empty_slots_attestations_and_cli(self):
        now = 1_800_000_000
        spec = RAID_BY_ID["bwl_40"]
        wctx = {
            "observed_at": "2026-11-25T20:00:00Z",
            "live_pos_ts": now,
            "companions": {
                "ironbulwark": {"guid": 201, "in_group": False, "live_telemetry": {}},
            },
        }

        # P0-4: Two guilds -> ambiguous -> bank_pool MUST be None!
        def multi_guild_sql(sql: str):
            if "FROM guild_member" in sql:
                return [["6"], ["99"]]
            if "dungeon_access_template" in sql:
                return [["60", "0"]]
            if "SUM(it.ItemLevel)" in sql:
                # 1 epic item (iLvl 120) in 1 slot, 16 empty slots -> 120 / 17 = 7.1 iLvl!
                return [["201", "120", "1", "0", "100.0", "0"]]
            if "character_instance" in sql:
                return []
            if "DISTINCT ci.guid, ii.itemEntry" in sql:
                return [["201", "15138"]]
            if "SUM(ii.count)" in sql:
                return [["201", "9"]]
            return []

        bundle_mg = fetch_raid_readiness_telemetry(wctx, spec, sql_exec=multi_guild_sql, now_ts=now)
        self.assertIsNone(bundle_mg["bank_pool"], "Ambiguous multi-guild bank MUST return None!")
        self.assertTrue(bundle_mg["access_verified_against_db"])
        self.assertEqual(bundle_mg["telemetry_by_slug"]["ironbulwark"]["avg_ilvl"], 7.1)
        self.assertEqual(bundle_mg["telemetry_by_slug"]["ironbulwark"]["filled_slots"], 1)
        self.assertEqual(bundle_mg["telemetry_by_slug"]["ironbulwark"]["expected_slots"], 17)

        # Single guild scopes query cleanly
        seen_sql: list[str] = []

        def single_guild_sql(sql: str):
            seen_sql.append(sql)
            if "FROM guild_member" in sql:
                return [["6"]]
            if "guild_bank_item" in sql:
                return [["42"]]
            if "dungeon_access_template" in sql:
                return [["60", "0"]]
            if "SUM(it.ItemLevel)" in sql:
                return [["201", "1122", "17", "0", "95.0", "0"]]
            if "character_instance" in sql:
                return []
            if "DISTINCT ci.guid, ii.itemEntry" in sql:
                return [["201", "15138"]]
            if "SUM(ii.count)" in sql:
                return [["201", "9"]]
            return []

        bundle_sg = fetch_raid_readiness_telemetry(wctx, spec, sql_exec=single_guild_sql, now_ts=now)
        self.assertEqual(bundle_sg["bank_pool"], 42)
        self.assertEqual(bundle_sg["guild_id"], 6)
        self.assertTrue(any("gbi.guildid = 6" in s for s in seen_sql))

        # Owner attestations loader (valid vs expired)
        with tempfile.TemporaryDirectory() as tmp:
            att_p = Path(tmp) / "att.json"
            att_p.write_text(json.dumps({
                "attestations": [
                    {"slug": "ironbulwark", "raid_id": "bwl_40", "tactics_verified": True, "verified_at": now - 60, "by": "Wonky"},
                    {"slug": "kelda", "raid_id": "bwl_40", "tactics_verified": True, "verified_at": now - 9999999, "by": "Wonky"},
                ]
            }), encoding="utf-8")
            att_map, att_errs = load_owner_attestations(att_p, raid_id="bwl_40", now_ts=now)
            self.assertIn("ironbulwark", att_map)
            self.assertNotIn("kelda", att_map)
            self.assertTrue(any("attestation_expired:kelda" in e for e in att_errs))

        # CLI --list-raids succeeds and invalid --human exits non-zero!
        r_list = subprocess.run([sys.executable, "-m", "hearthcore", "phase2-readiness", "--list-raids"], capture_output=True, text=True)
        self.assertEqual(r_list.returncode, 0, r_list.stderr)
        self.assertIn("mc_40", r_list.stdout)
        self.assertIn("bwl_40", r_list.stdout)
        self.assertIn("aq40_40", r_list.stdout)

        r_bad_h = subprocess.run(
            [sys.executable, "-m", "hearthcore", "phase2-readiness", "--raid", "karazhan_10", "--stage", "1", "--human", "Wonky:tnak"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(r_bad_h.returncode, 0, "Invalid --human role MUST exit non-zero!")


if __name__ == "__main__":
    unittest.main()
