import argparse
import json
import sys
import time
from pathlib import Path
from .core import Invalid, fallback, profile, profiles, snapshot
from .store import record


def main():
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

    commands.add_parser("supply-report", help="Compute 40-char Classic (1-300) profession material deficits vs Guild Bank")

    qm = commands.add_parser("quartermaster-tick", help="Run Claude Guild Quartermaster to plan 12+ orders & bank requests")
    qm.add_argument("--provider", choices=("anthropic", "fallback"), default="anthropic")
    qm.add_argument("--shadow", action="store_true", help="Compute & log without writing DB")
    qm.add_argument("--model", help="Override strategist model (default: claude-sonnet-5-5)")

    args = cli.parse_args()
    try:
        if args.command in ("roster", "validate-roster"):
            for slug in profiles():
                character = profile(slug)
                if args.command == "roster":
                    print(slug, character["class"], character["status"])
            return 0

        if args.command == "brain-tick":
            from .bridge import run_cycle
            res = run_cycle(
                provider=args.provider,
                apply=not args.shadow,
                force=args.force,
                limit=args.limit,
                character=args.character,
            )
            for item in res["planned"]:
                err = f" err={item['error']}" if item["error"] else ""
                print(
                    f"[{item['source']}] {item['name']:<12} Lv{item['level']:<2} -> "
                    f"{item['primary_goal_id']:<28} ({item['reason_code']}, {item['session_minutes']}m, "
                    f"tok={item['tokens']}, {item['latency_ms']}ms){err}"
                )
            t = res["summary"]["today"]
            print(
                f"Cycle done: planned={res['planned_count']} | today: ai_ok={t['ai_ok']} "
                f"ai_err={t['ai_err']} fb={t['fallback']} tok={t['input_tokens']}in/{t['output_tokens']}out "
                f"cap={res['summary']['daily_cap']}"
            )
            return 0

        if args.command == "brain-status":
            from .bridge import BrainStore, load_env
            load_env()
            s = BrainStore().summary(int(time.time()))
            t = s["today"]
            print(f"Hearthcore AI Brain | model={s['model']} effort={s['effort']} day={s['day']}")
            print(
                f"Today: ai_ok={t['ai_ok']} ai_err={t['ai_err']} fallback={t['fallback']} "
                f"cap={s['daily_cap']} tokens={t['input_tokens']}in/{t['output_tokens']}out "
                f"avg_latency={t['avg_latency_ms']}ms"
            )
            active = [v for v in s["plans"].values() if v["active"]]
            print(f"Active session plans ({len(active)}):")
            for v in sorted(active, key=lambda x: x["character_id"]):
                rem = max(0, (v["expires_at"] - int(time.time())) // 60)
                print(
                    f"  {v['character_id']:<12} [{v['source']:<9}] {v['primary_goal_id']:<28} "
                    f"({v['reason_code']}, {rem}m left) - {v['primary_desc']}"
                )
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
            from .quartermaster import run_quartermaster
            res = run_quartermaster(
                provider=args.provider,
                apply=not args.shadow,
                model=args.model,
            )
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

        character = profile(args.character)
        snap = snapshot(json.loads(Path(args.snapshot).read_text()), args.character)
        source = args.provider
        if source == "anthropic":
            from .provider import anthropic
            try:
                plan = anthropic(character, snap)
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
