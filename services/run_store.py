"""Durable Agent run state and transition audit log."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from agents import AlarmEvent


ACTIVE_STATUSES = {"analyzing", "decided", "executing", "retryable_failed"}
FINAL_STATUSES = {"filtered", "succeeded", "permanent_failed", "cancelled"}
ALLOWED_TRANSITIONS = {
    "analyzing": {"decided", "retryable_failed", "manual_takeover"},
    "decided": {"executing", "retryable_failed", "manual_takeover"},
    "executing": {"succeeded", "waiting_approval", "retryable_failed", "manual_takeover"},
    "waiting_approval": {"succeeded", "cancelled", "manual_takeover"},
    "retryable_failed": {"analyzing", "manual_takeover", "permanent_failed"},
    "manual_takeover": {"analyzing", "succeeded", "permanent_failed", "cancelled"},
}


class IngestConflictError(RuntimeError):
    """The same ingress identity was reused with a different request payload."""

    def __init__(self, ingest_key: str):
        super().__init__("idempotency key was already used for a different payload")
        self.ingest_key = ingest_key


class StaleRunOwnerError(RuntimeError):
    """A worker attempted to mutate a Run after losing its fencing token."""

    def __init__(self, run_id: str):
        super().__init__(f"stale_or_missing_run_lease:{run_id}")
        self.run_id = run_id


def event_snapshot(event: AlarmEvent) -> dict:
    """Serialize the restart-safe part of an event; image bytes stay in evidence storage."""
    return {
        "timestamp": event.timestamp,
        "events": event.events,
        "event_id": event.event_id,
        "run_id": event.run_id,
        "trace_id": event.trace_id,
        "source_event_id": event.source_event_id,
        "ingest_key": event.ingest_key,
        "ingest_payload_hash": event.ingest_payload_hash,
        "camera_id": event.camera_id,
        "evidence_id": event.evidence_id,
        "owner_id": event.owner_id,
        "execution_attempt": event.execution_attempt,
        "raw_json": event.raw_json,
        "image_url": event.image_url,
        "llm_analysis": event.llm_analysis,
        "llm_recommendation": event.llm_recommendation,
        "llm_status": event.llm_status,
        "llm_error": event.llm_error,
        "llm_latency_ms": event.llm_latency_ms,
        "llm_json_valid": event.llm_json_valid,
        "llm_model": event.llm_model,
        "prompt_version": event.prompt_version,
        "context_manifest": event.context_manifest,
        "evidence_replan": event.evidence_replan,
        "failure_attributions": event.failure_attributions,
        "repair_trace": event.repair_trace,
        "sop_retrieval": event.sop_retrieval,
        "rag_status": event.rag_status,
        "dispatch_decision": event.dispatch_decision,
        "dispatch_actions": event.dispatch_actions,
        "approval_id": event.approval_id,
        "approval_status": event.approval_status,
        "execution_id": event.execution_id,
        "execution_status": event.execution_status,
        "execution_result": event.execution_result,
        "execution_actions": event.execution_actions,
        "lifecycle_status": event.lifecycle_status,
        "timeline": event.timeline,
    }


def restore_event(snapshot: dict, alarm_dir: Path | None = None) -> AlarmEvent:
    event = AlarmEvent(
        timestamp=str(snapshot.get("timestamp") or ""),
        events=list(snapshot.get("events") or []),
        event_id=str(snapshot.get("event_id") or ""),
        run_id=str(snapshot.get("run_id") or ""),
        trace_id=str(snapshot.get("trace_id") or ""),
        source_event_id=str(snapshot.get("source_event_id") or ""),
        ingest_key=str(snapshot.get("ingest_key") or ""),
        ingest_payload_hash=str(snapshot.get("ingest_payload_hash") or ""),
        camera_id=str(snapshot.get("camera_id") or ""),
        evidence_id=str(snapshot.get("evidence_id") or ""),
        owner_id=str(snapshot.get("owner_id") or ""),
        execution_attempt=int(snapshot.get("execution_attempt") or 0),
        raw_json=dict(snapshot.get("raw_json") or {}),
        image_url=str(snapshot.get("image_url") or ""),
        llm_analysis=snapshot.get("llm_analysis"),
        llm_recommendation=dict(snapshot.get("llm_recommendation") or {}),
        llm_status=str(snapshot.get("llm_status") or "pending"),
        llm_error=str(snapshot.get("llm_error") or ""),
        llm_latency_ms=float(snapshot.get("llm_latency_ms") or 0),
        llm_json_valid=bool(snapshot.get("llm_json_valid", False)),
        llm_model=str(snapshot.get("llm_model") or ""),
        prompt_version=str(snapshot.get("prompt_version") or ""),
        context_manifest=dict(snapshot.get("context_manifest") or {}),
        evidence_replan=dict(snapshot.get("evidence_replan") or {}),
        failure_attributions=list(snapshot.get("failure_attributions") or []),
        repair_trace=dict(snapshot.get("repair_trace") or {}),
        sop_retrieval=dict(snapshot.get("sop_retrieval") or {}),
        rag_status=str(snapshot.get("rag_status") or "not_run"),
        dispatch_decision=dict(snapshot.get("dispatch_decision") or {}),
        dispatch_actions=list(snapshot.get("dispatch_actions") or []),
        approval_id=str(snapshot.get("approval_id") or ""),
        approval_status=str(snapshot.get("approval_status") or "auto"),
        execution_id=str(snapshot.get("execution_id") or ""),
        execution_status=str(snapshot.get("execution_status") or ""),
        execution_result=str(snapshot.get("execution_result") or ""),
        execution_actions=list(snapshot.get("execution_actions") or []),
        lifecycle_status=str(snapshot.get("lifecycle_status") or "analyzing"),
        timeline=list(snapshot.get("timeline") or []),
    )
    if alarm_dir and event.image_url:
        filename = Path(urlparse(event.image_url).path).name
        candidate = Path(alarm_dir) / filename
        if filename and candidate.is_file():
            event.image_bytes = candidate.read_bytes()
    return event


class RunStore:
    def __init__(self, database_path: str):
        self.database_path = str(database_path)
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.database_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    source TEXT,
                    source_event_id TEXT,
                    ingest_key TEXT,
                    ingest_payload_hash TEXT,
                    camera_id TEXT,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    recovery_count INTEGER NOT NULL DEFAULT 0,
                    owner_id TEXT,
                    lease_until REAL,
                    heartbeat_at REAL,
                    execution_attempt INTEGER NOT NULL DEFAULT 0,
                    event_json TEXT NOT NULL,
                    last_error_type TEXT,
                    last_error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_event ON agent_runs(event_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_run_transitions_run ON run_transitions(run_id)")

            columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
            migrations = {
                "source_event_id": "ALTER TABLE agent_runs ADD COLUMN source_event_id TEXT",
                "ingest_key": "ALTER TABLE agent_runs ADD COLUMN ingest_key TEXT",
                "ingest_payload_hash": "ALTER TABLE agent_runs ADD COLUMN ingest_payload_hash TEXT",
                "camera_id": "ALTER TABLE agent_runs ADD COLUMN camera_id TEXT",
                "owner_id": "ALTER TABLE agent_runs ADD COLUMN owner_id TEXT",
                "lease_until": "ALTER TABLE agent_runs ADD COLUMN lease_until REAL",
                "heartbeat_at": "ALTER TABLE agent_runs ADD COLUMN heartbeat_at REAL",
                "execution_attempt": (
                    "ALTER TABLE agent_runs ADD COLUMN execution_attempt INTEGER NOT NULL DEFAULT 0"
                ),
            }
            for name, sql in migrations.items():
                if name not in columns:
                    conn.execute(sql)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_ingest_key "
                "ON agent_runs(ingest_key) WHERE ingest_key IS NOT NULL"
            )

    def create(self, event: AlarmEvent, source: str) -> None:
        run, created = self.create_or_get(event, source)
        if not created:
            raise RuntimeError(f"run_already_exists:{run['run_id']}")

    def create_or_get(self, event: AlarmEvent, source: str, *,
                      initial_status: str = "analyzing",
                      initial_stage: str = "analysis",
                      transition_detail: str = "run_created") -> tuple[dict, bool]:
        """Atomically create a Run or return the Run owning this ingress identity.

        Correctness relies on the SQLite unique index, not on a query-before-insert
        sequence. A null ingest key preserves the legacy create-every-time behavior.
        """
        if initial_status not in {"analyzing", "filtered"}:
            raise ValueError(f"unsupported_initial_run_status:{initial_status}")
        now = datetime.now().isoformat()
        payload = json.dumps(event_snapshot(event), ensure_ascii=False, default=str)
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "INSERT INTO agent_runs "
                "(run_id,trace_id,event_id,source,source_event_id,ingest_key,"
                "ingest_payload_hash,camera_id,status,stage,event_json,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (
                    event.run_id, event.trace_id, event.event_id, source,
                    event.source_event_id or None, event.ingest_key or None,
                    event.ingest_payload_hash or None, event.camera_id or None,
                    initial_status, initial_stage, payload, now, now,
                ),
            )
            created = cursor.rowcount == 1
            if created:
                conn.execute(
                    "INSERT INTO run_transitions "
                    "(run_id,from_status,to_status,stage,detail,created_at) VALUES (?,?,?,?,?,?)",
                    (
                        event.run_id, None, initial_status, initial_stage,
                        transition_detail, now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM agent_runs WHERE run_id=?", (event.run_id,)
                ).fetchone()
            elif event.ingest_key:
                row = conn.execute(
                    "SELECT * FROM agent_runs WHERE ingest_key=?", (event.ingest_key,)
                ).fetchone()
                if row is not None and str(row["ingest_payload_hash"] or "") != event.ingest_payload_hash:
                    raise IngestConflictError(event.ingest_key)
            else:
                row = None

            if row is None:
                raise RuntimeError("agent_run_insert_conflict_without_ingest_owner")
            result = self._decode_row(row)
        return result or {}, created

    def get_by_ingest_key(self, ingest_key: str, payload_hash: str = "") -> dict | None:
        if not ingest_key:
            return None
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE ingest_key=?", (ingest_key,)
            ).fetchone()
        result = self._decode_row(row)
        if result and payload_hash and str(result.get("ingest_payload_hash") or "") != payload_hash:
            raise IngestConflictError(ingest_key)
        return result

    def get(self, run_id: str) -> dict | None:
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._decode_row(row)

    def claim_run(self, run_id: str, owner_id: str, lease_seconds: float,
                  allowed_statuses: set[str] | None = None) -> dict | None:
        """Atomically acquire an unowned or expired active Run and advance its fence."""
        owner_id = str(owner_id or "").strip()
        if not owner_id:
            raise ValueError("owner_id_required")
        lease_seconds = max(0.1, float(lease_seconds))
        statuses = tuple(sorted(allowed_statuses or ACTIVE_STATUSES))
        if not statuses:
            return None
        now_epoch = time.time()
        placeholders = ",".join("?" for _ in statuses)
        now_text = datetime.now().isoformat()
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "UPDATE agent_runs SET owner_id=?,lease_until=?,heartbeat_at=?,"
                "execution_attempt=execution_attempt+1,version=version+1,updated_at=? "
                f"WHERE run_id=? AND status IN ({placeholders}) "
                "AND (owner_id IS NULL OR owner_id='' OR lease_until IS NULL OR lease_until<=?)",
                (
                    owner_id, now_epoch + lease_seconds, now_epoch, now_text,
                    run_id, *statuses, now_epoch,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            return self._decode_row(row)

    def renew_lease(self, run_id: str, owner_id: str, execution_attempt: int,
                    lease_seconds: float) -> bool:
        now_epoch = time.time()
        statuses = tuple(sorted(ACTIVE_STATUSES))
        placeholders = ",".join("?" for _ in statuses)
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "UPDATE agent_runs SET lease_until=?,heartbeat_at=?,updated_at=? "
                "WHERE run_id=? AND owner_id=? AND execution_attempt=? "
                f"AND lease_until>? AND status IN ({placeholders})",
                (
                    now_epoch + max(0.1, float(lease_seconds)), now_epoch,
                    datetime.now().isoformat(), run_id, owner_id, int(execution_attempt),
                    now_epoch, *statuses,
                ),
            )
            return cursor.rowcount == 1

    def release_run(self, run_id: str, owner_id: str, execution_attempt: int) -> bool:
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "UPDATE agent_runs SET owner_id=NULL,lease_until=NULL,heartbeat_at=NULL,"
                "updated_at=? WHERE run_id=? AND owner_id=? AND execution_attempt=?",
                (datetime.now().isoformat(), run_id, owner_id, int(execution_attempt)),
            )
            return cursor.rowcount == 1

    def recover_expired_runs(self, limit: int = 100) -> list[dict]:
        statuses = tuple(sorted(ACTIVE_STATUSES))
        placeholders = ",".join("?" for _ in statuses)
        now_epoch = time.time()
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM agent_runs WHERE status IN ({placeholders}) "
                "AND (owner_id IS NULL OR owner_id='' OR lease_until IS NULL OR lease_until<=?) "
                "ORDER BY created_at LIMIT ?",
                (*statuses, now_epoch, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def assert_fence(self, run_id: str, owner_id: str, execution_attempt: int) -> dict:
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"run_not_found:{run_id}")
        self._require_fence(row, owner_id, execution_attempt)
        return self._decode_row(row) or {}

    def transition(self, run_id: str, to_status: str, stage: str, detail: str = "",
                   event: AlarmEvent | None = None, expected: set[str] | None = None,
                   error_type: str = "", error_message: str = "",
                   owner_id: str = "", execution_attempt: int | None = None) -> dict:
        now = datetime.now().isoformat()
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(f"run_not_found:{run_id}")
            self._require_fence(row, owner_id, execution_attempt)
            current = str(row["status"])
            if expected is not None and current not in expected:
                raise RuntimeError(f"unexpected_run_state:{current}->{to_status}")
            allowed = ALLOWED_TRANSITIONS.get(current, set())
            if current != to_status and to_status not in allowed:
                raise RuntimeError(f"invalid_run_transition:{current}->{to_status}")
            payload = (
                json.dumps(event_snapshot(event), ensure_ascii=False, default=str)
                if event is not None else row["event_json"]
            )
            release_owner = to_status not in ACTIVE_STATUSES
            where_sql, where_params = self._fence_where(owner_id, execution_attempt)
            cursor = conn.execute(
                "UPDATE agent_runs SET status=?,stage=?,version=version+1,event_json=?,"
                "last_error_type=?,last_error_message=?,updated_at=?,"
                "owner_id=?,lease_until=?,heartbeat_at=? WHERE run_id=? " + where_sql,
                (
                    to_status, stage, payload, error_type, error_message, now,
                    None if release_owner else row["owner_id"],
                    None if release_owner else row["lease_until"],
                    None if release_owner else row["heartbeat_at"],
                    run_id, *where_params,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleRunOwnerError(run_id)
            conn.execute(
                "INSERT INTO run_transitions "
                "(run_id,from_status,to_status,stage,detail,created_at) VALUES (?,?,?,?,?,?)",
                (run_id, current, to_status, stage, detail, now),
            )
        return self.get(run_id) or {}

    def mark_recovery_started(self, run_id: str, detail: str, *,
                              owner_id: str = "",
                              execution_attempt: int | None = None) -> None:
        now = datetime.now().isoformat()
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(f"run_not_found:{run_id}")
            self._require_fence(row, owner_id, execution_attempt)
            where_sql, where_params = self._fence_where(owner_id, execution_attempt)
            cursor = conn.execute(
                "UPDATE agent_runs SET recovery_count=recovery_count+1,updated_at=? "
                "WHERE run_id=? " + where_sql,
                (now, run_id, *where_params),
            )
            if cursor.rowcount != 1:
                raise StaleRunOwnerError(run_id)
            conn.execute(
                "INSERT INTO run_transitions "
                "(run_id,from_status,to_status,stage,detail,created_at) VALUES (?,?,?,?,?,?)",
                (run_id, row["status"], row["status"], "recovery", detail, now),
            )

    def save_snapshot(self, event: AlarmEvent, *, owner_id: str = "",
                      execution_attempt: int | None = None) -> None:
        payload = json.dumps(event_snapshot(event), ensure_ascii=False, default=str)
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (event.run_id,)).fetchone()
            if not row:
                raise KeyError(f"run_not_found:{event.run_id}")
            self._require_fence(row, owner_id, execution_attempt)
            where_sql, where_params = self._fence_where(owner_id, execution_attempt)
            cursor = conn.execute(
                "UPDATE agent_runs SET event_json=?,version=version+1,updated_at=? "
                "WHERE run_id=? " + where_sql,
                (payload, datetime.now().isoformat(), event.run_id, *where_params),
            )
            if cursor.rowcount != 1:
                raise StaleRunOwnerError(event.run_id)

    def patch_event(self, run_id: str, fields: dict, *, owner_id: str = "",
                    execution_attempt: int | None = None) -> None:
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT event_json FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(f"run_not_found:{run_id}")
            full_row = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            self._require_fence(full_row, owner_id, execution_attempt)
            try:
                payload = json.loads(row["event_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            payload.update(fields)
            where_sql, where_params = self._fence_where(owner_id, execution_attempt)
            cursor = conn.execute(
                "UPDATE agent_runs SET event_json=?,version=version+1,updated_at=? "
                "WHERE run_id=? " + where_sql,
                (
                    json.dumps(payload, ensure_ascii=False, default=str),
                    datetime.now().isoformat(), run_id, *where_params,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleRunOwnerError(run_id)

    def list_active(self) -> list[dict]:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM agent_runs WHERE status IN ({placeholders}) ORDER BY created_at",
                tuple(sorted(ACTIVE_STATUSES)),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def list_manual_takeover(self, limit: int = 50) -> list[dict]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_runs WHERE status='manual_takeover' "
                "ORDER BY updated_at DESC LIMIT ?", (max(1, min(int(limit), 200)),)
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def list_recent(self, limit: int = 500) -> list[dict]:
        """Return a bounded durable sample for read-only metrics projection."""
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def transitions_for_runs(self, run_ids: list[str]) -> list[dict]:
        run_ids = [str(run_id) for run_id in run_ids if run_id]
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM run_transitions WHERE run_id IN ({placeholders}) "
                "ORDER BY run_id,id",
                tuple(run_ids),
            ).fetchall()
        return [dict(row) for row in rows]

    def transitions(self, run_id: str) -> list[dict]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM run_transitions WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _require_fence(row, owner_id: str, execution_attempt: int | None) -> None:
        current_owner = str(row["owner_id"] or "")
        if current_owner:
            if (
                not owner_id
                or execution_attempt is None
                or current_owner != owner_id
                or int(row["execution_attempt"] or 0) != int(execution_attempt)
                or float(row["lease_until"] or 0) <= time.time()
            ):
                raise StaleRunOwnerError(str(row["run_id"]))
        elif owner_id or execution_attempt is not None:
            raise StaleRunOwnerError(str(row["run_id"]))

    @staticmethod
    def _fence_where(owner_id: str, execution_attempt: int | None) -> tuple[str, tuple]:
        if owner_id and execution_attempt is not None:
            return (
                "AND owner_id=? AND execution_attempt=? AND lease_until>?",
                (owner_id, int(execution_attempt), time.time()),
            )
        return "AND (owner_id IS NULL OR owner_id='')", ()

    @staticmethod
    def _decode_row(row) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        try:
            result["event"] = json.loads(result.pop("event_json"))
        except (TypeError, json.JSONDecodeError):
            result["event"] = {}
            result.pop("event_json", None)
        return result
