"""播放线程 → runtime 引擎线程的 50 Hz 口型帧环。

只保留最近约 5 秒；消费者按“时间戳 ≤ now 的最后一帧”取，队列满丢最旧。
"""
from __future__ import annotations

import threading
from collections import deque

from .types import SpeechFrame


class SpeechRing:
    def __init__(self, maxlen: int = 256) -> None:
        self._frames: deque[SpeechFrame] = deque(maxlen=max(2, int(maxlen)))
        self._lock = threading.Lock()

    def push(self, frame: SpeechFrame) -> None:
        with self._lock:
            self._frames.append(frame)

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()

    def latest(self, now: float) -> SpeechFrame | None:
        now = float(now)
        with self._lock:
            for frame in reversed(self._frames):
                if frame.t <= now + 0.05:
                    return frame
            return self._frames[-1] if self._frames else None

    def __len__(self) -> int:
        return len(self._frames)
