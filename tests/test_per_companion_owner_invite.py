"""Regression test: per-companion owner resolution for party invites, heals, blessings, quests, and replies."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMP_SRC = ROOT / "mod-playerbots-companions" / "src"


class PerCompanionOwnerTests(unittest.TestCase):
    def test_accept_invitation_uses_per_companion_owner(self) -> None:
        src = (COMP_SRC / "Ai" / "Base" / "Actions" / "AcceptInvitationAction.cpp").read_text(encoding="utf-8")
        self.assertIn("FindOwnerOnlineFor(bot)", src)
        self.assertIn("OwnerLowFor(bot)", src)
        self.assertNotIn("FindOwnerOnline()", src)

    def test_class_strategy_duty_and_quest_use_per_companion_owner(self) -> None:
        cls_src = (COMP_SRC / "Ai" / "Base" / "Strategy" / "CompanionClassStrategy.cpp").read_text(encoding="utf-8")
        self.assertNotIn("FindOwnerOnline()", cls_src)
        self.assertGreaterEqual(cls_src.count("FindOwnerOnlineFor(bot)"), 2)

        duty_src = (COMP_SRC / "Bot" / "CompanionDuty.cpp").read_text(encoding="utf-8")
        self.assertNotIn("FindOwnerOnline()", duty_src)
        self.assertIn("FindOwnerOnlineFor(bot)", duty_src)

        quest_src = (COMP_SRC / "Bot" / "CompanionQuest.cpp").read_text(encoding="utf-8")
        owner_has_quest = quest_src.split("bool CompanionQuest::OwnerHasQuest(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("FindOwnerOnlineFor(bot)", owner_has_quest)
        self.assertNotIn("FindOwnerOnline()", owner_has_quest)


if __name__ == "__main__":
    unittest.main()
