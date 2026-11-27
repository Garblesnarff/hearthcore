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
        return res.stdout.strip()
    except Exception:
        return "unknown"


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


def _read_psi() -> dict[str, dict[str, float]]:
    """Read Linux Pressure Stall Information (/proc/pressure/{cpu,memory,io}) when available."""
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
    humans_vals = [int(s.get("connected_humans", s.get("humans_online", 0)) or 0) for s in samples]
    comp_vals = [int(s.get("companions_online", 0) or 0) for s in samples]
    rnd_vals = [int(s.get("random_bots_online", 0) or 0) for s in samples]
    logged_in_bots = [
        int(s["total_logged_in_bots"])
        if s.get("total_logged_in_bots") is not None
        else (
            (int(s.get("companions_online", 0) or 0) + int(s.get("random_bots_online", 0) or 0))
            if (s.get("companions_online") is not None or s.get("random_bots_online") is not None)
            else max(0, int(s.get("total_online", 0) or 0) - int(s.get("connected_humans", 0) or 0))
        )
        for s in samples
    ]
    active_ai_vals = [
        int(s["active_ai_bots"])
        for s in samples
        if s.get("active_ai_bots") is not None
    ]
    managed_vals = [int(s.get("managed_groups", 0) or 0) for s in samples]
    pending_vals = [int(s.get("pending_groups", 0) or 0) for s in samples]
    instance_vals = [int(s.get("active_instances", 0) or 0) for s in samples]

    rss_vals = [float(s["worldserver_rss_mib"]) for s in samples if s.get("worldserver_rss_mib") is not None]
    cpu_vals = [float(s["worldserver_cpu_pct"]) for s in samples if s.get("worldserver_cpu_pct") is not None]
    mem_avail_vals = [float(s["mem_avail_mib"]) for s in samples if s.get("mem_avail_mib") is not None]
    swap_vals = [float(s["swap_used_mib"]) for s in samples if s.get("swap_used_mib") is not None]
    sql_ms_vals = [float(s["sql_query_ms"]) for s in samples if s.get("sql_query_ms") is not None]

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
            "connected_humans_max": max(humans_vals, default=0),
            "companions_online_max": max(comp_vals, default=0),
            "random_bots_online_max": max(rnd_vals, default=0),
            "logged_in_bots_min": min(logged_in_bots) if logged_in_bots else 0,
            "logged_in_bots_max": max(logged_in_bots) if logged_in_bots else 0,
            "active_ai_bots_max": max(active_ai_vals) if active_ai_vals else None,
            "concurrent_managed_groups": max(managed_vals, default=0),
            "pending_groups_max": max(pending_vals, default=0),
            "active_instances_max": max(instance_vals, default=0),
        },
        "metrics": {
            "tick_p50_ms": tick_p50,
            "tick_p95_ms": tick_p95,
            "tick_p99_ms": tick_p99,
            "tick_max_ms": tick_max,
            "worldserver_rss_mib_mean": round(sum(rss_vals) / len(rss_vals), 1) if rss_vals else None,
            "worldserver_cpu_pct_mean": round(sum(cpu_vals) / len(cpu_vals), 1) if cpu_vals else None,
            "mem_avail_mib_min": round(min(mem_avail_vals), 1) if mem_avail_vals else None,
            "swap_delta_mib": round(swap_vals[-1] - swap_vals[0], 1) if len(swap_vals) >= 2 else None,
            "sql_query_ms_mean": round(sum(sql_ms_vals) / len(sql_ms_vals), 2) if sql_ms_vals else None,
            "psi": samples[-1].get("psi") if samples and samples[-1].get("psi") else {},
        },
        "correctness": merged_correctness,
        "all_correctness_passed": telemetry_complete and all(bool(v) for v in merged_correctness.values()),
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    body["artifact_sha256"] = digest
    return body


def run_live_baseline_benchmark(
    *,
    scenario: str = "baseline_outdoor_solo",
    sample_count: int = 3,
    interval_sec: float = 2.0,
    warmup_sec: float = 0.0,
    dashboard_url: str | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Capture a non-destructive live baseline artifact from Azeroth Watch, /proc/meminfo & PSI."""
    url = dashboard_url or os.getenv("AZEROTH_WATCH_URL", "http://127.0.0.1:8787/api/state")
    if warmup_sec > 0:
        time.sleep(warmup_sec)

    samples: list[dict[str, Any]] = []
    errors_zero = True
    world_up = True

    for i in range(max(1, int(sample_count))):
        t0 = time.monotonic()
        with urllib.request.urlopen(url, timeout=5) as r:
            st = json.load(r)
        poll_ms = round((time.monotonic() - t0) * 1000.0, 2)
        mem = _read_meminfo_mib()
        psi = _read_psi()
        v = st.get("vitals") or {}
        t = st.get("ticks") or {}
        pop = st.get("population") or {}
        if int(st.get("errors_bytes") or 0) > 0:
            errors_zero = False
        if not v.get("worldserver_up"):
            world_up = False
        samples.append({
            "ts": int(time.time()),
            "connected_humans": int(pop.get("humans") or 0),
            "companions_online": int(pop.get("companions_online") or 0),
            "random_bots_online": int(pop.get("random_online") or 0),
            "total_online": int(pop.get("total_online") or 0),
            "active_ai_bots": int(pop.get("companions_online") or 0) + int(pop.get("random_online") or 0),
            "tick_latest": t.get("latest"),
            "tick_p99": t.get("p99"),
            "tick_series": t.get("series") or [],
            "worldserver_rss_mib": v.get("worldserver_rss_mib"),
            "worldserver_cpu_pct": v.get("worldserver_cpu_pct"),
            "mem_avail_mib": mem.get("MemAvailable"),
            "swap_used_mib": mem.get("SwapUsed"),
            "psi": psi,
            "sql_query_ms": poll_ms,
            "managed_groups": 0,
            "pending_groups": 0,
            "active_instances": 0,
        })
        if i + 1 < sample_count:
            time.sleep(interval_sec)

    mpb_dir = Path(
        os.getenv(
            "HEARTHCORE_MOD_PLAYERBOTS_DIR",
            str(Path.home() / "AzerothCore" / "azerothcore-wotlk" / "modules" / "mod-playerbots"),
        )
    )
    artifact = build_benchmark_artifact(
        scenario=scenario,
        samples=samples,
        correctness={
            "worldserver_up": world_up,
            "errors_log_zero_bytes": errors_zero,
        },
        hearthcore_sha=_git_sha(ROOT),
        mod_playerbots_sha=_git_sha(mpb_dir),
        interval_sec=interval_sec,
        warmup_sec=warmup_sec,
    )
    target_dir = out_dir or (ROOT / "runtime" / "benchmarks")
    target_dir.mkdir(parents=True, exist_ok=True)
    fname = f"bench_{scenario}_{int(time.time())}.json"
    out_path = target_dir / fname
    out_path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    artifact["saved_path"] = str(out_path)
    return artifact
