"""
数据库工具：SQLite 存储所有报警事件
"""
import sqlite3
import json
import os
import threading
from datetime import datetime


class DatabaseTool:
    def __init__(self, db_path: str = "./data/alarms.db"):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alarms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event_types TEXT NOT NULL,
                    level TEXT NOT NULL,
                    detail TEXT,
                    bbox_json TEXT,
                    llm_analysis TEXT,
                    llm_recommendation TEXT,
                    dispatch_decision TEXT,
                    dispatch_actions TEXT,
                    image_path TEXT,
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            self._ensure_columns(conn)
            # 索引加速查询
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alarms_time ON alarms(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alarms_level ON alarms(level)")

    @staticmethod
    def _ensure_columns(conn):
        columns = {row[1] for row in conn.execute("PRAGMA table_info(alarms)").fetchall()}
        migrations = {
            "llm_recommendation": "ALTER TABLE alarms ADD COLUMN llm_recommendation TEXT",
            "dispatch_decision": "ALTER TABLE alarms ADD COLUMN dispatch_decision TEXT",
            "dispatch_actions": "ALTER TABLE alarms ADD COLUMN dispatch_actions TEXT",
            "image_path": "ALTER TABLE alarms ADD COLUMN image_path TEXT",
        }
        for name, sql in migrations.items():
            if name not in columns:
                conn.execute(sql)

    def handle(self, event, action="store"):
        """处理数据库操作"""
        if action == "store":
            return self.store(event)
        elif action == "query_recent":
            return self.query_recent(hours=1)
        return "unknown action"

    def store(self, event) -> str:
        """存储报警事件"""
        event_types = ", ".join(e["type"] for e in event.events)
        level_weight = {"A": 3, "B": 2, "C": 1}
        level = max(
            (str(e.get("level", "B")).upper() for e in event.events),
            key=lambda lv: level_weight.get(lv, 2),
            default="B"
        )
        detail = "; ".join(e.get("detail", "") for e in event.events)
        bbox_json = json.dumps([e.get("bbox", {}) for e in event.events], ensure_ascii=False)
        llm_recommendation = json.dumps(getattr(event, "llm_recommendation", {}) or {}, ensure_ascii=False)
        dispatch_decision = json.dumps(getattr(event, "dispatch_decision", {}) or {}, ensure_ascii=False)
        dispatch_actions = json.dumps(getattr(event, "dispatch_actions", []) or [], ensure_ascii=False)
        image_path = getattr(event, "image_url", "")

        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO alarms (timestamp, event_types, level, detail, bbox_json, llm_analysis, "
                "llm_recommendation, dispatch_decision, dispatch_actions, image_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.timestamp, event_types, level, detail, bbox_json, event.llm_analysis,
                    llm_recommendation, dispatch_decision, dispatch_actions, image_path
                )
            )
        return f"已存入数据库 (total={self.count()})"

    def count(self) -> int:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM alarms").fetchone()[0]

    def query_recent(self, hours: int = 1) -> list:
        """查询最近的报警"""
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM alarms WHERE created_at >= datetime('now','localtime',? || ' hours') "
                "ORDER BY id DESC LIMIT 50",
                (f"-{hours}",)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """统计信息"""
        with self._lock, sqlite3.connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM alarms").fetchone()[0]
            a_count = conn.execute("SELECT COUNT(*) FROM alarms WHERE level='A'").fetchone()[0]
            b_count = conn.execute("SELECT COUNT(*) FROM alarms WHERE level='B'").fetchone()[0]
            today = conn.execute(
                "SELECT COUNT(*) FROM alarms WHERE date(created_at)=date('now','localtime')"
            ).fetchone()[0]
        return {"total": total, "A": a_count, "B": b_count, "today": today}
