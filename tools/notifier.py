"""
通知工具：钉钉 / 企业微信 Webhook 推送
"""
import json
import urllib.request


class NotifierTool:

    def __init__(self, webhook_url: str = "", platform: str = "wecom"):
        self.webhook = webhook_url
        self.platform = platform  # "dingtalk" 或 "wecom"
        self._image_url = ""

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
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode())
        platform_name = "钉钉" if self.platform == "dingtalk" else "企微"
        return f"已推送到{platform_name}: {result.get('errmsg', 'ok')}"
