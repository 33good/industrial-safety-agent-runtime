"""HTTP entry point for the local-vision industrial safety Agent system."""
import base64
import json
import os
import signal
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
from services.run_store import IngestConflictError


class Application:
    """Owns runtime services while keeping HTTP handlers stateless."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.bind_host = str(getattr(settings, "bind_host", "127.0.0.1"))
        self.realtime = RealtimeBroadcaster(
            settings.websocket_port, host=self.bind_host
        )
        self.runtime = AgentRuntime(settings, self.realtime)
        self.camera = CameraStreamWorker(
            settings.camera_rtsp_url,
            settings.camera_jpeg_quality,
            settings.camera_reconnect_seconds,
        )
        self.runtime.set_adjacent_frame_provider(self._adjacent_frame_evidence)
        self.vision = None
        self._started = False
        self._started_at = 0.0
        self._vision_arm_stop = threading.Event()
        self._vision_arm_thread = None

    def start(self) -> None:
        self.realtime.start()
        recovery = self.runtime.recover_incomplete_runs()
        self.runtime.start_recovery_monitor()
        print(
            "[Recovery] "
            f"audited={recovery['audited']} analysis={recovery['analysis_resumed']} "
            f"tools={recovery['tools_resumed']} manual={recovery['manual_takeover']}"
        )
        self._started = True
        self._started_at = time.time()
        if not self.settings.camera_rtsp_url:
            print("[Camera] 未配置 CAMERA_RTSP_URL，实时视频和本地推理已禁用")
        else:
            self.camera.start()
            print(f"[Camera] RTSP 接入已启动 id={self.settings.camera_id}")
            if not self.settings.vision_enabled:
                print("[Vision] 本地YOLO推理已禁用 (VISION_ENABLED=0)")
            else:
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
                if bool(getattr(self.settings, "vision_auto_start", True)):
                    self._vision_arm_thread = threading.Thread(
                        target=self._auto_arm_vision,
                        name="vision-auto-arm",
                        daemon=True,
                    )
                    self._vision_arm_thread.start()

    def _auto_arm_vision(self) -> None:
        """Arm inference when both the local model and RTSP dependency are ready."""
        while not self._vision_arm_stop.wait(0.5):
            if self.vision is None:
                return
            vision = self.vision.status()
            if vision.get("status") == "degraded":
                return
            if (
                vision.get("status") == "ready"
                and self.camera.status().get("status") == "online"
            ):
                ok, error = self.vision.activate()
                if ok:
                    print("[Vision] Model and RTSP ready; inference armed automatically")
                    self.realtime.publish({"type": "vision_mode", **self.vision_status()})
                    return
                if error and error != "model_not_ready":
                    print(f"[Vision] Automatic arm failed: {error}")
                    return

    def stop(self) -> None:
        """Stop owned background services in dependency order."""
        self._vision_arm_stop.set()
        if self._vision_arm_thread is not None and self._vision_arm_thread.is_alive():
            self._vision_arm_thread.join(timeout=2.0)
        if self.vision is not None:
            self.vision.stop()
        self.camera.stop()
        drain = self.runtime.shutdown(
            timeout=float(getattr(self.settings, "shutdown_drain_seconds", 10.0))
        )
        if not drain.get("drained", False):
            print(
                "[Runtime] drain timeout; unfinished Runs will be reclaimed by lease: "
                + ",".join(drain.get("active_run_ids") or [])
            )
        self.realtime.stop()
        self._started = False

    def readiness(self) -> dict:
        camera = self.camera.status()
        vision = self.vision.status() if self.vision else {
            "status": "disabled", "error": "vision_disabled"
        }
        websocket = self.realtime.status()
        runtime = self.runtime.lifecycle_status()
        degraded = []
        if self.settings.camera_rtsp_url and camera.get("status") != "online":
            degraded.append("camera")
        if self.settings.vision_enabled and vision.get("status") not in {"ready", "online"}:
            degraded.append("vision")
        core_ready = bool(
            self._started and websocket.get("status") == "ok" and runtime["accepting"]
        )
        return {
            "status": "ready" if core_ready else "starting",
            "ready": core_ready,
            "bind_host": self.bind_host,
            "degraded_dependencies": degraded,
            "camera": camera,
            "vision": vision,
            "websocket": websocket,
            "runtime": runtime,
        }

    def _ingest_local_detection(self, body: dict, image: bytes) -> dict:
        return self.runtime.ingest_detection(body, image, source="local_yolo")

    def _adjacent_frame_evidence(self, *, camera_id: str,
                                 anchor_frame_id: int, stream_session_id: str,
                                 limit: int) -> list[dict]:
        if str(camera_id or "") != str(self.settings.camera_id):
            return []
        return self.camera.evidence_frames(
            anchor_frame_id=anchor_frame_id,
            stream_session_id=stream_session_id,
            limit=limit,
        )

    def ingest_external_detection(self, body: dict, image: bytes,
                                  source_event_id: str = "") -> dict:
        if not source_event_id:
            return self.runtime.ingest_detection(body, image, source="external")
        return self.runtime.ingest_detection(
            body, image, source="external", source_event_id=source_event_id,
        )

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
            "accepting": "local_yolo" if worker.get("active") else "external_event_api",
            "vision": worker,
        }

    def health(self) -> dict:
        runtime = self.runtime.health()
        return {
            "status": "ok",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "lifecycle": {
                "started": self._started,
                "uptime_seconds": round(time.time() - self._started_at, 1)
                if self._started_at else 0.0,
                "bind_host": self.bind_host,
            },
            "services": {
                "http": {"status": "ok", "port": self.settings.http_port},
                "runtime": runtime["runtime"],
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
        origin = self._allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Approval-Action, Idempotency-Key",
        )
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
        if path == "/ready":
            readiness = app.readiness()
            return self._send_json(readiness, 200 if readiness["ready"] else 503)
        if path == "/metrics/runtime":
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = int(query.get("limit", [500])[0])
            except (TypeError, ValueError):
                limit = 500
            return self._send_json(app.runtime.runtime_metrics(limit))
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
        if path.startswith("/traces/"):
            run_id = path.rsplit("/", 1)[-1].strip()
            trace = app.runtime.get_trace(run_id)
            if trace is None:
                return self._send_json({
                    "status": "error", "message": f"run not found: {run_id}",
                }, 404)
            return self._send_json(trace)
        if path == "/latest.jpg":
            images = sorted(app.settings.alarm_dir.glob("*.jpg"), key=lambda item: item.stat().st_mtime, reverse=True)
            return self._serve_image(images[0] if images else None)
        if path.startswith("/alarms/"):
            return self._serve_image(app.settings.alarm_dir / Path(path).name)
        return self._send_json({"status": "error", "message": "not_found"}, 404)

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
        if path == "/recovery/resolve":
            result, status = app.runtime.resolve_recovery(self._read_json())
            return self._send_json(result, status)
        if path == "/vision/mode":
            result, status = app.set_detection_mode(self._read_json().get("mode", ""))
            return self._send_json(result, status)
        if path == "/alarm":
            try:
                body, image = self._read_alarm_payload()
                source_event_id = self._resolve_source_event_id(body)
                result = app.ingest_external_detection(
                    body, image, source_event_id=source_event_id
                )
                return self._send_json(
                    result, 503 if result.get("status") == "unavailable" else 200
                )
            except IngestConflictError as exc:
                print(f"[报警接入] 幂等键冲突: {exc}")
                return self._send_json({
                    "status": "error",
                    "error": "idempotency_conflict",
                    "message": str(exc),
                }, 409)
            except ValueError as exc:
                print(f"[报警接入] 请求无效: {exc}")
                return self._send_json({"status": "error", "message": str(exc)}, 400)
            except Exception as exc:
                print(f"[报警接入] {exc}")
                return self._send_json({"status": "error", "message": str(exc)}, 500)
        return self._send_json({"status": "error", "message": "not_found"}, 404)

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

    def _resolve_source_event_id(self, body: dict) -> str:
        header_value = str(self.headers.get("Idempotency-Key", "") or "").strip()
        body_value = str(
            body.get("source_event_id") or body.get("sourceEventId") or ""
        ).strip()
        if header_value and body_value and header_value != body_value:
            raise ValueError("Idempotency-Key and source_event_id must match")
        value = header_value or body_value
        if len(value) > 256:
            raise ValueError("source_event_id must not exceed 256 characters")
        return value

    def _stream_camera(self, app: Application) -> None:
        if not app.settings.camera_rtsp_url:
            return self._send_json({"status": "error", "message": "CAMERA_RTSP_URL not configured"}, 503)
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        origin = self._allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
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
        origin = self._allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _allowed_origin(self) -> str:
        origin = str(self.headers.get("Origin") or "").rstrip("/")
        if not origin:
            return ""
        app = self._app()
        frontend_port = int(getattr(app.settings, "frontend_port", 18080))
        allowed = {
            f"http://127.0.0.1:{frontend_port}",
            f"http://localhost:{frontend_port}",
        }
        return origin if origin in allowed else ""

    def log_message(self, format, *args):
        return


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    settings = Settings.from_env()
    app = Application(settings)
    ApiHandler.app = app
    server = LocalThreadingHTTPServer(
        (settings.bind_host, settings.http_port), ApiHandler
    )
    stopping = threading.Event()

    def request_stop(signum=None, frame=None):
        if stopping.is_set():
            return
        stopping.set()
        print(f"[Runtime] shutdown requested signal={signum or 'internal'}")
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, signal_name, None)
        if signum is not None:
            signal.signal(signum, request_stop)

    print(f"""
╔══════════════════════════════════════════╗
║       工业安全智能体后端                 ║
║  本地推理: RTSP + YOLO  截图: alarms/    ║
║  仅本机绑定: {settings.bind_host}                         ║
║  后端: http://{settings.bind_host}:{settings.http_port}       ║
║  WebSocket: ws://{settings.bind_host}:{settings.websocket_port} ║
╚══════════════════════════════════════════╝
""")
    print("[Agent] 初始化: 感知 -> 安全LLM -> 调度 -> 可信审批 -> 执行回写")
    stats = app.runtime.database.get_stats()
    print(f"[DB] 历史报警: {stats['total']} 条 (A:{stats['A']} B:{stats['B']} 今日:{stats['today']})")
    try:
        app.start()
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        app.stop()
        ApiHandler.app = None
        print("[Runtime] backend stopped cleanly")


if __name__ == "__main__":
    main()
