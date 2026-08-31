"""Bounded, read-only temporal evidence acquisition for safety decisions.

This module deliberately governs only adjacent-frame inspection. Historical
memory and SOP retrieval are already part of the normal context pipeline and
must not be repackaged as fake "agent tools" merely to increase step count.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Callable


EVIDENCE_REPLAN_SCHEMA_VERSION = "bounded-evidence-replan-v1"
EVIDENCE_TOOL_POLICY_VERSION = "readonly-evidence-tool-policy-v1"
ADJACENT_FRAME_TOOL = "vision.inspect_adjacent_frames"
ALLOWED_NEXT_STEPS = {"decide", "inspect_adjacent_frames", "manual_review"}
MAX_DECISION_ROUNDS = 2
MAX_EVIDENCE_ACTIONS = 1


def _canonical(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def content_sha256(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def new_replan_trace(enabled: bool) -> dict:
    return {
        "schema_version": EVIDENCE_REPLAN_SCHEMA_VERSION,
        "policy_version": EVIDENCE_TOOL_POLICY_VERSION,
        "enabled": bool(enabled),
        "max_decision_rounds": MAX_DECISION_ROUNDS,
        "max_evidence_actions": MAX_EVIDENCE_ACTIONS,
        "decision_rounds": [],
        "evidence_actions": [],
        "status": "not_started" if enabled else "disabled",
        "manual_review_required": False,
        "review_reason": "",
    }


def normalize_next_step(data: dict) -> tuple[str, str, list[str]]:
    """Normalize the model proposal without granting it tool authority."""
    raw_step = str(data.get("next_step") or "decide").strip().lower()
    reason = str(data.get("next_step_reason") or "").strip()[:220]
    rejected: list[str] = []
    if raw_step not in ALLOWED_NEXT_STEPS:
        if raw_step:
            rejected.append(raw_step[:80])
        raw_step = "manual_review"
        reason = reason or "model requested an unregistered evidence action"
    return raw_step, reason, rejected


def append_decision_round(trace: dict, *, round_index: int, recommendation: dict,
                          context_manifest: dict, raw_output: str) -> None:
    request = recommendation.get("evidence_request") or {}
    trace.setdefault("decision_rounds", []).append({
        "round": int(round_index),
        "context_sha256": str(context_manifest.get("context_sha256") or ""),
        "model_input_sha256": str(context_manifest.get("model_input_sha256") or ""),
        "output_sha256": hashlib.sha256(str(raw_output or "").encode("utf-8")).hexdigest(),
        "structured_valid": bool(recommendation.get("risk_level")),
        "risk_level": str(recommendation.get("risk_level") or ""),
        "evidence_relation": str(recommendation.get("evidence_relation") or ""),
        "next_step": str(request.get("action") or "decide"),
        "next_step_reason": str(request.get("reason") or "")[:220],
    })


class AdjacentFrameEvidenceTool:
    """Invoke an injected local frame archive and return an auditable receipt.

    The provider is expected to return dictionaries containing ``frame_id``,
    ``captured_at`` and ``image_bytes``. Raw bytes are returned separately for
    the VLM call and are never embedded in the persisted receipt.
    """

    def __init__(self, provider: Callable | None = None, *, max_frames: int = 3):
        self.provider = provider
        self.max_frames = max(1, min(5, int(max_frames)))

    def set_provider(self, provider: Callable | None) -> None:
        self.provider = provider

    def execute(self, event) -> tuple[dict, list[bytes]]:
        started = time.perf_counter()
        raw = dict(getattr(event, "raw_json", {}) or {})
        source = str(raw.get("source") or "")
        anchor = raw.get("frameId", raw.get("frame_id"))
        stream_session_id = str(
            raw.get("frameSessionId") or raw.get("frame_session_id") or ""
        )
        try:
            anchor = int(anchor)
        except (TypeError, ValueError):
            anchor = None

        base = {
            "tool": ADJACENT_FRAME_TOOL,
            "policy_version": EVIDENCE_TOOL_POLICY_VERSION,
            "event_id": str(getattr(event, "event_id", "") or ""),
            "run_id": str(getattr(event, "run_id", "") or ""),
            "trace_id": str(getattr(event, "trace_id", "") or ""),
            "evidence_id": str(getattr(event, "evidence_id", "") or ""),
            "source": source,
            "camera_id": str(getattr(event, "camera_id", "") or ""),
            "anchor_frame_id": anchor,
            "stream_session_id": stream_session_id,
            "requested_limit": self.max_frames,
        }
        if source != "local_yolo":
            return self._receipt(
                base, "unavailable", "event_source_not_trusted_for_frame_archive",
                [], started,
            ), []
        if self.provider is None:
            return self._receipt(base, "unavailable", "frame_archive_not_configured", [], started), []
        if anchor is None:
            return self._receipt(base, "unavailable", "anchor_frame_id_missing", [], started), []
        if not stream_session_id:
            return self._receipt(
                base, "unavailable", "frame_session_id_missing", [], started
            ), []

        try:
            rows = self.provider(
                camera_id=base["camera_id"], anchor_frame_id=anchor,
                stream_session_id=stream_session_id, limit=self.max_frames,
            )
        except Exception as exc:
            return self._receipt(
                base, "failed", f"{type(exc).__name__}: {exc}"[:220], [], started
            ), []

        metadata: list[dict] = []
        images: list[bytes] = []
        seen_hashes: set[str] = set()
        for row in list(rows or []):
            if not isinstance(row, dict):
                continue
            image = row.get("image_bytes") or row.get("jpeg") or b""
            if not isinstance(image, (bytes, bytearray)) or not image:
                continue
            image = bytes(image)
            digest = hashlib.sha256(image).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            images.append(image)
            metadata.append({
                "frame_id": int(row.get("frame_id") or 0),
                "offset_frames": int(row.get("offset_frames") or 0),
                "captured_at": float(row.get("captured_at") or 0),
                "stream_session_id": str(
                    row.get("stream_session_id") or stream_session_id
                ),
                "image_sha256": digest,
                "byte_length": len(image),
            })
            if len(images) >= self.max_frames:
                break

        if not images:
            return self._receipt(base, "no_evidence", "no_adjacent_frames_available", [], started), []
        return self._receipt(base, "succeeded", "", metadata, started), images

    @staticmethod
    def _receipt(base: dict, status: str, error: str, frames: list[dict],
                 started: float) -> dict:
        result = {
            **base,
            "status": status,
            "error": error,
            "frames": frames,
            "frame_count": len(frames),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        result["request_sha256"] = content_sha256({
            "tool": result["tool"],
            "policy_version": result["policy_version"],
            "event_id": result["event_id"],
            "run_id": result["run_id"],
            "trace_id": result["trace_id"],
            "evidence_id": result["evidence_id"],
            "source": result["source"],
            "camera_id": result["camera_id"],
            "anchor_frame_id": result["anchor_frame_id"],
            "stream_session_id": result["stream_session_id"],
            "requested_limit": result["requested_limit"],
        })
        result["receipt_sha256"] = content_sha256({
            key: value for key, value in result.items() if key != "latency_ms"
        })
        return result


def terminal_review_reason(recommendation: dict) -> str:
    assessment = recommendation.get("evidence_assessment") or {}
    relation = str(
        assessment.get("relation") or recommendation.get("evidence_relation") or ""
    ).lower()
    request = recommendation.get("evidence_request") or {}
    action = str(request.get("action") or "decide")
    if relation == "conflict":
        return "multimodal_evidence_conflict"
    if action == "manual_review":
        return "model_requested_evidence_review"
    if action == "inspect_adjacent_frames" or relation == "insufficient":
        return "temporal_evidence_unresolved"
    return ""
