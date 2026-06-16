"""
调度Agent：规则约束 + LLM 结构化建议的可信决策
"""
from . import AlarmEvent, BaseAgent


class DispatchAgent(BaseAgent):
    """检测等级/记忆升级/LLM建议 → 安全裁决 → 调用工具"""

    # 决策规则（最终执行仍由规则约束，避免 LLM 幻觉直接控制工具）
    RULES = {
        "A": [  # 高危——火焰/连续违规 → 人机协同审批
            {"tool": "human_loop", "action": "check", "priority": 0},   # 先拦截
            {"tool": "database", "action": "store", "priority": 1},
            {"tool": "notifier", "action": "send_urgent", "priority": 1},
            {"tool": "reporter", "action": "generate", "priority": 2},
        ],
        "B": [  # 一般违规——安全帽/背心 → 自动放行 + 推送
            {"tool": "human_loop", "action": "check", "priority": 0},
            {"tool": "database", "action": "store", "priority": 1},
            {"tool": "notifier", "action": "send", "priority": 2},
            {"tool": "reporter", "action": "log", "priority": 3},
        ],
        "C": [  # 提示——车辆
            {"tool": "human_loop", "action": "check", "priority": 0},
            {"tool": "database", "action": "store", "priority": 1},
        ],
    }

    LEVEL_WEIGHT = {"C": 1, "B": 2, "A": 3}

    def __init__(self):
        super().__init__("调度Agent")
        self.tools = {}

    def register_tool(self, name: str, handler):
        """注册工具处理器"""
        self.tools[name] = handler

    def dispatch(self, event: AlarmEvent) -> list:
        """
        分析事件，决定执行哪些工具。
        返回: [{"tool":"", "action":"", "result":""}, ...]
        """
        # 取检测/记忆后的最高等级，再结合 LLM 结构化建议做安全裁决。
        rule_level = self._highest_event_level(event)
        decision = self._make_decision(event, rule_level)
        top_level = decision["final_level"]
        event.dispatch_decision = decision

        rules = self.RULES.get(top_level, [])
        self.log(
            f"规则等级 {rule_level} + LLM建议 {decision.get('llm_level') or '无'}"
            f" → 最终 {top_level} ({decision['policy']})"
        )
        self.log(f"事件等级 {top_level} → {len(rules)} 个工具待执行")

        results = []
        for rule in sorted(rules, key=lambda r: r["priority"]):
            tool_name = rule["tool"]
            action = rule["action"]
            handler = self.tools.get(tool_name)
            if handler:
                try:
                    result = handler(event, action)
                    results.append({"tool": tool_name, "action": action, "result": result})
                    event.dispatch_actions = results
                    self.log(f"  [OK] {tool_name}.{action} -> {result}")
                except Exception as e:
                    self.log(f"  [FAIL] {tool_name}.{action} 失败: {e}")
                    results.append({"tool": tool_name, "action": action, "result": f"失败: {e}"})
                    event.dispatch_actions = results
            else:
                self.log(f"  - {tool_name}.{action} (未注册，跳过)")

        event.dispatch_actions = results
        return results

    def _highest_event_level(self, event: AlarmEvent) -> str:
        levels = {str(e.get("level", "B")).upper() for e in event.events}
        if "A" in levels:
            return "A"
        if "B" in levels:
            return "B"
        return "C"

    def _make_decision(self, event: AlarmEvent, rule_level: str) -> dict:
        rec = getattr(event, "llm_recommendation", {}) or {}
        llm_level = str(rec.get("risk_level", "")).upper()
        if llm_level not in self.LEVEL_WEIGHT:
            return {
                "rule_level": rule_level,
                "llm_level": "",
                "final_level": rule_level,
                "llm_adopted": False,
                "policy": "LLM无有效结构化等级，采用规则等级",
                "reason": "missing_or_invalid_llm_level",
            }

        rule_weight = self.LEVEL_WEIGHT.get(rule_level, 2)
        llm_weight = self.LEVEL_WEIGHT[llm_level]
        confidence = float(rec.get("confidence", 0) or 0)
        actions = set(rec.get("recommended_actions", []) or [])
        asks_human = bool(rec.get("need_human_confirm")) or "human_loop.check" in actions
        asks_urgent = "notifier.send_urgent" in actions or "reporter.generate" in actions

        if llm_weight > rule_weight:
            if confidence >= 0.55 or asks_human or asks_urgent:
                return {
                    "rule_level": rule_level,
                    "llm_level": llm_level,
                    "final_level": llm_level,
                    "llm_adopted": True,
                    "policy": "采纳LLM升级建议，执行更高安全等级规则",
                    "reason": rec.get("risk_reason", ""),
                    "confidence": confidence,
                    "recommended_actions": list(actions),
                }
            return {
                "rule_level": rule_level,
                "llm_level": llm_level,
                "final_level": rule_level,
                "llm_adopted": False,
                "policy": "LLM建议升级但置信度不足，采用规则等级",
                "reason": rec.get("risk_reason", ""),
                "confidence": confidence,
                "recommended_actions": list(actions),
            }

        if llm_weight < rule_weight:
            return {
                "rule_level": rule_level,
                "llm_level": llm_level,
                "final_level": rule_level,
                "llm_adopted": False,
                "policy": "拒绝LLM降级建议，采用更保守的规则等级",
                "reason": rec.get("risk_reason", ""),
                "confidence": confidence,
                "recommended_actions": list(actions),
            }

        return {
            "rule_level": rule_level,
            "llm_level": llm_level,
            "final_level": rule_level,
            "llm_adopted": True,
            "policy": "LLM建议与规则等级一致",
            "reason": rec.get("risk_reason", ""),
            "confidence": confidence,
            "recommended_actions": list(actions),
        }
