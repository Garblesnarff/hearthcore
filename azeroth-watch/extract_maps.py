#!/usr/bin/env python3
"""Extract WotLK 3.3.5a WorldMapArea.dbc bounding boxes & fully explored stitched map JPEGs."""

import collections
import io
import json
import math
import os
import struct
import mpyq
from PIL import Image

DATA = "/mnt/hdd/WoW335a/extracted/ChromieCraft_3.3.5a/Data/enUS"
OUT_MAPS = "/home/wonky/azeroth-watch/static/maps"
OUT_JSON = "/home/wonky/azeroth-watch/worldmap_areas.json"
os.makedirs(OUT_MAPS, exist_ok=True)


def parse_dbc_strings(raw: bytes):
    magic, records, fields, rec_size, str_size = struct.unpack("<4sIIII", raw[:20])
    assert magic == b"WDBC", f"Unexpected DBC magic: {magic}"
    rec_start = 20
    str_start = rec_start + records * rec_size
    str_block = raw[str_start : str_start + str_size]

    def get_str(offset: int) -> str:
        if offset <= 0 or offset >= len(str_block):
            return ""
        end = str_block.find(b"\x00", offset)
        if end < 0:
            end = len(str_block)
        return str_block[offset:end].decode("utf-8", "ignore")

    return records, rec_size, rec_start, get_str


def parse_worldmaparea_dbc(raw: bytes):
    records, rec_size, rec_start, get_str = parse_dbc_strings(raw)
    rows = []
    for i in range(records):
        chunk = raw[rec_start + i * rec_size : rec_start + (i + 1) * rec_size]
        vals = struct.unpack("<iiiIffffiii", chunk[:44])
        rows.append({
            "id": vals[0],
            "map_id": vals[1],
            "area_id": vals[2],
            "folder": get_str(vals[3]),
            "left": round(vals[4], 2),
            "right": round(vals[5], 2),
            "top": round(vals[6], 2),
            "bottom": round(vals[7], 2),
            "display_map_id": vals[8],
            "parent_world_map_id": vals[10],
        })
    return rows


def parse_worldmapoverlay_dbc(raw: bytes):
    records, rec_size, rec_start, get_str = parse_dbc_strings(raw)
    by_map_area = collections.defaultdict(list)
    for i in range(records):
        chunk = raw[rec_start + i * rec_size : rec_start + (i + 1) * rec_size]
        vals = struct.unpack("<" + "i" * 8 + "I" + "i" * 8, chunk[:68])
        tex = get_str(vals[8])
        if tex and vals[9] > 0 and vals[10] > 0:
            by_map_area[vals[1]].append({
                "tex": tex,
                "w": vals[9],
                "h": vals[10],
                "ox": vals[11],
                "oy": vals[12],
            })
    return by_map_area


def stitch_map(
    arc: mpyq.MPQArchive,
    file_index: dict,
    folder: str,
    out_path: str,
    overlays: list,
    width: int = 512,
    height: int = 342,
) -> bool:
    f_low = folder.lower()
    tiles = []
    for idx in range(1, 13):
        key = f"interface\\worldmap\\{f_low}\\{f_low}{idx}.blp"
        real_path = file_index.get(key)
        if not real_path:
            return False
        raw = arc.read_file(real_path)
        if not raw:
            return False
        tiles.append(Image.open(io.BytesIO(raw)).convert("RGBA"))

    canvas = Image.new("RGBA", (1024, 768))
    for idx, im in enumerate(tiles):
        canvas.paste(im, ((idx % 4) * 256, (idx // 4) * 256))

    # Alpha-composite all explored WorldMapOverlay.dbc textures (roads, towns, zone labels!)
    for ov in overlays:
        t_low = ov["tex"].lower()
        cols = max(1, math.ceil(ov["w"] / 256.0))
        rows = max(1, math.ceil(ov["h"] / 256.0))
        t_idx = 1
        for r in range(rows):
            for c in range(cols):
                key = f"interface\\worldmap\\{f_low}\\{t_low}{t_idx}.blp"
                real_path = file_index.get(key)
                if real_path:
                    try:
                        raw = arc.read_file(real_path)
                        if raw:
                            ov_im = Image.open(io.BytesIO(raw)).convert("RGBA")
                            canvas.alpha_composite(ov_im, dest=(ov["ox"] + c * 256, ov["oy"] + r * 256))
                    except Exception:
                        pass
                t_idx += 1

    cropped = canvas.crop((0, 0, 1002, 668)).convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
    cropped.save(out_path, format="JPEG", quality=84, optimize=True)
    return True


def main():
    p2 = mpyq.MPQArchive(os.path.join(DATA, "patch-enUS-2.MPQ"), listfile=False)
    loc = mpyq.MPQArchive(os.path.join(DATA, "locale-enUS.MPQ"), listfile=True)

    dbc_raw = p2.read_file("DBFilesClient\\WorldMapArea.dbc") or loc.read_file("DBFilesClient\\WorldMapArea.dbc")
    ov_raw = p2.read_file("DBFilesClient\\WorldMapOverlay.dbc") or loc.read_file("DBFilesClient\\WorldMapOverlay.dbc")
    rows = parse_worldmaparea_dbc(dbc_raw)
    overlays_by_area = parse_worldmapoverlay_dbc(ov_raw)
    print(f"Parsed {len(rows)} WorldMapArea entries & overlays for {len(overlays_by_area)} areas")

    file_index = {}
    folder_case = {}
    for raw_name in loc.files or []:
        s = raw_name.decode("utf-8", "ignore")
        low = s.lower()
        if low.startswith("interface\\worldmap\\") and low.endswith(".blp"):
            file_index[low] = s
            parts = s.split("\\")
            if len(parts) >= 4 and low.endswith("1.blp"):
                folder_case[parts[2].lower()] = parts[2]

    by_area_id = {}
    continents = {}
    extracted = 0
    seen_folders = set()

    if "world" in folder_case:
        wf = folder_case["world"]
        if stitch_map(loc, file_index, wf, os.path.join(OUT_MAPS, "World.jpg"), []):
            extracted += 1
            seen_folders.add("world")

    for r in rows:
        if r["map_id"] not in (0, 1, 530, 571, -1):
            continue
        f_low = r["folder"].lower()
        if not f_low or f_low not in folder_case:
            continue
        real_folder = folder_case[f_low]
        out_file = f"{real_folder}.jpg"
        out_path = os.path.join(OUT_MAPS, out_file)
        if f_low not in seen_folders:
            ovs = overlays_by_area.get(r["id"], []) if r["area_id"] != 0 else []
            if stitch_map(loc, file_index, real_folder, out_path, ovs):
                extracted += 1
                seen_folders.add(f_low)
            else:
                continue

        entry = {
            "id": r["id"],
            "map_id": r["map_id"],
            "area_id": r["area_id"],
            "folder": real_folder,
            "img": f"/static/maps/{out_file}",
            "left": r["left"],
            "right": r["right"],
            "top": r["top"],
            "bottom": r["bottom"],
        }
        if r["area_id"] == 0:
            continents[str(r["map_id"])] = entry
        else:
            if r["left"] != r["right"] and r["top"] != r["bottom"]:
                by_area_id[str(r["area_id"])] = entry

    meta = {
        "world": {"img": "/static/maps/World.jpg"} if "world" in seen_folders else None,
        "continents": continents,
        "zones": by_area_id,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    total_kb = sum(
        os.path.getsize(os.path.join(OUT_MAPS, fn))
        for fn in os.listdir(OUT_MAPS)
        if fn.endswith(".jpg")
    ) / 1024.0
    print(f"Extracted {extracted} fully-explored WotLK maps ({total_kb:.1f} KB total)")


if __name__ == "__main__":
    main()
