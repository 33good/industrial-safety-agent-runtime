"""Offline contract tests for one read-only evidence action and one re-decision."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from agents import AlarmEvent
from agents.context_builder import ContextBuilder
from agents.evidence_replan import (
    AdjacentFrameEvidenceTool,
    normalize_next_step,
)
from agents.safety_agent import SafetyAgent
from services.agent_runtime import AgentRuntime
from services.analysis_limiter import AnalysisLimiter
from services.camera_stream import CameraStreamWorker
from services.run_store import event_snapshot, restore_event
from services.trace_validator import validate_trace
from benchmarks.run_trace_benchmark import build_complete_trace
from tools.actuator import ActuatorTool
from tools.human_loop import HumanLoopTool


def recommendation(*, action: str = "decide", relation: str = "consistent",
                   level: str = "B") -> dict:
    return {
        "summary": "test decision",
        "risk_level": level,
        "evidence_relation": relation,
        "evidence_request": {"action": action, "reason": "test evidence need"},
        "evidence_assessment": {
            "schema_version": "evidence-assessment-v1",
            "policy_version": "evidence-consistency-v1",
            "relation": relation,
            "conflicts": ([{"detail": "test conflict"}] if relation == "conflict" else []),
            "review_required": relation == "conflict",
            "autonomy_allowed": relation != "conflict",
        },
        "recommended_actions": [],
        "action_plan": [],
        "confidence": 0.8,
    }


def event(*, action: str = "decide", relation: str = "consistent",
          level: str = "B") -> AlarmEvent:
    return AlarmEvent(
        timestamp="2026-08-21 20:00:00",
        events=[{
            "type": "PPE violation", "level": "B", "detail": "helmet unclear",
            "bbox": {"x": 10, "y": 20, "width": 30, "height": 40},
        }],
        event_id="EVT_REPLAN", run_id="RUN_REPLAN", trace_id="TRACE_REPLAN",
        source_event_id="SRC_REPLAN", camera_id="camera-01",
        evidence_id="EVID_REPLAN", owner_id="worker-test", execution_attempt=1,
        raw_json={
            "source": "local_yolo", "frameId": 100,
            "frameSessionId": "SESSION_REPLAN",
        },
        image_bytes=b"event-frame",
        llm_recommendation=recommendation(
            action=action, relation=relation, level=level
        ),
        llm_status="success", llm_json_valid=True, llm_model="fake-vlm",
        prompt_version=SafetyAgent.PROMPT_VERSION,
        context_manifest={
            "schema_version": "agent-context-v1",
            "builder_version": "context-builder-v1.0",
            "status": "built",
            "context_sha256": "1" * 64,
            "model_input_sha256": "2" * 64,
            "selected_citation_ids": [],
        },
        repair_trace={
            "schema_version": "agent-repair-v1", "policy_version": "bounded-repair-v1",
            "max_attempts": 1, "attempt_count": 0, "attempts": [],
            "status": "not_needed", "reason": "structured_output_valid",
        },
        sop_retrieval={"status": "no_evidence", "catalog_version": "test-v1", "citations": []},
    )


class Lease:
    def __init__(self):
        self.checks = 0

    def ensure_owned(self):
        self.checks += 1


class Store:
    def __init__(self):
        self.snapshots = []

    def save_snapshot(self, current, **_kwargs):
        self.snapshots.append(copy.deepcopy(current.evidence_replan))


class ScriptedSafety:
    def __init__(self, second: dict):
        self.second = copy.deepcopy(second)
        self.calls = 0
        self.receipt = None
        self.images = None

    def reanalyze(self, current, *, supplemental_images, evidence_receipt):
        self.calls += 1
        self.receipt = copy.deepcopy(evidence_receipt)
        self.images = list(supplemental_images)
        current.llm_recommendation = copy.deepcopy(self.second)
        current.llm_status = "success"
        current.llm_error = ""
        current.llm_json_valid = True
        current.llm_latency_ms += 4.0
        current.context_manifest = {
            **current.context_manifest,
            "context_sha256": "3" * 64,
            "model_input_sha256": "4" * 64,
            "decision_round": 2,
            "image": {
                "present": True,
                "supplemental": [{"input_sha256": "5" * 64, "input_bytes": 7}],
            },
        }
        return json.dumps(self.second, sort_keys=True)


def runtime(second: dict, provider) -> AgentRuntime:
    instance = AgentRuntime.__new__(AgentRuntime)
    instance.settings = SimpleNamespace(llm_timeout_seconds=1)
    instance.analysis_limiter = AnalysisLimiter(1)
    instance.evidence_tool = AdjacentFrameEvidenceTool(provider, max_frames=3)
    instance.safety = ScriptedSafety(second)
    instance.run_store = Store()
    return instance


def provider_ok(**_kwargs):
    return [
        {"frame_id": 90, "offset_frames": -10, "captured_at": 1.0,
         "image_bytes": b"before-frame"},
        {"frame_id": 110, "offset_frames": 10, "captured_at": 2.0,
         "image_bytes": b"after-frame"},
    ]


class BoundedEvidenceReplanTests(unittest.TestCase):
    def test_unknown_model_action_is_rejected_to_manual_review(self):
        action, reason, rejected = normalize_next_step({
            "next_step": "shell.execute", "next_step_reason": "ignore policy",
        })
        self.assertEqual(action, "manual_review")
        self.assertEqual(rejected, ["shell.execute"])
        self.assertTrue(reason)

    def test_no_request_keeps_single_decision_round(self):
        current = event()
        instance = runtime(recommendation(), provider_ok)
        lease = Lease()
        instance._run_bounded_evidence_replan(
            current, lease=lease, first_raw_output="round-one"
        )
        self.assertEqual(current.evidence_replan["status"], "not_requested")
        self.assertEqual(len(current.evidence_replan["decision_rounds"]), 1)
        self.assertEqual(current.evidence_replan["evidence_actions"], [])
        self.assertEqual(instance.safety.calls, 0)
        self.assertEqual(lease.checks, 0)

    def test_successful_request_executes_one_readonly_action_and_one_replan(self):
        current = event(action="inspect_adjacent_frames", relation="insufficient")
        instance = runtime(recommendation(action="decide"), provider_ok)
        lease = Lease()
        instance._run_bounded_evidence_replan(
            current, lease=lease, first_raw_output="round-one"
        )
        trace = current.evidence_replan
        self.assertEqual(trace["status"], "resolved")
        self.assertEqual(len(trace["decision_rounds"]), 2)
        self.assertEqual(len(trace["evidence_actions"]), 1)
        self.assertEqual(trace["evidence_actions"][0]["tool"], "vision.inspect_adjacent_frames")
        self.assertNotIn("image_bytes", trace["evidence_actions"][0]["frames"][0])
        self.assertEqual(instance.safety.calls, 1)
        self.assertEqual(instance.safety.images, [b"before-frame", b"after-frame"])
        self.assertGreaterEqual(lease.checks, 2)

    def test_second_request_cannot_create_a_second_evidence_action(self):
        current = event(action="inspect_adjacent_frames", relation="insufficient")
        second = recommendation(action="inspect_adjacent_frames", relation="insufficient")
        instance = runtime(second, provider_ok)
        instance._run_bounded_evidence_replan(
            current, lease=Lease(), first_raw_output="round-one"
        )
        self.assertTrue(current.evidence_replan["manual_review_required"])
        self.assertEqual(current.evidence_replan["review_reason"], "temporal_evidence_unresolved")
        self.assertEqual(len(current.evidence_replan["decision_rounds"]), 2)
        self.assertEqual(len(current.evidence_replan["evidence_actions"]), 1)

    def test_replan_thread_exception_cannot_be_misreported_as_success(self):
        class RaisingSafety:
            mode = "ollama"

            @staticmethod
            def reanalyze(*_args, **_kwargs):
                raise RuntimeError("injected replan failure")

        current = event(action="inspect_adjacent_frames", relation="insufficient")
        instance = runtime(recommendation(), provider_ok)
        instance.safety = RaisingSafety()
        instance._run_bounded_evidence_replan(
            current, lease=Lease(), first_raw_output="round-one"
        )
        self.assertTrue(current.evidence_replan["manual_review_required"])
        self.assertEqual(
            current.evidence_replan["review_reason"],
            "evidence_replan_model_failed",
        )
        self.assertIn("injected replan failure", current.evidence_replan["replan_error"])
        self.assertEqual(len(current.evidence_replan["decision_rounds"]), 1)

    def test_text_only_provider_cannot_claim_it_inspected_temporal_frames(self):
        current = event(action="inspect_adjacent_frames", relation="insufficient")
        instance = runtime(recommendation(), provider_ok)
        instance.safety.mode = "deepseek"
        instance._run_bounded_evidence_replan(
            current, lease=Lease(), first_raw_output="round-one"
        )
        self.assertEqual(current.evidence_replan["evidence_actions"], [])
        self.assertEqual(
            current.evidence_replan["review_reason"],
            "temporal_evidence_unavailable",
        )

    def test_missing_archive_converges_to_manual_review_without_replan(self):
        current = event(action="inspect_adjacent_frames", relation="insufficient")
        instance = runtime(recommendation(), lambda **_kwargs: [])
        instance._run_bounded_evidence_replan(
            current, lease=Lease(), first_raw_output="round-one"
        )
        self.assertEqual(instance.safety.calls, 0)
        self.assertTrue(current.evidence_replan["manual_review_required"])
        self.assertEqual(current.evidence_replan["review_reason"], "temporal_evidence_no_evidence")
        self.assertTrue(
            current.llm_recommendation["evidence_assessment"]["review_required"]
        )

    def test_frame_provider_failure_converges_to_manual_review(self):
        def failing_provider(**_kwargs):
            raise RuntimeError("injected frame archive failure")

        current = event(action="inspect_adjacent_frames", relation="insufficient")
        instance = runtime(recommendation(), failing_provider)
        instance._run_bounded_evidence_replan(
            current, lease=Lease(), first_raw_output="round-one"
        )
        self.assertEqual(instance.safety.calls, 0)
        self.assertEqual(
            current.evidence_replan["review_reason"], "temporal_evidence_failed"
        )

    def test_replan_capacity_exhaustion_converges_to_manual_review(self):
        current = event(action="inspect_adjacent_frames", relation="insufficient")
        instance = runtime(recommendation(), provider_ok)
        instance.analysis_limiter = SimpleNamespace(
            try_start=lambda *_args, **_kwargs: None
        )
        instance._run_bounded_evidence_replan(
            current, lease=Lease(), first_raw_output="round-one"
        )
        self.assertEqual(
            current.evidence_replan["review_reason"],
            "evidence_replan_capacity_exhausted",
        )

    def test_replan_timeout_converges_to_manual_review(self):
        current = event(action="inspect_adjacent_frames", relation="insufficient")
        instance = runtime(recommendation(), provider_ok)
        instance.settings.llm_timeout_seconds = 0.01
        instance.analysis_limiter = SimpleNamespace(
            try_start=lambda *_args, **_kwargs: threading.Event()
        )
        instance._run_bounded_evidence_replan(
            current, lease=Lease(), first_raw_output="round-one"
        )
        self.assertEqual(
            current.evidence_replan["review_reason"], "evidence_replan_timeout"
        )

    def test_temporal_review_approval_never_authorizes_actuator_commands(self):
        class Database:
            @staticmethod
            def update_approval_status(*_args):
                return True

            @staticmethod
            def update_execution_status(*_args):
                return True

        class RunStore:
            def __init__(self):
                self.run = {
                    "event": {
                        "timeline": [],
                        "evidence_replan": {
                            "schema_version": "bounded-evidence-replan-v1",
                            "status": "manual_review",
                            "manual_review_required": True,
                            "review_reason": "temporal_evidence_no_evidence",
                        },
                    }
                }

            def get(self, _run_id):
                return copy.deepcopy(self.run)

            def patch_event(self, _run_id, patch):
                self.run["event"].update(copy.deepcopy(patch))

            @staticmethod
            def transition(*_args, **_kwargs):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = AgentRuntime.__new__(AgentRuntime)
            instance.human_loop = HumanLoopTool(str(root / "pending"))
            instance.actuator = ActuatorTool(str(root / "executions"))
            instance.database = Database()
            instance.run_store = RunStore()
            instance._remember_and_broadcast = lambda _message: None
            instance.human_loop._approvals["PENDING_TEMPORAL"] = {
                "id": "PENDING_TEMPORAL",
                "event_id": "EVT_TEMPORAL",
                "run_id": "RUN_TEMPORAL",
                "trace_id": "TRACE_TEMPORAL",
                "status": "pending",
                "hold_reason": "temporal_evidence_no_evidence",
            }
            response, status = instance.approve("approve", {
                "approval_id": "PENDING_TEMPORAL", "operator": "tester",
            })
        self.assertEqual(status, 200)
        self.assertEqual(response["execution_status"], "reviewed")
        self.assertEqual(response["execution_actions"], [])
        closed = instance.run_store.run["event"]["evidence_replan"]
        self.assertEqual(closed["status"], "reviewed")
        self.assertFalse(closed["manual_review_required"])
        self.assertEqual(closed["review_resolution"]["decision"], "approved")

    def test_explicit_conflict_skips_frame_tool_and_requires_review(self):
        called = []

        def provider(**kwargs):
            called.append(kwargs)
            return provider_ok(**kwargs)

        current = event(action="inspect_adjacent_frames", relation="conflict")
        instance = runtime(recommendation(), provider)
        instance._run_bounded_evidence_replan(
            current, lease=Lease(), first_raw_output="round-one"
        )
        self.assertEqual(called, [])
        self.assertEqual(current.evidence_replan["review_reason"], "multimodal_evidence_conflict")

    def test_replan_risk_downgrade_requires_human_review(self):
        current = event(
            action="inspect_adjacent_frames", relation="insufficient", level="A"
        )
        instance = runtime(recommendation(action="decide", level="B"), provider_ok)
        instance._run_bounded_evidence_replan(
            current, lease=Lease(), first_raw_output="round-one"
        )
        self.assertEqual(
            current.evidence_replan["review_reason"],
            "replan_risk_downgrade_requires_review",
        )

    def test_request_identity_is_stable_and_payload_is_not_persisted(self):
        tool = AdjacentFrameEvidenceTool(provider_ok, max_frames=2)
        first, _ = tool.execute(event(action="inspect_adjacent_frames"))
        second, _ = tool.execute(event(action="inspect_adjacent_frames"))
        self.assertEqual(first["request_sha256"], second["request_sha256"])
        self.assertEqual(first["receipt_sha256"], second["receipt_sha256"])
        self.assertNotIn(b"before-frame", json.dumps(first).encode())

    def test_external_event_cannot_read_local_frame_archive(self):
        calls = []

        def provider(**kwargs):
            calls.append(kwargs)
            return provider_ok(**kwargs)

        current = event(action="inspect_adjacent_frames", relation="insufficient")
        current.raw_json["source"] = "external"
        receipt, images = AdjacentFrameEvidenceTool(provider).execute(current)
        self.assertEqual(receipt["status"], "unavailable")
        self.assertEqual(receipt["error"], "event_source_not_trusted_for_frame_archive")
        self.assertEqual(images, [])
        self.assertEqual(calls, [])

    def test_camera_archive_returns_fixed_policy_frames(self):
        worker = CameraStreamWorker("", evidence_buffer_size=40)
        with worker._lock:
            worker._stream_session_id = "SESSION_REPLAN"
            for frame_id in range(70, 121):
                worker._evidence_frames.append({
                    "frame_id": frame_id,
                    "stream_session_id": "SESSION_REPLAN",
                    "captured_at": float(frame_id),
                    "image_bytes": f"frame-{frame_id}".encode(),
                })
        rows = worker.evidence_frames(
            anchor_frame_id=100,
            stream_session_id="SESSION_REPLAN",
            limit=3,
        )
        self.assertEqual(len(rows), 3)
        self.assertNotIn(100, [row["frame_id"] for row in rows])
        self.assertEqual(
            [row["frame_id"] for row in rows],
            sorted(row["frame_id"] for row in rows),
        )
        self.assertEqual(worker.evidence_frames(
            anchor_frame_id=100,
            stream_session_id="OTHER_SESSION",
            limit=3,
        ), [])

    def test_camera_archive_refuses_frames_outside_local_window(self):
        worker = CameraStreamWorker("", evidence_buffer_size=16)
        with worker._lock:
            worker._stream_session_id = "SESSION_REPLAN"
            for frame_id in (1, 2, 3, 200, 201):
                worker._evidence_frames.append({
                    "frame_id": frame_id,
                    "stream_session_id": "SESSION_REPLAN",
                    "captured_at": float(frame_id),
                    "image_bytes": f"frame-{frame_id}".encode(),
                })
        self.assertEqual(worker.evidence_frames(
            anchor_frame_id=100,
            stream_session_id="SESSION_REPLAN",
            limit=3,
        ), [])

    def test_grader_labels_cannot_enter_replan_context(self):
        current = event(action="inspect_adjacent_frames", relation="insufficient")
        payload, _ = ContextBuilder().build(
            current,
            decision_context={
                "round": 2,
                "phase": "temporal_evidence_replan",
                "expected_conflict": "LEAK_CANARY",
                "expected_level": "A",
                "grader_notes": "LEAK_CANARY",
                "supplemental_frames": [{
                    "frame_id": 90,
                    "offset_frames": -10,
                    "image_sha256": "a" * 64,
                    "expected_visible_fact": "LEAK_CANARY",
                }],
            },
        )
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("LEAK_CANARY", serialized)
        self.assertNotIn("expected_conflict", serialized)
        self.assertNotIn("expected_level", serialized)
        self.assertNotIn("grader_notes", serialized)

    def test_second_round_reuses_first_round_memory_context(self):
        class ChangedMemory:
            def get_context(self, _bbox):
                raise AssertionError("second round must not re-query memory")

        current = event(action="inspect_adjacent_frames", relation="insufficient")
        current._decision_context_text = "frozen first-round memory"
        current._decision_memory_context = {
            "context_text": "frozen first-round memory",
            "zone": "zone-a",
            "zone_count": 2,
            "escalated": False,
            "recent_events": [],
        }
        agent = SafetyAgent(mode="ollama", memory=ChangedMemory())
        captured = {}

        def fake_call(_event, _context_text, _memory_context, _sop_context,
                      *, context_payload, supplemental_images):
            captured["context"] = copy.deepcopy(context_payload)
            captured["images"] = list(supplemental_images)
            return "round-two"

        agent._call_ollama = fake_call
        agent._finalize_model_result = lambda _event, raw, _payload: (
            raw, recommendation(action="decide")
        )
        agent.reanalyze(
            current,
            supplemental_images=[b"adjacent"],
            evidence_receipt={
                "tool": "vision.inspect_adjacent_frames",
                "status": "succeeded",
                "receipt_sha256": "b" * 64,
                "frames": [{
                    "frame_id": 90, "offset_frames": -10,
                    "image_sha256": "c" * 64,
                }],
            },
        )
        self.assertEqual(captured["context"]["memory"]["zone"], "zone-a")
        self.assertEqual(captured["images"], [b"adjacent"])

    def test_round_metadata_cannot_change_memory_or_sop_selection(self):
        current = event(action="inspect_adjacent_frames", relation="insufficient")
        builder = ContextBuilder(token_budget=700)
        memory = {
            "context_text": "historical event " * 20,
            "zone": "zone-a", "zone_count": 3, "escalated": False,
            "recent_events": [{
                "event_id": "OLD-1", "event_types": "PPE violation",
                "level": "B", "created_at": "2026-08-20",
            }],
        }
        sop = {
            "status": "retrieved", "catalog_version": "test-v1",
            "citations": [{
                "citation_id": "SOP-1", "title": "PPE", "section": "1",
                "version": "1", "effective_date": "2026-01-01",
                "excerpt": "wear protective equipment " * 10,
            }],
        }
        _, first = builder.build(
            current, context_text=memory["context_text"],
            memory_context=memory, sop_context=sop,
            decision_context={"round": 1, "phase": "initial"},
        )
        _, second = builder.build(
            current, context_text=memory["context_text"],
            memory_context=memory, sop_context=sop,
            decision_context={
                "round": 2, "phase": "temporal_evidence_replan",
                "prior_output_sha256": "d" * 64,
                "evidence_tool": "vision.inspect_adjacent_frames",
                "evidence_status": "succeeded",
                "evidence_receipt_sha256": "e" * 64,
                "supplemental_frames": [{
                    "frame_id": 90, "offset_frames": -10,
                    "image_sha256": "f" * 64,
                }],
            },
        )
        self.assertEqual(
            [item["item_id"] for item in first["selected_items"]],
            [item["item_id"] for item in second["selected_items"]],
        )
        self.assertEqual(
            first["selected_citation_ids"], second["selected_citation_ids"]
        )

    def test_snapshot_roundtrip_preserves_replan_trace_without_images(self):
        current = event(action="inspect_adjacent_frames", relation="insufficient")
        instance = runtime(recommendation(action="decide"), provider_ok)
        instance._run_bounded_evidence_replan(
            current, lease=Lease(), first_raw_output="round-one"
        )
        restored = restore_event(event_snapshot(current))
        self.assertEqual(restored.evidence_replan, current.evidence_replan)
        self.assertEqual(restored.image_bytes, b"")

    def test_normalizer_fails_closed_when_old_output_omits_evidence_decision(self):
        normalized = SafetyAgent._normalize_recommendation({
            "risk_level": "B", "recommended_actions": [], "confidence": 0.8,
        })
        self.assertEqual(normalized["evidence_request"]["action"], "manual_review")

    def test_first_round_insufficient_cannot_continue_autonomously(self):
        current = event(action="decide", relation="insufficient")
        instance = runtime(recommendation(), provider_ok)
        instance._run_bounded_evidence_replan(
            current, lease=Lease(), first_raw_output="round-one"
        )
        self.assertTrue(current.evidence_replan["manual_review_required"])
        self.assertEqual(
            current.evidence_replan["review_reason"],
            "temporal_evidence_unresolved",
        )
        self.assertFalse(
            current.llm_recommendation["evidence_assessment"]["autonomy_allowed"]
        )

    def test_trace_accepts_a_complete_bounded_replan(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = build_complete_trace(Path(tmp) / "trace.db")
        trace.pop("validation", None)
        trace["ingress"]["source"] = "local_yolo"
        current = event(action="inspect_adjacent_frames", relation="insufficient")
        current.event_id = trace["run"]["event_id"]
        current.run_id = trace["run"]["run_id"]
        current.trace_id = trace["run"]["trace_id"]
        current.evidence_id = trace["evidence"]["evidence_id"]
        current.camera_id = trace["ingress"]["camera_id"]
        current.raw_json.update({
            "source": "local_yolo", "frameId": 100,
            "frameSessionId": "SESSION_TRACE",
        })
        action, images = AdjacentFrameEvidenceTool(
            provider_ok, max_frames=2
        ).execute(current)
        self.assertEqual(action["status"], "succeeded")
        trace["context"].setdefault("image", {})["supplemental"] = [
            {
                "original_sha256": frame["image_sha256"],
                "input_sha256": frame["image_sha256"],
                "original_bytes": len(image),
                "input_bytes": len(image),
                "transformed": False,
            }
            for frame, image in zip(action["frames"], images)
        ]
        trace["evidence_replan"] = {
            "schema_version": "bounded-evidence-replan-v1",
            "policy_version": "readonly-evidence-tool-policy-v1",
            "status": "resolved",
            "manual_review_required": False,
            "review_reason": "",
            "decision_rounds": [
                {
                    "round": 1, "context_sha256": "7" * 64,
                    "model_input_sha256": "8" * 64,
                    "output_sha256": "1" * 64,
                },
                {
                    "round": 2,
                    "context_sha256": trace["context"]["context_sha256"],
                    "model_input_sha256": trace["context"]["model_input_sha256"],
                    "output_sha256": "2" * 64,
                },
            ],
            "evidence_actions": [action],
        }
        self.assertTrue(validate_trace(trace)["valid"])

        tampered = copy.deepcopy(trace)
        tampered["evidence_replan"]["evidence_actions"][0]["run_id"] = "OTHER_RUN"
        validation = validate_trace(tampered)
        self.assertIn("evidence_action_run_id_mismatch", validation["errors"])
        self.assertIn("evidence_action_request_hash_mismatch", validation["errors"])

        failed = copy.deepcopy(trace)
        failed["evidence_replan"]["evidence_actions"][0]["status"] = "failed"
        validation = validate_trace(failed)
        self.assertIn(
            "resolved_replan_without_successful_evidence", validation["errors"]
        )

        reviewed = copy.deepcopy(trace)
        reviewed["evidence_replan"].update({
            "status": "reviewed",
            "manual_review_required": False,
            "review_reason": "temporal_evidence_unresolved",
            "review_resolution": {
                "approval_id": "PENDING_TRACE", "decision": "approved",
                "operator": "tester", "resolved_at": "2026-08-21T20:00:00",
            },
        })
        reviewed["approval"] = {
            "approval_id": "PENDING_TRACE", "status": "approved",
        }
        reviewed["actuation"] = {
            "execution_id": "EXEC_PENDING_TRACE", "status": "reviewed",
            "result": "evidence review completed", "actions": [],
        }
        self.assertTrue(validate_trace(reviewed)["valid"])

        missing_approval = copy.deepcopy(reviewed)
        missing_approval["approval"] = {"approval_id": "", "status": "auto"}
        validation = validate_trace(missing_approval)
        self.assertIn(
            "reviewed_evidence_approval_id_mismatch", validation["errors"]
        )
        self.assertIn(
            "reviewed_evidence_approval_not_approved", validation["errors"]
        )

    def test_trace_rejects_an_effectful_evidence_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = build_complete_trace(Path(tmp) / "trace.db")
        trace.pop("validation", None)
        trace["evidence_replan"] = {
            "schema_version": "bounded-evidence-replan-v1",
            "policy_version": "readonly-evidence-tool-policy-v1",
            "status": "manual_review",
            "manual_review_required": False,
            "review_reason": "",
            "decision_rounds": [{"round": 1, "output_sha256": "1" * 64}],
            "evidence_actions": [{
                "tool": "notifier.send", "status": "failed",
                "request_sha256": "2" * 64, "receipt_sha256": "3" * 64,
                "frames": [],
            }],
        }
        validation = validate_trace(trace)
        self.assertIn("effectful_or_unknown_evidence_tool", validation["errors"])


if __name__ == "__main__":
    unittest.main()
