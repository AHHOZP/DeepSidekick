from __future__ import annotations

import unittest

from sidekick.scheduling.mode import ModeMachine


class ModeMachineTests(unittest.TestCase):
    def _quiet(self, mcfg=None):
        cfg = {"auto": True, "manual": None, "quiet_max_viewers": 5,
               "quiet_max_danmu_5min": 3, "hype_min_viewers": 20,
               "hype_min_danmu_5min": 10, "hysteresis_s": 0}
        cfg.update(mcfg or {})
        return cfg

    def test_leaving_manual_silent_returns_target_immediately(self):
        m = ModeMachine()
        self.assertEqual(m.decide(0, 0, self._quiet({"manual": "silent"})),
                         "silent")
        self.assertEqual(m.decide(0, 0, self._quiet()), "quiet")

    def test_auto_false_does_not_keep_manual_silent(self):
        m = ModeMachine()
        m.decide(0, 0, self._quiet({"manual": "silent"}))
        self.assertEqual(m.decide(0, 0, self._quiet({"auto": False})), "normal")

    def test_solo_clears_manual_silent(self):
        m = ModeMachine()
        self.assertEqual(
            m.decide(0, 0, self._quiet({"manual": "silent"})), "silent")
        self.assertEqual(m.decide(0, 0, self._quiet(), solo=True), "normal")
        # 单人模式之后状态机不能还停在 silent。
        self.assertEqual(m.current, "normal")


if __name__ == "__main__":
    unittest.main()
