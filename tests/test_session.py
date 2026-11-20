from __future__ import annotations

import unittest
from unittest import mock

from sidekick import config
from sidekick.app.session import SessionState


class SessionStateTests(unittest.TestCase):
    def setUp(self):
        # refresh() 会看全局 last_error；别让其它用例的读盘失败污染这里。
        self._last_error = config.last_error
        config.last_error = None
        self.cfg = config.defaults()
        self.cfg["danmu_source"] = "bili"

    def tearDown(self):
        config.last_error = self._last_error

    def test_solo_derives_none_without_mutating_base(self):
        s = SessionState(self.cfg)
        eff = s.effective()
        self.assertEqual(eff["danmu_source"], "none")
        self.assertFalse(eff["session"]["live"])
        self.assertEqual(self.cfg["danmu_source"], "bili")

    def test_live_restores_base_danmu(self):
        cfg = config.defaults()
        cfg["danmu_source"] = "bili"
        cfg["session"]["live"] = True
        s = SessionState(cfg)
        eff = s.effective()
        self.assertEqual(eff["danmu_source"], "bili")
        self.assertTrue(eff["session"]["live"])

    def test_refresh_does_not_write_into_loaded_config(self):
        s = SessionState(self.cfg)
        source = config.defaults()
        source["danmu_source"] = "bili"
        source["room_id"] = 12345
        eff = s.refresh(now=100.0, loader=lambda: source)
        self.assertEqual(eff["danmu_source"], "none")
        self.assertEqual(source["danmu_source"], "bili")

    def test_refresh_noop_when_config_unchanged(self):
        s = SessionState(self.cfg)
        source = config.deep_update(config.defaults(), self.cfg)
        self.assertIsNone(s.refresh(now=100.0, loader=lambda: source))

    def test_refresh_keeps_old_config_when_file_is_broken(self):
        s = SessionState(self.cfg)
        source = config.defaults()
        source["room_id"] = 12345
        with mock.patch.object(config, "last_error", "bad yaml"):
            self.assertIsNone(s.refresh(now=100.0, loader=lambda: source))

    def test_refresh_keeps_restart_level_live_switch(self):
        s = SessionState(self.cfg)
        source = config.defaults()
        source["danmu_source"] = "none"
        source["session"]["live"] = True
        source["room_id"] = 12345
        eff = s.refresh(now=100.0, loader=lambda: source)
        # 直播模式是重启级开关：热重载不跟着变。
        self.assertEqual(eff["room_id"], 12345)
        self.assertFalse(eff["session"]["live"])

    def test_refresh_applies_resolver(self):
        s = SessionState(self.cfg, resolve=lambda raw: {**raw, "room_id": 7})
        eff = s.refresh(now=100.0, loader=config.defaults)
        self.assertEqual(eff["room_id"], 7)


if __name__ == "__main__":
    unittest.main()
