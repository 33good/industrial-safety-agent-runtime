"""
一键启动：HTTP 服务器 + WebSocket + 后端
运行后浏览器打开 http://localhost:8080
支持加载 GLTF/GLB 3D 模型文件
"""
import atexit
import os, sys, threading, socketserver
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler

from config import Settings, resolve_executable

PORT = 8080
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
PROJECT_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = PROJECT_DIR / "runtime"
SERVE_PID = RUNTIME_DIR / "serve.pid"
BACKEND_PID = RUNTIME_DIR / "backend.pid"

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FRONTEND_DIR, **kwargs)

    def end_headers(self):
        # 允许加载 .glb .gltf .bin 等 3D 资源
        self.send_header("Cross-Origin-Embedder-Policy", "credentialless")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        super().end_headers()

    def log_message(self, format, *args):
        print(f"[Web] {args[0]}")

def main():
    settings = Settings.from_env()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    SERVE_PID.write_text(str(os.getpid()), encoding="ascii")
    # 先启动后端
    print("Starting backend...")
    import subprocess
    backend_python = resolve_executable(settings.backend_python)
    if backend_python is None:
        raise RuntimeError(f"BACKEND_PYTHON not found: {settings.backend_python}")
    backend = subprocess.Popen([str(backend_python), "backend.py"],
                               cwd=os.path.dirname(os.path.abspath(__file__)))
    BACKEND_PID.write_text(str(backend.pid), encoding="ascii")

    def cleanup():
        if backend.poll() is None:
            backend.terminate()
            try:
                backend.wait(timeout=5)
            except subprocess.TimeoutExpired:
                backend.kill()
        for path in (BACKEND_PID, SERVE_PID):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    atexit.register(cleanup)

    # 启动前端 HTTP 服务器
    os.chdir(FRONTEND_DIR)
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.allow_reuse_address = True
    print(f"""
╔══════════════════════════════════════════╗
║     研电赛 - 工厂安全数字孪生          ║
║                                         ║
║  前端: http://localhost:{PORT}              ║
║  后端: http://localhost:5000              ║
║  WebSocket: ws://localhost:5001          ║
║                                         ║
║  把 .glb 模型放到 frontend/ 目录        ║
║  自动加载替换程序化场景                 ║
╚══════════════════════════════════════════╝
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        print("\n已停止")
    finally:
        cleanup()

if __name__ == "__main__":
    main()
