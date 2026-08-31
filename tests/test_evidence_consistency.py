"""Tests for conservative cross-modal evidence handling."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents import AlarmEvent
from agents.dispatch import DispatchAgent
from agents.evidence_consistency import assess_evidence
from agents.safety_agent import SafetyAgent
from benchmarks.scenario_fixtures import scenario_alarm_body, scenario_image
from services.agent_runtime import AgentRuntime
from tools.actuator import ActuatorTool
from tools.human_loop import HumanLoopTool


def event(*, image: bool = True, level: str = "B") -> AlarmEvent:
    return AlarmEvent(
        timestamp="2026-08-06 12:00:00",
        event_id="EVT_EVIDENCE",
        run_id="RUN_EVIDENCE",
        trace_id="TRACE_EVIDENCE",
        evidence_id="EVID_EVIDENCE",
        events=[{
            "type": "未戴安全帽", "level": level,
            "bbox": {}, "detail": "detector reports missing helmet",
        }],
        image_bytes=b"synthetic-image" if image else b"",
        raw_json={"source": "test", "cameraId": "camera-test"},
    )


class EvidenceConsistencyTests(unittest.TestCase):
    def test_explicit_conflict_blocks_autonomy(self):
        assessment = assess_evidence(event(), {
            "evidence_relation": "conflict",
            "visual_observations": ["person wears helmet"],
            "detection_observations": ["detector says no helmet"],
            "evidence_conflicts": [{
                "visual_claim": "helmet visible",
                "detection_claim": "helmet absent",
                "detail": "PPE status disagrees",
            }],
        })
        self.assertEqual(assessment["relation"], "conflict")
        self.assertTrue(assessment["review_required"])
        self.assertFalse(assessment["autonomy_allowed"])

    def test_conflict_claim_without_details_is_not_treated_as_proven(self):
        assessment = assess_evidence(event(), {
            "evidence_relation": "conflict", "evidence_conflicts": [],
        })
        self.assertEqual(assessment["relation"], "insufficient")
        self.assertFalse(assessment["review_required"])

    def test_missing_image_cannot_be_called_cross_modal_conflict(self):
        assessment = assess_evidence(event(image=False), {
            "evidence_relation": "conflict",
            "evidence_conflicts": ["model claims conflict"],
        })
        self.assertEqual(assessment["relation"], "detections_only")
        self.assertTrue(assessment["autonomy_allowed"])

    def test_conflict_never_lowers_rule_baseline_and_routes_to_review(self):
        current = event(level="B")
        current.llm_recommendation = {
            "risk_level": "C", "confidence": 0.9,
            "evidence_assessment": assess_evidence(current, {
                "evidence_relation": "conflict",
                "evidence_conflicts": ["image and detector disagree"],
            }),
        }
        dispatch = DispatchAgent()
        dispatch.plan(current)
        self.assertEqual(current.dispatch_decision["final_level"], "B")
        self.assertTrue(
            current.dispatch_decision["evidence_policy"]["review_required"]
        )
        with tempfile.TemporaryDirectory() as tmp:
            human = HumanLoopTool(tmp)
            human.handle(current, "check")
            order = human._load_order(current.approval_id)
        self.assertEqual(order["level"], "B")
        self.assertEqual(order["hold_reason"], "multimodal_evidence_conflict")
        self.assertEqual(current.approval_status, "pending")

    def test_safety_agent_preserves_separate_evidence_diagnostics(self):
        response = json.dumps({
            "summary": "PPE evidence disagreement",
            "visual_observations": ["helmet appears visible"],
            "detection_observations": ["detector reports no helmet"],
            "evidence_relation": "conflict",
            "evidence_conflicts": [{
                "visual_claim": "helmet visible",
                "detection_claim": "helmet absent",
                "detail": "helmet evidence disagrees",
            }],
            "observed_facts": ["sources disagree"],
            "uncertainties": [],
            "risk_level": "B",
            "risk_reason": "keep deterministic B baseline",
            "recommended_actions": [],
            "need_human_confirm": False,
            "confidence": 0.7,
        }, ensure_ascii=False)
        agent = SafetyAgent(model="mock")
        current = event()
        with patch.object(agent, "_call_ollama", return_value=response):
            agent.analyze(current)
        assessment = current.llm_recommendation["evidence_assessment"]
        self.assertEqual(assessment["relation"], "conflict")
        self.assertEqual(
            current.llm_recommendation["visual_observations"], ["helmet appears visible"]
        )
        self.assertTrue(current.llm_recommendation["uncertainties"])

    def test_review_confirmation_has_no_actuator_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            actuator = ActuatorTool(tmp)
            order = {
                "id": "PENDING_CONFLICT",
                "event_id": "EVT_CONFLICT",
                "hold_reason": "multimodal_evidence_conflict",
            }
            first = actuator.handle(order, "review")
            second = actuator.handle(order, "review")
        self.assertEqual(first["status"], "reviewed")
        self.assertEqual(first["commands"], [])
        self.assertEqual(first["execution_id"], second["execution_id"])
        self.assertTrue(second["reused"])

    def test_runtime_conflict_review_finishes_without_high_risk_actuation(self):
        class Broadcaster:
            def publish(self, message):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(
                alarm_dir=root / "alarms", database_path=root / "runtime.db",
                pending_dir=root / "pending", report_dir=root / "reports",
                execution_dir=root / "executions",
                notify_webhook="https://example.com/YOUR_KEY",
                notify_platform="dingtalk", notify_image_required=False,
                notify_image_check_attempts=1, notify_image_check_timeout_seconds=1,
                llm_mode="benchmark", ollama_model="mock",
                ollama_url="http://127.0.0.1:1", llm_timeout_seconds=1,
                llm_max_inflight=1, vision_min_hits=1,
                vision_event_cooldown_seconds=0, camera_id="camera-test",
                public_url="", http_port=5000,
            )
            runtime = AgentRuntime(settings, Broadcaster())

            def analyze(current):
                current.llm_status = "success"
                current.llm_json_valid = True
                current.llm_model = "mock"
                current.prompt_version = SafetyAgent.PROMPT_VERSION
                current.context_manifest = {
                    "schema_version": "agent-context-v1",
                    "builder_version": "context-builder-v1.0", "status": "built",
                    "critical_evidence_retained": True, "selected_items": [],
                    "dropped_items": [], "selected_item_count": 0,
                    "dropped_item_count": 0, "selected_citation_ids": [],
                    "context_sha256": "c" * 64, "model_input_sha256": "m" * 64,
                }
                current.repair_trace = {
                    "schema_version": "agent-repair-v1",
                    "policy_version": "bounded-repair-v1", "max_attempts": 1,
                    "attempt_count": 0, "status": "not_needed", "attempts": [],
                }
                current.sop_retrieval = {
                    "status": "no_evidence", "catalog_version": "test-v1",
                    "citations": [], "refusal_reason": "test",
                }
                assessment = assess_evidence(current, {
                    "evidence_relation": "conflict",
                    "evidence_conflicts": ["image and detector disagree"],
                })
                current.llm_recommendation = {
                    "risk_level": "B", "confidence": 0.8,
                    "evidence_assessment": assessment,
                }
                current.llm_analysis = "conflict"
                current.rag_status = "no_evidence"
                return "conflict"

            runtime.safety.analyze = analyze
            body = scenario_alarm_body("b_ppe")
            result = runtime.ingest_detection(
                body, scenario_image(body, "b_ppe"), source="test"
            )
            deadline = time.time() + 3
            run = runtime.run_store.get(result["run_id"])
            while run["status"] != "waiting_approval" and time.time() < deadline:
                time.sleep(0.02)
                run = runtime.run_store.get(result["run_id"])
            self.assertEqual(run["status"], "waiting_approval")
            approval_id = run["event"]["approval_id"]
            response, status = runtime.approve("approve", {
                "approval_id": approval_id, "operator": "tester",
            })
            self.assertEqual(status, 200)
            self.assertEqual(response["execution_status"], "reviewed")
            self.assertEqual(response["execution_actions"], [])
            self.assertEqual(runtime.run_store.get(result["run_id"])["status"], "succeeded")
            trace = runtime.get_trace(result["run_id"])
            self.assertTrue(trace["validation"]["valid"], trace["validation"]["errors"])
            self.assertEqual(
                trace["decision"]["evidence_policy"]["relation"], "conflict"
            )


if __name__ == "__main__":
    unittest.main()
