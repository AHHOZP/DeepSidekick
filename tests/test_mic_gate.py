"""自适应门限：纯函数 + 噪声估计，不碰硬件。"""
from __future__ import annotations

import unittest

from sidekick.input.vad import (
    GATE_CEIL, GATE_PERCENT_MAX, GATE_PERCENT_MIN, HIGH_MARGIN,
    START_FLOOR, NoiseFloor, Segmenter, VadParams, start_gate_of,
)
from sidekick.input.devices import (
    KIND_HEADSET, KIND_SPEAKER, output_kind, resolve_mode,
)

BLOCK_MS = 30.0


class DeviceKindTests(unittest.TestCase):
    def test_headset_words_win_over_brand_words(self):
        # 顺序有意义：Realtek 的耳机名里也带品牌词，不能先被当成扬声器。
        self.assertEqual(output_kind("Headphones (Realtek(R) Audio)"),
                         KIND_HEADSET)
        self.assertEqual(output_kind("耳机 (USB Audio)"), KIND_HEADSET)
        self.assertEqual(output_kind("Headset Earphone (USB Audio)"),
                         KIND_HEADSET)

    def test_speaker_words(self):
        self.assertEqual(output_kind("扬声器 (Realtek(R) Audio)"),
                         KIND_SPEAKER)
        self.assertEqual(output_kind("显示器 (NVIDIA High Definition Audio)"),
                         KIND_SPEAKER)
        self.assertEqual(output_kind("HDMI Output"), KIND_SPEAKER)

    def test_unknown_falls_back_to_speaker(self):
        self.assertIsNone(output_kind(""))
        self.assertIsNone(output_kind("USB Audio Device"))
        self.assertEqual(resolve_mode("auto", ""), KIND_SPEAKER)
        self.assertEqual(resolve_mode("auto", "USB Audio Device"), KIND_SPEAKER)

    def test_explicit_mode_overrides_the_name(self):
        self.assertEqual(resolve_mode("headset", "扬声器"), KIND_HEADSET)
        self.assertEqual(resolve_mode("speaker", "Headphones"), KIND_SPEAKER)


class GateTests(unittest.TestCase):
    def test_cold_start_uses_fixed_floor(self):
        self.assertAlmostEqual(start_gate_of(None, None, 100), START_FLOOR,
                               places=6)

    def test_gate_never_above_ceiling(self):
        self.assertEqual(start_gate_of(1.0, 1.0, 150), GATE_CEIL)

    def test_gate_percent_is_monotonic_and_clamped(self):
        self.assertLess(start_gate_of(0.01, 0.02, 50),
                        start_gate_of(0.01, 0.02, 100))
        self.assertGreater(start_gate_of(0.01, 0.02, 150),
                           start_gate_of(0.01, 0.02, 100))
        self.assertEqual(start_gate_of(0.01, 0.02, 0),
                         start_gate_of(0.01, 0.02, GATE_PERCENT_MIN))
        self.assertEqual(start_gate_of(0.01, 0.02, 999),
                         start_gate_of(0.01, 0.02, GATE_PERCENT_MAX))

    def test_noise_and_ambient_peak_raise_start_gate(self):
        quiet = start_gate_of(0.0001, 0.0002, 100)
        noise = start_gate_of(0.01, 0.0002, 100)
        loud = start_gate_of(0.0001, 0.02, 100)
        self.assertGreater(noise, quiet)
        self.assertGreater(loud, quiet)
        self.assertGreaterEqual(loud, 0.02 * HIGH_MARGIN)


class NoiseFloorTests(unittest.TestCase):
    def _filled(self) -> NoiseFloor:
        est = NoiseFloor(block_ms=BLOCK_MS, warmup_s=0.0)
        for _ in range(120):                       # 3.6 秒
            est.feed(0.001, usable=True)
        return est

    def test_warmup_returns_none(self):
        est = NoiseFloor(block_ms=BLOCK_MS)
        for _ in range(60):                        # 1.8 秒
            est.feed(0.001, usable=True)
        self.assertIsNone(est.value())
        for _ in range(20):                        # 越过 2 秒
            est.feed(0.001, usable=True)
        self.assertIsNotNone(est.value())

    def test_playing_blocks_do_not_move_the_estimate(self):
        est = self._filled()
        before = est.value()
        for _ in range(60):
            est.feed(0.5, usable=False)
        self.assertEqual(est.value(), before)

    def test_estimate_tracks_quiet_level(self):
        est = self._filled()
        self.assertLess(est.value(), 0.01)

    def test_high_percentile_tracks_ambient_peaks(self):
        est = NoiseFloor(block_ms=BLOCK_MS, warmup_s=0.0)
        for i in range(120):
            est.feed(0.001 if i % 10 else 0.02, usable=True)
        self.assertGreater(est.value_high(), est.value())
        self.assertGreater(est.value_high(), 0.01)


class SegmenterGateTests(unittest.TestCase):
    def _params(self) -> VadParams:
        return VadParams(continue_ratio=0.4, start_blocks=3, silence_ms=120.0,
                         min_speech_ms=60.0, max_segment_s=30.0,
                         threshold=0.5)

    def test_explicit_gate_overrides_threshold(self):
        vad = Segmenter(self._params(), BLOCK_MS)
        for _ in range(3):
            vad.feed(0.03, 0.02)
        self.assertTrue(vad.speaking)

    def test_no_gate_uses_params_threshold(self):
        vad = Segmenter(self._params(), BLOCK_MS)
        for _ in range(3):
            vad.feed(0.03)
        self.assertFalse(vad.speaking)


if __name__ == "__main__":
    unittest.main()
