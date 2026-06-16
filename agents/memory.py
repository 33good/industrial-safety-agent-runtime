"""
记忆模块：短期上下文 + 历史检索
安全Agent 分析时自动注入最近 1 小时的历史记录
"""
import sqlite3
import json
import os
import threading
from datetime import datetime


class MemoryModule:
    """
    智能体记忆：
    - 短期记忆：最近 1 小时同类违规记录
    - 区域记忆：同一空间区域的历史报警频率
    - 连续违规：同一区域短时间内的重复违规计数
    """

    def __init__(self, db_path: str = "./data/alarms.db"):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self._lock = threading.Lock()
        self._ensure_db()

    def _ensure_db(self):
        """确保数据库和表存在"""
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alarms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT,
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
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(alarms)").fetchall()}
            migrations = {
                "event_id": "ALTER TABLE alarms ADD COLUMN event_id TEXT",
                "llm_recommendation": "ALTER TABLE alarms ADD COLUMN llm_recommendation TEXT",
                "dispatch_decision": "ALTER TABLE alarms ADD COLUMN dispatch_decision TEXT",
                "dispatch_actions": "ALTER TABLE alarms ADD COLUMN dispatch_actions TEXT",
                "approval_id": "ALTER TABLE alarms ADD COLUMN approval_id TEXT",
                "approval_status": "ALTER TABLE alarms ADD COLUMN approval_status TEXT DEFAULT 'auto'",
            }
            for name, sql in migrations.items():
                if name not in columns:
                    conn.execute(sql)

    def get_context(self, bbox: dict, lookback_minutes: int = 60) -> dict:
        """
        获取当前事件的上下文记忆
        返回: {"recent_events": [...], "zone_count": N, "escalated": bool, "context_text": "..."}
        """
        zone = self._bbox_zone(bbox)
        now = datetime.now()

        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            # 1. 最近 N 分钟所有报警
            recent = conn.execute(
                "SELECT * FROM alarms WHERE created_at >= datetime('now','localtime',? || ' minutes') "
                "ORDER BY id DESC LIMIT 20",
                (f"-{lookback_minutes}",)
            ).fetchall()

            # 2. 同一区域最近 N 分钟的报警次数
            rows = conn.execute(
                "SELECT * FROM alarms WHERE created_at >= datetime('now','localtime',? || ' minutes')",
                (f"-{lookback_minutes}",)
            ).fetchall()
            zone_count = sum(1 for r in rows if self._match_zone(r["bbox_json"], zone))

            # 3. 区域内最近一次同类事件的时间
            zone_recent = [r for r in rows if self._match_zone(r["bbox_json"], zone)]
            last_zone_time = zone_recent[0]["created_at"] if zone_recent else None

        # 构建上下文文本
        context_parts = []
        if zone_count >= 5:
            context_parts.append(f"⚠️ 该区域过去{lookback_minutes}分钟已发生{zone_count}次违规事件，属于高风险区域")
            escalated = True
        elif zone_count >= 2:
            context_parts.append(f"该区域过去{lookback_minutes}分钟已发生{zone_count}次违规，需关注")
            escalated = True
        else:
            escalated = False

        if last_zone_time:
            context_parts.append(f"最近一次同类事件发生在{last_zone_time}")

        if recent:
            recent_types = []
            for r in recent[:5]:
                recent_types.append(r["event_types"])
            context_parts.append(f"近期事件类型: {', '.join(set(recent_types))}")

        context_text = "；".join(context_parts) if context_parts else "无近期相关事件记录"

        return {
            "zone": zone,
            "zone_count": zone_count,
            "escalated": escalated,
            "recent_events": [dict(r) for r in recent[:10]],
            "context_text": context_text
        }

    @staticmethod
    def _bbox_zone(bbox: dict) -> str:
        """将 bbox 映射到空间区域标识"""
        gx = int(bbox.get("x", 0) / 200)
        gy = int(bbox.get("y", 0) / 200)
        return f"{gx}-{gy}"

    @staticmethod
    def _match_zone(bbox_json: str, zone: str) -> bool:
        """判断存储的 bbox 是否属于目标区域"""
        try:
            bboxes = json.loads(bbox_json) if isinstance(bbox_json, str) else bbox_json
            if isinstance(bboxes, list) and len(bboxes) > 0:
                b = bboxes[0]
                gx = int(b.get("x", 0) / 200)
                gy = int(b.get("y", 0) / 200)
                return f"{gx}-{gy}" == zone
        except Exception:
            pass
        return False
