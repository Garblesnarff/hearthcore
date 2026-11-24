# Persistent memory and experience — v2 design

Status: **proposed, documentation only**. No game changes, migrations, API requests or deployment in this PR. Supersedes the earlier conversational memory proposal. Owner: integration lead, with game-side changes reviewed separately. The current Hearthcore CLI is standalone; its profiles are provisional and may not match the installed personas. The actual dashboard producer, live database and companion implementation are not available to this review. All interfaces, thresholds and timings below are proposed defaults, not claims about installed behavior.

## 1. Goal and non-goals

Make persistent guild characters recognizably different human-like players over weeks of real play: they remember unfinished ambitions, learn practical lessons, cooperate with familiar guildmates, change their minds, sometimes waste time, and react to genuine outcomes. Neither maximum XP nor perfect raid efficiency is the objective. A challenging dungeon that requires the human healer's attention can be a success. Memory is not omniscience, sentiment simulation, automatic personality rewriting, or an authority on game rules.

Preserve the working native autonomy, existing activity leases, companion policies, owner invitation precedence, actual player control of raid composition, existing tactical behavior, expansion gates and legitimate rewards. All memory and model work is outside combat/world update. Scheduled bots and always-online companions use real or virtual sessions. No offline gameplay or awards. Start with the first companion group, then the second companion group and guild population; ordinary world bots remain uninstrumented or aggregated unless explicitly enrolled. Do not assume 100 logged-in bots are equivalent to 100 fully active bots.

## 2. What the original plan got wrong

1. Activity frequency, completion and loot do not establish enjoyment. People farm unwanted content for rewards, do chores because they must, or join a friend despite having no personal need.
2. Failure is not a negative preference signal. Wipes may be enjoyable; a bug, unlucky drop, owner interruption, bad route, unavailable tank and genuinely difficult quest require different responses.
3. A global dungeon penalty misgeneralizes one bad instance to all dungeons. Memory must be scoped to goal, method, encounter, place and participant where evidence permits.
4. A feed of positive milestones hides abandoned plans, queue waits, unsuccessful attempts and opportunity sets. Without those denominators learning is biased.
5. One event can affect many group members but must count once per relevant outcome. Co-location, joining a party and clearing a dungeon aren't three independent signals of friendship.
6. A repeated event, crash, stale snapshot or database restore can produce false experience or wrong-character attribution. A dashboard message is not an authoritative record.
7. Decaying a factual milestone or unfinished goal as if it were fatigue conflates distinct concepts. Time offline should not create fictitious play or imply forgetting practical knowledge.
8. Independent multiplicative preferences can double-count the same evidence through base utility, memory and Claude. Clamping alone doesn't solve this.
9. Multiple autonomous bots may all pursue the same scarce order, form cliques or overburden the scheduler. Cooperation needs advisory opportunities and reservations.
10. Model-written memories and prose can invent causal explanations, inject instructions or expose unsafe text. Facts and inferences require separate provenance, and no raw chat should be sent to a provider.
11. Replaying events only proves reducer consistency, not improved behavior or actual causal improvement. Counterfactual plans are proposals, not observed alternate histories.
12. Retention, recovery, resource caps, monitoring, ownership, schema versions and rollback must be operational contracts.

## 3. Data ownership and boundaries

| Data | Authority |
|---|---|
| Character identity, GUID, level, inventory, talents, quests, resources, current location, actual rewards | AzerothCore / character database |
| Stable persona and schedule | Existing installed persona/scheduler source, discovered before migration; reviewed configuration can be versioned in Git |
| Eligible candidate projects and their native base need/cost score | Game-side exporter, with documented scoring semantics |
| Active task, lease and group commitments | Existing C++ activity system |
| Event facts | Authoritative game event producer or explicitly labeled observations from an existing backend |
| Derived experience, memory projections, plan audit | Hearthcore runtime storage; rebuildable from evidence/checkpoints |
| Session intentions | Validated proposal from Hearthcore; accepted state recorded by native importer |
| Display text and charts | Dashboard, read-only consumer |

Never create two independent persona definitions. Map installed personas by realm/character GUID and incarnation, export sanitized stable profiles or reference one canonical source; unresolved mappings are ineligible for planning. Names/slugs are display aliases. Git stores schemas, rules, fixtures and reviewed configuration, not live character logs, private data, secrets or journals. Runtime state is local and access-controlled. This repository is public, so examples must be synthetic.

## 4. Architecture

    authoritative game hooks / existing feed producer
        -> bounded asynchronous collector / durable outbox where possible
        -> canonical journal (facts + observational provenance)
        -> single-writer reducer + checkpoint
        -> compact per-character memory projections
              |-> dashboard read API: evidence, score traces, health
              |-> session context builder + eligible candidate snapshot
                       -> deterministic ranker / optional Claude planner
                       -> validated, expiring proposal
                       -> trusted native importer at safe boundary
                       -> actual gameplay and outcome events

Reuse the existing feed backend if it can provide durable, structured, correctly scoped events. If it is a transient log tail or a SQL polling display, add an adapter at the event source; do not reverse-parse rendered text. Do not force a new service if the backend can safely host a single background worker. No per-tick SQL, API, synchronous network, global bot scans or event processing. Existing C++ remains functional without this subsystem. Dashboard reads must never be required for planning.

## 5. Event contract and reliability

Proposed versioned envelope (all examples synthetic):

    {
      "schema_version": 1,
      "source_id": "realm-1:game-hooks",
      "source_epoch": "restore-or-stream-generation",
      "source_event_id": "persistent-source-id",
      "occurred_at": "2026-11-24T18:00:00Z",
      "received_at": "2026-11-24T18:00:02Z",
      "kind": "dungeon_attempt_finished",
      "actor": {"realm_id": "realm-1", "guid": "1842", "incarnation": "a1"},
      "participant_ids": ["1943"],
      "session_id": "session-43",
      "attempt_id": "attempt-17",
      "goal_id": "gear:eligible-project",
      "map_id": 0,
      "instance_id": "instance-example",
      "provenance": "authoritative_hook",
      "payload": {"outcome": "completed", "boss_kills": 3}
    }

Validate fields and per-kind payloads, integer IDs, UTC timestamps, known entity mappings, bounded sizes (suggest 4 KiB per event), and schema versions. Use opaque source identities, not timestamps, names or hashes of displayed strings. In production scope participants by realm/incarnation; example participant IDs are shorthand. Use immutable IDs for sessions, attempts, leases, dungeon runs, orders and plans. Do not infer a boss kill, attribution, loot eligibility, deliberate action, or causation from a displayed feed line. Preserve unknown outcomes as unknown.

Sources must declare coverage, cursor/offset semantics, retention and delivery guarantees. Prefer an atomic outbox where a game persistence transaction already exists; not every in-memory action has a durable transaction. For observational sources explicitly mark partial coverage, and reconcile current state with snapshots without manufacturing missed historical events. Do not claim exactly-once delivery. Aim for at-least-once ingestion and idempotent effects. If a source cannot provide stable IDs or complete delivery, disable learning for affected event kinds until a suitable adapter exists.

Use a unique constraint on (source_id, source_epoch, source_event_id); compare payload digest on duplicate and quarantine conflicting duplicates. Normalize one fact with scoped participant links, rather than duplicating an entire encounter for each member. Persist source cursor and reducer updates in the same transaction as accepted batch/checkpoint, or use separate durable ingestion and projection checkpoints. Acknowledge upstream only after durable storage. Cursor semantics must come from source; SQL auto-increment gaps alone are not proof of data loss. Track late arrivals, watermarks and coverage intervals. Use deterministic order (event time plus stable source tie-breaker) and rebuild affected projections when late events affect results; never move decay clocks backward. Unknown version, malformed record and event identity conflict go to bounded quarantine with operator-visible metrics; don't silently skip learning-critical events.

On restore/reset, stop live import; explicitly reconcile realm epoch and character incarnation, cursor, checkpoint and pending proposals. Restore matching state backups together where possible. Never accidentally attach an old character's history to a reused GUID or interpret replay after rollback as newly earned progress. A gap/stale feed disables affected derived influences, not ordinary gameplay. A reconciliation snapshot can prove the current inventory, not the exact lost sequence of past actions.

## 6. Minimum useful event taxonomy

| Event family | Required context | Derived use |
|---|---|---|
| session start/end, login/logout | session ID, schedule, end reason, active elapsed time | habits, actual available time |
| goal considered/selected/started/blocked/ended | candidate ID, category, need, alternatives/scores at decision boundary, choice reason (voluntary/need/order/owner/safety/unknown), outcome, lease/plan ID | distinguish preference, commitment and opportunity |
| quest turn-in, level, skill rank, discovery | game ID and valid attribution | progress, milestone, knowledge |
| dungeon queue entered/exited, proposal, run ended | intended project, role, wait duration, result, run ID, participants | availability and time cost |
| boss kill, eligible loot roll, loot received | encounter/run ID, eligibility and item ID where known | real attempts and gear outcomes |
| death, wipe, recovery | encounter/attempt ID, location, reason if actually observed; otherwise unknown | scoped risk or operational issue |
| craft/gather/bank/order fulfillment | order/item and amount where verified; participant roles | resource contribution and dependencies |
| group invitation, accepted/declined, completed shared activity | group/run ID, invitation origin, role and commitment | cooperation evidence, never infer emotion |
| travel/lease failure, timeout, repair/vendor/train failures | machine-readable reason, retry count, goal ID | reliability and blocked-method cooldown |

The current dashboard already displays some useful event types according to the owner; coverage of the rest is unverified. Add missing hooks only when necessary. Do not log all mobs, spell casts, coordinates or action ticks: accumulate bounded counts/time spent by activity and emit on session/attempt boundary or coarse interval. Capture both success and failure. A gear farming attempt counts only if the relevant actual loot opportunity occurred and the bot was eligible; no-drop is not a failure of competence. Missing denominator => no success-rate inference. Do not count owner's commands or mandatory duties as evidence of voluntary preference.

## 7. Memory model: distinct kinds, distinct update rules

| Kind | Example | Lifetime and use |
|---|---|---|
| factual milestone | first mount, raid boss cleared | durable, never decay the fact; no automatic score boost |
| open intention | still needs weapon, wants to finish profession project | persists until completed, obsolete, deferred or explicitly abandoned; periodically revalidate |
| practical knowledge | route works, instance access, usable gathering area | confidence and applicability depend on patch, gear/level band, map, context and evidence; invalidate on relevant change |
| recent workload/fatigue | three lengthy runs this session | mostly active-play-time/repetition based, recovers with rest/wall-clock time; not punishment |
| situated difficulty | same quest route repeatedly failed | short contextual penalty to method; alternate strategy, help, or gear prep may get modest boost |
| reward/attempt history | relevant boss killed 4 times, desired item absent | retained counts, diminishing urgency only if appropriate to persona and alternatives |
| social familiarity | repeated successful shared runs, fulfilled request | directional, bounded, slow-decay; no inferred friendship/enmity as fact |
| temporary interest | decided to fish more this week | explicit planner intention or evidence of repeated discretionary choices; expires |
| stable persona | enjoys groups, perseveres, plans ahead | human-reviewed source; **no autonomous edits in v1/v2 rollout** |

Represent each episode as a deterministic reducer result: character key, memory key/kind, scoped subject IDs, event count, opportunity count if known, evidence event IDs or compact immutable evidence references, reducer/rule version, first/last seen, coverage, fact confidence, inference confidence, optional influence and expiry. A fact may be certain while inferred cause is unknown. Never treat an LLM's explanation as a fact or feed a prior generated explanation back as fresh evidence. Store model interpretations separately as auditable proposals.

Only summarize facts when the data supports them. If the engine cannot tell why a bot abandoned a quest, retain 'abandoned, reason unknown'; do not label boredom. If a bot repeatedly dies because of a code error, classify it operationally and alert the owner, not as personality evolution. Use gear/level changes, travel fixes, patch version and new group composition to reconsider old negative knowledge. Don't extrapolate one failure to a whole activity category. Combat pacing/raid tactics are outside this memory MVP.

### Reducer rules (initial defaults, subject to tuning)

- Complete a goal when authoritative current state satisfies it, not when text claims completion. A gear candidate disappears when obsolete or owned; no-drop keeps it open without forcing the bot to repeat forever.
- Two or more verified failures of the same method within a proposed 48-hour window can create a scoped caution signal. Three deaths in one encounter are one encounter outcome for preference purposes. A new viable approach or relevant level/gear change resets/reduces the caution signal. Technical errors use native retries and operational alerts; don't apply personality penalties.
- Track optional repetitions and actual active minutes; a third lengthy discretionary run in a session may add fatigue. An urgent new gear need may still win. Rest recovers fatigue; time spent offline doesn't generate play experience.
- Repeated voluntary selection when genuine alternatives existed may be surfaced as weak evidence of preference. No stable persona rewrite, no treating rewards or attendance as enjoyment.
- Social familiarity increments once per unique, meaningful shared outcome, with diminishing returns. Group membership, co-location and simple repeated invites alone earn no bonus. A wipe need not hurt familiarity; never infer blame without observed evidence. Different characters can value the same experience differently.
- Missing data, low confidence, stale source, invalid context or expired memory => neutral influence. Major milestones are facts and remain available, but have no automatic perpetual weight.
- Retain an open goal through interruption; defer/resume only after native eligibility check, keep distinct from actual active lease. Native owner/safety interruption wins.

For time-based signals, decay signed strength toward zero using half-life; for fatigue use accumulated optional activity and recovery, not just timestamps. Compute decay lazily at context generation, not per tick. Never decay authoritative facts. Clamp aggregate effects by category and final total. Initial half-life suggestions: fatigue hours to one day, caution 1–2 days, scoped loot-frustration 3–5 days if personality supports it, familiarity 14–21 days, practical knowledge conditional on context. Use deterministic integer/fixed-point representation or documented rounding where replay equality is needed.

## 8. Scoring and planning: do not double count

First audit the current C++ score semantics. The existing Hearthcore fallback multiplies candidate score by persona preference; the exported native score may already contain preference weights, travel cost or failure cooldowns. **Do not apply them twice.** Version the score contract, export components and mark already-applied factors. Separate mandatory duties, group commitments, owner requests, legal eligibility and native safety checks from discretionary project scoring. Do not lower safety/repair/required training or change tank combat decisions through memory.

For a nonnegative unweighted discretionary candidate score B only, initial proposal:

    stable P in [0.5, 1.5] applied once
    memory M = clamp(1 + sum(deduplicated scoped deltas), 0.90, 1.10)
    session I = clamp(1 + independent intent delta, 0.90, 1.10)
    effective W = clamp(P * M * I, 0.5, 1.5)
    proposed score = B * W

Keep original score and factors in a trace. If native score includes signed travel costs, penalties, or the persona multiplier, preserve native semantics and add only one incremental adjustment at an approved stage; never multiply a negative cost as though it were a positive reward. If base score semantics are unknown, stay shadow-only. Score changes do not make an ineligible action eligible. Use deterministic stable tie breaking in test mode and any existing bounded seeded variety in gameplay. Avoid amplifying a memory both in a numerical modifier and in an LLM category boost. The initial live policy should use either deterministic memory adjustments OR a model-chosen goal bias for the same evidence, not stacked bonuses. Score a specific dungeon/quest/order separately rather than penalizing all dungeons or all questing. Social preference is a small opportunity-specific tie-breaker (initial max +0.05); never an exclusion.

Claude reads a compact packet: identity/persona revision, available time, fresh authoritative current state, eligible candidate IDs with costs and confidence, prior open intentions, relevant scoped memories and a bounded recent history (suggest max 12 episodes + 6 social relationships; target <= 3,000 input tokens including candidates). Keep candidates and evidence IDs structured; avoid arbitrary raw logs, untrusted text or chat. Escape/strip names/descriptions if needed, treat them as data not instructions. No API key, account names, human chat, credentials, real-world identities or child information. A model has no tools or game access. Select from allowed candidates only, structured response validated locally; use deterministic fallback on invalid response, timeout, rate limit, budget exhaustion or stale data.

Claude must not generate fact records or durable memories. It proposes primary goal, up to three alternatives, short reason code and optionally evidence IDs already provided. Do not request autonomous persona updates or arbitrary game commands. Keep existing CLI compatibility by introducing a versioned plan envelope rather than changing existing required fields silently. New envelope: character key/incarnation, plan ID, schema version, snapshot revision/hash, persona revision, memory watermark/rule version, issued/expires timestamps, proposal fields, source and evidence refs. Define actual reason-code vocabulary. Mark model suggestions as suggestions. Never send raw model prose to game chat.

At an actual/virtual session boundary, or genuine milestone/block, queue a plan request (suggest 20-minute minimum replan interval, max 2–4 requests per character per day initially, except native no-model fallbacks). One in-flight request per character, bounded global concurrency (start 1–2). Plan may complete after its input is stale: reject expired/replaced plans, revalidate current eligibility and version, and respect a current lease or owner/group commitment. Importer must be idempotent (plan ID), compare-and-set per character with an acceptance/rejection record, and apply only approved goals/temporary factors. No instantaneous movement, bank changes, items or XP. If the brain is unavailable, use existing native behavior without waiting. API spending: configurable monthly hard ceiling below available credits, conservative per-call token limit, reserve against remaining budget before requests, log usage including failures and thinking, no retry storms. All calls opt-in; model ID configured explicitly after provider verification.

## 9. Social and guild-scale safeguards

Character-private knowledge is scoped to their own experience or legitimately shared guild information; don't give every bot omniscient access to all event history, locations, loot or other private ambitions. Expose only authorized guild orders/queue opportunities. Keep relationship edges sparse and directed; cap retained neighbors (e.g. 16 active, significant older links archived), exclude human identity outside game identifiers, no emotional labels as facts. Record helpful acts based on actual trade/order/participation; don't penalize legitimate loot rolls or infer blame. Familiarity never overrides owner decisions, valid party composition, dungeon eligibility, current commitments or raid readiness.

Concurrent planners may select the same item/order or incompatible group. A lightweight coordinator exposes available opportunities and expiring resource reservations; the actual guild-bank/queue/lease system arbitrates. Proposals don't reserve resources until accepted through the existing game mechanism. Recheck quantities and availability, handle losing reservation races by choosing another valid goal. Ensure no double counting of contribution or loot. Do not force a fixed group or optimize guild members into identical schedules. Fairness and opportunity availability are observable, not automatic social mandates.

## 10. Persistence, retention and privacy

Reuse an existing durable authoritative event journal if one exists; otherwise use local runtime storage. Prefer separate SQLite WAL database owned by one memory writer for a small pilot, or existing backend database if already suitable. Avoid duplicate raw event copies; retain references to shared journal when feasible. Use short transactions, indexes for character/time, event identity, attempt and goal IDs; bounded batches, paginated reads and read-only connections; avoid long-held reader transactions that prevent WAL checkpointing. Keep backups via supported consistent backup mechanisms and check restore/replay behavior. Do not store API keys or live data in Git.

Suggested logical entities (not a migration against an uninspected schema):

- source_checkpoints: source/epoch, offset, watermark, coverage/gap status.
- event_journal: unique source identity, type, actor, timestamps, attempt/group/session correlation, payload, provenance, digest.
- event_participants: event ID + character key, role, observed participation.
- experience_projection: character + scoped kind/subject, counts/exposure, last update, signal, coverage, rule version.
- memory_episode: character + stable reducer key, factual/inferred confidence, evidence refs, applicability, expiry, rule version.
- goal_history: plan/goal/lease/session IDs, lifecycle, choice source, actual outcome, reason and elapsed active time.
- relationship_projection: directional character pairs, meaningful unique outcomes, recency and bounded influence.
- planner_audit: input version/hash, evidence refs, candidate traces, proposed/accepted decision, source, cost, outcome.
- projection_checkpoint: reducer/schema versions, journal offsets, state digest, checkpoint manifest.

Use synthetic fixtures for testing. Retain low-value detailed events 30 days initially, subject to actual disk measurements, and compact older history into versioned counters/checkpoints. Keep significant milestone evidence and a minimal immutable fact stub or archived event excerpt before pruning. Do not claim replay from genesis after pruning; rebuild from an explicit consistent checkpoint plus retained suffix. Preserve dedup high-water marks/epoch semantics across compaction so an old replay cannot increment aggregates. If source cannot guarantee old duplicate exclusion, retain tombstones or adjust retention. Keep evidence for live influential memories, and label historical summaries as compacted. Operator can reset/rebuild derived memories without editing game facts. Separate retention of game facts from provider request/audit text; don't record raw prompts by default. No raw chat, account credentials or personal identifiers, no generated chat in child-visible views. All dashboard chat/event lines should remain approved, event-grounded templates.

## 11. Resource envelope and observability

Treat these as proposed acceptance targets, not measured Acer capabilities. Baseline against current production bot workload before enabling. Start with four characters; stress with representative synthetic event replay and recorded real event mix, then the enrolled guild. Measure active bots/instances, event rate, memory RSS/PSS, disk writes, p95/p99 world update, swap activity and backend latency at comparable load. Test during non-play windows, no disruptive stress test on live family server.

- No synchronous I/O or API calls on world thread; no per-tick SQL. Bounded nonblocking event handoff, initial queue cap 2,000 records, drain batches up to 100 every 1–5 seconds (tune). Durable authoritative sources can replay; never silently discard learning-critical events. If buffer overflows, drop/coalesce low-value telemetry first and mark an explicit coverage gap for affected source; recover/reconcile asynchronously. Don't stall gameplay to guarantee memory.
- Initial external worker target <= 64 MiB incremental RSS idle, <= 128 MiB during bounded batches, <= 5% of one CPU core averaged over representative load. Measure rather than assume; deploy elsewhere if budget fails. Avoid retaining all snapshots in memory. Limit concurrent planner requests. On pressure pause model planning/compaction first, then noncritical projection work; use native autonomy. No growing queues without limit.
- Initial journal planning envelope: at 10,000 significant events/day x 1 KiB average payload x 30 days ~300 MB raw payload before indexes, WAL and overhead. This is a scenario, not measured traffic. Set a configurable total storage quota with warnings, bounded retention/checkpointing, and fail-open memory disable if disk is critically low. Tune from observed rates.
- Provisional live benchmark: no meaningful game-update regression; agree on numeric tolerance after baseline, suggested p95/p99 <= baseline + max(2 ms, 5%) with repeated comparable runs. No increased sustained swapping, no crash or blocking gameplay. Don't use mean update alone. Fail safe on resource thresholds and show status.

Dashboard additions: memory freshness/coverage, cursor lag, dedup/quarantine counts, event backlog, API cost/latency/reject reasons, active vs projected memory, per-character intention vs reality, candidate score trace including base components and exact applied factors, memory details with evidence, expiry and confidence, relationship edges, blocked/abandoned reasons, repeated failure and stuck indicators. Distinguish displayed task from actual lease. Do not rank personalities by XP or show invented human-likeness scores. Add per-character and global switches: observe-only, shadow, deterministic-memory live, planner live. Separate model-disable switch. All default OFF/shadow; toggle must prevent new imports immediately and clear temporary modifiers at a safe boundary without cancelling combat or rewriting native profiles.

## 12. Delivery plan and acceptance criteria

### Phase 0 — audit the actual integration (blocker for production)

- Locate dashboard event producer, schema, persistence, retention, IDs, provenance, coverage and event rate.
- Locate canonical persona, roster including both companion groups and schedule, GUID/realm/incarnation mapping; reconcile with Git profiles without overwriting.
- Trace native candidate score/lease semantics and safe weight apply/remove hooks; current Hearthcore CLI does not provide a live bridge.
- Identify existing native hooks for goal/attempt/session, group, loot, bank, quest and death outcomes. Document gaps and safe read-only adapter.
- Agree on resource benchmark, source coverage, permissions, owner authority and policy for enrollment.
- Deliver interface map and fixture sample stripped of private data. No assumptions about existing backend.

### Phase 1 — event contract and durable ingestion (no behavioral change)

Implement schema validation, synthetic fixtures, adapter, source cursor, journal, replay, quarantine, gap metrics and identity mapping. Test duplicates, conflicts, out-of-order events, reset/restore, crash before/after commit, truncated data, unknown versions, partial polling coverage and deleted/recreated characters. Show evidence in dashboard. No API calls or game writes.

### Phase 2 — deterministic projection (no behavioral change)

Implement open intentions, attempts, scoped difficulty, fatigue, familiarity, knowledge and milestones as separate reducers. Version rules and checkpoints; tests for expiry, rest recovery, missing denominator, objective completion/obsolescence, technical failure and participant dedup. Rebuild from checkpoint + suffix; same inputs/rule version/evaluation time produce identical output. Run bounded memory and retention benchmarks. Demonstrate a memory evidence drilldown without trusting narrative generation.

### Phase 3 — shadow integration

Extend snapshot/plan envelope; read actual persona and eligible candidate scores, generate control and experimental traces at same decision boundary. Control = unchanged native baseline or equivalent documented fallback; experiment = same candidates plus scoped memory. Model optional and separate. Log differences, not simulated outcomes. No applying plans. Inspect several sessions across companions, tune only from evidence. Audit double-counting and guard precedence.

### Phase 4 — deterministic live pilot

Enable only explicitly selected companions, small bounded modifiers at safe boundaries, no model required. Run paired/crossover comparison with fixed persona and comparable workload/level band; avoid treating shadow score divergence as causal success. Log acceptance, actual goals and outcome; measure repeated failed attempts, idle/stuck time, activity diversity, excessive goal switching, optional vs compulsory activities, human playtest experience and performance. Pilot toggles off on bad provenance, stale data, excessive memory/cost or gameplay regressions. Confirm existing pulling pace, marks, pet off-tank, healer range, invitations and actual rewards unaffected. Owner can disable immediately; no destructive rollback of legitimate game progress.

### Phase 5 — Claude session planner + guild expansion

After deterministic pilot works, integrate optional per-character LLM context, budgets, validated expiring proposal contract, idempotent importer, reason/evidence refs and asynchronous scheduling. Test timeouts, malicious/instruction-like data, invalid IDs/JSON, hallucinated memories, stale/racing plans, concurrent reservations, caps and fallbacks. Pilot second companion group then scheduled guild members; do not enable ordinary random bots by default. Confirm no raw generated chat or private data leaves system. Consider group opportunity matching and shared institutional memory only after individual memory is proven.

### Release gates / adversarial cases

| Scenario | Required behavior |
|---|---|
| Same event delivered twice or once per party member | one fact, one scoped effect per eligible participant |
| Same dungeon failed repeatedly because of a bug | operational issue, native retry policy; no generalized dislike |
| 12 necessary runs for desired item | no evidence of liking dungeons merely from frequency |
| Voluntary optional run after gear goal satisfied | weak evidence of interest, bounded and inspectable |
| Goal achieved, gear replaced or quest no longer possible | remove/close eligible project; preserve history |
| Dungeon fails yet player reports fun challenge | no automatic global penalty or personality rewrite |
| Owner invites busy companion | existing owner priority, log interrupted intent, don't infer failure |
| No events during downtime | no invented play, fatigue recovery only as appropriate |
| Feed lost between polls | coverage gap, neutral affected signals; no fabricated recovery events |
| Restart, restore, duplicate or late event | safe reconciliation and deterministic projection; old plan rejected |
| All bots want same order | native reservation/admission recheck; losers choose alternatives |
| Familiarity favors same tank every time | bounded tie-break, eligible newcomers remain viable |
| Memory or API worker stopped | server runs unchanged; no blocking |
| Memory conflicts with repair/safety/raid commitment | native rules win |
| Invalid or stale model plan | reject, record reason; native autonomy |
| Human chat contains arbitrary prompt-like text | not included in planner context or treated as instruction |

Success means noticeable adaptation **without convergence on one optimized personality**: scoped failures change methods, genuine cooperative opportunities influence choice, unfinished projects resume or are explicitly dropped, and different personas remain recognizable. No permanent stable weight changes in initial release. Use actual observations and owner playtests; do not require every bot to maximize goal completion. Report limitations and test evidence.

## 13. Decisions deliberately deferred

- Exact backend/database, source event IDs and API endpoint shape until Neon/installed code audit.
- Final thresholds and retention based on measured event rate, workload and memory use.
- Automatic stable preference evolution, semantic embeddings/vector database, LLM-authored summaries as facts, guild-wide AI strategy, combat learning, simulated offline progress, original content generation: out of scope until the simple system proves value.
- No additional public personal data or real server credentials in this repository.

The most important implementation rule: memory may change what a character wants to do, not what actually happened or what the game allows.
