"""声音适配器：分句 → 逐句合成 → 逐句播到系统默认输出。

两个开关：

  tts.common.stream:   true   一次请求，厂商原生流式边收边播；不支持流式的厂商自动退回分段
  tts.common.segments: sentence | two | whole   非流式时怎么分句

`tts.common.prefetch` 只在非流式模式下预合成下一句。

播放期间麦克风怎么办由设备形态决定（见 `sidekick/input/devices.py`）：扬声器下整段
ducking（不采信），耳机下照常采集，于是可以被打断——`stop()` 置 `interrupted`，
`say()` 入口清、段循环/流式收口看它，被打断不是故障（不道歉、不计 fail_rounds）。

**Speaker 不拥有气泡**：它只发 `started/page/finished/level` 事件，
Runtime 把事件接到 `Display`。第一块 PCM 真正写进声卡时才发 `started`，
所以合成慢/网络慢都不会让气泡提前出现。

错误处理：单句合成失败重试 1 次，还失败整句放弃、不往下念；
播了一半才失败不道歉；整轮一句没播才算故障。
"""
from __future__ import annotations

import asyncio
import threading
from contextlib import aclosing
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

from sidekick.output import guard, player
from sidekick.core import text as textmod
from sidekick.core.ports import MicSource, SayResult, SpeechUpdate
from sidekick.motion.lipsync import SpeechAnalyzer
from sidekick.output.tts.base import decode_wav, resample


def _page_acts(acts: list[dict] | None,
               pages: list[str]) -> list[dict]:
    """Attach the page index derived from each act's character offset."""
    items = [dict(item) for item in (acts or []) if isinstance(item, dict)]
    if not items or not pages:
        return []
    page_total = max(1, sum(len(str(p)) for p in pages))
    starts: list[int] = []
    accumulated = 0
    for page in pages:
        starts.append(accumulated)
        accumulated += len(str(page))
    for item in items:
        try:
            offset = max(0.0, min(1.0, float(item.get("offset", 0.0))))
        except (TypeError, ValueError):
            offset = 0.0
        target = round(offset * page_total)
        page = 0
        for index, start in enumerate(starts):
            if target >= start:
                page = index
            else:
                break
        item["page"] = page
    return items


def _acts_for_page(acts: list[dict] | None, page: int) -> list[dict]:
    return [dict(item) for item in (acts or [])
            if isinstance(item, dict) and int(item.get("page", 0) or 0) == int(page)]


def _pcm_duration(res) -> float:
    """一句合成结果的实际音频时长；拿不到就 0（气泡退回全局 cps）。"""
    try:
        return len(np.asarray(res.pcm)) / float(res.sr or 1)
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return 0.0


class Speaker:
    def __init__(self, cfg: dict, *, provider,
                 mic: MicSource | None = None) -> None:
        self.retry_delay = float(cfg["errors"]["retry_delay_ms"]) / 1000.0

        self.mic = mic
        self.provider = provider
        self.profile_name = provider.profile
        self.preset_name = provider.preset
        self.available = bool(provider.has_key)

        self.cps = float(cfg["stage"]["cps"])
        self.reload(cfg)
        self._lock = asyncio.Lock()
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._owner: int | None = None
        self._on_event = None
        self.mute = False                 # 自检用：合成照走，就是不出声
        self.speaking = False
        # 打断：`stop()` 置位，`say()` 入口清；段循环/流式路径据此提前收口。
        self.interrupted = False
        # 最近一块播放能量 (rms, monotonic_ts)，整体赋值保证读到的是一对。
        self.play_ref = (0.0, 0.0)
        # 50 Hz 口型：播放线程按真正写进声卡的块喂它，不按收到的音频喂。
        self._analyzer = None
        self._analyzer_sr = 0
        # runtime 引擎宿主需要同一组 50 Hz 帧；用回调解耦，output 不认识 motion。
        self.mouth_sink = None
        self.mouth_reset = None
        self.last_error: str | None = None
        self.last_ms: dict | None = None
        self.last_request_at = 0.0
        self.fail_rounds = 0

    # ── 状态 ──────────────────────────────────────────
    @property
    def busy(self) -> bool:
        return self.speaking or self._lock.locked()

    @property
    def prompt_rules(self) -> str:
        """当前 provider 的发声标签规则；注入主对话的输出约束。"""
        return str(self.provider.prompt_rules or "")

    def snapshot(self) -> dict:
        return {"profile": self.profile_name, "preset": self.preset_name,
                "stream": self.stream,
                "segments": self.segments, "speaking": bool(self.speaking),
                "available": bool(self.available),
                "cps": round(float(self.cps or 0.0), 2),
                "fail_rounds": self.fail_rounds, "last": self.last_ms,
                "error": self.last_error}

    def reload(self, cfg: dict) -> None:
        """热重载 guard/TTS 参数，避免 reply 与 Speaker 各用一套旧快照。"""
        t = cfg["tts"]
        # config.load() 已合并默认值；这里不再写第二份默认值。
        common = t["common"]
        self.gain = float(common["gain"])
        self.segments = str(common["segments"]).strip().lower()
        self.stream = bool(common["stream"])
        self.prefetch = bool(common["prefetch"])
        g = cfg["guard"]
        self.max_chars = int(g["max_chars"])
        self.min_chars = int(g["min_chars"])
        self.extra_patterns = list(g["extra_patterns"])
        target_cps = float(cfg["stage"]["cps"])
        if getattr(self, "_cfg_cps", None) != target_cps:
            self.cps = target_cps
            self._cfg_cps = target_cps

    def stop(self) -> None:
        """外部唯一停止入口（打断回调 / 关闭进程）。

        置 `interrupted` 是给 `say()` 收口用的：被打断不是故障，不道歉、不兜底。
        """
        self.interrupted = True
        self._stop.set()
        self.speaking = False

    # ── 事件 ──────────────────────────────────────────
    def _deliver(self, update: SpeechUpdate) -> None:
        cb = self._on_event
        if cb is None:
            return
        try:
            cb(update)
        except Exception:
            pass

    def _emit(self, update: SpeechUpdate) -> None:
        """播放线程里发的事件必须投回事件循环，Display 只在循环里操作 stage。"""
        cb = self._on_event
        if cb is None:
            return
        if self._loop is None or threading.get_ident() == self._owner:
            self._deliver(update)
            return
        try:
            self._loop.call_soon_threadsafe(self._deliver, update)
        except Exception:
            pass

    def _push_level(self, chunk: np.ndarray, sr: int) -> None:
        data = np.asarray(chunk, dtype=np.float32)
        if data.ndim > 1:
            data = data.mean(axis=1)
        data = data.reshape(-1)
        try:
            v = round(float(np.sqrt(np.mean(np.square(data)))), 3)
        except (TypeError, ValueError, FloatingPointError):
            return
        # 扬声器模式下的打断判据要用它当参考能量：麦克风里的"她"不会大于这个。
        self.play_ref = (v, time.monotonic())
        self._emit(SpeechUpdate(kind="level", level=v))
        try:
            rate = int(sr or 0)
            if self._analyzer is None or rate != self._analyzer_sr:
                self._analyzer = SpeechAnalyzer(rate) if rate > 0 else None
                self._analyzer_sr = rate
            if self._analyzer is None:
                return
            for frame in self._analyzer.push(data):
                frame = replace(frame, t=time.monotonic())
                sink = self.mouth_sink
                if sink is not None:
                    try:
                        sink(frame)
                    except (RuntimeError, ValueError, TypeError):
                        pass
                self._emit(SpeechUpdate(
                    kind="mouth", level=frame.level,
                    openness=frame.openness, form=frame.form,
                    voiced=frame.voiced))
        except (ValueError, RuntimeError):
            # 口型分析失败只影响嘴，不影响播放；下一块/下一句会重试。
            self._analyzer = None

    def _reset_mouth(self) -> None:
        self._analyzer = None
        self._analyzer_sr = 0
        reset = self.mouth_reset
        if reset is not None:
            try:
                reset()
            except (RuntimeError, ValueError, TypeError):
                pass

    def _emit_mouth_silence(self) -> None:
        self._emit(SpeechUpdate(kind="mouth", level=0.0, openness=0.0,
                                form=0.0, voiced=False))

    # ── 播放 ──────────────────────────────────────────
    async def _play(self, pcm, sr: int, on_first) -> float:
        duration = len(np.asarray(pcm)) / float(sr or 1)
        if self.mute:
            on_first()
            await asyncio.sleep(min(3.0, duration))
            return duration
        return await asyncio.to_thread(
            player.play_blocking, pcm, sr,
            gain=self.gain, stop_event=self._stop,
            on_first=on_first, on_level=self._push_level)

    async def _synth_safe(self, sentence: str, *, prefetch: bool = False):
        if not prefetch:
            self.last_request_at = time.time()
        for attempt in (1, 2):
            try:
                return await self.provider.synth(sentence)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"合成失败（{attempt}/2）：{type(e).__name__}: {e}"
                if attempt == 1:
                    await asyncio.sleep(self.retry_delay)
        return None

    # ── 对外 ──────────────────────────────────────────
    async def say(self, raw: str, acts: list[dict] | None = None, *,
                  on_event=None) -> SayResult:
        """说一句话；任何适配器/播放异常都归一成 SayResult，不向调用方逃逸。"""
        # 打断标志与停止信号在入口清一次：每句清的话，第 2 句会把打断标志清掉，
        # 打断就只停当前句、后面继续播。
        self.interrupted = False
        self._stop.clear()
        self._reset_mouth()
        state = {"started": False, "pages": [], "acts": list(acts or []),
                 "text": str(raw or "")}
        try:
            return await self._say(state, raw, acts, on_event=on_event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.last_error = f"语音异常：{type(e).__name__}: {e}"
            self.fail_rounds += 1
            started = bool(state["started"])
            pages = list(state["pages"])
            if started:
                self._emit_finished(pages, state["acts"], 1, False)
                await self._flush_events()
            return SayResult(
                ok=False, started=started, text=state["text"],
                display=state.get("display", state["text"]),
                reason=self.last_error, failed=True, pages=pages)

    async def _say(self, state: dict, raw: str, acts: list[dict] | None, *,
                   on_event=None) -> SayResult:
        t0 = time.time()
        self.last_error = None
        self._on_event = on_event
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._owner = threading.get_ident()

        cap = self.max_chars
        allowed = self.provider.allowed_tags
        keep_square = self.provider.preserve_square_tags
        clean, why = guard.gate(raw, cap, self.min_chars, self.extra_patterns,
                                allowed_tags=allowed, keep_square=keep_square)
        if why:
            return SayResult(ok=False, text=clean, display=clean, reason=why,
                             failed=False, rejected=True)

        display = clean
        clean = self.provider.adapt_text(clean) or clean
        # 结果里的 text 始终是用户看到的版本；厂商标签只留在合成用的 clean 里。
        state["text"] = display
        state["display"] = display

        parts = textmod.split_sentences(clean, max_chars=max(12, cap // 2))
        if self.segments == "whole" or len(parts) <= 1:
            sents = [clean]
        elif self.segments == "two":
            sents = [parts[0], "".join(parts[1:])]
        else:
            sents = parts
        # pages 与 TTS 的 sents 用同一份切句：Fish 的 adapt_text 会改变句长，
        # 从两份文本各自切会让你 page 对不上音频。
        pages = [textmod.strip_tags(s) for s in sents]
        pages = [p for p in pages if p] or [textmod.strip_tags(clean) or clean]
        sentence_sync = len(sents) == len(pages)
        acts = _page_acts(acts, pages)
        if not sentence_sync:
            for item in acts:
                item["page"] = 0
        state.update(text=clean, acts=list(acts or []), pages=list(pages))

        if self.stream and self.provider.supports_stream:
            async with self._lock:
                return await self._say_stream(clean, t0, pages, acts,
                                              state=state, display=display)

        async with self._lock:
            started = False
            first_at = None
            played: list[str] = []
            stop_reason = None
            played_s = 0.0
            first_duration = 0.0
            total_chars = len(textmod.strip_tags(display))

            def on_first() -> None:
                nonlocal started, first_at
                if started:
                    return
                started = True
                state["started"] = True
                first_at = time.time() - t0
                self._emit_started(pages, acts, sentence_sync,
                                   duration=first_duration)

            pending = await self._synth_safe(sents[0])
            first_duration = _pcm_duration(pending) if pending is not None else 0.0
            nxt_task = None
            if self.prefetch and len(sents) > 1:
                nxt_task = asyncio.create_task(
                    self._synth_safe(sents[1], prefetch=True))

            for i in range(len(sents)):
                res = pending
                if res is None:
                    stop_reason = self.last_error or "合成失败"
                    break

                self.speaking = True
                if started and i > 0 and sentence_sync:
                    self._emit(SpeechUpdate(
                        kind="page", index=i, pages=list(pages),
                        acts=_acts_for_page(acts, i), sync=True,
                        cps=round(self.cps, 2),
                        duration=_pcm_duration(res)))
                sent_s = 0.0
                try:
                    sent_s = await self._play(res.pcm, res.sr, on_first)
                except Exception as e:
                    self.last_error = f"播放失败：{e}"
                    stop_reason = self.last_error
                    break
                finally:
                    self.speaking = False

                played_s += sent_s
                played.append(sents[i])
                if self.interrupted:
                    break
                if i + 1 >= len(sents):
                    break
                # 下一句在上一句播放期间合成；这里只等结果，不提前阻塞首音。
                if nxt_task is not None:
                    pending = await nxt_task
                    nxt_task = None
                else:
                    pending = await self._synth_safe(sents[i + 1])
                if self.prefetch and i + 2 < len(sents):
                    nxt_task = asyncio.create_task(
                        self._synth_safe(sents[i + 2], prefetch=True))

            if nxt_task is not None:
                nxt_task.cancel()

            if self.interrupted:
                return await self._interrupted_result(
                    clean=clean, display=display, pages=pages, acts=acts,
                    started=started, sync=sentence_sync, played=played,
                    played_s=played_s, total_chars=total_chars)

            if played:
                spoken = textmod.strip_tags("".join(played))
                upto = self._finish(spoken, played_s, pages, total_chars, clean)
                self.last_ms = {
                    "sentences": len(sents), "played": len(played),
                    "first_sentence_ms": round((first_at or 0) * 1000),
                    "total_ms": round((time.time() - t0) * 1000),
                }
                return await self._success_result(
                    clean=clean, display=display, pages=pages, acts=acts,
                    started=started, sync=sentence_sync, spoken=spoken,
                    upto=upto, ms=self.last_ms, reason=stop_reason)

            reason = stop_reason or self.last_error or "没播出去"
            return await self._failed_result(
                clean=clean, display=display, pages=pages, acts=acts,
                started=started, sync=sentence_sync, reason=reason)

    async def _flush_events(self) -> None:
        """让播放线程投回来的 started/finished 在 say() 返回前落到事件循环。"""
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # ── 结果/事件收口：_say 与 _say_stream 共用 ─────────
    def _emit_started(self, pages, acts, sync: bool,
                      duration: float = 0.0) -> None:
        self._emit(SpeechUpdate(
            kind="started", pages=list(pages), acts=_acts_for_page(acts, 0),
            sync=bool(sync), cps=round(self.cps, 2),
            duration=max(0.0, float(duration or 0.0))))

    def _emit_finished(self, pages, acts, upto: int, sync: bool) -> None:
        self._emit_mouth_silence()
        self._reset_mouth()
        self._emit(SpeechUpdate(
            kind="finished", pages=list(pages), upto=int(upto),
            acts=list(acts or []), sync=bool(sync),
            cps=round(self.cps, 2)))

    async def _failed_result(self, *, clean: str, display: str, pages, acts,
                             started: bool, sync: bool, reason: str) -> SayResult:
        if started:
            self._emit_finished(pages, acts, 1, sync)
        self.fail_rounds += 1
        await self._flush_events()
        return SayResult(ok=False, text=display, display=display, reason=reason,
                         failed=True, started=started, upto=1, pages=list(pages),
                         sync=bool(sync))

    async def _interrupted_result(self, *, clean: str, display: str, pages, acts,
                                  started: bool, sync: bool, played: list[str],
                                  played_s: float, total_chars: int) -> SayResult:
        """被打断的收口：已说的部分写历史/气泡，剩余不播，也不算故障。"""
        spoken = textmod.strip_tags("".join(played))
        upto = 1
        if spoken:
            upto = self._finish(spoken, played_s, pages, total_chars, clean)
        self.last_ms = {"played_s": round(played_s, 2),
                        "spoken_chars": len(spoken)}
        await self._flush_events()
        return SayResult(ok=False, started=started, text=display,
                         display=display, spoken=spoken, upto=upto,
                         reason="被打断", interrupted=True,
                         pages=list(pages), sync=bool(sync), ms=self.last_ms)

    async def _success_result(self, *, clean: str, display: str, pages, acts,
                              started: bool, sync: bool, spoken: str, upto: int,
                              ms: dict, reason: str | None = None) -> SayResult:
        await self._flush_events()
        return SayResult(ok=True, started=started, text=display,
                         display=display, spoken=spoken, upto=upto,
                         reason=reason, failed=False, pages=list(pages),
                         sync=bool(sync), ms=ms)

    # ── 收尾 / cps 校准 ───────────────────────────────
    def _finish(self, spoken: str, played_s: float, pages: list[str],
                total_chars: int, text: str) -> int:
        chars = len(spoken)
        if played_s > 0.6 and chars >= 6:
            obs = chars / played_s
            if 1.5 < obs < 15.0:
                self.cps = 0.7 * self.cps + 0.3 * obs
        upto = len(pages)
        if total_chars > 0 and chars < total_chars:
            upto = max(1, round(len(pages) * chars / total_chars))
        self._emit_finished(pages, None, upto, False)
        return upto

    # ── 真流式播放 ────────────────────────────────────
    async def _later_pages(self, pages: list[str], acts: list[dict] | None,
                           clean: str, started_box: list[bool], out) -> None:
        """流式没有逐句边界；按字数比例 + 实际播放进度翻页，并带出 <ACT>。

        分页不能只在有 <ACT> 时才发：否则气泡会一直停在第一页，等
        `finished` 才跳到最后一页（第一句念完了还挂在那里）。
        用 `out.played_s` 而不是墙上时间起算，避免首块 PCM 延迟/抖动时
        第一页多挂半秒；只前进，迟到的 page 不会把气泡拉回去。
        """
        if len(pages) <= 1:
            return
        # 等首块 PCM 真正写进声卡再起算，避免动作/分页比声音早。
        while not started_box[0]:
            if self._stop.is_set():
                return
            await asyncio.sleep(0.05)
        total = max(1, sum(len(str(p)) for p in pages))
        estimate = max(
            0.7, len(textmod.strip_tags(clean))
            / max(1.0, float(self.cps or 1.0)))
        start_played = float(getattr(out, "played_s", 0.0) or 0.0)
        accumulated = 0
        try:
            for index in range(1, len(pages)):
                accumulated += len(str(pages[index - 1]))
                target = start_played + estimate * accumulated / total
                while float(getattr(out, "played_s", 0.0) or 0.0) < target:
                    if self._stop.is_set() or getattr(out, "error", None):
                        return
                    await asyncio.sleep(0.02)
                page_dur = round(max(
                    0.3, len(pages[index]) / max(1.0, float(self.cps or 1.0))), 3)
                self._emit(SpeechUpdate(
                    kind="page", index=index, pages=list(pages),
                    acts=_acts_for_page(acts, index), sync=False,
                    cps=round(self.cps, 2), duration=page_dur))
        except asyncio.CancelledError:
            return

    async def _say_stream(self, clean: str, t0: float,
                          pages: list[str], acts: list[dict] | None,
                          *, state: dict, display: str) -> SayResult:
        src_sr = int(self.provider.stream_sr or 24000)
        out = player.StreamPlayer(mute=self.mute, src_sr=src_sr)
        if self.mute:
            out.configure()
        else:
            await asyncio.to_thread(out.configure)
        import queue as _queue

        q: _queue.Queue = _queue.Queue(maxsize=256)
        sentinel = object()

        started_box = [False]
        first_at = None

        def on_first() -> None:
            nonlocal first_at
            started_box[0] = True
            state["started"] = True
            first_at = time.time() - t0
            # 流式没有逐句音频长度；用同一套 cps 估第一页，至少让首屏速度同源。
            estimate = (len(pages[0]) / max(1.0, float(self.cps or 1.0))
                        if pages else 0.0)
            self._emit_started(pages, acts, False, duration=estimate)

        self.speaking = True
        page_task = asyncio.create_task(
            self._later_pages(pages, acts, clean, started_box, out))
        try:
            th = threading.Thread(
                target=out.run, args=(q, sentinel, self._stop),
                kwargs={"on_first": on_first, "on_level": self._push_level},
                name="tts-play", daemon=True)
            th.start()
        except Exception:
            # 线程构造/启动失败都没有后续 finally 能复位；这里先放开 ducking。
            page_task.cancel()
            self.speaking = False
            raise
        dropped = 0
        self.last_request_at = time.time()
        n_chunks = 0
        received_s = 0.0
        stream_error = None
        try:
            async with aclosing(self.provider.stream(clean)) as stream:
                async for chunk in stream:
                    if self._stop.is_set():
                        break
                    if out.error:
                        break
                    x = np.asarray(chunk, dtype=np.float32)
                    if len(x):
                        received_s += len(x) / float(src_sr or 1)
                    # 先重采样再削波：反过来会把重采样的过冲削平，产生额外谐波。
                    x = resample(x, src_sr, out.dst_sr)
                    x = np.clip(x * self.gain, -1.0, 1.0)
                    if out.nch > 1:
                        x = np.repeat(x[:, None], out.nch, axis=1)
                    try:
                        q.put_nowait(x)
                    except _queue.Full:
                        dropped += 1
                        continue
                    n_chunks += 1
        except Exception as e:
            stream_error = f"流式合成失败：{type(e).__name__}: {e}"
            self.last_error = stream_error
        finally:
            while True:
                try:
                    q.put_nowait(sentinel)
                    break
                except _queue.Full:
                    # 队列满时腾位置会丢掉一块已收到的音频；必须计数，
                    # 否则下面会把“只播了一部分”当成完整成功。
                    try:
                        q.get_nowait()
                        dropped += 1
                    except _queue.Empty:
                        pass
            try:
                await asyncio.to_thread(th.join, 60)
                if th.is_alive():
                    self.last_error = self.last_error or "播放线程卡住"
                    self._stop.set()
            finally:
                page_task.cancel()
                # 必须等队列排空、播放线程退出后再放开 ducking；否则麦克风
                # 会把她自己的尾音收进去，触发自问自答。
                self.speaking = False

        played_s = out.played_s
        if out.error:
            self.last_error = out.error
        if dropped:
            self.last_error = f"播放跟不上，丢了 {dropped} 块音频"

        started = bool(started_box[0])
        if self.interrupted:
            # 流式没有逐句边界，只能按"已播时长 / 估算总时长"折算已说文本。
            full = textmod.strip_tags(clean)
            est_total_s = max(0.5, len(full) / max(1.0, float(self.cps or 1.0)))
            ratio = max(0.0, min(1.0, played_s / est_total_s))
            if played_s <= 0.05:
                # 首块 PCM 还没写进声卡就被打断：一句都没说出去。
                upto, spoken = 1, ""
            else:
                upto = max(1, round(len(pages) * ratio)) if pages else 1
                spoken = textmod.strip_tags("".join(pages[:upto]))
            if spoken:
                self._emit_finished(pages, acts, upto, False)
            self.last_ms = {"mode": "stream", "played_s": round(played_s, 2),
                            "est_total_s": round(est_total_s, 2),
                            "spoken_chars": len(spoken), "interrupted": True}
            await self._flush_events()
            return SayResult(ok=False, started=started, text=display,
                             display=display, spoken=spoken, upto=upto,
                             reason="被打断", interrupted=True,
                             pages=list(pages), sync=False, ms=self.last_ms)

        if n_chunks == 0:
            reason = self.last_error or "流式没收到音频"
            return await self._failed_result(
                clean=clean, display=display, pages=pages, acts=acts,
                started=started, sync=False, reason=reason)

        if (not self.mute) and played_s <= 0.05:
            reason = self.last_error or "播放线程没出声"
            return await self._failed_result(
                clean=clean, display=display, pages=pages, acts=acts,
                started=started, sync=False, reason=reason)

        full = textmod.strip_tags(clean)
        spoken = full
        if self.last_error or dropped:
            expected = len(full) / max(1.0, float(self.cps or 1.0))
            ratio = 1.0 if expected <= 0 else played_s / expected
            ratio = max(0.0, min(1.0, ratio))
            if ratio < 0.999:
                keep = int(round(len(full) * ratio))
                spoken = full[:keep]
        upto = self._finish(spoken, played_s, pages, len(full), clean)
        self.last_ms = {"mode": "stream", "chunks": n_chunks,
                        "received_s": round(received_s, 2),
                        "played_s": round(played_s, 2),
                        "first_audio_ms": round((first_at or 0) * 1000),
                        "total_ms": round((time.time() - t0) * 1000)}
        return await self._success_result(
            clean=clean, display=display, pages=pages, acts=acts,
            started=started, sync=False, spoken=spoken, upto=upto,
            ms=self.last_ms,
            reason=stream_error or (self.last_error if spoken != full else None))

    # ── 兜底音频 ──────────────────────────────────────
    async def play_clip(self, path: Path) -> bool:
        try:
            pcm, sr = decode_wav(Path(path).read_bytes())
        except Exception as e:
            self.last_error = f"兜底音频读不了：{e}"
            return False
        async with self._lock:
            self.speaking = True
            self._stop.clear()
            self._reset_mouth()
            try:
                await self._play(pcm, sr, lambda: None)
                return True
            except Exception as e:
                self.last_error = f"兜底音频播不了：{e}"
                return False
            finally:
                self._emit_mouth_silence()
                self._reset_mouth()
                self.speaking = False

    async def close(self) -> None:
        self.stop()
        try:
            await self.provider.close()
        except Exception:
            pass
