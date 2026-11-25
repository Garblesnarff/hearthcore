#!/usr/bin/env python3
"""Lightweight LAN-only WoW 3.3.5a installer & client streamer for laptop setup.

Endpoints (read-only):
  GET /setup         -> setup-wow-laptop.sh
  GET /dxvk-d3d9.dll -> 32-bit DXVK d3d9.dll (from Acer's working wine-wotlk prefix)
  GET /wow.tar       -> uncompressed live-streamed tar of WoW 3.3.5a client
                        (excludes Cache, Logs, WTF/Account; uses zero extra disk on Acer)
"""

from __future__ import annotations

import http.server
import os
import shutil
import socketserver
import subprocess
from pathlib import Path

HOST = "0.0.0.0"
PORT = 8788
HERE = Path(__file__).resolve().parent
SETUP_SCRIPT = HERE / "setup-wow-laptop.sh"
WOW_DIR = Path("/mnt/hdd/WoW335a/extracted/ChromieCraft_3.3.5a")
DXVK_DLL = Path("/home/wonky/wine-wotlk/drive_c/windows/system32/d3d9.dll")


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[wow-lan-share] {self.client_address[0]} - {fmt % args}", flush=True)

    def _send_file(self, path: Path, content_type: str, head_only: bool = False) -> None:
        if not path.is_file():
            self.send_error(404, "File not found")
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Connection", "close")
        self.end_headers()
        if head_only:
            return
        with path.open("rb") as f:
            shutil.copyfileobj(f, self.wfile, length=256 * 1024)

    def do_HEAD(self) -> None:
        if self.path in ("/", "/setup", "/setup-wow-laptop.sh"):
            self._send_file(SETUP_SCRIPT, "text/x-shellscript; charset=utf-8", head_only=True)
        elif self.path == "/dxvk-d3d9.dll":
            self._send_file(DXVK_DLL, "application/octet-stream", head_only=True)
        elif self.path == "/wow.tar":
            if not (WOW_DIR / "Wow.exe").is_file():
                self.send_error(404, "WoW client not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-tar")
            self.send_header("Connection", "close")
            self.end_headers()
        else:
            self.send_error(404)

    def do_GET(self) -> None:
        if self.path in ("/", "/setup", "/setup-wow-laptop.sh"):
            self._send_file(SETUP_SCRIPT, "text/x-shellscript; charset=utf-8")
            return

        if self.path == "/dxvk-d3d9.dll":
            self._send_file(DXVK_DLL, "application/octet-stream")
            return

        if self.path == "/wow.tar":
            if not (WOW_DIR / "Wow.exe").is_file():
                self.send_error(404, "WoW client not found")
                return
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
                    str(WOW_DIR),
                    ".",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                assert proc.stdout is not None
                while True:
                    chunk = proc.stdout.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                proc.terminate()
            finally:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
            return

        self.send_error(404)


def main() -> None:
    try:
        os.nice(15)
    except OSError:
        pass
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[wow-lan-share] Serving WoW 3.3.5a & laptop installer on http://{HOST}:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
