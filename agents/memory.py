"""Scoped historical event memory for industrial safety decisions."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager


MEMORY_POLICY_VERSION = "scoped-event-memory-v1"
MEMORY_ESCALATION_HISTORY_COUNT = 2
_LEVEL_WEIGHT = {"C": 1, "B": 2, "A": 3}


def classify_event_family(event_type: str) -> str:
    """Map concrete detector events to a stable memory comparison family."""
    value = str(event_type or "").strip()
    lowered = value.lower()
    if not value:
        return ""
    if "复合风险" in value:
        return "zone_intrusion"
    if any(token in lowered for token in ("安全帽", "反光背心", "helmet", "vest", "ppe")):
        return "ppe"
    if any(token in lowered for token in ("区域入侵", "intrusion", "danger_zone")):
        return "zone_intrusion"
    if any(token in lowered for token in ("人车", "person_vehicle", "pedestrian_vehicle")):
        return "person_vehicle_proximity"
    if any(token in lowered for token in ("火焰", "fire", "flame")):
        return "fire"
    if any(token in lowered for token in ("车辆", "叉车", "vehicle", "forklift", "forktruck")):
        return "vehicle"
    # Unknown event types remain comparable only to the same normalized type.
    return "event:" + " ".join(lowered.split())[:80]


def bbox_zone(bbox: dict | None) -> str:
    """Map a valid bbox origin to the project's coarse 200px spatial cell."""
    if not isinstance(bbox, dict) or "x" not in bbox or "y" not in bbox:
        return ""
    try:
        gx = int(float(bbox["x"]) / 200)
        gy = int(float(bbox["y"]) / 200)
    except (TypeError, ValueError, OverflowError):
        return ""
    return f"{gx}-{gy}"


def event_camera_id(event) -> str:
    raw = dict(getattr(event, "raw_json", {}) or {})
    return str(
        getattr(event, "camera_id", "")
        or raw.get("camera_id")
        or raw.get("cameraId")
        or ""
    ).strip()


def memory_scope_for_detection(detection: dict, camera_id: str) -> dict:
    """Build the auditable memory identity for one structured event."""
    detection = dict(detection or {})
    family = classify_event_family(detection.get("type", ""))
    # Human-related risks should be associated with the person position even
    # when a larger risk box is used for visualization.
    spatial_bbox = detection.get("person_bbox") or detection.get("bbox") or {}
    zone = bbox_zone(spatial_bbox)
    return {
        "camera_id": str(camera_id or "").strip(),
        "event_family": family,
        "zone": zone,
        "event_type": str(detection.get("type") or "").strip(),
        "level": str(detection.get("level") or "B").upper(),
        "valid": bool(str(camera_id or "").strip() and family and zone),
    }


def memory_facts_for_event(event) -> list[dict]:
    """Return one fact per camera/family/zone for a persisted alarm.

    Several concrete detections in one alarm (for example helmet and vest)
    form one historical incident for their shared family and zone. This avoids
    inflating repetition counts merely because one frame contains more labels.
    """
    camera_id = event_camera_id(event)
    grouped: dict[tuple[str, str, str], dict] = {}
    for detection in list(getattr(event, "events", []) or []):
        if not isinstance(detection, dict):
            continue
        scope = memory_scope_for_detection(detection, camera_id)
        if not scope["valid"]:
            continue
        key = (scope["camera_id"], scope["event_family"], scope["zone"])
        fact = grouped.setdefault(key, {
            **scope,
            "event_types": [],
            "level": "C",
        })
        if scope["event_type"] and scope["event_type"] not in fact["event_types"]:
            fact["event_types"].append(scope["event_type"])
        if _LEVEL_WEIGHT.get(scope["level"], 2) > _LEVEL_WEIGHT.get(fact["level"], 1):
            fact["level"] = scope["level"]
    return list(grouped.values())


def ensure_memory_schema(conn: sqlite3.Connection) -> None:
    """Apply the idempotent schema required by scoped event memory."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(alarms)").fetchall()}
    if "camera_id" not in columns:
        conn.execute("ALTER TABLE alarms ADD COLUMN camera_id TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alarm_memory_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alarm_id INTEGER NOT NULL,
            event_id TEXT,
            run_id TEXT,
            camera_id TEXT NOT NULL,
            event_family TEXT NOT NULL,
            zone TEXT NOT NULL,
            event_types_json TEXT NOT NULL,
            level TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(alarm_id, camera_id, event_family, zone)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alarm_memory_scope "
        "ON alarm_memory_facts(camera_id,event_family,zone,created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alarm_memory_event "
        "ON alarm_memory_facts(event_id)"
    )


class MemoryModule:
    """Retrieve recent incidents only inside a trusted event identity scope."""

    def __init__(self, db_path: str = "./data/alarms.db"):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self._lock = threading.Lock()
        self._ensure_db()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_db(self) -> None:
        with self._lock, self._connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alarms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT,
                    run_id TEXT,
                    trace_id TEXT,
                    camera_id TEXT,
                    timestamp TEXT NOT NULL,
                    event_types TEXT NOT NULL,
                    level TEXT NOT NULL,
                    detail TEXT,
                    bbox_json TEXT,
                    llm_analysis TEXT,
                    llm_recommendation TEXT,
                    dispatch_decision TEXT,
                    dispatch_actions TEXT,
                    approval_id TEXT,
                    approval_status TEXT DEFAULT 'auto',
                    lifecycle_status TEXT,
                    timeline TEXT,
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(alarms)").fetchall()
            }
            migrations = {
                "event_id": "ALTER TABLE alarms ADD COLUMN event_id TEXT",
                "run_id": "ALTER TABLE alarms ADD COLUMN run_id TEXT",
                "trace_id": "ALTER TABLE alarms ADD COLUMN trace_id TEXT",
                "camera_id": "ALTER TABLE alarms ADD COLUMN camera_id TEXT",
                "llm_recommendation": "ALTER TABLE alarms ADD COLUMN llm_recommendation TEXT",
                "dispatch_decision": "ALTER TABLE alarms ADD COLUMN dispatch_decision TEXT",
                "dispatch_actions": "ALTER TABLE alarms ADD COLUMN dispatch_actions TEXT",
                "approval_id": "ALTER TABLE alarms ADD COLUMN approval_id TEXT",
                "approval_status": (
                    "ALTER TABLE alarms ADD COLUMN approval_status TEXT DEFAULT 'auto'"
                ),
                "lifecycle_status": "ALTER TABLE alarms ADD COLUMN lifecycle_status TEXT",
                "timeline": "ALTER TABLE alarms ADD COLUMN timeline TEXT",
            }
            for name, sql in migrations.items():
                if name not in columns:
                    conn.execute(sql)
            ensure_memory_schema(conn)

    def get_event_context(self, event, lookback_minutes: int = 60) -> dict:
        """Retrieve history for every valid scope in the current event."""
        camera_id = event_camera_id(event)
        if not camera_id:
            return self._empty_context("camera_id_missing", camera_id="")
        facts = memory_facts_for_event(event)
        if not facts:
            return self._empty_context(
                "event_family_or_zone_missing", camera_id=camera_id
            )
        return self._query_scopes(facts, lookback_minutes)

    def get_context(self, bbox: dict, lookback_minutes: int = 60, *,
                    camera_id: str = "", event_types: list[str] | None = None) -> dict:
        """Compatibility API for callers that can provide an explicit scope."""
        camera_id = str(camera_id or "").strip()
        if not camera_id:
            return self._empty_context("camera_id_missing", camera_id="")
        facts = []
        for event_type in list(event_types or []):
            scope = memory_scope_for_detection(
                {"type": event_type, "level": "B", "bbox": bbox}, camera_id
            )
            if scope["valid"]:
                facts.append({**scope, "event_types": [str(event_type)]})
        if not facts:
            return self._empty_context(
                "event_family_or_zone_missing", camera_id=camera_id
            )
        deduplicated = {
            (item["camera_id"], item["event_family"], item["zone"]): item
            for item in facts
        }
        return self._query_scopes(list(deduplicated.values()), lookback_minutes)

    def _query_scopes(self, facts: list[dict], lookback_minutes: int) -> dict:
        lookback_minutes = max(1, min(int(lookback_minutes), 24 * 60))
        matched_scopes = []
        recent_by_alarm: dict[int, dict] = {}
        with self._lock, self._connection() as conn:
            conn.row_factory = sqlite3.Row
            for fact in facts:
                rows = conn.execute(
                    "SELECT a.*, f.alarm_id, f.camera_id AS memory_camera_id, "
                    "f.event_family AS memory_event_family, f.zone AS memory_zone, "
                    "f.event_types_json AS memory_event_types_json, "
                    "f.policy_version AS memory_policy_version, "
                    "f.created_at AS memory_created_at "
                    "FROM alarm_memory_facts f JOIN alarms a ON a.id=f.alarm_id "
                    "WHERE f.camera_id=? AND f.event_family=? AND f.zone=? "
                    "AND f.policy_version=? "
                    "AND f.created_at >= datetime('now','localtime',? || ' minutes') "
                    "ORDER BY f.id DESC LIMIT 50",
                    (
                        fact["camera_id"], fact["event_family"], fact["zone"],
                        MEMORY_POLICY_VERSION,
                        f"-{lookback_minutes}",
                    ),
                ).fetchall()
                decoded = [dict(row) for row in rows]
                for row in decoded:
                    recent_by_alarm.setdefault(int(row["alarm_id"]), row)
                trigger_ids = [
                    str(row.get("event_id") or f"ALARM_{row['alarm_id']}")
                    for row in decoded
                ]
                matched_scopes.append({
                    "camera_id": fact["camera_id"],
                    "event_family": fact["event_family"],
                    "zone": fact["zone"],
                    "current_level": fact.get("level", "B"),
                    "history_count": len(decoded),
                    "trigger_event_ids": trigger_ids,
                    "last_occurrence_at": (
                        str(decoded[0].get("memory_created_at") or "")
                        if decoded else ""
                    ),
                })

        matched_scopes.sort(
            key=lambda item: (
                -int(item["history_count"]),
                -_LEVEL_WEIGHT.get(str(item.get("current_level") or "B"), 2),
                item["event_family"], item["zone"],
            )
        )
        primary = matched_scopes[0]
        escalated_scopes = [
            dict(item) for item in matched_scopes
            if int(item["history_count"]) >= MEMORY_ESCALATION_HISTORY_COUNT
        ]
        recent = sorted(
            recent_by_alarm.values(),
            key=lambda row: int(row.get("alarm_id") or 0),
            reverse=True,
        )[:10]
        return {
            "schema_version": "event-memory-context-v2",
            "policy_version": MEMORY_POLICY_VERSION,
            "scope_valid": True,
            "scope_reason": (
                "matched_history" if primary["history_count"] else "no_matching_history"
            ),
            "camera_id": primary["camera_id"],
            "event_family": primary["event_family"],
            "zone": primary["zone"],
            "zone_count": int(primary["history_count"]),
            "escalated": bool(escalated_scopes),
            "escalation_threshold": MEMORY_ESCALATION_HISTORY_COUNT,
            "trigger_event_ids": list(primary["trigger_event_ids"]),
            "matched_scopes": matched_scopes,
            "escalated_scopes": escalated_scopes,
            "recent_events": recent,
            "context_text": self._context_text(
                primary, escalated_scopes, lookback_minutes
            ),
        }

    @staticmethod
    def _context_text(primary: dict, escalated_scopes: list[dict],
                      lookback_minutes: int) -> str:
        if not int(primary.get("history_count") or 0):
            return "无近期相关事件记录"
        count = int(primary["history_count"])
        prefix = (
            f"摄像头{primary['camera_id']}的区域{primary['zone']}在过去"
            f"{lookback_minutes}分钟发生{count}次{primary['event_family']}同类事件"
        )
        if escalated_scopes:
            return prefix + "，达到历史重复风险升级阈值"
        return prefix + "，尚未达到历史重复风险升级阈值"

    @staticmethod
    def _empty_context(reason: str, *, camera_id: str) -> dict:
        return {
            "schema_version": "event-memory-context-v2",
            "policy_version": MEMORY_POLICY_VERSION,
            "scope_valid": False,
            "scope_reason": reason,
            "camera_id": camera_id,
            "event_family": "",
            "zone": "",
            "zone_count": 0,
            "escalated": False,
            "escalation_threshold": MEMORY_ESCALATION_HISTORY_COUNT,
            "trigger_event_ids": [],
            "matched_scopes": [],
            "escalated_scopes": [],
            "recent_events": [],
            "context_text": "无近期相关事件记录",
        }

    # Kept for old callers and tests that inspect the spatial mapping helper.
    _bbox_zone = staticmethod(bbox_zone)

    @staticmethod
    def _match_zone(bbox_json: str, zone: str) -> bool:
        try:
            bboxes = json.loads(bbox_json) if isinstance(bbox_json, str) else bbox_json
        except (TypeError, json.JSONDecodeError):
            return False
        return any(
            bbox_zone(item) == zone for item in (bboxes or [])
            if isinstance(item, dict)
        )
