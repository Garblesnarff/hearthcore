# Ideas and open work from the Job Board

Every board card that was not done at the snapshot (2026-11-25). Status words are the board's: **running** (a prompt was copied to an agent), **ready**, **waiting** (needs another job first), **idea** (not decided).

Read [README.md](README.md) first: parts of this are older than the repository.

## In progress at the snapshot

Full prompts: [OPEN-PROMPTS.md](OPEN-PROMPTS.md).

### cards7: turn-ins, death loop, grey quests, craft materials, guild bank (running)
Follows the cards6 build, whose main test failed on quest turn-ins.
- Find the turn-in cause first, with a config test and the lease log, before code changes.
- Death loop and danger zones: a route may not pass through an area where hostile creatures are more than 5 levels above the companion. After 2 deaths in one place, use the spirit healer and leave the zone.
- Grey quests and quests at the XP cap: value = reputation + reward + completion. Go to another continent when the sum of the quests there is worth one round trip.
- Low-level zones and quest chains: go back to zones with open quests, and take the follow-up quest also when its giver is in another zone.
- Keep riding when attacked on every trip type, not only quest trips.
- Craft materials by gathering: when a craft need is high and no material is in bags, bank or a vendor, make a gather trip. See issue #23.
- Guild bank full: report contents; withdraw and sell items above the stock limit; delete nothing.

### Lead mode v2: target persistence and healer tether (running)
- Melee damage dealers keep their target while it is alive, in combat and in reach. They do not follow each change of the tank's target.
- A skull on a pack that is not in combat yet does not pull the damage dealers.
- Before a new pull, the healer must be within 36 yards of the tank and in line of sight. No limit during combat.
- Do not change: pull pace, chain pull, buffs, pet off-tank, Garrote opener.

### Lead mode: kill order with raid marks (running)
- The tank's target (threat) and the group's kill order are two separate choices.
- Once per engaged pack, score each mob: heals others, casts, low health, fleeing, elite. Mark skull, X, circle in that order.
- The diamond stays the hunter pet's off-tank mark. Moon and square stay free for crowd control.
- A mark set by the owner wins.
- Later: role jobs (the rogue kicks a caster while the hunter shoots the skull), crowd-control marks.
- Source fact: the stock tank picks the attacker on which it has the lowest threat, so the order of its Sunders is not a kill order.

### Performance phase 2 (running, agent Neon)
- A change log and a check script, so two agents do not undo each other's config work.
- Prove that the lower sight distance and the slower react delay do not hurt the companions, or set them back.
- A 3-hour soak test and the memory per extra bot.
- Report only: what "no Horde bots" removes (battleground enemies, world PvP, the neutral auction house) and the cost of a small Horde group.

## Ready, not started

### Logan's Coach v1.1 (ready)
Addon files only. Find the voice that the quest audio uses and make the coach voice lines with it. Fix the count of Heroic Strike presses (an on-next-swing ability). Prompt in [OPEN-PROMPTS.md](OPEN-PROMPTS.md).

### Realm News: a nightly report by in-game mail (ready)
- A nightly job at 04:30. A script takes a snapshot of every character and compares it with the day before.
- Facts: level-ups, realm firsts (first to 10, 20, ... 60), richest gain, guild joins, market prices for 10 common materials.
- Up to 8 short lines from templates, mailed to the owner's character. Every line must trace to a recorded fact.
- An optional AI rewrite, only when memory is free and the game is closed. Each rewritten line is checked against its fact.
- Later: 3 event lines at login, a daily digest outside the game.
- Prompt in [OPEN-PROMPTS.md](OPEN-PROMPTS.md).

### PvP and twink bracket audit (ready, read-only)
Find what a level 19 Warsong Gulch bracket needs: how bots fill a queue, XP lock, a fixed-level bot pool, a fixed gear set, the battleground strategies. Prompt in [OPEN-PROMPTS.md](OPEN-PROMPTS.md).

### Weekly 5-question playtest (ready, manual)
After a normal session the owner answers:
1. Did another character surprise you?
2. Did you know anyone from an earlier session?
3. Did a group form without your order?
4. Did something change in the economy?
5. Was combat smooth the whole time?
The answers decide what gets built next.

## Waiting on another job

### Logan's Coach v1: first test in game (waiting, owner)
Built 2026-11-18 and not tested in the game. Built: letter keys per character (C Charge, R Rend, H Heroic Strike, B Battle Shout, T Thunder Clap, V Victory Rush) with bags moved to I, one large ability icon, a keyboard picture, target frame scale and glow, stars, stages A and B. Not built: the voice, the Thunder Clap rule. Known limits: the skull mark stays on the old mob; nameplate arrows and the solo skull are unproven.

### Autonomous dungeon groups (waiting) - see issues #20, #21, #14, #17
- Guild bots form groups and queue dungeons while the owner is offline.
- Need comes from each bot's best-gear list. Dungeon value = gear + quest + XP + social - opportunity cost, by personality.
- Use the real Dungeon Finder: it gives 1 tank, 1 healer, 3 damage and the game's own teleport.
- A "wants a dungeon" row lets a coordinator bring matching bots online together, within the memory budget.
- First milestone: one five-bot group queues, clears, loots and goes back to normal play.
- Group capacity is set by tanks. Plan tank-capable second specs for hybrids.
- Audit facts (2026-11-20): stock bots queue only for a dungeon that a non-bot player queued for; only random bots may queue; no boot-vote handling; no queue timeout; classic dungeons have no boss tactics.

### Bot brain roadmap (waiting) - see issues #3 to #8, #24
Built and proven on the core four: duties with scores, item classes KEEP / BANK / SELL, the activity lease, per-bot weights, "why" and "why not", the guild bank ledger with reservations, mounts, flight paths, boats.

Not built when the card was written:
- **Projects layer:** a gear upgrade from the best-gear table, reputation, class quests, guild orders, flight paths.
- **Whims:** fishing for fun, sitting in an inn, a duel, hunting a rare spawn, exploring.
- **Guild coordinator:** stock minimum and maximum per item; a minimum makes a craft or gather order; a maximum sends overstock to the auction house and gold to the guild fund; guild repairs.
- **Planner model outside the game:** reads a small state file, picks only from goals the code already checked, sets weight multipliers within 0.5x to 1.5x, never writes SQL or code. Shadow mode first. If it fails, bots keep the last plan, then scores only.
- **Daily goals for named bots** from a fixed list (reach a level, a bag, a material, a mount). The loader rejects goals that are not on the list.
- **Play sessions:** a bot logs in at an inn, plans, plays, goes home near the end, does bank and mail errands, logs out with rested XP.
- **Memory without training:** counters that fade. Deaths in a zone lower that zone's score. After a wipe, a caution value makes the tank wait longer and want more healer mana.

### Raid team plan (waiting) - see issues #16, #25
- 39 guild bots plus the owner = 40. Register about 45 to 50 so that there are substitutes.
- Alliance composition, 4 tanks, 10 healers, 26 damage: Warrior 5 (2 tank, 3 damage); Paladin 6 (1 tank, 3 heal, 2 damage); Priest 4 (3 heal with the owner, 1 shadow); Druid 5 (1 tank, 2 heal, 1 balance, 1 feral); Shaman 4 (2 heal, 1 enhancement, 1 elemental); Mage 5; Warlock 4; Hunter 4; Rogue 3. Add 2 death knights when Wrath opens.
- Professions for 40: about 22 with a crafting profession plus its gathering one, about 8 with two crafting, about 10 with two gathering (herbs are the raid bottleneck). Every crafting profession has 3 or more bots. Several enchanters.
- Left to do: raiders act well at the level cap (farm, gear, raid preparation).
- Risk: a guild led by a real account forces all its bot members fully active.

### Raid combat backlog (waiting) - see issue #16
- Raid strategies exist for Molten Core, Onyxia, Blackwing Lair, AQ20, Naxxramas, the Burning Crusade raids and the Wrath raids, plus AQ40 and Sunwell from the installer patches (untested).
- No Zul'Gurub strategy. No bot code for Skeram, the Bug Trio, Sartura, Fankriss, Viscidus or Ouro. Chromaggus has comments only.
- To do later: generic tank-swap and stack actions; a data table for simple boss mechanics; the missing AQ40 bosses.
- A bot-only test "gym" that logs each death: all reviewers said to delay it. First run dungeons with the owner and note each death.

### Bot shopping lists: real demand on the auction house (waiting)
Each bot has a short want list (food, a bag, one profession material). An hourly or nightly batch buys the cheapest listing under its limit and logs the price. Later: price bands that move up after sales and down after no sales.

### The named dozen (waiting)
Pick 12 world bots that stay near the owner's level. Each gets one row: current goal, a friend, a rival, last death. The goal biases the existing activity weights. The owner should know 5 of them by name after a week.

### Twink pool build (waiting)
Cheaper path first: make about 19 bots at level 19 by command and give gear by script, then play one Warsong match to see if it is fun. Build a pool module only if that works.

### Put the addons on the laptop (waiting, manual)

## Ideas, not decided

### Idea backlog from the outside reviews
**Guild life**
- Relationships per bot pair (runs together, wipes, help given, loot contests). They change who helps and groups with whom; never the owner's raid roster.
- Work orders with a story: who asked, for whom, why, materials, delivery ("Brannoc needs boots; Kelda makes them from skins she asked for").
- Owner session awareness: AVAILABLE, BUSY, COMMITTED. A bot finishes a valuable errand before it joins, and says so truthfully.
- Event memory (5 to 10 real events per bot) for chat and scores. Every line from a real event and a reviewed template.
- Personality archetypes as weight sets with small variation: Sentinel, Quartermaster, Artisan, Frugal, Packrat, Wayfinder, Scholar, Competitor, Helpful Neighbor, Ritualist. Flaws only outside combat.
- Bots propose activities to the owner (a dungeon, fishing, a duel). A no is fine, with a cooldown.
- Contextual emotes: wave back, cheer a level-up, sit at the inn.
- Fixed weekly events: a Sunday dungeon run, a fishing night. A rival guild with a weekly scoreboard. A named Horde rival in contested zones. Three guild cultures: progression, social, PvP.
- A neighbourhood budget: 4 to 8 active bots in the owner's quest area. One named crafter that posts on the auction house.

**Dungeons and raids**
- Arrival routines: travel to the instance, repair, wait at the entrance; meeting stones. Corpse recovery decisions. Bag projection before a long trip.
- Group contract: no pull before ready, one puller, wait for healer mana, one kill target, regroup after combat.
- Raid preparation: attunement registry, consumable bill per raid, encounter jobs (interrupts, dispels, crowd control), loot policy, attendance and bench fairness.
- Stopping fleeing mobs (Hamstring, Hammer of Justice, Concussive Shot, Kidney Shot). Not built on purpose: the owner liked the extra challenge.

**Tools and rules**
- A replay test harness for bot decisions.
- A compatibility gate: each change records the source commits and has a smoke test.
- Item provenance: auction items from the stocker are spawned gear. One written rules-true policy per tier, with each exception listed.
- Raid timeline model: measure XP per bot-hour for a week, then project when each raid role is ready.
- Two-level simulation: far bots log out and a formula adds their progress. Only if memory needs it.
- A judgment layer that turns the owner's chat into ledger entries. Never in combat.
- Skills to write for the agents: early-reader quest text (word list, sentence frames, checker per zone); boss-row (read a boss script, write table rows, run a test).

**For the child player**
- Treasure hunts: a parent places a chest and gives a clue at the child's reading level. A custom NPC with small jobs ("Bring me 5 apples").
- Sound-it-out mode; counters as circles that fill; 2 or 3 big choices the game acts on; letters from bot friends; a map to choose from; shop choices with real coins.
- Early-reader quest text for Westfall and the Deadmines, with at most 3 new words per quest.

**Risk noted:** many level 1 bots compete for the same quest monsters in start zones.

### Guild chat news (idea)
Guild bots post short news shout-outs from the feeds the owner follows, talk about the news with each other, and answer when the owner speaks to them.
- A batch job outside the game, 2 or 3 times a day: read new items; one model call writes the shout-outs and short scripted talks between named bots; each line is tied to an article ID and checked. Output to a table (time, speaker, line, link).
- In the game, a timer posts queued lines in guild chat by that bot, only if it is online. No model during play.
- Answers: when the owner names a bot in guild chat, a sidecar calls the model with the bot's personality and the day's articles, with a daily call limit.
- Open choices: in character (a "Gnomish Gazette" framing) or out of character like real players.
- Note: this conflicts with the repository's "Zero LLM Chat" invariant. It needs an explicit owner decision before any work.

### Code optimizations for this PC (idea)
Measure first with a profiler, change one hot spot, measure again. Likely targets: bot data prepared for levels above the cap; bots that think at full speed far from any player; caches for all bot characters when few are online; a cache for the hunter "pet is dead" value, which does a database query on each read; skipping the non-English locale tables. Keep each change small and behind a config switch.

### Arena and roster modules: remove the era-talents dependency (idea)
Four modules call code from mod-era-talents, so a stub of it is still compiled. A small patch can remove the call. Do it when the cap goes to 70, or when it blocks an update.

### More memory (idea, hardware)
The server PC has one 8 GB DDR4-2400 stick and one empty slot; the board supports 32 GB. A second 8 GB stick doubles the memory and gives dual-channel memory.

## No issue yet

These board items have no GitHub issue at the snapshot. Open one before work starts.

- Lead mode v2 (target persistence, healer tether) and the kill order with raid marks.
- Danger-zone routing and the death-loop rule (cards7, item 1), if not already covered by the installed "danger-death revive".
- Quest chains and low-level zone questing for companions.
- Keep riding when attacked on all trip types.
- Guild bank stock limits and overstock handling.
- Realm News.
- The named dozen.
- Bot shopping lists on the auction house.
- The twink bracket.
- Guild chat news (needs an owner decision first).
- Horde bots: how many, and what a zero-Horde world removes.
