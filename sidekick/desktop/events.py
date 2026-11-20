"""桌宠到 stage 的 WebSocket 通道：收 say/page/level/end，发 click。"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from urllib.parse import urlsplit, urlunsplit

import aiohttp


def _ws_url(url: str) -> str:
    parts = urlsplit(url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, "/ws", "", ""))


class StageEvents:
    def __init__(self, url: str) -> None:
        self.url = _ws_url(url)
        self.queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="desktop-stage", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                asyncio.run(self._connect())
            except Exception as e:
                if not self._stop.is_set():
                    self.queue.put({"t": "error", "error": f"{type(e).__name__}: {e}"})
            self._stop.wait(2.0)

    async def _connect(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.url, heartbeat=30) as ws:
                with self._lock:
                    self._loop = asyncio.get_running_loop()
                    self._ws = ws
                self.queue.put({"t": "connected"})
                try:
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            data = json.loads(msg.data)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(data, dict):
                            self.queue.put(data)
                finally:
                    with self._lock:
                        self._ws = None
                        self._loop = None

    def send(self, data: dict) -> None:
        with self._lock:
            loop, ws = self._loop, self._ws
        if loop is None or ws is None or ws.closed:
            return
        # 通道可能刚好在关闭：这一条发不出去就算了，重连后状态会自己刷新
        try:
            asyncio.run_coroutine_threadsafe(ws.send_json(data), loop)
        except (RuntimeError, ValueError):
            pass

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            loop, ws = self._loop, self._ws
        if loop is not None and ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except (RuntimeError, ValueError):
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
