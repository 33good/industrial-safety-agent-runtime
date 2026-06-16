"""
研电赛 - 智能安全监控后端
海康盒子 → 感知Agent → 安全Agent(LLM) → 调度Agent → 工具执行 → 3D前端
"""
import json
import base64
import os
import sys
import time
import threading
import io
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from PIL import Image, ImageDraw, ImageFont

# agent 模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agents.perception import PerceptionAgent
from agents.safety_agent import SafetyAgent
from agents.dispatch import DispatchAgent
from agents.memory import MemoryModule
from tools.database import DatabaseTool
from tools.notifier import NotifierTool
from tools.reporter import ReporterTool
from tools.human_loop import HumanLoopTool

# ===== 配置 =====
LISTEN_PORT = 5000
ALARM_DIR = "./alarms"

# ===== LLM 配置 =====
LLM_MODE = "ollama"          # "ollama" = 本地视觉模型, "deepseek" = 云端文本模型
OLLAMA_MODEL = "qwen2.5vl:7b"   # Ollama 视觉模型名称
OLLAMA_URL = "http://localhost:11434/api/generate"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# ===== 钉钉/企微 Webhook =====
NOTIFY_PLATFORM = "dingtalk"  # "dingtalk" 或 "wecom"
NOTIFY_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK", "")

# ===== 公网穿透（Cpolar/Ngrok）=====
# cpolar http 5000 后，把输出的公网 URL 填到这里
PUBLIC_URL = "https://3f6488c2.r9.cpolar.top"

os.makedirs(ALARM_DIR, exist_ok=True)

# ===== 去重冷却 =====
COOLDOWN = {"未戴安全帽": 5, "未穿反光背心": 5, "火焰检测": 3, "车辆检测": 10}
_last_alarm = {}

TYPE_NAMES = {0: "人员", 1: "安全帽", 2: "反光背心", 3: "火焰", 4: "车辆", 5: "头部"}
COLORS = {"未戴安全帽": (255, 50, 50), "未穿反光背心": (255, 165, 0),
          "火焰检测": (255, 0, 0), "车辆检测": (50, 50, 255)}

# 尝试加载中文字体
_FONT = None
for _fp in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"]:
    try:
        _FONT = ImageFont.truetype(_fp, 20)
        break
    except Exception:
        pass


def overlap_area(a, b):
    x1, y1 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x2, y2 = min(a["x"] + a["width"], b["x"] + b["width"]), min(a["y"] + a["height"], b["y"] + b["height"])
    if x1 >= x2 or y1 >= y2:
        return 0.0
    return (x2 - x1) * (y2 - y1) / (b["width"] * b["height"]) if b["width"] * b["height"] > 0 else 0.0


def in_upper_half(person, obj):
    cx, cy = obj["x"] + obj["width"] / 2, obj["y"] + obj["height"] / 2
    return (cx >= person["x"] and cx <= person["x"] + person["width"] and
            cy >= person["y"] and cy <= person["y"] + person["height"] * 0.6)


def analyze_frame(obj_list):
    """分析一帧，返回违规事件列表"""
    persons = [o for o in obj_list if o["targetType"] == 0]
    helmets = [o for o in obj_list if o["targetType"] == 1]
    vests = [o for o in obj_list if o["targetType"] == 2]
    fires = [o for o in obj_list if o["targetType"] == 3]
    bikes = [o for o in obj_list if o["targetType"] == 4]
    events = []

    for person in persons:
        rect = person["posRect"]
        conf = person["confidence"] / 10.0
        has_helmet = any(in_upper_half(rect, h["posRect"]) for h in helmets)
        has_vest = any(overlap_area(rect, v["posRect"]) > 0.3 for v in vests)
        if not has_helmet:
            events.append({"type": "未戴安全帽", "level": "B", "detail": f"人员未佩戴安全帽，置信度 {conf:.1f}%", "bbox": rect})
        if not has_vest:
            events.append({"type": "未穿反光背心", "level": "B", "detail": f"人员未穿反光背心，置信度 {conf:.1f}%", "bbox": rect})

    for fire in fires:
        conf = fire["confidence"] / 10.0
        events.append({"type": "火焰检测", "level": "A", "detail": f"检测到火焰，置信度 {conf:.1f}%", "bbox": fire["posRect"]})

    for bike in bikes:
        conf = bike["confidence"] / 10.0
        events.append({"type": "车辆检测", "level": "C", "detail": f"检测到车辆，置信度 {conf:.1f}%", "bbox": bike["posRect"]})

    return events


def _should_report(violation_type, bbox):
    zone = f"{int(bbox['x']/100)}-{int(bbox['y']/100)}"
    key = (violation_type, zone)
    now = time.time()
    if key in _last_alarm and now - _last_alarm[key] < COOLDOWN.get(violation_type, 5):
        return False
    _last_alarm[key] = now
    return True


def annotate_image(img_bytes, events):
    """在图片上画框和标签"""
    img = Image.open(io.BytesIO(img_bytes))
    draw = ImageDraw.Draw(img)

    for e in events:
        r = e["bbox"]
        x1, y1 = r["x"], r["y"]
        x2, y2 = r["x"] + r["width"], r["y"] + r["height"]
        color = COLORS.get(e["type"], (0, 255, 0))

        # 画框
        for t in range(3):  # 加粗
            draw.rectangle([x1 - t, y1 - t, x2 + t, y2 + t], outline=color)

        # 画标签
        label = f"{e['type']} {e['level']}级"
        tw = draw.textbbox((0, 0), label, font=_FONT)[2] if _FONT else len(label) * 12
        draw.rectangle([x1 - 1, y1 - 24, x1 + tw + 5, y1], fill=color)
        draw.text((x1 + 2, y1 - 23), label, fill=(255, 255, 255), font=_FONT)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


class AlarmHandler(BaseHTTPRequestHandler):

    # 由 main() 注入的全局 agent/tool 实例
    perception_agent = None
    safety_agent = None
    dispatch_agent = None

    def do_GET(self):
        """提供最新报警图片访问"""
        if self.path.startswith("/alarms/"):
            fname = os.path.basename(self.path)
            fpath = os.path.join(ALARM_DIR, fname)
            if os.path.exists(fpath):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                with open(fpath, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path == "/latest.jpg":
            # 找最新截图
            files = sorted([f for f in os.listdir(ALARM_DIR) if f.endswith('.jpg')],
                           key=lambda x: os.path.getmtime(os.path.join(ALARM_DIR, x)), reverse=True)
            if files:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                with open(os.path.join(ALARM_DIR, files[0]), "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/alarm":
            self.send_response(404)
            self.end_headers()
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length)
            content_type = self.headers.get("Content-Type", "")
            alarm_body = {}
            img_bytes = b""

            # 解析 multipart
            if "multipart/form-data" in content_type:
                boundary = content_type.split("boundary=")[1].strip().strip('"')
                for part in raw_body.split(("--" + boundary).encode()):
                    if b'name="body"' in part:
                        h = part.find(b"\r\n\r\n")
                        if h >= 0:
                            alarm_body = json.loads(part[h + 4:].rstrip(b"\r\n--").decode("utf-8"))
                    elif b'name="image"' in part:
                        h = part.find(b"\r\n\r\n")
                        if h >= 0:
                            img_bytes = part[h + 4:].rstrip(b"\r\n--")
            else:
                data = json.loads(raw_body.decode("utf-8"))
                alarm_body = data.get("body", {})
                img_bytes = base64.b64decode(data.get("image_base64", ""))

            # ---- Agent 管线 ----
            # Step 1: 感知Agent - 解析 + 违规判断
            event = self.perception_agent.process(alarm_body, img_bytes)

            # Step 2: 过滤去重
            filtered = [e for e in event.events if _should_report(e["type"], e["bbox"])]
            if not filtered:
                self._send_ok()
                return
            event.events = filtered

            # Step 3: 图片标注 + 保存
            annotated_img = img_bytes
            if img_bytes:
                try:
                    annotated_img = annotate_image(img_bytes, filtered)
                except Exception as e:
                    print(f"[标注] 画框失败: {e}")
            event.image_bytes = annotated_img

            if annotated_img:
                fname = datetime.now().strftime("alarm_%Y%m%d_%H%M%S_%f.jpg")
                fpath = os.path.join(ALARM_DIR, fname)
                with open(fpath, "wb") as f:
                    f.write(annotated_img)
                # 设置图片 URL（手机和电脑同局域网时用电脑 IP）
                base = PUBLIC_URL if "cpolar" in PUBLIC_URL else f"http://10.44.7.147:{LISTEN_PORT}"
                event.image_url = f"{base}/alarms/{fname}"
                print(f"  截图: {fpath}")

            # Step 4: 打印
            print(f"\n{'=' * 60}")
            print(f"[{event.timestamp}] 报警")
            for e in filtered:
                icon = {"A": "🔴", "B": "🟡", "C": "🟢"}.get(e["level"], "⚪")
                print(f"  {icon} [{e['level']}级] {e['type']}  |  {e['detail']}")
            print(f"{'=' * 60}\n")

            # Step 5+6: 异步——安全Agent分析 + 调度Agent决策 + 工具执行
            threading.Thread(target=self._agent_pipeline, args=(event,), daemon=True).start()

            # Step 7: WebSocket 广播到 3D 前端（含图片URL）
            base_ws = PUBLIC_URL if "cpolar" in PUBLIC_URL else f"http://localhost:{LISTEN_PORT}"
            ws_data = {
                "timestamp": event.timestamp,
                "image_url": f"{base_ws}/alarms/{fname}" if annotated_img else "",
                "events": [{"type": e["type"], "level": e["level"],
                            "bbox": e["bbox"], "detail": e["detail"],
                            "targetId": e.get("targetId", 0)} for e in filtered]
            }
            threading.Thread(target=broadcast_event, args=(ws_data,), daemon=True).start()

            self._send_ok()

        except Exception as e:
            print(f"[错误] {e}")
            self.send_response(500)
            self.end_headers()

    def _agent_pipeline(self, event):
        """异步执行：安全分析 → 调度决策 → 工具执行 → 推送结果"""
        # LLM 分析
        self.safety_agent.analyze(event)
        # 调度执行
        self.dispatch_agent.dispatch(event)
        # 推送报警事件 + LLM 分析到前端（一起发，确保收到）
        event_data = {
            "type": "alarm_with_llm",
            "timestamp": event.timestamp,
            "events": [{"type": e["type"], "level": e["level"],
                        "bbox": e["bbox"], "detail": e["detail"],
                        "targetId": e.get("targetId", 0)} for e in event.events],
            "llm_analysis": event.llm_analysis or "",
            "llm_recommendation": event.llm_recommendation or {},
            "dispatch_decision": event.dispatch_decision or {},
            "actions": event.dispatch_actions
        }
        broadcast_event(event_data)

    def _send_ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok"}).encode())

    def log_message(self, format, *args):
        pass


# ===== WebSocket 广播 =====
import asyncio
import queue
try:
    import websockets
    HAS_WS = True
except ImportError:
    HAS_WS = False

_WS_PORT = 5001
_broadcast_queue = queue.Queue()  # 线程安全队列
_ws_clients = set()


def broadcast_event(event_data):
    """将报警事件放入广播队列（JSON 格式）"""
    _broadcast_queue.put(json.dumps(event_data, ensure_ascii=False))


async def _ws_handler(websocket):
    """WebSocket 客户端连接处理"""
    _ws_clients.add(websocket)
    print(f"[WS] 前端已连接，当前连接数: {len(_ws_clients)}")
    try:
        await websocket.wait_closed()
    finally:
        _ws_clients.discard(websocket)
        print(f"[WS] 前端已断开，当前连接数: {len(_ws_clients)}")


async def _broadcast_loop():
    """把队列中的事件广播给所有已连接前端，避免多个页面抢消息。"""
    while True:
        try:
            msg = _broadcast_queue.get(timeout=0.1)
        except queue.Empty:
            await asyncio.sleep(0.05)
            continue

        if not _ws_clients:
            print("[WS] 无前端连接，丢弃一条待推送事件")
            continue

        print(f"[WS] 广播事件到 {len(_ws_clients)} 个前端")
        dead = []
        for client in list(_ws_clients):
            try:
                await client.send(msg)
            except Exception:
                dead.append(client)
        for client in dead:
            _ws_clients.discard(client)


async def _start_ws():
    """启动 WebSocket 服务器"""
    server = await websockets.serve(_ws_handler, "0.0.0.0", _WS_PORT)
    print(f"[WS] WebSocket 广播端口: {_WS_PORT}")
    asyncio.create_task(_broadcast_loop())
    await asyncio.Future()  # 永久运行


def _run_ws_server():
    """在独立线程中启动 WebSocket 服务"""
    if not HAS_WS:
        print("[WS] websockets 库未安装，跳过")
        return
    asyncio.run(_start_ws())


def main():
    # 先清理可能残留的端口占用
    import socket
    for p in [LISTEN_PORT, _WS_PORT]:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('0.0.0.0', p))
            s.close()
        except OSError:
            print(f"[Warn] 端口 {p} 被占用，请手动关闭旧进程: taskkill /F /IM python.exe")
            s.close()

    print(f"""
╔══════════════════════════════════════════╗
║     研电赛 - 智能安全监控后端           ║
║  报警接收: {LISTEN_PORT}  截图: {ALARM_DIR}         ║
║  3D推送: {_WS_PORT}  前端: frontend/index.html   ║
╚══════════════════════════════════════════╝
""")

    # ---- 初始化智能体系统 ----
    print("[Agent] 初始化智能体管线...")

    # 记忆模块
    memory = MemoryModule("./data/alarms.db")

    # 工具
    db = DatabaseTool("./data/alarms.db")
    human_loop = HumanLoopTool("./data/pending")
    notifier = NotifierTool(webhook_url=NOTIFY_WEBHOOK, platform=NOTIFY_PLATFORM)
    reporter = ReporterTool("./data/reports")

    # 智能体（安全Agent包含记忆模块）
    perception = PerceptionAgent()
    safety = SafetyAgent(mode=LLM_MODE, model=OLLAMA_MODEL, memory=memory)
    dispatch = DispatchAgent()

    # 注册工具到调度Agent（含人机协同审批）
    dispatch.register_tool("human_loop", lambda e, a: human_loop.handle(e, a))
    dispatch.register_tool("database", lambda e, a: db.handle(e, a))
    dispatch.register_tool("notifier", lambda e, a: notifier.handle(e, a))
    dispatch.register_tool("reporter", lambda e, a: reporter.handle(e, a))

    # 注入到 HTTP Handler
    AlarmHandler.perception_agent = perception
    AlarmHandler.safety_agent = safety
    AlarmHandler.dispatch_agent = dispatch

    print(f"[Agent] 感知 → 安全({LLM_MODE}+记忆) → 调度({len(dispatch.tools)}工具+可信审批) 就绪")
    stats = db.get_stats()
    print(f"[DB] 历史报警: {stats['total']} 条 (A:{stats['A']} B:{stats['B']} 今日:{stats['today']})")

    # ---- 启动服务 ----
    if HAS_WS:
        threading.Thread(target=_run_ws_server, daemon=True).start()
    HTTPServer(("0.0.0.0", LISTEN_PORT), AlarmHandler).serve_forever()


if __name__ == "__main__":
    main()
