import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hearthcore.dungeons import (
    DUNGEON_BY_ID,
    DUNGEON_CATALOG,
    enabled_dungeon_ids,
    estimate_travel_and_prep_minutes,
)

BOT = Path(__file__).resolve().parents[1] / "mod-playerbots-companions" / "src" / "Bot"

HARNESS = r"""
#include <cstdio>
#include "CompanionCoopLifecycleLogic.h"
#define CHECK(e) if (!(e)) { std::printf("FAIL line %d: %s\n", __LINE__, #e); return 1; }
using namespace CompanionLfgBridge;
int main()
{
    CHECK(ResolveSupportedLfgDungeon(189, 0) == nullptr);           // four wings: never guess
    CHECK(ResolveSupportedLfgDungeon(189, 0, 165)->lfgDungeonId == 165);
    CHECK(ResolveSupportedLfgDungeon(189, 0, 6) == nullptr);        // id from another map
    CHECK(ResolveSupportedLfgDungeon(409, 2) == nullptr);           // raid
    CHECK(ParseSupportedDungeonMapFilter("").empty());
    CHECK(ParseSupportedDungeonMapFilter("all").count(409) == 0);
    CHECK(ParseSupportedDungeonMapFilter("409,34").size() == 1);

    CompanionCoopLifecycle::LifecycleController c;
    // Single-wing dungeon with no scripted route
    c.RegisterOrRefreshRun(1, 10, 33, 100, true, 8);
    CHECK(!c.RecordCreatureKill(10, 33, 1, false, 101));
    CHECK(c.RecordCreatureKill(10, 33, 4275, true, 102));
    // Multi-wing: only the queued wing's boss completes the run
    c.RegisterOrRefreshRun(2, 20, 189, 100, true, 165);
    CHECK(!c.RecordCreatureKill(20, 189, 4543, true, 101));
    CHECK(c.RecordCreatureKill(20, 189, 6487, true, 102));
    // Multi-wing with unknown wing: no boss kill completes it
    c.RegisterOrRefreshRun(3, 30, 189, 100, true);
    CHECK(!c.RecordCreatureKill(30, 189, 6487, true, 101));
    return 0;
}
"""


class AllRdfDungeonsTests(unittest.TestCase):
    def test_python_and_cpp_tables_agree(self):
        text = (BOT / "CompanionLfgBridgeLogic.h").read_text()
        rows = re.findall(r'\{(\d+), (\d+), (\d+), \d+, \d+, (\d+), \d+, \d+, "(\w+)"\}', text)
        faction = {"both": 0, "alliance": 1, "horde": 2}
        cpp = {r[4]: (int(r[0]), int(r[1]), int(r[2]), int(r[3])) for r in rows}
        py = {d.dungeon_id: (d.lfg_dungeon_id, d.map_id, d.difficulty, faction[d.faction])
              for d in DUNGEON_CATALOG if d.supported}
        self.assertEqual(len(py), 92)
        self.assertEqual(py, cpp)

    def test_keywords_never_enable_raids_and_default_is_off(self):
        for raw, count in (("", 0), ("all", 92), ("*", 92), ("normal", 60), ("heroic", 32), ("molten_core_r", 0)):
            with mock.patch.dict(os.environ, {"HEARTHCORE_COOP_DUNGEONS": raw}):
                self.assertEqual(len(enabled_dungeon_ids()), count, raw)

    def test_unknown_entrance_is_never_treated_as_a_short_trip(self):
        spec = DUNGEON_BY_ID["wailing_caverns_n"]
        out = estimate_travel_and_prep_minutes(spec, current_map_id=1)
        self.assertIn("entrance_unknown", out["prep_needs"])
        self.assertNotIn("cross_continent_travel", out["prep_needs"])
        self.assertGreaterEqual(out["travel_minutes"], 25)

    @unittest.skipUnless(shutil.which("g++"), "g++ not available")
    def test_native_cpp17_wing_resolution_and_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, exe = Path(tmp) / "t.cpp", Path(tmp) / "t"
            src.write_text(HARNESS)
            comp = subprocess.run(
                ["g++", "-std=c++17", "-DNDEBUG", "-Wall", "-Wextra", "-Werror", f"-I{BOT}", str(src), "-o", str(exe)],
                capture_output=True, text=True)
            self.assertEqual(comp.returncode, 0, comp.stderr)
            run = subprocess.run([str(exe)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout)


if __name__ == "__main__":
    unittest.main()
