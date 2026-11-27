"""Tests for Issue #19: Voluntary party continuation, independent goal resolution, and bounded rerun decisions."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from hearthcore.continuation import PartyContinuationCoordinator
from hearthcore.dungeons import LFG_ROLE_DAMAGE, LFG_ROLE_HEALER, LFG_ROLE_TANK
from hearthcore.identity import PersonaRegistry
from hearthcore.opportunities import OpportunityCoordinator

BASE_TS = int(datetime(2026, 11, 30, 13, 0, 0, tzinfo=timezone.utc).timestamp())


def _seed_party(db_path: Path, now_ts: int = BASE_TS) -> dict[str, dict]:
    reg = PersonaRegistry(db_path)
    party = {
        "ironbulwark": {"guid": 101, "name": "Ironbulwark", "role": "tank", "class_id": 1, "spec_tab": 2, "role_mask": LFG_ROLE_TANK, "spells": [71, 355], "has_shield": True},
        "vespera": {"guid": 102, "name": "Vespera", "role": "healer", "class_id": 5, "spec_tab": 1, "role_mask": LFG_ROLE_HEALER, "spells": [2061], "has_shield": False},
        "kraghammer": {"guid": 103, "name": "Kraghammer", "role": "damage", "class_id": 3, "spec_tab": 0, "role_mask": LFG_ROLE_DAMAGE, "spells": [], "has_shield": False},
        "emberveil": {"guid": 104, "name": "Emberveil", "role": "damage", "class_id": 8, "spec_tab": 2, "role_mask": LFG_ROLE_DAMAGE, "spells": [], "has_shield": False},
        "duskblade": {"guid": 105, "name": "Duskblade", "role": "damage", "class_id": 4, "spec_tab": 1, "role_mask": LFG_ROLE_DAMAGE, "spells": [], "has_shield": False},
    }
    reg.sync_from_companions(
        {
            s: {"guid": v["guid"], "realm_id": 1, "name": v["name"], "faction": "alliance", "shift_group": "regular", "online": True}
            for s, v in party.items()
        },
        now_ts=now_ts,
    )
    for s, v in party.items():
        v["persona_id"] = reg.persona_for_guid(v["guid"], realm_id=1).persona_id
        v["incarnation"] = 1
    return party


def _mk_world(
    party: dict[str, dict],
    *,
    now_ts: int = BASE_TS,
    group_id: int = 777,
    in_group: bool = True,
    overrides: dict[str, dict] | None = None,
) -> dict:
    ovs = overrides or {}
    comps = {}
    for slug, info in party.items():
        ov = ovs.get(slug) or {}
        live_ov = ov.get("live_telemetry") or {}
        base_live = {
            "class_id": info["class_id"],
            "spec_tab": info["spec_tab"],
            "native_role_mask": info["role_mask"],
            "known_spells": info["spells"],
            "talent_spells": [],
            "equipped": {
                "has_main_hand": True,
                "has_shield": info["has_shield"],
                "broken_slots": 0,
                "min_durability_pct": 100.0,
            },
            "in_combat": False,
            "in_lfg_queue": False,
            "in_lfg_queue_dungeons": [],
            "is_dead": False,
            "native_lfg_eligible": True,
            "native_lfg_lock_reason": "",
            "lfg_deserter_remaining_sec": 0,
            "lfg_random_cooldown_sec": 0,
            "instances_entered_last_hour": 1,
            **live_ov,
        }
        comps[slug] = {
            "guid": info["guid"],
            "realm_id": 1,
            "name": info["name"],
            "online": ov.get("online", True),
            "level": ov.get("level", 24),
            "race_id": 1,
            "faction": "alliance",
            "class_id": info["class_id"],
            "shift_group": ov.get("shift_group", "regular"),
            "map_id": ov.get("map_id", 0),
            "zone_id": ov.get("zone_id", 1519),
            "in_group": ov.get("in_group", in_group),
            "group_id": ov.get("group_id", group_id),
            "schedule": ov.get("schedule", {"start_hour": 10, "end_hour": 18, "days_mask": 127}),
            "locked_instances": ov.get("locked_instances", []),
            "telemetry_observed_at": ov.get("telemetry_observed_at", now_ts),
            "live_telemetry": base_live,
        }
    return {
        "observed_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "companions": comps,
    }


def _members_list(party: dict[str, dict], *, willing_leaders: set[str] | None = None) -> list[dict]:
    wl = willing_leaders or set()
    return [
        {
            "persona_id": info["persona_id"],
            "realm_id": 1,
            "character_guid": info["guid"],
            "incarnation": 1,
            "character_slug": slug,
            "role": info["role"],
            "willing_leader": slug in wl,
        }
        for slug, info in party.items()
    ]


class TestIssue19PartyContinuation(unittest.TestCase):
    def test_ac1_independent_goal_resolution_idempotency_identity_binding_and_partial_rerun(self) -> None:
        """Player A loots item & leaves; Player B stays unfulfilled (idempotent, run-bound, char-bound) & requeues."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "brain.db"
            party = _seed_party(db_path)
            opp_coord = OpportunityCoordinator(db_path)
            cont = PartyContinuationCoordinator(db_path)

            for slug in ("kraghammer", "duskblade"):
                r = cont.register_member_goal(
                    opportunity_id=10, native_run_id="run-dm-1",
                    persona_id=party[slug]["persona_id"], character_guid=party[slug]["guid"],
                    goal_id="item:5191", goal_kind="loot_item", target_entry=5191, now_ts=BASE_TS,
                )
                self.assertTrue(r["ok"], r)
            # Quest goal for Emberveil & social_help for Vespera
            self.assertTrue(
                cont.register_member_goal(
                    opportunity_id=10, native_run_id="run-dm-1",
                    persona_id=party["emberveil"]["persona_id"], character_guid=party["emberveil"]["guid"],
                    goal_id="quest:166", goal_kind="dungeon_quest", target_entry=166, now_ts=BASE_TS,
                )["ok"]
            )
            self.assertTrue(
                cont.register_member_goal(
                    opportunity_id=10, native_run_id="run-dm-1",
                    persona_id=party["vespera"]["persona_id"], character_guid=party["vespera"]["guid"],
                    goal_id="help_guild", goal_kind="social_help", now_ts=BASE_TS,
                )["ok"]
            )

            events = [
                # Valid loot opportunities & Kraghammer's actual loot
                {"native_event_id": "ev-1", "native_run_id": "run-dm-1", "type": "loot_opportunity", "persona_id": party["kraghammer"]["persona_id"], "realm_id": 1, "character_guid": 103, "incarnation": 1, "item_entry": 5191},
                {"native_event_id": "ev-2", "native_run_id": "run-dm-1", "type": "loot_opportunity", "persona_id": party["duskblade"]["persona_id"], "realm_id": 1, "character_guid": 105, "incarnation": 1, "item_entry": 5191},
                {"native_event_id": "ev-3", "native_run_id": "run-dm-1", "type": "actual_loot", "persona_id": party["kraghammer"]["persona_id"], "realm_id": 1, "character_guid": 103, "incarnation": 1, "item_entry": 5191},
                # Wrong run_id -> MUST BE IGNORED (P1-2!)
                {"native_event_id": "ev-wrong-run", "native_run_id": "run-OTHER", "type": "actual_loot", "persona_id": party["duskblade"]["persona_id"], "realm_id": 1, "character_guid": 105, "incarnation": 1, "item_entry": 5191},
                # Persona/character mismatch (Duskblade persona with Kraghammer's guid) -> MUST BE REJECTED (P1-2!)
                {"native_event_id": "ev-mismatch", "native_run_id": "run-dm-1", "type": "actual_loot", "persona_id": party["duskblade"]["persona_id"], "realm_id": 1, "character_guid": 103, "incarnation": 1, "item_entry": 5191},
                # Quest completed event that also carries a reward item_entry -> must key on quest_id (P2-2!)
                {"native_event_id": "ev-quest", "native_run_id": "run-dm-1", "type": "quest_completed", "persona_id": party["emberveil"]["persona_id"], "realm_id": 1, "character_guid": 104, "incarnation": 1, "quest_id": 166, "item_entry": 9999},
                # Vespera present at run completion (P2-3!)
                {"native_event_id": "ev-comp-vesp", "native_run_id": "run-dm-1", "type": "run_completed", "persona_id": party["vespera"]["persona_id"], "realm_id": 1, "character_guid": 102, "incarnation": 1},
            ]

            res_g1 = cont.resolve_member_goals(
                opportunity_id=10, native_run_id="run-dm-1", run_completed=True, observed_events=events, now_ts=BASE_TS + 1800,
            )
            self.assertEqual(res_g1["wrong_run_ignored"], 1)
            self.assertEqual(res_g1["evidence_identity_mismatches"], 1)
            g1 = res_g1["goals_by_persona"]
            self.assertEqual(g1[party["kraghammer"]["persona_id"]][0]["state"], "fulfilled")
            self.assertEqual(g1[party["duskblade"]["persona_id"]][0]["state"], "unfulfilled")
            self.assertEqual(g1[party["duskblade"]["persona_id"]][0]["eligible_loot_attempts"], 1)
            self.assertFalse(g1[party["duskblade"]["persona_id"]][0]["technical_failure"])
            self.assertEqual(g1[party["emberveil"]["persona_id"]][0]["state"], "fulfilled")
            self.assertEqual(g1[party["vespera"]["persona_id"]][0]["state"], "fulfilled")

            # P1-1: Calling resolve_member_goals a SECOND time with the SAME events MUST NOT double-count attempts!
            res_g2 = cont.resolve_member_goals(
                opportunity_id=10, native_run_id="run-dm-1", run_completed=True, observed_events=events, now_ts=BASE_TS + 1805,
            )
            self.assertEqual(res_g2["goals_by_persona"][party["duskblade"]["persona_id"]][0]["eligible_loot_attempts"], 1)

            # P1-3: Re-registering fulfilled goal with a new target_entry MUST BE REJECTED!
            rewrite = cont.register_member_goal(
                opportunity_id=10, native_run_id="run-dm-1",
                persona_id=party["kraghammer"]["persona_id"], character_guid=103,
                goal_id="item:5191", goal_kind="loot_item", target_entry=9999, now_ts=BASE_TS + 1806,
            )
            self.assertFalse(rewrite["ok"])
            self.assertTrue(rewrite["reason"].startswith("goal_already_resolved"))

            # Partial rerun: Kraghammer & Vespera leave; Ironbulwark, Emberveil, Duskblade requeue
            b = cont.open_boundary(
                opportunity_id=10, native_run_id="run-dm-1", dungeon_id="deadmines_n", faction="alliance",
                group_id=777, leader_persona_id=party["ironbulwark"]["persona_id"],
                boundary_kind="run_completed", members=_members_list(party),
                safe_boundary=True, in_combat=False, is_shadow=True, now_ts=BASE_TS + 1810,
            )
            self.assertTrue(b["ok"], b)
            bid = b["boundary_id"]
            world = _mk_world(party, now_ts=BASE_TS + 1815, group_id=777)

            for slug, dec in (("kraghammer", "leave"), ("vespera", "leave"), ("ironbulwark", "requeue"), ("emberveil", "requeue"), ("duskblade", "requeue")):
                vr = cont.submit_member_decision(
                    bid, expected_revision=1, persona_id=party[slug]["persona_id"],
                    character_guid=party[slug]["guid"], decision=dec,
                    candidate=world["companions"][slug], now_ts=BASE_TS + 1815, tz=timezone.utc,
                )
                self.assertTrue(vr["ok"], f"{slug}: {vr}")

            res = cont.resolve_boundary(
                bid, expected_revision=1, world_ctx=world, opportunity_coordinator=opp_coord,
                now_ts=BASE_TS + 1820, tz=timezone.utc,
            )
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["outcome"], "partial_party_requeued")
            rerun_opp = opp_coord.get_opportunity(res["rerun_opportunity_id"])
            assert rerun_opp is not None
            admitted_pids = {m["persona_id"] for m in rerun_opp["members"]}
            self.assertEqual(admitted_pids, {party["ironbulwark"]["persona_id"], party["emberveil"]["persona_id"], party["duskblade"]["persona_id"]})
            self.assertNotIn(party["kraghammer"]["persona_id"], admitted_pids)
            self.assertNotIn(party["vespera"]["persona_id"], admitted_pids)

            # P1-10: Opening a SECOND run_completed boundary for the same run MUST BE REJECTED!
            dup_comp = cont.open_boundary(
                opportunity_id=10, native_run_id="run-dm-1", dungeon_id="deadmines_n", faction="alliance",
                group_id=777, leader_persona_id=party["ironbulwark"]["persona_id"],
                boundary_kind="run_completed", members=_members_list(party),
                safe_boundary=True, in_combat=False, is_shadow=True, now_ts=BASE_TS + 1830,
            )
            self.assertFalse(dup_comp["ok"])
            self.assertEqual(dup_comp["reason"], "run_already_completed")

    def test_p0_1_p0_2_p0_3_lease_recovery_mid_resolve_recall_and_concurrent_resolvers(self) -> None:
        """Stuck resolving lease recovers cleanly; mid-resolve recall removes recalled persona; concurrent resolvers never double-requeue."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "brain.db"
            party = _seed_party(db_path)
            opp_coord = OpportunityCoordinator(db_path)
            cont = PartyContinuationCoordinator(db_path)
            world = _mk_world(party, now_ts=BASE_TS, group_id=777)

            # 1. Mid-resolve owner recall: recall arrives AFTER create_opportunity -> Ironbulwark removed from rerun opp & boundary still resolves!
            b = cont.open_boundary(
                opportunity_id=40, native_run_id="run-race-1", dungeon_id="stockade_n", faction="alliance",
                group_id=777, leader_persona_id=party["ironbulwark"]["persona_id"],
                boundary_kind="run_completed", members=_members_list(party),
                safe_boundary=True, in_combat=False, is_shadow=True, now_ts=BASE_TS,
            )
            bid = b["boundary_id"]
            for slug, info in party.items():
                self.assertTrue(
                    cont.submit_member_decision(
                        bid, expected_revision=1, persona_id=info["persona_id"], character_guid=info["guid"],
                        decision="requeue", candidate=world["companions"][slug], now_ts=BASE_TS + 5, tz=timezone.utc,
                    )["ok"]
                )

            orig_create = opp_coord.create_opportunity
            tank_pid = party["ironbulwark"]["persona_id"]
            heal_pid = party["vespera"]["persona_id"]

            def _inject_two_mid_resolve_recalls(*args, **kwargs):
                created = orig_create(*args, **kwargs)
                # TWO concurrent deferred recalls for DIFFERENT personas during 'resolving'! (P0-2 multi-slot check!)
                r1 = cont.request_owner_recall(bid, target_persona_id=tank_pid, safe_boundary=False, now_ts=BASE_TS + 8)
                r2 = cont.request_owner_recall(bid, target_persona_id=heal_pid, safe_boundary=False, now_ts=BASE_TS + 8)
                self.assertTrue(r1["ok"] and r2["ok"])
                return created

            with mock.patch.object(opp_coord, "create_opportunity", side_effect=_inject_two_mid_resolve_recalls):
                res = cont.resolve_boundary(
                    bid, expected_revision=1, world_ctx=world, opportunity_coordinator=opp_coord,
                    now_ts=BASE_TS + 10, tz=timezone.utc,
                )
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["outcome"], "partial_party_requeued")
            self.assertNotIn(tank_pid, res["admitted_rerun_personas"])
            self.assertNotIn(heal_pid, res["admitted_rerun_personas"])
            self.assertIn(tank_pid, res["leaving_personas"])
            self.assertIn(heal_pid, res["leaving_personas"])
            # Verify withdrawn on OpportunityCoordinator too!
            opp_after = opp_coord.get_opportunity(res["rerun_opportunity_id"])
            active_in_opp = {m["persona_id"] for m in opp_after["members"] if m["state"] == "interested"}
            self.assertNotIn(tank_pid, active_in_opp)
            self.assertNotIn(heal_pid, active_in_opp)
            self.assertEqual(len(active_in_opp), 3)

            # Forwarded recall AFTER boundary resolved withdraws Duskblade from the rerun opp cleanly!
            fwd = cont.request_owner_recall(
                bid, target_persona_id=party["duskblade"]["persona_id"],
                safe_boundary=True, opportunity_coordinator=opp_coord, now_ts=BASE_TS + 20,
            )
            self.assertTrue(fwd["ok"] and fwd.get("forwarded_to_rerun_opportunity"))

            # 2. P0-1: Exception during Step 2 compensates opp & reverts 'resolving' -> 'open' (never wedged!)
            b2 = cont.open_boundary(
                opportunity_id=41, native_run_id="run-crash-1", dungeon_id="stockade_n", faction="alliance",
                group_id=777, leader_persona_id=party["ironbulwark"]["persona_id"],
                boundary_kind="run_completed", members=_members_list(party),
                safe_boundary=True, in_combat=False, is_shadow=True, now_ts=BASE_TS + 30,
            )
            bid2 = b2["boundary_id"]
            for slug, info in party.items():
                cont.submit_member_decision(
                    bid2, expected_revision=1, persona_id=info["persona_id"], character_guid=info["guid"],
                    decision="requeue", candidate=world["companions"][slug], now_ts=BASE_TS + 35, tz=timezone.utc,
                )
            with mock.patch.object(opp_coord, "admit_candidate", side_effect=RuntimeError("simulated_crash")):
                with self.assertRaises(RuntimeError):
                    cont.resolve_boundary(
                        bid2, expected_revision=1, world_ctx=world, opportunity_coordinator=opp_coord,
                        now_ts=BASE_TS + 40, tz=timezone.utc,
                    )
            b2_row = cont.get_boundary(bid2)
            assert b2_row is not None
            self.assertEqual(b2_row["state"], "open")  # Reverted cleanly, NOT stuck in 'resolving'!

            # 3. P0-1 & P0-3: Simulate hard process crash leaving row in 'resolving' with orphan opp -> reconcile_stale_boundaries cleans it!
            orphan = opp_coord.create_opportunity(dungeon_id="stockade_n", creator_guid=101, now_ts=BASE_TS + 45, is_shadow=True)
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "UPDATE continuation_boundaries SET state='resolving', resolving_until=?, rerun_opportunity_id=? WHERE id=?",
                    (BASE_TS + 50, orphan["id"], bid2),
                )
                conn.commit()
            finally:
                conn.close()
            rec = cont.reconcile_stale_boundaries(opportunity_coordinator=opp_coord, now_ts=BASE_TS + 100)
            self.assertEqual(rec["reverted_to_open"], 1)
            self.assertEqual(rec["released_orphan_opps"], 1)
            self.assertEqual(opp_coord.get_opportunity(orphan["id"])["state"], "cancelled")

            # 4. Concurrent threads racing resolve_boundary -> EXACTLY ONE succeeds!
            cur_rev = cont.get_boundary(bid2)["revision"]
            results: list[dict] = []
            lock = threading.Lock()
            w_race = _mk_world(party, now_ts=BASE_TS + 110, group_id=777)

            def _worker() -> None:
                c_loc = PartyContinuationCoordinator(db_path)
                o_loc = OpportunityCoordinator(db_path)
                r = c_loc.resolve_boundary(
                    bid2, expected_revision=cur_rev, world_ctx=w_race,
                    opportunity_coordinator=o_loc, now_ts=BASE_TS + 110, tz=timezone.utc,
                )
                with lock:
                    results.append(r)

            t1 = threading.Thread(target=_worker)
            t2 = threading.Thread(target=_worker)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            self.assertEqual(sum(1 for r in results if r["ok"]), 1, results)

    def test_p0_4_persisted_wipe_and_rerun_chain_bounds_cannot_be_bypassed_by_caller(self) -> None:
        """P0-4: Three wipes on the SAME run passing wipe_count=1 each time enforces server-derived bound on wipe #3; rerun chain persists across opportunities."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "brain.db"
            party = _seed_party(db_path)
            opp_coord = OpportunityCoordinator(db_path)
            cont = PartyContinuationCoordinator(db_path)

            # Same (opportunity_id=50, native_run_id='run-same') across 3 wipes, caller passes wipe_count=1 every time!
            for attempt in (1, 2):
                t_now = BASE_TS + attempt * 60
                w = _mk_world(party, now_ts=t_now, group_id=777, in_group=True)
                bw = cont.open_boundary(
                    opportunity_id=50, native_run_id="run-same", dungeon_id="stockade_n", faction="alliance",
                    group_id=777, leader_persona_id=party["ironbulwark"]["persona_id"],
                    boundary_kind="wipe_recovery", members=_members_list(party),
                    safe_boundary=True, in_combat=False, wipe_count=1, max_wipe_retries=2,
                    recovery_reason="patrol_add_recovery", is_shadow=True, now_ts=t_now,
                )
                self.assertTrue(bw["ok"], bw)
                self.assertEqual(bw["wipe_count"], attempt)  # Derived from DB history!
                self.assertTrue(bw["retry_allowed"])
                for slug, info in party.items():
                    self.assertTrue(
                        cont.submit_member_decision(
                            bw["boundary_id"], expected_revision=1, persona_id=info["persona_id"],
                            character_guid=info["guid"], decision="retry", candidate=w["companions"][slug],
                            now_ts=t_now + 2, tz=timezone.utc,
                        )["ok"]
                    )
                res = cont.resolve_boundary(bw["boundary_id"], expected_revision=1, world_ctx=w, now_ts=t_now + 5, tz=timezone.utc)
                self.assertEqual(res["outcome"], "retry_approved")

            # Wipe #3 on SAME run with caller STILL passing wipe_count=1 -> derived_wipe_count=3 > 2 -> retry DENIED!
            t3 = BASE_TS + 240
            w3 = _mk_world(party, now_ts=t3, group_id=777, in_group=True)
            bw3 = cont.open_boundary(
                opportunity_id=50, native_run_id="run-same", dungeon_id="stockade_n", faction="alliance",
                group_id=777, leader_persona_id=party["ironbulwark"]["persona_id"],
                boundary_kind="wipe_recovery", members=_members_list(party),
                safe_boundary=True, in_combat=False, wipe_count=1, max_wipe_retries=2,
                recovery_reason="overpull_recovery", is_shadow=True, now_ts=t3,
            )
            self.assertTrue(bw3["ok"])
            self.assertEqual(bw3["wipe_count"], 3)
            self.assertFalse(bw3["retry_allowed"])
            deny = cont.submit_member_decision(
                bw3["boundary_id"], expected_revision=1, persona_id=party["ironbulwark"]["persona_id"],
                character_guid=101, decision="retry", candidate=w3["companions"]["ironbulwark"],
                now_ts=t3 + 2, tz=timezone.utc,
            )
            self.assertFalse(deny["ok"])
            self.assertTrue(deny["reason"].startswith("wipe_retry_limit_exceeded"))

            # Rerun chain depth persisted automatically across chained opportunities (max_consecutive_reruns=1)
            opp_root = opp_coord.create_opportunity(dungeon_id="stockade_n", creator_guid=101, now_ts=BASE_TS, is_shadow=True)
            w_r1 = _mk_world(party, now_ts=BASE_TS + 300, group_id=777)
            br1 = cont.open_boundary(
                opportunity_id=opp_root["id"], native_run_id="chain-1", dungeon_id="stockade_n", faction="alliance",
                group_id=777, leader_persona_id=party["ironbulwark"]["persona_id"],
                boundary_kind="run_completed", members=_members_list(party),
                safe_boundary=True, in_combat=False, max_consecutive_reruns=1, is_shadow=True, now_ts=BASE_TS + 300,
            )
            for slug, info in party.items():
                cont.submit_member_decision(
                    br1["boundary_id"], expected_revision=1, persona_id=info["persona_id"],
                    character_guid=info["guid"], decision="requeue", candidate=w_r1["companions"][slug],
                    now_ts=BASE_TS + 305, tz=timezone.utc,
                )
            rr1 = cont.resolve_boundary(
                br1["boundary_id"], expected_revision=1, world_ctx=w_r1, opportunity_coordinator=opp_coord,
                now_ts=BASE_TS + 310, tz=timezone.utc,
            )
            self.assertTrue(rr1["ok"] and rr1["rerun_opportunity_id"] > 0)

            # Next run uses rr1["rerun_opportunity_id"] AND caller passes rerun_chain_depth=0 -> DB derives depth=1 == max(1) -> requeue BLOCKED!
            br2 = cont.open_boundary(
                opportunity_id=rr1["rerun_opportunity_id"], native_run_id="chain-2", dungeon_id="stockade_n", faction="alliance",
                group_id=777, leader_persona_id=party["ironbulwark"]["persona_id"],
                boundary_kind="run_completed", members=_members_list(party),
                safe_boundary=True, in_combat=False, rerun_chain_depth=0, max_consecutive_reruns=2,
                is_shadow=True, now_ts=BASE_TS + 320,
            )
            self.assertTrue(br2["ok"])
            self.assertEqual(br2["rerun_chain_depth"], 1)
            self.assertFalse(br2["requeue_allowed"])

    def test_p1_4_p1_5_p1_6_p1_8_p1_9_recall_over_dup_vote_human_timeout_lfg_deserter_and_live_kill_switch(self) -> None:
        """Covers P1-4, P1-5, P1-6, P1-8, P1-9, random cooldown, decision timeout, and live coop kill-switch."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "brain.db"
            party = _seed_party(db_path)
            cont = PartyContinuationCoordinator(db_path)
            world = _mk_world(party, now_ts=BASE_TS, group_id=777)

            # 1. P1-4: Resubmitting 'requeue' with owner_recalled=True AFTER already voting 'requeue' overrides to 'leave'!
            b = cont.open_boundary(
                opportunity_id=60, native_run_id="run-p14", dungeon_id="stockade_n", faction="alliance",
                group_id=777, leader_persona_id=party["ironbulwark"]["persona_id"],
                boundary_kind="run_completed", members=_members_list(party),
                safe_boundary=True, in_combat=False, requeue_mode="random", is_shadow=True, now_ts=BASE_TS,
            )
            bid = b["boundary_id"]
            self.assertTrue(
                cont.submit_member_decision(
                    bid, expected_revision=1, persona_id=party["ironbulwark"]["persona_id"],
                    character_guid=101, decision="requeue", candidate=world["companions"]["ironbulwark"],
                    now_ts=BASE_TS + 5, tz=timezone.utc,
                )["ok"]
            )
            ov = cont.submit_member_decision(
                bid, expected_revision=1, persona_id=party["ironbulwark"]["persona_id"],
                character_guid=101, decision="requeue", owner_recalled=True,
                candidate=world["companions"]["ironbulwark"], now_ts=BASE_TS + 6, tz=timezone.utc,
            )
            self.assertTrue(ov["ok"] and ov.get("overridden_to_leave"))
            self.assertEqual(ov["decision"], "leave")

            # Random cooldown blocks requeue in random mode!
            w_cd = _mk_world(party, now_ts=BASE_TS + 7, overrides={"vespera": {"live_telemetry": {"lfg_random_cooldown_sec": 300}}})
            r_cd = cont.submit_member_decision(
                bid, expected_revision=1, persona_id=party["vespera"]["persona_id"],
                character_guid=102, decision="requeue", candidate=w_cd["companions"]["vespera"],
                now_ts=BASE_TS + 7, tz=timezone.utc,
            )
            self.assertFalse(r_cd["ok"])
            self.assertTrue(r_cd["reason"].startswith("lfg_random_cooldown_active"))

            # Decision timeout safely turns remaining pending votes into 'leave'!
            res_to = cont.resolve_boundary(bid, expected_revision=1, world_ctx=_mk_world(party, now_ts=BASE_TS + 200), now_ts=BASE_TS + 200, tz=timezone.utc)
            self.assertTrue(res_to["ok"])
            self.assertEqual(res_to["outcome"], "all_left_cleanly")

            # 2. P1-9 & P1-8: leader_departure rejects departed leader in members; requeue without Deserter exemption fails closed!
            bad_ld = cont.open_boundary(
                opportunity_id=61, native_run_id="run-ld-bad", dungeon_id="stockade_n", faction="alliance",
                group_id=777, leader_persona_id=party["kraghammer"]["persona_id"],
                boundary_kind="leader_departure", members=_members_list(party),  # kraghammer still listed!
                safe_boundary=True, in_combat=False, is_shadow=True, now_ts=BASE_TS,
            )
            self.assertFalse(bad_ld["ok"])
            self.assertEqual(bad_ld["reason"], "departed_leader_still_in_members")

            rem = {k: v for k, v in party.items() if k != "kraghammer"}
            b_ld = cont.open_boundary(
                opportunity_id=61, native_run_id="run-ld-ok", dungeon_id="stockade_n", faction="alliance",
                group_id=777, leader_persona_id=party["kraghammer"]["persona_id"],
                boundary_kind="leader_departure", members=_members_list(rem, willing_leaders={"vespera"}),
                safe_boundary=True, in_combat=False, is_shadow=True, now_ts=BASE_TS,
            )
            w_ld = _mk_world(rem, now_ts=BASE_TS + 5, group_id=777)
            # Requeue on incomplete leader_departure without would_incur_deserter=False -> BLOCKED (P1-9!)
            r_des = cont.submit_member_decision(
                b_ld["boundary_id"], expected_revision=1, persona_id=party["duskblade"]["persona_id"],
                character_guid=105, decision="requeue", candidate=w_ld["companions"]["duskblade"],
                now_ts=BASE_TS + 5, tz=timezone.utc,
            )
            self.assertFalse(r_des["ok"])
            self.assertEqual(r_des["reason"], "requeue_would_incur_deserter")

            # 3. P1-5: Human participant approval timeout resolves non-destructively to awaiting_human_timeout_hold
            b_hum = cont.open_boundary(
                opportunity_id=62, native_run_id="run-hum", dungeon_id="stockade_n", faction="alliance",
                group_id=777, leader_persona_id=party["ironbulwark"]["persona_id"],
                boundary_kind="wipe_recovery", members=_members_list(party),
                safe_boundary=True, in_combat=False, recovery_reason="overpull_recovery",
                has_human_participant=True, human_approval_ttl_sec=60, is_shadow=True, now_ts=BASE_TS,
            )
            for slug, info in party.items():
                cont.submit_member_decision(
                    b_hum["boundary_id"], expected_revision=1, persona_id=info["persona_id"],
                    character_guid=info["guid"], decision="retry", candidate=world["companions"][slug],
                    now_ts=BASE_TS + 5, tz=timezone.utc,
                )
            res_hold = cont.resolve_boundary(
                b_hum["boundary_id"], expected_revision=1, world_ctx=_mk_world(party, now_ts=BASE_TS + 90),
                human_approved=False, now_ts=BASE_TS + 90, tz=timezone.utc,
            )
            self.assertTrue(res_hold["ok"])
            self.assertEqual(res_hold["outcome"], "awaiting_human_timeout_hold")

            # 4. P1-6: Live kill-switch flipped OFF between open and resolve forces safe_exit_recovery even on retry!
            with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_ENABLED": "1", "HEARTHCORE_COOP_DUNGEONS": "stockade_n"}, clear=False):
                b_live = cont.open_boundary(
                    opportunity_id=63, native_run_id="run-live-ks", dungeon_id="stockade_n", faction="alliance",
                    group_id=777, leader_persona_id=party["ironbulwark"]["persona_id"],
                    boundary_kind="wipe_recovery", members=_members_list(party),
                    safe_boundary=True, in_combat=False, recovery_reason="overpull_recovery",
                    is_shadow=False, now_ts=BASE_TS,
                )
                self.assertTrue(b_live["ok"], b_live)
                for slug, info in party.items():
                    self.assertTrue(
                        cont.submit_member_decision(
                            b_live["boundary_id"], expected_revision=1, persona_id=info["persona_id"],
                            character_guid=info["guid"], decision="retry", candidate=world["companions"][slug],
                            now_ts=BASE_TS + 5, tz=timezone.utc,
                        )["ok"]
                    )
            # Now flip kill switch OFF before resolve!
            with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_ENABLED": "0"}, clear=False):
                res_ks = cont.resolve_boundary(
                    b_live["boundary_id"], expected_revision=1, world_ctx=world,
                    now_ts=BASE_TS + 10, tz=timezone.utc,
                )
                self.assertTrue(res_ks["ok"])
                self.assertEqual(res_ks["outcome"], "safe_exit_recovery")
                self.assertEqual(res_ks["outcome_reason"], "coop_execution_disabled")


if __name__ == "__main__":
    unittest.main()
