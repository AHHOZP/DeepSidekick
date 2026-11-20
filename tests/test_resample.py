"""重采样：24k ↔ 48k / 44.1k，长度与形状要对，不能抛异常。"""
from __future__ import annotations

import unittest

import numpy as np

from sidekick.output.tts.base import resample


class ResampleTests(unittest.TestCase):
    def test_upsample_doubles_length(self):
        src = np.zeros(480, dtype=np.float32)
        out = resample(src, 24000, 48000)
        self.assertLessEqual(abs(len(out) - 960), 1)
        self.assertEqual(out.dtype, np.float32)

    def test_downsample_ratio(self):
        src = np.zeros(4800, dtype=np.float32)
        out = resample(src, 48000, 44100)
        self.assertLessEqual(abs(len(out) - 4410), 2)

    def test_same_rate_returns_input(self):
        src = np.arange(64, dtype=np.float32)
        self.assertIs(resample(src, 48000, 48000), src)

    def test_empty_array(self):
        out = resample(np.zeros(0, dtype=np.float32), 24000, 48000)
        self.assertEqual(out.size, 0)

    def test_mono_stays_one_dimensional(self):
        out = resample(np.ones(240, dtype=np.float32) * 0.1, 24000, 16000)
        self.assertEqual(out.ndim, 1)
        self.assertEqual(len(out), 160)


if __name__ == "__main__":
    unittest.main()
