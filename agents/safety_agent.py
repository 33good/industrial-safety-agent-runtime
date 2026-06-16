"""
安全分析Agent：调用 LLM（本地 Qwen2.5-VL / 云端 DeepSeek）进行深度分析
"""
import json
import base64
import os
import re
import urllib.request
from . import AlarmEvent, BaseAgent


class SafetyAgent(BaseAgent):
    """违规事件 → 记忆检索 → LLM 安全分析"""

    def __init__(self, mode: str = "ollama", model: str = "qwen2.5vl:7b",
                 memory=None):
        super().__init__("安全Agent")
        self.mode = mode
        self.model = model
        self.memory = memory  # MemoryModule 实例

    def analyze(self, event: AlarmEvent) -> str:
        """调用 LLM 分析（含记忆上下文），返回分析文本"""
        try:
            # 第一步：检索记忆上下文
            context_text = ""
            if self.memory and event.events:
                bbox = event.events[0].get("bbox", {})
                ctx = self.memory.get_context(bbox)
                context_text = ctx["context_text"]
                if ctx["escalated"]:
                    self.log(f"⚠️ 连续违规区域 {ctx['zone']}: 近1小时 {ctx['zone_count']} 次")
                    # 升级等级
                    for e in event.events:
                        if e["level"] == "B":
                            e["level"] = "A"
                            e["detail"] += f" [自动升级: 区域{ctx['zone_count']}次连续违规]"

            # 第二步：LLM 分析
            if self.mode == "ollama":
                result = self._call_ollama(event, context_text)
            else:
                result = self._call_deepseek(event, context_text)

            recommendation = self._parse_recommendation(result)
            event.llm_recommendation = recommendation
            event.llm_analysis = self._format_analysis(result, recommendation)
            if recommendation:
                self.log(
                    f"结构化建议: {recommendation.get('risk_level', '-')}"
                    f" | {', '.join(recommendation.get('recommended_actions', [])) or '无动作'}"
                )
            self.log(f"分析完成 ({len(event.llm_analysis or '')} 字)")
            return result
        except Exception as e:
            self.log(f"调用失败: {e}")
            return f"LLM 调用失败: {e}"

    def _call_ollama(self, event: AlarmEvent, context_text: str = "") -> str:
        """本地 Qwen2.5-VL 视觉分析（含记忆上下文）"""
        event_desc = "; ".join(f"{e['type']}({e['level']}级)" for e in event.events)
        img_b64 = base64.b64encode(event.image_bytes).decode("utf-8") if event.image_bytes else ""

        context_part = f"\n【历史背景】{context_text}" if context_text else ""
        prompt = (
            "你是工厂安全监控AI。请查看监控报警截图，并基于检测事件与历史背景给出结构化风险建议。"
            f"检测到: {event_desc}。{context_part}\n"
            "只输出JSON，不要输出Markdown。字段如下:\n"
            "{"
            "\"summary\":\"不超过80字的现场态势概述\","
            "\"risk_level\":\"A或B或C\","
            "\"risk_reason\":\"等级理由\","
            "\"recommended_actions\":[\"human_loop.check\",\"database.store\",\"notifier.send\",\"notifier.send_urgent\",\"reporter.log\",\"reporter.generate\"],"
            "\"need_human_confirm\":true或false,"
            "\"confidence\":0到1之间的小数"
            "}\n"
            "安全约束: 不确定时不得建议降级；涉及火焰、区域入侵、人车接近、连续违规时建议A级并需要人工确认。"
        )

        body = json.dumps({
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": prompt,
                "image": [img_b64] if img_b64 else [],
                "images": [img_b64] if img_b64 else []
            }],
            "stream": False
        }).encode()

        req = urllib.request.Request("http://localhost:11434/api/chat",
                                     data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode()).get("message", {}).get("content", "无响应")

    def _call_deepseek(self, event: AlarmEvent, context_text: str = "") -> str:
        """云端 DeepSeek 文本分析（含记忆上下文）"""
        event_desc = "\n".join(
            f"- {e['type']}({e['level']}级): {e['detail']}" for e in event.events
        )
        context_part = f"\n【历史背景】{context_text}" if context_text else ""
        prompt = (
            f"你是工厂安全生产AI专家。检测到以下异常:\n{event_desc}{context_part}\n"
            "请只输出JSON，不要输出Markdown。字段: "
            "summary, risk_level(A/B/C), risk_reason, recommended_actions, "
            "need_human_confirm, confidence。"
            "recommended_actions只能从human_loop.check,database.store,notifier.send,"
            "notifier.send_urgent,reporter.log,reporter.generate中选择。"
        )
        body = json.dumps({
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "你是工厂安全专家，回答简洁专业。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7, "max_tokens": 250
        }).encode()

        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise RuntimeError("DEEPSEEK_API_KEY 未配置")

        req = urllib.request.Request("https://api.deepseek.com/chat/completions",
                                     data=body,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())["choices"][0]["message"]["content"]

    def _parse_recommendation(self, text: str) -> dict:
        """解析 LLM 结构化建议；失败时返回空 dict，保证现有链路不受影响。"""
        raw = (text or "").strip()
        if not raw:
            return {}

        candidates = [raw]
        fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", raw, flags=re.IGNORECASE)
        candidates.extend(s.strip() for s in fenced)
        match = re.search(r"\{[\s\S]*\}", raw)
        if match:
            candidates.append(match.group(0))

        for candidate in candidates:
            try:
                data = json.loads(candidate)
                return self._normalize_recommendation(data)
            except Exception:
                continue
        return {}

    @staticmethod
    def _normalize_recommendation(data: dict) -> dict:
        if not isinstance(data, dict):
            return {}

        allowed_actions = {
            "human_loop.check",
            "database.store",
            "notifier.send",
            "notifier.send_urgent",
            "reporter.log",
            "reporter.generate",
        }
        level = str(data.get("risk_level", "")).upper().strip()
        if level not in {"A", "B", "C"}:
            level = ""

        actions = data.get("recommended_actions", [])
        if isinstance(actions, str):
            actions = [a.strip() for a in re.split(r"[,，;\s]+", actions) if a.strip()]
        actions = [a for a in actions if a in allowed_actions]

        try:
            confidence = float(data.get("confidence", 0))
        except Exception:
            confidence = 0
        confidence = max(0, min(1, confidence))

        need_human = data.get("need_human_confirm", False)
        if isinstance(need_human, str):
            need_human = need_human.strip().lower() in {"true", "yes", "1", "是", "需要"}

        return {
            "summary": str(data.get("summary", "")).strip()[:180],
            "risk_level": level,
            "risk_reason": str(data.get("risk_reason", "")).strip()[:220],
            "recommended_actions": actions,
            "need_human_confirm": bool(need_human),
            "confidence": confidence,
            "raw": data,
        }

    @staticmethod
    def _format_analysis(raw_text: str, recommendation: dict) -> str:
        if not recommendation:
            return raw_text or ""

        actions = recommendation.get("recommended_actions") or []
        confirm = "是" if recommendation.get("need_human_confirm") else "否"
        conf = recommendation.get("confidence", 0)
        parts = [
            f"【态势概述】{recommendation.get('summary') or '无'}",
            f"【建议等级】{recommendation.get('risk_level') or '未给出'}",
            f"【判断依据】{recommendation.get('risk_reason') or '无'}",
            f"【建议动作】{', '.join(actions) if actions else '无'}",
            f"【需人工确认】{confirm}，置信度 {conf:.2f}",
        ]
        return "\n".join(parts)
