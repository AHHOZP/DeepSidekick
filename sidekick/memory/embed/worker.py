"""语义模型服务：后台加载、单线程优先队列、query 超时、文档补索引。

为什么不用 `asyncio.to_thread` 直接跑：
  · 文档编码是长任务，query 必须能插队；单线程 + 优先级队列保证
    query 最多排在一个短文档后面；
  · query 有 `memory.embed_timeout_ms` 硬超时，超时就降级关键词，不阻塞主回复；
  · 加载 1–2 GB 的 ONNX 模型要几秒，必须放后台线程，启动不阻塞。

线程职责：
  · load 线程：只加载模型；
  · embed 线程：只跑 ONNX；回调在同一个线程里写 SQLite（store 自带锁）。
"""
from __future__ import annotations

import asyncio
import itertools
import queue
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from pathlib import Path

import numpy as np

from sidekick.memory.embed import catalog
from sidekick.memory.embed.encoder import FastEmbedEncoder


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    return data[min(len(data) - 1, int(len(data) * 0.95))]


class EmbeddingService:
    def __init__(self, cfg: dict, root: str | Path, log=print, *,
                 entry=None, encoder=None) -> None:
        self.cfg = cfg
        self.root = Path(root)
        self.log = log
        self.enabled = bool(cfg["memory"].get("embed_enabled", True))
        self.entry = entry or catalog.get(str(
            cfg["memory"].get("embed_model") or ""))
        self.model = self.entry.id if self.entry is not None else ""
        self.threads = int(cfg["memory"].get("embed_threads", 4))
        self.timeout_ms = int(cfg["memory"].get("embed_timeout_ms", 250))
        self.dir = self._dir(cfg)
        self._encoder = encoder
        self._error: str | None = None
        if not self.enabled:
            self._state = "disabled"
        elif self.entry is None:
            self._state = "unavailable"
            self._error = "没有可用的语义模型；请先在记忆页下载模型"
        else:
            self._state = "loading"
        self._last_error: str | None = None
        self._last_degraded: str | None = None
        self._identity = ""
        self._dim = 0
        self._lock = threading.RLock()
        self._closed = False
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._pending_prefix: set[str] = set()
        self._q: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._queries: list[float] = []
        self._query_count = 0
        self._doc_count = 0
        self._load_thread: threading.Thread | None = None
        self._worker = threading.Thread(target=self._loop, name="embed",
                                        daemon=True)
        self._worker.start()

    def _dir(self, cfg: dict) -> Path:
        rel = str(cfg["memory"].get("embed_dir") or "state/embed")
        base = Path(rel)
        if not base.is_absolute():
            base = self.root / base
        return base / self.model if self.model else base

    # ── 生命周期 ──────────────────────────────────────
    @property
    def ready(self) -> bool:
        return self._state == "ready"

    @property
    def state(self) -> str:
        return self._state

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def last_degraded(self) -> str | None:
        return self._last_degraded

    def start(self) -> None:
        if not self.enabled or self.entry is None or self._closed:
            return
        if self._load_thread is not None and self._load_thread.is_alive():
            return
        self._load_thread = threading.Thread(target=self._load, name="embed-load",
                                             daemon=True)
        self._load_thread.start()

    def _load(self) -> None:
        try:
            missing = self.entry.missing(self.dir)
            if missing:
                raise RuntimeError("模型文件不完整：" + "、".join(missing))
            if self._encoder is None:
                self._encoder = FastEmbedEncoder(self.entry, self.dir,
                                                 self.threads)
            self._encoder.load()
            # 预热一次 ONNX session；否则加载后的第一句 query 会多花几百毫秒。
            try:
                self._encoder.query("预热")
            except Exception:
                pass
            dim = int(getattr(self._encoder, "dim", 0) or 0)
            identity = (self._encoder.identity()
                        if hasattr(self._encoder, "identity") else "")
            with self._lock:
                self._dim = dim
                self._identity = identity
                self._state = "ready"
                self._error = None
            self.log(f"[embed] model={self.model} dim={dim} "
                     f"threads={self.threads} dir={self.dir}")
        except Exception as e:
            with self._lock:
                self._state = "unavailable"
                self._error = f"{type(e).__name__}: {e}"[:300]
                self._last_error = self._error
            self.log(f"[!!] feature embedding unavailable：{self._error}")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._q.put_nowait((9, 0, None))
        except queue.Full:
            pass
        self._worker.join(timeout=5.0)
        if self._load_thread is not None:
            self._load_thread.join(timeout=10.0)

    # ── 队列 ──────────────────────────────────────────
    def _submit(self, kind: str, payload, priority: int) -> Future:
        fut: Future = Future()
        self._q.put((priority, next(self._seq), (kind, payload, fut)))
        return fut

    def _loop(self) -> None:
        while True:
            try:
                _prio, _seq, job = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                break
            kind, payload, fut = job
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                if self._encoder is None:
                    raise RuntimeError("语义模型还没加载")
                if kind == "query":
                    fut.set_result(self._encoder.query(payload))
                else:
                    fut.set_result(self._encoder.documents(payload))
            except Exception as e:
                with self._lock:
                    self._last_error = f"{type(e).__name__}: {e}"[:300]
                fut.set_exception(e)

    # ── 查询 ──────────────────────────────────────────
    def _cached(self, text: str) -> np.ndarray | None:
        with self._lock:
            vec = self._cache.get(text)
            if vec is not None:
                self._cache.move_to_end(text)
            return vec

    def _remember(self, text: str, vec: np.ndarray) -> None:
        with self._lock:
            self._cache[text] = vec
            self._cache.move_to_end(text)
            while len(self._cache) > 8:
                self._cache.popitem(last=False)

    async def query(self, text: str, timeout_ms: int | None = None) -> np.ndarray | None:
        if not self.ready:
            self._last_degraded = ("loading" if self.enabled and self._state != "unavailable"
                                   else "unavailable")
            return None
        text = str(text or "")
        cached = self._cached(text)
        if cached is not None:
            self._last_degraded = None
            return cached
        fut = self._submit("query", text, 0)

        def keep(f: Future) -> None:
            if f.cancelled() or f.exception() is not None:
                return
            self._remember(text, f.result())

        fut.add_done_callback(keep)
        timeout = (self.timeout_ms if timeout_ms is None else int(timeout_ms)) / 1000.0
        t0 = time.perf_counter()
        try:
            vec = await asyncio.wait_for(asyncio.wrap_future(fut), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            self._last_degraded = "timeout"
            return None
        except Exception as e:
            self._last_degraded = "error"
            with self._lock:
                self._last_error = f"{type(e).__name__}: {e}"[:300]
            return None
        ms = (time.perf_counter() - t0) * 1000.0
        with self._lock:
            self._query_count += 1
            self._queries.append(ms)
            if len(self._queries) > 200:
                self._queries = self._queries[-200:]
            self._last_degraded = None
            self._cache[text] = vec
            while len(self._cache) > 8:
                self._cache.popitem(last=False)
        return vec

    def prefetch(self, text: str) -> None:
        """STT 识别完先预热；线程安全，不等待结果。"""
        if not self.ready:
            return
        text = str(text or "").strip()
        if not text:
            return
        with self._lock:
            if text in self._cache or text in self._pending_prefix:
                return
            self._pending_prefix.add(text)
        fut = self._submit("query", text, 1)

        def done(f: Future) -> None:
            with self._lock:
                self._pending_prefix.discard(text)
            if f.cancelled() or f.exception() is not None:
                return
            self._remember(text, f.result())

        fut.add_done_callback(done)

    def submit_docs(self, texts: list[str], callback=None) -> Future | None:
        """后台编码文档；`callback(vectors|None)` 在 embed 线程里执行。

        按小块提交：长文档编码时 query 最多排在一小块后面，不会等整批。
        """
        texts = [str(x or "") for x in (texts or [])]
        if not self.ready or not texts:
            return None
        chunks = [texts[i:i + 4] for i in range(0, len(texts), 4)]
        master: Future = Future()
        results: list = [None] * len(texts)
        remaining = [len(chunks)]
        with self._lock:
            self._doc_count += len(texts)

        def done_chunk(start: int, fut: Future) -> None:
            if fut.cancelled() or fut.exception() is not None:
                if not master.done():
                    master.set_exception(
                        fut.exception() or RuntimeError("文档编码被取消"))
                return
            results[start:start + len(fut.result())] = list(fut.result())
            remaining[0] -= 1
            if remaining[0] == 0 and not master.done():
                master.set_result(results)

        for i, chunk in enumerate(chunks):
            fut = self._submit("docs", chunk, 2)
            fut.add_done_callback(
                lambda f, start=i * 4: done_chunk(start, f))

        def done(f: Future) -> None:
            vectors = None
            if not f.cancelled() and f.exception() is None:
                vectors = f.result()
            if callback is not None:
                try:
                    callback(vectors)
                except Exception as e:
                    with self._lock:
                        self._last_error = f"向量回调失败：{type(e).__name__}: {e}"[:300]
                    self.log(f"[embed] 向量回调失败：{type(e).__name__}: {e}")

        master.add_done_callback(done)
        return master

    # ── 状态 ──────────────────────────────────────────
    def stats(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "enabled": self.enabled,
                "model": self.model,
                "identity": self._identity,
                "dim": self._dim,
                "threads": self.threads,
                "dir": str(self.dir),
                "error": self._error,
                "last_error": self._last_error,
                "last_degraded": self._last_degraded,
                "queries": self._query_count,
                "docs": self._doc_count,
                "last_query_ms": round(self._queries[-1], 1) if self._queries else 0.0,
                "query_p95_ms": round(_p95(self._queries), 1),
                "pending": self._q.qsize(),
                "cache": len(self._cache),
            }
