"""Durable Agent run state and transition audit log."""
from __future__ import annotations

import json
import sqlite3
import threading
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
        "sop_retrieval": event.sop_retrieval,
        "rag_status": event.rag_status,
        "dispatch_decision": event.dispatch_decision,
        "dispatch_actions": event.dispatch_actions,
        "approval_id": event.approval_id,
        "approval_status": event.approval_status,
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
        sop_retrieval=dict(snapshot.get("sop_retrieval") or {}),
        rag_status=str(snapshot.get("rag_status") or "not_run"),
        dispatch_decision=dict(snapshot.get("dispatch_decision") or {}),
        dispatch_actions=list(snapshot.get("dispatch_actions") or []),
        approval_id=str(snapshot.get("approval_id") or ""),
        approval_status=str(snapshot.get("approval_status") or "auto"),
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

    def transition(self, run_id: str, to_status: str, stage: str, detail: str = "",
                   event: AlarmEvent | None = None, expected: set[str] | None = None,
                   error_type: str = "", error_message: str = "") -> dict:
        now = datetime.now().isoformat()
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(f"run_not_found:{run_id}")
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
            conn.execute(
                "UPDATE agent_runs SET status=?,stage=?,version=version+1,event_json=?,"
                "last_error_type=?,last_error_message=?,updated_at=? WHERE run_id=?",
                (to_status, stage, payload, error_type, error_message, now, run_id),
            )
            conn.execute(
                "INSERT INTO run_transitions "
                "(run_id,from_status,to_status,stage,detail,created_at) VALUES (?,?,?,?,?,?)",
                (run_id, current, to_status, stage, detail, now),
            )
        return self.get(run_id) or {}

    def mark_recovery_started(self, run_id: str, detail: str) -> None:
        now = datetime.now().isoformat()
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT status FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(f"run_not_found:{run_id}")
            conn.execute(
                "UPDATE agent_runs SET recovery_count=recovery_count+1,updated_at=? WHERE run_id=?",
                (now, run_id),
            )
            conn.execute(
                "INSERT INTO run_transitions "
                "(run_id,from_status,to_status,stage,detail,created_at) VALUES (?,?,?,?,?,?)",
                (run_id, row["status"], row["status"], "recovery", detail, now),
            )

    def save_snapshot(self, event: AlarmEvent) -> None:
        payload = json.dumps(event_snapshot(event), ensure_ascii=False, default=str)
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE agent_runs SET event_json=?,version=version+1,updated_at=? WHERE run_id=?",
                (payload, datetime.now().isoformat(), event.run_id),
            )

    def patch_event(self, run_id: str, fields: dict) -> None:
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT event_json FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(f"run_not_found:{run_id}")
            try:
                payload = json.loads(row["event_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            payload.update(fields)
            conn.execute(
                "UPDATE agent_runs SET event_json=?,version=version+1,updated_at=? WHERE run_id=?",
                (json.dumps(payload, ensure_ascii=False, default=str), datetime.now().isoformat(), run_id),
            )

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

    def transitions(self, run_id: str) -> list[dict]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM run_transitions WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

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
