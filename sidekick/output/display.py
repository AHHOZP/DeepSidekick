"""气泡展示：只负责分页、`say/page/end` 和 cps 兜底，不知道 TTS 细节。

TTS 成功时由 `Speaker` 的首音事件驱动 `start()`，做到字音同步；
TTS 失败/缺失时由 `auto_show()` 走同一条 `say/end` 路径显示全文。
没有 stage 时全部是空操作，历史与声音不受影响。
"""
from __future__ import annotations

import asyncio

from sidekick.core import text as textmod
from sidekick.core.ports import Stage

class BubbleDisplay:
    def __init__(self, stage: Stage | None, cps: float) -> None:
        self.stage = stage
        self.cps = float(cps)
        self._token = 0
        self._active = False
        self._closed = True
        self._pages: list[str] = []
        self._auto_task: asyncio.Task | None = None
        self.last_error: str | None = None
        self._status_visible = False

    # ── 内部 ──────────────────────────────────────────
    def _push(self, event: str, **kw) -> None:
        if self.stage is None:
            return
        try:
            self.stage.emit(event, **kw)
            self.last_error = None
        except Exception as e:
            self.last_error = f"{event}: {type(e).__name__}: {e}"

    @property
    def _has_clients(self) -> bool:
        try:
            return int(getattr(self.stage, "clients", 0) or 0) > 0
        except Exception:
            return False

    @property
    def active(self) -> bool:
        """这一轮气泡是否已经开始（started 事件已经到达）。"""
        return self._active and not self._closed

    # ── 状态提示（LLM / TTS 阶段）────────────────────
    def status(self, text: str) -> None:
        """在气泡里显示阶段文字；空字符串 = 收起。

        如果这一轮的文字已经在播，不能用阶段提示盖掉正在念的内容；
        下一轮的组织语言提示会等上一轮结束后再显示。
        """
        text = str(text or "")
        if not text:
            if self._status_visible:
                self._push("status", text="")
                self._status_visible = False
            return
        if self.active:
            return
        if self._auto_task is not None and not self._auto_task.done():
            self._auto_task.cancel()
        self._auto_task = None
        self._status_visible = True
        self._push("status", text=text)

    # ── Speaker 事件 → 气泡 ───────────────────────────
    def start(self, pages: list[str], acts: list[dict] | None = None,
              sync: bool = False, cps: float | None = None,
              duration: float = 0.0) -> int:
        pages = [str(p) for p in (pages or []) if str(p).strip()]
        self._token += 1
        token = self._token
        if self._auto_task is not None and not self._auto_task.done():
            self._auto_task.cancel()
        self._auto_task = None
        self._pages = pages
        self._active = bool(pages)
        self._closed = not pages
        self._status_visible = False
        if cps:
            self.cps = float(cps)
        if not pages:
            return token
        self._push("say", pages=pages, cps=round(self.cps, 2),
                   idx=0, acts=acts or [], sync=bool(sync),
                   duration=max(0.0, float(duration or 0.0)))
        return token

    def page(self, i: int, acts: list[dict] | None = None,
             duration: float = 0.0) -> None:
        if not self._active or self._closed:
            return
        self._push("page", idx=int(i), acts=acts or [],
                   duration=max(0.0, float(duration or 0.0)))

    def finish(self, upto: int) -> None:
        if not self._active or self._closed:
            return
        self._closed = True
        self._active = False
        self._status_visible = False
        total = max(1, len(self._pages))
        try:
            n = int(upto)
        except (TypeError, ValueError):
            n = total
        self._push("end", pages=total, upto=max(1, min(n, total)))

    # ── TTS 失败/缺失的兜底 ───────────────────────────
    async def auto_show(self, text: str, acts: list[dict] | None = None) -> None:
        raw = textmod.strip_tags(str(text or "")).strip()
        if not raw:
            return
        pages = [p for p in textmod.split_sentences(raw, max_chars=35) if p.strip()]
        if not pages:
            pages = [raw]
        self.start(pages, acts, sync=False)
        token = self._token
        if not self._has_clients:
            self.finish(len(pages))
            return
        # 没有音频时钟，按配置 cps 估算阅读时长；**不阻塞回复路径**，
        # 到点只结束这一轮气泡（token 变了就不再收尾）。
        duration = max(0.8, len("".join(pages)) / max(1.0, self.cps))
        self._auto_task = asyncio.create_task(
            self._finish_later(token, len(pages), min(duration, 30.0)))

    async def _finish_later(self, token: int, n_pages: int, delay: float) -> None:
        await asyncio.sleep(delay)
        if token == self._token:
            self.finish(n_pages)
