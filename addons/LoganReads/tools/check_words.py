#!/usr/bin/env python3
"""LoganReads word checker.
Reads allowed_words.txt and names.txt, then checks every kid text field
in LoganReads_Data.lua. Fails (exit 1) and lists any word that is in
neither file. Comments (-- ...) and Lua syntax are ignored.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

def word_files():
    allowed = set()
    for name in ("allowed_words.txt", "names.txt"):
        p = HERE / name
        for line in p.read_text(encoding="utf-8").splitlines():
            w = line.strip()
            if w and not w.startswith("#"):
                for piece in w.split():
                    allowed.add(piece.casefold())
    return allowed

def kid_fields():
    src = (HERE / "LoganReads_Data.lua").read_text(encoding="utf-8")
    # strip Lua line comments
    src = re.sub(r"^\s*--.*$", "", src, flags=re.MULTILINE)
    fields = {}
    for key in ("title", "todo", "story", "notDone", "done"):
        fields[key] = re.findall(rf'\b{key}\s*=\s*"([^"]*)"', src)
    return fields

def main():
    allowed = word_files()
    fields = kid_fields()
    unknown = {}
    for key, values in fields.items():
        for text in values:
            for word in re.findall(r"[A-Za-z']+", text):
                if word.casefold() not in allowed:
                    bare = re.sub(r"'s$", "", word, flags=re.I).casefold()
                    if bare not in allowed:
                        unknown.setdefault(word, key)
    if unknown:
        print("FAIL - words in neither list:")
        for w, k in sorted(unknown.items()):
            print(f"  {w}  (in field: {k})")
        return 1
    total = sum(len(v) for v in fields.values())
    print(f"PASS - all words in all {total} fields are in allowed_words.txt or names.txt")
    return 0

if __name__ == "__main__":
    sys.exit(main())
