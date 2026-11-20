"""WebUI 的唯一状态采样器。

浏览器只收一条 SSE；状态每 2.0 秒采样一次，日志每 1.0 秒读一次。
采样只在有订阅者时运行；最后一个连接断开就停。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from sidekick.core.log import log

STATUS_EVERY_S = 2.0
LOGS_EVERY_S = 1.0
LOG_LINES = 180
QUEUE_SIZE = 4


class StatusFeed:
    def __init__(self, supervisor: Any, memory_stats: Callable[[], dict],
                 *, started_at: float) -> None:
        self.supervisor = supervisor
        self.memory_stats = memory_stats
        self.started_at = float(started_at)
        self.latest: dict | None = None
        self.latest_logs: list[str] | None = None
        self._subs: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._sample_lock = asyncio.Lock()
        self._last_projection: tuple | None = None
        self._closed = False

    # ── 订阅 ──────────────────────────────────────────
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subs.add(q)
        if self.latest is not None:
            self._put(q, ("status", self.latest))
        if self.latest_logs is not None:
            self._put(q, ("logs", {"lines": list(self.latest_logs)}))
        if self._closed:
            return q
        started = False
        if (self._task is None or self._task.done()
                or self._task.cancelling()):
            self._task = asyncio.create_task(self._run_loop())
            started = True
        if not started and self.latest is None:
            self._wake.set()
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)
        if not self._subs and self._task is not None:
            self._task.cancel()

    def wake(self) -> None:
        self._wake.set()

    def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
        self._subs.clear()

    # ── 采样 ──────────────────────────────────────────
    async def snapshot(self, *, force: bool = False) -> dict | None:
        if force or self.latest is None:
            await self.sample_now()
        return self.latest

    async def sample_now(self) -> dict | None:
        async with self._sample_lock:
            await self._sample()
        return self.latest

    async def _sample(self) -> None:
        st = await self.supervisor.status()
        try:
            # SQLite 统计可能被写锁挡住；不要在事件循环里等 busy_timeout。
            memory = await asyncio.to_thread(self.memory_stats)
        except Exception as e:
            memory = {"error": f"{type(e).__name__}: {e}"}
        snap = {"runtime": st, "memory": memory,
                "uptime_s": round(time.time() - self.started_at, 1)}
        self.latest = snap
        projection = self._project(snap)
        if projection != self._last_projection:
            self._last_projection = projection
            self._broadcast(("status", snap))

    def _read_logs(self) -> None:
        lines = self.supervisor.tail(LOG_LINES)
        if lines != self.latest_logs:
            self.latest_logs = list(lines)
            self._broadcast(("logs", {"lines": list(lines)}))

    @staticmethod
    def _project(snap: dict) -> tuple:
        runtime = snap.get("runtime") or {}
        inner = runtime.get("runtime") or {}
        features = inner.get("features") or {}
        stt = inner.get("stt") or {}
        brain = inner.get("brain") or {}
        tts = inner.get("tts") or {}
        mic = inner.get("mic") or {}
        capture = inner.get("capture") or {}
        memory = inner.get("memory") or {}
        stage = inner.get("stage") or {}
        user = brain.get("user") or {}
        return (
            bool(runtime.get("running")),
            runtime.get("external"),
            runtime.get("pid"),
            runtime.get("returncode"),
            inner.get("live"),
            inner.get("source"),
            inner.get("danmu_error"),
            inner.get("config_error"),
            inner.get("secrets_error"),
            inner.get("said_error"),
            inner.get("display_error"),
            tuple(sorted((k, v.get("state"), v.get("error"))
                         for k, v in features.items())),
            stt.get("disabled"),
            stt.get("dead"),
            stt.get("revivals"),
            stt.get("error"),
            brain.get("model"),
            brain.get("source"),
            tuple(sorted(user.items())),
            brain.get("error"),
            tts.get("provider"),
            tts.get("speaking"),
            tts.get("error"),
            mic.get("error"),
            capture.get("error"),
            memory.get("summary_error"),
            stage.get("error"),
            stage.get("dropped"),
            snap.get("memory") or {},
        )

    # ── 广播 ──────────────────────────────────────────
    async def _run_loop(self) -> None:
        if self._closed:
            return
        next_status = 0.0
        next_logs = 0.0
        try:
            while self._subs:
                now = time.monotonic()
                if now >= next_status:
                    try:
                        async with self._sample_lock:
                            await self._sample()
                    except Exception as e:
                        log(f"WebUI 状态采样失败：{type(e).__name__}: {e}")
                    next_status = now + STATUS_EVERY_S
                if now >= next_logs:
                    try:
                        self._read_logs()
                    except Exception as e:
                        log(f"WebUI 日志读取失败：{type(e).__name__}: {e}")
                    next_logs = now + LOGS_EVERY_S
                timeout = max(0.0, min(next_status, next_logs) - time.monotonic())
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
                else:
                    self._wake.clear()
                    next_status = 0.0
        except asyncio.CancelledError:
            raise
        finally:
            if self._task is asyncio.current_task():
                self._task = None
                if self._subs and not self._closed:
                    self._task = asyncio.create_task(self._run_loop())

    def _put(self, q: asyncio.Queue, event: tuple[str, Any]) -> None:
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def _broadcast(self, event: tuple[str, Any]) -> None:
        for q in list(self._subs):
            self._put(q, event)
