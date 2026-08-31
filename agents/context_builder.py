"""Budgeted, versioned context assembly for the safety Agent."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any


CONTEXT_SCHEMA_VERSION = "agent-context-v1"
CONTEXT_BUILDER_VERSION = "context-builder-v1.1-stable-round-selection"


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def estimate_tokens(value: Any) -> int:
    """Dependency-free, conservative token estimate for mixed Chinese/English JSON."""
    text = _canonical(value)
    utf8_bytes = len(text.encode("utf-8"))
    return max(1, math.ceil(max(len(text) / 4, utf8_bytes / 3)))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_bbox(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in ("x", "y", "width", "height")
        if isinstance(value.get(key), (int, float)) and not isinstance(value.get(key), bool)
    }


def _safe_scalar(value: Any, max_characters: int = 64) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:max_characters]


@dataclass(frozen=True)
class _ContextItem:
    item_id: str
    kind: str
    source: str
    trust: str
    priority: int
    required: bool
    freshness: str
    value: Any

    def summary(self, *, reason: str = "") -> dict:
        result = {
            "item_id": self.item_id,
            "kind": self.kind,
            "source": self.source,
            "trust": self.trust,
            "priority": self.priority,
            "required": self.required,
            "freshness": self.freshness,
            "estimated_tokens": estimate_tokens(self.value),
            "content_sha256": _sha256(self.value),
        }
        if reason:
            result["reason"] = reason
        return result


class ContextBuilder:
    """Select model context while never dropping the deterministic rule baseline."""

    def __init__(self, token_budget: int = 1200, *,
                 max_sop_citations: int = 3, max_memory_events: int = 5):
        self.token_budget = max(256, min(8192, int(token_budget)))
        self.max_sop_citations = max(0, min(10, int(max_sop_citations)))
        self.max_memory_events = max(0, min(20, int(max_memory_events)))

    @staticmethod
    def _render_payload(metadata: dict, selected: list[_ContextItem]) -> dict:
        payload = {
            **metadata,
            "detections": [],
            "memory": {
                "available": False, "zone": "", "zone_count": 0,
                "escalated": False, "summary": "", "recent_events": [],
            },
            "sop": {
                "status": "disabled", "catalog_version": "",
                "citations": [], "refusal_reason": "",
            },
        }
        for item in selected:
            if item.kind == "detection":
                payload["detections"].append(item.value)
            elif item.kind == "memory_status":
                payload["memory"].update(item.value)
            elif item.kind == "memory_summary":
                payload["memory"].update(item.value)
            elif item.kind == "memory_event":
                payload["memory"]["recent_events"].append(item.value)
            elif item.kind == "sop_status":
                payload["sop"].update(item.value)
            elif item.kind == "sop_citation":
                payload["sop"]["citations"].append(item.value)
        return payload

    def build(self, event, *, context_text: str = "", memory_context: dict | None = None,
              sop_context: dict | None = None,
              decision_context: dict | None = None) -> tuple[dict, dict]:
        memory_context = dict(memory_context or {})
        sop_context = dict(sop_context or {})
        raw = dict(getattr(event, "raw_json", {}) or {})
        timestamp = str(getattr(event, "timestamp", "") or "")
        metadata = {
            "source": str(raw.get("source") or "external"),
            "camera_id": str(
                getattr(event, "camera_id", "") or raw.get("cameraId") or "camera-01"
            ),
            "timestamp": timestamp,
        }
        if decision_context:
            raw_decision = dict(decision_context)
            metadata["decision_context"] = {
                "round": max(1, min(2, int(raw_decision.get("round") or 1))),
                "max_rounds": 2,
                "phase": str(raw_decision.get("phase") or "initial")[:32],
                "prior_output_sha256": str(
                    raw_decision.get("prior_output_sha256") or ""
                )[:64],
                "evidence_tool": str(raw_decision.get("evidence_tool") or "")[:80],
                "evidence_status": str(
                    raw_decision.get("evidence_status") or ""
                )[:40],
                "evidence_receipt_sha256": str(
                    raw_decision.get("evidence_receipt_sha256") or ""
                )[:64],
                "supplemental_frames": [
                    {
                        "frame_id": int(item.get("frame_id") or 0),
                        "offset_frames": int(item.get("offset_frames") or 0),
                        "image_sha256": str(item.get("image_sha256") or "")[:64],
                    }
                    for item in list(raw_decision.get("supplemental_frames") or [])[:5]
                    if isinstance(item, dict)
                ],
            }
        items: list[_ContextItem] = [
            _ContextItem(
                "event:metadata", "event_metadata", "ingress", "system",
                100, True, timestamp, metadata,
            )
        ]
        normalizations: list[dict] = []
        for index, raw_detection in enumerate(list(getattr(event, "events", []) or [])):
            raw_type = str(raw_detection.get("type") or "")
            raw_detail = str(raw_detection.get("detail") or "")
            raw_confidence = raw_detection.get("confidence")
            normalized_confidence = (
                raw_confidence
                if isinstance(raw_confidence, (int, float))
                and not isinstance(raw_confidence, bool)
                else None
            )
            detection = {
                "type": raw_type[:120],
                "rule_level": str(raw_detection.get("level") or "B")[:8],
                "detail": raw_detail[:240],
                "detail_truncated": len(raw_detail) > 240,
                "target_id": _safe_scalar(raw_detection.get("targetId", 0)),
                "confidence": normalized_confidence,
                "bbox": _safe_bbox(raw_detection.get("bbox")),
                "person_bbox": _safe_bbox(raw_detection.get("person_bbox")),
                "vehicle_bbox": _safe_bbox(raw_detection.get("vehicle_bbox")),
            }
            if raw_detection.get("base_level"):
                detection["base_rule_level"] = str(
                    raw_detection.get("base_level") or ""
                )[:8]
            memory_escalation = raw_detection.get("memory_escalation") or {}
            if isinstance(memory_escalation, dict) and memory_escalation:
                detection["memory_escalation"] = {
                    "policy_version": str(
                        memory_escalation.get("policy_version") or ""
                    )[:80],
                    "camera_id": str(memory_escalation.get("camera_id") or "")[:80],
                    "event_family": str(
                        memory_escalation.get("event_family") or ""
                    )[:80],
                    "zone": str(memory_escalation.get("zone") or "")[:40],
                    "history_count": max(
                        0, int(memory_escalation.get("history_count") or 0)
                    ),
                    "escalation_threshold": max(
                        0, int(memory_escalation.get("escalation_threshold") or 0)
                    ),
                    "trigger_event_ids": [
                        str(value)[:120]
                        for value in list(
                            memory_escalation.get("trigger_event_ids") or []
                        )[:20]
                    ],
                }
            item_id = f"detection:{index}:{_sha256(detection)[:12]}"
            if len(raw_type) > 120:
                normalizations.append({
                    "item_id": item_id, "field": "type",
                    "reason": "untrusted_text_length_limit",
                    "original_sha256": hashlib.sha256(
                        raw_type.encode("utf-8")
                    ).hexdigest(),
                    "retained_characters": 120,
                })
            if len(raw_detail) > 240:
                normalizations.append({
                    "item_id": item_id,
                    "field": "detail",
                    "reason": "untrusted_text_length_limit",
                    "original_sha256": hashlib.sha256(
                        raw_detail.encode("utf-8")
                    ).hexdigest(),
                    "retained_characters": 240,
                })
            if raw_confidence is not None and normalized_confidence is None:
                normalizations.append({
                    "item_id": item_id, "field": "confidence",
                    "reason": "non_numeric_value_removed",
                    "original_sha256": _sha256(raw_confidence),
                })
            for bbox_field in ("bbox", "person_bbox", "vehicle_bbox"):
                raw_bbox = raw_detection.get(bbox_field)
                if raw_bbox is not None and raw_bbox != detection[bbox_field]:
                    normalizations.append({
                        "item_id": item_id, "field": bbox_field,
                        "reason": "non_coordinate_fields_removed",
                        "original_sha256": _sha256(raw_bbox),
                    })
            items.append(_ContextItem(
                item_id,
                "detection", "perception", "constrained_event",
                100, True, timestamp, detection,
            ))

        sop_status = {
            "status": str(sop_context.get("status") or "disabled"),
            "catalog_version": str(sop_context.get("catalog_version") or ""),
            "refusal_reason": str(sop_context.get("refusal_reason") or ""),
        }
        items.append(_ContextItem(
            "sop:status", "sop_status", "sop_retriever", "verified_catalog",
            95, True, str(sop_context.get("catalog_version") or ""), sop_status,
        ))

        duplicate_items: list[dict] = []
        seen_citations: set[str] = set()
        selected_citation_candidates = 0
        for citation in list(sop_context.get("citations") or []):
            value = {
                "citation_id": str(citation.get("citation_id") or ""),
                "title": str(citation.get("title") or ""),
                "section": str(citation.get("section") or ""),
                "version": str(citation.get("version") or ""),
                "effective_date": str(citation.get("effective_date") or ""),
                "excerpt": str(citation.get("excerpt") or ""),
            }
            citation_id = value["citation_id"]
            item = _ContextItem(
                f"sop:{citation_id or _sha256(value)[:16]}", "sop_citation",
                "sop_retriever", "verified_catalog", 80, False,
                value["effective_date"] or value["version"], value,
            )
            if not citation_id or citation_id in seen_citations:
                duplicate_items.append(item.summary(reason="duplicate_or_missing_citation_id"))
                continue
            seen_citations.add(citation_id)
            if selected_citation_candidates >= self.max_sop_citations:
                duplicate_items.append(item.summary(reason="candidate_limit_exceeded"))
                continue
            items.append(item)
            selected_citation_candidates += 1

        summary = str(context_text or memory_context.get("context_text") or "").strip()
        memory_available = bool(
            summary and summary != "无近期相关事件记录"
        ) or bool(memory_context.get("recent_events"))
        memory_status = {
            "available": memory_available,
            "schema_version": str(memory_context.get("schema_version") or ""),
            "policy_version": str(memory_context.get("policy_version") or ""),
            "scope_valid": bool(memory_context.get("scope_valid", False)),
            "scope_reason": str(memory_context.get("scope_reason") or ""),
            "camera_id": str(memory_context.get("camera_id") or ""),
            "event_family": str(memory_context.get("event_family") or ""),
            "zone": str(memory_context.get("zone") or ""),
            "zone_count": int(memory_context.get("zone_count") or 0),
            "escalated": bool(memory_context.get("escalated", False)),
            "escalation_threshold": int(
                memory_context.get("escalation_threshold") or 0
            ),
            "trigger_event_ids": [
                str(value)[:120]
                for value in list(memory_context.get("trigger_event_ids") or [])[:20]
            ],
        }
        items.append(_ContextItem(
            "memory:status", "memory_status", "sqlite_memory", "historical_database",
            70, True, "lookback_60m", memory_status,
        ))
        if summary and summary != "无近期相关事件记录":
            items.append(_ContextItem(
                "memory:summary", "memory_summary", "sqlite_memory",
                "historical_database", 60, False, "lookback_60m",
                {"summary": summary[:600]},
            ))

        seen_memory: set[str] = set()
        selected_memory_candidates = 0
        for index, recent in enumerate(list(memory_context.get("recent_events") or [])):
            value = {
                "event_id": str(recent.get("event_id") or ""),
                "event_types": str(recent.get("event_types") or ""),
                "level": str(recent.get("level") or ""),
                "created_at": str(recent.get("created_at") or ""),
                "camera_id": str(
                    recent.get("memory_camera_id") or recent.get("camera_id") or ""
                ),
                "event_family": str(recent.get("memory_event_family") or ""),
                "zone": str(recent.get("memory_zone") or ""),
            }
            identity = value["event_id"] or _sha256(value)
            item = _ContextItem(
                f"memory:event:{identity}", "memory_event", "sqlite_memory",
                "historical_database", 50, False, value["created_at"], value,
            )
            if identity in seen_memory:
                duplicate_items.append(item.summary(reason="duplicate_memory_event"))
                continue
            seen_memory.add(identity)
            if selected_memory_candidates >= self.max_memory_events:
                duplicate_items.append(item.summary(reason="candidate_limit_exceeded"))
                continue
            items.append(item)
            selected_memory_candidates += 1

        required = [item for item in items if item.required]
        optional = sorted(
            (item for item in items if not item.required),
            key=lambda item: (-item.priority, item.item_id),
        )
        # Round-control metadata is mandatory governance context.  Excluding
        # only that metadata from optional-item admission keeps the exact same
        # Memory/SOP selection across decision rounds; the actual payload size
        # and any resulting overflow are still reported below.
        budget_metadata = dict(metadata)
        budget_metadata.pop("decision_context", None)
        selected = list(required)
        payload = self._render_payload(metadata, selected)
        used_tokens = estimate_tokens(payload)
        selection_tokens = estimate_tokens(
            self._render_payload(budget_metadata, selected)
        )
        dropped = list(duplicate_items)
        for item in optional:
            candidate_payload = self._render_payload(metadata, [*selected, item])
            candidate_selection_tokens = estimate_tokens(
                self._render_payload(budget_metadata, [*selected, item])
            )
            if candidate_selection_tokens <= self.token_budget:
                selected.append(item)
                payload = candidate_payload
                used_tokens = estimate_tokens(candidate_payload)
                selection_tokens = candidate_selection_tokens
            else:
                dropped.append(item.summary(reason="token_budget_exceeded"))

        selected_summaries = [item.summary() for item in selected]
        selected_citation_ids = [
            item.value["citation_id"] for item in selected if item.kind == "sop_citation"
        ]
        required_ids = {item.item_id for item in required}
        selected_ids = {item.item_id for item in selected}
        overflow = max(0, used_tokens - self.token_budget)
        context_sha256 = _sha256(payload)
        empty_image = {
            "present": False,
            "evidence_id": str(getattr(event, "evidence_id", "") or ""),
            "original_sha256": "",
            "input_sha256": "",
            "original_bytes": 0,
            "input_bytes": 0,
            "transformed": False,
        }
        manifest = {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "builder_version": CONTEXT_BUILDER_VERSION,
            "status": "built",
            "token_budget": self.token_budget,
            "estimated_tokens": used_tokens,
            "selection_estimated_tokens": selection_tokens,
            "budget_utilization_pct": round(100 * used_tokens / self.token_budget, 2),
            "budget_overflow_tokens": overflow,
            "truncated": bool(dropped),
            "critical_evidence_retained": required_ids.issubset(selected_ids),
            "input_item_count": len(items) + len(duplicate_items),
            "selected_item_count": len(selected),
            "dropped_item_count": len(dropped),
            "selected_items": selected_summaries,
            "dropped_items": dropped,
            "selected_citation_ids": selected_citation_ids,
            "normalizations": normalizations,
            "context_sha256": context_sha256,
            "image": empty_image,
            "model_input_sha256": _sha256({
                "context_sha256": context_sha256,
                "image_sha256": "",
            }),
            "source_versions": {
                "sop_catalog": str(sop_context.get("catalog_version") or ""),
                "memory_window": "60m",
            },
            "decision_round": int(
                (metadata.get("decision_context") or {}).get("round") or 1
            ),
        }
        return payload, manifest

    def skipped_manifest(self, event, reason: str) -> dict:
        identity = {
            "event_id": str(getattr(event, "event_id", "") or ""),
            "reason": str(reason or "context_not_built"),
        }
        context_sha256 = _sha256(identity)
        return {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "builder_version": CONTEXT_BUILDER_VERSION,
            "status": "skipped",
            "skip_reason": identity["reason"],
            "token_budget": self.token_budget,
            "estimated_tokens": 0,
            "selection_estimated_tokens": 0,
            "budget_utilization_pct": 0.0,
            "budget_overflow_tokens": 0,
            "truncated": False,
            "critical_evidence_retained": False,
            "input_item_count": 0,
            "selected_item_count": 0,
            "dropped_item_count": 0,
            "selected_items": [],
            "dropped_items": [],
            "selected_citation_ids": [],
            "normalizations": [],
            "context_sha256": context_sha256,
            "image": {
                "present": False,
                "evidence_id": str(getattr(event, "evidence_id", "") or ""),
                "original_sha256": "",
                "input_sha256": "",
                "original_bytes": 0,
                "input_bytes": 0,
                "transformed": False,
            },
            "model_input_sha256": _sha256({
                "context_sha256": context_sha256,
                "image_sha256": "",
            }),
            "source_versions": {},
        }
