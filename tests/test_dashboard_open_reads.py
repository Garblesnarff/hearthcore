import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1] / "azeroth-watch"
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location("aw_server_open_reads", ROOT / "server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


class _Collector:
    def get_snapshot(self):
        return 1, {"db_ok": True, "data_age_sec": 0, "ai_brain": {"model": "m"}, "humans": []}


class DashboardOpenReadsTests(unittest.TestCase):
    def _run(self, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        handler = server.make_handler(
            _Collector(), admin_token="secret-token", audit_path=str(Path(tmp.name) / "a.log"),
            env_path=str(Path(tmp.name) / ".env"), allowed_hosts={"127.0.0.1"},
            trust_loopback_reads=False, trust_loopback_writes=False, **kwargs,
        )
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def _req(self, url, data=None, headers=None):
        req = urllib.request.Request(url, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_open_reads_shows_brain_without_token_but_writes_stay_gated(self):
        base = self._run(open_reads=True, require_auth_for_reads=False)
        st, body = self._req(base + "/api/state")
        self.assertEqual((st, body["ai_brain"]), (200, {"model": "m"}))
        st, _ = self._req(base + "/api/brain/model", b'{"model":"x"}', {"Content-Type": "application/json"})
        self.assertEqual(st, 401)
        for _i in range(6):  # failed logins must not take the read-only view away
            self._req(base + "/api/brain/auth", b'{"token":"no"}', {"Content-Type": "application/json"})
        self.assertEqual(self._req(base + "/api/state")[0], 200)

    def test_stale_cookie_polling_never_locks_out(self):
        base = self._run(open_reads=False, require_auth_for_reads=False)
        cookie = {"Cookie": f"{server.SESSION_COOKIE_NAME}=stale-session-id-from-before-restart"}
        for _i in range(8):
            st, body = self._req(base + "/api/state", headers=cookie)
            self.assertEqual(st, 200)
            self.assertTrue(body["ai_brain"]["redacted"])

    def test_strict_read_auth_wins_over_open_reads(self):
        base = self._run(open_reads=True, require_auth_for_reads=True)
        self.assertEqual(self._req(base + "/api/state")[0], 401)


if __name__ == "__main__":
    unittest.main()
