# Phase II Cooperative Staging Pressure & Admission Thresholds

> **Target Host:** Acer Aspire TC-780 — Intel Core i5-7400 (4C/4T), 8 GiB RAM, Ubuntu Linux, Docker `ac-worldserver` + MySQL 8.0.
> **Live Safety Default:** `AiPlayerbot.Companion.Coop.Enabled = 0` and `HEARTHCORE_COOP_ENABLED = 0` until staging acceptance passes and the owner explicitly approves enablement.

## 1. Authoritative Admission Thresholds & Hysteresis Bands (Issue #12)

Both the native C++17 admission controller (`CompanionCoopAdmissionLogic.h` / `CompanionBrain.cpp`) and the Python admission gate (`hearthcore/opportunities.py`) enforce identical fail-closed thresholds with persisted hysteresis.

| Metric | Source | Pause / Block New Admits | Recover / Resume Admits | On Missing / Stale / NaN |
| --- | --- | --- | --- | --- |
| **Available RAM** | `/proc/meminfo` (`MemAvailable`) | `< 512 MiB` | `>= 640 MiB` | **Fail CLOSED** |
| **World Tick Tail Latency (p99)** | World-thread tick ring buffer (`>= 20` samples) / `/api/state` histogram | `> 45.0 ms` | `<= 38.0 ms` | **Fail CLOSED** |
| **Swap I/O Rate** | `/proc/vmstat` delta `(pswpin + pswpout) / dt` | `> 25.0 pages/s` | `<= 10.0 pages/s` | **Fail CLOSED** |
| **CPU Contention (Linux PSI)** | `/proc/pressure/cpu` (`some avg10`) | `> 20.0 %` | `<= 10.0 %` | **Fail CLOSED** |
| **CPU cgroup Throttling** | Python benchmark/admission when `cpu.stat` supplied (native uses Linux PSI) | `> 15.0 %` | `<= 5.0 %` | **Fail CLOSED** (if invalid/NaN) |
| **Telemetry Freshness** | Sample Unix timestamp vs current clock | Age `> 30 s` (native) / `> 300 s` (Python) or future clock skew | `0 <= age <= max_age` | **Fail CLOSED** |
| **Persisted Hysteresis State** | Shared JSON schema (`companion_coop_headroom.json` / `headroom_state.json`) | Persisted on headroom pause; corrupt existing file fails closed (`paused=true`) | Clears only when all headroom metrics enter recover band | **Fail CLOSED** |
| **Managed Cooperative Groups** | Committed active groups + pending uncommitted proposals | `>= 1` (`AiPlayerbot.Companion.Coop.MaxManagedGroups`, start at **1**) | `< max_managed_groups` | **Fail CLOSED** |
| **Total Active + Pending Instances** | `sMapMgr->GetNumInstances(dungeons, bgs, arenas)` + pending proposals | `>= 4` (`AiPlayerbot.Companion.Coop.MaxTotalInstances`, includes human/ordinary instances) | `< max_total_instances` | **Fail CLOSED** |
| **Online Population Cap** | `sWorldSessionMgr->GetPlayerCount()` | `>= 45` (`AiPlayerbot.Companion.Coop.MaxOnlinePopulation`) | `< max_online_population` | **Fail CLOSED** |
| **Companion Login Stagger** | `CompanionBotMgr::LoginMissingCompanions()` | `AiPlayerbot.Companion.MaxLoginsPerPass` (`0` = unlimited when coop OFF; defaults to `2` per 30 s pass when coop ON); no forced mass logins (`additionalLoginsNeeded == 0`) | No forced mass logins for coop admission | **Fail CLOSED** |

## 2. Non-Negotiable Safety Invariants

1. **Guards New Admissions Only:** Headroom pause and cap gates reject **new** automated queue admissions and proposal acceptances. Existing human gameplay, active combat, and already-committed cooperative groups continue safely without interruption or emergency combat logout.
2. **Atomic Serialized Resource Tokens:** Native admission mints a bounded-TTL token (`DEFAULT_TOKEN_TTL_SEC = 180s`) under `gAdmissionMutex` on the world thread; Python admission enforces group/instance/population caps inside SQLite `BEGIN IMMEDIATE` transactions plus `fcntl.flock`-serialized `headroom_state.json`. Concurrent proposals can never race past configured caps.
3. **Explicit Allowlists Only:** Both `AiPlayerbot.Companion.Coop.Enabled = 1` AND an explicit map allowlist (`AiPlayerbot.Companion.Coop.EnabledDungeonMaps`, default empty `""`) are required; unsupported or disabled dungeons always fail closed.
4. **No Emergency Combat Logout:** `CompanionBotMgr::LoginMissingCompanions()` never logs out a companion that is in combat, inside an instance, grouped with an owner, or in an authorized cooperative group.

## 3. Staging Pressure Test Procedure (Real Comparable Workload)

Before flipping `Coop.Enabled = 1` on a live server, run a comparable staging workload via `hearthcore.benchmark`:

```bash
# 1. Baseline (0 managed coop groups, normal human + guild companion population, 15 min)
python3 -m hearthcore benchmark --label staging-baseline --duration 900 --interval 15

# 2. Pilot workload (1 managed 5-player dungeon group + existing human/companion population, 15-30 min)
python3 -m hearthcore benchmark --label staging-1group-pilot --duration 1200 --interval 15
```

### Acceptance Gate
- `all_correctness_passed == true` (zero missing/stale telemetry, zero error-log growth).
- `tick_p99_ms <= 45.0 ms` throughout the run (zero hysteresis pause triggers under 1-group steady state).
- `mem_avail_mib >= 512.0 MiB` minimum sample and `swap_io_pages_per_sec <= 25.0 pages/s`.
- Human player group + dungeon entry continues with zero forced disconnects or bot combat logouts.
- Only after 1-group staging passes cleanly may `MaxManagedGroups` ever be raised above `1`.
