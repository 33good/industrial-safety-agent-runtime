"""Thread-safe WebSocket event broadcaster for the 3D frontend."""
import asyncio
import json
import queue
import threading

try:
    import websockets
except ImportError:
    websockets = None


class RealtimeBroadcaster:
    def __init__(self, port: int):
        self.port = port
        self._queue = queue.Queue()
        self._clients = set()
        self._started = False

    @property
    def enabled(self) -> bool:
        return websockets is not None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def start(self) -> None:
        if self._started or not self.enabled:
            return
        self._started = True
        threading.Thread(target=self._run, name="websocket-broadcaster", daemon=True).start()

    def publish(self, event_data: dict) -> None:
        self._queue.put(json.dumps(event_data, ensure_ascii=False))

    def status(self) -> dict:
        return {
            "status": "ok" if self.enabled else "disabled",
            "port": self.port,
            "clients": self.client_count,
        }

    def _run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        async with websockets.serve(self._handle_client, "0.0.0.0", self.port):
            print(f"[WS] WebSocket 广播端口: {self.port}")
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
        while True:
            try:
                message = self._queue.get(timeout=0.1)
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
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
