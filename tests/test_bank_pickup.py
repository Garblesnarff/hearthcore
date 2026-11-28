import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "mod-playerbots-companions" / "src"

HARNESS = r"""
#include <cstdio>
#include "CompanionBankPickupLogic.h"
#define CHECK(e) if (!(e)) { std::printf("FAIL line %d: %s\n", __LINE__, #e); return 1; }
using namespace CompanionBankPickup;
int main()
{
    // Never vendored out of the bank: bags, recipes, green+ equipment
    CHECK(NeverPurge(CLASS_CONTAINER, 1) && NeverPurge(CLASS_RECIPE, 1));
    CHECK(NeverPurge(CLASS_ARMOR, 2) && NeverPurge(CLASS_WEAPON, 3));
    CHECK(!NeverPurge(CLASS_ARMOR, 1) && !NeverPurge(7, 3) && !NeverPurge(0, 2));

    Candidate r; r.itemClass = CLASS_RECIPE; r.canUse = true; r.teachesSpell = true;
    CHECK(Decide(r) == Kind::Recipe);
    r.knowsSpell = true; CHECK(Decide(r) == Kind::None);
    r.knowsSpell = false; r.alreadyHolds = true; CHECK(Decide(r) == Kind::None);
    r.alreadyHolds = false; r.canUse = false; CHECK(Decide(r) == Kind::None);

    Candidate b; b.itemClass = CLASS_CONTAINER; b.canUse = true; b.emptyBagSlot = true;
    CHECK(Decide(b) == Kind::Bag);
    b.subClass = 1; CHECK(Decide(b) == Kind::None);           // soul bag etc.
    b.subClass = 0; b.emptyBagSlot = false; CHECK(Decide(b) == Kind::None);

    Candidate g; g.itemClass = CLASS_ARMOR; g.canUse = true; g.isUpgrade = true; g.equipAllowed = true;
    CHECK(Decide(g) == Kind::Gear);
    g.equipAllowed = false; CHECK(Decide(g) == Kind::None);   // heirloom / armor type rules
    g.equipAllowed = true; g.isUpgrade = false; CHECK(Decide(g) == Kind::None);

    Candidate m; m.itemClass = 7; m.canUse = true; m.isUpgrade = true; m.equipAllowed = true;
    CHECK(Decide(m) == Kind::None);                           // materials stay with the craft duty
    return 0;
}
"""


class BankPickupTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("g++"), "g++ not available")
    def test_native_cpp17_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, exe = Path(tmp) / "t.cpp", Path(tmp) / "t"
            src.write_text(HARNESS)
            comp = subprocess.run(
                ["g++", "-std=c++17", "-DNDEBUG", "-Wall", "-Wextra", "-Werror", f"-I{SRC / 'Bot'}", str(src),
                 "-o", str(exe)], capture_output=True, text=True)
            self.assertEqual(comp.returncode, 0, comp.stderr)
            run = subprocess.run([str(exe)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout)

    def test_organizer_and_duty_triggers_use_the_guard(self):
        organizer = (SRC / "Ai/Base/Actions/GuildBankAction.cpp").read_text()
        self.assertIn("if (CompanionBankPickup::NeverPurge(proto->Class, proto->Quality))", organizer)
        self.assertEqual((SRC / "Bot/CompanionDuty.cpp").read_text().count("CompanionBankPickup::NeverPurge("), 2)


if __name__ == "__main__":
    unittest.main()
