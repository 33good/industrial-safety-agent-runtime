"""Deterministic fault-injection benchmark for Agent Runtime semantics."""
from __future__ import annotations

import argparse
import json
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agents import AlarmEvent
from services.agent_runtime import AgentRuntime
from services.analysis_limiter import AnalysisLimiter
from services.run_store import RunStore
from services.tool_executor import ToolExecutor, ToolSpec


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "benchmarks" / "reports" / "runtime_faults.json"


def _event(case_id: str, decision: bool = False) -> AlarmEvent:
    return AlarmEvent(
        timestamp="benchmark",
        event_id=f"EVT_{case_id}",
        run_id=f"RUN_{case_id}",
        trace_id=f"TRACE_{case_id}",
        events=[{"type": "车辆检测", "level": "C", "bbox": {}, "detail": "benchmark"}],
        dispatch_decision=(
            {"final_level": "C", "plan_validation": {"final_plan": ["database.store"]}}
            if decision else {}
        ),
    )


def _runtime_for_recovery(store: RunStore, executor: ToolExecutor, alarm_dir: Path):
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.run_store = store
    runtime.tool_executor = executor
    runtime.settings = SimpleNamespace(alarm_dir=alarm_dir)
    return runtime


def run_fault_cases() -> list[dict]:
    results = []

    with tempfile.TemporaryDirectory() as tmp:
        executor = ToolExecutor(str(Path(tmp) / "runtime.db"))
        calls = []

        def transient(event, action):
            calls.append(action)
            if len(calls) == 1:
                raise TimeoutError("injected timeout")
            return "ok"

        executor.register("database", transient, ToolSpec("database", max_attempts=2))
        outcome = executor.execute(_event("TRANSIENT"), "database", "store")
        results.append({
            "case_id": "transient_retry_recovers", "passed": outcome.status == "succeeded" and outcome.attempts == 2,
            "detail": f"status={outcome.status},attempts={outcome.attempts}",
        })

        cached = executor.execute(_event("TRANSIENT"), "database", "store")
        results.append({
            "case_id": "duplicate_side_effect_suppressed",
            "passed": cached.reused and len(calls) == 2,
            "detail": f"reused={cached.reused},handler_calls={len(calls)}",
        })

    with tempfile.TemporaryDirectory() as tmp:
        executor = ToolExecutor(str(Path(tmp) / "runtime.db"))
        calls = []

        def permanent(event, action):
            calls.append(action)
            raise ValueError("injected invalid arguments")

        executor.register("reporter", permanent, ToolSpec("reporter", max_attempts=3))
        outcome = executor.execute(_event("PERMANENT"), "reporter", "generate")
        results.append({
            "case_id": "permanent_failure_not_retried",
            "passed": outcome.status == "failed" and outcome.attempts == 1 and len(calls) == 1,
            "detail": f"status={outcome.status},attempts={outcome.attempts}",
        })

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        alarm_dir = root / "alarms"
        alarm_dir.mkdir()
        store = RunStore(str(root / "runtime.db"))
        executor = ToolExecutor(str(root / "runtime.db"))
        event = _event("RUNNING", decision=True)
        store.create(event, "benchmark")
        store.transition(event.run_id, "decided", "policy", event=event)
        store.transition(event.run_id, "executing", "tools", event=event)
        executor.store.begin(
            execution_id="TOOL_RUNNING", run_id=event.run_id, event_id=event.event_id,
            step_id="STEP_RUNNING", idempotency_key="KEY_RUNNING", tool="database", action="store",
        )
        summary = _runtime_for_recovery(store, executor, alarm_dir).recover_incomplete_runs()
        status = store.get(event.run_id)["status"]
        results.append({
            "case_id": "indeterminate_tool_requires_human",
            "passed": summary["manual_takeover"] == 1 and status == "manual_takeover",
            "detail": f"status={status}",
        })

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        alarm_dir = root / "alarms"
        alarm_dir.mkdir()
        store = RunStore(str(root / "runtime.db"))
        executor = ToolExecutor(str(root / "runtime.db"))
        event = _event("RECONCILE", decision=True)
        store.create(event, "benchmark")
        store.transition(event.run_id, "decided", "policy", event=event)
        store.transition(event.run_id, "executing", "tools", event=event)
        executor.store.begin(
            execution_id="TOOL_RECONCILE", run_id=event.run_id, event_id=event.event_id,
            step_id="STEP_RECONCILE", idempotency_key="KEY_RECONCILE", tool="database", action="store",
        )
        executor.store.record_attempt("KEY_RECONCILE", 1)
        executor.store.finish("KEY_RECONCILE", "succeeded", result="stored")
        summary = _runtime_for_recovery(store, executor, alarm_dir).recover_incomplete_runs()
        status = store.get(event.run_id)["status"]
        results.append({
            "case_id": "completed_side_effect_reconciled",
            "passed": summary["finalized"] == 1 and status == "succeeded",
            "detail": f"status={status}",
        })

    limiter = AnalysisLimiter(max_inflight=1)
    release = threading.Event()
    first = limiter.try_start(lambda: release.wait(2), name="fault-benchmark-analysis")
    rejected = limiter.try_start(lambda: None)
    release.set()
    completed = bool(first and first.wait(2))
    results.append({
        "case_id": "vlm_overload_is_bounded",
        "passed": rejected is None and completed and limiter.status()["rejected_total"] == 1,
        "detail": f"rejected_total={limiter.status()['rejected_total']}",
    })
    return results


def build_report() -> dict:
    results = run_fault_cases()
    passed = sum(item["passed"] for item in results)
    return {
        "benchmark": "industrial-agent-runtime-faults-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "cases": len(results), "passed": passed, "failed": len(results) - passed,
            "pass_rate_pct": round(passed * 100 / len(results), 2) if results else 0.0,
        },
        "results": results,
    }


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# Agent Runtime Fault-Injection Benchmark", "",
        f"- Cases: {summary['passed']}/{summary['cases']} passed ({summary['pass_rate_pct']}%)", "",
        "| Case | Result | Detail |", "|---|---:|---|",
    ]
    for item in report["results"]:
        lines.append(f"| {item['case_id']} | {'PASS' if item['passed'] else 'FAIL'} | {item['detail']} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = args.output.resolve()
    report = build_report()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    output.with_suffix(".md").write_text(render_markdown(report), encoding="utf-8")
    summary = report["summary"]
    print(f"Runtime fault benchmark: {summary['passed']}/{summary['cases']} passed")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
