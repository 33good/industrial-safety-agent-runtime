"""
人机协同审批工具（Human-in-the-Loop）—— 可信机制核心模块

A 级高危事件：拦截自动执行，生成待审批工单，等待安全员确认
B/C 级事件：自动放行，仅记录日志
"""
import json
import os
import threading
import time
from datetime import datetime


class HumanLoopTool:
    """人工审批拦截器"""

    # These orders resolve evidence uncertainty only.  Approving them records
    # the operator review and must never be interpreted as authorization for a
    # high-risk actuator command.
    REVIEW_ONLY_HOLD_REASONS = frozenset({
        "multimodal_evidence_conflict",
        "model_requested_evidence_review",
        "temporal_evidence_unresolved",
        "temporal_evidence_unavailable",
        "temporal_evidence_no_evidence",
        "temporal_evidence_failed",
        "evidence_replan_capacity_exhausted",
        "evidence_replan_timeout",
        "evidence_replan_model_failed",
        "replan_risk_downgrade_requires_review",
        "evidence_review_required",
    })

    def __init__(self, pending_dir: str = "./data/pending"):
        os.makedirs(pending_dir, exist_ok=True)
        self.pending_dir = pending_dir
        self._lock = threading.Lock()
        self._approvals = {}  # 内存中的审批状态缓存

    def handle(self, event, action="check"):
        """处理审批请求"""
        if action == "check":
            return self._check_and_hold(event)
        elif action == "approve":
            return self._approve(event)
        elif action == "reject":
            return self._reject(event)
        return "unknown action"

    def _check_and_hold(self, event) -> str:
        """
        高危事件拦截机制：
        - A 级：必须人工审批，生成待办工单，模拟"停机指令被拦截"
        - B 级：自动通过，但记录日志
        - C 级：直接放行
        """
        decision = getattr(event, "dispatch_decision", {}) or {}
        top_level = str(decision.get("final_level") or "").upper()
        if top_level not in {"A", "B", "C"}:
            levels = {e.get("level", "B") for e in event.events}
            top_level = "A" if "A" in levels else ("B" if "B" in levels else "C")

        evidence_policy = decision.get("evidence_policy") or {}
        if evidence_policy.get("review_required") is True:
            hold_reason = str(
                evidence_policy.get("review_reason") or "multimodal_evidence_conflict"
            )
            if hold_reason not in self.REVIEW_ONLY_HOLD_REASONS:
                hold_reason = "evidence_review_required"
            return self._hold_for_approval(
                event, level=top_level, hold_reason=hold_reason
            )
        if top_level == "A":
            return self._hold_for_approval(event)
        elif top_level == "B":
            return self._auto_approve(event, "B级自动通过")
        else:
            return self._auto_approve(event, "C级自动通过")

    def _hold_for_approval(self, event, *, level: str = "A",
                           hold_reason: str = "high_risk") -> str:
        """拦截高危指令，创建待审批工单"""
        events_desc = ", ".join(e["type"] for e in event.events)
        pending_id = datetime.now().strftime("PENDING_%Y%m%d_%H%M%S_%f")

        # 创建待审批工单文件
        work_order = {
            "id": pending_id,
            "event_id": getattr(event, "event_id", ""),
            "run_id": getattr(event, "run_id", ""),
            "trace_id": getattr(event, "trace_id", ""),
            "timestamp": event.timestamp,
            "events": events_desc,
            "level": level,
            "hold_reason": hold_reason,
            "llm_analysis": event.llm_analysis or "待人工审核",
            "llm_recommendation": getattr(event, "llm_recommendation", {}) or {},
            "dispatch_decision": getattr(event, "dispatch_decision", {}) or {},
            "lifecycle_status": "pending_approval",
            "timeline": getattr(event, "timeline", []) or [],
            "status": "pending",
            "created_at": datetime.now().isoformat()
        }

        fpath = os.path.join(self.pending_dir, f"{pending_id}.json")
        with self._lock:
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(work_order, f, ensure_ascii=False, indent=2)
            self._approvals[pending_id] = work_order
        event.approval_id = pending_id
        event.approval_status = "pending"
        event.lifecycle_status = "pending_approval"

        print(f"\n{'!' * 60}")
        print(f"[可信拦截] {level}级事件进入人工复核！")
        print(f"  工单: {pending_id}")
        print(f"  事件: {events_desc}")
        print(f"  状态: 等待安全员审批...")
        print(f"  操作: 自动处置已被拦截，需人工确认后执行")
        print(f"{'!' * 60}\n")

        return f"已拦截并生成审批工单 {pending_id}，原因 {hold_reason}"

    def _auto_approve(self, event, reason: str) -> str:
        """自动放行非高危事件"""
        events_desc = ", ".join(e["type"] for e in event.events)
        print(f"[可信放行] {events_desc} -> {reason}")
        return reason

    def _approve(self, pending_id_or_event) -> str:
        """安全员审批通过"""
        pid = pending_id_or_event if isinstance(pending_id_or_event, str) else ""
        with self._lock:
            data = self._load_order(pid)
            if data:
                data["status"] = "approved"
                data["lifecycle_status"] = "approved"
                data["approved_at"] = datetime.now().isoformat()
                self._save_order(pid, data)
                self._approvals[pid] = data
                return f"工单 {pid} 已审批通过，执行高危处置指令"
        return f"工单 {pid} 未找到或已过期"

    def _reject(self, pending_id_or_event) -> str:
        """安全员驳回"""
        pid = pending_id_or_event if isinstance(pending_id_or_event, str) else ""
        with self._lock:
            data = self._load_order(pid)
            if data:
                data["status"] = "rejected"
                data["lifecycle_status"] = "rejected"
                data["rejected_at"] = datetime.now().isoformat()
                self._save_order(pid, data)
                self._approvals[pid] = data
                return f"工单 {pid} 已驳回，取消处置指令"
        return f"工单 {pid} 未找到或已过期"

    def _load_order(self, pid: str) -> dict:
        if not pid:
            return {}
        if pid in self._approvals:
            return dict(self._approvals[pid])
        fpath = os.path.join(self.pending_dir, f"{pid}.json")
        if not os.path.exists(fpath):
            return {}
        with open(fpath, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_order(self, pid: str, data: dict):
        fpath = os.path.join(self.pending_dir, f"{pid}.json")
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get_pending(self) -> list:
        """获取所有待审批工单"""
        with self._lock:
            result = []
            for fname in os.listdir(self.pending_dir):
                if fname.endswith(".json"):
                    fpath = os.path.join(self.pending_dir, fname)
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if data.get("status") == "pending":
                            result.append(data)
            return result
