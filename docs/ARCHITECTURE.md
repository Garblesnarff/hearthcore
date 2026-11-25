# Architecture and future integration

Git tracks reviewed character identity and stable preference data; it does not store live inventory, quests, gold, loot or constantly changing plans.

The existing C++ AzerothCore and companion tables are authoritative. The old and new bot autonomy systems continue running if Hearthcore is unavailable.

A future exporter must query eligible candidate projects (gear list, dungeon access, professions, guild orders) from verified game state. Its snapshots must have authoritative character GUID mapping, timestamp, source/version and bounded age. This foundational CLI currently validates candidate shape, not provenance or freshness.

Hearthcore may choose one primary goal and up to three alternatives; current code validates candidate IDs and 0.5–1.5 weight limits. It does NOT apply these plans to CompanionLease or companion_weights. The importer must be separately built, reviewed, and enforce current eligibility again.

Start with shadow-mode comparison of proposed vs actual outcomes. Replan only at session boundaries or major milestone events, not per tick. API errors must never block simulation.

The optional API planner sends only the character profile and candidate snapshot. API tokens must remain in environment secrets. No generated text is exposed as bot chat: child-visible messages require independently approved templates grounded in actual events.

Planned extensions: trusted game-state bridge, persistent event memory and relationships in DB, per-character schedule, guild opportunity pool, cost quotas, token accounting, and rate-limited scheduling. None are claimed implemented.

## Persistent memory proposal

See [Persistent memory and experience (v2)](PERSISTENT_MEMORY.md) and the [implementation checklist](MEMORY_IMPLEMENTATION.md). These documents describe a future event-driven memory subsystem; they do not change the standalone CLI or claim live integration.

## Canonical Weight Namespace & Native Merge Rule (Issue #60)

Profile preference keys in `characters/<slug>/profile.json` map to native `CompanionBrain.cpp` keys via one canonical mapping (`PROFILE_PREF_TO_NATIVE_WEIGHTS` in `hearthcore/bridge.py` and `KEY_MAP` in `ops/hearthcore-sync.py`):

| Profile Preference Key | Native `CompanionBrain` Weight Key(s) |
|---|---|
| `quest` | `quest_work`, `quest_turnin`, `quest_hub`, `quest_grind` |
| `craft` | `craft` |
| `gather` | `gather` |
| `fish` | `fish` |
| `train` | `train` |
| `bank` | `bank` |
| `repair` | `repair`, `sell` |
| `travel` | `travel` |
| `rest` | `hearth` |

- **Native merge rule (`CompanionBrain.cpp`):** Active, non-expired rows in `companion_weight_overrides` **REPLACE** baseline `companion_weights` values for each key present, **except** manually tuned baseline rows (`source != 'hearthcore'`), which retain strict precedence (`manualKeys`).
- **Persona preservation:** Because native overrides replace baseline keys, `weights_for_cpp()` starts from the companion's mapped `char_profile["preferences"]` baseline rather than `1.0`, so unmentioned persona traits persist throughout every session plan. `ops/hearthcore-sync.py` never overwrites manually tuned canonical rows unless `--overwrite-manual` is explicitly passed.
