"""Optional local audit history, not authoritative game-state storage."""
import json
import sqlite3
from datetime import datetime, timezone

def record(path, plan, source, observed_at):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS proposals (id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, character_id TEXT NOT NULL, source TEXT NOT NULL, observed_at TEXT NOT NULL, json TEXT NOT NULL)")
        db.execute("INSERT INTO proposals(recorded_at,character_id,source,observed_at,json) VALUES(?,?,?,?,?)",
                   (datetime.now(timezone.utc).isoformat(), plan["character_id"], source, observed_at, json.dumps(plan, sort_keys=True)))
