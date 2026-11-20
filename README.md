# Hearthcore

Character-first planning for the private AzerothCore WotLK guild.

**Status: standalone foundation only.** This tool generates validated *proposed* session goals; it does not connect to the live server, spend API credits by default, or grant WoW progress.

## Use

Requires Python 3.11+. No third-party packages.

    python -m hearthcore roster
    python -m hearthcore validate-roster
    python -m hearthcore plan --character brannoc --snapshot examples/brannoc.json
    python -m unittest discover -s tests -v

Optional real API request (requires your own key and a supported model ID):

    ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=... python -m hearthcore plan --character brannoc --snapshot examples/brannoc.json --provider anthropic

Run without --provider anthropic to avoid any API charge. No automatic scheduling or game integration yet.

## Structure

- characters/<slug>/profile.json: reviewed identities and slowly changing preferences. Entries marked proposed are not in-game facts.
- hearthcore/: shared deterministic and optional Anthropic planner, allowlist validator, and SQLite audit.
- examples/: synthetic candidate snapshots for dry runs.
- tests/: unit tests.
- docs/ARCHITECTURE.md: bridge and safety requirements.

The game server and its database remain authoritative for level, gear, items, bank, quests, game GUIDs, and bot actions. Git stores *stable identity and configuration*, never live inventory or mutable session history. An eventual trusted exporter supplies currently eligible candidate goals. Claude may only choose from those IDs. A future importer must independently revalidate eligibility before adjusting existing CompanionLease / companion_weights systems.

No model output goes to in-game chat. All text shown to a child must be approved, event-grounded templates.
