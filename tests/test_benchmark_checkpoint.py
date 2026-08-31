import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.failure_attribution import new_repair_trace
from benchmarks.run_multimodal_benchmark import (
    CHECKPOINT_SCHEMA_VERSION,
    build_report,
    load_cases,
    materialize_case,
    run_variant,
)


class BenchmarkCheckpointTests(unittest.TestCase):
    @staticmethod
    def _fake_analyze(event):
        event.llm_recommendation = {
            "risk_level": "B", "confidence": 0.9,
            "action_plan": [], "sop_citations": [],
            "sop_answerable": False, "sop_refusal_reason": "disabled",
        }
        event.llm_json_valid = True
        event.llm_status = "success"
        event.llm_latency_ms = 1.0
        event.prompt_version = "test"
        event.context_manifest = {
            "context_sha256": "c" * 64,
            "model_input_sha256": "m" * 64,
            "critical_evidence_retained": True,
            "estimated_tokens": 10,
        }
        event.repair_trace = new_repair_trace()
        event.sop_retrieval = {"status": "disabled", "catalog_version": ""}
        event.rag_status = "disabled"
        return "test"

    def test_matching_checkpoint_resumes_without_calling_model(self):
        case = materialize_case(load_cases()[0])
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "live.checkpoint.json"
            with patch(
                "benchmarks.run_multimodal_benchmark.SafetyAgent.analyze",
                side_effect=self._fake_analyze,
            ) as analyze:
                first = run_variant(
                    "no_rag", [case], "mock", "http://127.0.0.1:1", 1, None,
                    checkpoint_path=checkpoint,
                )
            self.assertEqual(analyze.call_count, 1)
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], CHECKPOINT_SCHEMA_VERSION)
            self.assertEqual(payload["status"], "complete")
            self.assertNotIn("llm_analysis", json.dumps(payload, ensure_ascii=False))

            with patch(
                "benchmarks.run_multimodal_benchmark.SafetyAgent.analyze",
                side_effect=AssertionError("model must not be called for completed row"),
            ) as analyze:
                resumed = run_variant(
                    "no_rag", [case], "mock", "http://127.0.0.1:1", 1, None,
                    checkpoint_path=checkpoint, resume=True,
                )
            self.assertEqual(analyze.call_count, 0)
            self.assertEqual(resumed["results"], first["results"])

    def test_checkpoint_refuses_changed_context_fingerprint(self):
        case = materialize_case(load_cases()[0])
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "live.checkpoint.json"
            with patch(
                "benchmarks.run_multimodal_benchmark.SafetyAgent.analyze",
                side_effect=self._fake_analyze,
            ):
                run_variant(
                    "no_rag", [case], "mock", "http://127.0.0.1:1", 1, None,
                    context_token_budget=1200, checkpoint_path=checkpoint,
                )
            with self.assertRaisesRegex(ValueError, "checkpoint_fingerprint_mismatch"):
                run_variant(
                    "no_rag", [case], "mock", "http://127.0.0.1:1", 1, None,
                    context_token_budget=800, checkpoint_path=checkpoint, resume=True,
                )

    def test_failed_model_warmup_stops_before_scored_cases(self):
        def failed_warmup(event):
            event.llm_status = "failed"
            event.llm_error = "injected_timeout"
            event.llm_json_valid = False
            event.llm_latency_ms = 90000.0

        with patch(
            "benchmarks.run_multimodal_benchmark.SafetyAgent.health",
            return_value={"status": "ready"},
        ), patch(
            "benchmarks.run_multimodal_benchmark.SafetyAgent.analyze",
            side_effect=failed_warmup,
        ) as analyze:
            report = build_report(
                "mock", "http://127.0.0.1:1", 1,
                compare_no_rag=False, case_limit=1,
            )
        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(report["status"], "model_warmup_failed")
        self.assertEqual(report["warmup"]["llm_status"], "failed")
        self.assertEqual(report["variants"], [])


if __name__ == "__main__":
    unittest.main()
