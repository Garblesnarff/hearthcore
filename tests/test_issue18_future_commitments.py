"""Tests for Issue #18: Future voluntary dungeon commitments across staggered shifts."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from hearthcore.commitments import (
    FutureCommitmentCoordinator,
    ScheduleAdjustmentPolicy,
    validate_adjustment_policy,
)
from hearthcore.dungeons import (
    LFG_ROLE_DAMAGE,
    LFG_ROLE_HEALER,
    LFG_ROLE_TANK,
    compute_schedule_remaining_minutes,
    enumerate_schedule_windows,
)
from hearthcore.identity import PersonaRegistry
from hearthcore.opportunities import OpportunityCoordinator, run_shadow_match_snapshot

# Fixed Monday 2026-11-30 08:00:00 UTC (c_wday = 1 -> bit 2, all days = 127)
BASE_TS = int(datetime(2026, 11, 30, 8, 0, 0, tzinfo=timezone.utc).timestamp())


def _wal_aware_db_digest(db_path: Path) -> str:
    h = hashlib.sha256()
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{db_path}{suffix}")
        h.update(suffix.encode("utf-8"))
        if p.is_file():
            h.update(b":present:")
            h.update(p.read_bytes())
        else:
            h.update(b":absent")
    if db_path.is_file():
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        try:
            for line in conn.iterdump():
                h.update(line.encode("utf-8"))
                h.update(b"\n")
        finally:
            conn.close()
    return h.hexdigest()


def _mk_live_comp(
    guid: int,
    name: str,
    *,
    class_id: int,
    spec_tab: int,
    role_mask: int,
    spells: list[int],
    has_shield: bool = False,
    online: bool = True,
    level: int = 24,
    race_id: int = 1,
    schedule: dict | None = None,
    locked_instances: list | None = None,
    in_combat: bool = False,
    in_group: bool = False,
    in_lfg_queue: bool = False,
    is_dead: bool = False,
    shift_group: str = "regular",
    now_ts: int = BASE_TS,
) -> dict:
    return {
        "guid": guid,
        "realm_id": 1,
        "name": name,
        "online": online,
        "level": level,
        "race_id": race_id,
        "faction": "alliance" if race_id in (1, 3, 4, 7, 11) else "horde",
        "class_id": class_id,
        "shift_group": shift_group,
        "map_id": 0,
        "zone_id": 1519,
        "in_group": in_group,
        "schedule": schedule if schedule is not None else {"start_hour": 10, "end_hour": 16, "days_mask": 127},
        "locked_instances": [] if locked_instances is None else locked_instances,
        "telemetry_observed_at": now_ts,
        "live_telemetry": {
            "class_id": class_id,
            "spec_tab": spec_tab,
            "native_role_mask": role_mask,
            "known_spells": spells,
            "talent_spells": [],
            "equipped": {
                "has_main_hand": True,
                "has_shield": has_shield,
                "broken_slots": 0,
                "min_durability_pct": 100.0,
            },
            "in_combat": in_combat,
            "in_lfg_queue": in_lfg_queue,
            "in_lfg_queue_dungeons": [],
            "is_dead": is_dead,
            "native_lfg_eligible": True,
            "native_lfg_lock_reason": "",
        },
    }


def _seed_five_personas(db_path: Path, now_ts: int = BASE_TS) -> dict[str, dict]:
    """Register 5 enrolled non-core Alliance personas (1T/1H/3D) in PersonaRegistry."""
    reg = PersonaRegistry(db_path)
    roster = {
        "ironbulwark": {"guid": 101, "name": "Ironbulwark", "role": "tank", "class_name": "warrior", "class_id": 1, "spec_tab": 2, "role_mask": LFG_ROLE_TANK, "spells": [71, 355], "has_shield": True},
        "vespera": {"guid": 102, "name": "Vespera", "role": "healer", "class_name": "priest", "class_id": 5, "spec_tab": 1, "role_mask": LFG_ROLE_HEALER, "spells": [2061], "has_shield": False},
        "kraghammer": {"guid": 103, "name": "Kraghammer", "role": "damage", "class_name": "hunter", "class_id": 3, "spec_tab": 0, "role_mask": LFG_ROLE_DAMAGE, "spells": [], "has_shield": False},
        "emberveil": {"guid": 104, "name": "Emberveil", "role": "damage", "class_name": "mage", "class_id": 8, "spec_tab": 2, "role_mask": LFG_ROLE_DAMAGE, "spells": [], "has_shield": False},
        "duskblade": {"guid": 105, "name": "Duskblade", "role": "damage", "class_name": "rogue", "class_id": 4, "spec_tab": 1, "role_mask": LFG_ROLE_DAMAGE, "spells": [], "has_shield": False},
    }
    sync_payload = {
        slug: {
            "guid": info["guid"],
            "realm_id": 1,
            "name": info["name"],
            "faction": "alliance",
            "shift_group": "regular",
            "online": False,
        }
        for slug, info in roster.items()
    }
    reg.sync_from_companions(sync_payload, now_ts=now_ts)
    for slug, info in roster.items():
        p = reg.persona_for_guid(info["guid"], realm_id=1)
        assert p is not None
        info["persona_id"] = p.persona_id
        info["incarnation"] = 1
    return roster


def _build_live_world(roster: dict[str, dict], *, now_ts: int, schedule: dict | None = None, overrides: dict[str, dict] | None = None) -> dict:
    ovs = overrides or {}
    comps = {}
    for slug, info in roster.items():
        kw = {
            "schedule": schedule or {"start_hour": 12, "end_hour": 16, "days_mask": 127},
            **(ovs.get(slug) or {}),
        }
        comps[slug] = _mk_live_comp(
            info["guid"],
            info["name"],
            class_id=info["class_id"],
            spec_tab=info["spec_tab"],
            role_mask=info["role_mask"],
            spells=info["spells"],
            has_shield=info["has_shield"],
            now_ts=now_ts,
            **kw,
        )
    return {
        "observed_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "companions": comps,
    }


class TestScheduleWindowEnumeration(unittest.TestCase):
    def test_daytime_overnight_and_fail_closed_schedules(self) -> None:
        wins, st = enumerate_schedule_windows(
            {"start_hour": 10, "end_hour": 16, "days_mask": 127},
            start_ts=BASE_TS,
            horizon_hours=24,
            tz=timezone.utc,
        )
        self.assertEqual(st, "ok")
        self.assertEqual(len(wins), 1)
        self.assertEqual(wins[0], (BASE_TS + 2 * 3600, BASE_TS + 8 * 3600))

        wins_ov, st_ov = enumerate_schedule_windows(
            {"start_hour": 22, "end_hour": 4, "days_mask": 127},
            start_ts=BASE_TS,
            horizon_hours=24,
            tz=timezone.utc,
        )
        self.assertEqual(st_ov, "ok")
        self.assertEqual(wins_ov[0], (BASE_TS + 14 * 3600, BASE_TS + 20 * 3600))

        self.assertEqual(enumerate_schedule_windows(None, start_ts=BASE_TS)[1], "missing_schedule_telemetry")
        self.assertEqual(
            enumerate_schedule_windows({"start_hour": 99, "end_hour": 10, "days_mask": 127}, start_ts=BASE_TS)[1],
            "invalid_schedule_telemetry",
        )

    def test_differential_parity_with_compute_schedule_remaining_minutes_across_dst(self) -> None:
        """P2-14: Differential property test across America/New_York DST spring/fall, overnight, end_hour=24, start==end."""
        ny = ZoneInfo("America/New_York")
        test_schedules = [
            {"start_hour": 9, "end_hour": 17, "days_mask": 0b0111110},   # Mon-Fri daytime
            {"start_hour": 22, "end_hour": 4, "days_mask": 0b0000010},   # Monday-only overnight
            {"start_hour": 0, "end_hour": 24, "days_mask": 0b1000001},   # Weekend full-day (0..24)
            {"start_hour": 6, "end_hour": 6, "days_mask": 0b0001000},    # Wednesday full-day (start==end)
        ]
        # Spring forward (2026-03-08) and Fall back (2026-11-01)
        anchors = [
            int(datetime(2026, 3, 7, 12, 0, tzinfo=ny).timestamp()),
            int(datetime(2026, 10, 31, 12, 0, tzinfo=ny).timestamp()),
        ]
        for anchor in anchors:
            for sched in test_schedules:
                wins, st = enumerate_schedule_windows(sched, start_ts=anchor, horizon_hours=72, tz=ny)
                self.assertIn(st, ("ok", "no_windows_in_horizon"))
                # Sample every 30 minutes across 72h and verify exact membership parity!
                for step in range(0, 72 * 2):
                    probe = anchor + (step * 1800)
                    in_win = any(ws <= probe < we for ws, we in wins)
                    rem, _ = compute_schedule_remaining_minutes(sched, now_ts=probe, tz=ny)
                    self.assertEqual(
                        in_win,
                        (rem is not None and rem > 0),
                        f"Parity mismatch at {datetime.fromtimestamp(probe, tz=ny)} for {sched}",
                    )


class TestIssue18FutureCommitments(unittest.TestCase):
    def test_ac1_cross_session_discovery_without_offline_queueing(self) -> None:
        """Future intent persists across sessions and is discoverable next session without offline LFG/opportunity rows."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "brain.db"
            runtime_dir = Path(td) / "runtime"
            roster = _seed_five_personas(db_path)
            OpportunityCoordinator(db_path)
            coord = FutureCommitmentCoordinator(db_path)

            tank = roster["ironbulwark"]
            res = coord.advertise_future_intent(
                persona_id=tank["persona_id"],
                character_guid=tank["guid"],
                character_slug="ironbulwark",
                faction="alliance",
                dungeon_id="stockade_n",
                roles=["tank"],
                class_name="warrior",
                reason_code="wants_stockade_later",
                is_shadow=True,
                now_ts=BASE_TS,
            )
            self.assertTrue(res["ok"], res)

            # P2-3: Intent whose earliest_start_ts is past expires_at fails closed
            bad_exp = coord.advertise_future_intent(
                persona_id=roster["vespera"]["persona_id"],
                character_guid=roster["vespera"]["guid"],
                character_slug="vespera",
                faction="alliance",
                dungeon_id="stockade_n",
                roles=["healer"],
                earliest_start_ts=BASE_TS + 10 * 3600,
                ttl_sec=3600,
                is_shadow=True,
                now_ts=BASE_TS,
            )
            self.assertFalse(bad_exp["ok"])
            self.assertEqual(bad_exp["reason"], "window_shorter_than_dungeon")

            conn = sqlite3.connect(db_path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM opportunity_interests").fetchone()[0], 0)
            finally:
                conn.close()

            coord2 = FutureCommitmentCoordinator(db_path)
            found = coord2.discover_future_intents(dungeon_id="stockade_n", is_shadow=True, now_ts=BASE_TS + 4 * 3600)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["persona_id"], tank["persona_id"])

            before_hash = _wal_aware_db_digest(db_path)
            shadow = run_shadow_match_snapshot(
                {"companions": {}, "observed_at": "2026-11-30T12:00:00Z"},
                db_path=db_path,
                runtime_dir=runtime_dir,
                vitals={"mem_avail_gib": 3.0, "tick_p99": 12.0},
                now_ts=BASE_TS + 4 * 3600,
            )
            self.assertEqual(before_hash, _wal_aware_db_digest(db_path))
            st_entry = next(d for d in shadow["shadow_dungeons"] if d["dungeon_id"] == "stockade_n")
            self.assertEqual(len(st_entry["future_intents"]), 1)

    def test_p0_4_exact_capacity_checked_window_and_group_cap_enforced(self) -> None:
        """QA Req #3 (P0-4): Stored window equals capacity-checked window; group cap=1 cannot be bypassed."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "brain.db"
            roster = _seed_five_personas(db_path)
            # Also register a second 5-player party so two parties can compete for group cap=1
            reg = PersonaRegistry(db_path)
            party2 = {
                "tank2": {"guid": 201, "name": "Tanktwo", "role": "tank"},
                "heal2": {"guid": 202, "name": "Healtwo", "role": "healer"},
                "dps2a": {"guid": 203, "name": "Dpstwoa", "role": "damage"},
                "dps2b": {"guid": 204, "name": "Dpstwob", "role": "damage"},
                "dps2c": {"guid": 205, "name": "Dpstwoc", "role": "damage"},
            }
            reg.sync_from_companions(
                {s: {"guid": v["guid"], "realm_id": 1, "name": v["name"], "faction": "alliance", "shift_group": "regular", "online": False} for s, v in party2.items()},
                now_ts=BASE_TS,
            )
            for v in party2.values():
                v["persona_id"] = reg.persona_for_guid(v["guid"], realm_id=1).persona_id
                v["incarnation"] = 1

            coord = FutureCommitmentCoordinator(db_path)
            # Party 1 only available 14:00-16:00 -> takes [14:00, 14:48] (grace=0)
            for slug, info in roster.items():
                coord.advertise_future_intent(
                    persona_id=info["persona_id"], character_guid=info["guid"], character_slug=slug,
                    faction="alliance", dungeon_id="stockade_n", roles=[info["role"]], is_shadow=True, now_ts=BASE_TS,
                )
            sched_a = {info["persona_id"]: {"start_hour": 14, "end_hour": 16, "days_mask": 127} for info in roster.values()}
            prop_a = coord.propose_future_commitment(
                dungeon_id="stockade_n", roster_schedules=sched_a, max_concurrent_online=15,
                max_managed_groups=1, activation_grace_sec=0, is_shadow=True, now_ts=BASE_TS, tz=timezone.utc,
            )
            self.assertTrue(prop_a["ok"], prop_a)
            req_sec = (30 + 8 + 10) * 60
            self.assertEqual(prop_a["window_start"], BASE_TS + 6 * 3600)  # 14:00
            self.assertEqual(prop_a["window_end"], prop_a["window_start"] + req_sec)  # EXACT 14:48, NOT 16:00!

            # First verify explicit group_capacity_exceeded while prop_a is alive at [14:00, 14:48]!
            for slug, info in party2.items():
                coord.advertise_future_intent(
                    persona_id=info["persona_id"], character_guid=info["guid"], character_slug=slug,
                    faction="alliance", dungeon_id="stockade_n", roles=[info["role"]],
                    earliest_start_ts=BASE_TS + 6 * 3600, latest_end_ts=BASE_TS + 6 * 3600 + req_sec,
                    is_shadow=True, now_ts=BASE_TS + 30,
                )
            sched_collide = {**sched_a, **{info["persona_id"]: {"start_hour": 14, "end_hour": 15, "days_mask": 127} for info in party2.values()}}
            blocked_cap = coord.propose_future_commitment(
                dungeon_id="stockade_n", roster_schedules=sched_collide, max_concurrent_online=15,
                max_managed_groups=1, activation_grace_sec=0, horizon_hours=8, is_shadow=True, now_ts=BASE_TS + 30, tz=timezone.utc,
            )
            self.assertFalse(blocked_cap["ok"])
            self.assertEqual(blocked_cap["reason"], "group_capacity_exceeded")

            # Now Party 2 widens availability to 12:00-16:00 -> gets EXACT [12:00, 12:48], NEVER widened into 14:00!
            for slug, info in party2.items():
                coord.advertise_future_intent(
                    persona_id=info["persona_id"], character_guid=info["guid"], character_slug=slug,
                    faction="alliance", dungeon_id="stockade_n", roles=[info["role"]], is_shadow=True, now_ts=BASE_TS + 40,
                )
            sched_both = {
                **sched_a,
                **{info["persona_id"]: {"start_hour": 12, "end_hour": 16, "days_mask": 127} for info in party2.values()},
            }
            prop_b = coord.propose_future_commitment(
                dungeon_id="stockade_n", roster_schedules=sched_both, max_concurrent_online=15,
                max_managed_groups=1, activation_grace_sec=0, is_shadow=True, now_ts=BASE_TS + 40, tz=timezone.utc,
            )
            self.assertTrue(prop_b["ok"], prop_b)
            self.assertEqual(prop_b["window_start"], BASE_TS + 4 * 3600)  # 12:00
            self.assertEqual(prop_b["window_end"], prop_b["window_start"] + req_sec)  # EXACT 12:48!

            # Accept Party 2, then attempt late activation at 14:30 (inside Party 1's slot) -> REJECTED as expired!
            for info in party2.values():
                coord.respond_to_commitment(
                    prop_b["commitment_id"], expected_revision=1, persona_id=info["persona_id"],
                    character_guid=info["guid"], incarnation=1, accepted=True, now_ts=BASE_TS + 60,
                )
            late_act = coord.revalidate_and_activate_commitment(
                prop_b["commitment_id"], world_ctx={"companions": {}}, now_ts=BASE_TS + 6 * 3600 + 1800, tz=timezone.utc,
            )
            self.assertFalse(late_act["ok"])
            self.assertEqual(late_act["reason"], "commitment_expired")

    def test_p0_1_p0_2_p0_3_deferred_target_activated_override_and_race_blocking(self) -> None:
        """QA Req #1 & #2: Deferred target recall closes target intent; activated override releases opp; mid-activation override blocks activation."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "brain.db"
            roster = _seed_five_personas(db_path)
            opp_coord = OpportunityCoordinator(db_path)
            coord = FutureCommitmentCoordinator(db_path)
            schedules = {info["persona_id"]: {"start_hour": 12, "end_hour": 16, "days_mask": 127} for info in roster.values()}

            def _make_scheduled(t0: int) -> int:
                for slug, info in roster.items():
                    r = coord.advertise_future_intent(
                        persona_id=info["persona_id"], character_guid=info["guid"], character_slug=slug,
                        faction="alliance", dungeon_id="stockade_n", roles=[info["role"]], is_shadow=True, now_ts=t0,
                    )
                    self.assertTrue(r["ok"], r)
                p = coord.propose_future_commitment(
                    dungeon_id="stockade_n", roster_schedules=schedules, max_concurrent_online=10,
                    is_shadow=True, now_ts=t0, tz=timezone.utc,
                )
                self.assertTrue(p["ok"], p)
                for info in roster.values():
                    ack = coord.respond_to_commitment(
                        p["commitment_id"], expected_revision=1, persona_id=info["persona_id"],
                        character_guid=info["guid"], incarnation=1, accepted=True, now_ts=t0 + 60,
                    )
                    self.assertTrue(ack["ok"], ack)
                return p["commitment_id"]

            # 1. Deferred owner recall with target -> honored at window -> recalled tank's intent is CLOSED (not reopened!)
            cid1 = _make_scheduled(BASE_TS)
            tank_pid = roster["ironbulwark"]["persona_id"]
            def_res = coord.override_commitment(
                cid1, override_kind="owner_recall", target_persona_id=tank_pid, safe_boundary=False, now_ts=BASE_TS + 300,
            )
            self.assertTrue(def_res["ok"])
            self.assertEqual(def_res["pending_override_target"], tank_pid)

            win_open = BASE_TS + 4 * 3600
            act1 = coord.revalidate_and_activate_commitment(
                cid1, world_ctx=_build_live_world(roster, now_ts=win_open), opportunity_coordinator=opp_coord, now_ts=win_open, tz=timezone.utc,
            )
            self.assertFalse(act1["ok"])
            self.assertEqual(act1["reason"], "overridden_owner_recall")
            # Tank's intent MUST NOT be open; other 4 teammates' intents ARE reopened!
            open_after = coord.discover_future_intents(dungeon_id="stockade_n", is_shadow=True, now_ts=win_open + 1)
            self.assertEqual(len(open_after), 4)
            self.assertNotIn(tank_pid, {i["persona_id"] for i in open_after})
            # Re-proposing cannot book tank again!
            reprop = coord.propose_future_commitment(
                dungeon_id="stockade_n", roster_schedules=schedules, max_concurrent_online=10,
                is_shadow=True, now_ts=win_open + 10, tz=timezone.utc,
            )
            self.assertFalse(reprop["ok"])
            self.assertEqual(reprop["reason"], "insufficient_future_intents")

            # 2. Override an ACTIVATED commitment -> requires explicit safe_boundary & releases linked OpportunityCoordinator party!
            t2 = BASE_TS + 86400
            cid2 = _make_scheduled(t2)
            win_open2 = t2 + 4 * 3600
            act2 = coord.revalidate_and_activate_commitment(
                cid2, world_ctx=_build_live_world(roster, now_ts=win_open2), opportunity_coordinator=opp_coord, now_ts=win_open2, tz=timezone.utc,
            )
            self.assertTrue(act2["ok"], act2)
            linked_id = act2["linked_opportunity_id"]
            # Omitting safe_boundary on activated fails closed (P2-15)
            no_sb = coord.override_commitment(cid2, override_kind="owner_recall", opportunity_coordinator=opp_coord, now_ts=win_open2 + 10)
            self.assertFalse(no_sb["ok"])
            self.assertEqual(no_sb["reason"], "safe_boundary_required_for_active")
            # Deferring on activated, then honoring via process_pending_overrides releases linked opportunity!
            def_act = coord.override_commitment(
                cid2, override_kind="owner_recall", target_persona_id=tank_pid, safe_boundary=False, now_ts=win_open2 + 20,
            )
            self.assertTrue(def_act["ok"])
            proc = coord.process_pending_overrides(
                opportunity_coordinator=opp_coord, safe_boundary_fn=lambda _row: True, now_ts=win_open2 + 30,
            )
            self.assertEqual(len(proc["processed"]), 1)
            self.assertEqual(proc["processed"][0]["result"]["state"], "overridden_owner_recall")
            opp_after = opp_coord.get_opportunity(linked_id)
            assert opp_after is not None
            self.assertEqual(opp_after["state"], "cancelled")

            # 3. P0-3: Deferred override arriving BETWEEN Step 1 and Step 3 blocks activation and releases newly created opp!
            t3 = BASE_TS + 2 * 86400
            cid3 = _make_scheduled(t3)
            win_open3 = t3 + 4 * 3600
            orig_create = opp_coord.create_opportunity

            def _inject_mid_activation_override(*args, **kwargs):
                created = orig_create(*args, **kwargs)
                # Inject deferred recall while cid3 is in 'activating'!
                coord.override_commitment(
                    cid3, override_kind="owner_recall", target_persona_id=tank_pid, safe_boundary=False, now_ts=win_open3 + 1,
                )
                return created

            with mock.patch.object(opp_coord, "create_opportunity", side_effect=_inject_mid_activation_override):
                raced = coord.revalidate_and_activate_commitment(
                    cid3, world_ctx=_build_live_world(roster, now_ts=win_open3), opportunity_coordinator=opp_coord, now_ts=win_open3, tz=timezone.utc,
                )
            self.assertFalse(raced["ok"])
            self.assertEqual(raced["reason"], "overridden_owner_recall")
            c3 = coord.get_commitment(cid3)
            assert c3 is not None
            self.assertEqual(c3["state"], "overridden_owner_recall")
            # Ensure the newly created opportunity was compensated/cancelled!
            conn = sqlite3.connect(db_path)
            try:
                recruiting_left = conn.execute("SELECT COUNT(*) FROM opportunities WHERE state='recruiting'").fetchone()[0]
                self.assertEqual(recruiting_left, 0)
            finally:
                conn.close()

    def test_p1_1_p1_2_weekly_quota_inflight_cross_dungeon_and_refund_on_withdraw(self) -> None:
        """QA Req #4: Weekly quota=1 blocks second in-flight adjusted proposal (different dungeon) and refunds on teammate withdraw."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "brain.db"
            roster = _seed_five_personas(db_path)
            # Add a second tank (08-13) for Deadmines (needs 67m; 12-13 is only 60m so Vespera 12-18 MUST slide -30m!)
            reg = PersonaRegistry(db_path)
            reg.sync_from_companions(
                {"tank_dm": {"guid": 199, "realm_id": 1, "name": "Tankdm", "faction": "alliance", "shift_group": "regular", "online": False}},
                now_ts=BASE_TS,
            )
            tank_dm_pid = reg.persona_for_guid(199, realm_id=1).persona_id
            coord = FutureCommitmentCoordinator(db_path)

            near_miss = {
                roster["ironbulwark"]["persona_id"]: {"start_hour": 8, "end_hour": 12, "days_mask": 127},  # Stockade: 0m overlap w/ Vespera
                tank_dm_pid: {"start_hour": 8, "end_hour": 13, "days_mask": 127},                          # Deadmines: 60m < 67m overlap w/ Vespera
                roster["vespera"]["persona_id"]: {"start_hour": 12, "end_hour": 18, "days_mask": 127},
                roster["kraghammer"]["persona_id"]: {"start_hour": 10, "end_hour": 16, "days_mask": 127},
                roster["emberveil"]["persona_id"]: {"start_hour": 10, "end_hour": 16, "days_mask": 127},
                roster["duskblade"]["persona_id"]: {"start_hour": 10, "end_hour": 16, "days_mask": 127},
            }
            policy = ScheduleAdjustmentPolicy(
                enabled=True, policy_id="pol-slide-1", reviewed_by="wonky",
                max_shift_start_early_minutes=30, max_shift_end_extend_minutes=30,
                preserve_total_shift_duration=True, max_adjustments_per_persona_per_week=1,
            )

            # Advertise intents for stockade_n (with ironbulwark) AND deadmines_n (with tank_dm, sharing vespera!)
            for slug, info in roster.items():
                r = coord.advertise_future_intent(
                    persona_id=info["persona_id"], character_guid=info["guid"], character_slug=slug,
                    faction="alliance", dungeon_id="stockade_n", roles=[info["role"]],
                    allow_bounded_adjustment=True, is_shadow=True, now_ts=BASE_TS,
                )
                self.assertTrue(r["ok"], r)
                if slug != "ironbulwark":
                    r2 = coord.advertise_future_intent(
                        persona_id=info["persona_id"], character_guid=info["guid"], character_slug=slug,
                        faction="alliance", dungeon_id="deadmines_n", roles=[info["role"]],
                        allow_bounded_adjustment=True, is_shadow=True, now_ts=BASE_TS,
                    )
                    self.assertTrue(r2["ok"], r2)
            self.assertTrue(
                coord.advertise_future_intent(
                    persona_id=tank_dm_pid, character_guid=199, character_slug="tank_dm",
                    faction="alliance", dungeon_id="deadmines_n", roles=["tank"],
                    allow_bounded_adjustment=False, is_shadow=True, now_ts=BASE_TS,
                )["ok"]
            )

            # Proposal #1 (stockade_n) uses Vespera's 1/week adjustment quota while STILL in 'proposed'!
            p1 = coord.propose_future_commitment(
                dungeon_id="stockade_n", roster_schedules=near_miss, max_concurrent_online=10,
                adjustment_policy=policy, activation_grace_sec=0, is_shadow=True, now_ts=BASE_TS, tz=timezone.utc,
            )
            self.assertTrue(p1["ok"], p1)
            self.assertIn(roster["vespera"]["persona_id"], p1["adjustment_applied"])

            # Proposal #2 (deadmines_n, with max_managed_groups=2 so group cap isn't the blocker) MUST BE REJECTED because in-flight p1 already holds Vespera's 1/week quota!
            p2 = coord.propose_future_commitment(
                dungeon_id="deadmines_n", roster_schedules=near_miss, max_concurrent_online=10,
                max_managed_groups=2, adjustment_policy=policy, activation_grace_sec=0, horizon_hours=48, is_shadow=True, now_ts=BASE_TS + 10, tz=timezone.utc,
            )
            self.assertFalse(p2["ok"])
            self.assertEqual(p2["reason"], "no_overlapping_role_composition")

            # Teammate (DPS) withdraws from p1 -> quota MUST be refunded so Vespera can immediately match deadmines_n!
            w = coord.withdraw_commitment(p1["commitment_id"], persona_id=roster["duskblade"]["persona_id"], now_ts=BASE_TS + 60)
            self.assertTrue(w["ok"])
            p2_retry = coord.propose_future_commitment(
                dungeon_id="deadmines_n", roster_schedules=near_miss, max_concurrent_online=10,
                max_managed_groups=2, adjustment_policy=policy, activation_grace_sec=0, horizon_hours=48, is_shadow=True, now_ts=BASE_TS + 70, tz=timezone.utc,
            )
            self.assertTrue(p2_retry["ok"], p2_retry)

    def test_p1_3_p1_4_adjusted_commitment_activates_end_to_end_and_slide_protects_existing_commitments(self) -> None:
        """QA Req #5: Adjusted commitment activates end-to-end AND shift slide refuses to drop time overlapping an existing commitment."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "brain.db"
            roster = _seed_five_personas(db_path)
            opp_coord = OpportunityCoordinator(db_path)
            coord = FutureCommitmentCoordinator(db_path)

            near_miss = {
                roster["ironbulwark"]["persona_id"]: {"start_hour": 8, "end_hour": 12, "days_mask": 127},
                roster["vespera"]["persona_id"]: {"start_hour": 12, "end_hour": 18, "days_mask": 127},
                roster["kraghammer"]["persona_id"]: {"start_hour": 10, "end_hour": 16, "days_mask": 127},
                roster["emberveil"]["persona_id"]: {"start_hour": 10, "end_hour": 16, "days_mask": 127},
                roster["duskblade"]["persona_id"]: {"start_hour": 10, "end_hour": 16, "days_mask": 127},
            }
            policy = ScheduleAdjustmentPolicy(
                enabled=True, policy_id="pol-e2e-slide", reviewed_by="wonky",
                max_shift_start_early_minutes=30, max_shift_end_extend_minutes=30,
                preserve_total_shift_duration=True, max_adjustments_per_persona_per_week=1,
            )

            # First verify P1-4: If tank already has a commitment at 08:00-08:48, sliding 08:00-12:00 to 08:30-12:30
            # would REMOVE 08:00-08:30 and MUST BE REJECTED!
            for slug, info in roster.items():
                coord.advertise_future_intent(
                    persona_id=info["persona_id"], character_guid=info["guid"], character_slug=slug,
                    faction="alliance", dungeon_id="stockade_n", roles=[info["role"]],
                    allow_bounded_adjustment=True, is_shadow=True, now_ts=BASE_TS,
                )
            # Insert an existing scheduled commitment for tank at 08:00-08:48 on Day 1
            conn = sqlite3.connect(db_path)
            try:
                cur = conn.execute(
                    "INSERT INTO future_commitments (dungeon_id, faction, window_start, window_end, estimated_minutes, required_minutes, accept_until, state, is_shadow, created_at, updated_at) "
                    "VALUES ('deadmines_n','alliance',?,?,45,48,?,'scheduled',1,?,?)",
                    (BASE_TS, BASE_TS + 2880, BASE_TS + 60, BASE_TS, BASE_TS),
                )
                c_block = cur.lastrowid
                conn.execute(
                    "INSERT INTO future_commitment_members (commitment_id, persona_id, character_guid, character_slug, role, state, created_at, updated_at) "
                    "VALUES (?,?,101,'ironbulwark','tank','accepted',?,?)",
                    (c_block, roster["ironbulwark"]["persona_id"], BASE_TS, BASE_TS),
                )
                conn.commit()
            finally:
                conn.close()

            # Horizon of 6h (08:00-14:00 today only) -> cannot slide today's 08:00-12:00 because 08:00-08:30 is busy!
            blocked_slide = coord.propose_future_commitment(
                dungeon_id="stockade_n", roster_schedules=near_miss, max_concurrent_online=10,
                adjustment_policy=policy, activation_grace_sec=0, horizon_hours=6, is_shadow=True, now_ts=BASE_TS, tz=timezone.utc,
            )
            self.assertFalse(blocked_slide["ok"])
            self.assertEqual(blocked_slide["reason"], "no_overlapping_role_composition")

            # Remove blocking Day-1 commitment and run full end-to-end adjusted activation at 11:30 UTC!
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("DELETE FROM future_commitment_members WHERE commitment_id=?", (c_block,))
                conn.execute("DELETE FROM future_commitments WHERE id=?", (c_block,))
                conn.commit()
            finally:
                conn.close()

            prop = coord.propose_future_commitment(
                dungeon_id="stockade_n", roster_schedules=near_miss, max_concurrent_online=10,
                adjustment_policy=policy, activation_grace_sec=300, horizon_hours=10, is_shadow=True, now_ts=BASE_TS, tz=timezone.utc,
            )
            self.assertTrue(prop["ok"], prop)
            cid = prop["commitment_id"]
            for info in roster.values():
                ack = coord.respond_to_commitment(
                    cid, expected_revision=1, persona_id=info["persona_id"],
                    character_guid=info["guid"], incarnation=1, accepted=True, now_ts=BASE_TS + 60,
                )
                self.assertTrue(ack["ok"], ack)

            # Verify get_active_schedule_overrides exposes the approved temporary slide
            overrides = coord.get_active_schedule_overrides(is_shadow=True, now_ts=BASE_TS + 3 * 3600)
            self.assertIn(roster["ironbulwark"]["persona_id"], overrides)
            self.assertIn(roster["vespera"]["persona_id"], overrides)

            # At 11:30 UTC (win_start), scheduler applied slide so ALL 5 bots are online (even though natural shifts are 08-12 & 12-18!)
            win_start = prop["window_start"]  # 11:30 UTC
            world_at_1130 = _build_live_world(
                roster,
                now_ts=win_start,
                overrides={
                    "ironbulwark": {"schedule": {"start_hour": 8, "end_hour": 12, "days_mask": 127}},
                    "vespera": {"schedule": {"start_hour": 12, "end_hour": 18, "days_mask": 127}},
                    "kraghammer": {"schedule": {"start_hour": 10, "end_hour": 16, "days_mask": 127}},
                    "emberveil": {"schedule": {"start_hour": 10, "end_hour": 16, "days_mask": 127}},
                    "duskblade": {"schedule": {"start_hour": 10, "end_hour": 16, "days_mask": 127}},
                },
            )
            act = coord.revalidate_and_activate_commitment(
                cid, world_ctx=world_at_1130, opportunity_coordinator=opp_coord, now_ts=win_start, tz=timezone.utc,
            )
            self.assertTrue(act["ok"], act)
            self.assertEqual(act["state"], "activated")
            self.assertGreater(act["linked_opportunity_id"], 0)

    def test_p1_5_p1_6_p1_7_transient_retry_concurrent_activation_crash_recovery_and_live_gates(self) -> None:
        """Transient offline poll retries within grace; concurrent activations never double-activate; live coop defaults OFF."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "brain.db"
            roster = _seed_five_personas(db_path)
            opp_coord = OpportunityCoordinator(db_path)
            coord = FutureCommitmentCoordinator(db_path)
            schedules = {info["persona_id"]: {"start_hour": 12, "end_hour": 16, "days_mask": 127} for info in roster.values()}

            # Live coop OFF by default when env vars UNSET!
            tank = roster["ironbulwark"]
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("HEARTHCORE_COOP_ENABLED", None)
                os.environ.pop("HEARTHCORE_COOP_DUNGEONS", None)
                self.assertEqual(
                    coord.advertise_future_intent(
                        persona_id=tank["persona_id"], character_guid=tank["guid"], character_slug="ironbulwark",
                        faction="alliance", dungeon_id="stockade_n", roles=["tank"], is_shadow=False, now_ts=BASE_TS,
                    )["reason"],
                    "coop_execution_disabled",
                )
                self.assertEqual(
                    coord.propose_future_commitment(
                        dungeon_id="stockade_n", roster_schedules=schedules, max_concurrent_online=10, is_shadow=False, now_ts=BASE_TS,
                    )["reason"],
                    "coop_execution_disabled",
                )

            # Schedule a shadow commitment with 5m grace so transient offline retry succeeds on poll 2!
            for slug, info in roster.items():
                coord.advertise_future_intent(
                    persona_id=info["persona_id"], character_guid=info["guid"], character_slug=slug,
                    faction="alliance", dungeon_id="stockade_n", roles=[info["role"]], is_shadow=True, now_ts=BASE_TS,
                )
            p = coord.propose_future_commitment(
                dungeon_id="stockade_n", roster_schedules=schedules, max_concurrent_online=10,
                activation_grace_sec=300, is_shadow=True, now_ts=BASE_TS, tz=timezone.utc,
            )
            cid = p["commitment_id"]
            for info in roster.values():
                coord.respond_to_commitment(
                    cid, expected_revision=1, persona_id=info["persona_id"],
                    character_guid=info["guid"], incarnation=1, accepted=True, now_ts=BASE_TS + 60,
                )

            win_open = p["window_start"]
            # Poll 1 at 12:00: tank hasn't finished logging in yet -> transient retry (stays scheduled!)
            poll1 = coord.revalidate_and_activate_commitment(
                cid,
                world_ctx=_build_live_world(roster, now_ts=win_open, overrides={"ironbulwark": {"online": False}}),
                opportunity_coordinator=opp_coord,
                now_ts=win_open,
                tz=timezone.utc,
            )
            self.assertFalse(poll1["ok"])
            self.assertEqual(poll1["reason"], "revalidation_transient_retry")
            self.assertTrue(poll1["retryable"])
            self.assertEqual(coord.get_commitment(cid)["state"], "scheduled")

            # Poll 2 at 12:01: two threads race to activate simultaneously -> EXACTLY ONE succeeds!
            results: list[dict] = []
            lock = threading.Lock()
            w_ok = _build_live_world(roster, now_ts=win_open + 60)

            def _worker() -> None:
                c_local = FutureCommitmentCoordinator(db_path)
                o_local = OpportunityCoordinator(db_path)
                r = c_local.revalidate_and_activate_commitment(
                    cid, world_ctx=w_ok, opportunity_coordinator=o_local, now_ts=win_open + 60, tz=timezone.utc,
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

            # Verify crash recovery for stuck 'activating' row releases orphan opportunity
            orphan_opp = opp_coord.create_opportunity(dungeon_id="stockade_n", creator_guid=101, now_ts=win_open + 120, is_shadow=True)
            conn = sqlite3.connect(db_path)
            try:
                cur = conn.execute(
                    "INSERT INTO future_commitments (dungeon_id, faction, window_start, window_end, estimated_minutes, required_minutes, accept_until, activating_until, state, linked_opportunity_id, is_shadow, created_at, updated_at) "
                    "VALUES ('stockade_n','alliance',?,?,30,48,?,?,'activating',?,1,?,?)",
                    (win_open, win_open + 7200, win_open, win_open + 100, orphan_opp["id"], BASE_TS, BASE_TS),
                )
                stuck_cid = cur.lastrowid
                conn.commit()
            finally:
                conn.close()
            rec = coord.reconcile_activating(opportunity_coordinator=opp_coord, now_ts=win_open + 200)
            self.assertEqual(rec["reverted_to_scheduled"], 1)
            self.assertEqual(rec["released_orphan_opps"], 1)
            self.assertEqual(coord.get_commitment(stuck_cid)["state"], "scheduled")
            self.assertEqual(opp_coord.get_opportunity(orphan_opp["id"])["state"], "cancelled")

    def test_neutral_decline_allows_immediate_readvertise_without_penalty(self) -> None:
        """Decliner suffers zero cooldown/penalty and can immediately re-advertise & match cleanly."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "brain.db"
            roster = _seed_five_personas(db_path)
            coord = FutureCommitmentCoordinator(db_path)
            schedules = {info["persona_id"]: {"start_hour": 12, "end_hour": 16, "days_mask": 127} for info in roster.values()}

            for slug, info in roster.items():
                coord.advertise_future_intent(
                    persona_id=info["persona_id"], character_guid=info["guid"], character_slug=slug,
                    faction="alliance", dungeon_id="stockade_n", roles=[info["role"]], is_shadow=True, now_ts=BASE_TS,
                )
            p1 = coord.propose_future_commitment(
                dungeon_id="stockade_n", roster_schedules=schedules, max_concurrent_online=10, is_shadow=True, now_ts=BASE_TS, tz=timezone.utc,
            )
            healer = roster["vespera"]
            dec = coord.respond_to_commitment(
                p1["commitment_id"], expected_revision=1, persona_id=healer["persona_id"],
                character_guid=healer["guid"], incarnation=1, accepted=False, now_ts=BASE_TS + 60,
            )
            self.assertTrue(dec["ok"])
            self.assertEqual(dec["state"], "declined")
            # Healer's intent recorded as distinct 'declined_offer' (P2-6) and healer can immediately re-advertise & match!
            re_adv = coord.advertise_future_intent(
                persona_id=healer["persona_id"], character_guid=healer["guid"], character_slug="vespera",
                faction="alliance", dungeon_id="stockade_n", roles=["healer"], is_shadow=True, now_ts=BASE_TS + 61,
            )
            self.assertTrue(re_adv["ok"], re_adv)
            p2 = coord.propose_future_commitment(
                dungeon_id="stockade_n", roster_schedules=schedules, max_concurrent_online=10, is_shadow=True, now_ts=BASE_TS + 62, tz=timezone.utc,
            )
            self.assertTrue(p2["ok"], p2)

    def test_validate_policy_strict_type_checks(self) -> None:
        self.assertEqual(validate_adjustment_policy(None), (False, "policy_disabled"))
        self.assertEqual(
            validate_adjustment_policy(ScheduleAdjustmentPolicy(enabled=True, policy_id="p", reviewed_by="", max_shift_start_early_minutes=15)),
            (False, "policy_unreviewed"),
        )
        self.assertEqual(
            validate_adjustment_policy(ScheduleAdjustmentPolicy(enabled=True, policy_id="p", reviewed_by="wonky", max_shift_start_early_minutes=True)),  # type: ignore[arg-type]
            (False, "policy_invalid_int_bounds"),
        )
        # Valid net-expansion policy <= 15m passes
        self.assertEqual(
            validate_adjustment_policy(
                ScheduleAdjustmentPolicy(
                    enabled=True, policy_id="p-exp", reviewed_by="wonky",
                    max_shift_end_extend_minutes=15, preserve_total_shift_duration=False,
                    allow_net_expansion=True, max_net_expansion_minutes=15,
                )
            ),
            (True, "ok"),
        )


if __name__ == "__main__":
    unittest.main()
