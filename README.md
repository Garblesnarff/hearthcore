# Hearthcore — Hearthguard Full-Stack Mono-Repo

Character-first AI planning, persistent companion memory, guild supply logistics, live observability dashboard, and AzerothCore `mod-playerbots` companion C++ source for our private WotLK 3.3.5a guild (**Hearthguard**).

> **Public-Safe Mono-Repo:** This repository contains all code, 38 reviewed companion profiles, C++ companion modules, dashboard code, operational scripts, and architectural docs so collaborators and external AI assistants can see the complete live stack. Live secrets (`.env`), runtime SQLite databases (`runtime/`), MySQL dumps, and copyrighted Blizzard MPQ assets (`.png`/`.jpg`) are strictly excluded.

---

## Mono-Repo Map

| Path | Description |
|---|---|
| [`hearthcore/`](./hearthcore/) | **Live Python AI Brain & Guild Quartermaster** (Python 3.11+ stdlib only):<br>• `core.py` — strict allowlist validator & deterministic fallback<br>• `provider.py` — Anthropic Messages API (`claude-sonnet-5-5`, `effort=medium`)<br>• `bridge.py` — live MySQL exporter, 15m session-boundary planner, bounded `0.5–1.5` C++ weight importer, and real session outcome tracker<br>• `memory.py` — **Memory v1** (factual milestones, DB-reconciled open intentions, session variety guard, scoped stall caution + operational alerts, social bonds, and soft work-order reservations)<br>• `guides.py` & `quartermaster.py` — 40-character WotLK Classic `1–300` profession supply calculator + hourly `claude-sonnet-5-5` Guild Quartermaster |
| [`characters/`](./characters/) | 38 reviewed Hearthguard companion identity profiles (`characters/<slug>/profile.json`). |
| [`azeroth-watch/`](./azeroth-watch/) | Zero-dependency Python SSE/HTTP live dashboard (port `8787`): Live Maps, Guild Vault & Professions, Raid Roster, and the human-readable **AI Brain Control Center** (credit balance, Quartermaster deficits, Memory v1 intentions/stall alerts, LAN controls). |
| [`mod-playerbots-companions/`](./mod-playerbots-companions/) | GPLv2 C++ source mirror of our custom AzerothCore `mod-playerbots` companion subsystem (`CompanionBotMgr`, `CompanionBrain`, `CompanionCraft`, `CompanionDuty`, `CompanionLife`, `CompanionQuest`, etc. + danger-death revive & `.companion` commands). |
| [`ops/`](./ops/) | Operational scripts (`perf-check.sh`, `heal-realmlist.sh`, `hearthcore-sync.py`, `sync-monorepo.sh`), LAN WoW 3.3.5a laptop setup streamer (`wow-lan-share/`), and `systemd --user` service/timer units (`ops/systemd/`). |
| [`docs/`](./docs/) | Architecture & design docs:<br>• [`SERVER_STATE.md`](./docs/SERVER_STATE.md) — **M0 Installed-System Architecture & DB Schema Inventory**<br>• [`ARCHITECTURE.md`](./docs/ARCHITECTURE.md) — bridge & safety invariants<br>• [`PERSISTENT_MEMORY.md`](./docs/PERSISTENT_MEMORY.md) & [`MEMORY_IMPLEMENTATION.md`](./docs/MEMORY_IMPLEMENTATION.md) — persistent memory v2 spec & implementation gates |
| [`tests/`](./tests/) | Unit tests covering profile validation, allowlist enforcement, C++ weight bounds, 40-char supply math, and Memory v1 + soft work-order reservations. |

---

## Quick Start

Requires Python 3.11+ (no third-party packages).

```bash
# Validate all 38 companion profiles and run unit tests
python3 -m hearthcore validate-roster
python3 -m unittest discover -s tests -v

# Dry-run single companion plan (deterministic fallback, $0 API cost)
python3 -m hearthcore plan --character brannoc --snapshot examples/brannoc.json

# Live server commands (requires AzerothCore Docker stack + local .env)
python3 -m hearthcore brain-status
python3 -m hearthcore brain-tick --provider anthropic
python3 -m hearthcore supply-report
python3 -m hearthcore quartermaster-tick --provider anthropic
```

---

## Hard Safety Invariants

- **Authoritative Server:** AzerothCore and MySQL remain the sole source of truth for levels, XP, gear, items, bank, quests, GUIDs, and actions.
- **Strict Allowlist:** Claude may only choose from verified candidate IDs generated from authoritative DB state. `core.validate()` rejects any unknown ID or out-of-range weight (`[0.5, 1.5]`).
- **Zero LLM Chat:** No model output is ever sent to in-game chat.
- **Zero API Cost by Default:** CLI dry runs default to deterministic fallback; live API runs enforce `HEARTHCORE_DAILY_CALL_CAP` and fall back cleanly on any error.

## Roadmap — Phase II (proposed)

See [The Living Guild technical specification](docs/PHASE_II_LIVING_GUILD.md) and [implementation cards and verification](docs/PHASE_II_IMPLEMENTATION.md). Cooperative stock dungeon gameplay, opportunity admission, readiness, persona identity and performance testing are proposed; live rollout depends on verified Phase I issues #3–9 and explicit owner approval.
