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
    GROUNDING_POLICY_VERSION = "final-sop-grounding-v1"

    def __init__(self, tool_executor=None):
        super().__init__("调度Agent")
        self.tools = {}
        self.tool_executor = tool_executor

    def register_tool(self, name: str, handler, spec=None):
        """注册工具处理器"""
        self.tools[name] = handler
        if self.tool_executor is not None:
            self.tool_executor.register(name, handler, spec)

    def dispatch(self, event: AlarmEvent) -> list:
        """
        分析事件，决定执行哪些工具。
        返回: [{"tool":"", "action":"", "result":""}, ...]
        """
        rules = self.plan(event)
        return self.execute_plan(event, rules)

    def plan(self, event: AlarmEvent) -> list[dict]:
        """Build and validate the deterministic plan without causing side effects."""
        # 取检测/记忆后的最高等级，再结合 LLM 结构化建议做安全裁决。
        rule_level = self._highest_event_level(event)
        decision = self._make_decision(event, rule_level)
        decision["evidence_policy"] = dict(
            (getattr(event, "llm_recommendation", {}) or {}).get(
                "evidence_assessment"
            ) or {}
        )
        top_level = decision["final_level"]
        decision["grounding"] = self._finalize_grounding(event)
        event.dispatch_decision = decision

        rules, plan_validation = self._validate_tool_plan(top_level, event)
        decision["plan_validation"] = plan_validation
        self.log(
            f"规则等级 {rule_level} + LLM建议 {decision.get('llm_level') or '无'}"
            f" → 最终 {top_level} ({decision['policy']})"
        )
        self.log(f"事件等级 {top_level} → {len(rules)} 个工具待执行")
        self.log(
            f"计划校验 接受{len(plan_validation['accepted'])}项 / "
            f"强制补齐{len(plan_validation['forced'])}项 / 拒绝{len(plan_validation['rejected'])}项"
        )
        return sorted(rules, key=lambda rule: rule["priority"])

    def _finalize_grounding(self, event: AlarmEvent) -> dict:
        """Bind final SOP evidence to trusted structured events, not model preference.

        Retrieval candidates must have entered this turn's governed context and
        declare an exact event-type match. The model-selected citations remain in
        ``llm_recommendation`` for audit, but cannot add or remove final evidence.
        """
        retrieval = getattr(event, "sop_retrieval", {}) or {}
        selected_ids = set(
            (getattr(event, "context_manifest", {}) or {}).get(
                "selected_citation_ids"
            ) or []
        )
        event_types = {
            str(item.get("type") or "").strip()
            for item in (getattr(event, "events", []) or [])
            if isinstance(item, dict) and str(item.get("type") or "").strip()
        }
        grounded = []
        for citation in retrieval.get("citations") or []:
            if not isinstance(citation, dict):
                continue
            citation_id = str(citation.get("citation_id") or "").strip()
            matched_event_types = sorted(
                event_types.intersection(
                    str(value).strip()
                    for value in citation.get("matched_event_types") or []
                    if str(value).strip()
                )
            )
            if not citation_id or citation_id not in selected_ids or not matched_event_types:
                continue
            grounded.append({
                "citation_id": citation_id,
                "document_id": str(citation.get("document_id") or ""),
                "title": str(citation.get("title") or ""),
                "section": str(citation.get("section") or ""),
                "version": str(citation.get("version") or ""),
                "source": str(citation.get("source") or ""),
                "excerpt": str(citation.get("excerpt") or "")[:280],
                "binding": "structured_event_exact",
                "matched_event_types": matched_event_types,
            })

        retrieval_status = str(retrieval.get("status") or "not_run")
        if grounded:
            status = "grounded"
            refusal_reason = ""
        elif retrieval_status == "retrieved":
            status = "retrieved_unbound"
            refusal_reason = "retrieved SOP was not exactly bound to a structured event"
        else:
            status = retrieval_status
            refusal_reason = str(retrieval.get("refusal_reason") or "")[:220]
        return {
            "policy_version": self.GROUNDING_POLICY_VERSION,
            "status": status,
            "catalog_version": str(retrieval.get("catalog_version") or ""),
            "citations": grounded,
            "citation_ids": [item["citation_id"] for item in grounded],
            "refusal_reason": refusal_reason,
            "model_candidate_citation_ids": [
                str(item.get("citation_id") or "")
                for item in (
                    (getattr(event, "llm_recommendation", {}) or {}).get(
                        "sop_citations"
                    ) or []
                )
                if isinstance(item, dict) and item.get("citation_id")
            ],
        }

    def execute_plan(self, event: AlarmEvent, rules: list[dict]) -> list:
        """Execute a previously validated plan through the configured tool boundary."""
        results = []
        for rule in rules:
            tool_name = rule["tool"]
            action = rule["action"]
            handler = self.tools.get(tool_name)
            if handler:
                if self.tool_executor is not None:
                    outcome = self.tool_executor.execute(event, tool_name, action)
                    item = outcome.as_dispatch_result(tool_name, action)
                    results.append(item)
                    event.dispatch_actions = results
                    marker = "OK" if outcome.status == "succeeded" else "FAIL"
                    self.log(
                        f"  [{marker}] {tool_name}.{action} status={outcome.status} "
                        f"attempts={outcome.attempts} reused={outcome.reused}"
                    )
                else:
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

    def _validate_tool_plan(self, level: str, event: AlarmEvent) -> tuple[list, dict]:
        """Validate the LLM candidate plan without ever removing baseline safety tools."""
        baseline = [dict(item) for item in self.RULES.get(level, [])]
        baseline_names = [f"{item['tool']}.{item['action']}" for item in baseline]
        recommendation = getattr(event, "llm_recommendation", {}) or {}
        candidates = recommendation.get("action_plan") or []
        candidate_names = []
        reasons = {}
        for item in candidates:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                tool, action = str(item.get("tool", "")).strip(), str(item.get("action", "")).strip()
                name = f"{tool}.{action}" if tool and action else ""
            if name and name not in candidate_names:
                candidate_names.append(name)
                reasons[name] = str(item.get("reason", ""))[:120]

        accepted = [
            {"name": name, "reason": reasons.get(name, "")}
            for name in candidate_names if name in baseline_names
        ]
        rejected = [
            {"name": name, "reason": f"not_allowed_for_{level}_level"}
            for name in candidate_names if name not in baseline_names
        ]
        rejected.extend(
            {"name": name, "reason": "not_in_tool_whitelist"}
            for name in recommendation.get("rejected_candidate_actions", [])
        )
        forced = [
            {"name": name, "reason": "mandatory_safety_policy"}
            for name in baseline_names if name not in candidate_names
        ]
        return baseline, {
            "level": level,
            "candidate_plan": candidate_names,
            "candidate_count": len(candidate_names),
            "accepted": accepted,
            "forced": forced,
            "rejected": rejected,
            "final_plan": baseline_names,
            "baseline_preserved": True,
        }

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
