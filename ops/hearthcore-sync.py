#!/usr/bin/env python3
"""Sync Garblesnarff/hearthcore character profiles with AzerothCore companion_weights
and report readiness for planned Hearthguard guild roster characters.

Usage:
  hearthcore-sync.py --dry-run   # preview weight sync & planned roster status
  hearthcore-sync.py --apply     # apply active companion weights to acore_characters.companion_weights
"""

import argparse
import glob
import json
import os
import subprocess
import sys

HEARTHCORE_DIR = os.getenv("HEARTHCORE_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
DB_CONTAINER = os.getenv("HEARTHCORE_DB_CONTAINER", "ac-database")
GUILD_ID = 6

# Canonical profile preference -> native CompanionBrain weight key mapping (Issue #60)
KEY_MAP = {
    "quest": ("quest_work", "quest_turnin", "quest_hub", "quest_grind"),
    "craft": ("craft",),
    "gather": ("gather",),
    "train": ("train",),
    "bank": ("bank",),
    "repair": ("repair", "sell"),
    "travel": ("travel",),
    "fish": ("fish",),
    "rest": ("hearth",),
}

ALLIANCE_DEFAULT_RACE = {
    "druid": "night elf",
    "shaman": "draenei",
}


def run_sql(sql: str) -> str:
    cmd = [
        "docker", "exec", "-i", DB_CONTAINER, "sh", "-c",
        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -u root -N -B'
    ]
    res = subprocess.run(cmd, input=sql, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip())
    return res.stdout


def load_profiles():
    profiles = []
    for path in sorted(glob.glob(os.path.join(HEARTHCORE_DIR, "characters", "*", "profile.json"))):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            data["_path"] = path
            profiles.append(data)
    return profiles


def load_guild_chars():
    out = run_sql(
        f"SELECT c.name, c.guid, c.level, c.race, c.class, c.gender "
        f"FROM acore_characters.guild_member gm "
        f"JOIN acore_characters.characters c ON c.guid = gm.guid "
        f"WHERE gm.guildid = {GUILD_ID};"
    )
    chars = {}
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 6:
            chars[parts[0].lower()] = {
                "name": parts[0],
                "guid": int(parts[1]),
                "level": int(parts[2]),
                "race": int(parts[3]),
                "class": int(parts[4]),
                "gender": int(parts[5]),
            }
    return chars


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Write active companion weights to companion_weights")
    ap.add_argument("--dry-run", action="store_true", help="Preview only (default)")
    ap.add_argument(
        "--overwrite-manual",
        action="store_true",
        help="Explicitly allow overwriting manually tuned rows (source != 'hearthcore') in companion_weights",
    )
    args = ap.parse_args()

    profiles = load_profiles()
    existing = load_guild_chars()

    print(f"=== Hearthcore Profiles ({len(profiles)} total) ===")
    sql_lines = []
    planned = []

    for p in profiles:
        name = p["name"]
        slug = p["slug"]
        status = p.get("status", "unknown")
        info = existing.get(name.lower())
        if info:
            guid = info["guid"]
            prefs = p.get("preferences", {})
            mapped = []
            for hk, ck_tuple in KEY_MAP.items():
                if hk in prefs and prefs[hk] is not None:
                    val = float(prefs[hk])
                    val = max(0.5, min(1.5, val))
                    for ck in ck_tuple:
                        mapped.append((ck, val))
                        if args.overwrite_manual:
                            upd = "`value` = VALUES(`value`), `source` = VALUES(`source`)"
                        else:
                            # Issue #60: NEVER overwrite manually tuned canonical rows unless --overwrite-manual!
                            upd = (
                                "`value` = IF(`source` = 'hearthcore', VALUES(`value`), `value`), "
                                "`source` = IF(`source` = 'hearthcore', VALUES(`source`), `source`)"
                            )
                        sql_lines.append(
                            f"INSERT INTO acore_characters.companion_weights (`guid`, `key`, `value`, `source`) "
                            f"VALUES ({guid}, '{ck}', {val:.2f}, 'hearthcore') "
                            f"ON DUPLICATE KEY UPDATE {upd};"
                        )
            pref_str = ", ".join(f"{k}={v:.2f}" for k, v in mapped if abs(v - 1.0) > 1e-6) or "all 1.0"
            print(f"  [LIVE]    {name:<12} guid={guid:<5} lv={info['level']:<2} {p.get('class')}/{p.get('primary_spec')} -> {pref_str}")
        else:
            cls = p.get("class") or "?"
            race = p.get("race") or f"(default {ALLIANCE_DEFAULT_RACE.get(cls, 'TBD')})"
            gender = p.get("gender") or "TBD"
            spec = p.get("primary_spec") or "TBD"
            role = p.get("primary_role") or "?"
            planned.append((name, race, gender, cls, spec, role, p.get("motivations", [])))
            print(f"  [PLANNED] {name:<12} race={race:<20} gender={gender:<6} {cls}/{spec} ({role})")

    if args.apply and sql_lines:
        run_sql("\n".join(sql_lines) + "\n")
        print(f"\nApplied {len(sql_lines)} weight rows to acore_characters.companion_weights (source='hearthcore').")
    else:
        print(f"\nDry-run: {len(sql_lines)} weight rows ready for active companions (pass --apply to write).")

    print(f"\nPlanned characters not yet created in Hearthguard ({len(planned)}):")
    for name, race, gender, cls, spec, role, mots in planned:
        gaps = []
        if "TBD" in race or "default" in race:
            gaps.append(f"race={race}")
        if gender == "TBD":
            gaps.append("gender")
        if spec == "TBD":
            gaps.append("spec")
        gap_txt = f"  [needs: {', '.join(gaps)}]" if gaps else ""
        print(f"  - {name}: {race} {gender} {cls} ({spec}, {role}) motivations={','.join(mots)}{gap_txt}")


if __name__ == "__main__":
    main()
