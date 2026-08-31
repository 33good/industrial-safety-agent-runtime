"""Contracts for failure attribution and bounded model repair."""
import json
import unittest
from unittest.mock import patch

from agents import AlarmEvent
from agents.failure_attribution import new_repair_trace
from agents.safety_agent import SafetyAgent
from benchmarks.run_repair_benchmark import build_report
from services.run_store import event_snapshot, restore_event


def _event() -> AlarmEvent:
    return AlarmEvent(
        timestamp="test", event_id="EVT_REPAIR_TEST", run_id="RUN_REPAIR_TEST",
        events=[{"type": "helmet_missing", "level": "B", "detail": "test", "bbox": {}}],
    )


def _valid() -> str:
    return json.dumps({
        "risk_level": "B", "recommended_actions": [], "confidence": 0.8,
        "sop_citations": [], "sop_answerable": False,
        "sop_refusal_reason": "no evidence",
    })


class BoundedRepairTests(unittest.TestCase):
    def test_repair_benchmark_has_no_failed_cases(self):
        report = build_report()
        self.assertEqual(report["summary"]["passed"], report["summary"]["cases"])

    def test_invalid_output_uses_exactly_one_repair_call(self):
        agent = SafetyAgent(mode="ollama", model="benchmark")
        current = _event()
        with patch.object(agent, "_call_ollama", return_value="bad-json"), patch.object(
            agent, "_call_repair", return_value=_valid()
        ) as repair:
            agent.analyze(current)
        self.assertEqual(repair.call_count, 1)
        self.assertTrue(current.llm_json_valid)
        self.assertEqual(current.repair_trace["status"], "repaired")
        self.assertEqual(current.failure_attributions[0]["status"], "resolved")

    def test_persisted_attempt_budget_blocks_a_second_repair(self):
        agent = SafetyAgent(mode="ollama", model="benchmark")
        current = _event()
        current.repair_trace = new_repair_trace("exhausted", "previous_failure")
        current.repair_trace["attempts"] = [{"attempt": 1, "status": "invalid"}]
        current.repair_trace["attempt_count"] = 1
        current.repair_trace["max_attempts"] = 99
        with patch.object(agent, "_call_ollama", return_value="bad-json"), patch.object(
            agent, "_call_repair", return_value=_valid()
        ) as repair:
            agent.analyze(current)
        self.assertEqual(repair.call_count, 0)
        self.assertFalse(current.llm_json_valid)
        self.assertEqual(current.repair_trace["attempt_count"], 1)
        self.assertEqual(current.repair_trace["max_attempts"], 1)

    def test_repair_and_failure_state_survive_snapshot_restore(self):
        current = _event()
        current.repair_trace = new_repair_trace("repaired", "schema_repair_succeeded")
        current.repair_trace["attempts"] = [{"attempt": 1, "status": "succeeded"}]
        current.repair_trace["attempt_count"] = 1
        current.failure_attributions = [{
            "schema_version": "agent-failure-v1", "attribution_id": "FAIL_TEST",
            "stage": "model_output", "code": "model_schema_invalid",
            "repairable": True, "resolution": "schema_repair", "status": "resolved",
            "detail": "", "evidence_sha256": "a" * 64,
        }]
        restored = restore_event(event_snapshot(current))
        self.assertEqual(restored.repair_trace, current.repair_trace)
        self.assertEqual(restored.failure_attributions, current.failure_attributions)


if __name__ == "__main__":
    unittest.main()
