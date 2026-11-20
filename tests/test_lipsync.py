from __future__ import annotations

import unittest

import numpy as np

from sidekick.motion.lipsync import (
    RHUBARB_SHAPES, SpeechAnalyzer, analyze_pcm, classify_vowel,
    form_from_f2, nearest_shape,
)


def synth_vowel(f1, f2, sr=24000, duration=0.55, amp=0.2):
    t = np.arange(int(sr * duration)) / sr
    return (amp * (
        np.sin(2 * np.pi * f1 * t)
        + 0.6 * np.sin(2 * np.pi * f2 * t)
        + 0.2 * np.sin(2 * np.pi * 120 * t)
    )).astype(np.float32)


class LipsyncTests(unittest.TestCase):
    def test_classify_vowel_targets(self):
        self.assertEqual(max(classify_vowel(730, 1090), key=classify_vowel(730, 1090).get), "a")
        self.assertEqual(max(classify_vowel(270, 2290), key=classify_vowel(270, 2290).get), "i")
        self.assertEqual(max(classify_vowel(300, 870), key=classify_vowel(300, 870).get), "u")

    def test_form_axis(self):
        self.assertLess(form_from_f2(850), -0.7)
        self.assertGreater(form_from_f2(2200), 0.7)
        self.assertAlmostEqual(form_from_f2(1050), 0.0, places=1)

    def test_synthetic_vowels_have_expected_formants(self):
        expected = {
            "a": (730, 1090), "i": (270, 2290), "u": (300, 870),
            "e": (530, 1840), "o": (570, 840),
        }
        for name, (f1, f2) in expected.items():
            frames = analyze_pcm(synth_vowel(f1, f2), 24000)
            last = max(frames, key=lambda f: f.openness)
            self.assertAlmostEqual(last.f1, f1, delta=120, msg=name)
            self.assertAlmostEqual(last.f2, f2, delta=160, msg=name)
            self.assertEqual(last.vowel, name)

    def test_mouth_closes_on_silence(self):
        frames = analyze_pcm(np.zeros(12000, dtype=np.float32), 24000)
        self.assertTrue(frames)
        self.assertEqual(frames[-1].openness, 0.0)
        self.assertFalse(frames[-1].voiced)

    def test_streaming_matches_batch(self):
        pcm = synth_vowel(730, 1090)
        batch = analyze_pcm(pcm, 24000)
        stream = SpeechAnalyzer(24000)
        got = []
        for i in range(0, len(pcm), 480):
            got.extend(stream.push(pcm[i:i + 480]))
        got.extend(stream.flush())
        self.assertEqual(len(batch), len(got))
        self.assertAlmostEqual(batch[-1].openness, got[-1].openness, places=3)

    def test_rhubarb_projection(self):
        self.assertEqual(nearest_shape(0.0, 0.0), "X")
        self.assertEqual(nearest_shape(0.92, 0.0), "D")
        self.assertEqual(nearest_shape(0.22, -0.75), "F")
        self.assertIn("A", RHUBARB_SHAPES)
        self.assertEqual(len(RHUBARB_SHAPES), 9)


if __name__ == "__main__":
    unittest.main()

