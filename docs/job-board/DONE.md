# Finished board cards (history)

Results of the cards that were done at the snapshot (2026-11-25), oldest first inside each area. Commit hashes are from the local `companions` branch of mod-playerbots on the server PC, not from this repository. The full prompt text of these cards stays on the board.

## Server

| Card | Date | Result |
|---|---|---|
| 18-section read-only server audit | 2026-11-14 | 15 bots used 202% CPU and 3.3 GiB. Alt bots had no autonomy and logged out with their master. Bots did not craft or use the auction house by themselves. |
| Fresh start | 2026-11-14 | Random bots wiped and recreated from level 1. |
| State check | 2026-11-16 | 550 bot characters existed, 15 online. Worldserver update time was healthy (5 to 10 ms), but swap was 96% full. |
| Nightly backup | 2026-11-18 | Cron runs the backup at 04:00 and keeps the newest 14. A restore test was not reported. |
| Performance tuning, four installer defaults | 2026-11-17 | Kept: MapUpdate.Threads = 2, MinWorldUpdateTime = 10, MapUpdateInterval = 100. CPU 123% to about 74%, mean update 11 ms. Compressed swap (zram) on. |
| Performance profile | 2026-11-17 | Finding: CPU is fine, memory is the limit. |
| Pure Wrath | 2026-11-17 | mod-individual-progression removed, stock world database rebuilt, mod-raid-gate built (cap 60; C'Thun then Kil'jaeden raise it). Spot checks later: 15 of 15 items and creatures match stock. |
| Offline tests and memory run | 2026-11-18 | Mean update 11 ms. An earlier 108 to 193 ms figure was a reading error (it averaged only the spike lines). The companion test failed: full bags stopped questing. |
| Housekeeping | 2026-11-18 | Code committed locally, GM addon installed. |
| Performance phase 1 (agent Neon) | 2026-11-21 | 54 Alliance bots (50 random + 4 companions). Worldserver 2.86 GiB with no swap (before: 4.64 GiB with 2.5 swapped), CPU about 40% (before 77%). Database 611 MiB. No update above 100 ms. Changes: database tuning (buffer pool 256M, performance schema off, binary log off), server containers pinned to cores 2-3, random bot maps 0 and 1, only bots within 110 yards of a player forced active, auction house cut from about 75,000 to 4,000 listings, DXVK for the game client. |
| Close the router port-forward | 2026-11-22 | Done by the owner. |

## Economy

| Card | Date | Result |
|---|---|---|
| Auction house: let the owner buy | 2026-11-17 | The stocker character moved to its own account with no GM level, so the owner's bids are no longer blocked. |

## Core four companions

| Card | Date | Result |
|---|---|---|
| Create the four characters; catch-up to level 17 | 2026-11-17 | Level 17, heirlooms and enchants, class-quest spells, 23 restored trainer spells. |
| Profession split | 2026-11-17 | Each companion has its two planned professions and tools. |
| Autonomy | 2026-11-17 | The four stay online with no master, quest and level alone, keep the owner XP cap, accept invites only from the owner, and follow him in a group. |
| Facts on bags, gathering, crafting, mail | 2026-11-18 | No stock strategy sells, banks, trains or crafts by itself. A bot with no master skips non-stackable loot above 80% bag use, so full bags stop quests. Guild bank: deposit only. Mail: only to the master. Gathering and skinning work within 100 yards. Activity weights are global. |
| Dungeon behaviour: tank leads | 2026-11-18 | Lead mode built: ready check, skull, pull. A companion never replaces an heirloom, wears only its armor type, and rolls Need only on a real upgrade. Progress goes to party chat; milestones by whisper. |
| Best-gear tables | 2026-11-18 | An offline script builds best-gear tables per 5 levels from the world database (Alliance sources, real drop chances, quest, dungeon, crafted and vendor sources). Not in the game. See issue #24. |
| Bag rules | 2026-11-20 | Replaced by Brain phase 1. |
| Brain phase 1: duties | 2026-11-20 | Sell, repair, train and guild bank (in and out) duties with scores; item classes; per-bot weights table; "why" command. Two bugs fixed on the way: the sell duty looped 490 times in one night (bag fullness counted as need); the quest-giver table held 582 of 6,656 givers, which was the root cause of the failed solo questing. |
| Activity lease and "why not" | 2026-11-19 | One owner per companion's movement. Commit 60 s, stall 120 s, cooldown 15 min, retry budget 3. |
| Class play tweaks | 2026-11-19 | Free Heroic Strike after Revenge (glyph). Paladin: Retribution Aura always on, heals only below 40%, Gift of the Naaru on the tank, Kings on the damage dealers so Battle Shout stays, Wisdom on himself and the healer. Hunter: pet Growl off in dungeons, pet tanks the diamond mark. Rogue: stealth, Garrote opener. Proven by the owner in two Stockades runs. |
| Brain phase 2: all professions | 2026-11-20 | Craft, cook, fish and first aid duties; gathering inside dungeons; need grows with the skill gap. Example: one companion took Fishing from 1 to 74 in a test. |
| Quests before grinding, with travel | 2026-11-20 | Quest scoring with travel cost: turn in, work, go to a hub, grind last. Flight paths, mounts bought with own gold, reputation goals and a "completionist" trait per bot. Run 4: 12 turn-ins by all four. |
| cards5: restore installer patches, fixes | 2026-11-20 | The companions branch had been built on clean upstream, so every installer patch was missing. Restored: module patches 0003 to 0021 and seven core patches (AQ40, Sunwell, Blackwing Lair, pet growl and others). Also: boats dock to dock, the Darnassus portal, hearthstones, the tank keeps a shield, Kelda turned in a Kalimdor quest. |
| cards6: no fights on trips, other continents, hearth rules, shutdown crash | 2026-11-20 | Pass: shutdown crash with companions in an instance fixed (3 clean stops); trip combat 3.5 min/h or less on quest trips; tram, Dalaran portal, milling, abandon guard. Fail: quest turn-ins (0, 2, 2 and 1 companions in four runs); Blacksmithing stuck with no materials; one companion in a death loop for hours in a zone far above its level. Follow-up: cards7. |

## Raid team

| Card | Date | Result |
|---|---|---|
| Raid feasibility audit | 2026-11-19 | Found the missing installer patches. No bot code for Skeram, Sartura or Fankriss. The C'Thun and Kil'jaeden core scripts are complete. |
| Guild 40 capability audit | 2026-11-20 | Top findings: bots cannot form a dungeon group without a human; memory is the limit; the random-bot refresh clears bags, so guild bots must be excluded from it; commitment, memory and personality existed only for the four companions; stock bot chat replies are on. Full text: [PHASE_II_AUDIT_II00.md](../PHASE_II_AUDIT_II00.md) covers the newer audit. |

## Outside reviews

| Card | Date | Result |
|---|---|---|
| Fresh-eyes review by other AI models | 2026-11-16 | Four models answered. Their ideas are in [IDEAS.md](IDEAS.md). |
| Brief: how the bots should act | 2026-11-19 | Sent for review. The answers are in [IDEAS.md](IDEAS.md). |

## Child player's addon (LoganReads)

| Card | Date | Result |
|---|---|---|
| Account and addon build | 2026-11-16 | Quest text rewritten in early-reader words, with audio and a word checker. It worked in the first session. |
| Kid text for Northshire and Elwynn | 2026-11-16 | 49 quests, 231 audio files. About 85 new words were added, more than the 3-per-quest rule allows. |
| Follow-up | 2026-11-17 | Bagnon, Skada and Questie installed; quiet mode (leaves public channels, declines duels and guild invites); a help log of the texts he asked to hear. |
