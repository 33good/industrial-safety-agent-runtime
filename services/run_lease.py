"""Lease heartbeat for fenced Agent Run execution."""
from __future__ import annotations

import threading

from .run_store import StaleRunOwnerError


class RunLeaseHeartbeat:
    """Keep one claimed Run alive and expose a synchronous ownership guard."""

    def __init__(self, run_store, run_id: str, owner_id: str,
                 execution_attempt: int, lease_seconds: float,
                 heartbeat_seconds: float):
        self.run_store = run_store
        self.run_id = run_id
        self.owner_id = owner_id
        self.execution_attempt = int(execution_attempt)
        self.lease_seconds = max(0.1, float(lease_seconds))
        self.heartbeat_seconds = max(
            0.05, min(float(heartbeat_seconds), self.lease_seconds / 2)
        )
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self.ensure_owned()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"run-heartbeat-{self.run_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.2, self.heartbeat_seconds * 2))

    def ensure_owned(self) -> None:
        if self._lost.is_set():
            raise StaleRunOwnerError(self.run_id)
        self.run_store.assert_fence(
            self.run_id, self.owner_id, self.execution_attempt
        )

    def _loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            if not self.run_store.renew_lease(
                self.run_id,
                self.owner_id,
                self.execution_attempt,
                self.lease_seconds,
            ):
                self._lost.set()
                return
