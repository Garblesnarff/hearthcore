#!/usr/bin/env python3
"""Azeroth Watch — lightweight read-only HTTP + SSE server (zero external deps)."""

from __future__ import annotations

import argparse
import http.server
import json
import mimetypes
import os
import signal
import socketserver
import subprocess
import sys
import threading
import time

from collector import AzerothCollector

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
HEARTHCORE_ENV = "/home/wonky/hearthcore/.env"
ALLOWED_MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5"}
_CTL_LOCK = threading.Lock()
_LAST_RUN_TS: dict[str, float] = {}


def _update_hearthcore_env(key: str, value: str) -> None:
    """Safely update one non-secret key in hearthcore/.env (preserves chmod 600, never echoes secrets)."""
    with _CTL_LOCK:
        lines = []
        if os.path.isfile(HEARTHCORE_ENV):
            with open(HEARTHCORE_ENV, "r", encoding="utf-8") as f:
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
        tmp = HEARTHCORE_ENV + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, HEARTHCORE_ENV)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(collector: AzerothCollector):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            # Quiet logging to avoid disk churn
            return

        def _send_bytes(self, status: int, content_type: str, body: bytes, cache: str = "no-store"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]

            if path == "/api/health":
                body = json.dumps({"ok": True}).encode("utf-8")
                self._send_bytes(200, "application/json; charset=utf-8", body)
                return

            if path == "/api/state":
                _, snap = collector.get_snapshot()
                body = json.dumps(snap, separators=(",", ":")).encode("utf-8")
                self._send_bytes(200, "application/json; charset=utf-8", body)
                return

            if path == "/api/stream":
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
                            payload = json.dumps(snap, separators=(",", ":"))
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
            path = self.path.split("?", 1)[0]
            try:
                clen = min(4096, int(self.headers.get("Content-Length", "0") or 0))
                raw = self.rfile.read(clen) if clen > 0 else b"{}"
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self._send_bytes(400, "application/json; charset=utf-8", b'{"ok":false,"error":"Invalid JSON"}')
                return

            if path == "/api/brain/model":
                model = str(body.get("model") or "").strip()
                if model not in ALLOWED_MODELS:
                    self._send_bytes(400, "application/json; charset=utf-8", b'{"ok":false,"error":"Unsupported model"}')
                    return
                _update_hearthcore_env("ANTHROPIC_MODEL", model)
                collector.refresh_once()
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
                _update_hearthcore_env("HEARTHCORE_CREDIT_USD", f"{new_baseline:.2f}")
                collector.refresh_once()
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
                        self._send_bytes(429, "application/json; charset=utf-8", b'{"ok":false,"error":"Please wait 15s between manual runs"}')
                        return
                    _LAST_RUN_TS[target] = now
                subprocess.Popen(
                    ["systemctl", "--user", "start", "--no-block", unit],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
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
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--interval", type=float, default=4.0)
    args = parser.parse_args()

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

    print(f"Azeroth Watch listening on http://{args.host}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
        srv.server_close()


if __name__ == "__main__":
    main()
