from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import asyncio
import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from sidekick.webui.status_feed import StatusFeed
from webui_fixture import webui


class FakeSupervisor:
    async def status(self) -> dict:
        return {"running": True, "pid": 1, "mode": "stream",
                "runtime": {"features": {}, "stt": {}, "brain": {},
                            "tts": {}, "mic": {}, "capture": {},
                            "memory": {}, "stage": {}}}

    def tail(self, _n: int) -> list[str]:
        return ["one", "two"]


class WebUiEventStreamTests(QuietIsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.ui = self.stack.enter_context(
            webui(self.root, {"memory": {"enabled": False}}))
        self.ui.csrf = "test-token"
        self.ui.supervisor = FakeSupervisor()
        self.ui.feed = StatusFeed(self.ui.supervisor, self.ui.memory_stats,
                                  started_at=self.ui.started_at)
        self.client = TestClient(TestServer(self.ui.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.stack.close()
        self.tmp.cleanup()

    async def test_first_frame_has_retry_status_and_logs(self):
        async with self.client.get("/api/events/stream") as resp:
            self.assertEqual(resp.status, 200)
            chunks = []
            for _ in range(6):
                chunk = await asyncio.wait_for(
                    resp.content.readuntil(b"\n\n"), timeout=1.0)
                chunks.append(chunk)
                text = b"".join(chunks)
                if b"event: status" in text and b"event: logs" in text:
                    break
            text = b"".join(chunks)
            self.assertIn(b"retry: 3000", text)
            self.assertIn(b"event: status", text)
            self.assertIn(b"event: logs", text)

    async def test_status_route_survives_memory_stats_error(self):
        def boom():
            raise sqlite3.OperationalError("database is locked")

        self.ui.feed.memory_stats = boom
        async with self.client.get("/api/status?refresh=1") as resp:
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertIn("OperationalError", data["memory"]["error"])
        async with self.client.get("/api/bootstrap") as resp:
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertIn("OperationalError", data["memory"]["error"])


if __name__ == "__main__":
    unittest.main()
