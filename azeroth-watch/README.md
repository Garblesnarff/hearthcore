# Azeroth Watch — Hearthguard Live Observability Dashboard

Lightweight, zero-dependency Python 3 SSE/HTTP observability dashboard (`~25 MiB` RAM) for the Hearthguard AzerothCore server, companion roster, guild vault, and Hearthcore Claude AI Brain.

## Views

1. **Overview (`1`)** — Live rotating world/continent/zone map (3s position stream from `live_positions.json`), server tick health (`ms` mean/p99), online companions & humans, Guild Vault snapshot, Guild Life chronicle, and 24h shift timeline.
2. **Vault & Professions (`2`)** — Searchable Guild Bank material inventory, guild-wide profession coverage bars toward the current progression cap, and recent `companion_ledger` activity.
3. **Raid Roster (`3`)** — Filterable 38-companion roster, reputation goals toward Exalted, Guild Work Orders, and top Guild Bonds (`companion_relationships`).
4. **AI Brain (`4`)** — Human-readable Control Center for `hearthcore`:
   - Tracked API credit balance, spend pace, daily safety cap, reliability %, and AI-guided progress (`+levels`, `+skillups`, `+orders`).
   - Hourly **Claude Sonnet 5.5 Guild Quartermaster** summary + 40-character Classic `1–300` material deficit bars.
   - Plain-English per-companion session focus, reason, priority nudges (`Crafting ↑↑ +45%`), **Memory v1** badges (open intention, variety streak, scoped stall caution), and live decision history.
   - LAN-only POST controls (`/api/brain/model`, `/api/brain/run`, `/api/brain/credit`).

## Copyrighted Client Assets Excluded

Extracted WoW map tiles (`static/maps/*.jpg`) and class/race/profession icons (`static/icons/*.png`) are **intentionally excluded** from this public repository. On a host with a local WoW 3.3.5a client, generate them locally with `extract_maps.py`.
