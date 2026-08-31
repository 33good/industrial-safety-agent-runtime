import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import AlarmEvent
from agents.memory import MEMORY_POLICY_VERSION, MemoryModule
from agents.perception import PerceptionAgent
from agents.safety_agent import SafetyAgent
from services.run_store import event_snapshot
from services.trace_validator import build_trace, validate_trace
from tools.database import DatabaseTool


def _event(event_id: str, camera_id: str, event_type: str, level: str = "B",
           *, x: float = 20, y: float = 600) -> AlarmEvent:
    return AlarmEvent(
        timestamp="2026-08-27 12:00:00",
        event_id=event_id,
        run_id=f"RUN_{event_id}",
        camera_id=camera_id,
        raw_json={"source": "test", "cameraId": camera_id},
        events=[{
            "type": event_type,
            "level": level,
            "detail": event_type,
            "targetId": 7,
            "confidence": 0.95,
            "bbox": {"x": x, "y": y, "width": 80, "height": 180},
        }],
    )


class MemorySemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "alarms.db")
        self.database = DatabaseTool(self.db_path)
        self.memory = MemoryModule(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_camera_zone_and_event_family_can_escalate(self):
        self.database.store(_event("HISTORY_1", "camera-01", "未戴安全帽"))
        self.database.store(_event("HISTORY_2", "camera-01", "未穿反光背心"))

        context = self.memory.get_event_context(
            _event("CURRENT", "camera-01", "安全帽佩戴不规范")
        )

        self.assertTrue(context["scope_valid"])
        self.assertTrue(context["escalated"])
        self.assertEqual(context["zone_count"], 2)
        self.assertEqual(context["event_family"], "ppe")
        self.assertEqual(context["camera_id"], "camera-01")
        self.assertEqual(
            set(context["trigger_event_ids"]), {"HISTORY_1", "HISTORY_2"}
        )
        self.assertEqual(context["policy_version"], MEMORY_POLICY_VERSION)

    def test_camera_family_and_zone_are_all_required(self):
        self.database.store(_event("OTHER_CAMERA", "camera-02", "未戴安全帽"))
        self.database.store(_event("OTHER_FAMILY", "camera-01", "车辆检测", "C"))
        self.database.store(
            _event("OTHER_ZONE", "camera-01", "未穿反光背心", x=620, y=600)
        )
        self.database.store(_event("ONE_MATCH", "camera-01", "未戴安全帽"))

        context = self.memory.get_event_context(
            _event("CURRENT", "camera-01", "未穿反光背心")
        )

        self.assertFalse(context["escalated"])
        self.assertEqual(context["zone_count"], 1)
        self.assertEqual(context["trigger_event_ids"], ["ONE_MATCH"])

    def test_one_alarm_with_multiple_ppe_labels_counts_as_one_incident(self):
        history = _event("HISTORY_MULTI", "camera-01", "未戴安全帽")
        history.events.append({
            **history.events[0],
            "type": "未穿反光背心",
            "detail": "未穿反光背心",
        })
        self.database.store(history)

        context = self.memory.get_event_context(
            _event("CURRENT", "camera-01", "未戴安全帽")
        )
        conn = sqlite3.connect(self.db_path)
        try:
            fact_count = conn.execute(
                "SELECT COUNT(*) FROM alarm_memory_facts WHERE event_id=?",
                ("HISTORY_MULTI",),
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(fact_count, 1)
        self.assertEqual(context["zone_count"], 1)
        self.assertFalse(context["escalated"])

    def test_events_outside_the_lookback_window_do_not_escalate(self):
        self.database.store(_event("OLD_1", "camera-01", "未戴安全帽"))
        self.database.store(_event("OLD_2", "camera-01", "未戴安全帽"))
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE alarm_memory_facts "
                "SET created_at=datetime('now','localtime','-2 hours')"
            )
            conn.commit()
        finally:
            conn.close()

        context = self.memory.get_event_context(
            _event("CURRENT", "camera-01", "未戴安全帽"),
            lookback_minutes=60,
        )

        self.assertEqual(context["zone_count"], 0)
        self.assertFalse(context["escalated"])

    def test_missing_camera_identity_cannot_escalate(self):
        self.database.store(_event("HISTORY_1", "camera-01", "未戴安全帽"))
        self.database.store(_event("HISTORY_2", "camera-01", "未戴安全帽"))

        current = _event("CURRENT", "", "未戴安全帽")
        current.raw_json = {"source": "test"}
        context = self.memory.get_event_context(current)

        self.assertFalse(context["scope_valid"])
        self.assertFalse(context["escalated"])
        self.assertEqual(context["zone_count"], 0)
        self.assertEqual(context["scope_reason"], "camera_id_missing")

    def test_safety_agent_upgrades_only_the_matching_b_family_with_provenance(self):
        self.database.store(_event("HISTORY_1", "camera-01", "未戴安全帽"))
        self.database.store(_event("HISTORY_2", "camera-01", "未穿反光背心"))
        current = _event("CURRENT", "camera-01", "未戴安全帽")
        current.events.append({
            "type": "区域入侵-仓储缓冲带",
            "level": "B",
            "detail": "进入仓储缓冲带",
            "targetId": 7,
            "confidence": 0.9,
            "bbox": dict(current.events[0]["bbox"]),
        })
        response = json.dumps({
            "summary": "历史 PPE 重复违规",
            "risk_level": "A",
            "risk_reason": "同摄像头同区域 PPE 重复出现",
            "recommended_actions": [],
            "need_human_confirm": True,
            "confidence": 0.9,
            "visual_observations": [],
            "detection_observations": [],
            "evidence_relation": "detections_only",
            "conflict_details": [],
            "next_step": {"action": "decide", "reason": "structured history"},
        }, ensure_ascii=False)
        agent = SafetyAgent(mode="ollama", model="test", memory=self.memory)

        with patch.object(agent, "_call_ollama", return_value=response):
            agent.analyze(current)

        ppe, zone = current.events
        self.assertEqual(ppe["base_level"], "B")
        self.assertEqual(ppe["level"], "A")
        self.assertEqual(ppe["memory_escalation"]["event_family"], "ppe")
        self.assertEqual(
            set(ppe["memory_escalation"]["trigger_event_ids"]),
            {"HISTORY_1", "HISTORY_2"},
        )
        self.assertEqual(zone["level"], "B")
        self.assertNotIn("memory_escalation", zone)

        trace = build_trace({
            "source": "test",
            "source_event_id": "SOURCE_CURRENT",
            "camera_id": current.camera_id,
            "event_id": current.event_id,
            "run_id": current.run_id,
            "trace_id": "TRACE_CURRENT",
            "status": "manual_takeover",
            "stage": "test",
            "event": {
                **event_snapshot(current),
                "trace_id": "TRACE_CURRENT",
                "source_event_id": "SOURCE_CURRENT",
            },
        }, [], [])
        self.assertEqual(trace["memory"]["escalation_count"], 1)
        self.assertEqual(
            set(trace["memory"]["escalations"][0]["trigger_event_ids"]),
            {"HISTORY_1", "HISTORY_2"},
        )
        broken = copy.deepcopy(trace)
        broken["memory"]["escalations"][0]["trigger_event_ids"] = []
        self.assertIn(
            "memory_escalation_missing_trigger_event_ids",
            validate_trace(broken)["errors"],
        )

    def test_old_database_migration_is_idempotent_and_keeps_legacy_rows(self):
        legacy_path = str(Path(self.tmp.name) / "legacy.db")
        conn = sqlite3.connect(legacy_path)
        try:
            conn.execute("""
                CREATE TABLE alarms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event_types TEXT NOT NULL,
                    level TEXT NOT NULL,
                    bbox_json TEXT,
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            conn.execute(
                "INSERT INTO alarms(timestamp,event_types,level,bbox_json) "
                "VALUES (?,?,?,?)",
                ("legacy", "未戴安全帽", "B", '[{"x":20,"y":600}]'),
            )
            conn.commit()
        finally:
            conn.close()

        MemoryModule(legacy_path)
        DatabaseTool(legacy_path)
        MemoryModule(legacy_path)
        conn = sqlite3.connect(legacy_path)
        try:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(alarms)").fetchall()
            }
            facts_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='alarm_memory_facts'"
            ).fetchone()
            legacy_count = conn.execute("SELECT COUNT(*) FROM alarms").fetchone()[0]
            fact_count = conn.execute(
                "SELECT COUNT(*) FROM alarm_memory_facts"
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertIn("camera_id", columns)
        self.assertIsNotNone(facts_table)
        self.assertEqual(legacy_count, 1)
        self.assertEqual(fact_count, 0)


class CompoundRiskTests(unittest.TestCase):
    @staticmethod
    def _person(x: float, y: float, *, target_id: int = 9) -> dict:
        return {
            "targetType": 0,
            "targetId": target_id,
            "confidence": 950,
            "posRect": {"x": x, "y": y, "width": 100, "height": 180},
            "ppeStatus": {
                "helmet": {"status": "missing", "confidence": 0.96},
                "vest": {"status": "missing", "confidence": 0.91},
            },
        }

    def test_double_ppe_violation_is_written_back_once(self):
        current = PerceptionAgent().process(
            {"objInfo": [self._person(400, 600)]}, verbose=False
        )
        types = [item["type"] for item in current.events]
        self.assertEqual(types.count("复合违规-双重缺失"), 1)
        self.assertEqual(len(current.events), 3)
        self.assertEqual(PerceptionAgent()._compound_risk(current.events), [])

    def test_only_a_level_zone_creates_compound_high_risk(self):
        high = PerceptionAgent().process(
            {"objInfo": [self._person(150, 120)]}, verbose=False
        )
        buffer = PerceptionAgent().process(
            {"objInfo": [self._person(40, 450)]}, verbose=False
        )

        high_types = [item["type"] for item in high.events]
        buffer_types = [item["type"] for item in buffer.events]
        self.assertEqual(high_types.count("复合风险-高危"), 1)
        self.assertNotIn("复合风险-高危", buffer_types)
        self.assertIn("复合违规-双重缺失", buffer_types)


if __name__ == "__main__":
    unittest.main()
