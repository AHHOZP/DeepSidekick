from __future__ import annotations

import unittest

from sidekick.motion.blink import BlinkScheduler


class BlinkTests(unittest.TestCase):
    def test_blink_rate_and_closure(self):
        blink = BlinkScheduler(seed=3)
        frames = []
        for i in range(30 * 60):
            frames.append(blink.update(1 / 60))
        closures = 0
        previous = False
        for state in frames:
            closed = state.factor_l < 0.2
            if closed and not previous:
                closures += 1
            previous = closed
        self.assertGreaterEqual(closures, 3)
        self.assertLessEqual(closures, 12)
        self.assertLess(min(s.factor_l for s in frames), 0.15)

    def test_factors_stay_in_range(self):
        blink = BlinkScheduler(seed=11)
        for _ in range(30 * 60):
            state = blink.update(1 / 60, squint=0.2, speaking=True)
            self.assertGreaterEqual(state.factor_l, 0.0)
            self.assertLessEqual(state.factor_l, 1.0)
            self.assertGreaterEqual(state.factor_r, 0.0)
            self.assertLessEqual(state.factor_r, 1.0)

    def test_eyes_are_not_perfectly_synchronized(self):
        blink = BlinkScheduler(seed=5)
        differences = []
        for _ in range(20 * 60):
            state = blink.update(1 / 60)
            differences.append(abs(state.factor_l - state.factor_r))
        self.assertGreater(max(differences), 0.01)

    def test_squint_lowers_the_base_but_does_not_close(self):
        blink = BlinkScheduler(seed=9)
        state = blink.update(1 / 60, squint=0.5)
        self.assertLess(state.factor_l, 1.0)
        self.assertGreater(state.factor_l, 0.6)


if __name__ == "__main__":
    unittest.main()

