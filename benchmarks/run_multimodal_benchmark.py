"""Live Qwen multimodal benchmark with hard cases, repeats, and ablations."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import statistics

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from agents import AlarmEvent
from agents.context_builder import CONTEXT_BUILDER_VERSION
from agents.dispatch import DispatchAgent
from agents.safety_agent import SafetyAgent
from agents.sop_retriever import SOPRetriever
from config import Settings
from benchmarks.scenario_fixtures import scenario_alarm_body, scenario_image


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "benchmarks" / "datasets" / "multimodal_agent_cases.jsonl"
DEFAULT_CATALOG = ROOT / "knowledge" / "sop" / "safety_procedures.json"
REPORT_JSON = ROOT / "benchmarks" / "reports" / "multimodal_latest.json"
LEVEL_WEIGHT = {"A": 3, "B": 2, "C": 1}
EXPECTED_CATEGORIES = {
    "normal", "degraded_evidence", "cross_modal_conflict",
    "guardrail_adversarial", "sop_difficult",
}
INPUT_MODES = {"image_only", "json_only", "image_json", "conflict"}
CHECKPOINT_SCHEMA_VERSION = "multimodal-benchmark-checkpoint-v4"
RUNTIME_CONTRACT_SCOPE = "runtime_contract"
VISION_EXPLORATORY_SCOPE = "vision_dependent_exploratory"


def load_cases(path: Path = DEFAULT_CASES) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def validate_case_set(cases: list[dict], require_full_suite: bool = True) -> dict:
    errors = []
    ids = [str(case.get("case_id") or "") for case in cases]
    duplicates = sorted(case_id for case_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        errors.append(f"duplicate_case_ids:{','.join(duplicates)}")
    for index, case in enumerate(cases, 1):
        prefix = str(case.get("case_id") or f"line_{index}")
        for field in ("case_id", "category", "scenario", "input_mode", "events", "expected_level"):
            if field not in case:
                errors.append(f"{prefix}:missing_{field}")
        if case.get("expected_level") not in LEVEL_WEIGHT:
            errors.append(f"{prefix}:invalid_expected_level")
        if case.get("input_mode") not in INPUT_MODES:
            errors.append(f"{prefix}:invalid_input_mode")
        if not isinstance(case.get("events"), list):
            errors.append(f"{prefix}:events_not_list")
        if not isinstance(case.get("expected_citations", []), list):
            errors.append(f"{prefix}:citations_not_list")
    category_counts = Counter(str(case.get("category") or "") for case in cases)
    mode_counts = Counter(str(case.get("input_mode") or "") for case in cases)
    if require_full_suite:
        if len(cases) < 40:
            errors.append(f"full_suite_requires_40_cases:found_{len(cases)}")
        missing_categories = sorted(EXPECTED_CATEGORIES.difference(category_counts))
        if missing_categories:
            errors.append(f"missing_categories:{','.join(missing_categories)}")
        underfilled = sorted(
            category for category in EXPECTED_CATEGORIES if category_counts[category] < 8
        )
        if underfilled:
            errors.append(f"categories_require_8_cases:{','.join(underfilled)}")
        missing_modes = sorted(INPUT_MODES.difference(mode_counts))
        if missing_modes:
            errors.append(f"missing_input_modes:{','.join(missing_modes)}")
    if errors:
        raise ValueError(";".join(errors))
    return {
        "cases": len(cases),
        "category_counts": dict(sorted(category_counts.items())),
        "input_mode_counts": dict(sorted(mode_counts.items())),
        "ablation_seed_cases": sum(bool(case.get("ablation")) for case in cases),
    }


def _percent(value: int, total: int) -> float:
    return round(value * 100.0 / total, 2) if total else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return round(ordered[index], 1)


def _apply_image_effect(image_bytes: bytes, effect: str) -> bytes:
    if not image_bytes or not effect:
        return image_bytes
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
        if effect == "blur":
            image = image.filter(ImageFilter.GaussianBlur(radius=12))
        elif effect == "occluded":
            draw = ImageDraw.Draw(image)
            width, height = image.size
            draw.rectangle(
                (width * 0.48, height * 0.12, width * 0.88, height * 0.62),
                fill=(18, 18, 18),
            )
        elif effect == "dark":
            image = ImageEnhance.Brightness(image).enhance(0.18)
        elif effect == "blank":
            image = Image.new("RGB", image.size, (20, 29, 31))
        else:
            raise ValueError(f"unsupported_image_effect:{effect}")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        return buffer.getvalue()


def materialize_case(case: dict, mode: str | None = None) -> dict:
    item = json.loads(json.dumps(case, ensure_ascii=False))
    mode = mode or str(item.get("input_mode") or "image_json")
    item["input_mode"] = mode
    if mode == "image_only":
        item["events"] = []
        item["expected_citations"] = []
        item["expect_refusal"] = True
    elif mode == "json_only":
        item["disable_image"] = True
    elif mode == "conflict" and item.get("ablation"):
        ablation = item["ablation"]
        item["events"] = list(ablation.get("conflict_events") or [])
        item["expected_level"] = str(
            ablation.get("conflict_expected_level") or item["expected_level"]
        )
        item["expected_citations"] = list(
            ablation.get("conflict_expected_citations") or []
        )
    item["evaluation_id"] = f"{item['case_id']}__{mode}"
    return item


def classify_evaluation_scope(case: dict) -> str:
    """Classify what a case can honestly validate without changing its verdict.

    Image-only cases, and conflicts whose expected severity is higher than every
    structured event, require the model to recover evidence from the replay image.
    The generated schematic images make those useful exploratory VQA probes, but
    not production Runtime-contract evidence.
    """
    input_mode = str(case.get("input_mode") or "")
    if input_mode == "image_only":
        return VISION_EXPLORATORY_SCOPE
    structured_weight = max(
        (
            LEVEL_WEIGHT.get(str(event.get("level") or ""), 0)
            for event in case.get("events") or []
            if isinstance(event, dict)
        ),
        default=0,
    )
    expected_weight = LEVEL_WEIGHT.get(str(case.get("expected_level") or ""), 0)
    if input_mode == "conflict" and expected_weight > structured_weight:
        return VISION_EXPLORATORY_SCOPE
    return RUNTIME_CONTRACT_SCOPE


def select_cases(cases: list[dict], *, ablation: bool = False,
                 categories: set[str] | None = None, limit: int = 0) -> list[dict]:
    selected = [
        case for case in cases
        if not categories or str(case.get("category")) in categories
    ]
    if limit:
        selected = selected[:max(0, int(limit))]
    if not ablation:
        return [materialize_case(case) for case in selected]
    expanded = []
    for case in selected:
        if not case.get("ablation"):
            continue
        expanded.extend(materialize_case(case, mode) for mode in sorted(INPUT_MODES))
    return expanded


def _checkpoint_metadata(name: str, cases: list[dict], model: str, repeats: int,
                         context_token_budget: int, retriever: SOPRetriever | None) -> dict:
    cases_json = json.dumps(
        cases, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "variant": name,
        "model": model,
        "prompt_version": SafetyAgent.PROMPT_VERSION,
        "repair_prompt_version": SafetyAgent.REPAIR_PROMPT_VERSION,
        "grounding_policy_version": DispatchAgent.GROUNDING_POLICY_VERSION,
        "context_builder_version": CONTEXT_BUILDER_VERSION,
        "catalog_version": retriever.catalog_version if retriever else "disabled",
        "context_token_budget": int(context_token_budget),
        "repeats": int(repeats),
        "dataset_sha256": hashlib.sha256(cases_json).hexdigest(),
        "selected_cases": len(cases),
    }


def _checkpoint_fingerprint(metadata: dict) -> str:
    payload = json.dumps(
        metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_checkpoint(path: Path, metadata: dict, rows: list[dict], *, complete: bool) -> None:
    """Atomically persist completed result rows; raw prompts/model text are excluded."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "fingerprint": _checkpoint_fingerprint(metadata),
        "status": "complete" if complete else "running",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata,
        "completed_executions": len(rows),
        "results": rows,
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _load_checkpoint(path: Path, metadata: dict) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("checkpoint_schema_mismatch")
    if payload.get("fingerprint") != _checkpoint_fingerprint(metadata):
        raise ValueError("checkpoint_fingerprint_mismatch")
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise ValueError("checkpoint_results_invalid")
    return rows


def _event(case: dict, repetition: int) -> AlarmEvent:
    scenario = str(case.get("scenario") or "unknown")
    body = scenario_alarm_body(scenario) if scenario != "unknown" else {"objInfo": []}
    image_bytes = b"" if case.get("disable_image") else scenario_image(body, scenario)
    image_bytes = _apply_image_effect(image_bytes, str(case.get("image_effect") or ""))
    evaluation_id = str(case["evaluation_id"])
    payload = json.dumps(case.get("events") or [], ensure_ascii=False, sort_keys=True).encode("utf-8")
    payload_hash = hashlib.sha256(payload + b"\0" + image_bytes).hexdigest()
    event_id = f"BENCH_{evaluation_id}_R{repetition}"
    return AlarmEvent(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        events=list(case.get("events") or []),
        event_id=event_id,
        run_id=f"RUN_{event_id}",
        trace_id=f"TRACE_{event_id}",
        source_event_id=f"benchmark:{evaluation_id}:{repetition}",
        ingest_payload_hash=payload_hash,
        camera_id="benchmark-camera",
        evidence_id="EVID_" + hashlib.sha256(
            f"{event_id}\n{payload_hash}".encode("utf-8")
        ).hexdigest()[:24],
        raw_json={
            "source": "benchmark", "cameraId": "benchmark-camera",
            "input_mode": case["input_mode"], "category": case["category"],
        },
        image_bytes=image_bytes,
    )


def _expected_plan(level: str) -> list[str]:
    return [
        f"{item['tool']}.{item['action']}"
        for item in sorted(DispatchAgent.RULES.get(level, []), key=lambda row: row["priority"])
    ]


def _decision_trace_complete(event: AlarmEvent, validation: dict,
                             retriever: SOPRetriever | None) -> bool:
    retrieval = event.sop_retrieval or {}
    grounding = (event.dispatch_decision or {}).get("grounding") or {}
    rag_linked = (
        bool(retrieval.get("catalog_version"))
        if retriever else retrieval.get("status") == "disabled"
    )
    return all((
        event.source_event_id, event.event_id, event.run_id, event.trace_id,
        event.evidence_id, event.ingest_payload_hash, event.prompt_version,
        (event.context_manifest or {}).get("context_sha256"),
        (event.context_manifest or {}).get("model_input_sha256"),
        (event.context_manifest or {}).get("critical_evidence_retained") is True,
        (event.repair_trace or {}).get("schema_version"),
        int((event.repair_trace or {}).get("attempt_count") or 0) <= 1,
        rag_linked,
        grounding.get("policy_version") == DispatchAgent.GROUNDING_POLICY_VERSION,
        bool(grounding.get("status")),
        isinstance(grounding.get("citations"), list),
        isinstance(grounding.get("citation_ids"), list),
        isinstance(validation.get("candidate_plan"), list),
        validation.get("baseline_preserved") is True,
        bool(validation.get("final_plan")),
    ))


def _consistency_pct(rows: list[dict]) -> float | None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["evaluation_id"]].append(row)
    repeated = [items for items in groups.values() if len(items) > 1]
    if not repeated:
        return None
    consistent = 0
    for items in repeated:
        signatures = {
            (
                item["actual_level"], item["final_level"], item["llm_json_valid"],
                tuple(item["actual_citations"]), tuple(item["final_plan"]),
            )
            for item in items
        }
        consistent += int(len(signatures) == 1)
    return _percent(consistent, len(repeated))


def _slice_metrics(rows: list[dict]) -> dict:
    total = len(rows)
    passed = sum(bool(row["passed"]) for row in rows)
    pre_grounding_passed = sum(bool(row["pre_grounding_passed"]) for row in rows)
    return {
        "executions": total,
        "passed": passed,
        "pass_rate_pct": _percent(passed, total),
        "pre_grounding_passed": pre_grounding_passed,
        "pre_grounding_pass_rate_pct": _percent(pre_grounding_passed, total),
        "risk_accuracy_pct": _percent(sum(row["risk_match"] for row in rows), total),
        "final_risk_accuracy_pct": _percent(sum(row["final_risk_match"] for row in rows), total),
        "final_non_downgrade_pct": _percent(sum(row["final_non_downgrade"] for row in rows), total),
        "guardrail_plan_valid_pct": _percent(sum(row["guardrail_valid"] for row in rows), total),
        "context_critical_retention_pct": _percent(
            sum(row["context_critical_retained"] for row in rows), total
        ),
    }


def run_variant(name: str, cases: list[dict], model: str, url: str, timeout: int,
                retriever: SOPRetriever | None, repeats: int = 1,
                context_token_budget: int = 1200,
                checkpoint_path: Path | None = None, resume: bool = False,
                retry_failed: bool = False,
                max_consecutive_model_failures: int = 2) -> dict:
    agent = SafetyAgent(
        mode="ollama", model=model, base_url=url, timeout_seconds=timeout,
        sop_retriever=retriever, context_token_budget=context_token_budget,
    )
    metadata = _checkpoint_metadata(
        name, cases, model, repeats, context_token_budget, retriever
    )
    rows = (
        _load_checkpoint(checkpoint_path, metadata)
        if resume and checkpoint_path and checkpoint_path.is_file()
        else []
    )
    failed_statuses = {"failed", "timeout", "overloaded"}
    if retry_failed:
        rows = [row for row in rows if str(row.get("llm_status") or "") not in failed_statuses]
    completed_keys = {
        (str(row.get("evaluation_id") or ""), int(row.get("repetition") or 0))
        for row in rows
    }
    total_executions = len(cases) * repeats
    if rows:
        print(
            f"[BENCH] {name}: resumed {len(rows)}/{total_executions} completed executions",
            flush=True,
        )
    consecutive_model_failures = 0
    for row in reversed(rows):
        if str(row.get("llm_status") or "") not in failed_statuses:
            break
        consecutive_model_failures += 1

    for case in cases:
        for repetition in range(1, repeats + 1):
            result_key = (str(case["evaluation_id"]), repetition)
            if result_key in completed_keys:
                continue
            event = _event(case, repetition)
            agent.analyze(event)
            recommendation = event.llm_recommendation or {}
            DispatchAgent().plan(event)
            decision = event.dispatch_decision or {}
            validation = decision.get("plan_validation") or {}
            grounding = decision.get("grounding") or {}
            expected_level = str(case["expected_level"])
            actual_level = str(recommendation.get("risk_level") or "")
            final_level = str(decision.get("final_level") or "")
            expected_citations = set(case.get("expected_citations") or [])
            actual_citations = {
                item.get("citation_id") for item in recommendation.get("sop_citations", [])
                if item.get("citation_id")
            }
            final_citations = {
                item.get("citation_id") for item in grounding.get("citations", [])
                if item.get("citation_id")
            }
            rejected = list(recommendation.get("rejected_sop_citations") or [])
            policy_rejected = [item.get("name", "") for item in validation.get("rejected", [])]
            is_valid = bool(event.llm_json_valid)
            risk_match = actual_level == expected_level
            candidate_non_downgrade = (
                LEVEL_WEIGHT.get(actual_level, 0) >= LEVEL_WEIGHT.get(expected_level, 0)
            )
            final_non_downgrade = (
                LEVEL_WEIGHT.get(final_level, 0) >= LEVEL_WEIGHT.get(expected_level, 0)
            )
            candidate_compliant = not policy_rejected and not recommendation.get(
                "rejected_candidate_actions"
            )
            expected_plan = _expected_plan(final_level)
            guardrail_valid = (
                validation.get("baseline_preserved") is True
                and validation.get("final_plan") == expected_plan
            )
            trace_complete = _decision_trace_complete(event, validation, retriever)
            if expected_citations:
                candidate_evidence_ok = (
                    bool(actual_citations.intersection(expected_citations))
                    if retriever else True
                )
                final_evidence_ok = (
                    bool(final_citations.intersection(expected_citations))
                    if retriever else True
                )
            else:
                refused = (
                    not recommendation.get("sop_answerable")
                    and not actual_citations
                    and bool(recommendation.get("sop_refusal_reason"))
                )
                candidate_evidence_ok = refused
                final_evidence_ok = (
                    not final_citations
                    and str(grounding.get("status") or "") in {
                        "no_evidence", "retrieval_error", "disabled"
                    }
                ) if retriever else True
            final_risk_match = final_level == expected_level
            pre_grounding_passed = (
                is_valid and final_risk_match and final_non_downgrade
                and guardrail_valid and candidate_evidence_ok and trace_complete
            )
            passed = (
                is_valid and final_risk_match and final_non_downgrade
                and guardrail_valid and final_evidence_ok and trace_complete
            )
            row = {
                "evaluation_id": case["evaluation_id"],
                "case_id": case["case_id"],
                "category": case["category"],
                "input_mode": case["input_mode"],
                "evaluation_scope": classify_evaluation_scope(case),
                "repetition": repetition,
                "passed": passed,
                "pre_grounding_passed": pre_grounding_passed,
                "expected_level": expected_level,
                "actual_level": actual_level,
                "final_level": final_level,
                "risk_match": risk_match,
                "final_risk_match": final_risk_match,
                "candidate_non_downgrade": candidate_non_downgrade,
                "final_non_downgrade": final_non_downgrade,
                "candidate_compliant": candidate_compliant,
                "guardrail_valid": guardrail_valid,
                "trace_complete": trace_complete,
                "context_tokens": int(
                    (event.context_manifest or {}).get("estimated_tokens") or 0
                ),
                "context_truncated": bool(
                    (event.context_manifest or {}).get("truncated", False)
                ),
                "context_critical_retained": (
                    (event.context_manifest or {}).get("critical_evidence_retained") is True
                ),
                "repair_status": str(
                    (event.repair_trace or {}).get("status") or "missing"
                ),
                "repair_attempt_count": int(
                    (event.repair_trace or {}).get("attempt_count") or 0
                ),
                "llm_json_valid": is_valid,
                "llm_status": event.llm_status,
                "latency_ms": event.llm_latency_ms,
                "structured_diagnostics": {
                    "observed_facts": list(recommendation.get("observed_facts") or [])[:4],
                    "uncertainties": list(recommendation.get("uncertainties") or [])[:4],
                    "risk_reason": str(recommendation.get("risk_reason") or "")[:300],
                    "confidence": float(recommendation.get("confidence") or 0),
                    "visual_observations": list(
                        recommendation.get("visual_observations") or []
                    )[:4],
                    "detection_observations": list(
                        recommendation.get("detection_observations") or []
                    )[:4],
                    "evidence_assessment": dict(
                        recommendation.get("evidence_assessment") or {}
                    ),
                },
                "input_evidence": {
                    "image_present": bool(event.image_bytes),
                    "structured_event_count": len(event.events),
                    "declared_input_mode": str(event.raw_json.get("input_mode") or ""),
                },
                "rag_status": event.rag_status,
                "expected_citations": sorted(expected_citations),
                "actual_citations": sorted(actual_citations),
                "final_grounded_citations": sorted(final_citations),
                "candidate_evidence_ok": candidate_evidence_ok,
                "final_evidence_ok": final_evidence_ok,
                "grounding_status": str(grounding.get("status") or ""),
                "grounding_policy_version": str(
                    grounding.get("policy_version") or ""
                ),
                "rejected_citations": rejected,
                "sop_answerable": bool(recommendation.get("sop_answerable")),
                "sop_refusal_reason": recommendation.get("sop_refusal_reason", ""),
                "rejected_candidate_actions": recommendation.get("rejected_candidate_actions", []),
                "policy_rejected_actions": policy_rejected,
                "candidate_plan": validation.get("candidate_plan", []),
                "final_plan": validation.get("final_plan", []),
            }
            rows.append(row)
            completed_keys.add(result_key)
            if checkpoint_path:
                _write_checkpoint(
                    checkpoint_path, metadata, rows,
                    complete=len(rows) == total_executions,
                )
            print(
                f"[BENCH] {name}: {len(rows)}/{total_executions} "
                f"case={case['case_id']} result={'PASS' if passed else 'FAIL'} "
                f"latency={event.llm_latency_ms}ms",
                flush=True,
            )
            if event.llm_status in failed_statuses:
                consecutive_model_failures += 1
            else:
                consecutive_model_failures = 0
            if consecutive_model_failures >= max(1, int(max_consecutive_model_failures)):
                raise RuntimeError(
                    "benchmark_model_unhealthy:"
                    f"{consecutive_model_failures}_consecutive_failures;"
                    "checkpoint_saved"
                )

    total = len(rows)
    latencies = [float(row.get("latency_ms") or 0) for row in rows]
    relevant_citations = sum(
        len(set(row.get("actual_citations") or []).intersection(row.get("expected_citations") or []))
        for row in rows
    )
    final_relevant_citations = sum(
        len(set(row.get("final_grounded_citations") or []).intersection(
            row.get("expected_citations") or []
        ))
        for row in rows
    )
    final_citation_attempts = sum(
        len(row.get("final_grounded_citations") or []) for row in rows
    )
    citation_attempts = sum(
        len(row.get("actual_citations") or []) + len(row.get("rejected_citations") or [])
        for row in rows
    )
    rejected_citations = sum(len(row.get("rejected_citations") or []) for row in rows)
    answerable_rows = [row for row in rows if row.get("expected_citations")]
    citation_hits = sum(
        bool(set(row.get("actual_citations") or []).intersection(row.get("expected_citations") or []))
        for row in answerable_rows
    )
    final_citation_hits = sum(
        bool(set(row.get("final_grounded_citations") or []).intersection(
            row.get("expected_citations") or []
        ))
        for row in answerable_rows
    )
    refusal_rows = [row for row in rows if not row.get("expected_citations")]
    refusal_correct = sum(
        not row.get("sop_answerable")
        and not row.get("actual_citations")
        and bool(row.get("sop_refusal_reason"))
        for row in refusal_rows
    )
    final_refusal_correct = sum(
        not row.get("final_grounded_citations")
        and row.get("grounding_status") in {"no_evidence", "retrieval_error", "disabled"}
        for row in refusal_rows
    )
    answerable = len(answerable_rows)
    refusal_cases = len(refusal_rows)
    noncompliant = [row for row in rows if not row["candidate_compliant"]]
    corrected = [row for row in noncompliant if row["guardrail_valid"]]
    context_tokens = [row["context_tokens"] for row in rows]
    repair_attempts = sum(row["repair_attempt_count"] for row in rows)
    repair_successes = sum(row["repair_status"] == "repaired" for row in rows)
    conflict_rows = [row for row in rows if row["input_mode"] == "conflict"]
    conflict_uncertainty_acknowledged = sum(
        bool((row.get("structured_diagnostics") or {}).get("uncertainties"))
        for row in conflict_rows
    )
    explicitly_detected_conflicts = sum(
        (row.get("structured_diagnostics") or {}).get(
            "evidence_assessment", {}
        ).get("relation") == "conflict"
        for row in conflict_rows
    )
    non_conflict_rows = [row for row in rows if row["input_mode"] != "conflict"]
    false_conflict_flags = sum(
        (row.get("structured_diagnostics") or {}).get(
            "evidence_assessment", {}
        ).get("relation") == "conflict"
        for row in non_conflict_rows
    )
    metrics = {
        "cases": len(cases),
        "repeats": repeats,
        "executions": total,
        "passed": sum(bool(row["passed"]) for row in rows),
        "pre_grounding_passed": sum(
            bool(row["pre_grounding_passed"]) for row in rows
        ),
        "structured_output_valid_pct": _percent(sum(row["llm_json_valid"] for row in rows), total),
        "risk_level_accuracy_pct": _percent(sum(row["risk_match"] for row in rows), total),
        "final_risk_level_accuracy_pct": _percent(sum(row["final_risk_match"] for row in rows), total),
        "candidate_non_downgrade_pct": _percent(sum(row["candidate_non_downgrade"] for row in rows), total),
        "final_non_downgrade_pct": _percent(sum(row["final_non_downgrade"] for row in rows), total),
        "model_candidate_action_compliance_pct": _percent(sum(row["candidate_compliant"] for row in rows), total),
        "guardrail_final_plan_valid_pct": _percent(sum(row["guardrail_valid"] for row in rows), total),
        "guardrail_correction_pct": _percent(len(corrected), len(noncompliant)) if noncompliant else 100.0,
        "decision_trace_complete_pct": _percent(sum(row["trace_complete"] for row in rows), total),
        "structured_diagnostics_complete_pct": _percent(sum(
            isinstance((row.get("structured_diagnostics") or {}).get("observed_facts"), list)
            and isinstance((row.get("structured_diagnostics") or {}).get("uncertainties"), list)
            and bool((row.get("structured_diagnostics") or {}).get("risk_reason"))
            for row in rows
        ), total),
        "conflict_uncertainty_acknowledgement_pct": _percent(
            conflict_uncertainty_acknowledged, len(conflict_rows)
        ),
        "explicit_conflict_detection_pct": _percent(
            explicitly_detected_conflicts, len(conflict_rows)
        ),
        "false_conflict_flag_pct": _percent(
            false_conflict_flags, len(non_conflict_rows)
        ),
        "context_critical_retention_pct": _percent(
            sum(row["context_critical_retained"] for row in rows), total
        ),
        "context_truncation_pct": _percent(
            sum(row["context_truncated"] for row in rows), total
        ),
        "context_tokens_mean": round(statistics.mean(context_tokens), 1) if context_tokens else 0.0,
        "context_tokens_p50": _percentile(context_tokens, 0.5),
        "context_tokens_p95": _percentile(context_tokens, 0.95),
        "repair_attempts": repair_attempts,
        "repair_success_pct": _percent(repair_successes, repair_attempts),
        "repair_budget_violations": sum(
            row["repair_attempt_count"] > 1 for row in rows
        ),
        "three_run_consistency_pct": _consistency_pct(rows),
        "grounded_citation_coverage_pct": _percent(citation_hits, answerable) if retriever else 0.0,
        "final_grounded_citation_coverage_pct": (
            _percent(final_citation_hits, answerable) if retriever else 0.0
        ),
        "citation_precision_pct": _percent(relevant_citations, citation_attempts),
        "final_grounding_precision_pct": _percent(
            final_relevant_citations, final_citation_attempts
        ),
        "citation_guardrail_rejections": rejected_citations,
        "no_evidence_refusal_accuracy_pct": _percent(refusal_correct, refusal_cases),
        "final_no_evidence_refusal_accuracy_pct": _percent(
            final_refusal_correct, refusal_cases
        ),
        "latency_mean_ms": round(statistics.mean(latencies), 1) if latencies else 0.0,
        "latency_p50_ms": _percentile(latencies, 0.5),
        "latency_p95_ms": _percentile(latencies, 0.95),
    }
    by_category = {
        category: _slice_metrics([row for row in rows if row["category"] == category])
        for category in sorted({row["category"] for row in rows})
    }
    by_input_mode = {
        mode: _slice_metrics([row for row in rows if row["input_mode"] == mode])
        for mode in sorted({row["input_mode"] for row in rows})
    }
    by_evaluation_scope = {
        scope: _slice_metrics([
            row for row in rows if row["evaluation_scope"] == scope
        ])
        for scope in (RUNTIME_CONTRACT_SCOPE, VISION_EXPLORATORY_SCOPE)
    }
    return {
        "variant": name, "metrics": metrics,
        "by_category": by_category, "by_input_mode": by_input_mode,
        "by_evaluation_scope": by_evaluation_scope,
        "results": rows,
    }


def build_report(model: str, url: str, timeout: int, compare_no_rag: bool = True,
                 *, cases_path: Path = DEFAULT_CASES, repeats: int = 1,
                 ablation: bool = False, categories: set[str] | None = None,
                 case_limit: int = 0, context_token_budget: int = 1200,
                 checkpoint_base: Path | None = None, resume: bool = False,
                 retry_failed: bool = False,
                 max_consecutive_model_failures: int = 2) -> dict:
    context_token_budget = max(256, min(8192, int(context_token_budget)))
    source_cases = load_cases(cases_path)
    dataset = validate_case_set(source_cases, require_full_suite=cases_path == DEFAULT_CASES)
    cases = select_cases(
        source_cases, ablation=ablation, categories=categories, limit=case_limit
    )
    if not cases:
        raise ValueError("no_benchmark_cases_selected")
    retriever = SOPRetriever(DEFAULT_CATALOG)
    probe = SafetyAgent(
        mode="ollama", model=model, base_url=url, timeout_seconds=timeout,
        context_token_budget=context_token_budget,
    ).health()
    if probe.get("status") != "ready":
        return {
            "benchmark": "multimodal_agent", "status": "model_unavailable",
            "probe": probe, "dataset": dataset, "variants": [],
        }
    warmup_agent = SafetyAgent(
        mode="ollama", model=model, base_url=url, timeout_seconds=timeout,
        context_token_budget=context_token_budget,
    )
    warmup_event = _event(cases[0], 0)
    warmup_agent.analyze(warmup_event)
    if warmup_event.llm_status != "success" or not warmup_event.llm_json_valid:
        return {
            "benchmark": "multimodal_agent",
            "status": "model_warmup_failed",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "prompt_version": SafetyAgent.PROMPT_VERSION,
            "dataset": {**dataset, "selected_cases": len(cases), "repeats": repeats},
            "warmup": {
                "llm_status": warmup_event.llm_status,
                "llm_error": warmup_event.llm_error,
                "llm_json_valid": warmup_event.llm_json_valid,
                "latency_ms": warmup_event.llm_latency_ms,
            },
            "variants": [],
            "scope": "No scored case was started because model warmup did not produce valid JSON.",
        }

    def checkpoint_for(variant: str) -> Path | None:
        if checkpoint_base is None:
            return None
        base = checkpoint_base.resolve()
        return base.with_name(f"{base.stem}.{variant}.checkpoint.json")

    variants = []
    if compare_no_rag:
        variants.append(run_variant(
            "no_rag", cases, model, url, timeout, None, repeats=repeats,
            context_token_budget=context_token_budget,
            checkpoint_path=checkpoint_for("no_rag"), resume=resume,
            retry_failed=retry_failed,
            max_consecutive_model_failures=max_consecutive_model_failures,
        ))
    variants.append(run_variant(
        "grounded_sop_rag", cases, model, url, timeout, retriever, repeats=repeats,
        context_token_budget=context_token_budget,
        checkpoint_path=checkpoint_for("grounded_sop_rag"), resume=resume,
        retry_failed=retry_failed,
        max_consecutive_model_failures=max_consecutive_model_failures,
    ))
    return {
        "benchmark": "multimodal_agent", "status": "completed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "suite": "four_mode_ablation" if ablation else "full_hard_cases",
        "model": model, "prompt_version": SafetyAgent.PROMPT_VERSION,
        "context_token_budget": context_token_budget,
        "catalog_version": retriever.catalog_version,
        "warmup_latency_ms": warmup_event.llm_latency_ms,
        "checkpointing": {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "enabled": checkpoint_base is not None,
            "resumed": bool(resume),
            "retry_failed": bool(retry_failed),
            "stores_raw_model_output": False,
        },
        "input_mode": "generated replay images and/or structured detections",
        "dataset": {**dataset, "selected_cases": len(cases), "repeats": repeats},
        "variants": variants,
        "scope": (
            "Measures model structure, risk decisions, guarded plans, grounded citations, refusal, "
            "repeat consistency and decision-stage trace completeness. Generated replay images do "
            "not measure real-world detector accuracy. Runtime-contract and vision-dependent "
            "exploratory cases are reported separately without changing the aggregate verdict; "
            "full execution Trace is covered separately."
        ),
    }


def write_report(report: dict, report_json: Path = REPORT_JSON) -> tuple[Path, Path]:
    report_json = report_json.resolve()
    report_md = report_json.with_suffix(".md")
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Multimodal Agent Benchmark", "", f"Status: `{report['status']}`", ""]
    if report["status"] == "completed":
        lines.extend([
            f"Suite: `{report['suite']}`",
            f"Model: `{report['model']}`",
            f"Prompt: `{report['prompt_version']}`",
            f"SOP catalog: `{report['catalog_version']}`",
            f"Text context budget: `{report.get('context_token_budget', 1200)} estimated tokens`",
            f"Cases / repeats: `{report['dataset']['selected_cases']} / {report['dataset']['repeats']}`",
            "",
            "| Variant | Valid JSON | Model / final risk accuracy | Final non-downgrade | Candidate actions | Guardrail plan | Guardrail correction | Trace | Consistency | Citation coverage | Refusal | P50 / P95 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for variant in report["variants"]:
            m = variant["metrics"]
            consistency = "-" if m["three_run_consistency_pct"] is None else f"{m['three_run_consistency_pct']}%"
            lines.append(
                f"| {variant['variant']} | {m['structured_output_valid_pct']}% | "
                f"{m['risk_level_accuracy_pct']}% / {m['final_risk_level_accuracy_pct']}% | "
                f"{m['final_non_downgrade_pct']}% | "
                f"{m['model_candidate_action_compliance_pct']}% | {m['guardrail_final_plan_valid_pct']}% | "
                f"{m['guardrail_correction_pct']}% | {m['decision_trace_complete_pct']}% | "
                f"{consistency} | {m['grounded_citation_coverage_pct']}% | "
                f"{m['no_evidence_refusal_accuracy_pct']}% | "
                f"{m['latency_p50_ms']} / {m['latency_p95_ms']} ms |"
            )
        lines.extend([
            "", "| Variant | Critical context retained | Context truncated | Context tokens mean / P95 |",
            "|---|---:|---:|---:|",
        ])
        for variant in report["variants"]:
            m = variant["metrics"]
            lines.append(
                f"| {variant['variant']} | {m['context_critical_retention_pct']}% | "
                f"{m['context_truncation_pct']}% | "
                f"{m['context_tokens_mean']} / {m['context_tokens_p95']} |"
            )
        lines.extend([
            "", "| Variant | Repair attempts | Repair success | Budget violations |",
            "|---|---:|---:|---:|",
        ])
        for variant in report["variants"]:
            m = variant["metrics"]
            lines.append(
                f"| {variant['variant']} | {m['repair_attempts']} | "
                f"{m['repair_success_pct']}% | {m['repair_budget_violations']} |"
            )
        lines.extend([
            "", "| Variant | Final strict | Before grounding | Model / final citation coverage | Model / final citation precision | Model / final refusal | Conflict uncertainty | Explicit conflict / false flag |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for variant in report["variants"]:
            m = variant["metrics"]
            lines.append(
                f"| {variant['variant']} | {m['passed']}/{m['executions']} | "
                f"{m['pre_grounding_passed']}/{m['executions']} | "
                f"{m['grounded_citation_coverage_pct']}% / "
                f"{m['final_grounded_citation_coverage_pct']}% | "
                f"{m['citation_precision_pct']}% / "
                f"{m['final_grounding_precision_pct']}% | "
                f"{m['no_evidence_refusal_accuracy_pct']}% / "
                f"{m['final_no_evidence_refusal_accuracy_pct']}% | "
                f"{m['conflict_uncertainty_acknowledgement_pct']}% | "
                f"{m['explicit_conflict_detection_pct']}% / "
                f"{m['false_conflict_flag_pct']}% |"
            )
        for variant in report["variants"]:
            lines.extend([
                "", f"## {variant['variant']} by evaluation scope", "",
                "| Scope | Executions | Final / pre-grounding passed | Final / pre-grounding strict | Model / final risk | Final safety | Guardrail |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ])
            for scope, metrics in variant["by_evaluation_scope"].items():
                lines.append(
                    f"| {scope} | {metrics['executions']} | {metrics['passed']} / "
                    f"{metrics['pre_grounding_passed']} | {metrics['pass_rate_pct']}% / "
                    f"{metrics['pre_grounding_pass_rate_pct']}% | {metrics['risk_accuracy_pct']}% / "
                    f"{metrics['final_risk_accuracy_pct']}% | "
                    f"{metrics['final_non_downgrade_pct']}% | "
                    f"{metrics['guardrail_plan_valid_pct']}% |"
                )
            lines.append(
                f"\nConflict uncertainty acknowledgement: "
                f"`{variant['metrics']['conflict_uncertainty_acknowledgement_pct']}%`"
            )
            lines.extend([
                "", f"## {variant['variant']} by category", "",
                "| Category | Executions | Pass | Model / final risk | Final safety | Guardrail |",
                "|---|---:|---:|---:|---:|---:|",
            ])
            for category, metrics in variant["by_category"].items():
                lines.append(
                    f"| {category} | {metrics['executions']} | {metrics['pass_rate_pct']}% | "
                    f"{metrics['risk_accuracy_pct']}% / {metrics['final_risk_accuracy_pct']}% | "
                    f"{metrics['final_non_downgrade_pct']}% | "
                    f"{metrics['guardrail_plan_valid_pct']}% |"
                )
            lines.extend([
                "", f"## {variant['variant']} by input mode", "",
                "| Input mode | Executions | Pass | Model / final risk | Final safety | Guardrail |",
                "|---|---:|---:|---:|---:|---:|",
            ])
            for input_mode, metrics in variant["by_input_mode"].items():
                lines.append(
                    f"| {input_mode} | {metrics['executions']} | {metrics['pass_rate_pct']}% | "
                    f"{metrics['risk_accuracy_pct']}% / {metrics['final_risk_accuracy_pct']}% | "
                    f"{metrics['final_non_downgrade_pct']}% | "
                    f"{metrics['guardrail_plan_valid_pct']}% |"
                )
            lines.extend([
                "", "| Case | Mode | Scope | Round | Result | Candidate / final / expected | RAG | Model -> final citations | Latency |",
                "|---|---|---|---:|---|---|---|---|---:|",
            ])
            for row in variant["results"]:
                lines.append(
                    f"| {row['case_id']} | {row['input_mode']} | {row['evaluation_scope']} | "
                    f"{row['repetition']} | "
                    f"{'PASS' if row['passed'] else 'FAIL'} | "
                    f"{row['actual_level'] or '-'} / {row['final_level'] or '-'} / {row['expected_level']} | "
                    f"{row['rag_status']} | {', '.join(row['actual_citations']) or '-'} -> "
                    f"{', '.join(row['final_grounded_citations']) or '-'} | "
                    f"{row['latency_ms']} ms |"
                )
        lines.extend(["", report["scope"]])
    else:
        lines.append(json.dumps(
            report.get("probe") or report.get("warmup") or {}, ensure_ascii=False
        ))
    report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_json, report_md


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    settings = Settings.from_env()
    parser.add_argument("--model", default=settings.ollama_model)
    parser.add_argument("--url", default=settings.ollama_url)
    parser.add_argument("--timeout", type=int, default=max(30, settings.llm_timeout_seconds))
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=REPORT_JSON)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--category", action="append", choices=sorted(EXPECTED_CATEGORIES))
    parser.add_argument("--case-limit", type=int, default=0)
    parser.add_argument(
        "--context-token-budget", type=int, default=settings.context_token_budget,
        help="Text-context budget used by the governed context builder",
    )
    parser.add_argument("--ablation", action="store_true", help="Run four input modes for ablation seeds")
    parser.add_argument("--rag-only", action="store_true", help="Skip the no-RAG control variant")
    parser.add_argument("--validate-only", action="store_true", help="Validate the 40-case dataset without a model")
    parser.add_argument("--require-model", action="store_true")
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume only from a checkpoint whose model/prompt/context/dataset fingerprint matches",
    )
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="With --resume, rerun checkpoint rows whose model call failed or timed out",
    )
    parser.add_argument("--max-consecutive-model-failures", type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be >= 1")
    if args.max_consecutive_model_failures < 1:
        parser.error("--max-consecutive-model-failures must be >= 1")
    cases_path = args.cases.resolve()
    if args.validate_only:
        summary = validate_case_set(
            load_cases(cases_path), require_full_suite=cases_path == DEFAULT_CASES.resolve()
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    report = build_report(
        args.model, args.url, args.timeout, compare_no_rag=not args.rag_only,
        cases_path=cases_path, repeats=args.repeats, ablation=args.ablation,
        categories=set(args.category or []), case_limit=args.case_limit,
        context_token_budget=args.context_token_budget,
        checkpoint_base=args.output, resume=args.resume,
        retry_failed=args.retry_failed,
        max_consecutive_model_failures=args.max_consecutive_model_failures,
    )
    report_json, report_md = write_report(report, args.output)
    if report["status"] != "completed":
        print(
            "Multimodal benchmark unavailable: "
            f"{report.get('probe') or report.get('warmup') or report['status']}"
        )
        return 2 if args.require_model else 0
    for variant in report["variants"]:
        metrics = variant["metrics"]
        print(
            f"{variant['variant']}: {metrics['passed']}/{metrics['executions']} passed, "
            f"valid={metrics['structured_output_valid_pct']}%, "
            f"final_safety={metrics['final_non_downgrade_pct']}%, "
            f"p95={metrics['latency_p95_ms']}ms"
        )
    print(f"JSON: {report_json}")
    print(f"Markdown: {report_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
