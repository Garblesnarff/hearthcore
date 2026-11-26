"""Operator-visible alerting & health watchdog for Hearthcore (Issue #59).

Replaces silent log-only alerting with a unified, cooldown-deduplicated dispatcher:
- Always appends structured JSON entries to `runtime/failure_alerts.log` (rendered as a visible
  red banner on the Azeroth Watch dashboard).
- Always logs loudly to Python logging / systemd-journald.
- Optionally POSTs to `HEARTHCORE_ALERT_WEBHOOK_URL` (Discord/Slack/ntfy/custom webhook) when set.
- Optionally sends a desktop notification (`notify-send`) when `DBUS_SESSION_BUS_ADDRESS` is set.
- Deduplicates identical `(source, detail)` external notifications within `HEARTHCORE_ALERT_COOLDOWN_SEC`
  (default 900s) so persistent failures never spam webhooks or desktop popups.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import ROOT

logger = logging.getLogger("hearthcore.alerts")

DEFAULT_RUNTIME_DIR = Path(os.getenv("HEARTHCORE_RUNTIME_DIR") or str(ROOT / "runtime"))
DEFAULT_COOLDOWN_SEC = int(os.getenv("HEARTHCORE_ALERT_COOLDOWN_SEC", "900") or 900)


def _load_cooldown_state(path: Path) -> dict[str, int]:
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k): int(v) for k, v in raw.items()}
    except Exception as exc:
        logger.warning("Failed reading alert cooldown state %s: %s", path, exc)
    return {}


def _save_cooldown_state(path: Path, state: dict[str, int]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Failed persisting alert cooldown state %s: %s", path, exc)


def dispatch_alert(
    source: str,
    detail: str,
    *,
    severity: str = "warning",
    runtime_dir: Path | None = None,
    webhook_url: str | None = None,
    enable_desktop: bool = True,
    cooldown_sec: int | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Record and deliver an operator alert across log, journal, dashboard, webhook, and desktop."""
    rt = Path(runtime_dir) if runtime_dir is not None else DEFAULT_RUNTIME_DIR
    ts = int(now_ts if now_ts is not None else time.time())
    cd = int(cooldown_sec if cooldown_sec is not None else DEFAULT_COOLDOWN_SEC)
    sev = str(severity or "warning").lower()[:16]
    src = str(source)[:48]
    msg = str(detail)[:240]

    entry = {
        "ts": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "epoch": ts,
        "severity": sev,
        "source": src,
        "detail": msg,
    }

    log_ok = False
    try:
        rt.mkdir(parents=True, exist_ok=True)
        with open(rt / "failure_alerts.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        log_ok = True
    except OSError as exc:
        logger.warning("Failed writing failure_alerts.log: %s", exc)

    if sev in ("critical", "error"):
        logger.error("ALERT [%s] %s: %s", sev.upper(), src, msg)
    else:
        logger.warning("ALERT [%s] %s: %s", sev.upper(), src, msg)

    # Deduplicate external notifications (webhook / desktop) within cooldown window
    sig = hashlib.sha256(f"{src}|{msg}".encode("utf-8")).hexdigest()[:16]
    cd_path = rt / "alert_cooldown.json"
    cd_state = _load_cooldown_state(cd_path)
    last_sent = cd_state.get(sig, 0)
    suppressed = (cd > 0) and ((ts - last_sent) < cd)

    webhook_sent = False
    webhook_error = ""
    desktop_sent = False

    if not suppressed:
        url = (webhook_url if webhook_url is not None else os.getenv("HEARTHCORE_ALERT_WEBHOOK_URL", "")).strip()
        if url and url.startswith(("http://", "https://")):
            text = f"[Hearthcore {sev.upper()}] {src}: {msg}"
            payload = json.dumps({
                "content": text,
                "text": text,
                "severity": sev,
                "source": src,
                "detail": msg,
                "ts": entry["ts"],
            }).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "hearthcore-alerts/1"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    webhook_sent = 200 <= int(getattr(resp, "status", 200)) < 300
            except Exception as exc:
                webhook_error = f"{type(exc).__name__}: {exc}"
                logger.warning("Alert webhook POST failed: %s", webhook_error)

        if enable_desktop and os.environ.get("DBUS_SESSION_BUS_ADDRESS") and shutil.which("notify-send"):
            urgency = "critical" if sev in ("critical", "error") else "normal"
            try:
                subprocess.run(
                    ["notify-send", "-u", urgency, f"Hearthcore Alert ({src})", msg],
                    check=False,
                    timeout=3,
                    capture_output=True,
                )
                desktop_sent = True
            except Exception as exc:
                logger.debug("Desktop notify-send failed: %s", exc)

        cd_state[sig] = ts
        # Prune stale keys older than 7 days
        cd_state = {k: v for k, v in cd_state.items() if (ts - v) < 7 * 86400}
        _save_cooldown_state(cd_path, cd_state)

    return {
        "entry": entry,
        "log_ok": log_ok,
        "suppressed": suppressed,
        "webhook_sent": webhook_sent,
        "webhook_error": webhook_error,
        "desktop_sent": desktop_sent,
    }


def run_health_watch(
    *,
    brain_db_path: Path | None = None,
    runtime_dir: Path | None = None,
    dashboard_url: str | None = None,
    webhook_url: str | None = None,
    enable_desktop: bool = True,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Periodic watchdog checking brain staleness, DB health, and tick latency (Issue #59)."""
    from .bridge import BRAIN_DB_PATH, BrainStore, is_brain_disabled

    rt = Path(runtime_dir) if runtime_dir is not None else DEFAULT_RUNTIME_DIR
    dbp = Path(brain_db_path) if brain_db_path is not None else BRAIN_DB_PATH
    ts = int(now_ts if now_ts is not None else time.time())
    alerts_fired: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}

    # 1. Brain planning staleness check (only when brain is enabled and has historical plans)
    disabled = is_brain_disabled(rt)
    checks["brain_disabled"] = disabled
    if not disabled and dbp.is_file():
        st = BrainStore(dbp).summary(ts)
        last_ok = int(st.get("last_success_at") or 0)
        age_sec = max(0, ts - last_ok) if last_ok > 0 else None
        checks["stale_alarm"] = bool(st.get("stale_alarm"))
        checks["last_apply_age_sec"] = age_sec
        if st.get("stale_alarm"):
            res = dispatch_alert(
                "brain_stale",
                f"No successful companion plan applied in {age_sec}s (check hearthcore-brain.timer / DB / API budget)",
                severity="critical",
                runtime_dir=rt,
                webhook_url=webhook_url,
                enable_desktop=enable_desktop,
                now_ts=ts,
            )
            alerts_fired.append(res["entry"])

    # 2. Optional local Azeroth Watch dashboard telemetry check
    url = (dashboard_url if dashboard_url is not None else os.getenv("HEARTHCORE_DASHBOARD_STATE_URL", "")).strip()
    if url:
        try:
            with urllib.request.urlopen(url, timeout=4) as resp:
                dash = json.loads(resp.read().decode("utf-8"))
            db_ok = bool(dash.get("db_ok", True))
            age_sec = int(dash.get("data_age_sec") or 0)
            tick_p99 = (dash.get("ticks") or {}).get("p99")
            checks["dashboard"] = {"db_ok": db_ok, "data_age_sec": age_sec, "tick_p99": tick_p99}
            if not db_ok:
                res = dispatch_alert(
                    "dashboard_db_down",
                    "Azeroth Watch reports db_ok=False (MySQL connectivity issue)",
                    severity="critical",
                    runtime_dir=rt,
                    webhook_url=webhook_url,
                    enable_desktop=enable_desktop,
                    now_ts=ts,
                )
                alerts_fired.append(res["entry"])
            elif age_sec > 180:
                res = dispatch_alert(
                    "dashboard_stale",
                    f"Azeroth Watch telemetry stale ({age_sec}s without collector refresh)",
                    severity="warning",
                    runtime_dir=rt,
                    webhook_url=webhook_url,
                    enable_desktop=enable_desktop,
                    now_ts=ts,
                )
                alerts_fired.append(res["entry"])
            if tick_p99 is not None and float(tick_p99) > 80.0:
                res = dispatch_alert(
                    "worldserver_tick_high",
                    f"Worldserver tick p99 elevated at {float(tick_p99):.1f}ms",
                    severity="warning",
                    runtime_dir=rt,
                    webhook_url=webhook_url,
                    enable_desktop=enable_desktop,
                    now_ts=ts,
                )
                alerts_fired.append(res["entry"])
        except Exception as exc:
            checks["dashboard_error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("Health-watch dashboard probe failed (%s): %s", url, exc)

    return {
        "ok": len(alerts_fired) == 0,
        "checked_at": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alerts_fired": alerts_fired,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=getattr(logging, os.getenv("HEARTHCORE_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser(description="Hearthcore operator alerting & health watchdog")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_send = sub.add_parser("send", help="Dispatch an alert immediately")
    p_send.add_argument("--source", required=True)
    p_send.add_argument("--detail", required=True)
    p_send.add_argument("--severity", default="warning")

    p_chk = sub.add_parser("check", help="Run periodic health watchdog checks")
    p_chk.add_argument("--dashboard-url", default="http://127.0.0.1:8787/api/state")

    args = ap.parse_args(argv)
    if args.cmd == "send":
        res = dispatch_alert(args.source, args.detail, severity=args.severity)
        print(json.dumps(res))
        return 0
    if args.cmd == "check":
        res = run_health_watch(dashboard_url=args.dashboard_url)
        print(json.dumps(res))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
