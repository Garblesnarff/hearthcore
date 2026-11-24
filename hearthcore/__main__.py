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
