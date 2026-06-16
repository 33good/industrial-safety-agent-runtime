"""Edge actuator simulation tool for post-approval safety execution."""
import json
import os
import threading
from datetime import datetime


class ActuatorTool:
    """Record deterministic actuator outcomes after human approval."""

    def __init__(self, log_dir: str = "./data/executions"):
        os.makedirs(log_dir, exist_ok=True)
        self.log_dir = log_dir
        self._lock = threading.Lock()
        self.last_execution = {}

    def handle(self, order: dict, action: str = "execute") -> dict:
        if action == "execute":
            return self.execute(order)
        if action == "cancel":
            return self.cancel(order)
        return {"status": "unknown", "detail": f"unknown actuator action: {action}"}

    def execute(self, order: dict) -> dict:
        event_id = order.get("event_id", "")
        approval_id = order.get("id") or order.get("approval_id", "")
        execution_id = "EXEC_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        commands = [
            {"name": "sound_light_alarm", "status": "sent", "detail": "声光报警联动已下发"},
            {"name": "equipment_interlock", "status": "sent", "detail": "高危区域设备联锁停机已确认"},
            {"name": "safety_work_order", "status": "sent", "detail": "现场安全员处置工单已派发"},
        ]
        result = {
            "execution_id": execution_id,
            "event_id": event_id,
            "approval_id": approval_id,
            "status": "executed",
            "detail": "审批通过，已执行高危处置联动",
            "commands": commands,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._write(result)
        print(f"[Actuator] {execution_id} event={event_id or '-'} status=executed")
        return result

    def cancel(self, order: dict) -> dict:
        event_id = order.get("event_id", "")
        approval_id = order.get("id") or order.get("approval_id", "")
        execution_id = "EXEC_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        result = {
            "execution_id": execution_id,
            "event_id": event_id,
            "approval_id": approval_id,
            "status": "cancelled",
            "detail": "审批驳回，自动处置指令已取消",
            "commands": [],
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._write(result)
        print(f"[Actuator] {execution_id} event={event_id or '-'} status=cancelled")
        return result

    def status(self) -> dict:
        return {
            "status": "ok",
            "last_execution": dict(self.last_execution),
        }

    def _write(self, result: dict):
        with self._lock:
            self.last_execution = dict(result)
            fpath = os.path.join(self.log_dir, f"{result['execution_id']}.json")
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
