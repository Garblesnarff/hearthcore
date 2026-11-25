#!/usr/bin/env python3
"""Azeroth Watch — lightweight HTTP + SSE server with authenticated admin & read-view protection.

Security model (Issues #9 & #71):
- Default CLI bind is 127.0.0.1 (safe local default); LAN family dashboard explicitly binds 0.0.0.0
  in systemd while strictly validating Host headers on EVERY route (DNS-rebinding protection).
- Read-only unauthenticated kiosk view (/api/state, /api/stream) uses an explicit field allow-list:
  - Never exposes login account names, raw exception strings, API spend/credit balance, or raw
    runtime planner/admin JSON.
  - Full AI Brain & budget telemetry is served ONLY to authenticated admin sessions (via
    X-Azeroth-Admin-Token header or HttpOnly SameSite=Strict HMAC session cookie).
- ALL administrative POST endpoints (/api/brain/model, /api/brain/credit, /api/brain/run) require:
  1. Host-header allow-list + Same-Origin / CSRF check (403 on mismatch).
  2. Strict Content-Length <= 4096 bytes and application/json Content-Type.
  3. Explicit authorization via X-Azeroth-Admin-Token or HttpOnly SameSite=Strict session cookie.
  4. Rate limiting (15s run cooldown + 5/min auth brute-force protection).
  5. Audit logging to runtime/admin_audit.log (zero secrets logged or returned).
"""

from __future__ import annotations

import argparse
import hashlib
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
AUDIT_LOG_PATH = os.getenv("AZEROTH_WATCH_AUDIT_LOG", str(_default_hearthcore_root() / "runtime" / "admin_audit.log"))
ALLOWED_MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5"}
MAX_POST_BYTES = 4096
SESSION_COOKIE_NAME = "aw_admin_session"
SESSION_TTL_SEC = 30 * 86400

_CTL_LOCK = threading.Lock()
_LAST_RUN_TS: dict[str, float] = {}
_AUTH_FAILS: dict[str, list[float]] = {}

# Explicit read-only kiosk allow-list (Issue #71)
PUBLIC_TOP_KEYS = (
    "generated_at",
    "clock",
    "hour",
    "minute",
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


def _discover_default_allowed_hosts() -> set[str]:
    hosts = {"127.0.0.1", "localhost", "[::1]", "::1"}
    try:
        hn = socket.gethostname().strip().lower()
        if hn:
            hosts.add(hn)
            hosts.add(f"{hn}.local")
    except OSError:
        pass
    try:
        # Discover primary LAN IP without sending packets
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.168.1.1", 80))
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
        # IPv6 literal [::1]:8787
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
        # Authenticated admin session receives full AI Brain & budget dashboard data (minus any raw account field)
        if "ai_brain" in snap:
            out["ai_brain"] = snap["ai_brain"]
        out["view_mode"] = "admin"
    else:
        ab = snap.get("ai_brain") if isinstance(snap.get("ai_brain"), dict) else {}
        active_list = ab.get("active_list") if isinstance(ab.get("active_list"), list) else []
        out["ai_brain"] = {
            "auth_required": True,
            "redacted": True,
            "active_plans_count": sum(1 for item in active_list if isinstance(item, dict) and item.get("active")),
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


def _update_hearthcore_env(key: str, value: str, env_path: str = HEARTHCORE_ENV) -> None:
    """Safely update one key in hearthcore/.env (preserves chmod 600, never echoes secrets)."""
    with _CTL_LOCK:
        parent = os.path.dirname(env_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        lines = []
        if os.path.isfile(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
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
        tmp = env_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)


def ensure_admin_token(env_path: str = HEARTHCORE_ENV) -> str:
    """Return configured admin token, generating and saving one (chmod 600) if absent."""
    tok = _read_env_value("AZEROTH_WATCH_ADMIN_TOKEN", env_path=env_path)
    if tok and len(tok) >= 16:
        return tok
    new_tok = secrets.token_urlsafe(24)
    _update_hearthcore_env("AZEROTH_WATCH_ADMIN_TOKEN", new_tok, env_path=env_path)
    return new_tok


def _sign_session(token: str, issued_at: int) -> str:
    msg = f"aw-admin:{issued_at}".encode("utf-8")
    sig = hmac.new(token.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"{issued_at}.{sig}"


def _verify_session(cookie_val: str, token: str, now_ts: int | None = None) -> bool:
    if not cookie_val or "." not in cookie_val or not token:
        return False
    parts = cookie_val.split(".", 1)
    if not parts[0].isdigit():
        return False
    issued_at = int(parts[0])
    now = int(now_ts if now_ts is not None else time.time())
    if issued_at > now + 60 or (now - issued_at) > SESSION_TTL_SEC:
        return False
    expected = _sign_session(token, issued_at)
    return hmac.compare_digest(cookie_val, expected)


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
    audit_path: str = AUDIT_LOG_PATH,
    systemctl_runner=None,
    allowed_hosts: set[str] | None = None,
):
    token_secret = admin_token if admin_token is not None else ensure_admin_token(env_path)
    host_allowlist = set(allowed_hosts) if allowed_hosts is not None else _discover_default_allowed_hosts()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def _client_ip(self) -> str:
            return str(self.client_address[0]) if self.client_address else "unknown"

        def _send_bytes(
            self,
            status: int,
            content_type: str,
            body: bytes,
            cache: str = "no-store",
            extra_headers: list[tuple[str, str]] | None = None,
        ):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            for hk, hv in (extra_headers or []):
                self.send_header(hk, hv)
            self.end_headers()
            self.wfile.write(body)

        def _check_host_header(self) -> bool:
            if not _host_allowed(self.headers.get("Host") or "", host_allowlist):
                self._send_bytes(403, "application/json; charset=utf-8", b'{"ok":false,"error":"Host not allowed"}')
                return False
            return True

        def _is_authenticated(self) -> bool:
            if not token_secret:
                return False
            hdr_tok = (self.headers.get("X-Azeroth-Admin-Token") or "").strip()
            if hdr_tok and hmac.compare_digest(hdr_tok, token_secret):
                return True
            raw_cookie = self.headers.get("Cookie") or ""
            if raw_cookie:
                c = http.cookies.SimpleCookie()
                try:
                    c.load(raw_cookie)
                except Exception:
                    return False
                morsel = c.get(SESSION_COOKIE_NAME)
                if morsel and _verify_session(morsel.value, token_secret):
                    return True
            return False

        def do_GET(self):
            if not self._check_host_header():
                return
            path = self.path.split("?", 1)[0]

            if path == "/api/health":
                body = json.dumps({"ok": True}).encode("utf-8")
                self._send_bytes(200, "application/json; charset=utf-8", body)
                return

            if path == "/api/brain/auth-status":
                body = json.dumps({"ok": True, "authenticated": self._is_authenticated()}).encode("utf-8")
                self._send_bytes(200, "application/json; charset=utf-8", body)
                return

            if path == "/api/state":
                _, snap = collector.get_snapshot()
                view = project_snapshot(snap, authenticated=self._is_authenticated())
                body = json.dumps(view, separators=(",", ":")).encode("utf-8")
                self._send_bytes(200, "application/json; charset=utf-8", body)
                return

            if path == "/api/stream":
                is_auth = self._is_authenticated()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                last_ver = -1
                try:
                    while True:
                        ver, snap = collector.get_snapshot()
                        if ver != last_ver and snap:
                            view = project_snapshot(snap, authenticated=is_auth)
                            payload = json.dumps(view, separators=(",", ":"))
                            msg = f"id: {ver}\nevent: state\ndata: {payload}\n\n".encode("utf-8")
                            self.wfile.write(msg)
                            self.wfile.flush()
                            last_ver = ver
                        else:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                        time.sleep(2.5)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

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
                self._send_bytes(403, "application/json; charset=utf-8", b'{"ok":false,"error":"Cross-origin request denied"}')
                return

            # 2. Require application/json to prevent simple form CSRF
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                _audit_log(ip, path, 415, "invalid_content_type", audit_path=audit_path)
                self._send_bytes(415, "application/json; charset=utf-8", b'{"ok":false,"error":"Content-Type must be application/json"}')
                return

            # 3. Bound request size strictly
            try:
                raw_clen = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                self._send_bytes(400, "application/json; charset=utf-8", b'{"ok":false,"error":"Invalid Content-Length"}')
                return
            if raw_clen < 0 or raw_clen > MAX_POST_BYTES:
                _audit_log(ip, path, 413, f"payload_too_large:{raw_clen}", audit_path=audit_path)
                self._send_bytes(413, "application/json; charset=utf-8", b'{"ok":false,"error":"Request body too large"}')
                return

            try:
                raw = self.rfile.read(raw_clen) if raw_clen > 0 else b"{}"
                body = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
            except Exception:
                self._send_bytes(400, "application/json; charset=utf-8", b'{"ok":false,"error":"Invalid JSON"}')
                return

            # Auth login endpoint (rate-limited against brute force)
            if path == "/api/brain/auth":
                now_m = time.monotonic()
                with _CTL_LOCK:
                    recent_fails = [t for t in _AUTH_FAILS.get(ip, []) if now_m - t < 60.0]
                    _AUTH_FAILS[ip] = recent_fails
                    if len(recent_fails) >= 5:
                        _audit_log(ip, path, 429, "auth_rate_limited", audit_path=audit_path)
                        self._send_bytes(429, "application/json; charset=utf-8", b'{"ok":false,"error":"Too many failed attempts; wait 60s"}')
                        return
                cand = str(body.get("token") or "").strip()
                if not cand or not token_secret or not hmac.compare_digest(cand, token_secret):
                    with _CTL_LOCK:
                        _AUTH_FAILS.setdefault(ip, []).append(now_m)
                    _audit_log(ip, path, 401, "auth_failed", audit_path=audit_path)
                    self._send_bytes(401, "application/json; charset=utf-8", b'{"ok":false,"error":"Invalid admin token"}')
                    return
                signed = _sign_session(token_secret, int(time.time()))
                cookie_hdr = f"{SESSION_COOKIE_NAME}={signed}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL_SEC}"
                _audit_log(ip, path, 200, "auth_ok", audit_path=audit_path)
                self._send_bytes(
                    200,
                    "application/json; charset=utf-8",
                    b'{"ok":true,"authenticated":true}',
                    extra_headers=[("Set-Cookie", cookie_hdr)],
                )
                return

            if path == "/api/brain/logout":
                cookie_hdr = f"{SESSION_COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
                _audit_log(ip, path, 200, "logout", audit_path=audit_path)
                self._send_bytes(
                    200,
                    "application/json; charset=utf-8",
                    b'{"ok":true,"authenticated":false}',
                    extra_headers=[("Set-Cookie", cookie_hdr)],
                )
                return

            # 4. Require explicit authorization for ALL state-changing controls
            if not self._is_authenticated():
                _audit_log(ip, path, 401, "unauthorized", audit_path=audit_path)
                self._send_bytes(401, "application/json; charset=utf-8", b'{"ok":false,"error":"Admin authorization required"}')
                return

            if path == "/api/brain/model":
                model = str(body.get("model") or "").strip()
                if model not in ALLOWED_MODELS:
                    self._send_bytes(400, "application/json; charset=utf-8", b'{"ok":false,"error":"Unsupported model"}')
                    return
                _update_hearthcore_env("ANTHROPIC_MODEL", model, env_path=env_path)
                collector.refresh_once()
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
                _update_hearthcore_env("HEARTHCORE_CREDIT_USD", f"{new_baseline:.2f}", env_path=env_path)
                collector.refresh_once()
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

    print(f"Azeroth Watch listening on http://{args.host}:{args.port} (Host allow-list + admin auth enabled)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
        srv.server_close()


if __name__ == "__main__":
    main()
