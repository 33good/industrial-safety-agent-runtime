"""
一键启动：HTTP 服务器 + WebSocket + 后端
运行后浏览器打开 http://localhost:8080
支持加载 GLTF/GLB 3D 模型文件
"""
import os, sys, threading, socketserver
from http.server import HTTPServer, SimpleHTTPRequestHandler

PORT = 8080
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

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
    # 先启动后端
    print("Starting backend...")
    import subprocess
    backend = subprocess.Popen([sys.executable, "backend.py"],
                               cwd=os.path.dirname(os.path.abspath(__file__)))

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
        backend.terminate()
        print("\n已停止")

if __name__ == "__main__":
    main()
