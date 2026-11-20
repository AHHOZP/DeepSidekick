"""事件 + 环形缓冲。

弹幕源（blivedm / none）只管往 EventBus 里 push Event，
上层（模式判断、prompt 组装）只从 EventBus 里读。两者互不认识。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

DANMU = "danmu"
GIFT = "gift"
SC = "sc"
GUARD_BUY = "guard_buy"
ENTER = "enter"
ME = "me"          # 主播自己说的话（语音转文字来的）

# 会触发说话的事件；进场和人数不算
SPEAKABLE = (ME, DANMU, GIFT, SC, GUARD_BUY)
# 观众侧互动（不含主播自己的 ME）：模式判断 / 人数气氛用这个
AUDIENCE = (DANMU, GIFT, SC, GUARD_BUY)


@dataclass(slots=True)
class Event:
    kind: str
    ts: float = field(default_factory=time.time)
    name: str = ""
    text: str = ""
    price: float = 0.0
    extra: dict = field(default_factory=dict)
    # 递增序号：消费按它来，不靠时间戳（Windows 的 time.time() 只有 ~15ms 精度，
    # 同一刻到达的事件会被 since() 漏掉）
    seq: int = 0

    def format_line(self, called: bool = False,
                    name: str | None = None) -> str:
        """给模型看的一行；`called` 表示这条弹幕叫到了主播名字。"""
        who = name or self.name or "有人"
        body = str(self.text or "").replace("\n", " ")
        if self.kind == ME:
            return f"我说：{body}"
        if self.kind == DANMU:
            mark = "（叫你）" if called else ""
            return f"{who}{mark}：{body}"
        if self.kind == GIFT:
            n = self.extra.get("num", 1)
            return f"[礼物] {who} 投喂了 {body} x{n}"
        if self.kind == SC:
            return f"[醒目留言 ¥{self.price:g}] {who}：{body}"
        if self.kind == GUARD_BUY:
            return f"[上舰] {who} 开通了 {body or '舰长'}"
        if self.kind == ENTER:
            return f"[进场] {who}"
        return f"[{self.kind}] {who} {body}".strip()

    def is_speakable(self) -> bool:
        return self.kind in SPEAKABLE


class EventBus:
    def __init__(self, ring_size: int = 400, on_event=None) -> None:
        self._ring: deque[Event] = deque(maxlen=max(50, ring_size))
        self._viewers: int = 0
        self._seq = 0
        # push 来自 STT 线程和事件循环两边；序号和 append 必须一起完成，
        # 否则调度器可能读到新序号、却还看不到那条事件，把它永久漏掉。
        self._lock = threading.Lock()
        # 只给形象层看的旁路回调：不消费事件、不影响调度
        self.on_event = on_event

    # ── 写入 ──────────────────────────────────────────
    def push(self, ev: Event) -> None:
        with self._lock:
            self._seq += 1
            ev.seq = self._seq
            self._ring.append(ev)
        if self.on_event is not None:
            try:
                self.on_event(ev)
            except Exception:
                pass

    # ── 读取 ──────────────────────────────────────────
    @property
    def viewers(self) -> int:
        return self._viewers

    def set_viewers(self, n: int) -> None:
        self._viewers = max(0, int(n))

    @property
    def seq(self) -> int:
        """最新事件的序号。调度器记它，就不用怕时钟精度。"""
        with self._lock:
            return self._seq

    def since_seq(self, seq: int) -> list[Event]:
        with self._lock:
            return [e for e in self._ring if e.seq > seq]

    def recent(self, seconds: float) -> list[Event]:
        cut = time.time() - max(0.0, seconds)
        with self._lock:
            return [e for e in self._ring if e.ts >= cut]

    def count(self, seconds: float, kinds: tuple[str, ...] = SPEAKABLE) -> int:
        return sum(1 for e in self.recent(seconds) if e.kind in kinds)

    def count_audience(self, seconds: float = 300.0) -> int:
        """最近 N 秒观众侧互动条数（弹幕/礼物/SC/上舰，不含主播自己的话）。"""
        return self.count(seconds, AUDIENCE)
