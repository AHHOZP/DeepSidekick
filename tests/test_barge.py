"""打断判决：纯函数，不碰硬件。

回归重点：旧实现用 `speech_ms` 当打断判据，而它在播放期被 ducking 清零，
所以打断永远触发不了。这里必须验证"播放期照样能攒够"。
"""
from __future__ import annotations

import unittest

from sidekick import config
from sidekick.input.barge import BargeGate

BLOCK = 30.0


def gate(**over) -> BargeGate:
    cfg = config.defaults()
    cfg["barge"].update(over)
    return BargeGate(cfg)


def feed(g: BargeGate, level: float, *, now: float, playing: bool = True,
         ref: float = 0.0, ref_age: float = 9.0, thr: float = 0.05,
         mic_mode: str = "headset") -> bool:
    return g.feed(level, playing=playing, ref=ref, ref_age=ref_age,
                  gate=thr, mic_mode=mic_mode, block_ms=BLOCK, now=now)


class BargeGateTests(unittest.TestCase):
    def test_old_bug_regression_counts_while_playing(self):
        g = gate(enabled="on", min_ms=100)
        now = 0.0
        hit = False
        for _ in range(4):
            hit = feed(g, 0.2, now=now)
            now += BLOCK / 1000.0
        self.assertTrue(hit)
        self.assertEqual(g.triggers, 1)

    def test_not_playing_never_triggers(self):
        g = gate(enabled="on", min_ms=100)
        for i in range(10):
            self.assertFalse(feed(g, 0.5, now=i * 0.03, playing=False))
        self.assertEqual(g.triggers, 0)
        self.assertEqual(g.last_ms, 0.0)

    def test_cooldown_blocks_the_next_trigger(self):
        g = gate(enabled="on", min_ms=100, cooldown_ms=500)
        now = 0.0
        for _ in range(4):
            feed(g, 0.2, now=now)
            now += BLOCK / 1000.0
        self.assertEqual(g.triggers, 1)
        # 冷却期内即使一直很响也不再触发。
        for _ in range(4):
            self.assertFalse(feed(g, 0.2, now=now))
            now += BLOCK / 1000.0
        self.assertEqual(g.triggers, 1)
        # 冷却结束后可以再次触发。
        now += 0.5
        hit = False
        for _ in range(4):
            hit = feed(g, 0.2, now=now) or hit
            now += BLOCK / 1000.0
        self.assertTrue(hit)
        self.assertEqual(g.triggers, 2)

    def test_alternating_levels_decay_instead_of_triggering(self):
        g = gate(enabled="on", min_ms=100)
        now = 0.0
        hit = False
        for _ in range(20):
            hit = feed(g, 0.2, now=now) or hit
            now += BLOCK / 1000.0
            feed(g, 0.001, now=now)
            now += BLOCK / 1000.0
        self.assertFalse(hit)
        self.assertEqual(g.triggers, 0)

    def test_speaker_mode_needs_to_beat_playback_energy(self):
        g = gate(enabled="on", min_ms=60, ratio=1.8)
        ref = 0.1
        now = 0.0
        hit = False
        for _ in range(3):
            hit = feed(g, ref * 1.5, now=now, ref=ref, ref_age=0.05,
                       mic_mode="speaker") or hit
            now += BLOCK / 1000.0
        self.assertFalse(hit)

        hit = False
        for _ in range(3):
            hit = feed(g, ref * 2.0, now=now, ref=ref, ref_age=0.05,
                       mic_mode="speaker") or hit
            now += BLOCK / 1000.0
        self.assertTrue(hit)

    def test_auto_does_not_barge_on_speaker(self):
        g = gate(enabled="auto", min_ms=60)
        self.assertFalse(g.active("speaker"))
        for i in range(10):
            self.assertFalse(feed(g, 10.0, now=i * 0.03, mic_mode="speaker"))
        self.assertTrue(g.active("headset"))

    def test_off_never_triggers(self):
        g = gate(enabled="off", min_ms=60)
        self.assertFalse(g.active("headset"))
        for i in range(10):
            self.assertFalse(feed(g, 10.0, now=i * 0.03))

    def test_stale_reference_falls_back_to_gate(self):
        g = gate(enabled="on", min_ms=60, ratio=1.8)
        now = 0.0
        hit = False
        for _ in range(3):
            # ref 很大但已过期：不按 ref 放大阈值，低电平仍能命中。
            hit = feed(g, 0.1, now=now, ref=10.0, ref_age=1.0) or hit
            now += BLOCK / 1000.0
        self.assertTrue(hit)


if __name__ == "__main__":
    unittest.main()
