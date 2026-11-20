from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import unittest

import numpy as np

from sidekick import config
from sidekick.output.display import BubbleDisplay
from sidekick.output.speaker import Speaker
from sidekick.output.tts.base import SynthResult


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
        return SynthResult(pcm=np.zeros(240, dtype=np.float32), sr=24000)

    async def close(self):
        return None


class FakeStage:
    clients = 1

    def __init__(self):
        self.events = []

    def emit(self, event, **kw):
        self.events.append((event, kw))


class SpeakerDisplayIntegrationTests(QuietIsolatedAsyncioTestCase):
    async def test_three_sentences_emit_exact_pages_and_end_upto(self):
        stage = FakeStage()
        display = BubbleDisplay(stage, cps=10)
        speaker = Speaker(config.defaults(), provider=_ToneProvider())
        speaker.mute = True
        try:
            result = await speaker.say(
                "第一句。第二句。第三句。",
                on_event=lambda e: (
                    display.start(e.pages, e.acts, e.sync, e.cps)
                    if e.kind == "started" else
                    display.page(e.index) if e.kind == "page" else
                    display.finish(e.upto) if e.kind == "finished" else None))
        finally:
            await speaker.close()

        self.assertTrue(result.ok)
        self.assertEqual(result.spoken, "第一句。第二句。第三句。")
        self.assertEqual(result.pages, ["第一句。", "第二句。", "第三句。"])
        self.assertEqual([e[0] for e in stage.events],
                         ["say", "page", "page", "end"])
        self.assertEqual(stage.events[0][1]["pages"],
                         ["第一句。", "第二句。", "第三句。"])
        self.assertEqual([e[1]["idx"] for e in stage.events[1:3]], [1, 2])
        self.assertEqual(stage.events[-1][1]["upto"], 3)
        self.assertEqual(stage.events[-1][1]["pages"], 3)


if __name__ == "__main__":
    unittest.main()
