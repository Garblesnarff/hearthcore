#!/usr/bin/env python3
"""LoganReads HELP LOG report.

Reads the LoganReads SavedVariables file (account-wide LoganReadsLog) written
by the client each time a "Hear it" button is pressed, and prints one list per
week: each text he asked to hear and how many times, most often first.

Default file:
  <client>/WTF/Account/LOGAN/SavedVariables/LoganReads.lua

Usage:
  python3 help_report.py                      # default file
  python3 help_report.py path/to/LoganReads.lua
"""
import datetime
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

CLIENT = Path("/mnt/hdd/WoW335a/extracted/ChromieCraft_3.3.5a")
DEFAULT_FILE = CLIENT / "WTF" / "Account" / "LOGAN" / "SavedVariables" / "LoganReads.lua"

ENTRY_SPLIT = re.compile(r"^\s*\[\d+\]\s*=\s*\{", re.MULTILINE)
PAIR = re.compile(r'\["(\w+)"\]\s*=\s*("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?)\s*,?')


def unquote(raw):
    if raw.startswith('"'):
        body = raw[1:-1]
        return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), body)
    return raw


TOP_KEY = re.compile(r"^\S+\s*=", re.MULTILINE)


def section(text, name):
    """Text of one top-level SavedVariables table, e.g. LoganReadsLog = { ... }."""
    m = re.search(r"^" + re.escape(name) + r"\s*=", text, re.MULTILINE)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = TOP_KEY.search(rest)
    return rest if nxt is None else rest[:nxt.start()]


def load_records(path, name="LoganReadsLog"):
    text = path.read_text(encoding="utf-8", errors="replace")
    body = section(text, name)
    records = []
    # each record starts at "[n] = {"; the table ends before the next top-level key
    chunks = ENTRY_SPLIT.split(body)[1:]
    for chunk in chunks:
        end = chunk.find("\n\t}")
        chunk = chunk if end < 0 else chunk[:end]
        fields = {k: unquote(v) for k, v in PAIR.findall(chunk)}
        if "date" in fields and ("text" in fields or name == "LoganReadsCoachLog"):
            records.append(fields)
    return records


def coach_report(path):
    """Per ability: presses, misses, median seconds to the press, by week."""
    records = load_records(path, "LoganReadsCoachLog")
    if not records:
        print("No coach records (LoganReadsCoachLog) in this file.")
        return
    stats = defaultdict(lambda: defaultdict(lambda: {"presses": 0, "misses": 0, "secs": []}))
    for rec in records:
        label = week_label(rec["date"])
        cell = stats[rec.get("ability", "?")][label]
        secs = rec.get("secs", "missed")
        if secs == "missed":
            cell["misses"] += 1
        else:
            try:
                cell["presses"] += 1
                cell["secs"].append(float(secs))
            except ValueError:
                cell["misses"] += 1
    print()
    print("#### Coach: key presses per ability ####")
    print(f"Coach records: {len(records)}")
    for ability in sorted(stats):
        total_p = sum(c["presses"] for c in stats[ability].values())
        total_m = sum(c["misses"] for c in stats[ability].values())
        print()
        print(f"== {ability}: presses {total_p}, misses {total_m} ==")
        for label in sorted(stats[ability]):
            cell = stats[ability][label]
            med = f"{statistics.median(cell['secs']):.1f}s" if cell["secs"] else "-"
            print(f"  {label}: presses {cell['presses']}, misses {cell['misses']}, median {med}")


def week_label(date_text):
    try:
        day = datetime.datetime.strptime(date_text[:10], "%Y-%m-%d").date()
    except ValueError:
        return "unknown week"
    monday = day - datetime.timedelta(days=day.weekday())
    iso_year, iso_week, _ = day.isocalendar()
    return f"week of {monday.isoformat()} (ISO {iso_year}-W{iso_week:02d})"


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FILE
    if not path.exists():
        print(f"No SavedVariables file found at: {path}")
        print("Log in on Logan's account once, press a Hear it button, then log out so the file is written.")
        return 1
    records = load_records(path)
    if not records:
        print(f"No Hear-it records in {path}")
        coach_report(path)
        return 0

    weeks = defaultdict(Counter)
    for rec in records:
        label = week_label(rec["date"])
        key = (rec.get("quest", "?"), rec.get("field", "?"), rec["text"])
        weeks[label][key] += 1

    print(f"Source: {path}")
    print(f"Total Hear-it presses: {len(records)}")
    for label in sorted(weeks, key=lambda w: w):
        print()
        print(f"== {label} ==")
        for (quest, field, text), count in weeks[label].most_common():
            print(f"  {count:>3}x  quest {quest} / {field}: {text}")
    coach_report(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
