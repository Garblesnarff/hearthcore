---
name: hearthcore-qa-playbook
description: >-
  Living QA checklist and Claude Opus 5.5 High review workflow for Hearthcore and Azeroth Watch.
  Load before opening PRs or starting issue work in hearthcore to avoid recurring security,
  reliability, database, and UI pitfalls caught by prior QA audits.
---

# Hearthcore Engineering & Claude QA Playbook

This is a living skill updated continuously after every Claude Opus 5.5 High QA pass in `Garblesnarff/hearthcore`.

## 1. Standard Issue & QA Workflow

1. **Zero `ac-worldserver` downtime** unless the user explicitly approves a maintenance window. Never rebuild, stop, or restart `ac-worldserver` during normal Python/dashboard/SQL/addon sprints.
2. **One issue per clean branch/PR** off latest `master`.
3. **Run local tests + secret scan first:**
   ```bash
   python3 -W ignore::ResourceWarning -m unittest discover -s tests -v
   node --check azeroth-watch/static/app.js
   node --check azeroth-watch/static/character.js
   bash ops/sync-monorepo.sh --scan-only
   ```
4. **Run Claude Opus 5.5 (high effort) QA before opening/merging the PR:**
   Pipe the exact diff via stdin with `--tools ""` for fast (~1-2 min), single-turn, high-signal reviews:
   ```bash
   git diff master | claude -p --model claude-opus-5-5 --effort high --tools "" --output-format text \
     "Strict principal-engineer QA review against acceptance criteria for Issue #N. Group findings by P0/P1/P2/P3 with exact file/function refs and concrete fixes."
   ```
   *(For broad multi-file audits where surrounding context matters, use `--restricted --tools Read,Grep,Glob --output-format json`.)*
5. **Fix all real P0/P1/P2 findings**, add targeted regression tests, re-verify tests locally, push, wait for **BOTH** `tests` and `mysql-integration` CI jobs to turn green, then squash-merge.
6. **Deploy zero-downtime host files cleanly** when `azeroth-watch/` or `ops/` changed (`bash ops/sync-monorepo.sh --deploy --force`, restart ONLY `azeroth-watch.service`), and explicitly state what was deployed live vs repo-only.
7. **Update this playbook** whenever a QA review uncovers a new recurring pattern!

---

## 2. Recurring Pitfalls & Non-Negotiable Invariants

### Database & Least-Privilege Separation
- **Fail closed on DB credentials — NEVER silently fall back to `root`:** Dashboard read/write modules (`character_ctl.py`, `collector.py`) must require their least-privilege profile (`AZEROTH_WATCH_DB_*`, `HEARTHCORE_OPERATOR_DB_*`) and raise on missing config (only allowing `HEARTHCORE_DB_HOST` TCP fallback when `HEARTHCORE_TEST_DB=1` in CI).
- **Keep `hearthcore_brain` isolated from manual overrides & schedules:** `hearthcore_brain` must NEVER be granted write access to `companion_weights` or `companion_schedule`. Human operator writes use `hearthcore_operator`.
- **Use ONE monotonic revision counter (`remote_max + 1`), never epoch timestamps:** Writing `unix_timestamp` into `companion_weight_overrides.revision` causes `BrainStore.sync_sequence_floor()` to think `brain.db` fell behind MySQL and triggers false `brain_sequence_resync` alerts.
- **Protect unexpired `source='operator'` overrides from `apply_plan_to_db`:** `ON DUPLICATE KEY UPDATE` must not overwrite an unexpired `source='operator'` row when a scheduled AI tick runs.
- **Read-only SQLite in GET endpoints:** Never instantiate full `BrainStore(db_path)` (which runs DDL & `UPDATE` migrations) inside HTTP GET handlers; use `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`.
- **Explicit column lists in MySQL integration test fixtures:** Never write positional `INSERT INTO table VALUES (...)` across shared CI test databases where another test may add columns.

### Telemetry, Benchmarks & Fail-Closed Gates
- **Unknown is `None`, NEVER fabricated `0`:** Missing population subfields, missing DB tables, missing `errors_bytes`, missing `data_age_sec`, or missing `active_ai_bots` must evaluate to `None` and fail closed — never `or 0`.
- **Require `>=2` samples for deltas & catch mid-run rotation:** Comparing only `first` vs `last` `errors_bytes` misses log rotation + regrowth back to the same byte count; track per-sample monotonicity.
- **Verify artifact SHA-256 digests and duration/method allowlists** in admission summaries (`benchmark_summary_for_admission`), never trusting `all_correctness_passed` alone.
- **Separate HTTP poll latency from SQL latency:** Never label HTTP `/api/state` round-trip time as `sql_query_ms`.
- **Make benchmark tests 100% hermetic:** Patch `/proc`, `brain.db`, and config directory lookups so tests never read live host state.
- **Never coerce unknown `None` telemetry with `bool(x)` or permissive defaults:** `bool(None)` silently turns unknown `in_combat`/`in_lfg_queue`/`is_dead` into "all clear" (`False`), and permissive defaults like `available_minutes=90` hide fail-open bugs. Test with exact bridge-shaped `None` fixtures and require explicit fail-closed checks on every live-state field.
- **Un-nest multi-stage reporting funnels & validate volunteer/party invariants:** Report independent sets (`eligible`, `available`, `willing`, `queued`, `matched`) so transient blockers don't erase queued/willing members; respect volunteer role choices, single-faction party rules, and joint party-level shift windows (slowest traveler).
- **Always add a positive baseline control assertion before negative mutation loops in tests**, and hash read-only SQLite files before/after to prove zero mutation.
- **Keep obtained vs potential supplies strictly separated & scope bank SQL to guild:** Never let undistributed Guild Bank stock make `feasible_now` true or overwrite character `overall` to `ready_available`; scope `guild_bank_item` by `guildid` (failing closed to `None` on multi-guild/missing); never let bench candidates consume active-roster bank stock.
- **Check per-source freshness & full-slot gear averages:** Never mask stale SQL/live data with current clock `observed_at`, and compute average iLvl over expected gear slots (`SUM(ItemLevel) / expected_slots`) rather than SQL `AVG()` over filled slots only.
- **Fail closed on missing persona DB & unverified roster requirements:** Missing `brain.db` or missing persona tables must return `db_ok=False` (preventing alt double-counting); enforce roster-level encounter coverage and fail-closed CLI arg validation (`--human`, `--raid`, `--stage`).

### Concurrency, Locks & Control Planes
- **NEVER acquire `_CTL_LOCK` inside a block that already holds `_CTL_LOCK`:** `_audit_log()` acquires `_CTL_LOCK` (non-reentrant). Always release `_CTL_LOCK` before calling `_audit_log()` or `_send_bytes()`.
- **Check rate limits BEFORE mutating state:** Rate-limiting a combined save+trigger endpoint (like directive + replan) after saving leaves mutated state behind a `429` response.
- **Per-character pause must actually stop Claude & respect global kill switches:** Persist paused characters in `brain.db` so `run_cycle()` skips them (no API spend), `reconcile_state()` ignores them, and presets/resume refuse to run while `brain.disabled` exists.
- **Consume one-shot directives once, and ONLY on real AI plans:** Filter `consumed_at = 0` when reading one-shot notes and stamp `consumed_at` only when `source == 'anthropic'` (never on fallback/holdout).
- **Fail-closed crash reconciliation & correlation keys:** Never treat `None` or unverified empty sets as "nothing is running" during crash reconciliation — require an explicit `authoritative=True` snapshot and persist a deterministic `correlation_key` BEFORE external handoffs so lost responses can be adopted rather than cancelled into orphans.
- **Guard SQLite rollbacks & persist TTL sweeps:** Always use `if conn.in_transaction: conn.rollback()` (avoiding "no transaction is active" masking the real error), and commit TTL expiry sweeps on early returns rather than rolling them back.
- **Forward-only idempotent replays & strict native run binding:** Reject conflicting non-empty IDs or backward native state transitions on callback replays, reject shadow rows on live native endpoints, and require exact `native_run_id` match before recording `completed`.

### HTTP Security, Auth & Loopback Trust
- **Scope opt-in loopback trust narrowly:** `AZEROTH_WATCH_TRUST_LOOPBACK_WRITES=1` applies ONLY to `/api/character/*` routes, rejects requests with `X-Forwarded-For` / `Forwarded` headers (no reverse-proxy/tunnel leak), and requires an explicit same-origin `Origin` header.
- **Enforce public allowlists on unauthenticated LAN reads:** Any new GET endpoint (like `/api/character/<slug>`) must redact human private fields (money, homebind, quests, gear, played time) and internal AI flags when `redact_admin=True`.
- **Add short TTL caching + bounded concurrency** on heavy on-demand GET endpoints so unauthenticated LAN clients cannot spawn unbounded DB subprocesses.
- **Place trusted operator directives in delimited system-prompt sections**, separate from untrusted game JSON strings, while relying on strict schema/allowlist validation (`core.validate()`).

### Code Quality & Frontend Polish
- **Zero silent `except: pass`:** Always log with `logger.debug(...)` or `logger.warning(...)` (enforced by Issue #61 AST test).
- **Avoid `exec()` for loading sibling modules** — use `importlib.util`.
- **Frontend polling vs user edits:** Every editable control (textareas, selects, checkboxes, sliders) needs a dirty flag so periodic refreshes never clobber in-progress user changes, and action buttons need a `busy` guard against double-click duplicates.
- **Tighten test assertions:** Parse real SQL `VALUES(...)` tuples instead of broad substring matches like `", 0, "`, and assert exact expected revisions/statuses rather than loose inequalities.
