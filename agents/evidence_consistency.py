"""Conservative policy for separating visual and structured detector evidence."""
from __future__ import annotations

from typing import Any


EVIDENCE_ASSESSMENT_SCHEMA_VERSION = "evidence-assessment-v1"
EVIDENCE_POLICY_VERSION = "multimodal-conflict-policy-v1"
RELATIONS = {"consistent", "conflict", "image_only", "detections_only", "insufficient"}


def _short_list(value: Any, limit: int = 4, item_limit: int = 100) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:item_limit] for item in value if str(item).strip()][:limit]


def _conflicts(value: Any) -> list[dict]:
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:4]:
        if isinstance(item, str):
            detail = item.strip()[:180]
            if detail:
                result.append({"visual_claim": "", "detection_claim": "", "detail": detail})
        elif isinstance(item, dict):
            conflict = {
                "visual_claim": str(item.get("visual_claim") or "").strip()[:100],
                "detection_claim": str(item.get("detection_claim") or "").strip()[:100],
                "detail": str(item.get("detail") or "").strip()[:180],
            }
            if any(conflict.values()):
                result.append(conflict)
    return result


def assess_evidence(event, recommendation: dict) -> dict:
    """Normalize model diagnostics and only use them to reduce autonomy.

    The model may flag a conflict, but it can never use this field to lower the
    deterministic risk baseline. A conflict is actionable only when both evidence
    sources exist and the model supplies an auditable conflict description.
    """
    image_present = bool(getattr(event, "image_bytes", b""))
    detection_present = bool(getattr(event, "events", []) or [])
    claimed = str(recommendation.get("evidence_relation") or "").strip().lower()
    if claimed not in RELATIONS:
        claimed = "insufficient"
    visual = _short_list(recommendation.get("visual_observations"))
    detections = _short_list(recommendation.get("detection_observations"))
    conflicts = _conflicts(recommendation.get("evidence_conflicts"))

    if not image_present and detection_present:
        relation = "detections_only"
    elif image_present and not detection_present:
        relation = "image_only"
    elif not image_present and not detection_present:
        relation = "insufficient"
    elif claimed == "conflict" and conflicts:
        relation = "conflict"
    elif claimed == "consistent":
        relation = "consistent"
    else:
        relation = "insufficient"

    review_required = relation == "conflict"
    return {
        "schema_version": EVIDENCE_ASSESSMENT_SCHEMA_VERSION,
        "policy_version": EVIDENCE_POLICY_VERSION,
        "relation": relation,
        "model_claimed_relation": claimed,
        "image_present": image_present,
        "structured_detections_present": detection_present,
        "visual_observations": visual,
        "detection_observations": detections,
        "conflicts": conflicts,
        "review_required": review_required,
        "autonomy_allowed": not review_required,
        "reason": (
            "visual and structured evidence conflict requires operator review"
            if review_required else "no actionable cross-modal conflict established"
        ),
    }
