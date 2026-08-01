"""Runtime configuration for the local-vision safety Agent system."""
from dataclasses import dataclass
import os
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def resolve_executable(value: str) -> Path | None:
    """Resolve an absolute path or a command available on PATH."""
    candidate = Path(str(value or "")).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    discovered = shutil.which(str(value or ""))
    return Path(discovered).resolve() if discovered else None


def _load_dotenv(path: Path) -> None:
    """Load a minimal local .env file without adding a runtime dependency."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name, str(int(default))).strip().lower()
    return value not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    http_port: int
    websocket_port: int
    backend_python: str
    alarm_dir: Path
    database_path: Path
    pending_dir: Path
    report_dir: Path
    execution_dir: Path

    camera_id: str
    camera_rtsp_url: str
    camera_jpeg_quality: int
    camera_reconnect_seconds: float

    vision_enabled: bool
    vision_model_path: Path
    vision_interval_seconds: float
    vision_confidence: float
    vision_image_size: int
    vision_device: str
    vision_require_ppe: bool
    vision_min_hits: int
    vision_event_cooldown_seconds: float
    vision_profile: str

    llm_mode: str
    ollama_model: str
    ollama_url: str
    llm_timeout_seconds: int
    llm_max_inflight: int
    sop_catalog_path: Path
    sop_top_k: int
    sop_min_score: float
    notify_platform: str
    notify_webhook: str
    notify_image_required: bool
    notify_image_check_attempts: int
    notify_image_check_timeout_seconds: float
    public_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(PROJECT_ROOT / ".env")
        return cls(
            http_port=int(os.environ.get("HTTP_PORT", "5000")),
            websocket_port=int(os.environ.get("WEBSOCKET_PORT", "5001")),
            backend_python=os.environ.get("BACKEND_PYTHON", sys.executable),
            alarm_dir=PROJECT_ROOT / os.environ.get("ALARM_DIR", "alarms"),
            database_path=PROJECT_ROOT / os.environ.get("DATABASE_PATH", "data/alarms.db"),
            pending_dir=PROJECT_ROOT / os.environ.get("PENDING_DIR", "data/pending"),
            report_dir=PROJECT_ROOT / os.environ.get("REPORT_DIR", "data/reports"),
            execution_dir=PROJECT_ROOT / os.environ.get("EXECUTION_DIR", "data/executions"),
            camera_id=os.environ.get("CAMERA_ID", "camera-01"),
            camera_rtsp_url=os.environ.get("CAMERA_RTSP_URL", ""),
            camera_jpeg_quality=int(os.environ.get("CAMERA_JPEG_QUALITY", "72")),
            camera_reconnect_seconds=float(os.environ.get("CAMERA_RECONNECT_SECONDS", "2")),
            vision_enabled=_flag("VISION_ENABLED", True),
            vision_model_path=PROJECT_ROOT / os.environ.get(
                "VISION_MODEL_PATH", "models/yolo26n_safety6_demo_best.pt"
            ),
            vision_interval_seconds=float(os.environ.get("VISION_INTERVAL_SECONDS", "0.4")),
            vision_confidence=float(os.environ.get("VISION_CONFIDENCE", "0.35")),
            vision_image_size=int(os.environ.get("VISION_IMAGE_SIZE", "640")),
            vision_device=os.environ.get("VISION_DEVICE", "auto"),
            vision_require_ppe=_flag("VISION_REQUIRE_PPE", True),
            vision_min_hits=int(os.environ.get("VISION_MIN_HITS", "3")),
            vision_event_cooldown_seconds=float(os.environ.get("VISION_EVENT_COOLDOWN_SECONDS", "15")),
            vision_profile=os.environ.get("VISION_PROFILE", "yolo26").strip().lower(),
            llm_mode=os.environ.get("LLM_MODE", "ollama"),
            ollama_model=os.environ.get("OLLAMA_MODEL", "qwen2.5vl:7b"),
            ollama_url=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
            llm_timeout_seconds=int(os.environ.get("LLM_TIMEOUT_SECONDS", "20")),
            llm_max_inflight=max(1, int(os.environ.get("LLM_MAX_INFLIGHT", "2"))),
            sop_catalog_path=PROJECT_ROOT / os.environ.get(
                "SOP_CATALOG_PATH", "knowledge/sop/safety_procedures.json"
            ),
            sop_top_k=max(1, int(os.environ.get("SOP_TOP_K", "3"))),
            sop_min_score=max(0.0, float(os.environ.get("SOP_MIN_SCORE", "3.0"))),
            notify_platform=os.environ.get("NOTIFY_PLATFORM", "dingtalk"),
            notify_webhook=os.environ.get("NOTIFY_WEBHOOK", ""),
            notify_image_required=_flag("NOTIFY_IMAGE_REQUIRED", True),
            notify_image_check_attempts=max(1, int(os.environ.get("NOTIFY_IMAGE_CHECK_ATTEMPTS", "3"))),
            notify_image_check_timeout_seconds=max(1.0, float(os.environ.get("NOTIFY_IMAGE_CHECK_TIMEOUT_SECONDS", "5"))),
            public_url=os.environ.get("PUBLIC_URL", "").rstrip("/"),
        )
