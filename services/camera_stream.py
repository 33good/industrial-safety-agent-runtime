"""RTSP camera puller exposed as latest JPEG frames."""
import threading
import time
from datetime import datetime

try:
    import cv2
    HAS_CV2 = True
except Exception:
    cv2 = None
    HAS_CV2 = False


class CameraStreamWorker:
    """Pull one RTSP stream in the background and expose latest frame as MJPEG."""

    def __init__(self, rtsp_url: str, jpeg_quality: int = 72, reconnect_seconds: float = 2.0):
        self.rtsp_url = rtsp_url
        self.jpeg_quality = jpeg_quality
        self.reconnect_seconds = reconnect_seconds
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
                    time.sleep(self.reconnect_seconds)
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
                    ok, encoded = cv2.imencode(
                        ".jpg",
                        frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
                    )
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
            time.sleep(self.reconnect_seconds)

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
