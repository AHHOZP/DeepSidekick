from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from sidekick.webui.status_feed import StatusFeed
from webui_fixture import webui


class MemoryRoutesTests(QuietIsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        raw = {
            "memory": {"enabled": True, "db_path": "state/test.db"},
            "game": {"enabled": True, "id": "g1"},
            "webui": {"port": 17880},
        }
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
        self.store = self.ui.get_memory().store
        self.item = self.store.add_memory(
            scope="live", kind="summary", content="旧内容", title="旧标题")

    async def asyncTearDown(self):
        await self.client.close()
        self.stack.close()
        self.tmp.cleanup()

    def _headers(self):
        return {"X-CSRF-Token": "test-token"}

    async def test_put_can_change_scope_and_kind(self):
        r = await self.client.put(
            f"/api/memories/{self.item['id']}",
            json={"content": "新内容", "scope": "game", "kind": "plot"},
            headers=self._headers())
        self.assertEqual(r.status, 200, await r.text())
        got = self.store.get_memory(self.item["id"])
        self.assertEqual(got["content"], "新内容")
        self.assertEqual(got["scope"], "game")
        self.assertEqual(got["kind"], "plot")
        self.assertEqual(got["game_id"], "g1")

    async def test_put_ignores_enabled(self):
        before = self.store.get_memory(self.item["id"])["enabled"]
        r = await self.client.put(
            f"/api/memories/{self.item['id']}",
            json={"enabled": not before}, headers=self._headers())
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual(self.store.get_memory(self.item["id"])["enabled"], before)

    async def test_put_rejects_unknown_scope(self):
        r = await self.client.put(
            f"/api/memories/{self.item['id']}",
            json={"scope": "other"}, headers=self._headers())
        self.assertEqual(r.status, 400)

    async def test_add_rejects_unknown_scope_and_kind(self):
        r = await self.client.post(
            "/api/memories",
            json={"content": "x", "scope": "other"},
            headers=self._headers())
        self.assertEqual(r.status, 400)
        r = await self.client.post(
            "/api/memories",
            json={"content": "x", "kind": "other"},
            headers=self._headers())
        self.assertEqual(r.status, 400)
        r = await self.client.post(
            "/api/memories",
            json={"content": "x", "kind": "permanent"},
            headers=self._headers())
        self.assertEqual(r.status, 400)

    async def test_model_list_and_current_cannot_be_deleted(self):
        r = await self.client.get("/api/memory/model")
        self.assertEqual(r.status, 200, await r.text())
        data = await r.json()
        self.assertEqual(Path(data["root"]), self.root / "state" / "embed")
        self.assertTrue(data["models"])
        current = data["current"]
        self.assertTrue(any(m["id"] == current for m in data["models"]))
        self.assertTrue(all("max_context" in m for m in data["models"]))
        self.assertTrue(all("cmteb" in m and "mteb" in m and "mmteb" in m
                            for m in data["models"]))
        langs = {m["id"]: m["langs"] for m in data["models"]}
        self.assertEqual(langs["jina-embeddings-v3"], "89+ 语种")
        self.assertEqual(langs["bge-m3"], "100+ 语种")
        self.assertTrue(all("dim" not in m and "license" not in m
                            for m in data["models"]))
        self.assertNotIn("nomic-embed-text-v1.5-q",
                         {m["id"] for m in data["models"]})
        r = await self.client.post(
            "/api/memory/model/delete",
            json={"model": current, "confirm": True},
            headers=self._headers())
        self.assertEqual(r.status, 409)

    async def test_model_download_job(self):
        model = "qwen3-embedding-0.6b-q"
        def fake_install(entry, dest, job):
            job["status"] = "done"
            job["stage"] = "完成"
            return Path(dest)
        with mock.patch("sidekick.webui.routes.memory.download.install",
                        side_effect=fake_install):
            r = await self.client.post(
                "/api/memory/model/download",
                json={"model": model, "confirm": True, "mirror": True},
                headers=self._headers())
            self.assertEqual(r.status, 200, await r.text())
            job = (await r.json())["job"]
            for _ in range(20):
                r2 = await self.client.get(
                    f"/api/memory/model/download/{job['id']}")
                data = await r2.json()
                if data["job"]["status"] != "running":
                    break
                import asyncio
                await asyncio.sleep(0.02)
            self.assertEqual(data["job"]["status"], "done")

    async def test_archived_quest_kind_is_accepted(self):
        r = await self.client.post(
            "/api/memories",
            json={"content": "找钥匙：从北门进", "scope": "game",
                  "kind": "quests", "title": "找钥匙"},
            headers=self._headers())
        self.assertEqual(r.status, 200, await r.text())
        data = await (await self.client.get(
            "/api/memories?scope=game&kind=quests")).json()
        self.assertEqual(data["items"][0]["title"], "找钥匙")


if __name__ == "__main__":
    unittest.main()
