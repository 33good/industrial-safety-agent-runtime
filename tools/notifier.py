"""
通知工具：钉钉 / 企业微信 Webhook 推送
"""
import json
import ssl
import threading
import time
import urllib.request
from urllib.parse import urlparse

try:
    import certifi
except ImportError:
    certifi = None


class NotifierTool:

    def __init__(self, webhook_url: str = "", platform: str = "wecom",
                 image_required: bool = True, image_check_attempts: int = 3,
                 image_check_timeout: float = 5.0):
        self.webhook = webhook_url
        self.platform = platform  # "dingtalk" 或 "wecom"
        self._image_url = ""
        self.image_required = bool(image_required)
        self.image_check_attempts = max(1, int(image_check_attempts))
        self.image_check_timeout = max(1.0, float(image_check_timeout))
        self._lock = threading.Lock()
        self._last_status = {
            "status": "not_sent", "image_verified": False, "image_url": "",
            "attempts": 0, "last_error": "", "last_sent_at": "",
        }

    @staticmethod
    def _https_context():
        """Use a deterministic CA bundle instead of a damaged Windows cert entry."""
        if certifi is not None:
            return ssl.create_default_context(cafile=certifi.where())
        return ssl.create_default_context()

    def _urlopen(self, request, timeout):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        context = self._https_context() if urlparse(url).scheme == "https" else None
        if context is not None:
            return urllib.request.urlopen(request, timeout=timeout, context=context)
        return urllib.request.urlopen(request, timeout=timeout)

    def handle(self, event, action="send"):
        if action == "send_urgent":
            return self._send_markdown(event, urgent=True)
        elif action == "send":
            return self._send_markdown(event, urgent=False)
        return "unknown action"

    def set_image_url(self, url: str):
        """设置报警截图 URL，推送时附带"""
        self._image_url = url

    def _send_markdown(self, event, urgent: bool = False) -> str:
        """推送结构化告警到钉钉/企业微信"""
        if not self.webhook:
            self._record_status("blocked", False, "", 0, "webhook_not_configured")
            raise RuntimeError("通知Webhook未配置")
        if "YOUR_KEY" in self.webhook and "YOUR_TOKEN" not in self.webhook:
            print(f"[通知] 模拟推送 (Webhook未配置): {len(event.events)} 个违规事件")
            return "模拟推送成功 (Webhook未配置)"

        types = ", ".join(e["type"] for e in event.events)
        _lv = {"A":3,"B":2,"C":1}
        level = max(event.events, key=lambda e:_lv.get(e.get("level","B"),0)).get("level","B")
        title = f"{'🔴' if level=='A' else '⚠️'} 厂区安全违规告警 [{level}级]"
        llm_text = (event.llm_analysis or "等待大模型分析中...")[:200]
        decision = getattr(event, "dispatch_decision", {}) or {}
        decision_text = decision.get("policy", "规则调度")[:120]
        # 直接从 event 读取图片 URL
        img_url = getattr(event, 'image_url', '') or self._image_url
        attempts = 0
        if self.image_required:
            if not img_url:
                self._record_status("blocked", False, img_url, 0, "evidence_image_url_missing")
                raise RuntimeError("钉钉图片必达模式：报警证据URL缺失，未发送残缺消息")
            ok, attempts, error = self._verify_public_image(img_url)
            if not ok:
                self._record_status("blocked", False, img_url, attempts, error)
                raise RuntimeError(f"钉钉图片必达模式：公网证据图不可访问 ({error})，消息未发送")

        if self.platform == "dingtalk":
            # 统一用 markdown：有图时图片铺满，无图时纯文字
            img_md = f"![现场截图]({img_url})\n\n" if img_url else ""
            text = (
                f"## {title}\n\n"
                f"{img_md}"
                f"**告警类型**: {types}  \n"
                f"**发生时间**: {event.timestamp}  \n"
                f"**大模型分析**: {llm_text}  \n\n"
                f"**调度裁决**: {decision_text}  \n\n"
                f"---  \n"
                f"> 边缘检测与认知系统已介入，请立即前往数字孪生指挥舱审批！"
            )
            payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
        else:
            # 企业微信 Markdown 格式
            bbox = event.events[0].get("bbox", {})
            zone = f"X:{bbox.get('x',0):.0f} Y:{bbox.get('y',0):.0f}"
            color = "warning" if level == "A" else "comment"
            text = (
                f"# {title}\n"
                f"> 告警等级: <font color=\"{color}\">{level}级</font>\n"
                f"> 告警类型: <font color=\"comment\">{types}</font>\n"
                f"> 发生时间: {event.timestamp}\n"
                f"> 空间防区: [{zone}]\n"
                f"\n**🧠 大模型态势认知**:\n{llm_text}\n"
                f"\n**可信调度裁决**:\n{decision_text}\n"
                f"\n---\n⚠️ 请安全员立即前往数字孪生指挥舱审批！"
            )
            payload = {"msgtype": "markdown", "markdown": {"content": text}}

        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.webhook, data=body,
                                     headers={"Content-Type": "application/json"})
        with self._urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode())
        if int(result.get("errcode", 0) or 0) != 0:
            error = f"webhook_rejected:{result.get('errcode')}:{result.get('errmsg', '')}"
            self._record_status("failed", True, img_url, attempts if self.image_required else 0, error)
            raise RuntimeError(error)
        platform_name = "钉钉" if self.platform == "dingtalk" else "企微"
        self._record_status("sent", bool(img_url), img_url, attempts if self.image_required else 0, "")
        return f"已推送到{platform_name}: {result.get('errmsg', 'ok')}"

    def status(self) -> dict:
        with self._lock:
            return {
                "configured": bool(self.webhook),
                "platform": self.platform,
                "image_required": self.image_required,
                **self._last_status,
            }

    def _verify_public_image(self, url: str) -> tuple[bool, int, str]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False, 0, "invalid_public_image_url"
        if parsed.hostname in {"localhost", "127.0.0.1", "0.0.0.0"}:
            return False, 0, "image_url_is_local_only"

        last_error = "unknown"
        for attempt in range(1, self.image_check_attempts + 1):
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "DigitalTwin-Evidence-Probe/1.0", "Accept": "image/*"},
                )
                with self._urlopen(req, timeout=self.image_check_timeout) as resp:
                    status = int(getattr(resp, "status", 200))
                    content_type = str(resp.headers.get("Content-Type", "")).split(";", 1)[0].lower()
                    prefix = resp.read(16)
                is_image = content_type in {"image/jpeg", "image/jpg", "image/png"}
                valid_magic = prefix.startswith(b"\xff\xd8\xff") or prefix.startswith(b"\x89PNG\r\n\x1a\n")
                if status == 200 and is_image and valid_magic:
                    return True, attempt, ""
                last_error = f"invalid_image_response:http={status},type={content_type or '-'}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}:{exc}"
            if attempt < self.image_check_attempts:
                time.sleep(min(2.0, float(attempt)))
        return False, self.image_check_attempts, last_error

    def _record_status(self, status: str, image_verified: bool, image_url: str,
                       attempts: int, error: str) -> None:
        from datetime import datetime
        with self._lock:
            self._last_status = {
                "status": status,
                "image_verified": image_verified,
                "image_url": image_url,
                "attempts": attempts,
                "last_error": error,
                "last_sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S") if status == "sent" else "",
            }
