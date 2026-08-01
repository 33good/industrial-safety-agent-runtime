"""Bounded daemon-task runner for local multimodal inference."""
from __future__ import annotations

import threading
from collections.abc import Callable


class AnalysisLimiter:
    """Bound in-flight daemon analyses without blocking event ingestion.

    The VLM transport owns its request timeout. A timed-out caller may continue running
    briefly in the background, so the slot is released only when that worker actually
    exits. This prevents repeated timeouts from creating an unbounded number of threads.
    """

    def __init__(self, max_inflight: int = 2):
        self.max_inflight = max(1, int(max_inflight))
        self._slots = threading.BoundedSemaphore(self.max_inflight)
        self._lock = threading.Lock()
        self._inflight = 0
        self._rejected = 0

    def try_start(self, target: Callable[[], None], name: str = "agent-analysis") -> threading.Event | None:
        if not self._slots.acquire(blocking=False):
            with self._lock:
                self._rejected += 1
            return None

        completed = threading.Event()
        with self._lock:
            self._inflight += 1

        def run() -> None:
            try:
                target()
            finally:
                with self._lock:
                    self._inflight -= 1
                self._slots.release()
                completed.set()

        threading.Thread(target=run, name=name, daemon=True).start()
        return completed

    def status(self) -> dict:
        with self._lock:
            return {
                "max_inflight": self.max_inflight,
                "inflight": self._inflight,
                "available": self.max_inflight - self._inflight,
                "rejected_total": self._rejected,
            }
