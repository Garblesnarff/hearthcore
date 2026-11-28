"""Trusted live game-state bridge: exporter, session-boundary AI planner, and safe DB importer.

Safety & architecture invariants (Issues #3-#8, docs/ARCHITECTURE.md, docs/PERSISTENT_MEMORY.md):
- Game server & DB remain authoritative for level, gear, items, quests, GUIDs, and actions.
- Plans are generated strictly out-of-loop at session boundaries (never per tick).
- Every plan (AI or fallback) passes core.validate() allowlist checks before touching DB.
- Temporary AI weights are written to `companion_weight_overrides` with expiry & monotonic
  revision (NEVER overwriting canonical `companion_weights` persona/manual baselines).
- Typed, versioned goal proposals are written to `companion_proposals` so native C++ validates
  eligibility, executes concrete work orders, and reports accepted/executing/completed/rejected/
  interrupted/expired outcomes; unsupported goal types are explicitly flagged `advisory_only=1`.
- Durable event history is ingested incrementally via cursor checkpoints into `event_journal`.
- Shadow runs (`apply=False`) are strictly isolated from live weights, proposals, intentions,
  reservations, active_plans.json, and outcome tracking (while still accounting API spend in
  the shared transactional `BudgetLedger`).
- Zero LLM text is ever sent to in-game chat.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("hearthcore")
_STOP_EVENT = threading.Event()


def request_graceful_stop(*_args) -> None:
    """Signal active planning cycle to finish current companion cleanly and exit without orphans."""
    _STOP_EVENT.set()


def install_sigterm_handler() -> None:
    try:
        signal.signal(signal.SIGTERM, request_graceful_stop)
    except (ValueError, OSError) as exc:
        logger.debug("SIGTERM handler not installed (non-main thread or unsupported OS): %s", exc)

from .budget import BudgetExceeded, BudgetLedger, InvalidWithUsage, ProviderBackoffError
from .core import Invalid, ROOT, fallback, profile, profiles, snapshot, validate
from .dungeons import (
    ALL_ROLE_SPELL_IDS,
    CLASS_ID_TO_NAME,
    INVTYPE_SHIELD,
    faction_from_race,
)
from .guides import LEVELING_ORDER_CATALOG, ORDER_BY_ENTRY, SKILL_NAMES, best_farm_hint
from .memory import (
    MemoryEngine,
    classify_plan_outcome,
    goal_category,
    goal_prefix,
    is_advisory_goal,
    parse_order_id,
)
from .playstyle import apply_playstyle_scoring
from .provider import anthropic_with_meta, load_env

DB_CONTAINER = os.getenv("HEARTHCORE_DB_CONTAINER", "ac-database")
LIVE_POS_FILE = Path(
    os.getenv(
        "HEARTHCORE_LIVE_POS_FILE",
        str(Path.home() / "AzerothCore" / "azerothcore-wotlk" / "env" / "dist" / "logs" / "live_positions.json"),
    )
)
RUNTIME_DIR = Path(os.getenv("HEARTHCORE_RUNTIME_DIR", str(ROOT / "runtime")))
BRAIN_DB_PATH = RUNTIME_DIR / "brain.db"
ACTIVE_PLANS_PATH = RUNTIME_DIR / "active_plans.json"
SHADOW_PLANS_PATH = RUNTIME_DIR / "shadow_plans.json"
QUARTERMASTER_PATH = RUNTIME_DIR / "quartermaster.json"
DEFAULT_SQL_TIMEOUT_SEC = float(os.getenv("HEARTHCORE_SQL_TIMEOUT_SEC", "20") or 20)
DEFAULT_STALE_MINUTES = int(os.getenv("HEARTHCORE_STALE_MINUTES", "45") or 45)
DEFAULT_QM_MAX_AGE_SEC = int(os.getenv("HEARTHCORE_QM_MAX_AGE_SEC", str(6 * 3600)) or (6 * 3600))
DEFAULT_FALLBACK_HOLDOUT_PCT = int(os.getenv("HEARTHCORE_FALLBACK_HOLDOUT_PCT", "0") or 0)
DEFAULT_MAX_PLANS_PER_TICK = int(os.getenv("HEARTHCORE_MAX_PLANS_PER_TICK", "8") or 8)
DEFAULT_MAX_CONSECUTIVE_APPLY_FAILURES = int(os.getenv("HEARTHCORE_MAX_APPLY_FAILURES", "2") or 2)
DEFAULT_APPLY_FAILURE_BACKOFF_SEC = int(os.getenv("HEARTHCORE_APPLY_BACKOFF_SEC", "900") or 900)

# Canonical 1-to-many mapping between character profile preference keys and native CompanionBrain keys (Issue #60).
# Native C++ merge rule (CompanionBrain.cpp:523,555): active companion_weight_overrides rows REPLACE
# baseline companion_weights values for each key present, EXCEPT rows with `source == 'manual'`
# (`manualKeys`), which always take precedence.
PROFILE_PREF_TO_NATIVE_WEIGHTS: dict[str, tuple[str, ...]] = {
    "quest": ("quest_work", "quest_turnin", "quest_hub", "quest_grind"),
    "craft": ("craft",),
    "gather": ("gather",),
    "fish": ("fish",),
    "train": ("train",),
    "bank": ("bank",),
    "repair": ("repair", "sell"),
    "travel": ("travel",),
    "rest": ("hearth",),
}
NATIVE_MERGE_RULE = "replace_except_manual_source"

CRAFT_SKILLS = {164, 165, 171, 197, 202, 333, 755, 773}
GATHER_SKILLS = {182, 186, 393}
SKILL_BY_SLUG = {re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_"): sid for sid, name in SKILL_NAMES.items()}

WORK_ORDER_SKILL = {t.item_entry: t.skill_id for t in LEVELING_ORDER_CATALOG}
WORK_ORDER_SKILL.update({3470: 164, 3486: 164, 3576: 186})

ZONE_NAMES = {
    1: "Dun Morogh", 10: "Duskwood", 11: "Wetlands", 12: "Elwynn Forest",
    33: "Stranglethorn Vale", 38: "Loch Modan", 40: "Westfall", 44: "Redridge Mountains",
    45: "Arathi Highlands", 141: "Teldrassil", 148: "Darkshore", 331: "Ashenvale",
    1519: "Stormwind City", 1537: "Ironforge", 1657: "Darnassus",
    3524: "Azuremyst Isle", 3525: "Bloodmyst Isle", 3557: "The Exodar",
}


def _clamp(val: float, lo: float = 0.5, hi: float = 1.5) -> float:
    return round(max(lo, min(hi, float(val))), 3)


def _slug_token(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")[:24] or "item"


def _sql_esc(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("'", "\\'")


class RunLockBusy(Invalid):
    """Raised when another brain-tick or quartermaster-tick already holds the shared flock."""


@contextlib.contextmanager
def acquire_run_lock(runtime_dir: Path = RUNTIME_DIR, *, timeout_sec: float = 0.0):
    """Process-safe shared flock between brain-tick and quartermaster-tick (Issues #54 & #59)."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_dir / "hearthcore.lock"
    fh = open(lock_path, "a+", encoding="utf-8")
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RunLockBusy(f"Shared run lock busy: {lock_path}")
                time.sleep(0.1)
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} ts={int(time.time())}\n")
        fh.flush()
        yield lock_path
    finally:
        if acquired:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                logger.debug("flock unlock ignored for %s: %s", lock_path, exc)
        fh.close()


def brain_disabled_path(runtime_dir: Path = RUNTIME_DIR) -> Path:
    return runtime_dir / "brain.disabled"


def is_brain_disabled(runtime_dir: Path = RUNTIME_DIR) -> bool:
    return brain_disabled_path(runtime_dir).is_file()


def set_brain_disabled(
    disabled: bool,
    *,
    runtime_dir: Path = RUNTIME_DIR,
    guid: int | None = None,
    sql_exec=None,
    skip_db: bool = False,
) -> dict[str, Any]:
    """Operator kill switch: toggle global flag file and/or MySQL companion_weight_overrides.enabled (Issue #59)."""
    flag_path = brain_disabled_path(runtime_dir)
    if guid is None:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        if disabled:
            flag_path.write_text(
                f"disabled_at={datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n",
                encoding="utf-8",
            )
        elif flag_path.exists():
            flag_path.unlink()
    db_updated = False
    db_error = ""
    if not skip_db:
        try:
            set_override_enabled(guid, not disabled, sql_exec=sql_exec)
            db_updated = True
        except Exception as exc:
            db_error = f"{type(exc).__name__}: {exc}"
    return {
        "disabled": disabled,
        "guid": guid,
        "flag_file": str(flag_path),
        "flag_present": flag_path.is_file(),
        "db_updated": db_updated,
        "db_error": db_error,
    }


def record_failure_alert(source: str, detail: str, *, runtime_dir: Path = RUNTIME_DIR, severity: str = "warning") -> None:
    """Dispatch a structured operator failure alert (log, journal, dashboard, optional webhook/desktop, Issue #59)."""
    from .alerts import dispatch_alert

    dispatch_alert(source, detail, severity=severity, runtime_dir=runtime_dir)


def _unescape_mysql_batch_field(field: str) -> str | None:
    """Decode one mysql --batch (-B) TSV field: NULL -> None, unescape \\t/\\n/\\\\/\\0 (Issue #62)."""
    if field == "NULL":
        return None
    if "\\" not in field:
        return field
    out: list[str] = []
    i = 0
    n = len(field)
    while i < n:
        ch = field[i]
        if ch == "\\" and i + 1 < n:
            nxt = field[i + 1]
            if nxt == "t":
                out.append("\t")
            elif nxt == "n":
                out.append("\n")
            elif nxt == "r":
                out.append("\r")
            elif nxt == "0":
                out.append("\x00")
            elif nxt == "\\":
                out.append("\\")
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_mysql_tsv(raw_stdout: str, *, expected_cols: int | None = None) -> list[list[str | None]]:
    """Parse mysql -N -B output splitting strictly on '\\n' ONLY (never U+2028/\\x0b/\\x0c/\\x85, Issue #62).

    Decodes SQL NULL to None and raises ValueError loudly if expected_cols is violated.
    """
    if not raw_stdout:
        return []
    rows: list[list[str | None]] = []
    for raw_line in raw_stdout.split("\n"):
        if raw_line == "":
            continue
        if raw_line.endswith("\r"):
            raw_line = raw_line[:-1]
        parts = [_unescape_mysql_batch_field(f) for f in raw_line.split("\t")]
        if expected_cols is not None and len(parts) != expected_cols:
            raise ValueError(f"Malformed MySQL row: expected {expected_cols} columns, got {len(parts)}")
        rows.append(parts)
    return rows


def _int_or(val: Any, default: int = 0) -> int:
    if val is None or val == "":
        return default
    return int(val)


def run_sql(sql: str, *, timeout: float | None = None, expected_cols: int | None = None) -> list[list[Any]]:
    use_timeout = float(timeout if timeout is not None else os.getenv("HEARTHCORE_SQL_TIMEOUT_SEC", str(DEFAULT_SQL_TIMEOUT_SEC)))
    db_host = os.getenv("HEARTHCORE_DB_HOST", "").strip()
    db_port = os.getenv("HEARTHCORE_DB_PORT", "3306").strip()
    db_name = os.getenv("HEARTHCORE_DB_NAME", "acore_characters").strip()
    db_user = os.getenv("HEARTHCORE_DB_USER", "").strip()
    db_pass = os.getenv("HEARTHCORE_DB_PASSWORD", "")
    defaults_file = os.getenv("HEARTHCORE_DB_DEFAULTS_FILE", "").strip()
    container_defaults = os.getenv("HEARTHCORE_DB_CONTAINER_DEFAULTS_FILE", "").strip()

    # Use raw bytes (NOT text=True universal newlines!) so lone '\r' in MySQL text columns
    # is NEVER translated into '\n' and cannot split a row (Issue #62).
    sql_bytes = sql.encode("utf-8")
    if defaults_file:
        # Safest host-TCP path: chmod-600 MySQL option file (zero password in env or argv)
        cmd = ["mysql", f"--defaults-extra-file={defaults_file}", "-N", "-B", db_name]
        res = subprocess.run(cmd, input=sql_bytes, capture_output=True, check=True, timeout=max(1.0, use_timeout))
    elif container_defaults:
        # Least-privilege user inside DB container via chmod-600 option file (zero root, zero password on argv!)
        cmd = [
            "docker", "exec", "-i", DB_CONTAINER,
            "mysql", f"--defaults-extra-file={container_defaults}", "-N", "-B", db_name,
        ]
        res = subprocess.run(cmd, input=sql_bytes, capture_output=True, check=True, timeout=max(1.0, use_timeout))
    elif db_host:
        # Direct TCP mysql client with MYSQL_PWD in child env ONLY (never on argv)
        env = dict(os.environ)
        env["MYSQL_PWD"] = db_pass
        cmd = [
            "mysql",
            f"-h{db_host}",
            f"-P{db_port}",
            f"-u{db_user or 'root'}",
            "-N",
            "-B",
            db_name,
        ]
        res = subprocess.run(
            cmd,
            input=sql_bytes,
            capture_output=True,
            check=True,
            timeout=max(1.0, use_timeout),
            env=env,
        )
    else:
        # Fallback to container root shell using container's own internal env var (never passed on host argv)
        res = subprocess.run(
            [
                "docker", "exec", "-i", DB_CONTAINER, "sh", "-c",
                f'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -u root -N -B {db_name}',
            ],
            input=sql_bytes,
            capture_output=True,
            check=True,
            timeout=max(1.0, use_timeout),
        )
    return parse_mysql_tsv(res.stdout.decode("utf-8", errors="replace"), expected_cols=expected_cols)


def fetch_remote_max_revision(*, sql_exec=None) -> int:
    """Query MySQL for the highest existing plan_id / revision across overrides and proposals (Issue #53)."""
    runner = sql_exec if sql_exec is not None else run_sql
    rows = runner(
        "SELECT GREATEST("
        "COALESCE((SELECT MAX(revision) FROM companion_weight_overrides), 0), "
        "COALESCE((SELECT MAX(plan_id) FROM companion_weight_overrides), 0), "
        "COALESCE((SELECT MAX(plan_id) FROM companion_proposals), 0), "
        "COALESCE((SELECT MAX(snapshot_rev) FROM companion_proposals), 0)"
        ");\n"
    )
    if rows and rows[0]:
        try:
            return max(0, int(rows[0][0]))
        except (TypeError, ValueError):
            return 0
    return 0


def read_live_context(*, now_ts: float | None = None, max_age_sec: float = 30.0) -> dict[str, Any]:
    """Read fresh weekly_event, live positions, and optional native combat/group/LFG telemetry if present."""
    now = float(now_ts if now_ts is not None else time.time())
    ctx: dict[str, Any] = {
        "weekly_event": "",
        "live_by_guid": {},
        "live_pos_ts": None,
        "live_pos_age_sec": None,
        "live_pos_fresh": False,
    }
    try:
        st = LIVE_POS_FILE.stat()
        age_sec = max(0.0, now - float(st.st_mtime))
        ctx["live_pos_age_sec"] = round(age_sec, 2)
        if age_sec < float(max_age_sec):
            raw = json.loads(LIVE_POS_FILE.read_text(encoding="utf-8"))
            ctx["weekly_event"] = str(raw.get("weekly_event") or "")
            ctx["live_pos_ts"] = int(raw["t"]) if raw.get("t") is not None else int(st.st_mtime)
            ctx["live_pos_fresh"] = True
            for u in raw.get("units", []):
                ctx["live_by_guid"][int(u["g"])] = u
    except FileNotFoundError:
        logger.debug("live_positions.json not present at %s", LIVE_POS_FILE)
    except Exception as exc:
        logger.warning("Failed reading live_positions.json (%s): %s: %s", LIVE_POS_FILE, type(exc).__name__, exc)
    return ctx


def sync_event_journal(
    mem: MemoryEngine,
    guid_to_slug: dict[int, str],
    now_ts: int,
    *,
    sql_exec=None,
) -> dict[str, Any]:
    """Incrementally ingest companion_events into durable event_journal with stall/reset/gap protection (Issues #4 & #57)."""
    runner = sql_exec if sql_exec is not None else run_sql
    cursor = mem.get_event_cursor()
    bounds_rows = runner("SELECT COALESCE(MIN(id), 0), COALESCE(MAX(id), 0) FROM companion_events;")
    min_avail = _int_or(bounds_rows[0][0], 0) if (bounds_rows and len(bounds_rows[0]) >= 2) else 0
    max_avail = _int_or(bounds_rows[0][1], 0) if (bounds_rows and len(bounds_rows[0]) >= 2) else 0

    # Detect source counter reset / truncation BEFORE querying WHERE id > cursor (Issue #57)
    if cursor > 0 and max_avail < cursor:
        reset_info = mem.ingest_events(
            [],
            now_ts=now_ts,
            min_available_source_id=min_avail,
            max_available_source_id=max_avail,
        )
        logger.warning("Event journal source counter reset detected: %s", reset_info.get("gap_note"))
        cursor = int(reset_info["cursor"])

    total_inserted = 0
    last_info: dict[str, Any] = {
        "inserted": 0,
        "cursor": cursor,
        "latest_source_id": max_avail,
        "journal_lag": max(0, max_avail - cursor),
        "gap_detected": False,
        "gap_note": "",
    }
    # Paginate in bounded chunks; ALWAYS advance persisted cursor past filtered non-companion pages!
    for _ in range(10):
        rows = runner(
            f"SELECT id, guid, event_type, summary, partner_guid, zone_id, created_at "
            f"FROM companion_events WHERE id > {int(cursor)} ORDER BY id ASC LIMIT 500;"
        )
        if not rows:
            last_info = mem.ingest_events(
                [],
                now_ts=now_ts,
                min_available_source_id=min_avail,
                max_available_source_id=max_avail,
            )
            break
        scanned_max = _int_or(rows[-1][0], cursor)
        batch = []
        for r in rows:
            if len(r) < 7:
                continue
            g = _int_or(r[1], 0)
            slug = guid_to_slug.get(g)
            if not slug:
                continue
            batch.append({
                "source_id": _int_or(r[0], 0),
                "guid": g,
                "character_id": slug,
                "event_type": str(r[2] or ""),
                "summary": str(r[3] or ""),
                "partner_guid": _int_or(r[4], 0),
                "zone_id": _int_or(r[5], 0),
                "created_at": _int_or(r[6], now_ts),
            })
        last_info = mem.ingest_events(
            batch,
            now_ts=now_ts,
            min_available_source_id=min_avail,
            max_available_source_id=max_avail,
            scanned_max_source_id=scanned_max,
        )
        total_inserted += last_info["inserted"]
        if scanned_max <= cursor or len(rows) < 500:
            cursor = scanned_max
            break
        cursor = scanned_max

    last_info["inserted"] = total_inserted
    return last_info


def _strict_int(val: Any) -> int | None:
    """Parse an integer strictly; returns None for None, '', 'NULL', or non-integer values."""
    if val is None or isinstance(val, bool):
        return None
    s = str(val).strip()
    if not s or s.upper() == "NULL":
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _opt_bool(val: Any) -> bool | None:
    """Parse an optional boolean strictly; returns None if absent/null."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    s = str(val).strip().lower()
    if s in ("1", "true", "yes"):
        return True
    if s in ("0", "false", "no"):
        return False
    return None


def fetch_world_context(mem: MemoryEngine | None = None, now_ts: int | None = None) -> dict[str, Any]:
    """Query live MySQL for companions, skills, quests, ledger ops, work orders, proposals, and events."""
    live = read_live_context()
    ts = int(now_ts if now_ts is not None else time.time())
    telemetry_errors: list[str] = []
    world_db = (os.getenv("HEARTHCORE_WORLD_DB_NAME") or "acore_world").strip() or "acore_world"
    if not re.match(r"^[A-Za-z0-9_]+$", world_db):
        world_db = "acore_world"

    # Attempt to include activeTalentGroup for dual-spec talent/spell filtering; degrade cleanly if absent
    has_active_talent_group = False
    try:
        comp_rows = run_sql("""
            SELECT c.guid, c.name, c.level, c.zone, c.map, c.money, c.online,
                   COALESCE(cs.shift_group, ''), c.xp, c.race, c.class,
                   cs.start_hour, cs.end_hour, cs.days_mask, cs.always_online,
                   c.activeTalentGroup
            FROM companion_schedule cs
            JOIN characters c ON c.name = cs.name
            ORDER BY c.online DESC, c.name;
        """)
        has_active_talent_group = True
    except Exception as exc:
        logger.debug("characters.activeTalentGroup unavailable (%s); querying base columns", exc)
        comp_rows = run_sql("""
            SELECT c.guid, c.name, c.level, c.zone, c.map, c.money, c.online,
                   COALESCE(cs.shift_group, ''), c.xp, c.race, c.class,
                   cs.start_hour, cs.end_hour, cs.days_mask, cs.always_online
            FROM companion_schedule cs
            JOIN characters c ON c.name = cs.name
            ORDER BY c.online DESC, c.name;
        """)

    companions: dict[str, dict[str, Any]] = {}
    guid_to_slug: dict[int, str] = {}
    active_talent_group_by_guid: dict[int, int] = {}
    for r in comp_rows:
        if len(r) < 9:
            continue
        guid = int(r[0])
        name = r[1]
        slug = name.lower()
        lp = live["live_by_guid"].get(guid)
        zid = _strict_int(lp.get("z")) if isinstance(lp, dict) and lp.get("z") is not None else int(r[3])
        mid = _strict_int(lp.get("m")) if isinstance(lp, dict) and lp.get("m") is not None else int(r[4])
        if zid is None:
            zid = int(r[3])
        if mid is None:
            mid = int(r[4])
        race_id = _strict_int(r[9]) if len(r) >= 11 else None
        class_id = _strict_int(r[10]) if len(r) >= 11 else None
        class_name = CLASS_ID_TO_NAME.get(class_id, "") if class_id else ""
        faction = faction_from_race(race_id)

        # Strict schedule parsing (P1-8: never invent 0..24 / 127 on NULL or malformed values!)
        schedule_info: dict[str, Any] | None = None
        if len(r) >= 15:
            sh = _strict_int(r[11])
            eh = _strict_int(r[12])
            dm = _strict_int(r[13])
            ao = _strict_int(r[14])
            if sh is not None and eh is not None and dm is not None and ao is not None:
                schedule_info = {
                    "shift_group": str(r[7] or ""),
                    "start_hour": sh,
                    "end_hour": eh,
                    "days_mask": dm,
                    "always_online": ao == 1,
                }
            else:
                telemetry_errors.append(f"invalid_schedule_row:{slug}")
        if has_active_talent_group and len(r) >= 16:
            atg = _strict_int(r[15])
            if atg is not None:
                active_talent_group_by_guid[guid] = atg

        live_telem: dict[str, Any] = {
            "class_id": class_id,
            "class_name": class_name or None,
            "spec_tab": None,
            "talent_spells": None,
            "known_spells": None,
            "equipped": None,
            "native_role_mask": None,
            "native_lfg_eligible": None,
            "native_lfg_lock_reason": "",
            "in_combat": None,
            "in_lfg_queue": None,
            "in_lfg_queue_dungeons": [],
            "is_dead": None,
            "live_pos_fresh": bool(live.get("live_pos_fresh")),
            "live_pos_age_sec": live.get("live_pos_age_sec"),
        }
        companions[slug] = {
            "guid": guid,
            "name": name,
            "slug": slug,
            "race_id": race_id,
            "class_id": class_id,
            "class_name": class_name or None,
            "faction": faction,
            "level": int(r[2]),
            "xp": int(r[8]),
            "zone_id": zid,
            "zone": ZONE_NAMES.get(zid, f"Zone {zid}"),
            "map_id": mid,
            "gold": round(int(r[5]) / 10000.0, 2),
            "online": (int(r[6]) == 1) or (guid in live["live_by_guid"]),
            "shift_group": r[7],
            "schedule": schedule_info,
            "locked_maps": None,
            "locked_instances": None,
            "group_id": None,
            "in_group": None,
            "live_telemetry": live_telem,
            "telemetry_observed_at": ts,
            "skills": {},
            "orders_delivered": 0,
            "quests_rewarded": 0,
            "ledger_ops": 0,
            "recent_events": [],
            "social_bonds": [],
        }
        # P1-7: Guard EVERY optional live_positions.json field cleanly (malformed entry -> None + error, never crash!)
        if isinstance(lp, dict):
            try:
                xc = _strict_int(lp.get("xp_cap_level"))
                if xc is not None:
                    companions[slug]["xp_cap_level"] = xc
                xe = _opt_bool(lp.get("xp_eligible"))
                if xe is not None:
                    companions[slug]["xp_eligible"] = xe
                if lp.get("owner_name"):
                    companions[slug]["owner_name"] = str(lp["owner_name"])

                if "d" in lp or "dead" in lp or "hp" in lp:
                    d_val = _opt_bool(lp.get("d") if "d" in lp else lp.get("dead"))
                    hp_val = _strict_int(lp.get("hp")) if "hp" in lp else None
                    if d_val is not None:
                        live_telem["is_dead"] = d_val
                    elif hp_val is not None:
                        live_telem["is_dead"] = (hp_val == 0)

                if "combat" in lp or "in_combat" in lp:
                    live_telem["in_combat"] = _opt_bool(lp.get("combat") if "combat" in lp else lp.get("in_combat"))

                if "grp" in lp or "group_id" in lp:
                    raw_g = lp.get("grp") if "grp" in lp else lp.get("group_id")
                    lgid = _strict_int(raw_g)
                    if lgid is not None:
                        companions[slug]["group_id"] = lgid
                        companions[slug]["in_group"] = lgid > 0

                if "lfg" in lp or "in_lfg_queue" in lp:
                    live_telem["in_lfg_queue"] = _opt_bool(lp.get("in_lfg_queue") if "in_lfg_queue" in lp else lp.get("lfg"))

                if isinstance(lp.get("lfg_dungeons"), list):
                    parsed_d = []
                    for item in lp["lfg_dungeons"]:
                        iv = _strict_int(item)
                        if iv is not None:
                            parsed_d.append(iv)
                    live_telem["in_lfg_queue_dungeons"] = parsed_d

                if "roles" in lp or "native_role_mask" in lp:
                    rm = _strict_int(lp.get("native_role_mask") if "native_role_mask" in lp else lp.get("roles"))
                    if rm is not None:
                        live_telem["native_role_mask"] = rm

                st_val = _strict_int(lp.get("spec_tab"))
                if st_val is not None:
                    live_telem["spec_tab"] = st_val

                if "lfg_eligible" in lp or "native_lfg_eligible" in lp:
                    raw_el = lp.get("native_lfg_eligible") if "native_lfg_eligible" in lp else lp.get("lfg_eligible")
                    live_telem["native_lfg_eligible"] = _opt_bool(raw_el)

                if lp.get("lfg_lock_reason") or lp.get("native_lfg_lock_reason"):
                    live_telem["native_lfg_lock_reason"] = str(
                        lp.get("native_lfg_lock_reason") or lp.get("lfg_lock_reason") or ""
                    )
            except Exception as exc:
                telemetry_errors.append(f"malformed_live_pos:{slug}:{type(exc).__name__}")
                logger.warning("Malformed live_positions entry for %s: %s", slug, exc)
        guid_to_slug[guid] = slug

    all_guids = [str(c["guid"]) for c in companions.values()]
    if all_guids:
        glist = ",".join(all_guids)
        for r in run_sql(
            f"SELECT guid, skill, value, max FROM character_skills WHERE guid IN ({glist}) "
            f"AND skill IN (164,165,171,182,186,197,202,333,393,755,773,129,185,356);"
        ):
            if len(r) >= 4:
                sl = guid_to_slug.get(int(r[0]))
                if sl:
                    companions[sl]["skills"][int(r[1])] = (int(r[2]), int(r[3]))

        for r in run_sql(
            "SELECT crafter_guid, COUNT(*) FROM companion_work_orders "
            "WHERE status = 'delivered' AND crafter_guid > 0 GROUP BY crafter_guid;"
        ):
            if len(r) >= 2:
                sl = guid_to_slug.get(int(r[0]))
                if sl:
                    companions[sl]["orders_delivered"] = int(r[1])

        # Rewarded quest counts per companion (Issue #5: quest progress without level-up)
        for r in run_sql(
            f"SELECT guid, COUNT(*) FROM character_queststatus_rewarded WHERE guid IN ({glist}) GROUP BY guid;"
        ):
            if len(r) >= 2:
                sl = guid_to_slug.get(int(r[0]))
                if sl:
                    companions[sl]["quests_rewarded"] = int(r[1])

        # Productive ledger counts per companion (Issue #5: gathering/crafting/depositing at skill cap!)
        for r in run_sql(
            f"SELECT guid, COUNT(*) FROM companion_ledger "
            f"WHERE guid IN ({glist}) AND action IN ('deposit','craft','withdraw','train','buy') GROUP BY guid;"
        ):
            if len(r) >= 2:
                sl = guid_to_slug.get(int(r[0]))
                if sl:
                    companions[sl]["ledger_ops"] = int(r[1])

        # Issue #14: Live permanent instance lockouts keyed by (map, difficulty) (fail-closed -> None)
        try:
            lock_rows = run_sql(
                f"SELECT ci.guid, i.map, i.difficulty, i.resettime "
                f"FROM character_instance ci JOIN instance i ON i.id = ci.instance "
                f"WHERE ci.guid IN ({glist}) AND ci.permanent = 1 AND (i.resettime = 0 OR i.resettime > {int(ts)});"
            )
            for c in companions.values():
                c["locked_maps"] = []
                c["locked_instances"] = []
            for r in lock_rows:
                if len(r) >= 3:
                    sl = guid_to_slug.get(_int_or(r[0], 0))
                    if sl:
                        m_id = _int_or(r[1], 0)
                        diff = _int_or(r[2], 0)
                        if m_id > 0:
                            pair = (m_id, diff)
                            if pair not in companions[sl]["locked_instances"]:
                                companions[sl]["locked_instances"].append(pair)
                            if diff == 0 and m_id not in companions[sl]["locked_maps"]:
                                companions[sl]["locked_maps"].append(m_id)
        except Exception as exc:
            telemetry_errors.append(f"lockouts:{type(exc).__name__}")
            logger.warning("Failed reading character_instance lockouts (fail-closed -> None): %s", exc)

        # Issue #14: Live group membership (never copy group_member.roles into native_role_mask!)
        try:
            grp_rows = run_sql(
                f"SELECT memberGuid, guid FROM group_member WHERE memberGuid IN ({glist});"
            )
            for c in companions.values():
                if c["group_id"] is None:
                    c["group_id"] = 0
                if c["in_group"] is None:
                    c["in_group"] = False
            for r in grp_rows:
                if len(r) >= 2:
                    sl = guid_to_slug.get(_int_or(r[0], 0))
                    if sl:
                        gid = _int_or(r[1], 0)
                        companions[sl]["group_id"] = gid
                        companions[sl]["in_group"] = gid > 0
        except Exception as exc:
            telemetry_errors.append(f"group_member:{type(exc).__name__}")
            logger.warning("Failed reading group_member (fail-closed -> None): %s", exc)

        # Issue #14: Live active-spec talent spells (filtered by activeTalentGroup specMask!)
        try:
            tal_rows = run_sql(
                f"SELECT guid, spell, specMask FROM character_talent WHERE guid IN ({glist});"
            )
            for c in companions.values():
                c["live_telemetry"]["talent_spells"] = []
            for r in tal_rows:
                if len(r) >= 3:
                    g_id = _int_or(r[0], 0)
                    sl = guid_to_slug.get(g_id)
                    if not sl:
                        continue
                    sp_mask = _int_or(r[2], 1)
                    active_bit = 1 << active_talent_group_by_guid.get(g_id, 0)
                    if sp_mask == 0 or (sp_mask & active_bit):
                        companions[sl]["live_telemetry"]["talent_spells"].append(_int_or(r[1], 0))
        except Exception as exc:
            telemetry_errors.append(f"character_talent:{type(exc).__name__}")
            logger.warning("Failed reading character_talent (fail-closed -> None): %s", exc)

        # Issue #14: Live active-spec role-defining spells (compact IN filter; fail-closed -> None)
        try:
            role_spells_csv = ",".join(str(s) for s in sorted(ALL_ROLE_SPELL_IDS))
            sp_rows = run_sql(
                f"SELECT guid, spell, specMask FROM character_spell "
                f"WHERE guid IN ({glist}) AND spell IN ({role_spells_csv});"
            )
            for c in companions.values():
                c["live_telemetry"]["known_spells"] = []
            for r in sp_rows:
                if len(r) >= 3:
                    g_id = _int_or(r[0], 0)
                    sl = guid_to_slug.get(g_id)
                    if not sl:
                        continue
                    sp_mask = _int_or(r[2], 1)
                    active_bit = 1 << active_talent_group_by_guid.get(g_id, 0)
                    if sp_mask == 0 or (sp_mask & active_bit):
                        companions[sl]["live_telemetry"]["known_spells"].append(_int_or(r[1], 0))
        except Exception as exc:
            telemetry_errors.append(f"character_spell:{type(exc).__name__}")
            logger.warning("Failed reading character_spell (fail-closed -> None): %s", exc)

        # Issue #14: Live equipped gear (slot 15 = main hand ONLY, slot 16 shield, durability; fail-closed -> None)
        try:
            eq_rows = run_sql(
                f"SELECT ci.guid, ci.slot, ii.durability, COALESCE(it.MaxDurability, 0), COALESCE(it.InventoryType, 0) "
                f"FROM character_inventory ci "
                f"JOIN item_instance ii ON ii.guid = ci.item "
                f"LEFT JOIN `{world_db}`.item_template it ON it.entry = ii.itemEntry "
                f"WHERE ci.guid IN ({glist}) AND ci.bag = 0 AND ci.slot BETWEEN 0 AND 18;"
            )
            eq_acc: dict[str, dict[str, Any]] = {
                sl: {
                    "equipped_slots": 0,
                    "has_main_hand": False,
                    "has_shield": False,
                    "broken_slots": 0,
                    "min_durability_pct": None,
                }
                for sl in companions
            }
            for r in eq_rows:
                if len(r) < 5:
                    continue
                sl = guid_to_slug.get(_int_or(r[0], 0))
                if not sl:
                    continue
                slot = _int_or(r[1], -1)
                dur = _int_or(r[2], 0)
                max_dur = _int_or(r[3], 0)
                inv_type = _int_or(r[4], 0)
                acc = eq_acc[sl]
                acc["equipped_slots"] += 1
                if slot == 15:
                    acc["has_main_hand"] = True
                if slot == 16 and inv_type == INVTYPE_SHIELD:
                    acc["has_shield"] = True
                if max_dur > 0:
                    pct = round(min(100.0, max(0.0, (dur / float(max_dur)) * 100.0)), 1)
                    if dur <= 0:
                        acc["broken_slots"] += 1
                    cur_min = acc["min_durability_pct"]
                    acc["min_durability_pct"] = pct if cur_min is None else min(cur_min, pct)
            for sl, acc in eq_acc.items():
                companions[sl]["live_telemetry"]["equipped"] = acc
        except Exception as exc:
            telemetry_errors.append(f"equipment:{type(exc).__name__}")
            logger.warning("Failed reading equipped gear telemetry (fail-closed -> None): %s", exc)

    # Sync durable event journal with fair cursor-based pagination (Issues #4 & #57)
    if mem is not None and guid_to_slug:
        try:
            sync_event_journal(mem, guid_to_slug, ts)
        except Exception as exc:
            logger.warning("sync_event_journal failed: %s: %s", type(exc).__name__, exc)
            record_failure_alert("event_journal_sync", f"{type(exc).__name__}: {exc}")

    online_guids = [str(c["guid"]) for c in companions.values() if c["online"]]
    if online_guids:
        oglist = ",".join(online_guids)
        # Fallback recent events from DB if journal is empty
        if mem is None:
            for r in run_sql(
                f"SELECT guid, event_type, summary FROM companion_events "
                f"WHERE guid IN ({oglist}) ORDER BY id DESC LIMIT 180;"
            ):
                if len(r) >= 3:
                    sl = guid_to_slug.get(int(r[0]))
                    if sl and len(companions[sl]["recent_events"]) < 5:
                        companions[sl]["recent_events"].append(f"{r[1]}: {r[2][:72]}")

        rel_rows = run_sql(f"""
            SELECT r.guid_a, ca.name, r.guid_b, cb.name, r.runs_together, r.help_given, r.affinity, r.last_event
            FROM companion_relationships r
            JOIN characters ca ON ca.guid = r.guid_a
            JOIN characters cb ON cb.guid = r.guid_b
            WHERE r.guid_a IN ({oglist}) OR r.guid_b IN ({oglist})
            ORDER BY r.affinity DESC, r.runs_together DESC
            LIMIT 160;
        """)
        for r in rel_rows:
            if len(r) < 8:
                continue
            ga, na, gb, nb = int(r[0]), r[1], int(r[2]), r[3]
            runs, helps, aff, last_ev = int(r[4]), int(r[5]), int(r[6]), r[7][:48]
            for g_me, other_name in ((ga, nb), (gb, na)):
                sl = guid_to_slug.get(g_me)
                if sl and len(companions[sl]["social_bonds"]) < 3:
                    bond = f"{other_name} (affinity {aff}, {runs} runs, {helps} helps)"
                    if last_ev:
                        bond = f"{bond}: {last_ev}"
                    companions[sl]["social_bonds"].append(bond[:110])

    # Native C++ proposal statuses from companion_proposals (Issue #3)
    proposals_by_plan_id: dict[int, dict[str, Any]] = {}
    try:
        prop_rows = run_sql(
            "SELECT plan_id, status, outcome_reason, started_at, ended_at, active_seconds, progress_delta, advisory_only "
            "FROM companion_proposals ORDER BY id DESC LIMIT 200;"
        )
        for r in prop_rows:
            if len(r) >= 8:
                proposals_by_plan_id[int(r[0])] = {
                    "status": r[1],
                    "outcome_reason": r[2],
                    "started_at": int(r[3]),
                    "ended_at": int(r[4]),
                    "active_seconds": int(r[5]),
                    "progress_delta": int(r[6]),
                    "advisory_only": bool(int(r[7])),
                }
    except Exception as exc:
        logger.warning("Failed reading companion_proposals: %s: %s", type(exc).__name__, exc)

    # Open guild work orders
    work_orders: list[dict[str, Any]] = []
    for r in run_sql(
        "SELECT id, requester_guid, requester_name, item_entry, item_name, quantity, story "
        "FROM companion_work_orders WHERE status IN ('open','claimed') ORDER BY id DESC LIMIT 16;"
    ):
        if len(r) >= 7:
            item_entry = int(r[3])
            work_orders.append({
                "id": int(r[0]),
                "requester_guid": int(r[1]),
                "requester_name": r[2],
                "item_entry": item_entry,
                "item_name": r[4],
                "quantity": int(r[5]),
                "story": r[6],
                "skill": WORK_ORDER_SKILL.get(item_entry, 0),
            })

    human_rows = run_sql("""
        SELECT c.name, c.level, c.zone
        FROM characters c
        JOIN acore_auth.account a ON a.id = c.account
        WHERE c.online = 1
          AND a.username NOT LIKE 'RNDBOT%'
          AND c.name NOT IN (SELECT name FROM companion_schedule);
    """)
    humans = [
        {"name": r[0], "level": int(r[1]), "zone_id": int(r[2]), "zone": ZONE_NAMES.get(int(r[2]), f"Zone {r[2]}")}
        for r in human_rows if len(r) >= 3
    ]

    # Authoritative owner levels & CompanionLevelLead discovered dynamically (Issue #23 — no hardcoded owners/+3!)
    owner_bindings = discover_owner_bindings(companions)
    owner_names_wanted = sorted({v for v in owner_bindings.values() if v})
    owner_levels: dict[str, int] = {}
    if owner_names_wanted:
        in_clause = ", ".join(f"'{_sql_esc(n)}'" for n in owner_names_wanted)
        for r in run_sql(f"SELECT name, level FROM characters WHERE LOWER(name) IN ({in_clause});"):
            if len(r) >= 2:
                owner_levels[r[0].lower()] = int(r[1])
    companion_level_lead = load_companion_level_lead()

    # Live Quartermaster directives with freshness check (Issues #7 & #61)
    qm_focus, qm_mats_by_skill, qm_meta = load_fresh_quartermaster_directives(QUARTERMASTER_PATH, now_ts=ts)

    world_ctx = {
        "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "telemetry_observed_at": ts,
        "telemetry_errors": telemetry_errors,
        "live_pos_fresh": bool(live.get("live_pos_fresh")),
        "live_pos_age_sec": live.get("live_pos_age_sec"),
        "weekly_event": live["weekly_event"],
        "companions": companions,
        "work_orders": work_orders,
        "humans": humans,
        "owner_levels": owner_levels,
        "owner_bindings": owner_bindings,
        "companion_level_lead": companion_level_lead,
        "qm_focus": qm_focus,
        "qm_mats_by_skill": qm_mats_by_skill,
        "qm_directives_meta": qm_meta,
        "proposals_by_plan_id": proposals_by_plan_id,
    }
    # Export per-companion level lead/cap status cleanly (Issue #14 & #23)
    for comp in companions.values():
        cap_st = _xp_cap_prof_catchup_state(comp, world_ctx)
        comp["level_lead_state"] = {
            "companion_level_lead": companion_level_lead,
            "owner_cap_level": int(cap_st["cap_level"]),
            "at_level_cap": bool(cap_st["at_cap"]),
            "near_level_cap": bool(cap_st["near_cap"]),
            "xp_eligible": comp.get("xp_eligible"),
        }
    return world_ctx


def load_companion_level_lead(conf_path: Path | None = None) -> int:
    """Read authoritative AiPlayerbot.CompanionLevelLead from playerbots.conf or env override (Issue #23)."""
    env_override = (os.getenv("HEARTHCORE_COMPANION_LEVEL_LEAD") or "").strip()
    if env_override:
        try:
            return max(0, int(env_override))
        except ValueError as exc:
            logger.warning("Invalid HEARTHCORE_COMPANION_LEVEL_LEAD=%r: %s", env_override, exc)

    cfg = conf_path
    if cfg is None:
        env_cfg = (os.getenv("HEARTHCORE_PLAYERBOTS_CONF") or "").strip()
        cfg = Path(env_cfg) if env_cfg else (
            Path.home() / "AzerothCore" / "azerothcore-wotlk" / "env" / "dist" / "etc" / "modules" / "playerbots.conf"
        )
    try:
        if cfg.is_file():
            for raw in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = raw.split("#", 1)[0].strip()
                if not ln or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                if k.strip() == "AiPlayerbot.CompanionLevelLead":
                    return max(0, int(v.strip().strip('"').strip("'")))
    except (OSError, ValueError) as exc:
        logger.warning("Failed reading CompanionLevelLead from %s: %s", cfg, exc)
    return 3


def discover_owner_bindings(companions: dict[str, dict[str, Any]] | None = None) -> dict[str, str]:
    """Build shift_group -> owner_character mapping from HEARTHCORE_OWNER_GROUPS and `core-<owner>` groups (Issue #23)."""
    bindings: dict[str, str] = {}
    # 1. Dynamic convention: any shift_group named `core-<owner>` binds to `<owner>`
    if isinstance(companions, dict):
        for comp in companions.values():
            sg = str(comp.get("shift_group") or "").strip().lower()
            if sg.startswith("core-") and len(sg) > 5:
                bindings[sg] = sg[5:]
            explicit_owner = str(comp.get("owner_name") or "").strip().lower()
            if sg and explicit_owner:
                bindings[sg] = explicit_owner

    # 2. Explicit env config overrides/adds mappings: e.g. `core-wonky:Wonky,core-logan:Logan,team-alex:Alex`
    raw_env = (os.getenv("HEARTHCORE_OWNER_GROUPS") or "").strip()
    for pair in raw_env.split(","):
        if ":" in pair:
            grp, owner = pair.split(":", 1)
            g_k = grp.strip().lower()
            o_v = owner.strip().lower()
            if g_k and o_v:
                bindings[g_k] = o_v
    return bindings


def _xp_cap_prof_catchup_state(comp: dict[str, Any], world_ctx: dict[str, Any]) -> dict[str, Any]:
    """Compute authoritative per-character XP-cap & primary-profession catch-up state (Issue #23).

    Rules:
    - Scoped ONLY to companions with an authoritative owner-cap binding (`xp_cap_level`, `owner_name`,
      `world_ctx["owner_bindings"]`, or `core-<owner>` shift group). Ordinary guild bots never inherit
      any owner's level cap!
    - Reads authoritative `companion_level_lead` from `playerbots.conf` (`AiPlayerbot.CompanionLevelLead`)
      or `HEARTHCORE_COMPANION_LEVEL_LEAD` (fallback 3).
    - Respects explicit `xp_eligible` exported by runtime (`live_positions.json` / C++ seam).
    - Distinguishes `at_cap` (0 XP) from `near_cap` (1 level below cap, still earning XP).
    - Respects trained skill ceilings (`val >= mx` and `mx < 300` -> needs profession trainer!).
    """
    lvl = int(comp.get("level") or 0)
    sg = str(comp.get("shift_group") or "").strip().lower()
    owner_levels = world_ctx.get("owner_levels") or {}
    owner_bindings = world_ctx.get("owner_bindings") or discover_owner_bindings(world_ctx.get("companions"))
    if world_ctx.get("companion_level_lead") is not None:
        level_lead = max(0, int(world_ctx["companion_level_lead"]))
    else:
        level_lead = load_companion_level_lead()

    cap_lvl = 0
    if comp.get("xp_cap_level") is not None:
        cap_lvl = int(comp["xp_cap_level"])
    else:
        owner_name = str(comp.get("owner_name") or "").strip().lower()
        if not owner_name and sg:
            owner_name = owner_bindings.get(sg, "")
            if not owner_name and sg.startswith("core-") and len(sg) > 5:
                owner_name = sg[5:]
        if owner_name and owner_levels.get(owner_name, 0) > 0:
            cap_lvl = int(owner_levels[owner_name]) + level_lead

    if cap_lvl <= 0 and comp.get("xp_eligible") is not False:
        return {
            "mode": "none",
            "at_cap": False,
            "near_cap": False,
            "cap_level": 0,
            "lagging_primary": False,
            "needs_trainer": False,
            "target_skill": 0,
        }

    explicit_xp_elig = comp.get("xp_eligible")
    at_cap = (explicit_xp_elig is False) or (cap_lvl > 0 and lvl >= cap_lvl)
    near_cap = (not at_cap) and (cap_lvl > 0 and lvl == cap_lvl - 1)

    if not at_cap and not near_cap:
        return {
            "mode": "none",
            "at_cap": False,
            "near_cap": False,
            "cap_level": cap_lvl,
            "lagging_primary": False,
            "needs_trainer": False,
            "target_skill": 0,
        }

    skills = comp.get("skills") or {}
    prof_cap = int(world_ctx.get("prof_cap") or 300)
    target_skill = min(prof_cap, max(75, lvl * 4))
    needs_trainer = False
    lagging_trainable = False
    for sid, (val, mx) in skills.items():
        if sid not in CRAFT_SKILLS and sid not in GATHER_SKILLS:
            continue
        v_i, mx_i = int(val), int(mx)
        if v_i >= mx_i and mx_i < prof_cap and mx_i < target_skill:
            needs_trainer = True
        elif v_i < min(target_skill, mx_i):
            lagging_trainable = True

    active_catchup = lagging_trainable or needs_trainer
    mode = "at_cap" if (at_cap and active_catchup) else ("near_cap" if (near_cap and active_catchup) else "none")
    return {
        "mode": mode,
        "at_cap": mode == "at_cap",
        "near_cap": mode == "near_cap",
        "cap_level": cap_lvl,
        "lagging_primary": lagging_trainable,
        "needs_trainer": needs_trainer,
        "target_skill": target_skill,
    }


def _is_xp_capped_prof_catchup(comp: dict[str, Any], world_ctx: dict[str, Any]) -> tuple[bool, int]:
    """Compatibility wrapper returning (True, cap_level) ONLY when companion is actually AT XP cap with lagging primary skills."""
    st = _xp_cap_prof_catchup_state(comp, world_ctx)
    return bool(st["at_cap"]), int(st["cap_level"])


def build_snapshot(
    comp: dict[str, Any],
    world_ctx: dict[str, Any],
    *,
    memory_ctx: dict[str, Any] | None = None,
    order_reservations: dict[int, str] | None = None,
    char_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a strictly validated candidate snapshot for one companion from authoritative DB state."""
    lvl = comp["level"]
    zone = comp["zone"]
    skills = comp["skills"]
    weekly = world_ctx.get("weekly_event", "")
    is_craft_night = "Craft" in weekly
    is_fish_night = "Fishing" in weekly
    is_sunday = "Sunday" in weekly
    res_map = order_reservations or {}
    catchup = _xp_cap_prof_catchup_state(comp, world_ctx)
    xp_capped_catchup = bool(catchup["at_cap"])
    near_cap_catchup = bool(catchup["near_cap"])
    cap_lvl = int(catchup["cap_level"])
    needs_trainer = bool(catchup["needs_trainer"])
    target_skill = int(catchup["target_skill"] or min(225, max(75, lvl * 4)))

    candidates: list[dict[str, Any]] = []

    if xp_capped_catchup:
        # Companion is AT owner XP cap (0 XP from quests/mobs!) — keep gear/rep/gold turn-ins viable while preferring lagging professions.
        q_score = 35
        q_desc = f"Quest around {zone} (Lv {lvl} at XP cap {cap_lvl}: 0 XP; gear/gold/rep only — catch up lagging professions!)"
    elif near_cap_catchup:
        # 1 level below cap still earns full XP — keep questing viable while gently encouraging professions!
        q_score = 72
        q_desc = f"Complete quests and turn-ins around {zone} (Lv {lvl}, 1 level below cap {cap_lvl}; still earns XP)"
    else:
        q_score = 78 if lvl < 20 else 72
        if comp["shift_group"] in ("core-wonky", "core-logan"):
            q_score = min(92, q_score + 8)
        q_desc = f"Complete quests and turn-ins around {zone} (Lv {lvl})"
    candidates.append({
        "id": f"quest:zone_{comp['zone_id']}",
        "type": "quest",
        "score": q_score,
        "description": q_desc[:240],
    })

    eligible_wos = []
    for wo in world_ctx.get("work_orders", []):
        if wo["requester_guid"] == comp["guid"]:
            continue
        sk = wo["skill"]
        if not sk or sk not in skills:
            continue
        comp_sk_val = skills[sk][0]
        tpl = ORDER_BY_ENTRY.get(wo["item_entry"])
        if tpl and comp_sk_val + 15 < tpl.min_skill:
            continue
        holder = res_map.get(int(wo["id"]))
        claimed_by_other = bool(holder and holder != comp["slug"])
        eligible_wos.append((claimed_by_other, holder or "", wo, sk, comp_sk_val, tpl))

    eligible_wos.sort(key=lambda x: (1 if x[0] else 0, -(x[5].priority if x[5] else 1)))
    for claimed_by_other, holder, wo, sk, comp_sk_val, tpl in eligible_wos[:3]:
        score = 88 if is_craft_night else 82
        if tpl and tpl.priority == 2:
            score = min(90, score + 2)
        claim_note = ""
        if claimed_by_other:
            score = max(40, score - 25)
            claim_note = f" [Claimed by {holder.title()}]"
        candidates.append({
            "id": f"guild_order:{wo['id']}:{_slug_token(wo['item_name'])}",
            "type": "guild_order",
            "score": score,
            "description": f"Craft {wo['quantity']}x {wo['item_name']} for {wo['requester_name']} ({SKILL_NAMES.get(sk, 'craft')} {comp_sk_val}){claim_note}"[:240],
        })

    qm_focus = world_ctx.get("qm_focus") or set()
    qm_mats = world_ctx.get("qm_mats_by_skill") or {}
    for sk_id, (val, mx) in sorted(skills.items()):
        if sk_id in CRAFT_SKILLS:
            sk_name = SKILL_NAMES[sk_id]
            gap = max(0, min(lvl * 5, mx) - val)
            qm_boost = 10 if sk_name.lower() in qm_focus else 0
            at_trained_cap = (val >= mx and mx < 300)
            catchup_boost = 0
            if not at_trained_cap and val < target_skill:
                if xp_capped_catchup:
                    catchup_boost = 18
                elif near_cap_catchup:
                    catchup_boost = 8
            score = min(96, 52 + gap // 2 + (14 if is_craft_night else 0) + qm_boost + catchup_boost)
            candidates.append({
                "id": f"craft:{_slug_token(sk_name)}",
                "type": "craft",
                "score": max(45, score),
                "description": (
                    f"Advance {sk_name} ({val}/{mx}) and supply Hearthguard"
                    + (" [At Trained Cap — Visit Trainer!]" if at_trained_cap else "")
                    + (" [XP-Cap Profession Catch-Up!]" if catchup_boost >= 18 else "")
                    + (" [Guild Priority]" if qm_boost else "")
                )[:240],
            })
        elif sk_id in GATHER_SKILLS:
            sk_name = SKILL_NAMES[sk_id]
            gap = max(0, min(lvl * 5, mx) - val)
            needed_names = qm_mats.get(sk_name.lower(), [])[:3]
            qm_boost = 12 if (sk_name.lower() in qm_focus or needed_names) else 0
            at_trained_cap = (val >= mx and mx < 300)
            catchup_boost = 0
            if not at_trained_cap and val < target_skill:
                if xp_capped_catchup:
                    catchup_boost = 18
                elif near_cap_catchup:
                    catchup_boost = 8
            score = min(96, 54 + gap // 2 + (8 if is_craft_night else 0) + qm_boost + catchup_boost)
            need_txt = f" — needs {', '.join(needed_names)}" if needed_names else ""
            spot_hint = best_farm_hint(sk_id, lvl)
            hint_txt = f" | {spot_hint}" if spot_hint else ""
            candidates.append({
                "id": f"gather:{_slug_token(sk_name)}",
                "type": "gather",
                "score": max(45, score),
                "description": (
                    f"Gather {sk_name} ({val}/{mx}) for guild vault{need_txt}{hint_txt}"
                    + (" [At Trained Cap — Visit Trainer!]" if at_trained_cap else "")
                    + (" [XP-Cap Catch-Up!]" if catchup_boost >= 18 else "")
                )[:240],
            })

    if 356 in skills:
        f_val, f_mx = skills[356]
        fish_need = qm_mats.get("fishing", [])[:2]
        f_boost = 12 if ("fishing" in qm_focus or fish_need) else 0
        f_score = 88 if is_fish_night else min(86, (58 if f_val < lvl * 4 else 48) + f_boost)
        fish_txt = f" ({', '.join(fish_need)} Mp5)" if fish_need else ""
        spot_hint = best_farm_hint(356, lvl)
        hint_txt = f" | {spot_hint}" if spot_hint else ""
        candidates.append({
            "id": "fish:hearthguard",
            "type": "fish",
            "score": f_score,
            "description": (f"Fish for guild cooking (Fishing {f_val}/{f_mx}){fish_txt}{hint_txt}"
                            + (" [Fishing Night!]" if is_fish_night else ""))[:240],
        })

    if 185 in skills or 129 in skills:
        parts = []
        if 185 in skills:
            parts.append(f"Cooking {skills[185][0]}")
        if 129 in skills:
            parts.append(f"First Aid {skills[129][0]}")
        candidates.append({
            "id": "profession:secondaries",
            "type": "profession",
            "score": 56 + (10 if is_craft_night else 0),
            "description": f"Keep secondary skills stocked ({', '.join(parts)}) [Advisory]",
        })

    if lvl >= 15:
        d_score = 84 if is_sunday else 66
        candidates.append({
            "id": "dungeon:prep",
            "type": "dungeon",
            "score": d_score,
            "description": (f"Prepare gear/consumables for Lv {lvl} dungeons [Advisory]"
                            + (" [Sunday Dungeon Day!]" if is_sunday else ""))[:240],
        })

    t_score = 90 if needs_trainer else (70 if (lvl % 2 == 0 and lvl >= 4) else 52)
    t_desc = (
        f"Visit profession trainer to unlock next skill cap (at trained ceiling, Lv {lvl})"
        if needs_trainer
        else f"Visit class or profession trainer at level {lvl} [Advisory]"
    )
    candidates.append({
        "id": "train:skills",
        "type": "train",
        "score": t_score,
        "description": t_desc[:240],
    })
    candidates.append({
        "id": "bank:vault",
        "type": "bank",
        "score": 54,
        "description": "Deposit spare trade goods and herbs into Hearthguard guild bank [Advisory]",
    })
    candidates.append({
        "id": "repair:vendor",
        "type": "repair",
        "score": 55,
        "description": "Sell gray clutter, restock essentials, and repair gear [Advisory]",
    })

    near_owner = next(
        (h for h in world_ctx.get("humans", []) if abs(h["level"] - lvl) <= 6),
        None,
    )
    if near_owner:
        candidates.append({
            "id": f"explore:near_{_slug_token(near_owner['name'])}",
            "type": "explore",
            "score": 76 if not is_sunday else 84,
            "description": f"Adventure in {near_owner['zone']} alongside {near_owner['name']} (Lv {near_owner['level']}) [Advisory]",
        })
    else:
        candidates.append({
            "id": "explore:frontier",
            "type": "explore",
            "score": 52,
            "description": f"Explore neighboring hubs and questing roads near {zone} [Advisory]",
        })

    if memory_ctx:
        fat_cat = str(memory_ctx.get("fatigue_category") or "")
        stalled_id = str(memory_ctx.get("stalled_goal_id") or "")
        for cand in candidates:
            sc = int(cand["score"])
            if fat_cat:
                c_cat = goal_category(cand["id"])
                if c_cat == fat_cat:
                    sc -= 4 if cand["type"] == "guild_order" else 8
                elif cand["type"] in ("quest", "gather", "craft", "fish"):
                    sc += 6
            if stalled_id and cand["id"] == stalled_id and not is_advisory_goal(cand["id"]):
                sc -= 15
                cand["description"] = f"{cand['description']} [Stalled: 0 progress]"[:240]
            cand["score"] = max(1, min(100, sc))

    has_fat = bool(memory_ctx and memory_ctx.get("fatigue_category"))
    has_stall = bool(memory_ctx and memory_ctx.get("stalled_goal_id"))
    if char_profile is not None:
        candidates, _ = apply_playstyle_scoring(
            candidates,
            char_profile,
            has_fatigue_nudge=has_fat,
            has_stall_caution=has_stall,
        )

    snap = {
        "character_id": comp["slug"],
        "observed_at": world_ctx["observed_at"],
        "candidates": candidates,
    }
    return snapshot(snap, comp["slug"])


def load_fresh_quartermaster_directives(
    qm_path: Path = QUARTERMASTER_PATH,
    *,
    now_ts: int | None = None,
    max_age_sec: int | None = None,
) -> tuple[set[str], dict[str, list[str]], dict[str, Any]]:
    """Load live quartermaster.json ONLY when non-shadow and fresh (Issue #61)."""
    ts = int(now_ts if now_ts is not None else time.time())
    limit_sec = int(max_age_sec if max_age_sec is not None else os.getenv("HEARTHCORE_QM_MAX_AGE_SEC", str(DEFAULT_QM_MAX_AGE_SEC)))
    qm_focus: set[str] = set()
    qm_mats: dict[str, list[str]] = {}
    meta: dict[str, Any] = {"present": False, "stale": False, "age_sec": None, "max_age_sec": limit_sec}
    if not qm_path.is_file():
        return qm_focus, qm_mats, meta
    meta["present"] = True
    try:
        qm = json.loads(qm_path.read_text(encoding="utf-8"))
        if qm.get("is_shadow"):
            meta["stale"] = True
            return qm_focus, qm_mats, meta
        upd = str(qm.get("updated_at") or "")
        if upd:
            dt = datetime.strptime(upd, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age = max(0, ts - int(dt.timestamp()))
        else:
            age = max(0, int(ts - qm_path.stat().st_mtime))
        meta["age_sec"] = age
        if age > limit_sec:
            meta["stale"] = True
            logger.warning("Ignoring stale quartermaster.json (age=%ds > max=%ds)", age, limit_sec)
            return qm_focus, qm_mats, meta
        qm_focus = {str(s).lower() for s in (qm.get("focus_skills") or [])}
        for br in qm.get("bank_requests") or []:
            gsk = str(br.get("gather_skill") or "").lower()
            if gsk:
                qm_mats.setdefault(gsk, []).append(str(br.get("name") or ""))
    except Exception as exc:
        meta["stale"] = True
        logger.warning("Failed reading quartermaster.json: %s: %s", type(exc).__name__, exc)
    return qm_focus, qm_mats, meta


def persona_baseline_weights(char_profile: dict[str, Any] | None) -> dict[str, float]:
    """Map character profile preferences into the canonical native CompanionBrain weight namespace (Issue #60)."""
    base: dict[str, float] = {}
    for keys in PROFILE_PREF_TO_NATIVE_WEIGHTS.values():
        for k in keys:
            base[k] = 1.0
    if not char_profile:
        return base
    prefs = char_profile.get("preferences") or {}
    for pkey, native_keys in PROFILE_PREF_TO_NATIVE_WEIGHTS.items():
        if pkey in prefs and prefs[pkey] is not None:
            val = _clamp(float(prefs[pkey]))
            for nk in native_keys:
                base[nk] = val
    return base


def weights_for_cpp(
    plan: dict[str, Any],
    snap: dict[str, Any],
    *,
    xp_capped_catchup: bool = False,
    char_profile: dict[str, Any] | None = None,
    only_changed: bool = False,
) -> dict[str, float]:
    """Translate validated Hearthcore plan into bounded 0.5-1.5 keys read by CompanionBrain.cpp (Issue #60).

    Starts from the companion's persona baseline (`char_profile["preferences"]`) instead of flattening
    omitted keys to 1.0, because native CompanionBrain.cpp merges overrides by REPLACE (except manualKeys).
    """
    mods = plan["weight_modifiers"]
    cand_type = {c["id"]: c["type"] for c in snap["candidates"]}
    primary_type = cand_type.get(plan["primary_goal_id"], "")
    baseline = persona_baseline_weights(char_profile)

    def pref_default(t: str) -> float:
        native_keys = PROFILE_PREF_TO_NATIVE_WEIGHTS.get(t)
        if native_keys:
            return baseline.get(native_keys[0], 1.0)
        return 1.0

    def eff(t: str, default: float | None = None) -> float:
        dflt = pref_default(t) if default is None else default
        base = float(mods.get(t, dflt))
        if primary_type == t:
            base += 0.15
        return _clamp(base)

    q = eff("quest")
    if primary_type in ("explore", "dungeon"):
        q = _clamp(max(q, eff(primary_type, 1.0) * 0.95))

    craft_val = max(
        eff("craft"),
        eff("profession", baseline["craft"]) * 0.98 if "profession" in mods or primary_type == "profession" else 0.5,
        eff("guild_order", baseline["craft"]) if "guild_order" in mods or primary_type == "guild_order" else 0.5,
    )
    craft_val = _clamp(craft_val)
    gather_val = eff("gather")
    fish_val = eff("fish")
    train_val = eff("train")
    travel_val = eff("travel")
    hearth_val = eff("rest")
    bank_val = eff("bank")
    repair_val = eff("repair")
    sell_val = _clamp((repair_val + bank_val) / 2.0) if ("repair" in mods or "bank" in mods) else baseline["sell"]

    # Issue #23: When AT XP cap with lagging primary professions, suppress 0-XP mob grinding
    # while preserving necessary travel (trainers/nodes), quest turn-ins (gear/gold/rep), and
    # fishing if fishing is the explicit primary goal!
    if xp_capped_catchup and primary_type in ("craft", "gather", "guild_order", "train", "profession"):
        q = _clamp(min(q, 0.55))
        fish_val = _clamp(min(fish_val, 0.65))
        craft_val = _clamp(max(craft_val, 1.45))
        gather_val = _clamp(max(gather_val, 1.45))
        train_val = _clamp(max(train_val, 1.40))
        travel_val = _clamp(max(travel_val, 0.90))
        hearth_val = _clamp(max(hearth_val, 1.35))

    full = {
        "quest_work": q,
        "quest_turnin": _clamp(max(q, 0.95 if xp_capped_catchup else max(1.0, baseline["quest_turnin"]))),
        "quest_hub": _clamp(max(q, 0.75 if xp_capped_catchup else q)),
        "quest_grind": _clamp(min(q, 0.50 if xp_capped_catchup else 1.15)),
        "craft": craft_val,
        "gather": gather_val,
        "fish": fish_val,
        "train": train_val,
        "bank": bank_val,
        "repair": repair_val,
        "sell": sell_val,
        "travel": travel_val,
        "hearth": hearth_val,
    }
    if only_changed:
        return {k: v for k, v in full.items() if abs(v - baseline.get(k, 1.0)) >= 0.01}
    return full


def build_proposal_spec(plan: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    """Extract typed, versioned native objective metadata for companion_proposals (Issue #3)."""
    gid = str(plan["primary_goal_id"])
    gtype = goal_prefix(gid)
    advisory = is_advisory_goal(gid)
    target_id = 0
    if gtype == "guild_order":
        target_id = int(parse_order_id(gid) or 0)
        advisory = target_id <= 0
    elif gtype in ("craft", "gather"):
        parts = gid.split(":", 1)
        if len(parts) == 2:
            target_id = int(SKILL_BY_SLUG.get(parts[1], 0))
    elif gtype == "fish":
        target_id = 356
    elif gtype == "quest":
        parts = gid.split("zone_")
        if len(parts) == 2 and parts[1].isdigit():
            target_id = int(parts[1])
    return {
        "goal_id": gid[:96],
        "goal_type": gtype[:24],
        "target_id": target_id,
        "advisory_only": 1 if advisory else 0,
    }


def apply_plan_to_db(
    guid: int,
    character_name: str,
    plan: dict[str, Any],
    snap: dict[str, Any],
    source: str,
    *,
    plan_id: int = 0,
    now_ts: int | None = None,
    sql_exec=None,
    xp_capped_catchup: bool = False,
    char_profile: dict[str, Any] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Re-validate plan and atomically write expiring overrides + typed proposal to MySQL.

    Issues #3, #6 & #60:
    - NEVER overwrites canonical persona/manual rows in `companion_weights`.
    - Starts override weights from companion persona baseline (`char_profile["preferences"]`).
    - Writes expiring overrides to `companion_weight_overrides` with monotonic revision guard.
    - Writes a typed, versioned proposal row to `companion_proposals` for native C++ validation.
    """
    validate(plan, snap)
    ts = int(now_ts if now_ts is not None else time.time())
    expires_at = ts + int(plan["session_minutes"]) * 60
    revision = int(plan_id) if int(plan_id) > 0 else ts
    cpp_weights = weights_for_cpp(plan, snap, xp_capped_catchup=xp_capped_catchup, char_profile=char_profile)
    prop = build_proposal_spec(plan, snap)
    db_src = "hearthcore_ai" if source == "anthropic" else ("hearthcore_holdout" if source == "fallback_holdout" else "hearthcore_fb")

    stmts: list[str] = ["START TRANSACTION;"]
    for k, v in sorted(cpp_weights.items()):
        # Preserve unexpired manual operator focus presets (source='operator' AND expires_at > ts)
        op_guard = f"NOT (`source` = 'operator' AND `enabled` = 1 AND `expires_at` > {ts})"
        stmts.append(
            "INSERT INTO companion_weight_overrides "
            "(`guid`, `key`, `value`, `plan_id`, `revision`, `source`, `enabled`, `created_at`, `expires_at`) "
            f"VALUES ({int(guid)}, '{_sql_esc(k)}', {v:.3f}, {int(plan_id)}, {revision}, '{db_src}', 1, {ts}, {expires_at}) "
            "ON DUPLICATE KEY UPDATE "
            f"`value` = IF(VALUES(`revision`) >= `revision` AND {op_guard}, VALUES(`value`), `value`), "
            f"`plan_id` = IF(VALUES(`revision`) >= `revision` AND {op_guard}, VALUES(`plan_id`), `plan_id`), "
            f"`source` = IF(VALUES(`revision`) >= `revision` AND {op_guard}, VALUES(`source`), `source`), "
            f"`created_at` = IF(VALUES(`revision`) >= `revision` AND {op_guard}, VALUES(`created_at`), `created_at`), "
            f"`expires_at` = IF(VALUES(`revision`) >= `revision` AND {op_guard}, VALUES(`expires_at`), `expires_at`), "
            "`revision` = GREATEST(`revision`, VALUES(`revision`));"
        )

    # Supersede older open proposals for this character, then insert this versioned proposal
    stmts.append(
        f"UPDATE companion_proposals SET status = 'expired', outcome_reason = 'superseded', ended_at = {ts} "
        f"WHERE guid = {int(guid)} AND status IN ('proposed', 'accepted') AND plan_id < {int(plan_id)};"
    )
    init_status = "proposed"
    init_reason = "advisory_soft_weights" if prop["advisory_only"] else ""
    stmts.append(
        "INSERT INTO companion_proposals "
        "(`plan_id`, `guid`, `character_name`, `goal_id`, `goal_type`, `target_id`, `advisory_only`, "
        "`snapshot_rev`, `status`, `outcome_reason`, `created_at`, `expires_at`) "
        f"VALUES ({int(plan_id)}, {int(guid)}, '{_sql_esc(character_name)}', '{_sql_esc(prop['goal_id'])}', "
        f"'{_sql_esc(prop['goal_type'])}', {int(prop['target_id'])}, {int(prop['advisory_only'])}, "
        f"{revision}, '{init_status}', '{_sql_esc(init_reason)}', {ts}, {expires_at}) "
        "ON DUPLICATE KEY UPDATE "
        "`expires_at` = IF(VALUES(`snapshot_rev`) >= `snapshot_rev`, VALUES(`expires_at`), `expires_at`);"
    )
    stmts.append("COMMIT;")
    runner = sql_exec if sql_exec is not None else run_sql
    runner("\n".join(stmts) + "\n")
    return cpp_weights, prop


def set_override_enabled(guid: int | None, enabled: bool, *, sql_exec=None) -> None:
    """Global (guid=None) or per-character toggle for temporary AI weight overrides (Issue #6)."""
    flag = 1 if enabled else 0
    if guid is None:
        sql = f"UPDATE companion_weight_overrides SET enabled = {flag};"
    else:
        sql = f"UPDATE companion_weight_overrides SET enabled = {flag} WHERE guid = {int(guid)};"
    runner = sql_exec if sql_exec is not None else run_sql
    runner(sql + "\n")


class BrainStore:
    """SQLite audit, session-expiry tracker, and non-overlapping outcome store."""

    def __init__(self, path: Path = BRAIN_DB_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sequence_floor = 0
        with self._conn() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("""
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY,
                    planned_at INTEGER NOT NULL,
                    planned_iso TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    character_id TEXT NOT NULL,
                    game_guid INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    model TEXT NOT NULL,
                    effort TEXT NOT NULL,
                    primary_goal_id TEXT NOT NULL,
                    primary_desc TEXT NOT NULL,
                    secondary_goal_ids TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    session_minutes INTEGER NOT NULL,
                    weight_modifiers TEXT NOT NULL,
                    cpp_weights TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    thinking_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    applied INTEGER NOT NULL DEFAULT 0,
                    error_note TEXT NOT NULL DEFAULT '',
                    observed_at TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_plans_char_exp ON plans(character_id, expires_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_plans_day ON plans(day_key, source)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS brain_meta (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS operator_directives (
                    character_id TEXT PRIMARY KEY,
                    note TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    consumed_at INTEGER NOT NULL DEFAULT 0
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS paused_characters (
                    character_id TEXT PRIMARY KEY,
                    paused_at INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT 'operator'
                )
            """)
            existing_cols = {r[1] for r in db.execute("PRAGMA table_info(plans)").fetchall()}
            for col, typ in [
                ("start_level", "INTEGER NOT NULL DEFAULT 0"),
                ("start_xp", "INTEGER NOT NULL DEFAULT 0"),
                ("start_skill_sum", "INTEGER NOT NULL DEFAULT 0"),
                ("start_orders_done", "INTEGER NOT NULL DEFAULT 0"),
                ("start_quests_done", "INTEGER NOT NULL DEFAULT 0"),
                ("start_ledger_ops", "INTEGER NOT NULL DEFAULT 0"),
                ("outcome_levels", "INTEGER NOT NULL DEFAULT 0"),
                ("outcome_xp", "INTEGER NOT NULL DEFAULT 0"),
                ("outcome_skillups", "INTEGER NOT NULL DEFAULT 0"),
                ("outcome_orders", "INTEGER NOT NULL DEFAULT 0"),
                ("outcome_quests", "INTEGER NOT NULL DEFAULT 0"),
                ("outcome_ledger_ops", "INTEGER NOT NULL DEFAULT 0"),
                ("outcome_checked_at", "INTEGER NOT NULL DEFAULT 0"),
                ("outcome_finalized", "INTEGER NOT NULL DEFAULT 0"),
                ("plan_state", "TEXT NOT NULL DEFAULT 'applied'"),
                ("proposal_status", "TEXT NOT NULL DEFAULT ''"),
                ("proposal_reason", "TEXT NOT NULL DEFAULT ''"),
                ("advisory_only", "INTEGER NOT NULL DEFAULT 0"),
            ]:
                if col not in existing_cols:
                    db.execute(f"ALTER TABLE plans ADD COLUMN {col} {typ}")
            # Mark any historical unapplied rows explicitly as 'shadow' (Issue #7 provenance)
            db.execute("UPDATE plans SET plan_state = 'shadow' WHERE applied = 0 AND plan_state = 'applied'")
            floor_row = db.execute("SELECT v FROM brain_meta WHERE k = 'sequence_floor'").fetchone()
            if floor_row and str(floor_row["v"]).isdigit():
                self._sequence_floor = int(floor_row["v"])

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def get_meta(self, key: str, default: str = "") -> str:
        with self._conn() as db:
            row = db.execute("SELECT v FROM brain_meta WHERE k = ?", (key,)).fetchone()
            return str(row["v"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as db:
            db.execute(
                "INSERT INTO brain_meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, str(value)),
            )

    def set_operator_directive(
        self,
        slug: str,
        note: str,
        *,
        now_ts: int | None = None,
        ttl_hours: int = 24,
    ) -> dict[str, Any]:
        """Store or clear a bounded guild-leader directive for one companion's next Claude plan."""
        ts = int(now_ts if now_ts is not None else time.time())
        clean_slug = str(slug or "").strip().lower()
        clean_note = re.sub(r"[\x00-\x1f\x7f]+", " ", str(note or "")).strip()[:240]
        hours = max(1, min(168, int(ttl_hours)))
        with self._conn() as db:
            if not clean_note:
                db.execute("DELETE FROM operator_directives WHERE character_id = ?", (clean_slug,))
                return {"character_id": clean_slug, "note": "", "active": False, "updated_at": ts, "expires_at": 0, "consumed_at": 0}
            exp = ts + hours * 3600
            db.execute(
                """
                INSERT INTO operator_directives (character_id, note, updated_at, expires_at, consumed_at)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(character_id) DO UPDATE SET
                    note = excluded.note,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at,
                    consumed_at = 0
                """,
                (clean_slug, clean_note, ts, exp),
            )
            return {
                "character_id": clean_slug,
                "note": clean_note,
                "active": True,
                "updated_at": ts,
                "expires_at": exp,
                "consumed_at": 0,
            }

    def get_operator_directive(
        self, slug: str, *, now_ts: int | None = None, include_consumed: bool = False
    ) -> dict[str, Any] | None:
        """Return active unexpired operator directive for slug (unconsumed only by default)."""
        ts = int(now_ts if now_ts is not None else time.time())
        clean_slug = str(slug or "").strip().lower()
        cond = "AND consumed_at = 0" if not include_consumed else ""
        with self._conn() as db:
            row = db.execute(
                f"""
                SELECT character_id, note, updated_at, expires_at, consumed_at
                FROM operator_directives
                WHERE character_id = ? AND expires_at > ? {cond}
                """,
                (clean_slug, ts),
            ).fetchone()
            if not row:
                return None
            return {
                "character_id": str(row["character_id"]),
                "note": str(row["note"]),
                "active": int(row["consumed_at"] or 0) == 0,
                "updated_at": int(row["updated_at"]),
                "expires_at": int(row["expires_at"]),
                "consumed_at": int(row["consumed_at"] or 0),
            }

    def mark_operator_directive_consumed(self, slug: str, *, now_ts: int | None = None) -> None:
        ts = int(now_ts if now_ts is not None else time.time())
        clean_slug = str(slug or "").strip().lower()
        with self._conn() as db:
            db.execute(
                "UPDATE operator_directives SET consumed_at = ? WHERE character_id = ? AND expires_at > ? AND consumed_at = 0",
                (ts, clean_slug, ts),
            )

    def set_character_paused(self, slug: str, paused: bool, *, now_ts: int | None = None, reason: str = "operator") -> bool:
        """Persist per-character AI pause flag so run_cycle skips paused companions without burning API credits."""
        ts = int(now_ts if now_ts is not None else time.time())
        clean_slug = str(slug or "").strip().lower()
        with self._conn() as db:
            if paused:
                db.execute(
                    "INSERT INTO paused_characters (character_id, paused_at, reason) VALUES (?, ?, ?) "
                    "ON CONFLICT(character_id) DO UPDATE SET paused_at = excluded.paused_at, reason = excluded.reason",
                    (clean_slug, ts, str(reason)[:64]),
                )
            else:
                db.execute("DELETE FROM paused_characters WHERE character_id = ?", (clean_slug,))
        return bool(paused)

    def is_character_paused(self, slug: str) -> bool:
        clean_slug = str(slug or "").strip().lower()
        with self._conn() as db:
            row = db.execute("SELECT 1 FROM paused_characters WHERE character_id = ?", (clean_slug,)).fetchone()
            return row is not None

    def paused_character_slugs(self) -> set[str]:
        with self._conn() as db:
            return {str(r["character_id"]) for r in db.execute("SELECT character_id FROM paused_characters").fetchall()}

    def sync_sequence_floor(self, remote_max_rev: int) -> dict[str, Any]:
        """Detect if brain.db is behind MySQL revisions and advance monotonic sequence floor (Issue #53)."""
        remote_max = max(0, int(remote_max_rev))
        with self._conn() as db:
            row = db.execute("SELECT COALESCE(MAX(id), 0) AS mx FROM plans").fetchone()
            local_max = int(row["mx"] or 0)
            new_floor = max(self._sequence_floor, local_max, remote_max)
            behind = remote_max > max(local_max, self._sequence_floor)
            if new_floor > self._sequence_floor:
                self._sequence_floor = new_floor
                db.execute(
                    "INSERT INTO brain_meta (k, v) VALUES ('sequence_floor', ?) "
                    "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    (str(new_floor),),
                )
        return {
            "local_max": local_max,
            "remote_max": remote_max,
            "sequence_floor": self._sequence_floor,
            "resynced": behind,
        }

    def mark_plan_applied(self, plan_id: int) -> None:
        """Promote a pending plan to applied=1 ONLY after MySQL write acknowledges (Issue #52)."""
        with self._conn() as db:
            db.execute(
                "UPDATE plans SET applied = 1, plan_state = 'applied' WHERE id = ?",
                (int(plan_id),),
            )

    def recent_apply_failed(self, slug: str, now_ts: int, backoff_sec: int = DEFAULT_APPLY_FAILURE_BACKOFF_SEC) -> bool:
        """Return True if slug had a failed MySQL apply within backoff_sec (prevents paid retry storms)."""
        if backoff_sec <= 0:
            return False
        with self._conn() as db:
            row = db.execute(
                """
                SELECT planned_at FROM plans
                WHERE character_id = ? AND plan_state = 'apply_failed' AND planned_at > ?
                ORDER BY id DESC LIMIT 1
                """,
                (slug, int(now_ts) - int(backoff_sec)),
            ).fetchone()
            return row is not None

    def mark_plan_failed(self, plan_id: int, error_note: str) -> None:
        """Mark a pending plan as apply_failed so it never blocks replanning on the next tick (Issue #52)."""
        with self._conn() as db:
            db.execute(
                "UPDATE plans SET applied = 0, plan_state = 'apply_failed', proposal_status = 'apply_failed', "
                "error_note = ? WHERE id = ?",
                (str(error_note)[:200], int(plan_id)),
            )

    def refresh_outcomes(
        self,
        companions: dict[str, dict[str, Any]],
        now_ts: int,
        proposals_by_plan_id: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        """Update goal-specific outcome deltas ONCE per applied plan window (no overlapping double-credit, Issue #5)."""
        props = proposals_by_plan_id or {}
        with self._conn() as db:
            # Determine latest applied plan id per character so superseded plans finalize immediately
            latest_applied: dict[str, int] = {}
            for lr in db.execute(
                "SELECT character_id, MAX(id) AS max_id FROM plans WHERE applied = 1 AND plan_state = 'applied' GROUP BY character_id"
            ).fetchall():
                latest_applied[str(lr["character_id"])] = int(lr["max_id"])

            rows = db.execute(
                """
                SELECT id, character_id, expires_at, start_level, start_xp, start_skill_sum,
                       start_orders_done, start_quests_done, start_ledger_ops,
                       outcome_finalized, proposal_status, proposal_reason
                FROM plans
                WHERE applied = 1
                  AND plan_state = 'applied'
                  AND outcome_finalized = 0
                  AND start_level > 0
                ORDER BY id DESC LIMIT 200
                """
            ).fetchall()

            updates = []
            for r in rows:
                pid = int(r["id"])
                slug = str(r["character_id"])
                comp = companions.get(slug)
                if not comp:
                    continue
                is_superseded = latest_applied.get(slug, pid) > pid
                is_expired = int(r["expires_at"]) <= now_ts
                p_info = props.get(pid)
                p_status = str(p_info["status"]) if p_info else str(r["proposal_status"] or "")
                p_reason = str(p_info["outcome_reason"]) if p_info else str(r["proposal_reason"] or "")

                # Do NOT overwrite already-superseded older plans with fresh deltas from later plans
                if is_superseded and int(r["outcome_finalized"]) == 0:
                    updates.append((
                        None, None, None, None, None, None,
                        p_status, p_reason, 1, now_ts, pid,
                    ))
                    continue

                cur_lvl = int(comp["level"])
                cur_xp = int(comp.get("xp", 0))
                cur_sk = sum(v for v, _ in comp["skills"].values())
                cur_ord = int(comp.get("orders_delivered", 0))
                cur_q = int(comp.get("quests_rewarded", 0))
                cur_led = int(comp.get("ledger_ops", 0))

                d_lvl = max(0, cur_lvl - int(r["start_level"]))
                d_xp = max(0, cur_xp - int(r["start_xp"])) if d_lvl == 0 else max(1, cur_xp)
                d_sk = max(0, cur_sk - int(r["start_skill_sum"]))
                d_ord = max(0, cur_ord - int(r["start_orders_done"]))
                d_q = max(0, cur_q - int(r["start_quests_done"]))
                d_led = max(0, cur_led - int(r["start_ledger_ops"]))

                finalize = 1 if (is_expired or p_status in ("completed", "rejected", "interrupted", "expired")) else 0
                updates.append((d_lvl, d_xp, d_sk, d_ord, d_q, d_led, p_status, p_reason, finalize, now_ts, pid))

            for u in updates:
                if u[0] is None:
                    db.execute(
                        "UPDATE plans SET proposal_status=?, proposal_reason=?, outcome_finalized=?, outcome_checked_at=? WHERE id=?",
                        (u[6], u[7], u[8], u[9], u[10]),
                    )
                else:
                    db.execute(
                        """
                        UPDATE plans
                        SET outcome_levels=?, outcome_xp=?, outcome_skillups=?, outcome_orders=?,
                            outcome_quests=?, outcome_ledger_ops=?,
                            proposal_status=?, proposal_reason=?, outcome_finalized=?, outcome_checked_at=?
                        WHERE id=?
                        """,
                        u,
                    )

    def finalize_character_open_plans(
        self, slug: str, comp: dict[str, Any], now_ts: int, *, exclude_plan_id: int | None = None
    ) -> None:
        """Finalize any unfinalized active applied plan for slug right BEFORE marking a new plan applied."""
        with self._conn() as db:
            if exclude_plan_id is not None:
                rows = db.execute(
                    """
                    SELECT id, start_level, start_xp, start_skill_sum, start_orders_done,
                           start_quests_done, start_ledger_ops
                    FROM plans
                    WHERE character_id = ? AND applied = 1 AND plan_state = 'applied'
                      AND outcome_finalized = 0 AND id != ?
                    """,
                    (slug, int(exclude_plan_id)),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT id, start_level, start_xp, start_skill_sum, start_orders_done,
                           start_quests_done, start_ledger_ops
                    FROM plans
                    WHERE character_id = ? AND applied = 1 AND plan_state = 'applied'
                      AND outcome_finalized = 0
                    """,
                    (slug,),
                ).fetchall()
            if not rows:
                return
            cur_lvl = int(comp["level"])
            cur_xp = int(comp.get("xp", 0))
            cur_sk = sum(v for v, _ in comp["skills"].values())
            cur_ord = int(comp.get("orders_delivered", 0))
            cur_q = int(comp.get("quests_rewarded", 0))
            cur_led = int(comp.get("ledger_ops", 0))
            for r in rows:
                d_lvl = max(0, cur_lvl - int(r["start_level"])) if int(r["start_level"]) > 0 else 0
                d_xp = (max(0, cur_xp - int(r["start_xp"])) if d_lvl == 0 else max(1, cur_xp)) if int(r["start_level"]) > 0 else 0
                d_sk = max(0, cur_sk - int(r["start_skill_sum"])) if int(r["start_level"]) > 0 else 0
                d_ord = max(0, cur_ord - int(r["start_orders_done"])) if int(r["start_level"]) > 0 else 0
                d_q = max(0, cur_q - int(r["start_quests_done"])) if int(r["start_level"]) > 0 else 0
                d_led = max(0, cur_led - int(r["start_ledger_ops"])) if int(r["start_level"]) > 0 else 0
                db.execute(
                    """
                    UPDATE plans
                    SET outcome_levels=?, outcome_xp=?, outcome_skillups=?, outcome_orders=?,
                        outcome_quests=?, outcome_ledger_ops=?,
                        outcome_finalized=1, outcome_checked_at=?
                    WHERE id=?
                    """,
                    (d_lvl, d_xp, d_sk, d_ord, d_q, d_led, now_ts, int(r["id"])),
                )

    def active_plan(self, slug: str, now_ts: int) -> dict[str, Any] | None:
        """Return unexpired APPLIED live plan for slug (pending/failed/shadow never suppress live planning)."""
        with self._conn() as db:
            row = db.execute(
                """
                SELECT * FROM plans
                WHERE character_id = ? AND applied = 1 AND plan_state = 'applied' AND expires_at > ?
                ORDER BY id DESC LIMIT 1
                """,
                (slug, now_ts),
            ).fetchone()
            return dict(row) if row else None

    def daily_api_calls(self, day_key: str) -> int:
        with self._conn() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM plans WHERE day_key = ? AND source IN ('anthropic', 'fallback_after_api')",
                (day_key,),
            ).fetchone()
            return int(row[0]) if row else 0

    def record_plan(
        self,
        *,
        now_ts: int,
        slug: str,
        guid: int,
        source: str,
        model: str,
        effort: str,
        plan: dict[str, Any],
        primary_desc: str,
        cpp_weights: dict[str, float],
        meta: dict[str, Any],
        applied: bool,
        error_note: str,
        observed_at: str,
        start_level: int = 0,
        start_xp: int = 0,
        start_skill_sum: int = 0,
        start_orders_done: int = 0,
        start_quests_done: int = 0,
        start_ledger_ops: int = 0,
        advisory_only: bool = False,
        proposal_status: str = "",
        plan_state: str | None = None,
    ) -> int:
        dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        day_key = dt.strftime("%Y-%m-%d")
        expires_at = now_ts + int(plan["session_minutes"]) * 60
        p_state = plan_state if plan_state is not None else ("applied" if applied else "shadow")
        p_status = proposal_status or ("proposed" if applied else ("pending" if p_state == "pending" else "shadow"))
        with self._conn() as db:
            mx_row = db.execute("SELECT COALESCE(MAX(id), 0) AS mx FROM plans").fetchone()
            next_id = max(int(mx_row["mx"] or 0), self._sequence_floor) + 1
            cur = db.execute(
                """
                INSERT INTO plans (
                    id, planned_at, planned_iso, day_key, expires_at, character_id, game_guid,
                    source, model, effort, primary_goal_id, primary_desc, secondary_goal_ids,
                    reason_code, session_minutes, weight_modifiers, cpp_weights,
                    input_tokens, output_tokens, thinking_tokens, latency_ms,
                    applied, error_note, observed_at,
                    start_level, start_xp, start_skill_sum, start_orders_done,
                    start_quests_done, start_ledger_ops,
                    plan_state, proposal_status, advisory_only
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    next_id,
                    now_ts,
                    dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    day_key,
                    expires_at,
                    slug,
                    int(guid),
                    source,
                    model,
                    effort,
                    plan["primary_goal_id"],
                    primary_desc,
                    json.dumps(plan["secondary_goal_ids"]),
                    plan["reason_code"],
                    int(plan["session_minutes"]),
                    json.dumps(plan["weight_modifiers"], sort_keys=True),
                    json.dumps(cpp_weights, sort_keys=True),
                    int(meta.get("input_tokens", 0)),
                    int(meta.get("output_tokens", 0)),
                    int(meta.get("thinking_tokens", 0)),
                    int(meta.get("latency_ms", 0)),
                    1 if applied else 0,
                    error_note[:200],
                    observed_at,
                    int(start_level),
                    int(start_xp),
                    int(start_skill_sum),
                    int(start_orders_done),
                    int(start_quests_done),
                    int(start_ledger_ops),
                    p_state,
                    p_status,
                    1 if advisory_only else 0,
                ),
            )
            self._sequence_floor = max(self._sequence_floor, next_id)
            return int(cur.lastrowid or next_id)

    def summary(self, now_ts: int) -> dict[str, Any]:
        day_key = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        with self._conn() as db:
            # Latest APPLIED live plan per character (shadow/pending/failed excluded)
            latest_rows = db.execute("""
                SELECT p.* FROM plans p
                INNER JOIN (
                    SELECT character_id, MAX(id) AS max_id
                    FROM plans
                    WHERE applied = 1 AND plan_state = 'applied'
                    GROUP BY character_id
                ) m ON m.max_id = p.id
                ORDER BY p.planned_at DESC
            """).fetchall()
            day_row = db.execute("""
                SELECT
                    COUNT(*) AS total_today,
                    SUM(CASE WHEN source='anthropic' THEN 1 ELSE 0 END) AS ai_ok,
                    SUM(CASE WHEN source='fallback_after_api' THEN 1 ELSE 0 END) AS ai_err,
                    SUM(CASE WHEN source='fallback' THEN 1 ELSE 0 END) AS fb_only,
                    COALESCE(SUM(input_tokens), 0) AS in_tok,
                    COALESCE(SUM(output_tokens), 0) AS out_tok,
                    COALESCE(AVG(CASE WHEN source='anthropic' THEN latency_ms END), 0) AS avg_ms,
                    COALESCE(SUM(CASE WHEN applied=1 THEN outcome_levels ELSE 0 END), 0) AS out_lvls,
                    COALESCE(SUM(CASE WHEN applied=1 THEN outcome_skillups ELSE 0 END), 0) AS out_sks,
                    COALESCE(SUM(CASE WHEN applied=1 THEN outcome_orders ELSE 0 END), 0) AS out_ords
                FROM plans WHERE day_key = ?
            """, (day_key,)).fetchone()

        active: dict[str, Any] = {}
        for r in latest_rows:
            d = dict(r)
            active[d["character_id"]] = {
                "plan_id": int(d["id"]),
                "character_id": d["character_id"],
                "source": d["source"],
                "model": d["model"],
                "effort": d["effort"],
                "primary_goal_id": d["primary_goal_id"],
                "primary_desc": d["primary_desc"],
                "secondary_goal_ids": json.loads(d["secondary_goal_ids"]),
                "reason_code": d["reason_code"],
                "session_minutes": d["session_minutes"],
                "planned_at": d["planned_at"],
                "planned_iso": d["planned_iso"],
                "expires_at": d["expires_at"],
                "active": d["expires_at"] > now_ts,
                "applied": bool(d["applied"]),
                "plan_state": d.get("plan_state") or "applied",
                "proposal_status": d.get("proposal_status") or ("advisory" if d.get("advisory_only") else "proposed"),
                "proposal_reason": d.get("proposal_reason") or "",
                "advisory_only": bool(d.get("advisory_only") or is_advisory_goal(d["primary_goal_id"])),
                "outcome_class": classify_plan_outcome(d),
                "error_note": d["error_note"],
            }

        last_ok_raw = self.get_meta("last_success_at", "0")
        last_success_at = int(last_ok_raw) if last_ok_raw.isdigit() else 0
        stale_min = int(os.getenv("HEARTHCORE_STALE_MINUTES", str(DEFAULT_STALE_MINUTES)) or DEFAULT_STALE_MINUTES)
        stale_alarm = bool(last_success_at > 0 and (now_ts - last_success_at) > stale_min * 60)

        return {
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "day": day_key,
            "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            "effort": os.getenv("ANTHROPIC_EFFORT", "medium"),
            "daily_cap": int(os.getenv("HEARTHCORE_DAILY_CALL_CAP", "500") or 500),
            "last_success_at": last_success_at,
            "stale_alarm": stale_alarm,
            "stale_threshold_minutes": stale_min,
            "today": {
                "total": int(day_row["total_today"] or 0),
                "ai_ok": int(day_row["ai_ok"] or 0),
                "ai_err": int(day_row["ai_err"] or 0),
                "fallback": int(day_row["fb_only"] or 0),
                "input_tokens": int(day_row["in_tok"] or 0),
                "output_tokens": int(day_row["out_tok"] or 0),
                "avg_latency_ms": int(day_row["avg_ms"] or 0),
                "outcome_levels": int(day_row["out_lvls"] or 0),
                "outcome_skillups": int(day_row["out_sks"] or 0),
                "outcome_orders": int(day_row["out_ords"] or 0),
            },
            "ab_report": self.ab_outcome_report(now_ts=now_ts),
            "plans": active,
        }

    def ab_outcome_report(self, *, days: int = 14, now_ts: int | None = None) -> dict[str, Any]:
        """Compare outcomes & cost per session across AI vs deterministic fallback/holdout (Issue #65)."""
        from .budget import estimate_cost_usd

        ts = int(now_ts if now_ts is not None else time.time())
        cutoff = ts - max(1, int(days)) * 86400
        with self._conn() as db:
            rows = db.execute(
                """
                SELECT source, model, primary_goal_id, applied, plan_state, proposal_status, advisory_only,
                       start_level, outcome_levels, outcome_xp, outcome_skillups, outcome_orders,
                       outcome_quests, outcome_ledger_ops, input_tokens, output_tokens
                FROM plans
                WHERE applied = 1 AND plan_state = 'applied' AND planned_at >= ?
                """,
                (cutoff,),
            ).fetchall()

        by_source: dict[str, dict[str, Any]] = {}
        by_goal_type: dict[str, dict[str, dict[str, int]]] = {}
        for r in rows:
            d = dict(r)
            src = str(d["source"] or "unknown")
            gtype = goal_prefix(str(d["primary_goal_id"] or ""))
            oc = classify_plan_outcome(d)
            cost = estimate_cost_usd(str(d["model"] or ""), int(d["input_tokens"] or 0), int(d["output_tokens"] or 0))

            s_entry = by_source.setdefault(
                src,
                {
                    "sessions": 0,
                    "progressed_or_completed": 0,
                    "completed": 0,
                    "stalled": 0,
                    "levels": 0,
                    "skillups": 0,
                    "orders": 0,
                    "quests": 0,
                    "total_cost_usd": 0.0,
                },
            )
            s_entry["sessions"] += 1
            if oc in ("completed", "progressed"):
                s_entry["progressed_or_completed"] += 1
            if oc == "completed":
                s_entry["completed"] += 1
            if oc == "stalled":
                s_entry["stalled"] += 1
            s_entry["levels"] += int(d["outcome_levels"] or 0)
            s_entry["skillups"] += int(d["outcome_skillups"] or 0)
            s_entry["orders"] += int(d["outcome_orders"] or 0)
            s_entry["quests"] += int(d["outcome_quests"] or 0)
            s_entry["total_cost_usd"] += cost

            gt_src = by_goal_type.setdefault(gtype, {}).setdefault(src, {"sessions": 0, "positive": 0, "stalled": 0})
            gt_src["sessions"] += 1
            if oc in ("completed", "progressed"):
                gt_src["positive"] += 1
            elif oc == "stalled":
                gt_src["stalled"] += 1

        for src, entry in by_source.items():
            cnt = max(1, entry["sessions"])
            entry["progress_rate"] = round(entry["progressed_or_completed"] / cnt, 3)
            entry["avg_skillups"] = round(entry["skillups"] / cnt, 2)
            entry["total_cost_usd"] = round(entry["total_cost_usd"], 4)
            entry["cost_per_session_usd"] = round(entry["total_cost_usd"] / cnt, 5)

        report = {
            "window_days": int(days),
            "holdout_pct": int(os.getenv("HEARTHCORE_FALLBACK_HOLDOUT_PCT", str(DEFAULT_FALLBACK_HOLDOUT_PCT)) or 0),
            "by_source": by_source,
            "by_goal_type": by_goal_type,
        }
        report["recommendation"] = recommend_planner_tuning(report)
        return report


def recommend_planner_tuning(ab_report: dict[str, Any], *, min_samples: int = 10) -> dict[str, Any]:
    """Turn measured AI-vs-fallback A/B outcomes into concrete model/effort/holdout tuning advice (Issue #65)."""
    by_source = ab_report.get("by_source") or {}
    holdout_pct = int(ab_report.get("holdout_pct") or 0)
    ai = by_source.get("anthropic")
    base = by_source.get("fallback_holdout") or by_source.get("fallback")

    ai_n = int((ai or {}).get("sessions") or 0)
    base_n = int((base or {}).get("sessions") or 0)
    if ai_n < min_samples or base_n < min_samples:
        if holdout_pct <= 0 and base_n < min_samples:
            action = (
                "Holdout is currently OFF (0%). Opt in with HEARTHCORE_FALLBACK_HOLDOUT_PCT=10 "
                "for 7 days to collect a comparable fallback baseline before tuning."
            )
        else:
            action = (
                f"Collect at least {min_samples} sessions per arm "
                f"(currently ai={ai_n}, baseline={base_n}) before changing model or effort."
            )
        return {
            "status": "insufficient_data",
            "lift_pp": None,
            "ai_cost_per_session_usd": float((ai or {}).get("cost_per_session_usd") or 0.0),
            "action": action,
        }

    ai_rate = float(ai.get("progress_rate") or 0.0)
    base_rate = float(base.get("progress_rate") or 0.0)
    lift = round((ai_rate - base_rate) * 100.0, 1)
    cost_per_sess = float(ai.get("cost_per_session_usd") or 0.0)

    if lift >= 10.0:
        return {
            "status": "strong_ai_lift",
            "lift_pp": lift,
            "ai_cost_per_session_usd": cost_per_sess,
            "action": (
                f"Keep current AI model/effort: AI outperforms fallback by +{lift:.1f} pp "
                f"at ${cost_per_sess:.4f}/session."
            ),
        }
    if lift >= 2.0:
        return {
            "status": "marginal_lift_optimize_cost",
            "lift_pp": lift,
            "ai_cost_per_session_usd": cost_per_sess,
            "action": (
                f"Modest +{lift:.1f} pp AI lift at ${cost_per_sess:.4f}/session: "
                "test claude-haiku-5-5 or ANTHROPIC_EFFORT=low / longer session intervals to improve ROI."
            ),
        }
    return {
        "status": "no_measured_lift",
        "lift_pp": lift,
        "ai_cost_per_session_usd": cost_per_sess,
        "action": (
            f"No positive AI lift measured ({lift:+.1f} pp vs fallback at ${cost_per_sess:.4f}/session): "
            "audit prompt/candidate quality, lower ANTHROPIC_EFFORT=low, or widen deterministic fallback."
        ),
    }


def is_fallback_holdout(slug: str, day_key: str, *, holdout_pct: int | None = None) -> bool:
    """Deterministic daily-rotated fallback holdout selector for A/B measurement (Issue #65)."""
    pct = int(
        holdout_pct
        if holdout_pct is not None
        else (os.getenv("HEARTHCORE_FALLBACK_HOLDOUT_PCT", str(DEFAULT_FALLBACK_HOLDOUT_PCT)) or 0)
    )
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    digest = hashlib.sha256(f"{day_key}:{slug.lower()}".encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:2], "big") % 100
    return bucket < pct


def reconcile_state(
    *,
    db_path: Path = BRAIN_DB_PATH,
    sql_exec=None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Compare SQLite applied plans & journal cursor against live MySQL tables; report drift (Issue #64)."""
    ts = int(now_ts if now_ts is not None else time.time())
    runner = sql_exec if sql_exec is not None else run_sql
    store = BrainStore(db_path)
    mem = MemoryEngine(store.path)

    with store._conn() as db:
        active_rows = db.execute(
            """
            SELECT p.id, p.character_id, p.game_guid, p.expires_at, p.primary_goal_id
            FROM plans p
            INNER JOIN (
                SELECT character_id, MAX(id) AS max_id
                FROM plans WHERE applied = 1 AND plan_state = 'applied'
                GROUP BY character_id
            ) m ON m.max_id = p.id
            WHERE p.expires_at > ?
            """,
            (ts,),
        ).fetchall()

    prop_rows = runner("SELECT plan_id, status FROM companion_proposals;")
    mysql_props = {_int_or(r[0], 0): str(r[1] or "") for r in prop_rows if len(r) >= 2}

    ov_rows = runner(
        "SELECT guid, MAX(plan_id), MAX(revision), MIN(enabled), MAX(expires_at) "
        "FROM companion_weight_overrides GROUP BY guid;"
    )
    mysql_ov = {
        _int_or(r[0], 0): {
            "plan_id": _int_or(r[1], 0),
            "revision": _int_or(r[2], 0),
            "enabled": _int_or(r[3], 0),
            "expires_at": _int_or(r[4], 0),
        }
        for r in ov_rows
        if len(r) >= 5
    }

    ev_rows = runner("SELECT COALESCE(MAX(id), 0) FROM companion_events;")
    mysql_max_ev = _int_or(ev_rows[0][0], 0) if (ev_rows and ev_rows[0]) else 0
    jstat = mem.journal_status()
    journal_lag = max(0, mysql_max_ev - int(jstat["cursor"]))

    paused_slugs = store.paused_character_slugs()
    orphan_plans: list[dict[str, Any]] = []
    override_drift: list[dict[str, Any]] = []
    for r in active_rows:
        pid = int(r["id"])
        guid = int(r["game_guid"])
        slug = str(r["character_id"])
        if slug in paused_slugs:
            continue
        if pid not in mysql_props:
            orphan_plans.append({"plan_id": pid, "character_id": slug, "reason": "missing_mysql_proposal"})
        ov = mysql_ov.get(guid)
        if not ov:
            override_drift.append({"plan_id": pid, "character_id": slug, "reason": "missing_mysql_override"})
        elif ov["enabled"] == 0:
            override_drift.append({"plan_id": pid, "character_id": slug, "reason": "override_disabled"})
        elif ov["expires_at"] <= ts:
            override_drift.append({"plan_id": pid, "character_id": slug, "reason": "override_expired"})
        elif ov["revision"] < pid:
            override_drift.append({"plan_id": pid, "character_id": slug, "reason": f"override_behind:{ov['revision']}<{pid}"})

    has_drift = bool(orphan_plans or override_drift or journal_lag > 500)
    return {
        "ok": not has_drift,
        "checked_at": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "active_sqlite_plans": len(active_rows),
        "orphan_plans": orphan_plans,
        "override_drift": override_drift,
        "journal_cursor": int(jstat["cursor"]),
        "mysql_max_event_id": mysql_max_ev,
        "journal_lag": journal_lag,
        "journal_gap_detected": bool(jstat["gap_detected"]),
    }


def replay_snapshot(
    character_slug: str,
    snap_raw: dict[str, Any],
    *,
    guid: int = 1001,
    plan_id: int = 1,
    now_ts: int = 1_800_000_000,
) -> dict[str, Any]:
    """Offline no-network replay harness: recorded snapshot in -> validated plan & SQL statements out (Issue #64)."""
    char_prof = profile(character_slug)
    snap = snapshot(snap_raw, character_slug)
    plan = fallback(char_prof, snap)
    sql_captured: list[str] = []
    cpp_w, prop = apply_plan_to_db(
        guid,
        char_prof["name"],
        plan,
        snap,
        "fallback",
        plan_id=plan_id,
        now_ts=now_ts,
        sql_exec=lambda s: (sql_captured.append(s), [])[1],
        char_profile=char_prof,
    )
    return {
        "character_id": character_slug,
        "plan": plan,
        "cpp_weights": cpp_w,
        "proposal": prop,
        "sql": sql_captured[0] if sql_captured else "",
    }


def write_active_plans_json(
    store: BrainStore,
    now_ts: int,
    mem: MemoryEngine | None = None,
    budget: BudgetLedger | None = None,
    *,
    runtime_dir: Path = RUNTIME_DIR,
) -> dict[str, Any]:
    data = store.summary(now_ts)
    if budget is not None:
        data["budget"] = budget.summary(now_ts)
    if mem is not None:
        intentions_map = mem.all_open_intentions()
        alerts: list[dict[str, Any]] = []
        fatigue_map: dict[str, dict[str, Any]] = {}
        for slug, pinfo in data.get("plans", {}).items():
            rec = mem.analyze_recent_plans(slug, now_ts)
            pinfo["open_intentions"] = [it["label"] for it in intentions_map.get(slug, [])]
            pinfo["fatigue_category"] = rec["fatigue_category"]
            pinfo["fatigue_streak"] = rec["fatigue_streak"]
            pinfo["stalled_goal_id"] = rec["stalled_goal_id"]
            if rec["fatigue_category"]:
                fatigue_map[slug] = {
                    "category": rec["fatigue_category"],
                    "streak": rec["fatigue_streak"],
                }
            if rec["operational_alert"]:
                alerts.append(rec["operational_alert"])
        data["memory"] = {
            "open_intentions": intentions_map,
            "completed_intentions": mem.recent_completed_intentions(10),
            "operational_alerts": alerts,
            "active_reservations": {str(k): v for k, v in mem.active_order_reservations(now_ts).items()},
            "fatigue_count": len(fatigue_map),
            "journal": mem.journal_status(),
        }
    runtime_dir.mkdir(parents=True, exist_ok=True)
    out_path = runtime_dir / "active_plans.json"
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(out_path)
    return data


def run_cycle(
    *,
    provider: str = "anthropic",
    apply: bool = True,
    force: bool = False,
    limit: int = 0,
    character: str | None = None,
    db_path: Path = BRAIN_DB_PATH,
    runtime_dir: Path = RUNTIME_DIR,
    world_ctx_override: dict[str, Any] | None = None,
    http_post_fn=None,
    sql_exec=None,
    now_ts_override: int | None = None,
    remote_max_rev_override: int | None = None,
    lock_timeout_sec: float = 2.0,
) -> dict[str, Any]:
    """Run one session-boundary planning cycle for online Hearthguard companions."""
    load_env()
    now_ts = int(now_ts_override if now_ts_override is not None else time.time())
    cfg_model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    cfg_effort = os.getenv("ANTHROPIC_EFFORT", "medium")

    store = BrainStore(db_path)
    mem = MemoryEngine(store.path)
    budget = BudgetLedger(store.path)

    # Validate budget config up front so invalid env vars fail fast (Issue #55)
    budget.configured_caps()

    # Check operator kill switch (Issue #59)
    if apply and is_brain_disabled(runtime_dir):
        summary = write_active_plans_json(store, now_ts, mem=mem, budget=budget, runtime_dir=runtime_dir)
        summary["disabled"] = True
        return {
            "planned_count": 0,
            "planned": [],
            "failed": [],
            "disabled": True,
            "summary": summary,
        }

    with acquire_run_lock(runtime_dir, timeout_sec=lock_timeout_sec):
        # Sync monotonic plan_id / revision authority with MySQL before planning (Issue #53: FAIL CLOSED!)
        seq_info: dict[str, Any] = {"resynced": False}
        if apply:
            remote_max = remote_max_rev_override
            if remote_max is None:
                if world_ctx_override is not None and sql_exec is None:
                    remote_max = int(world_ctx_override.get("remote_max_revision") or 0)
                else:
                    # Do NOT swallow MySQL errors into remote_max=0; fail closed before making paid API calls!
                    remote_max = fetch_remote_max_revision(sql_exec=sql_exec)
            seq_info = store.sync_sequence_floor(int(remote_max or 0))
            if seq_info.get("resynced"):
                record_failure_alert(
                    "brain_sequence_resync",
                    f"brain.db behind MySQL ({seq_info['local_max']} < {seq_info['remote_max']}); resynced floor",
                    runtime_dir=runtime_dir,
                )

        # Shadow mode NEVER syncs/mutates live event_journal (Issue #15)
        world_ctx = (
            world_ctx_override
            if world_ctx_override is not None
            else fetch_world_context(mem=(mem if apply else None), now_ts=now_ts)
        )
        if apply:
            store.refresh_outcomes(
                world_ctx["companions"],
                now_ts,
                proposals_by_plan_id=world_ctx.get("proposals_by_plan_id"),
            )
        # Shadow runs read a snapshot copy of reservations and NEVER mutate live reservations (Issue #7)
        reservations = dict(mem.active_order_reservations(now_ts))
        known_slugs = set(profiles())
        paused_slugs = store.paused_character_slugs() if apply else set()

        targets = []
        for slug, comp in sorted(world_ctx["companions"].items()):
            if character and slug != character.lower():
                continue
            if not comp["online"] and not character:
                continue
            if slug not in known_slugs:
                continue
            if apply and slug in paused_slugs:
                logger.info("Skipping AI planning for %s (paused by operator)", slug)
                continue
            if not force and store.active_plan(slug, now_ts) is not None:
                continue
            # Per-companion paid-API backoff after a recent apply_failed (prevents repeated paid calls while DB broken)
            if (
                apply
                and provider == "anthropic"
                and not force
                and store.recent_apply_failed(slug, now_ts, DEFAULT_APPLY_FAILURE_BACKOFF_SEC)
            ):
                logger.warning("Skipping paid API retry for %s (recent apply_failed backoff active)", slug)
                continue
            targets.append(comp)

        eff_limit = int(limit)
        if eff_limit <= 0 and provider == "anthropic" and not character and world_ctx_override is None:
            eff_limit = int(os.getenv("HEARTHCORE_MAX_PLANS_PER_TICK", str(DEFAULT_MAX_PLANS_PER_TICK)) or 0)
        if eff_limit > 0:
            targets = targets[:eff_limit]

        planned: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        consecutive_apply_failures = 0
        max_apply_failures = int(
            os.getenv("HEARTHCORE_MAX_APPLY_FAILURES", str(DEFAULT_MAX_CONSECUTIVE_APPLY_FAILURES))
            or DEFAULT_MAX_CONSECUTIVE_APPLY_FAILURES
        )
        counters = {
            "planned": 0,
            "anthropic": 0,
            "fallback": 0,
            "fallback_holdout": 0,
            "api_error": 0,
            "sql_error": 0,
            "skipped": max(0, len(world_ctx.get("companions") or {}) - len(targets)),
        }
        day_key_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        coop_sync: dict[str, Any] = {"ok": True, "skipped": "shadow_mode"}
        try:
            if apply:
                from .native_lfg_bridge import sync_coop_native_lfg_bridge
                try:
                    coop_sync = sync_coop_native_lfg_bridge(
                        db_path=store.path,
                        sql_exec=sql_exec if sql_exec is not None else run_sql,
                        now_ts=now_ts,
                    )
                except Exception as exc:
                    logger.warning("Native coop LFG bridge sync skipped on error: %s", exc)
                    coop_sync = {"ok": False, "error": str(exc)}
            for comp in targets:
                if _STOP_EVENT.is_set():
                    logger.warning("Graceful stop requested (SIGTERM); ending cycle cleanly before %s", comp.get("name"))
                    break
                if consecutive_apply_failures >= max(1, max_apply_failures):
                    logger.error(
                        "Aborting remaining companions after %d consecutive apply failures (protecting API budget)",
                        consecutive_apply_failures,
                    )
                    break
                slug = str(comp.get("slug") or comp.get("name", "")).lower()
                pending_plan_id: int | None = None
                try:
                    char_prof = profile(slug)
                    mem_ctx = mem.build_packet(comp, world_ctx, now_ts, mutate_intentions=apply)
                    snap = build_snapshot(
                        comp,
                        world_ctx,
                        memory_ctx=mem_ctx,
                        order_reservations=reservations,
                        char_profile=char_prof,
                    )
                    desc_by_id = {c["id"]: c["description"] for c in snap["candidates"]}

                    source = provider
                    meta: dict[str, Any] = {}
                    err_note = ""
                    active_directive = store.get_operator_directive(slug, now_ts=now_ts)
                    directive_note = active_directive["note"] if active_directive else None

                    if provider == "anthropic" and is_fallback_holdout(slug, day_key_utc):
                        source = "fallback_holdout"
                        plan = fallback(char_prof, snap)
                        counters["fallback_holdout"] += 1
                    elif provider == "anthropic":
                        try:
                            plan, meta = anthropic_with_meta(
                                char_prof,
                                snap,
                                model=cfg_model,
                                effort=cfg_effort,
                                memory=mem_ctx["packet"],
                                operator_directive=directive_note,
                                budget=budget,
                                caller="shadow_planner" if not apply else "planner",
                                is_shadow=not apply,
                                http_post_fn=http_post_fn,
                                now_ts=now_ts,
                            )
                            counters["anthropic"] += 1
                        except (BudgetExceeded, ProviderBackoffError) as exc:
                            source = "fallback"
                            err_note = str(exc)
                            plan = fallback(char_prof, snap)
                            counters["fallback"] += 1
                        except InvalidWithUsage as exc:
                            source = "fallback_after_api"
                            err_note = str(exc)
                            meta = exc.meta
                            plan = fallback(char_prof, snap)
                            counters["api_error"] += 1
                        except Invalid as exc:
                            source = "fallback_after_api"
                            err_note = str(exc)
                            plan = fallback(char_prof, snap)
                            counters["api_error"] += 1
                    else:
                        plan = fallback(char_prof, snap)
                        counters["fallback"] += 1

                    xp_catchup, _ = _is_xp_capped_prof_catchup(comp, world_ctx)
                    cpp_w = weights_for_cpp(plan, snap, xp_capped_catchup=xp_catchup, char_profile=char_prof)
                    prop_spec = build_proposal_spec(plan, snap)
                    primary_desc = desc_by_id.get(plan["primary_goal_id"], plan["primary_goal_id"])

                    # Issue #52: record live plan as 'pending' (applied=0) BEFORE MySQL write;
                    # promote to applied=1 ONLY after apply_plan_to_db succeeds!
                    # Shadow mode (apply=False) writes ONLY to shadow_plans.json, NEVER mutating brain.db plans!
                    plan_id = 0
                    if apply:
                        plan_id = store.record_plan(
                            now_ts=now_ts,
                            slug=slug,
                            guid=comp["guid"],
                            source=source,
                            model=meta.get("model") or (cfg_model if source == "anthropic" else source),
                            effort=meta.get("effort") or (cfg_effort if source == "anthropic" else ""),
                            plan=plan,
                            primary_desc=primary_desc,
                            cpp_weights=cpp_w,
                            meta=meta,
                            applied=False,
                            plan_state="pending",
                            error_note=err_note,
                            observed_at=snap["observed_at"],
                            start_level=int(comp["level"]),
                            start_xp=int(comp.get("xp", 0)),
                            start_skill_sum=sum(v for v, _ in comp["skills"].values()),
                            start_orders_done=int(comp.get("orders_delivered", 0)),
                            start_quests_done=int(comp.get("quests_rewarded", 0)),
                            start_ledger_ops=int(comp.get("ledger_ops", 0)),
                            advisory_only=bool(prop_spec["advisory_only"]),
                            proposal_status=("advisory" if prop_spec["advisory_only"] else "proposed"),
                        )
                        pending_plan_id = plan_id
                        cpp_w, prop_spec = apply_plan_to_db(
                            comp["guid"],
                            comp["name"],
                            plan,
                            snap,
                            source,
                            plan_id=plan_id,
                            now_ts=now_ts,
                            sql_exec=sql_exec,
                            xp_capped_catchup=xp_catchup,
                            char_profile=char_prof,
                        )
                        # MySQL acknowledged! Finalize previous open plan and mark this one applied.
                        store.finalize_character_open_plans(slug, comp, now_ts, exclude_plan_id=plan_id)
                        store.mark_plan_applied(plan_id)
                        if active_directive and source == "anthropic":
                            store.mark_operator_directive_consumed(slug, now_ts=now_ts)
                        pending_plan_id = None
                        consecutive_apply_failures = 0
                        mem.record_plan_intention(slug, plan, primary_desc, now_ts)
                        picked_oid = parse_order_id(plan["primary_goal_id"])
                        if picked_oid is not None:
                            reservations[picked_oid] = slug

                    counters["planned"] += 1
                    planned.append({
                        "plan_id": plan_id,
                        "name": comp["name"],
                        "level": comp["level"],
                        "source": source,
                        "model": meta.get("model") or source,
                        "applied": apply,
                        "advisory_only": bool(prop_spec["advisory_only"]),
                        "primary_goal_id": plan["primary_goal_id"],
                        "reason_code": plan["reason_code"],
                        "session_minutes": plan["session_minutes"],
                        "tokens": f"{meta.get('input_tokens', 0)}/{meta.get('output_tokens', 0)}",
                        "latency_ms": meta.get("latency_ms", 0),
                        "error": err_note,
                    })
                except Exception as exc:
                    counters["sql_error"] += 1
                    consecutive_apply_failures += 1
                    err_msg = f"{type(exc).__name__}: {exc}"
                    logger.warning("Companion planning/apply failed for %s: %s", slug, err_msg)
                    if pending_plan_id is not None:
                        try:
                            store.mark_plan_failed(pending_plan_id, err_msg)
                        except Exception as mark_exc:
                            logger.warning("Failed marking pending plan %s failed: %s", pending_plan_id, mark_exc)
                    failed.append({"character_id": slug, "name": comp.get("name", slug), "error": err_msg[:200]})
                    record_failure_alert("brain_companion_error", f"{slug}: {err_msg}", runtime_dir=runtime_dir)
                    continue
        finally:
            logger.info("cycle_summary %s", json.dumps({"apply": apply, **counters}, sort_keys=True))
            if apply:
                if not failed or planned:
                    store.set_meta("last_success_at", str(now_ts))
                summary = write_active_plans_json(store, now_ts, mem=mem, budget=budget, runtime_dir=runtime_dir)
            else:
                # Strict shadow isolation (Issue #7): never overwrite active_plans.json!
                summary = store.summary(now_ts)
                summary["budget"] = budget.summary(now_ts)
                runtime_dir.mkdir(parents=True, exist_ok=True)
                sh_path = runtime_dir / "shadow_plans.json"
                sh_out = {
                    "generated_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "is_shadow": True,
                    "planned_count": len(planned),
                    "failed_count": len(failed),
                    "planned": planned,
                    "failed": failed,
                    "budget": summary["budget"],
                }
                tmp = sh_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(sh_out, indent=2) + "\n", encoding="utf-8")
                tmp.replace(sh_path)

        return {
            "planned_count": len(planned),
            "planned": planned,
            "failed": failed,
            "counters": counters,
            "sequence_sync": seq_info,
            "coop_sync": coop_sync,
            "summary": summary,
        }
