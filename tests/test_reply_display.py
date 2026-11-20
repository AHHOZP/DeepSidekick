from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import asyncio
import queue
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from sidekick import config
from identity_fixture import runtime_cfg
from sidekick.brain.brain import Brain
from sidekick.brain.worldbook import Worldbook
from sidekick.core.events import EventBus
from sidekick.core.null import (
    InMemoryHistory,
    NullAvatarActions,
    NullFrames,
    NullLongTerm,
    NullMic,
    NullStage,
    NullStt,
)
from sidekick.output import guard
from sidekick.output.display import BubbleDisplay
from sidekick.core.ports import SayResult, SpeechUpdate
from sidekick.app.runtime import Runtime
from sidekick.output.speaker import Speaker
from sidekick.output.tts.base import SynthResult


class FakeProvider:
    allowed_tags = frozenset()
    preserve_square_tags = frozenset()
    supports_stream = False
    name = "fake"
    profile = "fake"
    preset = "edge"
    has_key = True

    def adapt_text(self, text):
        return text


class FakeLLM:
    ready = True
    model = "fake-model"
    source = "fake"
    effort = ""
    effort_dropped = False
    temp = 0.0
    warmup_enabled = False

    def body_defaults(self):
        return {"model": self.model, "temperature": self.temp, "stream": False}

    def request_body(self, messages, temperature=None):
        body = {"messages": messages,
                **self.body_defaults()}
        if temperature is not None:
            body["temperature"] = float(temperature)
        return body

    async def chat(self, messages, **kw):
        return {"choices": [{"message": {"content": "模型原始文本"}}],
                "usage": {}}

    def cache_line(self):
        return ""

    async def close(self):
        return None


class FakeSpeaker:
    available = True
    speaking = False
    busy = False
    last_request_at = 0.0
    profile_name = "fake"
    preset_name = "edge"
    allowed_tags = None
    preserve_square_tags = None
    prompt_rules = ""
    provider = FakeProvider()

    def reload(self, cfg):
        return None

    def __init__(self, result: SayResult, events=()) -> None:
        self.result = result
        self.events = list(events)
        self.seen: list[SayResult] = []

    async def say(self, text, acts=None, *, on_event=None):
        if on_event is not None:
            for e in self.events:
                on_event(e)
        self.seen.append(self.result)
        return self.result

    async def play_clip(self, path) -> bool:
        return False

    async def close(self) -> None:
        return None


class FakeDisplay:
    cps = 4.6

    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.auto: list[str] = []
        self.statuses: list[str] = []
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def status(self, text: str) -> None:
        self.statuses.append(text)

    def start(self, pages, acts=None, sync=False, cps=None,
              duration=0.0) -> None:
        self._active = True
        self.events.append(("start", list(pages), sync))

    def page(self, i: int, acts=None, duration=0.0) -> None:
        self.events.append(("page", i))

    def finish(self, upto: int) -> None:
        if not self._active:
            return
        self._active = False
        self.events.append(("end", upto))

    async def auto_show(self, text: str, acts=None) -> None:
        self.auto.append(text)
        self.events.append(("say", [text]))
        self.events.append(("end", 1))


class FakeActions:
    avatar_id = "test"

    def parse(self, text):
        from sidekick.avatar.act_tags import ActParse

        return ActParse(text=str(text))

    def summarize(self, acts):
        return ""


class FakeBrain:
    last_ms = 12
    last_tasks: list = []

    def __init__(self) -> None:
        self.spoken: list[tuple] = []
        self.failed: list[int] = []

    def mark_spoken(self, rid, spoken, *, original=None):
        self.spoken.append((rid, spoken))

    def mark_failed(self, rid):
        self.failed.append(rid)

    def cache_line(self):
        return ""


class FakeSaid:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, text):
        self.items.append(text)


class FakeFallback:
    def record_success(self, kind):
        return None

    def record_failure(self, kind, **kwargs):
        return None

    def clip(self, kind):
        return None

    def stats(self):
        return {"brain": {"degraded": False}, "tts": {"degraded": False}}


def make_runtime(speaker, display=None) -> Runtime:
    cfg = config.defaults()
    return Runtime(
        cfg=cfg, root=Path("."), events=EventBus(), said=FakeSaid(),
        frames=NullFrames(), mic=NullMic(), stt=NullStt(),
        stage=NullStage(), display=display or FakeDisplay(), speaker=speaker,
        scheduler=SimpleNamespace(current_mode="normal"),
        brain=FakeBrain(), actions=FakeActions(), catalog=None,
        fallback=FakeFallback(), history=InMemoryHistory(4),
        memory=NullLongTerm(), session=SimpleNamespace(mode="stream"))


def snap() -> dict:
    return {"round": 7, "reason": "我说话", "trigger": "我说话",
            "waited_s": 0.0, "events": 1, "meta": {"must_reply": True}}


class _ToneProvider:
    name = "tone"
    profile = "tone"
    preset = "edge"
    has_key = True
    allowed_tags = frozenset()
    preserve_square_tags = frozenset()
    supports_stream = False

    def adapt_text(self, text):
        return text

    async def synth(self, text):
        return SynthResult(pcm=np.zeros(2400, dtype=np.float32), sr=24000)


class _FailingTagProvider(_ToneProvider):
    name = "failing-tag"

    def adapt_text(self, text):
        return "（小声）" + str(text)

    async def synth(self, text):
        raise RuntimeError("synth down")


class _StreamProvider:
    name = "tone-stream"
    profile = "tone-stream"
    preset = "openai"
    has_key = True
    allowed_tags = frozenset()
    preserve_square_tags = frozenset()
    supports_stream = True
    stream_sr = 24000

    def adapt_text(self, text):
        return text

    async def stream(self, text):
        for _ in range(10):
            await asyncio.sleep(0)
            yield np.full(2400, 0.05, dtype=np.float32)


class _StopAfterThreeBlocksPlayer:
    """播放线程只吃掉 3 块（=0.3s）就当作被打断，剩下的块不再播。

    真实播放器是 sounddevice 的输出流，单测里没有声卡；契约只看
    `dst_sr` / `nch` / `configure` / `run` / `played_s` / `error`。
    """

    def __init__(self, *, mute=False, src_sr=24000):
        self.mute = mute
        self.src_sr = int(src_sr)
        self.dst_sr = int(src_sr)
        self.nch = 1
        self.error = None
        self.played_s = 0.0
        self.started = False
        self.left = 3
        self.speaker = None

    def configure(self):
        return None

    def run(self, q, sentinel, stop_event, *, on_first=None, on_level=None):
        while True:
            try:
                item = q.get(timeout=0.2)
            except queue.Empty:
                return
            if item is sentinel:
                return
            if not self.started:
                self.started = True
                if on_first is not None:
                    on_first()
            self.played_s += len(item) / float(self.dst_sr or 1)
            self.left -= 1
            if self.left <= 0:
                if self.speaker is not None:
                    self.speaker.stop()
                return


class SpeakerStreamInterruptTests(QuietIsolatedAsyncioTestCase):
    async def test_stream_interrupt_estimates_spoken_text(self):
        cfg = config.defaults()
        cfg["tts"]["stream"] = True
        speaker = Speaker(cfg, provider=_StreamProvider())
        fake = _StopAfterThreeBlocksPlayer()
        fake.speaker = speaker
        try:
            with mock.patch("sidekick.output.speaker.player.StreamPlayer",
                            return_value=fake):
                result = await speaker.say("第一句。第二句。第三句。")
        finally:
            await speaker.close()
        self.assertTrue(result.interrupted)
        self.assertFalse(result.ok)
        self.assertFalse(result.failed)
        self.assertTrue(result.started)
        # 0.3s / 估算 2.6s ≈ 11%：只算第一页。
        self.assertEqual(result.upto, 1)
        self.assertEqual(result.spoken, "第一句。")
        self.assertEqual(speaker.fail_rounds, 0)


class GuardPrecisionTests(unittest.TestCase):
    def test_game_server_and_quest_key_are_not_blocked(self):
        self.assertIsNone(guard.check("这个游戏服务器太卡了"))
        self.assertIsNone(guard.check("任务密钥在哪儿"))

    def test_system_server_and_api_key_are_blocked(self):
        self.assertIsNotNone(guard.check("后台服务器地址"))
        self.assertIsNotNone(guard.check("API密钥"))

    def test_invalid_extra_pattern_warns_once(self):
        guard._WARNED_PATTERNS.clear()
        with mock.patch("sidekick.output.guard.log") as log:
            guard.check("普通", ["["])
            guard.check("普通", ["["])
        self.assertEqual(1, log.call_count)


class MarkSpokenEllipsisTests(unittest.TestCase):
    def test_truncated_reply_gets_ellipsis(self):
        class History:
            text = ""

            def update_assistant(self, assistant, text):
                self.text = text

            def drop_pair(self, *args):
                return None

        class Summary:
            def maybe_start(self):
                return None

        brain = object.__new__(Brain)
        brain.history = History()
        brain.summary = Summary()
        brain.prompt = SimpleNamespace(actions=NullAvatarActions())
        brain._pending_exchange = {
            1: ({"role": "user", "content": "问"},
                {"role": "assistant", "content": "前半句。后半句。"})}
        brain.mark_spoken(1, "前半句")
        self.assertEqual("前半句…", brain.history.text)

        brain._pending_exchange = {
            2: ({"role": "user", "content": "问"},
                {"role": "assistant", "content": "完整。"})}
        brain.mark_spoken(2, "完整。")
        self.assertEqual("完整。", brain.history.text)

        brain._pending_exchange = {
            3: ({"role": "user", "content": "问"},
                {"role": "assistant", "content": "你好<ACT>action=wave</ACT>"})}
        brain.mark_spoken(3, "你好")
        self.assertEqual("你好", brain.history.text)

        # guard 在出口删掉 emoji 后，不能拿未清洗的原文判成“截断”。
        brain._pending_exchange = {
            4: ({"role": "user", "content": "问"},
                {"role": "assistant", "content": "你好😄"})}
        brain.mark_spoken(4, "你好", original="你好")
        self.assertEqual("你好", brain.history.text)


class SpeakerPageEventTests(QuietIsolatedAsyncioTestCase):
    async def test_prefetch_does_not_move_request_clock(self):
        speaker = Speaker(config.defaults(), provider=_ToneProvider())
        speaker.last_request_at = 123.0
        try:
            await speaker._synth_safe("预取一句", prefetch=True)
            self.assertEqual(123.0, speaker.last_request_at)
            await speaker._synth_safe("正文一句")
            self.assertGreater(speaker.last_request_at, 123.0)
        finally:
            await speaker.close()

    async def test_display_text_excludes_provider_adaptation(self):
        speaker = Speaker(config.defaults(), provider=_FailingTagProvider())
        speaker.retry_delay = 0
        try:
            result = await speaker.say("你好")
        finally:
            await speaker.close()
        self.assertFalse(result.ok)
        self.assertEqual(result.text, "你好")
        self.assertEqual(result.display, "你好")

    async def test_stop_releases_speaking_immediately(self):
        speaker = Speaker(config.defaults(), provider=_ToneProvider())
        speaker.speaking = True
        speaker.stop()
        self.assertFalse(speaker.speaking)
        await speaker.close()

    async def test_stop_mid_say_is_interrupted_not_a_failure(self):
        speaker = Speaker(config.defaults(), provider=_ToneProvider())
        speaker.mute = True

        def handler(e):
            if e.kind == "started":
                speaker.stop()

        try:
            result = await speaker.say("第一句。第二句。第三句。",
                                       on_event=handler)
        finally:
            await speaker.close()
        self.assertTrue(result.interrupted)
        self.assertFalse(result.ok)
        self.assertFalse(result.failed)
        self.assertEqual(result.reason, "被打断")
        # 只说到第一句：后面两句不再播。
        self.assertEqual(result.spoken, "第一句。")
        self.assertEqual(result.upto, 1)
        self.assertEqual(speaker.fail_rounds, 0)


class ReplyPathTests(QuietIsolatedAsyncioTestCase):
    def setUp(self):
        self._log = mock.patch("sidekick.output.reply.log", lambda *a, **k: None)
        self._log.start()

    def tearDown(self):
        self._log.stop()

    async def test_no_tts_autoshows_full_text_and_keeps_history(self):
        speaker = FakeSpeaker(SayResult())
        speaker.available = False
        rt = make_runtime(speaker)
        await rt._talk(snap(), "你好呀，今天也一起玩。", 0.0)
        self.assertEqual(rt.display.auto, ["你好呀，今天也一起玩。"])
        self.assertEqual(rt.said.items, ["你好呀，今天也一起玩。"])
        self.assertEqual(rt.brain.failed, [])
        self.assertEqual(rt.brain.spoken[0][1], "你好呀，今天也一起玩。")

    async def test_failure_before_first_sound_autoshows(self):
        speaker = FakeSpeaker(SayResult(
            ok=False, failed=True, started=False, text="（小声）你好",
            display="你好", reason="合成失败"))
        rt = make_runtime(speaker)
        logs = []
        with mock.patch("sidekick.output.reply.log", logs.append):
            await rt._talk(snap(), "你好", 0.0)
        self.assertEqual(rt.display.auto, ["你好"])
        self.assertEqual(rt.said.items, ["你好"])
        self.assertEqual(rt.brain.failed, [])
        self.assertEqual(rt.brain.spoken[0][1], "你好")
        self.assertTrue(any("气泡兜底：你好" in line for line in logs))

    async def test_started_then_failure_keeps_bubble_and_spoken(self):
        events = [SpeechUpdate(kind="started", pages=["前半"], sync=True)]
        speaker = FakeSpeaker(
            SayResult(ok=False, started=True, failed=True, spoken="前半",
                      upto=1, text="前半后半"),
            events)
        rt = make_runtime(speaker)
        await rt._talk(snap(), "前半后半", 0.0)
        self.assertEqual(rt.display.auto, [])
        self.assertIn(("start", ["前半"], True), rt.display.events)
        self.assertIn(("end", 1), rt.display.events)
        self.assertEqual(rt.said.items, ["前半"])
        self.assertEqual(rt.brain.failed, [])
        self.assertEqual(rt.brain.spoken[0][1], "前半")

    async def test_page_events_follow_started(self):
        events = [
            SpeechUpdate(kind="started", pages=["第一句。", "第二句。"], sync=True),
            SpeechUpdate(kind="page", index=1),
            SpeechUpdate(kind="finished", upto=2),
        ]
        speaker = FakeSpeaker(
            SayResult(ok=True, started=True, text="第一句。第二句。",
                      spoken="第一句。第二句。", pages=["第一句。", "第二句。"],
                      sync=True, upto=2),
            events)
        rt = make_runtime(speaker)
        await rt._talk(snap(), "第一句。第二句。", 0.0)
        kinds = [e[0] for e in rt.display.events]
        self.assertEqual(kinds[0], "start")
        self.assertEqual(kinds.count("start"), 1)
        self.assertEqual(kinds.count("end"), 1)
        self.assertIn("page", kinds)
        self.assertEqual(rt.display.auto, [])
        self.assertEqual(rt.brain.failed, [])

    async def test_interrupted_keeps_only_spoken_part_and_does_not_apologize(self):
        events = [SpeechUpdate(kind="started", pages=["前半。"], sync=False)]
        speaker = FakeSpeaker(
            SayResult(ok=False, started=True, interrupted=True, spoken="前半。",
                      text="前半。后半。", upto=1, reason="被打断"),
            events)
        rt = make_runtime(speaker)
        logs = []
        with mock.patch("sidekick.output.reply.log", logs.append):
            await rt._talk(snap(), "前半。后半。", 0.0)
        self.assertEqual(rt.said.items, ["前半。"])
        self.assertEqual(rt.brain.spoken, [(7, "前半。")])
        self.assertEqual(rt.brain.failed, [])
        # 被打断不兜底显示全文、不道歉。
        self.assertEqual(rt.display.auto, [])
        self.assertTrue(any("被打断" in line for line in logs))

    async def test_interrupted_before_any_sound_drops_the_history_pair(self):
        speaker = FakeSpeaker(
            SayResult(ok=False, started=False, interrupted=True, spoken="",
                      text="整句", reason="被打断"))
        rt = make_runtime(speaker)
        with mock.patch("sidekick.output.reply.log", lambda *a, **k: None):
            await rt._talk(snap(), "整句", 0.0)
        # mark_spoken("") 的语义是"整对删除"，她一句没说。
        self.assertEqual(rt.brain.spoken, [(7, "")])
        self.assertEqual(rt.said.items, [])
        self.assertEqual(rt.brain.failed, [])
        self.assertEqual(rt.display.auto, [])

    async def test_guard_rejection_does_not_show_bubble(self):
        speaker = FakeSpeaker(SayResult(
            ok=False, rejected=True, reason="命中「网址」",
            text="看这个"))
        rt = make_runtime(speaker)
        await rt._talk(snap(), "看这个 http://example.com", 0.0)
        self.assertEqual(rt.display.auto, [])
        self.assertEqual(len(speaker.seen), 1)
        self.assertEqual(rt.brain.failed, [7])
        self.assertEqual(rt.said.items, [])

    async def test_brain_apology_falls_back_to_bubble(self):
        class FailFallback(FakeFallback):
            def record_failure(self, kind):
                return "我这边卡了一下，你先说。"

        speaker = FakeSpeaker(SayResult(
            ok=False, started=False, failed=True, reason="合成失败",
            text="我这边卡了一下，你先说。"))
        rt = make_runtime(speaker)
        rt.fb = FailFallback()
        await rt._apologize("brain")
        self.assertEqual(rt.display.auto, ["我这边卡了一下，你先说。"])
        self.assertEqual(rt.said.items, ["我这边卡了一下，你先说。"])


class DisplayTests(QuietIsolatedAsyncioTestCase):
    async def test_status_events_are_forwarded(self):
        class Stage:
            clients = 1

            def __init__(self):
                self.events = []

            def emit(self, event, **kw):
                self.events.append((event, kw))

        stage = Stage()
        display = BubbleDisplay(stage, cps=10)
        display.status("思考中")
        display.status("")
        self.assertEqual([e[0] for e in stage.events], ["status", "status"])
        self.assertEqual(stage.events[0][1]["text"], "思考中")
        self.assertEqual(stage.events[1][1]["text"], "")

    async def test_auto_show_pushes_say_and_end(self):
        class Stage:
            clients = 0

            def __init__(self):
                self.events = []

            def emit(self, event, **kw):
                self.events.append((event, kw))

        stage = Stage()
        display = BubbleDisplay(stage, cps=10)
        await display.auto_show("你好呀。今天也一起玩。")
        kinds = [e[0] for e in stage.events]
        self.assertEqual(kinds, ["say", "end"])
        self.assertEqual(stage.events[0][1]["pages"], ["你好呀。", "今天也一起玩。"])

    async def test_status_does_not_overwrite_active_speech(self):
        class Stage:
            clients = 1

            def __init__(self):
                self.events = []

            def emit(self, event, **kw):
                self.events.append((event, kw))

        stage = Stage()
        display = BubbleDisplay(stage, cps=10)
        display.start(["正在说话"])
        display.status("思考中")
        self.assertEqual([e[0] for e in stage.events], ["say"])

    async def test_start_finish_idempotent(self):
        class Stage:
            clients = 0

            def __init__(self):
                self.events = []

            def emit(self, event, **kw):
                self.events.append((event, kw))

        stage = Stage()
        display = BubbleDisplay(stage, cps=10)
        display.start(["一", "二"], sync=True)
        display.page(1)
        display.finish(2)
        display.finish(2)
        self.assertEqual([e[0] for e in stage.events], ["say", "page", "end"])


class FakeTtsProvider:
    name = "fake"
    profile = "fake"
    preset = "edge"
    has_key = True
    supports_stream = False
    allowed_tags = frozenset()
    preserve_square_tags = frozenset()

    def adapt_text(self, text):
        return text

    async def synth(self, text, style=None):
        return SynthResult(np.zeros(240, dtype=np.float32), 24000)

    async def close(self):
        return None


class AdaptBoomProvider(FakeTtsProvider):
    def adapt_text(self, text):
        raise RuntimeError("adapt boom")


class StreamProvider(FakeTtsProvider):
    supports_stream = True
    stream_sr = 24000

    async def stream(self, text, style=None):
        yield np.zeros(24000, dtype=np.float32)


class SpeakerEventTests(QuietIsolatedAsyncioTestCase):
    def test_speaker_available_follows_provider_key(self):
        class NoKey(FakeTtsProvider):
            has_key = False

        cfg = config.defaults()
        self.assertFalse(Speaker(cfg, provider=NoKey()).available)
        self.assertTrue(Speaker(cfg, provider=FakeTtsProvider()).available)

    async def test_started_fires_before_finished_in_mute_mode(self):
        cfg = config.defaults()
        speaker = Speaker(cfg, provider=FakeTtsProvider())
        speaker.mute = True
        events = []
        result = await speaker.say("你好呀", on_event=events.append)
        self.assertTrue(result.started)
        self.assertTrue(result.ok)
        kinds = [e.kind for e in events]
        self.assertEqual(kinds[0], "started")
        self.assertEqual(kinds[-1], "finished")
        await speaker.close()

    async def test_adapt_error_returns_say_result_instead_of_raising(self):
        cfg = config.defaults()
        speaker = Speaker(cfg, provider=AdaptBoomProvider())
        speaker.mute = True
        result = await speaker.say("你好呀")
        self.assertFalse(result.ok)
        self.assertFalse(result.started)
        self.assertTrue(result.failed)
        self.assertIn("adapt boom", result.reason or "")
        self.assertEqual(speaker.fail_rounds, 1)
        await speaker.close()

    async def test_stream_keeps_ducking_until_playback_thread_exits(self):
        class SlowPlayer:
            dst_sr = 24000
            nch = 1
            error = None

            def __init__(self):
                self.played_s = 0.0

            def configure(self):
                return None

            def run(self, q, sentinel, stop, *, on_first, on_level):
                on_first()
                while True:
                    item = q.get()
                    if item is sentinel:
                        break
                time.sleep(0.2)      # 模拟声卡缓冲区还没播完
                self.played_s = 1.0

        slow = SlowPlayer()
        cfg = config.defaults()
        cfg["tts"]["stream"] = True
        speaker = Speaker(cfg, provider=StreamProvider())
        speaker.mute = True
        with mock.patch("sidekick.output.speaker.player.StreamPlayer", return_value=slow):
            task = asyncio.create_task(speaker.say("你好呀"))
            await asyncio.sleep(0.05)
            self.assertTrue(speaker.speaking)
            result = await task
        self.assertFalse(speaker.speaking)
        self.assertTrue(result.ok)
        await speaker.close()

    async def test_guard_rejection_returns_rejected_result_without_synth(self):
        class NoSynth(FakeTtsProvider):
            calls = 0

            async def synth(self, text, style=None):
                self.calls += 1
                return await super().synth(text, style)

        provider = NoSynth()
        cfg = config.defaults()
        speaker = Speaker(cfg, provider=provider)
        speaker.mute = True
        result = await speaker.say("看 https://example.com")
        self.assertTrue(result.rejected)
        self.assertFalse(result.started)
        self.assertEqual(provider.calls, 0)
        await speaker.close()

    async def test_reload_updates_guard_snapshot(self):
        cfg = config.defaults()
        speaker = Speaker(cfg, provider=FakeTtsProvider())
        new = config.defaults()
        new["guard"]["max_chars"] = 12
        new["guard"]["extra_patterns"] = ["自定义"]
        new["tts"]["stream"] = True
        speaker.reload(new)
        self.assertEqual(speaker.max_chars, 12)
        self.assertEqual(speaker.extra_patterns, ["自定义"])
        self.assertTrue(speaker.stream)
        await speaker.close()

    async def test_stream_thread_start_failure_resets_ducking(self):
        class DummyOut:
            dst_sr = 24000
            nch = 1
            error = None
            played_s = 0.0

            def configure(self):
                return None

            def run(self, *a, **k):
                return None

        class BoomThread:
            def __init__(self, *a, **k):
                pass

            def start(self):
                raise RuntimeError("no thread")

        cfg = config.defaults()
        cfg["tts"]["stream"] = True
        speaker = Speaker(cfg, provider=StreamProvider())
        speaker.mute = True
        with mock.patch("sidekick.output.speaker.player.StreamPlayer", return_value=DummyOut()), \
                mock.patch("sidekick.output.speaker.threading.Thread", BoomThread):
            result = await speaker.say("你好呀")
        self.assertFalse(result.ok)
        self.assertTrue(result.failed)
        self.assertFalse(speaker.speaking)
        await speaker.close()

    async def test_history_uses_spoken_text_not_model_text(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            history = InMemoryHistory(10)
            cfg = runtime_cfg(root)
            brain = Brain(
                cfg, history=history, memory=NullLongTerm(),
                worldbook=Worldbook(cfg, root),
                actions=NullAvatarActions(), llm=FakeLLM(), root=root,
                log=lambda *a, **k: None)
            self.assertEqual(await brain.reply("材料", round_id=7), "模型原始文本")

            speaker = FakeSpeaker(SayResult(
                ok=True, started=True, text="模型原始文本",
                spoken="实际播出的文本", pages=["实际播出的文本"], upto=1))
            rt = make_runtime(speaker)
            rt.brain = brain
            rt.history = history
            with mock.patch("sidekick.output.reply.log", lambda *a, **k: None):
                await rt._talk(snap(), "模型原始文本", time.time())
            rows = history.messages()
            self.assertEqual(rows[-1]["content"], "实际播出的文本")


if __name__ == "__main__":
    unittest.main()
