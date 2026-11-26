#!/usr/bin/env python3
"""On-demand, time-limited, token-authenticated LAN WoW 3.3.5a client streamer (Issue #70).

Security & resource controls:
- Runs strictly on demand (never enabled at boot) with an automatic TTL shutdown (default 30 min).
- Requires a per-run URL token (/<token>/...) verified with constant-time hmac.compare_digest;
  the token is NEVER written to access logs (`<REDACTED_TOKEN>`) and has bounded uses
  (`max_tar_uses=1` and `max_total_uses=12`) so it cannot be abused across the 30-min window.
- Publishes SHA-256 digests at /<token>/checksums.sha256 for setup-wow-laptop.sh, dxvk-d3d9.dll,
  and Wow.exe + key WoW client binaries, and prints the out-of-band SHA-256 of `checksums.sha256`
  itself (`EXPECTED_CHECKSUMS_SHA256`) at startup so the laptop verifies integrity BEFORE trusting
  plain HTTP or running `sudo apt-get`.
- Allows at most ONE concurrent /wow.tar stream with a per-write socket timeout and wall-clock
  timeout so a stalled client can never starve ac-worldserver or hold the tar lock indefinitely.
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
import sys
import threading
import time
from pathlib import Path

HOST = os.getenv("WOW_SHARE_HOST", "0.0.0.0")
PORT = int(os.getenv("WOW_SHARE_PORT", "8788"))
TTL_SEC = int(os.getenv("WOW_SHARE_TTL_SEC", "1800"))
TAR_IDLE_TIMEOUT_SEC = float(os.getenv("WOW_SHARE_TAR_IDLE_TIMEOUT_SEC", "60"))
TAR_MAX_SEC = float(os.getenv("WOW_SHARE_TAR_MAX_SEC", "1800"))
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


def build_checksums_manifest(
    *,
    setup_script: Path = SETUP_SCRIPT,
    wow_dir: Path = WOW_DIR,
    dxvk_dll: Path = DXVK_DLL,
) -> bytes:
    """Build deterministic SHA-256 manifest covering script, DXVK DLL, and WoW executables/DLLs."""
    lines: list[str] = []
    for label, path in (
        ("setup-wow-laptop.sh", setup_script),
        ("dxvk-d3d9.dll", dxvk_dll),
    ):
        if path.is_file():
            lines.append(f"{sha256_file(path)}  {label}")

    if wow_dir.is_dir():
        # Hash top-level executables/DLLs (Wow.exe, DivxDecoder.dll, etc.) so extracted archive integrity is verified
        seen: set[str] = set()
        for entry in sorted(wow_dir.iterdir(), key=lambda p: p.name):
            if entry.is_file() and entry.suffix.lower() in (".exe", ".dll", ".bin"):
                rel = entry.name
                seen.add(rel)
                lines.append(f"{sha256_file(entry)}  wow/{rel}")
        # Always ensure Wow.exe is listed by canonical name too
        wow_exe = wow_dir / "Wow.exe"
        if wow_exe.is_file():
            lines.append(f"{sha256_file(wow_exe)}  Wow.exe")
    return ("\n".join(lines) + "\n").encode("utf-8")


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
    max_tar_uses: int = 1,
    max_total_uses: int = 12,
    tar_idle_timeout_sec: float = TAR_IDLE_TIMEOUT_SEC,
    tar_max_sec: float = TAR_MAX_SEC,
    log_sink=None,
):
    stream_lock = tar_lock if tar_lock is not None else threading.Lock()
    usage_lock = threading.Lock()
    usage = {"total": 0, "tar": 0}
    cached_checksums = build_checksums_manifest(
        setup_script=setup_script, wow_dir=wow_dir, dxvk_dll=dxvk_dll
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            rendered = fmt % args
            if token:
                rendered = rendered.replace(token, "<REDACTED_TOKEN>")
            line = f"[wow-lan-share] {self.client_address[0]} - {rendered}"
            if log_sink is not None:
                log_sink.append(line)
            else:
                print(line, flush=True)

        def _extract_route(self, *, count_use: bool = False) -> str | None:
            clean = self.path.split("?", 1)[0]
            parts = [p for p in clean.split("/") if p]
            if len(parts) < 2:
                return None
            req_tok, route = parts[0], "/" + "/".join(parts[1:])
            if not token or not hmac.compare_digest(req_tok, token):
                return None
            if count_use:
                with usage_lock:
                    if usage["total"] >= max(1, int(max_total_uses)):
                        return None
                    usage["total"] += 1
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
            route = self._extract_route(count_use=False)
            if route is None:
                self.send_error(403, "Valid one-time share token required")
                return
            if route in ("/setup", "/setup-wow-laptop.sh"):
                self._send_file(setup_script, "text/x-shellscript; charset=utf-8", head_only=True)
            elif route == "/checksums.sha256":
                self._send_bytes(cached_checksums, "text/plain; charset=utf-8", head_only=True)
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
            route = self._extract_route(count_use=True)
            if route is None:
                self.send_error(403, "Valid one-time share token required (or token uses exhausted)")
                return

            if route in ("/setup", "/setup-wow-laptop.sh"):
                self._send_file(setup_script, "text/x-shellscript; charset=utf-8")
                return

            if route == "/checksums.sha256":
                self._send_bytes(cached_checksums, "text/plain; charset=utf-8")
                return

            if route == "/dxvk-d3d9.dll":
                self._send_file(dxvk_dll, "application/octet-stream")
                return

            if route == "/wow.tar":
                if not (wow_dir / "Wow.exe").is_file():
                    self.send_error(404, "WoW client not found")
                    return
                with usage_lock:
                    if usage["tar"] >= max(1, int(max_tar_uses)):
                        self.send_error(403, "One-time /wow.tar download token already used")
                        return
                if not stream_lock.acquire(blocking=False):
                    self.send_error(429, "Another /wow.tar transfer is already active")
                    return
                with usage_lock:
                    if usage["tar"] >= max(1, int(max_tar_uses)):
                        stream_lock.release()
                        self.send_error(403, "One-time /wow.tar download token already used")
                        return
                    usage["tar"] += 1
                proc = None
                try:
                    self.connection.settimeout(max(0.5, float(tar_idle_timeout_sec)))
                    deadline = time.monotonic() + max(1.0, float(tar_max_sec))
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
                        if time.monotonic() > deadline:
                            raise TimeoutError("tar wall-clock timeout exceeded")
                        chunk = proc.stdout.read(256 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
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
    manifest = build_checksums_manifest(setup_script=SETUP_SCRIPT, wow_dir=WOW_DIR, dxvk_dll=DXVK_DLL)
    manifest_sha = hashlib.sha256(manifest).hexdigest()
    setup_sha = sha256_file(SETUP_SCRIPT) if SETUP_SCRIPT.is_file() else "missing"

    srv = ThreadingHTTPServer((HOST, PORT), make_handler(token=token))

    def _auto_shutdown():
        time.sleep(max(30, TTL_SEC))
        print(f"[wow-lan-share] TTL ({TTL_SEC}s) reached — shutting down.", flush=True)
        srv.shutdown()

    threading.Thread(target=_auto_shutdown, daemon=True).start()
    print(f"[wow-lan-share] On-demand share listening on http://{HOST}:{PORT} (auto-stops in {TTL_SEC}s)", flush=True)
    # Print secret token & out-of-band checksums digest to stderr only (never in access logs)
    print(f"[wow-lan-share] One-time token (single /wow.tar use): {token}", file=sys.stderr, flush=True)
    print(f"[wow-lan-share] setup-wow-laptop.sh SHA-256: {setup_sha}", file=sys.stderr, flush=True)
    print(f"[wow-lan-share] checksums.sha256 SHA-256:    {manifest_sha}", file=sys.stderr, flush=True)
    print(
        f"[wow-lan-share] Safe laptop download & out-of-band verified install:\n"
        f"  curl -fsSL http://<SERVER_IP>:{PORT}/{token}/setup -o setup-wow-laptop.sh\n"
        f"  echo '{setup_sha}  setup-wow-laptop.sh' | sha256sum -c -\n"
        f"  SERVER_IP=<SERVER_IP> SHARE_TOKEN='{token}' EXPECTED_CHECKSUMS_SHA256='{manifest_sha}' bash setup-wow-laptop.sh",
        file=sys.stderr,
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
