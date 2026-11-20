import argparse
import json
import sys
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
    args = cli.parse_args()
    try:
        if args.command in ("roster", "validate-roster"):
            for slug in profiles():
                character = profile(slug)
                if args.command == "roster":
                    print(slug, character["class"], character["status"])
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
