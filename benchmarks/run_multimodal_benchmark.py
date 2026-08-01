"""Live Qwen2.5-VL benchmark with a grounded-SOP/no-RAG comparison."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import statistics

from agents import AlarmEvent
from agents.dispatch import DispatchAgent
from agents.safety_agent import SafetyAgent
from agents.sop_retriever import SOPRetriever
from config import Settings
from services.demo_scenarios import demo_alarm_body, demo_image


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "benchmarks" / "datasets" / "multimodal_agent_cases.jsonl"
DEFAULT_CATALOG = ROOT / "knowledge" / "sop" / "safety_procedures.json"
REPORT_JSON = ROOT / "benchmarks" / "reports" / "multimodal_latest.json"
REPORT_MD = ROOT / "benchmarks" / "reports" / "multimodal_latest.md"
LEVEL_WEIGHT = {"A": 3, "B": 2, "C": 1}


def load_cases(path: Path = DEFAULT_CASES) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _percent(value: int, total: int) -> float:
    return round(value * 100.0 / total, 2) if total else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return round(ordered[index], 1)


def _event(case: dict) -> AlarmEvent:
    scenario = case.get("scenario", "unknown")
    body = demo_alarm_body(scenario) if scenario != "unknown" else {"objInfo": []}
    return AlarmEvent(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        events=list(case.get("events") or []),
        event_id=f"BENCH_{case['case_id']}", run_id=f"RUN_BENCH_{case['case_id']}",
        trace_id=f"TRACE_BENCH_{case['case_id']}",
        raw_json={"source": "benchmark", "cameraId": "benchmark-camera"},
        image_bytes=demo_image(body, scenario),
    )


def run_variant(name: str, cases: list[dict], model: str, url: str, timeout: int,
                retriever: SOPRetriever | None) -> dict:
    agent = SafetyAgent(
        mode="ollama", model=model, base_url=url, timeout_seconds=timeout,
        sop_retriever=retriever,
    )
    rows = []
    valid = risk_match = non_downgrade = candidate_compliant = final_plan_valid = 0
    citation_hits = refusal_correct = 0
    answerable = refusal_cases = 0
    relevant_citations = citation_attempts = rejected_citations = 0
    latencies = []

    for case in cases:
        event = _event(case)
        agent.analyze(event)
        recommendation = event.llm_recommendation or {}
        DispatchAgent().plan(event)
        validation = event.dispatch_decision.get("plan_validation", {})
        expected_level = case["expected_level"]
        actual_level = recommendation.get("risk_level", "")
        expected_citations = set(case.get("expected_citations") or [])
        actual_citations = {
            item.get("citation_id") for item in recommendation.get("sop_citations", [])
            if item.get("citation_id")
        }
        rejected = list(recommendation.get("rejected_sop_citations") or [])
        is_valid = bool(event.llm_json_valid)
        matches = actual_level == expected_level
        safe_level = LEVEL_WEIGHT.get(actual_level, 0) >= LEVEL_WEIGHT.get(expected_level, 0)
        policy_rejected = [item.get("name", "") for item in validation.get("rejected", [])]
        actions_compliant = not policy_rejected and not recommendation.get("rejected_candidate_actions")
        guardrail_valid = bool(validation.get("baseline_preserved"))
        valid += int(is_valid)
        risk_match += int(matches)
        non_downgrade += int(safe_level)
        candidate_compliant += int(actions_compliant)
        final_plan_valid += int(guardrail_valid)
        latencies.append(event.llm_latency_ms)
        relevant_citations += len(actual_citations.intersection(expected_citations))
        citation_attempts += len(actual_citations) + len(rejected)
        rejected_citations += len(rejected)
        if expected_citations:
            answerable += 1
            citation_hits += int(bool(actual_citations.intersection(expected_citations)))
            evidence_ok = bool(actual_citations.intersection(expected_citations)) if retriever else True
        else:
            refusal_cases += 1
            refused = (
                not recommendation.get("sop_answerable")
                and not actual_citations and not rejected
                and bool(recommendation.get("sop_refusal_reason"))
            )
            refusal_correct += int(refused)
            evidence_ok = refused
        passed = is_valid and matches and safe_level and guardrail_valid and evidence_ok
        rows.append({
            "case_id": case["case_id"], "passed": passed,
            "expected_level": expected_level, "actual_level": actual_level,
            "llm_status": event.llm_status, "latency_ms": event.llm_latency_ms,
            "rag_status": event.rag_status,
            "expected_citations": sorted(expected_citations),
            "actual_citations": sorted(actual_citations),
            "rejected_citations": rejected,
            "sop_refusal_reason": recommendation.get("sop_refusal_reason", ""),
            "rejected_candidate_actions": recommendation.get("rejected_candidate_actions", []),
            "policy_rejected_actions": policy_rejected,
            "final_plan": validation.get("final_plan", []),
        })

    return {
        "variant": name,
        "metrics": {
            "cases": len(cases), "passed": sum(int(row["passed"]) for row in rows),
            "structured_output_valid_pct": _percent(valid, len(cases)),
            "risk_level_accuracy_pct": _percent(risk_match, len(cases)),
            "non_downgrade_pct": _percent(non_downgrade, len(cases)),
            "model_candidate_action_compliance_pct": _percent(candidate_compliant, len(cases)),
            "guardrail_final_plan_valid_pct": _percent(final_plan_valid, len(cases)),
            "grounded_citation_coverage_pct": _percent(citation_hits, answerable) if retriever else 0.0,
            "citation_precision_pct": _percent(relevant_citations, citation_attempts),
            "citation_guardrail_rejections": rejected_citations,
            "no_evidence_refusal_accuracy_pct": _percent(refusal_correct, refusal_cases),
            "latency_mean_ms": round(statistics.mean(latencies), 1) if latencies else 0.0,
            "latency_p50_ms": _percentile(latencies, 0.5),
            "latency_p95_ms": _percentile(latencies, 0.95),
        },
        "results": rows,
    }


def build_report(model: str, url: str, timeout: int, compare_no_rag: bool = True) -> dict:
    cases = load_cases()
    retriever = SOPRetriever(DEFAULT_CATALOG)
    probe = SafetyAgent(mode="ollama", model=model, base_url=url, timeout_seconds=timeout).health()
    if probe.get("status") != "ready":
        return {"benchmark": "multimodal_agent", "status": "model_unavailable", "probe": probe, "variants": []}
    warmup_agent = SafetyAgent(mode="ollama", model=model, base_url=url, timeout_seconds=timeout)
    warmup_event = _event(cases[0])
    warmup_agent.analyze(warmup_event)
    variants = []
    if compare_no_rag:
        variants.append(run_variant("no_rag", cases, model, url, timeout, None))
    variants.append(run_variant("grounded_sop_rag", cases, model, url, timeout, retriever))
    return {
        "benchmark": "multimodal_agent", "status": "completed",
        "model": model, "prompt_version": SafetyAgent.PROMPT_VERSION,
        "catalog_version": retriever.catalog_version,
        "warmup_latency_ms": warmup_event.llm_latency_ms,
        "input_mode": "generated replay image + structured detections",
        "variants": variants,
        "scope": (
            "Measures local model structure, policy-facing risk output, grounded citations, refusal and latency. "
            "Generated replay images are not a substitute for a real-world vision accuracy dataset."
        ),
    }


def write_report(report: dict) -> None:
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Multimodal Agent Benchmark", "", f"Status: `{report['status']}`", ""]
    if report["status"] == "completed":
        lines.extend([
            f"Model: `{report['model']}`",
            f"Prompt: `{report['prompt_version']}`",
            f"SOP catalog: `{report['catalog_version']}`", "",
            "| Variant | Valid JSON | Risk accuracy | Non-downgrade | Candidate actions | Guardrail plan | Citation coverage | Citation precision | Refusal accuracy | Mean latency |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for variant in report["variants"]:
            m = variant["metrics"]
            lines.append(
                f"| {variant['variant']} | {m['structured_output_valid_pct']}% | "
                f"{m['risk_level_accuracy_pct']}% | {m['non_downgrade_pct']}% | "
                f"{m['model_candidate_action_compliance_pct']}% | {m['guardrail_final_plan_valid_pct']}% | "
                f"{m['grounded_citation_coverage_pct']}% | {m['citation_precision_pct']}% | "
                f"{m['no_evidence_refusal_accuracy_pct']}% | {m['latency_mean_ms']} ms |"
            )
        for variant in report["variants"]:
            lines.extend(["", f"## {variant['variant']}", "", "| Case | Result | Risk | RAG | Citations | Latency |", "|---|---|---|---|---|---:|"])
            for row in variant["results"]:
                lines.append(
                    f"| {row['case_id']} | {'PASS' if row['passed'] else 'FAIL'} | "
                    f"{row['actual_level'] or '-'} / {row['expected_level']} | {row['rag_status']} | "
                    f"{', '.join(row['actual_citations']) or '-'} | {row['latency_ms']} ms |"
                )
        lines.extend(["", report["scope"]])
    else:
        lines.append(json.dumps(report.get("probe", {}), ensure_ascii=False))
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    settings = Settings.from_env()
    parser.add_argument("--model", default=settings.ollama_model)
    parser.add_argument("--url", default=settings.ollama_url)
    parser.add_argument("--timeout", type=int, default=max(30, settings.llm_timeout_seconds))
    parser.add_argument("--rag-only", action="store_true", help="Skip the no-RAG control variant")
    parser.add_argument("--require-model", action="store_true")
    args = parser.parse_args()
    report = build_report(args.model, args.url, args.timeout, compare_no_rag=not args.rag_only)
    write_report(report)
    if report["status"] != "completed":
        print(f"Multimodal benchmark unavailable: {report.get('probe', {})}")
        return 2 if args.require_model else 0
    for variant in report["variants"]:
        metrics = variant["metrics"]
        print(f"{variant['variant']}: {metrics['passed']}/{metrics['cases']} passed, "
              f"valid={metrics['structured_output_valid_pct']}%, latency={metrics['latency_mean_ms']}ms")
    print(f"JSON: {REPORT_JSON}")
    print(f"Markdown: {REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
