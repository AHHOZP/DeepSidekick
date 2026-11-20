"""回归测试：本轮审查修复的「测试全绿但实际行为不对」的点。"""
from __future__ import annotations

import asyncio
import threading
import time
import unittest

from sidekick.brain.material import material_digest
from sidekick.core.text import squeeze_ws


class TextAndMaterialTests(unittest.TestCase):
    def test_squeeze_ws_keeps_english_word_spaces(self):
        self.assertEqual(squeeze_ws("I think you should dodge"),
                         "I think you should dodge")
        self.assertEqual(squeeze_ws("你好 世界"), "你好世界")
        self.assertEqual(squeeze_ws("[in a hurry tone] 你好"),
                         "[in a hurry tone]你好")

    def test_material_digest_does_not_stop_at_bracket_emoji(self):
        text = (
            "[弹幕]4 条\n"
            "  03:15:44 小张：hello [doge]\n"
            "  03:15:49 阿蓝：血不多了\n"
            "[冷场]用户已经 26 秒没有说话了。"
        )
        digest = material_digest(text, "用户")
        self.assertIn("阿蓝", digest)
        self.assertIn("血不多了", digest)
        self.assertIn("[doge]", digest)


class BubblePagingTests(unittest.TestCase):
    def test_unsynced_bubble_advances_with_cps(self):
        from sidekick.desktop.bubble import Bubble

        bubble = Bubble()
        bubble.show(["第一页" * 4, "第二页" * 4], cps=100.0, sync=False)
        bubble._shown = len(bubble.pages[0])
        bubble._started = time.monotonic() - 2.0
        bubble._visible_text()
        self.assertEqual(bubble.idx, 1)

    def test_tick_advances_unsynced_without_draw(self):
        from sidekick.desktop.bubble import Bubble

        bubble = Bubble()
        bubble.show(["第一页" * 4, "第二页" * 4], cps=100.0, sync=False)
        bubble._shown = len(bubble.pages[0])
        bubble._started = time.monotonic() - 2.0
        self.assertTrue(bubble.tick())
        self.assertEqual(1, bubble.idx)

    def test_repeated_page_does_not_restart_typewriter(self):
        from sidekick.desktop.bubble import Bubble

        bubble = Bubble()
        bubble.show(["第一页", "第二页"], cps=100.0, sync=False)
        bubble.page(1)
        bubble._shown = 2
        bubble.page(1)
        self.assertEqual(2, bubble._shown)
        self.assertEqual(1, bubble.idx)

    def test_finish_never_moves_backwards(self):
        from sidekick.desktop.bubble import Bubble

        bubble = Bubble()
        bubble.show(["one", "two", "three"], cps=100.0, sync=False)
        bubble.page(2)
        bubble.finish(1)
        self.assertEqual(bubble.idx, 2)

    def test_page_speed_uses_audio_duration(self):
        from sidekick.desktop.bubble import Bubble

        bubble = Bubble()
        bubble.show(["一二三四五六七八", "下一句"], cps=1.0, sync=True,
                    duration=2.0)
        self.assertAlmostEqual(4.0, bubble.cps)
        bubble.page(1, duration=0.5)
        self.assertAlmostEqual(6.0, bubble.cps)

    def test_stream_pages_are_emitted_without_acts(self):
        from sidekick.output.speaker import Speaker

        speaker = object.__new__(Speaker)
        speaker._stop = threading.Event()
        speaker.cps = 10.0
        events = []
        speaker._emit = events.append
        class _Advancing:
            error = None
            _t = 0.0

            @property
            def played_s(self):
                self._t += 1000.0
                return self._t

        out = _Advancing()

        async def run_pages():
            await asyncio.wait_for(Speaker._later_pages(
                speaker, ["第一句", "第二句"], [], "第一句第二句",
                [True], out), timeout=1.0)

        asyncio.run(run_pages())
        self.assertEqual(1, len(events))
        self.assertEqual("page", events[0].kind)
        self.assertEqual(1, events[0].index)
        self.assertEqual([], events[0].acts)
        self.assertGreater(events[0].duration, 0.0)

    def test_finish_completes_text_instead_of_typing_after_audio(self):
        from sidekick.desktop.bubble import Bubble

        bubble = Bubble()
        bubble.show(["一二三四五六七八九十", "下一句"], cps=1.0, sync=True)
        bubble._shown = 1
        bubble.finish(1)
        self.assertEqual("一二三四五六七八九十", bubble._visible_text())
        self.assertFalse(bubble.animating)
        self.assertLess(bubble.until - time.monotonic(), 1.1)
        # 下一句开始要立刻换页，不等上一句的停留时间。
        bubble.show(["新的一句"], cps=4.0)
        self.assertEqual(0, bubble.idx)
        self.assertEqual("新的一句", bubble.pages[0])
