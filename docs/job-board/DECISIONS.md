# Decisions log

The owner's decisions, copied from the board's "Decisions log" card and from the later cards. Newest rules win where two disagree. Check [SERVER_STATE.md](../SERVER_STATE.md) for what is installed now.

## Design rules that still apply

- **No AI model in the game loop.** Real-time play is C++ and data tables. A model may plan outside the game and write only data that the code checks.
- **Scores, not fixed rules.** Each choice is a score: need x weight, less travel cost. A bot may manage its time badly; that is realistic.
- **Rules-true.** Bots earn levels, items, gold, professions and mounts inside the game rules. Exceptions only where they save PC resources, and each exception is named.
- **The guild bank is the exchange hub** for materials, gear and consumables. Bots walk to a real guild bank.
- **The owner has the final say** on the raid roster and on when raids happen.
- **Personality is weights** on the same score stack, within bounds. No exaggerated traits.
- **Build only with the worldserver stopped.** Measure before and after each change.
- **Never enable the stock "maintenance" strategy for companions.** It destroys items.

## By date

**2026-11-14**
- Fresh start approved. Random bots were wiped and start at level 1.

**2026-11-16**
- All content is WotLK. Only the expansion is capped.
- Companions stay at the owner's level + 3 (a limit, never a goal).
- Rules-true, unless another way saves resources.
- Auction house: any stocker item is fair to buy with gold. No provenance rule.
- The next month's priority is the owner's own leveling.

**2026-11-17**
- Remove mod-individual-progression. Pure 3.3.5a world. A small raid-gate module sets the level cap: 60, then 70 after C'Thun, then 80 after Kil'jaeden.
- Profession split: Wonky Enchanting + Tailoring; Brannoc Mining + Blacksmithing; Aldric Herbalism + Alchemy; Kelda Skinning + Leatherworking; Wren Jewelcrafting + Inscription. Engineering is left out.
- Kelda is a Night Elf.
- The owner and the companions are in one guild.

**2026-11-18**
- Armor type is a per-character setting. Brannoc (main tank): best type only. Aldric (Retribution, not a tank) may wear leather or his best type. Kelda and Wren: strict.
- The core four have full-size bags. Bags are not a goal for them.
- A companion never replaces an heirloom.

**2026-11-19**
- All companions have Cooking, Fishing and First Aid. Leveling all their professions is a goal.
- Consumable buffs (food, weapon oil) are used only when the buff is missing.
- Builds can be ambitious: larger jobs per build are fine.
- In dungeons the group does not stop for a gatherer. Mobs near a node are cleared first.
- No cap on crafting per session. A bot may spend a whole session crafting when its skill gap is large.
- Raids: a full 40 is preferred; fewer is fine where Wrath scaling allows. Raids happen when the owner asks, not on a fixed night.
- Guild bots may invite each other and queue dungeons while the owner is offline, to pursue their best-gear lists.
- The raid team is also level-capped near the owner. Each raider gets its own offset from -3 to +3, so the owner meets them in the world at his level. This replaces the 2026-11-16 plan of "no level cap for raiders".
- All bots learn riding and buy their mounts with their own gold.
- Grey quests are fully doable. Bots are XP-capped near the owner, so quests are valued for reputation, items, gold and completion. Reputation grinds are a goal.

**2026-11-20**
- Travel: "During travel time, combat should be avoided as much as possible."
- Hearthstone on the same continent is fine when the bot does not need the hearth soon.
- Keep the stuck teleport: "if a bot is stuck it should be able to unstuck itself."
- Companions take boats to work quests on other continents. Boats work dock to dock: the bot travels to the dock, waits, and is moved to the other dock.
- Companions do low-level quests and follow quest chains. They may buy vendor quest items with their own gold.
- No road data on this server is acceptable. Routes use the navmesh.
- Bot chat: the owner decided that no extra chat filter is needed.
- Lead mode: do not change the tank's pull pace. Keep the buffs, the pet off-tank on the diamond mark, and the Garrote opener. Add target persistence for melee damage dealers and a soft healer tether before a new pull.
- Home inn in Dalaran with the city portals is an accepted travel route for companions.

**2026-11-21 and 2026-11-22**
- Performance work is config first, measured, one group of changes at a time.
- Two agents work on the server PC. Only one restarts the server at a time. Each agent records its config changes so that the other's rollback does not undo them.
- The router port-forward is closed (card done 2026-11-22).

**2026-11-25**
- The tram move is approved as an instant platform-to-platform move, on the same terms as the boat move (issue #80).
- The three fallback teleports that fire when a portal or area trigger refuses a companion are to be removed (issue #80). A companion walks to the trigger, and the leg fails if the game does not accept it.
- The character name Logan may stay in the public repository.
- A code audit added issues #52 to #86. See the issue list for the findings.
