from __future__ import annotations

import asyncio
import unittest

import numpy as np

from sidekick.avatar.stage import _LIVE_EVENTS, _drop_live
from sidekick.core.ports import SpeechUpdate
from sidekick.output.reply import _route_display
from sidekick.output.speaker import Speaker


class MouthPipelineTests(unittest.TestCase):
    def test_push_level_emits_50hz_mouth_frames(self):
        speaker = object.__new__(Speaker)
        speaker.play_ref = (0.0, 0.0)
        speaker._analyzer = None
        speaker._analyzer_sr = 0
        speaker.mouth_sink = None
        speaker.mouth_reset = None
        speaker._loop = None
        speaker._owner = None
        updates = []
        speaker._on_event = updates.append

        sr = 48000
        t = np.arange(int(sr * 0.1), dtype=np.float32) / sr
        chunk = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        speaker._push_level(chunk, sr)
        speaker._push_level(chunk, sr)

        mouths = [u for u in updates if u.kind == "mouth"]
        self.assertGreaterEqual(len(mouths), 5)
        self.assertTrue(all(0.0 <= u.openness <= 1.0 for u in mouths))
        self.assertTrue(all(-1.0 <= u.form <= 1.0 for u in mouths))
        self.assertTrue(all(np.isfinite([u.level, u.openness, u.form]).all()
                            for u in mouths))
        self.assertGreater(speaker.play_ref[0], 0.0)

    def test_finished_emits_closed_mouth(self):
        speaker = object.__new__(Speaker)
        speaker._analyzer = None
        speaker._analyzer_sr = 0
        speaker.mouth_sink = None
        speaker.mouth_reset = None
        speaker._loop = None
        speaker._owner = None
        speaker.cps = 4.6
        updates = []
        speaker._on_event = updates.append
        speaker._emit_finished([], [], 1, False)
        self.assertEqual(updates[0].kind, "mouth")
        self.assertEqual(updates[0].openness, 0.0)
        self.assertEqual(updates[1].kind, "finished")


class StageDropTests(unittest.TestCase):
    def test_live_frames_are_dropped_controls_kept(self):
        q = asyncio.Queue()
        for item in (
                {"t": "say"}, {"t": "level"}, {"t": "mouth"},
                {"t": "end"}, {"t": "pose"}, {"t": "control"}):
            q.put_nowait(item)
        _drop_live(q)
        kept = []
        while not q.empty():
            kept.append(q.get_nowait())
        self.assertEqual([x["t"] for x in kept], ["say", "end", "control"])
        self.assertEqual(set(_LIVE_EVENTS), {"level", "mouth", "pose"})


class ReplyRouteTests(unittest.TestCase):
    def test_mouth_is_forwarded_to_stage(self):
        class Stage:
            def __init__(self):
                self.sent = []

            def emit(self, event, **kw):
                self.sent.append((event, kw))

        class Rt:
            stage = Stage()

        handler = _route_display(Rt())
        handler(SpeechUpdate(kind="mouth", level=0.4, openness=0.7,
                             form=-0.2, voiced=True))
        self.assertEqual(Rt.stage.sent, [("mouth", {
            "v": 0.4, "open": 0.7, "form": -0.2, "voiced": True})])


if __name__ == "__main__":
    unittest.main()
