from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import tempfile
import unittest
from pathlib import Path

from sidekick import config
from sidekick.app.identity import resolve
from sidekick.core.events import DANMU, ME, SC, Event, EventBus
from sidekick.scheduling.mode import ModeMachine
from sidekick.scheduling.scheduler import Scheduler
from sidekick.output.said_log import SaidLog


class FakeClock:
    last_request_at = 0.0
    busy = False


class FakeMic:
    speech_ms = 0.0
    last_voice_at = 0.0
    user_speaking = False


class SchedulerTests(QuietIsolatedAsyncioTestCase):
    def test_reload_identity_only_updates_identity_keys(self):
        sched = object.__new__(Scheduler)
        sched.cfg = {"persona": {}, "user": {}, "role": {},
                     "scheduler": {"min_gap_s": 1}}
        sched.reload_identity({
            "persona": {"bot_name": "新名字"},
            "user": {"name": "新用户"},
            "role": {"id": "new"},
            "scheduler": {"min_gap_s": 99},
        })
        self.assertEqual(sched.cfg["persona"]["bot_name"], "新名字")
        self.assertEqual(sched.cfg["user"]["name"], "新用户")
        self.assertEqual(sched.cfg["role"]["id"], "new")
        self.assertEqual(sched.cfg["scheduler"]["min_gap_s"], 1)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = resolve(config.defaults(), Path(self.tmp.name)).cfg
        self.cfg["session"]["live"] = True
        self.cfg["scheduler"].update({"min_gap_s": 0, "batch_events": 1,
                                      "me_wait_short_ms": 0})
        self.cfg["capture"]["interval_s"] = 0
        self.bus = EventBus(50)
        self.said = SaidLog(str(Path(self.tmp.name) / "said.jsonl"))
        self.said.start()
        self.clock = FakeClock()
        self.mic = FakeMic()
        self.s = Scheduler(self.cfg, self.bus, self.said, self.clock,
                           ModeMachine(), take_frames=lambda: [], mic=self.mic)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_take_round_consumes_danmu_and_does_not_need_voice(self):
        self.bus.push(Event(kind=DANMU, name="观众", text="这怎么打"))
        snap = self.s.take_round()
        self.assertIsNotNone(snap)
        self.assertIn("这怎么打", snap["text"])
        self.assertEqual(snap["round"], 1)

    async def test_me_round_and_gap_use_clock(self):
        self.bus.push(Event(kind=ME, name="我", text="你猜怎么着"))
        snap = self.s.take_round()
        self.assertIsNotNone(snap)
        self.assertIn("我说话", snap["reason"])
        import time

        self.s._last_round = 0.0
        self.s.min_gap = 6.0
        self.clock.last_request_at = time.time() - 100
        self.assertEqual(self.s.gap_left(), 0.0)
        self.clock.last_request_at = time.time()
        self.assertGreater(self.s.gap_left(), 0.0)

    async def test_me_trigger_grabs_a_fresh_frame_when_stale(self):
        import time

        self.bus.push(Event(kind=ME, name="我", text="现在呢"))
        calls = []
        latest = [time.time() - 3.0]

        def grab():
            calls.append(1)
            latest[0] = time.time()
            return True

        self.s.capture_now = grab
        self.s.latest_frame_ts = lambda: latest[0]
        self.cfg["capture"]["capture_now_stale_s"] = 1.0
        snap = await self.s.wait(1.0)
        self.assertIsNotNone(snap)
        self.assertTrue(calls)
        self.assertLess(time.time() - latest[0], 0.2)

    async def test_barge_releases_the_next_round_once(self):
        import time

        self.s.min_gap = 6.0
        self.clock.last_request_at = time.time()
        self.assertGreater(self.s.gap_left(), 0.0)

        self.s.note_barge()
        self.assertEqual(self.s.gap_left(), 0.0)
        self.bus.push(Event(kind=ME, name="我", text="等一下，我还没说完"))
        snap = self.s.take_round()
        self.assertIsNotNone(snap)
        # 只放行一次：下一轮又要回到 min_gap 闸门。
        self.assertGreater(self.s.gap_left(), 0.0)

    def test_solo_still_honors_manual_silent(self):
        self.cfg["session"]["live"] = False
        self.cfg["mode"]["manual"] = "silent"
        self.s.reload(self.cfg)
        self.assertIsNone(self.s.take_round())
        self.assertEqual(self.s.current_mode, "silent")

    def test_stats_do_not_crash_after_silent_round(self):
        self.cfg["mode"]["manual"] = "silent"
        self.s.reload(self.cfg)
        self.assertIsNone(self.s.take_round())
        self.assertEqual(self.s.current_mode, "silent")
        st = self.s.stats()
        self.assertEqual(st["idle_threshold_s"], 0.0)
        self.assertEqual(st["cold_left_s"], 0.0)

    def test_stats_do_not_advance_mode_machine(self):
        self.cfg["mode"].update({"manual": "", "auto": True,
                                 "quiet_max_viewers": 5,
                                 "quiet_max_danmu_5min": 3,
                                 "hysteresis_s": 0})
        self.s.reload(self.cfg)
        self.s._mm.decide(100, 100, self.cfg["mode"])
        self.assertEqual(self.s.current_mode, "hype")
        for _ in range(5):
            self.s.stats()
        self.assertEqual(self.s.current_mode, "hype")

    def test_due_and_material_due_agree_on_short_sentence_wait(self):
        self.s.reload({**self.cfg, "scheduler": {**self.cfg["scheduler"],
                                                 "me_wait_short_ms": 800}})
        self.bus.push(Event(kind=ME, name="我", text="好"))
        due, _reason = self.s._due()
        self.assertFalse(due)
        self.assertGreater(self.s.material_due_in_s(), 0.5)
        self.assertLessEqual(self.s.material_due_in_s(), 0.9)

    def test_due_in_does_not_fake_zero_while_talking(self):
        import time

        self.cfg["cadence"]["normal"]["idle_s"] = 1
        self.s.reload(self.cfg)
        self.s._consumed = time.time() - 1.5
        self.s._last_round = 0.0
        self.mic.speech_ms = 500.0
        self.assertFalse(self.s._due()[0])
        self.assertIsNone(self.s.material_due_in_s())

    def test_unconfirmed_talking_is_capped_at_idle_plus_six(self):
        import time

        now = time.time()
        self.mic.speech_ms = 500.0
        self.assertIsNone(self.s._cold_wait(now, 25.0, 20.0))
        self.assertEqual(0.0, self.s._cold_wait(now, 27.0, 20.0))

    def test_me_plus_sc_reason_mentions_super_chat(self):
        self.bus.push(Event(kind=ME, name="我", text="谢谢"))
        self.bus.push(Event(kind=SC, name="金主", text="加油"))
        due, reason = self.s._due()
        self.assertTrue(due)
        self.assertTrue(reason.startswith("我说话"))
        self.assertIn("醒目留言", reason)

    def test_recent_voice_waits_for_the_full_idle_window(self):
        import time

        self.cfg["cadence"]["normal"]["idle_s"] = 45
        self.s.reload(self.cfg)
        self.s._consumed = time.time() - 10
        self.s._last_round = 0.0
        self.mic.last_voice_at = time.time() - 2
        self.mic.speech_ms = 0.0
        self.assertFalse(self.s._due()[0])
        left = self.s.material_due_in_s()
        self.assertIsNotNone(left)
        self.assertGreater(left, 30)
        self.assertLessEqual(left, 45)

    def test_due_and_material_due_agree_when_ready(self):
        self.bus.push(Event(kind=DANMU, name="观众", text="在吗"))
        self.assertTrue(self.s._due()[0])
        self.assertEqual(self.s.material_due_in_s(), 0.0)

    def test_solo_without_manual_uses_normal(self):
        self.cfg["session"]["live"] = False
        self.cfg["mode"]["manual"] = ""
        self.s.reload(self.cfg)
        self.assertIsNone(self.s.take_round())
        self.assertEqual(self.s.current_mode, "normal")


if __name__ == "__main__":
    unittest.main()
