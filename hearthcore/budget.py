"""Shared transactional API budget & usage accounting ledger (SQLite WAL).

Addresses Issue #8:
- Single shared durable budget across individual planner, Guild Quartermaster, manual runs,
  and shadow runs.
- Atomic cross-process pre-call reservation via SQLite BEGIN IMMEDIATE.
- Records usage from every HTTP 200 response BEFORE JSON parsing or schema validation so
  billable invalid/rejected responses are never lost.
- Handles cache read/write tokens and avoids double-counting Anthropic thinking tokens
  (which Anthropic includes inside output_tokens).
- Conservative reservation retained for timed-out / uncertain network failures until reconciled.
- Configurable monthly USD ceiling (below credit allowance) + optional daily call cap.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import Invalid

# USD per 1M tokens: (input, output, cache_write, cache_read)
MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    "opus": (15.0, 75.0, 18.75, 1.50),
    "sonnet": (3.0, 15.0, 3.75, 0.30),
    "haiku": (1.0, 5.0, 1.25, 0.10),
}


class BudgetExceeded(Invalid):
    """Raised when monthly USD ceiling or daily call cap prevents an API call."""


class InvalidWithUsage(Invalid):
    """Raised when an API call returned HTTP 200 (billable) but failed JSON/schema validation."""

    def __init__(self, message: str, meta: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.meta: dict[str, Any] = meta or {}


def pricing_for_model(model_name: str) -> tuple[float, float, float, float]:
    ml = (model_name or "").lower()
    if "opus" in ml:
        return MODEL_PRICING["opus"]
    if "sonnet" in ml:
        return MODEL_PRICING["sonnet"]
    return MODEL_PRICING["haiku"]


def estimate_cost_usd(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Compute USD cost without double-counting thinking_tokens (already included in output_tokens)."""
    in_r, out_r, cw_r, cr_r = pricing_for_model(model_name)
    total = (
        max(0, int(input_tokens)) * in_r
        + max(0, int(output_tokens)) * out_r
        + max(0, int(cache_creation_tokens)) * cw_r
        + max(0, int(cache_read_tokens)) * cr_r
    ) / 1_000_000.0
    return round(total, 6)


def conservative_max_cost_usd(
    model_name: str,
    est_input_tokens: int,
    max_output_tokens: int,
) -> float:
    """Conservative worst-case pre-call cost reservation."""
    # Add 25% safety headroom on estimated input tokens + full max_output_tokens
    safe_in = int(max(500, est_input_tokens) * 1.25)
    safe_out = max(200, int(max_output_tokens))
    return estimate_cost_usd(model_name, safe_in, safe_out)


def extract_usage_meta(
    result: dict[str, Any],
    fallback_model: str,
    effort: str,
    latency_ms: int,
) -> dict[str, Any]:
    """Extract billable usage metadata directly from raw Anthropic JSON BEFORE validating content."""
    usage = result.get("usage") or {}
    out_details = usage.get("output_tokens_details") or {}
    actual_model = str(result.get("model") or fallback_model or "")
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    cw_tok = int(usage.get("cache_creation_input_tokens") or 0)
    cr_tok = int(usage.get("cache_read_input_tokens") or 0)
    think_tok = int(out_details.get("thinking_tokens") or 0)
    cost = estimate_cost_usd(actual_model, in_tok, out_tok, cw_tok, cr_tok)
    return {
        "model": actual_model,
        "effort": effort,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_creation_tokens": cw_tok,
        "cache_read_tokens": cr_tok,
        "thinking_tokens": think_tok,
        "cost_usd": cost,
        "latency_ms": int(latency_ms),
    }


def _month_key(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def _day_key(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


class BudgetLedger:
    """Process-safe transactional API budget ledger stored in brain.db."""

    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("""
                CREATE TABLE IF NOT EXISTS api_ledger (
                    id INTEGER PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    settled_at INTEGER NOT NULL DEFAULT 0,
                    month_key TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    caller TEXT NOT NULL,
                    character_id TEXT NOT NULL DEFAULT '',
                    is_shadow INTEGER NOT NULL DEFAULT 0,
                    requested_model TEXT NOT NULL,
                    actual_model TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    reserved_usd REAL NOT NULL DEFAULT 0.0,
                    actual_usd REAL NOT NULL DEFAULT 0.0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    thinking_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    error_note TEXT NOT NULL DEFAULT ''
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_ledger_month ON api_ledger(month_key, status)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ledger_day ON api_ledger(day_key, status)")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def configured_caps(self) -> tuple[float, int]:
        """Return (monthly_usd_cap, daily_call_cap) from environment with safe defaults."""
        credit_total = float(os.getenv("HEARTHCORE_CREDIT_USD", "98.0") or 98.0)
        default_monthly = round(max(1.0, min(85.0, credit_total * 0.88)), 2)
        monthly_usd = float(os.getenv("HEARTHCORE_MONTHLY_BUDGET_USD", str(default_monthly)) or default_monthly)
        daily_calls = int(os.getenv("HEARTHCORE_DAILY_CALL_CAP", "500") or 500)
        return max(0.0, monthly_usd), max(0, daily_calls)

    def reserve(
        self,
        *,
        caller: str,
        model: str,
        est_input_tokens: int,
        max_output_tokens: int,
        character_id: str = "",
        is_shadow: bool = False,
        now_ts: int | None = None,
        monthly_usd_cap: float | None = None,
        daily_call_cap: int | None = None,
    ) -> int:
        """Atomically reserve budget before making an API request, or raise BudgetExceeded."""
        ts = int(now_ts if now_ts is not None else time.time())
        mkey = _month_key(ts)
        dkey = _day_key(ts)
        cfg_m_usd, cfg_d_calls = self.configured_caps()
        m_cap = cfg_m_usd if monthly_usd_cap is None else float(monthly_usd_cap)
        d_cap = cfg_d_calls if daily_call_cap is None else int(daily_call_cap)
        reserve_usd = conservative_max_cost_usd(model, est_input_tokens, max_output_tokens)

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Expire stale in-flight 'reserved' rows older than 5 minutes into 'uncertain'
            conn.execute(
                "UPDATE api_ledger SET status='uncertain', settled_at=?, error_note='stale_reservation' "
                "WHERE status='reserved' AND created_at < ?",
                (ts, ts - 300),
            )

            # Committed spend this month: actual_usd for ok/rejected, reserved_usd for in-flight reserved & uncertain
            row_m = conn.execute(
                """
                SELECT COALESCE(SUM(
                    CASE
                        WHEN status IN ('ok', 'rejected') THEN actual_usd
                        WHEN status IN ('reserved', 'uncertain') THEN reserved_usd
                        ELSE 0.0
                    END
                ), 0.0) AS committed_usd
                FROM api_ledger
                WHERE month_key = ?
                """,
                (mkey,),
            ).fetchone()
            committed_month = float(row_m["committed_usd"] or 0.0)

            # Daily billable/attempted calls (excluding clean pre-call budget blocks)
            row_d = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM api_ledger
                WHERE day_key = ? AND status IN ('reserved', 'ok', 'rejected', 'uncertain')
                """,
                (dkey,),
            ).fetchone()
            calls_today = int(row_d["cnt"] or 0)

            if d_cap > 0 and calls_today >= d_cap:
                conn.execute("ROLLBACK")
                raise BudgetExceeded(f"daily_call_cap_reached:{calls_today}/{d_cap}")

            if m_cap >= 0.0 and (committed_month + reserve_usd) > m_cap:
                conn.execute("ROLLBACK")
                raise BudgetExceeded(
                    f"monthly_budget_cap_reached:{committed_month:.4f}+{reserve_usd:.4f}>{m_cap:.2f}"
                )

            cur = conn.execute(
                """
                INSERT INTO api_ledger (
                    created_at, month_key, day_key, caller, character_id, is_shadow,
                    requested_model, status, reserved_usd
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (ts, mkey, dkey, caller, character_id, 1 if is_shadow else 0, model, "reserved", reserve_usd),
            )
            res_id = int(cur.lastrowid)
            conn.execute("COMMIT")
            return res_id
        except BudgetExceeded:
            raise
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def settle(
        self,
        reservation_id: int,
        *,
        status: str,
        meta: dict[str, Any] | None = None,
        error_note: str = "",
        now_ts: int | None = None,
    ) -> None:
        """Finalize a pre-call reservation with exact usage ('ok'/'rejected'), 'uncertain', or 'error'."""
        ts = int(now_ts if now_ts is not None else time.time())
        m = meta or {}
        actual_model = str(m.get("model") or "")
        in_tok = int(m.get("input_tokens") or 0)
        out_tok = int(m.get("output_tokens") or 0)
        cw_tok = int(m.get("cache_creation_tokens") or 0)
        cr_tok = int(m.get("cache_read_tokens") or 0)
        think_tok = int(m.get("thinking_tokens") or 0)
        lat_ms = int(m.get("latency_ms") or 0)
        if status in ("ok", "rejected"):
            actual_usd = float(
                m.get("cost_usd")
                if m.get("cost_usd") is not None
                else estimate_cost_usd(actual_model, in_tok, out_tok, cw_tok, cr_tok)
            )
        else:
            actual_usd = 0.0

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE api_ledger
                SET settled_at = ?, actual_model = COALESCE(NULLIF(?, ''), requested_model),
                    status = ?, actual_usd = ?,
                    input_tokens = ?, output_tokens = ?,
                    cache_creation_tokens = ?, cache_read_tokens = ?, thinking_tokens = ?,
                    latency_ms = ?, error_note = ?
                WHERE id = ?
                """,
                (
                    ts, actual_model, status, actual_usd,
                    in_tok, out_tok, cw_tok, cr_tok, think_tok,
                    lat_ms, error_note[:200], int(reservation_id),
                ),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def summary(self, now_ts: int | None = None) -> dict[str, Any]:
        ts = int(now_ts if now_ts is not None else time.time())
        mkey = _month_key(ts)
        dkey = _day_key(ts)
        m_cap, d_cap = self.configured_caps()
        conn = self._conn()
        try:
            m_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_cnt,
                    SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rej_cnt,
                    SUM(CASE WHEN status='uncertain' THEN 1 ELSE 0 END) AS unc_cnt,
                    COALESCE(SUM(CASE WHEN status IN ('ok','rejected') THEN actual_usd ELSE 0.0 END), 0.0) AS settled_usd,
                    COALESCE(SUM(CASE WHEN status IN ('reserved','uncertain') THEN reserved_usd ELSE 0.0 END), 0.0) AS reserved_usd
                FROM api_ledger WHERE month_key = ?
                """,
                (mkey,),
            ).fetchone()
            d_row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status IN ('reserved','ok','rejected','uncertain') THEN 1 ELSE 0 END) AS calls,
                    COALESCE(SUM(CASE WHEN status IN ('ok','rejected') THEN actual_usd WHEN status IN ('reserved','uncertain') THEN reserved_usd ELSE 0.0 END), 0.0) AS usd
                FROM api_ledger WHERE day_key = ?
                """,
                (dkey,),
            ).fetchone()
            settled = float(m_row["settled_usd"] or 0.0)
            reserved = float(m_row["reserved_usd"] or 0.0)
            committed = round(settled + reserved, 4)
            return {
                "month_key": mkey,
                "day_key": dkey,
                "monthly_cap_usd": round(m_cap, 2),
                "monthly_settled_usd": round(settled, 4),
                "monthly_reserved_usd": round(reserved, 4),
                "monthly_committed_usd": committed,
                "monthly_remaining_usd": round(max(0.0, m_cap - committed), 2),
                "daily_call_cap": d_cap,
                "daily_calls": int(d_row["calls"] or 0),
                "daily_committed_usd": round(float(d_row["usd"] or 0.0), 4),
                "month_ok": int(m_row["ok_cnt"] or 0),
                "month_rejected": int(m_row["rej_cnt"] or 0),
                "month_uncertain": int(m_row["unc_cnt"] or 0),
            }
        finally:
            conn.close()
