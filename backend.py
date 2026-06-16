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
import uuid
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from PIL import Image, ImageDraw, ImageFont

try:
    import cv2
    HAS_CV2 = True
except Exception:
    cv2 = None
    HAS_CV2 = False

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
CAMERA_RTSP_URL = "rtsp://admin:HBgkjk%402022@10.53.4.81:554/Streaming/Channels/102"
CAMERA_JPEG_QUALITY = int(os.environ.get("CAMERA_JPEG_QUALITY", "72"))
CAMERA_RECONNECT_SECONDS = float(os.environ.get("CAMERA_RECONNECT_SECONDS", "2"))

# ===== LLM 配置 =====
LLM_MODE = "ollama"          # "ollama" = 本地视觉模型, "deepseek" = 云端文本模型
OLLAMA_MODEL = "qwen2.5vl:7b"   # Ollama 视觉模型名称
OLLAMA_URL = "http://localhost:11434/api/generate"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
LLM_TIMEOUT_SECONDS = 20

# ===== 钉钉/企微 Webhook =====
NOTIFY_PLATFORM = "dingtalk"  # "dingtalk" 或 "wecom"
NOTIFY_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=c05b8b809f36037a880a3ba59f1047f835698870f8ca83bb79336547bf010abc"

# ===== 公网穿透（Cpolar/Ngrok）=====
# cpolar http 5000 后，把输出的公网 URL 填到这里
PUBLIC_URL = "https://3f6488c2.r9.cpolar.top"

os.makedirs(ALARM_DIR, exist_ok=True)

_RECENT_EVENTS = []
_RECENT_LOCK = threading.Lock()
_RECENT_LIMIT = 50


def _new_event_id() -> str:
    return "EVT_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _timeline(stage: str, label: str, detail: str = "") -> dict:
    return {
        "stage": stage,
        "label": label,
        "detail": detail,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _remember_event(event_data: dict):
    """保存最近事件，供前端刷新后恢复状态。按 event_id 合并快速告警和LLM结果。"""
    if not isinstance(event_data, dict):
        return
    event_id = event_data.get("event_id") or _new_event_id()
    event_data["event_id"] = event_id
    with _RECENT_LOCK:
        for idx, item in enumerate(_RECENT_EVENTS):
            if item.get("event_id") == event_id:
                merged = dict(item)
                merged.update(event_data)
                if item.get("timeline") or event_data.get("timeline"):
                    seen = set()
                    merged["timeline"] = [
                        step for step in [*(item.get("timeline") or []), *(event_data.get("timeline") or [])]
                        if not ((step.get("stage"), step.get("timestamp"), step.get("detail")) in seen
                                or seen.add((step.get("stage"), step.get("timestamp"), step.get("detail"))))
                    ][-20:]
                _RECENT_EVENTS[idx] = merged
                return
        _RECENT_EVENTS.insert(0, dict(event_data))
        del _RECENT_EVENTS[_RECENT_LIMIT:]


def _recent_events(limit: int = 20) -> list:
    with _RECENT_LOCK:
        return [dict(e) for e in _RECENT_EVENTS[:max(1, min(limit, _RECENT_LIMIT))]]


class CameraStreamWorker:
    """Pull one RTSP stream in the background and expose latest frame as MJPEG."""

    def __init__(self, rtsp_url: str):
        self.rtsp_url = rtsp_url
        self._lock = threading.Lock()
        self._latest_jpeg = b""
        self._latest_at = 0.0
        self._fps = 0.0
        self._online = False
        self._error = "not_configured" if not rtsp_url else ""
        self._started = False
        self._reconnects = 0
        self._frames_total = 0
        self._stream_label = self._infer_stream_label(rtsp_url)
        self._stop = threading.Event()

    @staticmethod
    def _infer_stream_label(rtsp_url: str) -> str:
        if "/Streaming/Channels/102" in rtsp_url:
            return "102 子码流"
        if "/Streaming/Channels/101" in rtsp_url:
            return "101 主码流"
        return "RTSP"

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        if not self.rtsp_url:
            return
        if not HAS_CV2:
            with self._lock:
                self._error = "opencv_unavailable"
            return
        while not self._stop.is_set():
            cap = None
            try:
                cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
                if not cap.isOpened():
                    with self._lock:
                        self._online = False
                        self._error = "open_failed"
                        self._reconnects += 1
                    time.sleep(CAMERA_RECONNECT_SECONDS)
                    continue
                last_tick = time.time()
                frames = 0
                with self._lock:
                    self._online = True
                    self._error = ""
                    self._reconnects += 1
                while not self._stop.is_set():
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        raise RuntimeError("frame_read_failed")
                    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), CAMERA_JPEG_QUALITY])
                    if not ok:
                        continue
                    now = time.time()
                    frames += 1
                    if now - last_tick >= 1.0:
                        fps = frames / (now - last_tick)
                        frames = 0
                        last_tick = now
                    else:
                        fps = self._fps
                    with self._lock:
                        self._latest_jpeg = encoded.tobytes()
                        self._latest_at = now
                        self._fps = fps
                        self._online = True
                        self._error = ""
                        self._frames_total += 1
            except Exception as e:
                with self._lock:
                    self._online = False
                    self._error = str(e)
                    self._reconnects += 1
            finally:
                try:
                    if cap:
                        cap.release()
                except Exception:
                    pass
            time.sleep(CAMERA_RECONNECT_SECONDS)

    def latest(self) -> bytes:
        with self._lock:
            return self._latest_jpeg

    def status(self) -> dict:
        with self._lock:
            age = time.time() - self._latest_at if self._latest_at else None
            source = "configured" if self.rtsp_url else "missing"
            return {
                "status": "online" if self._online and self._latest_jpeg else "offline",
                "source": source,
                "fps": round(self._fps, 1),
                "frame_age": None if age is None else round(age, 2),
                "last_frame_at": datetime.fromtimestamp(self._latest_at).strftime("%Y-%m-%d %H:%M:%S") if self._latest_at else "",
                "error": self._error,
                "reconnects": self._reconnects,
                "frames_total": self._frames_total,
                "stream": self._stream_label,
                "configured": bool(self.rtsp_url),
            }


camera_worker = CameraStreamWorker(CAMERA_RTSP_URL)

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

    perception_agent = None
    safety_agent = None
    dispatch_agent = None
    human_loop_tool = None
    database_tool = None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/camera/status":
            self._send_json(camera_worker.status())
        elif self.path == "/camera/stream":
            self._stream_camera()
        elif self.path == "/health":
            self._send_json(self._health_snapshot())
        elif self.path == "/latest_event":
            events = _recent_events(1)
            self._send_json(events[0] if events else {})
        elif self.path.startswith("/recent_alarms"):
            limit = 20
            if "?" in self.path:
                try:
                    from urllib.parse import parse_qs, urlparse
                    query = parse_qs(urlparse(self.path).query)
                    limit = int(query.get("limit", [20])[0])
                except Exception:
                    limit = 20
            self._send_json({"events": _recent_events(limit)})
        elif self.path.startswith("/alarms/"):
            fname = os.path.basename(self.path)
            fpath = os.path.join(ALARM_DIR, fname)
            if os.path.exists(fpath):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                with open(fpath, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404); self.end_headers()
        elif self.path == "/latest.jpg":
            files = sorted([f for f in os.listdir(ALARM_DIR) if f.endswith('.jpg')],
                           key=lambda x: os.path.getmtime(os.path.join(ALARM_DIR, x)), reverse=True)
            if files:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                with open(os.path.join(ALARM_DIR, files[0]), "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404); self.end_headers()
        elif self.path == "/approval/pending":
            pending = self.human_loop_tool.get_pending() if self.human_loop_tool else []
            self._send_json({"pending": pending})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path in ("/approval/approve", "/approval/reject"):
            self._handle_approval(self.path.rsplit("/", 1)[-1])
            return
        if self.path == "/api/approval":
            self._handle_approval("approve" if "approve" in (self.headers.get("X-Approval-Action") or "") else "reject")
            return
        if self.path != "/alarm":
            self.send_response(404); self.end_headers(); return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length)
            content_type = self.headers.get("Content-Type", "")
            alarm_body = {}
            img_bytes = b""

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

            event = self.perception_agent.process(alarm_body, img_bytes)
            event.event_id = _new_event_id()
            event.lifecycle_status = "analyzing"

            filtered = [e for e in event.events if _should_report(e["type"], e["bbox"])]
            if not filtered:
                self._send_ok(); return
            event.events = filtered

            event.timeline = [
                _timeline("detected", "感知接收", f"{len(filtered)} 项报警进入数字孪生"),
                _timeline("analyzing", "Agent分析", "安全Agent正在进行视觉语义分析"),
            ]

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
                with open(fpath, "wb") as f: f.write(annotated_img)
                base = PUBLIC_URL if "cpolar" in PUBLIC_URL else f"http://10.44.7.147:{LISTEN_PORT}"
                event.image_url = f"{base}/alarms/{fname}"
                print(f"  截图: {fpath}")

            print(f"\n{'=' * 60}")
            print(f"[{event.timestamp}] 报警")
            for e in filtered:
                icon = {"A": "🔴", "B": "🟡", "C": "🟢"}.get(e["level"], "⚪")
                print(f"  {icon} [{e['level']}级] {e['type']}  |  {e['detail']}")
            print(f"{'=' * 60}\n")

            threading.Thread(target=self._agent_pipeline, args=(event,), daemon=True).start()

            base_ws = PUBLIC_URL if "cpolar" in PUBLIC_URL else f"http://localhost:{LISTEN_PORT}"
            ws_data = {
                "type": "alarm",
                "event_id": event.event_id,
                "timestamp": event.timestamp,
                "lifecycle_status": event.lifecycle_status,
                "timeline": event.timeline,
                "image_url": f"{base_ws}/alarms/{fname}" if annotated_img else "",
                "events": [{"type": e["type"], "level": e["level"], "bbox": e["bbox"], "detail": e["detail"], "targetId": e.get("targetId", 0)} for e in filtered]
            }
            _remember_event(ws_data)
            threading.Thread(target=broadcast_event, args=(ws_data,), daemon=True).start()

            self._send_ok()

        except Exception as e:
            print(f"[错误] {e}")
            self.send_response(500); self.end_headers()

    def _agent_pipeline(self, event):
        try: self._do_agent_pipeline(event)
        except Exception as e:
            print(f"[Agent管线] 致命错误: {e}")
            import traceback; traceback.print_exc()
            event.llm_analysis = event.llm_analysis or f"LLM分析异常: {e}"
            event_data = {"type": "alarm_with_llm", "event_id": event.event_id, "timestamp": event.timestamp, "events": [{"type": ev["type"], "level": ev["level"], "bbox": ev["bbox"], "detail": ev["detail"], "targetId": ev.get("targetId", 0)} for ev in event.events], "llm_analysis": event.llm_analysis, "actions": event.dispatch_actions}
            broadcast_event(event_data)

    def _do_agent_pipeline(self, event):
        done = threading.Event()
        def _run_safety():
            try: self.safety_agent.analyze(event)
            finally: done.set()
        threading.Thread(target=_run_safety, daemon=True).start()
        if not done.wait(LLM_TIMEOUT_SECONDS):
            event.llm_analysis = f"【LLM状态】分析超过 {LLM_TIMEOUT_SECONDS}s，系统已启用规则兜底调度。高危事件仍按确定性安全规则执行。"
            event.llm_recommendation = {}
            event.timeline.append(_timeline("llm_timeout", "LLM超时", "启用规则兜底调度"))
        self.dispatch_agent.dispatch(event)
        decision = event.dispatch_decision or {}
        event.lifecycle_status = "pending_approval" if event.approval_status == "pending" else "decided"
        event.timeline.append(_timeline("decided", "调度裁决", f"规则 {decision.get('rule_level','-')} + LLM {decision.get('llm_level','-') or '-'} -> {decision.get('final_level','-') or '-'}"))
        if event.dispatch_actions:
            event.timeline.append(_timeline("tools", "工具执行", "；".join(f"{a.get('tool','')}.{a.get('action','')}" for a in event.dispatch_actions)))
        if event.approval_status == "pending":
            event.timeline.append(_timeline("pending_approval", "等待审批", event.approval_id or "已生成审批工单"))
        event_data = {"type": "alarm_with_llm", "event_id": event.event_id, "timestamp": event.timestamp, "lifecycle_status": event.lifecycle_status, "timeline": event.timeline, "events": [{"type": e["type"], "level": e["level"], "bbox": e["bbox"], "detail": e["detail"], "targetId": e.get("targetId", 0)} for e in event.events], "llm_analysis": event.llm_analysis or "", "llm_recommendation": event.llm_recommendation or {}, "dispatch_decision": event.dispatch_decision or {}, "approval_id": event.approval_id or "", "approval_status": event.approval_status or "auto", "actions": event.dispatch_actions}
        _remember_event(event_data)
        broadcast_event(event_data)

    def _handle_approval(self, action):
        try:
            if action not in {"approve", "reject"}:
                self._send_json({"status": "error", "message": f"unsupported approval action: {action}"}, code=400)
                return
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length) if content_length else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            pending_id = data.get("approval_id") or data.get("pending_id") or data.get("id") or ""
            if not pending_id or not self.human_loop_tool:
                self._send_json({"status": "error", "message": "approval_id missing"}, code=400); return
            order = self.human_loop_tool._load_order(pending_id)
            if not order:
                self._send_json({"status": "error", "message": f"approval order not found: {pending_id}"}, code=404)
                return
            current_status = order.get("status", "pending")
            if current_status != "pending":
                self._send_json({"status": "error", "message": f"approval order already {current_status}: {pending_id}"}, code=409)
                return
            result = self.human_loop_tool.handle(pending_id, action)
            status = "approved" if action == "approve" else "rejected"
            order = self.human_loop_tool._load_order(pending_id) or order
            event_id = data.get("event_id") or order.get("event_id", "")
            operator = data.get("operator") or "frontend"
            comment = data.get("comment") or ""
            detail = f"{operator} {result}" + (f"；备注：{comment}" if comment else "")
            timeline_step = _timeline(status, "人工审批", detail)
            db_persisted = False
            if self.database_tool:
                db_persisted = self.database_tool.update_approval_status(event_id, pending_id, status, timeline_step)
            msg = {
                "type": "approval_result",
                "event_id": event_id,
                "approval_id": pending_id,
                "approval_status": status,
                "lifecycle_status": status,
                "result": result,
                "operator": operator,
                "comment": comment,
                "db_persisted": db_persisted,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "timeline": [timeline_step],
            }
            print(f"[Approval] {pending_id} -> {status} event={event_id or '-'} db={db_persisted}")
            _remember_event(msg)
            broadcast_event(msg)
            self._send_json({"status": "ok", **msg})
        except Exception as e:
            self._send_json({"status": "error", "message": str(e)}, code=500)

    def _stream_camera(self):
        if not CAMERA_RTSP_URL:
            self._send_json({"status": "error", "message": "CAMERA_RTSP_URL not configured"}, code=503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        last_frame = b""
        try:
            while True:
                frame = camera_worker.latest()
                if not frame:
                    time.sleep(0.2)
                    continue
                if frame == last_frame:
                    time.sleep(0.03)
                    continue
                last_frame = frame
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            print(f"[Camera] MJPEG stream closed: {e}")

    def _health_snapshot(self):
        pending = self.human_loop_tool.get_pending() if self.human_loop_tool else []
        db_stats = self.database_tool.get_stats() if self.database_tool else {}
        recent = _recent_events(10)
        return {
            "status": "ok",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "services": {
                "http": {"status": "ok", "port": LISTEN_PORT},
                "websocket": {"status": "ok" if HAS_WS else "disabled", "port": _WS_PORT, "clients": len(_ws_clients)},
                "llm": {"status": "configured" if LLM_MODE else "disabled", "mode": LLM_MODE, "model": OLLAMA_MODEL, "timeout_seconds": LLM_TIMEOUT_SECONDS},
                "database": {"status": "ok" if self.database_tool else "missing", **db_stats},
                "approval": {"status": "ok" if self.human_loop_tool else "missing", "pending": len(pending)},
                "camera": camera_worker.status(),
            },
            "recent_events": len(recent),
            "last_event": recent[0] if recent else {},
            "tools": sorted(list(self.dispatch_agent.tools.keys())) if self.dispatch_agent else [],
        }

    def _send_ok(self): self._send_json({"status": "ok"})
    def _send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    def log_message(self, format, *args): pass


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
    AlarmHandler.human_loop_tool = human_loop
    AlarmHandler.database_tool = db

    print(f"[Agent] 感知 → 安全({LLM_MODE}+记忆) → 调度({len(dispatch.tools)}工具+可信审批) 就绪")
    stats = db.get_stats()
    print(f"[DB] 历史报警: {stats['total']} 条 (A:{stats['A']} B:{stats['B']} 今日:{stats['today']})")

    # ---- 启动服务 ----
    if HAS_WS:
        threading.Thread(target=_run_ws_server, daemon=True).start()
    if CAMERA_RTSP_URL:
        camera_worker.start()
        print("[Camera] RTSP 子码流接入已启动")
    else:
        print("[Camera] 未配置 CAMERA_RTSP_URL，实时视频流禁用")
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), AlarmHandler).serve_forever()


if __name__ == "__main__":
    main()
