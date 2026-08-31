"""Read-only preflight for the personal local Agent runtime."""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import urllib.parse
import urllib.request

from config import Settings, resolve_executable


def report(ok: bool, name: str, detail: str) -> bool:
    print(f"[{'OK' if ok else 'FAIL':<4}] {name:<18} {detail}")
    return ok


def warn(name: str, detail: str) -> None:
    print(f"[WARN] {name:<18} {detail}")


def tcp(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def get_json(url: str, timeout: float = 3.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def browser_host(bind_host: str) -> str:
    return "127.0.0.1" if bind_host in {"0.0.0.0", "::", "::1"} else bind_host


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--startup", action="store_true",
        help="Check that configured ports are available before supervisor startup",
    )
    args = parser.parse_args()
    settings = Settings.from_env()
    host = browser_host(settings.bind_host)
    frontend_url = f"http://{host}:{settings.frontend_port}"
    api_url = f"http://{host}:{settings.http_port}"
    checks: list[bool] = []
    already_running = False

    if args.startup:
        ports = tuple(dict.fromkeys((
            settings.frontend_port, settings.http_port, settings.websocket_port,
        )))
        port_states = {port: tcp(host, port, 0.3) for port in ports}
        if all(port_states.values()):
            try:
                readiness = get_json(f"{api_url}/ready", 2)
                with urllib.request.urlopen(frontend_url, timeout=2) as response:
                    frontend_ok = int(getattr(response, "status", 200)) == 200
                already_running = readiness.get("ready") is True and frontend_ok
            except Exception:
                already_running = False
        for port, in_use in port_states.items():
            if already_running:
                checks.append(report(True, f"Local port {port}", "project already running"))
            else:
                checks.append(report(
                    not in_use, f"Local port {port}",
                    "already in use" if in_use else "available",
                ))

    camera = urllib.parse.urlparse(settings.camera_rtsp_url)
    camera_ok = bool(camera.hostname) and tcp(camera.hostname, camera.port or 554)
    if camera_ok:
        report(True, "Camera RTSP", camera.hostname or "configured")
    elif settings.camera_rtsp_url:
        warn("Camera RTSP", f"{camera.hostname or 'invalid'} unreachable; runtime starts degraded")
    else:
        warn("Camera RTSP", "not configured; external /alarm ingestion remains available")

    backend_python = resolve_executable(settings.backend_python)
    checks.append(report(
        backend_python is not None, "Backend Python",
        str(backend_python or settings.backend_python),
    ))
    if settings.vision_enabled:
        checks.append(report(settings.vision_profile == "yolo26", "Vision profile", settings.vision_profile))
        checks.append(report(settings.vision_model_path.is_file(), "YOLO26 model", settings.vision_model_path.name))
        if backend_python is not None:
            try:
                probe = subprocess.run(
                    [str(backend_python), "-c", (
                        "import torch,ultralytics,websockets;"
                        "print('cuda' if torch.cuda.is_available() else 'cpu')"
                    )],
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

    if settings.llm_mode.lower() == "ollama":
        try:
            tags = get_json(f"{settings.ollama_url}/api/tags", 2)
            models = {
                str(item.get("name") or item.get("model") or "")
                for item in tags.get("models", [])
            }
            model_ok = settings.ollama_model in models
            checks.append(report(
                model_ok, "Ollama model",
                settings.ollama_model if model_ok else f"missing: {settings.ollama_model}",
            ))
        except Exception as exc:
            checks.append(report(False, "Ollama", str(exc)))
    else:
        warn("Ollama", f"not required for LLM_MODE={settings.llm_mode}")

    if not args.startup:
        try:
            readiness = get_json(f"{api_url}/ready")
            checks.append(report(readiness.get("ready") is True, "Backend readiness", readiness.get("status", "unknown")))
            degraded = readiness.get("degraded_dependencies") or []
            if degraded:
                warn("Dependencies", ", ".join(map(str, degraded)))
        except Exception as exc:
            checks.append(report(False, "Backend readiness", str(exc)))
        try:
            with urllib.request.urlopen(frontend_url, timeout=3) as response:
                frontend_ok = int(getattr(response, "status", 200)) == 200
            checks.append(report(frontend_ok, "Frontend", frontend_url))
        except Exception as exc:
            checks.append(report(False, "Frontend", str(exc)))

    try:
        settings.alarm_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.alarm_dir / ".write_probe"
        probe.write_bytes(b"ok")
        probe.unlink()
        checks.append(report(True, "Evidence storage", str(settings.alarm_dir)))
    except OSError as exc:
        checks.append(report(False, "Evidence storage", str(exc)))

    if all(checks) and already_running:
        print(f"\nSYSTEM ALREADY RUNNING - open {frontend_url}")
        return 10
    print("\nPREFLIGHT " + ("PASSED" if all(checks) else "FAILED"))
    if all(checks) and args.startup:
        print(f"Configured frontend: {frontend_url}")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
