import io
import json
import gc
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from agents import AlarmEvent
from agents.dispatch import DispatchAgent
from agents.perception import PerceptionAgent
from agents.safety_agent import SafetyAgent
from agents.sop_retriever import SOPRetriever
from backend import Application
from benchmarks.run_agent_benchmark import DEFAULT_CASES, build_report, load_cases
from benchmarks.run_runtime_faults import build_report as build_fault_report
from benchmarks.run_sop_benchmark import build_report as build_sop_report
from services.agent_runtime import AgentRuntime, event_payload
from services.analysis_limiter import AnalysisLimiter
from services.evidence import annotate_image
from services.local_vision import DEFAULT_CLASS_MAP, LocalVisionWorker
from services.recent_events import RecentEventStore
from services.run_store import RunStore, restore_event
from services.tool_executor import ToolExecutor, ToolSpec
from tools.actuator import ActuatorTool
from tools.database import DatabaseTool
from tools.notifier import NotifierTool


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "application/json", status: int = 200):
        self._body = io.BytesIO(body)
        self.headers = {"Content-Type": content_type}
        self.status = status

    def read(self, size=-1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class StabilityTests(unittest.TestCase):
    def test_agent_runtime_persists_complete_c_level_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class Broadcaster:
                def __init__(self):
                    self.messages = []
                def publish(self, message):
                    self.messages.append(message)

            settings = SimpleNamespace(
                alarm_dir=root / "alarms", database_path=root / "runtime.db",
                pending_dir=root / "pending", report_dir=root / "reports",
                execution_dir=root / "executions", notify_webhook="",
                notify_platform="dingtalk", notify_image_required=False,
                notify_image_check_attempts=1, notify_image_check_timeout_seconds=1,
                llm_mode="benchmark", ollama_model="mock", ollama_url="http://127.0.0.1:1",
                llm_timeout_seconds=1, llm_max_inflight=1, vision_min_hits=1,
                vision_event_cooldown_seconds=0, camera_id="camera-test",
                public_url="", http_port=5000,
            )
            broadcaster = Broadcaster()
            runtime = AgentRuntime(settings, broadcaster)

            def analyze(event):
                event.llm_status = "success"
                event.llm_json_valid = True
                event.llm_recommendation = {"risk_level": "C", "confidence": 0.9}
                event.llm_analysis = "benchmark"
                return "benchmark"

            runtime.safety.analyze = analyze
            result = runtime.trigger_demo("c_vehicle")
            deadline = time.time() + 3
            row = runtime.run_store.get(result["run_id"])
            while row["status"] not in {"succeeded", "manual_takeover"} and time.time() < deadline:
                time.sleep(0.02)
                row = runtime.run_store.get(result["run_id"])

            self.assertEqual(row["status"], "succeeded")
            self.assertEqual(
                [item["to_status"] for item in runtime.run_store.transitions(result["run_id"])],
                ["analyzing", "decided", "executing", "succeeded"],
            )
            self.assertTrue(any(message.get("type") == "alarm_with_llm" for message in broadcaster.messages))

    def test_a_level_approval_and_actuation_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class Broadcaster:
                def __init__(self):
                    self.messages = []
                def publish(self, message):
                    self.messages.append(message)

            settings = SimpleNamespace(
                alarm_dir=root / "alarms", database_path=root / "runtime.db",
                pending_dir=root / "pending", report_dir=root / "reports",
                execution_dir=root / "executions", notify_webhook="https://example.com/YOUR_KEY",
                notify_platform="dingtalk", notify_image_required=False,
                notify_image_check_attempts=1, notify_image_check_timeout_seconds=1,
                llm_mode="benchmark", ollama_model="mock", ollama_url="http://127.0.0.1:1",
                llm_timeout_seconds=1, llm_max_inflight=1, vision_min_hits=1,
                vision_event_cooldown_seconds=0, camera_id="camera-test",
                public_url="", http_port=5000,
            )
            runtime = AgentRuntime(settings, Broadcaster())

            def analyze(event):
                event.llm_status = "success"
                event.llm_json_valid = True
                event.llm_recommendation = {
                    "risk_level": "A", "confidence": 0.95, "need_human_confirm": True,
                }
                event.llm_analysis = "benchmark"
                return "benchmark"

            runtime.safety.analyze = analyze
            result = runtime.trigger_demo("a_person_vehicle")
            deadline = time.time() + 3
            row = runtime.run_store.get(result["run_id"])
            while row["status"] not in {"waiting_approval", "manual_takeover"} and time.time() < deadline:
                time.sleep(0.02)
                row = runtime.run_store.get(result["run_id"])
            self.assertEqual(row["status"], "waiting_approval")
            approval_id = row["event"]["approval_id"]

            first, first_status = runtime.approve("approve", {
                "approval_id": approval_id, "operator": "tester",
            })
            second, second_status = runtime.approve("approve", {
                "approval_id": approval_id, "operator": "tester",
            })

            self.assertEqual(first_status, 200)
            self.assertEqual(second_status, 200)
            self.assertEqual(first["execution_id"], second["execution_id"])
            self.assertEqual(runtime.run_store.get(result["run_id"])["status"], "succeeded")
            self.assertEqual(len(list((root / "executions").glob("EXEC_*.json"))), 1)

    def test_run_store_persists_snapshot_and_transition_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(str(Path(tmp) / "runtime.db"))
            event = AlarmEvent(
                timestamp="test", event_id="EVT_STATE", run_id="RUN_STATE", trace_id="TRACE_STATE",
                events=[{"type": "车辆检测", "level": "C", "bbox": {}, "detail": "test"}],
                llm_model="qwen2.5vl:7b", prompt_version="prompt-v2",
                sop_retrieval={"status": "retrieved", "citations": [{"citation_id": "TEST#1@1"}]},
                rag_status="grounded",
            )
            store.create(event, "benchmark")
            store.transition(event.run_id, "decided", "policy", event=event)
            store.transition(event.run_id, "executing", "tools", event=event)
            store.transition(event.run_id, "succeeded", "complete", event=event)

            row = store.get(event.run_id)
            restored = restore_event(row["event"])
            self.assertEqual(row["status"], "succeeded")
            self.assertEqual(restored.trace_id, event.trace_id)
            self.assertEqual(restored.llm_model, "qwen2.5vl:7b")
            self.assertEqual(restored.prompt_version, "prompt-v2")
            self.assertEqual(restored.rag_status, "grounded")
            self.assertEqual(restored.sop_retrieval["citations"][0]["citation_id"], "TEST#1@1")
            self.assertEqual(
                [item["to_status"] for item in store.transitions(event.run_id)],
                ["analyzing", "decided", "executing", "succeeded"],
            )

    def test_recovery_marks_running_tool_as_manual_takeover(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "runtime.db")
            alarm_dir = Path(tmp) / "alarms"
            alarm_dir.mkdir()
            store = RunStore(db_path)
            executor = ToolExecutor(db_path)
            event = AlarmEvent(
                timestamp="test", event_id="EVT_UNCERTAIN", run_id="RUN_UNCERTAIN",
                trace_id="TRACE_UNCERTAIN",
                events=[{"type": "车辆检测", "level": "C", "bbox": {}, "detail": "test"}],
                dispatch_decision={"plan_validation": {"final_plan": ["database.store"]}},
            )
            store.create(event, "benchmark")
            store.transition(event.run_id, "decided", "policy", event=event)
            store.transition(event.run_id, "executing", "tools", event=event)
            executor.store.begin(
                execution_id="TOOL_UNCERTAIN", run_id=event.run_id, event_id=event.event_id,
                step_id="STEP_UNCERTAIN", idempotency_key="KEY_UNCERTAIN",
                tool="database", action="store",
            )
            runtime = AgentRuntime.__new__(AgentRuntime)
            runtime.run_store = store
            runtime.tool_executor = executor
            runtime.settings = SimpleNamespace(alarm_dir=alarm_dir)

            summary = runtime.recover_incomplete_runs()

            self.assertEqual(summary["manual_takeover"], 1)
            self.assertEqual(store.get(event.run_id)["status"], "manual_takeover")

    def test_operator_can_auditably_close_manual_takeover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alarm_dir = root / "alarms"
            alarm_dir.mkdir()
            store = RunStore(str(root / "runtime.db"))
            executor = ToolExecutor(str(root / "runtime.db"))
            event = AlarmEvent(
                timestamp="test", event_id="EVT_MANUAL", run_id="RUN_MANUAL",
                trace_id="TRACE_MANUAL",
                events=[{"type": "车辆检测", "level": "C", "bbox": {}, "detail": "test"}],
            )
            store.create(event, "benchmark")
            store.transition(event.run_id, "manual_takeover", "recovery", "injected", event=event)

            class Broadcaster:
                def __init__(self):
                    self.messages = []
                def publish(self, message):
                    self.messages.append(message)

            runtime = AgentRuntime.__new__(AgentRuntime)
            runtime.run_store = store
            runtime.tool_executor = executor
            runtime.settings = SimpleNamespace(alarm_dir=alarm_dir)
            runtime._recent = RecentEventStore(limit=10)
            runtime.broadcaster = Broadcaster()

            result, status = runtime.resolve_recovery({
                "run_id": event.run_id, "resolution": "mark_failed",
                "operator": "safety-owner", "comment": "confirmed external failure",
            })

            self.assertEqual(status, 200)
            self.assertEqual(result["lifecycle_status"], "permanent_failed")
            self.assertEqual(store.get(event.run_id)["status"], "permanent_failed")
            self.assertEqual(runtime.broadcaster.messages[-1]["operator"], "safety-owner")

    def test_recovery_finalizes_run_when_all_tool_results_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "runtime.db")
            alarm_dir = Path(tmp) / "alarms"
            alarm_dir.mkdir()
            store = RunStore(db_path)
            executor = ToolExecutor(db_path)
            event = AlarmEvent(
                timestamp="test", event_id="EVT_RECONCILE", run_id="RUN_RECONCILE",
                trace_id="TRACE_RECONCILE",
                events=[{"type": "车辆检测", "level": "C", "bbox": {}, "detail": "test"}],
                dispatch_decision={"plan_validation": {"final_plan": ["database.store"]}},
            )
            store.create(event, "benchmark")
            store.transition(event.run_id, "decided", "policy", event=event)
            store.transition(event.run_id, "executing", "tools", event=event)
            executor.store.begin(
                execution_id="TOOL_DONE", run_id=event.run_id, event_id=event.event_id,
                step_id="STEP_DONE", idempotency_key="KEY_DONE", tool="database", action="store",
            )
            executor.store.record_attempt("KEY_DONE", 1)
            executor.store.finish("KEY_DONE", "succeeded", result="stored")
            runtime = AgentRuntime.__new__(AgentRuntime)
            runtime.run_store = store
            runtime.tool_executor = executor
            runtime.settings = SimpleNamespace(alarm_dir=alarm_dir)

            summary = runtime.recover_incomplete_runs()

            self.assertEqual(summary["finalized"], 1)
            self.assertEqual(store.get(event.run_id)["status"], "succeeded")

    def test_actuator_reuses_execution_for_same_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            actuator = ActuatorTool(tmp)
            order = {"id": "PENDING_FIXED", "event_id": "EVT_FIXED"}
            first = actuator.execute(order)
            second = actuator.execute(order)
            self.assertEqual(first["execution_id"], second["execution_id"])
            self.assertTrue(second["reused"])
            self.assertEqual(len(list(Path(tmp).glob("EXEC_*.json"))), 1)

    def test_analysis_limiter_rejects_overload_until_worker_really_finishes(self):
        limiter = AnalysisLimiter(max_inflight=1)
        release = threading.Event()
        first = limiter.try_start(lambda: release.wait(2), name="test-analysis")
        self.assertIsNotNone(first)
        self.assertIsNone(limiter.try_start(lambda: None))
        self.assertEqual(limiter.status()["inflight"], 1)
        self.assertEqual(limiter.status()["rejected_total"], 1)

        release.set()
        self.assertTrue(first.wait(2))
        third = limiter.try_start(lambda: None)
        self.assertIsNotNone(third)
        self.assertTrue(third.wait(2))
        self.assertEqual(limiter.status()["inflight"], 0)

    def test_tool_executor_retries_transient_failure_and_reuses_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = ToolExecutor(str(Path(tmp) / "runtime.db"))
            calls = []

            def flaky(event, action):
                calls.append(action)
                if len(calls) == 1:
                    raise TimeoutError("temporary timeout")
                return "sent"

            executor.register("notifier", flaky, ToolSpec("notifier", max_attempts=3))
            event = AlarmEvent(timestamp="test", event_id="EVT_IDEMPOTENT", run_id="RUN_1", events=[])
            first = executor.execute(event, "notifier", "send")
            second = executor.execute(event, "notifier", "send")

            self.assertEqual(first.status, "succeeded")
            self.assertEqual(first.attempts, 2)
            self.assertTrue(second.reused)
            self.assertEqual(second.result, "sent")
            self.assertEqual(calls, ["send", "send"])

    def test_tool_executor_does_not_retry_permanent_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = ToolExecutor(str(Path(tmp) / "runtime.db"))
            calls = []

            def invalid(event, action):
                calls.append(action)
                raise ValueError("invalid arguments")

            executor.register("reporter", invalid, ToolSpec("reporter", max_attempts=3))
            event = AlarmEvent(timestamp="test", event_id="EVT_PERMANENT", run_id="RUN_2", events=[])
            outcome = executor.execute(event, "reporter", "generate")

            self.assertEqual(outcome.status, "failed")
            self.assertEqual(outcome.error_type, "ValueError")
            self.assertEqual(outcome.attempts, 1)
            self.assertEqual(calls, ["generate"])

    def test_dispatch_idempotency_prevents_duplicate_database_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "runtime.db")
            executor = ToolExecutor(db_path)
            database = DatabaseTool(db_path)
            dispatch = DispatchAgent(tool_executor=executor)
            dispatch.register_tool("human_loop", lambda event, action: "C级自动通过")
            dispatch.register_tool("database", lambda event, action: database.handle(event, action))
            event = AlarmEvent(
                timestamp="test",
                event_id="EVT_DISPATCH_IDEMPOTENT",
                run_id="RUN_DISPATCH_IDEMPOTENT",
                trace_id="TRACE_DISPATCH_IDEMPOTENT",
                events=[{"type": "车辆检测", "level": "C", "bbox": {}, "detail": "普通车辆"}],
                llm_recommendation={"risk_level": "C", "confidence": 0.9},
            )

            first = dispatch.dispatch(event)
            second = dispatch.dispatch(event)

            self.assertTrue(all(item["status"] == "succeeded" for item in first))
            self.assertTrue(all(item["reused"] for item in second))
            self.assertEqual(database.count(), 1)
            stored = database.query_recent(hours=1)[0]
            self.assertEqual(stored["run_id"], event.run_id)
            self.assertEqual(stored["trace_id"], event.trace_id)

    def test_agent_policy_benchmark_has_no_failed_cases(self):
        report = build_report(load_cases(DEFAULT_CASES), DEFAULT_CASES)
        self.assertEqual(report["summary"]["failed"], 0)
        self.assertEqual(report["summary"]["forbidden_action_block_rate_pct"], 100.0)
        self.assertEqual(report["summary"]["fallback_success_rate_pct"], 100.0)

    def test_runtime_fault_benchmark_has_no_failed_cases(self):
        report = build_fault_report()
        self.assertEqual(report["summary"]["failed"], 0)

    def test_startup_runs_local_preflight_before_server(self):
        script = Path("start.bat").read_text(encoding="utf-8")
        preflight_at = script.index("preflight.py --startup")
        serve_at = script.index("serve.py")
        self.assertLess(preflight_at, serve_at)
        self.assertNotIn("python recover_", script.lower())

    def test_application_accepts_local_and_generic_event_sources(self):
        class Runtime:
            def __init__(self):
                self.calls = []
            def ingest_detection(self, body, image, source):
                self.calls.append(source)
                return {"status": "ok", "source": source}

        app = Application.__new__(Application)
        app.runtime = Runtime()
        self.assertEqual(app.ingest_external_detection({}, b"")["status"], "ok")
        self.assertEqual(app._ingest_local_detection({}, b"")["status"], "ok")
        self.assertEqual(app.runtime.calls, ["external", "local_yolo"])

    def test_local_inference_failure_pauses_vision_without_disabling_agent(self):
        class Realtime:
            def __init__(self):
                self.messages = []
            def publish(self, message):
                self.messages.append(message)

        app = Application.__new__(Application)
        app.realtime = Realtime()
        app._handle_vision_unavailable("gpu_error")
        message = app.realtime.messages[-1]
        self.assertEqual(message["mode"], "paused")
        self.assertEqual(message["status"], "degraded")

    def test_local_vision_can_be_armed_only_after_model_is_ready(self):
        worker = LocalVisionWorker(None, lambda *_: None, "unused.pt")
        ok, error = worker.activate()
        self.assertFalse(ok)
        self.assertEqual(error, "model_not_ready")
        worker._model = object()
        worker._status = "ready"
        ok, error = worker.activate()
        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertTrue(worker.status()["active"])
        worker.deactivate()
        self.assertFalse(worker.status()["active"])
        self.assertEqual(worker.status()["status"], "ready")

    def test_yolo26_demo_labels_map_to_existing_perception_protocol(self):
        self.assertEqual(DEFAULT_CLASS_MAP["person"], 0)
        self.assertEqual(DEFAULT_CLASS_MAP["helmet"], 1)
        self.assertEqual(DEFAULT_CLASS_MAP["vest"], 2)
        self.assertEqual(DEFAULT_CLASS_MAP["fire"], 3)
        self.assertEqual(DEFAULT_CLASS_MAP["forktruck"], 4)

    def test_explicit_ppe_compliance_does_not_create_violation(self):
        person = {
            "targetType": 0, "targetId": 8, "confidence": 950,
            "posRect": {"x": 20, "y": 600, "width": 100, "height": 220},
            "ppeStatus": {
                "helmet": {"status": "correct", "confidence": 0.98},
                "vest": {"status": "correct", "confidence": 0.97},
            },
        }
        event = PerceptionAgent().process({"objInfo": [person]}, verbose=False)
        self.assertEqual(event.events, [])

    def test_explicit_ppe_missing_creates_b_level_events(self):
        person = {
            "targetType": 0, "targetId": 9, "confidence": 950,
            "posRect": {"x": 20, "y": 600, "width": 100, "height": 220},
            "ppeStatus": {
                "helmet": {"status": "missing", "confidence": 0.96},
                "vest": {"status": "missing", "confidence": 0.91},
            },
        }
        event = PerceptionAgent().process({"objInfo": [person]}, verbose=False)
        self.assertEqual({item["type"] for item in event.events}, {"未戴安全帽", "未穿反光背心"})
        self.assertTrue(all(item["level"] == "B" for item in event.events))

    def test_frontend_exposes_controlled_live_detection_overlay(self):
        html = Path("frontend/index.html").read_text(encoding="utf-8")
        script = Path("frontend/js/app.js").read_text(encoding="utf-8")
        self.assertIn('id="vision-toggle"', html)
        self.assertIn('id="vision-overlay"', html)
        self.assertIn("/vision/mode", script)
        self.assertIn("data.type==='vision_frame'", script)
        self.assertIn("ppe-correct", script)

    def test_detector_confidence_is_normalized_without_random_fallback(self):
        self.assertAlmostEqual(PerceptionAgent._confidence({"confidence": 937}), 0.937)
        self.assertAlmostEqual(PerceptionAgent._confidence({"confidence": 94}), 0.94)
        self.assertAlmostEqual(PerceptionAgent._confidence({"confidence": 0.82}), 0.82)

    def test_person_vehicle_risk_preserves_individual_boxes_for_3d_mapping(self):
        person = {"targetType": 0, "targetId": 101, "confidence": 95,
                  "posRect": {"x": 628, "y": 306, "width": 45, "height": 112}}
        helmet = {"targetType": 1, "targetId": 103, "confidence": 93,
                  "posRect": {"x": 640, "y": 306, "width": 18, "height": 18}}
        vest = {"targetType": 2, "targetId": 102, "confidence": 93,
                "posRect": {"x": 636, "y": 323, "width": 29, "height": 61}}
        vehicle = {"targetType": 4, "targetId": 201, "confidence": 92,
                   "posRect": {"x": 456, "y": 226, "width": 132, "height": 166}}
        risk_box = {"x": 440, "y": 180, "width": 300, "height": 270}
        event = PerceptionAgent().process(
            {"objInfo": [person, helmet, vest, vehicle], "riskBox": risk_box, "focusLevel": "A"},
            verbose=False,
        )
        proximity = next(item for item in event.events if item["type"] == "人车混行风险")
        self.assertEqual(proximity["bbox"], risk_box)
        self.assertEqual(proximity["person_bbox"], person["posRect"])
        self.assertEqual(proximity["vehicle_bbox"], vehicle["posRect"])
        payload = event_payload(proximity)
        self.assertEqual(payload["person_bbox"], person["posRect"])
        self.assertEqual(payload["vehicle_bbox"], vehicle["posRect"])

    def test_rgba_png_evidence_can_receive_a_level_annotation(self):
        source = io.BytesIO()
        Image.new("RGBA", (320, 200), (20, 30, 40, 180)).save(source, format="PNG")
        result = annotate_image(source.getvalue(), [{
            "type": "区域入侵-车辆通道",
            "level": "A",
            "bbox": {"x": 40, "y": 30, "width": 180, "height": 100},
        }])
        self.assertTrue(result.startswith(b"\xff\xd8"))
        self.assertEqual(Image.open(io.BytesIO(result)).mode, "RGB")

    def test_frontend_has_no_remote_runtime_dependency(self):
        html = Path("frontend/index.html").read_text(encoding="utf-8")
        self.assertNotIn("https://cdn.", html)
        self.assertNotIn("https://unpkg.com", html)
        self.assertNotIn("Math.random()*.09", Path("frontend/js/app.js").read_text(encoding="utf-8"))

    def test_repository_quality_gate_is_reproducibly_wired(self):
        workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        env_example = Path(".env.example").read_text(encoding="utf-8")
        self.assertIn("python -B verify.py", workflow)
        self.assertIn("requirements-ci.txt", workflow)
        self.assertTrue(Path("requirements.txt").is_file())
        self.assertTrue(Path("requirements-ci.txt").is_file())
        self.assertIn("VISION_ENABLED=0", env_example)
        self.assertNotIn(r"D:\study", env_example)

    def test_ollama_vision_payload_uses_plural_images_only(self):
        captured = {}

        def fake_open(request, timeout=0):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            body = json.dumps({"message": {"content": '{"risk_level":"B"}'}}).encode()
            return FakeResponse(body)

        event = AlarmEvent(
            timestamp="test",
            events=[{"type": "test", "level": "B", "bbox": {}, "detail": "test"}],
            image_bytes=b"jpeg",
        )
        with patch("urllib.request.urlopen", fake_open):
            SafetyAgent()._call_ollama(event)
        message = captured["payload"]["messages"][0]
        self.assertIn("images", message)
        self.assertNotIn("image", message)
        self.assertEqual(captured["payload"]["format"], "json")
        self.assertEqual(captured["payload"]["options"]["temperature"], 0)

    def test_sop_retrieval_is_traceable_and_refuses_unknown_event(self):
        retriever = SOPRetriever(Path("knowledge/sop/safety_procedures.json"))
        known = retriever.retrieve("人员未佩戴安全帽", ["未戴安全帽"], ["B"])
        unknown = retriever.retrieve("储罐疑似液氨泄漏", ["液氨泄漏"], ["A"])

        self.assertEqual(known["status"], "retrieved")
        self.assertEqual(known["citations"][0]["citation_id"], "PPE-001#4.2-helmet@1.2")
        self.assertTrue(known["citations"][0]["source"])
        self.assertEqual(unknown["status"], "no_evidence")
        self.assertEqual(unknown["citations"], [])
        self.assertTrue(unknown["refusal_reason"])

    def test_model_cannot_invent_sop_citation(self):
        recommendation = SafetyAgent._normalize_recommendation({
            "risk_level": "B",
            "sop_citations": [
                {"citation_id": "PPE-001#4.2-helmet@1.2", "claim": "需要纠正"},
                {"citation_id": "FAKE-999#1@9", "claim": "模型虚构"},
            ],
        }, {"PPE-001#4.2-helmet@1.2"})

        self.assertEqual(
            recommendation["sop_citations"][0]["citation_id"],
            "PPE-001#4.2-helmet@1.2",
        )
        self.assertEqual(recommendation["rejected_sop_citations"], ["FAKE-999#1@9"])
        self.assertTrue(recommendation["sop_answerable"])

    def test_safety_agent_records_grounded_sop_provenance(self):
        retriever = SOPRetriever(Path("knowledge/sop/safety_procedures.json"))
        agent = SafetyAgent(mode="ollama", model="benchmark-model", sop_retriever=retriever)
        event = AlarmEvent(
            timestamp="test",
            events=[{"type": "火焰检测", "level": "A", "bbox": {}, "detail": "检测到火焰"}],
        )
        response = json.dumps({
            "summary": "发现火焰", "risk_level": "A",
            "sop_citations": [{
                "citation_id": "FIRE-003#6.1-initial-response@1.1", "claim": "立即告警",
            }],
            "recommended_actions": [], "confidence": 0.9,
        }, ensure_ascii=False)
        with patch.object(agent, "_call_ollama", return_value=response):
            agent.analyze(event)

        self.assertEqual(event.rag_status, "grounded")
        self.assertEqual(event.prompt_version, SafetyAgent.PROMPT_VERSION)
        self.assertEqual(event.sop_retrieval["catalog_version"], "2026.08-demo.1")
        self.assertEqual(
            event.llm_recommendation["sop_citations"][0]["citation_id"],
            "FIRE-003#6.1-initial-response@1.1",
        )
        self.assertEqual(event.llm_recommendation["sop_citations"][0]["version"], "1.1")
        self.assertTrue(event.llm_recommendation["sop_citations"][0]["excerpt"])

    def test_sop_benchmark_has_no_failed_cases(self):
        report = build_sop_report()
        self.assertEqual(report["metrics"]["passed"], report["metrics"]["cases"])
        self.assertEqual(report["metrics"]["no_evidence_refusal_accuracy_pct"], 100.0)

    def test_structured_agent_plan_keeps_legacy_action_list(self):
        rec = SafetyAgent._normalize_recommendation({
            "risk_level": "B",
            "recommended_actions": [
                {"tool": "database", "action": "store", "reason": "留存证据", "priority": 1},
                {"tool": "notifier", "action": "send", "reason": "通知负责人", "priority": 2},
                {"tool": "reporter.log", "action": "log", "reason": "兼容模型字段漂移", "priority": 3},
                {"tool": "human_loop", "action": ".check", "reason": "兼容前导点", "priority": 0},
                {"tool": "plc", "action": "stop", "reason": "越权动作", "priority": 0},
            ],
            "confidence": 0.8,
        })
        self.assertEqual(
            rec["recommended_actions"],
            ["database.store", "notifier.send", "reporter.log", "human_loop.check"],
        )
        self.assertEqual([item["name"] for item in rec["action_plan"]], rec["recommended_actions"])
        self.assertEqual(rec["rejected_candidate_actions"], ["plc.stop"])
        self.assertFalse(rec["need_human_confirm"])

        a_rec = SafetyAgent._normalize_recommendation({
            "risk_level": "A", "need_human_confirm": False, "recommended_actions": []
        })
        self.assertTrue(a_rec["need_human_confirm"])

    def test_dispatch_tool_chains_are_exactly_preserved_for_all_levels(self):
        expected = {
            "A": ["human_loop.check", "database.store", "notifier.send_urgent", "reporter.generate"],
            "B": ["human_loop.check", "database.store", "notifier.send", "reporter.log"],
            "C": ["human_loop.check", "database.store"],
        }
        for level, chain in expected.items():
            with self.subTest(level=level):
                dispatch = DispatchAgent()
                called = []
                for tool in ("human_loop", "database", "notifier", "reporter"):
                    dispatch.register_tool(
                        tool,
                        lambda event, action, tool=tool: called.append(f"{tool}.{action}") or "ok",
                    )
                event = AlarmEvent(
                    timestamp="test",
                    events=[{"type": "test", "level": level, "bbox": {}, "detail": "test"}],
                    image_bytes=b"",
                )
                event.llm_recommendation = {
                    "risk_level": level,
                    "confidence": 0.9,
                    "recommended_actions": ["database.store"],
                    "action_plan": [
                        {"name": "database.store", "reason": "合法候选"},
                        {"name": "plc.stop", "reason": "不得执行"},
                    ],
                    "rejected_candidate_actions": ["shell.execute"],
                }
                results = dispatch.dispatch(event)
                self.assertEqual(called, chain)
                self.assertEqual(
                    [f"{item['tool']}.{item['action']}" for item in results],
                    chain,
                )
                validation = event.dispatch_decision["plan_validation"]
                self.assertTrue(validation["baseline_preserved"])
                self.assertEqual(validation["final_plan"], chain)
                self.assertIn("plc.stop", [item["name"] for item in validation["rejected"]])

    def test_llm_cannot_downgrade_a_level_dispatch(self):
        dispatch = DispatchAgent()
        event = AlarmEvent(
            timestamp="test",
            events=[{"type": "fire", "level": "A", "bbox": {}, "detail": "fire"}],
            image_bytes=b"",
        )
        event.llm_recommendation = {
            "risk_level": "C",
            "confidence": 1.0,
            "recommended_actions": [],
            "action_plan": [],
        }
        rules, validation = dispatch._validate_tool_plan(
            dispatch._make_decision(event, "A")["final_level"], event
        )
        self.assertEqual([f"{r['tool']}.{r['action']}" for r in rules], [
            "human_loop.check", "database.store", "notifier.send_urgent", "reporter.generate"
        ])
        self.assertTrue(validation["baseline_preserved"])

    def test_completed_dispatch_snapshot_replaces_mid_chain_database_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = DatabaseTool(str(Path(tmp) / "alarms.db"))
            event = AlarmEvent(
                timestamp="test",
                events=[{"type": "test", "level": "B", "bbox": {}, "detail": "test"}],
                image_bytes=b"",
            )
            event.event_id = "EVT_TEST"
            event.llm_model = "qwen2.5vl:7b"
            event.prompt_version = "safety-v2"
            event.sop_retrieval = {"status": "retrieved", "citations": [{"citation_id": "TEST#1@1"}]}
            event.rag_status = "grounded"
            event.dispatch_actions = [{"tool": "human_loop", "action": "check", "result": "ok"}]
            database.store(event)
            event.dispatch_actions = [
                {"tool": "human_loop", "action": "check", "result": "ok"},
                {"tool": "database", "action": "store", "result": "ok"},
                {"tool": "notifier", "action": "send", "result": "ok"},
                {"tool": "reporter", "action": "log", "result": "ok"},
            ]
            event.lifecycle_status = "decided"
            self.assertTrue(database.update_event_snapshot(event))
            row = database.query_recent(hours=1)[0]
            self.assertEqual(len(json.loads(row["dispatch_actions"])), 4)
            self.assertEqual(row["lifecycle_status"], "decided")
            self.assertEqual(row["llm_model"], "qwen2.5vl:7b")
            self.assertEqual(row["prompt_version"], "safety-v2")
            self.assertEqual(row["rag_status"], "grounded")
            self.assertEqual(json.loads(row["sop_retrieval"])["status"], "retrieved")
            del database
            gc.collect()

    def test_public_image_rejects_html(self):
        notifier = NotifierTool(image_check_attempts=1)
        with patch("urllib.request.urlopen", return_value=FakeResponse(b"<html>", "text/html")):
            ok, attempts, error = notifier._verify_public_image("https://example.com/alarm.jpg")
        self.assertFalse(ok)
        self.assertEqual(attempts, 1)
        self.assertIn("invalid_image_response", error)

    def test_public_image_accepts_jpeg(self):
        notifier = NotifierTool(image_check_attempts=1)
        response = FakeResponse(b"\xff\xd8\xff\xe0" + b"x" * 20, "image/jpeg")
        with patch("urllib.request.urlopen", return_value=response):
            ok, attempts, error = notifier._verify_public_image("https://example.com/alarm.jpg")
        self.assertTrue(ok)
        self.assertEqual(attempts, 1)
        self.assertEqual(error, "")


if __name__ == "__main__":
    unittest.main()
