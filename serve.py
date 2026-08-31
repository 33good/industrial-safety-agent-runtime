"""Supervise the personal local Agent runtime and its static frontend."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request

from config import Settings, resolve_executable


PROJECT_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = PROJECT_DIR / "frontend"
class FrontendServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _browser_host(bind_host: str) -> str:
    return "127.0.0.1" if bind_host in {"0.0.0.0", "::", "::1"} else bind_host


def runtime_config(settings: Settings) -> dict:
    host = _browser_host(settings.bind_host)
    return {
        "apiBase": f"http://{host}:{settings.http_port}",
        "wsUrl": f"ws://{host}:{settings.websocket_port}",
        "frontendUrl": f"http://{host}:{settings.frontend_port}",
        "localOnly": settings.bind_host in {"127.0.0.1", "localhost", "::1"},
    }


def make_frontend_handler(settings: Settings):
    config_script = (
        "window.__RUNTIME_CONFIG__="
        + json.dumps(runtime_config(settings), ensure_ascii=False, separators=(",", ":"))
        + ";\n"
    ).encode("utf-8")

    class FrontendHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

        def do_GET(self):
            if self.path.split("?", 1)[0] == "/runtime-config.js":
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(config_script)))
                self.end_headers()
                self.wfile.write(config_script)
                return
            super().do_GET()

        def end_headers(self):
            self.send_header("Cross-Origin-Embedder-Policy", "credentialless")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            super().end_headers()

        def log_message(self, format, *args):
            print(f"[Web] {args[0]}")

    return FrontendHandler


def _get_json(url: str, timeout: float = 1.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _port_accepting(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_backend(process: subprocess.Popen, settings: Settings) -> dict:
    deadline = time.monotonic() + settings.startup_timeout_seconds
    ready_url = f"http://{_browser_host(settings.bind_host)}:{settings.http_port}/ready"
    last_error = "backend_not_ready"
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(f"backend_exited_during_startup:{exit_code}")
        try:
            readiness = _get_json(ready_url)
            if readiness.get("ready") is True:
                return readiness
            last_error = str(readiness.get("status") or last_error)
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP_{exc.code}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}:{exc}"
        time.sleep(0.25)
    raise RuntimeError(f"backend_startup_timeout:{last_error}")


def _write_pid(path: Path, pid: int) -> None:
    path.write_text(str(pid), encoding="ascii")


def _remove_if_owned(path: Path, pid: int) -> None:
    try:
        if path.read_text(encoding="ascii").strip() == str(pid):
            path.unlink()
    except FileNotFoundError:
        pass


def _request_backend_stop(process: subprocess.Popen, timeout: float = 8.0) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def main() -> int:
    settings = Settings.from_env()
    runtime_dir = settings.runtime_state_dir
    supervisor_pid_path = runtime_dir / "serve.pid"
    backend_pid_path = runtime_dir / "backend.pid"
    supervisor_state_path = runtime_dir / "supervisor.json"
    host = _browser_host(settings.bind_host)
    config = runtime_config(settings)
    ready_url = f"{config['apiBase']}/ready"

    if _port_accepting(host, settings.frontend_port):
        try:
            readiness = _get_json(ready_url)
        except Exception:
            readiness = {}
        if readiness.get("ready") is True:
            print(f"[Runtime] already running: {config['frontendUrl']}")
            return 0
        raise RuntimeError(
            f"frontend_port_conflict:{settings.bind_host}:{settings.frontend_port}"
        )
    for name, port in (("http", settings.http_port), ("websocket", settings.websocket_port)):
        if _port_accepting(host, port):
            raise RuntimeError(f"{name}_port_conflict:{settings.bind_host}:{port}")

    backend_python = resolve_executable(settings.backend_python)
    if backend_python is None:
        raise RuntimeError(f"backend_python_not_found:{settings.backend_python}")

    runtime_dir.mkdir(parents=True, exist_ok=True)
    frontend = FrontendServer(
        (settings.bind_host, settings.frontend_port),
        make_frontend_handler(settings),
    )
    frontend.timeout = 0.5
    creationflags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    )
    backend = subprocess.Popen(
        [str(backend_python), "-B", "backend.py"],
        cwd=PROJECT_DIR,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    supervisor_pid = os.getpid()
    _write_pid(supervisor_pid_path, supervisor_pid)
    _write_pid(backend_pid_path, backend.pid)
    stop = threading.Event()

    def request_stop(signum=None, frame=None):
        if not stop.is_set():
            print(f"[Supervisor] shutdown requested signal={signum or 'internal'}")
            stop.set()

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, signal_name, None)
        if signum is not None:
            signal.signal(signum, request_stop)

    try:
        readiness = _wait_for_backend(backend, settings)
        state = {
            "schema_version": "personal-agent-supervisor-v1",
            "status": "running",
            "supervisor_pid": supervisor_pid,
            "backend_pid": backend.pid,
            "started_at": time.time(),
            "endpoints": config,
            "degraded_dependencies": readiness.get("degraded_dependencies") or [],
        }
        supervisor_state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("\n[Runtime] personal Agent service is ready")
        print(f"[Runtime] frontend  {config['frontendUrl']}")
        print(f"[Runtime] API       {config['apiBase']}")
        print(f"[Runtime] WebSocket {config['wsUrl']}")
        if state["degraded_dependencies"]:
            print(
                "[Runtime] degraded dependencies: "
                + ", ".join(state["degraded_dependencies"])
                + " (background recovery remains active)"
            )
        while not stop.is_set():
            frontend.handle_request()
            exit_code = backend.poll()
            if exit_code is not None:
                raise RuntimeError(f"backend_exited:{exit_code}")
        return 0
    finally:
        frontend.server_close()
        _request_backend_stop(backend)
        _remove_if_owned(backend_pid_path, backend.pid)
        _remove_if_owned(supervisor_pid_path, supervisor_pid)
        try:
            supervisor_state_path.unlink()
        except FileNotFoundError:
            pass
        print("[Supervisor] all owned processes stopped")


if __name__ == "__main__":
    raise SystemExit(main())
