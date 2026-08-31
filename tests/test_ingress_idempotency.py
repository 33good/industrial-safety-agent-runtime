import base64
from contextlib import contextmanager
import http.client
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from agents import AlarmEvent
from backend import ApiHandler
from services.agent_runtime import AgentRuntime
from benchmarks.scenario_fixtures import scenario_alarm_body
from services.run_store import IngestConflictError, RunStore
from services.tool_executor import ToolSpec


def _test_context_manifest():
    return {
        "schema_version": "agent-context-v1",
        "builder_version": "context-builder-v1.0",
        "status": "built",
        "token_budget": 1200,
        "estimated_tokens": 1,
        "budget_utilization_pct": 0.08,
        "budget_overflow_tokens": 0,
        "truncated": False,
        "critical_evidence_retained": True,
        "input_item_count": 0,
        "selected_item_count": 0,
        "dropped_item_count": 0,
        "selected_items": [],
        "dropped_items": [],
        "selected_citation_ids": [],
        "context_sha256": "c" * 64,
        "model_input_sha256": "m" * 64,
        "source_versions": {},
    }


class Broadcaster:
    def __init__(self):
        self.messages = []
        self._lock = threading.Lock()

    def publish(self, message):
        with self._lock:
            self.messages.append(message)


class SideEffects:
    def __init__(self):
        self.counts = {}
        self._lock = threading.Lock()

    def handler(self, name):
        def invoke(event, action):
            with self._lock:
                self.counts[name] = self.counts.get(name, 0) + 1
            return {"ok": True, "tool": name, "action": action}

        return invoke


def settings_for(root: Path):
    return SimpleNamespace(
        alarm_dir=root / "alarms", database_path=root / "runtime.db",
        pending_dir=root / "pending", report_dir=root / "reports",
        execution_dir=root / "executions", notify_webhook="",
        notify_platform="dingtalk", notify_image_required=False,
        notify_image_check_attempts=1, notify_image_check_timeout_seconds=1,
        llm_mode="benchmark", ollama_model="mock", ollama_url="http://127.0.0.1:1",
        llm_timeout_seconds=1, llm_max_inflight=4, vision_min_hits=1,
        vision_event_cooldown_seconds=0, camera_id="camera-default",
        public_url="", http_port=5000,
    )


def alarm_body(camera_id="camera-01"):
    body = scenario_alarm_body("b_ppe")
    body["cameraId"] = camera_id
    return body


class IngressIdempotencyTests(unittest.TestCase):
    def _runtime(self, root: Path):
        broadcaster = Broadcaster()
        runtime = AgentRuntime(settings_for(root), broadcaster)
        runtime.COOLDOWNS = {name: 0 for name in runtime.COOLDOWNS}

        def analyze(event):
            event.llm_status = "success"
            event.llm_json_valid = True
            event.llm_recommendation = {"risk_level": "B", "confidence": 0.95}
            event.llm_analysis = "idempotency-test"
            event.llm_model = "mock"
            event.prompt_version = "test-prompt-v1"
            event.context_manifest = _test_context_manifest()
            event.sop_retrieval = {
                "status": "no_evidence", "catalog_version": "test-catalog-v1",
                "citations": [], "refusal_reason": "test",
            }
            event.rag_status = "refused_no_evidence"
            return event.llm_analysis

        runtime.safety.analyze = analyze
        effects = SideEffects()
        for name in ("human_loop", "database", "notifier", "reporter"):
            runtime.dispatch.register_tool(
                name, effects.handler(name), ToolSpec(name=name, max_attempts=1),
            )
        return runtime, broadcaster, effects

    @contextmanager
    def _http_runtime(self, root: Path):
        runtime, broadcaster, effects = self._runtime(root)

        class HttpApplication:
            def ingest_external_detection(self, body, image, source_event_id=""):
                return runtime.ingest_detection(
                    body, image, source="external", source_event_id=source_event_id,
                )

        HttpApplication.runtime = runtime

        previous_app = ApiHandler.app
        ApiHandler.app = HttpApplication()
        server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.01), daemon=True,
        )
        thread.start()
        try:
            yield runtime, broadcaster, effects, server.server_address[1]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            ApiHandler.app = previous_app

    @staticmethod
    def _post_alarm(port, body, *, idempotency_key="", image=b""):
        payload = json.dumps({
            "body": body,
            "image_base64": base64.b64encode(image).decode("ascii"),
        }, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("POST", "/alarm", body=payload, headers=headers)
            response = connection.getresponse()
            response_body = json.loads(response.read().decode("utf-8"))
            return response.status, response_body
        finally:
            connection.close()

    @staticmethod
    def _get_json(port, path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            response_body = json.loads(response.read().decode("utf-8"))
            return response.status, response_body
        finally:
            connection.close()

    @staticmethod
    def _run_count(runtime):
        conn = sqlite3.connect(runtime.run_store.database_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        finally:
            conn.close()

    @staticmethod
    def _wait_for_terminal(runtime, run_id):
        deadline = time.time() + 5
        row = runtime.run_store.get(run_id)
        while row and row["status"] not in {"succeeded", "manual_takeover"} and time.time() < deadline:
            time.sleep(0.01)
            row = runtime.run_store.get(run_id)
        return row

    @staticmethod
    def _wait_for_pipeline_messages(broadcaster, expected):
        deadline = time.time() + 5
        while time.time() < deadline:
            completed = sum(
                message.get("type") == "alarm_with_llm" for message in broadcaster.messages
            )
            if completed >= expected:
                return
            time.sleep(0.01)
        raise AssertionError(f"expected {expected} completed pipeline messages")

    def test_same_event_submitted_twenty_times_creates_one_run_and_one_side_effect_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, broadcaster, effects = self._runtime(Path(tmp))
            body = alarm_body()

            results = [
                runtime.ingest_detection(body, b"", source="external", source_event_id="alarm-001")
                for _ in range(20)
            ]
            row = self._wait_for_terminal(runtime, results[0]["run_id"])
            self._wait_for_pipeline_messages(broadcaster, 1)

            self.assertEqual({item["run_id"] for item in results}, {results[0]["run_id"]})
            self.assertEqual(sum(not item["reused"] for item in results), 1)
            self.assertEqual(sum(item["reused"] for item in results), 19)
            self.assertEqual(self._run_count(runtime), 1)
            self.assertEqual(row["status"], "succeeded")
            self.assertEqual(effects.counts, {
                "human_loop": 1, "database": 1, "notifier": 1, "reporter": 1,
            })
            self.assertEqual(
                sum(message.get("type") == "alarm" for message in broadcaster.messages), 1,
            )

    def test_filtered_event_is_persisted_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, broadcaster, effects = self._runtime(Path(tmp))
            body = {"cameraId": "camera-01", "objInfo": []}

            first = runtime.ingest_detection(
                body, b"", source="external", source_event_id="filtered-001",
            )
            second = runtime.ingest_detection(
                body, b"", source="external", source_event_id="filtered-001",
            )

            self.assertEqual(first["status"], "filtered")
            self.assertEqual(second["status"], "filtered")
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertEqual(runtime.run_store.get(first["run_id"])["status"], "filtered")
            self.assertEqual(self._run_count(runtime), 1)
            self.assertEqual(effects.counts, {})
            self.assertEqual(broadcaster.messages, [])

    def test_cooldown_filtered_event_cannot_become_a_run_after_cooldown_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, broadcaster, effects = self._runtime(Path(tmp))
            runtime.COOLDOWNS = {name: 60 for name in runtime.COOLDOWNS}
            body = alarm_body()

            accepted = runtime.ingest_detection(
                body, b"", source="external", source_event_id="cooldown-seed",
            )
            filtered = runtime.ingest_detection(
                body, b"", source="external", source_event_id="cooldown-target",
            )
            runtime._last_report.clear()  # Simulate the reporting cooldown expiring.
            replayed = runtime.ingest_detection(
                body, b"", source="external", source_event_id="cooldown-target",
            )

            self.assertEqual(filtered["status"], "filtered")
            self.assertFalse(filtered["reused"])
            self.assertEqual(replayed["status"], "filtered")
            self.assertTrue(replayed["reused"])
            self.assertEqual(filtered["run_id"], replayed["run_id"])
            self.assertEqual(self._run_count(runtime), 2)
            self._wait_for_terminal(runtime, accepted["run_id"])
            self._wait_for_pipeline_messages(broadcaster, 1)
            self.assertEqual(effects.counts, {
                "human_loop": 1, "database": 1, "notifier": 1, "reporter": 1,
            })

    def test_twenty_concurrent_submissions_create_one_run_and_one_side_effect_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, broadcaster, effects = self._runtime(Path(tmp))
            body = alarm_body()
            barrier = threading.Barrier(20)

            def submit(_):
                barrier.wait(timeout=5)
                return runtime.ingest_detection(
                    body, b"", source="external", source_event_id="alarm-concurrent",
                )

            with ThreadPoolExecutor(max_workers=20) as pool:
                results = list(pool.map(submit, range(20)))
            row = self._wait_for_terminal(runtime, results[0]["run_id"])
            self._wait_for_pipeline_messages(broadcaster, 1)

            self.assertEqual(len({item["event_id"] for item in results}), 1)
            self.assertEqual(len({item["run_id"] for item in results}), 1)
            self.assertEqual(len({item["trace_id"] for item in results}), 1)
            self.assertEqual(sum(not item["reused"] for item in results), 1)
            self.assertEqual(self._run_count(runtime), 1)
            self.assertEqual(row["status"], "succeeded")
            self.assertEqual(effects.counts, {
                "human_loop": 1, "database": 1, "notifier": 1, "reporter": 1,
            })

    def test_sqlite_unique_ingest_key_has_one_winner_across_store_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            database_path = str(Path(tmp) / "runtime.db")
            stores = [RunStore(database_path) for _ in range(20)]
            barrier = threading.Barrier(20)

            def create(index):
                event = AlarmEvent(
                    timestamp="test",
                    event_id=f"EVT_DB_{index}",
                    run_id=f"RUN_DB_{index}",
                    trace_id=f"TRACE_DB_{index}",
                    source_event_id="upstream-db-race",
                    ingest_key="shared-ingest-key",
                    ingest_payload_hash="shared-payload-hash",
                    camera_id="camera-db",
                    events=[{
                        "type": "未戴安全帽", "level": "B",
                        "bbox": {"x": 1, "y": 1, "width": 1, "height": 1},
                        "detail": "test",
                    }],
                )
                barrier.wait(timeout=5)
                run, created = stores[index].create_or_get(event, "external")
                return run, created

            with ThreadPoolExecutor(max_workers=20) as pool:
                results = list(pool.map(create, range(20)))

            winners = [run for run, created in results if created]
            self.assertEqual(len(winners), 1)
            self.assertEqual({run["run_id"] for run, _ in results}, {winners[0]["run_id"]})
            self.assertEqual(self._run_count(SimpleNamespace(run_store=stores[0])), 1)

    def test_same_source_event_id_is_isolated_by_camera(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, broadcaster, _ = self._runtime(Path(tmp))
            first = runtime.ingest_detection(
                alarm_body("camera-01"), b"", source="external", source_event_id="alarm-002",
            )
            second = runtime.ingest_detection(
                alarm_body("camera-02"), b"", source="external", source_event_id="alarm-002",
            )

            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertFalse(first["reused"])
            self.assertFalse(second["reused"])
            self.assertEqual(self._run_count(runtime), 2)
            self._wait_for_terminal(runtime, first["run_id"])
            self._wait_for_terminal(runtime, second["run_id"])
            self._wait_for_pipeline_messages(broadcaster, 2)

    def test_same_source_event_id_is_isolated_by_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, broadcaster, _ = self._runtime(Path(tmp))
            body = alarm_body()
            first = runtime.ingest_detection(
                body, b"", source="partner-a", source_event_id="alarm-003",
            )
            second = runtime.ingest_detection(
                body, b"", source="partner-b", source_event_id="alarm-003",
            )

            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertEqual(self._run_count(runtime), 2)
            self._wait_for_terminal(runtime, first["run_id"])
            self._wait_for_terminal(runtime, second["run_id"])
            self._wait_for_pipeline_messages(broadcaster, 2)

    def test_same_key_with_different_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, broadcaster, _ = self._runtime(Path(tmp))
            first_body = alarm_body()
            runtime.ingest_detection(
                first_body, b"", source="external", source_event_id="alarm-conflict",
            )
            changed_body = alarm_body()
            changed_body["objInfo"][0]["confidence"] = 0.51

            with self.assertRaises(IngestConflictError):
                runtime.ingest_detection(
                    changed_body, b"", source="external", source_event_id="alarm-conflict",
                )
            self.assertEqual(self._run_count(runtime), 1)
            self._wait_for_terminal(runtime, runtime.run_store.get_by_ingest_key(
                runtime._ingress_identity(
                    "external", "camera-01", "alarm-conflict", first_body, b""
                )[0]
            )["run_id"])
            self._wait_for_pipeline_messages(broadcaster, 1)

    def test_missing_source_event_id_preserves_create_every_time_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, broadcaster, _ = self._runtime(Path(tmp))
            first = runtime.ingest_detection(alarm_body(), b"", source="external")
            second = runtime.ingest_detection(alarm_body(), b"", source="external")

            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertFalse(first["reused"])
            self.assertFalse(second["reused"])
            self.assertEqual(self._run_count(runtime), 2)
            self._wait_for_terminal(runtime, first["run_id"])
            self._wait_for_terminal(runtime, second["run_id"])
            self._wait_for_pipeline_messages(broadcaster, 2)

    def test_http_header_and_body_idempotency_keys_must_match(self):
        handler = ApiHandler.__new__(ApiHandler)
        handler.headers = {"Idempotency-Key": "alarm-header"}

        self.assertEqual(
            handler._resolve_source_event_id({"source_event_id": "alarm-header"}),
            "alarm-header",
        )
        with self.assertRaises(ValueError):
            handler._resolve_source_event_id({"source_event_id": "alarm-body"})

    def test_http_header_only_request_reuses_original_run(self):
        with tempfile.TemporaryDirectory() as tmp, self._http_runtime(Path(tmp)) as context:
            runtime, broadcaster, _, port = context
            first_status, first = self._post_alarm(
                port, alarm_body(), idempotency_key="http-header-001",
            )
            second_status, second = self._post_alarm(
                port, alarm_body(), idempotency_key="http-header-001",
            )

            self.assertEqual((first_status, second_status), (200, 200))
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertEqual(self._run_count(runtime), 1)
            self._wait_for_pipeline_messages(broadcaster, 1)

    def test_http_body_only_request_reuses_original_run(self):
        with tempfile.TemporaryDirectory() as tmp, self._http_runtime(Path(tmp)) as context:
            runtime, broadcaster, _, port = context
            body = alarm_body()
            body["source_event_id"] = "http-body-001"
            first_status, first = self._post_alarm(port, body)
            second_status, second = self._post_alarm(port, body)

            self.assertEqual((first_status, second_status), (200, 200))
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertEqual(self._run_count(runtime), 1)
            self._wait_for_pipeline_messages(broadcaster, 1)

    def test_http_same_key_with_different_json_returns_409(self):
        with tempfile.TemporaryDirectory() as tmp, self._http_runtime(Path(tmp)) as context:
            runtime, broadcaster, _, port = context
            first_body = alarm_body()
            first_status, first = self._post_alarm(
                port, first_body, idempotency_key="http-json-conflict",
            )
            changed_body = alarm_body()
            changed_body["objInfo"][0]["confidence"] = 0.51
            second_status, second = self._post_alarm(
                port, changed_body, idempotency_key="http-json-conflict",
            )

            self.assertEqual(first_status, 200)
            self.assertEqual(second_status, 409)
            self.assertEqual(second["error"], "idempotency_conflict")
            self.assertEqual(self._run_count(runtime), 1)
            self._wait_for_terminal(runtime, first["run_id"])
            self._wait_for_pipeline_messages(broadcaster, 1)

    def test_http_same_key_with_different_image_returns_409(self):
        with tempfile.TemporaryDirectory() as tmp, self._http_runtime(Path(tmp)) as context:
            runtime, broadcaster, _, port = context
            first_status, first = self._post_alarm(
                port, alarm_body(), idempotency_key="http-image-conflict", image=b"image-one",
            )
            second_status, second = self._post_alarm(
                port, alarm_body(), idempotency_key="http-image-conflict", image=b"image-two",
            )

            self.assertEqual(first_status, 200)
            self.assertEqual(second_status, 409)
            self.assertEqual(second["error"], "idempotency_conflict")
            self.assertEqual(self._run_count(runtime), 1)
            self._wait_for_terminal(runtime, first["run_id"])
            self._wait_for_pipeline_messages(broadcaster, 1)

    def test_http_trace_endpoint_returns_valid_end_to_end_trace(self):
        with tempfile.TemporaryDirectory() as tmp, self._http_runtime(Path(tmp)) as context:
            runtime, broadcaster, _, port = context
            status, created = self._post_alarm(
                port, alarm_body(), idempotency_key="http-trace-001",
            )
            self.assertEqual(status, 200)
            self._wait_for_terminal(runtime, created["run_id"])
            self._wait_for_pipeline_messages(broadcaster, 1)

            trace_status, trace = self._get_json(port, f"/traces/{created['run_id']}")
            self.assertEqual(trace_status, 200)
            self.assertEqual(trace["run"]["run_id"], created["run_id"])
            self.assertEqual(trace["run"]["trace_id"], created["trace_id"])
            self.assertTrue(trace["evidence"]["evidence_id"].startswith("EVID_"))
            self.assertTrue(trace["validation"]["valid"], trace["validation"]["errors"])

    def test_http_runtime_metrics_endpoint_projects_completed_run(self):
        with tempfile.TemporaryDirectory() as tmp, self._http_runtime(Path(tmp)) as context:
            runtime, broadcaster, _, port = context
            status, created = self._post_alarm(
                port, alarm_body(), idempotency_key="http-metrics-001",
            )
            self.assertEqual(status, 200)
            self._wait_for_terminal(runtime, created["run_id"])
            self._wait_for_pipeline_messages(broadcaster, 1)

            metrics_status, metrics = self._get_json(port, "/metrics/runtime?limit=10")
            self.assertEqual(metrics_status, 200)
            self.assertEqual(metrics["schema_version"], "agent-runtime-metrics-v1")
            self.assertEqual(metrics["scope"]["run_count"], 1)
            self.assertEqual(metrics["runs"]["status_counts"].get("succeeded"), 1)
            self.assertEqual(metrics["latency_ms"]["end_to_end"]["count"], 1)

    def test_http_overlong_idempotency_key_returns_400(self):
        with tempfile.TemporaryDirectory() as tmp, self._http_runtime(Path(tmp)) as context:
            runtime, _, _, port = context
            status, response = self._post_alarm(
                port, alarm_body(), idempotency_key="x" * 257,
            )

            self.assertEqual(status, 400)
            self.assertIn("256", response["message"])
            self.assertEqual(self._run_count(runtime), 0)


if __name__ == "__main__":
    unittest.main()
