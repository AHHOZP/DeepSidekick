from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import contextlib
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from sidekick.webui.status_feed import StatusFeed
from webui_fixture import webui


class SttDeleteTests(QuietIsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        raw = {
            "memory": {"enabled": False},
            "stt": {"model": "current", "model_dir": "state/asr",
                    "models": {
                        "current": {"kind": "sense-voice", "dir": "current"},
                        "other": {"kind": "sense-voice", "dir": "other"},
                    }},
            "webui": {"port": 17880},
        }
        (self.root / "state" / "asr" / "current").mkdir(parents=True)
        (self.root / "state" / "asr" / "other").mkdir(parents=True)
        (self.root / "state" / "asr" / "other" / "model.onnx").write_bytes(b"x")

        self.stack = contextlib.ExitStack()
        self.ui = self.stack.enter_context(webui(self.root, raw))
        self.ui.csrf = "test-token"

        class Sup:
            async def status(self):
                return {}

        self.ui.supervisor = Sup()
        self.ui.feed = StatusFeed(self.ui.supervisor, self.ui.memory_stats,
                                  started_at=self.ui.started_at)
        self.client = TestClient(TestServer(self.ui.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.stack.close()
        self.tmp.cleanup()

    async def test_delete_removes_only_model_directory(self):
        r = await self.client.post(
            "/api/stt/delete", json={"model": "other", "confirm": True},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 200, await r.text())
        self.assertFalse((self.root / "state" / "asr" / "other").exists())
        self.assertTrue((self.root / "state" / "asr" / "current").is_dir())

    async def test_current_model_cannot_be_deleted(self):
        r = await self.client.post(
            "/api/stt/delete", json={"model": "current", "confirm": True},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 409, await r.text())
        self.assertTrue((self.root / "state" / "asr" / "current").is_dir())

    async def test_manual_model_has_no_download_action(self):
        r = await self.client.get("/api/stt/models")
        self.assertEqual(r.status, 200, await r.text())
        data = await r.json()
        self.assertEqual(Path(data["root"]), self.root / "state" / "asr")
        row = next(m for m in data["models"] if m["id"] == "other")
        self.assertFalse(row["downloadable"])


if __name__ == "__main__":
    unittest.main()
