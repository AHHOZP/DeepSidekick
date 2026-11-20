"""出口路径：把 Brain 的话交给 Speaker，再把播放事件接到 Display。

	TTS 成功：首音事件 → Display.start；page → Display.page；finished → Display.finish
	TTS 失败/缺失：Display.auto_show 全文兜底；历史仍记录，不 mark_failed
	guard 拒发：不显示、不记录（mark_failed）

Runtime 只负责持有依赖和调度；这里只做“这句话怎么出去”。
"""
from __future__ import annotations

import time

from sidekick.output import guard
from sidekick.core.log import log, one_line
from sidekick.core.ports import SayResult
from sidekick.brain.material import round_why


def _route_display(rt):
    def on_speech(e) -> None:
        if e.kind == "started":
            host = getattr(rt, "stage_motion", None)
            if host is not None:
                host.submit_assets(e.acts, page=0)
            rt.display.start(e.pages, e.acts, e.sync, e.cps, e.duration)
        elif e.kind == "page":
            host = getattr(rt, "stage_motion", None)
            if host is not None:
                host.submit_assets(e.acts, page=e.index)
            rt.display.page(e.index, e.acts, e.duration)
        elif e.kind == "finished":
            rt.display.finish(e.upto)
        elif e.kind == "level":
            rt.stage.emit("level", v=e.level)
        elif e.kind == "mouth":
            rt.stage.emit("mouth", v=e.level, open=e.openness,
                          form=e.form, voiced=bool(e.voiced))
    return on_speech


async def say_line(rt, text: str, acts: list[dict] | None = None) -> SayResult:
    """把一句话送出去；TTS 在就播，不在就走同一条气泡兜底。

    guard 只在这里做一次：有 TTS 时由 Speaker 内部调用同一份 guard；
    无 TTS 时没有 Speaker 兜底，这里先过滤再显示。
    """
    if not rt.speaker.available:
        g = rt.cfg["guard"]
        clean, why = guard.gate(
            text, int(g["max_chars"]), int(g["min_chars"]),
            list(g["extra_patterns"]), allowed_tags=frozenset())
        if why:
            return SayResult(ok=False, rejected=True, reason=why, text=clean)
        host = getattr(rt, "stage_motion", None)
        if host is not None:
            host.submit_assets(acts, page=0)
        await rt.display.auto_show(clean, acts)
        return SayResult(ok=True, text=clean, spoken=clean, pages=[clean])
    r = await rt.speaker.say(text, acts, on_event=_route_display(rt))
    if r.started:
        # Speaker 正常会在返回前发 finished；这里只兜底极端丢事件。
        # BubbleDisplay.finish 是幂等的，不会重复发 end。
        rt.display.finish(r.upto)
    return r


async def talk(rt, snap: dict, out: str, t0: float) -> None:
    rid = snap["round"]
    committed = False
    try:
        back = rt.fb.record_success("brain")
        if back:
            r0 = await say_line(rt, back)
            if r0.ok or r0.started:
                rt.said.add(r0.spoken or back)
                log(f"[{rid}] 恢复：{r0.spoken or back}")

        parsed = rt.actions.parse(out)
        text_out, acts = parsed.text, parsed.items
        for warning in parsed.warnings:
            log(f"[{rid}] 表演标签：{warning}")
        if acts:
            log(f"[{rid}] 表演：{rt.actions.summarize(acts)}")
        if not text_out:
            log(f"[{rid}] 只剩标签没有话，跳过：{one_line(out)}")
            rt.brain.mark_failed(rid)
            committed = True
            return

        r = await say_line(rt, text_out, acts)
        # 首音前失败时用厂商 adapt_text 之前的显示文本，不要把 (laughs) 之类
        # 的标签写进气泡、said 和历史。
        clean = r.display or r.text or text_out
        if r.rejected:
            log(f"[{rid}] 没说出去：{r.reason}；想说：{one_line(text_out)}")
            rt.brain.mark_failed(rid)
            committed = True
            return
        if r.interrupted:
            # 被打断不是故障：不道歉、不兜底、不计 fail_rounds。
            # 历史只留已说出口的部分；一句没说就整对删除（mark_spoken("") 的语义）。
            spoken = r.spoken or ""
            if spoken:
                rt.said.add(spoken)
            rt.brain.mark_spoken(rid, spoken, original=r.display or None)
            committed = True
            log(f"[{rid}] 被打断；已说：{one_line(spoken) or '（无）'}")
            return
        if r.ok and not r.started:
            # 无 TTS：say_line 已经走过 guard 并显示全文，这里只补记录。
            rt.said.add(clean)
            rt.brain.mark_spoken(rid, clean, original=clean)
            committed = True
            log(f"[{rid}] {round_why(snap)} → {clean}"
                   f"  | 无 TTS，气泡兜底 · 脑 {rt.brain.last_ms}ms")
            return
        if r.started:
            spoken = r.spoken or ""
            if spoken:
                rt.said.add(spoken)
            # 历史只保留真正播出的版本：guard / adapt_text 改写过也以 spoken 为准。
            rt.brain.mark_spoken(rid, spoken, original=r.display or None)
            committed = True
            if not r.ok:
                log(f"[{rid}] 首音后语音失败：{r.reason}；已说："
                    f"{one_line(spoken) or '（无）'}；全文：{one_line(clean)}")
                await apologize(rt, "tts")
                return
            ms = r.ms or {}
            log(f"[{rid}] {round_why(snap)} → {spoken}"
                   f"  | 脑 {rt.brain.last_ms}ms"
                   f" · 首个音 {ms.get('first_audio_ms', ms.get('first_sentence_ms'))}ms"
                   f" · 共 {ms.get('total_ms')}ms"
                   f" · 全程 {round((time.time() - t0) * 1000)}ms"
                   f" · {rt.brain.cache_line()}"
                   + (f" · {r.reason}" if r.reason else ""))
            recovered = rt.fb.record_success("tts")
            if recovered:
                rr = await say_line(rt, recovered)
                if rr.ok or rr.started:
                    rt.said.add(rr.spoken or recovered)
                    log(f"[{rid}] 恢复：{rr.spoken or recovered}")
            return

        # 首音前 TTS 失败：气泡兜底显示全文，历史仍记录，不 mark_failed。
        await rt.display.auto_show(clean, acts)
        rt.said.add(clean)
        rt.brain.mark_spoken(rid, clean, original=r.display or clean)
        committed = True
        log(f"[{rid}] 语音失败（首音前）：{r.reason}；气泡兜底：{one_line(clean)}")
        await apologize(rt, "tts")
    finally:
        if not committed:
            rt.brain.mark_failed(rid)


async def apologize(rt, kind: str) -> None:
    if kind == "tts":
        clip = rt.fb.clip(kind)
        line = rt.fb.record_failure(kind, has_clip=clip is not None)
        if not line:
            st = rt.fb.stats()[kind]
            if st.get("degraded"):
                log(f"{kind} 连续失败，已停止道歉（静默降级）")
            return
        if clip is not None and await rt.speaker.play_clip(clip):
            # 预录内容和 POOL 文本不是同一句，不能写进 said/历史。
            log(f"语音挂了，播预录兜底：{clip.name}")
        else:
            log("语音挂了，且没有预录音频"
                   "（state/fallback/tts_*.wav），只能安静")
        return
    line = rt.fb.record_failure(kind)
    if not line:
        st = rt.fb.stats()[kind]
        if st.get("degraded"):
            log(f"{kind} 连续失败，已停止道歉（静默降级）")
        return
    r = await say_line(rt, line)
    if r.ok or r.started:
        rt.said.add(r.spoken or line)
        log(f"兜底出声：{r.spoken or line}")
    elif not r.rejected:
        # 首音前失败：和正常回复一样用气泡显示全文，不能静默。
        await rt.display.auto_show(line)
        rt.said.add(line)
        log(f"兜底语音失败（首音前）：{r.reason}；气泡兜底：{line}")
    else:
        log(f"兜底也没说出来：{r.reason}")
