"""桌宠的纯文字气泡：分页、换行、停留、淡出。"""
from __future__ import annotations

import time

import pygame


def _font(size: int) -> pygame.font.Font:
    # 测试只构造 Bubble、不初始化视频子系统；字体模块自己初始化即可。
    if not pygame.font.get_init():
        pygame.font.init()
    for name in ("C:/Windows/Fonts/msyh.ttc",
                 "C:/Windows/Fonts/msyhbd.ttc",
                 "C:/Windows/Fonts/simhei.ttf"):
        try:
            return pygame.font.Font(name, size)
        except OSError:
            continue
    return pygame.font.SysFont("microsoftyahei", size)


class Bubble:
    def __init__(self) -> None:
        self.pages: list[str] = []
        self.idx = 0
        self.visible = False
        self.until = 0.0
        self.cps = 4.6
        self.sync = True
        self.font = _font(22)
        self.rect = pygame.Rect(0, 0, 0, 0)
        self._shown = 0
        self._started = 0.0
        self._animate = True

    def show(self, pages: list[str], cps: float = 4.6,
             animate: bool = True, sync: bool = True,
             duration: float = 0.0) -> None:
        self.pages = [str(p) for p in (pages or []) if str(p).strip()]
        self.idx = 0
        self.cps = max(1.0, float(cps or 4.6))
        self.sync = bool(sync)
        self.visible = bool(self.pages)
        self.until = 0.0
        self._animate = bool(animate) and self.visible
        self._shown = 0 if self._animate else len(
            self.pages[0] if self.pages else "")
        self._started = time.monotonic()
        self._match_speech(0, duration)

    def status(self, text: str) -> None:
        text = str(text or "").strip()
        if text:
            self.show([text], animate=False)
        else:
            self.visible = False

    def page(self, idx: int, duration: float = 0.0) -> None:
        if not self.visible or not self.pages:
            return
        target = max(0, min(int(idx), len(self.pages) - 1))
        if target < self.idx:
            # 迟到的 page 不能把已经念到的页拉回去。
            return
        if target == self.idx:
            # 同一页的迟到事件只更新本页语速，不重置已经打出来的字。
            self._match_speech(self.idx, duration)
            return
        self.idx = target
        self._animate = True
        self._shown = 0
        self._started = time.monotonic()
        self._match_speech(self.idx, duration)

    def finish(self, upto: int | None = None) -> None:
        """语音播完：当前页直接补全，停 0.9 秒后收起。

        打字机只负责“跟着这一句说”，不能比声音晚结束；一句话已经念完时，
        剩余的几颗字没有信息量，再逐字蹦只会让画面落后于声音。
        """
        if not self.visible:
            return
        if upto is not None and self.pages:
            target = max(0, min(int(upto) - 1, len(self.pages) - 1))
            # 只前进、不倒退：page 事件可能已经按播放位置翻过页。
            if target > self.idx:
                self.idx = target
        self._animate = False
        self._shown = len(self._current())
        self._start_hold(hold=0.9)

    def _match_speech(self, idx: int, duration: float) -> None:
        """按这一页真实音频时长校准出字速度；未知时长沿用全局 cps。"""
        try:
            seconds = float(duration or 0.0)
        except (TypeError, ValueError):
            seconds = 0.0
        text = self.pages[idx] if 0 <= idx < len(self.pages) else ""
        if seconds <= 0.15 or not text:
            return
        cps = len(text) / seconds
        self.cps = max(1.0, min(30.0, cps))

    def _start_hold(self, hold: float = 0.9) -> None:
        self.until = time.monotonic() + max(0.5, float(hold))

    def _current(self) -> str:
        return self.pages[self.idx] if self.pages else ""

    @property
    def animating(self) -> bool:
        return bool(self.visible and self._animate
                    and self._shown < len(self._current()))

    def _visible_text(self) -> str:
        self._advance()
        text = self._current()
        if not self._animate:
            return text
        elapsed = max(0.0, time.monotonic() - self._started)
        n = min(len(text), max(1, int(elapsed * self.cps) + 1))
        if n > self._shown:
            self._shown = n
        return text[:self._shown]

    def _advance(self, now: float | None = None) -> None:
        """非同步（流式/无 TTS 兜底）按 cps 自动翻页；页边界由 page 事件精确控制时不动。"""
        if (self.sync or not self.visible or self.until
                or self.idx >= len(self.pages) - 1):
            return
        if self.animating:
            return
        text = self._current()
        # 流式没有逐句边界时，按本页字数/语速翻页；不能再加“停留 0.6 秒”，
        # 否则下一句已经开口，气泡还挂在上一页。
        duration = max(0.5, len(text) / max(1.0, self.cps))
        current = time.monotonic() if now is None else now
        if current - self._started >= duration:
            self.page(self.idx + 1)

    def tick(self, now: float | None = None) -> bool:
        """主循环每帧调用：推进打字机/自动翻页，返回是否需要重绘 overlay。

        不依赖 ``draw()``；否则打字结束后 overlay 不再重建，无 TTS 多页会停在第一页。
        """
        if not self.visible or not self.pages:
            return False
        prev_idx = self.idx
        prev_shown = self._shown
        self._advance(now)
        if self._animate and self._shown < len(self.pages[self.idx]):
            t = time.monotonic() if now is None else now
            elapsed = max(0.0, t - self._started)
            n = min(len(self.pages[self.idx]),
                    max(1, int(elapsed * self.cps) + 1))
            if n > self._shown:
                self._shown = n
        return (self.idx != prev_idx) or (self._shown != prev_shown)

    def _wrap(self, text: str, width: int) -> list[str]:
        lines: list[str] = []
        for para in str(text or "").splitlines() or [""]:
            line = ""
            for ch in para:
                trial = line + ch
                if line and self.font.size(trial)[0] > width:
                    lines.append(line)
                    line = ch
                else:
                    line = trial
            if line:
                lines.append(line)
        return lines or [""]

    def expired(self) -> bool:
        """停留时间到了（由画布那一层负责收起来并重画 overlay）。"""
        return bool(self.until) and time.monotonic() >= self.until

    def draw(self, surface: pygame.Surface) -> None:
        if not self.visible:
            return
        text = self._visible_text()
        max_w = max(120, surface.get_width() - 24)
        lines = self._wrap(text, max_w - 20)
        line_h = self.font.get_linesize()
        h = line_h * len(lines) + 16
        w = min(max_w, max(self.font.size(x)[0] for x in lines) + 20)
        x = max(12, (surface.get_width() - w) // 2)
        y = 10
        self.rect = pygame.Rect(x, y, w, h)
        box = pygame.Surface((w, h), pygame.SRCALPHA)
        pygame.draw.rect(box, (18, 22, 28, 225), box.get_rect(),
                         border_radius=10)
        pygame.draw.rect(box, (90, 120, 145, 220), box.get_rect(),
                         width=1, border_radius=10)
        for i, line in enumerate(lines):
            img = self.font.render(line, True, (240, 246, 252))
            box.blit(img, (10, 8 + i * line_h))
        surface.blit(box, (x, y))
