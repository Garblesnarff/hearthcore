"""Behavioral tests for operator-visible alerting & health watchdog (Issue #59)."""
from __future__ import annotations

import http.server
import json
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "azeroth-watch"))

from hearthcore.alerts import dispatch_alert, run_health_watch
from hearthcore.bridge import BrainStore
from hearthcore.core import fallback, profile, snapshot
from server import project_snapshot


class Issue59AlertingTests(unittest.TestCase):
    def test_dispatch_alert_logs_delivers_webhook_and_deduplicates_within_cooldown(self):
        received: list[dict] = []

        class HookHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.append(json.loads(self.rfile.read(length).decode("utf-8")))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

        httpd = socketserver.TCPServer(("127.0.0.1", 0), HookHandler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                rt = Path(tmp) / "runtime"
                url = f"http://127.0.0.1:{port}/hook"

                r1 = dispatch_alert(
                    "systemd_failure", "Unit failed: hearthcore-brain.service",
                    severity="critical", runtime_dir=rt, webhook_url=url,
                    enable_desktop=False, cooldown_sec=600, now_ts=1_800_000_000,
                )
                self.assertTrue(r1["log_ok"])
                self.assertTrue(r1["webhook_sent"])
                self.assertFalse(r1["suppressed"])
                self.assertEqual(len(received), 1)
                self.assertIn("hearthcore-brain.service", received[0]["content"])

                # Second identical alert within cooldown writes log but suppresses duplicate webhook!
                r2 = dispatch_alert(
                    "systemd_failure", "Unit failed: hearthcore-brain.service",
                    severity="critical", runtime_dir=rt, webhook_url=url,
                    enable_desktop=False, cooldown_sec=600, now_ts=1_800_000_100,
                )
                self.assertTrue(r2["suppressed"])
                self.assertFalse(r2["webhook_sent"])
                self.assertEqual(len(received), 1)

                # After cooldown elapses, webhook delivers again
                r3 = dispatch_alert(
                    "systemd_failure", "Unit failed: hearthcore-brain.service",
                    severity="critical", runtime_dir=rt, webhook_url=url,
                    enable_desktop=False, cooldown_sec=600, now_ts=1_800_000_700,
                )
                self.assertFalse(r3["suppressed"])
                self.assertTrue(r3["webhook_sent"])
                self.assertEqual(len(received), 2)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_health_watch_detects_stale_brain_and_dashboard_surfaces_alert_banner(self):
        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            rt = tp / "runtime"
            db_p = tp / "brain.db"
            store = BrainStore(db_p)
            p = profile("brannoc")
            snap = snapshot(json.loads((ROOT / "examples" / "brannoc.json").read_text(encoding="utf-8")), "brannoc")
            plan = fallback(p, snap)
            store.record_plan(
                now_ts=1_800_000_000, slug="brannoc", guid=101, source="fallback",
                model="fallback", effort="", plan=plan, primary_desc="Test",
                cpp_weights={"craft": 1.2}, meta={}, applied=True, error_note="",
                observed_at="2026-11-25T16:00:00Z",
            )
            store.set_meta("last_success_at", "1800000000")

            # Fresh (<stale threshold) -> OK, zero alerts fired
            fresh = run_health_watch(
                brain_db_path=db_p, runtime_dir=rt, dashboard_url="",
                webhook_url="", enable_desktop=False, now_ts=1_800_001_000,
            )
            self.assertTrue(fresh["ok"])
            self.assertEqual(fresh["alerts_fired"], [])

            # Stale (>default 6h threshold) -> fires brain_stale critical alert!
            stale = run_health_watch(
                brain_db_path=db_p, runtime_dir=rt, dashboard_url="",
                webhook_url="", enable_desktop=False, now_ts=1_800_030_000,
            )
            self.assertFalse(stale["ok"])
            self.assertEqual(stale["alerts_fired"][0]["source"], "brain_stale")

            # Verify dashboard kiosk view exposes stale_alarm + latest_alert summary without secrets
            kiosk = project_snapshot(
                {
                    "db_ok": True,
                    "data_age_sec": 5,
                    "ai_brain": {
                        "stale_alarm": True,
                        "failure_alerts": [stale["alerts_fired"][0]],
                        "balance": {"remaining_usd": 99.0},
                    },
                },
                authenticated=False,
            )
            self.assertTrue(kiosk["ai_brain"]["stale_alarm"])
            self.assertEqual(kiosk["ai_brain"]["recent_alert_count"], 1)
            self.assertEqual(kiosk["ai_brain"]["latest_alert"]["source"], "brain_stale")
            self.assertNotIn("balance", kiosk["ai_brain"])


if __name__ == "__main__":
    unittest.main()
