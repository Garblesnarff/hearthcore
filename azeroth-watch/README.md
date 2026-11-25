# Azeroth Watch — Hearthguard Live Observability Dashboard

Lightweight, zero-dependency Python 3 SSE/HTTP observability dashboard (`~25 MiB` RAM) for the Hearthguard AzerothCore server, companion roster, guild vault, and Hearthcore Claude AI Brain.

## Views

1. **Overview (`1`)** — Live rotating world/continent/zone map (3s position stream from `live_positions.json`), server tick health (`ms` mean/p99), online companions & humans, Guild Vault snapshot, Guild Life chronicle, and 24h shift timeline.
2. **Vault & Professions (`2`)** — Searchable Guild Bank material inventory, guild-wide profession coverage bars toward the current progression cap, and recent `companion_ledger` activity.
3. **Raid Roster (`3`)** — Filterable 38-companion roster, reputation goals toward Exalted, Guild Work Orders, and top Guild Bonds (`companion_relationships`).
4. **AI Brain (`4`)** — Human-readable Control Center for `hearthcore`:
   - Credit baseline vs shared transactional monthly budget ceiling (`api_ledger`), spend pace, daily safety cap, reliability %, and AI-guided progress (`+levels`, `+skillups`, `+quests`, `+orders`).
   - Hourly **Claude Sonnet 5.5 Guild Quartermaster** summary + 40-character Classic `1–300` material deficit bars.
   - Plain-English per-companion session focus, **native proposal lifecycle badges** (`Proposed`, `Accepted`, `Executing`, `Completed`, `Rejected`, `Interrupted`, `Advisory`), reason, priority nudges (`Crafting ↑↑ +45%`), **Memory v1** badges (open intention, variety streak, scoped stall caution), and live decision history.
   - Authenticated admin controls (`🔒 Unlock Admin` via `AZEROTH_WATCH_ADMIN_TOKEN`).

## Security & Deployment Model

- **Safe Default Bind:** `server.py` defaults to `--host 127.0.0.1`. On our private home LAN, `ops/systemd/azeroth-watch.service` binds `0.0.0.0:8787` so read-only family dashboards/kiosks can view `GET /`, `/api/state`, and `/api/stream`.
- **Authenticated Admin Controls:** ALL state-changing endpoints (`POST /api/brain/{model,credit,run}`) require:
  1. Explicit authorization via `X-Azeroth-Admin-Token` header or `HttpOnly; SameSite=Strict` HMAC-signed cookie issued by `POST /api/brain/auth` against `AZEROTH_WATCH_ADMIN_TOKEN` (auto-generated in local `.env` with `chmod 600`; never logged or returned in `/api/state`).
  2. Strict Same-Origin / CSRF validation (`Origin`/`Referer` must match `Host`) and `Content-Type: application/json`.
  3. Strict `<= 4096`-byte body limit, rate limiting (15s run cooldown + 5/min auth fail backoff), and non-secret audit logging to `runtime/admin_audit.log`.
- **Deployment Assumption:** Designed for a private home LAN behind a residential NAT/firewall; do not expose port `8787` directly to the public internet without a TLS-terminating reverse proxy.

## Copyrighted Client Assets Excluded

Extracted WoW map tiles (`static/maps/*.jpg`) and class/race/profession icons (`static/icons/*.png`) are **intentionally excluded** from this public repository. On a host with a local WoW 3.3.5a client, generate them locally with `extract_maps.py`.
