"""Authoritative MySQL <-> SQLite Native WotLK RDF/LFG Execution Bridge (Issue #20).

Architecture & Authority Invariants:
- MySQL (`acore_characters.companion_opportunities` + `companion_opportunity_interests`) and native
  AzerothCore `LFGMgr` are the AUTHORITATIVE runtime state for live cooperative dungeon runs.
- SQLite (`brain.db` / `OpportunityCoordinator`) is a planning cache & READ PROJECTION only:
  it NEVER overwrites live MySQL state with stale SQLite state, and live outcomes recorded in MySQL
  (queue IDs, proposal IDs, group IDs, run IDs, completed, drained, aborted, cancelled, expired) are
  projected back into SQLite via `record_native_handoff`, `record_native_outcome`, `release_party`,
  and `reconcile_native_state`.
- Native queue IDs are NEVER fabricated at publish time (`native_queue_id` is published as `''` and
  populated ONLY by C++ on the world thread once AzerothCore `LFGMgr` confirms all 5 members are in
  `LFG_STATE_QUEUED`).
- Publish statements execute inside a single MySQL transaction (`START TRANSACTION ... COMMIT`) and
  guard all interest inserts against `correlation_key` so foreign rosters can NEVER attach to an
  unrelated MySQL opportunity ID.
- Unsupported dungeons, shadow opportunities, unconfirmed rosters, and disabled/draining coop modes
  fail CLOSED and are NEVER published to MySQL for native RDF execution.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from .dungeons import DUNGEON_BY_ID
from .opportunities import (
    NativeReconcileSnapshot,
    OpportunityCoordinator,
    coop_enabled,
    enabled_dungeon_ids,
)

logger = logging.getLogger("hearthcore.native_lfg_bridge")

# Canonical native MySQL state -> SQLite NativeReconcileSnapshot / record_native_outcome mapping
MYSQL_LIVE_STATES = frozenset({"assembling", "active"})
MYSQL_TERMINAL_OUTCOMES = frozenset({"completed", "drained", "aborted", "cancelled", "expired"})
MIN_QUEUE_WINDOW_SEC = 300  # At least 5 minutes for native RDF queueing & proposal assembly


def _sql_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _int_or(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def build_mysql_publish_statements(
    opportunity: dict[str, Any],
    *,
    now_ts: int | None = None,
    include_lfg_columns: bool = True,
    include_transaction: bool = True,
) -> tuple[bool, str, list[str]]:
    """Build transactional MySQL INSERT/UPSERT SQL to publish a confirmed live opportunity to MySQL.

    Fail-closed checks:
    - `coop_enabled()` MUST be True (`HEARTHCORE_COOP_ENABLED=1` / `HEARTHCORE_COOP_MODE=enabled`).
    - Opportunity MUST be live (`is_shadow == False`) and `state == 'confirmed'`.
    - Dungeon MUST be in `DUNGEON_BY_ID`, `supported == True`, `lfg_dungeon_id > 0`, and in `enabled_dungeon_ids()`.
    - Roster MUST contain exact 5 `accepted` members matching `roles_json` with valid persona_id & character_guid.
    - Publishes `native_queue_id = ''` (populated ONLY after C++ confirms all 5 bots are in `LFG_STATE_QUEUED`).
    - Guards interest inserts with `WHERE id = opp_id AND correlation_key = corr_key` so an unrelated MySQL
      opportunity row can NEVER have foreign interest rows attached.
    """
    ts = int(now_ts if now_ts is not None else time.time())
    if not coop_enabled():
        return False, "coop_disabled_or_draining", []
    if not opportunity or bool(opportunity.get("is_shadow", True)):
        return False, "shadow_opportunity_blocked", []
    if str(opportunity.get("state") or "") != "confirmed":
        return False, f"opportunity_not_confirmed:{opportunity.get('state')}", []

    dungeon_id = str(opportunity.get("dungeon_id") or "")
    spec = DUNGEON_BY_ID.get(dungeon_id)
    if spec is None or not spec.supported or spec.lfg_dungeon_id <= 0 or spec.difficulty not in (0, 1):
        return False, f"unsupported_dungeon:{dungeon_id}", []
    if dungeon_id not in enabled_dungeon_ids():
        return False, f"dungeon_not_enabled:{dungeon_id}", []

    members = [m for m in (opportunity.get("members") or []) if str(m.get("state") or "") == "accepted"]
    if len(members) != 5:
        return False, f"incomplete_confirmed_roster:{len(members)}", []
    role_counts = {"tank": 0, "healer": 0, "damage": 0}
    seen_guids: set[int] = set()
    seen_personas: set[str] = set()
    member_factions: set[str] = set()
    for m in members:
        role = str(m.get("role") or "")
        guid = int(m.get("character_guid") or 0)
        pid = str(m.get("persona_id") or "").strip()
        mf = str(m.get("faction") or "").strip().lower()
        if mf:
            member_factions.add(mf)
        if role not in role_counts or guid <= 0 or not pid:
            return False, "invalid_member_record", []
        if guid in seen_guids or pid in seen_personas:
            return False, "duplicate_member_in_roster", []
        seen_guids.add(guid)
        seen_personas.add(pid)
        role_counts[role] += 1
    expected_roles = dict(spec.roles)
    if role_counts != expected_roles:
        return False, f"role_composition_mismatch:{role_counts}", []

    opp_id = int(opportunity["id"])
    rev = max(1, int(opportunity.get("revision") or 1))
    creator_guid = int(opportunity.get("creator_guid") or 0)
    # P2-5: Derive faction & guild_id from opportunity/roster metadata rather than overriding with hard-coded values.
    raw_faction = str(opportunity.get("faction") or "").strip().lower()
    if not raw_faction:
        if len(member_factions) == 1:
            raw_faction = next(iter(member_factions))
        elif getattr(spec, "faction", None) in ("alliance", "horde"):
            raw_faction = str(spec.faction)
        else:
            raw_faction = "alliance"
    if raw_faction not in ("alliance", "horde"):
        return False, f"invalid_faction:{raw_faction}", []
    faction = raw_faction[:16]
    guild_id = max(0, int(opportunity.get("guild_id") or 0))
    roles_json = json.dumps(expected_roles, separators=(",", ":"), sort_keys=True)
    created_at = int(opportunity.get("created_at") or ts)
    est_min = max(10, int(opportunity.get("estimated_minutes") or spec.estimated_minutes))
    # P1-1: Real queue/assembly window based on estimated_minutes (>= 300s), never a cramped 60s window!
    queue_window_sec = max(MIN_QUEUE_WINDOW_SEC, est_min * 30)
    recruit_until = max(int(opportunity.get("recruit_until") or ts), ts + queue_window_sec)
    corr_key = str(opportunity.get("correlation_key") or f"opp:{opp_id}:r{rev}")[:96]

    if not include_lfg_columns:
        # P2-4: Fail closed on unmigrated DBs — never publish without correlation_key guard & native ID columns!
        return False, "lfg_columns_missing", []

    stmts: list[str] = []
    if include_transaction:
        stmts.append("START TRANSACTION;")

    # P1-5: native_queue_id starts as '' (empty) and is populated ONLY by C++ once core confirms LFG_STATE_QUEUED!
    # P1-10: Duplicate key update requires correlation_key match so unrelated rows are never clobbered!
    stmts.append(
        "INSERT INTO companion_opportunities "
        "(id, schema_version, revision, kind, creator_guid, faction, guild_id, dungeon_map_id, difficulty, "
        "roles_json, state, created_at, recruit_until, estimated_minutes, group_guid, outcome_reason, is_shadow, "
        "lfg_dungeon_id, native_queue_id, native_proposal_id, native_run_id, correlation_key) "
        f"VALUES ({opp_id}, 1, {rev}, 'dungeon_run', {creator_guid}, {_sql_quote(faction)}, {guild_id}, "
        f"{spec.map_id}, {spec.difficulty}, {_sql_quote(roles_json)}, 'assembling', {created_at}, "
        f"{recruit_until}, {est_min}, 0, 'published_for_native_rdf', 0, "
        f"{spec.lfg_dungeon_id}, '', 0, '', {_sql_quote(corr_key)}) "
        "ON DUPLICATE KEY UPDATE "
        "revision = IF(is_shadow = 0 AND state = 'assembling' AND (correlation_key = '' OR correlation_key = VALUES(correlation_key)), VALUES(revision), revision), "
        "recruit_until = IF(is_shadow = 0 AND state = 'assembling' AND (correlation_key = '' OR correlation_key = VALUES(correlation_key)), GREATEST(recruit_until, VALUES(recruit_until)), recruit_until), "
        "lfg_dungeon_id = IF(is_shadow = 0 AND state = 'assembling' AND (correlation_key = '' OR correlation_key = VALUES(correlation_key)), VALUES(lfg_dungeon_id), lfg_dungeon_id), "
        "correlation_key = IF(is_shadow = 0 AND state = 'assembling' AND correlation_key = '', VALUES(correlation_key), correlation_key);"
    )

    for m in sorted(members, key=lambda item: int(item["character_guid"])):
        guid = int(m["character_guid"])
        pid = str(m["persona_id"])
        role = str(m["role"])
        avail = max(int(m.get("available_until") or (ts + est_min * 60)), recruit_until)
        # P1-10: Attach interest ONLY when the target MySQL opportunity matches our exact correlation_key!
        stmts.append(
            "INSERT INTO companion_opportunity_interests "
            "(opportunity_id, opportunity_revision, persona_id, character_guid, role, state, reason_code, "
            "available_until, reserved_until, created_at, updated_at) "
            f"SELECT o.id, {rev}, {_sql_quote(pid)}, {guid}, {_sql_quote(role)}, 'accepted', "
            f"'voluntary_rdf_confirmed', {avail}, {recruit_until}, {ts}, {ts} "
            f"FROM companion_opportunities o "
            f"WHERE o.id = {opp_id} AND o.is_shadow = 0 AND o.state = 'assembling' "
            f"AND o.correlation_key = {_sql_quote(corr_key)} "
            "ON DUPLICATE KEY UPDATE "
            "opportunity_revision = IF(companion_opportunity_interests.state IN ('interested','reserved','accepted'), VALUES(opportunity_revision), companion_opportunity_interests.opportunity_revision), "
            "role = IF(companion_opportunity_interests.state IN ('interested','reserved','accepted'), VALUES(role), companion_opportunity_interests.role), "
            "state = IF(companion_opportunity_interests.state IN ('interested','reserved','accepted'), 'accepted', companion_opportunity_interests.state), "
            "available_until = GREATEST(companion_opportunity_interests.available_until, VALUES(available_until)), "
            "updated_at = VALUES(updated_at);"
        )

    if include_transaction:
        stmts.append("COMMIT;")

    return True, "ok", stmts


def publish_confirmed_opportunity_to_mysql(
    coordinator: OpportunityCoordinator,
    opportunity_id: int,
    execute_sql: Callable[[str], Any],
    *,
    now_ts: int | None = None,
    include_lfg_columns: bool = True,
    single_batch: bool = True,
    verify_readback: bool = True,
) -> dict[str, Any]:
    """Publish a confirmed live opportunity into authoritative MySQL for native WotLK RDF queueing."""
    opp = coordinator.get_opportunity(opportunity_id)
    if opp is None:
        return {"ok": False, "reason": "opportunity_not_found"}
    ok, reason, stmts = build_mysql_publish_statements(
        opp,
        now_ts=now_ts,
        include_lfg_columns=include_lfg_columns,
        include_transaction=True,
    )
    if not ok:
        return {"ok": False, "reason": reason}
    if single_batch:
        # Execute START TRANSACTION ... COMMIT in one MySQL session so transaction boundaries actually hold!
        execute_sql("\n".join(stmts))
    else:
        for sql in stmts:
            execute_sql(sql)

    opp_id = int(opp["id"])
    rev = max(1, int(opp.get("revision") or 1))
    corr_key = str(opp.get("correlation_key") or f"opp:{opp_id}:r{rev}")[:96]
    if verify_readback:
        # P2-3: Read back correlation_key and confirmed interest count so a correlation-guard block NEVER reports ok!
        rb = execute_sql(
            "SELECT o.correlation_key, "
            "(SELECT COUNT(*) FROM companion_opportunity_interests i "
            " WHERE i.opportunity_id = o.id AND i.state = 'accepted') "
            f"FROM companion_opportunities o WHERE o.id = {opp_id} AND o.is_shadow = 0;"
        )
        if not rb or not rb[0] or len(rb[0]) < 2:
            return {"ok": False, "reason": "publish_readback_missing"}
        if str(rb[0][0] or "") != corr_key or _int_or(rb[0][1], 0) != 5:
            return {
                "ok": False,
                "reason": f"publish_correlation_or_interest_mismatch:key={rb[0][0]}:count={rb[0][1]}",
            }

    spec = DUNGEON_BY_ID[str(opp["dungeon_id"])]
    return {
        "ok": True,
        "opportunity_id": opp_id,
        "revision": rev,
        "lfg_dungeon_id": int(spec.lfg_dungeon_id),
        "native_queue_id": "",  # Confirmed ONLY after C++ observes all 5 bots in LFG_STATE_QUEUED
        "statement_count": len(stmts),
    }


def build_reconcile_snapshot_from_mysql(
    mysql_rows: list[dict[str, Any]],
    *,
    authoritative: bool = True,
) -> NativeReconcileSnapshot:
    """Build an authoritative NativeReconcileSnapshot directly from MySQL `companion_opportunities` rows.

    Maps:
    - Live rows (`assembling`/`active`) -> `active_group_ids`, `active_queue_ids`, `active_run_ids`, `correlation_map`
    - Drained rows (`state == 'drained'`) -> `drained_outcomes[opp_id] = outcome_reason` (Issue #22 neutral projection!)
    """
    if not authoritative:
        return NativeReconcileSnapshot(authoritative=False)

    active_groups: set[int] = set()
    active_queues: set[str] = set()
    active_runs: set[str] = set()
    corr_map: dict[str, dict[str, Any]] = {}
    drained_outcomes: dict[int, str] = {}

    for row in mysql_rows:
        if int(row.get("is_shadow") or 0) != 0:
            continue
        oid = int(row.get("id") or 0)
        if oid <= 0:
            continue
        state = str(row.get("state") or "").strip().lower()
        gid = max(0, int(row.get("group_guid") or 0))
        qid = str(row.get("native_queue_id") or "").strip()
        rid = str(row.get("native_run_id") or "").strip()
        pid = max(0, int(row.get("native_proposal_id") or 0))
        ckey = str(row.get("correlation_key") or "").strip()
        outcome_reason = str(row.get("outcome_reason") or "").strip()

        if state in MYSQL_LIVE_STATES:
            if gid > 0:
                active_groups.add(gid)
            if qid:
                active_queues.add(qid)
            if rid:
                active_runs.add(rid)
            if ckey and (rid or gid > 0 or pid > 0 or qid):
                n_state = "in_instance" if rid else ("active" if gid > 0 else ("proposal_sent" if pid > 0 else "queued"))
                corr_map[ckey] = {
                    "native_state": n_state,
                    "native_queue_id": qid,
                    "group_id": gid,
                    "native_run_id": rid,
                }
        elif state == "drained":
            drained_outcomes[oid] = outcome_reason or "coop_drained"

    return NativeReconcileSnapshot(
        authoritative=True,
        active_group_ids=frozenset(active_groups),
        active_queue_ids=frozenset(active_queues),
        active_run_ids=frozenset(active_runs),
        correlation_map=corr_map,
        drained_outcomes=drained_outcomes,
    )


def project_mysql_authority_to_sqlite(
    coordinator: OpportunityCoordinator,
    mysql_rows: list[dict[str, Any]],
    *,
    authoritative: bool = True,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Project authoritative MySQL native RDF/LFG states & outcomes into SQLite read projection.

    Runs cleanly regardless of `coop_enabled()` (so `draining` and `off` modes STILL record native
    `completed`, `drained`, `aborted`, `cancelled`, and `expired` outcomes faithfully!).
    """
    ts = int(now_ts if now_ts is not None else time.time())
    handoffs_applied = 0
    outcomes_applied = 0

    for row in mysql_rows:
        if int(row.get("is_shadow") or 0) != 0:
            continue
        oid = int(row.get("id") or 0)
        if oid <= 0:
            continue
        local = coordinator.get_opportunity(oid)
        if local is None or bool(local.get("is_shadow")):
            continue

        # P2-2: Never project a foreign MySQL row onto an unrelated local opportunity after a DB reset!
        mysql_ckey = str(row.get("correlation_key") or "").strip()
        local_ckey = str(local.get("correlation_key") or "").strip()
        if mysql_ckey and local_ckey and mysql_ckey != local_ckey:
            continue

        state = str(row.get("state") or "").strip().lower()
        gid = max(0, int(row.get("group_guid") or 0))
        qid = str(row.get("native_queue_id") or "").strip()
        rid = str(row.get("native_run_id") or "").strip()
        pid = max(0, int(row.get("native_proposal_id") or 0))
        reason = str(row.get("outcome_reason") or "").strip()
        local_state = str(local.get("state") or "")
        local_rev = int(local.get("revision") or 1)

        # 1. Forward native handoff projection ONLY once core actually confirmed queued/proposal/group/instance (P1-5)!
        if state in MYSQL_LIVE_STATES and local_state in ("confirmed", "active"):
            if not (rid or gid > 0 or pid > 0 or qid):
                continue  # Published to MySQL but not yet confirmed queued in AzerothCore LFGMgr!
            n_state = "in_instance" if rid else ("active" if gid > 0 else ("proposal_sent" if pid > 0 else "queued"))
            if (
                local_state == "confirmed"
                or str(local.get("native_state") or "") != n_state
                or int(local.get("group_id") or 0) != gid
                or (qid and str(local.get("native_queue_id") or "") != qid)
                or (rid and str(local.get("native_run_id") or "") != rid)
            ):
                res = coordinator.record_native_handoff(
                    oid,
                    expected_revision=local_rev,
                    native_state=n_state,
                    native_queue_id=qid,
                    group_id=gid,
                    native_run_id=rid,
                    now_ts=ts,
                )
                if res.get("ok"):
                    handoffs_applied += 1
            continue

        # 2. Terminal native outcome projection (completed / drained / aborted / cancelled / expired)
        if state in MYSQL_TERMINAL_OUTCOMES and local_state in ("confirmed", "active", "cancelling"):
            if state in ("completed", "drained", "aborted"):
                if local_state == "confirmed" and (gid > 0 or qid or rid):
                    h = coordinator.record_native_handoff(
                        oid,
                        expected_revision=local_rev,
                        native_state="in_instance" if rid else ("active" if gid > 0 else "queued"),
                        native_queue_id=qid,
                        group_id=gid,
                        native_run_id=rid,
                        now_ts=ts,
                    )
                    if h.get("ok"):
                        local_state = "active"
                if local_state in ("active", "cancelling"):
                    res = coordinator.record_native_outcome(
                        oid,
                        native_run_id=rid,
                        native_queue_id=qid,
                        group_id=gid,
                        outcome=state,
                        outcome_reason=reason or f"mysql_{state}",
                        now_ts=ts,
                    )
                    if res.get("ok") and not res.get("idempotent"):
                        outcomes_applied += 1
            elif state in ("cancelled", "expired"):
                if local_state in ("confirmed", "cancelling"):
                    res = coordinator.release_party(
                        oid,
                        reason or f"mysql_{state}",
                        cancel_opp=True,
                        native_confirmed=True,
                        now_ts=ts,
                    )
                    if res.get("ok"):
                        outcomes_applied += 1
                elif local_state == "active":
                    # P2-6: Active SQLite projection whose authoritative MySQL row was cancelled/expired
                    # (e.g. proposal declined/timed out after queue confirmation) resolves neutrally as drained
                    # (if pre-instance) or aborted (if instance already entered), never left dangling!
                    mapped_outcome = "aborted" if (gid > 0 or rid) else "drained"
                    res = coordinator.record_native_outcome(
                        oid,
                        native_run_id=rid,
                        native_queue_id=qid,
                        group_id=gid,
                        outcome=mapped_outcome,
                        outcome_reason=reason or f"mysql_{state}",
                        now_ts=ts,
                    )
                    if res.get("ok") and not res.get("idempotent"):
                        outcomes_applied += 1

    snap = build_reconcile_snapshot_from_mysql(mysql_rows, authoritative=authoritative)
    rec = coordinator.reconcile_native_state(snap, now_ts=ts)
    return {
        "ok": True,
        "handoffs_applied": handoffs_applied,
        "outcomes_applied": outcomes_applied,
        "reconcile": rec,
    }


def _fetch_mysql_opportunity_rows(
    sql_exec: Callable[[str], Any],
    *,
    local_live_ids: list[int] | None = None,
) -> tuple[bool, bool, list[dict[str, Any]]]:
    """Read authoritative MySQL `companion_opportunities` rows without blind LIMIT truncation (P2-5).

    Always selects ALL active/assembling rows PLUS every ID currently live in local SQLite.
    Returns `(table_exists, has_lfg_columns, rows)`.
    """
    col_rows = sql_exec(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'companion_opportunities' "
        "AND column_name IN ('lfg_dungeon_id', 'native_queue_id', 'native_proposal_id', 'native_run_id', 'correlation_key');"
    )
    has_lfg_cols = bool(col_rows and col_rows[0] and _int_or(col_rows[0][0], 0) >= 5)
    safe_ids = sorted({int(i) for i in (local_live_ids or []) if int(i) > 0})
    where_filter = (
        f"is_shadow = 0 AND (state IN ('assembling', 'active') OR id IN ({','.join(str(i) for i in safe_ids)}))"
        if safe_ids
        else "is_shadow = 0 AND state IN ('assembling', 'active')"
    )
    if has_lfg_cols:
        raw = sql_exec(
            "SELECT id, state, group_guid, is_shadow, outcome_reason, "
            "lfg_dungeon_id, native_queue_id, native_proposal_id, native_run_id, correlation_key "
            f"FROM companion_opportunities WHERE {where_filter} ORDER BY id DESC;"
        )
        rows = [
            {
                "id": _int_or(r[0], 0),
                "state": str(r[1] or ""),
                "group_guid": _int_or(r[2], 0),
                "is_shadow": _int_or(r[3], 0),
                "outcome_reason": str(r[4] or ""),
                "lfg_dungeon_id": _int_or(r[5], 0),
                "native_queue_id": str(r[6] or ""),
                "native_proposal_id": _int_or(r[7], 0),
                "native_run_id": str(r[8] or ""),
                "correlation_key": str(r[9] or ""),
            }
            for r in (raw or [])
            if len(r) >= 10
        ]
        return True, True, rows

    tbl_rows = sql_exec(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = 'companion_opportunities';"
    )
    if not tbl_rows or not tbl_rows[0] or _int_or(tbl_rows[0][0], 0) < 1:
        return False, False, []
    raw = sql_exec(
        "SELECT id, state, group_guid, is_shadow, outcome_reason "
        f"FROM companion_opportunities WHERE {where_filter} ORDER BY id DESC;"
    )
    rows = [
        {
            "id": _int_or(r[0], 0),
            "state": str(r[1] or ""),
            "group_guid": _int_or(r[2], 0),
            "is_shadow": _int_or(r[3], 0),
            "outcome_reason": str(r[4] or ""),
            "lfg_dungeon_id": 0,
            "native_queue_id": "",
            "native_proposal_id": 0,
            "native_run_id": "",
            "correlation_key": "",
        }
        for r in (raw or [])
        if len(r) >= 5
    ]
    return True, False, rows


def sync_coop_native_lfg_bridge(
    *,
    db_path: Path,
    sql_exec: Callable[[str], Any],
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Runtime coordinator loop step (P0-3): projects MySQL authority -> SQLite AND publishes confirmed live opps.

    Safe to invoke on every `bridge.run_cycle(apply=True)` tick:
    - When `coop_enabled()` is False AND there are no live non-shadow SQLite opportunities, performs ZERO MySQL writes
      and zero disruptive work!
    - When live non-shadow opportunities exist in SQLite (even in draining/off mode), projects authoritative MySQL
      outcomes (`completed`, `drained`, `aborted`, `cancelled`, `expired`) back into SQLite.
    - ONLY when `coop_enabled()` is True, publishes confirmed live opportunities to MySQL for native WotLK RDF queueing.
    """
    ts = int(now_ts if now_ts is not None else time.time())
    coordinator = OpportunityCoordinator(db_path)
    live_opps = [
        o for o in coordinator.list_opportunities(include_shadow=False)
        if str(o.get("state") or "") in ("confirmed", "active", "cancelling")
    ]
    enabled = coop_enabled()
    if not enabled and not live_opps:
        return {"ok": True, "skipped": "coop_disabled_no_live_opps", "published": 0, "handoffs": 0, "outcomes": 0}

    local_live_ids = [int(o["id"]) for o in live_opps]
    table_ok, has_lfg_cols, mysql_rows = _fetch_mysql_opportunity_rows(sql_exec, local_live_ids=local_live_ids)
    if not table_ok:
        return {"ok": False, "reason": "mysql_table_missing", "published": 0, "handoffs": 0, "outcomes": 0}

    proj = project_mysql_authority_to_sqlite(coordinator, mysql_rows, authoritative=True, now_ts=ts)
    mysql_by_id = {int(r["id"]): r for r in mysql_rows if int(r.get("id") or 0) > 0}

    published = 0
    publish_errors: list[str] = []
    if enabled:
        for opp in live_opps:
            oid = int(opp["id"])
            if str(opp.get("state") or "") != "confirmed":
                continue
            existing = mysql_by_id.get(oid)
            if existing is not None:
                continue  # Already present in MySQL (assembling/active/terminal); never overwrite live MySQL authority!
            res = publish_confirmed_opportunity_to_mysql(
                coordinator,
                oid,
                sql_exec,
                now_ts=ts,
                include_lfg_columns=has_lfg_cols,
            )
            if res.get("ok"):
                published += 1
            else:
                publish_errors.append(f"opp#{oid}:{res.get('reason')}")

    return {
        "ok": True,
        "coop_enabled": enabled,
        "has_lfg_columns": has_lfg_cols,
        "published": published,
        "publish_errors": publish_errors,
        "handoffs": int(proj.get("handoffs_applied") or 0),
        "outcomes": int(proj.get("outcomes_applied") or 0),
    }
