"""HTTP entry point for the local-vision industrial safety Agent system."""
import base64
import json
import os
import socket
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from config import Settings
from services.agent_runtime import AgentRuntime
from services.camera_stream import CameraStreamWorker
from services.local_vision import LocalVisionWorker
from services.realtime import RealtimeBroadcaster


class Application:
    """Owns runtime services while keeping HTTP handlers stateless."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.realtime = RealtimeBroadcaster(settings.websocket_port)
        self.runtime = AgentRuntime(settings, self.realtime)
        self.camera = CameraStreamWorker(
            settings.camera_rtsp_url,
            settings.camera_jpeg_quality,
            settings.camera_reconnect_seconds,
        )
        self.vision = None

    def start(self) -> None:
        self.realtime.start()
        recovery = self.runtime.recover_incomplete_runs()
        print(
            "[Recovery] "
            f"audited={recovery['audited']} analysis={recovery['analysis_resumed']} "
            f"tools={recovery['tools_resumed']} manual={recovery['manual_takeover']}"
        )
        if not self.settings.camera_rtsp_url:
            print("[Camera] 未配置 CAMERA_RTSP_URL，实时视频和本地推理已禁用")
            return
        self.camera.start()
        print(f"[Camera] RTSP 接入已启动 id={self.settings.camera_id}")
        if not self.settings.vision_enabled:
            print("[Vision] 本地YOLO推理已禁用 (VISION_ENABLED=0)")
            return
        self.vision = LocalVisionWorker(
            camera_worker=self.camera,
            on_detections=self._ingest_local_detection,
            model_path=str(self.settings.vision_model_path),
            camera_id=self.settings.camera_id,
            interval_seconds=self.settings.vision_interval_seconds,
            confidence=self.settings.vision_confidence,
            image_size=self.settings.vision_image_size,
            device=self.settings.vision_device,
            require_ppe=self.settings.vision_require_ppe,
            on_frame=self._publish_vision_frame,
            on_unavailable=self._handle_vision_unavailable,
            profile=self.settings.vision_profile,
        )
        self.vision.start()
        print(f"[Vision] 本地YOLO推理已请求: {self.settings.vision_model_path}")

    def _ingest_local_detection(self, body: dict, image: bytes) -> dict:
        return self.runtime.ingest_detection(body, image, source="local_yolo")

    def ingest_external_detection(self, body: dict, image: bytes) -> dict:
        return self.runtime.ingest_detection(body, image, source="external")

    def _publish_vision_frame(self, message: dict) -> None:
        payload = dict(message)
        payload["detection_mode"] = "local" if payload.get("active") else "paused"
        self.realtime.publish(payload)

    def _handle_vision_unavailable(self, error: str) -> None:
        print(f"[Vision] 连续推理失败，本地检测已暂停；Agent仍可通过通用事件入口运行: {error}")
        self.realtime.publish({
            "type": "vision_mode",
            "mode": "paused",
            "status": "degraded",
            "reason": error,
        })

    def set_detection_mode(self, mode: str) -> tuple[dict, int]:
        mode = str(mode or "").strip().lower()
        if mode not in {"local", "paused"}:
            return {"status": "error", "message": f"unsupported detection mode: {mode or '-'}"}, 400
        if mode == "local":
            if self.vision is None:
                return {"status": "error", "message": "local vision is disabled"}, 409
            if self.camera.status().get("status") != "online":
                return {"status": "error", "message": "camera stream is not online"}, 409
            ok, error = self.vision.activate()
            if not ok:
                return {"status": "error", "message": error or "local vision is not ready"}, 409
        else:
            if self.vision is not None:
                self.vision.deactivate()
        result = {"status": "ok", **self.vision_status()}
        self.realtime.publish({"type": "vision_mode", **result})
        print(f"[Vision] 实时感知模式 -> {mode}")
        return result, 200

    def vision_status(self) -> dict:
        worker = self.vision.status() if self.vision else {
            "status": "disabled",
            "source": "local_yolo",
            "active": False,
            "error": "vision_disabled",
        }
        return {
            "mode": "local" if worker.get("active") else "paused",
            "accepting": "local_yolo" if worker.get("active") else "external_or_demo",
            "vision": worker,
        }

    def health(self) -> dict:
        runtime = self.runtime.health()
        return {
            "status": "ok",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "services": {
                "http": {"status": "ok", "port": self.settings.http_port},
                "websocket": self.realtime.status(),
                "llm": runtime["llm"],
                "notifier": runtime["notifier"],
                "database": runtime["database"],
                "approval": runtime["approval"],
                "actuator": runtime["actuator"],
                "camera": self.camera.status(),
                "vision": self.vision.status() if self.vision else {"status": "disabled", "source": "local_yolo"},
                "detection": self.vision_status(),
            },
            "recent_events": runtime["recent_events"],
            "last_event": runtime["last_event"],
            "tools": runtime["tools"],
        }


class ApiHandler(BaseHTTPRequestHandler):
    app: Application | None = None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Approval-Action")
        self.end_headers()

    def do_GET(self):
        app = self._app()
        path = urlparse(self.path).path
        if path == "/camera/status":
            return self._send_json(app.camera.status())
        if path == "/vision/status":
            return self._send_json(app.vision_status())
        if path == "/camera/stream":
            return self._stream_camera(app)
        if path == "/health":
            return self._send_json(app.health())
        if path == "/latest_event":
            events = app.runtime.recent_events(1)
            return self._send_json(events[0] if events else {})
        if path == "/recent_alarms":
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = int(query.get("limit", [20])[0])
            except (TypeError, ValueError):
                limit = 20
            return self._send_json({"events": app.runtime.recent_events(limit)})
        if path == "/approval/pending":
            return self._send_json({"pending": app.runtime.pending_approvals()})
        if path == "/recovery/pending":
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = int(query.get("limit", [50])[0])
            except (TypeError, ValueError):
                limit = 50
            return self._send_json({"pending": app.runtime.pending_recoveries(limit)})
        if path == "/latest.jpg":
            images = sorted(app.settings.alarm_dir.glob("*.jpg"), key=lambda item: item.stat().st_mtime, reverse=True)
            return self._serve_image(images[0] if images else None)
        if path.startswith("/alarms/"):
            return self._serve_image(app.settings.alarm_dir / Path(path).name)
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        app = self._app()
        path = urlparse(self.path).path
        if path in {"/approval/approve", "/approval/reject"}:
            body = self._read_json()
            result, status = app.runtime.approve(path.rsplit("/", 1)[-1], body)
            return self._send_json(result, status)
        if path == "/api/approval":
            action = "approve" if "approve" in self.headers.get("X-Approval-Action", "") else "reject"
            result, status = app.runtime.approve(action, self._read_json())
            return self._send_json(result, status)
        if path == "/demo/trigger":
            body = self._read_json()
            scenario = str(body.get("scenario") or "a_person_vehicle").lower()
            return self._send_json(app.runtime.trigger_demo(scenario))
        if path == "/recovery/resolve":
            result, status = app.runtime.resolve_recovery(self._read_json())
            return self._send_json(result, status)
        if path == "/vision/mode":
            result, status = app.set_detection_mode(self._read_json().get("mode", ""))
            return self._send_json(result, status)
        if path == "/alarm":
            try:
                body, image = self._read_alarm_payload()
                return self._send_json(app.ingest_external_detection(body, image))
            except Exception as exc:
                print(f"[报警接入] {exc}")
                return self._send_json({"status": "error", "message": str(exc)}, 500)
        self.send_response(404)
        self.end_headers()

    def _app(self) -> Application:
        if self.app is None:
            raise RuntimeError("application not initialized")
        return self.app

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _read_alarm_payload(self) -> tuple[dict, bytes]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            data = json.loads(raw.decode("utf-8") or "{}")
            return data.get("body", {}), base64.b64decode(data.get("image_base64", ""))

        boundary = content_type.split("boundary=", 1)[1].strip().strip('"').encode()
        body, image = {}, b""
        for part in raw.split(b"--" + boundary):
            marker = part.find(b"\r\n\r\n")
            if marker < 0:
                continue
            content = part[marker + 4:].rstrip(b"\r\n-")
            if b'name="body"' in part:
                body = json.loads(content.decode("utf-8"))
            elif b'name="image"' in part:
                image = content
        return body, image

    def _stream_camera(self, app: Application) -> None:
        if not app.settings.camera_rtsp_url:
            return self._send_json({"status": "error", "message": "CAMERA_RTSP_URL not configured"}, 503)
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        last_frame = b""
        try:
            while True:
                frame = app.camera.latest()
                if not frame or frame == last_frame:
                    time.sleep(0.03)
                    continue
                last_frame = frame
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            print(f"[Camera] MJPEG stream closed: {exc}")

    def _serve_image(self, path: Path | None) -> None:
        if path is None or not path.exists() or not path.is_file():
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.end_headers()
        self.wfile.write(path.read_bytes())

    def _send_json(self, data: dict, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        return


def _warn_if_port_busy(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("0.0.0.0", port))
    except OSError:
        print(f"[Warn] 端口 {port} 已被占用，请先关闭旧服务")
    finally:
        probe.close()


def main() -> None:
    settings = Settings.from_env()
    _warn_if_port_busy(settings.http_port)
    _warn_if_port_busy(settings.websocket_port)
    app = Application(settings)
    ApiHandler.app = app

    print(f"""
╔══════════════════════════════════════════╗
║       工业安全智能体后端                 ║
║  本地推理: RTSP + YOLO  截图: alarms/    ║
║  前端: http://localhost:8080             ║
║  后端: http://localhost:{settings.http_port}                ║
║  WebSocket: ws://localhost:{settings.websocket_port}          ║
╚══════════════════════════════════════════╝
""")
    print("[Agent] 初始化: 感知 -> 安全LLM -> 调度 -> 可信审批 -> 执行回写")
    stats = app.runtime.database.get_stats()
    print(f"[DB] 历史报警: {stats['total']} 条 (A:{stats['A']} B:{stats['B']} 今日:{stats['today']})")
    app.start()
    ThreadingHTTPServer(("0.0.0.0", settings.http_port), ApiHandler).serve_forever()


if __name__ == "__main__":
    main()
