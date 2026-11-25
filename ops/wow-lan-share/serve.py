#!/usr/bin/env python3
"""On-demand, time-limited, token-authenticated LAN WoW 3.3.5a client streamer (Issue #70).

Security & resource controls:
- Runs strictly on demand (never enabled at boot) with an automatic TTL shutdown (default 30 min).
- Requires a per-run URL token (/<token>/...) verified with constant-time hmac.compare_digest.
- Publishes SHA-256 digests at /<token>/checksums.sha256 for setup-wow-laptop.sh, dxvk-d3d9.dll,
  and Wow.exe so the laptop verifies integrity before executing or loading anything.
- Allows at most ONE concurrent /wow.tar stream so disk I/O never starves ac-worldserver.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import os
import secrets
import shutil
import socketserver
import subprocess
import threading
import time
from pathlib import Path

HOST = os.getenv("WOW_SHARE_HOST", "0.0.0.0")
PORT = int(os.getenv("WOW_SHARE_PORT", "8788"))
TTL_SEC = int(os.getenv("WOW_SHARE_TTL_SEC", "1800"))
HERE = Path(__file__).resolve().parent
SETUP_SCRIPT = HERE / "setup-wow-laptop.sh"
WOW_DIR = Path(os.getenv("WOW_CLIENT_DIR", "/mnt/hdd/WoW335a/extracted/ChromieCraft_3.3.5a"))
DXVK_DLL = Path(os.getenv("WOW_DXVK_DLL", str(Path.home() / "wine-wotlk/drive_c/windows/system32/d3d9.dll")))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(256 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(
    *,
    token: str,
    setup_script: Path = SETUP_SCRIPT,
    wow_dir: Path = WOW_DIR,
    dxvk_dll: Path = DXVK_DLL,
    tar_lock: threading.Lock | None = None,
):
    stream_lock = tar_lock if tar_lock is not None else threading.Lock()

    def _build_checksums() -> bytes:
        lines: list[str] = []
        for label, path in (
            ("setup-wow-laptop.sh", setup_script),
            ("dxvk-d3d9.dll", dxvk_dll),
            ("Wow.exe", wow_dir / "Wow.exe"),
        ):
            if path.is_file():
                lines.append(f"{sha256_file(path)}  {label}")
        return ("\n".join(lines) + "\n").encode("utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            print(f"[wow-lan-share] {self.client_address[0]} - {fmt % args}", flush=True)

        def _extract_route(self) -> str | None:
            clean = self.path.split("?", 1)[0]
            parts = [p for p in clean.split("/") if p]
            if len(parts) < 2:
                return None
            req_tok, route = parts[0], "/" + "/".join(parts[1:])
            if not token or not hmac.compare_digest(req_tok, token):
                return None
            return route

        def _send_bytes(self, data: bytes, content_type: str, head_only: bool = False) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-SHA256", hashlib.sha256(data).hexdigest())
            self.send_header("Connection", "close")
            self.end_headers()
            if not head_only:
                self.wfile.write(data)

        def _send_file(self, path: Path, content_type: str, head_only: bool = False) -> None:
            if not path.is_file():
                self.send_error(404, "File not found")
                return
            size = path.stat().st_size
            digest = sha256_file(path)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("X-Content-SHA256", digest)
            self.send_header("Connection", "close")
            self.end_headers()
            if head_only:
                return
            with path.open("rb") as f:
                shutil.copyfileobj(f, self.wfile, length=256 * 1024)

        def do_HEAD(self) -> None:
            route = self._extract_route()
            if route is None:
                self.send_error(403, "Valid one-time share token required")
                return
            if route in ("/setup", "/setup-wow-laptop.sh"):
                self._send_file(setup_script, "text/x-shellscript; charset=utf-8", head_only=True)
            elif route == "/checksums.sha256":
                self._send_bytes(_build_checksums(), "text/plain; charset=utf-8", head_only=True)
            elif route == "/dxvk-d3d9.dll":
                self._send_file(dxvk_dll, "application/octet-stream", head_only=True)
            elif route == "/wow.tar":
                if not (wow_dir / "Wow.exe").is_file():
                    self.send_error(404, "WoW client not found")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-tar")
                self.send_header("Connection", "close")
                self.end_headers()
            else:
                self.send_error(404)

        def do_GET(self) -> None:
            route = self._extract_route()
            if route is None:
                self.send_error(403, "Valid one-time share token required")
                return

            if route in ("/setup", "/setup-wow-laptop.sh"):
                self._send_file(setup_script, "text/x-shellscript; charset=utf-8")
                return

            if route == "/checksums.sha256":
                self._send_bytes(_build_checksums(), "text/plain; charset=utf-8")
                return

            if route == "/dxvk-d3d9.dll":
                self._send_file(dxvk_dll, "application/octet-stream")
                return

            if route == "/wow.tar":
                if not (wow_dir / "Wow.exe").is_file():
                    self.send_error(404, "WoW client not found")
                    return
                if not stream_lock.acquire(blocking=False):
                    self.send_error(429, "Another /wow.tar transfer is already active")
                    return
                proc = None
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-tar")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    proc = subprocess.Popen(
                        [
                            "tar",
                            "-cf",
                            "-",
                            "--exclude=./Cache",
                            "--exclude=./Logs",
                            "--exclude=./WTF/Account",
                            "-C",
                            str(wow_dir),
                            ".",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                    assert proc.stdout is not None
                    while True:
                        chunk = proc.stdout.read(256 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    if proc is not None:
                        proc.terminate()
                finally:
                    if proc is not None:
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            proc.kill()
                    stream_lock.release()
                return

            self.send_error(404)

    return Handler


def main() -> None:
    try:
        os.nice(15)
    except OSError:
        pass
    token = (os.getenv("WOW_SHARE_TOKEN") or "").strip() or secrets.token_urlsafe(18)
    srv = ThreadingHTTPServer((HOST, PORT), make_handler(token=token))

    def _auto_shutdown():
        time.sleep(max(30, TTL_SEC))
        print(f"[wow-lan-share] TTL ({TTL_SEC}s) reached — shutting down.", flush=True)
        srv.shutdown()

    threading.Thread(target=_auto_shutdown, daemon=True).start()
    setup_sha = sha256_file(SETUP_SCRIPT) if SETUP_SCRIPT.is_file() else "missing"
    print(f"[wow-lan-share] On-demand share listening on http://{HOST}:{PORT} (auto-stops in {TTL_SEC}s)", flush=True)
    print(f"[wow-lan-share] One-time token: {token}", flush=True)
    print(f"[wow-lan-share] setup-wow-laptop.sh SHA-256: {setup_sha}", flush=True)
    print(
        f"[wow-lan-share] Safe laptop download & verify:\n"
        f"  curl -fsSL http://<SERVER_IP>:{PORT}/{token}/setup -o setup-wow-laptop.sh\n"
        f"  echo '{setup_sha}  setup-wow-laptop.sh' | sha256sum -c -\n"
        f"  SERVER_IP=<SERVER_IP> SHARE_TOKEN='{token}' bash setup-wow-laptop.sh",
        flush=True,
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
