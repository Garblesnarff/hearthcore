# Phase II Native WotLK RDF/LFG Investigation & Execution Bridge Audit (Issue #20)

**Audited against:** Installed AzerothCore WotLK 3.3.5a (`/home/wonky/AzerothCore/azerothcore-wotlk/`) and `mod-playerbots`.

## 1. Conclusion: Native WotLK RDF/LFG Is Primary (No Physical-Travel Fallback Needed)

Core AzerothCore's Dungeon Finder (`src/server/game/DungeonFinding/LFGMgr.cpp`, `LFGQueue.cpp`, `LFGScripts.cpp`, `Handlers/LFGHandler.cpp`) **fully supports 5 individual bots queueing specific dungeons, popping a proposal, accepting, forming a 5-player LFG group, and teleporting in/out natively** via standard WotLK packets (`CMSG_LFG_JOIN`, `SMSG_LFG_PROPOSAL_UPDATE`, `CMSG_LFG_PROPOSAL_RESULT`, `CMSG_LFG_LEAVE`, `LFGGroupScript::OnRemoveMember` -> `TeleportToEntryPoint()`).

Core `LFGMgr` enforces all native rules automatically:
- `LFGMgr::JoinLfg` (`LFGMgr.cpp:598-886`) validates permissions, BG/arena exclusion, `LFG_SPELL_DUNGEON_DESERTER` (71041), `LFG_SPELL_DUNGEON_COOLDOWN` (71328), and `GetCompatibleDungeons` against `GetLockedDungeons(guid)`.
- `LFGQueue::CheckCompatibility` (`LFGQueue.cpp:247-476`) matches 5 individual queued GUIDs with 1 Tank + 1 Healer + 3 DPS sharing a common dungeon ID, creates a 40s proposal (`LFG_TIME_PROPOSAL`), and sends `SMSG_LFG_PROPOSAL_UPDATE` to all 5 players.
- `LFGMgr::UpdateProposal` -> `MakeNewGroup` (`LFGMgr.cpp:1672-2018`) creates a non-restricted LFG group (`ConvertToLFG(false)`), adds all 5 members with their roles, sets dungeon difficulty, and calls `TeleportPlayer(player, false, teleportLocation)` to port everyone into the instance natively.
- `LFGMgr::RemoveProposal` (`LFGMgr.cpp:2026-2125`) handles declines/40s timeouts natively, removing the declining player and re-adding remaining willing members to queue.
- `LFGGroupScript::OnAddMember` (`LFGScripts.cpp:175`) natively removes any solo `LFG_STATE_QUEUED` state when a player joins a group (so companions joining the owner's group never need a manual `CMSG_LFG_LEAVE`, avoiding any disruption to an owner's own group LFG queue).

Physical travel fallback is **NOT** implemented or used; native RDF/LFG is used end-to-end.

## 2. Exact Blockers Found in Stock `mod-playerbots` & How PR #20 Fixes Them

| # | Installed Location | Blocker | Fix |
|---|---|---|---|
| 1 | `modules/mod-playerbots/src/Script/Playerbots.cpp:562-578` (`PlayerbotsScript::OnPlayerbotCheckLFGQueue`) | Hard-rejects every 5-bot LFG match unless at least one entry is `guid.IsGroup()`, `IsRealPlayer(player)`, or `IsSelfBot(player)`. Core `LFGQueue::CheckCompatibility` (`LFGQueue.cpp:415`) returns `LFG_INCOMPATIBLES_HAS_IGNORES` whenever this hook returns `false`. Also allowed mixed combos with bridge-tracked coop bots + humans/random bots. | Mirror `Playerbots.cpp` and: (a) if ANY candidate is a bridge-tracked coop companion (`IsCoopLfgTrackedBot`), reject mixed human/random-bot combos and require exact 5-member `AllowBotOnlyLfgProposal`; (b) allow bot-only 5-GUID matches ONLY via `AllowBotOnlyLfgProposal` under `CoopEnabled() == true` & Issue #12 admission limits. Random bots remain blocked. |
| 2 | `modules/mod-playerbots/src/Ai/Base/Actions/LfgActions.cpp` (`LfgJoinAction`, `LfgLeaveAction`) | `LfgJoinAction` pulls random dungeons from `RandomPlayerbotMgr::LfgDungeons[team]` and calls `sLFGMgr->GetState()` (non-const `PlayersStore[guid]` insert!) from map threads. | Cooperative companions return `false` from map-thread `LfgJoinAction`/`LfgLeaveAction`; ALL cooperative LFG join, confirmed-queue observation, stale-queue requeue (90s), leave, and deferred-proposal re-evaluation run exclusively on the **world thread** inside `CompanionBrain::TickCoopLfgOnWorldThread()` (called every 2s from `CompanionBrain::Update()`, with zero `sLFGMgr` calls when coop is OFF and nothing is tracked). |
| 3 | `modules/mod-playerbots/src/Ai/Base/Actions/LfgActions.cpp` (`LfgAcceptAction`) | Does not check `SMSG_LFG_PROPOSAL_UPDATE` `uint8 state == LFG_PROPOSAL_INITIATING (0)`, immediately declines in combat, and calls blanket `ClearUnitState(UNIT_STATE_ALL_STATE)` + `botAI->Reset()`. | Check `state == LFG_PROPOSAL_INITIATING`; clear stored state & persist `ProposalFailed` on non-initiating updates; defer in combat / flight / vehicle / cast / fall up to 25s with an unconditional 35s hard cutoff (< 40s native timeout) + `LFG_STATE_PROPOSAL` verification on the world thread; remove blanket `ClearUnitState` / `Reset()` on cooperative accept. |
| 4 | `hearthcore/opportunities.py` + `hearthcore/bridge.py` ↔ `CompanionBrain.cpp` | SQLite coordinator and MySQL native reader had no live execution bridge or real native queue/proposal/run ID recording. | Added pure C++17 `CompanionLfgBridgeLogic.h`, world-thread MySQL native ID persistence in `CompanionBrain.cpp` (writing `native_queue_id` only after core confirms all 5 in `LFG_STATE_QUEUED`, writing `native_run_id` after binds commit, persisting `ProposalFailed`, and cancelling assembling opps on member decline/timeout/recall), MySQL 8.0-compatible idempotent migration `ops/sql/migrations/2026_11_27_issue20_native_lfg_columns.sql`, and transactional `correlation_key`-guarded `hearthcore/native_lfg_bridge.py` wired into `hearthcore/bridge.py::run_cycle()`. |

## 3. Architecture Contract (MySQL + Native LFGMgr Authority vs SQLite Read Projection)
- **Native WotLK `LFGMgr` / `LFGQueue` performs ALL actual queueing, matching, 40s proposal lifecycle, native group creation (`MakeNewGroup`), instance teleport (`TeleportPlayer`), and native return (`TeleportToEntryPoint`).**
- **MySQL (`companion_opportunities` + `companion_opportunity_interests`) is the authoritative opt-in, role-reservation, and admission contract** established across Issues #11, #12, and #22 so bots never spam native LFG with arbitrary un-enrolled or non-willing groups.
- **SQLite (`brain.db` / `OpportunityCoordinator`) is strictly a planning cache and read projection:** `bridge.run_cycle(apply=True)` invokes `sync_coop_native_lfg_bridge()`, which projects authoritative MySQL states & outcomes back into SQLite and publishes newly confirmed live opportunities inside a single `START TRANSACTION ... COMMIT` guarded by `correlation_key` (only when `coop_enabled()` is True).

## 4. Zero-Downtime Repo-Only Delivery & Staging Verification Checklist
Live `ac-worldserver` was **NOT** rebuilt or restarted during this PR per the zero-downtime constraint (`AiPlayerbot.Companion.Coop.Enabled = 0` and `HEARTHCORE_COOP_ENABLED = 0` remain OFF by default). During the next approved staging rebuild window:
1. Apply `ops/sql/migrations/2026_11_27_issue20_native_lfg_columns.sql` (MySQL 8.0+ idempotent `PREPARE/EXECUTE`).
2. Rebuild `mod-playerbots` with `Playerbots.cpp`, `LfgActions.cpp`, `CompanionBrain.{h,cpp}`, `CompanionBotMgr.cpp`, and `CompanionLfgBridgeLogic.h`.
3. Enable `AiPlayerbot.Companion.Coop.Enabled = 1`, `AiPlayerbot.Companion.Coop.EnabledDungeons = 34`, and `HEARTHCORE_COOP_ENABLED = 1` on **staging ONLY** and attach `[CompanionCoop]` logs + `SELECT * FROM companion_opportunities` for:
   - 5 independently enrolled bots -> native RDF queue (`LFG_STATE_QUEUED` x5) -> native proposal pop -> accept x5 -> native teleport into Stockade (`native_run_id` recorded) with zero humans.
   - 1 member decline, 1 native 40s timeout, and 1 owner recall releasing the assembling opportunity and leaving the other 4 bots cleanly unqueued.

## 5. Safety Defaults Preserved
- `AiPlayerbot.Companion.Coop.Enabled = 0` and `HEARTHCORE_COOP_ENABLED = 0` remain **OFF** by default.
- While OFF, `AllowBotOnlyLfgProposal()` returns `false`, `TickCoopLfgOnWorldThread()` returns immediately with zero `sLFGMgr` calls, `LfgJoinAction`/`LfgLeaveAction` send zero packets for companions, and `OnCompanionJoinedOwnerGroup()` never touches the owner's own group LFG queue.
