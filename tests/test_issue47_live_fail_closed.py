"""Issue #47 remaining items: errors_bytes is a delta, missing telemetry fails closed, minimum duration."""
from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from unittest import mock
import tempfile

from hearthcore import benchmark


def _state(errors_bytes=None, up=True, include_all=True):
    st = {
        "vitals": {"worldserver_up": up, "worldserver_rss_mib": 900.0, "worldserver_cpu_pct": 40.0},
        "ticks": {"latest": 12.0, "p99": 20.0, "series": [12.0]},
        "population": {"humans": 1, "companions_online": 13, "random_online": 50, "total_online": 64},
    }
    if errors_bytes is not None:
        st["errors_bytes"] = errors_bytes
    if not include_all:
        st.pop("population", None)
    return st


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class LiveFailClosedTests(unittest.TestCase):
    def _run(self, states, *, min_duration_sec=0.0):
        """Run the live benchmark against a scripted sequence of dashboard states."""
        queue = [_FakeResponse(json.dumps(s).encode("utf-8")) for s in states]

        def fake_urlopen(*_a, **_k):
            return queue.pop(0)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(benchmark.urllib.request, "urlopen", side_effect=fake_urlopen), \
                mock.patch.object(benchmark.time, "sleep", lambda _s: None), \
                mock.patch.object(benchmark, "_read_meminfo_mib", return_value={}), \
                mock.patch.object(benchmark, "_read_psi", return_value={}), \
                mock.patch.object(benchmark, "_git_sha", return_value="test"):
            return benchmark.run_live_baseline_benchmark(
                sample_count=len(states), interval_sec=0.0, min_duration_sec=min_duration_sec,
                out_dir=Path(tmp),
            )

    def test_old_error_bytes_do_not_fail_when_delta_is_zero(self):
        # A large but unchanged errors_bytes (an old error line) must not fail the run.
        art = self._run([_state(4096), _state(4096), _state(4096)])
        self.assertEqual(art["run"]["errors_bytes_delta"], 0)
        self.assertTrue(art["correctness"]["errors_log_zero_bytes"])

    def test_error_growth_between_first_and_last_sample_fails(self):
        art = self._run([_state(100), _state(100), _state(164)])
        self.assertEqual(art["run"]["errors_bytes_delta"], 64)
        self.assertFalse(art["correctness"]["errors_log_zero_bytes"])
        self.assertFalse(art["all_correctness_passed"])

    def test_missing_errors_bytes_is_unknown_not_zero(self):
        art = self._run([_state(None), _state(None)])
        self.assertIsNone(art["run"]["errors_bytes_delta"])
        self.assertFalse(art["correctness"]["errors_log_zero_bytes"])
        self.assertFalse(art["all_correctness_passed"])

    def test_missing_population_section_fails_telemetry_present(self):
        art = self._run([_state(0), _state(0, include_all=False)])
        self.assertFalse(art["correctness"]["telemetry_present"])
        self.assertFalse(art["all_correctness_passed"])

    def test_short_run_fails_minimum_duration(self):
        art = self._run([_state(0), _state(0)], min_duration_sec=600.0)
        self.assertFalse(art["correctness"]["min_duration_met"])
        self.assertFalse(art["all_correctness_passed"])

    def test_long_enough_run_meets_minimum_duration(self):
        art = self._run([_state(0), _state(0)], min_duration_sec=0.0)
        self.assertTrue(art["correctness"]["min_duration_met"])
        self.assertEqual(art["run"]["min_duration_sec"], 0.0)

    def test_run_info_is_inside_the_hashed_body(self):
        art = self._run([_state(0), _state(0)])
        body = {k: v for k, v in art.items() if k not in ("artifact_sha256", "saved_path")}
        import hashlib
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(digest, art["artifact_sha256"])


if __name__ == "__main__":
    unittest.main()
