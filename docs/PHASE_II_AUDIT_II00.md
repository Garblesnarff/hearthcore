# Phase II Installed-System Audit Report (Card II-00)

**Date:** 2026-11-25  
**Scope:** Read-only installed-system audit & Phase I (`#3`–`#9`) acceptance verification before enabling any Phase II cooperative gameplay features.  
**Policy:** No mass logins, no item/progress grants, no API spend, all Phase II live execution switches default **OFF / shadow**.

---

## 1. Exact Installed Versions & Hashes

| Component | Path / Container | Verified Commit / Tag | Status |
|---|---|---|---|
| **Hearthcore Mono-Repo** | `/home/wonky/hearthcore` (`master`) | `5ee45be` (PR #10 merged atop `e6a3d40`) | Clean, `19/19` unit tests passing |
| **AzerothCore `mod-playerbots`** | `modules/mod-playerbots` (`companions`) | `2bcb40bf` | Deployed in `ac-worldserver` |
| **Worldserver Docker Image** | `ac-worldserver` | `acore/ac-wotlk-worldserver:issues-346-2bcb40bf` | Up, `33s` boot, `Errors.log` = `0` B |
| **Azeroth Watch** | `/home/wonky/azeroth-watch` | Synced with `e6a3d40` | Active (`~25 MiB` RSS, auth enabled) |
| **Config Perf Guard** | `/home/wonky/perf/check.sh` | Phase 1+2 tuned | `0 diffs` |

---

## 2. Phase I Issue Acceptance Gate (`#3`–`#9`)

| Issue | Verification Method | Result |
|---|---|---|
| **#3** Native goal proposal binding | Live `companion_proposals` table + `CompanionBrain::ReloadProposals()` 60s cycle: order #3 rejected (`order_unavailable`), online crafters/gatherers transitioned to `accepted` (`native_weight_bound`); `test_issue3_*` pass | **PASS** |
| **#4** Durable event journal | Live `companion_events` (`446` rows, `82` milestones preserved) + SQLite `event_journal` & `event_cursors`; `test_issue4_*` replay/gap tests pass | **PASS** |
| **#5** Goal-specific outcomes | `classify_plan_outcome()` + `finalize_character_open_plans()` in `bridge.py`/`memory.py`; `test_issue5_*` (XP, capped-skill ledger ops, interrupted/expired, advisory, non-overlap) pass | **PASS** |
| **#6** Expiring weight overrides | `companion_weights` restored cleanly (`304` hearthcore, `4` quest card, `5` manual); `52` active TTL rows in `companion_weight_overrides`; `AiPlayerbot.Companion.Brain.OverridesEnabled = 1`; `test_issue6_*` pass | **PASS** |
| **#7** Shadow mode isolation | Live `brain-tick --shadow` & `test_issue7_*` confirmed zero mutation of live weights, proposals, intentions, reservations, `active_plans.json`, or `quartermaster.json` | **PASS** |
| **#8** Shared transactional budget | `BudgetLedger` (`api_ledger`, `BEGIN IMMEDIATE`) across planner + Quartermaster; `InvalidWithUsage` bills rejected responses; `test_issue8_*` concurrency/rollover/cap pass | **PASS** |
| **#9** Azeroth Watch admin auth | Live unauth `POST /api/brain/model` -> `401`; read-only `GET /api/state` -> `200`; `test_issue9_*` (401/403/413/200/cookie/audit) pass | **PASS** |

---

## 3. Exact C++ Native Hook Audit (Thread Context & Blocker Map)

| Hook | File & Line | Thread Context | Finding / Phase II Action |
|---|---|---|---|
| **Owner-absent auto-disband (Blocker)** | `CompanionBotMgr.cpp:1596-1601` | Map thread (`TickBot`, 10s cadence) | Calls `botAI->LeaveOrDisbandGroup()` whenever `group && !groupedWithOwner`. Must permit ONLY explicitly authorized `COMPANION_COOPERATIVE` groups when `AiPlayerbot.Companion.Coop.Enabled = 1` (keeping guard intact for all unrecognized groups). |
| **Autonomy strategy self-heal** | `CompanionBotMgr.cpp:1606-1622` | Map thread (`TickBot`) | Re-applies `+new rpg,+companion duty,+companion quest` if not in `COMPANION_FOLLOW`. Must NOT run in `COMPANION_COOPERATIVE` so solo duties never fight party follow/combat. |
| **Owner group precedence** | `CompanionBotMgr.cpp:1580-1593` | Map thread (`TickBot`) | `groupedWithOwner` runs BEFORE cooperative checks and calls `CompanionLease::RequestInterrupt(..., Interrupt::Group)` -> owner group ALWAYS wins cleanly. |
| **Lead / Assist / Chain strategies** | `CompanionBotMgr.cpp:1374-1399`, `CompanionLeadStrategy.cpp:113,148,177,615,666` | Map thread | Currently gated on `IsGroupedWithOwner(bot)` and `FindOwnerOnlineFor(bot)`. For authorized cooperative dungeon groups, allow tank `companion lead` + member `companion assist` using `FindGroupHealer(bot)` instead of requiring an online owner. |
| **CompanionMode enum** | `PlayerbotAI.h` | Any thread | Currently `COMPANION_FOLLOW`, `COMPANION_AUTONOMY`. Add `COMPANION_COOPERATIVE` for explicit cooperative party state. |
| **Activity lease** | `CompanionLease.{h,cpp}` | Map thread (mutex-guarded) | Already suspends on combat/death and ends cleanly (neutral, no cooldown) on `Interrupt::Group`. |
| **LFG bot-only initiation** | `LfgActions.cpp:85-120` | Map thread | `LfgJoinAction` is tied to `RandomPlayerbotMgr::LfgDungeons` and classic LFG level rules; bot-only LFG queue initiation is fragile. **Decision:** use standard native preformed groups (`Group::Create` + `Group::AddMember`) with legitimate travel to verified portal coordinates (`areatrigger_teleport`) — zero teleport cheats, zero core bypasses. |

---

## 4. Verified Installed Dungeon Catalog (`acore_world`)

Queried directly from `acore_world.instance_template` and `acore_world.areatrigger_teleport`:

| Dungeon | Map ID | Script | Portal AT ID | Outside Entrance (Map 0/1) | Inside Spawn | Level Band | Phase II Status |
|---|---|---|---|---|---|---|---|
| **The Stockade** | `34` | `instance_the_stockade` | `101` | Map 0: `-8764.8, 846.1, 87.5` (Stormwind) | `54.2, 0.3, -18.3` | 22–30 | **Pilot #1 (default OFF)** — zero travel risk, Stormwind-local, single wing |
| **The Deadmines** | `36` | `instance_deadmines` | `78` | Map 0: `-11208.3, 1672.5, 24.7` (Westfall) | `-16.4, -383.1, 61.8` | 17–24 | **Pilot #2 (default OFF)** — matches current Lv 10–14 crews as they hit 17+ |
| Shadowfang Keep | `33` | `instance_shadowfang_keep` | `145` | Map 0: `-232.8, 1568.3, 76.9` | `-229.1, 2109.2, 76.9` | 20–28 | Unsupported (disabled) |
| Blackfathom Deeps | `48` | `instance_blackfathom_deeps` | `257` | Map 1 (Ashenvale) | `-151.9, 107.0, -39.9` | 22–30 | Unsupported (disabled) |
| Gnomeregan | `90` | `instance_gnomeregan` | `324` | Map 0: `-5163.3, 927.6, 257.2` | `-329.1, -3.2, -152.9` | 26–35 | Unsupported (disabled) |
| Scarlet Monastery | `189` | `instance_scarlet_monastery` | `45/610/612/614` | Map 0 (Tirisfal) | Multi-wing | 28–42 | Unsupported (disabled) |
| Uldaman | `70` | `instance_uldaman` | `286` | Map 0 (Badlands) | `-226.8, 49.1, -46.0` | 36–46 | Unsupported (disabled) |

---

## 5. Measured Hardware & Workload Baseline (Acer Aspire TC-780)

Measured live on `2026-11-25 07:10` with **0 humans**, **13 online companions**, **49 online random bots** (**62 total bots**, `0` active dungeon instances):

- **Worldserver Tick Diff:** `mean = 11.7 ms`, `latest = 10 ms`, `p99 = 25 ms`, `max = 25 ms`
- **CPU:** Host `27.2%`, `ac-worldserver` `30.5%` (pinned to cores `2-3`)
- **Memory:** `7.17 GiB` total, `6.19 GiB` used, **`0.98 GiB` available**, `2.98 GiB` swap used (`ac-worldserver` RSS `2.55 GiB`, `ac-database` RSS `383 MiB`)
- **Implication for Resource Admission:** With `~1.0 GiB` free RAM headroom, Phase II MUST enforce `max_managed_groups = 1` initially and pause new admissions if `mem_avail_mib < 512` or `tick_p99_ms > 45`.

## 6. Current Roster Level Snapshot

- `core-wonky` (4 bots, protected from auto-recruitment by default): Lv 32–33 (`Aldric`, `Kelda`, `Wren`, `Brannoc`)
- `core-logan` (4 bots, protected from auto-recruitment by default): Lv 11–14 (`Ashwick`, `Flintlock`, `Tidecaller`, `Bramblepaw`)
- Guild shift crews (30 bots): Lv 3–11 (`Pooferz`, `Stormcaller`, `Vance` at 11; 7 companions at 10; 6 at 9) — naturally leveling into Deadmines (17+) and Stockade (22+) range.
