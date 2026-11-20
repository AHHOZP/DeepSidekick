from __future__ import annotations

import sys
import time
import types
import unittest
from unittest import mock

import numpy as np

from sidekick import config
from sidekick.input.mic import Mic


class FakeInputStream:
    def __init__(self, levels, mic):
        self.levels = list(levels)
        self.mic = mic
        self.i = 0
        self.block = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, block):
        self.block = block
        if self.i >= len(self.levels):
            raise StopIteration("录音结束")
        level = float(self.levels[self.i])
        self.i += 1
        return np.full((block, 1), level, dtype=np.float32), None


def fake_sounddevice(mic, levels):
    stream = FakeInputStream(levels, mic)
    module = types.SimpleNamespace(
        query_devices=lambda kind="input": {"default_samplerate": 48000},
        InputStream=lambda **kw: stream,
    )
    return module, stream


class MicCaptureTests(unittest.TestCase):
    def _cfg(self, **mic):
        cfg = config.defaults()
        cfg["mic"].update({
            "mode": "speaker", "gate_percent": 100,
            "continue_ratio": 0.4, "start_blocks": 3,
            "silence_ms": 120, "min_speech_ms": 60, "max_segment_s": 30,
        })
        cfg["mic"].update(mic)
        return cfg

    def test_capture_emits_trimmed_segments(self):
        got = []
        mic = Mic(self._cfg(), on_segment=lambda pcm, sr: got.append(pcm))
        levels = [0.1] * 3 + [0.001] * 4 + [0.1] * 3 + [0.001] * 4
        module, _stream = fake_sounddevice(mic, levels)
        with mock.patch.dict(sys.modules, {"sounddevice": module}):
            with self.assertRaises(StopIteration):
                mic._capture()
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0].size, 3 * 1440)
        self.assertEqual(got[1].size, 3 * 1440)
        self.assertAlmostEqual(float(np.max(got[0])), 0.1)

    def test_gate_is_published_for_status(self):
        mic = Mic(self._cfg())
        module, _stream = fake_sounddevice(mic, [0.001] * 4)
        with mock.patch.dict(sys.modules, {"sounddevice": module}):
            with self.assertRaises(StopIteration):
                mic._capture()
        self.assertGreater(mic.gate, 0.0)
        self.assertEqual(mic.noise, 0.0)     # warmup 期还没估计出来
        self.assertEqual(mic.mode, "speaker")
        self.assertEqual(mic.mode_cfg, "speaker")
        self.assertEqual(mic.gate_percent, 100)

    def test_playback_still_feeds_vad(self):
        got = []
        mic = Mic(self._cfg(), on_segment=lambda pcm, sr: got.append(pcm))
        mic.is_playing = lambda: True
        mic.play_ref = lambda: (0.002, time.monotonic())
        levels = [0.2] * 3 + [0.001] * 4
        module, _stream = fake_sounddevice(mic, levels)
        with mock.patch.dict(sys.modules, {"sounddevice": module}):
            with self.assertRaises(StopIteration):
                mic._capture()
        # 播放期不再整段吞掉：VAD 照常工作，TTS 漏音交给 barge/门限。
        self.assertEqual(len(got), 1)
        self.assertGreater(mic.last_voice_at, 0.0)
        self.assertEqual(mic.speech_ms, 0.0)

    def test_playback_echo_gate_does_not_cut_an_open_segment(self):
        got = []
        mic = Mic(self._cfg(), on_segment=lambda pcm, sr: got.append(pcm))
        reads = 0

        def playing():
            nonlocal reads
            reads += 1
            return reads > 60

        mic.is_playing = playing
        mic.play_ref = lambda: (0.1, time.monotonic())
        levels = [0.02] * 260 + [0.001] * 10
        module, _stream = fake_sounddevice(mic, levels)
        with mock.patch.dict(sys.modules, {"sounddevice": module}):
            with self.assertRaises(StopIteration):
                mic._capture()
        self.assertEqual(len(got), 1)
        self.assertGreater(got[0].size, 200 * 1440)

    def test_headset_barge_works_while_playing(self):
        hits = []
        cfg = self._cfg(mode="headset")
        cfg["barge"]["min_ms"] = 90
        mic = Mic(cfg)
        mic.on_barge = lambda: hits.append(1)
        mic.is_playing = lambda: True
        mic.play_ref = lambda: (0.0, 0.0)
        module, _stream = fake_sounddevice(mic, [0.2] * 12)
        with mock.patch.dict(sys.modules, {"sounddevice": module}):
            with self.assertRaises(StopIteration):
                mic._capture()
        self.assertTrue(hits)
        self.assertGreater(mic.barge_triggers, 0)
if __name__ == "__main__":
    unittest.main()
