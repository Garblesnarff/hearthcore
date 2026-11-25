# Phase II — implementation cards and verification

Companion to [PHASE_II_LIVING_GUILD.md](PHASE_II_LIVING_GUILD.md). Proposed work packages, not GitHub issues or deployed features. Owner-approved priorities; no feature enabled by documentation PR. Dependency order can overlap read-only audit and staging work; live gates are strict.

| Card | Scope | Depends on | Acceptance / deliverable |
|---|---|---|---|
| II-00 | Phase I acceptance and installed audit | #3–9 | Verify exact code, DB, feature flags, open findings, native LFG/group/strategy/owner/schedule hooks, actual dungeon support and workload. No code or game-state change. |
| II-01 | Persona/character identity seam | II-00 mapping audit | Additive mapping for current roster, compatibility with slug, faction, session and owner group; no alt creation or destructive rekey. |
| II-02 | Typed opportunity contract and read model | II-00, II-01 | Versioned scope/revision/IDs/lifecycle, existing work-order adapter, allowlist and pure shadow candidate generation. |
| II-03 | Dungeon catalog and role eligibility | II-00 | Installed IDs, entry/lockout/progression and role checks, one reviewed stock pilot; unsupported disabled. |
| II-04 | Native reservations and matchmaking | II-02, II-03 | Voluntary intents, atomic revision/slot admission, TTL, fair compatible match, partial invite compensation, restart tests. |
| II-05 | Cooperative mode and native group lifecycle | II-00, II-04 | Address owner-absent auto-disband, master/lead/strategy and lease conflict; owner precedence, schedule, safe exit and recovery. |
| II-06 | Dungeon execution vertical slice | II-03–05 | Actual bot-only native group travel/entry/clear, stock loot/XP/locks, no LLM tactics, safe wipe/recall/replacement. |
| II-07 | Correlated outcomes + memory / dashboard | #4–5 verified, II-02, II-05–06 | Structured evidence from actual lifecycle and run, scoped outcomes, queue/role/readiness visibility, no shadow contamination. |
| II-08 | Readiness read model and commitments | II-00, II-01, II-03 | Role/raid requirements, evidence/unknown, distinct available personas, supply claims, feasible roster; no autonomous raids. |
| II-09 | Bounded playstyle selection and planner adapter | II-02, II-07, #6–8 verified | Canonical traits, deterministic fallback and validated optional model goals, no stacked memory/persona factors. |
| II-10 | Combat/performance laboratory | II-00; fixture design can parallelize | Isolated staging fixtures, paired repeated measurements, baseline and correctness gates, config/resource rollback. |
| II-11 | Supervised pilot and seven-day observation | II-01–10, #3–9 verified | One group initially, core protected, safe off-switch, real outcomes and owner playtest; expand only with headroom. |

## First agent assignment: II-00

Inspect installed server and public mirror; compare exact hashes. Return actual paths, functions, thread context, config keys, schema/migrations and evidence of runtime behavior, not a speculative implementation. Trace CompanionBotMgr owner-absent group disband path, strategy drift/self-heal, master selection, owner group priority, lead strategy, lease arbitration, schedule/exit and LFG bot-only queue/proposal/group support. Check available classic dungeon route/tactics against level and progression gates. Verify prior issues #3–9 via tests and actual installation. Identify smallest native admission/execution hook and propose one supported five-player normal dungeon pilot. Report measured active bot/instance mix and p95/p99/RAM/swap baseline. Do not mass-login, alter live config, grant items, spend API credits or deploy.

## Mandatory test matrix

| Scenario | Expected behavior |
|---|---|
| Owner absent and five willing eligible bots | Native valid party persists, no stray-group disband; actual dungeon run |
| Arbitrary/unrecognized group | Existing guard remains; no unauthorized controller |
| Owner invites participating bot | Owner priority at safe boundary; no false failure or abrupt unsafe exit |
| Two invitations for same tank | One accepted reservation, other expires/withdraws |
| Interest expires, member logs off, role changes | Revalidate, release, replace only if valid |
| Shift end in combat | Bounded safe exit/grace, no indefinite forced attendance |
| Leader leaves/restart | Native group reconciliation or safe drain; no orphan lease |
| Entry denied/locked/faction mismatch | Reject, no teleport or DB bypass |
| Wipe, path issue, healer out of range | Bounded native recovery, factual reason; no learned generalized dislike |
| Desired loot does not drop | Valid attempt, not fabricated reward or failure of competence |
| Group clears, personal gear goal unmet | Run success distinct from individual intention outcome |
| Lost journal events/duplicate replay | Neutral affected memory, no double counted participation |
| Unapplied shadow opportunity | No live membership, reservation, intent, bank request or score change |
| Shared resource reserved for raider | Cannot double-count or volunteer protected items |
| Same persona owns two characters (synthetic future fixture) | Distinct facts; at most one counted/admitted active persona |
| Planner/API offline/over budget | Native autonomy; no blocked group/tick |
| Toggle disabled while active | Stop new admits; safe drain; existing owner groups unaffected |
| Unsupported dungeon/raid | No automated run |
| Mixed load over cap | Stop new admits; no forced logout during combat; recover with hysteresis |

## Release evidence checklist

- [ ] Exact installed core/module/bridge versions and migrations, Phase I issue acceptance report.
- [ ] Approved dungeon catalog entry and real eligibility/loot/instance checks.
- [ ] Shadow trace and matching diagnostics isolated from production.
- [ ] Staging successful run and failure/recovery fixtures with actual native event IDs.
- [ ] Owner-group regression, both companion groups protected and recall tested.
- [ ] Comparable workload baseline/pilot p50/p95/p99, active bots, instances, CPU/RSS/PSS/swap and benchmark logs.
- [ ] Authorized controls, budget caps, no model game chat; rollback/disable procedure rehearsed.
- [ ] Pilot actual run outcomes and personal goal outcomes, readiness evidence, optional-choice diversity and owner playtest.
- [ ] No claim of support for all stock dungeons or raids; expand incrementally.

## Parallel lanes and coordination

Integration lead owns identity/opportunity contract and native admission schema; avoid competing writers to group/lease/schedule code. Game-side lane owns II-03/05/06; planner lane owns II-02/04/09 with native review; telemetry lane owns II-07; readiness lane owns II-08; performance/test lane owns II-10. Freeze shared interfaces and fixture schema before parallel implementation. Each card ships a small PR, tests and documented feature flag. Maintain authoritative native state, no direct core group table writes. Do not enable production features until explicit review. These are work packages; create separate issues only when requested.

No runtime tests are run by this documentation-only PR. Exact thresholds, role policies and availability expectations are proposed defaults pending installed-system audit and owner confirmation.
