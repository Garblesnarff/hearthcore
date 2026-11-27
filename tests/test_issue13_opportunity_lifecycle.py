"""Behavioral & concurrency regression tests for Issue #13 (opportunity confirmation, TTL & crash reconciliation)."""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from hearthcore.identity import persona_id_for_slug
from hearthcore.opportunities import NativeReconcileSnapshot, OpportunityCoordinator

PARTY = [
    ("ironbulwark", 201, "tank"),
    ("voss", 202, "healer"),
    ("halvar", 203, "damage"),
    ("bryn", 204, "damage"),
    ("lyraeth", 205, "damage"),
]


def _seed_and_reserve(coord: OpportunityCoordinator, *, now: int = 1_800_000_000, is_shadow: bool = False, confirm_ttl: int = 120):
    opp = coord.create_opportunity(dungeon_id="stockade_n", creator_guid=201, is_shadow=is_shadow, now_ts=now)
    for slug, guid, role in PARTY:
        ok = coord.express_interest(
            opportunity_id=opp["id"],
            expected_revision=1,
            persona_id=persona_id_for_slug(slug),
            character_guid=guid,
            character_slug=slug,
            role=role,
            reason_code="voluntary",
            available_until=now + 3600,
            now_ts=now,
        )
        assert ok
    res = coord.reserve_party(opp["id"], expected_revision=1, now_ts=now + 1, confirm_ttl_sec=confirm_ttl)
    assert res["ok"]
    return opp["id"], res["revision"]


class Issue13OpportunityLifecycleTests(unittest.TestCase):
    def test_full_confirmation_native_handoff_exclusivity_idempotency_and_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            coord = OpportunityCoordinator(Path(tmp) / "brain.db")
            now = 1_800_000_000
            oid, rev = _seed_and_reserve(coord, now=now, confirm_ttl=60)

            for idx, (slug, guid, _) in enumerate(PARTY[:4]):
                r = coord.confirm_member(
                    oid, expected_revision=rev, persona_id=persona_id_for_slug(slug),
                    character_guid=guid, accepted=True, ack_token=f"tok_{idx}", now_ts=now + 10,
                )
                self.assertTrue(r["ok"] and not r["all_confirmed"])

            # Duplicate accept with SAME ack_token succeeds idempotently; DIFFERENT ack_token rejected!
            dup = coord.confirm_member(
                oid, expected_revision=rev, persona_id=persona_id_for_slug(PARTY[0][0]),
                character_guid=PARTY[0][1], accepted=True, ack_token="tok_0", now_ts=now + 11,
            )
            self.assertTrue(dup["ok"] and dup.get("idempotent"))
            bad_tok = coord.confirm_member(
                oid, expected_revision=rev, persona_id=persona_id_for_slug(PARTY[0][0]),
                character_guid=PARTY[0][1], accepted=True, ack_token="wrong_tok", now_ts=now + 11,
            )
            self.assertFalse(bad_tok["ok"])
            self.assertEqual(bad_tok["reason"], "ack_token_mismatch")

            # Wrong character GUID & stale revision rejected!
            self.assertEqual(
                coord.confirm_member(
                    oid, expected_revision=rev, persona_id=persona_id_for_slug(PARTY[4][0]),
                    character_guid=999999, accepted=True, now_ts=now + 12,
                )["reason"],
                "character_key_mismatch",
            )
            self.assertFalse(
                coord.confirm_member(
                    oid, expected_revision=rev - 1, persona_id=persona_id_for_slug(PARTY[4][0]),
                    character_guid=PARTY[4][1], accepted=True, now_ts=now + 12,
                )["ok"]
            )

            # 5th member confirms -> advances to 'confirmed' with deterministic correlation_key
            r5 = coord.confirm_member(
                oid, expected_revision=rev, persona_id=persona_id_for_slug(PARTY[4][0]),
                character_guid=PARTY[4][1], accepted=True, ack_token="tok_4", now_ts=now + 15,
            )
            self.assertTrue(r5["ok"] and r5["all_confirmed"] and r5["state"] == "confirmed")
            self.assertTrue(r5["correlation_key"].startswith(f"opp:{oid}:rev"))
            conf_rev = r5["revision"]

            # Native handoff starts queued (no run_id yet) -> foreign completed outcome MUST BE REJECTED (P0.2)!
            nh = coord.record_native_handoff(
                oid, expected_revision=conf_rev, native_state="queued",
                native_queue_id="lfg_q_42", group_id=77, now_ts=now + 20,
            )
            self.assertTrue(nh["ok"] and nh["state"] == "active")
            act_rev = nh["revision"]
            self.assertFalse(
                coord.record_native_outcome(oid, native_run_id="foreign_run", outcome="completed", now_ts=now + 22)["ok"],
                "Unbound run MUST reject foreign completed outcome!",
            )

            # Forward replay binds native_run_id & moves to in_instance; backward state & conflicting ID rejected (P1.4)!
            nh_fwd = coord.record_native_handoff(
                oid, expected_revision=conf_rev, native_state="in_instance",
                native_queue_id="lfg_q_42", group_id=77, native_run_id="run_77", now_ts=now + 25,
            )
            self.assertTrue(nh_fwd["ok"] and nh_fwd["idempotent"])
            self.assertEqual(
                coord.record_native_handoff(
                    oid, expected_revision=conf_rev, native_state="queued",
                    native_queue_id="lfg_q_42", group_id=77, native_run_id="run_77", now_ts=now + 26,
                )["reason"],
                "backward_native_state:in_instance->queued",
            )
            self.assertEqual(
                coord.record_native_handoff(
                    oid, expected_revision=conf_rev, native_state="in_instance",
                    native_queue_id="lfg_q_42", group_id=888, native_run_id="run_77", now_ts=now + 26,
                )["reason"],
                "native_identity_conflict",
            )
            # Duplicate confirm callback even after 'active' is still idempotent!
            dup_act = coord.confirm_member(
                oid, expected_revision=rev, persona_id=persona_id_for_slug(PARTY[0][0]),
                character_guid=PARTY[0][1], accepted=True, ack_token="tok_0", now_ts=now + 30,
            )
            self.assertTrue(dup_act["ok"] and dup_act["idempotent"])

            # Active commitment MUST stay exclusive well past confirmation TTL (now + 5000 >> confirm_ttl=60)!
            opp2 = coord.create_opportunity(dungeon_id="deadmines_n", creator_guid=201, is_shadow=False, now_ts=now + 5000)
            for slug, guid, role in PARTY:
                coord.express_interest(
                    opportunity_id=opp2["id"], expected_revision=1,
                    persona_id=persona_id_for_slug(slug), character_guid=guid,
                    character_slug=slug, role=role, reason_code="second",
                    available_until=now + 9000, now_ts=now + 5000,
                )
            self.assertEqual(coord.reserve_party(opp2["id"], expected_revision=1, now_ts=now + 5005)["reason"], "insufficient_roles")

            # Live active cancel without native_confirmed moves to 'cancelling' (keeps exclusivity until native confirms!)
            c_wait = coord.release_party(oid, "owner_recall", native_confirmed=False, now_ts=now + 5100)
            self.assertEqual(c_wait["state"], "cancelling")
            self.assertEqual(coord.reserve_party(opp2["id"], expected_revision=1, now_ts=now + 5105)["reason"], "insufficient_roles")

            # Genuine native completion with matching native_run_id completes cleanly & frees members!
            done = coord.record_native_outcome(oid, native_run_id="run_77", outcome="completed", now_ts=now + 5500)
            self.assertTrue(done["ok"] and done["state"] == "completed")
            done_dup = coord.record_native_outcome(oid, native_run_id="run_77", outcome="completed", now_ts=now + 5505)
            self.assertTrue(done_dup["ok"] and done_dup["idempotent"])
            self.assertTrue(coord.reserve_party(opp2["id"], expected_revision=1, now_ts=now + 5510)["ok"])

    def test_shadow_rejected_on_native_path_and_partial_decline_and_expiry_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            coord = OpportunityCoordinator(Path(tmp) / "brain.db")
            now = 1_800_000_000

            # 1. Shadow opportunity MUST be rejected by record_native_handoff & record_native_outcome (P1.5)!
            sh_id, sh_rev = _seed_and_reserve(coord, now=now, is_shadow=True)
            for slug, guid, _ in PARTY:
                r = coord.confirm_member(sh_id, expected_revision=sh_rev, persona_id=persona_id_for_slug(slug), character_guid=guid, accepted=True, now_ts=now + 5)
            self.assertEqual(
                coord.record_native_handoff(sh_id, expected_revision=r["revision"], native_state="queued", native_queue_id="q1", now_ts=now + 10)["reason"],
                "shadow_opportunity",
            )
            self.assertEqual(
                coord.record_native_outcome(sh_id, native_run_id="r1", outcome="completed", now_ts=now + 10)["reason"],
                "shadow_opportunity",
            )

            # 2. Partial accept then decline -> whole party released; duplicate decline idempotent; late handoff compensates!
            oid1, rev1 = _seed_and_reserve(coord, now=now + 10, confirm_ttl=120)
            coord.confirm_member(oid1, expected_revision=rev1, persona_id=persona_id_for_slug(PARTY[0][0]), character_guid=PARTY[0][1], accepted=True, now_ts=now + 15)
            dec = coord.confirm_member(oid1, expected_revision=rev1, persona_id=persona_id_for_slug(PARTY[1][0]), character_guid=PARTY[1][1], accepted=False, ack_token="dec1", now_ts=now + 16)
            self.assertTrue(dec["ok"] and dec["state"] == "cancelled")
            dec_dup = coord.confirm_member(oid1, expected_revision=rev1, persona_id=persona_id_for_slug(PARTY[1][0]), character_guid=PARTY[1][1], accepted=False, ack_token="dec1", now_ts=now + 17)
            self.assertTrue(dec_dup["ok"] and dec_dup["idempotent"])
            late_nh = coord.record_native_handoff(oid1, expected_revision=rev1 + 1, native_state="queued", native_queue_id="orphan_q", now_ts=now + 20)
            self.assertFalse(late_nh["ok"])
            self.assertEqual(late_nh.get("compensate"), "leave_queue")

            # 3. Confirmation TTL expiry persists on confirm_member early return & reserve_party returns 'expired' (P2.8)!
            oid2, rev2 = _seed_and_reserve(coord, now=now + 50, confirm_ttl=30)
            late_conf = coord.confirm_member(oid2, expected_revision=rev2, persona_id=persona_id_for_slug(PARTY[0][0]), character_guid=PARTY[0][1], accepted=True, now_ts=now + 100)
            self.assertFalse(late_conf["ok"])
            self.assertEqual(late_conf["reason"], "confirm_ttl_expired")
            # Persisted in DB even after early return!
            self.assertEqual(coord.get_opportunity(oid2)["state"], "expired")

    def test_fail_closed_reconciliation_correlation_adoption_late_completion_and_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_p = Path(tmp) / "brain.db"
            coord = OpportunityCoordinator(db_p)
            now = 1_800_000_000

            # 1. Opportunity A confirmed -> response lost -> native actually created queue under correlation_key -> ADOPTS!
            oid_a, rev_a = _seed_and_reserve(coord, now=now, confirm_ttl=60)
            ckey_a = ""
            for slug, guid, _ in PARTY:
                r = coord.confirm_member(oid_a, expected_revision=rev_a, persona_id=persona_id_for_slug(slug), character_guid=guid, accepted=True, now_ts=now + 5)
                ckey_a = r.get("correlation_key", ckey_a)

            # Non-authoritative reconcile (e.g., worldserver unreachable) MUST NOT cancel or abort anything (P0.1)!
            rec_unauth = coord.reconcile_native_state(NativeReconcileSnapshot(authoritative=False), now_ts=now + 500)
            self.assertFalse(rec_unauth["authoritative"])
            self.assertEqual(coord.get_opportunity(oid_a)["state"], "confirmed")

            # Authoritative reconcile finds correlation_key -> adopts into 'active' cleanly (P0.3)!
            rec_adopt = coord.reconcile_native_state(
                NativeReconcileSnapshot(
                    authoritative=True,
                    active_group_ids=frozenset({99}),
                    active_queue_ids=frozenset({"q_99"}),
                    active_run_ids=frozenset({"run_99"}),
                    correlation_map={ckey_a: {"native_state": "in_instance", "native_queue_id": "q_99", "group_id": 99, "native_run_id": "run_99"}},
                ),
                now_ts=now + 510,
            )
            self.assertEqual(rec_adopt["adopted_confirmed"], 1)
            self.assertEqual(coord.get_opportunity(oid_a)["state"], "active")

            # Authoritative reconcile with None active_run_ids fails closed (unknown -> does NOT abort!)
            rec_partial = coord.reconcile_native_state(
                NativeReconcileSnapshot(authoritative=True, active_group_ids=frozenset(), active_queue_ids=frozenset(), active_run_ids=None),
                now_ts=now + 520,
            )
            self.assertEqual(rec_partial["aborted_missing_native"], 0)
            self.assertEqual(coord.get_opportunity(oid_a)["state"], "active")

            # Authoritative reconcile with ALL empty sets -> aborts missing run & frees members!
            rec_gone = coord.reconcile_native_state(
                NativeReconcileSnapshot(authoritative=True, active_group_ids=frozenset(), active_queue_ids=frozenset(), active_run_ids=frozenset(), correlation_map={}),
                now_ts=now + 600,
            )
            self.assertEqual(rec_gone["aborted_missing_native"], 1)
            self.assertEqual(coord.get_opportunity(oid_a)["state"], "aborted")

            # Late genuine completion for run_99 upgrades opportunity ledger to 'completed' without touching members (P1.7)!
            late_done = coord.record_native_outcome(oid_a, native_run_id="run_99", outcome="completed", now_ts=now + 620)
            self.assertTrue(late_done["ok"] and late_done["late_reconciled"] and late_done["state"] == "completed")

            # 2. Pre-#13 migration backfill unstrands assembling rows with confirm_until=0 (P2.9)!
            with sqlite3.connect(db_p) as conn:
                conn.execute(
                    "INSERT INTO opportunities (schema_version, revision, kind, creator_guid, faction, guild_id, dungeon_id, dungeon_map_id, difficulty, roles_json, state, created_at, recruit_until, confirm_until, estimated_minutes, is_shadow) "
                    "VALUES (1, 2, 'dungeon_run', 201, 'alliance', 6, 'stockade_n', 34, 0, '{\"tank\":1}', 'assembling', ?, ?, 0, 30, 0)",
                    (now - 500, now - 100),
                )
            coord2 = OpportunityCoordinator(db_p)
            exp = coord2.expire_stale(now + 700)
            self.assertEqual(exp["expired_assembling"], 1)

    def test_concurrent_confirmations(self):
        with tempfile.TemporaryDirectory() as tmp:
            coord = OpportunityCoordinator(Path(tmp) / "brain.db")
            now = 1_800_000_000
            oid, rev = _seed_and_reserve(coord, now=now, confirm_ttl=120)

            results: list[dict] = []
            lock = threading.Lock()

            def _worker(slug: str, guid: int):
                r = coord.confirm_member(
                    oid, expected_revision=rev, persona_id=persona_id_for_slug(slug),
                    character_guid=guid, accepted=True, ack_token=f"t_{slug}", now_ts=now + 5,
                )
                with lock:
                    results.append(r)

            threads = [threading.Thread(target=_worker, args=(s, g)) for s, g, _ in PARTY]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(results), 5)
            self.assertTrue(all(r["ok"] for r in results))
            self.assertEqual(sum(1 for r in results if r.get("all_confirmed")), 1)
            self.assertEqual(coord.get_opportunity(oid)["state"], "confirmed")


if __name__ == "__main__":
    unittest.main()
