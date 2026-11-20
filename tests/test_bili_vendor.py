from __future__ import annotations

import unittest
from types import SimpleNamespace

from sidekick.core.events import DANMU, ENTER, GIFT, GUARD_BUY, SC, EventBus
from sidekick.input import bili


class BiliVendorTests(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        handler_cls = bili._handler_class(self.bus)
        self.handler = handler_cls()

    def events(self):
        return self.bus.since_seq(0)

    def test_danmaku_maps_to_danmu_event(self):
        self.handler._on_danmaku(
            None, SimpleNamespace(uname="观众", msg="晚上好"))
        events = self.events()
        self.assertEqual([e.kind for e in events], [DANMU])
        self.assertEqual(events[0].name, "观众")
        self.assertEqual(events[0].text, "晚上好")

    def test_gift_maps_to_gift_event(self):
        self.handler._on_gift(
            None, SimpleNamespace(uname="观众", gift_name="小心心", num=3))
        events = self.events()
        self.assertEqual([e.kind for e in events], [GIFT])
        self.assertEqual(events[0].extra["num"], 3)

    def test_guard_buy_maps_to_guard_event(self):
        self.handler._on_buy_guard(
            None, SimpleNamespace(username="舰长", gift_name="舰长"))
        events = self.events()
        self.assertEqual([e.kind for e in events], [GUARD_BUY])
        self.assertEqual(events[0].name, "舰长")

    def test_super_chat_maps_to_sc_event(self):
        self.handler._on_super_chat(
            None, SimpleNamespace(uname="观众", message="加油", price=30))
        events = self.events()
        self.assertEqual([e.kind for e in events], [SC])
        self.assertEqual(events[0].price, 30)

    def test_only_enter_messages_create_enter_events(self):
        self.handler._on_interact_word_v2(
            None, SimpleNamespace(username="新观众", msg_type=1))
        self.handler._on_interact_word_v2(
            None, SimpleNamespace(username="路过", msg_type=2))
        events = self.events()
        self.assertEqual([e.kind for e in events], [ENTER])
        self.assertEqual(events[0].name, "新观众")


if __name__ == "__main__":
    unittest.main()
