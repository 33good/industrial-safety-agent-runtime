"""RTSP camera puller exposed as latest JPEG frames."""
import threading
import time
import uuid
from collections import deque
from datetime import datetime

try:
    import cv2
    HAS_CV2 = True
except Exception:
    cv2 = None
    HAS_CV2 = False


class CameraStreamWorker:
    """Pull one RTSP stream in the background and expose latest frame as MJPEG."""

    def __init__(self, rtsp_url: str, jpeg_quality: int = 72, reconnect_seconds: float = 2.0,
                 evidence_buffer_size: int = 240):
        self.rtsp_url = rtsp_url
        self.jpeg_quality = jpeg_quality
        self.reconnect_seconds = reconnect_seconds
        self._lock = threading.Lock()
        self._latest_frame = None
        self._latest_jpeg = b""
        self._latest_at = 0.0
        self._fps = 0.0
        self._online = False
        self._error = "not_configured" if not rtsp_url else ""
        self._started = False
        self._reconnects = 0
        self._frames_total = 0
        self._frame_size = (0, 0)
        self._evidence_frames = deque(maxlen=max(8, int(evidence_buffer_size)))
        self._stream_session_id = ""
        self._stream_label = self._infer_stream_label(rtsp_url)
        self._stop = threading.Event()
        self._thread = None
        self._capture = None

    @staticmethod
    def _infer_stream_label(rtsp_url: str) -> str:
        if "/Streaming/Channels/102" in rtsp_url:
            return "102 子码流"
        if "/Streaming/Channels/101" in rtsp_url:
            return "101 主码流"
        return "RTSP"

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._started = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="camera-stream", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0):
        self._stop.set()
        capture = self._capture
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=max(0.1, float(timeout)))
        self._started = False

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
                self._capture = cap
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
                    # A reconnect starts a new evidence namespace.  Old frame
                    # numbers must never be matched to a recovered event.
                    self._stream_session_id = uuid.uuid4().hex
                    self._evidence_frames.clear()
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
                        # Keep the decoded frame for local inference. Consumers receive a copy.
                        self._latest_frame = frame
                        self._latest_jpeg = encoded.tobytes()
                        self._latest_at = now
                        self._fps = fps
                        self._online = True
                        self._error = ""
                        self._frames_total += 1
                        self._evidence_frames.append({
                            "frame_id": self._frames_total,
                            "stream_session_id": self._stream_session_id,
                            "captured_at": now,
                            "image_bytes": encoded.tobytes(),
                        })
                        self._frame_size = (int(frame.shape[1]), int(frame.shape[0]))
            except Exception as e:
                with self._lock:
                    self._online = False
                    self._error = str(e)
                    self._reconnects += 1
            finally:
                self._capture = None
                try:
                    if cap:
                        cap.release()
                except Exception:
                    pass
            time.sleep(self.reconnect_seconds)

    def latest(self) -> bytes:
        with self._lock:
            return self._latest_jpeg

    def latest_frame_snapshot(self):
        """Return the newest decoded frame without opening a second RTSP connection."""
        with self._lock:
            if self._latest_frame is None:
                return None, b"", self._frames_total
            return self._latest_frame.copy(), self._latest_jpeg, self._frames_total

    def evidence_session_id(self) -> str:
        """Return the opaque identity of the current RTSP connection."""
        with self._lock:
            return self._stream_session_id

    def evidence_frames(self, *, anchor_frame_id: int,
                        stream_session_id: str = "", limit: int = 3) -> list[dict]:
        """Return fixed-policy frames around an inference frame from memory only.

        The caller cannot supply paths, URLs, or arbitrary offsets. Target offsets
        are deliberately fixed so model text cannot expand filesystem or network
        access. Returned JPEG bytes are immutable copies/references and are not
        written to SQLite.
        """
        anchor = int(anchor_frame_id)
        session_id = str(stream_session_id or "")
        limit = max(1, min(5, int(limit)))
        with self._lock:
            current_session_id = self._stream_session_id
            rows = [dict(row) for row in self._evidence_frames]
        if not session_id or session_id != current_session_id:
            return []
        session_rows = [
            row for row in rows
            if str(row.get("stream_session_id") or "") == session_id
        ]
        if not any(int(row.get("frame_id") or 0) == anchor for row in session_rows):
            return []
        # Refuse to substitute unrelated history when the anchor has already
        # fallen out of the ring buffer.  The model sees the truthful offset,
        # but every supplied frame must remain inside this fixed local window.
        candidates = [
            row for row in session_rows
            if int(row["frame_id"]) != anchor
            and abs(int(row["frame_id"]) - anchor) <= 60
        ]
        if not candidates:
            return []

        selected: list[dict] = []
        selected_ids: set[int] = set()
        for target_offset in (-30, -10, 10, 30):
            target = anchor + target_offset
            row = min(candidates, key=lambda item: abs(int(item["frame_id"]) - target))
            frame_id = int(row["frame_id"])
            if frame_id in selected_ids:
                continue
            selected_ids.add(frame_id)
            selected.append({
                **row,
                "offset_frames": frame_id - anchor,
            })
            if len(selected) >= limit:
                break
        selected.sort(key=lambda item: int(item["frame_id"]))
        return selected

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
                "evidence_buffer_frames": len(self._evidence_frames),
                "evidence_buffer_capacity": self._evidence_frames.maxlen,
                "resolution": {"width": self._frame_size[0], "height": self._frame_size[1]},
                "stream": self._stream_label,
                "configured": bool(self.rtsp_url),
            }
