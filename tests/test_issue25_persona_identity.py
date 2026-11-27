"""Comprehensive tests for Issue #25: persistent opaque persona identity & multi-character mapping."""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hearthcore.dungeons import LFG_ROLE_DAMAGE, LFG_ROLE_TANK
from hearthcore.identity import PersonaRegistry, persona_id_for_slug
from hearthcore.opportunities import OpportunityCoordinator, run_shadow_match_snapshot
from hearthcore.readiness import evaluate_raid_roster


def _wal_aware_db_digest(db_p: Path) -> str:
    """Hash DB file + -wal + -shm + logical SQLite iterdump so stray WAL writes can NEVER hide (P2-10!)."""
    h = hashlib.sha256()
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{db_p}{suffix}")
        h.update(suffix.encode("utf-8"))
        if p.is_file():
            h.update(p.read_bytes())
    with sqlite3.connect(f"file:{db_p}?mode=ro", uri=True) as conn:
        for line in conn.iterdump():
            h.update(line.encode("utf-8"))
    return h.hexdigest()


def _valid_cand(slug: str, guid: int, *, now: int, realm_id: int = 1, shift_group: str = "early-birds", class_id: int = 8) -> dict:
    return {
        "guid": guid, "name": slug.capitalize(), "slug": slug, "realm_id": realm_id,
        "level": 24, "online": True, "shift_group": shift_group, "race_id": 1, "class_id": class_id,
        "faction": "alliance", "map_id": 0, "zone_id": 1519, "locked_maps": [], "in_group": False,
        "available_minutes": 120, "telemetry_observed_at": now, "willing_dungeons": ["stockade_n"],
        "live_telemetry": {
            "class_id": class_id, "spec_tab": 2 if class_id == 8 else 1, "known_spells": [25780] if class_id == 2 else [],
            "native_role_mask": LFG_ROLE_TANK if class_id == 2 else LFG_ROLE_DAMAGE,
            "native_lfg_eligible": True, "in_combat": False, "in_lfg_queue": False, "is_dead": False,
            "equipped": {"has_main_hand": True, "has_shield": class_id == 2, "broken_slots": 0, "min_durability_pct": 100.0},
        },
    }


class Issue25PersonaIdentityTests(unittest.TestCase):
    def test_opaque_immutable_id_and_atomic_legacy_backfill_preserves_enrolled_core(self):
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp:
            db_p = Path(tmp) / "brain.db"
            # Seed a legacy pre-#25 schema: one default core-wonky (enrolled=0) and one explicitly enrolled core-logan (enrolled=1)
            with sqlite3.connect(db_p) as conn:
                conn.execute("""
                    CREATE TABLE personas (
                        persona_id TEXT PRIMARY KEY,
                        slug TEXT NOT NULL UNIQUE,
                        faction TEXT NOT NULL DEFAULT 'alliance',
                        owner_group TEXT NOT NULL DEFAULT 'guild',
                        enrolled_coop INTEGER NOT NULL DEFAULT 0,
                        state TEXT NOT NULL DEFAULT 'active',
                        updated_at INTEGER NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE persona_characters (
                        realm_id INTEGER NOT NULL DEFAULT 1,
                        character_guid INTEGER NOT NULL,
                        incarnation INTEGER NOT NULL DEFAULT 1,
                        persona_id TEXT NOT NULL,
                        slug TEXT NOT NULL,
                        slot_kind TEXT NOT NULL DEFAULT 'main',
                        active INTEGER NOT NULL DEFAULT 1,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY (realm_id, character_guid, incarnation)
                    )
                """)
                conn.execute("INSERT INTO personas VALUES ('per_brannoc', 'brannoc', 'alliance', 'core-wonky', 0, 'active', ?)", (now - 1000,))
                conn.execute("INSERT INTO persona_characters VALUES (1, 101, 1, 'per_brannoc', 'brannoc', 'main', 1, ?)", (now - 1000,))
                conn.execute("INSERT INTO personas VALUES ('per_ashwick', 'ashwick', 'alliance', 'core-logan', 1, 'active', ?)", (now - 1000,))
                conn.execute("INSERT INTO persona_characters VALUES (1, 102, 1, 'per_ashwick', 'ashwick', 'main', 1, ?)", (now - 1000,))
                conn.commit()

            # Reopening twice tests atomic migration idempotency!
            reg = PersonaRegistry(db_p)
            reg = PersonaRegistry(db_p)

            p_legacy = reg.persona_for_guid(101, realm_id=1)
            self.assertEqual(p_legacy.persona_id, "per_brannoc", "Legacy ID MUST NOT be destructively rekeyed!")
            self.assertTrue(p_legacy.core_protected)
            self.assertFalse(p_legacy.enrolled_coop)
            self.assertIn("brannoc", p_legacy.aliases)

            # P1-4: Legacy explicitly enrolled core row keeps enrolled_coop=1 while latching core_protected=1!
            p_ash = reg.persona_for_guid(102, realm_id=1)
            self.assertTrue(p_ash.core_protected)
            self.assertTrue(p_ash.enrolled_coop)

            # Brand new companion gets IMMUTABLE OPAQUE persona_id (ignoring any injected comp['persona_id'] per P1-6!)
            res = reg.sync_from_companions({
                "ironbulwark": {
                    "guid": 201, "slug": "ironbulwark", "persona_id": "per_brannoc",
                    "race_id": 2, "class_id": 2, "shift_group": "early-birds", "online": True,
                },
            }, realm_id=1, now_ts=now)
            self.assertEqual(res["synced"], 1)
            p_new = reg.persona_for_guid(201, realm_id=1)
            self.assertTrue(p_new.persona_id.startswith("per_"))
            self.assertNotEqual(p_new.persona_id, "per_brannoc")
            self.assertNotEqual(p_new.persona_id, persona_id_for_slug("ironbulwark"))
            self.assertEqual(p_new.faction, "horde")
            self.assertTrue(p_new.faction_verified)

    def test_p0_1_and_p0_2_no_name_binding_cross_realm_same_slug_and_restore_incarnation_sync(self):
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp:
            reg = PersonaRegistry(Path(tmp) / "brain.db")
            reg.sync_from_companions({
                "brannoc": {"guid": 101, "slug": "brannoc", "account_id": 1, "race_id": 4, "class_id": 1, "shift_group": "early-birds", "online": True},
            }, realm_id=1, now_ts=now)
            pid_r1 = reg.persona_for_guid(101, realm_id=1).persona_id

            # P0-1a: SAME slug "brannoc" on realm 2 (guid 555) MUST create its OWN separate persona (NEVER merge into realm-1!)
            r2_res = reg.sync_from_companions({
                "brannoc": {"guid": 555, "slug": "brannoc", "realm_id": 2, "race_id": 1, "class_id": 2, "shift_group": "early-birds", "online": True},
            }, now_ts=now + 10, strict_realm=True)
            self.assertEqual(r2_res["synced"], 1)
            self.assertEqual(r2_res["quarantined"], [])
            pid_r2 = reg.persona_for_guid(555, realm_id=2).persona_id
            self.assertNotEqual(pid_r2, pid_r1, "Same name on different realms MUST NOT merge personas!")

            # P0-1b: New GUID (999) reusing existing slug "brannoc" on SAME realm 1 -> QUARANTINED (never bound by name!)
            r_reuse = reg.sync_from_companions({
                "brannoc": {"guid": 999, "slug": "brannoc", "realm_id": 1, "race_id": 4, "class_id": 1, "shift_group": "early-birds"},
            }, now_ts=now + 20, strict_realm=True)
            self.assertEqual(len(r_reuse["quarantined"]), 1)
            self.assertIn("unmapped_key_slug_collision", r_reuse["quarantined"][0])
            self.assertIsNone(reg.persona_for_guid(999, realm_id=1))

            # P0-2: After bind_new_incarnation (inc 1 retired -> inc 2 active), normal telemetry sync WITHOUT `incarnation`
            # MUST resolve to active inc 2 (NEVER revive dead inc 1 or trap session_active on inc 1!)
            r_inc = reg.bind_new_incarnation(
                persona_id=pid_r1, realm_id=1, character_guid=101, slug="brannoc",
                fingerprint={"account_id": 1, "race_id": 4, "class_id": 1}, now_ts=now + 30,
            )
            self.assertTrue(r_inc["ok"])
            self.assertEqual(r_inc["incarnation"], 2)

            r_sync_after = reg.sync_from_companions({
                "brannoc": {"guid": 101, "slug": "brannoc", "realm_id": 1, "account_id": 1, "race_id": 4, "class_id": 1, "shift_group": "early-birds", "online": True},
            }, now_ts=now + 40, strict_realm=True)
            self.assertEqual(r_sync_after["synced"], 1)
            self.assertEqual(r_sync_after["quarantined"], [])
            act = reg.active_character_for_persona(pid_r1)
            self.assertIsNotNone(act)
            self.assertEqual(act["incarnation"], 2, "Omitted incarnation MUST attach active session to inc 2, NOT dead inc 1!")

            # Explicitly sending retired inc 1 in sync is quarantined!
            r_dead = reg.sync_from_companions({
                "brannoc": {"guid": 101, "slug": "brannoc", "realm_id": 1, "incarnation": 1, "shift_group": "early-birds", "online": True},
            }, now_ts=now + 50, strict_realm=True)
            self.assertEqual(len(r_dead["quarantined"]), 1)
            self.assertIn("retired_incarnation_seen", r_dead["quarantined"][0])

            # P2-11: True restore via reactivate_incarnation (retire inc 2 manually, reactivate inc 2 without minting inc 3!)
            with sqlite3.connect(reg.path) as conn:
                conn.execute("UPDATE persona_characters SET active=0, session_active=0 WHERE realm_id=1 AND character_guid=101 AND incarnation=2")
                conn.commit()
            r_rest = reg.reactivate_incarnation(persona_id=pid_r1, realm_id=1, character_guid=101, incarnation=2, now_ts=now + 60)
            self.assertTrue(r_rest["ok"])
            self.assertEqual(reg.persona_for_guid(101, realm_id=1, incarnation=2).persona_id, pid_r1)

    def test_rename_roundtrip_alt_slug_collision_fingerprint_merge_and_conflict_recovery(self):
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp:
            reg = PersonaRegistry(Path(tmp) / "brain.db")
            # Initial sync has ONLY race_id + class_id (no account_id yet!)
            reg.sync_from_companions({
                "ironbulwark": {"guid": 201, "slug": "ironbulwark", "race_id": 3, "class_id": 2, "shift_group": "early-birds"},
                "aelindra": {"guid": 202, "slug": "aelindra", "race_id": 4, "class_id": 5, "shift_group": "early-birds"},
            }, realm_id=1, now_ts=now)
            pid_iron = reg.persona_for_guid(201).persona_id
            pid_ael = reg.persona_for_guid(202).persona_id
            reg.register_alt(persona_id=pid_ael, slug="aelalt", character_guid=299, realm_id=1, now_ts=now)

            # P1-5: Adding account_id later enriches fingerprint cleanly (NO false mismatch!)
            r_enrich = reg.sync_from_companions({
                "ironbulwark": {"guid": 201, "slug": "ironbulwark", "account_id": 5, "race_id": 3, "class_id": 2, "shift_group": "early-birds"},
            }, realm_id=1, now_ts=now + 10)
            self.assertEqual(r_enrich["quarantined"], [])

            # Rename A -> B -> A preserves exact persona_id & alias history!
            r_ab = reg.sync_from_companions({
                "ironshield": {"guid": 201, "slug": "ironshield", "account_id": 5, "race_id": 3, "class_id": 2, "shift_group": "early-birds"},
            }, realm_id=1, now_ts=now + 20)
            self.assertEqual(len(r_ab["renamed"]), 1)
            self.assertEqual(reg.persona_for_guid(201).persona_id, pid_iron)
            r_ba = reg.rename_character(realm_id=1, character_guid=201, new_slug="ironbulwark", now_ts=now + 30)
            self.assertTrue(r_ba["ok"])
            self.assertEqual(reg.persona_for_alias("ironshield").persona_id, pid_iron)

            # P2-6: Renaming to another persona's ACTIVE ALT slug (`aelalt`) is refused!
            r_alt_col = reg.rename_character(realm_id=1, character_guid=201, new_slug="aelalt", now_ts=now + 40)
            self.assertFalse(r_alt_col["ok"])
            self.assertIn("slug_owned_by_other_persona", r_alt_col["reason"])

            # Hard fingerprint mismatch (different class_id on GUID 201) quarantines incoming sync WITHOUT destroying existing persona!
            r_mis = reg.sync_from_companions({
                "recreated_mage": {"guid": 201, "slug": "recreated_mage", "account_id": 5, "race_id": 3, "class_id": 8, "shift_group": "early-birds"},
            }, realm_id=1, now_ts=now + 50)
            self.assertEqual(len(r_mis["quarantined"]), 1)
            self.assertEqual(reg.persona_for_guid(201).state, "active", "Incoming mismatch must quarantine incoming entry, not destroy existing persona!")

            # Explicit quarantine + resolve_conflict recovery works cleanly!
            reg.quarantine_persona(pid_iron, "manual_review", now_ts=now + 55)
            self.assertEqual(reg.persona_for_guid(201).state, "quarantined")
            open_c = reg.conflicts(only_open=True)
            self.assertGreaterEqual(len(open_c), 1)
            r_res = reg.resolve_conflict(open_c[0]["id"], action="reactivate_persona", note="verified", now_ts=now + 60)
            self.assertTrue(r_res["ok"])
            self.assertEqual(reg.persona_for_guid(201).state, "active")

    def test_p1_1_sticky_core_protection_and_owner_override_survives_resync(self):
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp:
            reg = PersonaRegistry(Path(tmp) / "brain.db")
            reg.sync_from_companions({
                "brannoc": {"guid": 101, "slug": "brannoc", "race_id": 4, "class_id": 1, "shift_group": "core-wonky"},
            }, realm_id=1, now_ts=now)
            pid = reg.persona_for_guid(101).persona_id

            # Move schedule out of core-wonky -> core_protected stays 1, enrolled_coop stays 0!
            reg.sync_from_companions({
                "brannoc": {"guid": 101, "slug": "brannoc", "race_id": 4, "class_id": 1, "shift_group": "prime-time"},
            }, realm_id=1, now_ts=now + 60)
            self.assertTrue(reg.persona_for_guid(101).core_protected)
            self.assertFalse(reg.persona_for_guid(101).enrolled_coop)

            # Non-override enrollment refused; explicit owner_override succeeds AND SURVIVES next sync tick (P1-1!)
            self.assertFalse(reg.set_coop_enrollment(pid, enrolled=True, owner_override=False)["ok"])
            self.assertTrue(reg.set_coop_enrollment(pid, enrolled=True, owner_override=True, now_ts=now + 70)["ok"])
            reg.sync_from_companions({
                "brannoc": {"guid": 101, "slug": "brannoc", "race_id": 4, "class_id": 1, "shift_group": "prime-time"},
            }, realm_id=1, now_ts=now + 120)
            self.assertTrue(reg.persona_for_guid(101).enrolled_coop, "P1-1: owner override MUST survive subsequent sync ticks!")

    def test_p1_2_p1_3_one_session_admission_block_no_registry_fail_closed_and_wal_readonly(self):
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            # P1-3: OpportunityCoordinator WITHOUT PersonaRegistry MUST fail closed with `persona_registry_missing`!
            no_reg_coord = OpportunityCoordinator(tp / "no_reg.db")
            opp_nr = no_reg_coord.create_opportunity(dungeon_id="stockade_n", creator_guid=201, is_shadow=True, now_ts=now)
            r_nr = no_reg_coord.admit_candidate(
                opportunity_id=opp_nr["id"], expected_revision=1,
                persona_id="per_ironbulwark", character_guid=201, character_slug="ironbulwark",
                role="tank", reason_code="vol",
                candidate=_valid_cand("ironbulwark", 201, now=now, class_id=2), now_ts=now,
            )
            self.assertFalse(r_nr["ok"])
            self.assertEqual(r_nr["reason"], "persona_registry_missing")

            # Now set up a real PersonaRegistry with main (ironbulwark, guid 201, session_active=1) + alt (vespera, guid 299)
            db_p = tp / "brain.db"
            reg = PersonaRegistry(db_p)
            reg.sync_from_companions({
                "ironbulwark": {"guid": 201, "slug": "ironbulwark", "race_id": 3, "class_id": 2, "shift_group": "early-birds", "online": True},
            }, realm_id=1, now_ts=now)
            pid_iron = reg.persona_for_guid(201).persona_id
            reg.register_alt(persona_id=pid_iron, slug="vespera", character_guid=299, realm_id=1, now_ts=now)

            coord = OpportunityCoordinator(db_p)
            opp = coord.create_opportunity(dungeon_id="stockade_n", creator_guid=201, is_shadow=True, now_ts=now)

            # P1-2: Admitting alt `vespera` while main `ironbulwark` holds active session MUST BE REJECTED!
            r_alt_sess = coord.admit_candidate(
                opportunity_id=opp["id"], expected_revision=1,
                persona_id=pid_iron, character_guid=299, character_slug="vespera",
                role="damage", reason_code="vol",
                candidate=_valid_cand("vespera", 299, now=now, class_id=8), now_ts=now,
            )
            self.assertFalse(r_alt_sess["ok"])
            self.assertEqual(r_alt_sess["reason"], "persona_session_on_other_character")

            # P0-2: realm_id=0 MUST BE REJECTED as invalid_realm_id (never coerced to 1!)
            r_r0 = coord.admit_candidate(
                opportunity_id=opp["id"], expected_revision=1,
                persona_id=pid_iron, character_guid=201, character_slug="ironbulwark",
                role="tank", reason_code="vol",
                candidate=_valid_cand("ironbulwark", 201, now=now, realm_id=0, class_id=2), now_ts=now,
            )
            self.assertFalse(r_r0["ok"])
            self.assertEqual(r_r0["reason"], "invalid_realm_id")

            # Unresolved GUID 777 MUST BE REJECTED!
            r_unres = coord.admit_candidate(
                opportunity_id=opp["id"], expected_revision=1,
                persona_id="per_fake", character_guid=777, character_slug="alaric",
                role="damage", reason_code="vol",
                candidate=_valid_cand("alaric", 777, now=now, class_id=8), now_ts=now,
            )
            self.assertFalse(r_unres["ok"])
            self.assertEqual(r_unres["reason"], "unresolved_persona_mapping")

            # P2-10: WAL-aware read-only shadow pass leaves brain.db + -wal + -shm + iterdump 100% unchanged!
            dig_before = _wal_aware_db_digest(db_p)
            sh = run_shadow_match_snapshot(
                {
                    "companions": {
                        "ironbulwark": _valid_cand("ironbulwark", 201, now=now, class_id=2),
                        "vespera": _valid_cand("vespera", 299, now=now, class_id=8),
                        "alaric": _valid_cand("alaric", 777, now=now, class_id=8),
                    },
                    "work_orders": [],
                    "telemetry_observed_at": now,
                },
                db_path=db_p, runtime_dir=tp / "rt",
                vitals={"mem_avail_gib": 1.0, "tick_p99": 15.0}, now_ts=now,
            )
            self.assertEqual(_wal_aware_db_digest(db_p), dig_before, "Shadow pass MUST NOT write brain.db or -wal!")
            stock = next(d for d in sh["shadow_dungeons"] if d["dungeon_id"] == "stockade_n")
            self.assertEqual(stock["stages"]["eligible"]["count"], 1, "Only session-active ironbulwark should pass!")
            self.assertEqual(stock["excluded_reasons"].get("persona_session_on_other_character"), 1)
            self.assertEqual(stock["excluded_reasons"].get("unresolved_persona_mapping"), 1)

            # Distinct roster count in raid readiness with main + alt both online (only 1 distinct persona!)
            rep_rd = evaluate_raid_roster(
                {
                    "ironbulwark": {**_valid_cand("ironbulwark", 201, now=now, class_id=2), "level": 60, "locked_instances": []},
                    "vespera": {**_valid_cand("vespera", 299, now=now, class_id=8), "level": 60, "locked_instances": []},
                },
                raid_id="mc_40", raid_stage=0, db_path=db_p, now_ts=now,
            )
            self.assertEqual(rep_rd["distinct_personas_evaluated"], 1)


if __name__ == "__main__":
    unittest.main()
