"""Read-only, durable runtime metrics derived from Agent execution facts."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import math
from typing import Iterable


RUNTIME_METRICS_SCHEMA_VERSION = "agent-runtime-metrics-v1"
RUN_TIMING_SCHEMA_VERSION = "agent-run-timing-v1"
OUTCOME_STATUSES = {
    "filtered", "succeeded", "waiting_approval", "manual_takeover",
    "permanent_failed", "cancelled",
}
ACTIVE_STATUSES = {"analyzing", "decided", "executing", "retryable_failed"}


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _elapsed_ms(start: object, end: object) -> float | None:
    start_at = _parse_timestamp(start)
    end_at = _parse_timestamp(end)
    if start_at is None or end_at is None:
        return None
    # SQLite records produced by older versions are naive local timestamps. Do not
    # mix those with timezone-aware values in the same duration calculation.
    if (start_at.tzinfo is None) != (end_at.tzinfo is None):
        return None
    elapsed = (end_at - start_at).total_seconds() * 1000
    return round(elapsed, 3) if elapsed >= 0 else None


def _first_transition(transitions: list[dict], statuses: set[str], *, after: int = -1):
    for index, row in enumerate(transitions):
        if index > after and str(row.get("to_status") or "") in statuses:
            return index, row.get("created_at")
    return -1, None


def build_run_timing(run: dict, transitions: list[dict]) -> dict:
    """Build honest stage timings only where both durable boundaries exist."""
    created_at = run.get("created_at")
    status = str(run.get("status") or "")
    decided_index, decided_at = _first_transition(transitions, {"decided"})
    executing_index, executing_at = _first_transition(
        transitions, {"executing"}, after=decided_index
    )
    outcome_index, outcome_at = _first_transition(
        transitions, OUTCOME_STATUSES, after=executing_index
    )
    waiting_index, waiting_at = _first_transition(transitions, {"waiting_approval"})
    _, approval_end_at = _first_transition(
        transitions,
        {"succeeded", "manual_takeover", "permanent_failed", "cancelled"},
        after=waiting_index,
    ) if waiting_index >= 0 else (-1, None)

    final_at = None
    if status in OUTCOME_STATUSES:
        for row in reversed(transitions):
            if str(row.get("to_status") or "") == status:
                final_at = row.get("created_at")
                break

    return {
        "schema_version": RUN_TIMING_SCHEMA_VERSION,
        "transition_count": len(transitions),
        "end_to_end_ms": _elapsed_ms(created_at, final_at),
        "ingest_to_decision_ms": _elapsed_ms(created_at, decided_at),
        "decision_to_execution_ms": _elapsed_ms(decided_at, executing_at),
        "execution_to_outcome_ms": _elapsed_ms(executing_at, outcome_at),
        "approval_wait_ms": _elapsed_ms(waiting_at, approval_end_at),
    }


def summarize_distribution(values: Iterable[float | int | None]) -> dict:
    samples = sorted(float(value) for value in values if value is not None)
    if not samples:
        return {
            "count": 0, "min": None, "mean": None,
            "p50": None, "p95": None, "max": None,
        }

    def percentile(fraction: float) -> float:
        position = (len(samples) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return samples[lower]
        weight = position - lower
        return samples[lower] * (1 - weight) + samples[upper] * weight

    return {
        "count": len(samples),
        "min": round(samples[0], 3),
        "mean": round(sum(samples) / len(samples), 3),
        "p50": round(percentile(0.50), 3),
        "p95": round(percentile(0.95), 3),
        "max": round(samples[-1], 3),
    }


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator * 100 / denominator, 2) if denominator else 0.0


class RuntimeMetricsService:
    """Aggregate a bounded recent-run window without mutating the Agent path."""

    def __init__(self, run_store, tool_store, analysis_limiter=None):
        self.run_store = run_store
        self.tool_store = tool_store
        self.analysis_limiter = analysis_limiter

    def snapshot(self, limit: int = 500) -> dict:
        # Keep the SQLite IN projections below conservative host-parameter limits.
        limit = max(1, min(int(limit), 500))
        runs = self.run_store.list_recent(limit)
        run_ids = [str(run.get("run_id") or "") for run in runs if run.get("run_id")]
        transitions_by_run: dict[str, list[dict]] = defaultdict(list)
        for row in self.run_store.transitions_for_runs(run_ids):
            transitions_by_run[str(row.get("run_id") or "")].append(row)
        tool_rows = self.tool_store.list_for_runs(run_ids)

        status_counts = Counter(str(run.get("status") or "unknown") for run in runs)
        outcome_count = sum(status_counts.get(status, 0) for status in OUTCOME_STATUSES)
        recovered = [run for run in runs if int(run.get("recovery_count") or 0) > 0]
        recovered_outcomes = [run for run in recovered if run.get("status") in OUTCOME_STATUSES]

        model_statuses: Counter[str] = Counter()
        repair_statuses: Counter[str] = Counter()
        evidence_relations: Counter[str] = Counter()
        evidence_review_required = 0
        failure_stages: Counter[str] = Counter()
        failure_codes: Counter[str] = Counter()
        failure_resolutions: Counter[str] = Counter()
        llm_latencies: list[float] = []
        timings = []
        for run in runs:
            event = run.get("event") or {}
            model_statuses[str(event.get("llm_status") or "unknown")] += 1
            repair_statuses[str((event.get("repair_trace") or {}).get("status") or "missing")] += 1
            evidence_policy = (
                (event.get("dispatch_decision") or {}).get("evidence_policy") or {}
            )
            if evidence_policy:
                evidence_relations[str(evidence_policy.get("relation") or "insufficient")] += 1
                evidence_review_required += int(
                    evidence_policy.get("review_required") is True
                )
            for failure in event.get("failure_attributions") or []:
                failure_stages[str(failure.get("stage") or "unknown")] += 1
                failure_codes[str(failure.get("code") or "unknown")] += 1
                failure_resolutions[str(failure.get("resolution") or "unknown")] += 1
            latency = event.get("llm_latency_ms")
            if isinstance(latency, (int, float)) and latency >= 0:
                llm_latencies.append(float(latency))
            timings.append(build_run_timing(
                run, transitions_by_run.get(str(run.get("run_id") or ""), [])
            ))

        tool_statuses = Counter(str(row.get("status") or "unknown") for row in tool_rows)
        tool_latencies = [_elapsed_ms(row.get("started_at"), row.get("completed_at")) for row in tool_rows]
        tools_by_action: dict[str, dict] = {}
        grouped_tools: dict[str, list[dict]] = defaultdict(list)
        for row in tool_rows:
            grouped_tools[f"{row.get('tool') or 'unknown'}.{row.get('action') or 'unknown'}"].append(row)
        for name, rows in sorted(grouped_tools.items()):
            statuses = Counter(str(row.get("status") or "unknown") for row in rows)
            tools_by_action[name] = {
                "execution_count": len(rows),
                "status_counts": dict(sorted(statuses.items())),
                "success_rate_pct": _rate(statuses.get("succeeded", 0), len(rows)),
                "retry_attempts": sum(
                    max(0, int(row.get("attempts") or 0) - 1) for row in rows
                ),
                "latency_ms": summarize_distribution(
                    _elapsed_ms(row.get("started_at"), row.get("completed_at"))
                    for row in rows
                ),
            }
        total_attempts = sum(int(row.get("attempts") or 0) for row in tool_rows)
        retry_attempts = sum(max(0, int(row.get("attempts") or 0) - 1) for row in tool_rows)
        successful_runs = status_counts.get("succeeded", 0)
        manual_runs = status_counts.get("manual_takeover", 0)
        recovered_successes = sum(run.get("status") == "succeeded" for run in recovered_outcomes)
        fallback_statuses = {"overloaded", "timeout", "failed", "invalid_json"}
        fallback_count = sum(model_statuses.get(status, 0) for status in fallback_statuses)
        analyzed_count = sum(
            count for status, count in model_statuses.items()
            if status not in {"unknown", "pending", "analyzing"}
        )
        repair_attempted = repair_statuses.get("repaired", 0) + repair_statuses.get("exhausted", 0)

        created_values = [str(run.get("created_at") or "") for run in runs]
        return {
            "schema_version": RUNTIME_METRICS_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scope": {
                "source": "durable_sqlite_projection",
                "max_runs": limit,
                "run_count": len(runs),
                "oldest_created_at": min(created_values) if created_values else "",
                "newest_created_at": max(created_values) if created_values else "",
                "notes": (
                    "Run and tool metrics cover a bounded recent-run sample; capacity counters "
                    "cover the current process lifetime. This is not a distributed SLA view."
                ),
            },
            "runs": {
                "status_counts": dict(sorted(status_counts.items())),
                "outcome_count": outcome_count,
                "active_count": sum(status_counts.get(status, 0) for status in ACTIVE_STATUSES),
                "success_rate_pct": _rate(successful_runs, outcome_count),
                "manual_takeover_rate_pct": _rate(manual_runs, outcome_count),
                "recovered_run_count": len(recovered),
                "recovery_attempt_count": sum(int(run.get("recovery_count") or 0) for run in runs),
                "recovery_success_rate_pct": _rate(recovered_successes, len(recovered_outcomes)),
            },
            "model": {
                "status_counts": dict(sorted(model_statuses.items())),
                "fallback_count": fallback_count,
                "fallback_rate_pct": _rate(fallback_count, analyzed_count),
                "repair_status_counts": dict(sorted(repair_statuses.items())),
                "repair_attempted_count": repair_attempted,
                "repair_success_rate_pct": _rate(repair_statuses.get("repaired", 0), repair_attempted),
                "evidence_relation_counts": dict(sorted(evidence_relations.items())),
                "evidence_review_required_count": evidence_review_required,
                "failure_stage_counts": dict(sorted(failure_stages.items())),
                "failure_code_counts": dict(sorted(failure_codes.items())),
                "failure_resolution_counts": dict(sorted(failure_resolutions.items())),
            },
            "tools": {
                "execution_count": len(tool_rows),
                "status_counts": dict(sorted(tool_statuses.items())),
                "success_rate_pct": _rate(tool_statuses.get("succeeded", 0), len(tool_rows)),
                "total_attempts": total_attempts,
                "retry_attempts": retry_attempts,
                "retried_execution_count": sum(int(row.get("attempts") or 0) > 1 for row in tool_rows),
                "by_action": tools_by_action,
            },
            "latency_ms": {
                "end_to_end": summarize_distribution(item["end_to_end_ms"] for item in timings),
                "ingest_to_decision": summarize_distribution(
                    item["ingest_to_decision_ms"] for item in timings
                ),
                "decision_to_execution": summarize_distribution(
                    item["decision_to_execution_ms"] for item in timings
                ),
                "execution_to_outcome": summarize_distribution(
                    item["execution_to_outcome_ms"] for item in timings
                ),
                "approval_wait": summarize_distribution(item["approval_wait_ms"] for item in timings),
                "model": summarize_distribution(llm_latencies),
                "tool_execution": summarize_distribution(tool_latencies),
            },
            "capacity": self.analysis_limiter.status() if self.analysis_limiter else {},
        }
