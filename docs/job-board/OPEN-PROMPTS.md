# Open prompts from the Job Board

The prompt text of the cards that were ready or in progress at the snapshot (2026-11-25). Each prompt was written for an AI coding agent on the server PC. Commit hashes are from the local `companions` branch of mod-playerbots on that PC.

These are historical work orders. Before you use one, check it against the installed state in [SERVER_STATE.md](../SERVER_STATE.md) and the open GitHub issues.

---

## cards7 - turn-ins, death loop, grey quests, craft materials, guild bank

Status: running. Owner: Coder.

```text
TASK: Continue on the companions branch from b8230680. Worldserver STOPPED for each build. Back up acore_characters and tag the image first (pre-cards7). Commit locally only. Each new feature has its own on/off key.

OWNER DECISIONS (2026-11-20, to your 7 questions)
1. Kelda's grey Kalimdor quests: value them. Standing owner rule: 'grey quests should still be able to be done ... completionist ... Rep grinds' and 'The bots cap their xp anyway, so grey doesn't matter.' Do not abandon them.
2. Quests at the level cap: yes, companions take grey quests, and may buy vendor quest items with their own gold (rules-true).
3. Full guild bank: see item 6. Do not delete items. The owner may buy another tab later.
4. Roads: no road data is OK. Use the navmesh only.
5. Bot chat: the owner says no extra filter is needed. Leave bot chat as it is. Build nothing.
6. Prospecting test: yes, one stack of Copper Ore by .additem to Wren (not SQL).
7. Next round: yes, all of it, as below.

STEP 1 - FIND THE TURN-IN CAUSE (no build)
a. Set DoQuest.ItemSearch = 0, restart, run 60 minutes. Compare with run 7 (9 turn-ins) and run 8 (2).
b. For each companion and each run 5 to 8, make a table from the lease log and 'why': minutes per activity (quest work, turn-in trip, hub trip, grind, craft, duty, dead, stuck, idle), quests in the log by state (complete, workable, blocked and why, grey), and the top quest scores. Aldric's 7 hours dead count as the cause for him.
c. State the cause with proof before you change the quest code. Likely causes to check: no non-grey quests left at the cap; grey quests scored too low or not accepted; item-drop quests with no loot (Brannoc 0/10 and 0/6 on quests 90 and 93: check that he kills the right mobs and that he loots them; mod-junk-to-gold must not remove quest items).

STEP 2 - BUILD
1. DEATH LOOP AND DANGER ZONES (Aldric, Burning Steppes, level 27 among level 58 mobs, 11:16 to 18:05).
 a. Route rule: a route may not pass an area where hostile creatures are more than 5 levels above the companion, unless it is on a flight path. If no safe route exists, the destination is blocked: cooldown, pick another goal. Report how Aldric got there (which goal, which route).
 b. Death rule: after 2 deaths within 10 minutes in the same area, do not revive at the corpse again. Use the spirit healer, then leave the zone (hearth if ready, else walk a safe route), and put a 2-hour cooldown on that area. Whisper the owner once.
 c. A watchdog: a companion dead or in a death loop for more than 15 minutes is logged at WARN and shown in 'companion status'.
2. GREY QUESTS AND THE CAP. Accept, work and turn in grey quests. At the XP cap, quest value = reputation + item or gold reward + completion value, with no penalty for grey. Vendor quest items: buy with own gold, under the daily spend cap. For another continent: add up the value of ALL quests there (Kelda has 6 in Kalimdor, with Darnassus reputation as her goal) against one round trip; go when the sum wins, and do all of them in one visit.
   LOW-LEVEL QUESTS (owner, 2026-11-20: 'are the bots doing low level quests? they should also be doing that stuff'). The audit shows Companion.Quest.AcceptGrey = 1, so the setting is on. Prove what happens: per companion, count grey quests accepted and turned in during runs 5 to 8, and list the hubs each one chose with the hub level. Then make sure: (a) hub choice includes low-level zones (Elwynn, Westfall, Loch Modan, Dun Morogh, Darkshore, Teldrassil, Azuremyst, Bloodmyst and the cities) when their quests are not done; (b) quest chains continue: after a turn-in, take the follow-up quest (quest_template_addon NextQuestID / PrevQuestID, RewardNextQuest), also when its giver is in another zone (the audit found no chain logic); (c) a companion at its XP cap prefers a zone it can clear fast. Pass: each companion turns in at least 3 grey quests in the test, and one chain of 2 or more quests is finished.
3. RIDE-THROUGH ON ALL TRIPS. The rule 'keep riding when attacked' works only on quest trips. Use it on craft, bank, vendor, train and hub trips too. Same fight-back conditions as cards6: fight back only if the companion is dismounted, OR the same mob still hits it 10 seconds after the attack started, OR health is below 50%, OR the mob is a quest mob for a quest in its log.
4. CRAFT MATS BY GATHERING. Brannoc's Blacksmithing is 32 with no Copper Bar or Rough Stone. When a craft need is high and the mats are not in bags, bank or a vendor, the craft duty makes a gather goal: go to a zone with the node (Copper Vein for Brannoc), mine enough for N crafts, smelt, craft. The same rule for Aldric (herbs), Kelda (leather), and Wren (ore and herbs from the bank first). Rough Stone comes from mining. Pass: Brannoc gains Blacksmithing.
5. SAME-CONTINENT HEARTH. The rule never fired (savings under 3 minutes). Lower the limit to 2 minutes and report how often it would have fired in the run-5-to-8 logs. If still never, leave it; no more work.
6. GUILD BANK FULL. Report the bank contents per tab: item, stacks, who deposited (ledger). Check why Companion.Bank.MaxStacksPerItem (3) did not stop it. Then: for items above the limit, the depositing companion withdraws the excess and sells it at a vendor (log each in the ledger). Never remove gear, enchanting mats for Wonky, or items a companion profession uses below the limit. Report free slots before and after. List the rank 4 rights per tab so the owner can decide on a new tab.
7. PROSPECTING TEST. Give Wren one stack of Copper Ore with .additem. She must prospect it and use the gems. Report.
8. MEMORY. Runs 5 and 7 grew about 0.25 GiB; runs 6 and 8 did not. Find what differs (smaps or the allocator stats per subsystem at start and end). Report only; fix if it is in companion code.

TEST: owner offline, two 60-minute runs, all features on, no teleports by you.
Pass: at least three companions turn in quests in each run; no companion dead for more than 5 minutes total; no death loop; Brannoc gains Blacksmithing; trip combat 5 min/h or less on all trip types; guild bank has free slots; mean update 15 ms or less.

REPORT each item with proof, the commit hashes, decisions you need, and the rollback command. Put the Step 1 cause at the top.
```

---

## Lead mode v2 - target persistence and healer tether

Status: running. Owner: Coder.

```text
TASK: Lead mode v2. Small change. Worldserver STOPPED for the build. Tag the image first (pre-leadv2). Commit locally. Each change has its own config switch.

OWNER FEEDBACK (quote, 2026-11-20):
- 'The hairy pulls were caused by low-health mobs fleeing into other groups and bringing adds. Brannoc wasn't necessarily pulling too aggressively. ... Do not adjust her current pull pace yet.'
- 'Wren and Aldric can stay close to Brannoc. They're melee DPS, while Kelda and I can remain farther back.'
- 'Brannoc should maintain reasonable healing distance and line of sight from Wonky before initiating new pulls. A soft healer-range tether for starting the next fight seems appropriate, without restricting necessary movement during combat.'
- 'Kelda's diamond-marked pet off-tanking worked correctly. The wolf enabled Growl and held the diamond target. Preserve that behavior.'
- 'Check whether Wren and Aldric are changing targets unnecessarily when Brannoc switches targets or marks a new pack. Prefer target persistence and engagement awareness rather than blindly following every change in the tank's selected target.'
- 'Preserve the behaviors that are already working while investigating the remaining targeting issue.'

DO NOT CHANGE: Brannoc's pull pace, chain pull, skull rule; Aldric's buffs (Kings on DPS so Battle Shout stays, Wisdom, Ret Aura), heals (40% rule, Gift of the Naaru, Lay on Hands); Wren's Garrote opener; Kelda's diamond pet tank with Growl. The regression test must show each of these still works.

FACTS (upstream base 7bae1b5c, src/Ai/Base/Value/DpsTargetValue.cpp; confirm in the installed build): DpsTargetValue::Calculate returns the RTI (skull) target FIRST, even if that mob is not in combat yet. With no skull it uses a 'smart' strategy over the group's attackers (time to kill, a bonus inside attack range + 5 yd). It does not look at the tank's target. Suspect: when the skull moves to the next pack before the current pack is dead, the DPS run to an unengaged mob. Prove or disprove it.

STEP 1, FIND (before code): from the logs of the owner's Stockades lead runs, or a new owner run with logging on, list each target change of Aldric and Wren with the reason: skull moved, tank target changed, target died, other. Also their distance to Brannoc. If there is no log, add the logging first.

STEP 2, TARGET PERSISTENCE (companion DPS in lead mode only):
 a. Keep the current target while it is alive, in combat with the group, and reachable. Do not switch only because the tank's target changed.
 b. Switch when: the target dies, the target is CC'd (do not break CC), the target is out of reach for more than 3 s, or a NEW skull is placed on a mob that is IN COMBAT with the group.
 c. A skull on a mob that is not in combat yet (the next pack) does not pull the DPS. They finish the current pack first; they join when Brannoc has the new pack (threat on Brannoc, or 2 s after he attacks it).
 d. With no current target: skull if engaged; else Brannoc's target if engaged; else the upstream choice.
 e. Melee (Aldric Ret, Wren) stay close to Brannoc's fight. Ranged (Kelda) and the owner stay back. In lead mode the melee companions follow Brannoc, not the owner. Kelda keeps her current follow.

STEP 3, SOFT HEALER TETHER (Brannoc, before a NEW pull only):
 a. Before Brannoc starts a new pull (not a mob that is already attacking the group), the healer (Wonky, the owner) must be within 36 yd of Brannoc and in line of sight (Unit::IsWithinLOSInMap). If not, Brannoc waits at most 8 s, then says once in party chat 'Waiting for heals.' and keeps waiting until the healer is in range and in sight, or the owner tells him to go (a 'pull' command or a skull on an engaged mob).
 b. No limit during combat: Brannoc moves freely to pick up adds, chain pull mobs already in combat, and position.
 c. The healer is the group member with the heal role; if none, the owner.

NOT IN THIS CARD (idea only, owner to decide later): stopping fleeing mobs (Hamstring, Hammer of Justice, Concussive Shot, Kidney Shot). The owner liked the extra challenge.

TEST: one owner dungeon run (Stockades or Deadmines) with logging on.
Pass: fewer than 3 unneeded target changes per melee DPS per run; no DPS runs to an unengaged skull; Brannoc never starts a new pull with the healer out of range or sight for more than 8 s without the message; all DO NOT CHANGE behaviours still seen; no new wipes. Report the log tables and the rollback command.
```

---

## Lead mode - kill order with raid marks

Status: running. Owner: Coder.

```text
TASK: Kill order v1 for companion lead mode. Small to medium change, behind its own config switch (Companion.Lead.KillOrder, default 0 until the owner tests). Worldserver STOPPED for the build. Tag the image first (pre-killorder). Commit locally.

OWNER IDEA (quote): 'Say the first mob in a pack that brannoc sunders gets a skull (first prio) ... the next mob ... gets an orange circle ... The dps attacks mobs in that order, like they should. This isnt needed at these low levels, but it might be needed for heroics at 80 or something.'

FACTS (upstream base 7bae1b5c; confirm in the installed build):
- TankTargetValue / FindTargetForTankStrategy: the tank picks the attacker with the lowest tank threat. So Brannoc's target changes often by design. Do NOT change this.
- DpsTargetValue::Calculate takes the RTI (skull) target first.

DESIGN:
1. Two separate decisions. Tank target (threat, unchanged) and group kill order (new). Store the kill order as a group-level list of creature GUIDs. The marks only show the list.
2. When: once per engaged pack, when Brannoc has initial threat on the pack (he has hit each mob once, or 2 s after the pull). Change the order only for a real reason: a mob dies, a new mob joins the fight, a mob starts to heal or cast, a mob flees.
3. Score each engaged mob (higher = earlier): heals others +50; casts spells +30; health below 30% +20 (finish it); fleeing toward other mobs +40; elite +10. Exclude the diamond (pet) target and any crowd-controlled mob. Ties: the mob Brannoc hit first.
4. Marks: skull = 1st, X (cross) = 2nd, circle = 3rd. Diamond stays the pet off-tank mark (Kelda's current code, do not change). Do not use moon or square. When a mark's mob dies, move each mark up one.
5. DPS (lead mode v2 persistence rules stay): with no current target, take the lowest mark that is engaged. Do not switch only because Brannoc's target changed. Switch to a new skull only when it is engaged.
6. Owner override: if the owner sets a mark by hand, it wins for that pack; the bot fills only the free marks.
7. Do not change Brannoc's pull pace, chain pull, threat rotation or the healer tether.
LATER (not v1): role jobs (Wren kicks a caster while Kelda shoots the skull), CC marks (moon, square).

TEST: one owner run (Stockades or Deadmines) with logging on. Log each order with the scores and each DPS target change. Pass: marks do not bounce (no mark moves more than 2 times per pack without a death or a new mob); DPS kill in mark order in at least 80% of packs; the diamond pet tank still works; no change in Brannoc's pull behaviour. Report and give the rollback command.
```

---

## Performance phase 2 - protect the companions, soak test, memory per bot

Status: running. Owner: Neon.

```text
TASK: Performance phase 2. You did phase 1 (54 Alliance bots, no swap). Now verify it, protect the companion bots, and write it down. Config and measurement only. No C++ build. Do not restart the server while the other agent (the companion coder on this PC) runs a build or a test: check for a running docker build and for a recent test log in ~/companion-tests/ first, and ask the owner if you are not sure.

SECURITY FIRST: never print a password in a report or a command. Use a client config file (mode 600) or MYSQL_PWD from a file.

PART 1 - CHANGE LOG (do this first)
Write ~/perf/CHANGES.md: every file you changed in phase 1, each key with the old and the new value, where the backup copy is, and one rollback command per file. Include docker-compose.override.yml, mysql-tuning.cnf, worldserver.conf, playerbots.conf, mod_ahbot.conf, mod_battleground_bots.conf, ~/bin/wow.sh, Config.wtf, the d3d9.dll swap. Reason: the companion coder's rollback commands copy older config files back (for example ~/companion-tests/playerbots.conf.pre-*), and that would silently undo your work. Add at the top: 'After any config rollback, re-apply these keys.' Also write a script ~/perf/check.sh that prints each key's live value and marks any that differ from your values.

PART 2 - PROTECT THE COMPANIONS
The always-on companion bots (code behind IsCompanion in mod-playerbots, branch companions) are the main project. Three of your changes touch them:
a. AiPlayerbot.SightDistance 100 to 65. Companion lead mode searches up to 120 yards for the next pack, and companion gathering uses nodes within 100 yards (40 in dungeons). Find in the source if the companion code reads SightDistance or its own keys (Companion.*). If the companions are affected, set SightDistance back to 100 and report the CPU cost, or report how to scope it to random bots only (no build in this job).
b. AiPlayerbot.ReactDelay 100 to 250. It slows the tank's and the others' reactions in dungeons. Same check: is there a per-bot or companion value? If not, set it to 150 as a compromise and report the CPU cost of 100, 150 and 250.
c. Logger levels: confirm you did NOT lower Logger.playerbots. The companion tests and the 'why' command need the Info lines.
d. cpuset 2-3: the owner builds the server image on this PC (clang, with the worldserver stopped). Confirm the build is not limited to 2 cores by your override, and report the build container's CPU set.
e. RandomBotMaps 0,1: confirm the companions can still travel to Azuremyst, Bloodmyst and the Exodar (map 530; Aldric is a Draenei with an Exodar reputation goal) and that the Dalaran portals and boats still work for them. This key should bind random bots only: prove it from the source.

PART 3 - SOAK AND OWNER TEST
1. 3-hour soak, owner offline: every 10 minutes log VmRSS and VmSwap (worldserver, mysqld), MemAvailable, zram use, load, and the 'Last 500 diffs' summary (mean, p95, max). Pass: no growth above 0.3 GiB in worldserver after the first 30 minutes, no disk swap, mean update 15 ms or less.
2. Owner plays 20 minutes with DXVK (he will tell you when). Log the same numbers plus Wow.exe CPU and the frame rate if you can read it. Report DXVK versus WineD3D CPU (he can switch with your rollback command). Confirm WINEFSYNC really works in this Wine build (the log line that proves it), or remove it.
3. Memory per bot: from your own before and after data, and from one more step (50 to 60 random bots for 20 minutes, then back to 50), give the measured MiB per extra random bot. Then estimate: how many more always-active guild bots (companion style, never throttled) fit with 1.5 GiB kept free.

PART 4 - REPORT ONLY (the owner decides)
a. Horde: you set 0 Horde bots and no Horde or neutral auctions. List what that breaks or removes: battleground and arena queues (who is the enemy team?), world PvP, the neutral auction house, Horde towns. Give the cost in memory and CPU of 40 Alliance + 10 Horde, and of bots that are created only when the owner queues a battleground.
b. Death Knight random bots: report how many exist. Do not change the key.
c. acore_world_old: dump it to the backup disk, verify the dump, and give the owner the drop command with no password in it.
d. zram: with swap use low, lz4 is optional. Report current zram use and your recommendation in one line. vm.swappiness=15 is fine: give the command.
e. RAM: DIMM1 has 8 GB DDR4-2400 (1.2 V, single rank), DIMM2 is empty, maximum 32 GB. Name the exact kind of stick to buy for dual channel, and what 16 GB changes in your bot estimate.
f. Phase 2 code ideas, ranked by MB or CPU saved per line changed, with a measured number where you can get one (for example the memory of the locale tables): no build now.

REPORT: ~/perf/REPORT-phase2.md, with a one-screen summary first: what you changed in this job, the pass or fail of each test, and the decisions you need from the owner.
```

---

## Logan's Coach v1.1 - voice lines and Heroic Strike counting

Status: ready. Owner: Coder. Addon files only.

```text
TASK: Logan's Coach v1.1. Addon files only. No server restart, no database change. Addon: Interface/AddOns/LoganReads in the game client folder (WoW 3.3.5a, interface 30300). Mark every part 'untested in game'. Run the Lua 5.1 syntax check on every changed file.

PART 1 - VOICE
1. Find how the LoganReads quest audio was made: search the LoganReads folder, its tools/ folder, the shell history, and the home folder for the text-to-speech tool and voice model used (for example a Piper .onnx voice file and its .json). Report the tool, the voice file path, and the command line, with proof (a file or a script that calls it).
2. If you find it: use tools/make_coach_audio.py (or fix it) to make one file per coach line with THAT voice, in the same audio format and folder layout as the quest audio: 'Press C! Charge!' and the same for each ability in the coach table, 'Press Tab!', 'Eat!', 'New power!', and the three praise lines. Level 2 lines with the name only ('Charge!').
3. If you do not find it: do not pick a new voice. Report what you searched, and stop Part 1. The owner chooses.
4. Check each file plays: correct length, not silent, the format the 3.3.5a client plays with PlaySoundFile.

PART 2 - HEROIC STRIKE COUNT
Heroic Strike is an on-next-swing ability, so the event the coach uses for a correct press may not fire as for other spells, or may fire at the swing. Prove from the 3.3.5a API and FrameXML which event reports it (for example UNIT_SPELLCAST_SUCCEEDED, or the combat log SPELL_DAMAGE or SPELL_MISSED with the player as source). Count the press when the player QUEUES it (the key press), if a 3.3.5a event or the action button state shows that; else at the swing. State the method and its limit.

PART 3 - ZIP
Update the laptop addon zip with the new LoganReads files.

REPORT: the voice tool and file found (or what was searched), the list of audio files made, the Heroic Strike method with proof, the syntax check result, and the exact test steps for the owner.
```

---

## Realm News - a nightly report by in-game mail

Status: ready. Owner: Coder.

```text
TASK: Build "Realm News" v1 on this PC. A nightly job that reports what happened in the game world and mails it to the owner's character.

HARD RULES
- Read-only on the game databases. No config changes. No server restart.
- The only write to the game is one in-game mail per day, sent with a supported console command.
- All files live in ~/realm-news/.
- Every news line must come from a recorded fact. Never invent an event, a name, or a number.
- Do not stop to ask questions. If a step cannot be done, skip it, and say why in the report.

STEP 1 - FACTS (plain script, no AI)
a. Each night, save a snapshot of every character: name, bot or real (bot = account name starts with RNDBOT), race, class, level, zone name, money, guild.
b. Compare with the snapshot of the day before and make a list of facts. Each fact has an id, a type, and its data. Types:
   - level-up: name, old level, new level.
   - realm first: the first character to reach level 10, 20, 30, 40, 50, 60 (keep a record so each is reported once).
   - richest gain: the character with the largest gold gain.
   - guild: a new guild, or a character that joined or left a guild.
   - market: for Copper Ore, Linen Cloth, Light Leather, Peacebloom, Silverleaf, Copper Bar, Wool Cloth, Tin Ore, Medium Leather, and Briarthorn: number of listings and median buyout today and yesterday.
   - owner: what the owner's own characters gained (levels, gold).
c. Check if this core records deaths, dungeon clears, or auction sales anywhere that a read-only query can reach. If yes, add those fact types. If no, say so in the report.
d. Write the facts to ~/realm-news/facts/DATE.json. Keep this format stable. A later job will read it.

STEP 2 - TEMPLATE NEWS (plain script, no AI)
a. Pick up to 8 facts, most interesting first: realm firsts, then large level jumps, then market moves above 15%, then guild news, then the richest gain.
b. Write one short line per fact from templates. Give each fact type at least 3 template variants so the text is not the same each day.
c. Save to ~/realm-news/news/DATE.txt. This version must always work with no AI.

STEP 3 - OPTIONAL AI REWRITE
a. Check if a non-interactive AI command line tool is installed on this PC. Prove it with one small test. If none works, skip step 3.
b. Run the rewrite only if both are true: system available RAM is 1.5 GiB or more, and no game client process is running.
c. Give the tool the facts file and this instruction: rewrite the lines in the voice of a town crier, at most 8 lines, at most 180 characters each, and end each line with its fact id in square brackets.
d. Check each returned line: it has a valid fact id, and the names and numbers in the line match that fact. If a line fails, use the template line for that fact.
e. Record the peak RAM use and the run time of the AI tool. If the tool does not exit within 5 minutes, stop it and use the template news.

STEP 4 - DELIVERY
a. Find the console command that sends in-game mail to a character. Prove its exact form from the command table in acore_world.
b. Send one mail to the owner's character with the subject "Realm News" and the date. Remove the fact ids from the text. If the mail body has a length limit, split it into two mails.
c. Never send two mails for the same date.

STEP 5 - SCHEDULE
a. Add a cron job at 04:30 each day (the database backup runs at 04:00).
b. Write a log line for each run to ~/realm-news/log.txt: date, number of facts, template or AI version, RAM numbers, mail sent or not.

STEP 6 - TEST
a. Run once in dry-run mode (no mail) and show the facts and the news text.
b. Run once for real and confirm the mail is in the character's mailbox (check the mail table).

FINAL REPORT
- What fact types work, and which could not be built and why.
- A sample of the facts file and the news text.
- AI rewrite: tool found or not, peak RAM, run time, and how many lines passed the check.
- The exact mail command used.
- The cron line.
- The result of both tests.
```

Note: an AI rewrite of mail text is model output inside the game. Check it against the repository's "Zero LLM Chat" invariant before you build step 3.

---

## PvP and twink bracket audit (read-only)

Status: ready. Owner: Coder.

```text
READ-ONLY AUDIT: battlegrounds and twink brackets. Change nothing. Mark each finding PROVEN (file and line, or command and output) or NOT FOUND.

1. Explain what mod-battleground-bots and the installer's battleground director patch do. List their config keys and live values.
2. Report the live values of RandomBotJoinBG, RandomBotAutoJoinBG, and every AutoJoinBG bracket and count key, plus the arena keys.
3. Show in the source how bots are chosen to fill a battleground queue when a real player queues. Must the bots already be in the same level bracket, or does the module change or create bots for the bracket? Quote file names and line numbers.
4. For Warsong Gulch at level 19 (bracket 10 to 19, 10 against 10): how many bots in that bracket are needed on each faction? How many random bots are in that bracket now, per faction?
5. XP lock: does this core support the XP-off flag for players (the Behsten and Slahtz NPCs)? Does it work for a bot? Does this core put XP-off players in a separate battleground queue? Quote file names and line numbers.
6. Fixed level: explain AiPlayerbot.RandomBotFixedLevel and DowngradeMaxLevelBot. Can a named set of bots be held at level 19 while the other bots level normally? If not, say what code is needed.
7. Gear: how do bots get gear before a battleground? Can a fixed item set with enchants be given to specific bots, and does it stay with EquipAndSpecPersistence = 1?
8. List the battleground strategy files for Warsong Gulch and Arathi Basin and what each handles (flag carry, flag return, defence, node capture).
9. Final summary in three lists: what works today with no change, what needs only config, and what needs new code, for this goal: "the owner queues Warsong Gulch at level 19 and plays 10 against 10 with and against twink-geared bots".
```
