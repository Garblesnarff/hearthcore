"""Shared transactional API budget, pricing, circuit breaker & usage ledger (SQLite WAL).

Addresses Issues #8 and #55 (#56 consolidated):
- Single shared durable budget across individual planner, Guild Quartermaster, manual CLI,
  and shadow runs (no unbudgeted production API path).
- Atomic cross-process pre-call reservation via SQLite BEGIN IMMEDIATE.
- Unknown model IDs are priced conservatively at the highest known tier (Opus).
- Cap value 0 unambiguously blocks calls; invalid env values fail fast with a clear error.
- Unified UTC timezone across day and billing-period windows, aligned with HEARTHCORE_CREDIT_RESET.
- Records usage from every HTTP 200 response BEFORE JSON parsing or schema validation so
  billable invalid/rejected/max_tokens-truncated responses are never lost.
- Tracks per-provider circuit breaker & Retry-After backoff state in SQLite so bad keys or
  rate limits never hammer the API silently.
"""
from __future__ import annotations

import calendar
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import Invalid

logger = logging.getLogger("hearthcore.budget")

# USD per 1M tokens: (input, output, cache_write, cache_read)
MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    "opus": (15.0, 75.0, 18.75, 1.50),
    "sonnet": (3.0, 15.0, 3.75, 0.30),
    "haiku": (1.0, 5.0, 1.25, 0.10),
}
MOST_EXPENSIVE_PRICING = MODEL_PRICING["opus"]
DEFAULT_DAILY_CALL_CAP = 500
DEFAULT_CREDIT_USD = 98.0
DEFAULT_CIRCUIT_THRESHOLD = 3
DEFAULT_CIRCUIT_COOLDOWN_SEC = 300


class BudgetExceeded(Invalid):
    """Raised when monthly USD ceiling or daily call cap prevents an API call."""


class ProviderBackoffError(Invalid):
    """Raised when provider circuit breaker or Retry-After backoff blocks an API call."""


class InvalidWithUsage(Invalid):
    """Raised when an API call returned HTTP 200 (billable) but failed JSON/schema/truncation checks."""

    def __init__(self, message: str, meta: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.meta: dict[str, Any] = meta or {}


def pricing_for_model(model_name: str) -> tuple[float, float, float, float]:
    """Return (input, output, cache_write, cache_read) USD/1M; unknown models use highest tier."""
    ml = (model_name or "").lower()
    if "opus" in ml:
        return MODEL_PRICING["opus"]
    if "sonnet" in ml:
        return MODEL_PRICING["sonnet"]
    if "haiku" in ml:
        return MODEL_PRICING["haiku"]
    return MOST_EXPENSIVE_PRICING


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
    stop_reason = str(result.get("stop_reason") or "")
    cost = estimate_cost_usd(actual_model, in_tok, out_tok, cw_tok, cr_tok)
    return {
        "model": actual_model,
        "effort": effort,
        "stop_reason": stop_reason,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_creation_tokens": cw_tok,
        "cache_read_tokens": cr_tok,
        "thinking_tokens": think_tok,
        "cost_usd": cost,
        "latency_ms": int(latency_ms),
    }


def _parse_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = float(raw.strip())
    except ValueError as exc:
        raise Invalid(f"Invalid {name}={raw!r}: expected non-negative number") from exc
    if val < 0.0:
        raise Invalid(f"Invalid {name}={raw!r}: must be >= 0")
    return val


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = int(raw.strip())
    except ValueError as exc:
        raise Invalid(f"Invalid {name}={raw!r}: expected non-negative integer") from exc
    if val < 0:
        raise Invalid(f"Invalid {name}={raw!r}: must be >= 0")
    return val


def _day_key(ts: int) -> str:
    """Unified UTC day key (YYYY-MM-DD)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _month_key(ts: int, credit_reset: str | None = None) -> str:
    """Unified UTC billing-period key aligned with HEARTHCORE_CREDIT_RESET (YYYY-MM-DD) when set."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    reset_str = (credit_reset if credit_reset is not None else os.getenv("HEARTHCORE_CREDIT_RESET", "")).strip()
    if reset_str:
        try:
            reset_dt = datetime.strptime(reset_str, "%Y-%m-%d")
            reset_day = reset_dt.day
            year, month = dt.year, dt.month
            last_dom = calendar.monthrange(year, month)[1]
            eff_day = min(reset_day, last_dom)
            if dt.day < eff_day:
                if month == 1:
                    year -= 1
                    month = 12
                else:
                    month -= 1
                eff_day = min(reset_day, calendar.monthrange(year, month)[1])
            return f"{year:04d}-{month:02d}-{eff_day:02d}"
        except ValueError as exc:
            logger.warning("Ignoring unparseable credit_reset %r: %s", credit_reset, exc)
    return dt.strftime("%Y-%m")


class BudgetLedger:
    """Process-safe transactional API budget & provider-health ledger stored in brain.db."""

    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._conn()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("""
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_month ON api_ledger(month_key, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_day ON api_ledger(day_key, status)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_provider_state (
                    provider TEXT PRIMARY KEY,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    circuit_open_until INTEGER NOT NULL DEFAULT 0,
                    retry_after_until INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL DEFAULT 0
                )
            """)
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def configured_caps(self) -> tuple[float, int]:
        """Return (monthly_usd_cap, daily_call_cap); fail fast on invalid env values. Cap 0 = block."""
        credit_total = _parse_float_env("HEARTHCORE_CREDIT_USD", DEFAULT_CREDIT_USD)
        default_monthly = round(max(1.0, min(85.0, credit_total * 0.88)), 2) if credit_total > 0 else 0.0
        monthly_usd = _parse_float_env("HEARTHCORE_MONTHLY_BUDGET_USD", default_monthly)
        daily_calls = _parse_int_env("HEARTHCORE_DAILY_CALL_CAP", DEFAULT_DAILY_CALL_CAP)
        return monthly_usd, daily_calls

    def check_provider_available(self, provider: str = "anthropic", now_ts: int | None = None) -> None:
        """Raise ProviderBackoffError if provider circuit breaker or Retry-After window is active."""
        st = self.provider_state(provider, now_ts=now_ts)
        if not st["available"]:
            wait_s = st["wait_seconds"]
            reason = "retry_after" if st["retry_after_active"] else "circuit_open"
            raise ProviderBackoffError(
                f"provider_{reason}:{provider}:wait_{wait_s}s:{st['last_error']}"
            )

    def record_provider_success(self, provider: str = "anthropic", now_ts: int | None = None) -> None:
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO api_provider_state (provider, consecutive_failures, circuit_open_until, retry_after_until, last_error, updated_at)
                VALUES (?, 0, 0, 0, '', ?)
                ON CONFLICT(provider) DO UPDATE SET
                    consecutive_failures = 0,
                    circuit_open_until = 0,
                    retry_after_until = 0,
                    last_error = '',
                    updated_at = excluded.updated_at
                """,
                (provider, ts),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def record_provider_failure(
        self,
        provider: str = "anthropic",
        *,
        error_note: str = "",
        retry_after_sec: int = 0,
        threshold: int | None = None,
        cooldown_sec: int = DEFAULT_CIRCUIT_COOLDOWN_SEC,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        ts = int(now_ts if now_ts is not None else time.time())
        thresh = threshold if threshold is not None else _parse_int_env(
            "HEARTHCORE_CIRCUIT_BREAKER_THRESHOLD", DEFAULT_CIRCUIT_THRESHOLD
        )
        thresh = max(1, int(thresh))
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT consecutive_failures, circuit_open_until, retry_after_until FROM api_provider_state WHERE provider = ?",
                (provider,),
            ).fetchone()
            fails = (int(row["consecutive_failures"]) if row else 0) + 1
            open_until = int(row["circuit_open_until"]) if row else 0
            retry_until = int(row["retry_after_until"]) if row else 0
            if retry_after_sec > 0:
                retry_until = max(retry_until, ts + int(retry_after_sec))
            if fails >= thresh:
                open_until = max(open_until, ts + int(cooldown_sec))
            conn.execute(
                """
                INSERT INTO api_provider_state (provider, consecutive_failures, circuit_open_until, retry_after_until, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    consecutive_failures = excluded.consecutive_failures,
                    circuit_open_until = excluded.circuit_open_until,
                    retry_after_until = excluded.retry_after_until,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (provider, fails, open_until, retry_until, error_note[:160], ts),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
        return self.provider_state(provider, now_ts=ts)

    def provider_state(self, provider: str = "anthropic", now_ts: int | None = None) -> dict[str, Any]:
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT consecutive_failures, circuit_open_until, retry_after_until, last_error, updated_at "
                "FROM api_provider_state WHERE provider = ?",
                (provider,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {
                "provider": provider,
                "available": True,
                "consecutive_failures": 0,
                "circuit_open": False,
                "retry_after_active": False,
                "wait_seconds": 0,
                "last_error": "",
            }
        open_until = int(row["circuit_open_until"] or 0)
        retry_until = int(row["retry_after_until"] or 0)
        circuit_open = open_until > ts
        retry_active = retry_until > ts
        wait_sec = max(0, max(open_until, retry_until) - ts)
        return {
            "provider": provider,
            "available": not (circuit_open or retry_active),
            "consecutive_failures": int(row["consecutive_failures"] or 0),
            "circuit_open": circuit_open,
            "retry_after_active": retry_active,
            "wait_seconds": wait_sec,
            "last_error": str(row["last_error"] or ""),
        }

    def reconcile_uncertain(
        self,
        *,
        reservation_id: int | None = None,
        older_than_sec: int = 86400,
        resolution_status: str = "reconciled",
        actual_usd: float = 0.0,
        note: str = "reconciled",
        now_ts: int | None = None,
    ) -> int:
        """Reconcile uncertain reservations either by ID or when aged past older_than_sec."""
        ts = int(now_ts if now_ts is not None else time.time())
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if reservation_id is not None:
                cur = conn.execute(
                    "UPDATE api_ledger SET status=?, actual_usd=?, settled_at=?, error_note=? "
                    "WHERE id=? AND status='uncertain'",
                    (resolution_status, max(0.0, float(actual_usd)), ts, note[:200], int(reservation_id)),
                )
            else:
                cur = conn.execute(
                    "UPDATE api_ledger SET status=?, actual_usd=?, settled_at=?, error_note=? "
                    "WHERE status='uncertain' AND created_at <= ?",
                    (resolution_status, max(0.0, float(actual_usd)), ts, note[:200], ts - max(0, int(older_than_sec))),
                )
            changed = int(cur.rowcount or 0)
            conn.execute("COMMIT")
            return changed
        finally:
            conn.close()

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
        credit_reset: str | None = None,
    ) -> int:
        """Atomically reserve budget before making an API request, or raise BudgetExceeded."""
        ts = int(now_ts if now_ts is not None else time.time())
        mkey = _month_key(ts, credit_reset=credit_reset)
        dkey = _day_key(ts)
        cfg_m_usd, cfg_d_calls = self.configured_caps()
        m_cap = cfg_m_usd if monthly_usd_cap is None else float(monthly_usd_cap)
        d_cap = cfg_d_calls if daily_call_cap is None else int(daily_call_cap)
        if m_cap < 0.0:
            raise Invalid(f"Invalid monthly_usd_cap={m_cap}: must be >= 0")
        if d_cap < 0:
            raise Invalid(f"Invalid daily_call_cap={d_cap}: must be >= 0")

        reserve_usd = conservative_max_cost_usd(model, est_input_tokens, max_output_tokens)

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE api_ledger SET status='uncertain', settled_at=?, error_note='stale_reservation' "
                "WHERE status='reserved' AND created_at < ?",
                (ts, ts - 300),
            )

            row_m = conn.execute(
                """
                SELECT COALESCE(SUM(
                    CASE
                        WHEN status IN ('ok', 'rejected', 'reconciled') THEN actual_usd
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

            # Daily attempted calls include ok, rejected, uncertain, reserved, AND error (prevents silent retry storms)
            row_d = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM api_ledger
                WHERE day_key = ? AND status IN ('reserved', 'ok', 'rejected', 'uncertain', 'error')
                """,
                (dkey,),
            ).fetchone()
            calls_today = int(row_d["cnt"] or 0)

            # Cap value 0 unambiguously blocks calls (Issue #55/#56)
            if calls_today >= d_cap:
                conn.execute("ROLLBACK")
                raise BudgetExceeded(f"daily_call_cap_reached:{calls_today}/{d_cap}")

            if (committed_month + reserve_usd) > m_cap:
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
            except sqlite3.Error as rb_exc:
                logger.debug("Budget ledger rollback skipped: %s", rb_exc)
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

    def summary(self, now_ts: int | None = None, credit_reset: str | None = None) -> dict[str, Any]:
        ts = int(now_ts if now_ts is not None else time.time())
        mkey = _month_key(ts, credit_reset=credit_reset)
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
                    SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS err_cnt,
                    COALESCE(SUM(CASE WHEN status IN ('ok','rejected','reconciled') THEN actual_usd ELSE 0.0 END), 0.0) AS settled_usd,
                    COALESCE(SUM(CASE WHEN status IN ('reserved','uncertain') THEN reserved_usd ELSE 0.0 END), 0.0) AS reserved_usd
                FROM api_ledger WHERE month_key = ?
                """,
                (mkey,),
            ).fetchone()
            d_row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status IN ('reserved','ok','rejected','uncertain','error') THEN 1 ELSE 0 END) AS attempted,
                    SUM(CASE WHEN status IN ('ok','rejected') THEN 1 ELSE 0 END) AS billed,
                    COALESCE(SUM(CASE WHEN status IN ('ok','rejected','reconciled') THEN actual_usd WHEN status IN ('reserved','uncertain') THEN reserved_usd ELSE 0.0 END), 0.0) AS usd
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
                "timezone": "UTC",
                "monthly_cap_usd": round(m_cap, 2),
                "monthly_settled_usd": round(settled, 4),
                "monthly_reserved_usd": round(reserved, 4),
                "monthly_committed_usd": committed,
                "monthly_remaining_usd": round(max(0.0, m_cap - committed), 2),
                "daily_call_cap": d_cap,
                "daily_calls": int(d_row["attempted"] or 0),
                "daily_billed_calls": int(d_row["billed"] or 0),
                "daily_committed_usd": round(float(d_row["usd"] or 0.0), 4),
                "month_ok": int(m_row["ok_cnt"] or 0),
                "month_rejected": int(m_row["rej_cnt"] or 0),
                "month_uncertain": int(m_row["unc_cnt"] or 0),
                "month_errors": int(m_row["err_cnt"] or 0),
                "provider_state": self.provider_state("anthropic", now_ts=ts),
            }
        finally:
            conn.close()
