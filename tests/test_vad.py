from __future__ import annotations

import unittest

from sidekick import config
from sidekick.input.vad import (START_FLOOR, NoiseFloor, Segmenter, VadParams,
                            segment_levels)

BLOCK_MS = 30.0


def params(**over) -> VadParams:
    base = dict(threshold=0.02, continue_ratio=0.4, start_blocks=3,
                silence_ms=120.0, min_speech_ms=60.0, max_segment_s=0.3)
    base.update(over)
    return VadParams(**base)


class VadParamsTests(unittest.TestCase):
    def test_defaults_come_from_config(self):
        cfg = config.defaults()
        p = VadParams.from_cfg(cfg)
        # 线上门限由固定基线 + 噪声/峰值 + 百分比现算；离线默认跟随固定基线。
        self.assertEqual(p.threshold, START_FLOOR)
        self.assertEqual(p.continue_ratio, cfg["mic"]["continue_ratio"])
        self.assertEqual(p.start_blocks, cfg["mic"]["start_blocks"])
        self.assertEqual(p.silence_ms, cfg["mic"]["silence_ms"])
        self.assertEqual(p.min_speech_ms, cfg["mic"]["min_speech_ms"])
        self.assertEqual(p.max_segment_s, cfg["mic"]["max_segment_s"])


class NoiseFloorReseedTests(unittest.TestCase):
    def test_force_reseed_sets_estimate_from_flat_levels(self):
        noise = NoiseFloor()
        noise.force_reseed([0.02] * 200)
        self.assertAlmostEqual(0.02, noise.value(), places=4)
        self.assertAlmostEqual(0.02, noise.value_high(), places=4)


class SegmenterTests(unittest.TestCase):
    def test_one_loud_block_does_not_start(self):
        vad = Segmenter(params(), BLOCK_MS)
        self.assertIsNone(vad.feed(0.1))
        self.assertFalse(vad.speaking)

    def test_tail_silence_is_trimmed(self):
        vad = Segmenter(params(), BLOCK_MS)
        seg = None
        for level in (0.1, 0.1, 0.1, 0.001, 0.001, 0.001, 0.001):
            seg = vad.feed(level) or seg
        self.assertIsNotNone(seg)
        self.assertEqual(seg.start, 0)
        self.assertEqual(seg.end, 7)
        self.assertEqual(seg.speech_blocks, 3)
        self.assertFalse(vad.speaking)

    def test_continue_ratio_keeps_quiet_speech(self):
        vad = Segmenter(params(), BLOCK_MS)
        for _ in range(3):
            vad.feed(0.1)
        # 0.01 低于起手阈值，但高于续接门槛 0.008：不算说完。
        self.assertIsNone(vad.feed(0.01))
        self.assertTrue(vad.speaking)

    def test_hard_cut_keeps_current_block_for_next_segment(self):
        vad = Segmenter(params(), BLOCK_MS)
        seg = None
        for _ in range(11):
            seg = vad.feed(0.1) or seg
        self.assertIsNotNone(seg)
        self.assertEqual((seg.start, seg.end, seg.speech_blocks), (0, 10, 10))
        self.assertTrue(vad.speaking)
        self.assertEqual(vad.start, 10)

    def test_reset_drops_partial_segment(self):
        vad = Segmenter(params(), BLOCK_MS)
        for _ in range(4):
            vad.feed(0.1)
        vad.reset()
        self.assertFalse(vad.speaking)
        self.assertIsNone(vad.feed(0.001))


class SegmentLevelsTests(unittest.TestCase):
    def test_short_segment_is_dropped(self):
        levels = [0.1, 0.001, 0.001, 0.001, 0.001]
        # 只有 1 块 = 30ms，低于 min_speech_ms。
        self.assertEqual(segment_levels(levels, BLOCK_MS, params()), [])

    def test_offline_flush_keeps_last_segment(self):
        levels = [0.1] * 5
        segs = segment_levels(levels, BLOCK_MS, params())
        self.assertEqual(len(segs), 1)
        self.assertGreaterEqual(segs[0].speech_blocks, 3)

    def test_segments_report_duration_blocks(self):
        levels = ([0.1] * 4 + [0.001] * 4) * 2
        segs = segment_levels(levels, BLOCK_MS, params())
        self.assertEqual(len(segs), 2)
        self.assertEqual([s.speech_blocks for s in segs], [4, 4])


if __name__ == "__main__":
    unittest.main()
