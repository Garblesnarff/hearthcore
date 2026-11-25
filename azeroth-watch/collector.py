"""Lightweight read-only collector for Azeroth Watch.

Zero impact on ac-worldserver:
- Persistent read-only MySQL pipe via `docker exec -i ac-database` (no password files, no per-poll fork).
- Incremental `seek()` log tailing for Server.log, Errors.log, Playerbots.log.
- Pure `/proc` host/process vitals (no `docker stats` subprocesses).
"""

from __future__ import annotations

import collections
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from data_maps import (
    ALL_TRACKED_SKILLS,
    CLASSES,
    COMPANION_ROLES,
    ITEM_QUALITY_COLORS,
    MAT_CATEGORY_ORDER,
    PRIMARY_SKILLS,
    RACES,
    SECONDARY_SKILLS,
    SHIFT_SCHEDULE,
    categorize_item,
    rep_standing_info,
    zone_name,
)

LOG_DIR = "/home/wonky/AzerothCore/azerothcore-wotlk/env/dist/logs"
SERVER_LOG = os.path.join(LOG_DIR, "Server.log")
ERRORS_LOG = os.path.join(LOG_DIR, "Errors.log")
PLAYERBOTS_LOG = os.path.join(LOG_DIR, "Playerbots.log")
LIVE_POS_FILE = os.path.join(LOG_DIR, "live_positions.json")
AI_PLANS_FILE = "/home/wonky/hearthcore/runtime/active_plans.json"
AI_QM_FILE = "/home/wonky/hearthcore/runtime/quartermaster.json"
AI_BRAIN_DB = "/home/wonky/hearthcore/runtime/brain.db"
AI_ENV_FILE = "/home/wonky/hearthcore/.env"
WORLDMAP_META_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worldmap_areas.json")
HEARTHGUARD_GUILD_ID = 6

# Exact calibrated landmass mappings from each Continent map's (u%, v%) to World.jpg (u%, v%),
# verified against Stormwind, Ironforge, Undercity, Booty Bay, Darnassus, Orgrimmar, Gadgetzan, and Dalaran.
CONTINENT_META = {
    "0": {
        "label": "Eastern Kingdoms",
        # Azeroth.jpg landmass spans u=[37.5..61.0], v=[3.5..95.0] -> World.jpg u=[70.5..90.5], v=[14.0..92.0]
        "src_box": (37.5, 61.0, 3.5, 95.0),
        "world_box": (70.5, 90.5, 14.0, 92.0),
    },
    "1": {
        "label": "Kalimdor",
        # Kalimdor.jpg landmass spans u=[37.0..67.5], v=[5.0..95.0] -> World.jpg u=[10.5..34.0], v=[22.5..91.0]
        "src_box": (37.0, 67.5, 5.0, 95.0),
        "world_box": (10.5, 34.0, 22.5, 91.0),
    },
    "571": {
        "label": "Northrend",
        "src_box": (14.0, 86.0, 10.0, 88.0),
        "world_box": (36.5, 66.0, 4.0, 35.5),
    },
    "530": {
        "label": "Outland",
        "src_box": (0.0, 100.0, 0.0, 100.0),
        "world_box": (4.0, 12.0, 30.0, 45.0),
    },
}
# Zones on map 530 that canonically sit off NW Kalimdor on World.jpg & Kalimdor.jpg
AZUREMYST_ZONES = {3524, 3525, 3557}
# Zones on map 530 that canonically sit in NE Eastern Kingdoms on World.jpg
QUELTHALAS_ZONES = {3430, 3433, 3487, 4080}


class PersistentMySQLPipe:
    """Single persistent read-only mysql CLI process inside ac-database."""

    SENTINEL = "__AW_END_OF_QUERY__"

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def _ensure(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        self._close()
        try:
            self._proc = subprocess.Popen(
                [
                    "docker",
                    "exec",
                    "-i",
                    "ac-database",
                    "sh",
                    "-c",
                    'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" --batch --raw --quick --unbuffered --skip-column-names 2>/dev/null',
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            return True
        except Exception:
            self._proc = None
            return False

    def _close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def query(self, sql: str) -> List[List[str]]:
        """Execute a single SELECT query and return rows as list of strings."""
        with self._lock:
            for _attempt in range(2):
                if not self._ensure():
                    return []
                assert self._proc and self._proc.stdin and self._proc.stdout
                clean_sql = " ".join(sql.strip().splitlines()).rstrip(";")
                payload = f"{clean_sql};\nSELECT '{self.SENTINEL}';\n"
                try:
                    self._proc.stdin.write(payload)
                    self._proc.stdin.flush()
                    rows: List[List[str]] = []
                    while True:
                        line = self._proc.stdout.readline()
                        if not line:
                            raise BrokenPipeError("mysql pipe EOF")
                        stripped = line.rstrip("\r\n")
                        if stripped == self.SENTINEL:
                            return rows
                        rows.append(stripped.split("\t"))
                except Exception:
                    self._close()
            return []


class LogMonitor:
    """Incremental seek()-based log tailer for worldserver health & live bot intent."""

    DIFF_RE = re.compile(r"Update time diff:\s*(\d+)")
    BOOT_RE = re.compile(r"WORLD: World Initialized In (\d+) Minutes (\d+) Seconds")
    SLOW_RE = re.compile(r"\[Slow(?:Bot|TickBot|Duty|DoNext)\]")

    # Companion activity patterns from Playerbots.log
    ACT_PATTERNS = [
        (re.compile(r"Companion (\w+): craft session start city=([^\s]+)"), lambda m: ("Crafting", f"Crafting in {m.group(2).replace('_', ' ')}", 0, 0)),
        (re.compile(r"Companion (\w+): gather-farm start item=(\d+) skill=(\d+) have=(\d+)/(\d+)"), lambda m: ("Gathering", f"Farming mats ({m.group(4)}/{m.group(5)})", int(m.group(2)), int(m.group(5)))),
        (re.compile(r"Companion (\w+): gather-farm progress item=(\d+) have=(\d+)/(\d+)"), lambda m: ("Gathering", f"Farming mats ({m.group(3)}/{m.group(4)})", int(m.group(2)), int(m.group(4)))),
        (re.compile(r"Companion (\w+): trip start kind=(\w+) id=(\d+) .* by=(\w+) dist=(\d+)"), lambda m: ("Traveling", f"{m.group(2).title()} ({m.group(4)}, {m.group(5)} yd)", 0, 0)),
        (re.compile(r"Companion (\w+): do quest target quest=(\d+) .* dist=([\d.]+)"), lambda m: ("Questing", f"Quest objective ({int(float(m.group(3)))} yd)", 0, 0)),
        (re.compile(r"Companion (\w+): quest pick (turnin|workhere|work|grind)(?:=(\d+))?"), lambda m: ("Questing", "Turning in quest" if m.group(1) == "turnin" else ("Grinding mobs" if m.group(1) == "grind" else "Questing"), 0, 0)),
        (re.compile(r"\[Companion\] (\w+) duty decision: start (\w+)"), lambda m: (m.group(2).title(), f"Duty: {m.group(2)}", 0, 0)),
        (re.compile(r"Companion (\w+): lease start owner=(\w+):(\S+)"), lambda m: (m.group(2).title(), f"{m.group(2).title()}: {m.group(3)}", 0, 0)),
        (re.compile(r"Companion (\w+): lease paused-tick .* by=death dead=1"), lambda m: ("Dead", "Corpse run / reviving", 0, 0)),
        (re.compile(r"\[Companion\] (\w+): schedule shift ended"), lambda m: ("Offline", "Shift ended", 0, 0)),
    ]

    def __init__(self) -> None:
        self._server_pos = 0
        self._server_inode: Optional[int] = None
        self._pbot_pos = 0
        self._pbot_inode: Optional[int] = None

        self.tick_diffs: collections.deque = collections.deque(maxlen=60)
        self.boot_ts: Optional[float] = None
        self.boot_duration_s: Optional[int] = None
        self.slow_warnings_since_boot = 0
        self.bot_activity: Dict[str, Dict[str, Any]] = {}
        self.errors_bytes = 0
        self.last_error = ""

        self._prime_initial()

    def _prime_initial(self) -> None:
        """Read tail of existing logs once on startup without loading multi-GB files."""
        self._read_tail(SERVER_LOG, 256 * 1024, self._process_server_line, is_server=True)
        self._read_tail(PLAYERBOTS_LOG, 512 * 1024, self._process_pbot_line, is_server=False)
        self._check_errors()

    def _read_tail(self, path: str, max_bytes: int, handler, is_server: bool) -> None:
        try:
            st = os.stat(path)
            start = max(0, st.st_size - max_bytes)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                if start > 0:
                    f.seek(start)
                    f.readline()  # align to next full line
                for line in f:
                    handler(line, priming=True)
                pos = f.tell()
            if is_server:
                self._server_inode = st.st_ino
                self._server_pos = pos
                if self.boot_ts is None:
                    self.boot_ts = st.st_ctime
            else:
                self._pbot_inode = st.st_ino
                self._pbot_pos = pos
        except FileNotFoundError:
            pass

    def poll(self) -> None:
        self._poll_file(SERVER_LOG, True)
        self._poll_file(PLAYERBOTS_LOG, False)
        self._check_errors()

    def _poll_file(self, path: str, is_server: bool) -> None:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return
        cur_inode = self._server_inode if is_server else self._pbot_inode
        cur_pos = self._server_pos if is_server else self._pbot_pos
        if cur_inode != st.st_ino or st.st_size < cur_pos:
            cur_pos = 0
            if is_server:
                self.tick_diffs.clear()
                self.slow_warnings_since_boot = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(cur_pos)
                # Cap bytes read per tick so we stay ultra-light even during bursts
                chunk = f.read(256 * 1024)
                new_pos = f.tell()
            if chunk:
                for line in chunk.splitlines():
                    if is_server:
                        self._process_server_line(line, priming=False)
                    else:
                        self._process_pbot_line(line, priming=False)
            if is_server:
                self._server_inode = st.st_ino
                self._server_pos = new_pos
            else:
                self._pbot_inode = st.st_ino
                self._pbot_pos = new_pos
        except Exception:
            pass

    def _process_server_line(self, line: str, priming: bool = False) -> None:
        m_boot = self.BOOT_RE.search(line)
        if m_boot:
            self.boot_duration_s = int(m_boot.group(1)) * 60 + int(m_boot.group(2))
            self.tick_diffs.clear()
            self.slow_warnings_since_boot = 0
            if not priming:
                self.boot_ts = time.time()
            return
        m_diff = self.DIFF_RE.search(line)
        if m_diff:
            self.tick_diffs.append({"t": int(time.time()), "ms": int(m_diff.group(1))})
        if self.SLOW_RE.search(line) and not priming:
            self.slow_warnings_since_boot += 1

    def _process_pbot_line(self, line: str, priming: bool = False) -> None:
        if self.SLOW_RE.search(line) and not priming:
            self.slow_warnings_since_boot += 1
            return
        ts_str = line[:19] if len(line) >= 19 and line[4] == "-" else ""
        for pat, fmt in self.ACT_PATTERNS:
            m = pat.search(line)
            if m:
                bot = m.group(1)
                tag, detail, item_id, target_cnt = fmt(m)
                self.bot_activity[bot] = {
                    "tag": tag,
                    "detail": detail,
                    "item_id": item_id,
                    "target_cnt": target_cnt,
                    "at": ts_str or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                break

    def _check_errors(self) -> None:
        try:
            st = os.stat(ERRORS_LOG)
            self.errors_bytes = st.st_size
            if st.st_size > 0:
                with open(ERRORS_LOG, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(max(0, st.st_size - 1024))
                    lines = [l.strip() for l in f.read().splitlines() if l.strip()]
                    self.last_error = lines[-1] if lines else ""
            else:
                self.last_error = ""
        except FileNotFoundError:
            self.errors_bytes = 0
            self.last_error = ""


class HostVitals:
    """Zero-subprocess host and worldserver/mysqld vitals via /proc."""

    def __init__(self) -> None:
        self._prev_cpu: Optional[Tuple[int, int]] = None
        self._prev_ws: Optional[Tuple[int, int, float]] = None
        self._clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])

    def sample(self) -> Dict[str, Any]:
        # Host CPU %
        cpu_pct = 0.0
        try:
            with open("/proc/stat", "r") as f:
                parts = [int(x) for x in f.readline().split()[1:]]
            idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
            total = sum(parts)
            if self._prev_cpu is not None:
                d_tot = total - self._prev_cpu[0]
                d_idle = idle - self._prev_cpu[1]
                if d_tot > 0:
                    cpu_pct = round(100.0 * (1.0 - d_idle / d_tot), 1)
            self._prev_cpu = (total, idle)
        except Exception:
            pass

        # Host RAM & Swap
        mem_total = mem_avail = swap_total = swap_free = 0
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    val_kb = int(v.strip().split()[0])
                    if k == "MemTotal":
                        mem_total = val_kb
                    elif k == "MemAvailable":
                        mem_avail = val_kb
                    elif k == "SwapTotal":
                        swap_total = val_kb
                    elif k == "SwapFree":
                        swap_free = val_kb
        except Exception:
            pass
        mem_used = max(0, mem_total - mem_avail)
        swap_used = max(0, swap_total - swap_free)

        # Disk free on /
        du = shutil.disk_usage("/")
        disk_pct = round(100.0 * du.used / max(1, du.total), 1)
        disk_free_gb = round(du.free / (1024 ** 3), 1)

        # Find worldserver & mysqld processes in /proc
        ws_rss_mib = 0.0
        ws_cpu_pct = 0.0
        ws_up = False
        ws_uptime_s = 0
        db_rss_mib = 0.0
        db_up = False
        auth_up = False

        try:
            with open("/proc/uptime", "r") as f:
                host_uptime = float(f.read().split()[0])
        except Exception:
            host_uptime = 0.0

        try:
            for pid_str in os.listdir("/proc"):
                if not pid_str.isdigit():
                    continue
                try:
                    with open(f"/proc/{pid_str}/comm", "r") as f:
                        comm = f.read().strip()
                    if comm not in ("worldserver", "mysqld", "authserver"):
                        continue
                    with open(f"/proc/{pid_str}/stat", "r") as f:
                        raw = f.read()
                    rparen = raw.rfind(")")
                    fields = raw[rparen + 2 :].split()
                    rss_pages = int(fields[21])
                    rss_mib = round((rss_pages * 4096) / (1024 * 1024), 1)
                    if comm == "worldserver":
                        ws_up = True
                        ws_rss_mib = rss_mib
                        utime = int(fields[11])
                        stime = int(fields[12])
                        start_ticks = int(fields[19])
                        ws_uptime_s = max(0, int(host_uptime - (start_ticks / self._clk_tck)))
                        now = time.monotonic()
                        pid_int = int(pid_str)
                        if self._prev_ws is not None and self._prev_ws[0] == pid_int:
                            dt = now - self._prev_ws[2]
                            if dt > 0.2:
                                d_ticks = (utime + stime) - self._prev_ws[1]
                                ws_cpu_pct = round(100.0 * (d_ticks / self._clk_tck) / dt, 1)
                        self._prev_ws = (pid_int, utime + stime, now)
                    elif comm == "mysqld":
                        db_up = True
                        db_rss_mib = rss_mib
                    elif comm == "authserver":
                        auth_up = True
                except Exception:
                    continue
        except Exception:
            pass

        return {
            "host_cpu_pct": cpu_pct,
            "mem_used_gib": round(mem_used / (1024 * 1024), 2),
            "mem_total_gib": round(mem_total / (1024 * 1024), 2),
            "mem_avail_gib": round(mem_avail / (1024 * 1024), 2),
            "mem_pct": round(100.0 * mem_used / max(1, mem_total), 1),
            "swap_used_gib": round(swap_used / (1024 * 1024), 2),
            "disk_pct": disk_pct,
            "disk_free_gb": disk_free_gb,
            "worldserver_up": ws_up,
            "worldserver_rss_mib": ws_rss_mib,
            "worldserver_cpu_pct": ws_cpu_pct,
            "worldserver_uptime_s": ws_uptime_s,
            "database_up": db_up,
            "database_rss_mib": db_rss_mib,
            "authserver_up": auth_up,
        }


class AzerothCollector:
    """Background polling loop assembling the full read-only world snapshot."""

    def __init__(self, poll_interval_s: float = 4.0) -> None:
        self.poll_interval_s = poll_interval_s
        self.db = PersistentMySQLPipe()
        self.logs = LogMonitor()
        self.vitals = HostVitals()

        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {}
        self._version = 0
        self._xp_table: Dict[int, int] = {}
        self._map_meta: Dict[str, Any] = {}
        try:
            with open(WORLDMAP_META_FILE, "r", encoding="utf-8") as f:
                self._map_meta = json.load(f)
        except Exception:
            self._map_meta = {"world": None, "continents": {}, "zones": {}}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="collector", daemon=True)

    def start(self) -> None:
        self.refresh_once()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.db._close()

    def get_snapshot(self) -> Tuple[int, Dict[str, Any]]:
        with self._lock:
            return self._version, self._state

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.refresh_once()
            except Exception as exc:
                with self._lock:
                    if isinstance(self._state, dict):
                        self._state["collector_error"] = str(exc)
            elapsed = time.monotonic() - t0
            wait_s = max(0.5, self.poll_interval_s - elapsed)
            self._stop.wait(wait_s)

    def refresh_once(self) -> None:
        self.logs.poll()
        vitals = self.vitals.sample()
        now = datetime.datetime.now()

        # 0. Load player_xp_for_level once (cached in memory)
        if not self._xp_table:
            xp_rows = self.db.query("SELECT level, Experience FROM acore_world.player_xp_for_level")
            self._xp_table = {int(r[0]): int(r[1]) for r in xp_rows if len(r) >= 2}

        # 1. Realmlist status
        realm_rows = self.db.query(
            "SELECT name, address, port, flag FROM acore_auth.realmlist LIMIT 1"
        )
        realm = {
            "name": realm_rows[0][0] if realm_rows else "AzerothCore",
            "address": realm_rows[0][1] if realm_rows else "192.168.1.3",
            "port": int(realm_rows[0][2]) if realm_rows else 8085,
            "flag": int(realm_rows[0][3]) if realm_rows else 2,
        }

        # 2. Raid Gate stage
        rg_rows = self.db.query(
            "SELECT stage, unlocked_at, killed_by FROM acore_characters.raid_gate ORDER BY stage DESC LIMIT 1"
        )
        raid_stage = int(rg_rows[0][0]) if rg_rows else 0
        raid_caps = {0: 60, 1: 70, 2: 80}
        raid_cap = raid_caps.get(raid_stage, 60)
        prof_cap = {0: 300, 1: 375, 2: 450}.get(raid_stage, 300)

        # 0b. Read optional 3-second real-time position snapshot if fresh (< 20s old)
        live_pos: Dict[int, Dict[str, Any]] = {}
        weekly_event = ""
        try:
            st = os.stat(LIVE_POS_FILE)
            if time.time() - st.st_mtime < 20.0:
                with open(LIVE_POS_FILE, "r", encoding="utf-8") as f:
                    raw_lp = json.load(f)
                weekly_event = str(raw_lp.get("weekly_event") or "")
                for row in raw_lp.get("units", []):
                    live_pos[int(row["g"])] = row
        except Exception:
            pass

        # 0c. Read optional Hearthcore AI Brain active session plans + SQLite audit + credit balance
        ai_brain: Dict[str, Any] = {
            "model": "",
            "effort": "medium",
            "daily_cap": 500,
            "today": {},
            "active_list": [],
            "history": [],
            "est_cost_usd": 0.0,
            "quartermaster": None,
            "balance": {
                "credit_total_usd": 98.0,
                "spent_total_usd": 0.0,
                "spent_today_usd": 0.0,
                "remaining_usd": 98.0,
                "daily_burn_usd": 0.0,
                "reset_date": "2026-12-02",
                "days_left": 0,
            },
            "all_time": {
                "plans": 0,
                "levels": 0,
                "skillups": 0,
                "orders": 0,
                "qm_runs": 0,
            },
            "model_stats": [],
            "budget": {},
            "memory": {
                "open_intentions": {},
                "completed_intentions": [],
                "operational_alerts": [],
                "active_reservations": {},
                "fatigue_count": 0,
            },
        }
        ai_plans_by_slug: Dict[str, Dict[str, Any]] = {}

        def _cost_usd(model_name: str, in_t: int, out_t: int) -> float:
            ml = (model_name or "").lower()
            if "opus" in ml:
                ir, or_ = 15.0, 75.0
            elif "sonnet" in ml:
                ir, or_ = 3.0, 15.0
            else:
                ir, or_ = 1.0, 5.0
            return (in_t * ir + out_t * or_) / 1_000_000.0

        # Read ONLY non-secret display config from hearthcore/.env (never touches ANTHROPIC_API_KEY)
        env_pub: Dict[str, str] = {}
        try:
            if os.path.isfile(AI_ENV_FILE):
                with open(AI_ENV_FILE, "r", encoding="utf-8") as ef:
                    for raw_line in ef:
                        ln = raw_line.strip()
                        if not ln or ln.startswith("#") or "=" not in ln:
                            continue
                        ek, ev = ln.split("=", 1)
                        ek = ek.strip()
                        if ek in ("ANTHROPIC_MODEL", "ANTHROPIC_EFFORT", "HEARTHCORE_DAILY_CALL_CAP",
                                  "HEARTHCORE_CREDIT_USD", "HEARTHCORE_CREDIT_RESET"):
                            env_pub[ek] = ev.strip().strip('"').strip("'")
        except Exception:
            pass

        credit_total = float(env_pub.get("HEARTHCORE_CREDIT_USD", "98.0") or 98.0)
        reset_str = env_pub.get("HEARTHCORE_CREDIT_RESET", "2026-12-02") or "2026-12-02"
        days_left = 0
        try:
            rdt = datetime.date.fromisoformat(reset_str)
            days_left = max(0, (rdt - datetime.date.today()).days)
        except Exception:
            pass

        try:
            if os.path.isfile(AI_PLANS_FILE):
                with open(AI_PLANS_FILE, "r", encoding="utf-8") as f:
                    raw_ai = json.load(f)
                ai_brain["model"] = env_pub.get("ANTHROPIC_MODEL") or str(raw_ai.get("model") or "")
                ai_brain["effort"] = env_pub.get("ANTHROPIC_EFFORT") or str(raw_ai.get("effort") or "medium")
                ai_brain["daily_cap"] = int(env_pub.get("HEARTHCORE_DAILY_CALL_CAP") or raw_ai.get("daily_cap") or 500)
                ai_brain["today"] = raw_ai.get("today", {}) or {}
                ai_plans_by_slug = raw_ai.get("plans", {}) or {}
                if isinstance(raw_ai.get("memory"), dict):
                    ai_brain["memory"] = raw_ai["memory"]
                if isinstance(raw_ai.get("budget"), dict):
                    ai_brain["budget"] = raw_ai["budget"]
            if os.path.isfile(AI_QM_FILE):
                with open(AI_QM_FILE, "r", encoding="utf-8") as f:
                    ai_brain["quartermaster"] = json.load(f)
            if os.path.isfile(AI_BRAIN_DB):
                now_epoch = int(time.time())
                today_key = now.strftime("%Y-%m-%d")
                conn = sqlite3.connect(f"file:{AI_BRAIN_DB}?mode=ro", uri=True, timeout=1.0)
                conn.row_factory = sqlite3.Row
                try:
                    # Live today totals directly from SQLite (always fresh even between ticks)
                    pcols = {r[1] for r in conn.execute("PRAGMA table_info(plans)").fetchall()}
                    has_out = "outcome_levels" in pcols
                    out_sel = (
                        "COALESCE(SUM(outcome_levels),0) AS ol, COALESCE(SUM(outcome_skillups),0) AS os, COALESCE(SUM(outcome_orders),0) AS oo"
                        if has_out else "0 AS ol, 0 AS os, 0 AS oo"
                    )
                    day_r = conn.execute(f"""
                        SELECT COUNT(*) AS total,
                               SUM(CASE WHEN source='anthropic' THEN 1 ELSE 0 END) AS ai_ok,
                               SUM(CASE WHEN source='fallback_after_api' THEN 1 ELSE 0 END) AS ai_err,
                               SUM(CASE WHEN source='fallback' THEN 1 ELSE 0 END) AS fb,
                               COALESCE(SUM(input_tokens), 0) AS in_tok,
                               COALESCE(SUM(output_tokens), 0) AS out_tok,
                               COALESCE(AVG(CASE WHEN source='anthropic' THEN latency_ms END), 0) AS avg_ms,
                               {out_sel}
                        FROM plans WHERE day_key = ?
                    """, (today_key,)).fetchone()
                    ai_brain["today"] = {
                        "total": int(day_r["total"] or 0),
                        "ai_ok": int(day_r["ai_ok"] or 0),
                        "ai_err": int(day_r["ai_err"] or 0),
                        "fallback": int(day_r["fb"] or 0),
                        "input_tokens": int(day_r["in_tok"] or 0),
                        "output_tokens": int(day_r["out_tok"] or 0),
                        "avg_latency_ms": int(day_r["avg_ms"] or 0),
                        "outcome_levels": int(day_r["ol"] or 0),
                        "outcome_skillups": int(day_r["os"] or 0),
                        "outcome_orders": int(day_r["oo"] or 0),
                    }

                    # All-time model scorecard & exact spend across plans + quartermaster_runs
                    spent_total = 0.0
                    spent_today = 0.0
                    model_map: Dict[str, Dict[str, Any]] = {}
                    m_rows = conn.execute(f"""
                        SELECT model,
                               COUNT(*) AS cnt,
                               SUM(CASE WHEN source='anthropic' THEN 1 ELSE 0 END) AS ok_cnt,
                               COALESCE(SUM(input_tokens), 0) AS in_t,
                               COALESCE(SUM(output_tokens), 0) AS out_t,
                               COALESCE(AVG(CASE WHEN source='anthropic' THEN latency_ms END), 0) AS avg_ms,
                               {out_sel}
                        FROM plans
                        WHERE source IN ('anthropic', 'fallback_after_api')
                        GROUP BY model
                    """).fetchall()
                    all_plans = 0
                    all_lvls = 0
                    all_sks = 0
                    all_ords = 0
                    for mr in m_rows:
                        mname = mr["model"] or "unknown"
                        c_usd = _cost_usd(mname, int(mr["in_t"]), int(mr["out_t"]))
                        spent_total += c_usd
                        cnt = int(mr["cnt"] or 0)
                        okc = int(mr["ok_cnt"] or 0)
                        all_plans += cnt
                        all_lvls += int(mr["ol"] or 0)
                        all_sks += int(mr["os"] or 0)
                        all_ords += int(mr["oo"] or 0)
                        model_map[mname] = {
                            "model": mname,
                            "role": "Companion Planner",
                            "calls": cnt,
                            "ok_pct": round(100.0 * okc / max(1, cnt), 1),
                            "avg_ms": int(mr["avg_ms"] or 0),
                            "levels": int(mr["ol"] or 0),
                            "skillups": int(mr["os"] or 0),
                            "orders": int(mr["oo"] or 0),
                            "cost_usd": round(c_usd, 4),
                        }

                    # Today's plan spend by model
                    for tr in conn.execute(
                        "SELECT model, COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) FROM plans WHERE day_key = ? GROUP BY model",
                        (today_key,),
                    ).fetchall():
                        spent_today += _cost_usd(tr[0], int(tr[1]), int(tr[2]))

                    # Quartermaster runs spend
                    qm_runs_cnt = 0
                    has_qm = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='quartermaster_runs'").fetchone()[0] > 0
                    if has_qm:
                        for qr in conn.execute("""
                            SELECT model, COUNT(*) AS cnt,
                                   COALESCE(SUM(input_tokens), 0) AS in_t,
                                   COALESCE(SUM(output_tokens), 0) AS out_t,
                                   COALESCE(AVG(latency_ms), 0) AS avg_ms
                            FROM quartermaster_runs WHERE source = 'anthropic' GROUP BY model
                        """).fetchall():
                            mname = qr["model"] or "claude-sonnet-5-5"
                            q_usd = _cost_usd(mname, int(qr["in_t"]), int(qr["out_t"]))
                            spent_total += q_usd
                            qm_runs_cnt += int(qr["cnt"] or 0)
                            key_qm = f"{mname} (Quartermaster)"
                            model_map[key_qm] = {
                                "model": mname,
                                "role": "Guild Quartermaster",
                                "calls": int(qr["cnt"] or 0),
                                "ok_pct": 100.0,
                                "avg_ms": int(qr["avg_ms"] or 0),
                                "levels": 0,
                                "skillups": 0,
                                "orders": 0,
                                "cost_usd": round(q_usd, 4),
                            }
                        today_start = int(datetime.datetime.combine(now.date(), datetime.time.min).timestamp())
                        for qtr in conn.execute(
                            "SELECT model, COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) FROM quartermaster_runs WHERE ran_at >= ? GROUP BY model",
                            (today_start,),
                        ).fetchall():
                            spent_today += _cost_usd(qtr[0], int(qtr[1]), int(qtr[2]))

                    days_active = conn.execute("SELECT COUNT(DISTINCT day_key) FROM plans").fetchone()[0] or 1
                    daily_burn = spent_total / max(1, int(days_active))

                    ai_brain["est_cost_usd"] = round(spent_today, 4)
                    ai_brain["balance"] = {
                        "credit_total_usd": round(credit_total, 2),
                        "spent_total_usd": round(spent_total, 4),
                        "spent_today_usd": round(spent_today, 4),
                        "remaining_usd": round(max(0.0, credit_total - spent_total), 2),
                        "daily_burn_usd": round(daily_burn, 3),
                        "reset_date": reset_str,
                        "days_left": days_left,
                    }
                    ai_brain["all_time"] = {
                        "plans": all_plans,
                        "levels": all_lvls,
                        "skillups": all_sks,
                        "orders": all_ords,
                        "qm_runs": qm_runs_cnt,
                    }
                    ai_brain["model_stats"] = list(model_map.values())

                    # Latest active/recent APPLIED live plan per companion (shadow plans excluded, Issue #7)
                    has_pstate = "plan_state" in pcols
                    live_where = "WHERE applied = 1 AND COALESCE(plan_state, 'applied') != 'shadow'" if has_pstate else "WHERE applied = 1"
                    lat_rows = conn.execute(f"""
                        SELECT p.* FROM plans p
                        INNER JOIN (
                            SELECT character_id, MAX(id) AS max_id FROM plans {live_where} GROUP BY character_id
                        ) m ON m.max_id = p.id
                        ORDER BY (p.expires_at > ?) DESC, p.planned_at DESC
                    """, (now_epoch,)).fetchall()
                    active_list = []
                    for r in lat_rows:
                        cpp_w = json.loads(r["cpp_weights"] or "{}")
                        top_w = sorted(
                            ((k, float(v)) for k, v in cpp_w.items() if abs(float(v) - 1.0) >= 0.08),
                            key=lambda kv: -abs(kv[1] - 1.0),
                        )[:5]
                        p_meta = ai_plans_by_slug.get(r["character_id"]) or {}
                        prop_status = (r["proposal_status"] if "proposal_status" in pcols else "") or p_meta.get("proposal_status") or "proposed"
                        advisory_only = bool(r["advisory_only"]) if "advisory_only" in pcols else bool(p_meta.get("advisory_only"))
                        active_list.append({
                            "character_id": r["character_id"],
                            "source": r["source"],
                            "model": r["model"],
                            "effort": r["effort"],
                            "primary_goal_id": r["primary_goal_id"],
                            "primary_desc": r["primary_desc"],
                            "secondary_goal_ids": json.loads(r["secondary_goal_ids"] or "[]"),
                            "reason_code": r["reason_code"],
                            "session_minutes": int(r["session_minutes"]),
                            "rem_minutes": max(0, (int(r["expires_at"]) - now_epoch) // 60),
                            "active": int(r["expires_at"]) > now_epoch,
                            "applied": bool(r["applied"]),
                            "proposal_status": prop_status,
                            "proposal_reason": (r["proposal_reason"] if "proposal_reason" in pcols else "") or p_meta.get("proposal_reason") or "",
                            "advisory_only": advisory_only,
                            "outcome_class": p_meta.get("outcome_class") or "",
                            "top_weights": [{"k": k, "v": round(v, 2)} for k, v in top_w],
                            "input_tokens": int(r["input_tokens"]),
                            "output_tokens": int(r["output_tokens"]),
                            "latency_ms": int(r["latency_ms"]),
                            "error_note": r["error_note"] or "",
                            "outcome_levels": int(r["outcome_levels"]) if has_out else 0,
                            "outcome_skillups": int(r["outcome_skillups"]) if has_out else 0,
                            "outcome_orders": int(r["outcome_orders"]) if has_out else 0,
                            "outcome_xp": int(r["outcome_xp"]) if "outcome_xp" in pcols else 0,
                            "outcome_quests": int(r["outcome_quests"]) if "outcome_quests" in pcols else 0,
                            "open_intentions": p_meta.get("open_intentions") or [],
                            "fatigue_category": p_meta.get("fatigue_category") or "",
                            "fatigue_streak": int(p_meta.get("fatigue_streak") or 0),
                            "stalled_goal_id": p_meta.get("stalled_goal_id") or "",
                        })
                    ai_brain["active_list"] = active_list

                    hist_rows = conn.execute("""
                        SELECT id, planned_at, character_id, source, model, effort,
                               primary_goal_id, primary_desc, reason_code, session_minutes,
                               input_tokens, output_tokens, latency_ms, applied, error_note
                        FROM plans ORDER BY id DESC LIMIT 30
                    """).fetchall()
                    ai_brain["history"] = [
                        {
                            "id": int(r["id"]),
                            "time_str": datetime.datetime.fromtimestamp(int(r["planned_at"])).strftime("%H:%M"),
                            "character_id": r["character_id"],
                            "source": r["source"],
                            "model": r["model"],
                            "effort": r["effort"],
                            "primary_goal_id": r["primary_goal_id"],
                            "primary_desc": r["primary_desc"],
                            "reason_code": r["reason_code"],
                            "session_minutes": int(r["session_minutes"]),
                            "input_tokens": int(r["input_tokens"]),
                            "output_tokens": int(r["output_tokens"]),
                            "cost_usd": round(_cost_usd(r["model"], int(r["input_tokens"]), int(r["output_tokens"])), 4),
                            "latency_ms": int(r["latency_ms"]),
                            "applied": bool(r["applied"]),
                            "error_note": r["error_note"] or "",
                        }
                        for r in hist_rows
                    ]
                finally:
                    conn.close()
        except Exception:
            pass

        # 3. Online human players (non-RNDBOT, non-companion)
        human_rows = self.db.query("""
            SELECT c.guid, c.name, c.level, c.class, c.race, c.gender, c.zone, c.money, c.xp, a.username,
                   c.map, c.position_x, c.position_y
            FROM acore_characters.characters c
            JOIN acore_auth.account a ON a.id = c.account
            WHERE c.online = 1
              AND a.username NOT LIKE 'RNDBOT%'
              AND c.name NOT IN (SELECT name FROM acore_characters.companion_schedule)
            ORDER BY c.level DESC, c.name
        """)
        humans = []
        for r in human_rows:
            guid = int(r[0])
            cls_id = int(r[3])
            race_id = int(r[4])
            gender = int(r[5])
            lvl = int(r[2])
            xp = int(r[8])
            xp_max = self._xp_table.get(lvl, 0)
            xp_pct = round(min(100.0, 100.0 * xp / xp_max), 1) if xp_max > 0 else 100.0
            lp = live_pos.get(guid)
            map_id = int(lp["m"]) if lp else int(r[10])
            pos_x = float(lp["x"]) if lp else float(r[11])
            pos_y = float(lp["y"]) if lp else float(r[12])
            zid = int(lp["z"]) if lp else int(r[6])
            cls = CLASSES.get(cls_id, {"name": "?", "short": "?", "color": "#fff"})
            humans.append({
                "guid": guid,
                "name": r[1],
                "level": lvl,
                "class_id": cls_id,
                "class_name": cls["name"],
                "class_color": cls["color"],
                "race_id": race_id,
                "gender": gender,
                "race": RACES.get(race_id, "?"),
                "zone_id": zid,
                "zone": zone_name(zid),
                "map_id": map_id,
                "x": pos_x,
                "y": pos_y,
                "gold": round(int(r[7]) / 10000.0, 1),
                "xp": xp,
                "xp_max": xp_max,
                "xp_pct": xp_pct,
                "account": r[9],
            })

        # 4. All 38 companions + schedule + profiles + live health, xp, position & bag usage
        comp_rows = self.db.query("""
            SELECT c.guid, c.name, c.level, c.class, c.race, c.gender, c.zone, c.online,
                   c.totaltime, c.money, c.health, c.xp,
                   s.shift_group, s.start_hour, s.end_hour, s.days_mask, s.always_online,
                   COALESCE(p.archetype, ''), COALESCE(p.culture, ''), COALESCE(p.bio, ''),
                   (SELECT COUNT(*) FROM acore_characters.character_inventory ci
                    WHERE ci.guid = c.guid AND ((ci.bag = 0 AND ci.slot BETWEEN 23 AND 38) OR ci.bag > 0)) AS bag_used,
                   (16 + COALESCE((SELECT SUM(it.ContainerSlots)
                    FROM acore_characters.character_inventory ci
                    JOIN acore_characters.item_instance ii ON ii.guid = ci.item
                    JOIN acore_world.item_template it ON it.entry = ii.itemEntry
                    WHERE ci.guid = c.guid AND ci.bag = 0 AND ci.slot BETWEEN 19 AND 22), 0)) AS bag_total,
                   c.map, c.position_x, c.position_y
            FROM acore_characters.companion_schedule s
            JOIN acore_characters.characters c ON c.name = s.name
            LEFT JOIN acore_characters.companion_profiles p ON p.guid = c.guid
            ORDER BY c.online DESC, s.shift_group, c.level DESC, c.name
        """)

        # Resolve live item name & bag count for any online companion currently gather-farming
        gather_pairs = []
        for r in comp_rows:
            g_id = int(r[0])
            is_on = (g_id in live_pos) if live_pos else (int(r[7]) == 1)
            if is_on:
                act_raw = self.logs.bot_activity.get(r[1])
                if act_raw and act_raw.get("item_id"):
                    gather_pairs.append((g_id, act_raw["item_id"]))
        gather_live: Dict[Tuple[int, int], Tuple[str, int]] = {}
        if gather_pairs:
            g_ids = ",".join(str(g) for g, _ in gather_pairs)
            i_ids = ",".join(str(i) for _, i in gather_pairs)
            g_rows = self.db.query(f"""
                SELECT ci.guid, ii.itemEntry, it.name, COALESCE(SUM(ii.count), 0)
                FROM acore_characters.character_inventory ci
                JOIN acore_characters.item_instance ii ON ii.guid = ci.item
                JOIN acore_world.item_template it ON it.entry = ii.itemEntry
                WHERE ci.guid IN ({g_ids}) AND ii.itemEntry IN ({i_ids})
                GROUP BY ci.guid, ii.itemEntry, it.name
            """)
            for gr in g_rows:
                gather_live[(int(gr[0]), int(gr[1]))] = (gr[2], int(gr[3]))
            # Also resolve item names even when count is 0
            n_rows = self.db.query(f"SELECT entry, name FROM acore_world.item_template WHERE entry IN ({i_ids})")
            item_names_map = {int(nr[0]): nr[1] for nr in n_rows}
        else:
            item_names_map = {}

        # 5. Companion skills
        skill_ids_csv = ",".join(str(k) for k in ALL_TRACKED_SKILLS.keys())
        skill_rows = self.db.query(f"""
            SELECT cs.guid, cs.skill, cs.value, cs.max
            FROM acore_characters.character_skills cs
            JOIN acore_characters.characters c ON c.guid = cs.guid
            JOIN acore_characters.companion_schedule s ON s.name = c.name
            WHERE cs.skill IN ({skill_ids_csv})
        """)
        skills_by_guid: Dict[int, List[Dict[str, Any]]] = collections.defaultdict(list)
        for r in skill_rows:
            g, sid, val, mx = int(r[0]), int(r[1]), int(r[2]), int(r[3])
            meta = ALL_TRACKED_SKILLS.get(sid)
            if not meta:
                continue
            skills_by_guid[g].append({
                "id": sid,
                "name": meta["name"],
                "short": meta["short"],
                "kind": meta["kind"],
                "value": val,
                "max": mx,
            })

        # Build companion records + profession coverage + raid readiness
        companions = []
        prof_coverage: Dict[int, Dict[str, Any]] = {
            sid: {
                "id": sid,
                "name": meta["name"],
                "short": meta["short"],
                "kind": meta["kind"],
                "cap": prof_cap,
                "max_value": 0,
                "practitioners": [],
            }
            for sid, meta in ALL_TRACKED_SKILLS.items()
        }
        roles = {"tank": 0, "healer": 0, "mdps": 0, "rdps": 0}
        brackets = {"ready": 0, "high": 0, "mid": 0, "low": 0}
        online_shifts = set()

        for r in comp_rows:
            guid = int(r[0])
            name = r[1]
            level = int(r[2])
            cls_id = int(r[3])
            race_id = int(r[4])
            gender = int(r[5])
            lp = live_pos.get(guid)
            # Authoritative online check: live_positions.json is written directly from ObjectAccessor::GetPlayers() every 3s
            online = (lp is not None) if live_pos else (int(r[7]) == 1)
            zid = int(lp["z"]) if lp else int(r[6])
            map_id = int(lp["m"]) if lp else int(r[22])
            pos_x = float(lp["x"]) if lp else float(r[23])
            pos_y = float(lp["y"]) if lp else float(r[24])
            hours = round(int(r[8]) / 3600.0, 1)
            gold = round(int(r[9]) / 10000.0, 1)
            health = int(lp["hp"]) if (lp and "hp" in lp) else int(r[10])
            xp = int(r[11])
            xp_max = self._xp_table.get(level, 0)
            xp_pct = round(min(100.0, 100.0 * xp / xp_max), 1) if xp_max > 0 else 100.0
            shift_group = r[12]
            bag_used = int(r[20])
            bag_total = max(16, int(r[21]))
            cls = CLASSES.get(cls_id, {"name": "?", "short": "?", "color": "#fff", "default_role": "mdps"})
            role = COMPANION_ROLES.get(name, cls["default_role"])
            roles[role] = roles.get(role, 0) + 1

            if level >= raid_cap:
                brackets["ready"] += 1
            elif level >= int(raid_cap * 0.65):
                brackets["high"] += 1
            elif level >= int(raid_cap * 0.30):
                brackets["mid"] += 1
            else:
                brackets["low"] += 1

            if online:
                online_shifts.add(shift_group)

            bot_skills = skills_by_guid.get(guid, [])
            primaries = sorted(
                [s for s in bot_skills if s["id"] in PRIMARY_SKILLS],
                key=lambda x: (-x["value"], x["name"]),
            )
            secondaries = sorted(
                [s for s in bot_skills if s["id"] in SECONDARY_SKILLS],
                key=lambda x: (-x["value"], x["name"]),
            )
            for s in bot_skills:
                pc = prof_coverage[s["id"]]
                pc["practitioners"].append({
                    "name": name,
                    "value": s["value"],
                    "max": s["max"],
                    "online": online,
                    "class_color": cls["color"],
                })
                if s["value"] > pc["max_value"]:
                    pc["max_value"] = s["value"]

            act = dict(self.logs.bot_activity[name]) if (online and name in self.logs.bot_activity) else None
            if act and act.get("item_id"):
                iid = act["item_id"]
                iname, icnt = gather_live.get((guid, iid), (item_names_map.get(iid, "mats"), 0))
                act["detail"] = f"{iname} ({icnt}/{act.get('target_cnt', 20)})"
            is_dead = online and (health <= 1 or (act is not None and act.get("tag") == "Dead"))
            companions.append({
                "guid": guid,
                "name": name,
                "level": level,
                "class_id": cls_id,
                "class_name": cls["name"],
                "class_short": cls["short"],
                "class_color": cls["color"],
                "race_id": race_id,
                "gender": gender,
                "race": RACES.get(race_id, "?"),
                "role": role,
                "zone_id": zid,
                "zone": zone_name(zid),
                "map_id": map_id,
                "x": pos_x,
                "y": pos_y,
                "online": online,
                "dead": is_dead,
                "health": health,
                "xp": xp,
                "xp_max": xp_max,
                "xp_pct": xp_pct,
                "bag_used": bag_used,
                "bag_total": bag_total,
                "hours": hours,
                "gold": gold,
                "shift_group": shift_group,
                "start_hour": int(r[13]),
                "end_hour": int(r[14]),
                "always_online": int(r[16]) == 1,
                "archetype": r[17],
                "culture": r[18],
                "bio": r[19],
                "primaries": primaries,
                "secondaries": secondaries,
                "activity": act or ({"tag": "Active", "detail": f"In {zone_name(zid)}", "at": ""} if online else None),
                "ai_plan": ai_plans_by_slug.get(name.lower()),
            })

        # Sort profession practitioners descending
        prof_list = []
        for sid, pc in prof_coverage.items():
            pc["practitioners"].sort(key=lambda x: (-x["value"], not x["online"], x["name"]))
            pc["leaders"] = pc["practitioners"][:3]
            pc["count"] = len(pc["practitioners"])
            pc["pct"] = round(min(100.0, 100.0 * pc["max_value"] / max(1, prof_cap)), 1)
            prof_list.append(pc)
        prof_list.sort(key=lambda x: (0 if x["kind"] == "craft" else (1 if x["kind"] == "gather" else 2), x["name"]))

        # 6. World random-bot zone distribution & positions for live maps
        rnd_rows = self.db.query("""
            SELECT c.guid, c.map, c.zone, c.position_x, c.position_y
            FROM acore_characters.characters c
            JOIN acore_auth.account a ON a.id = c.account
            WHERE c.online = 1 AND a.username LIKE 'RNDBOT%'
        """)
        zone_counts: Dict[int, int] = collections.defaultdict(int)
        random_units = []
        for r in rnd_rows:
            g = int(r[0])
            lp = live_pos.get(g)
            m = int(lp["m"]) if lp else int(r[1])
            z = int(lp["z"]) if lp else int(r[2])
            x = float(lp["x"]) if lp else float(r[3])
            y = float(lp["y"]) if lp else float(r[4])
            zone_counts[z] += 1
            random_units.append({"map_id": m, "zone_id": z, "x": x, "y": y})
        random_total = len(random_units)
        world_zones = [
            {"zone": zone_name(zid), "count": cnt}
            for zid, cnt in sorted(zone_counts.items(), key=lambda kv: -kv[1])[:12]
        ]

        # 6b. Build Rotating Live Map Slides (World -> Active Continents -> Active Zones)
        map_slides = self._build_map_slides(humans, [c for c in companions if c["online"]], random_units, bool(live_pos))

        # 7. Hearthguard Guild Bank Mats
        bank_rows = self.db.query(f"""
            SELECT ii.itemEntry, it.name, it.class, it.subclass, it.Quality,
                   SUM(ii.count) AS qty, COUNT(*) AS stacks, MIN(gbi.TabId) AS tab
            FROM acore_characters.guild_bank_item gbi
            JOIN acore_characters.item_instance ii ON ii.guid = gbi.item_guid
            JOIN acore_world.item_template it ON it.entry = ii.itemEntry
            WHERE gbi.guildid = {HEARTHGUARD_GUILD_ID}
            GROUP BY ii.itemEntry, it.name, it.class, it.subclass, it.Quality
            ORDER BY qty DESC, it.name
        """)
        bank_by_cat: Dict[str, List[Dict[str, Any]]] = {c: [] for c in MAT_CATEGORY_ORDER}
        total_slots = 0
        total_units = 0
        for r in bank_rows:
            entry, iname, icls, isub, qual, qty, stacks, tab = (
                int(r[0]), r[1], int(r[2]), int(r[3]), int(r[4]), int(r[5]), int(r[6]), int(r[7])
            )
            cat = categorize_item(icls, isub, iname)
            total_slots += stacks
            total_units += qty
            bank_by_cat.setdefault(cat, []).append({
                "entry": entry,
                "name": iname,
                "qty": qty,
                "stacks": stacks,
                "quality": qual,
                "color": ITEM_QUALITY_COLORS.get(qual, "#e8ecf2"),
                "tab": tab,
            })
        bank_categories = []
        for cat in MAT_CATEGORY_ORDER:
            items = bank_by_cat.get(cat, [])
            if items:
                bank_categories.append({
                    "category": cat,
                    "distinct": len(items),
                    "total_qty": sum(i["qty"] for i in items),
                    "items": items,
                })

        # 8. Recent companion_ledger (mats, crafts, bank deposits/withdrawals)
        ledger_rows = self.db.query("""
            SELECT l.time, c.name, c.class, l.action, l.item, COALESCE(it.name, ''), l.count, l.note
            FROM acore_characters.companion_ledger l
            JOIN acore_characters.characters c ON c.guid = l.guid
            LEFT JOIN acore_world.item_template it ON it.entry = l.item
            WHERE l.action IN ('deposit', 'withdraw', 'buy', 'craft', 'train', 'mount')
            ORDER BY l.id DESC
            LIMIT 25
        """)
        ledger = []
        for r in ledger_rows:
            cls = CLASSES.get(int(r[2]), {"color": "#fff"})
            ledger.append({
                "time": r[0],
                "bot": r[1],
                "class_color": cls["color"],
                "action": r[3],
                "item_id": int(r[4]),
                "item_name": r[5],
                "count": int(r[6]),
                "note": r[7],
            })

        # 9. Guild Life events (chronicle)
        event_rows = self.db.query("""
            SELECT e.id, c.name, c.class, e.event_type, e.summary, e.created_at
            FROM acore_characters.companion_events e
            JOIN acore_characters.characters c ON c.guid = e.guid
            ORDER BY e.id DESC
            LIMIT 30
        """)
        events = []
        for r in event_rows:
            cls = CLASSES.get(int(r[2]), {"color": "#fff"})
            ts = int(r[5])
            events.append({
                "id": int(r[0]),
                "bot": r[1],
                "class_color": cls["color"],
                "type": r[3],
                "summary": r[4],
                "ts": ts,
                "time_str": datetime.datetime.fromtimestamp(ts).strftime("%H:%M"),
            })

        # 10. Work orders, Rep goals, Relationships
        order_rows = self.db.query("""
            SELECT id, requester_name, crafter_name, item_name, quantity, story, status, created_at
            FROM acore_characters.companion_work_orders
            ORDER BY id DESC LIMIT 12
        """)
        work_orders = [
            {
                "id": int(r[0]),
                "requester": r[1],
                "crafter": r[2],
                "item": r[3],
                "qty": int(r[4]),
                "story": r[5],
                "status": r[6],
            }
            for r in order_rows
        ]

        rep_rows = self.db.query("""
            SELECT c.name, c.class, rg.note, COALESCE(cr.standing, 0)
            FROM acore_characters.companion_rep_goals rg
            JOIN acore_characters.characters c ON c.guid = rg.guid
            LEFT JOIN acore_characters.character_reputation cr
              ON cr.guid = rg.guid AND cr.faction = rg.faction
            ORDER BY COALESCE(cr.standing, 0) DESC
        """)
        rep_goals = []
        for r in rep_rows:
            cls = CLASSES.get(int(r[1]), {"color": "#fff"})
            standing = int(r[3])
            info = rep_standing_info(standing)
            rep_goals.append({
                "bot": r[0],
                "class_color": cls["color"],
                "faction": r[2],
                "standing": standing,
                **info,
            })

        rel_rows = self.db.query("""
            SELECT ca.name, cb.name, r.runs_together, r.help_given, r.affinity, r.last_event
            FROM acore_characters.companion_relationships r
            JOIN acore_characters.characters ca ON ca.guid = r.guid_a
            JOIN acore_characters.characters cb ON cb.guid = r.guid_b
            ORDER BY r.affinity DESC, r.runs_together DESC LIMIT 10
        """)
        relationships = [
            {
                "a": r[0],
                "b": r[1],
                "runs": int(r[2]),
                "helps": int(r[3]),
                "affinity": int(r[4]),
                "last_event": r[5],
            }
            for r in rel_rows
        ]

        # Tick stats
        ticks = list(self.logs.tick_diffs)
        ms_vals = [x["ms"] for x in ticks]
        if ms_vals:
            s_ms = sorted(ms_vals)
            tick_stats = {
                "latest": ms_vals[-1],
                "mean": round(sum(ms_vals) / len(ms_vals), 1),
                "p99": s_ms[min(len(s_ms) - 1, int(len(s_ms) * 0.99))],
                "max": max(ms_vals),
                "series": ticks,
            }
        else:
            tick_stats = {"latest": None, "mean": None, "p99": None, "max": None, "series": []}

        # Shift timeline status
        cur_hour = now.hour
        is_weekend = now.weekday() in (4, 5, 6)  # Fri, Sat, Sun
        shifts = []
        for sh in SHIFT_SCHEDULE:
            active = sh["always"] or (
                sh["start"] <= cur_hour < sh["end"]
                and (sh["id"] != "weekend-raiders" or is_weekend)
            )
            members = [c for c in companions if c["shift_group"] == sh["id"]]
            shifts.append({
                **sh,
                "active": active,
                "online_count": sum(1 for m in members if m["online"]),
                "total_count": len(members),
            })

        comp_online = sum(1 for c in companions if c["online"])
        avg_all = round(sum(c["level"] for c in companions) / max(1, len(companions)), 1)
        core_wonky = [c for c in companions if c["shift_group"] == "core-wonky"]
        avg_core_w = round(sum(c["level"] for c in core_wonky) / max(1, len(core_wonky)), 1)
        core_logan = [c for c in companions if c["shift_group"] == "core-logan"]
        avg_core_l = round(sum(c["level"] for c in core_logan) / max(1, len(core_logan)), 1)

        snapshot = {
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "clock": now.strftime("%I:%M:%S %p").lstrip("0"),
            "hour": cur_hour,
            "minute": now.minute,
            "realm": realm,
            "vitals": vitals,
            "ticks": tick_stats,
            "errors_bytes": self.logs.errors_bytes,
            "last_error": self.logs.last_error,
            "slow_warnings": self.logs.slow_warnings_since_boot,
            "population": {
                "humans": len(humans),
                "companions_online": comp_online,
                "companions_total": len(companions),
                "random_online": random_total,
                "total_online": len(humans) + comp_online + random_total,
            },
            "humans": humans,
            "raid": {
                "stage": raid_stage,
                "level_cap": raid_cap,
                "prof_cap": prof_cap,
                "stages": [
                    {"stage": 0, "era": "Classic", "cap": 60, "boss": "C'Thun (AQ40)", "unlocked": True},
                    {"stage": 1, "era": "Burning Crusade", "cap": 70, "boss": "Kil'jaeden (SWP)", "unlocked": raid_stage >= 1},
                    {"stage": 2, "era": "Wrath of the Lich King", "cap": 80, "boss": "The Lich King (ICC)", "unlocked": raid_stage >= 2},
                ],
                "roles": roles,
                "brackets": brackets,
                "avg_guild_level": avg_all,
                "avg_core_wonky": avg_core_w,
                "avg_core_logan": avg_core_l,
                "rep_goals": rep_goals,
            },
            "companions": companions,
            "professions": prof_list,
            "bank": {
                "total_slots": total_slots,
                "max_slots": 196,  # 2 tabs x 98 slots
                "distinct_items": len(bank_rows),
                "total_units": total_units,
                "categories": bank_categories,
            },
            "ledger": ledger,
            "events": events,
            "work_orders": work_orders,
            "relationships": relationships,
            "shifts": shifts,
            "world_zones": world_zones,
            "map_slides": map_slides,
            "weekly_event": weekly_event,
            "ai_brain": ai_brain,
        }

        with self._lock:
            self._state = snapshot
            self._version += 1

    def _project(self, box: Dict[str, Any], x: float, y: float) -> Optional[Tuple[float, float]]:
        dx = box["left"] - box["right"]
        dy = box["top"] - box["bottom"]
        if abs(dx) < 1e-3 or abs(dy) < 1e-3:
            return None
        u = ((box["left"] - y) / dx) * 100.0
        v = ((box["top"] - x) / dy) * 100.0
        if -5.0 <= u <= 105.0 and -5.0 <= v <= 105.0:
            return round(max(1.5, min(98.5, u)), 2), round(max(2.0, min(98.0, v)), 2)
        return None

    def _project_world(self, map_id: int, zone_id: int, x: float, y: float) -> Optional[Tuple[float, float]]:
        continents = self._map_meta.get("continents", {})
        zones = self._map_meta.get("zones", {})
        # Azuremyst / Bloodmyst / Exodar sit off NW Kalimdor on World.jpg
        if map_id == 530 and zone_id in AZUREMYST_ZONES:
            zbox = zones.get(str(zone_id)) or zones.get("3524")
            if zbox:
                p = self._project(zbox, x, y)
                if p:
                    if zone_id == 3525:  # Bloodmyst (pink island)
                        u = 2.5 + (p[0] / 100.0) * 5.0
                        v = 31.0 + (p[1] / 100.0) * 6.0
                    else:  # Azuremyst / Exodar (island just south of Bloodmyst)
                        u = 3.8 + (p[0] / 100.0) * 5.5
                        v = 37.5 + (p[1] / 100.0) * 6.5
                    return round(u, 2), round(v, 2)
        # Quel'Thalas sits NE Eastern Kingdoms on World.jpg
        if map_id == 530 and zone_id in QUELTHALAS_ZONES:
            return 85.5, 22.5
        cbox = continents.get(str(map_id))
        cmeta = CONTINENT_META.get(str(map_id))
        if not cbox or not cmeta:
            return None
        p = self._project(cbox, x, y)
        if not p:
            return None
        su0, su1, sv0, sv1 = cmeta["src_box"]
        wu0, wu1, wv0, wv1 = cmeta["world_box"]
        nu = (p[0] - su0) / max(1e-3, (su1 - su0))
        nv = (p[1] - sv0) / max(1e-3, (sv1 - sv0))
        u = max(1.5, min(98.5, wu0 + nu * (wu1 - wu0)))
        v = max(2.0, min(98.0, wv0 + nv * (wv1 - wv0)))
        return round(u, 2), round(v, 2)

    def _project_on_continent(self, cont_map_id: int, unit_map_id: int, zone_id: int, x: float, y: float) -> Optional[Tuple[float, float]]:
        continents = self._map_meta.get("continents", {})
        zones = self._map_meta.get("zones", {})
        # Project Azuremyst / Bloodmyst / Exodar (map 530) onto Kalimdor.jpg's NW islands!
        if cont_map_id == 1 and unit_map_id == 530 and zone_id in AZUREMYST_ZONES:
            zbox = zones.get(str(zone_id)) or zones.get("3524")
            if zbox:
                p = self._project(zbox, x, y)
                if p:
                    if zone_id == 3525:  # Bloodmyst on Kalimdor.jpg
                        return round(26.5 + (p[0] / 100.0) * 7.0, 2), round(14.5 + (p[1] / 100.0) * 8.5, 2)
                    # Azuremyst / Exodar on Kalimdor.jpg
                    return round(27.5 + (p[0] / 100.0) * 10.0, 2), round(22.5 + (p[1] / 100.0) * 10.5, 2)
        if unit_map_id != cont_map_id:
            return None
        cbox = continents.get(str(cont_map_id))
        return self._project(cbox, x, y) if cbox else None

    def _build_map_slides(
        self,
        humans: List[Dict[str, Any]],
        online_comps: List[Dict[str, Any]],
        random_units: List[Dict[str, Any]],
        is_realtime: bool,
    ) -> List[Dict[str, Any]]:
        slides: List[Dict[str, Any]] = []
        world_meta = self._map_meta.get("world")
        continents = self._map_meta.get("continents", {})
        zones = self._map_meta.get("zones", {})

        named: List[Dict[str, Any]] = []
        for h in humans:
            named.append({
                "name": h["name"], "level": h["level"], "color": "#f2d480",
                "class_color": h["class_color"], "human": True, "dead": False,
                "map_id": h["map_id"], "zone_id": h["zone_id"], "zone": h["zone"],
                "x": h["x"], "y": h["y"], "race_id": h["race_id"], "gender": h["gender"],
            })
        for c in online_comps:
            named.append({
                "name": c["name"], "level": c["level"], "color": c["class_color"],
                "class_color": c["class_color"], "human": False, "dead": c["dead"],
                "map_id": c["map_id"], "zone_id": c["zone_id"], "zone": c["zone"],
                "x": c["x"], "y": c["y"], "race_id": c["race_id"], "gender": c["gender"],
            })

        # 1. World Map slide
        if world_meta:
            w_pins = []
            for n in named:
                p = self._project_world(n["map_id"], n["zone_id"], n["x"], n["y"])
                if p:
                    w_pins.append({
                        "name": n["name"], "level": n["level"], "color": n["color"],
                        "human": n["human"], "dead": n["dead"], "zone": n["zone"],
                        "u": p[0], "v": p[1],
                    })
            w_dots = []
            for ru in random_units:
                p = self._project_world(ru["map_id"], ru["zone_id"], ru["x"], ru["y"])
                if p:
                    w_dots.append({"u": p[0], "v": p[1]})
            slides.append({
                "id": "world",
                "kind": "world",
                "title": "World of Azeroth",
                "subtitle": f"{len(w_pins)} Hearthguard · {len(w_dots)} World",
                "img": world_meta["img"],
                "realtime": is_realtime,
                "pins": w_pins,
                "dots": w_dots,
            })

        # 2. Active Continent slides (only continents with >=1 human/companion)
        active_maps = []
        for mid_str in ("0", "1", "530", "571"):
            mid = int(mid_str)
            cbox = continents.get(mid_str)
            if not cbox:
                continue
            c_pins = []
            for n in named:
                p = self._project_on_continent(mid, n["map_id"], n["zone_id"], n["x"], n["y"])
                if p:
                    c_pins.append({
                        "name": n["name"], "level": n["level"], "color": n["color"],
                        "human": n["human"], "dead": n["dead"], "zone": n["zone"],
                        "u": p[0], "v": p[1],
                    })
            if not c_pins:
                continue
            c_dots = []
            for ru in random_units:
                p = self._project_on_continent(mid, ru["map_id"], ru["zone_id"], ru["x"], ru["y"])
                if p:
                    c_dots.append({"u": p[0], "v": p[1]})
            label = CONTINENT_META.get(mid_str, {}).get("label", cbox["folder"])
            active_maps.append({
                "id": f"cont-{mid}",
                "kind": "continent",
                "title": label,
                "subtitle": f"{len(c_pins)} Hearthguard · {len(c_dots)} World",
                "img": cbox["img"],
                "realtime": is_realtime,
                "pins": c_pins,
                "dots": c_dots,
            })
        slides.extend(active_maps)

        # 3. Active Zone slides (every zone with >=1 human or companion that has an official map!)
        by_zone: Dict[int, List[Dict[str, Any]]] = collections.defaultdict(list)
        for n in named:
            if str(n["zone_id"]) in zones:
                by_zone[n["zone_id"]].append(n)

        # Order: zones containing a human first, then by count desc, then alphabetically
        ordered_zids = sorted(
            by_zone.keys(),
            key=lambda z: (0 if any(u["human"] for u in by_zone[z]) else 1, -len(by_zone[z]), zone_name(z)),
        )
        for zid in ordered_zids:
            zbox = zones[str(zid)]
            z_pins = []
            for n in by_zone[zid]:
                p = self._project(zbox, n["x"], n["y"])
                if p:
                    z_pins.append({
                        "name": n["name"], "level": n["level"], "color": n["color"],
                        "human": n["human"], "dead": n["dead"], "zone": n["zone"],
                        "u": p[0], "v": p[1],
                    })
            if not z_pins:
                continue
            z_dots = []
            for ru in random_units:
                if ru["zone_id"] == zid:
                    p = self._project(zbox, ru["x"], ru["y"])
                    if p:
                        z_dots.append({"u": p[0], "v": p[1]})
            slides.append({
                "id": f"zone-{zid}",
                "kind": "zone",
                "title": zone_name(zid),
                "subtitle": f"{len(z_pins)} Hearthguard" + (f" · {len(z_dots)} World" if z_dots else ""),
                "img": zbox["img"],
                "realtime": is_realtime,
                "pins": z_pins,
                "dots": z_dots,
            })

        return slides
