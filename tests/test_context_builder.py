"""Context Engineering contracts for the safety Agent."""
import json
import unittest
from unittest.mock import patch

from agents import AlarmEvent
from agents.context_builder import ContextBuilder
from agents.safety_agent import SafetyAgent
from benchmarks.run_context_benchmark import build_report


class StaticRetriever:
    catalog_version = "test-catalog-v1"

    def retrieve_event(self, event):
        return {
            "status": "retrieved",
            "catalog_version": self.catalog_version,
            "citations": [{
                "citation_id": "TEST#1@1",
                "document_id": "TEST",
                "title": "测试规程",
                "section": "1",
                "version": "1",
                "source": "test.json",
                "effective_date": "2026-01-01",
                "excerpt": "必须执行安全处置。" * 500,
            }],
            "refusal_reason": "",
        }


def event() -> AlarmEvent:
    return AlarmEvent(
        timestamp="test",
        event_id="EVT_CONTEXT_TEST",
        camera_id="camera-01",
        events=[{
            "type": "未戴安全帽", "level": "B", "detail": "检测到违规",
            "bbox": {"x": 1, "y": 2, "width": 3, "height": 4},
        }],
    )


class ContextBuilderTests(unittest.TestCase):
    def test_context_benchmark_has_no_failed_cases(self):
        report = build_report()
        self.assertEqual(report["summary"]["passed"], report["summary"]["cases"])

    def test_rule_baseline_survives_an_impossibly_small_optional_budget(self):
        builder = ContextBuilder(256)
        current = event()
        current.events[0]["detail"] = "高风险证据" * 500
        payload, manifest = builder.build(
            current,
            context_text="历史记录" * 500,
            memory_context={"context_text": "历史记录" * 500},
            sop_context=StaticRetriever().retrieve_event(current),
        )
        self.assertEqual(len(payload["detections"]), 1)
        self.assertTrue(manifest["critical_evidence_retained"])
        self.assertGreater(manifest["budget_overflow_tokens"], 0)

    def test_manifest_persists_provenance_without_raw_evidence(self):
        payload, manifest = ContextBuilder(1200).build(
            event(),
            context_text="敏感历史文本",
            memory_context={"context_text": "敏感历史文本"},
            sop_context={"status": "no_evidence", "catalog_version": "v1", "citations": []},
        )
        serialized = json.dumps(manifest, ensure_ascii=False)
        self.assertNotIn("敏感历史文本", serialized)
        self.assertEqual(len(manifest["context_sha256"]), 64)
        self.assertEqual(len(manifest["model_input_sha256"]), 64)
        self.assertEqual(payload["memory"]["summary"], "敏感历史文本")

    def test_model_cannot_cite_retrieved_but_non_injected_sop(self):
        agent = SafetyAgent(
            model="benchmark", sop_retriever=StaticRetriever(),
            context_builder=ContextBuilder(256),
        )
        response = json.dumps({
            "risk_level": "B",
            "sop_citations": [{"citation_id": "TEST#1@1", "claim": "模型尝试引用"}],
            "recommended_actions": [],
        }, ensure_ascii=False)
        current = event()
        with patch.object(agent, "_call_ollama", return_value=response):
            agent.analyze(current)
        self.assertEqual(current.context_manifest["selected_citation_ids"], [])
        self.assertEqual(current.llm_recommendation["sop_citations"], [])
        self.assertEqual(current.llm_recommendation["rejected_sop_citations"], ["TEST#1@1"])

    def test_skipped_context_is_explicit_and_hashed(self):
        manifest = ContextBuilder().skipped_manifest(event(), "analysis_timeout")
        self.assertEqual(manifest["status"], "skipped")
        self.assertEqual(manifest["skip_reason"], "analysis_timeout")
        self.assertEqual(len(manifest["context_sha256"]), 64)
        self.assertEqual(len(manifest["model_input_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
