"""Tests for Issue #24: Bracketed item-specific gear ambition engine."""
from __future__ import annotations

import os
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hearthcore.gear_ambition import (
    ARMOR_SUBCLASS_CLOTH,
    ARMOR_SUBCLASS_MAIL,
    ARMOR_SUBCLASS_PLATE,
    ARMOR_SUBCLASS_SHIELD,
    INVTYPE_HOLDABLE,
    ITEM_CLASS_ARMOR,
    ITEM_CLASS_WEAPON,
    ITEM_FLAG_EXTRA_HORDE_ONLY,
    ITEM_FLAG_UNIQUE_EQUIPPABLE,
    CatalogItem,
    GearAmbitionStore,
    ItemSource,
    MarketListing,
    MarketSnapshot,
    can_character_equip_item,
    decode_item_template_row,
    load_catalog_from_db,
    load_character_state_from_db,
    load_market_snapshot_from_db,
    plan_character_gear_ambitions,
)
from hearthcore.identity import PersonaRegistry


def _seed_installed_schema_db() -> tuple[sqlite3.Connection, sqlite3.Connection]:
    """Create in-memory SQLite DBs matching exact AzerothCore WotLK 3.3.5a world & character schemas (P0-1)."""
    wconn = sqlite3.connect(":memory:")
    wconn.executescript("""
        CREATE TABLE item_template (
            entry INTEGER PRIMARY KEY, class INTEGER, subclass INTEGER, name TEXT, Quality INTEGER,
            Flags INTEGER DEFAULT 0, FlagsExtra INTEGER DEFAULT 0, BuyPrice INTEGER DEFAULT 0,
            InventoryType INTEGER, AllowableClass INTEGER DEFAULT -1, AllowableRace INTEGER DEFAULT -1,
            ItemLevel INTEGER, RequiredLevel INTEGER, RequiredSkill INTEGER DEFAULT 0,
            RequiredSkillRank INTEGER DEFAULT 0, RequiredReputationFaction INTEGER DEFAULT 0,
            RequiredReputationRank INTEGER DEFAULT 0, maxcount INTEGER DEFAULT 0,
            ItemLimitCategory INTEGER DEFAULT 0, bonding INTEGER DEFAULT 0, armor INTEGER DEFAULT 0,
            delay INTEGER DEFAULT 0, dmg_min1 REAL DEFAULT 0, dmg_max1 REAL DEFAULT 0,
            dmg_min2 REAL DEFAULT 0, dmg_max2 REAL DEFAULT 0,
            stat_type1 INTEGER DEFAULT 0, stat_value1 INTEGER DEFAULT 0,
            stat_type2 INTEGER DEFAULT 0, stat_value2 INTEGER DEFAULT 0,
            stat_type3 INTEGER DEFAULT 0, stat_value3 INTEGER DEFAULT 0
        );
        CREATE TABLE quest_template (
            ID INTEGER PRIMARY KEY, MinLevel INTEGER DEFAULT 1, QuestLevel INTEGER DEFAULT 1,
            SuggestedGroupNum INTEGER DEFAULT 0, AllowableRaces INTEGER DEFAULT 0, LogTitle TEXT,
            RewardItem1 INTEGER DEFAULT 0, RewardItem2 INTEGER DEFAULT 0, RewardItem3 INTEGER DEFAULT 0, RewardItem4 INTEGER DEFAULT 0,
            RewardChoiceItemID1 INTEGER DEFAULT 0, RewardChoiceItemID2 INTEGER DEFAULT 0, RewardChoiceItemID3 INTEGER DEFAULT 0,
            RewardChoiceItemID4 INTEGER DEFAULT 0, RewardChoiceItemID5 INTEGER DEFAULT 0, RewardChoiceItemID6 INTEGER DEFAULT 0
        );
        CREATE TABLE quest_template_addon (
            ID INTEGER PRIMARY KEY, PrevQuestID INTEGER DEFAULT 0, ExclusiveGroup INTEGER DEFAULT 0,
            AllowableClasses INTEGER DEFAULT 0, RequiredSkillID INTEGER DEFAULT 0, RequiredSkillPoints INTEGER DEFAULT 0,
            RequiredMinRepFaction INTEGER DEFAULT 0, RequiredMinRepValue INTEGER DEFAULT 0
        );
        CREATE TABLE creature_template (entry INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE creature (guid INTEGER PRIMARY KEY, id INTEGER, map INTEGER);
        CREATE TABLE creature_loot_template (Entry INTEGER, Item INTEGER, Reference INTEGER DEFAULT 0, Chance REAL DEFAULT 0, GroupId INTEGER DEFAULT 0);
        CREATE TABLE npc_vendor (entry INTEGER, item INTEGER, maxcount INTEGER DEFAULT 0, ExtendedCost INTEGER DEFAULT 0);
        CREATE TABLE crafted_recipes (
            recipe_spell_id INTEGER PRIMARY KEY, item_entry INTEGER, recipe_name TEXT,
            required_skill INTEGER, required_skill_rank INTEGER, material_cost_copper INTEGER DEFAULT 0,
            raid_stage_min INTEGER DEFAULT 0, estimated_minutes INTEGER DEFAULT 15
        );
    """)
    # Insert real WotLK items across slots & sources
    wconn.executemany(
        "INSERT INTO item_template (entry,class,subclass,name,Quality,Flags,FlagsExtra,BuyPrice,InventoryType,ItemLevel,RequiredLevel,RequiredReputationFaction,RequiredReputationRank,maxcount,bonding,armor,delay,dmg_min1,dmg_max1,stat_type1,stat_value1,stat_type2,stat_value2) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            # Equipped weak chest
            (1001, 4, 3, "Worn Mail Hauberk", 2, 0, 0, 0, 5, 18, 15, 0, 0, 0, 0, 140, 0, 0, 0, 7, 3, 4, 2),
            # Solo Quest reward chest
            (2041, 4, 3, "Tunic of Westfall", 3, 0, 0, 0, 5, 24, 18, 0, 0, 0, 1, 210, 0, 0, 0, 7, 7, 4, 6),
            # Deadmines drop chest (map 36 -> deadmines_n, boss 639)
            (10399, 4, 3, "Blackened Defias Armor", 3, 0, 0, 0, 5, 24, 19, 0, 0, 0, 1, 240, 0, 0, 0, 7, 10, 12, 5),
            # Crafted BoE chest (guild order eligible)
            (3480, 4, 3, "Rough Bronze Cuirass", 2, 0, 0, 0, 5, 23, 18, 0, 0, 0, 2, 205, 0, 0, 0, 7, 6, 4, 5),
            # Vendor helm (2000c) & Rep vendor boots (2000c, ExtendedCost=0) & Badge belt (ExtendedCost=42!)
            (4101, 4, 3, "Ironforge Guard Helm", 2, 0, 0, 2000, 1, 24, 20, 0, 0, 0, 1, 170, 0, 0, 0, 7, 8, 4, 4),
            (4102, 4, 3, "Ironforge Guard Boots", 2, 0, 0, 2000, 8, 24, 20, 0, 0, 0, 1, 150, 0, 0, 0, 7, 7, 4, 3),
            (4103, 4, 3, "Emblem Belt", 3, 0, 0, 0, 6, 25, 20, 0, 0, 0, 1, 140, 0, 0, 0, 7, 9, 12, 4),
            # Unique-Equipped Ring (NO finger equipped -> MUST rank in at most ONE finger slot!)
            (2933, 4, 0, "Seal of Wrynn", 3, ITEM_FLAG_UNIQUE_EQUIPPABLE, 0, 0, 11, 27, 20, 0, 0, 1, 1, 0, 0, 0, 0, 7, 6, 4, 5),
            # 1H Sword in bags (1 copy -> MUST emit only ONE equip proposal, NOT MH + OH!)
            (5191, 2, 7, "Cruel Barb", 3, 0, 0, 0, 13, 24, 19, 0, 0, 0, 1, 0, 2800, 31, 59, 38, 12, 7, 4),
            # 2H Axe (tests 2H vs 1H+OH and protected off_hand)
            (5194, 2, 1, "Taskmaster Axe", 3, 0, 0, 0, 17, 25, 20, 0, 0, 0, 1, 0, 3400, 50, 85, 4, 10, 7, 8),
            # Off-hand Shield
            (5195, 4, 6, "Gold-Plated Buckler", 3, 0, 0, 0, 14, 25, 20, 0, 0, 0, 1, 650, 0, 0, 0, 7, 7, 15, 5),
            # AH BoE cloak
            (4200, 4, 1, "Fortified Cloak", 2, 0, 0, 0, 16, 25, 22, 0, 0, 0, 2, 32, 0, 0, 0, 7, 8, 13, 4),
            # Next bracket (30-39) & Cap (60) items
            (6773, 4, 3, "Scarlet Mail Chest", 3, 0, 0, 0, 5, 38, 34, 0, 0, 0, 1, 340, 0, 0, 0, 7, 14, 4, 12),
            (16865, 4, 4, "Breastplate of Valor", 3, 0, 0, 0, 5, 63, 58, 0, 0, 0, 1, 680, 0, 0, 0, 7, 26, 4, 15),
        ],
    )
    wconn.execute("INSERT INTO quest_template (ID, MinLevel, SuggestedGroupNum, AllowableRaces, LogTitle, RewardChoiceItemID1) VALUES (166, 18, 0, 1101, 'Westfall Patrol', 2041)")
    wconn.execute("INSERT INTO quest_template (ID, MinLevel, SuggestedGroupNum, AllowableRaces, LogTitle, RewardItem1) VALUES (399, 20, 0, 1101, 'Seal of the King', 2933)")
    wconn.execute("INSERT INTO quest_template (ID, MinLevel, SuggestedGroupNum, AllowableRaces, LogTitle, RewardItem1) VALUES (400, 34, 0, 1101, 'Scarlet Crusade', 6773)")
    wconn.execute("INSERT INTO quest_template (ID, MinLevel, SuggestedGroupNum, AllowableRaces, LogTitle, RewardItem1) VALUES (401, 58, 0, 1101, 'Valor Chest', 16865)")
    wconn.execute("INSERT INTO creature_template VALUES (639, 'Edwin VanCleef')")
    wconn.execute("INSERT INTO creature VALUES (1, 639, 36)")
    # Equal-weight group (Chance=0, GroupId=1 -> 2 items => 50% each!)
    wconn.execute("INSERT INTO creature_loot_template VALUES (639, 10399, 0, 0, 1)")
    wconn.execute("INSERT INTO creature_loot_template VALUES (639, 5194, 0, 0, 1)")
    wconn.execute("INSERT INTO creature_loot_template VALUES (639, 5195, 0, 25.0, 0)")
    wconn.execute("INSERT INTO npc_vendor VALUES (1285, 4101, 0, 0)")
    wconn.execute("INSERT INTO npc_vendor VALUES (1285, 4102, 0, 0)")
    wconn.execute("INSERT INTO npc_vendor VALUES (1285, 4103, 0, 42)")
    wconn.execute("INSERT INTO crafted_recipes VALUES (3320, 3480, 'Rough Bronze Cuirass', 164, 105, 1000, 0, 15)")
    wconn.commit()

    cconn = sqlite3.connect(":memory:")
    cconn.executescript("""
        CREATE TABLE characters (guid INTEGER PRIMARY KEY, name TEXT, race INTEGER, class INTEGER, level INTEGER, money INTEGER);
        CREATE TABLE character_inventory (guid INTEGER, bag INTEGER, slot INTEGER, item INTEGER);
        CREATE TABLE item_instance (guid INTEGER PRIMARY KEY, itemEntry INTEGER, count INTEGER DEFAULT 1);
        CREATE TABLE character_queststatus_rewarded (guid INTEGER, quest INTEGER);
        CREATE TABLE character_skills (guid INTEGER, skill INTEGER, value INTEGER);
        CREATE TABLE character_reputation (guid INTEGER, faction INTEGER, standing INTEGER);
        CREATE TABLE character_spell (guid INTEGER, spell INTEGER, active INTEGER DEFAULT 1);
        CREATE TABLE auctionhouse (id INTEGER PRIMARY KEY, houseid INTEGER, itemguid INTEGER, buyoutprice INTEGER, time INTEGER);
    """)
    cconn.execute("INSERT INTO characters VALUES (101, 'Ironbulwark', 1, 1, 24, 5000)")
    cconn.execute("INSERT INTO item_instance VALUES (9001, 1001, 1)")
    cconn.execute("INSERT INTO character_inventory VALUES (101, 0, 4, 9001)")  # Chest slot 4
    cconn.execute("INSERT INTO item_instance VALUES (9002, 5191, 1)")
    cconn.execute("INSERT INTO character_inventory VALUES (101, 19, 0, 9002)")  # Bag copy of 1H sword
    cconn.execute("INSERT INTO item_instance VALUES (9003, 4200, 1)")
    cconn.execute("INSERT INTO auctionhouse VALUES (7001, 2, 9003, 1800, 9999999999)")
    cconn.execute("INSERT INTO character_skills VALUES (101, 164, 50)")
    cconn.commit()
    return wconn, cconn


class TestIssue24GearAmbitionEngine(unittest.TestCase):
    def test_p0_1_p0_2_p0_3_installed_db_loader_coop_off_gate_and_provenance(self) -> None:
        """QA #5 & #13: Real DB loaders build catalog/state/market with row keys; while coop OFF EVERY proposal has executable=False."""
        wconn, cconn = _seed_installed_schema_db()
        try:
            catalog = load_catalog_from_db(wconn)
            cstate = load_character_state_from_db(cconn, 101)
            market = load_market_snapshot_from_db(cconn, observed_at=1000, guild_craftable_entries={3480})
        finally:
            wconn.close()
            cconn.close()

        self.assertTrue(cstate["ok"])
        self.assertEqual(cstate["equipped_items"]["chest"], 1001)
        self.assertEqual(cstate["bag_item_counts"][5191], 1)
        # Verify equal-weight group drop chance decoded accurately (2 items with Chance=0 in GroupId=1 => 50.0% each!)
        dm_src = catalog[10399].sources[0]
        self.assertEqual(dm_src.drop_chance_pct, 50.0)
        self.assertEqual(dm_src.evidence_key, ("creature_loot_template", 639, 10399, 1))

        # Coop OFF (unset) -> EVERY single emitted proposal MUST have executable=False, advisory_only=True! (P0-2/P0-3)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HEARTHCORE_COOP_ENABLED", None)
            os.environ.pop("HEARTHCORE_COOP_DUNGEONS", None)
            plan = plan_character_gear_ambitions(
                catalog=catalog,
                class_id=cstate["class_id"], race_id=cstate["race_id"], faction=cstate["faction"],
                level=cstate["level"], role="damage",
                equipped_items=cstate["equipped_items"], bag_item_counts=cstate["bag_item_counts"],
                completed_quests=cstate["completed_quests"], skills=cstate["skills"],
                reputations=cstate["reputations"], known_spells=cstate["known_spells"],
                gold_copper=cstate["gold_copper"], raid_readiness_gold_reserve_copper=2500,
                market_snapshot=market, raid_stage=0, brackets=("current", "next", "cap"), now_ts=1000,
            )

        self.assertTrue(plan["ok"])
        for u in plan["equip_owned_upgrades"]:
            self.assertFalse(u["proposal"]["executable"])
            self.assertTrue(u["proposal"]["advisory_only"])
        for slot_data in plan["slots"].values():
            for c in slot_data["attainable"] + slot_data["aspirational"]:
                self.assertFalse(c["proposal"]["executable"], f"Expected advisory while coop OFF: {c}")
                self.assertTrue(c["proposal"]["advisory_only"])

        # P1-9: ExtendedCost=42 vendor belt stays aspirational (never treated as 0c attainable!)
        self.assertEqual(plan["slots"]["waist"]["attainable"], [])
        self.assertEqual([c["item_entry"] for c in plan["slots"]["waist"]["aspirational"]], [4103])
        self.assertIn("extended_cost_unmodeled", plan["slots"]["waist"]["aspirational"][0]["best_source"]["reason"])

        # P1-7: Multi-bracket plans include 'next' (30-39) and 'cap' (60_classic_cap) strictly aspirational!
        self.assertIn("next", plan["bracket_plans"])
        self.assertIn("cap", plan["bracket_plans"])
        self.assertEqual([c["item_entry"] for c in plan["bracket_plans"]["next"]["slots"]["chest"]["aspirational"]], [6773])
        self.assertEqual([c["item_entry"] for c in plan["bracket_plans"]["cap"]["slots"]["chest"]["aspirational"]], [16865])

    def test_p1_1_p1_2_p1_3_p1_4_paired_unique_bag_copies_2h_vs_oh_unknown_equipped_and_budget_exhaustion(self) -> None:
        """QA #1, #2, #4, #7, #8, #9, #14: Paired Unique, 1 bag copy, shrinking gold budget, 2H/OH, unknown equipped, determinism."""
        wconn, cconn = _seed_installed_schema_db()
        try:
            catalog = load_catalog_from_db(wconn)
            market = load_market_snapshot_from_db(cconn, observed_at=1000, guild_craftable_entries={3480})
        finally:
            wconn.close()
            cconn.close()

        # 1. QA #1 & #2 & #4: Both finger slots empty + 1 bag copy of 1H sword (5191) + 2500c spare gold vs two 2000c vendor items (4101 + 4102)!
        plan = plan_character_gear_ambitions(
            catalog=catalog, class_id=1, race_id=1, faction="alliance", level=24, role="damage",
            equipped_items={"chest": 1001}, bag_item_counts={5191: 1},
            gold_copper=5000, raid_readiness_gold_reserve_copper=2500,  # 2500c spare -> can afford ONE 2000c item, NOT BOTH!
            market_snapshot=market, raid_stage=0, now_ts=1000,
        )
        # QA #1: Unique-Equipped ring 2933 ranked in AT MOST ONE finger slot!
        f1 = [c["item_entry"] for c in plan["slots"]["finger1"]["attainable"]]
        f2 = [c["item_entry"] for c in plan["slots"]["finger2"]["attainable"]]
        self.assertEqual(f1.count(2933) + f2.count(2933), 1)
        # QA #2: 1 bag copy of 1H sword produces EXACTLY ONE equip_owned_upgrades entry, NOT two!
        self.assertEqual(sum(1 for u in plan["equip_owned_upgrades"] if u["item_entry"] == 5191), 1)
        # QA #4: Two 2000c vendor items (4101 helm, 4102 boots) + 1800c cloak with 2500c spare -> at most ONE paid item stays attainable; others budget_exhausted!
        paid_att = [
            s["attainable"][0] for s in plan["slots"].values()
            if s["attainable"] and s["attainable"][0]["best_source"]["cost_copper"] > 0
        ]
        self.assertLessEqual(sum(x["best_source"]["cost_copper"] for x in paid_att), 2500)
        budget_demoted = [
            c for s in plan["slots"].values() for c in s["aspirational"]
            if c["best_source"]["reason"] == "budget_exhausted"
        ]
        self.assertGreaterEqual(len(budget_demoted), 1)

        # 2. QA #7: Wearing 2H (5194) with NO 1H in bags -> off_hand shield (5195) is NOT ranked as an upgrade over empty OH!
        plan_2h = plan_character_gear_ambitions(
            catalog=catalog, class_id=1, race_id=1, faction="alliance", level=24, role="damage",
            equipped_items={"main_hand": 5194}, bag_item_counts={}, gold_copper=5000, raid_stage=0, now_ts=1000,
        )
        self.assertEqual(plan_2h["slots"]["off_hand"]["attainable"], [])

        # 3. QA #8: Protected off_hand -> 2H main_hand (5194) MUST be skipped!
        plan_prot_oh = plan_character_gear_ambitions(
            catalog=catalog, class_id=1, race_id=1, faction="alliance", level=24, role="damage",
            equipped_items={"off_hand": 5195}, raid_readiness_protected_slots={"off_hand"},
            gold_copper=5000, raid_stage=0, now_ts=1000,
        )
        self.assertNotIn(5194, [c["item_entry"] for c in plan_prot_oh["slots"]["main_hand"]["attainable"]])

        # 4. QA #9: Equipped entry missing from catalog -> slot fails closed (`unknown_equipped=True`, empty targets)!
        plan_unk = plan_character_gear_ambitions(
            catalog=catalog, class_id=1, race_id=1, faction="alliance", level=24, role="damage",
            equipped_items={"chest": 777777}, raid_stage=0, now_ts=1000,
        )
        self.assertTrue(plan_unk["slots"]["chest"]["unknown_equipped"])
        self.assertEqual(plan_unk["slots"]["chest"]["attainable"], [])

        # 5. QA #14: Determinism under shuffled catalog order!
        keys = list(catalog.keys())
        rng = random.Random(42)
        rng.shuffle(keys)
        shuffled_catalog = {k: catalog[k] for k in keys}
        plan_shuf = plan_character_gear_ambitions(
            catalog=shuffled_catalog, class_id=1, race_id=1, faction="alliance", level=24, role="damage",
            equipped_items={"chest": 1001}, bag_item_counts={5191: 1},
            gold_copper=5000, raid_readiness_gold_reserve_copper=2500,
            market_snapshot=market, raid_stage=0, now_ts=1000,
        )
        self.assertEqual(plan, plan_shuf)

    def test_p1_5_p1_6_store_validation_composite_event_dedup_identity_and_outlevel_reconcile(self) -> None:
        """QA #3, #6, #10, #11: set_ambition catalog checks, composite event PK, wrong incarnation rejected, outleveling closes goal."""
        wconn, _ = _seed_installed_schema_db()
        try:
            catalog = load_catalog_from_db(wconn)
        finally:
            wconn.close()

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "brain.db"
            reg = PersonaRegistry(db_path)
            reg.sync_from_companions(
                {"ironbulwark": {"guid": 101, "realm_id": 1, "name": "Ironbulwark", "faction": "alliance", "shift_group": "regular", "online": True}},
                now_ts=1000,
            )
            pid = reg.persona_for_guid(101, realm_id=1).persona_id
            store = GearAmbitionStore(db_path)

            # QA #10: set_ambition rejects uncataloged item, wrong slot, and unverified source!
            self.assertEqual(
                store.set_ambition(
                    persona_id=pid, realm_id=1, character_guid=101, incarnation=1, slot="chest", item_entry=888888,
                    bracket_id="20-29", source_kind="dungeon_drop", source_id=6, target_score=40.0,
                    proposal={"proposal_kind": "rdf_dungeon_queue"}, catalog=catalog,
                    class_id=1, race_id=1, faction="alliance", level=24, role="tank", now_ts=1000,
                )["reason"],
                "item_not_in_catalog:888888",
            )
            self.assertTrue(
                store.set_ambition(
                    persona_id=pid, realm_id=1, character_guid=101, incarnation=1, slot="head", item_entry=10399,
                    bracket_id="20-29", source_kind="dungeon_drop", source_id=6, target_score=40.0,
                    proposal={"proposal_kind": "rdf_dungeon_queue"}, catalog=catalog,
                    class_id=1, race_id=1, faction="alliance", level=24, role="tank", now_ts=1000,
                )["reason"].startswith("slot_mismatch_for_item")
            )
            self.assertTrue(
                store.set_ambition(
                    persona_id=pid, realm_id=1, character_guid=101, incarnation=1, slot="chest", item_entry=10399,
                    bracket_id="20-29", source_kind="vendor", source_id=1285, target_score=40.0,
                    proposal={"proposal_kind": "native_vendor_purchase"}, catalog=catalog,
                    class_id=1, race_id=1, faction="alliance", level=24, role="tank", now_ts=1000,
                )["reason"].startswith("unverified_source")
            )

            # Register TWO ambitions from the same boss (10399 chest & 5195 shield from Edwin VanCleef 639!)
            a1 = store.set_ambition(
                persona_id=pid, realm_id=1, character_guid=101, incarnation=1, slot="chest", item_entry=10399,
                bracket_id="20-29", source_kind="dungeon_drop", source_id=6, target_score=40.0,
                proposal={"proposal_kind": "rdf_dungeon_queue"}, catalog=catalog,
                class_id=1, race_id=1, faction="alliance", level=24, role="tank", now_ts=1000,
            )
            a2 = store.set_ambition(
                persona_id=pid, realm_id=1, character_guid=101, incarnation=1, slot="off_hand", item_entry=5195,
                bracket_id="20-29", source_kind="dungeon_drop", source_id=6, target_score=40.0,
                proposal={"proposal_kind": "rdf_dungeon_queue"}, catalog=catalog,
                class_id=1, race_id=1, faction="alliance", level=24, role="tank", now_ts=1000,
            )
            self.assertTrue(a1["ok"] and a2["ok"])

            # QA #3: ONE native_event_id covering BOTH items from the same boss kill credits BOTH ambitions!
            shared_events = [
                {"native_event_id": "boss-kill-639-1", "type": "loot_opportunity", "persona_id": pid, "realm_id": 1, "character_guid": 101, "incarnation": 1, "item_entry": 10399, "boss_entry": 639},
                {"native_event_id": "boss-kill-639-1", "type": "loot_opportunity", "persona_id": pid, "realm_id": 1, "character_guid": 101, "incarnation": 1, "item_entry": 5195, "boss_entry": 639},
            ]
            r1 = store.reconcile_ambitions(
                persona_id=pid, realm_id=1, character_guid=101, incarnation=1, catalog=catalog,
                class_id=1, race_id=1, faction="alliance", role="tank", level=24,
                equipped_items={"chest": 1001}, observed_events=shared_events, now_ts=1020,
            )
            self.assertTrue(r1["ok"])
            self.assertEqual({x["item_entry"]: x["eligible_loot_attempts"] for x in r1["reconciled"]}, {10399: 1, 5195: 1})

            # QA #6: Wrong incarnation rejected on BOTH reconcile & abandon!
            bad_rec = store.reconcile_ambitions(
                persona_id=pid, realm_id=1, character_guid=101, incarnation=99, catalog=catalog,
                class_id=1, race_id=1, faction="alliance", role="tank", level=24,
                equipped_items={"chest": 1001}, now_ts=1030,
            )
            self.assertFalse(bad_rec["ok"])
            self.assertEqual(bad_rec["reason"], "incarnation_mismatch")
            bad_ab = store.abandon_ambition(
                a1["ambition_id"], persona_id=pid, realm_id=1, character_guid=101, incarnation=99, now_ts=1031,
            )
            self.assertFalse(bad_ab["ok"])
            self.assertEqual(bad_ab["reason"], "incarnation_mismatch")

            # QA #11: Leveling out of 20-29 bracket (Lv32 -> 30-39) closes active 20-29 ambitions as obsolete_outleveled!
            r_lvl = store.reconcile_ambitions(
                persona_id=pid, realm_id=1, character_guid=101, incarnation=1, catalog=catalog,
                class_id=1, race_id=1, faction="alliance", role="tank", level=32,
                equipped_items={"chest": 1001}, now_ts=1100,
            )
            self.assertEqual({x["item_entry"]: x["state"] for x in r_lvl["reconciled"]}, {10399: "obsolete_outleveled", 5195: "obsolete_outleveled"})

    def test_equipability_matrix_wotlk_legality(self) -> None:
        """QA #12: Class/race masks, Horde-only flag, priest+shield, wrong relic, shaman dual-wield talent, tank holdable."""
        shield = CatalogItem(entry=1, name="Shield", item_level=20, required_level=15, quality=2, item_class=ITEM_CLASS_ARMOR, item_subclass=ARMOR_SUBCLASS_SHIELD, inventory_type=14)
        self.assertEqual(
            can_character_equip_item(shield, class_id=5, race_id=1, faction="alliance", level=20, role="healer", target_slot="off_hand"),
            (False, "class_cannot_use_shield"),
        )
        holdable = CatalogItem(entry=2, name="Orb", item_level=20, required_level=15, quality=2, item_class=ITEM_CLASS_ARMOR, item_subclass=0, inventory_type=INVTYPE_HOLDABLE)
        self.assertEqual(
            can_character_equip_item(holdable, class_id=1, race_id=1, faction="alliance", level=20, role="tank", target_slot="off_hand"),
            (False, "tank_requires_shield_not_holdable"),
        )
        horde_item = CatalogItem(entry=3, name="Horde Chest", item_level=20, required_level=15, quality=2, item_class=ITEM_CLASS_ARMOR, item_subclass=ARMOR_SUBCLASS_MAIL, inventory_type=5, flags_extra=ITEM_FLAG_EXTRA_HORDE_ONLY)
        self.assertEqual(
            can_character_equip_item(horde_item, class_id=1, race_id=1, faction="alliance", level=20, role="tank", target_slot="chest"),
            (False, "item_horde_only"),
        )
        oh_axe = CatalogItem(entry=4, name="1H Axe", item_level=45, required_level=40, quality=2, item_class=ITEM_CLASS_WEAPON, item_subclass=0, inventory_type=13)
        self.assertEqual(
            can_character_equip_item(oh_axe, class_id=7, race_id=11, faction="alliance", level=45, role="damage", target_slot="off_hand", known_spells=set()),
            (False, "shaman_dual_wield_talent_missing"),
        )
        plate_cloak = CatalogItem(entry=5, name="Plate Cloak", item_level=20, required_level=15, quality=2, item_class=ITEM_CLASS_ARMOR, item_subclass=ARMOR_SUBCLASS_PLATE, inventory_type=16)
        self.assertEqual(
            can_character_equip_item(plate_cloak, class_id=8, race_id=1, faction="alliance", level=20, role="damage", target_slot="back"),
            (False, "cloak_armor_subclass_above_class_proficiency"),
        )
        # Registered executor + coop ON makes proposal executable
        wconn, _ = _seed_installed_schema_db()
        try:
            catalog = load_catalog_from_db(wconn)
        finally:
            wconn.close()
        with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_ENABLED": "1", "HEARTHCORE_COOP_DUNGEONS": "deadmines_n"}, clear=False):
            p_live = plan_character_gear_ambitions(
                catalog=catalog, class_id=1, race_id=1, faction="alliance", level=24, role="tank",
                equipped_items={"chest": 1001}, raid_stage=0, now_ts=1000,
                registered_executors={"rdf_dungeon_queue"},
            )
            dm = next(c for c in p_live["slots"]["chest"]["attainable"] if c["item_entry"] == 10399)
            self.assertTrue(dm["proposal"]["executable"])
            self.assertFalse(dm["proposal"]["advisory_only"])


if __name__ == "__main__":
    unittest.main()
