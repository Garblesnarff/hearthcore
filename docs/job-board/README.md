# Azeroth Job Board export

This folder is a copy of the Azeroth Job Board. The board is the planning page that the owner and the planning assistant used from 2026-11-14 to 2026-11-22, before most work moved into this repository and its GitHub issues.

**Snapshot date:** 2026-11-25. **Cards on the board:** 61.

The board stays the place where prompts are copied from. This export exists so that contributors and AI assistants can read the ideas, the decisions and the history without access to the board.

## Read this first

- The board is older than parts of this repository. Where the two disagree, the repository wins: see [SERVER_STATE.md](../SERVER_STATE.md), [PHASE_II_LIVING_GUILD.md](../PHASE_II_LIVING_GUILD.md) and the GitHub issues.
- Examples of facts that changed after the board was written: the roster is now 38 named companions (the board talks about the "core four" plus a planned raid wave); a planner model now runs outside the game loop through `hearthcore/`; 50 random bots run (the board measured 15).
- Nothing here is a commitment. Ideas are ideas until an issue exists for them.
- Personal details were removed for this public repository. No passwords or keys were ever on the board.

## Files

| File | Content |
|---|---|
| [IDEAS.md](IDEAS.md) | Every card that is not done: ideas, waiting plans, ready jobs. This is the backlog. |
| [DECISIONS.md](DECISIONS.md) | The owner's decisions, by date. Design rules that still apply. |
| [OPEN-PROMPTS.md](OPEN-PROMPTS.md) | The full prompt text of the cards that were ready or in progress at the snapshot. |
| [DONE.md](DONE.md) | Finished cards with their results, as a history. |

## How the board maps to the GitHub issues

| Board card | Related issue or document |
|---|---|
| Autonomous dungeon groups (`dungeon-groups`) | #20, #21, #14, #17, and Phase II cards II-03 to II-06 |
| Bot brain roadmap: goals, projects, planner (`brain-roadmap`) | #3 to #8 (done), #24, `hearthcore/` |
| Raid team plan (`raid-plan`) | #16, #25, Phase II card II-08 |
| Raid combat backlog (`raid-combat`) | #16 |
| Best-gear tables and gear projects (`companion-bis`) | #24 |
| Profession catch-up at the XP cap (cards7, item 4) | #23 |
| Lead mode v2, kill order (`lead-dps-target`, `kill-order`) | #21 (leader execution); no issue of their own yet |
| Guild 40 capability audit (`audit-guild40`) | [PHASE_II_AUDIT_II00.md](../PHASE_II_AUDIT_II00.md) |
| Performance phases 1 and 2 (`perf-phase1`, `perf-phase2`) | [SERVER_STATE.md](../SERVER_STATE.md), `ops/perf-check.sh` |

Cards with no issue yet are listed in [IDEAS.md](IDEAS.md) under "No issue yet".

## Words used on the board

- **Owner:** the human player. His character is Wonky, a dwarf priest and the group healer.
- **Core four:** the first four companions: Brannoc (Protection warrior, tank), Aldric (Retribution paladin), Kelda (Beast Mastery hunter), Wren (Combat rogue).
- **Coder:** the AI coding agent on the server PC. **Neon:** a second agent that did the performance work.
- **Rules-true:** bots earn things inside the game rules. No item or gold from the database.
- **Lead mode:** the companion tank leads a dungeon: ready check, mark, pull.
- **Lease:** one activity owns a companion's movement at a time.
- **cards4, cards5, cards6, cards7:** names of the large overnight builds.
