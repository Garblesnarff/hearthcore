"""Issue #47 regression tests: fail-closed live benchmark, error deltas, staleness, workload & admission contract."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hearthcore import benchmark


def _state(
    errors_bytes=0,
    *,
    up=True,
    include_all=True,
    db_ok=True,
    data_age_sec=2.0,
    active_ai_bots=None,
    tick_ts=1_800_000_000,
):
    st = {
        "db_ok": db_ok,
        "data_age_sec": data_age_sec,
        "vitals": {"worldserver_up": up, "worldserver_rss_mib": 900.0, "worldserver_cpu_pct": 40.0},
        "ticks": {"latest": 12.0, "p99": 20.0, "series": [{"t": tick_ts, "ms": 12.0}]},
        "population": {"humans": 1, "companions_online": 13, "random_online": 50, "total_online": 64},
    }
    if active_ai_bots is not None:
        st["population"]["active_ai_bots"] = active_ai_bots
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
    def _run(
        self,
        states,
        *,
        min_duration_sec=0.0,
        monotonic_seq=None,
        time_seq=None,
        brain_db_path=None,
        sql_exec=None,
        swap_seq=None,
        psi_seq=None,
    ):
        """Hermetic runner against a scripted sequence of dashboard states."""
        queue = [_FakeResponse(json.dumps(s).encode("utf-8")) for s in states]

        def fake_urlopen(*_a, **_k):
            return queue.pop(0)

        m_vals = list(monotonic_seq) if monotonic_seq is not None else None
        t_vals = list(time_seq) if time_seq is not None else None

        def fake_mono():
            if m_vals is None:
                return 100.0
            return m_vals.pop(0) if len(m_vals) > 1 else m_vals[0]

        def fake_time():
            if t_vals is None:
                return 1_800_000_005.0
            return t_vals.pop(0) if len(t_vals) > 1 else t_vals[0]

        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            with mock.patch.object(benchmark.urllib.request, "urlopen", side_effect=fake_urlopen), \
                    mock.patch.object(benchmark.time, "sleep", lambda _s: None), \
                    mock.patch.object(benchmark.time, "monotonic", side_effect=fake_mono), \
                    mock.patch.object(benchmark.time, "time", side_effect=fake_time), \
                    mock.patch.object(benchmark, "_read_meminfo_mib", return_value={"MemAvailable": 1400.0, "SwapUsed": 50.0}), \
                    mock.patch.object(benchmark, "_read_psi", side_effect=psi_seq if psi_seq else (lambda: {})), \
                    mock.patch.object(benchmark, "_read_vmstat_swap_io", side_effect=swap_seq if swap_seq else (lambda: {"pswpin": 0, "pswpout": 0})), \
                    mock.patch.object(benchmark, "_read_worldserver_pss_mib", return_value=820.5), \
                    mock.patch.object(benchmark, "_read_cgroup_limits", return_value={"memory_max": "max"}), \
                    mock.patch.object(benchmark, "_read_installed_config_snapshot", return_value={"python_version": "3.11", "config_files_found": ["playerbots.conf"]}), \
                    mock.patch.object(benchmark, "_git_sha", return_value="test"):
                return benchmark.run_live_baseline_benchmark(
                    sample_count=len(states),
                    interval_sec=0.0,
                    min_duration_sec=min_duration_sec,
                    out_dir=tp,
                    brain_db_path=brain_db_path if brain_db_path is not None else (tp / "missing.db"),
                    sql_exec=sql_exec,
                )

    def test_old_error_bytes_do_not_fail_when_delta_is_zero(self):
        art = self._run([_state(4096), _state(4096), _state(4096)])
        self.assertEqual(art["run"]["errors_bytes_delta"], 0)
        self.assertTrue(art["correctness"]["errors_log_no_growth"])
        self.assertTrue(art["correctness"]["errors_log_zero_bytes"])

    def test_single_sample_cannot_pass_error_growth_check(self):
        art = self._run([_state(0)])
        self.assertIsNone(art["run"]["errors_bytes_delta"])
        self.assertFalse(art["correctness"]["errors_log_no_growth"])
        self.assertFalse(art["all_correctness_passed"])

    def test_error_growth_between_first_and_last_sample_fails(self):
        art = self._run([_state(100), _state(100), _state(164)])
        self.assertEqual(art["run"]["errors_bytes_delta"], 64)
        self.assertFalse(art["correctness"]["errors_log_no_growth"])
        self.assertFalse(art["all_correctness_passed"])

    def test_error_log_rotation_and_regrowth_fails_closed(self):
        # 100 -> 0 (rotated) -> 100 (regrew to same size!): mid-run change MUST fail & delta is None!
        art = self._run([_state(100), _state(0), _state(100)])
        self.assertTrue(art["run"]["errors_bytes_changed_mid_run"])
        self.assertIsNone(art["run"]["errors_bytes_delta"])
        self.assertFalse(art["correctness"]["errors_log_no_growth"])
        self.assertFalse(art["all_correctness_passed"])

    def test_missing_errors_bytes_is_unknown_not_zero(self):
        art = self._run([_state(None), _state(None)])
        self.assertIsNone(art["run"]["errors_bytes_delta"])
        self.assertTrue(art["run"]["errors_bytes_missing"])
        self.assertFalse(art["correctness"]["errors_log_no_growth"])
        self.assertFalse(art["all_correctness_passed"])

    def test_missing_population_or_stale_or_db_down_fails_telemetry_present(self):
        art_pop = self._run([_state(0), _state(0, include_all=False)])
        self.assertFalse(art_pop["correctness"]["telemetry_present"])
        self.assertFalse(art_pop["all_correctness_passed"])

        art_stale = self._run([_state(0, data_age_sec=60.5), _state(0, data_age_sec=60.5)])
        self.assertFalse(art_stale["correctness"]["telemetry_present"])

        art_no_age = self._run([_state(0, data_age_sec=None), _state(0, data_age_sec=None)])
        self.assertFalse(art_no_age["correctness"]["telemetry_present"])

        art_db = self._run([_state(0, db_ok=False), _state(0, db_ok=False)])
        self.assertFalse(art_db["correctness"]["telemetry_present"])

    def test_min_duration_600s_boundary_and_staleness_compatibility(self):
        # 599.9s fails; 600.0s passes AND last sample stays fresh under 180s window!
        # monotonic calls: run_start, sample0 t0, sample0 t1, sample1 t0, sample1 t1, end
        art_short = self._run(
            [_state(0, tick_ts=1_800_000_000), _state(0, tick_ts=1_800_000_599)],
            min_duration_sec=600.0,
            monotonic_seq=[0.0, 0.1, 0.2, 599.7, 599.8, 599.9],
            time_seq=[1_800_000_000, 1_800_000_599, 1_800_000_600, 1_800_000_600],
        )
        self.assertFalse(art_short["correctness"]["min_duration_met"])
        self.assertFalse(art_short["all_correctness_passed"])

        art_ok = self._run(
            [_state(0, tick_ts=1_800_000_000), _state(0, tick_ts=1_800_000_600)],
            min_duration_sec=600.0,
            monotonic_seq=[0.0, 0.1, 0.2, 599.8, 599.9, 600.0],
            time_seq=[1_800_000_000, 1_800_000_600, 1_800_000_601, 1_800_000_601],
        )
        self.assertTrue(art_ok["correctness"]["min_duration_met"])
        self.assertEqual(art_ok["telemetry_status"], "ok")
        self.assertTrue(art_ok["all_correctness_passed"])

    def test_run_info_is_inside_the_hashed_body(self):
        art = self._run([_state(0), _state(0)])
        body = {k: v for k, v in art.items() if k not in ("artifact_sha256", "saved_path")}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(digest, art["artifact_sha256"])

    def test_workload_managed_groups_instances_pss_psi_delta_and_strict_admission_contract(self):
        from hearthcore.opportunities import OpportunityCoordinator
        with tempfile.TemporaryDirectory() as tmp:
            tp = Path(tmp)
            db_p = tp / "brain.db"
            coord = OpportunityCoordinator(db_p)
            o1 = coord.create_opportunity(dungeon_id="stockade_n", creator_guid=201, is_shadow=False, now_ts=1_800_000_000)
            coord.create_opportunity(dungeon_id="deadmines_n", creator_guid=202, is_shadow=False, now_ts=1_800_000_000)
            # Advance o1 to active managed state to verify BOTH managed & pending counts
            with sqlite3.connect(db_p) as conn:
                conn.execute("UPDATE opportunities SET state='active' WHERE id=?", (o1["id"],))

            # Full 600s run with active_ai_bots, PSI deltas, swap I/O, PSS, and SQL telemetry -> admit_ready=True!
            art = self._run(
                [
                    _state(0, active_ai_bots=60, tick_ts=1_800_000_000),
                    _state(0, active_ai_bots=62, tick_ts=1_800_000_600),
                ],
                min_duration_sec=600.0,
                monotonic_seq=[0.0, 0.1, 0.2, 0.25, 0.26, 599.8, 599.85, 599.9, 599.95, 600.0],
                time_seq=[1_800_000_000, 1_800_000_600, 1_800_000_601, 1_800_000_601],
                brain_db_path=db_p,
                sql_exec=lambda _s: [["2"]],
                swap_seq=[{"pswpin": 10, "pswpout": 20}, {"pswpin": 12, "pswpout": 25}],
                psi_seq=[
                    {"memory": {"some_avg10": 0.1, "some_total_us": 1000.0}},
                    {"memory": {"some_avg10": 0.2, "some_total_us": 1500.0}},
                ],
            )
            self.assertTrue(art["all_correctness_passed"])
            self.assertEqual(art["workload"]["concurrent_managed_groups"], 1)
            self.assertEqual(art["workload"]["pending_groups_max"], 1)
            self.assertEqual(art["workload"]["active_instances_max"], 2)
            self.assertEqual(art["workload"]["active_ai_bots_max"], 62)
            self.assertEqual(art["metrics"]["worldserver_pss_mib_mean"], 820.5)
            self.assertEqual(art["metrics"]["swap_in_pages_delta"], 2)
            self.assertEqual(art["metrics"]["swap_out_pages_delta"], 5)
            self.assertEqual(art["metrics"]["psi_delta"]["memory"]["some_total_us_delta"], 500.0)

            adm = benchmark.benchmark_summary_for_admission(art)
            self.assertTrue(adm["admit_ready"], adm)
            self.assertTrue(adm["digest_verified"])

            # Negative admission checks: tampered digest, missing active_ai_bots, and short duration MUST reject!
            tampered = dict(art, metrics={**art["metrics"], "tick_p99_ms": 1.0})
            self.assertFalse(benchmark.benchmark_summary_for_admission(tampered)["admit_ready"])

            short_art = self._run([_state(0), _state(0)], min_duration_sec=0.0, brain_db_path=db_p, sql_exec=lambda _s: [["1"]])
            adm_short = benchmark.benchmark_summary_for_admission(short_art)
            self.assertFalse(adm_short["admit_ready"])
            self.assertIn("min_duration_not_met", adm_short["reasons"])
            self.assertIn("missing_active_ai_bots", adm_short["reasons"])


if __name__ == "__main__":
    unittest.main()
