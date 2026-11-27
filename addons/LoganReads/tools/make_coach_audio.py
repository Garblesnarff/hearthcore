#!/usr/bin/env python3
"""Make the LoganReads coach voice lines with Piper, then write the Lua manifest.

Usage (the voice MUST be the same Piper voice used for the quest audio in audio/):
  python3 make_coach_audio.py --voice /path/to/voice.onnx
  python3 make_coach_audio.py --list          # print the lines only, no audio

Writes:
  audio/coach/<name>.mp3                 one file per line
  LoganReads_Coach_Audio.lua             manifest: name -> true (read by the addon)
Needs: piper (command line, reads text on stdin) and ffmpeg.
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADDON = HERE.parent
OUT_DIR = ADDON / "audio" / "coach"
MANIFEST = ADDON / "LoganReads_Coach_Audio.lua"

# name -> text. L1 = key and name (level 1), L2 = name only (level 2).
LINES = {
    "L1_charge": "Press C! Charge!",
    "L2_charge": "Charge!",
    "L1_rend": "Press R! Rend!",
    "L2_rend": "Rend!",
    "L1_heroicstrike": "Press 1! Heroic Strike!",
    "L2_heroicstrike": "Heroic Strike!",
    "L1_battleshout": "Click Battle Shout!",
    "L2_battleshout": "Battle Shout!",
    "L1_thunderclap": "Press T! Thunder Clap!",
    "L2_thunderclap": "Thunder Clap!",
    "L1_victoryrush": "Press V! Victory Rush!",
    "L2_victoryrush": "Victory Rush!",
    "L1_hamstring": "Press H! Hamstring!",
    "L2_hamstring": "Hamstring!",
    "L1_overpower": "Press 2! Overpower!",
    "L2_overpower": "Overpower!",
    "L1_execute": "Press 4! Execute!",
    "L2_execute": "Execute!",
    "L1_mortalstrike": "Press 5! Mortal Strike!",
    "L2_mortalstrike": "Mortal Strike!",
    "L1_bladestorm": "Press 6! Bladestorm!",
    "L2_bladestorm": "Bladestorm!",
    "tab": "Press Tab!",
    "eat": "Eat!",
    "closer": "Move closer!",
    "stance": "Battle Stance!",
    "newpower": "New power!",
    "praise1": "Good!",
    "praise2": "Nice!",
    "praise3": "Great job!",
}


def run(cmd, **kw):
    subprocess.run(cmd, check=True, **kw)


def make_one(piper, voice, name, text, tmp):
    wav = Path(tmp) / (name + ".wav")
    run([piper, "--model", str(voice), "--output_file", str(wav)],
        input=text.encode("utf-8"), stdout=subprocess.DEVNULL)
    mp3 = OUT_DIR / (name + ".mp3")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-codec:a", "libmp3lame",
         "-q:a", "4", str(mp3)])
    return mp3


def write_manifest(names):
    lines = ["-- Auto-generated manifest of coach audio files. Written by tools/make_coach_audio.py. Do not edit by hand.",
             "LoganReads_Coach_Audio = {"]
    for n in sorted(names):
        lines.append('\t["%s"] = true,' % n)
    lines.append("}")
    MANIFEST.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", help="path to the Piper .onnx voice (same voice as the quest audio)")
    ap.add_argument("--piper", default=shutil.which("piper") or "piper")
    ap.add_argument("--list", action="store_true", help="print the lines and exit")
    args = ap.parse_args()

    if args.list or not args.voice:
        for name, text in LINES.items():
            print(f"{name:18} {text}")
        if not args.voice and not args.list:
            print("\nno --voice given: nothing generated", file=sys.stderr)
            return 2
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    made = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in LINES.items():
            make_one(args.piper, args.voice, name, text, tmp)
            made.append(name)
            print("made", name)
    write_manifest(made)
    print("manifest written:", MANIFEST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
