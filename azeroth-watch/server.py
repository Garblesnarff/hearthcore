#!/usr/bin/env python3
"""Azeroth Watch — hardened HTTP + SSE server with authenticated admin & read-view protection.

Security & robustness model (Issues #9, #71 & #72):
- Default CLI bind is 127.0.0.1; LAN family dashboard explicitly binds 0.0.0.0 in systemd while
  validating Host headers on EVERY route (DNS-rebinding protection) and setting a strict CSP.
- Handler socket timeout (15s), bounded SSE client pool (MAX_SSE_CLIENTS=16 -> 503 when full),
  and cached JSON serialization per (snapshot_version, authenticated) pair.
- Constant-time UTF-8 safe token & cookie checks (never raise TypeError on non-ASCII), with
  per-IP rate limiting across ALL auth failures (header, cookie, and login endpoint).
- Server-side session store with short 12h TTL and real server-side revocation on logout.
- Dashboard NEVER rewrites `.env` (which holds ANTHROPIC_API_KEY); mutable settings live in a
  separate `runtime/dashboard_overrides.env` file created atomically with `os.open(..., 0o600)`.
- Admin actions signal the collector thread (`request_refresh()`) instead of blocking the HTTP thread.
"""

from __future__ import annotations

import argparse
import hmac
import http.cookies
import http.server
import json
import mimetypes
import os
import secrets
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from collector import AzerothCollector

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")


def _default_hearthcore_root() -> Path:
    env_root = os.getenv("HEARTHCORE_ROOT")
    if env_root:
        return Path(env_root)
    parent = Path(BASE_DIR).parent
    if (parent / "hearthcore").is_dir():
        return parent
    return Path.home() / "hearthcore"


HEARTHCORE_ENV = os.getenv("HEARTHCORE_ENV_PATH", str(_default_hearthcore_root() / ".env"))
DASHBOARD_OVERRIDES_ENV = os.getenv(
    "HEARTHCORE_DASHBOARD_OVERRIDES_PATH",
    str(_default_hearthcore_root() / "runtime" / "dashboard_overrides.env"),
)
AUDIT_LOG_PATH = os.getenv("AZEROTH_WATCH_AUDIT_LOG", str(_default_hearthcore_root() / "runtime" / "admin_audit.log"))
ALLOWED_MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5"}
MAX_POST_BYTES = 4096
MAX_SSE_CLIENTS = int(os.getenv("AZEROTH_WATCH_MAX_SSE_CLIENTS", "16") or 16)
HANDLER_TIMEOUT_SEC = float(os.getenv("AZEROTH_WATCH_SOCKET_TIMEOUT", "15.0") or 15.0)
SESSION_COOKIE_NAME = "aw_admin_session"
SESSION_TTL_SEC = 12 * 3600  # 12h short server-side revocable session (Issue #72)
CSP_HEADER = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'self'"
)

_CTL_LOCK = threading.Lock()
_LAST_RUN_TS: dict[str, float] = {}
_AUTH_FAILS: dict[str, list[float]] = {}

# Explicit read-only kiosk allow-list (Issues #71 & #72)
PUBLIC_TOP_KEYS = (
    "generated_at",
    "clock",
    "hour",
    "minute",
    "db_ok",
    "data_age_sec",
    "realm",
    "vitals",
    "ticks",
    "errors_bytes",
    "slow_warnings",
    "population",
    "humans",
    "raid",
    "companions",
    "professions",
    "bank",
    "ledger",
    "events",
    "work_orders",
    "relationships",
    "shifts",
    "world_zones",
    "map_slides",
    "weekly_event",
)
PUBLIC_HUMAN_KEYS = (
    "guid",
    "name",
    "level",
    "class_id",
    "class_name",
    "class_color",
    "race_id",
    "gender",
    "race",
    "zone_id",
    "zone",
    "map_id",
    "x",
    "y",
    "xp",
    "xp_max",
    "xp_pct",
)


def _safe_digest_eq(a: str, b: str) -> bool:
    """UTF-8 constant-time comparison that never raises TypeError on non-ASCII input (Issue #72)."""
    try:
        return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))
    except Exception:
        return False


def _discover_default_allowed_hosts() -> set[str]:
    hosts = {"127.0.0.1", "localhost", "[::1]", "::1"}
    try:
        hn = socket.gethostname().strip().lower()
        if hn:
            hosts.add(hn)
            hosts.add(f"{hn}.local")
    except OSError:
        pass
    # Discover ALL local IPv4 interface addresses cleanly (never assumes 192.168.1.1 gateway!)
    try:
        res = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.split()
                if "inet" in parts:
                    idx = parts.index("inet")
                    if idx + 1 < len(parts):
                        ip = parts[idx + 1].split("/", 1)[0].strip()
                        if ip:
                            hosts.add(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip:
                hosts.add(ip)
        finally:
            s.close()
    except OSError:
        pass
    extra = os.getenv("AZEROTH_WATCH_ALLOWED_HOSTS", "")
    for item in extra.split(","):
        tok = item.strip().lower()
        if tok:
            hosts.add(tok)
    return hosts


def _strip_host_port(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    if not h:
        return ""
    if h.startswith("["):
        end = h.find("]")
        return h[: end + 1] if end != -1 else h
    if ":" in h:
        return h.split(":", 1)[0]
    return h


def _host_allowed(host_header: str, allowed_hosts: set[str]) -> bool:
    host_only = _strip_host_port(host_header)
    if not host_only:
        return False
    return host_only in allowed_hosts


def project_snapshot(snap: dict | None, *, authenticated: bool) -> dict:
    """Apply explicit field allow-list for public kiosk vs authenticated admin view (Issue #71)."""
    if not isinstance(snap, dict):
        return {}
    out: dict = {}
    for k in PUBLIC_TOP_KEYS:
        if k not in snap:
            continue
        if k == "humans" and isinstance(snap[k], list):
            out["humans"] = [
                {hk: h[hk] for hk in PUBLIC_HUMAN_KEYS if hk in h}
                for h in snap[k]
                if isinstance(h, dict)
            ]
        else:
            out[k] = snap[k]

    if authenticated:
        if "ai_brain" in snap:
            out["ai_brain"] = snap["ai_brain"]
        out["view_mode"] = "admin"
    else:
        ab = snap.get("ai_brain") if isinstance(snap.get("ai_brain"), dict) else {}
        active_list = ab.get("active_list") if isinstance(ab.get("active_list"), list) else []
        alerts = ab.get("failure_alerts") if isinstance(ab.get("failure_alerts"), list) else []
        recent_cutoff = int(time.time()) - 7200
        recent_alerts = [
            a for a in alerts
            if isinstance(a, dict) and (int(a.get("epoch") or 0) >= recent_cutoff or not a.get("epoch"))
        ]
        latest = recent_alerts[-1] if recent_alerts else None
        out["ai_brain"] = {
            "auth_required": True,
            "redacted": True,
            "active_plans_count": sum(1 for item in active_list if isinstance(item, dict) and item.get("active")),
            "stale_alarm": bool(ab.get("stale_alarm")),
            "recent_alert_count": len(recent_alerts),
            "latest_alert": {
                "ts": str(latest.get("ts") or ""),
                "source": str(latest.get("source") or ""),
                "severity": str(latest.get("severity") or "warning"),
            } if latest else None,
        }
        out["view_mode"] = "kiosk"
    return out


def _read_env_value(key: str, env_path: str = HEARTHCORE_ENV) -> str:
    if os.environ.get(key):
        return str(os.environ[key]).strip()
    if not os.path.isfile(env_path):
        return ""
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw in f:
                ln = raw.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                ek, ev = ln.split("=", 1)
                if ek.strip() == key:
                    return ev.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def _atomic_write_0600(target_path: str, content: str) -> None:
    """Atomically write file created directly with mode 0600 (zero umask window, Issue #72)."""
    parent = os.path.dirname(target_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{target_path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
        os.replace(tmp, target_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _update_env_file(key: str, value: str, target_path: str) -> None:
    """Safely update one key in a dedicated 0600 settings file (never rewrites .env API keys, Issue #72)."""
    with _CTL_LOCK:
        lines = []
        if os.path.isfile(target_path):
            with open(target_path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        out = []
        found = False
        for ln in lines:
            if ln.strip().startswith(f"{key}="):
                out.append(f"{key}={value}")
                found = True
            else:
                out.append(ln)
        if not found:
            out.append(f"{key}={value}")
        _atomic_write_0600(target_path, "\n".join(out) + "\n")


def ensure_admin_token(env_path: str = HEARTHCORE_ENV, overrides_path: str = DASHBOARD_OVERRIDES_ENV) -> str:
    """Return configured admin token; if absent, generate into dedicated 0600 overrides file."""
    for p in (env_path, overrides_path):
        tok = _read_env_value("AZEROTH_WATCH_ADMIN_TOKEN", env_path=p)
        if tok and len(tok) >= 16:
            return tok
    new_tok = secrets.token_urlsafe(24)
    _update_env_file("AZEROTH_WATCH_ADMIN_TOKEN", new_tok, overrides_path)
    return new_tok


def _same_origin_ok(headers, host_header: str) -> bool:
    """Reject cross-origin POST requests using Origin / Referer comparison against Host."""
    expected_host = (host_header or "").strip().lower()
    if not expected_host:
        return False
    for hdr_name in ("Origin", "Referer"):
        val = (headers.get(hdr_name) or "").strip()
        if not val:
            continue
        if val.lower() == "null":
            return False
        parsed = urllib.parse.urlparse(val)
        req_host = (parsed.netloc or "").strip().lower()
        if not req_host or req_host != expected_host:
            return False
    return True


def _audit_log(ip: str, path: str, status: int, detail: str, audit_path: str = AUDIT_LOG_PATH) -> None:
    """Append a non-secret structured audit line for every administrative action."""
    try:
        parent = os.path.dirname(audit_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ip": ip,
            "path": path,
            "status": int(status),
            "detail": detail[:160],
        }
        with _CTL_LOCK:
            with open(audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(
    collector: AzerothCollector,
    *,
    admin_token: str | None = None,
    env_path: str = HEARTHCORE_ENV,
    overrides_path: str | None = None,
    audit_path: str = AUDIT_LOG_PATH,
    systemctl_runner=None,
    allowed_hosts: set[str] | None = None,
    max_sse_clients: int = MAX_SSE_CLIENTS,
    session_ttl_sec: int = SESSION_TTL_SEC,
):
    # Separate mutable settings file so dashboard NEVER rewrites .env holding ANTHROPIC_API_KEY (Issue #72).
    settings_path = overrides_path if overrides_path is not None else (
        env_path if env_path != HEARTHCORE_ENV else DASHBOARD_OVERRIDES_ENV
    )
    token_secret = admin_token if admin_token is not None else ensure_admin_token(env_path, settings_path)
    host_allowlist = set(allowed_hosts) if allowed_hosts is not None else _discover_default_allowed_hosts()

    # Per-server-instance server-side session store (revocable!), SSE counter, and serialization cache
    state_lock = threading.Lock()
    sessions: dict[str, float] = {}  # session_id -> expires_at_epoch
    sse_active = {"count": 0}
    ser_cache: dict[tuple[int, bool, int], bytes] = {}
    auth_fails: dict[str, list[float]] = {}

    def _is_rate_limited(ip: str) -> bool:
        now_m = time.monotonic()
        with state_lock:
            fails = [t for t in auth_fails.get(ip, []) if now_m - t < 60.0]
            auth_fails[ip] = fails
            return len(fails) >= 5

    def _record_auth_fail(ip: str) -> None:
        now_m = time.monotonic()
        with state_lock:
            fails = [t for t in auth_fails.get(ip, []) if now_m - t < 60.0]
            fails.append(now_m)
            auth_fails[ip] = fails

    def _issue_session() -> str:
        sid = secrets.token_urlsafe(32)
        exp = time.time() + float(session_ttl_sec)
        with state_lock:
            now_t = time.time()
            # Prune expired sessions opportunistically
            for k in [k for k, v in sessions.items() if v <= now_t]:
                sessions.pop(k, None)
            sessions[sid] = exp
        return sid

    def _validate_session(sid: str) -> bool:
        if not sid or len(sid) < 16:
            return False
        now_t = time.time()
        with state_lock:
            exp = sessions.get(sid)
            if exp is None:
                return False
            if exp <= now_t:
                sessions.pop(sid, None)
                return False
            return True

    def _revoke_session(sid: str) -> None:
        if not sid:
            return
        with state_lock:
            sessions.pop(sid, None)

    def _cached_serialized_state(ver: int, snap: dict | None, authenticated: bool) -> bytes:
        # Bucket live data_age_sec every 5s so stale DB age NEVER freezes in cache while healthy traffic hits 100%!
        age_sec = int((snap or {}).get("data_age_sec") or 0) if isinstance(snap, dict) else 0
        age_bucket = age_sec // 5
        key = (int(ver), bool(authenticated), age_bucket)
        with state_lock:
            hit = ser_cache.get(key)
            if hit is not None:
                return hit
        payload = json.dumps(project_snapshot(snap, authenticated=authenticated), separators=(",", ":")).encode("utf-8")
        with state_lock:
            if len(ser_cache) > 12:
                ser_cache.clear()
            ser_cache[key] = payload
        return payload

    def _trigger_collector_refresh() -> None:
        req_fn = getattr(collector, "request_refresh", None)
        if callable(req_fn):
            req_fn()
        else:
            collector.refresh_once()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            try:
                self.request.settimeout(HANDLER_TIMEOUT_SEC)
            except OSError:
                pass

        def log_message(self, fmt, *args):
            return

        def _client_ip(self) -> str:
            return str(self.client_address[0]) if self.client_address else "unknown"

        def _extract_session_cookie(self) -> str:
            raw_cookie = self.headers.get("Cookie") or ""
            if not raw_cookie:
                return ""
            try:
                c = http.cookies.SimpleCookie()
                c.load(raw_cookie)
                morsel = c.get(SESSION_COOKIE_NAME)
                return str(morsel.value) if morsel else ""
            except Exception:
                return ""

        def _send_bytes(
            self,
            status: int,
            content_type: str,
            body: bytes,
            cache: str = "no-store",
            extra_headers: list[tuple[str, str]] | None = None,
            close_conn: bool = False,
        ):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Content-Security-Policy", CSP_HEADER)
            if close_conn:
                self.send_header("Connection", "close")
                self.close_connection = True
            for hk, hv in (extra_headers or []):
                self.send_header(hk, hv)
            self.end_headers()
            self.wfile.write(body)

        def _check_host_header(self) -> bool:
            if not _host_allowed(self.headers.get("Host") or "", host_allowlist):
                self._send_bytes(
                    403,
                    "application/json; charset=utf-8",
                    b'{"ok":false,"error":"Host not allowed"}',
                    close_conn=True,
                )
                return False
            return True

        def _is_authenticated(self, *, count_failure: bool = False) -> tuple[bool, bool]:
            """Return (authenticated, rate_limited). Never raises on non-ASCII or malformed inputs."""
            ip = self._client_ip()
            if _is_rate_limited(ip):
                return False, True
            if not token_secret:
                return False, False
            hdr_tok = (self.headers.get("X-Azeroth-Admin-Token") or "").strip()
            if hdr_tok:
                if _safe_digest_eq(hdr_tok, token_secret):
                    return True, False
                if count_failure:
                    _record_auth_fail(ip)
                return False, False
            sid = self._extract_session_cookie()
            if sid:
                if _validate_session(sid):
                    return True, False
                if count_failure:
                    _record_auth_fail(ip)
                return False, False
            return False, False

        def do_GET(self):
            if not self._check_host_header():
                return
            path = self.path.split("?", 1)[0]

            if path == "/api/health":
                body = json.dumps({"ok": True}).encode("utf-8")
                self._send_bytes(200, "application/json; charset=utf-8", body)
                return

            if path == "/api/brain/auth-status":
                is_auth, rate_lim = self._is_authenticated(count_failure=True)
                if rate_lim:
                    self._send_bytes(429, "application/json; charset=utf-8", b'{"ok":false,"error":"Too many failed attempts; wait 60s"}', close_conn=True)
                    return
                body = json.dumps({"ok": True, "authenticated": is_auth}).encode("utf-8")
                self._send_bytes(200, "application/json; charset=utf-8", body)
                return

            if path == "/api/state":
                is_auth, rate_lim = self._is_authenticated(count_failure=True)
                if rate_lim:
                    self._send_bytes(429, "application/json; charset=utf-8", b'{"ok":false,"error":"Too many failed attempts; wait 60s"}', close_conn=True)
                    return
                ver, snap = collector.get_snapshot()
                body = _cached_serialized_state(ver, snap, is_auth)
                self._send_bytes(200, "application/json; charset=utf-8", body)
                return

            if path == "/api/stream":
                is_auth, rate_lim = self._is_authenticated(count_failure=True)
                if rate_lim:
                    self._send_bytes(429, "application/json; charset=utf-8", b'{"ok":false,"error":"Too many failed attempts; wait 60s"}', close_conn=True)
                    return
                with state_lock:
                    if sse_active["count"] >= max(1, int(max_sse_clients)):
                        self._send_bytes(
                            503,
                            "application/json; charset=utf-8",
                            b'{"ok":false,"error":"Too many active SSE clients"}',
                            close_conn=True,
                        )
                        return
                    sse_active["count"] += 1
                try:
                    stream_sid = self._extract_session_cookie()
                    stream_used_cookie = bool(is_auth and stream_sid and not (self.headers.get("X-Azeroth-Admin-Token") or "").strip())
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Security-Policy", CSP_HEADER)
                    self.end_headers()
                    last_key = None
                    while True:
                        # Re-check session revocation on active SSE streams so logout immediately stops admin view!
                        if stream_used_cookie and not _validate_session(stream_sid):
                            is_auth = False
                            stream_used_cookie = False
                        ver, snap = collector.get_snapshot()
                        age_bucket = int((snap or {}).get("data_age_sec") or 0) // 5 if isinstance(snap, dict) else 0
                        cur_key = (ver, is_auth, age_bucket)
                        if cur_key != last_key and snap:
                            payload_b = _cached_serialized_state(ver, snap, is_auth)
                            msg = f"id: {ver}\nevent: state\ndata: ".encode("utf-8") + payload_b + b"\n\n"
                            self.wfile.write(msg)
                            self.wfile.flush()
                            last_key = cur_key
                        else:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                        time.sleep(2.5)
                except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                    return
                finally:
                    with state_lock:
                        sse_active["count"] = max(0, sse_active["count"] - 1)

            if path == "/" or path == "/index.html":
                self._serve_static("index.html")
                return

            if path.startswith("/static/"):
                rel = os.path.normpath(path[len("/static/"):]).lstrip("/")
                if ".." not in rel:
                    self._serve_static(rel)
                    return

            self._send_bytes(404, "text/plain; charset=utf-8", b"Not found")

        def do_POST(self):
            if not self._check_host_header():
                return
            path = self.path.split("?", 1)[0]
            ip = self._client_ip()

            # 1. Cross-Origin / CSRF defense
            if not _same_origin_ok(self.headers, self.headers.get("Host") or ""):
                _audit_log(ip, path, 403, "cross_origin_denied", audit_path=audit_path)
                self._send_bytes(403, "application/json; charset=utf-8", b'{"ok":false,"error":"Cross-origin request denied"}', close_conn=True)
                return

            # 2. Require application/json to prevent simple form CSRF
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                _audit_log(ip, path, 415, "invalid_content_type", audit_path=audit_path)
                self._send_bytes(415, "application/json; charset=utf-8", b'{"ok":false,"error":"Content-Type must be application/json"}', close_conn=True)
                return

            # 3. Bound request size strictly (Connection: close on rejection so body never poisons keep-alive)
            try:
                raw_clen = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                self._send_bytes(400, "application/json; charset=utf-8", b'{"ok":false,"error":"Invalid Content-Length"}', close_conn=True)
                return
            if raw_clen < 0 or raw_clen > MAX_POST_BYTES:
                _audit_log(ip, path, 413, f"payload_too_large:{raw_clen}", audit_path=audit_path)
                self._send_bytes(413, "application/json; charset=utf-8", b'{"ok":false,"error":"Request body too large"}', close_conn=True)
                return

            try:
                raw = self.rfile.read(raw_clen) if raw_clen > 0 else b"{}"
                body = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
            except Exception:
                self._send_bytes(400, "application/json; charset=utf-8", b'{"ok":false,"error":"Invalid JSON"}', close_conn=True)
                return

            # Auth login endpoint (rate-limited + server-side revocable session store)
            if path == "/api/brain/auth":
                if _is_rate_limited(ip):
                    _audit_log(ip, path, 429, "auth_rate_limited", audit_path=audit_path)
                    self._send_bytes(429, "application/json; charset=utf-8", b'{"ok":false,"error":"Too many failed attempts; wait 60s"}', close_conn=True)
                    return
                cand = str(body.get("token") or "").strip()
                if not cand or not token_secret or not _safe_digest_eq(cand, token_secret):
                    _record_auth_fail(ip)
                    _audit_log(ip, path, 401, "auth_failed", audit_path=audit_path)
                    self._send_bytes(401, "application/json; charset=utf-8", b'{"ok":false,"error":"Invalid admin token"}', close_conn=True)
                    return
                sid = _issue_session()
                cookie_hdr = f"{SESSION_COOKIE_NAME}={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={int(session_ttl_sec)}"
                _audit_log(ip, path, 200, "auth_ok", audit_path=audit_path)
                self._send_bytes(
                    200,
                    "application/json; charset=utf-8",
                    b'{"ok":true,"authenticated":true}',
                    extra_headers=[("Set-Cookie", cookie_hdr)],
                )
                return

            if path == "/api/brain/logout":
                sid = self._extract_session_cookie()
                _revoke_session(sid)
                cookie_hdr = f"{SESSION_COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
                _audit_log(ip, path, 200, "logout", audit_path=audit_path)
                self._send_bytes(
                    200,
                    "application/json; charset=utf-8",
                    b'{"ok":true,"authenticated":false}',
                    extra_headers=[("Set-Cookie", cookie_hdr)],
                )
                return

            # 4. Require explicit authorization for ALL state-changing controls (rate-limited!)
            is_auth, rate_lim = self._is_authenticated(count_failure=True)
            if rate_lim:
                _audit_log(ip, path, 429, "auth_rate_limited", audit_path=audit_path)
                self._send_bytes(429, "application/json; charset=utf-8", b'{"ok":false,"error":"Too many failed attempts; wait 60s"}', close_conn=True)
                return
            if not is_auth:
                _audit_log(ip, path, 401, "unauthorized", audit_path=audit_path)
                self._send_bytes(401, "application/json; charset=utf-8", b'{"ok":false,"error":"Admin authorization required"}', close_conn=True)
                return

            if path == "/api/brain/model":
                model = str(body.get("model") or "").strip()
                if model not in ALLOWED_MODELS:
                    self._send_bytes(400, "application/json; charset=utf-8", b'{"ok":false,"error":"Unsupported model"}')
                    return
                _update_env_file("ANTHROPIC_MODEL", model, settings_path)
                _trigger_collector_refresh()
                _audit_log(ip, path, 200, f"model={model}", audit_path=audit_path)
                resp = json.dumps({"ok": True, "model": model}).encode("utf-8")
                self._send_bytes(200, "application/json; charset=utf-8", resp)
                return

            if path == "/api/brain/credit":
                try:
                    rem = float(body.get("remaining_usd"))
                    if not (0.0 <= rem <= 10000.0):
                        raise ValueError("out of range")
                except Exception:
                    self._send_bytes(400, "application/json; charset=utf-8", b'{"ok":false,"error":"Invalid dollar amount"}')
                    return
                _, snap = collector.get_snapshot()
                spent_so_far = float(((snap or {}).get("ai_brain") or {}).get("balance", {}).get("spent_total_usd", 0.0))
                new_baseline = round(rem + spent_so_far, 2)
                _update_env_file("HEARTHCORE_CREDIT_USD", f"{new_baseline:.2f}", settings_path)
                _trigger_collector_refresh()
                _audit_log(ip, path, 200, f"credit_remaining={rem:.2f}", audit_path=audit_path)
                resp = json.dumps({"ok": True, "remaining_usd": round(rem, 2)}).encode("utf-8")
                self._send_bytes(200, "application/json; charset=utf-8", resp)
                return

            if path == "/api/brain/run":
                target = str(body.get("target") or "").strip()
                unit_map = {
                    "brain": "hearthcore-brain.service",
                    "quartermaster": "hearthcore-quartermaster.service",
                }
                unit = unit_map.get(target)
                if not unit:
                    self._send_bytes(400, "application/json; charset=utf-8", b'{"ok":false,"error":"Unknown target"}')
                    return
                now = time.monotonic()
                with _CTL_LOCK:
                    if now - _LAST_RUN_TS.get(target, 0.0) < 15.0:
                        _audit_log(ip, path, 429, f"rate_limited:{target}", audit_path=audit_path)
                        self._send_bytes(429, "application/json; charset=utf-8", b'{"ok":false,"error":"Please wait 15s between manual runs"}')
                        return
                    _LAST_RUN_TS[target] = now
                if systemctl_runner is not None:
                    systemctl_runner(unit)
                else:
                    subprocess.Popen(
                        ["systemctl", "--user", "start", "--no-block", unit],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                _audit_log(ip, path, 200, f"started={unit}", audit_path=audit_path)
                resp = json.dumps({"ok": True, "started": unit}).encode("utf-8")
                self._send_bytes(200, "application/json; charset=utf-8", resp)
                return

            self._send_bytes(404, "application/json; charset=utf-8", b'{"ok":false,"error":"Not found"}')

        def _serve_static(self, rel_path: str):
            full = os.path.join(STATIC_DIR, rel_path)
            if not os.path.isfile(full):
                self._send_bytes(404, "text/plain; charset=utf-8", b"Not found")
                return
            ctype, _ = mimetypes.guess_type(full)
            if not ctype:
                ctype = "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as f:
                data = f.read()
            if rel_path.startswith("icons/") or rel_path.startswith("maps/"):
                cache_hdr = "public, max-age=86400, immutable"
            else:
                cache_hdr = "no-cache"
            self._send_bytes(200, ctype, data, cache=cache_hdr)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Azeroth Watch server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1; use 0.0.0.0 for LAN read-only dashboard)")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--interval", type=float, default=4.0)
    args = parser.parse_args()

    ensure_admin_token()
    collector = AzerothCollector(poll_interval_s=args.interval)
    collector.start()

    srv = ThreadingHTTPServer((args.host, args.port), make_handler(collector))

    def shutdown(*_args):
        collector.stop()
        threading_shutdown = getattr(srv, "shutdown", None)
        if threading_shutdown:
            srv.server_close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"Azeroth Watch listening on http://{args.host}:{args.port} (hardened Host/CSP/SSE/session auth enabled)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
        srv.server_close()


if __name__ == "__main__":
    main()
