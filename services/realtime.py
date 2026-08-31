"""Thread-safe WebSocket event broadcaster for the 3D frontend."""
import asyncio
import json
import queue
import threading
import time

try:
    import websockets
except ImportError:
    websockets = None


class RealtimeBroadcaster:
    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self._queue = queue.Queue()
        self._clients = set()
        self._started = False
        self._thread = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._finished = threading.Event()
        self._error = ""

    @property
    def enabled(self) -> bool:
        return websockets is not None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def start(self, timeout: float = 5.0) -> None:
        if self._started:
            return
        if not self.enabled:
            raise RuntimeError("websockets_not_installed")
        self._started = True
        self._stop.clear()
        self._ready.clear()
        self._finished.clear()
        self._error = ""
        self._thread = threading.Thread(
            target=self._run, name="websocket-broadcaster", daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            if self._ready.wait(0.05):
                return
            if self._finished.is_set():
                raise RuntimeError(self._error or "websocket_startup_failed")
        raise RuntimeError(self._error or "websocket_startup_timeout")

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=max(0.1, float(timeout)))
        self._started = False
        self._ready.clear()

    def publish(self, event_data: dict) -> None:
        if self._stop.is_set():
            return
        self._queue.put(json.dumps(event_data, ensure_ascii=False))

    def status(self) -> dict:
        if not self.enabled:
            status = "disabled"
        elif self._ready.is_set() and self._thread is not None and self._thread.is_alive():
            status = "ok"
        elif self._error:
            status = "degraded"
        else:
            status = "starting"
        return {
            "status": status,
            "host": self.host,
            "port": self.port,
            "clients": self.client_count,
            "error": self._error,
        }

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
        finally:
            self._finished.set()

    async def _serve(self) -> None:
        async with websockets.serve(self._handle_client, self.host, self.port):
            self._ready.set()
            print(f"[WS] WebSocket broadcast: ws://{self.host}:{self.port}")
            await self._broadcast_loop()

    async def _handle_client(self, websocket) -> None:
        self._clients.add(websocket)
        print(f"[WS] 前端已连接，当前连接数: {self.client_count}")
        try:
            await websocket.wait_closed()
        finally:
            self._clients.discard(websocket)
            print(f"[WS] 前端已断开，当前连接数: {self.client_count}")

    async def _broadcast_loop(self) -> None:
        while not self._stop.is_set():
            try:
                message = self._queue.get(timeout=0.1)
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if message is None:
                break
            if not self._clients:
                continue
            dead_clients = []
            for client in list(self._clients):
                try:
                    await client.send(message)
                except Exception:
                    dead_clients.append(client)
            for client in dead_clients:
                self._clients.discard(client)
