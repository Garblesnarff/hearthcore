import argparse
import json
import logging
import sys
import time
from pathlib import Path
from .core import Invalid, fallback, profile, profiles, snapshot
from .store import record


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cli = argparse.ArgumentParser()
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("roster")
    commands.add_parser("validate-roster")

    p = commands.add_parser("plan")
    p.add_argument("--character", required=True)
    p.add_argument("--snapshot", required=True)
    p.add_argument("--provider", choices=("fallback", "anthropic"), default="fallback")
    p.add_argument("--output")
    p.add_argument("--db")

    bt = commands.add_parser("brain-tick", help="Run one session-boundary planning pass for online companions")
    bt.add_argument("--provider", choices=("anthropic", "fallback"), default="anthropic")
    bt.add_argument("--shadow", action="store_true", help="Log plans only; do not write companion_weights")
    bt.add_argument("--force", action="store_true", help="Replan even if companion has an unexpired session plan")
    bt.add_argument("--limit", type=int, default=0, help="Max companions to plan in this run (0 = all due)")
    bt.add_argument("--character", help="Plan only a single companion slug")

    commands.add_parser("brain-status", help="Show active AI Brain session plans and today's API stats")
    bd = commands.add_parser("brain-disable", help="Disable AI Brain live cycles and/or MySQL weight overrides")
    bd.add_argument("--guid", type=int, default=None, help="Optional single character GUID to disable in MySQL")
    bd.add_argument("--skip-db", action="store_true", help="Only write the local kill-switch flag file")
    be = commands.add_parser("brain-enable", help="Re-enable AI Brain live cycles and/or MySQL weight overrides")
    be.add_argument("--guid", type=int, default=None, help="Optional single character GUID to re-enable in MySQL")
    be.add_argument("--skip-db", action="store_true", help="Only remove the local kill-switch flag file")

    commands.add_parser("reconcile", help="Compare SQLite plans/journal against live MySQL proposals/overrides; non-zero on drift")
    rep = commands.add_parser("replay", help="Offline no-network replay: recorded snapshot in -> validated plan & SQL out")
    rep.add_argument("--character", required=True)
    rep.add_argument("--snapshot", required=True)
    rep.add_argument("--guid", type=int, default=1001)
    rep.add_argument("--plan-id", type=int, default=1)

    commands.add_parser("supply-report", help="Compute 40-char Classic (1-300) profession material deficits vs Guild Bank")

    qm = commands.add_parser("quartermaster-tick", help="Run Claude Guild Quartermaster to plan 12+ orders & bank requests")
    qm.add_argument("--provider", choices=("anthropic", "fallback"), default="anthropic")
    qm.add_argument("--shadow", action="store_true", help="Compute & log without writing DB")
    qm.add_argument("--model", help="Override strategist model (default: claude-sonnet-5-5)")

    commands.add_parser("phase2-shadow-match", help="Run pure Phase II shadow dungeon matchmaking (zero live DB writes)")
    pr = commands.add_parser("phase2-readiness", help="Evaluate owner-selected distinct-persona Raid Readiness & shortfalls (read-only)")
    pr.add_argument("--raid", default=None, help="Specific raid_id (e.g. mc_40, bwl_40, aq40_40, karazhan_10, gruul_25, naxx_10, onyxia_10)")
    pr.add_argument("--stage", type=int, default=None, help="Explicit server raid_stage override (default: read live from dashboard)")
    pr.add_argument("--human", action="append", default=[], help="Confirmed human slot as Name:role (repeatable, e.g. --human Wonky:tank)")
    pr.add_argument("--include", action="append", default=[], help="Restrict companion roster to specific slug(s) (repeatable)")
    pr.add_argument("--exclude", action="append", default=[], help="Exclude specific companion slug(s) (repeatable)")
    pr.add_argument("--pin", action="append", default=[], help="Pin companion to specific role as slug:role (repeatable)")
    pr.add_argument("--attestations", default=None, help="Path to read-only owner encounter-tactics attestation JSON")
    pr.add_argument("--owner-group-id", type=int, default=None, help="Owner's forming raid group ID (members in this group count as available)")
    pr.add_argument("--list-raids", action="store_true", help="List all defined raid specs and exit")
    bmk = commands.add_parser("phase2-benchmark", help="Capture non-destructive live baseline benchmark artifact")
    bmk.add_argument("--scenario", default="baseline_outdoor_solo")
    bmk.add_argument("--samples", type=int, default=3)
    bmk.add_argument("--interval", type=float, default=2.0)
    bmk.add_argument("--warmup", type=float, default=0.0, help="Seconds to wait before the first sample")
    bmk.add_argument(
        "--min-duration", type=float, default=600.0,
        help="Minimum measured span in seconds; shorter runs fail correctness (default 600 = 10 min)",
    )

    args = cli.parse_args()
    try:
        if args.command in ("roster", "validate-roster"):
            for slug in profiles():
                character = profile(slug)
                if args.command == "roster":
                    print(slug, character["class"], character["status"])
            return 0

        if args.command == "brain-tick":
            from .bridge import RunLockBusy, install_sigterm_handler, run_cycle
            install_sigterm_handler()
            try:
                res = run_cycle(
                    provider=args.provider,
                    apply=not args.shadow,
                    force=args.force,
                    limit=args.limit,
                    character=args.character,
                )
            except RunLockBusy as exc:
                logging.getLogger("hearthcore").warning("brain-tick skipped cleanly: %s", exc)
                return 0
            for item in res["planned"]:
                err = f" err={item['error']}" if item["error"] else ""
                print(
                    f"[{item['source']}] {item['name']:<12} Lv{item['level']:<2} -> "
                    f"{item['primary_goal_id']:<28} ({item['reason_code']}, {item['session_minutes']}m, "
                    f"tok={item['tokens']}, {item['latency_ms']}ms){err}"
                )
            t = res["summary"]["today"]
            failed = res.get("failed") or []
            print(
                f"Cycle done: planned={res['planned_count']} failed={len(failed)} | today: ai_ok={t['ai_ok']} "
                f"ai_err={t['ai_err']} fb={t['fallback']} tok={t['input_tokens']}in/{t['output_tokens']}out "
                f"cap={res['summary']['daily_cap']}"
            )
            for fitem in failed:
                print(f"FAILED {fitem['name']}: {fitem['error']}", file=sys.stderr)
            return 1 if failed else 0

        if args.command == "brain-status":
            from .bridge import BRAIN_DB_PATH, BrainStore, is_brain_disabled, load_env
            from .memory import MemoryEngine
            load_env()
            store = BrainStore()
            s = store.summary(int(time.time()))
            jstat = MemoryEngine(BRAIN_DB_PATH).journal_status()
            t = s["today"]
            flags = []
            if is_brain_disabled():
                flags.append("DISABLED")
            if s.get("stale_alarm"):
                flags.append(f"STALE>{s.get('stale_threshold_minutes')}m")
            flag_str = f" [{' '.join(flags)}]" if flags else ""
            print(f"Hearthcore AI Brain | model={s['model']} effort={s['effort']} day={s['day']}{flag_str}")
            print(
                f"Today: ai_ok={t['ai_ok']} ai_err={t['ai_err']} fallback={t['fallback']} "
                f"cap={s['daily_cap']} tokens={t['input_tokens']}in/{t['output_tokens']}out "
                f"avg_latency={t['avg_latency_ms']}ms"
            )
            print(
                f"Event Journal: events={jstat['total_events']} cursor={jstat['cursor']} "
                f"lag={jstat['journal_lag']} gap={jstat['gap_detected']}"
            )
            ab = s.get("ab_report") or {}
            by_src = ab.get("by_source") or {}
            if by_src:
                parts = [
                    f"{src}: n={v['sessions']} prog={v['progress_rate']*100:.0f}% $/sess=${v['cost_per_session_usd']:.4f}"
                    for src, v in sorted(by_src.items())
                ]
                print(f"A/B Outcomes ({ab.get('window_days', 14)}d, holdout={ab.get('holdout_pct', 0)}%): " + " | ".join(parts))
            from .bridge import RUNTIME_DIR
            alerts_file = RUNTIME_DIR / "failure_alerts.log"
            if alerts_file.is_file():
                recent_alerts = alerts_file.read_text(encoding="utf-8").splitlines()[-5:]
                if recent_alerts:
                    print(f"Recent Failure Alerts ({len(recent_alerts)} shown from {alerts_file.name}):")
                    for ln in recent_alerts:
                        print(f"  ! {ln}")
            active = [v for v in s["plans"].values() if v["active"]]
            print(f"Active session plans ({len(active)}):")
            for v in sorted(active, key=lambda x: x["character_id"]):
                rem = max(0, (v["expires_at"] - int(time.time())) // 60)
                print(
                    f"  {v['character_id']:<12} [{v['source']:<9}] {v['primary_goal_id']:<28} "
                    f"({v['reason_code']}, {rem}m left) - {v['primary_desc']}"
                )
            return 0

        if args.command == "reconcile":
            from .bridge import load_env, reconcile_state
            load_env()
            report = reconcile_state()
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0 if report["ok"] else 1

        if args.command == "replay":
            from .bridge import replay_snapshot
            snap_raw = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
            out = replay_snapshot(args.character, snap_raw, guid=args.guid, plan_id=args.plan_id)
            print(json.dumps(out, indent=2, sort_keys=True))
            return 0

        if args.command in ("brain-disable", "brain-enable"):
            from .bridge import set_brain_disabled
            res = set_brain_disabled(
                args.command == "brain-disable",
                guid=args.guid,
                skip_db=args.skip_db,
            )
            print(json.dumps(res, indent=2, sort_keys=True))
            return 0

        if args.command == "supply-report":
            from .quartermaster import fetch_40char_supply_state
            st = fetch_40char_supply_state()
            sup = st["supply"]
            print(f"Hearthguard 40-Char Classic (1-300) Supply Report ({sup['characters_analyzed']} characters)")
            print("\nTop 20 Material Deficits (Active Bracket vs Guild Bank | Full 1-300 Total):")
            for m in sup["materials"][:20]:
                print(
                    f"  {m['name']:<22} have={m['vault_have']:<4} now={m['active_need']:<5} "
                    f"def_now={m['active_deficit']:<5} full300={m['full_300_need']:<5} ({m['driven_by_active'] or m['driven_by_all']})"
                )
            return 0

        if args.command == "quartermaster-tick":
            from .bridge import RunLockBusy
            from .quartermaster import run_quartermaster
            try:
                res = run_quartermaster(
                    provider=args.provider,
                    apply=not args.shadow,
                    model=args.model,
                )
            except RunLockBusy as exc:
                logging.getLogger("hearthcore").warning("quartermaster-tick skipped cleanly: %s", exc)
                return 0
            p = res["plan"]
            m = res["meta"]
            print(f"[{res['source']}] model={m.get('model', res['source'])} ({m.get('latency_ms', 0)}ms, tok={m.get('input_tokens', 0)}/{m.get('output_tokens', 0)})")
            print("Strategy:", p["strategy_summary"])
            print(f"Focus skills: {', '.join(p['focus_skills'])}")
            print(f"Work orders added ({res['orders_added']}):")
            for wo in p["work_orders"]:
                print(f"  [P{wo['priority']}] {wo['name']} x{wo['quantity']} - {wo['reason']}")
            print(f"Bank gathering directives set ({res['banks_set']}):")
            for br in p["bank_requests"][:10]:
                print(f"  {br['name']:<22} min={br['min_count']:<3} ({br['note']})")
            if res["error_note"]:
                print("Error note:", res["error_note"])
            return 0

        if args.command == "phase2-shadow-match":
            from .bridge import BRAIN_DB_PATH, RUNTIME_DIR, fetch_world_context
            from .opportunities import run_shadow_match_snapshot
            import urllib.request
            vitals = {}
            raid_stage = 0
            try:
                with urllib.request.urlopen("http://127.0.0.1:8787/api/state", timeout=3) as r:
                    st = json.load(r)
                vitals = {
                    "mem_avail_gib": (st.get("vitals") or {}).get("mem_avail_gib"),
                    "tick_p99": (st.get("ticks") or {}).get("p99"),
                }
                raid_stage = int((st.get("raid") or {}).get("stage") or 0)
            except Exception as exc:
                logging.getLogger("hearthcore").warning("Dashboard telemetry fetch failed (fail-closed): %s", exc)
            world_ctx = fetch_world_context()
            res = run_shadow_match_snapshot(
                world_ctx, db_path=BRAIN_DB_PATH, runtime_dir=RUNTIME_DIR, vitals=vitals, raid_stage=raid_stage
            )
            print(f"Phase II Shadow Match | coop_live={res['coop_enabled_live']} | headroom_ok={res['headroom']['admit_ok']} ({res['headroom']['reasons'] or 'OK'})")
            for d in res["shadow_dungeons"]:
                print(
                    f"  {d['name']:<16} (LFG#{d.get('lfg_dungeon_id', 0)} Lv {d['level_band']} rec {d.get('recommended_level_band', '')}) "
                    f"eligible={d['eligible_counts']} avail={d.get('available_counts')} "
                    f"willing={d.get('willing_counts')} queued={d.get('queued_counts')} "
                    f"matched={d.get('matched_counts')} feasible={d['feasible_now']} "
                    f"roster_feasible={d.get('roster_feasible_now')} live_enabled={d['pilot_enabled_live']}"
                )
            return 0

        if args.command == "phase2-readiness":
            import os
            import tempfile
            import urllib.request
            from .bridge import BRAIN_DB_PATH, RUNTIME_DIR, fetch_world_context
            from .readiness import (
                ALLOWED_HUMAN_ROLES,
                RAID_CATALOG,
                evaluate_raid_roster,
                fetch_raid_readiness_telemetry,
                load_owner_attestations,
                resolve_raid_spec,
            )
            if args.list_raids:
                for rs in RAID_CATALOG:
                    print(f"  {rs.raid_id:<14} {rs.name:<28} stage>={rs.raid_stage_min} size={rs.raid_size:<2} map={rs.map_id:<3} diff={rs.difficulty} ilvl>={rs.min_avg_ilvl:.0f} roles={rs.roles_dict()}")
                return 0

            raid_stage = args.stage
            raid_stage_source = "owner_override" if raid_stage is not None else "unknown"
            if raid_stage is None:
                dash_url = (os.getenv("HEARTHCORE_DASHBOARD_URL") or "http://127.0.0.1:8787").rstrip("/")
                try:
                    with urllib.request.urlopen(f"{dash_url}/api/state", timeout=3) as r:
                        st = json.load(r)
                    raw_st = (st.get("raid") or {}).get("stage")
                    if raw_st is not None:
                        raid_stage = int(raw_st)
                        raid_stage_source = "live_dashboard"
                except Exception as exc:
                    logging.getLogger("hearthcore").warning("Live raid_stage fetch failed (fail-closed -> None): %s", exc)

            spec = resolve_raid_spec(args.raid, raid_stage=raid_stage)
            if not args.raid:
                logging.getLogger("hearthcore").info("No --raid provided; using stage %s default raid %s", raid_stage, spec.raid_id)

            # Strictly validate --human Name:role arguments (P1-6: never silently drop or guess role!)
            human_slots = []
            seen_h: set[str] = set()
            for h_raw in (args.human or []):
                if ":" not in h_raw:
                    raise ValueError(f"Invalid --human {h_raw!r}: must be Name:role (tank|healer|damage)")
                hn, hr = h_raw.split(":", 1)
                hn = hn.strip()
                hr = hr.strip().lower()
                if hr in ("dps", "mdps", "rdps"):
                    hr = "damage"
                if not hn or hr not in ALLOWED_HUMAN_ROLES:
                    raise ValueError(f"Invalid --human {h_raw!r}: role must be tank, healer, or damage")
                if hn.lower() in seen_h:
                    raise ValueError(f"Duplicate --human name: {hn!r}")
                seen_h.add(hn.lower())
                human_slots.append({"name": hn, "role": hr, "status": "confirmed"})

            owner_pins: dict[str, str] = {}
            for p_raw in (args.pin or []):
                if ":" not in p_raw:
                    raise ValueError(f"Invalid --pin {p_raw!r}: must be slug:role")
                ps, pr_role = p_raw.split(":", 1)
                ps, pr_role = ps.strip().lower(), pr_role.strip().lower()
                if pr_role in ("dps", "mdps", "rdps"):
                    pr_role = "damage"
                if not ps or pr_role not in ALLOWED_HUMAN_ROLES:
                    raise ValueError(f"Invalid --pin {p_raw!r}: role must be tank, healer, or damage")
                owner_pins[ps] = pr_role

            att_map, att_errs = load_owner_attestations(
                Path(args.attestations) if args.attestations else None, raid_id=spec.raid_id
            )
            world_ctx = fetch_world_context()
            telem_bundle = fetch_raid_readiness_telemetry(world_ctx, spec, attestations=att_map)
            rep = evaluate_raid_roster(
                world_ctx["companions"],
                raid_id=spec.raid_id,
                raid_stage=raid_stage,
                raid_stage_source=raid_stage_source,
                bank_consumable_pool=telem_bundle["bank_pool"],
                telemetry_by_slug=telem_bundle["telemetry_by_slug"],
                human_slots=human_slots,
                owner_selected_slugs=(args.include if args.include else None),
                owner_excluded_slugs=(args.exclude if args.exclude else None),
                owner_pinned_roles=(owner_pins if owner_pins else None),
                owner_group_id=args.owner_group_id,
                access_verified_against_db=telem_bundle["access_verified_against_db"],
                db_legal_min_level=telem_bundle["db_legal_min_level"],
                db_path=BRAIN_DB_PATH,
                observed_at=world_ctx["observed_at"],
            )
            all_errs = list(att_errs) + list(telem_bundle["errors"])
            if all_errs:
                rep["telemetry_errors"] = all_errs
            out_path = RUNTIME_DIR / "phase2_readiness.json"
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=RUNTIME_DIR, prefix="phase2_readiness.", suffix=".tmp", delete=False, encoding="utf-8") as tf:
                tf.write(json.dumps(rep, indent=2) + "\n")
                tmp_name = tf.name
            Path(tmp_name).replace(out_path)
            print(
                f"Raid Readiness ({rep['raid_id']} — {rep['tier_name']}, stage={rep['raid_stage']} [{rep['raid_stage_source']}]): "
                f"obtained={rep['summary_counts_obtained']} | feasible_now(obtained)={rep['feasible_now']} "
                f"| with_potential_bank={rep['feasible_with_potential_bank']} "
                f"| bench={len(rep['viable_bench'])} | bank_pool={rep['bank_consumables_pool']} "
                f"(pot_alloc={rep['bank_consumables_potential_allocated']}, unalloc={rep['bank_consumables_unallocated']})"
            )
            for b in rep["bottlenecks"]:
                print("  -", b)
            return 1 if rep["rejected_humans"] else 0

        if args.command == "phase2-benchmark":
            from .benchmark import run_live_baseline_benchmark
            from .bridge import BRAIN_DB_PATH
            from .character_ctl import run_read_sql
            art = run_live_baseline_benchmark(
                scenario=args.scenario, sample_count=args.samples, interval_sec=args.interval,
                warmup_sec=args.warmup, min_duration_sec=args.min_duration,
                brain_db_path=BRAIN_DB_PATH, sql_exec=run_read_sql,
            )
            print(f"Benchmark saved: {art['saved_path']}")
            print(f"Metrics: {art['metrics']} | correctness={art['all_correctness_passed']} | sha256={art['artifact_sha256'][:16]}")
            return 0

        character = profile(args.character)
        snap = snapshot(json.loads(Path(args.snapshot).read_text()), args.character)
        source = args.provider
        if source == "anthropic":
            from .budget import BudgetLedger
            from .provider import anthropic, default_budget_db_path
            ledger = BudgetLedger(default_budget_db_path())
            try:
                plan = anthropic(character, snap, budget=ledger, caller="cli_plan")
            except Invalid as e:
                print("API plan rejected; using fallback: " + str(e), file=sys.stderr)
                plan, source = fallback(character, snap), "fallback_after_api"
        else:
            plan = fallback(character, snap)
        result = json.dumps(plan, indent=2, sort_keys=True) + "\n"
        if args.output:
            Path(args.output).write_text(result)
        else:
            print(result, end="")
        if args.db:
            record(args.db, plan, source, snap["observed_at"])
        return 0
    except (Invalid, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
