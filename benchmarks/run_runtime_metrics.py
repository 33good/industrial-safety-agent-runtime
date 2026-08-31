"""Deterministic concurrency and observability benchmark for the SQLite control plane."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

from agents import AlarmEvent
from agents.failure_attribution import new_repair_trace
from services.analysis_limiter import AnalysisLimiter
from services.run_store import RunStore
from services.runtime_metrics import RuntimeMetricsService, summarize_distribution
from services.tool_executor import ToolExecutionStore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "benchmarks" / "reports" / "runtime_metrics.json"
UNIQUE_RUNS = 32
DUPLICATE_REQUESTS = 20
WORKERS = 12


def _event(index: int, *, duplicate: bool = False) -> AlarmEvent:
    suffix = "duplicate" if duplicate else f"{index:03d}"
    event = AlarmEvent(
        timestamp="benchmark",
        event_id=f"EVT_METRIC_{suffix}_{index}",
        run_id=f"RUN_METRIC_{suffix}_{index}",
        trace_id=f"TRACE_METRIC_{suffix}_{index}",
        source_event_id=f"source-{suffix}",
        ingest_key=f"ingest-{suffix}",
        ingest_payload_hash=("d" if duplicate else f"{index % 10}") * 64,
        camera_id="benchmark-camera",
        evidence_id=f"EVID_METRIC_{suffix}_{index}",
        events=[{"type": "benchmark", "level": "C", "bbox": {}, "detail": "load"}],
        llm_status="success",
        llm_latency_ms=float(10 + index),
        repair_trace=new_repair_trace(),
        lifecycle_status="analyzing",
    )
    if not duplicate and index % 8 == 0:
        event.failure_attributions = [{
            "stage": "policy", "code": "candidate_action_rejected",
            "resolution": "guardrail", "status": "contained",
        }]
    return event


def _create_completed(database: Path, index: int) -> dict:
    store = RunStore(str(database))
    event = _event(index)
    started = time.perf_counter()
    _, created = store.create_or_get(event, "metrics-benchmark")
    store.transition(event.run_id, "decided", "policy", event=event)
    store.transition(event.run_id, "executing", "tools", event=event)
    event.lifecycle_status = "succeeded"
    store.transition(event.run_id, "succeeded", "complete", event=event)
    return {
        "created": created,
        "run_id": event.run_id,
        "latency_ms": (time.perf_counter() - started) * 1000,
    }


def _create_duplicate(database: Path, index: int) -> dict:
    store = RunStore(str(database))
    event = _event(index, duplicate=True)
    started = time.perf_counter()
    row, created = store.create_or_get(event, "metrics-benchmark")
    return {
        "created": created,
        "run_id": row["run_id"],
        "latency_ms": (time.perf_counter() - started) * 1000,
    }


def _seed_tools(database: Path, run_ids: list[str]) -> None:
    store = ToolExecutionStore(str(database))
    for index, run_id in enumerate(run_ids[:8]):
        key = f"metric-tool-key-{index}"
        store.begin(
            execution_id=f"TOOL_METRIC_{index}", run_id=run_id,
            event_id=f"EVT_TOOL_{index}", step_id=f"STEP_METRIC_{index}",
            idempotency_key=key, tool="database", action="store",
        )
        attempts = 2 if index < 2 else 1
        store.record_attempt(key, attempts)
        if index < 6:
            store.finish(key, "succeeded", result={"stored": True})
        else:
            store.finish(
                key, "failed", error_type="benchmark_failure",
                error_message="injected",
            )


def build_report() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp) / "runtime.db"
        # Initialize once before concurrent independent connections race.
        RunStore(str(database))
        wall_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            unique_results = list(pool.map(
                lambda index: _create_completed(database, index), range(UNIQUE_RUNS)
            ))
        unique_wall_ms = (time.perf_counter() - wall_started) * 1000

        duplicate_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            duplicate_results = list(pool.map(
                lambda index: _create_duplicate(database, index),
                range(DUPLICATE_REQUESTS),
            ))
        duplicate_wall_ms = (time.perf_counter() - duplicate_started) * 1000

        run_ids = [item["run_id"] for item in unique_results]
        _seed_tools(database, run_ids)
        run_store = RunStore(str(database))
        tool_store = ToolExecutionStore(str(database))
        limiter = AnalysisLimiter(max_inflight=2)
        metrics = RuntimeMetricsService(run_store, tool_store, limiter).snapshot(500)

    unique_latency = summarize_distribution(item["latency_ms"] for item in unique_results)
    duplicate_latency = summarize_distribution(item["latency_ms"] for item in duplicate_results)
    unique_created = sum(bool(item["created"]) for item in unique_results)
    duplicate_created = sum(bool(item["created"]) for item in duplicate_results)
    duplicate_run_ids = {item["run_id"] for item in duplicate_results}
    latency = metrics["latency_ms"]
    percentile_ordered = all(
        item["count"] == 0 or item["p50"] <= item["p95"] <= item["max"]
        for item in latency.values()
    )
    results = [{
        "case_id": "concurrent_unique_runs_are_not_lost",
        "passed": unique_created == UNIQUE_RUNS,
        "detail": f"created={unique_created}/{UNIQUE_RUNS}",
    }, {
        "case_id": "concurrent_duplicate_ingress_has_one_owner",
        "passed": duplicate_created == 1 and len(duplicate_run_ids) == 1,
        "detail": f"created={duplicate_created} run_ids={len(duplicate_run_ids)}",
    }, {
        "case_id": "durable_run_projection_is_consistent",
        "passed": (
            metrics["scope"]["run_count"] == UNIQUE_RUNS + 1
            and metrics["runs"]["status_counts"].get("succeeded") == UNIQUE_RUNS
            and metrics["runs"]["active_count"] == 1
            and metrics["runs"]["success_rate_pct"] == 100.0
        ),
        "detail": (
            f"runs={metrics['scope']['run_count']} "
            f"statuses={metrics['runs']['status_counts']}"
        ),
    }, {
        "case_id": "tool_retry_metrics_match_durable_rows",
        "passed": (
            metrics["tools"]["execution_count"] == 8
            and metrics["tools"]["status_counts"] == {"failed": 2, "succeeded": 6}
            and metrics["tools"]["retry_attempts"] == 2
            and metrics["tools"]["retried_execution_count"] == 2
            and metrics["latency_ms"]["tool_execution"]["count"] == 8
            and metrics["tools"]["by_action"]["database.store"]["execution_count"] == 8
        ),
        "detail": str(metrics["tools"]),
    }, {
        "case_id": "stage_percentiles_are_well_formed",
        "passed": percentile_ordered and latency["end_to_end"]["count"] == UNIQUE_RUNS,
        "detail": f"end_to_end={latency['end_to_end']}",
    }, {
        "case_id": "capacity_scope_is_explicit",
        "passed": (
            metrics["capacity"].get("max_inflight") == 2
            and "current process lifetime" in metrics["scope"]["notes"]
            and "not a distributed SLA" in metrics["scope"]["notes"]
        ),
        "detail": str(metrics["capacity"]),
    }]
    passed = sum(bool(item["passed"]) for item in results)
    total_operations = UNIQUE_RUNS + DUPLICATE_REQUESTS
    total_wall_seconds = max((unique_wall_ms + duplicate_wall_ms) / 1000, 0.000001)
    return {
        "benchmark": "industrial-agent-runtime-observability-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": (
            "Measures concurrent SQLite control-plane writes, ingress uniqueness and durable "
            "metrics aggregation. It does not measure Qwen, detector or network-tool throughput."
        ),
        "configuration": {
            "unique_runs": UNIQUE_RUNS,
            "duplicate_requests": DUPLICATE_REQUESTS,
            "workers": WORKERS,
        },
        "summary": {
            "cases": len(results), "passed": passed, "failed": len(results) - passed,
            "pass_rate_pct": round(passed * 100 / len(results), 2),
            "observed_operations_per_second": round(total_operations / total_wall_seconds, 2),
        },
        "load_observations": {
            "unique_wall_ms": round(unique_wall_ms, 3),
            "duplicate_wall_ms": round(duplicate_wall_ms, 3),
            "unique_operation_latency_ms": unique_latency,
            "duplicate_operation_latency_ms": duplicate_latency,
        },
        "metrics_snapshot": metrics,
        "results": results,
    }


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    observations = report["load_observations"]
    lines = [
        "# Runtime Observability & Concurrency Benchmark", "",
        f"- Cases: {summary['passed']}/{summary['cases']} passed ({summary['pass_rate_pct']}%)",
        f"- Observed control-plane throughput: {summary['observed_operations_per_second']} ops/s",
        f"- Unique write P50/P95: {observations['unique_operation_latency_ms']['p50']}/"
        f"{observations['unique_operation_latency_ms']['p95']} ms",
        f"- Duplicate ingress P50/P95: {observations['duplicate_operation_latency_ms']['p50']}/"
        f"{observations['duplicate_operation_latency_ms']['p95']} ms", "",
        "> " + report["scope"], "",
        "| Case | Result | Detail |", "|---|---:|---|",
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
    print(f"Runtime observability benchmark: {summary['passed']}/{summary['cases']} passed")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
