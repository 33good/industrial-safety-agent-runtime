"""Persistent, idempotent execution boundary for Agent tools."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import urllib.error
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .run_store import ACTIVE_STATUSES, StaleRunOwnerError


POLICY_VERSION = "tool-policy-v1"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    max_attempts: int = 1
    backoff_seconds: float = 0.0

    def __post_init__(self):
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds must be >= 0")


@dataclass
class ToolOutcome:
    status: str
    result: Any = None
    attempts: int = 0
    execution_id: str = ""
    step_id: str = ""
    idempotency_key: str = ""
    error_type: str = ""
    error_message: str = ""
    reused: bool = False

    def as_dispatch_result(self, tool: str, action: str) -> dict:
        display_result = self.result
        if self.status != "succeeded":
            display_result = f"失败: {self.error_type or self.status}: {self.error_message}".rstrip(": ")
        return {
            "tool": tool,
            "action": action,
            "status": self.status,
            "result": display_result,
            "attempts": self.attempts,
            "execution_id": self.execution_id,
            "step_id": self.step_id,
            "idempotency_key": self.idempotency_key,
            "reused": self.reused,
            "error_type": self.error_type,
        }
class ToolExecutionStore:
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
                CREATE TABLE IF NOT EXISTS tool_executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_id TEXT NOT NULL UNIQUE,
                    run_id TEXT,
                    event_id TEXT,
                    step_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    policy_version TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner_id TEXT,
                    execution_attempt INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_runs ON tool_executions(run_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_events ON tool_executions(event_id)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tool_executions)").fetchall()}
            if "owner_id" not in columns:
                conn.execute("ALTER TABLE tool_executions ADD COLUMN owner_id TEXT")
            if "execution_attempt" not in columns:
                conn.execute("ALTER TABLE tool_executions ADD COLUMN execution_attempt INTEGER")

    def assert_fence(self, run_id: str, owner_id: str, execution_attempt: int | None) -> None:
        if not owner_id and execution_attempt is None:
            return
        with self._lock, self._connection() as conn:
            self._assert_fence_conn(conn, run_id, owner_id, execution_attempt)

    def get(self, idempotency_key: str) -> dict | None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM tool_executions WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            return dict(row) if row else None

    def list_for_run(self, run_id: str) -> list[dict]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_executions WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_for_runs(self, run_ids: list[str]) -> list[dict]:
        run_ids = [str(run_id) for run_id in run_ids if run_id]
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM tool_executions WHERE run_id IN ({placeholders}) "
                "ORDER BY id",
                tuple(run_ids),
            ).fetchall()
        return [dict(row) for row in rows]

    def begin(self, *, execution_id: str, run_id: str, event_id: str, step_id: str,
              idempotency_key: str, tool: str, action: str,
              owner_id: str = "", execution_attempt: int | None = None) -> None:
        now = datetime.now().isoformat()
        with self._lock, self._connection() as conn:
            self._assert_fence_conn(conn, run_id, owner_id, execution_attempt)
            conn.execute(
                "INSERT INTO tool_executions "
                "(execution_id,run_id,event_id,step_id,idempotency_key,policy_version,"
                "tool,action,status,owner_id,execution_attempt,started_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (execution_id, run_id, event_id, step_id, idempotency_key, POLICY_VERSION,
                 tool, action, "running", owner_id or None, execution_attempt, now, now),
            )

    def record_attempt(self, idempotency_key: str, attempts: int, error_type: str = "",
                       error_message: str = "", *, run_id: str = "",
                       owner_id: str = "", execution_attempt: int | None = None) -> None:
        with self._lock, self._connection() as conn:
            self._assert_fence_conn(conn, run_id, owner_id, execution_attempt)
            fence_sql, fence_params = self._execution_fence_where(
                run_id, owner_id, execution_attempt
            )
            cursor = conn.execute(
                "UPDATE tool_executions SET attempts=?,error_type=?,error_message=?,updated_at=? "
                "WHERE idempotency_key=? " + fence_sql,
                (
                    attempts, error_type, error_message, datetime.now().isoformat(),
                    idempotency_key, *fence_params,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleRunOwnerError(run_id)

    def finish(self, idempotency_key: str, status: str, result: Any = None,
               error_type: str = "", error_message: str = "", *, run_id: str = "",
               owner_id: str = "", execution_attempt: int | None = None) -> None:
        result_json = json.dumps(result, ensure_ascii=False, default=str)
        now = datetime.now().isoformat()
        with self._lock, self._connection() as conn:
            self._assert_fence_conn(conn, run_id, owner_id, execution_attempt)
            fence_sql, fence_params = self._execution_fence_where(
                run_id, owner_id, execution_attempt
            )
            cursor = conn.execute(
                "UPDATE tool_executions SET status=?,result_json=?,error_type=?,error_message=?,"
                "completed_at=?,updated_at=? WHERE idempotency_key=? " + fence_sql,
                (
                    status, result_json, error_type, error_message, now, now,
                    idempotency_key, *fence_params,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleRunOwnerError(run_id)

    @staticmethod
    def _execution_fence_where(run_id: str, owner_id: str,
                               execution_attempt: int | None) -> tuple[str, tuple]:
        if owner_id and execution_attempt is not None:
            return (
                "AND run_id=? AND owner_id=? AND execution_attempt=?",
                (run_id, owner_id, int(execution_attempt)),
            )
        return "", ()

    @staticmethod
    def _assert_fence_conn(conn, run_id: str, owner_id: str,
                           execution_attempt: int | None) -> None:
        if not owner_id and execution_attempt is None:
            return
        row = conn.execute(
            "SELECT run_id,owner_id,execution_attempt,lease_until,status "
            "FROM agent_runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if (
            row is None
            or str(row["owner_id"] or "") != owner_id
            or int(row["execution_attempt"] or 0) != int(execution_attempt or 0)
            or float(row["lease_until"] or 0) <= time.time()
            or str(row["status"] or "") not in ACTIVE_STATUSES
        ):
            raise StaleRunOwnerError(run_id)


class ToolExecutor:
    """Execute registered tools once per idempotency key with bounded safe retries.

    Tool adapters remain responsible for network deadlines. The executor retries only
    failures classified as transient; an indeterminate/running prior execution is never
    replayed automatically because its external side effect may already have occurred.
    """

    def __init__(self, database_path: str):
        self.store = ToolExecutionStore(database_path)
        self._handlers: dict[str, Callable] = {}
        self._specs: dict[str, ToolSpec] = {}
        self._key_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def register(self, name: str, handler: Callable, spec: ToolSpec | None = None) -> None:
        self._handlers[name] = handler
        self._specs[name] = spec or ToolSpec(name=name)

    def execute(self, event, tool: str, action: str) -> ToolOutcome:
        handler = self._handlers.get(tool)
        if handler is None:
            return ToolOutcome(status="failed", error_type="tool_not_registered", error_message=tool)

        run_id = str(getattr(event, "run_id", "") or "")
        event_id = str(getattr(event, "event_id", "") or "")
        owner_id = str(getattr(event, "owner_id", "") or "")
        execution_attempt = getattr(event, "execution_attempt", None)
        if not owner_id:
            execution_attempt = None
        identity = f"{event_id}|{tool}.{action}|{POLICY_VERSION}"
        idempotency_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        key_lock = self._key_lock(idempotency_key)

        with key_lock:
            self.store.assert_fence(run_id, owner_id, execution_attempt)
            existing = self.store.get(idempotency_key)
            if existing:
                return self._existing_outcome(existing)

            execution_id = "TOOL_" + uuid.uuid4().hex
            step_id = "STEP_" + uuid.uuid4().hex
            self.store.begin(
                execution_id=execution_id,
                run_id=run_id,
                event_id=event_id,
                step_id=step_id,
                idempotency_key=idempotency_key,
                tool=tool,
                action=action,
                owner_id=owner_id,
                execution_attempt=execution_attempt,
            )
            spec = self._specs.get(tool) or ToolSpec(name=tool)
            for attempt in range(1, spec.max_attempts + 1):
                try:
                    result = handler(event, action)
                    self.store.record_attempt(
                        idempotency_key, attempt, run_id=run_id,
                        owner_id=owner_id, execution_attempt=execution_attempt,
                    )
                    self.store.finish(
                        idempotency_key, "succeeded", result=result, run_id=run_id,
                        owner_id=owner_id, execution_attempt=execution_attempt,
                    )
                    return ToolOutcome(
                        status="succeeded", result=result, attempts=attempt,
                        execution_id=execution_id, step_id=step_id,
                        idempotency_key=idempotency_key,
                    )
                except StaleRunOwnerError:
                    raise
                except Exception as exc:
                    error_type, transient = self._classify_error(exc)
                    self.store.record_attempt(
                        idempotency_key, attempt, error_type, str(exc), run_id=run_id,
                        owner_id=owner_id, execution_attempt=execution_attempt,
                    )
                    if transient and attempt < spec.max_attempts:
                        if spec.backoff_seconds:
                            time.sleep(spec.backoff_seconds * (2 ** (attempt - 1)))
                        continue
                    self.store.finish(
                        idempotency_key, "failed", error_type=error_type,
                        error_message=str(exc), run_id=run_id, owner_id=owner_id,
                        execution_attempt=execution_attempt,
                    )
                    return ToolOutcome(
                        status="failed", attempts=attempt, execution_id=execution_id,
                        step_id=step_id, idempotency_key=idempotency_key,
                        error_type=error_type, error_message=str(exc),
                    )
        raise AssertionError("unreachable")

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    @staticmethod
    def _existing_outcome(row: dict) -> ToolOutcome:
        result = None
        if row.get("result_json"):
            try:
                result = json.loads(row["result_json"])
            except json.JSONDecodeError:
                result = row["result_json"]
        status = row.get("status", "failed")
        error_type = row.get("error_type") or ""
        error_message = row.get("error_message") or ""
        if status == "running":
            status = "indeterminate"
            error_type = "previous_execution_indeterminate"
            error_message = "manual review required before replay"
        return ToolOutcome(
            status=status,
            result=result,
            attempts=int(row.get("attempts") or 0),
            execution_id=row.get("execution_id") or "",
            step_id=row.get("step_id") or "",
            idempotency_key=row.get("idempotency_key") or "",
            error_type=error_type,
            error_message=error_message,
            reused=True,
        )

    @staticmethod
    def _classify_error(exc: Exception) -> tuple[str, bool]:
        if isinstance(exc, (TimeoutError, urllib.error.URLError)):
            return "timeout_or_network", True
        if isinstance(exc, ConnectionError):
            return "connection_error", True
        if isinstance(exc, sqlite3.OperationalError):
            return "database_operational_error", True
        return type(exc).__name__, False
