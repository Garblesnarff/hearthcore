# Memory v2 — implementation checklist

This is a proposed work breakdown, not a claim that any runtime code exists. Follow [the design](PERSISTENT_MEMORY.md). Each card is separately reviewable; no automatic production deploy. Begin with M0.

| Card | Deliverable | Depends on | Definition of done |
|---|---|---|---|
| M0 | Installed-system audit and sanitized event fixture | None | Identify feed producer, canonical personas, native score semantics, lease integration, mapping, baseline |
| M1 | Versioned canonical event schema + adapter | M0 | Validates payload/provenance/identity; supports stable event IDs and declared coverage |
| M2 | Durable ingestion and replay | M1 | Crash/restart/duplicate/late/conflicting/restore tests; metrics and bounded resource usage |
| M3 | Deterministic memory reducers + retention | M2 | Scoped episodes, attempt denominators, open goals, fatigue, familiarity; deterministic checkpoint replay |
| M4 | Read-only dashboard memory inspector | M2–M3 | Evidence and score trace; freshness, coverage, lag, faults visible |
| M5 | Shadow score experiment | M0–M4 | Same candidate snapshot for control/experiment; no double-counted weights; no game writes |
| M6 | Native safe importer + limited deterministic pilot | M5 | Idempotent versioned proposal validation; switches, fallback, performance/regression tests |
| M7 | Optional Claude context and budget controls | M6 | Bounded asynchronous requests, schema validation, stale-plan rejection, privacy and failure tests |
| M8 | Guild-scale cooperation and staged rollout | M6–M7 | Reservations, bounded familiarity, schedule/load tests, review by owner |
| M9 | Review actual playtest results | M8 | Document observed impact and failures, tune or disable; no invented enjoyment metrics |

## First coding-agent task (M0)

Inspect only the installed server, observability backend and persona/scheduling configuration. Return file paths, function names, tables, configuration keys, sample sanitized structured events and proof of actual behavior versus source-only claims. Identify whether events are durable or derived from polling; distinguish observed event from current state. Describe actual IDs and coverage, existing goal lifecycle and retry reasons, whether native scores already include persona/travel/failure terms, current resource measurements, and the smallest safe adapter. Confirm both companion groups and guild bots can be mapped without replacing personas. No migrations, no mass logins, no live settings change, no API spending.

## Mandatory tests before live rollout

- [ ] Duplicate, out-of-order, late, corrupt and conflicting events.
- [ ] Crash before/after transactional checkpoint, restore and character identity reuse.
- [ ] Partial feed and stale snapshot neutralize affected modifiers.
- [ ] No-drop vs ineligible loot, failed route vs deliberate abandonment, voluntary vs required choices.
- [ ] Stable persona unchanged; goal completion/obsolescence and revalidation.
- [ ] Scoped adaptation and fatigue recovery; relationship dedup and limits.
- [ ] Score component audit; no duplicate preference or evidence boost; safety and group priorities win.
- [ ] No per-tick SQL/network, bounded worker and queue, measured p95/p99 and swap.
- [ ] Expired/replaced plans, concurrent imports/reservations, API errors and spending cap.
- [ ] Shadow-only and live switches, fail-open native autonomy, actual rewards only, no generated game chat.
- [ ] Checkpoint + retained suffix deterministic rebuild; evidence links preserved through retention.

## Review note

No tests are executed by this documentation PR. Implementation estimates, thresholds, model context limits and storage assumptions are provisional. Validate them on the installed server before enabling behavior.
