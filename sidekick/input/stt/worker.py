"""STT 工作线程：队列、识别、统计、复活/停用日志。

不认识 Runtime，只拿 `stt` 和 `EventBus`；识别结果作为 `ME` 事件进总线。
"""
from __future__ import annotations

import queue
import threading
import time

from sidekick.core.events import ME, Event
from sidekick.core.ports import Transcriber


class SttWorker:
    def __init__(self, stt: Transcriber, bus, log=print) -> None:
        self.stt = stt
        self.bus = bus
        self.log = log
        self.report_s = 60.0
        self._q: queue.Queue = queue.Queue(maxsize=8)
        self._drops = 0
        self._closed = False
        self._th: threading.Thread | None = None
        self.last_me: str | None = None
        self.last_ms = 0.0
        # 识别完成后给记忆层预热 query；组合根注入，失败不影响识别。
        self.on_final = None

    # ── 生命周期 ──────────────────────────────────────
    def start(self) -> None:
        if self._th is not None and self._th.is_alive():
            return
        self._closed = False
        self._th = threading.Thread(target=self._loop, name="stt", daemon=True)
        self._th.start()

    def close(self, timeout: float = 3.0) -> bool:
        """停止并 join；返回 worker 是否已经真正退出。"""
        self._closed = True
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        th = self._th
        if th is not None and th.is_alive():
            th.join(timeout)
            if th.is_alive():
                self._th = th
                return False
        self._th = None
        return True

    # ── 入队（麦克风线程里调，必须秒回）─────────────────
    def on_segment(self, pcm, sr) -> None:
        try:
            self._q.put_nowait((pcm, sr))
        except queue.Full:
            self._drops += 1
            self.log("STT 队列满，丢一段（识别跟不上）")

    # ── 主循环 ────────────────────────────────────────
    def _loop(self) -> None:
        next_report = time.time() + self.report_s
        was_dead = False
        was_disabled = False
        while not self._closed:
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                if time.time() >= next_report:
                    next_report = self._report()
                continue
            if item is None:
                break
            pcm, sr = item
            t0 = time.time()
            try:
                res = self.stt.analyze(pcm, sr)
            except Exception as e:
                self.log(f"STT 出错：{e}")
                continue
            dead = bool(getattr(self.stt, "dead", False))
            done = bool(getattr(self.stt, "disabled", False))
            if done and not was_disabled:
                self.log("")
                self.log("=" * 62)
                self.log("[!!] 语音识别已永久停用 —— 她现在听不到你说话了")
                self.log(f"     连续 3 次崩溃，自动复活 "
                         f"{getattr(self.stt, 'revivals', 0)} 次也没救回来")
                self.log(f"     最后错误：{self._last_error() or '未知'}")
                self.log("     重启 runtime（WebUI 顶部的「重启」）才会重新加载模型；")
                self.log("     重启前请先看日志确认是模型坏了还是内存不够。")
                self.log("=" * 62)
                self.log("")
            elif dead != was_dead:
                if dead:
                    self.log(f"[stt] 识别引擎连续失败，冷却 30 秒后重试"
                             f"（已复活 {getattr(self.stt, 'revivals', 0)}/3 次）")
                elif not done:
                    self.log("[stt] 识别引擎冷却结束，恢复重试")
            was_dead, was_disabled = dead, done
            text = str(getattr(res, "text", "") or "").strip()
            if text:
                self.last_me = text
                self.last_ms = (time.time() - t0) * 1000.0
                self.bus.push(Event(kind=ME, name="我", text=text))
                if self.on_final is not None:
                    try:
                        self.on_final(text)
                    except Exception as e:
                        self.log(f"记忆预热失败：{type(e).__name__}: {e}")
                extra = ""
                emo = getattr(res, "emotion", "")
                if emo and str(emo).upper() not in ("NEUTRAL", "EMO_UNKNOWN"):
                    extra += f"，{emo}"
                lang = getattr(res, "lang", "")
                if lang and str(lang).lower() != "zh":
                    extra += f"，{lang}"
                self.log(f"[我] {text}（识别 {self.last_ms:.0f} ms，"
                         f"{getattr(self.stt, 'name', '?')}/"
                         f"{getattr(self.stt, 'model_size', '?')}{extra}）")
            if time.time() >= next_report:
                next_report = self._report()

    def _last_error(self):
        return (getattr(self.stt, "engine_error", None)
                or getattr(self.stt, "error", None))

    def _report(self) -> float:
        try:
            s = self.stt.stats()
            if s.get("window"):
                self.log(f"[stt] {s['window']} 段 p50={s['p50_ms']:.0f}ms "
                         f"p95={s['p95_ms']:.0f}ms 队列深={self._q.qsize()} "
                         f"丢段={self._drops}")
        except Exception:
            pass
        return time.time() + self.report_s

    # ── 状态 ──────────────────────────────────────────
    def status(self) -> dict:
        out = dict(self.stt.snapshot())
        out.update({"queue": self._q.qsize(), "drops": self._drops})
        try:
            s = self.stt.stats()
            out.update({"n": s["n"], "last_ms": round(s["last_ms"]),
                        "p50_ms": round(s["p50_ms"]),
                        "p95_ms": round(s["p95_ms"])})
        except Exception:
            pass
        return out
