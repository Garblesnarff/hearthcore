"""Persistent Memory v1.5 + Durable Event Journal + Goal-Specific Outcome Classification.

Addresses Issues #3, #4, #5, #7:
- Durable event journal (`event_journal` + `event_cursors`) keyed by authoritative
  `companion_events.id` (`INSERT OR IGNORE`): preserves milestones beyond any short UI window,
  prevents duplicate memories on replay/bursts, detects ID gaps honestly without fabrication,
  and guarantees fair per-character retrieval.
- Strict shadow isolation: all live memory/reservation/fatigue/outcome queries filter
  `applied = 1 AND COALESCE(plan_state, 'applied') != 'shadow'`.
- Goal-specific observed outcome classification (`classify_plan_outcome`):
  - Recognizes XP without level-up, rewarded quests, gathering/crafting ledger activity at skill cap,
    and native `companion_proposals` lifecycle outcomes (`completed`, `interrupted`, `rejected`, `expired`).
  - Suppresses behavioral caution for advisory/unsupported goals (`dungeon`, `explore`, `bank`,
    `repair`, `train`) and for unaccepted/owner-interrupted plans.
  - Separates operational stuck alerts from scoped behavioral caution (strictly per exact `goal_id`).
"""
from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Any

from .guides import SKILL_NAMES

TIER_BREAKPOINTS = (75, 150, 225, 300)
MILESTONE_EVENT_TYPES = frozenset({
    "level_up",
    "skill_milestone",
    "boss_kill",
    "work_order",
    "dungeon",
    "reputation",
})
ADVISORY_GOAL_PREFIXES = frozenset({"dungeon", "explore", "bank", "repair", "train", "profession"})


def goal_category(goal_id: str) -> str:
    """Map a candidate goal_id to a broad session category for variety tracking."""
    prefix = (goal_id or "").split(":", 1)[0].lower()
    if prefix in ("craft", "guild_order", "profession"):
        return "craft"
    if prefix == "gather":
        return "gather"
    if prefix == "fish":
        return "fish"
    if prefix in ("quest", "explore", "dungeon"):
        return "adventure"
    return "errand"


def goal_prefix(goal_id: str) -> str:
    return (goal_id or "").split(":", 1)[0].lower()


def is_advisory_goal(goal_id: str) -> bool:
    """Return True for goal types without a dedicated native C++ objective executor."""
    return goal_prefix(goal_id) in ADVISORY_GOAL_PREFIXES


def parse_order_id(goal_id: str) -> int | None:
    """Extract numeric work-order ID from 'guild_order:<id>:<slug>', or None."""
    parts = (goal_id or "").split(":")
    if len(parts) >= 2 and parts[0] == "guild_order" and parts[1].isdigit():
        return int(parts[1])
    return None


def next_skill_tier(val: int, mx: int) -> int | None:
    """Return the next Classic skill breakpoint (75/150/225/300) above val, if reachable."""
    cap = max(mx, 75)
    for bp in TIER_BREAKPOINTS:
        if val < bp <= max(cap, bp):
            return bp
    return None


def classify_plan_outcome(row: dict[str, Any]) -> str:
    """Classify a finished/aged applied plan using goal-specific observed outcomes (Issue #5).

    Returns one of:
      'completed'     - native proposal or work order verified completed
      'progressed'    - verified goal-relevant progress (XP, quests, skill-ups, ledger ops, orders)
      'interrupted'   - owner/group priority or external interruption (NOT a failed attempt)
      'not_attempted' - expired before acceptance, rejected as unavailable, or unapplied/shadow
      'unknown'       - advisory-only goal or incomplete telemetry (suppresses behavioral penalty)
      'stalled'       - attempted non-advisory goal with 0 verified goal-specific progress
    """
    if not int(row.get("applied") or 0) or str(row.get("plan_state") or "applied") == "shadow":
        return "not_attempted"

    gid = str(row.get("primary_goal_id") or "")
    prefix = goal_prefix(gid)
    prop_status = str(row.get("proposal_status") or "").lower()
    advisory = bool(int(row.get("advisory_only") or 0)) or is_advisory_goal(gid)

    if prop_status == "completed":
        return "completed"
    if prop_status == "interrupted":
        return "interrupted"
    if prop_status in ("rejected", "expired"):
        return "not_attempted"

    # Incomplete baseline telemetry -> degrade honestly to unknown
    if int(row.get("start_level") or 0) <= 0:
        return "unknown"

    lvls = int(row.get("outcome_levels") or 0)
    xp = int(row.get("outcome_xp") or 0)
    sks = int(row.get("outcome_skillups") or 0)
    ords = int(row.get("outcome_orders") or 0)
    quests = int(row.get("outcome_quests") or 0)
    ledger_ops = int(row.get("outcome_ledger_ops") or 0)

    if prefix == "quest":
        if lvls > 0 or xp > 0 or quests > 0:
            return "progressed"
        return "stalled"

    if prefix in ("gather", "fish"):
        # Gathering/fishing at skill cap still yields bag/vault ledger deposits!
        if sks > 0 or ledger_ops > 0:
            return "progressed"
        return "stalled"

    if prefix == "craft":
        if sks > 0 or ords > 0 or ledger_ops > 0:
            return "progressed"
        return "stalled"

    if prefix == "guild_order":
        if ords > 0:
            return "completed"
        if sks > 0 or ledger_ops > 0:
            return "progressed"
        # If native executor never accepted the order (still 'proposed'), do not label as behavioral stall
        if prop_status == "proposed":
            return "not_attempted"
        return "stalled"

    # Unsupported / advisory goals: never apply behavioral stall penalty
    if advisory:
        return "progressed" if (lvls > 0 or xp > 0 or sks > 0 or ords > 0 or quests > 0 or ledger_ops > 0) else "unknown"

    if lvls > 0 or xp > 0 or sks > 0 or ords > 0 or quests > 0 or ledger_ops > 0:
        return "progressed"
    return "unknown"


class MemoryEngine:
    """Persistent companion memory, durable event journal, and soft multi-bot coordination."""

    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("""
                CREATE TABLE IF NOT EXISTS intentions (
                    id INTEGER PRIMARY KEY,
                    character_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '',
                    UNIQUE(character_id, target_key)
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_intentions_char_status "
                "ON intentions(character_id, status, updated_at DESC)"
            )
            # Durable autobiographical event journal (Issue #4)
            db.execute("""
                CREATE TABLE IF NOT EXISTS event_journal (
                    source_id INTEGER PRIMARY KEY,
                    guid INTEGER NOT NULL,
                    character_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    partner_guid INTEGER NOT NULL DEFAULT 0,
                    zone_id INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    is_milestone INTEGER NOT NULL DEFAULT 0,
                    ingested_at INTEGER NOT NULL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ej_char_ms "
                "ON event_journal(character_id, is_milestone DESC, created_at DESC, source_id DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ej_char_time "
                "ON event_journal(character_id, created_at DESC, source_id DESC)"
            )
            db.execute("""
                CREATE TABLE IF NOT EXISTS event_cursors (
                    scope TEXT PRIMARY KEY,
                    last_source_id INTEGER NOT NULL DEFAULT 0,
                    first_source_id INTEGER NOT NULL DEFAULT 0,
                    latest_source_id INTEGER NOT NULL DEFAULT 0,
                    gap_detected INTEGER NOT NULL DEFAULT 0,
                    gap_note TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                )
            """)
            ec_cols = {r[1] for r in db.execute("PRAGMA table_info(event_cursors)").fetchall()}
            if "latest_source_id" not in ec_cols:
                db.execute("ALTER TABLE event_cursors ADD COLUMN latest_source_id INTEGER NOT NULL DEFAULT 0")

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Durable Event Journal & Cursor Checkpointing (Issues #4 & #57)
    # ------------------------------------------------------------------
    def get_event_cursor(self, scope: str = "companion_events") -> int:
        with self._conn() as db:
            row = db.execute(
                "SELECT last_source_id FROM event_cursors WHERE scope = ?",
                (scope,),
            ).fetchone()
            return int(row["last_source_id"]) if row else 0

    def ingest_events(
        self,
        events: list[dict[str, Any]],
        *,
        now_ts: int | None = None,
        scope: str = "companion_events",
        min_available_source_id: int | None = None,
        max_available_source_id: int | None = None,
        scanned_max_source_id: int | None = None,
    ) -> dict[str, Any]:
        """Idempotently ingest authoritative companion_events rows and advance checkpoint cursor.

        Issues #4 & #57:
        - Advances persisted cursor to scanned_max_source_id even when a page holds only
          non-companion rows (prevents infinite rescan stalls).
        - Checks for pruning gaps AND source counter resets/truncations on EVERY run.
        - Tracks latest_source_id so journal_lag = max(0, latest_source_id - cursor) is always visible.
        """
        ts = int(now_ts if now_ts is not None else time.time())
        inserted = 0
        gap_detected = False
        gap_note = ""
        reset_detected = False

        with self._conn() as db:
            cur_row = db.execute(
                "SELECT last_source_id, first_source_id, latest_source_id, gap_detected, gap_note "
                "FROM event_cursors WHERE scope = ?",
                (scope,),
            ).fetchone()
            prev_last = int(cur_row["last_source_id"]) if cur_row else 0
            first_seen = int(cur_row["first_source_id"]) if cur_row else 0
            prev_latest = int(cur_row["latest_source_id"]) if cur_row else 0
            prev_gap = bool(cur_row["gap_detected"]) if cur_row else False
            prev_gap_note = str(cur_row["gap_note"]) if cur_row else ""

            # Detect source table truncation / AUTO_INCREMENT counter reset on EVERY run (Issue #57)
            if max_available_source_id is not None and prev_last > 0 and max_available_source_id < prev_last:
                reset_detected = True
                gap_detected = True
                gap_note = (
                    f"Source counter reset/truncated (max #{max_available_source_id} < cursor #{prev_last}); rewound cursor"
                )
                prev_last = 0

            # Detect pruning gaps on EVERY run (not only on first sync!)
            if min_available_source_id is not None and min_available_source_id > 1 and not reset_detected:
                if prev_last == 0:
                    gap_detected = True
                    gap_note = f"History prior to event #{min_available_source_id} was already pruned before journaling"
                elif min_available_source_id > prev_last + 1:
                    gap_detected = True
                    gap_note = f"Source gap between #{prev_last} and #{min_available_source_id}"

            max_id = prev_last
            for ev in sorted(events, key=lambda x: int(x["source_id"])):
                sid = int(ev["source_id"])
                etype = str(ev.get("event_type") or "")
                is_ms = 1 if etype in MILESTONE_EVENT_TYPES else 0
                res = db.execute(
                    """
                    INSERT OR IGNORE INTO event_journal (
                        source_id, guid, character_id, event_type, summary,
                        partner_guid, zone_id, created_at, is_milestone, ingested_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        sid,
                        int(ev.get("guid") or 0),
                        str(ev.get("character_id") or "").lower(),
                        etype[:24],
                        str(ev.get("summary") or "")[:200],
                        int(ev.get("partner_guid") or 0),
                        int(ev.get("zone_id") or 0),
                        int(ev.get("created_at") or ts),
                        is_ms,
                        ts,
                    ),
                )
                if res.rowcount > 0:
                    inserted += 1
                if first_seen == 0 or sid < first_seen:
                    first_seen = sid
                if sid > max_id:
                    max_id = sid

            # Advance cursor past scanned non-companion pages too (Issue #57)
            if scanned_max_source_id is not None and int(scanned_max_source_id) > max_id:
                max_id = int(scanned_max_source_id)

            latest_src = max_id
            if max_available_source_id is not None:
                latest_src = max(max_id, int(max_available_source_id))
            elif prev_latest > latest_src and not reset_detected:
                latest_src = prev_latest

            final_gap = 1 if (prev_gap or gap_detected) else 0
            final_note = gap_note or prev_gap_note
            if reset_detected:
                db.execute(
                    """
                    INSERT INTO event_cursors (scope, last_source_id, first_source_id, latest_source_id, gap_detected, gap_note, updated_at)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(scope) DO UPDATE SET
                        last_source_id = excluded.last_source_id,
                        first_source_id = CASE WHEN first_source_id = 0 THEN excluded.first_source_id ELSE first_source_id END,
                        latest_source_id = excluded.latest_source_id,
                        gap_detected = 1,
                        gap_note = excluded.gap_note,
                        updated_at = excluded.updated_at
                    """,
                    (scope, max_id, first_seen, latest_src, final_gap, final_note[:180], ts),
                )
            else:
                db.execute(
                    """
                    INSERT INTO event_cursors (scope, last_source_id, first_source_id, latest_source_id, gap_detected, gap_note, updated_at)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(scope) DO UPDATE SET
                        last_source_id = MAX(last_source_id, excluded.last_source_id),
                        first_source_id = CASE WHEN first_source_id = 0 THEN excluded.first_source_id ELSE MIN(first_source_id, excluded.first_source_id) END,
                        latest_source_id = MAX(latest_source_id, excluded.latest_source_id),
                        gap_detected = MAX(gap_detected, excluded.gap_detected),
                        gap_note = CASE WHEN excluded.gap_note != '' THEN excluded.gap_note ELSE gap_note END,
                        updated_at = excluded.updated_at
                    """,
                    (scope, max_id, first_seen, latest_src, final_gap, final_note[:180], ts),
                )

        return {
            "inserted": inserted,
            "cursor": max_id,
            "latest_source_id": latest_src,
            "journal_lag": max(0, latest_src - max_id),
            "reset_detected": reset_detected,
            "gap_detected": bool(prev_gap or gap_detected),
            "gap_note": gap_note or prev_gap_note,
        }

    def get_character_milestones(self, slug: str, limit: int = 5) -> list[str]:
        """Fair per-character retrieval of durable milestones (survives >12 later events & restarts)."""
        with self._conn() as db:
            ms_rows = db.execute(
                """
                SELECT source_id, event_type, summary
                FROM event_journal
                WHERE character_id = ? AND is_milestone = 1
                ORDER BY created_at DESC, source_id DESC
                LIMIT ?
                """,
                (slug.lower(), limit),
            ).fetchall()
            out = [f"{r['event_type']}: {r['summary'][:72]} [ev#{r['source_id']}]" for r in ms_rows]
            if len(out) < limit:
                need = limit - len(out)
                other_rows = db.execute(
                    """
                    SELECT source_id, event_type, summary
                    FROM event_journal
                    WHERE character_id = ? AND is_milestone = 0
                    ORDER BY created_at DESC, source_id DESC
                    LIMIT ?
                    """,
                    (slug.lower(), need),
                ).fetchall()
                out.extend(f"{r['event_type']}: {r['summary'][:72]} [ev#{r['source_id']}]" for r in other_rows)
            return out

    def journal_status(self, scope: str = "companion_events") -> dict[str, Any]:
        with self._conn() as db:
            cur = db.execute("SELECT * FROM event_cursors WHERE scope = ?", (scope,)).fetchone()
            cnt = db.execute("SELECT COUNT(*), SUM(is_milestone) FROM event_journal").fetchone()
            cursor_val = int(cur["last_source_id"]) if cur else 0
            latest_val = int(cur["latest_source_id"]) if (cur and "latest_source_id" in cur.keys()) else cursor_val
            return {
                "total_events": int(cnt[0] or 0),
                "total_milestones": int(cnt[1] or 0),
                "cursor": cursor_val,
                "first_source_id": int(cur["first_source_id"]) if cur else 0,
                "latest_source_id": latest_val,
                "journal_lag": max(0, latest_val - cursor_val),
                "gap_detected": bool(cur["gap_detected"]) if cur else False,
                "gap_note": str(cur["gap_note"]) if cur else "",
            }

    # ------------------------------------------------------------------
    # Soft Work-Order Reservations (live applied plans ONLY — Issue #7)
    # ------------------------------------------------------------------
    def active_order_reservations(self, now_ts: int) -> dict[int, str]:
        """Return {order_id: character_slug} for unexpired APPLIED guild_order plans only."""
        reservations: dict[int, str] = {}
        with self._conn() as db:
            has_plans = db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='plans'"
            ).fetchone()[0]
            if not has_plans:
                return reservations
            pcols = {r[1] for r in db.execute("PRAGMA table_info(plans)").fetchall()}
            state_clause = "AND COALESCE(p.plan_state, 'applied') != 'shadow'" if "plan_state" in pcols else ""
            sub_state_clause = "AND COALESCE(plan_state, 'applied') != 'shadow'" if "plan_state" in pcols else ""
            rows = db.execute(
                f"""
                SELECT p.character_id, p.primary_goal_id
                FROM plans p
                INNER JOIN (
                    SELECT character_id, MAX(id) AS max_id
                    FROM plans
                    WHERE applied = 1 {sub_state_clause}
                    GROUP BY character_id
                ) m ON m.max_id = p.id
                WHERE p.applied = 1 {state_clause}
                  AND p.expires_at > ?
                  AND p.primary_goal_id LIKE 'guild_order:%'
                """,
                (now_ts,),
            ).fetchall()
            for r in rows:
                oid = parse_order_id(r["primary_goal_id"])
                if oid is not None:
                    reservations[oid] = str(r["character_id"])
        return reservations

    # ------------------------------------------------------------------
    # Open Intentions (reconciled against authoritative DB state)
    # ------------------------------------------------------------------
    def reconcile_intentions(
        self,
        comp: dict[str, Any],
        open_order_ids: set[int],
        now_ts: int,
        *,
        mutate: bool = True,
    ) -> list[dict[str, Any]]:
        """Close fulfilled intentions against authoritative MySQL state & seed natural next tier.

        When mutate=False (shadow mode, Issue #7), reads open intentions without mutating DB.
        """
        slug = comp["slug"]
        skills = comp.get("skills") or {}

        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM intentions WHERE character_id = ? AND status = 'open' ORDER BY updated_at DESC",
                (slug,),
            ).fetchall()
            if not mutate:
                return [
                    {"kind": str(r["kind"]), "target_key": str(r["target_key"]), "label": str(r["label"])}
                    for r in rows[:3]
                ]

            open_items: list[dict[str, Any]] = []
            for r in rows:
                kind = r["kind"]
                tkey = r["target_key"]
                done_reason = ""
                if kind == "skill_tier":
                    parts = tkey.split(":")
                    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                        sid, target_val = int(parts[1]), int(parts[2])
                        cur_val = skills.get(sid, (0, 0))[0]
                        if cur_val >= target_val:
                            done_reason = f"Reached {SKILL_NAMES.get(sid, sid)} {cur_val}/{target_val}"
                elif kind == "work_order":
                    parts = tkey.split(":")
                    if len(parts) == 2 and parts[1].isdigit():
                        oid = int(parts[1])
                        if oid not in open_order_ids:
                            done_reason = f"Order #{oid} closed/delivered"

                if done_reason:
                    db.execute(
                        "UPDATE intentions SET status = 'completed', updated_at = ?, evidence = ? WHERE id = ?",
                        (now_ts, done_reason, int(r["id"])),
                    )
                else:
                    open_items.append({
                        "kind": kind,
                        "target_key": tkey,
                        "label": r["label"],
                    })

            has_skill_intent = any(it["kind"] == "skill_tier" for it in open_items)
            if not has_skill_intent:
                best_seed: tuple[int, int, int, str] | None = None
                for sid, (val, mx) in sorted(skills.items()):
                    if sid not in (164, 165, 171, 182, 186, 197, 202, 333, 393, 755, 773):
                        continue
                    bp = next_skill_tier(val, mx)
                    if bp is None:
                        continue
                    gap = bp - val
                    if 1 <= gap <= 45:
                        lbl = f"Reach {SKILL_NAMES.get(sid, str(sid))} {bp} (currently {val})"
                        if best_seed is None or gap < best_seed[0]:
                            best_seed = (gap, sid, bp, lbl)
                if best_seed is not None:
                    _, sid, bp, lbl = best_seed
                    tkey = f"skill:{sid}:{bp}"
                    db.execute(
                        """
                        INSERT INTO intentions (character_id, kind, target_key, label, status, created_at, updated_at, evidence)
                        VALUES (?, 'skill_tier', ?, ?, 'open', ?, ?, 'auto_tier')
                        ON CONFLICT(character_id, target_key) DO UPDATE SET
                            label = excluded.label,
                            status = 'open',
                            updated_at = excluded.updated_at
                        """,
                        (slug, tkey, lbl, now_ts, now_ts),
                    )
                    open_items.append({"kind": "skill_tier", "target_key": tkey, "label": lbl})
            else:
                for it in open_items:
                    if it["kind"] == "skill_tier":
                        parts = it["target_key"].split(":")
                        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                            sid, bp = int(parts[1]), int(parts[2])
                            cur = skills.get(sid, (0, 0))[0]
                            new_lbl = f"Reach {SKILL_NAMES.get(sid, str(sid))} {bp} (currently {cur})"
                            if new_lbl != it["label"]:
                                it["label"] = new_lbl
                                db.execute(
                                    "UPDATE intentions SET label = ?, updated_at = ? WHERE character_id = ? AND target_key = ?",
                                    (new_lbl, now_ts, slug, it["target_key"]),
                                )

        return open_items[:3]

    def record_plan_intention(
        self,
        slug: str,
        plan: dict[str, Any],
        primary_desc: str,
        now_ts: int,
    ) -> None:
        """Persist a claimed guild work order as an open intention (ONLY call for applied live plans)."""
        oid = parse_order_id(plan.get("primary_goal_id", ""))
        if oid is None:
            return
        tkey = f"order:{oid}"
        label = f"Deliver order #{oid}: {primary_desc[:90]}"
        with self._conn() as db:
            db.execute(
                """
                INSERT INTO intentions (character_id, kind, target_key, label, status, created_at, updated_at, evidence)
                VALUES (?, 'work_order', ?, ?, 'open', ?, ?, 'planned')
                ON CONFLICT(character_id, target_key) DO UPDATE SET
                    label = excluded.label,
                    status = 'open',
                    updated_at = excluded.updated_at
                """,
                (slug, tkey, label, now_ts, now_ts),
            )

    # ------------------------------------------------------------------
    # Recent Session Analysis: Fatigue + Goal-Specific Observed Outcomes
    # ------------------------------------------------------------------
    def analyze_recent_plans(self, slug: str, now_ts: int) -> dict[str, Any]:
        """Inspect recent APPLIED plans for variety fatigue and verified goal-specific stalls."""
        result: dict[str, Any] = {
            "recent_goals": [],
            "fatigue_category": "",
            "fatigue_streak": 0,
            "stalled_goal_id": "",
            "stall_streak": 0,
            "operational_alert": None,
        }
        with self._conn() as db:
            has_plans = db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='plans'"
            ).fetchone()[0]
            if not has_plans:
                return result
            pcols = {r[1] for r in db.execute("PRAGMA table_info(plans)").fetchall()}
            state_clause = "AND COALESCE(plan_state, 'applied') != 'shadow'" if "plan_state" in pcols else ""

            def col_or(name: str, default_sql: str) -> str:
                return name if name in pcols else f"{default_sql} AS {name}"

            sel = ", ".join([
                "planned_at",
                "expires_at",
                "applied",
                "primary_goal_id",
                "reason_code",
                col_or("plan_state", "'applied'"),
                col_or("proposal_status", "''"),
                col_or("advisory_only", "0"),
                col_or("start_level", "0"),
                col_or("outcome_levels", "0"),
                col_or("outcome_xp", "0"),
                col_or("outcome_skillups", "0"),
                col_or("outcome_orders", "0"),
                col_or("outcome_quests", "0"),
                col_or("outcome_ledger_ops", "0"),
            ])
            rows = db.execute(
                f"""
                SELECT {sel}
                FROM plans
                WHERE character_id = ? AND applied = 1 {state_clause}
                ORDER BY id DESC LIMIT 6
                """,
                (slug,),
            ).fetchall()

        if not rows:
            return result

        # Compact last-3 history for prompt with honest outcome labels
        for r in rows[:3]:
            rd = dict(r)
            out_class = classify_plan_outcome(rd)
            out_bits = [out_class]
            if int(rd["outcome_levels"] or 0) > 0:
                out_bits.append(f"+{int(rd['outcome_levels'])}lv")
            if int(rd["outcome_xp"] or 0) > 0:
                out_bits.append(f"+{int(rd['outcome_xp'])}xp")
            if int(rd["outcome_quests"] or 0) > 0:
                out_bits.append(f"+{int(rd['outcome_quests'])}q")
            if int(rd["outcome_skillups"] or 0) > 0:
                out_bits.append(f"+{int(rd['outcome_skillups'])}sk")
            if int(rd["outcome_orders"] or 0) > 0:
                out_bits.append(f"+{int(rd['outcome_orders'])}ord")
            if int(rd["outcome_ledger_ops"] or 0) > 0:
                out_bits.append(f"+{int(rd['outcome_ledger_ops'])}ops")
            result["recent_goals"].append({
                "goal_id": rd["primary_goal_id"],
                "category": goal_category(rd["primary_goal_id"]),
                "outcome": " ".join(out_bits),
            })

        # 1. Session Variety / Fatigue streak on consecutive identical category (applied plans only)
        first_cat = goal_category(rows[0]["primary_goal_id"])
        cat_streak = 0
        for r in rows:
            if goal_category(r["primary_goal_id"]) == first_cat:
                cat_streak += 1
            else:
                break
        if cat_streak >= 2:
            result["fatigue_category"] = first_cat
            result["fatigue_streak"] = cat_streak

        # 2. Goal-Specific Verified Stall Caution (Issue #5):
        #    ONLY count finished/aged applied plans classified explicitly as 'stalled'.
        #    Advisory goals, interrupted plans, expired/rejected/unaccepted proposals, XP gains,
        #    capped-skill gathering ops, and quest turn-ins are NEVER counted as stalls.
        aged = [
            dict(r) for r in rows
            if int(r["start_level"] or 0) > 0
            and (int(r["expires_at"]) <= now_ts or (now_ts - int(r["planned_at"])) >= 900)
        ]
        if len(aged) >= 2:
            same_goal = aged[0]["primary_goal_id"]
            if not is_advisory_goal(same_goal):
                stall_cnt = 0
                for rd in aged:
                    if rd["primary_goal_id"] != same_goal:
                        break
                    if classify_plan_outcome(rd) == "stalled":
                        stall_cnt += 1
                    else:
                        break
                if stall_cnt >= 2:
                    result["stalled_goal_id"] = same_goal
                    result["stall_streak"] = stall_cnt
                    result["operational_alert"] = {
                        "character_id": slug,
                        "goal_id": same_goal,
                        "streak": stall_cnt,
                        "kind": "verified_goal_stall",
                        "message": f"{stall_cnt} verified sessions on {same_goal} with 0 goal-specific progress",
                    }

        return result

    # ------------------------------------------------------------------
    # Full Compact Memory Packet (~200-320 tokens) for Claude Sonnet 5.5
    # ------------------------------------------------------------------
    def build_packet(
        self,
        comp: dict[str, Any],
        world_ctx: dict[str, Any],
        now_ts: int,
        *,
        mutate_intentions: bool = True,
    ) -> dict[str, Any]:
        """Build the bounded, strictly grounded Memory packet for one companion."""
        open_order_ids = {int(wo["id"]) for wo in world_ctx.get("work_orders", [])}
        intentions = self.reconcile_intentions(
            comp, open_order_ids, now_ts, mutate=mutate_intentions
        )
        recent = self.analyze_recent_plans(comp["slug"], now_ts)

        # Prefer durable journal milestones (survives >12 events), falling back to live snapshot list
        milestones = self.get_character_milestones(comp["slug"], limit=5)
        if not milestones:
            milestones = list(comp.get("recent_events") or [])[:5]

        jstat = self.journal_status()
        packet: dict[str, Any] = {
            "milestones": milestones,
            "history_coverage": "partial" if jstat["gap_detected"] else "complete",
            "open_intentions": [it["label"] for it in intentions],
            "social_bonds": list(comp.get("social_bonds") or [])[:3],
            "recent_sessions": recent["recent_goals"],
        }
        if recent["fatigue_category"]:
            packet["variety_hint"] = (
                f"Spent {recent['fatigue_streak']} consecutive sessions on '{recent['fatigue_category']}'; "
                f"consider rotating unless a guild order is urgent."
            )
        if recent["stalled_goal_id"]:
            packet["stall_caution"] = (
                f"Goal '{recent['stalled_goal_id']}' had 0 verified progress for {recent['stall_streak']} "
                f"sessions (possible missing mats or pathing snag); prefer an alternative goal."
            )

        return {
            "packet": packet,
            "intentions": intentions,
            "fatigue_category": recent["fatigue_category"],
            "fatigue_streak": recent["fatigue_streak"],
            "stalled_goal_id": recent["stalled_goal_id"],
            "stall_streak": recent["stall_streak"],
            "operational_alert": recent["operational_alert"],
        }

    def all_open_intentions(self) -> dict[str, list[dict[str, str]]]:
        """Return {character_slug: [{kind, target_key, label}, ...]} for Azeroth Watch."""
        out: dict[str, list[dict[str, str]]] = {}
        with self._conn() as db:
            rows = db.execute(
                "SELECT character_id, kind, target_key, label FROM intentions WHERE status = 'open' ORDER BY updated_at DESC"
            ).fetchall()
            for r in rows:
                out.setdefault(str(r["character_id"]), []).append({
                    "kind": str(r["kind"]),
                    "target_key": str(r["target_key"]),
                    "label": str(r["label"]),
                })
        return out

    def recent_completed_intentions(self, limit: int = 12) -> list[dict[str, Any]]:
        """Return recently completed intentions for observability."""
        with self._conn() as db:
            rows = db.execute(
                """
                SELECT character_id, kind, label, evidence, updated_at
                FROM intentions WHERE status = 'completed'
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
