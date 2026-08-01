"""Read-only competition preflight for the complete demonstration chain."""
import argparse
import json
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from config import Settings, resolve_executable
from tools.notifier import NotifierTool


def report(ok: bool, name: str, detail: str) -> bool:
    print(f"[{'OK' if ok else 'FAIL':<4}] {name:<18} {detail}")
    return ok


def warn(name: str, detail: str) -> None:
    print(f"[WARN] {name:<18} {detail}")


def tcp(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def get_json(url: str, timeout: float = 3.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--startup", action="store_true", help="Skip checks requiring a running backend")
    args = parser.parse_args()
    settings = Settings.from_env()
    checks = []
    already_running = False

    if args.startup:
        ports = (settings.http_port, settings.websocket_port, 8080)
        port_states = {port: tcp("127.0.0.1", port, 0.3) for port in ports}
        if all(port_states.values()):
            try:
                health = get_json(f"http://127.0.0.1:{settings.http_port}/health", 2)
                with urllib.request.urlopen("http://127.0.0.1:8080/", timeout=2) as response:
                    frontend_ok = int(getattr(response, "status", 200)) == 200
                already_running = health.get("status") == "ok" and frontend_ok
            except Exception:
                already_running = False
        for port, in_use in port_states.items():
            if already_running:
                checks.append(report(True, f"Local port {port}", "project already running"))
            else:
                checks.append(report(not in_use, f"Local port {port}", "already in use" if in_use else "available"))

    camera = urllib.parse.urlparse(settings.camera_rtsp_url)
    camera_ok = bool(camera.hostname) and tcp(camera.hostname, camera.port or 554)
    if camera_ok:
        checks.append(report(True, "Camera RTSP", camera.hostname or "configured"))
    else:
        warn("Camera RTSP", f"{camera.hostname or 'not configured'}; demo/API mode remains available")

    backend_python = resolve_executable(settings.backend_python)
    checks.append(report(backend_python is not None, "Backend Python", str(backend_python or settings.backend_python)))
    if settings.vision_enabled:
        profile_ok = settings.vision_profile == "yolo26"
        checks.append(report(profile_ok, "Vision profile", settings.vision_profile))
        checks.append(report(settings.vision_model_path.is_file(), "YOLO26 model", settings.vision_model_path.name))
        try:
            probe = subprocess.run(
                [str(backend_python), "-c",
                 "import torch,ultralytics,websockets;print('cuda' if torch.cuda.is_available() else 'cpu')"],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            device = probe.stdout.strip().splitlines()[-1] if probe.stdout.strip() else "unknown"
            device_ok = settings.vision_device == "cpu" or device == "cuda"
            checks.append(report(device_ok, "YOLO runtime", f"device={device}"))
        except Exception as exc:
            checks.append(report(False, "YOLO runtime", str(exc)))

    try:
        tags = get_json(f"{settings.ollama_url}/api/tags", 2)
        models = {str(item.get("name") or item.get("model") or "") for item in tags.get("models", [])}
        model_ok = settings.ollama_model in models
        checks.append(report(model_ok, "Ollama model", settings.ollama_model if model_ok else f"missing: {settings.ollama_model}"))
    except Exception as exc:
        checks.append(report(False, "Ollama", str(exc)))

    if args.startup:
        if settings.public_url:
            checks.append(report(True, "PUBLIC_URL", settings.public_url))
        else:
            warn("PUBLIC_URL", "not configured; local UI and Agent remain available")
    else:
        try:
            health = get_json(f"http://127.0.0.1:{settings.http_port}/health")
            checks.append(report(True, "Backend", health.get("timestamp", "online")))
            image_url = (health.get("last_event") or {}).get("image_url", "")
            if not image_url:
                existing = sorted(Path(settings.alarm_dir).glob("*.jpg"), key=lambda item: item.stat().st_mtime, reverse=True)
                if existing and settings.public_url:
                    image_url = f"{settings.public_url}/alarms/{existing[0].name}"
            verifier = NotifierTool(image_required=True, image_check_attempts=1, image_check_timeout=5)
            public_ok, _, error = verifier._verify_public_image(image_url) if image_url else (False, 0, "no_recent_evidence")
            checks.append(report(public_ok, "Public evidence", image_url if public_ok else error))
        except Exception as exc:
            checks.append(report(False, "Backend/public", str(exc)))

    try:
        settings.alarm_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.alarm_dir / ".write_probe"
        probe.write_bytes(b"ok")
        probe.unlink()
        checks.append(report(True, "Evidence storage", str(settings.alarm_dir)))
    except OSError as exc:
        checks.append(report(False, "Evidence storage", str(exc)))

    if all(checks) and already_running:
        print("\nSYSTEM ALREADY RUNNING - open http://localhost:8080")
        return 10
    print("\nPREFLIGHT " + ("PASSED" if all(checks) else "FAILED"))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
