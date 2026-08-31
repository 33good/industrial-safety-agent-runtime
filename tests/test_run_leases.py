"""M2 acceptance tests for leases, fencing, crash recovery, and side effects."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from agents import AlarmEvent
from services.agent_runtime import AgentRuntime
from services.run_lease import RunLeaseHeartbeat
from services.run_store import RunStore, StaleRunOwnerError
from services.tool_executor import ToolExecutor, ToolSpec


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER = PROJECT_ROOT / "tests" / "fixtures" / "runtime_crash_worker.py"


def event(case_id: str) -> AlarmEvent:
    return AlarmEvent(
        timestamp="test",
        event_id=f"EVT_{case_id}",
        run_id=f"RUN_{case_id}",
        trace_id=f"TRACE_{case_id}",
        events=[{"type": "vehicle_detection", "level": "C", "bbox": {}, "detail": "test"}],
        dispatch_decision={
            "final_level": "C",
            "plan_validation": {"final_plan": ["database.store"]},
        },
    )


def recovery_runtime(database: Path, alarm_dir: Path) -> AgentRuntime:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.run_store = RunStore(str(database))
    runtime.tool_executor = ToolExecutor(str(database))
    runtime.settings = SimpleNamespace(
        alarm_dir=alarm_dir,
        runtime_owner_id="recovery-worker",
        run_lease_seconds=0.5,
        run_heartbeat_seconds=0.1,
    )
    return runtime


class RunLeaseTests(unittest.TestCase):
    def test_two_process_workers_have_exactly_one_claim_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "runtime.db"
            item = event("TWO_WORKERS")
            RunStore(str(database)).create(item, "test")
            gate = root / "start.gate"
            outputs = [root / "worker-a.json", root / "worker-b.json"]
            workers = []
            for index, output in enumerate(outputs):
                workers.append(subprocess.Popen([
                    sys.executable, "-B", str(WORKER), "claim",
                    "--database", str(database), "--run-id", item.run_id,
                    "--owner-id", f"worker-{index}", "--lease-seconds", "5",
                    "--gate", str(gate), "--output", str(output),
                ], cwd=PROJECT_ROOT))
            gate.write_text("go", encoding="utf-8")
            for worker in workers:
                self.assertEqual(worker.wait(timeout=10), 0)
            results = [json.loads(path.read_text(encoding="utf-8")) for path in outputs]
            self.assertEqual(sum(result["claimed"] for result in results), 1)
            self.assertEqual(RunStore(str(database)).get(item.run_id)["execution_attempt"], 1)

    def test_expired_worker_cannot_write_after_new_fence_is_issued(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(str(Path(tmp) / "runtime.db"))
            item = event("STALE")
            store.create(item, "test")
            first = store.claim_run(item.run_id, "worker-old", 0.1)
            self.assertIsNotNone(first)
            time.sleep(0.15)
            second = store.claim_run(item.run_id, "worker-new", 1)
            self.assertIsNotNone(second)
            self.assertEqual(second["execution_attempt"], first["execution_attempt"] + 1)
            with self.assertRaises(StaleRunOwnerError):
                store.transition(
                    item.run_id, "decided", "policy", owner_id="worker-old",
                    execution_attempt=first["execution_attempt"],
                )
            item.owner_id = "worker-old"
            item.execution_attempt = first["execution_attempt"]
            with self.assertRaises(StaleRunOwnerError):
                store.save_snapshot(
                    item, owner_id=item.owner_id,
                    execution_attempt=item.execution_attempt,
                )
            self.assertFalse(store.renew_lease(
                item.run_id, "worker-old", first["execution_attempt"], 1
            ))
            store.transition(
                item.run_id, "decided", "policy", owner_id="worker-new",
                execution_attempt=second["execution_attempt"],
            )

    def test_heartbeat_keeps_a_live_worker_from_being_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(str(Path(tmp) / "runtime.db"))
            item = event("HEARTBEAT")
            store.create(item, "test")
            claimed = store.claim_run(item.run_id, "worker-live", 0.15)
            with RunLeaseHeartbeat(
                store, item.run_id, "worker-live", claimed["execution_attempt"],
                lease_seconds=0.15, heartbeat_seconds=0.05,
            ):
                time.sleep(0.35)
                self.assertIsNone(store.claim_run(item.run_id, "worker-other", 1))

    def test_stale_worker_cannot_persist_tool_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "runtime.db"
            store = RunStore(str(database))
            executor = ToolExecutor(str(database))
            item = event("STALE_TOOL")
            store.create(item, "test")
            first = store.claim_run(item.run_id, "worker-old", 0.1)
            executor.store.begin(
                execution_id="TOOL_STALE", run_id=item.run_id,
                event_id=item.event_id, step_id="STEP_STALE",
                idempotency_key="KEY_STALE", tool="database", action="store",
                owner_id="worker-old", execution_attempt=first["execution_attempt"],
            )
            time.sleep(0.15)
            second = store.claim_run(item.run_id, "worker-new", 1)
            self.assertIsNotNone(second)
            with self.assertRaises(StaleRunOwnerError):
                executor.store.finish(
                    "KEY_STALE", "succeeded", result="late",
                    run_id=item.run_id, owner_id="worker-old",
                    execution_attempt=first["execution_attempt"],
                )
            with self.assertRaises(StaleRunOwnerError):
                executor.store.finish(
                    "KEY_STALE", "succeeded", result="reconciled_without_proof",
                    run_id=item.run_id, owner_id="worker-new",
                    execution_attempt=second["execution_attempt"],
                )
            self.assertEqual(executor.store.get("KEY_STALE")["status"], "running")

    def test_periodic_recovery_catches_a_lease_that_expires_after_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "runtime.db"
            alarm_dir = root / "alarms"
            alarm_dir.mkdir()
            store = RunStore(str(database))
            item = event("LATE_EXPIRY")
            store.create(item, "test")
            first = store.claim_run(item.run_id, "dead-worker", 0.15)
            self.assertIsNotNone(first)

            runtime = recovery_runtime(database, alarm_dir)
            runtime.settings.run_recovery_scan_seconds = 0.5
            resumed = threading.Event()
            runtime._run_agent_pipeline = lambda _event: resumed.set()
            initial = runtime.recover_incomplete_runs()
            self.assertEqual(initial["audited"], 0)
            runtime.start_recovery_monitor()
            try:
                self.assertTrue(resumed.wait(2), "expired Run was not recovered by periodic scan")
            finally:
                runtime._recovery_stop.set()
                runtime._recovery_thread.join(timeout=2)
            recovered = store.get(item.run_id)
            self.assertEqual(recovered["owner_id"], "recovery-worker")
            self.assertEqual(recovered["execution_attempt"], first["execution_attempt"] + 1)

    def test_tool_idempotency_key_survives_worker_fence_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "runtime.db"
            store = RunStore(str(database))
            executor = ToolExecutor(str(database))
            item = event("TOOL_REUSE")
            store.create(item, "test")
            first = store.claim_run(item.run_id, "worker-old", 0.1)
            item.owner_id = "worker-old"
            item.execution_attempt = first["execution_attempt"]
            calls = []
            executor.register(
                "database", lambda _event, _action: calls.append("effect") or "ok",
                ToolSpec("database", max_attempts=1),
            )
            first_outcome = executor.execute(item, "database", "store")
            time.sleep(0.15)
            second = store.claim_run(item.run_id, "worker-new", 1)
            item.owner_id = "worker-new"
            item.execution_attempt = second["execution_attempt"]
            second_outcome = executor.execute(item, "database", "store")
            self.assertEqual(first_outcome.idempotency_key, second_outcome.idempotency_key)
            self.assertTrue(second_outcome.reused)
            self.assertEqual(calls, ["effect"])

    def test_real_process_kill_at_three_boundaries_recovers_safely(self):
        cases = {
            "after_create": "safe_replay",
            "after_side_effect": "manual_takeover",
            "after_tool_result": "reconciled",
        }
        for point, expected in cases.items():
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                database = root / "runtime.db"
                alarm_dir = root / "alarms"
                alarm_dir.mkdir()
                ready = root / "ready"
                marker = root / "effect.txt"
                case_id = point.upper()
                process = subprocess.Popen([
                    sys.executable, "-B", str(WORKER), "crash",
                    "--database", str(database), "--case-id", case_id,
                    "--point", point, "--lease-seconds", "0.25",
                    "--ready", str(ready), "--marker", str(marker),
                ], cwd=PROJECT_ROOT)
                deadline = time.time() + 10
                while not ready.exists() and time.time() < deadline:
                    if process.poll() is not None:
                        self.fail(f"fixture exited early with {process.returncode}")
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "fixture did not reach crash boundary")
                process.kill()
                process.wait(timeout=10)
                time.sleep(0.3)

                store = RunStore(str(database))
                run_id = f"RUN_{case_id}"
                if point == "after_create":
                    claimed = store.claim_run(run_id, "recovery-worker", 1)
                    self.assertIsNotNone(claimed)
                    token = claimed["execution_attempt"]
                    store.transition(
                        run_id, "decided", "policy", owner_id="recovery-worker",
                        execution_attempt=token,
                    )
                    store.transition(
                        run_id, "executing", "tools", owner_id="recovery-worker",
                        execution_attempt=token,
                    )
                    store.transition(
                        run_id, "succeeded", "complete", owner_id="recovery-worker",
                        execution_attempt=token,
                    )
                    self.assertEqual(store.get(run_id)["status"], "succeeded")
                    self.assertFalse(marker.exists())
                    continue

                summary = recovery_runtime(database, alarm_dir).recover_incomplete_runs()
                status = store.get(run_id)["status"]
                self.assertEqual(marker.read_text(encoding="utf-8").count("side-effect"), 1)
                if expected == "manual_takeover":
                    self.assertEqual(status, "manual_takeover")
                    self.assertEqual(summary["manual_takeover"], 1)
                else:
                    self.assertEqual(status, "succeeded")
                    self.assertEqual(summary["finalized"], 1)


if __name__ == "__main__":
    unittest.main()
