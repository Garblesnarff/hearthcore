"""Issue #27 tests: native C++17 work-order quantity & delivery verification + SQL migration parity."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CPP_HEADER = ROOT / "mod-playerbots-companions" / "src" / "Bot" / "CompanionWorkOrderLogic.h"
SCHEMA_SQL = ROOT / "ops" / "sql" / "companion_schema.sql"
MIGRATION_003 = ROOT / "ops" / "sql" / "migrations" / "003_work_order_verification.sql"

CPP_HARNESS = r"""
#include "CompanionWorkOrderLogic.h"

#include <cassert>
#include <iostream>
#include <vector>

using namespace CompanionWorkOrderLogic;

static OrderState MakeOpenOrder(std::uint64_t id, std::uint32_t reqGuid, std::uint32_t itemEntry,
                                std::uint32_t qty, std::uint32_t createdAt = 1000)
{
    OrderState wo;
    wo.id = id;
    wo.requesterGuid = reqGuid;
    wo.requesterName = "Aldric";
    wo.itemEntry = itemEntry;
    wo.itemName = "Minor Healing Potion";
    wo.quantity = qty;
    wo.status = "open";
    wo.revision = 1;
    wo.createdAt = createdAt;
    wo.updatedAt = createdAt;
    return wo;
}

int main()
{
    auto alwaysInGuild = [](std::uint32_t) { return true; };
    auto tab0Accessible = [](std::uint32_t, std::uint8_t tab) { return tab == 0; };

    // 1. One-of-20 partial craft remains partial; crafting 20/20 for oneself is NEVER delivery.
    {
        Tracker tr;
        tr.LoadOrders({MakeOpenOrder(101, 10, 118, 20)});

        ActionResult r1 = tr.RecordProduction(20, "Kelda", 118, 1, 101, true, alwaysInGuild, 1001, 1010);
        assert(r1.ok && r1.stateChanged && !r1.completedNow);
        assert(r1.order.status == "claimed");
        assert(r1.order.crafterGuid == 20);
        assert(r1.order.attemptId > 0);
        assert(r1.order.producedQty == 1);
        assert(r1.order.transferredQty == 0);
        assert(r1.order.receivedQty == 0);
        assert(r1.expectedRevision == 1 && r1.newRevision == 2);
        assert(r1.reason == "produced_partial");
        assert(tr.PendingDeliveryQty(20, 118) == 1);

        // Craft remaining 19 -> still NOT delivered until verified bank transfer & accessible receipt!
        ActionResult r2 = tr.RecordProduction(20, "Kelda", 118, 19, 101, true, alwaysInGuild, 1002, 1020);
        assert(r2.ok && r2.stateChanged && !r2.completedNow);
        assert(r2.order.status == "claimed");
        assert(r2.order.producedQty == 20);
        assert(r2.order.transferredQty == 0);
        assert(r2.order.receivedQty == 0);
        assert(r2.reason == "produced_ready_for_delivery");
        assert(tr.PendingDeliveryQty(20, 118) == 20);

        // Partial deposit of 12/20 -> stays claimed, partial receipt tracked, NOT completedNow, reserved for requester.
        ActionResult d1 = tr.RecordBankDelivery(20, 118, 12, 12, 0, true, alwaysInGuild, tab0Accessible, 2001, 1030);
        assert(d1.ok && d1.stateChanged && !d1.completedNow);
        assert(d1.order.status == "claimed");
        assert(d1.order.transferredQty == 12 && d1.order.receivedQty == 12);
        assert(d1.reason == "delivered_partial");
        assert(tr.PendingDeliveryQty(20, 118) == 8);
        assert(tr.ReservedBankQty(118, 1035, 0) == 12);
        assert(tr.ReservedBankQty(118, 1035, 10) == 0);   // requester itself is not blocked from withdrawing!

        // Final deposit of 8/20 -> atomically transitions claimed -> delivered with completedNow=true!
        ActionResult d2 = tr.RecordBankDelivery(20, 118, 8, 8, 0, true, alwaysInGuild, tab0Accessible, 2002, 1040);
        assert(d2.ok && d2.stateChanged && d2.completedNow);
        assert(d2.order.status == "delivered");
        assert(d2.order.producedQty == 20 && d2.order.transferredQty == 20 && d2.order.receivedQty == 20);
        assert(d2.order.deliveryTab == 0);
        assert(d2.reason == "delivered_complete");
        assert(!tr.HasPendingDeliveryFor(20, 118));
        assert(tr.ReservedBankQty(118, 1045, 99) == 20);  // reserved against other bots/auto-organize!
        assert(tr.ReservedBankQty(118, 1045, 10) == 0);   // requester can withdraw!
    }

    // 2. Another crafter cannot steal or overwrite an active claim (nor can requester self-deliver).
    {
        Tracker tr;
        tr.LoadOrders({MakeOpenOrder(102, 10, 2840, 10)});

        ActionResult selfClaim = tr.ClaimOrder(102, 10, "Aldric", true, true, 1005);
        assert(!selfClaim.ok && selfClaim.reason == "self_order");

        ActionResult claimA = tr.ClaimOrder(102, 20, "Kelda", true, true, 1010);
        assert(claimA.ok && claimA.stateChanged && claimA.attemptId > 0);
        assert(!tr.HasOpenOrderFor(2840, 30));  // second crafter must not see claimed order as open
        assert(tr.HasOpenOrderFor(2840, 20));   // owner of claim still sees it while produced < quantity

        ActionResult stealClaim = tr.ClaimOrder(102, 30, "Brannoc", true, true, 1015);
        assert(!stealClaim.ok && stealClaim.reason == "claimed_by_other");

        ActionResult stealProdPref = tr.RecordProduction(30, "Brannoc", 2840, 10, 102, true, alwaysInGuild, 1101, 1016);
        assert(!stealProdPref.ok && stealProdPref.reason == "claimed_by_other");

        ActionResult stealProdAny = tr.RecordProduction(30, "Brannoc", 2840, 10, 0, true, alwaysInGuild, 1102, 1017);
        assert(!stealProdAny.ok && stealProdAny.reason == "no_matching_order");

        ActionResult stealDeliv = tr.RecordBankDelivery(30, 2840, 10, 10, 0, true, alwaysInGuild, tab0Accessible, 2101, 1018);
        assert(!stealDeliv.ok && stealDeliv.reason == "no_claimed_order");
        assert(tr.Orders()[0].crafterGuid == 20 && tr.Orders()[0].producedQty == 0);
    }

    // 3. Duplicate callbacks (craft & delivery) are strictly idempotent (no double credit, no double completion).
    {
        Tracker tr;
        tr.LoadOrders({MakeOpenOrder(103, 10, 118, 5)});

        ActionResult p1 = tr.RecordProduction(20, "Kelda", 118, 5, 103, true, alwaysInGuild, 3001, 1010);
        ActionResult pDup = tr.RecordProduction(20, "Kelda", 118, 5, 103, true, alwaysInGuild, 3001, 1011);
        assert(p1.ok && p1.creditedQty == 5);
        assert(pDup.ok && pDup.duplicate && !pDup.stateChanged && pDup.reason == "duplicate_callback");
        assert(tr.Orders()[0].producedQty == 5 && tr.Orders()[0].revision == 2);

        ActionResult d1 = tr.RecordBankDelivery(20, 118, 5, 5, 0, true, alwaysInGuild, tab0Accessible, 4001, 1020);
        ActionResult dDup = tr.RecordBankDelivery(20, 118, 5, 5, 0, true, alwaysInGuild, tab0Accessible, 4001, 1021);
        ActionResult dLate = tr.RecordBankDelivery(20, 118, 5, 5, 0, true, alwaysInGuild, tab0Accessible, 4002, 1022);
        assert(d1.ok && d1.completedNow);
        assert(dDup.ok && dDup.duplicate && !dDup.completedNow && !dDup.stateChanged);
        assert(!dLate.ok && !dLate.completedNow && dLate.reason == "no_claimed_order");
    }

    // 4. Non-transferable / bound (BoP / quest / conjured) item is rejected at claim, production, and delivery.
    {
        Tracker tr;
        tr.LoadOrders({MakeOpenOrder(104, 10, 99999, 1)});
        assert(!tr.ClaimOrder(104, 20, "Kelda", false, true, 1010).ok);
        ActionResult p = tr.RecordProduction(20, "Kelda", 99999, 1, 104, false, alwaysInGuild, 5001, 1011);
        assert(!p.ok && p.reason == "bound_item");
        ActionResult d = tr.RecordBankDelivery(20, 99999, 1, 1, 0, false, alwaysInGuild, tab0Accessible, 5002, 1012);
        assert(!d.ok && d.reason == "bound_item");
        assert(tr.Orders()[0].status == "open");
    }

    // 5. Full bank (TAB_NONE) and inaccessible tab (tab 2) do NOT credit delivery or complete order.
    {
        Tracker tr;
        tr.LoadOrders({MakeOpenOrder(105, 10, 118, 4)});
        assert(tr.RecordProduction(20, "Kelda", 118, 4, 105, true, alwaysInGuild, 6001, 1010).ok);

        ActionResult fullBank = tr.RecordBankDelivery(20, 118, 4, 4, TAB_NONE, true, alwaysInGuild, tab0Accessible, 6002, 1011);
        assert(!fullBank.ok && fullBank.reason == "bank_full");
        assert(tr.Orders()[0].transferredQty == 0 && tr.Orders()[0].receivedQty == 0 && tr.Orders()[0].status == "claimed");

        ActionResult badTab = tr.RecordBankDelivery(20, 118, 4, 4, 2, true, alwaysInGuild, tab0Accessible, 6003, 1012);
        assert(!badTab.ok && badTab.reason == "tab_inaccessible");
        assert(tr.Orders()[0].transferredQty == 0 && tr.Orders()[0].receivedQty == 0 && tr.Orders()[0].status == "claimed");
    }

    // 6. Missing recipient (left guild) blocks claim, production, and bank delivery receipt.
    {
        auto recipientGone = [](std::uint32_t) { return false; };
        Tracker tr;
        tr.LoadOrders({MakeOpenOrder(106, 10, 118, 2)});
        assert(tr.ClaimOrder(106, 20, "Kelda", true, false, 1010).reason == "recipient_missing");
        assert(tr.RecordProduction(20, "Kelda", 118, 2, 106, true, alwaysInGuild, 7001, 1011).ok);

        // Recipient leaves guild before bank delivery: must NOT mark delivered!
        ActionResult gone = tr.RecordBankDelivery(20, 118, 2, 2, 0, true, recipientGone, tab0Accessible, 7002, 1012);
        assert(!gone.ok && gone.reason == "recipient_missing");
        assert(tr.Orders()[0].status == "claimed" && tr.Orders()[0].receivedQty == 0);
    }

    // 7. Crash / reload recovery resumes partial persisted state with monotonic attemptId & revision CAS,
    //    including maxPersistedAttemptId seeding beyond active rows.
    {
        OrderState persisted = MakeOpenOrder(107, 10, 118, 20);
        persisted.status = "claimed";
        persisted.crafterGuid = 20;
        persisted.crafterName = "Kelda";
        persisted.attemptId = 42;
        persisted.producedQty = 12;
        persisted.transferredQty = 8;
        persisted.receivedQty = 8;
        persisted.deliveryTab = 0;
        persisted.revision = 9;

        Tracker trAfterRestart;
        trAfterRestart.LoadOrders({persisted, MakeOpenOrder(108, 11, 2840, 5)}, 99);

        // New claim on #108 gets attemptId > maxPersistedAttemptId (100)!
        ActionResult cNew = trAfterRestart.ClaimOrder(108, 30, "Brannoc", true, true, 2000);
        assert(cNew.ok && cNew.attemptId == 100);

        // Delivering 10 from bags when only 4 (12-8) were produced credits ONLY 4 (never phantom-delivers!).
        ActionResult dPartial = trAfterRestart.RecordBankDelivery(20, 118, 10, 10, 0, true, alwaysInGuild, tab0Accessible, 8001, 2005);
        assert(dPartial.ok && !dPartial.completedNow && dPartial.creditedQty == 4);
        assert(dPartial.expectedRevision == 9 && dPartial.newRevision == 10);
        assert(dPartial.order.receivedQty == 12);

        // Produce remaining 8 and deliver remaining 8 -> completes cleanly at revision 12.
        ActionResult pRest = trAfterRestart.RecordProduction(20, "Kelda", 118, 8, 107, true, alwaysInGuild, 8002, 2010);
        assert(pRest.ok && pRest.creditedQty == 8 && pRest.newRevision == 11);
        ActionResult dRest = trAfterRestart.RecordBankDelivery(20, 118, 8, 8, 0, true, alwaysInGuild, tab0Accessible, 8003, 2015);
        assert(dRest.ok && dRest.completedNow && dRest.newRevision == 12);
        assert(dRest.order.status == "delivered" && dRest.order.attemptId == 42);
    }

    // 8. Two claimed orders for the same item + separate transferred vs received receipt + DB CAS rollback on expiry race.
    {
        OrderState firstClaimed = MakeOpenOrder(201, 10, 118, 5);
        firstClaimed.status = "claimed";
        firstClaimed.crafterGuid = 20;
        firstClaimed.crafterName = "Kelda";
        firstClaimed.attemptId = 1;
        firstClaimed.producedQty = 0;   // claimed but nothing produced yet

        OrderState secondClaimed = MakeOpenOrder(202, 11, 118, 5);
        secondClaimed.status = "claimed";
        secondClaimed.crafterGuid = 20;
        secondClaimed.crafterName = "Kelda";
        secondClaimed.attemptId = 2;
        secondClaimed.producedQty = 5;  // produced and ready to deliver

        Tracker tr;
        tr.LoadOrders({firstClaimed, secondClaimed}, 2);
        assert(tr.PendingDeliveryQty(20, 118) == 5);

        // Must skip #201 (0 produced) and credit #202 cleanly!
        ActionResult dPartialRecv = tr.RecordBankDelivery(20, 118, 5, 3, 0, true, alwaysInGuild, tab0Accessible, 9001, 3000);
        assert(dPartialRecv.ok && dPartialRecv.orderId == 202 && !dPartialRecv.completedNow);
        assert(dPartialRecv.order.transferredQty == 5 && dPartialRecv.order.receivedQty == 3);

        // Simulate DB expiry race rollback: RollbackOrReplaceOrder restores authoritative expired row.
        OrderState expiredDb = secondClaimed;
        expiredDb.status = "expired";
        expiredDb.revision = 5;
        tr.RollbackOrReplaceOrder(expiredDb);
        assert(tr.Orders()[1].status == "expired" && tr.Orders()[1].revision == 5);
        assert(!tr.HasPendingDeliveryFor(20, 118));
    }

    // 9. Proposal completion guard rejects expired, interrupted, completed, wrong-order, or wrong-crafter proposals.
    {
        ProposalState validProp{501, 20, 101, 118, 3, "executing", 2000};
        assert(CanCompleteProposal(validProp, 101, 20, 1500));

        ProposalState expiredProp = validProp;
        expiredProp.expiresAt = 1500;
        assert(!CanCompleteProposal(expiredProp, 101, 20, 1500));

        ProposalState interruptedProp = validProp;
        interruptedProp.status = "interrupted";
        assert(!CanCompleteProposal(interruptedProp, 101, 20, 1400));

        ProposalState alreadyDone = validProp;
        alreadyDone.status = "completed";
        assert(!CanCompleteProposal(alreadyDone, 101, 20, 1400));

        assert(!CanCompleteProposal(validProp, 999, 20, 1400));  // wrong order ID
        assert(!CanCompleteProposal(validProp, 101, 99, 1400));  // wrong crafter GUID
    }

    std::cout << "ALL_ISSUE27_CPP_TESTS_PASSED\n";
    return 0;
}
"""


class Issue27WorkOrderVerificationTests(unittest.TestCase):
    def test_native_cpp17_work_order_state_machine_all_scenarios(self) -> None:
        """Compile and run the real C++17 CompanionWorkOrderLogic.h state machine across all Issue #27 scenarios."""
        self.assertTrue(CPP_HEADER.is_file(), f"Missing {CPP_HEADER}")
        compiler = shutil.which("g++") or shutil.which("clang++")
        if not compiler:
            self.skipTest("No C++17 compiler (g++/clang++) available")

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "test_work_orders.cpp"
            exe = Path(tmp) / "test_work_orders"
            src.write_text(CPP_HARNESS, encoding="utf-8")

            compile_res = subprocess.run(
                [compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", f"-I{CPP_HEADER.parent}", str(src), "-o", str(exe)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(compile_res.returncode, 0, f"C++ compile failed:\n{compile_res.stderr}")

            run_res = subprocess.run([str(exe)], capture_output=True, text=True, check=False)
            self.assertEqual(run_res.returncode, 0, f"C++ test binary failed:\n{run_res.stdout}\n{run_res.stderr}")
            self.assertIn("ALL_ISSUE27_CPP_TESTS_PASSED", run_res.stdout)

    def test_sql_migration_003_and_schema_parity(self) -> None:
        self.assertTrue(MIGRATION_003.is_file())
        mig = MIGRATION_003.read_text(encoding="utf-8")
        schema = SCHEMA_SQL.read_text(encoding="utf-8")

        for col in ("attempt_id", "produced_qty", "transferred_qty", "received_qty", "delivery_tab", "revision"):
            self.assertIn(f"`{col}`", mig, f"Migration 003 missing column {col}")
            self.assertIn(f"`{col}`", schema, f"Canonical companion_schema.sql missing column {col}")

        # MySQL 8.0 safety: must use information_schema.columns guard, never MariaDB-only ADD COLUMN IF NOT EXISTS.
        self.assertNotIn("ADD COLUMN IF NOT EXISTS", mig.upper())
        self.assertIn("information_schema.columns", mig)

    def test_cpp_integration_wiring_invariants(self) -> None:
        life_cpp = (ROOT / "mod-playerbots-companions" / "src" / "Bot" / "CompanionLife.cpp").read_text(encoding="utf-8")
        brain_cpp = (ROOT / "mod-playerbots-companions" / "src" / "Bot" / "CompanionBrain.cpp").read_text(encoding="utf-8")
        craft_strat = (ROOT / "mod-playerbots-companions" / "src" / "Ai" / "Base" / "Strategy" / "CompanionCraftStrategy.cpp").read_text(encoding="utf-8")
        bank_act = (ROOT / "mod-playerbots-companions" / "src" / "Ai" / "Base" / "Actions" / "GuildBankAction.cpp").read_text(encoding="utf-8")

        # OnCraftMade must NEVER mark delivered or call MarkProposalCompleted directly.
        on_craft_idx = life_cpp.index("void CompanionLife::OnCraftMade(")
        on_bank_idx = life_cpp.index("void CompanionLife::OnBankDepositVerified(")
        on_craft_body = life_cpp[on_craft_idx:on_bank_idx]
        self.assertNotIn("status = 'delivered'", on_craft_body)
        self.assertNotIn("MarkProposalCompleted", on_craft_body)
        self.assertNotIn("BumpRelationInternal", on_craft_body)

        # OnBankDepositVerified uses synchronous DB confirmation before side effects.
        on_bank_tail = life_cpp[on_bank_idx : on_bank_idx + 5500]
        self.assertIn("PersistWorkOrderTransitionSync(res, now)", on_bank_tail)
        self.assertIn("if (!ConfirmWorkOrderRowFromDb(res, low))", on_bank_tail)
        self.assertIn("RollbackOrReplaceOrder", life_cpp)
        self.assertIn("MarkProposalCompleted", on_bank_tail)

        # MarkProposalCompleted & ReloadProposals guard against overwriting completed/expired proposals.
        self.assertIn("AND status IN ('accepted', 'executing') AND expires_at > {}", brain_cpp)
        self.assertIn("gCompletedPropIds.Contains(propId)", brain_cpp)
        # CompanionCraftStrategy refreshes productBefore and passes producedDelta + callback ID.
        self.assertIn("CompanionLife::OnCraftMade(bot, r.product, \"\", r.skillId, producedDelta, cbId)", craft_strat)
        self.assertIn("run.productBefore = made;", craft_strat)
        # GuildBankAction splits pending deliveries unconditionally, protects ReservedBankQty, and calls OnBankDepositVerified.
        self.assertIn("uint32 const splitAmt = (pendingOrderDelivery && count > pendingOrderNeed) ? pendingOrderNeed : 0;", bank_act)
        self.assertIn("CompanionLife::OnBankDepositVerified(bot, entry, movedCount, movedCount, takenTab, cbId)", bank_act)
        self.assertIn("CompanionLife::ReservedBankQty(", bank_act)
        self.assertIn("CompanionLife::IsGuildBankTransferable(", brain_cpp)
        self.assertIn("CompanionLife::IsGuildBankTransferable(", bank_act)


if __name__ == "__main__":
    unittest.main()
