"""Build and validate a complete, externally inspectable Agent Run trace."""
from __future__ import annotations

from copy import deepcopy
import json

from agents.evidence_replan import content_sha256
from .runtime_metrics import RUN_TIMING_SCHEMA_VERSION, build_run_timing


TRACE_SCHEMA_VERSION = "agent-trace-v4"
TRACE_FINAL_STATUSES = {
    "filtered", "succeeded", "waiting_approval", "manual_takeover",
    "permanent_failed", "cancelled",
}


def _tool_result(row: dict):
    raw = row.get("result_json")
    if raw in (None, ""):
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw


def build_trace(run: dict, tool_rows: list[dict], transitions: list[dict]) -> dict:
    event = deepcopy(run.get("event") or {})
    retrieval = deepcopy(event.get("sop_retrieval") or {})
    recommendation = deepcopy(event.get("llm_recommendation") or {})
    decision = deepcopy(event.get("dispatch_decision") or {})
    validation = deepcopy(decision.get("plan_validation") or {})
    context = deepcopy(event.get("context_manifest") or {})
    evidence_replan = deepcopy(event.get("evidence_replan") or {})
    repair = deepcopy(event.get("repair_trace") or {})
    failure_attributions = deepcopy(event.get("failure_attributions") or [])
    citations = deepcopy(retrieval.get("citations") or [])
    selected_citations = deepcopy(recommendation.get("sop_citations") or [])
    memory_escalations = []
    for index, detection in enumerate(event.get("events") or []):
        escalation = (
            detection.get("memory_escalation") or {}
            if isinstance(detection, dict) else {}
        )
        if not isinstance(escalation, dict) or not escalation:
            continue
        memory_escalations.append({
            "event_index": index,
            "event_type": str(detection.get("type") or ""),
            "base_level": str(detection.get("base_level") or ""),
            "final_rule_level": str(detection.get("level") or ""),
            "policy_version": str(escalation.get("policy_version") or ""),
            "camera_id": str(escalation.get("camera_id") or ""),
            "event_family": str(escalation.get("event_family") or ""),
            "zone": str(escalation.get("zone") or ""),
            "history_count": int(escalation.get("history_count") or 0),
            "escalation_threshold": int(
                escalation.get("escalation_threshold") or 0
            ),
            "trigger_event_ids": [
                str(value) for value in escalation.get("trigger_event_ids") or []
                if str(value)
            ],
        })
    trace = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "ingress": {
            "source": str(run.get("source") or ""),
            "source_event_id": str(run.get("source_event_id") or event.get("source_event_id") or ""),
            "camera_id": str(run.get("camera_id") or event.get("camera_id") or ""),
            "ingest_key": str(run.get("ingest_key") or event.get("ingest_key") or ""),
            "payload_sha256": str(
                run.get("ingest_payload_hash") or event.get("ingest_payload_hash") or ""
            ),
        },
        "run": {
            "event_id": str(run.get("event_id") or ""),
            "run_id": str(run.get("run_id") or ""),
            "trace_id": str(run.get("trace_id") or ""),
            "status": str(run.get("status") or ""),
            "stage": str(run.get("stage") or ""),
            "execution_attempt": int(run.get("execution_attempt") or 0),
        },
        "evidence": {
            "evidence_id": str(event.get("evidence_id") or ""),
            "image_url": str(event.get("image_url") or ""),
            "event_count": len(event.get("events") or []),
        },
        "model": {
            "model": str(event.get("llm_model") or ""),
            "status": str(event.get("llm_status") or ""),
            "prompt_version": str(event.get("prompt_version") or ""),
            "catalog_version": str(retrieval.get("catalog_version") or ""),
            "rag_status": str(event.get("rag_status") or ""),
            "retrieved_citations": citations,
            "selected_citations": selected_citations,
        },
        "context": context,
        "memory": {
            "schema_version": "trace-memory-escalation-v1",
            "escalation_count": len(memory_escalations),
            "escalations": memory_escalations,
        },
        "evidence_replan": evidence_replan,
        "reliability": {
            "repair": repair,
            "failure_attributions": failure_attributions,
        },
        "decision": {
            "candidate_plan": deepcopy(validation.get("candidate_plan")),
            "rule_level": str(decision.get("rule_level") or ""),
            "llm_level": str(decision.get("llm_level") or ""),
            "final_level": str(decision.get("final_level") or ""),
            "evidence_policy": deepcopy(decision.get("evidence_policy") or {}),
            "grounding": deepcopy(decision.get("grounding") or {}),
            "guardrail_decision": {
                "accepted": deepcopy(validation.get("accepted") or []),
                "forced": deepcopy(validation.get("forced") or []),
                "rejected": deepcopy(validation.get("rejected") or []),
                "final_plan": deepcopy(validation.get("final_plan") or []),
                "baseline_preserved": validation.get("baseline_preserved"),
            },
        },
        "tool_executions": [
            {
                "run_id": str(row.get("run_id") or ""),
                "event_id": str(row.get("event_id") or ""),
                "step_id": str(row.get("step_id") or ""),
                "execution_id": str(row.get("execution_id") or ""),
                "idempotency_key": str(row.get("idempotency_key") or ""),
                "policy_version": str(row.get("policy_version") or ""),
                "tool": str(row.get("tool") or ""),
                "action": str(row.get("action") or ""),
                "status": str(row.get("status") or ""),
                "attempts": int(row.get("attempts") or 0),
                "tool_result": _tool_result(row),
                "error_type": str(row.get("error_type") or ""),
                "error_message": str(row.get("error_message") or ""),
            }
            for row in tool_rows
        ],
        "timing": build_run_timing(run, transitions),
        "transitions": [
            {
                "from_status": row.get("from_status"),
                "to_status": str(row.get("to_status") or ""),
                "stage": str(row.get("stage") or ""),
                "detail": str(row.get("detail") or ""),
                "created_at": str(row.get("created_at") or ""),
            }
            for row in transitions
        ],
        "outcome": {
            "final_status": str(run.get("status") or ""),
            "error_type": str(run.get("last_error_type") or ""),
            "error_message": str(run.get("last_error_message") or ""),
        },
        "approval": {
            "approval_id": str(event.get("approval_id") or ""),
            "status": str(event.get("approval_status") or "auto"),
        },
        "actuation": {
            "execution_id": str(event.get("execution_id") or ""),
            "status": str(event.get("execution_status") or ""),
            "result": str(event.get("execution_result") or ""),
            "actions": deepcopy(event.get("execution_actions") or []),
        },
        "snapshot_ids": {
            "event_id": str(event.get("event_id") or ""),
            "run_id": str(event.get("run_id") or ""),
            "trace_id": str(event.get("trace_id") or ""),
        },
    }
    trace["validation"] = validate_trace(trace)
    return trace


def validate_trace(trace: dict) -> dict:
    errors: list[str] = []

    def require(value, code: str) -> None:
        if value is None or value == "" or value == []:
            errors.append(code)

    run = trace.get("run") or {}
    ingress = trace.get("ingress") or {}
    evidence = trace.get("evidence") or {}
    snapshot_ids = trace.get("snapshot_ids") or {}
    status = str(run.get("status") or "")
    timing = trace.get("timing") or {}
    require(ingress.get("source"), "missing_source")
    require(ingress.get("source_event_id"), "missing_source_event_id")
    require(ingress.get("payload_sha256"), "missing_payload_hash")
    require(run.get("event_id"), "missing_event_id")
    require(run.get("run_id"), "missing_run_id")
    require(run.get("trace_id"), "missing_trace_id")
    require(evidence.get("evidence_id"), "missing_evidence_id")
    for field in ("event_id", "run_id", "trace_id"):
        if run.get(field) != snapshot_ids.get(field):
            errors.append(f"snapshot_{field}_mismatch")
    if status not in TRACE_FINAL_STATUSES:
        errors.append("run_not_final")

    memory = trace.get("memory") or {}
    memory_escalations = memory.get("escalations") or []
    if not isinstance(memory_escalations, list):
        errors.append("invalid_memory_escalations")
        memory_escalations = []
    if int(memory.get("escalation_count") or 0) != len(memory_escalations):
        errors.append("memory_escalation_count_mismatch")
    seen_memory_scopes = set()
    for escalation in memory_escalations:
        for field in (
            "event_type", "base_level", "final_rule_level", "policy_version",
            "camera_id", "event_family", "zone", "history_count",
            "escalation_threshold", "trigger_event_ids",
        ):
            require(escalation.get(field), f"memory_escalation_missing_{field}")
        if escalation.get("base_level") != "B":
            errors.append("memory_escalation_invalid_base_level")
        if escalation.get("final_rule_level") != "A":
            errors.append("memory_escalation_invalid_final_level")
        if escalation.get("camera_id") != ingress.get("camera_id"):
            errors.append("memory_escalation_camera_mismatch")
        history_count = int(escalation.get("history_count") or 0)
        threshold = int(escalation.get("escalation_threshold") or 0)
        if threshold < 1 or history_count < threshold:
            errors.append("memory_escalation_below_threshold")
        trigger_ids = list(escalation.get("trigger_event_ids") or [])
        if len(trigger_ids) != len(set(trigger_ids)):
            errors.append("memory_escalation_duplicate_trigger_event")
        scope = (
            escalation.get("event_index"), escalation.get("camera_id"),
            escalation.get("event_family"), escalation.get("zone"),
        )
        if scope in seen_memory_scopes:
            errors.append("duplicate_memory_escalation_scope")
        seen_memory_scopes.add(scope)

    require(timing.get("schema_version"), "missing_timing_schema_version")
    if timing.get("schema_version") != RUN_TIMING_SCHEMA_VERSION:
        errors.append("invalid_timing_schema_version")
    require(timing.get("end_to_end_ms"), "missing_end_to_end_timing")
    for field in (
        "end_to_end_ms", "ingest_to_decision_ms", "decision_to_execution_ms",
        "execution_to_outcome_ms", "approval_wait_ms",
    ):
        value = timing.get(field)
        if value is not None and (not isinstance(value, (int, float)) or value < 0):
            errors.append(f"invalid_timing_{field}")

    transitions = trace.get("transitions") or []
    if not transitions:
        errors.append("missing_transitions")
    elif transitions[-1].get("to_status") != status:
        errors.append("final_transition_mismatch")
    if int(timing.get("transition_count") or 0) != len(transitions):
        errors.append("timing_transition_count_mismatch")

    if status != "filtered":
        model = trace.get("model") or {}
        context = trace.get("context") or {}
        reliability = trace.get("reliability") or {}
        repair = reliability.get("repair") or {}
        failure_attributions = reliability.get("failure_attributions") or []
        decision = trace.get("decision") or {}
        grounding = decision.get("grounding") or {}
        guardrail = decision.get("guardrail_decision") or {}
        evidence_policy = decision.get("evidence_policy") or {}
        evidence_replan = trace.get("evidence_replan") or {}
        require(model.get("prompt_version"), "missing_prompt_version")
        require(model.get("catalog_version"), "missing_catalog_version")
        require(context.get("schema_version"), "missing_context_schema_version")
        require(context.get("builder_version"), "missing_context_builder_version")
        require(context.get("context_sha256"), "missing_context_hash")
        require(context.get("model_input_sha256"), "missing_model_input_hash")
        require(repair.get("schema_version"), "missing_repair_schema_version")
        require(repair.get("policy_version"), "missing_repair_policy_version")
        require(repair.get("status"), "missing_repair_status")
        attempts = repair.get("attempts")
        if not isinstance(attempts, list):
            errors.append("missing_repair_attempts")
            attempts = []
        if int(repair.get("max_attempts") or 0) != 1:
            errors.append("invalid_repair_attempt_budget")
        if int(repair.get("attempt_count") or 0) != len(attempts):
            errors.append("repair_attempt_count_mismatch")
        if len(attempts) > 1:
            errors.append("repair_attempt_budget_exceeded")
        for index, attempt in enumerate(attempts, 1):
            if int(attempt.get("attempt") or 0) != index:
                errors.append("invalid_repair_attempt_sequence")
            for field in (
                "prompt_version", "trigger_code", "input_sha256",
                "original_output_sha256", "status",
            ):
                require(attempt.get(field), f"repair_attempt_missing_{field}")
        repair_status = str(repair.get("status") or "")
        if repair_status in {"not_needed", "not_allowed"} and attempts:
            errors.append("unexpected_repair_attempt")
        if repair_status == "repaired" and (
            len(attempts) != 1 or attempts[0].get("status") != "succeeded"
        ):
            errors.append("invalid_successful_repair_trace")
        if repair_status not in {"not_needed", "not_allowed", "repaired", "exhausted"}:
            errors.append("invalid_repair_status")
        if not isinstance(failure_attributions, list):
            errors.append("invalid_failure_attributions")
            failure_attributions = []
        for attribution in failure_attributions:
            for field in (
                "schema_version", "attribution_id", "stage", "code",
                "resolution", "status",
            ):
                require(attribution.get(field), f"failure_attribution_missing_{field}")
        context_status = str(context.get("status") or "")
        if context_status == "built":
            if context.get("critical_evidence_retained") is not True:
                errors.append("context_dropped_critical_evidence")
            if not isinstance(context.get("selected_items"), list):
                errors.append("missing_context_selected_items")
            if not isinstance(context.get("dropped_items"), list):
                errors.append("missing_context_dropped_items")
            if int(context.get("selected_item_count") or 0) != len(
                context.get("selected_items") or []
            ):
                errors.append("context_selected_count_mismatch")
            if int(context.get("dropped_item_count") or 0) != len(
                context.get("dropped_items") or []
            ):
                errors.append("context_dropped_count_mismatch")
        elif context_status == "skipped":
            if str(model.get("status") or "") not in {"overloaded", "timeout"}:
                errors.append("context_skipped_without_runtime_fallback")
            require(context.get("skip_reason"), "missing_context_skip_reason")
        else:
            errors.append("invalid_context_status")
        if not isinstance(decision.get("candidate_plan"), list):
            errors.append("missing_candidate_plan")
        require(decision.get("final_level"), "missing_final_level")
        require(grounding.get("policy_version"), "missing_grounding_policy_version")
        require(grounding.get("status"), "missing_grounding_status")
        require(guardrail.get("final_plan"), "missing_final_plan")
        if guardrail.get("baseline_preserved") is not True:
            errors.append("guardrail_baseline_not_preserved")
        if evidence_policy:
            require(evidence_policy.get("schema_version"), "evidence_policy_missing_schema_version")
            require(evidence_policy.get("policy_version"), "evidence_policy_missing_policy_version")
            relation = str(evidence_policy.get("relation") or "")
            if relation not in {
                "consistent", "conflict", "image_only", "detections_only", "insufficient",
            }:
                errors.append("evidence_policy_invalid_relation")
            if relation == "conflict":
                if not evidence_policy.get("conflicts"):
                    errors.append("evidence_conflict_missing_details")
                if evidence_policy.get("review_required") is not True:
                    errors.append("evidence_conflict_review_not_required")
                if evidence_policy.get("autonomy_allowed") is not False:
                    errors.append("evidence_conflict_autonomy_not_blocked")

        if evidence_replan:
            if evidence_replan.get("schema_version") != "bounded-evidence-replan-v1":
                errors.append("invalid_evidence_replan_schema")
            if evidence_replan.get("policy_version") != "readonly-evidence-tool-policy-v1":
                errors.append("invalid_evidence_replan_policy")
            rounds = evidence_replan.get("decision_rounds") or []
            actions = evidence_replan.get("evidence_actions") or []
            if len(rounds) > 2:
                errors.append("evidence_replan_round_budget_exceeded")
            if len(actions) > 1:
                errors.append("evidence_action_budget_exceeded")
            for index, model_round in enumerate(rounds, 1):
                if int(model_round.get("round") or 0) != index:
                    errors.append("invalid_evidence_replan_round_sequence")
                for field in (
                    "context_sha256", "model_input_sha256", "output_sha256",
                ):
                    require(
                        model_round.get(field),
                        f"evidence_replan_round_missing_{field}",
                    )
            for action in actions:
                if action.get("tool") != "vision.inspect_adjacent_frames":
                    errors.append("effectful_or_unknown_evidence_tool")
                require(action.get("request_sha256"), "evidence_action_missing_request_hash")
                require(action.get("receipt_sha256"), "evidence_action_missing_receipt_hash")
                expected_identity = {
                    "event_id": run.get("event_id"),
                    "run_id": run.get("run_id"),
                    "trace_id": run.get("trace_id"),
                    "evidence_id": evidence.get("evidence_id"),
                    "source": ingress.get("source"),
                    "camera_id": ingress.get("camera_id"),
                }
                for field, expected in expected_identity.items():
                    if str(action.get(field) or "") != str(expected or ""):
                        errors.append(f"evidence_action_{field}_mismatch")
                if action.get("status") == "succeeded" and action.get("source") != "local_yolo":
                    errors.append("evidence_action_untrusted_source")
                request_payload = {
                    key: action.get(key) for key in (
                        "tool", "policy_version", "event_id", "run_id",
                        "trace_id", "evidence_id", "source", "camera_id",
                        "anchor_frame_id", "stream_session_id", "requested_limit",
                    )
                }
                if content_sha256(request_payload) != action.get("request_sha256"):
                    errors.append("evidence_action_request_hash_mismatch")
                receipt_payload = {
                    key: value for key, value in action.items()
                    if key not in {"latency_ms", "receipt_sha256"}
                }
                if content_sha256(receipt_payload) != action.get("receipt_sha256"):
                    errors.append("evidence_action_receipt_hash_mismatch")
                frames = action.get("frames") or []
                if int(action.get("frame_count") or 0) != len(frames):
                    errors.append("evidence_action_frame_count_mismatch")
                if action.get("status") == "succeeded":
                    require(
                        action.get("stream_session_id"),
                        "evidence_action_missing_stream_session",
                    )
                for frame in frames:
                    require(frame.get("image_sha256"), "evidence_frame_missing_hash")
                    if frame.get("stream_session_id") != action.get("stream_session_id"):
                        errors.append("evidence_frame_stream_session_mismatch")
                if action.get("status") == "succeeded" and not action.get("frames"):
                    errors.append("evidence_action_success_without_frames")
            replan_status = str(evidence_replan.get("status") or "")
            if replan_status == "resolved" and (len(rounds) != 2 or len(actions) != 1):
                errors.append("resolved_replan_missing_round_or_action")
            if replan_status == "resolved" and actions and actions[0].get("status") != "succeeded":
                errors.append("resolved_replan_without_successful_evidence")
            if len(rounds) == 2 and len(actions) == 1 and actions[0].get("status") == "succeeded":
                if rounds[1].get("context_sha256") != context.get("context_sha256"):
                    errors.append("replan_final_context_hash_mismatch")
                if rounds[1].get("model_input_sha256") != context.get("model_input_sha256"):
                    errors.append("replan_final_model_input_hash_mismatch")
                receipt_hashes = [
                    str(item.get("image_sha256") or "")
                    for item in actions[0].get("frames") or []
                ]
                model_hashes = [
                    str(item.get("original_sha256") or "")
                    for item in ((context.get("image") or {}).get("supplemental") or [])
                ]
                if not model_hashes or receipt_hashes != model_hashes:
                    errors.append("evidence_receipt_model_input_mismatch")
            if evidence_replan.get("manual_review_required") is True:
                if evidence_policy.get("review_required") is not True:
                    errors.append("evidence_replan_review_not_enforced")
                if evidence_policy.get("autonomy_allowed") is not False:
                    errors.append("evidence_replan_autonomy_not_blocked")
                if not evidence_replan.get("review_reason"):
                    errors.append("evidence_replan_review_reason_missing")
                if status == "succeeded":
                    errors.append("unresolved_evidence_run_succeeded")
            if replan_status == "manual_review" and evidence_replan.get(
                "manual_review_required"
            ) is not True:
                errors.append("manual_review_status_without_hold")
            if replan_status == "reviewed":
                resolution = evidence_replan.get("review_resolution") or {}
                review_approval = trace.get("approval") or {}
                review_actuation = trace.get("actuation") or {}
                if evidence_replan.get("manual_review_required") is not False:
                    errors.append("reviewed_evidence_still_requires_review")
                if resolution.get("decision") != "approved":
                    errors.append("reviewed_evidence_missing_resolution")
                require(
                    resolution.get("approval_id"),
                    "reviewed_evidence_missing_approval_id",
                )
                if resolution.get("approval_id") != review_approval.get("approval_id"):
                    errors.append("reviewed_evidence_approval_id_mismatch")
                if review_approval.get("status") != "approved":
                    errors.append("reviewed_evidence_approval_not_approved")
                if review_actuation.get("status") != "reviewed":
                    errors.append("reviewed_evidence_actuation_status_mismatch")
                require(
                    review_actuation.get("execution_id"),
                    "reviewed_evidence_missing_execution_id",
                )
                if review_actuation.get("actions"):
                    errors.append("reviewed_evidence_created_actuator_actions")

        retrieved = model.get("retrieved_citations") or []
        retrieved_ids = set()
        for citation in retrieved:
            citation_id = str(citation.get("citation_id") or "")
            if not citation_id or not citation.get("version") or not citation.get("source"):
                errors.append("citation_missing_provenance")
            retrieved_ids.add(citation_id)
        for citation in model.get("selected_citations") or []:
            if str(citation.get("citation_id") or "") not in retrieved_ids:
                errors.append("selected_citation_not_retrieved")
            if str(citation.get("citation_id") or "") not in set(
                context.get("selected_citation_ids") or []
            ):
                errors.append("selected_citation_not_in_model_context")

        grounded_citations = grounding.get("citations") or []
        if grounding.get("status") == "grounded" and not grounded_citations:
            errors.append("grounded_status_without_citations")
        if grounding.get("status") != "grounded" and grounded_citations:
            errors.append("grounding_citations_without_grounded_status")
        grounded_ids = []
        for citation in grounded_citations:
            citation_id = str(citation.get("citation_id") or "")
            grounded_ids.append(citation_id)
            if citation_id not in retrieved_ids:
                errors.append("grounded_citation_not_retrieved")
            if citation_id not in set(context.get("selected_citation_ids") or []):
                errors.append("grounded_citation_not_in_context")
            if citation.get("binding") != "structured_event_exact":
                errors.append("invalid_grounding_binding")
            if not citation.get("matched_event_types"):
                errors.append("grounded_citation_missing_event_match")
        if list(grounding.get("citation_ids") or []) != grounded_ids:
            errors.append("grounding_citation_ids_mismatch")

        tool_rows = trace.get("tool_executions") or []
        seen_actions: set[str] = set()
        seen_steps: set[str] = set()
        seen_executions: set[str] = set()
        seen_keys: set[str] = set()
        for row in tool_rows:
            for field in ("step_id", "execution_id", "idempotency_key", "tool", "action", "status"):
                require(row.get(field), f"tool_missing_{field}")
            if row.get("run_id") != run.get("run_id"):
                errors.append("tool_run_id_mismatch")
            if row.get("event_id") != run.get("event_id"):
                errors.append("tool_event_id_mismatch")
            action_name = f"{row.get('tool')}.{row.get('action')}"
            seen_actions.add(action_name)
            for value, seen, code in (
                (row.get("step_id"), seen_steps, "duplicate_step_id"),
                (row.get("execution_id"), seen_executions, "duplicate_execution_id"),
                (row.get("idempotency_key"), seen_keys, "duplicate_idempotency_key"),
            ):
                if value in seen:
                    errors.append(code)
                seen.add(value)
        expected_actions = set(guardrail.get("final_plan") or [])
        if not expected_actions.issubset(seen_actions):
            errors.append("final_plan_missing_tool_execution")
        if status in {"succeeded", "waiting_approval"}:
            statuses = {
                f"{row.get('tool')}.{row.get('action')}": row.get("status")
                for row in tool_rows
            }
            if any(statuses.get(name) != "succeeded" for name in expected_actions):
                errors.append("successful_run_has_unconfirmed_tool")

        approval = trace.get("approval") or {}
        actuation = trace.get("actuation") or {}
        if approval.get("approval_id"):
            if status == "waiting_approval" and approval.get("status") != "pending":
                errors.append("waiting_run_has_invalid_approval_status")
            if status in {"succeeded", "cancelled"}:
                expected_approval = "approved" if status == "succeeded" else "rejected"
                if approval.get("status") != expected_approval:
                    errors.append("final_approval_status_mismatch")
                require(actuation.get("execution_id"), "missing_actuator_execution_id")
                require(actuation.get("status"), "missing_actuator_status")

    unique_errors = list(dict.fromkeys(errors))
    return {
        "valid": not unique_errors,
        "error_count": len(unique_errors),
        "errors": unique_errors,
    }


class RunTraceService:
    def __init__(self, run_store, tool_execution_store):
        self.run_store = run_store
        self.tool_execution_store = tool_execution_store

    def get(self, run_id: str) -> dict | None:
        run = self.run_store.get(run_id)
        if run is None:
            return None
        return build_trace(
            run,
            self.tool_execution_store.list_for_run(run_id),
            self.run_store.transitions(run_id),
        )
