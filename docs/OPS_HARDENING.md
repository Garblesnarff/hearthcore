# Host & Service Hardening Runbook (Issue #75)

## Active Protections
1. **Least-Privilege MySQL Credentials (Issue #62):**
   - `hearthcore_brain` and `azeroth_watch` MySQL users are provisioned via `ops/provision-least-priv-db.sh` with random 32-char passwords stored in `chmod 600` option files inside `ac-database`'s persistent volume (`/var/lib/mysql/.hearthcore_brain.cnf` and `/var/lib/mysql/.azeroth_watch.cnf`).
   - Neither service uses MySQL `root` in normal runtime operation, and no password ever appears on process `argv`.
2. **Systemd User Unit Hardening:**
   - All services (`azeroth-watch.service`, `hearthcore-brain.service`, `hearthcore-quartermaster.service`, `hearthcore-perf-check.service`, `hearthcore-health-watch.service`, `hearthcore-backup.service`, `hearthcore-restore-drill.service`) set `NoNewPrivileges=yes`, `UMask=0077`, resource quotas/nice levels, and `OnFailure=hearthcore-failure@%n.service`.
   - `azeroth-watch.service` defaults to loopback `--host 127.0.0.1`; LAN viewing is an explicit systemd drop-in opt-in.
3. **Automated Nightly Backups & Weekly Restore Drills:**
   - `hearthcore-backup.timer` runs `ops/backup.sh` nightly at `04:00` (chmod `600` bundle containing `database.sql.gz`, `env`, and WAL-safe `sqlite3.backup()` copy of `brain.db`).
   - `hearthcore-restore-drill.timer` verifies the latest backup weekly on Sundays at `04:45`; pass `--mysql-restore-test` to perform a full throwaway-database MySQL restore drill (`hearthcore_restore_drill_<pid>`, auto-dropped on exit).

## Optional Next Step: Dedicated Non-Docker User for `azeroth-watch`
Currently, the host does not have the `mysql-client` apt package installed, so `azeroth-watch` executes its read-only queries via `docker exec ac-database mysql --defaults-extra-file=/var/lib/mysql/.azeroth_watch.cnf`.
When the host owner is ready to remove Docker socket access from `azeroth-watch` completely:
1. Install the host MySQL client: `sudo apt-get update && sudo apt-get install -y mysql-client`
2. Bind MySQL port `3306` on loopback (`127.0.0.1:3306:3306`) in `docker-compose.yml` (on a planned maintenance window).
3. Create a dedicated non-Docker system user `azeroth-watch` with a `chmod 600` `~/.azeroth_watch.cnf` (`host=127.0.0.1`, `user=azeroth_watch`) and set `AZEROTH_WATCH_DB_HOST=127.0.0.1` + `AZEROTH_WATCH_DB_DEFAULTS_FILE=...`.
4. Run `azeroth-watch.service` as `User=azeroth-watch` with `ProtectSystem=strict`, `ProtectHome=yes`, and `SupplementaryGroups=` (no `docker` group).
