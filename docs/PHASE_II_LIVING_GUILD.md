# Phase II — The Living Guild
## Technical specification: cooperative players on a stock WotLK realm

**Status:** Proposed, documentation only. No feature is enabled by this PR.  
**Review baseline:** Hearthcore master `e6a3d403a6f1dcbb57467db248ed1a7ef0a673e6`. See [SERVER_STATE.md](SERVER_STATE.md), [PERSISTENT_MEMORY.md](PERSISTENT_MEMORY.md) and [PHASE_II_IMPLEMENTATION.md](PHASE_II_IMPLEMENTATION.md). The installed-system inventory is contributor-reported; this review inspected selected source, not the running server or full upstream module. Names, example IDs, thresholds and interfaces below are illustrative unless explicitly marked verified.

## 1. Product decisions and scope

Hearthguard is a community of simulated WoW players, not a centrally scheduled colony. The owner explicitly permits optional work to be declined, ambitions to be abandoned, inefficient leisure, events missed while absent, eventual retirement and alts/twinks. In exchange, designated raiders must be ready when needed. The owner retains authority over raid initiation, roster and progression unlocks. Respect the two owner companion groups and their existing invitation behavior. Horde persistent players are a later expansion conditioned on measured hardware headroom. Stay near stock 3.3.5a gameplay now: no new spells, items, encounters, quests, client patches or altered rewards to make Phase II work.

### Phase II delivery boundary

- **Core deliverable:** independent guildmates discover mutually useful dungeon opportunities, volunteer, form valid five-player parties, travel/enter legitimately, run stock dungeons, finish or recover, and return to individual plans. No human needs to be online for ordinary bot-only dungeons.
- Generalize the existing work-order pattern into a small typed opportunity contract, with deterministic admission and explicit commitments. Start with dungeon opportunities; keep current craft/order execution authoritative. Add quest assistance or crafting request adapters only after one vertical slice is proven.
- Introduce a readiness read model, persona/character identity seam, bounded playstyle traits, society observability, and repeatable combat/performance test harness now. Full raid tactics across every encounter, autonomous alternate creation, battleground economy, Horde society, guild governance, dynamic world content and custom client modifications are later projects.
- Maintain existing native autonomous questing, professions, scheduling, owner-controlled groups, guild supply and memory behavior. Do not replace Playerbots or create a second combat/pathfinding engine.
- Success is believable independent choices plus genuine cooperation and reliable owner availability, **not** maximum XP, perfect gear, maximizing dungeon counts or forced guild service.

## 2. Repository baseline and required Phase I gate

At review, issues #5, #7, #8 and #9 were marked closed; #3, #4 and #6 remained open, although source already includes typed proposal, durable journal and expiring override code. Closed status is not proof of deployed correctness. Do not assume Phase I is complete. Review and reproduce each issue's acceptance criteria on the exact installed build and DB migrations. Update stale architecture prose separately when verified.

| Existing capability / observation | Phase II reuse or required change |
|---|---|
| Python bridge generates eligible menus, native proposal records, weights and audit | Extend versioned goal types with opportunity IDs and revisions. Existing `dungeon:prep` is not an actual dungeon execution target. |
| C++ `CompanionBrain` loads baseline and expiring overrides | Keep bounded, temporary nudges; native admission is authoritative. No direct model SQL. |
| C++ `CompanionLease` grants one activity owner; combat/death suspend; owner/group interrupts end lease | Reuse lease arbitration; explicit cooperative commitment and group mode must prevent solo duty/quest movement from fighting party control. |
| **Confirmed blocker:** `CompanionBotMgr::TickBot` makes companions leave/disband any group without their assigned owner | Introduce explicit authorized cooperative group membership; never remove the guard globally. |
| Lead strategies currently gated on owner group in dungeon/raid | Audit and adapt approved bot-led dungeon party; preserve owner-led mode and original tactics. |
| Schedule and logout rules in BotMgr | Add bounded voluntary commitment windows and safe logout/withdrawal policy; never strand groups when a shift ends. |
| Work orders, bank requests, guild life and Quartermaster | Reuse existing claims, delivery and resource ownership; avoid duplicate order tables and rewards. |
| Memory journal/intentions/social relations | Consume actual correlated opportunity/attempt events; no invented experiences. Existing aggregate affinity is not proof of meaningful cooperation. |
| Dashboard collector + HTTP/SSE | Add batched read projections and evidence traces, existing authorization guard for controls. |
| 38 named profiles, 50 configured random bots, documented 10–15 ms mean update, 7.2 GiB RAM, four cores | Use as baseline context only. Measure actual concurrent active workload and tail latency on the installed Acer. |

Source evidence: [bridge.py:494](https://github.com/Garblesnarff/hearthcore/blob/e6a3d403a6f1dcbb57467db248ed1a7ef0a673e6/hearthcore/bridge.py#L494), [proposal mapping](https://github.com/Garblesnarff/hearthcore/blob/e6a3d403a6f1dcbb57467db248ed1a7ef0a673e6/hearthcore/bridge.py#L604-L628), [owner group policy](https://github.com/Garblesnarff/hearthcore/blob/e6a3d403a6f1dcbb57467db248ed1a7ef0a673e6/mod-playerbots-companions/src/Bot/CompanionBotMgr.cpp#L1552-L1620), [lead strategies](https://github.com/Garblesnarff/hearthcore/blob/e6a3d403a6f1dcbb57467db248ed1a7ef0a673e6/mod-playerbots-companions/src/Bot/CompanionBotMgr.cpp#L1380-L1399), [lease contract](https://github.com/Garblesnarff/hearthcore/blob/e6a3d403a6f1dcbb57467db248ed1a7ef0a673e6/mod-playerbots-companions/src/Bot/CompanionLease.h#L19-L76), [weight override reader](https://github.com/Garblesnarff/hearthcore/blob/e6a3d403a6f1dcbb57467db248ed1a7ef0a673e6/mod-playerbots-companions/src/Bot/CompanionBrain.cpp#L477-L550).

**Entry gates:** verified #3 goal lifecycle and native target execution; #4 event journal coverage/recovery; #5 actual outcome classification; #6 expiry/baseline recovery; #7 shadow isolation; #8 shared API budget; #9 authenticated admin control. Read-only audit, fixtures, and identity/schema design can proceed before gate completion. No live cooperative execution until gates are met. Capture exact deployed hashes, config, migrations and regression evidence; if the mirror differs, patch the real source first and synchronize it.

## 3. Architecture and responsibilities

    native facts + existing event journal + persona/schedule configuration
      -> Hearthcore read models (candidate eligibility, readiness, opportunities)
      -> per-persona/session planner (deterministic or optional Claude)
      -> bounded, typed proposal: express interest / open opportunity
      -> native coordinator: validate, reserve, commit, execute via existing systems
      -> actual group/lease/quest/loot events
      -> memory reducers + dashboard + updated opportunities

One shared lightweight service/process family, not one daemon or model instance per bot. Python may rank and suggest, C++ / native services own execution and game truth. MySQL holds authoritative active opportunities/admission and existing work orders; SQLite stores read-side projections, experiment traces and audit. No distributed transaction pretending to make MySQL/SQLite atomic: persist authoritative transitions first, idempotently reconcile Python from their events; never claim success before native acknowledgement. No per-tick network, model calls, SQL or repeated full roster scans. Cache small immutable dungeon catalog; batch snapshot reads; process only due candidates/events.

**Hard precedence:** game legality, safety/combat and actual human group/commands > active accepted obligations and readiness/owner availability policy > optional interests and memory > novelty. Accepted commitments are not the same as proposed intentions. A human summons is not a teleport privilege; route through native legal travel or existing explicitly authorized rescue policies only. Never create rewards, XP, gold, items, skill points, dungeon eligibility or raid unlocks by manipulating DB. Offline personas do not perform gameplay. Do not allow arbitrary model-generated chat or actions.

## 4. Identity seam: a player can own multiple characters

Keep existing character profiles and schedules working. Add an immutable opaque `persona_id` and mapping to character key `(realm_id, guid, incarnation)`, plus faction and ownership group. Use existing authoritative GUID/name mapping; no assumed mapping of null Git GUIDs. Every currently enrolled companion starts as a one-character persona. Do not create extra accounts/characters in Phase II.

Proposed logical schema:

- persona: id, faction, canonical profile reference/revision, schedule reference, state, owner-affinity group.
- persona_character: persona_id, realm_id, character_guid, incarnation, slot/main/alt/twink designation, enrolled/retired status. Unique active mapping per game character; no automatic transfer.
- persona_session: session_id, persona_id, active_character_key, start/end, active time, schedule reason, status. Enforce at most one active character per persona for future alt support, subject to explicit reviewed exceptions only. Existing always-online core remains online under current policy; virtual sessions do not require logout.
- commitment: subject persona and/or character, owner/faction/guild scope, kind, requirements revision, state, expiry/deadline, provenance. Raid readiness belongs to specific character/role, while availability to recall the main belongs to persona.
- Every opportunity, plan, attempt and event carries character identity; add persona/session references where relevant. Keep factual character accomplishments separate from transferable player knowledge. Do not copy gear, quests or character-only knowledge to another character. Private memories and opportunities have faction/guild visibility controls; do not leak cross-faction plans.

Migrate additively with idempotent backfill, consistency report, existing slugs as display aliases and compatibility adapter; no rekeying existing live facts destructively. If a GUID is reused or a persona mapping changes, quarantine and reconcile. Treat account creation, twink XP lock, alt selection, retirement and succession as Phase IV work; this seam prevents a later rewrite.

## 5. Opportunity protocol

An opportunity is an offer of optional cooperative gameplay, **not an assignment** or a guaranteed reward. It may expire with no participants. An eligible persona can express interest, decline, wait, withdraw or accept. All participants keep independent reasons, not a shared generated plan. Reuse `companion_work_orders` for existing crafting order lifecycles and expose a read adapter rather than copying their state.

### 5.1 Logical data contracts (proposed, versioned)

Opportunity:
    {
      "schema_version": 1,
      "opportunity_id": "opp-example",
      "revision": 3,
      "kind": "dungeon_run",
      "creator_character": {"realm": "realm-example", "guid": "101", "incarnation": "a1"},
      "visibility": {"faction": "alliance", "guild_id": "example"},
      "target": {"dungeon_id": 123, "difficulty": "normal"},
      "requirements": {"roles": {"tank": 1, "healer": 1, "damage": 3}},
      "created_at": "2026-11-24T18:00:00Z",
      "recruit_until": "2026-11-24T18:20:00Z",
      "estimated_minutes": 45,
      "state": "recruiting"
    }

Interest:
    { "opportunity_id": "opp-example", "opportunity_revision": 3,
      "persona_id": "persona-example", "character_guid": "102",
      "session_id": "session-example", "role": "healer",
      "intent_id": "proposal-example", "state": "interested",
      "reason_code": "wanted_loot_or_group_play",
      "available_until": "2026-11-24T19:15:00Z" }

Reason codes are bounded, validated labels, not evidence of human emotion or objective completion. The native system verifies role, target, level, faction, access, instance locks, actual talent/equipment role viability and schedule. Do not accept model-supplied eligibility as fact. Use actual supported catalog IDs, not invented dungeon ID 123. Game database constraints must include realm/incarnation in real keys; examples abbreviated. Store versioned payload per kind and reject unknown kinds. Times UTC; monotonic elapsed time in native run loop. All requests include idempotency key, expected revision, actor and expiry. Use integer database keys or bounded opaque IDs mapped to native numeric IDs; current planner validates strict candidate shape and IDs, so keep metadata in the trusted exporter/sidecar rather than smuggling JSON inside goal strings.

Candidate example:
    {"id":"dungeon_join:842","type":"dungeon","score":74,"description":"Join eligible normal dungeon, healer role, approx 45m"}
The target 842 represents an authoritative opportunity record, not arbitrary user text. New native proposal kind maps allowlisted candidate ID to typed opportunity ID/revision and accepted role. Maintain compatibility with current `core.validate()` and versioned proposal envelope. `dungeon:prep` remains advisory. Only admit proposals that match fresh snapshot, currently valid active character and visibility. Reject unsupported or stale targets.

### 5.2 State machines

Opportunity:
    proposed -> recruiting -> assembling -> active -> completed
                   |             |           |
                   +-> expired   +-> cancelled+-> failed / cancelled

Interest/commitment:
    offered/eligible -> interested -> reserved -> accepted -> active -> released
                         |               |          |          |
                         +-> withdrawn   +-> expired+-> declined+-> completed/interrupted/failed

Distinguish invitation, role reservation and actual group membership. Party members' individual objectives (wanted loot/quest/etc.) may remain unfulfilled despite a successful dungeon run. A run completion is not necessarily fulfillment of each participant's ambition. Log terminal reason and explicit unknowns.

Coordinator (native authoritative state or audited native-compatible admission service) must perform atomic conditional transition on expected revision. Unique active reservation per persona/character, role slot and opportunity as appropriate; all five reservations have short expiry. No distributed locks held while waiting for LLMs or players. Treat party assembly as a saga: reserve atomically, ask native group manager to invite, verify group membership; compensate/release if partial join fails. Retry boundedly with idempotency and backoff. Do not count a reservation as an in-game group. Reconcile after restart from actual group/instance state, expire orphan reservations, recover an existing valid party if safe, otherwise release without invented completion. No direct edits to core group tables.

### 5.3 Opportunity ranking and emergent behavior

Native candidate exporter enumerates only compatible, accessible, scoped opportunities. Use a bounded menu per persona (initial <= 8 group opportunities, <= 3 newly creatable target options; tune). Score based on actual personal need, time/travel cost, readiness priorities, role fit, schedule overlap, existing commitments, and stable preference applied exactly once. Familiarity bonus small (suggest <= 5% of discretionary score), opportunity-specific and evidence-backed. Existing fatigue/caution and temporary interest adjustments must not double count. Preserve ordinary solo/gather/fish/errand choices. A low score, no interest or all candidates blocked means continue native autonomy.

Do not optimize globally for clearing all queues or maximizing throughput. Deterministic coordinator finds feasible parties among **voluntary** interests; it does not assign uninterested characters. Prefer exact dungeon compatibility, sufficient time and role feasibility, then fair waiting age and modest social preference, with stable tie-breaking/test seed. Limit consecutive selections only when appropriate and do not punish repeat play motivated by genuine gear needs. Keep leader role separate from tank role; prefer a willing and qualified native leader. A creator may not have to be tank. An absent role may remain unfilled. Avoid permanently fixed parties, starvation and endless solicitation. Do not penalize a declined optional invite. Model is optional, not needed for matching. Do not call the API on every queue poll.

## 6. Autonomous dungeon vertical slice

### 6.1 Audit before enabling

Verify installed Playerbots/LFG branch: can companions start a queue and accept proposals with no real player? What group/master requirements exist in AI strategy selection? What native group APIs are valid on the world/map thread? Which dungeon tactics/entrances are supported? Determine whether actual group membership, ownership, invite acceptance, loot policy, LFG restrictions, teleport semantics, instance binds, strategy synchronization, logout and reset hooks work for bot-only groups. Inspect missing upstream dependencies; mirror is not full build tree. Record actual available functions, signatures, thread constraints and tests. Do not promise Dungeon Finder already works; prior testing found initiation limitations. If LFG is unavailable, use a normal native preformed party with legitimate travel/entrance; do not invent an unsupported teleport or silently bypass the core.

### 6.2 Dungeon catalog and eligibility

Build small versioned catalog from installed DBC/core data and reviewed encounter capability. Each entry: native map/dungeon/difficulty IDs, level range, faction/entrance accessibility, raid gate stage, role requirements, entry/attunement/lockout constraints, estimated duration/travel (learned from actual sessions), known route, supported encounter/mechanics flags, required client/data version. Native revalidates every condition at acceptance and entry. Keep catalog of safe initial stock five-player normal instances; choose pilot based on available bot levels and a verified path, e.g. Stockades or Deadmines if eligible. Do not assume all classic dungeons have working tactics, all characters meet level restrictions, or level 15 alone proves eligibility. Unsupported dungeons stay disabled. No autonomous raids or heroics in initial rollout. Use native WotLK and individual progression gates; do not bypass faction, level, lockout or gear requirements.

Gear/BiS interests use bracket- and role-specific candidate gear lists where actual source can be verified; must account for owned/equipped gear, alternative sources, class usability, loot mode, level and current raid stage. No simulated loot, fake drop rolls or item injection. An item opportunity is optional and can expire when obsolete. If gear metadata unavailable, offer broad dungeon opportunity without claiming a particular drop.

### 6.3 New explicit companion mode

The existing owner-only guard disbands owner-absent groups. Add a distinct cooperative mode (proposed name `COMPANION_COOPERATIVE`) or equivalent explicit native state; avoid simply allowing every arbitrary group. Validate group ID plus active commitment, enrolled participants, faction, membership, leader/controller GUID and target instance. Native owner-led group path remains unchanged and takes precedence. Existing random bot policies remain unchanged. Preserve both owner companion groups; initially exclude core groups from automated dungeon recruitment unless owner explicitly opts in. No group containing an unrecognized real player may be auto-controlled.

| Mode | Movement/activity ownership |
|---|---|
| Solo native autonomy | Existing duty/quest/travel lease |
| Interested / waiting | Continue solo work; no exclusive group lease |
| Assembling / traveling | Valid cooperative commitment; safe yield of solo lease; approved leader/travel controller |
| In dungeon | Native group/dungeon strategies and combat; no solo duty/quest self-heal competing with follow |
| Owner grouped | Existing owner-led follow/lead behavior, overrides optional commitment when safe |
| Recovery/exit | Explicit recovery policy, then release party and restore native autonomy |

Audit `SetMaster`, lead/follow strategy predicates, group-disband calls, idle/combat unstuck watchdogs, group-ready checks, schedule expiry, owner whispers/summon and death/resurrection. Avoid false strategy-drift repairs in cooperative mode. Only use native group mutation on the appropriate thread. Group leader loss triggers bounded reelection if supported or controlled exit; no invalid master pointer. A party is not a single bot's permanent lease: each member gets an accepted commitment bound to the same party/run ID while actual native lease controls movement. Preserve native suspend-on-combat/death and failure cooldown semantics. Exempt legitimate queue/ready pauses from idle rescue only with bounded watchdog and actual state; no teleport advantage. No automatic leader pull until party ready/in-range and not in recovery; reuse and test current mark/heal-range/lead tactics.

### 6.4 Session lifecycle

1. **Discover:** independently eligible persona chooses to open/join a dungeon opportunity for personal reason (gear, quest, practice, helping, optional leisure). Persist reason and candidate revision; no forced matching.
2. **Recruit:** roles advertise available windows; bots continue quests/crafts safely while waiting. Rate-limit invitations and expire interest if offline, unavailable, owner-requested, or goal no longer useful. Do not start a queue when no supported route/executor exists.
3. **Reserve/confirm:** deterministic coordinator finds 1 tank, 1 healer, 3 damage roles, supported levels/specs and sufficient overlapping time. All members individually confirm at safe boundary. Recheck actual roster and gear/talents; a class label alone isn't role capability. Reserve one persona/role. Release on refusal/timeout.
4. **Form and travel:** native group creation/invites, verify all members; use verified Dungeon Finder if supported, otherwise normal travel/entrance. Handle boats, flight paths, hearth, repair, ammo, supplies and travel estimates. No automatic teleport just because travel is slow. At entry verify instance and actual player presence.
5. **Run:** use native strategies. Respect ready checks, tank/healer range, kill-order marks, interrupts, CC, loot rules and combat/death recovery. No model-driven tactical commands. Capture real encounter events and individual goals. On wipe, bounded retry and reconsideration; no punishment for challenge. No farming resets that bypass stock locks.
6. **Finish/exit:** native completion conditions (not merely entering or killing arbitrary mob), report boss/instance result, eligible loot attempts, individual goal status and participants. Exit legitimately, restore solo leases and strategy. Decide whether to repeat through a new voluntary opportunity; no endless automatic reruns.
7. **Interrupt:** owner invitation/command overrides optional activity; if in active combat or unsafe to leave, acknowledge recall as pending and release at first safe native boundary, not force-disband mid-pull. If group has human participant, never auto-disband or replace without human approval. Record owner interruption, not failure. Replacement only if native entry/lock rules allow, actual participant willingly accepts, and safe checkpoint; otherwise controlled end.
8. **Shift end/restart:** reserve only if expected finish plus travel/buffer fits schedule. Allow explicitly bounded grace (initial 10 minutes, configurable) for a safe exit; never indefinite extension. Existing owner group always wins. On crash, reconcile actual membership, instance and locks; no artificial completion/loot. On disabling feature, stop new admissions and drain or safely exit existing parties.

Initial proposed defaults: one simultaneous managed bot-only dungeon party, max five participants, recruitment TTL 20m, confirmation TTL 2m, 10m start buffer and 10m bounded exit grace, max two wipe retries before fresh decision, cooldown on repeated technical failures. These are tuneable engineering defaults, not verified timings. No increase in total logged-in bot cap to fill groups. Raise concurrent groups one step at a time only after workload and playtest gates. Group party may never transition into an autonomous raid group.

## 7. Readiness contract: autonomy with obligations

Readiness is a separate commitment, not an activity preference or gear score. Owner chooses target raid/tier, role and designated core/bench roster; no bot initiates or unlocks raids. Build a read-only, versioned requirements catalog first; readiness drives ordinary eligible opportunities only after verified. Respect existing level 60 -> 70 -> 80 gate and owner raid authority. Specific attunements/requirements depend on installed WotLK progression modules; inspect actual config/requirements rather than importing a generic Vanilla checklist.

### 7.1 Character readiness

Return state per component: pass / fail / unknown / not applicable, with authoritative timestamp and evidence. Unknown cannot count as ready. Requirements depend on raid, role, progression stage and chosen difficulty:
- Actual level, access, attunements/quests if required, relevant instance binds/lockouts.
- Role feasibility from actual spec/talents, weapon skills, usable gear and specific role minimums. Distinguish "viable" from "BiS"; don't gate progression behind unavailable raid drops or endless perfect gear. Owner-reviewed flexible minimums and alternatives; no arbitrary aggregate gear score as sole criterion.
- Repair/durability, ammunition/reagents, consumables and inventory availability; bank supply is not personal inventory unless allocation is reserved and withdrawable. Reserve scarce resources without counting them for multiple raiders.
- Tested encounter capability/role mechanics for the installed build, not a fake skill percentage. Track unknown/unsupported tactics and explicit owner exceptions for exploratory pulls.
- Schedule/availability, current group commitment and recall travel time.

States: not_ready, ready_but_unavailable, ready_available, unknown. A main can be geared but currently committed elsewhere. Group roster solver must choose distinct personas/characters and satisfy owner-configured role/utility composition, availability, lockouts, and human slots; never count an alt/main twice or sum independent role counts to claim a valid raid. Report bottlenecks (e.g. only two eligible tanks) and feasible roster(s), not an absolute guarantee. The owner chooses final roster. Readiness policy must not override valid native rules, force login outside population budget, or create items.

### 7.2 On-demand raid availability

Maintain a configured ready reserve and explicit recall policy. Before known raid time, raise missing preparation candidates for designated raiders; otherwise ready personas can pursue any optional activity. On owner recall, acknowledge availability, stop accepting new optional commitments and finish or safely withdraw from current ones; return via normal travel. Report ETA ranges and actual shortfalls, don't claim instant readiness. No always-online requirement for all 38. Reserve seats within global population cap and prioritize owner groups; if not enough available, show incomplete roster and let owner decide. Deadline/readiness thresholds and recall grace must be agreed with owner during pilot; no hidden automatic coercion or forced mass logins. Model must not override readiness contract. Twink/alt recall and retirement succession are future, but schema supports them. An essential raider cannot retire without accepted replacement readiness or explicit owner release.

Quartermaster gets only aggregated, verified raid supply deficits and approved guild priorities; current profession deficits remain separate. Avoid conflicting bank requests, oversupply, duplicated inventory claims, and treating all guild members as mandatory workers. Guild activities remain optional unless an explicit commitment was accepted. Start readiness in observation mode and verify against actual character inventory and raid access.

## 8. Player playstyle and planning

Current profiles describe preferences and archetype/culture. Add optional versioned persona traits with neutral defaults (0.5 on 0–1 scale) and reviewed canonical source; no autonomous stable-profile edits. Keep existing JSON compatibility and migration validation. Start with only traits that have implemented effects; unsupported ones are descriptive/advisory, not claimed behavior.

| Trait | Proposed operational effect (discretionary decisions only) |
|---|---|
| persistence | number of reasonable reattempts within native retry limits, willingness to defer |
| preparation | discretionary supply/travel buffer; mandatory safety unchanged |
| sociability / independence | willingness to express interest or help; no forced group |
| curiosity | tie-break for new eligible destinations rather than repeated routine |
| frugality | spend vs save trade-off; actual affordability always enforced |
| variety seeking | response to repeated voluntary sessions, not obligation frequency |
| generosity | modest volunteer/crafting interest; cannot donate protected or reserved resources |
| risk tolerance | optional choice of supported challenge; no unsafe exploit or role sabotage |

Competition/completionism may be added after stock dungeon behavior is stable. Each trait changes explicit bounded scoring/selection policy with trace, not arbitrary C++ combat tuning. Do not use a trait to override mandatory repair, raid readiness, role feasibility or owner instructions. All thresholds proposed, tune via playtests. Keep identity stable and memory contextual; distinguish actual voluntary choice from plan, need, order, no alternatives or human instruction. Claude gets compact relevant facts/candidates and optional memories; no raw chat/credentials/private human identity, no all-world omniscience. Use existing shared API ledger and fallback; no call per invitation or encounter. Avoid copying model suggestions into permanent factual memory. Optional social activities may simply remain unchosen.

## 9. Memory, evidence and telemetry

Extend the Phase I journal contracts, not an independent feed. Event types:
- opportunity_created/revised/expired; interest_expressed/withdrawn; admission_reserved/accepted/rejected/released; actual group_formed/disbanded.
- travel_started/arrived; dungeon_entered/run_started; boss_attempt/kill/wipe/recovery; loot_opportunity/actual_loot; run_ended; participant_joined/left; owner_recall_requested/acknowledged/completed; readiness_component_changed.
- Correlation: authoritative event/source ID + epoch, character key, persona/session, opportunity ID, party/run/attempt/lease/plan IDs, role, actual participants, evidence and outcome reason. No raw human chat. Separate event producer coverage and observed vs inferred facts.
- Emit on meaningful transitions, not spellcasts or ticks; aggregate combat metrics per encounter. Count one shared outcome once per relevant participant; party membership/proximity alone not social success. Record actual active seconds and waiting time, reasons unknown when unobserved. Wipes do not infer dislike or blame. A no-drop after valid loot opportunity is not technical failure. Only actual completion of target closes personal intention.
- Existing native event window, Python journal and checkpoint/recovery must be verified (#4); do not depend on recent feed alone. Replay and dedup by durable IDs, protect against epoch/restore collisions, mark gaps and suppress affected influence. Preserve milestone evidence and bounded storage. Correlated facts feed memory projections and opportunity scoring; no parallel model-generated truth.

### Dashboard views (read-only unless authenticated admin)

1. Opportunity board: recruiting, role gaps, offers/voluntary interests, age, expiry, native acceptance, wait reasons, actual group ID; label synthetic/shadow data clearly.
2. Dungeon run detail: actual party, leader/roles, instance, travel/entry status, attempt timeline, encounter outcomes, loot provenance, personal goal result, recall/interruption, stuck reason.
3. Readiness: character/component evidence, viable roster and role shortfalls, readiness vs availability, supply allocations, unknown data, owner recall status; no fake readiness score.
4. Society: active minutes by actual activity (not proposed plans), voluntary vs required/unknown participation, queue wait p50/p95, role coverage, recurring groups, opportunity completion/expiry, order conflicts, invitation acceptance/decline, distribution/diversity of choices, actual gear/quest progress, blocked time, owner interruptions. Do not display invented enjoyment or human-likeness percentage.
5. Operational health: actual online and active characters, active instances, world update p50/p95/p99, CPU/RSS/PSS/swap, journal lag/gaps, queue depth, plan accept/reject, API spend/budget, expired reservations, feature switches. Use existing batch snapshot/SSE; no per-card DB query or new unbounded polling. Existing authorization requirements apply to controls; no new unsafe LAN endpoint.

## 10. Combat and performance laboratory

Build repeatable staging fixtures against exact installed core/module commits; mirror alone cannot compile a full realm. Isolated test DB/account namespace and ports, no production credentials/data, no real-world bot inventory injection. Test fixtures may seed controlled characters only in explicitly isolated staging. Run one scenario at a time on Acer during non-play windows; compile on other available hardware if compatible; no destructive live benchmarks. Use measured active workloads, not logged-in counts. Document warmup, sample interval, seed, map/instance mix, baseline bot count, build flags and machine load. Compare same scenario before/after with repeated trials, raw logs and median/tail ranges; avoid interpreting differences due to random encounter or level/gear as optimization.

| Fixture | Required checks |
|---|---|
| Solo unchanged | quest/gather/craft/train/repair, current owner invitation and strategy drift recovery |
| Five-person owner-led party | existing pace, marks, healer range, pet off-tank, owner commands, loot behavior |
| Bot-only pilot group | native invites, actual group ownership, legal travel/entry, no conflicting solo duties, one tank/healer/three DPS |
| Combat scenarios | threat/taunt, interrupts, AoE avoidance, CC, fleeing adds, resource use, healing/defensives, wipe/reset, replacement/leader loss |
| Schedule/recall | pending recall mid-combat, safe release, shift end, timeout, logout/restart, owner priority |
| Race conditions | two opportunities compete for one tank, stale interest, duplicate invite, roster change, lost worker, concurrent order claim |
| Progression | actual lockouts/attunements/loot/XP, completed vs failed target, gate remains unchanged |
| Mixed load | measured current outdoor bots + one dungeon group; later 2/3/5 groups only if gates pass; no artificial population increase |
| Failure isolation | planner/API stopped, expired weights, journal gap, DB restart, shadow isolation, authorization, feature off |

Initial targets (not measured guarantees): no world-thread network/per-tick SQL, no deadlocks or severe correctness regressions; p95/p99 <= baseline + max(2 ms, 5%) on comparable workload, no sustained extra swapping, no crash/OOM, bounded worker memory <= existing 128 MiB service cap and incremental work target <= 5% one core averaged over representative load. Review actual baseline before ratifying; instance simulation has intrinsic CPU/RAM cost, separately compare equal total workload vs additional workload. Existing perf-check script verifies config, not a benchmark. Add benchmark result artifact schema (scenario/build/config/sample size, active bots, concurrent instances, tick distribution, CPU, RSS/PSS, major faults, swap I/O, correctness assertions, output digest); no fake performance numbers. Optimize measured bottlenecks only; never silently reduce combat quality or skip legal world updates to improve metrics.

Resource admission: configure total active character/instance caps, minimum memory/latency headroom, pending queue caps, role/recruitment menu limits. Pause new party recruitment/admission and optional planning on sustained pressure, not active combat. Recover with hysteresis; owner gameplay and existing commitments take precedence. If no headroom, stay at one group or disable. Horde population, 40-person autonomous raid stress tests and 200-bot scenarios are future benchmarks, not promises for i5-7400.

## 11. Deployment, migration and rollback

- Additive versioned schema and native adapter; migration tested against sanitized snapshot with rollback/export and re-run safety. Do not delete existing profiles, weights, orders, guild relationships, game facts or live progress. Keep runtime DB out of Git. Credentials local, least privileges for Python; native authority validates every proposal. Use existing systemd timer/resource model and budget guard rather than new per-character services.
- Feature switches independently cover opportunity read model, shadow matchmaking, bot-only group mode, each dungeon catalog entry, memory influence, readiness influence, and model planning; default disabled/shadow. Allow per-persona enrollment and global maximum managed groups. Existing owner groups protected by default. Toggle only via authorized local controls or reviewed config; stop new admissions immediately, drain/exit existing safely, release reservations, clear temporary overrides at native boundary. Never rollback legitimate earned loot/XP to undo a feature.
- Test on exact installed version; sync C++ mirror and docs after deployment, verify installed migrations/service hashes. No automatic deploy from this design PR.
- Pilot: shadow using actual candidate snapshots; compare proposed matching vs valid feasible parties without changing live state. Then two or more supervised stock runs on staging. Then one live managed group, limited enrolled roster excluding core companions by default, during approved window. Observe multiple sessions and interruptions. Expand to more guildmates only after owner playtest and measured headroom.
- Seven-day optional observation experiment after pilot: allow normal human gameplay, no manual task assignment or granted resources; record owner requests as such. Establish pre-pilot baseline, report denominators and uncertainty, comparable level bands, real events and workload. Do not equate shadow choices or before/after correlation with causal effect. Report outcomes, wait/recovery, variety, actual readiness and hardware. Success can include declined/expired opportunities and unproductive leisure. Improve system based on specific observed failures, not maximizing all statistics.

## 12. Release acceptance and out of scope

Phase II is complete when at least one eligible voluntary bot-only party forms, enters and legitimately completes a supported stock dungeon without an online owner, and repeatable subsequent sessions demonstrate reliable wait/exit/recovery, actual target outcomes, no duplicate rewards/claims, no owner-group regression, safe recall and bounded resources. Require actual source/test evidence plus owner playtest, not dashboard prose. Readiness report must identify unknowns and feasible roster constraints; persona identity mapping must be verified without creating alts. The exact number of successful runs, resource tolerance and acceptable recall time are agreed at audit, not fabricated here. An unsupported encounter stays disabled until verified. No blanket assertion of all dungeons/raids working.

Later: full tier-specific raid tactics and raid preparation execution, alt creation/XP-lock/twink economy, Horde persistent faction, PvP, sophisticated market/social institution simulation, original content/world events, custom client, automatic stable personality evolution. No change to stock rules for Phase II. Design the seams now; implement only the minimum cooperative system and support it with evidence.
