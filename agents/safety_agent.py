"""
安全分析Agent：调用 LLM（本地 Qwen2.5-VL / 云端 DeepSeek）进行深度分析
"""
import json
import base64
import io
import os
import re
import threading
import time
import urllib.request
from . import AlarmEvent, BaseAgent

try:
    from PIL import Image
except ImportError:
    Image = None


class SafetyAgent(BaseAgent):
    """违规事件 → 记忆检索 → LLM 安全分析"""

    PROMPT_VERSION = "safety-v2.2-grounded-sop"

    def __init__(self, mode: str = "ollama", model: str = "qwen2.5vl:7b",
                 memory=None, sop_retriever=None,
                 base_url: str = "http://127.0.0.1:11434", timeout_seconds: int = 20):
        super().__init__("安全Agent")
        self.mode = mode
        self.model = model
        self.memory = memory  # MemoryModule 实例
        self.sop_retriever = sop_retriever
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(1, int(timeout_seconds))
        self._status_lock = threading.Lock()
        self._last_status = {
            "last_inference_status": "not_run", "last_error": "", "last_latency_ms": 0.0,
            "last_call_at": "", "json_valid": False,
        }

    def analyze(self, event: AlarmEvent) -> str:
        """调用 LLM 分析（含记忆上下文），返回分析文本"""
        started = time.perf_counter()
        event.llm_status = "analyzing"
        event.llm_error = ""
        event.llm_model = self.model
        event.prompt_version = self.PROMPT_VERSION
        try:
            # 第一步：检索记忆上下文
            context_text = ""
            memory_context = {}
            if self.memory and event.events:
                bbox = event.events[0].get("bbox", {})
                ctx = self.memory.get_context(bbox)
                memory_context = ctx
                context_text = ctx["context_text"]
                if ctx["escalated"]:
                    self.log(f"⚠️ 连续违规区域 {ctx['zone']}: 近1小时 {ctx['zone_count']} 次")
                    # 升级等级
                    for e in event.events:
                        if e["level"] == "B":
                            e["level"] = "A"
                            e["detail"] += f" [自动升级: 区域{ctx['zone_count']}次连续违规]"

            # SOP evidence is separate from historical memory. A retrieval
            # failure cannot bypass the deterministic rule and tool policies.
            if self.sop_retriever:
                try:
                    event.sop_retrieval = self.sop_retriever.retrieve_event(event)
                except Exception as exc:
                    event.sop_retrieval = {
                        "status": "retrieval_error", "catalog_version": "",
                        "citations": [], "refusal_reason": f"{type(exc).__name__}: {exc}",
                    }
            else:
                event.sop_retrieval = {
                    "status": "disabled", "catalog_version": "",
                    "citations": [], "refusal_reason": "SOP检索未启用",
                }

            # 第二步：LLM 分析
            if self.mode == "ollama":
                result = self._call_ollama(event, context_text, memory_context, event.sop_retrieval)
            else:
                result = self._call_deepseek(event, context_text, event.sop_retrieval)

            allowed_citations = {
                item.get("citation_id"): item
                for item in event.sop_retrieval.get("citations", []) if item.get("citation_id")
            }
            recommendation = self._parse_recommendation(result, allowed_citations)
            event.llm_recommendation = recommendation
            if recommendation.get("sop_citations"):
                event.rag_status = "grounded"
            elif event.sop_retrieval.get("status") == "retrieved":
                event.rag_status = "citation_missing"
            elif event.sop_retrieval.get("status") == "no_evidence":
                event.rag_status = "no_evidence"
            else:
                event.rag_status = event.sop_retrieval.get("status", "not_run")
            event.llm_analysis = self._format_analysis(result, recommendation)
            event.llm_json_valid = bool(recommendation and recommendation.get("risk_level"))
            event.llm_status = "success" if event.llm_json_valid else "invalid_json"
            event.llm_latency_ms = round((time.perf_counter() - started) * 1000, 1)
            if not event.llm_json_valid:
                event.llm_error = "model_output_is_not_valid_structured_json"
            self._remember_status(event)
            if recommendation:
                self.log(
                    f"结构化建议: {recommendation.get('risk_level', '-')}"
                    f" | {', '.join(recommendation.get('recommended_actions', [])) or '无动作'}"
                )
            self.log(f"分析完成 ({len(event.llm_analysis or '')} 字)")
            return result
        except Exception as e:
            self.log(f"调用失败: {e}")
            event.llm_status = "failed"
            event.llm_error = f"{type(e).__name__}: {e}"
            event.llm_latency_ms = round((time.perf_counter() - started) * 1000, 1)
            event.llm_json_valid = False
            event.llm_recommendation = {}
            if event.rag_status == "not_run":
                event.rag_status = event.sop_retrieval.get("status", "not_run")
            event.llm_analysis = f"【LLM状态】图像分析失败：{event.llm_error}；系统已启用规则兜底。"
            self._remember_status(event)
            return event.llm_analysis

    def _call_ollama(self, event: AlarmEvent, context_text: str = "", memory_context: dict | None = None,
                     sop_context: dict | None = None) -> str:
        """本地 Qwen2.5-VL 视觉分析（含记忆上下文）"""
        evidence = self._evidence_payload(event, context_text, memory_context or {}, sop_context or {})
        analysis_image = self._prepare_analysis_image(event.image_bytes)
        img_b64 = base64.b64encode(analysis_image).decode("utf-8") if analysis_image else ""
        prompt = (
            "你是工业安全多模态分析Agent。截图和下方JSON是唯一证据，"
            "不得虚构未提供的人员、设备、距离或处置结果。"
            "请区分可验证事实、风险判断和不确定性，生成简短的受约束处置计划。\n"
            f"【结构化证据】{json.dumps(evidence, ensure_ascii=False)}\n"
            "只输出一个JSON对象，不要Markdown，不要额外文字：\n"
            "{\"summary\":\"不超过60字\","
            "\"observed_facts\":[\"最多4条、每条不超过50字\"],"
            "\"memory_evidence\":[\"最多2条历史依据\"],"
            "\"sop_citations\":[{\"citation_id\":\"只能复制候选引用ID\",\"claim\":\"该引用支持的处置依据\"}],"
            "\"sop_answerable\":true或false,\"sop_refusal_reason\":\"无证据时说明原因\","
            "\"uncertainties\":[\"遮挡/模糊/距离局限，没有则空数组\"],"
            "\"risk_level\":\"A或B或C\",\"risk_reason\":\"简短可审计依据\","
            "\"recommended_actions\":[{\"tool\":\"database\",\"action\":\"store\",\"reason\":\"理由\",\"priority\":1}],"
            "\"need_human_confirm\":true或false,\"confidence\":0.0}\n"
            "工具只能从 human_loop.check,database.store,notifier.send,notifier.send_urgent,"
            "reporter.log,reporter.generate 中选择。不得建议直接控制PLC或跳过人工审批。"
            "tool字段只能填写human_loop/database/notifier/reporter，action字段填写点号后的动作。"
            "候选工具链必须与风险等级一致：A级仅使用human_loop.check,database.store,"
            "notifier.send_urgent,reporter.generate；B级仅使用human_loop.check,database.store,"
            "notifier.send,reporter.log；C级仅使用human_loop.check,database.store。"
            "检测JSON中的rule_level是确定性规则基线：不得降级；只有截图或历史证据明确出现火焰、"
            "区域入侵、人车接近或连续违规时才建议A级。仅有未戴安全帽/未穿反光背心时保持B级，"
            "仅有普通车辆提示时保持C级；不要因一般性安全隐患措辞擅自升级。"
            "need_human_confirm必须与等级一致：A级为true，B/C级为false。"
            "SOP引用只能复制结构化证据sop.citations中的citation_id，不得编造编号、章节或版本。"
            "若sop.status不是retrieved或候选为空，sop_citations必须为空、sop_answerable必须为false，"
            "并在sop_refusal_reason中明确拒绝提供SOP依据；视觉风险判断仍可照常给出。"
        )

        body = json.dumps({
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": prompt,
                "images": [img_b64] if img_b64 else []
            }],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0, "seed": 20260712}
        }).encode()

        req = urllib.request.Request(f"{self.base_url}/api/chat",
                                     data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            return json.loads(resp.read().decode()).get("message", {}).get("content", "无响应")

    @staticmethod
    def _prepare_analysis_image(image_bytes: bytes, max_side: int = 1280) -> bytes:
        """Downscale only the LLM copy; full-resolution evidence remains untouched."""
        if not image_bytes or Image is None:
            return image_bytes or b""
        try:
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            if max(image.size) <= max_side:
                return image_bytes
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=86, optimize=True)
            return output.getvalue()
        except Exception:
            return image_bytes

    @staticmethod
    def _evidence_payload(event: AlarmEvent, context_text: str, memory_context: dict,
                          sop_context: dict | None = None) -> dict:
        detections = []
        for item in event.events[:8]:
            detections.append({
                "type": item.get("type", ""),
                "rule_level": item.get("level", "B"),
                "detail": item.get("detail", ""),
                "target_id": item.get("targetId", 0),
                "confidence": item.get("confidence"),
                "bbox": item.get("bbox", {}),
                "vehicle_bbox": item.get("vehicle_bbox"),
            })
        raw = event.raw_json or {}
        sop_context = sop_context or {}
        citations = []
        for item in sop_context.get("citations", [])[:3]:
            citations.append({
                "citation_id": item.get("citation_id", ""),
                "title": item.get("title", ""),
                "section": item.get("section", ""),
                "version": item.get("version", ""),
                "effective_date": item.get("effective_date", ""),
                "excerpt": item.get("excerpt", ""),
            })
        return {
            "source": raw.get("source", "external"),
            "camera_id": raw.get("cameraId", "camera-01"),
            "timestamp": event.timestamp,
            "detections": detections,
            "memory": {
                "zone": memory_context.get("zone", ""),
                "zone_count": memory_context.get("zone_count", 0),
                "escalated": bool(memory_context.get("escalated", False)),
                "summary": context_text or "无近期相关事件记录",
            },
            "sop": {
                "status": sop_context.get("status", "disabled"),
                "catalog_version": sop_context.get("catalog_version", ""),
                "citations": citations,
                "refusal_reason": sop_context.get("refusal_reason", ""),
            },
        }

    def health(self) -> dict:
        """Verify that Ollama is reachable and the configured model is installed."""
        if self.mode != "ollama":
            return {"status": "configured", "mode": self.mode, "model": self.model, **self.last_status()}
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=2) as resp:
                data = json.loads(resp.read().decode())
            names = {str(item.get("name") or item.get("model") or "") for item in data.get("models", [])}
            installed = self.model in names or any(name.split(":", 1)[0] == self.model.split(":", 1)[0] for name in names)
            probe_status = "ready" if installed else "model_missing"
            probe_error = "" if installed else f"model_not_installed:{self.model}"
        except Exception as exc:
            installed = False
            probe_status = "offline"
            probe_error = f"{type(exc).__name__}: {exc}"
        return {
            "status": probe_status,
            "mode": self.mode,
            "model": self.model,
            "base_url": self.base_url,
            "model_installed": installed,
            "probe_latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "probe_error": probe_error,
            **self.last_status(),
        }

    def last_status(self) -> dict:
        with self._status_lock:
            return dict(self._last_status)

    def _remember_status(self, event: AlarmEvent) -> None:
        from datetime import datetime
        with self._status_lock:
            self._last_status = {
                "last_inference_status": event.llm_status,
                "last_error": event.llm_error,
                "last_latency_ms": event.llm_latency_ms,
                "last_call_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "json_valid": event.llm_json_valid,
            }

    def _call_deepseek(self, event: AlarmEvent, context_text: str = "",
                       sop_context: dict | None = None) -> str:
        """云端 DeepSeek 文本分析（含记忆上下文）"""
        event_desc = "\n".join(
            f"- {e['type']}({e['level']}级): {e['detail']}" for e in event.events
        )
        context_part = f"\n【历史背景】{context_text}" if context_text else ""
        sop_part = "\n【SOP候选】" + json.dumps(sop_context or {}, ensure_ascii=False)
        prompt = (
            f"你是工厂安全生产AI专家。检测到以下异常:\n{event_desc}{context_part}{sop_part}\n"
            "请只输出JSON，不要输出Markdown。字段: "
            "summary, risk_level(A/B/C), risk_reason, recommended_actions, "
            "need_human_confirm, confidence, sop_citations, sop_answerable, sop_refusal_reason。"
            "recommended_actions只能从human_loop.check,database.store,notifier.send,"
            "notifier.send_urgent,reporter.log,reporter.generate中选择。"
            "sop_citations只能引用SOP候选中的citation_id；没有候选时必须拒绝提供SOP依据。"
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

    def _parse_recommendation(self, text: str,
                              allowed_citations: set[str] | dict[str, dict] | None = None) -> dict:
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
                return self._normalize_recommendation(data, allowed_citations)
            except Exception:
                continue
        return {}

    @staticmethod
    def _normalize_recommendation(data: dict,
                                  allowed_citations: set[str] | dict[str, dict] | None = None) -> dict:
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
        if not isinstance(actions, list):
            actions = []
        normalized_actions = []
        action_plan = []
        rejected_candidate_actions = []
        for item in actions:
            if isinstance(item, str):
                name, reason, priority = item.strip(), "", 99
            elif isinstance(item, dict):
                tool = str(item.get("tool", "")).strip().rstrip(".")
                action = str(item.get("action", "")).strip().lstrip(".")
                # Some local models put the complete name in ``tool`` and repeat
                # the suffix in ``action``. Accept that harmless schema drift.
                if tool in allowed_actions:
                    name = tool
                else:
                    name = f"{tool}.{action}" if tool and action else str(item.get("name", "")).strip()
                reason = str(item.get("reason", "")).strip()[:120]
                try:
                    priority = int(item.get("priority", 99))
                except (TypeError, ValueError):
                    priority = 99
            else:
                continue
            if name and name not in allowed_actions:
                rejected_candidate_actions.append(name[:80])
            elif name in allowed_actions and name not in normalized_actions:
                normalized_actions.append(name)
                tool, action = name.split(".", 1)
                action_plan.append({
                    "name": name, "tool": tool, "action": action,
                    "reason": reason, "priority": max(0, min(99, priority)),
                })

        try:
            confidence = float(data.get("confidence", 0))
        except Exception:
            confidence = 0
        confidence = max(0, min(1, confidence))

        need_human = data.get("need_human_confirm", False)
        if isinstance(need_human, str):
            need_human = need_human.strip().lower() in {"true", "yes", "1", "是", "需要"}
        # Keep the explanation consistent with the deterministic human-loop
        # policy: A waits for approval, while B/C pass the policy check.
        if level == "A":
            need_human = True
        elif level in {"B", "C"}:
            need_human = False

        def short_list(name: str, limit: int, item_limit: int) -> list[str]:
            value = data.get(name, [])
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                return []
            return [str(item).strip()[:item_limit] for item in value if str(item).strip()][:limit]

        citation_provenance = allowed_citations if isinstance(allowed_citations, dict) else {}
        allowed_citation_ids = set(citation_provenance) if citation_provenance else set(allowed_citations or set())
        raw_citations = data.get("sop_citations", [])
        if isinstance(raw_citations, (str, dict)):
            raw_citations = [raw_citations]
        if not isinstance(raw_citations, list):
            raw_citations = []
        sop_citations = []
        rejected_sop_citations = []
        seen_citations = set()
        for item in raw_citations:
            if isinstance(item, str):
                citation_id, claim = item.strip(), ""
            elif isinstance(item, dict):
                citation_id = str(item.get("citation_id") or item.get("id") or "").strip()
                claim = str(item.get("claim") or "").strip()[:180]
            else:
                continue
            if not citation_id or citation_id in seen_citations:
                continue
            seen_citations.add(citation_id)
            if citation_id not in allowed_citation_ids:
                rejected_sop_citations.append(citation_id[:100])
                continue
            canonical = citation_provenance.get(citation_id, {})
            sop_citations.append({
                "citation_id": citation_id,
                "claim": claim,
                "document_id": canonical.get("document_id", ""),
                "title": canonical.get("title", ""),
                "section": canonical.get("section", ""),
                "version": canonical.get("version", ""),
                "source": canonical.get("source", ""),
                "excerpt": canonical.get("excerpt", ""),
            })

        sop_answerable = bool(sop_citations)
        refusal_reason = str(data.get("sop_refusal_reason") or "").strip()[:220]
        if not sop_answerable and not refusal_reason:
            refusal_reason = "未提供可验证的SOP引用"

        return {
            "summary": str(data.get("summary", "")).strip()[:180],
            "observed_facts": short_list("observed_facts", 4, 80),
            "memory_evidence": short_list("memory_evidence", 2, 80),
            "uncertainties": short_list("uncertainties", 3, 80),
            "risk_level": level,
            "risk_reason": str(data.get("risk_reason", "")).strip()[:220],
            "recommended_actions": normalized_actions,
            "action_plan": action_plan,
            "rejected_candidate_actions": rejected_candidate_actions[:5],
            "need_human_confirm": bool(need_human),
            "confidence": confidence,
            "sop_citations": sop_citations[:3],
            "rejected_sop_citations": rejected_sop_citations[:5],
            "sop_answerable": sop_answerable,
            "sop_refusal_reason": "" if sop_answerable else refusal_reason,
            "raw": data,
        }

    @staticmethod
    def _format_analysis(raw_text: str, recommendation: dict) -> str:
        if not recommendation:
            return raw_text or ""

        actions = recommendation.get("action_plan") or []
        confirm = "是" if recommendation.get("need_human_confirm") else "否"
        conf = recommendation.get("confidence", 0)
        facts = recommendation.get("observed_facts") or []
        memories = recommendation.get("memory_evidence") or []
        uncertainties = recommendation.get("uncertainties") or []
        sop_citations = recommendation.get("sop_citations") or []
        action_lines = [
            f"{index + 1}. {item.get('name', '')}" + (f"：{item.get('reason')}" if item.get("reason") else "")
            for index, item in enumerate(actions)
        ]
        parts = [
            f"【态势概述】{recommendation.get('summary') or '无'}",
            f"【感知事实】{'；'.join(facts) if facts else '以检测事件与报警截图为据'}",
            f"【历史记忆】{'；'.join(memories) if memories else '无额外历史升级依据'}",
            f"【SOP依据】{'；'.join(item.get('citation_id', '') for item in sop_citations) if sop_citations else recommendation.get('sop_refusal_reason', '无可验证引用')}",
            f"【建议等级】{recommendation.get('risk_level') or '未给出'}",
            f"【判断依据】{recommendation.get('risk_reason') or '无'}",
            f"【不确定性】{'；'.join(uncertainties) if uncertainties else '未发现需额外声明的不确定性'}",
            f"【候选计划】{' '.join(action_lines) if action_lines else '由确定性规则补齐必选工具'}",
            f"【需人工确认】{confirm}，置信度 {conf:.2f}",
        ]
        return "\n".join(parts)
