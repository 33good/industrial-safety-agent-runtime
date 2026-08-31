"""Deterministic failure attribution and bounded-repair policy."""
from __future__ import annotations

import hashlib
import json
from typing import Any


FAILURE_SCHEMA_VERSION = "agent-failure-v1"
REPAIR_SCHEMA_VERSION = "agent-repair-v1"
REPAIR_POLICY_VERSION = "bounded-repair-v1"
MAX_MODEL_REPAIR_ATTEMPTS = 1


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def content_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def new_repair_trace(status: str = "not_needed", reason: str = "") -> dict:
    return {
        "schema_version": REPAIR_SCHEMA_VERSION,
        "policy_version": REPAIR_POLICY_VERSION,
        "max_attempts": MAX_MODEL_REPAIR_ATTEMPTS,
        "attempt_count": 0,
        "status": status,
        "reason": str(reason or "")[:160],
        "attempts": [],
    }


class FailureAttributor:
    """Map failures to stable codes without exposing chain-of-thought."""

    _TRANSIENT_TOOL_ERRORS = {
        "timeout_or_network", "connection_error", "database_operational_error",
    }

    @staticmethod
    def _record(stage: str, code: str, *, repairable: bool, resolution: str,
                status: str = "observed", detail: str = "", evidence: Any = None) -> dict:
        identity = {
            "stage": stage,
            "code": code,
            "detail": str(detail or "")[:180],
            "evidence_sha256": content_sha256(evidence) if evidence is not None else "",
        }
        return {
            "schema_version": FAILURE_SCHEMA_VERSION,
            "attribution_id": "FAIL_" + content_sha256(identity)[:20],
            **identity,
            "repairable": bool(repairable),
            "resolution": resolution,
            "status": status,
        }

    def model_output(self, raw_output: str, recommendation: dict) -> dict | None:
        if recommendation.get("risk_level") in {"A", "B", "C"}:
            return None
        raw = str(raw_output or "")
        code = "model_empty_output" if not raw.strip() else "model_schema_invalid"
        return self._record(
            "model_output", code,
            repairable=True,
            resolution="pending_schema_repair",
            detail="model output did not contain a valid risk_level schema",
            evidence=raw,
        )

    def runtime_model_failure(self, status: str, error: str = "") -> dict:
        normalized = str(status or "failed")
        code = {
            "timeout": "model_timeout",
            "overloaded": "model_capacity_exhausted",
            "failed": "model_call_failed",
        }.get(normalized, "model_runtime_failure")
        return self._record(
            "model_runtime", code,
            repairable=False,
            resolution="deterministic_rule_fallback",
            status="contained",
            detail=str(error or normalized),
        )

    def policy_findings(self, decision: dict) -> list[dict]:
        records = []
        decision = decision or {}
        validation = decision.get("plan_validation") or {}
        rejected = list(validation.get("rejected") or [])
        if rejected:
            names = sorted({
                str(item.get("name") or "") for item in rejected if item.get("name")
            })
            records.append(self._record(
                "guardrail", "candidate_action_policy_rejected",
                repairable=False,
                resolution="guardrail_replaced_with_deterministic_plan",
                status="contained",
                detail=",".join(names)[:180],
                evidence=names,
            ))

        weights = {"C": 1, "B": 2, "A": 3}
        rule_level = str(decision.get("rule_level") or "")
        llm_level = str(decision.get("llm_level") or "")
        final_level = str(decision.get("final_level") or "")
        if (
            rule_level in weights and llm_level in weights
            and weights[llm_level] < weights[rule_level]
            and final_level == rule_level
        ):
            records.append(self._record(
                "guardrail", "candidate_risk_downgrade_rejected",
                repairable=False,
                resolution="deterministic_risk_baseline_preserved",
                status="contained",
                detail=f"{llm_level}->{rule_level}",
                evidence={"rule_level": rule_level, "llm_level": llm_level},
            ))
        evidence_policy = decision.get("evidence_policy") or {}
        if (
            evidence_policy.get("relation") == "conflict"
            and evidence_policy.get("review_required") is True
        ):
            records.append(self._record(
                "multimodal_evidence", "cross_modal_conflict_detected",
                repairable=False,
                resolution="operator_review_required",
                status="contained",
                detail="visual and structured detector evidence disagree",
                evidence=evidence_policy.get("conflicts") or [],
            ))
        return records

    def tool_failures(self, actions: list[dict]) -> list[dict]:
        records = []
        for item in actions or []:
            status = str(item.get("status") or "")
            if status not in {"failed", "indeterminate"}:
                continue
            error_type = str(item.get("error_type") or status)
            name = f"{item.get('tool', '')}.{item.get('action', '')}".strip(".")
            if status == "indeterminate" or error_type == "previous_execution_indeterminate":
                code = "tool_side_effect_indeterminate"
            elif error_type in self._TRANSIENT_TOOL_ERRORS:
                code = "tool_transient_retries_exhausted"
            else:
                code = "tool_permanent_failure"
            records.append(self._record(
                "tool_execution", code,
                repairable=False,
                resolution="manual_takeover",
                status="unresolved",
                detail=f"{name}:{error_type}",
                evidence={
                    "execution_id": item.get("execution_id", ""),
                    "idempotency_key": item.get("idempotency_key", ""),
                    "status": status,
                    "error_type": error_type,
                },
            ))
        return records


def append_unique_attributions(event, records: list[dict]) -> None:
    existing = {
        str(item.get("attribution_id") or "")
        for item in getattr(event, "failure_attributions", []) or []
    }
    for record in records:
        identity = str(record.get("attribution_id") or "")
        if identity and identity not in existing:
            event.failure_attributions.append(record)
            existing.add(identity)
