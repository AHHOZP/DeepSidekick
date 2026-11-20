"""播出记录：她说过什么、多久没说话。

只干两件事：给 prompt 提供"你刚说过"，给 trigger 提供"距离上次出声多久"。
落一份 jsonl 方便下播复盘。
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

from sidekick.config import path as resolve_path

class SaidLog:
    def __init__(self, log_rel: str = "state/said.jsonl",
                 root: str | Path | None = None) -> None:
        p = Path(log_rel)
        if p.is_absolute():
            self._log = p
        elif root is None:
            self._log = resolve_path(log_rel)
        else:
            self._log = Path(root) / p
        self._keep = 30
        self._mem: list[dict] = []
        # 「最近一小时说了几句」不能用 _mem：它只留 keep(30) 条。
        # 单独留一小时窗口，只存 {ts,text}，一小时最多几百条。
        self._hour: deque[dict] = deque()
        self._opened = 0.0
        self.last_error: str | None = None
        # /status 在 worker 线程读，add 在事件循环写；deque 迭代不能和 append 并发。
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            self._opened = time.time()

    @property
    def started_at(self) -> float:
        """这一场开始的时间戳；0 表示还没 start()。"""
        with self._lock:
            return self._opened

    def add(self, text: str) -> None:
        row = {"ts": time.time(), "text": text}
        with self._lock:
            self._mem.append(row)
            if len(self._mem) > self._keep:
                self._mem = self._mem[-self._keep:]
            self._hour.append(row)
            cut = time.time() - 3600
            while self._hour and self._hour[0]["ts"] < cut:
                self._hour.popleft()
        try:
            self._log.parent.mkdir(parents=True, exist_ok=True)
            with self._log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            with self._lock:
                self.last_error = None
        except OSError as e:
            with self._lock:
                self.last_error = f"{type(e).__name__}: {e}"

    def recent(self, n: int = 3) -> list[str]:
        if n <= 0:
            return []
        with self._lock:
            return [r["text"] for r in self._mem[-n:]]

    @property
    def last_ts(self) -> float:
        with self._lock:
            return self._mem[-1]["ts"] if self._mem else 0.0

    def gap(self) -> float | None:
        """距上次出声多少秒；一句没说返回 None。"""
        with self._lock:
            if not self._mem:
                return None
            return max(0.0, time.time() - self._mem[-1]["ts"])

    def count_last_hour(self) -> int:
        # 只读：不裁剪 deque（裁剪在 add() 里做），避免 /status 改变运行态。
        cut = time.time() - 3600
        with self._lock:
            return sum(1 for r in self._hour if r["ts"] >= cut)

    def session_minutes(self) -> float:
        with self._lock:
            if not self._opened:
                return 0.0
            return (time.time() - self._opened) / 60.0

