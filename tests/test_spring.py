from __future__ import annotations

import math
import unittest

from sidekick.motion.spring import SecondOrder, SpringBank


class SpringTests(unittest.TestCase):
    def test_step_response_reaches_target(self):
        spring = SecondOrder(frequency=2.0, damping=0.9)
        for _ in range(300):
            value = spring.update(1.0, 1 / 60)
        self.assertAlmostEqual(value, 1.0, places=2)

    def test_underdamped_overshoots(self):
        spring = SecondOrder(frequency=1.5, damping=0.35)
        peak = 0.0
        for _ in range(240):
            peak = max(peak, spring.update(1.0, 1 / 60))
        self.assertGreater(peak, 1.02)

    def test_critical_damping_does_not_overshoot_much(self):
        spring = SecondOrder(frequency=1.5, damping=1.0)
        peak = 0.0
        for _ in range(240):
            peak = max(peak, spring.update(1.0, 1 / 60))
        self.assertLess(peak, 1.01)

    def test_large_frame_is_stable(self):
        spring = SecondOrder(frequency=10.0, damping=0.4)
        value = 0.0
        for _ in range(20):
            value = spring.update(1.0, 0.5)
        self.assertTrue(math.isfinite(value))
        self.assertLess(abs(value), 10.0)

    def test_static_zero_channel_is_skipped(self):
        bank = SpringBank({"x": (2.0, 0.9, 0.5)})
        self.assertEqual({"x": 0.0}, bank.step({}, 1 / 60))
        self.assertEqual(0.0, bank.springs["x"].position)

    def test_negative_or_nan_dt_is_ignored(self):
        spring = SecondOrder()
        self.assertEqual(spring.update(1.0, -1.0), 0.0)
        self.assertEqual(spring.update(float("nan"), 0.016), 0.0)


if __name__ == "__main__":
    unittest.main()

