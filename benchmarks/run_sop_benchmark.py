"""Deterministic SOP retrieval and refusal benchmark."""
from __future__ import annotations

import json
from pathlib import Path

from agents.sop_retriever import SOPRetriever


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "benchmarks" / "datasets" / "sop_retrieval_cases.jsonl"
DEFAULT_CATALOG = ROOT / "knowledge" / "sop" / "safety_procedures.json"
REPORT_JSON = ROOT / "benchmarks" / "reports" / "sop_retrieval.json"
REPORT_MD = ROOT / "benchmarks" / "reports" / "sop_retrieval.md"


def _percent(value: int, total: int) -> float:
    return round(value * 100.0 / total, 2) if total else 0.0


def load_cases(path: Path = DEFAULT_CASES) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_report(cases: list[dict] | None = None, retriever: SOPRetriever | None = None) -> dict:
    cases = cases or load_cases()
    retriever = retriever or SOPRetriever(DEFAULT_CATALOG)
    results = []
    reciprocal_rank_sum = 0.0
    answerable = refusal_cases = 0
    hit_at_1 = hit_at_k = refusals_correct = traceable = 0

    for case in cases:
        result = retriever.retrieve(
            case.get("query", ""), case.get("event_types", []), case.get("risk_levels", [])
        )
        actual = [item["citation_id"] for item in result["citations"]]
        expected = list(case.get("expected_citations") or [])
        if expected:
            answerable += 1
            first_rank = next((index + 1 for index, item in enumerate(actual) if item in expected), 0)
            if first_rank == 1:
                hit_at_1 += 1
            if first_rank:
                hit_at_k += 1
                reciprocal_rank_sum += 1.0 / first_rank
        else:
            refusal_cases += 1
            if result["status"] == "no_evidence" and not actual and result.get("refusal_reason"):
                refusals_correct += 1
        citations_traceable = all(
            all(item.get(field) for field in ("citation_id", "document_id", "section", "version", "source", "excerpt"))
            for item in result["citations"]
        )
        traceable += int(citations_traceable)
        passed = (
            (bool(expected) and any(item in expected for item in actual))
            or (not expected and result["status"] == "no_evidence" and not actual)
        ) and citations_traceable
        results.append({
            "case_id": case["case_id"], "passed": passed,
            "expected_citations": expected, "actual_citations": actual,
            "status": result["status"], "refusal_reason": result.get("refusal_reason", ""),
        })

    metrics = {
        "cases": len(cases),
        "passed": sum(int(item["passed"]) for item in results),
        "retrieval_hit_at_1_pct": _percent(hit_at_1, answerable),
        "retrieval_hit_at_k_pct": _percent(hit_at_k, answerable),
        "mean_reciprocal_rank": round(reciprocal_rank_sum / answerable, 4) if answerable else 0.0,
        "no_evidence_refusal_accuracy_pct": _percent(refusals_correct, refusal_cases),
        "citation_traceability_pct": _percent(traceable, len(cases)),
    }
    return {
        "benchmark": "sop_retrieval", "catalog_version": retriever.catalog_version,
        "metrics": metrics, "results": results,
        "scope": "Deterministic retrieval/citation/refusal only; does not measure LLM quality.",
    }


def write_report(report: dict) -> None:
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = report["metrics"]
    lines = [
        "# SOP Retrieval Benchmark", "",
        f"Catalog: `{report['catalog_version']}`", "",
        "| Metric | Value |", "|---|---:|",
        *[f"| {key} | {value} |" for key, value in metrics.items()], "",
        "| Case | Status | Expected | Actual |", "|---|---|---|---|",
    ]
    for item in report["results"]:
        lines.append(
            f"| {item['case_id']} | {'PASS' if item['passed'] else 'FAIL'} | "
            f"{', '.join(item['expected_citations']) or 'REFUSE'} | "
            f"{', '.join(item['actual_citations']) or 'REFUSE'} |"
        )
    lines.extend(["", report["scope"]])
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    report = build_report()
    write_report(report)
    metrics = report["metrics"]
    print(f"SOP benchmark: {metrics['passed']}/{metrics['cases']} passed")
    print(f"JSON: {REPORT_JSON}")
    print(f"Markdown: {REPORT_MD}")
    return 0 if metrics["passed"] == metrics["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
