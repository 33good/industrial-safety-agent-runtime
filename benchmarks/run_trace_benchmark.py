"""Deterministic benchmark for end-to-end Agent Trace integrity."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

from agents import AlarmEvent
from services.run_store import RunStore
from services.tool_executor import ToolExecutionStore
from services.trace_validator import RunTraceService, validate_trace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "benchmarks" / "reports" / "trace_integrity.json"


def complete_event() -> AlarmEvent:
    citation = {
        "citation_id": "PPE-001#4.2-helmet@1.2",
        "document_id": "PPE-001",
        "version": "1.2",
        "source": "safety_procedures.json",
        "excerpt": "helmet required",
    }
    return AlarmEvent(
        timestamp="benchmark",
        event_id="EVT_TRACE_COMPLETE",
        run_id="RUN_TRACE_COMPLETE",
        trace_id="TRACE_COMPLETE",
        source_event_id="upstream-trace-001",
        ingest_key="ingest-trace-001",
        ingest_payload_hash="a" * 64,
        camera_id="camera-01",
        evidence_id="EVID_TRACE_COMPLETE",
        events=[{"type": "helmet_missing", "level": "B", "bbox": {}, "detail": "test"}],
        llm_model="qwen2.5vl:7b",
        llm_status="ok",
        prompt_version="safety-v3",
        context_manifest={
            "schema_version": "agent-context-v1",
            "builder_version": "context-builder-v1.0",
            "status": "built",
            "token_budget": 1200,
            "estimated_tokens": 120,
            "budget_utilization_pct": 10.0,
            "budget_overflow_tokens": 0,
            "truncated": False,
            "critical_evidence_retained": True,
            "input_item_count": 3,
            "selected_item_count": 3,
            "dropped_item_count": 0,
            "selected_items": [
                {"item_id": "event:metadata"},
                {"item_id": "detection:0"},
                {"item_id": "sop:PPE-001#4.2-helmet@1.2"},
            ],
            "dropped_items": [],
            "selected_citation_ids": ["PPE-001#4.2-helmet@1.2"],
            "context_sha256": "c" * 64,
            "model_input_sha256": "m" * 64,
            "source_versions": {"sop_catalog": "2026.08.1"},
        },
        repair_trace={
            "schema_version": "agent-repair-v1",
            "policy_version": "bounded-repair-v1",
            "max_attempts": 1,
            "attempt_count": 0,
            "status": "not_needed",
            "reason": "",
            "attempts": [],
        },
        failure_attributions=[],
        sop_retrieval={
            "status": "retrieved", "catalog_version": "2026.08.1",
            "citations": [citation],
        },
        rag_status="grounded",
        llm_recommendation={
            "risk_level": "B", "action_plan": [{"name": "database.store"}],
            "sop_citations": [citation],
        },
        dispatch_decision={
            "rule_level": "B", "llm_level": "B", "final_level": "B",
            "grounding": {
                "policy_version": "final-sop-grounding-v1",
                "status": "grounded",
                "catalog_version": "2026.08.1",
                "citations": [{
                    **citation,
                    "binding": "structured_event_exact",
                    "matched_event_types": ["helmet_missing"],
                }],
                "citation_ids": ["PPE-001#4.2-helmet@1.2"],
                "refusal_reason": "",
                "model_candidate_citation_ids": ["PPE-001#4.2-helmet@1.2"],
            },
            "plan_validation": {
                "candidate_plan": ["database.store"],
                "accepted": [{"name": "database.store"}],
                "forced": [], "rejected": [],
                "final_plan": ["database.store"], "baseline_preserved": True,
            },
        },
        lifecycle_status="executing",
    )


def build_complete_trace(database: Path) -> dict:
    event = complete_event()
    runs = RunStore(str(database))
    tools = ToolExecutionStore(str(database))
    runs.create(event, "benchmark")
    claimed = runs.claim_run(event.run_id, "trace-worker", 10)
    event.owner_id = claimed["owner_id"]
    event.execution_attempt = claimed["execution_attempt"]
    runs.save_snapshot(
        event, owner_id=event.owner_id, execution_attempt=event.execution_attempt
    )
    runs.transition(
        event.run_id, "decided", "policy", event=event,
        owner_id=event.owner_id, execution_attempt=event.execution_attempt,
    )
    runs.transition(
        event.run_id, "executing", "tools", event=event,
        owner_id=event.owner_id, execution_attempt=event.execution_attempt,
    )
    tools.begin(
        execution_id="TOOL_TRACE", run_id=event.run_id, event_id=event.event_id,
        step_id="STEP_TRACE", idempotency_key="KEY_TRACE",
        tool="database", action="store", owner_id=event.owner_id,
        execution_attempt=event.execution_attempt,
    )
    tools.record_attempt(
        "KEY_TRACE", 1, run_id=event.run_id, owner_id=event.owner_id,
        execution_attempt=event.execution_attempt,
    )
    tools.finish(
        "KEY_TRACE", "succeeded", result={"stored": True}, run_id=event.run_id,
        owner_id=event.owner_id, execution_attempt=event.execution_attempt,
    )
    event.lifecycle_status = "succeeded"
    runs.transition(
        event.run_id, "succeeded", "complete", event=event,
        owner_id=event.owner_id, execution_attempt=event.execution_attempt,
    )
    return RunTraceService(runs, tools).get(event.run_id)


def build_filtered_trace(database: Path) -> dict:
    event = AlarmEvent(
        timestamp="benchmark", event_id="EVT_TRACE_FILTERED",
        run_id="RUN_TRACE_FILTERED", trace_id="TRACE_FILTERED",
        source_event_id="upstream-filtered-001", ingest_key="ingest-filtered-001",
        ingest_payload_hash="b" * 64, camera_id="camera-01",
        evidence_id="EVID_TRACE_FILTERED", events=[], lifecycle_status="filtered",
    )
    runs = RunStore(str(database))
    tools = ToolExecutionStore(str(database))
    runs.create_or_get(
        event, "benchmark", initial_status="filtered", initial_stage="ingress",
        transition_detail="no_policy_incident",
    )
    return RunTraceService(runs, tools).get(event.run_id)


def run_cases() -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp) / "runtime.db"
        trace = build_complete_trace(database)
        filtered_trace = build_filtered_trace(database)

    results = [{
        "case_id": "complete_trace_is_valid",
        "passed": trace["validation"]["valid"],
        "detail": str(trace["validation"]["errors"]),
    }, {
        "case_id": "filtered_ingress_trace_is_valid",
        "passed": filtered_trace["validation"]["valid"],
        "detail": str(filtered_trace["validation"]["errors"]),
    }]
    repaired_trace = deepcopy(trace)
    repaired_trace.pop("validation", None)
    repaired_trace["reliability"] = {
        "repair": {
            "schema_version": "agent-repair-v1",
            "policy_version": "bounded-repair-v1",
            "max_attempts": 1,
            "attempt_count": 1,
            "status": "repaired",
            "reason": "schema_repair_succeeded",
            "attempts": [{
                "attempt": 1, "prompt_version": "schema-repair-v1",
                "trigger_code": "model_schema_invalid",
                "input_sha256": "d" * 64, "original_output_sha256": "e" * 64,
                "output_sha256": "f" * 64, "status": "succeeded",
                "latency_ms": 1.0, "post_failure_code": "", "error": "",
            }],
        },
        "failure_attributions": [{
            "schema_version": "agent-failure-v1", "attribution_id": "FAIL_TRACE_REPAIR",
            "stage": "model_output", "code": "model_schema_invalid",
            "repairable": True, "resolution": "schema_repair", "status": "resolved",
            "detail": "", "evidence_sha256": "a" * 64,
        }],
    }
    repaired_validation = validate_trace(repaired_trace)
    results.append({
        "case_id": "single_repaired_trace_is_valid",
        "passed": repaired_validation["valid"],
        "detail": str(repaired_validation["errors"]),
    })
    mutations = {
        "missing_evidence_is_rejected": (
            lambda item: item["evidence"].update({"evidence_id": ""}),
            "missing_evidence_id",
        ),
        "snapshot_identity_mismatch_is_rejected": (
            lambda item: item["snapshot_ids"].update({"trace_id": "TRACE_WRONG"}),
            "snapshot_trace_id_mismatch",
        ),
        "fabricated_citation_is_rejected": (
            lambda item: item["model"].update({
                "selected_citations": [{"citation_id": "FAKE-999#1@9"}]
            }),
            "selected_citation_not_retrieved",
        ),
        "missing_tool_link_is_rejected": (
            lambda item: item.update({"tool_executions": []}),
            "final_plan_missing_tool_execution",
        ),
        "missing_context_manifest_is_rejected": (
            lambda item: item.update({"context": {}}),
            "missing_context_schema_version",
        ),
        "missing_model_input_hash_is_rejected": (
            lambda item: item["context"].update({"model_input_sha256": ""}),
            "missing_model_input_hash",
        ),
        "missing_repair_trace_is_rejected": (
            lambda item: item["reliability"].update({"repair": {}}),
            "missing_repair_schema_version",
        ),
        "missing_run_timing_is_rejected": (
            lambda item: item.update({"timing": {}}),
            "missing_timing_schema_version",
        ),
        "second_repair_attempt_is_rejected": (
            lambda item: item["reliability"]["repair"].update({
                "status": "exhausted", "attempt_count": 2,
                "attempts": [{"attempt": 1}, {"attempt": 2}],
            }),
            "repair_attempt_budget_exceeded",
        ),
        "malformed_failure_attribution_is_rejected": (
            lambda item: item["reliability"].update({
                "failure_attributions": [{"schema_version": "agent-failure-v1"}]
            }),
            "failure_attribution_missing_attribution_id",
        ),
        "citation_not_in_injected_context_is_rejected": (
            lambda item: item["context"].update({"selected_citation_ids": []}),
            "selected_citation_not_in_model_context",
        ),
        "fabricated_final_grounding_is_rejected": (
            lambda item: item["decision"]["grounding"].update({
                "citation_ids": ["FAKE-999#1@9"],
                "citations": [{
                    "citation_id": "FAKE-999#1@9",
                    "binding": "structured_event_exact",
                    "matched_event_types": ["helmet_missing"],
                }],
            }),
            "grounded_citation_not_retrieved",
        ),
    }
    for case_id, (mutate, expected_error) in mutations.items():
        broken = deepcopy(trace)
        broken.pop("validation", None)
        mutate(broken)
        validation = validate_trace(broken)
        results.append({
            "case_id": case_id,
            "passed": not validation["valid"] and expected_error in validation["errors"],
            "detail": str(validation["errors"]),
        })
    return results


def build_report() -> dict:
    results = run_cases()
    passed = sum(bool(item["passed"]) for item in results)
    return {
        "benchmark": "industrial-agent-trace-integrity-v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "cases": len(results), "passed": passed, "failed": len(results) - passed,
            "pass_rate_pct": round(100 * passed / len(results), 2),
        },
        "results": results,
    }


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# Agent Trace Integrity Benchmark", "",
        f"- Cases: {summary['passed']}/{summary['cases']} passed ({summary['pass_rate_pct']}%)",
        "", "| Case | Result | Detail |", "|---|---:|---|",
    ]
    for item in report["results"]:
        lines.append(
            f"| {item['case_id']} | {'PASS' if item['passed'] else 'FAIL'} | {item['detail']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = build_report()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    output.with_suffix(".md").write_text(render_markdown(report), encoding="utf-8")
    summary = report["summary"]
    print(f"Trace integrity benchmark: {summary['passed']}/{summary['cases']} passed")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
