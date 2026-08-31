import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import ApiHandler, Application, LocalThreadingHTTPServer
from serve import FrontendServer, make_frontend_handler, runtime_config
from services.agent_runtime import AgentRuntime
from services.realtime import RealtimeBroadcaster


class PersonalRuntimeTests(unittest.TestCase):
    @staticmethod
    def _settings(**overrides):
        values = {
            "bind_host": "127.0.0.1",
            "frontend_port": 18080,
            "http_port": 15000,
            "websocket_port": 15001,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_runtime_config_is_injected_from_one_settings_source(self):
        config = runtime_config(self._settings(
            frontend_port=18180, http_port=15100, websocket_port=15101,
        ))
        self.assertEqual(config["frontendUrl"], "http://127.0.0.1:18180")
        self.assertEqual(config["apiBase"], "http://127.0.0.1:15100")
        self.assertEqual(config["wsUrl"], "ws://127.0.0.1:15101")
        self.assertTrue(config["localOnly"])

    def test_frontend_serves_dynamic_runtime_config(self):
        settings = self._settings(http_port=15110, websocket_port=15111)
        server = FrontendServer(
            ("127.0.0.1", 0), make_frontend_handler(settings)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=3
        )
        try:
            connection.request("GET", "/runtime-config.js")
            response = connection.getresponse()
            payload = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            serialized = payload.split("=", 1)[1].rstrip(";\n")
            config = json.loads(serialized)
            self.assertEqual(config["apiBase"], "http://127.0.0.1:15110")
            self.assertEqual(config["wsUrl"], "ws://127.0.0.1:15111")
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_readiness_is_public_but_demo_trigger_is_not(self):
        class ReadyApplication:
            settings = SimpleNamespace(frontend_port=18080)

            @staticmethod
            def readiness():
                return {
                    "status": "ready", "ready": True,
                    "degraded_dependencies": ["camera"],
                }

        previous = ApiHandler.app
        ApiHandler.app = ReadyApplication()
        server = LocalThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=3
        )
        try:
            connection.request("GET", "/ready")
            response = connection.getresponse()
            readiness = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertTrue(readiness["ready"])
            self.assertEqual(readiness["degraded_dependencies"], ["camera"])

            connection.request(
                "POST", "/demo/trigger", body=b"{}",
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            missing = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 404)
            self.assertEqual(missing["status"], "error")
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            ApiHandler.app = previous

    def test_application_stop_releases_owned_workers_in_order(self):
        calls = []

        class Stopper:
            def __init__(self, name):
                self.name = name

            def stop(self):
                calls.append(self.name)

        class RuntimeStopper:
            def shutdown(self, timeout=0):
                calls.append("runtime")
                return {"drained": True, "active_run_ids": []}

        app = Application.__new__(Application)
        app._vision_arm_stop = threading.Event()
        app._vision_arm_thread = None
        app.settings = SimpleNamespace(shutdown_drain_seconds=0.5)
        app.vision = Stopper("vision")
        app.camera = Stopper("camera")
        app.runtime = RuntimeStopper()
        app.realtime = Stopper("websocket")
        app._started = True

        app.stop()

        self.assertEqual(calls, ["vision", "camera", "runtime", "websocket"])
        self.assertFalse(app._started)

    def test_runtime_shutdown_drains_tracked_pipeline_and_closes_admission(self):
        runtime = AgentRuntime.__new__(AgentRuntime)
        started = threading.Event()
        release = threading.Event()
        event = SimpleNamespace(run_id="RUN_DRAIN", event_id="EVT_DRAIN")

        def work(_event):
            started.set()
            release.wait(1)

        self.assertTrue(runtime._start_pipeline(event, work, "test"))
        self.assertTrue(started.wait(1))
        timer = threading.Timer(0.05, release.set)
        timer.start()
        result = runtime.shutdown(timeout=1)
        timer.join(timeout=1)

        self.assertTrue(result["drained"])
        self.assertEqual(result["active_run_count"], 0)
        self.assertFalse(result["accepting"])
        self.assertFalse(runtime._start_pipeline(event, work, "late"))

    def test_websocket_broadcaster_starts_and_stops_without_orphan_thread(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        broadcaster = RealtimeBroadcaster(port, host="127.0.0.1")
        broadcaster.start(timeout=3)
        self.assertEqual(broadcaster.status()["status"], "ok")
        broadcaster.stop(timeout=3)
        self.assertFalse(broadcaster._thread.is_alive())

    def test_missing_websocket_dependency_fails_fast(self):
        broadcaster = RealtimeBroadcaster(15001)
        with patch("services.realtime.websockets", None):
            with self.assertRaisesRegex(RuntimeError, "websockets_not_installed"):
                broadcaster.start(timeout=0.1)

    def test_supervisor_process_starts_ready_and_shuts_down_cleanly(self):
        probes = []
        try:
            for _ in range(3):
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                probe.bind(("127.0.0.1", 0))
                probes.append(probe)
            frontend_port, http_port, websocket_port = [
                probe.getsockname()[1] for probe in probes
            ]
        finally:
            for probe in probes:
                probe.close()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = os.environ.copy()
            env.update({
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUTF8": "1",
                "BIND_HOST": "127.0.0.1",
                "FRONTEND_PORT": str(frontend_port),
                "HTTP_PORT": str(http_port),
                "WEBSOCKET_PORT": str(websocket_port),
                "STARTUP_TIMEOUT_SECONDS": "15",
                "SHUTDOWN_DRAIN_SECONDS": "2",
                "BACKEND_PYTHON": sys.executable,
                "RUNTIME_STATE_DIR": str(root / "runtime"),
                "DATABASE_PATH": str(root / "data" / "runtime.db"),
                "ALARM_DIR": str(root / "alarms"),
                "PENDING_DIR": str(root / "pending"),
                "REPORT_DIR": str(root / "reports"),
                "EXECUTION_DIR": str(root / "executions"),
                "CAMERA_RTSP_URL": "",
                "VISION_ENABLED": "0",
                "LLM_MODE": "benchmark",
                "NOTIFY_IMAGE_REQUIRED": "0",
            })
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.name == "nt" else 0
            )
            process = subprocess.Popen(
                [sys.executable, "-B", "serve.py"],
                cwd=Path.cwd(), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            output = ""
            try:
                ready = None
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", http_port, timeout=0.5
                    )
                    try:
                        connection.request("GET", "/ready")
                        response = connection.getresponse()
                        ready = json.loads(response.read().decode("utf-8"))
                        if response.status == 200 and ready.get("ready"):
                            break
                    except (OSError, json.JSONDecodeError):
                        ready = None
                    finally:
                        connection.close()
                    time.sleep(0.1)
                self.assertIsNotNone(ready, "supervisor never exposed readiness")
                self.assertTrue(ready["ready"])
                self.assertTrue(ready["runtime"]["accepting"])

                connection = http.client.HTTPConnection(
                    "127.0.0.1", frontend_port, timeout=2
                )
                try:
                    connection.request("GET", "/runtime-config.js")
                    response = connection.getresponse()
                    config_script = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                    self.assertIn(f'"apiBase":"http://127.0.0.1:{http_port}"', config_script)
                finally:
                    connection.close()

                state_path = root / "runtime" / "supervisor.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["schema_version"], "personal-agent-supervisor-v1")

                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=12), 0)
                output = process.stdout.read() if process.stdout else ""
                self.assertIn("all owned processes stopped", output)
                self.assertFalse(state_path.exists())
                self.assertFalse((root / "runtime" / "backend.pid").exists())
            finally:
                if process.poll() is None:
                    if os.name == "nt":
                        process.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                if process.stdout:
                    process.stdout.close()

    def test_production_startup_has_no_demo_or_tunnel_shortcut(self):
        production_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                Path("backend.py"),
                Path("serve.py"),
                Path("ensure_runtime.py"),
                Path("start.bat"),
                Path("frontend/index.html"),
                Path("frontend/js/app.js"),
            )
        )
        self.assertNotIn("/demo/trigger", production_text)
        self.assertNotIn("sync_cpolar", production_text)
        self.assertNotIn("cpolar", production_text.lower())


if __name__ == "__main__":
    unittest.main()
