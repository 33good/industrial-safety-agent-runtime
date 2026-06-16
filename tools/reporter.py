"""
报告工具：生成安全事件报告
"""
import os
from datetime import datetime


class ReporterTool:
    def __init__(self, report_dir: str = "./data/reports"):
        os.makedirs(report_dir, exist_ok=True)
        self.report_dir = report_dir

    def handle(self, event, action="log"):
        if action == "generate":
            return self.generate(event)
        elif action == "log":
            return self.log(event)
        return "unknown action"

    def generate(self, event) -> str:
        """生成 Markdown 报告"""
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"report_{now}.md"
        fpath = os.path.join(self.report_dir, fname)

        events_md = "\n".join(
            f"- **{e['type']}** ({e['level']}级): {e['detail']}" for e in event.events
        )
        rec = getattr(event, "llm_recommendation", {}) or {}
        decision = getattr(event, "dispatch_decision", {}) or {}
        rec_md = (
            f"- 建议等级: {rec.get('risk_level', '无')}\n"
            f"- 判断依据: {rec.get('risk_reason', '无')}\n"
            f"- 建议动作: {', '.join(rec.get('recommended_actions', []) or []) or '无'}\n"
            f"- 需要人工确认: {'是' if rec.get('need_human_confirm') else '否'}\n"
            f"- 置信度: {float(rec.get('confidence', 0) or 0):.2f}"
        ) if rec else "无结构化建议"
        decision_md = (
            f"- 规则等级: {decision.get('rule_level', '无')}\n"
            f"- LLM建议等级: {decision.get('llm_level', '无')}\n"
            f"- 最终等级: {decision.get('final_level', '无')}\n"
            f"- 裁决策略: {decision.get('policy', '无')}\n"
            f"- 是否采纳LLM: {'是' if decision.get('llm_adopted') else '否'}"
        ) if decision else "无调度裁决"

        content = f"""# 工厂安全事件报告

**时间**: {event.timestamp}
**等级**: {("A" if any(e.get('level')=='A' for e in event.events) else ("B" if any(e.get('level')=='B' for e in event.events) else "C"))}级

## 检测事件
{events_md}

## AI 分析
{event.llm_analysis or '无'}

## LLM 结构化风险建议
{rec_md}

## 调度 Agent 可信裁决
{decision_md}

## 处置措施
{chr(10).join('- ' + a.get('action','') for a in event.dispatch_actions) if event.dispatch_actions else '无'}
---
*由安全智能体系统自动生成*
"""
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        return f"报告已生成: {fname}"

    def log(self, event) -> str:
        """简单日志记录"""
        log_path = os.path.join(self.report_dir, "event_log.txt")
        line = f"[{event.timestamp}] {'; '.join(e['type'] for e in event.events)} | LLM: {bool(event.llm_analysis)}\n"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
        return "日志已追加"
