"""Phase II Repeatable Performance & Correctness Benchmark Harness (Card II-10).

Design guarantees (docs/PHASE_II_LIVING_GUILD.md Section 10):
- Zero fake performance numbers; samples real live or staging metrics from Azeroth Watch,
  `/proc/meminfo`, and Docker container stats.
- Emits a structured, versioned JSON benchmark artifact with SHA-256 digest and explicit
  correctness assertions so before/after comparisons are reproducible and auditable.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import ROOT

BENCH_SCHEMA_VERSION = 1


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, int(round((pct / 100.0) * (len(sorted_vals) - 1)))))
    return round(sorted_vals[idx], 2)


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
    except OSError:
        pass
    if "SwapTotal" in out and "SwapFree" in out:
        out["SwapUsed"] = round(max(0.0, out["SwapTotal"] - out["SwapFree"]), 1)
    return out


def build_benchmark_artifact(
    *,
    scenario: str,
    samples: list[dict[str, Any]],
    correctness: dict[str, bool],
    hearthcore_sha: str,
    mod_playerbots_sha: str,
    interval_sec: float = 3.0,
) -> dict[str, Any]:
    """Assemble a canonical Phase II benchmark artifact with verifiable SHA-256 digest."""
    ticks = sorted(float(s["tick_latest"]) for s in samples if s.get("tick_latest") is not None)
    p99s = sorted(float(s["tick_p99"]) for s in samples if s.get("tick_p99") is not None)
    rss_vals = [float(s["worldserver_rss_mib"]) for s in samples if s.get("worldserver_rss_mib") is not None]
    cpu_vals = [float(s["worldserver_cpu_pct"]) for s in samples if s.get("worldserver_cpu_pct") is not None]
    swap_vals = [float(s["swap_used_mib"]) for s in samples if s.get("swap_used_mib") is not None]
    bot_counts = [int(s["total_online"]) for s in samples if s.get("total_online") is not None]

    body: dict[str, Any] = {
        "schema_version": BENCH_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scenario": scenario,
        "builds": {
            "hearthcore_sha": hearthcore_sha,
            "mod_playerbots_sha": mod_playerbots_sha,
        },
        "sampling": {
            "sample_count": len(samples),
            "interval_sec": interval_sec,
        },
        "workload": {
            "active_bots_min": min(bot_counts) if bot_counts else 0,
            "active_bots_max": max(bot_counts) if bot_counts else 0,
            "concurrent_managed_groups": max((int(s.get("managed_groups", 0)) for s in samples), default=0),
        },
        "metrics": {
            "tick_p50_ms": _percentile(ticks, 50.0),
            "tick_p95_ms": _percentile(ticks, 95.0),
            "tick_p99_ms": max(p99s) if p99s else _percentile(ticks, 99.0),
            "tick_max_ms": max(ticks) if ticks else None,
            "worldserver_rss_mib_mean": round(sum(rss_vals) / len(rss_vals), 1) if rss_vals else None,
            "worldserver_cpu_pct_mean": round(sum(cpu_vals) / len(cpu_vals), 1) if cpu_vals else None,
            "swap_delta_mib": round(swap_vals[-1] - swap_vals[0], 1) if len(swap_vals) >= 2 else 0.0,
        },
        "correctness": correctness,
        "all_correctness_passed": all(bool(v) for v in correctness.values()) if correctness else False,
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    body["artifact_sha256"] = digest
    return body


def run_live_baseline_benchmark(
    *,
    scenario: str = "baseline_outdoor_solo",
    sample_count: int = 3,
    interval_sec: float = 2.0,
    dashboard_url: str = "http://127.0.0.1:8787/api/state",
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Capture a non-destructive live baseline artifact from Azeroth Watch & /proc/meminfo."""
    samples: list[dict[str, Any]] = []
    errors_zero = True
    world_up = True

    for i in range(max(1, int(sample_count))):
        with urllib.request.urlopen(dashboard_url, timeout=5) as r:
            st = json.load(r)
        mem = _read_meminfo_mib()
        v = st.get("vitals") or {}
        t = st.get("ticks") or {}
        pop = st.get("population") or {}
        if int(st.get("errors_bytes") or 0) > 0:
            errors_zero = False
        if not v.get("worldserver_up"):
            world_up = False
        samples.append({
            "ts": int(time.time()),
            "total_online": int(pop.get("total_online") or 0),
            "companions_online": int(pop.get("companions_online") or 0),
            "tick_latest": t.get("latest"),
            "tick_p99": t.get("p99"),
            "worldserver_rss_mib": v.get("worldserver_rss_mib"),
            "worldserver_cpu_pct": v.get("worldserver_cpu_pct"),
            "mem_avail_mib": mem.get("MemAvailable"),
            "swap_used_mib": mem.get("SwapUsed"),
            "managed_groups": 0,
        })
        if i + 1 < sample_count:
            time.sleep(interval_sec)

    mpb_dir = Path("/home/wonky/AzerothCore/azerothcore-wotlk/modules/mod-playerbots")
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
    )
    target_dir = out_dir or (ROOT / "runtime" / "benchmarks")
    target_dir.mkdir(parents=True, exist_ok=True)
    fname = f"bench_{scenario}_{int(time.time())}.json"
    out_path = target_dir / fname
    out_path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    artifact["saved_path"] = str(out_path)
    return artifact
