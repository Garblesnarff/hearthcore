"""Phase II Repeatable Performance & Correctness Benchmark Harness (Card II-10, Issue #47).

Design guarantees (docs/PHASE_II_LIVING_GUILD.md Section 10 & Issue #47):
- Computes p50/p95/p99/max over raw tick histogram buckets OR deduplicated non-overlapping world
  tick samples (`ticks.series` keyed by timestamp), explicitly labeling `tick_distribution_method`.
- Missing or stale telemetry is reported as unknown (`None`) and fails correctness (`all_correctness_passed=False`)
  — NEVER fabricating a pass.
- Separates connected humans, logged-in bots (companions + random), active AI bots, managed/pending
  groups, and active instances.
- Captures host MemAvailable, SwapUsed/delta, Linux PSI (`/proc/pressure/{cpu,memory,io}`), and SQL
  telemetry when available.
- Supports warmup + repeatable staging runs (recommended 10-30 min for staging acceptance).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import sqlite3
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import ROOT

logger = logging.getLogger("hearthcore.benchmark")

BENCH_SCHEMA_VERSION = 2


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, int(round((pct / 100.0) * (len(sorted_vals) - 1)))))
    return round(sorted_vals[idx], 2)


def percentile_from_histogram(buckets: list[tuple[float, int]], pct: float) -> float | None:
    """Compute percentile from sorted (upper_bound_ms, count) raw histogram buckets."""
    clean = sorted((float(b), max(0, int(c))) for b, c in buckets if int(c) > 0)
    total = sum(c for _, c in clean)
    if total <= 0:
        return None
    target = (pct / 100.0) * total
    cum = 0
    for bound, cnt in clean:
        cum += cnt
        if cum >= target:
            return round(bound, 2)
    return round(clean[-1][0], 2)


def _git_sha(repo_dir: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True, timeout=3,
        )
        sha = res.stdout.strip()
        dirty_res = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain"],
            capture_output=True, text=True, check=False, timeout=3,
        )
        if dirty_res.returncode == 0 and dirty_res.stdout.strip():
            return f"{sha}-dirty"
        return sha
    except Exception:
        return "unknown"


def _find_worldserver_pid() -> str | None:
    try:
        for pid_str in os.listdir("/proc"):
            if not pid_str.isdigit():
                continue
            try:
                if Path(f"/proc/{pid_str}/comm").read_text(encoding="utf-8").strip() == "worldserver":
                    return pid_str
            except OSError:
                continue
    except OSError as exc:
        logger.debug("Unable to scan /proc for worldserver: %s", exc)
    return None


def _read_meminfo_mib() -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in ("MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"):
                out[parts[0].rstrip(":")] = round(int(parts[1]) / 1024.0, 1)
    except OSError as exc:
        logger.debug("Unable to read /proc/meminfo: %s", exc)
    if "SwapTotal" in out and "SwapFree" in out:
        out["SwapUsed"] = round(max(0.0, out["SwapTotal"] - out["SwapFree"]), 1)
    return out


def _read_vmstat_swap_io() -> dict[str, int]:
    """Read cumulative swap-in / swap-out page counters from /proc/vmstat when available."""
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/vmstat").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in ("pswpin", "pswpout"):
                out[parts[0]] = int(parts[1])
    except OSError as exc:
        logger.debug("Unable to read /proc/vmstat: %s", exc)
    return out


def _read_worldserver_pss_mib() -> float | None:
    """Read Proportional Set Size (PSS) for the worldserver process from /proc/<pid>/smaps_rollup."""
    pid_str = _find_worldserver_pid()
    if not pid_str:
        return None
    try:
        smaps = Path(f"/proc/{pid_str}/smaps_rollup")
        if not smaps.is_file():
            return None
        for line in smaps.read_text(encoding="utf-8").splitlines():
            if line.startswith("Pss:"):
                parts = line.split()
                if len(parts) >= 2:
                    return round(int(parts[1]) / 1024.0, 1)
    except OSError as exc:
        logger.debug("Unable to read worldserver PSS for pid %s: %s", pid_str, exc)
    return None


def _read_cgroup_limits() -> dict[str, Any]:
    """Read worldserver's cgroup v2 CPU/memory limits & counters via /proc/<pid>/cgroup when available."""
    out: dict[str, Any] = {}
    cg_dir = Path("/sys/fs/cgroup")
    pid_str = _find_worldserver_pid()
    if pid_str:
        try:
            for line in Path(f"/proc/{pid_str}/cgroup").read_text(encoding="utf-8").splitlines():
                if line.startswith("0::"):
                    rel = line.split("::", 1)[1].lstrip("/")
                    cand = Path("/sys/fs/cgroup") / rel
                    if cand.is_dir():
                        cg_dir = cand
                        out["cgroup_path"] = "/" + rel
                    break
        except OSError as exc:
            logger.debug("Unable to resolve worldserver cgroup: %s", exc)
    for key, fname in (
        ("memory_max", "memory.max"),
        ("memory_current", "memory.current"),
        ("cpu_max", "cpu.max"),
    ):
        p = cg_dir / fname
        try:
            if p.is_file():
                out[key] = p.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.debug("Unable to read %s: %s", p, exc)
    cpu_stat_p = cg_dir / "cpu.stat"
    try:
        if cpu_stat_p.is_file():
            for ln in cpu_stat_p.read_text(encoding="utf-8").splitlines():
                parts = ln.split()
                if len(parts) >= 2 and parts[0] in ("nr_throttled", "throttled_usec"):
                    out[f"cpu_{parts[0]}"] = int(parts[1])
    except OSError as exc:
        logger.debug("Unable to read %s: %s", cpu_stat_p, exc)
    return out


def _read_installed_config_snapshot(ac_dir: Path | None = None) -> dict[str, Any]:
    """Capture non-secret installed server/playerbots configuration parameters for benchmark comparability."""
    base = ac_dir or Path(
        os.getenv("HEARTHCORE_AC_DIR", str(Path.home() / "AzerothCore" / "azerothcore-wotlk"))
    )
    found_files: list[str] = []
    cfg: dict[str, Any] = {
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "config_files_found": found_files,
    }
    wanted_keys = {
        "AiPlayerbot.MinRandomBots",
        "AiPlayerbot.MaxRandomBots",
        "AiPlayerbot.CompanionLevelLead",
        "AiPlayerbot.RandomBotUpdateInterval",
        "MaxPlayerLevel",
        "UpdateUptimeInterval",
    }
    for rel in ("env/dist/etc/modules/playerbots.conf", "env/dist/etc/worldserver.conf"):
        p = base / rel
        if not p.is_file():
            continue
        found_files.append(p.name)
        try:
            for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = raw.split("#", 1)[0].strip()
                if not ln or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                ks = k.strip()
                if ks in wanted_keys:
                    cfg[ks] = v.strip().strip('"').strip("'")
        except OSError as exc:
            logger.debug("Unable to read config %s: %s", p, exc)
    return cfg


def _probe_live_group_and_instance_counts(
    *,
    brain_db_path: Path | None = None,
    sql_exec=None,
) -> dict[str, Any]:
    """Read real managed/pending cooperative groups from brain.db and active instances/SQL latency from MySQL.

    Never fabricates 0 when tables or DB executors are absent — returns None (unknown).
    """
    out: dict[str, Any] = {
        "managed_groups": None,
        "pending_groups": None,
        "active_instances": None,
        "sql_query_ms": None,
    }
    db_p = brain_db_path or Path(os.getenv("HEARTHCORE_RUNTIME_DIR", str(ROOT / "runtime"))) / "brain.db"
    if db_p.is_file():
        try:
            conn = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True, timeout=1.0)
            try:
                has_tbl = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunities'"
                ).fetchone()
                if has_tbl:
                    row = conn.execute(
                        "SELECT "
                        "SUM(CASE WHEN is_shadow=0 AND state IN ('active','running') THEN 1 ELSE 0 END), "
                        "SUM(CASE WHEN is_shadow=0 AND state IN ('recruiting','assembling','confirming') THEN 1 ELSE 0 END) "
                        "FROM opportunities"
                    ).fetchone()
                    out["managed_groups"] = int(row[0] or 0) if row else 0
                    out["pending_groups"] = int(row[1] or 0) if row else 0
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("Unable to query opportunities table in %s: %s", db_p, exc)

    if sql_exec is not None:
        t0 = time.monotonic()
        try:
            rows = sql_exec(
                "SELECT COUNT(DISTINCT instance_id) FROM characters WHERE online = 1 AND instance_id > 0;"
            )
            out["sql_query_ms"] = round((time.monotonic() - t0) * 1000.0, 2)
            if rows and rows[0] and rows[0][0] is not None:
                out["active_instances"] = int(rows[0][0])
        except Exception as exc:
            logger.debug("Live SQL instance probe failed: %s", exc)
    return out


ADMIT_ALLOWED_TICK_METHODS = frozenset({"raw_histogram", "deduplicated_series_samples"})


def benchmark_summary_for_admission(artifact: dict[str, Any], *, min_duration_sec: float = 600.0) -> dict[str, Any]:
    """Return a fail-closed admission summary contract for #12 admission and #17 RDF verification."""
    metrics = artifact.get("metrics") or {}
    workload = artifact.get("workload") or {}
    sampling = artifact.get("sampling") or {}
    correctness = artifact.get("correctness") or {}
    run_info = artifact.get("run") or {}

    # Recompute SHA-256 over canonical body to reject tampered artifacts
    stored_sha = str(artifact.get("artifact_sha256") or "")
    body_for_hash = {k: v for k, v in artifact.items() if k not in ("artifact_sha256", "saved_path")}
    recomputed_sha = hashlib.sha256(
        json.dumps(body_for_hash, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    digest_ok = bool(stored_sha) and stored_sha == recomputed_sha

    tick_method = str(sampling.get("tick_distribution_method") or "missing")
    duration_sec = run_info.get("duration_sec")
    reasons: list[str] = []
    if not digest_ok:
        reasons.append("artifact_digest_mismatch")
    if not artifact.get("all_correctness_passed"):
        reasons.append("correctness_failed")
    if artifact.get("telemetry_status") != "ok":
        reasons.append("telemetry_not_ok")
    if tick_method not in ADMIT_ALLOWED_TICK_METHODS:
        reasons.append(f"tick_method_unacceptable:{tick_method}")
    if not correctness.get("min_duration_met") or duration_sec is None or float(duration_sec) < float(min_duration_sec):
        reasons.append("min_duration_not_met")
    if metrics.get("tick_p99_ms") is None:
        reasons.append("missing_tick_p99")
    if metrics.get("mem_avail_mib_min") is None:
        reasons.append("missing_mem_avail")
    if workload.get("concurrent_managed_groups") is None:
        reasons.append("missing_managed_groups")
    if workload.get("active_instances_max") is None:
        reasons.append("missing_active_instances")
    if workload.get("active_ai_bots_max") is None:
        reasons.append("missing_active_ai_bots")

    return {
        "admit_ready": len(reasons) == 0,
        "reasons": reasons,
        "digest_verified": digest_ok,
        "telemetry_status": str(artifact.get("telemetry_status") or "unknown_missing_or_stale"),
        "tick_distribution_method": tick_method,
        "tick_p99_ms": metrics.get("tick_p99_ms"),
        "mem_avail_mib_min": metrics.get("mem_avail_mib_min"),
        "active_managed_groups": workload.get("concurrent_managed_groups"),
        "artifact_sha256": stored_sha,
    }


def _read_psi() -> dict[str, dict[str, float]]:
    """Read Linux Pressure Stall Information (/proc/pressure/{cpu,memory,io}) including cumulative total us."""
    psi: dict[str, dict[str, float]] = {}
    for res_name in ("cpu", "memory", "io"):
        p = Path(f"/proc/pressure/{res_name}")
        if not p.is_file():
            continue
        try:
            res_map: dict[str, float] = {}
            for line in p.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if not parts or parts[0] not in ("some", "full"):
                    continue
                kind = parts[0]
                for kv in parts[1:]:
                    if kv.startswith("avg10="):
                        res_map[f"{kind}_avg10"] = float(kv.split("=", 1)[1])
                    elif kv.startswith("avg60="):
                        res_map[f"{kind}_avg60"] = float(kv.split("=", 1)[1])
                    elif kv.startswith("total="):
                        res_map[f"{kind}_total_us"] = float(kv.split("=", 1)[1])
            if res_map:
                psi[res_name] = res_map
        except Exception as exc:
            logger.debug("Unable to parse /proc/pressure/%s: %s", res_name, exc)
            continue
    return psi


def build_benchmark_artifact(
    *,
    scenario: str,
    samples: list[dict[str, Any]],
    correctness: dict[str, bool],
    hearthcore_sha: str,
    mod_playerbots_sha: str,
    interval_sec: float = 3.0,
    warmup_sec: float = 0.0,
    tick_histogram_ms: list[tuple[float, int]] | None = None,
    config_snapshot: dict[str, Any] | None = None,
    run_info: dict[str, Any] | None = None,
    stale_after_sec: int = 180,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Assemble a canonical Phase II benchmark artifact with verifiable SHA-256 digest (Issue #47)."""
    ref_ts = int(now_ts if now_ts is not None else time.time())

    # 1. Tick distribution: prefer raw histogram -> deduplicated tick_series -> explicit sampled fallback
    dedup_series: dict[int, float] = {}
    for s in samples:
        for pt in s.get("tick_series") or []:
            if isinstance(pt, dict) and pt.get("t") is not None and pt.get("ms") is not None:
                dedup_series[int(pt["t"])] = float(pt["ms"])

    tick_method = "missing"
    tick_sample_count = 0
    tick_p50 = tick_p95 = tick_p99 = tick_max = None
    stale_telemetry = False

    if tick_histogram_ms:
        valid_buckets = [(float(b), int(c)) for b, c in tick_histogram_ms if int(c) > 0]
        if valid_buckets:
            tick_method = "raw_histogram"
            tick_sample_count = sum(c for _, c in valid_buckets)
            tick_p50 = percentile_from_histogram(valid_buckets, 50.0)
            tick_p95 = percentile_from_histogram(valid_buckets, 95.0)
            tick_p99 = percentile_from_histogram(valid_buckets, 99.0)
            tick_max = round(max(b for b, _ in valid_buckets), 2)
    elif dedup_series:
        latest_pt_ts = max(dedup_series.keys())
        if (ref_ts - latest_pt_ts) > int(stale_after_sec):
            stale_telemetry = True
        else:
            vals = sorted(dedup_series.values())
            tick_method = "deduplicated_series_samples"
            tick_sample_count = len(vals)
            tick_p50 = _percentile(vals, 50.0)
            tick_p95 = _percentile(vals, 95.0)
            tick_p99 = _percentile(vals, 99.0)
            tick_max = round(max(vals), 2)
    else:
        ticks = sorted(float(s["tick_latest"]) for s in samples if s.get("tick_latest") is not None)
        p99s = sorted(float(s["tick_p99"]) for s in samples if s.get("tick_p99") is not None)
        if ticks:
            tick_method = "sampled_window_fallback"
            tick_sample_count = len(ticks)
            tick_p50 = _percentile(ticks, 50.0)
            tick_p95 = _percentile(ticks, 95.0)
            tick_p99 = max(p99s) if p99s else _percentile(ticks, 99.0)
            tick_max = round(max(ticks), 2)

    telemetry_complete = bool(samples) and (tick_p50 is not None) and (not stale_telemetry)
    telemetry_status = "ok" if telemetry_complete else "unknown_missing_or_stale"

    # 2. Honest workload attribution (humans vs companions vs random bots vs active AI vs groups/instances)
    humans_vals = [
        int(s["connected_humans"])
        if s.get("connected_humans") is not None
        else (int(s["humans_online"]) if s.get("humans_online") is not None else None)
        for s in samples
    ]
    humans_present = [v for v in humans_vals if v is not None]
    comp_vals = [int(s["companions_online"]) for s in samples if s.get("companions_online") is not None]
    rnd_vals = [int(s["random_bots_online"]) for s in samples if s.get("random_bots_online") is not None]
    logged_in_bots: list[int] = []
    for s in samples:
        if s.get("total_logged_in_bots") is not None:
            logged_in_bots.append(int(s["total_logged_in_bots"]))
        elif s.get("companions_online") is not None or s.get("random_bots_online") is not None:
            logged_in_bots.append(int(s.get("companions_online") or 0) + int(s.get("random_bots_online") or 0))
        elif s.get("total_online") is not None:
            logged_in_bots.append(max(0, int(s["total_online"]) - int(s.get("connected_humans") or 0)))
    active_ai_vals = [
        int(s["active_ai_bots"])
        for s in samples
        if s.get("active_ai_bots") is not None
    ]
    managed_vals = [int(s["managed_groups"]) for s in samples if s.get("managed_groups") is not None]
    pending_vals = [int(s["pending_groups"]) for s in samples if s.get("pending_groups") is not None]
    instance_vals = [int(s["active_instances"]) for s in samples if s.get("active_instances") is not None]

    rss_vals = [float(s["worldserver_rss_mib"]) for s in samples if s.get("worldserver_rss_mib") is not None]
    pss_vals = [float(s["worldserver_pss_mib"]) for s in samples if s.get("worldserver_pss_mib") is not None]
    cpu_vals = [float(s["worldserver_cpu_pct"]) for s in samples if s.get("worldserver_cpu_pct") is not None]
    mem_avail_vals = [float(s["mem_avail_mib"]) for s in samples if s.get("mem_avail_mib") is not None]
    swap_vals = [float(s["swap_used_mib"]) for s in samples if s.get("swap_used_mib") is not None]
    pswpin_vals = [int(s["swap_in_pages"]) for s in samples if s.get("swap_in_pages") is not None]
    pswpout_vals = [int(s["swap_out_pages"]) for s in samples if s.get("swap_out_pages") is not None]
    sql_ms_vals = [float(s["sql_query_ms"]) for s in samples if s.get("sql_query_ms") is not None]
    poll_ms_vals = [float(s["dashboard_poll_ms"]) for s in samples if s.get("dashboard_poll_ms") is not None]

    # Compute cumulative PSI stall deltas across first -> last sample when available
    psi_deltas: dict[str, dict[str, float]] = {}
    if len(samples) >= 2 and isinstance(samples[0].get("psi"), dict) and isinstance(samples[-1].get("psi"), dict):
        p_first = samples[0]["psi"]
        p_last = samples[-1]["psi"]
        for rname in ("cpu", "memory", "io"):
            if isinstance(p_first.get(rname), dict) and isinstance(p_last.get(rname), dict):
                dmap: dict[str, float] = {}
                for k in ("some_total_us", "full_total_us"):
                    if k in p_first[rname] and k in p_last[rname]:
                        dmap[f"{k}_delta"] = round(float(p_last[rname][k]) - float(p_first[rname][k]), 1)
                if dmap:
                    psi_deltas[rname] = dmap

    merged_correctness = dict(correctness)
    merged_correctness["telemetry_complete"] = telemetry_complete

    body: dict[str, Any] = {
        "schema_version": BENCH_SCHEMA_VERSION,
        "generated_at": datetime.fromtimestamp(ref_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scenario": scenario,
        "telemetry_status": telemetry_status,
        "builds": {
            "hearthcore_sha": hearthcore_sha,
            "mod_playerbots_sha": mod_playerbots_sha,
            "config": config_snapshot or {},
        },
        "sampling": {
            "sample_count": len(samples),
            "interval_sec": interval_sec,
            "warmup_sec": warmup_sec,
            "tick_distribution_method": tick_method,
            "tick_sample_count": tick_sample_count,
        },
        "workload": {
            "connected_humans_max": max(humans_present) if humans_present else None,
            "companions_online_max": max(comp_vals) if comp_vals else None,
            "random_bots_online_max": max(rnd_vals) if rnd_vals else None,
            "logged_in_bots_min": min(logged_in_bots) if logged_in_bots else None,
            "logged_in_bots_max": max(logged_in_bots) if logged_in_bots else None,
            "active_ai_bots_max": max(active_ai_vals) if active_ai_vals else None,
            "concurrent_managed_groups": max(managed_vals) if managed_vals else None,
            "pending_groups_max": max(pending_vals) if pending_vals else None,
            "active_instances_max": max(instance_vals) if instance_vals else None,
        },
        "metrics": {
            "tick_p50_ms": tick_p50,
            "tick_p95_ms": tick_p95,
            "tick_p99_ms": tick_p99,
            "tick_max_ms": tick_max,
            "worldserver_rss_mib_mean": round(sum(rss_vals) / len(rss_vals), 1) if rss_vals else None,
            "worldserver_pss_mib_mean": round(sum(pss_vals) / len(pss_vals), 1) if pss_vals else None,
            "worldserver_cpu_pct_mean": round(sum(cpu_vals) / len(cpu_vals), 1) if cpu_vals else None,
            "mem_avail_mib_min": round(min(mem_avail_vals), 1) if mem_avail_vals else None,
            "swap_delta_mib": round(swap_vals[-1] - swap_vals[0], 1) if len(swap_vals) >= 2 else None,
            "swap_in_pages_delta": (pswpin_vals[-1] - pswpin_vals[0]) if len(pswpin_vals) >= 2 else None,
            "swap_out_pages_delta": (pswpout_vals[-1] - pswpout_vals[0]) if len(pswpout_vals) >= 2 else None,
            "sql_query_ms_mean": round(sum(sql_ms_vals) / len(sql_ms_vals), 2) if sql_ms_vals else None,
            "dashboard_poll_ms_mean": round(sum(poll_ms_vals) / len(poll_ms_vals), 2) if poll_ms_vals else None,
            "psi": samples[-1].get("psi") if samples and samples[-1].get("psi") else {},
            "psi_delta": psi_deltas,
            "cgroup": samples[-1].get("cgroup") if samples and samples[-1].get("cgroup") else {},
        },
        "correctness": merged_correctness,
        "all_correctness_passed": telemetry_complete and all(bool(v) for v in merged_correctness.values()),
    }
    if run_info is not None:
        body["run"] = run_info
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    body["artifact_sha256"] = digest
    return body


def run_live_baseline_benchmark(
    *,
    scenario: str = "baseline_outdoor_solo",
    sample_count: int = 3,
    interval_sec: float = 2.0,
    warmup_sec: float = 0.0,
    min_duration_sec: float = 600.0,
    dashboard_url: str | None = None,
    out_dir: Path | None = None,
    brain_db_path: Path | None = None,
    sql_exec=None,
    max_data_age_sec: float = 60.0,
) -> dict[str, Any]:
    """Capture a non-destructive live baseline artifact from Azeroth Watch, /proc & SQL/brain.db.

    Fails closed: missing or stale fields are unknown (never fabricated as 0), errors require >=2
    samples with zero mid-run change, and the run must span at least min_duration_sec.
    """
    url = dashboard_url or os.getenv("AZEROTH_WATCH_URL", "http://127.0.0.1:8787/api/state")
    if warmup_sec > 0:
        time.sleep(warmup_sec)

    samples: list[dict[str, Any]] = []
    world_up = True
    telemetry_present = True
    errors_first: int | None = None
    errors_last: int | None = None
    errors_prev: int | None = None
    errors_samples_seen = 0
    errors_missing = False
    errors_changed = False
    cgroup_limits = _read_cgroup_limits()
    run_start = time.monotonic()

    for i in range(max(1, int(sample_count))):
        t0 = time.monotonic()
        with urllib.request.urlopen(url, timeout=5) as r:
            st = json.load(r)
        poll_ms = round((time.monotonic() - t0) * 1000.0, 2)
        mem = _read_meminfo_mib()
        psi = _read_psi()
        swap_io = _read_vmstat_swap_io()
        pss_mib = _read_worldserver_pss_mib()
        live_counts = _probe_live_group_and_instance_counts(brain_db_path=brain_db_path, sql_exec=sql_exec)

        v = st.get("vitals") if isinstance(st.get("vitals"), dict) else None
        t = st.get("ticks") if isinstance(st.get("ticks"), dict) else None
        pop = st.get("population") if isinstance(st.get("population"), dict) else None
        if v is None or t is None or pop is None or "errors_bytes" not in st:
            telemetry_present = False
        if st.get("db_ok") is not True:
            telemetry_present = False
        raw_age = st.get("data_age_sec")
        if raw_age is None:
            telemetry_present = False
        else:
            try:
                if float(raw_age) > float(max_data_age_sec):
                    telemetry_present = False
            except (TypeError, ValueError):
                telemetry_present = False

        if t is None or (not t.get("series") and t.get("latest") is None and t.get("p99") is None):
            telemetry_present = False
        if pop is None or any(pop.get(k) is None for k in ("humans", "companions_online", "random_online", "total_online")):
            telemetry_present = False

        raw_err = st.get("errors_bytes")
        if raw_err is None:
            telemetry_present = False
            errors_missing = True
        else:
            cur_err = int(raw_err)
            errors_samples_seen += 1
            if errors_first is None:
                errors_first = cur_err
            if errors_prev is not None and cur_err != errors_prev:
                # Detects both error growth AND log rotation + regrowth!
                errors_changed = True
            errors_prev = cur_err
            errors_last = cur_err
        if not v or not v.get("worldserver_up"):
            world_up = False

        # Do NOT conflate logged-in bots with active AI unless explicitly reported by telemetry
        explicit_active_ai = pop.get("active_ai_bots") if pop else None
        samples.append({
            "ts": int(time.time()),
            "connected_humans": int(pop["humans"]) if (pop and pop.get("humans") is not None) else None,
            "companions_online": int(pop["companions_online"]) if (pop and pop.get("companions_online") is not None) else None,
            "random_bots_online": int(pop["random_online"]) if (pop and pop.get("random_online") is not None) else None,
            "total_online": int(pop["total_online"]) if (pop and pop.get("total_online") is not None) else None,
            "active_ai_bots": int(explicit_active_ai) if explicit_active_ai is not None else None,
            "tick_latest": t.get("latest") if t else None,
            "tick_p99": t.get("p99") if t else None,
            "tick_series": (t.get("series") or []) if t else [],
            "worldserver_rss_mib": v.get("worldserver_rss_mib") if v else None,
            "worldserver_pss_mib": pss_mib,
            "worldserver_cpu_pct": v.get("worldserver_cpu_pct") if v else None,
            "mem_avail_mib": mem.get("MemAvailable"),
            "swap_used_mib": mem.get("SwapUsed"),
            "swap_in_pages": swap_io.get("pswpin"),
            "swap_out_pages": swap_io.get("pswpout"),
            "psi": psi,
            "cgroup": cgroup_limits,
            "dashboard_poll_ms": poll_ms,
            "sql_query_ms": live_counts["sql_query_ms"],
            "managed_groups": live_counts["managed_groups"],
            "pending_groups": live_counts["pending_groups"],
            "active_instances": live_counts["active_instances"],
        })
        if i + 1 < sample_count:
            time.sleep(interval_sec)

    ac_dir = Path(
        os.getenv("HEARTHCORE_AC_DIR", str(Path.home() / "AzerothCore" / "azerothcore-wotlk"))
    )
    mpb_dir = Path(
        os.getenv(
            "HEARTHCORE_MOD_PLAYERBOTS_DIR",
            str(ac_dir / "modules" / "mod-playerbots"),
        )
    )
    cfg_snap = _read_installed_config_snapshot(ac_dir)
    cfg_snap["azerothcore_sha"] = _git_sha(ac_dir)

    # Error growth requires >=2 samples with errors_bytes and zero mid-run change (rotation/regrowth -> None delta & fail!)
    if errors_missing or errors_samples_seen < 2 or errors_first is None or errors_last is None:
        errors_delta: int | None = None
        errors_no_growth = False
    elif errors_changed:
        errors_delta = (errors_last - errors_first) if errors_last != errors_first else None
        errors_no_growth = False
    else:
        errors_delta = errors_last - errors_first
        errors_no_growth = (errors_delta == 0)

    duration_sec = round(time.monotonic() - run_start, 2)
    correctness_map: dict[str, bool] = {
        "worldserver_up": world_up,
        "errors_log_no_growth": errors_no_growth,
        "errors_log_zero_bytes": errors_no_growth,  # backwards-compatible alias
        "telemetry_present": telemetry_present,
        "min_duration_met": duration_sec >= float(min_duration_sec),
    }
    if sql_exec is not None:
        correctness_map["sql_telemetry_present"] = any(s.get("sql_query_ms") is not None for s in samples)

    artifact = build_benchmark_artifact(
        scenario=scenario,
        samples=samples,
        correctness=correctness_map,
        hearthcore_sha=_git_sha(ROOT),
        mod_playerbots_sha=_git_sha(mpb_dir),
        config_snapshot=cfg_snap,
        interval_sec=interval_sec,
        warmup_sec=warmup_sec,
        run_info={
            "duration_sec": duration_sec,
            "min_duration_sec": float(min_duration_sec),
            "errors_bytes_first": errors_first,
            "errors_bytes_last": errors_last,
            "errors_bytes_delta": errors_delta,
            "errors_bytes_missing": errors_missing,
            "errors_bytes_changed_mid_run": errors_changed,
            "warmup_sec": float(warmup_sec),
        },
    )
    target_dir = out_dir or (ROOT / "runtime" / "benchmarks")
    target_dir.mkdir(parents=True, exist_ok=True)
    fname = f"bench_{scenario}_{int(time.time())}.json"
    out_path = target_dir / fname
    out_path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    artifact["saved_path"] = str(out_path)
    return artifact
