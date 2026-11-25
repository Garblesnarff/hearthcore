"""Lean Persistent Memory v1 + Soft Work-Order Reservations for Hearthcore.

Design principles (aligned with docs/PERSISTENT_MEMORY.md M1-M3):
- Single SQLite WAL store (runtime/brain.db) + authoritative MySQL facts. Zero extra services.
- Distinct memory kinds, never blended into one opaque score:
  1. Factual Milestones (from companion_events) — authoritative, never decay.
  2. Open Intentions (intentions table in brain.db) — reconciled against DB state each turn.
  3. Session Variety / Fatigue Guard — nudges variety after 2+ consecutive sessions in one category.
  4. Scoped Stall Caution + Operational Alerts — flags 2+ consecutive 0-progress sessions on the
     exact same primary_goal_id without globally penalizing the activity category.
  5. Social Familiarity (from companion_relationships) — top guild bonds.
- Soft Work-Order Reservations — prevents online crafters from colliding on the same guild order.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from .guides import SKILL_NAMES

TIER_BREAKPOINTS = (75, 150, 225, 300)


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


class MemoryEngine:
    """Persistent companion memory + soft multi-bot coordination backed by brain.db."""

    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            db.execute("PRAGMA journal_mode=WAL")
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

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Soft Work-Order Reservations (multi-bot coordination)
    # ------------------------------------------------------------------
    def active_order_reservations(self, now_ts: int) -> dict[int, str]:
        """Return {order_id: character_slug} for currently unexpired guild_order plans."""
        reservations: dict[int, str] = {}
        with self._conn() as db:
            has_plans = db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='plans'"
            ).fetchone()[0]
            if not has_plans:
                return reservations
            rows = db.execute(
                """
                SELECT p.character_id, p.primary_goal_id
                FROM plans p
                INNER JOIN (
                    SELECT character_id, MAX(id) AS max_id FROM plans GROUP BY character_id
                ) m ON m.max_id = p.id
                WHERE p.expires_at > ? AND p.primary_goal_id LIKE 'guild_order:%'
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
    ) -> list[dict[str, Any]]:
        """Close fulfilled intentions against authoritative MySQL state & seed natural next tier."""
        slug = comp["slug"]
        skills = comp.get("skills") or {}

        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM intentions WHERE character_id = ? AND status = 'open' ORDER BY updated_at DESC",
                (slug,),
            ).fetchall()

            open_items: list[dict[str, Any]] = []
            for r in rows:
                kind = r["kind"]
                tkey = r["target_key"]
                done_reason = ""
                if kind == "skill_tier":
                    # target_key format: skill:<skill_id>:<target_val>
                    parts = tkey.split(":")
                    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                        sid, target_val = int(parts[1]), int(parts[2])
                        cur_val = skills.get(sid, (0, 0))[0]
                        if cur_val >= target_val:
                            done_reason = f"Reached {SKILL_NAMES.get(sid, sid)} {cur_val}/{target_val}"
                elif kind == "work_order":
                    # target_key format: order:<order_id>
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

            # Seed at most 1 natural profession tier intention if companion has no open skill_tier intention
            has_skill_intent = any(it["kind"] == "skill_tier" for it in open_items)
            if not has_skill_intent:
                best_seed: tuple[int, int, int, str] | None = None  # (gap, sid, target_bp, label)
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
                # Refresh current progress label for open skill_tier intentions
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
        """If Claude chose a guild work order, persist it as an open intention until delivered."""
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
    # Recent Session Analysis: Fatigue + Scoped Stall Caution
    # ------------------------------------------------------------------
    def analyze_recent_plans(self, slug: str, now_ts: int) -> dict[str, Any]:
        """Inspect recent plans in brain.db for variety fatigue and scoped goal stalls."""
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
            has_out = "outcome_levels" in pcols
            if has_out:
                rows = db.execute(
                    """
                    SELECT planned_at, expires_at, primary_goal_id, reason_code,
                           start_level, outcome_levels, outcome_skillups, outcome_orders
                    FROM plans
                    WHERE character_id = ?
                    ORDER BY id DESC LIMIT 5
                    """,
                    (slug,),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT planned_at, expires_at, primary_goal_id, reason_code,
                           0 AS start_level, 0 AS outcome_levels, 0 AS outcome_skillups, 0 AS outcome_orders
                    FROM plans
                    WHERE character_id = ?
                    ORDER BY id DESC LIMIT 5
                    """,
                    (slug,),
                ).fetchall()

        if not rows:
            return result

        # Compact last-3 history for prompt
        for r in rows[:3]:
            out_bits = []
            if int(r["outcome_levels"] or 0) > 0:
                out_bits.append(f"+{int(r['outcome_levels'])}lv")
            if int(r["outcome_skillups"] or 0) > 0:
                out_bits.append(f"+{int(r['outcome_skillups'])}sk")
            if int(r["outcome_orders"] or 0) > 0:
                out_bits.append(f"+{int(r['outcome_orders'])}ord")
            result["recent_goals"].append({
                "goal_id": r["primary_goal_id"],
                "category": goal_category(r["primary_goal_id"]),
                "outcome": ",".join(out_bits) if out_bits else "none",
            })

        # 1. Session Variety / Fatigue streak on consecutive identical category
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

        # 2. Scoped Stall Caution: 2+ consecutive aged/finished plans on EXACT SAME goal_id with 0 progress
        aged = [
            r for r in rows
            if int(r["start_level"] or 0) > 0
            and (int(r["expires_at"]) <= now_ts or (now_ts - int(r["planned_at"])) >= 900)
        ]
        if len(aged) >= 2:
            same_goal = aged[0]["primary_goal_id"]
            stall_cnt = 0
            for r in aged:
                if r["primary_goal_id"] != same_goal:
                    break
                progress = (
                    int(r["outcome_levels"] or 0)
                    + int(r["outcome_skillups"] or 0)
                    + int(r["outcome_orders"] or 0)
                )
                if progress == 0:
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
                    "message": f"{stall_cnt} sessions on {same_goal} with 0 levels/skill-ups/orders",
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
    ) -> dict[str, Any]:
        """Build the bounded, strictly grounded Memory v1 packet for one companion."""
        open_order_ids = {int(wo["id"]) for wo in world_ctx.get("work_orders", [])}
        intentions = self.reconcile_intentions(comp, open_order_ids, now_ts)
        recent = self.analyze_recent_plans(comp["slug"], now_ts)

        packet: dict[str, Any] = {
            "milestones": list(comp.get("recent_events") or [])[:5],
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
                f"Goal '{recent['stalled_goal_id']}' showed 0 progress for {recent['stall_streak']} "
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
