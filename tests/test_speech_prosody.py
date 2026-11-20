from __future__ import annotations

import unittest

from sidekick.motion import ProsodyTracker, SpeechFrame


class SpeechProsodyTests(unittest.TestCase):
    def test_alternating_frames_keep_beat_bounded(self):
        tracker = ProsodyTracker()
        last = 0.0
        peak_step = 0.0
        for i in range(180):
            frame = SpeechFrame(t=i / 60, level=0.7,
                                openness=1.0 if i % 2 else 0.0,
                                voiced=bool(i % 2))
            state = tracker.update(frame, 1 / 60, speaking=True)
            peak_step = max(peak_step, abs(state.a_beat - last))
            last = state.a_beat
        self.assertLessEqual(abs(last), 1.0)
        self.assertGreater(abs(last), 0.0)
        self.assertLess(peak_step, 1.2)

    def test_steady_speech_builds_a_phrase_envelope(self):
        tracker = ProsodyTracker()
        state = None
        for i in range(90):
            state = tracker.update(
                SpeechFrame(t=i / 60, level=0.7, openness=0.8, voiced=True),
                1 / 60, speaking=True)
        self.assertIsNotNone(state)
        self.assertGreater(state.env_phrase, 0.5)
        self.assertTrue(-1.0 <= state.a_beat <= 1.0)
        self.assertNotEqual(state.pose().get("head_x"), 0.0)

    def test_phase_freezes_after_speech(self):
        tracker = ProsodyTracker()
        for i in range(60):
            tracker.update(
                SpeechFrame(t=i / 60, level=0.8, openness=0.8, voiced=True),
                1 / 60, speaking=True)
        for _ in range(120):
            tracker.update(None, 1 / 60, speaking=False)
        before = tracker.phi
        for _ in range(120):
            tracker.update(None, 1 / 60, speaking=False)
        self.assertAlmostEqual(tracker.phi, before, places=6)


if __name__ == "__main__":
    unittest.main()
