"""Local GPU vision worker for RTSP camera safety detection.

The worker consumes only the newest decoded frame from CameraStreamWorker. This keeps
video rendering responsive when inference is slower than the camera frame rate.
"""
import json
import os
import threading
import time

import numpy as np

try:
    import torch
except Exception:
    torch = None

# The competition runtime is intentionally offline. Optional image codecs must not
# trigger package installation attempts while the backend is starting.
os.environ.setdefault("YOLO_AUTOINSTALL", "false")

try:
    from ultralytics import YOLO
except Exception:
    YOLO = None

DEFAULT_CLASS_MAP = {
    "person": 0,
    "worker": 0,
    "head": 5,
    "helmet": 1,
    "hardhat": 1,
    "hard_hat": 1,
    "safety_helmet": 1,
    "vest": 2,
    "safety_vest": 2,
    "reflective_vest": 2,
    "hi_vis_vest": 2,
    "fire": 3,
    "flame": 3,
    "smoke_fire": 3,
    "vehicle": 4,
    "forklift": 4,
    "forktruck": 4,
    "car": 4,
    "truck": 4,
    "bus": 4,
    "motorcycle": 4,
}


def _iou(a, b):
    ax2, ay2 = a["x"] + a["width"], a["y"] + a["height"]
    bx2, by2 = b["x"] + b["width"], b["y"] + b["height"]
    x1, y1 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x2, y2 = min(ax2, bx2), min(ay2, by2)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    intersection = (x2 - x1) * (y2 - y1)
    union = a["width"] * a["height"] + b["width"] * b["height"] - intersection
    return intersection / union if union > 0 else 0.0


class SimpleTracker:
    """Lightweight IoU tracker used to keep event identity stable between frames."""

    def __init__(self, iou_threshold=0.35, max_age_seconds=2.0):
        self.iou_threshold = iou_threshold
        self.max_age_seconds = max_age_seconds
        self._tracks = []
        self._next_id = 1

    def assign(self, objects):
        now = time.monotonic()
        self._tracks = [t for t in self._tracks if now - t["seen_at"] <= self.max_age_seconds]
        for obj in objects:
            rect = obj["posRect"]
            target_type = obj["targetType"]
            best = None
            best_iou = self.iou_threshold
            for track in self._tracks:
                if track["targetType"] != target_type:
                    continue
                score = _iou(rect, track["posRect"])
                if score > best_iou:
                    best, best_iou = track, score
            if best is None:
                best = {"id": self._next_id, "targetType": target_type}
                self._next_id += 1
                self._tracks.append(best)
            best["posRect"] = dict(rect)
            best["seen_at"] = now
            obj["targetId"] = best["id"]
        return objects


class ViolationGate:
    """Require persistent detections before a visual observation becomes an Agent event."""

    def __init__(self, min_hits=3, cooldown_seconds=15.0, stale_seconds=3.0):
        self.min_hits = max(1, int(min_hits))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.stale_seconds = max(0.1, float(stale_seconds))
        self._states = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(event):
        rect = event.get("bbox", {})
        target_id = event.get("targetId")
        if target_id:
            return event.get("type", "unknown"), str(target_id)
        return event.get("type", "unknown"), f"{int(rect.get('x', 0) / 100)}-{int(rect.get('y', 0) / 100)}"

    def filter(self, events):
        now = time.monotonic()
        ready = []
        with self._lock:
            for key in [key for key, value in self._states.items() if now - value["seen_at"] > self.stale_seconds]:
                del self._states[key]
            for event in events:
                key = self._key(event)
                state = self._states.get(key)
                if state is None:
                    state = {"hits": 0, "seen_at": now, "emitted_at": 0.0}
                    self._states[key] = state
                state["hits"] = state["hits"] + 1 if now - state["seen_at"] <= self.stale_seconds else 1
                state["seen_at"] = now
                if state["hits"] >= self.min_hits and now - state["emitted_at"] >= self.cooldown_seconds:
                    state["emitted_at"] = now
                    ready.append(event)
        return ready


class LocalVisionWorker:
    """Run a PPE-capable YOLO model locally and publish normalized detections."""

    def __init__(self, camera_worker, on_detections, model_path, camera_id="camera-01",
                 interval_seconds=0.4, confidence=0.35, image_size=640, device="auto",
                 require_ppe=True, on_frame=None, on_unavailable=None,
                 profile="yolo26"):
        self.camera_worker = camera_worker
        self.on_detections = on_detections
        self.model_path = model_path
        self.camera_id = camera_id
        self.interval_seconds = max(0.05, float(interval_seconds))
        self.confidence = max(0.01, min(0.99, float(confidence)))
        self.image_size = int(image_size)
        self.device = device
        self.require_ppe = require_ppe
        self.on_frame = on_frame
        self.on_unavailable = on_unavailable
        self.profile = str(profile or "yolo26").strip().lower()
        self._model = None
        self._class_map = dict(DEFAULT_CLASS_MAP)
        self._tracker = SimpleTracker()
        self._thread = None
        self._stop = threading.Event()
        self._active = threading.Event()
        self._lock = threading.Lock()
        self._status = "starting"
        self._error = ""
        self._last_frame_id = -1
        self._last_inference_at = 0.0
        self._last_latency_ms = 0.0
        self._inference_fps = 0.0
        self._detections_total = 0
        self._frames_processed = 0
        self._frames_skipped = 0
        self._supported_types = []
        self._consecutive_failures = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="local-vision", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        self._active.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=max(0.1, float(timeout)))

    def activate(self) -> tuple[bool, str]:
        with self._lock:
            if self._model is None or self._status == "degraded":
                return False, self._error or "model_not_ready"
            self._status = "online"
        self._active.set()
        return True, ""

    def deactivate(self):
        self._active.clear()
        with self._lock:
            if self._model is not None and self._status != "degraded":
                self._status = "ready"
        self._publish_frame([], 0, 0, active=False)

    def _load_model(self):
        if self.profile != "yolo26":
            raise RuntimeError(f"unsupported_vision_profile:{self.profile}")
        if YOLO is None:
            raise RuntimeError("ultralytics_not_installed")
        if not self.model_path or not os.path.exists(self.model_path):
            raise RuntimeError(f"ppe_model_not_found:{self.model_path or 'not_configured'}")
        custom_map = os.environ.get("VISION_CLASS_MAP", "").strip()
        if custom_map:
            parsed = json.loads(custom_map)
            self._class_map.update({str(key).lower(): int(value) for key, value in parsed.items()})
        self._model = YOLO(self.model_path)
        names = self._model.names
        names = names.values() if isinstance(names, dict) else names
        self._supported_types = sorted({self._class_map[name.lower()] for name in names if name.lower() in self._class_map})
        required_types = {0, 1, 2} if self.require_ppe else {0}
        missing_types = sorted(required_types - set(self._supported_types))
        if missing_types:
            labels = {0: "person", 1: "helmet", 2: "vest"}
            missing = ",".join(labels[item] for item in missing_types)
            raise RuntimeError(f"ppe_model_missing_required_classes:{missing}")
        if self.device == "auto":
            self.device = "0" if torch and torch.cuda.is_available() else "cpu"
    def _warmup(self):
        """Allocate the inference path before the live stream is armed."""
        sample = np.zeros((self.image_size, self.image_size, 3), dtype=np.uint8)
        self._model.predict(
            sample,
            conf=self.confidence,
            imgsz=self.image_size,
            device=self.device,
            verbose=False,
        )

    def _run(self):
        try:
            self._load_model()
            self._warmup()
            with self._lock:
                self._status = "ready"
                self._error = ""
            print(f"[Vision] 本地YOLO已加载并预热 model={os.path.basename(self.model_path)} device={self.device}")
        except Exception as exc:
            with self._lock:
                self._status = "degraded"
                self._error = str(exc)
            print(f"[Vision] 本地YOLO未启动: {exc}")
            return

        while not self._stop.is_set():
            if not self._active.wait(0.1):
                continue
            if self._stop.is_set():
                break
            frame, jpeg, frame_id = self.camera_worker.latest_frame_snapshot()
            if frame is None or frame_id == self._last_frame_id:
                time.sleep(0.02)
                continue
            if self._last_frame_id >= 0 and frame_id - self._last_frame_id > 1:
                with self._lock:
                    self._frames_skipped += frame_id - self._last_frame_id - 1
            self._last_frame_id = frame_id
            started = time.monotonic()
            try:
                results = self._model.predict(frame, conf=self.confidence, imgsz=self.image_size,
                                              device=self.device, verbose=False)
                objects = self._normalize_boxes(results[0])
                objects = self._tracker.assign(objects)
                elapsed = time.monotonic() - started
                inference_active = self._active.is_set() and not self._stop.is_set()
                with self._lock:
                    self._frames_processed += 1
                    self._detections_total += len(objects)
                    self._last_inference_at = time.time()
                    self._last_latency_ms = round(elapsed * 1000, 1)
                    self._inference_fps = round(1 / elapsed, 2) if elapsed else 0.0
                    self._status = "online" if inference_active else "ready"
                    self._error = ""
                    self._consecutive_failures = 0
                self._publish_frame(
                    objects,
                    int(frame.shape[1]),
                    int(frame.shape[0]),
                    active=inference_active,
                    frame_id=frame_id,
                )
                self.on_detections({
                    "objInfo": objects,
                    "source": "local_yolo",
                    "cameraId": self.camera_id,
                    "frameId": frame_id,
                    "frameSessionId": (
                        self.camera_worker.evidence_session_id()
                        if hasattr(self.camera_worker, "evidence_session_id") else ""
                    ),
                    "model": os.path.basename(self.model_path),
                    "profile": self.profile,
                }, jpeg)
            except Exception as exc:
                with self._lock:
                    self._status = "degraded"
                    self._error = str(exc)
                    self._consecutive_failures += 1
                    failures = self._consecutive_failures
                print(f"[Vision] inference failed: {exc}")
                if failures >= 3:
                    self._active.clear()
                    self._publish_frame([], 0, 0, active=False)
                    if self.on_unavailable:
                        self.on_unavailable(str(exc))
                time.sleep(1.0)
            self._stop.wait(self.interval_seconds)

    def _publish_frame(self, objects, width, height, active, frame_id=None):
        if not self.on_frame:
            return
        self.on_frame({
            "type": "vision_frame",
            "source": "local_yolo",
            "active": bool(active),
            "camera_id": self.camera_id,
            "frame_id": frame_id,
            "frame_width": int(width),
            "frame_height": int(height),
            "model": os.path.basename(self.model_path),
            "profile": self.profile,
            "objects": objects,
            "timestamp": time.time(),
        })

    def _normalize_boxes(self, result):
        objects = []
        names = result.names
        for box in result.boxes:
            class_id = int(box.cls[0].item())
            label = str(names[class_id]).lower()
            target_type = self._class_map.get(label)
            if target_type is None:
                continue
            x1, y1, x2, y2 = (float(value) for value in box.xyxy[0].tolist())
            objects.append({
                "targetType": target_type,
                "classId": class_id,
                "label": label,
                "confidence": int(float(box.conf[0].item()) * 1000),
                "posRect": {
                    "x": round(x1, 1),
                    "y": round(y1, 1),
                    "width": round(max(0.0, x2 - x1), 1),
                    "height": round(max(0.0, y2 - y1), 1),
                },
            })
        return objects

    def status(self):
        cuda_available = bool(torch and torch.cuda.is_available())
        with self._lock:
            return {
                "status": self._status,
                "source": "local_yolo",
                "profile": self.profile,
                "active": self._active.is_set() and not self._stop.is_set(),
                "camera_id": self.camera_id,
                "model_path": self.model_path,
                "model_loaded": self._model is not None,
                "device": self.device,
                "cuda_available": cuda_available,
                "inference_fps": self._inference_fps,
                "last_latency_ms": self._last_latency_ms,
                "last_inference_at": self._last_inference_at,
                "frames_processed": self._frames_processed,
                "frames_skipped": self._frames_skipped,
                "detections_total": self._detections_total,
                "supported_target_types": self._supported_types,
                "error": self._error,
            }
