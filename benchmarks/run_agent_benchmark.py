"""Run deterministic policy and guardrail evaluations.

This benchmark deliberately bypasses camera and detector quality. It evaluates the
Agent contract after a stable incident has been produced: structured model output,
risk fusion, mandatory actions, forbidden actions, and deterministic fallback.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents import AlarmEvent
from agents.dispatch import DispatchAgent
from agents.safety_agent import SafetyAgent


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = PROJECT_ROOT / "benchmarks" / "datasets" / "agent_policy_cases.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "benchmarks" / "reports" / "latest.json"


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        case = json.loads(line)
        if not case.get("case_id"):
            raise ValueError(f"missing case_id at {path}:{line_number}")
        cases.append(case)
    if not cases:
        raise ValueError(f"no benchmark cases found in {path}")
    return cases


def _percent(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(numerator * 100.0 / denominator, 2)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 3)


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    safety = SafetyAgent(mode="benchmark")
    dispatch = DispatchAgent()

    raw_output = case.get("llm_output", "")
    if isinstance(raw_output, dict):
        raw_output = json.dumps(raw_output, ensure_ascii=False)
    recommendation = safety._parse_recommendation(str(raw_output))
    structured_valid = bool(recommendation.get("risk_level"))

    event = AlarmEvent(
        timestamp="benchmark",
        event_id=f"BENCH_{case['case_id']}",
        events=case.get("events") or [],
        llm_recommendation=recommendation,
    )
    rule_level = dispatch._highest_event_level(event)
    decision = dispatch._make_decision(event, rule_level)
    _, validation = dispatch._validate_tool_plan(decision["final_level"], event)

    expected = case.get("expected") or {}
    final_actions = validation["final_plan"]
    rejected_actions = {item["name"] for item in validation["rejected"]}
    required_actions = set(expected.get("required_actions") or [])
    forbidden_actions = set(expected.get("forbidden_actions") or [])
    expected_actions = expected.get("final_actions")

    checks = {
        "structured_output_handled": structured_valid == bool(expected.get("structured_valid", True)),
        "final_level_correct": decision["final_level"] == expected.get("final_level"),
        "required_actions_present": required_actions.issubset(final_actions),
        "final_actions_exact": expected_actions is None or final_actions == expected_actions,
        "forbidden_actions_blocked": (
            not forbidden_actions
            or (forbidden_actions.isdisjoint(final_actions) and forbidden_actions.issubset(rejected_actions))
        ),
        "fallback_correct": (
            not expected.get("fallback", False)
            or (not structured_valid and decision["final_level"] == rule_level)
        ),
        "approval_policy_correct": (
            expected.get("approval_required") is None
            or (decision["final_level"] == "A") == bool(expected["approval_required"])
        ),
    }
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "case_id": case["case_id"],
        "category": case.get("category", "uncategorized"),
        "passed": all(checks.values()),
        "checks": checks,
        "latency_ms": elapsed_ms,
        "actual": {
            "structured_valid": structured_valid,
            "rule_level": rule_level,
            "llm_level": decision.get("llm_level", ""),
            "final_level": decision["final_level"],
            "final_actions": final_actions,
            "rejected_actions": sorted(rejected_actions),
            "policy": decision["policy"],
        },
    }


def build_report(cases: list[dict[str, Any]], dataset_path: Path) -> dict[str, Any]:
    results = [evaluate_case(case) for case in cases]
    latencies = [result["latency_ms"] for result in results]

    def count_check(name: str) -> int:
        return sum(bool(result["checks"].get(name)) for result in results)

    forbidden_cases = [case for case in cases if (case.get("expected") or {}).get("forbidden_actions")]
    fallback_cases = [case for case in cases if (case.get("expected") or {}).get("fallback")]
    high_risk_cases = [case for case in cases if (case.get("expected") or {}).get("approval_required") is True]

    result_by_id = {result["case_id"]: result for result in results}
    forbidden_passed = sum(result_by_id[case["case_id"]]["checks"]["forbidden_actions_blocked"] for case in forbidden_cases)
    fallback_passed = sum(result_by_id[case["case_id"]]["checks"]["fallback_correct"] for case in fallback_cases)
    approval_passed = sum(result_by_id[case["case_id"]]["checks"]["approval_policy_correct"] for case in high_risk_cases)

    total = len(results)
    passed = sum(result["passed"] for result in results)
    return {
        "benchmark": "industrial-agent-policy-v1",
        "scope": "post-perception policy, guardrails, and deterministic fallback",
        "dataset": str(dataset_path.relative_to(PROJECT_ROOT)),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "cases": total,
            "passed": passed,
            "failed": total - passed,
            "case_pass_rate_pct": _percent(passed, total),
            "structured_output_handling_pct": _percent(count_check("structured_output_handled"), total),
            "final_level_accuracy_pct": _percent(count_check("final_level_correct"), total),
            "action_plan_exact_match_pct": _percent(count_check("final_actions_exact"), total),
            "mandatory_action_coverage_pct": _percent(count_check("required_actions_present"), total),
            "forbidden_action_block_rate_pct": _percent(forbidden_passed, len(forbidden_cases)),
            "fallback_success_rate_pct": _percent(fallback_passed, len(fallback_cases)),
            "high_risk_approval_policy_pct": _percent(approval_passed, len(high_risk_cases)),
            "policy_latency_ms": {
                "mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
            },
        },
        "results": results,
    }


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Agent Policy Benchmark",
        "",
        f"- Scope: {report['scope']}",
        f"- Dataset: `{report['dataset']}`",
        f"- Cases: {summary['passed']}/{summary['cases']} passed ({summary['case_pass_rate_pct']}%)",
        f"- Final-level accuracy: {summary['final_level_accuracy_pct']}%",
        f"- Action-plan exact match: {summary['action_plan_exact_match_pct']}%",
        f"- Forbidden-action block rate: {summary['forbidden_action_block_rate_pct']}%",
        f"- Fallback success rate: {summary['fallback_success_rate_pct']}%",
        f"- High-risk approval policy: {summary['high_risk_approval_policy_pct']}%",
        f"- Policy latency P50/P95: {summary['policy_latency_ms']['p50']} / {summary['policy_latency_ms']['p95']} ms",
        "",
        "| Case | Category | Result | Final level | Rejected actions |",
        "|---|---|---:|---:|---|",
    ]
    for result in report["results"]:
        rejected = ", ".join(result["actual"]["rejected_actions"]) or "-"
        lines.append(
            f"| {result['case_id']} | {result['category']} | "
            f"{'PASS' if result['passed'] else 'FAIL'} | {result['actual']['final_level']} | {rejected} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    cases_path = args.cases.resolve()
    output_path = args.output.resolve()
    report = build_report(load_cases(cases_path), cases_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path = output_path.with_suffix(".md")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")

    summary = report["summary"]
    print(
        f"Agent benchmark: {summary['passed']}/{summary['cases']} passed "
        f"({summary['case_pass_rate_pct']}%)"
    )
    print(f"JSON: {output_path}")
    print(f"Markdown: {markdown_path}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
