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
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .budget import BudgetExceeded, BudgetLedger, InvalidWithUsage, ProviderBackoffError
from .core import Invalid, ROOT, fallback, profile, profiles, snapshot, validate
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
            except OSError:
                pass
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


def record_failure_alert(source: str, detail: str, *, runtime_dir: Path = RUNTIME_DIR) -> None:
    """Append a structured failure alert entry to runtime/failure_alerts.log (Issue #59)."""
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": str(source)[:48],
            "detail": str(detail)[:240],
        }
        with open(runtime_dir / "failure_alerts.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


def run_sql(sql: str, *, timeout: float | None = None) -> list[list[str]]:
    use_timeout = float(timeout if timeout is not None else os.getenv("HEARTHCORE_SQL_TIMEOUT_SEC", str(DEFAULT_SQL_TIMEOUT_SEC)))
    res = subprocess.run(
        [
            "docker", "exec", "-i", DB_CONTAINER, "sh", "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -u root -N -B acore_characters',
        ],
        input=sql,
        text=True,
        capture_output=True,
        check=True,
        timeout=max(1.0, use_timeout),
    )
    rows: list[list[str]] = []
    for line in res.stdout.splitlines():
        if line != "":
            rows.append(line.split("\t"))
    return rows


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


def read_live_context() -> dict[str, Any]:
    """Read fresh weekly_event and live zone overrides from live_positions.json if present."""
    ctx: dict[str, Any] = {"weekly_event": "", "live_by_guid": {}}
    try:
        st = LIVE_POS_FILE.stat()
        if time.time() - st.st_mtime < 30.0:
            raw = json.loads(LIVE_POS_FILE.read_text(encoding="utf-8"))
            ctx["weekly_event"] = str(raw.get("weekly_event") or "")
            for u in raw.get("units", []):
                ctx["live_by_guid"][int(u["g"])] = u
    except Exception:
        pass
    return ctx


def sync_event_journal(mem: MemoryEngine, guid_to_slug: dict[int, str], now_ts: int) -> dict[str, Any]:
    """Incrementally ingest companion_events into durable event_journal using checkpoint cursor (Issue #4)."""
    cursor = mem.get_event_cursor()
    min_rows = run_sql("SELECT COALESCE(MIN(id), 0) FROM companion_events;")
    min_avail = int(min_rows[0][0]) if (min_rows and min_rows[0]) else 0

    total_inserted = 0
    last_info: dict[str, Any] = {"inserted": 0, "cursor": cursor, "gap_detected": False, "gap_note": ""}
    # Paginate in bounded chunks so bursts never lose or crowd out quieter companions
    for _ in range(10):
        rows = run_sql(
            f"SELECT id, guid, event_type, summary, partner_guid, zone_id, created_at "
            f"FROM companion_events WHERE id > {int(cursor)} ORDER BY id ASC LIMIT 500;"
        )
        if not rows:
            if min_avail > 0 and cursor == 0:
                last_info = mem.ingest_events([], now_ts=now_ts, min_available_source_id=min_avail)
            break
        batch = []
        for r in rows:
            if len(r) < 7:
                continue
            g = int(r[1])
            slug = guid_to_slug.get(g)
            if not slug:
                continue
            batch.append({
                "source_id": int(r[0]),
                "guid": g,
                "character_id": slug,
                "event_type": r[2],
                "summary": r[3],
                "partner_guid": int(r[4]),
                "zone_id": int(r[5]),
                "created_at": int(r[6]),
            })
        last_info = mem.ingest_events(
            batch,
            now_ts=now_ts,
            min_available_source_id=min_avail if cursor == 0 else None,
        )
        total_inserted += last_info["inserted"]
        new_cursor = int(rows[-1][0])
        if new_cursor <= cursor or len(rows) < 500:
            cursor = new_cursor
            break
        cursor = new_cursor

    last_info["inserted"] = total_inserted
    return last_info


def fetch_world_context(mem: MemoryEngine | None = None, now_ts: int | None = None) -> dict[str, Any]:
    """Query live MySQL for companions, skills, quests, ledger ops, work orders, proposals, and events."""
    live = read_live_context()
    ts = int(now_ts if now_ts is not None else time.time())

    comp_rows = run_sql("""
        SELECT c.guid, c.name, c.level, c.zone, c.map, c.money, c.online,
               COALESCE(cs.shift_group, ''), c.xp
        FROM companion_schedule cs
        JOIN characters c ON c.name = cs.name
        ORDER BY c.online DESC, c.name;
    """)
    companions: dict[str, dict[str, Any]] = {}
    guid_to_slug: dict[int, str] = {}
    for r in comp_rows:
        if len(r) < 9:
            continue
        guid = int(r[0])
        name = r[1]
        slug = name.lower()
        lp = live["live_by_guid"].get(guid)
        zid = int(lp["z"]) if lp else int(r[3])
        mid = int(lp["m"]) if lp else int(r[4])
        companions[slug] = {
            "guid": guid,
            "name": name,
            "slug": slug,
            "level": int(r[2]),
            "xp": int(r[8]),
            "zone_id": zid,
            "zone": ZONE_NAMES.get(zid, f"Zone {zid}"),
            "map_id": mid,
            "gold": round(int(r[5]) / 10000.0, 2),
            "online": (int(r[6]) == 1) or (guid in live["live_by_guid"]),
            "shift_group": r[7],
            "skills": {},
            "orders_delivered": 0,
            "quests_rewarded": 0,
            "ledger_ops": 0,
            "recent_events": [],
            "social_bonds": [],
        }
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

    # Sync durable event journal with fair cursor-based pagination (Issue #4)
    if mem is not None and guid_to_slug:
        try:
            sync_event_journal(mem, guid_to_slug, ts)
        except Exception:
            pass

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
    except Exception:
        pass

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

    # Authoritative owner levels (Wonky / Logan) for CompanionLevelLead XP-cap awareness
    owner_levels: dict[str, int] = {}
    for r in run_sql("SELECT name, level FROM characters WHERE name IN ('Wonky', 'Logan');"):
        if len(r) >= 2:
            owner_levels[r[0].lower()] = int(r[1])

    # Live Quartermaster directives (strictly reads live quartermaster.json, never shadow)
    qm_focus: set[str] = set()
    qm_mats_by_skill: dict[str, list[str]] = {}
    try:
        if QUARTERMASTER_PATH.is_file():
            qm = json.loads(QUARTERMASTER_PATH.read_text(encoding="utf-8"))
            if not qm.get("is_shadow"):
                qm_focus = {str(s).lower() for s in (qm.get("focus_skills") or [])}
                for br in qm.get("bank_requests") or []:
                    gsk = str(br.get("gather_skill") or "").lower()
                    if gsk:
                        qm_mats_by_skill.setdefault(gsk, []).append(str(br.get("name") or ""))
    except Exception:
        pass

    return {
        "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "weekly_event": live["weekly_event"],
        "companions": companions,
        "work_orders": work_orders,
        "humans": humans,
        "owner_levels": owner_levels,
        "qm_focus": qm_focus,
        "qm_mats_by_skill": qm_mats_by_skill,
        "proposals_by_plan_id": proposals_by_plan_id,
    }


def _xp_cap_prof_catchup_state(comp: dict[str, Any], world_ctx: dict[str, Any]) -> dict[str, Any]:
    """Compute authoritative per-character XP-cap & primary-profession catch-up state (Issue #23).

    Rules:
    - Scoped ONLY to companions with an authoritative owner-cap binding (`core-wonky`, `core-logan`,
      or explicit `owner_name` / `xp_cap_level`). Ordinary guild bots are never falsely treated as
      Wonky-capped!
    - Respects explicit `xp_eligible` and configured `HEARTHCORE_COMPANION_LEVEL_LEAD` (default 3).
    - Distinguishes `at_cap` (0 XP) from `near_cap` (1 level below cap, still earning XP).
    - Respects trained skill ceilings (`val >= mx` and `mx < 300` -> needs profession trainer!).
    """
    lvl = int(comp.get("level") or 0)
    sg = str(comp.get("shift_group") or "").strip().lower()
    owner_levels = world_ctx.get("owner_levels") or {}
    level_lead = int(world_ctx.get("companion_level_lead") or os.getenv("HEARTHCORE_COMPANION_LEVEL_LEAD", "3") or 3)

    cap_lvl = 0
    if comp.get("xp_cap_level") is not None:
        cap_lvl = int(comp["xp_cap_level"])
    else:
        owner_name = str(comp.get("owner_name") or "").strip().lower()
        if not owner_name:
            if sg == "core-logan":
                owner_name = "logan"
            elif sg == "core-wonky":
                owner_name = "wonky"
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


def weights_for_cpp(
    plan: dict[str, Any],
    snap: dict[str, Any],
    *,
    xp_capped_catchup: bool = False,
) -> dict[str, float]:
    """Translate validated Hearthcore plan into bounded 0.5-1.5 keys read by CompanionBrain.cpp."""
    mods = plan["weight_modifiers"]
    cand_type = {c["id"]: c["type"] for c in snap["candidates"]}
    primary_type = cand_type.get(plan["primary_goal_id"], "")

    def eff(t: str, default: float = 1.0) -> float:
        base = float(mods.get(t, default))
        if primary_type == t:
            base += 0.15
        return _clamp(base)

    q = eff("quest")
    if primary_type in ("explore", "dungeon"):
        q = _clamp(max(q, eff(primary_type) * 0.95))

    craft_val = max(
        eff("craft"),
        eff("profession") * 0.98 if "profession" in mods or primary_type == "profession" else 0.5,
        eff("guild_order") if "guild_order" in mods or primary_type == "guild_order" else 0.5,
    )
    craft_val = _clamp(craft_val)
    gather_val = eff("gather")
    fish_val = eff("fish")
    train_val = eff("train")
    travel_val = eff("travel")
    hearth_val = eff("rest")

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

    return {
        "quest_work": q,
        "quest_turnin": _clamp(max(q, 0.95 if xp_capped_catchup else 1.0)),
        "quest_hub": _clamp(max(q, 0.75 if xp_capped_catchup else q)),
        "quest_grind": _clamp(min(q, 0.50 if xp_capped_catchup else 1.15)),
        "craft": craft_val,
        "gather": gather_val,
        "fish": fish_val,
        "train": train_val,
        "bank": eff("bank"),
        "repair": eff("repair"),
        "sell": _clamp((eff("repair") + eff("bank")) / 2.0),
        "travel": travel_val,
        "hearth": hearth_val,
    }


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
) -> tuple[dict[str, float], dict[str, Any]]:
    """Re-validate plan and atomically write expiring overrides + typed proposal to MySQL.

    Issues #3 & #6:
    - NEVER overwrites canonical persona/manual rows in `companion_weights`.
    - Writes expiring overrides to `companion_weight_overrides` with monotonic revision guard
      so older/replayed plans can never supersede newer plans.
    - Writes a typed, versioned proposal row to `companion_proposals` for native C++ validation.
    """
    validate(plan, snap)
    ts = int(now_ts if now_ts is not None else time.time())
    expires_at = ts + int(plan["session_minutes"]) * 60
    revision = int(plan_id) if int(plan_id) > 0 else ts
    cpp_weights = weights_for_cpp(plan, snap, xp_capped_catchup=xp_capped_catchup)
    prop = build_proposal_spec(plan, snap)
    db_src = "hearthcore_ai" if source == "anthropic" else "hearthcore_fb"

    stmts: list[str] = ["START TRANSACTION;"]
    for k, v in sorted(cpp_weights.items()):
        stmts.append(
            "INSERT INTO companion_weight_overrides "
            "(`guid`, `key`, `value`, `plan_id`, `revision`, `source`, `enabled`, `created_at`, `expires_at`) "
            f"VALUES ({int(guid)}, '{_sql_esc(k)}', {v:.3f}, {int(plan_id)}, {revision}, '{db_src}', 1, {ts}, {expires_at}) "
            "ON DUPLICATE KEY UPDATE "
            "`value` = IF(VALUES(`revision`) >= `revision`, VALUES(`value`), `value`), "
            "`plan_id` = IF(VALUES(`revision`) >= `revision`, VALUES(`plan_id`), `plan_id`), "
            "`source` = IF(VALUES(`revision`) >= `revision`, VALUES(`source`), `source`), "
            "`created_at` = IF(VALUES(`revision`) >= `revision`, VALUES(`created_at`), `created_at`), "
            "`expires_at` = IF(VALUES(`revision`) >= `revision`, VALUES(`expires_at`), `expires_at`), "
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
            "plans": active,
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
        # Sync monotonic plan_id / revision authority with MySQL before planning (Issue #53)
        seq_info: dict[str, Any] = {"resynced": False}
        if apply:
            remote_max = remote_max_rev_override
            if remote_max is None:
                if world_ctx_override is not None and sql_exec is None:
                    remote_max = int(world_ctx_override.get("remote_max_revision") or 0)
                else:
                    try:
                        remote_max = fetch_remote_max_revision(sql_exec=sql_exec)
                    except Exception:
                        remote_max = 0
            seq_info = store.sync_sequence_floor(int(remote_max or 0))
            if seq_info.get("resynced"):
                record_failure_alert(
                    "brain_sequence_resync",
                    f"brain.db behind MySQL ({seq_info['local_max']} < {seq_info['remote_max']}); resynced floor",
                    runtime_dir=runtime_dir,
                )

        world_ctx = (
            world_ctx_override
            if world_ctx_override is not None
            else fetch_world_context(mem=mem, now_ts=now_ts)
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

        targets = []
        for slug, comp in sorted(world_ctx["companions"].items()):
            if character and slug != character.lower():
                continue
            if not comp["online"] and not character:
                continue
            if slug not in known_slugs:
                continue
            if not force and store.active_plan(slug, now_ts) is not None:
                continue
            targets.append(comp)

        if limit > 0:
            targets = targets[:limit]

        planned: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        try:
            for comp in targets:
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

                    if provider == "anthropic":
                        try:
                            plan, meta = anthropic_with_meta(
                                char_prof,
                                snap,
                                model=cfg_model,
                                effort=cfg_effort,
                                memory=mem_ctx["packet"],
                                budget=budget,
                                caller="shadow_planner" if not apply else "planner",
                                is_shadow=not apply,
                                http_post_fn=http_post_fn,
                                now_ts=now_ts,
                            )
                        except (BudgetExceeded, ProviderBackoffError) as exc:
                            source = "fallback"
                            err_note = str(exc)
                            plan = fallback(char_prof, snap)
                        except InvalidWithUsage as exc:
                            source = "fallback_after_api"
                            err_note = str(exc)
                            meta = exc.meta
                            plan = fallback(char_prof, snap)
                        except Invalid as exc:
                            source = "fallback_after_api"
                            err_note = str(exc)
                            plan = fallback(char_prof, snap)
                    else:
                        plan = fallback(char_prof, snap)

                    xp_catchup, _ = _is_xp_capped_prof_catchup(comp, world_ctx)
                    cpp_w = weights_for_cpp(plan, snap, xp_capped_catchup=xp_catchup)
                    prop_spec = build_proposal_spec(plan, snap)
                    primary_desc = desc_by_id.get(plan["primary_goal_id"], plan["primary_goal_id"])

                    # Issue #52: record live plan as 'pending' (applied=0) BEFORE MySQL write;
                    # promote to applied=1 ONLY after apply_plan_to_db succeeds!
                    plan_id = store.record_plan(
                        now_ts=now_ts,
                        slug=slug,
                        guid=comp["guid"],
                        source=source,
                        model=meta.get("model") or (cfg_model if source == "anthropic" else "fallback"),
                        effort=meta.get("effort") or (cfg_effort if source == "anthropic" else ""),
                        plan=plan,
                        primary_desc=primary_desc,
                        cpp_weights=cpp_w,
                        meta=meta,
                        applied=False,
                        plan_state="pending" if apply else "shadow",
                        error_note=err_note,
                        observed_at=snap["observed_at"],
                        start_level=int(comp["level"]),
                        start_xp=int(comp.get("xp", 0)),
                        start_skill_sum=sum(v for v, _ in comp["skills"].values()),
                        start_orders_done=int(comp.get("orders_delivered", 0)),
                        start_quests_done=int(comp.get("quests_rewarded", 0)),
                        start_ledger_ops=int(comp.get("ledger_ops", 0)),
                        advisory_only=bool(prop_spec["advisory_only"]),
                        proposal_status=("advisory" if prop_spec["advisory_only"] else "proposed") if apply else "shadow",
                    )
                    if apply:
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
                        )
                        # MySQL acknowledged! Finalize previous open plan and mark this one applied.
                        store.finalize_character_open_plans(slug, comp, now_ts, exclude_plan_id=plan_id)
                        store.mark_plan_applied(plan_id)
                        pending_plan_id = None
                        mem.record_plan_intention(slug, plan, primary_desc, now_ts)
                        picked_oid = parse_order_id(plan["primary_goal_id"])
                        if picked_oid is not None:
                            reservations[picked_oid] = slug

                    planned.append({
                        "plan_id": plan_id,
                        "name": comp["name"],
                        "level": comp["level"],
                        "source": source,
                        "model": meta.get("model") or "fallback",
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
                    err_msg = f"{type(exc).__name__}: {exc}"
                    if pending_plan_id is not None:
                        try:
                            store.mark_plan_failed(pending_plan_id, err_msg)
                        except Exception:
                            pass
                    failed.append({"character_id": slug, "name": comp.get("name", slug), "error": err_msg[:200]})
                    record_failure_alert("brain_companion_error", f"{slug}: {err_msg}", runtime_dir=runtime_dir)
                    continue
        finally:
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
            "sequence_sync": seq_info,
            "summary": summary,
        }
