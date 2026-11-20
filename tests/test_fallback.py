from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import tempfile
import unittest
from pathlib import Path

import numpy as np

from sidekick import config
from sidekick.output.fallback import Fallback
from sidekick.output.speaker import Speaker
from sidekick.output.tts.base import Provider, SynthResult


class Broken(Provider):
    """永远失败的 provider，数一下被调了几次。"""

    name = "broken"

    def __init__(self) -> None:
        self.calls = 0

    async def synth(self, text, style=None):
        self.calls += 1
        raise RuntimeError("模拟：HTTP 502")


class Flaky(Provider):
    """第一次失败、第二次成功。"""

    name = "flaky"

    def __init__(self) -> None:
        self.calls = 0

    async def synth(self, text, style=None):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("模拟：第一次超时")
        return SynthResult(pcm=np.zeros(2400, dtype="float32"), sr=24000)


def speaker_with(provider, *, mute: bool = True) -> Speaker:
    cfg = config.defaults()
    cfg["tts"] = {**cfg["tts"], "stream": False, "segments": "sentence"}
    speaker = Speaker(cfg, provider=provider)
    speaker.mute = mute
    speaker.retry_delay = 0.0
    speaker.max_chars = 40
    return speaker


def fallback_cfg(**errors) -> dict:
    cfg = config.defaults()
    cfg["errors"] = {**cfg["errors"], **errors}
    return cfg


class SpeakerRetryTests(QuietIsolatedAsyncioTestCase):
    async def test_always_failing_provider_is_called_twice(self):
        speaker = speaker_with(Broken())
        result = await speaker.say("这是一句测试。")
        self.assertFalse(result.ok)
        self.assertTrue(result.failed)
        self.assertEqual(speaker.provider.calls, 2)
        await speaker.close()

    async def test_second_attempt_can_succeed(self):
        speaker = speaker_with(Flaky())
        result = await speaker.say("这是一句测试。")
        self.assertTrue(result.ok)
        self.assertEqual(speaker.provider.calls, 2)
        await speaker.close()


class FallbackTests(unittest.TestCase):
    def test_cooldown_breaker_and_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            fb = Fallback(fallback_cfg(fallback_cooldown_s=0,
                                       fallback_max_apologies=3,
                                       recover_say_s=0),
                          root=Path(td))
            lines = [fb.record_failure("brain") for _ in range(6)]
            self.assertTrue(all(lines[:3]))
            self.assertTrue(all(line is None for line in lines[3:]))
            self.assertTrue(fb.stats()["brain"]["degraded"])
            self.assertTrue(fb.record_success("brain"))
            self.assertEqual(fb.stats()["brain"]["streak"], 0)

    def test_shuffle_bag_does_not_repeat_back_to_back(self):
        with tempfile.TemporaryDirectory() as td:
            fb = Fallback(fallback_cfg(fallback_cooldown_s=0,
                                       fallback_max_apologies=99),
                          root=Path(td))
            lines = [fb.record_failure("brain") for _ in range(12)]
            self.assertFalse(any(a == b for a, b in zip(lines, lines[1:])))
            self.assertEqual(len(set(lines)), 10)


if __name__ == "__main__":
    unittest.main()
