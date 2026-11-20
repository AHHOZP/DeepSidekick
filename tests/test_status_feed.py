from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import asyncio
import sqlite3
import unittest
from unittest import mock

from sidekick.webui import status_feed
from sidekick.webui.status_feed import StatusFeed


class FakeSupervisor:
    def __init__(self) -> None:
        self.running = True
        self.calls = 0
        self.tail_calls = 0
        self.lines = ["a", "b"]
        # 这些是“不该触发重新广播”的易变字段。
        self.uptime_s = 10
        self.mic_level = 0.1
        self.brain_last_ms = 10

    async def status(self) -> dict:
        self.calls += 1
        return {
            "running": self.running,
            "external": False,
            "pid": 123 if self.running else None,
            "returncode": None if self.running else 1,
            "uptime_s": self.uptime_s,
            "mode": "stream",
            "runtime": {
                "live": False,
                "source": "fake",
                "config_error": None,
                "secrets_error": None,
                "features": {"mic": {"state": "healthy", "error": None}},
                "stt": {"disabled": False, "dead": False, "error": None},
                "brain": {"model": "m", "source": "s", "user": {"name": "你"},
                          "error": None, "last_ms": self.brain_last_ms},
                "tts": {"provider": "edge", "error": None},
                "mic": {"error": None, "level": self.mic_level},
                "capture": {"error": None},
                "memory": {"summary_error": None},
                "stage": {"error": None},
            },
        }

    def tail(self, _n: int) -> list[str]:
        self.tail_calls += 1
        return list(self.lines)


class StatusFeedTests(QuietIsolatedAsyncioTestCase):
    async def test_first_frame_has_status_and_logs(self):
        sup = FakeSupervisor()
        feed = StatusFeed(sup, lambda: {"messages": 2}, started_at=0.0)
        q = feed.subscribe()
        try:
            first = await asyncio.wait_for(q.get(), 1.0)
            second = await asyncio.wait_for(q.get(), 1.0)
            got = {name: data for name, data in (first, second)}
            self.assertIn("status", got)
            self.assertIn("logs", got)
            self.assertEqual(got["logs"]["lines"], ["a", "b"])
            self.assertTrue(got["status"]["runtime"]["running"])
        finally:
            feed.unsubscribe(q)
            feed.close()

    async def test_sample_now_without_subscriber(self):
        sup = FakeSupervisor()
        feed = StatusFeed(sup, lambda: {}, started_at=0.0)
        snap = await feed.snapshot(force=True)
        self.assertEqual(1, sup.calls)
        self.assertTrue(snap["runtime"]["running"])
        # 没有订阅者就不该有后台采样循环。
        await asyncio.sleep(0.05)
        self.assertEqual(1, sup.calls)

    async def test_memory_stats_error_keeps_runtime_snapshot(self):
        def boom():
            raise sqlite3.OperationalError("database is locked")

        sup = FakeSupervisor()
        feed = StatusFeed(sup, boom, started_at=0.0)
        snap = await feed.snapshot(force=True)
        self.assertTrue(snap["runtime"]["running"])
        self.assertIn("OperationalError", snap["memory"]["error"])

    async def test_volatile_fields_do_not_rebroadcast(self):
        sup = FakeSupervisor()
        feed = StatusFeed(sup, lambda: {"messages": 1}, started_at=0.0)
        q = feed.subscribe()
        try:
            await asyncio.wait_for(q.get(), 1.0)
            while not q.empty():
                q.get_nowait()

            # 只改电平/耗时这类易变字段：投影不变，不能推给浏览器。
            sup.mic_level = 0.9
            sup.brain_last_ms = 999
            sup.uptime_s = 99
            await feed.sample_now()
            self.assertTrue(q.empty(), "易变字段变化不应重新广播")

            sup.running = False
            await feed.sample_now()
            name, data = await asyncio.wait_for(q.get(), 1.0)
            self.assertEqual(name, "status")
            self.assertFalse(data["runtime"]["running"])
        finally:
            feed.unsubscribe(q)
            feed.close()

    async def test_resubscribe_restarts_sampling(self):
        sup = FakeSupervisor()
        feed = StatusFeed(sup, lambda: {}, started_at=0.0)
        with mock.patch.object(status_feed, "STATUS_EVERY_S", 0.01), \
                mock.patch.object(status_feed, "LOGS_EVERY_S", 0.01):
            q1 = feed.subscribe()
            try:
                for _ in range(2):
                    await asyncio.wait_for(q1.get(), 1.0)
            finally:
                feed.unsubscribe(q1)

            q2 = feed.subscribe()
            try:
                for _ in range(2):
                    await asyncio.wait_for(q2.get(), 1.0)
                deadline = asyncio.get_running_loop().time() + 1.0
                while (sup.calls < 2 or sup.tail_calls < 2) \
                        and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
                self.assertGreaterEqual(sup.calls, 2)
                self.assertGreaterEqual(sup.tail_calls, 2)
            finally:
                feed.unsubscribe(q2)
        feed.close()

    async def test_wake_forces_immediate_sample(self):
        sup = FakeSupervisor()
        feed = StatusFeed(sup, lambda: {}, started_at=0.0)
        q = feed.subscribe()
        try:
            for _ in range(2):
                await asyncio.wait_for(q.get(), 1.0)
            sup.running = False
            feed.wake()
            deadline = asyncio.get_running_loop().time() + 1.0
            while feed.latest["runtime"]["running"] \
                    and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            self.assertFalse(feed.latest["runtime"]["running"])
        finally:
            feed.unsubscribe(q)
            feed.close()

    async def test_subscribe_after_close_does_not_start_task(self):
        sup = FakeSupervisor()
        feed = StatusFeed(sup, lambda: {}, started_at=0.0)
        feed.close()
        q = feed.subscribe()
        try:
            await asyncio.sleep(0.05)
            self.assertEqual(sup.calls, 0)
            self.assertIsNone(feed.latest)
        finally:
            feed.unsubscribe(q)

    async def test_slow_client_queue_keeps_newest(self):
        sup = FakeSupervisor()
        feed = StatusFeed(sup, lambda: {}, started_at=0.0)
        q = feed.subscribe()
        try:
            while not q.empty():
                q.get_nowait()
            for i in range(6):
                feed._put(q, ("status", {"i": i}))
            got = [q.get_nowait()[1]["i"] for _ in range(q.qsize())]
            # QUEUE_SIZE=4：丢最旧，留最新四条。
            self.assertEqual(got, [2, 3, 4, 5])
        finally:
            feed.unsubscribe(q)
            feed.close()

    async def test_last_subscriber_stops_sampling(self):
        sup = FakeSupervisor()
        feed = StatusFeed(sup, lambda: {}, started_at=0.0)
        q = feed.subscribe()
        await asyncio.wait_for(q.get(), 1.0)
        feed.unsubscribe(q)
        await asyncio.sleep(0.05)
        calls = sup.calls
        await asyncio.sleep(0.05)
        self.assertEqual(sup.calls, calls)
        feed.close()


if __name__ == "__main__":
    unittest.main()
