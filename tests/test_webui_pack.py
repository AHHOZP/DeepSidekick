"""WebUI：对话预设的读写与三件套导入导出的接线。"""
from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from sidekick import config
from sidekick.avatar.catalog import AvatarCatalog
from sidekick.webui.status_feed import StatusFeed
from webui_fixture import webui

MODEL3 = {"FileReferences": {"Moc": "m.moc3", "Textures": ["t.png"]}}
MOC = b"MOC3" + (0).to_bytes(4, "little")


class WebUIPresetApiTests(QuietIsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = config.defaults()
        self.cfg["memory"]["enabled"] = False
        self.stack = contextlib.ExitStack()
        self.ui = self.stack.enter_context(webui(self.root, self.cfg))
        self.ui._catalog = AvatarCatalog(self.root)
        self.ui.csrf = "test-token"

        class Sup:
            async def status(self):
                return {}

        self.ui.supervisor = Sup()
        self.ui.feed = StatusFeed(self.ui.supervisor, self.ui.memory_stats,
                                  started_at=self.ui.started_at)
        self.patch = mock.patch("sidekick.webui.app.config.load", return_value=self.cfg)
        self.patch.start()
        self.client = TestClient(TestServer(self.ui.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.patch.stop()
        self.stack.close()
        self.tmp.cleanup()

    def csrf(self) -> dict:
        return {"X-CSRF-Token": "test-token"}

    async def test_create_save_and_read_preset(self):
        r = await self.client.post("/api/presets", json={"id": "p1"},
                                   headers=self.csrf())
        self.assertEqual(r.status, 200, await r.text())

        blocks = [{"id": "h", "title": "开头", "slot": "start",
                   "role": "system", "text": "只陪一个人"}]
        r = await self.client.put("/api/presets/p1",
                                  json={"name": "陪玩", "desc": "", "blocks": blocks},
                                  headers=self.csrf())
        self.assertEqual(r.status, 200, await r.text())
        saved = (self.root / "persona" / "presets" / "p1" / "preset.yaml")
        raw = saved.read_text(encoding="utf-8")
        self.assertIn("只陪一个人", raw)
        # 位置只剩 slot（落在哪两个锚点之间），顺序由列表/书写顺序决定。
        self.assertNotIn("order:", raw)

        r = await self.client.get("/api/presets/p1")
        one = await r.json()
        self.assertEqual("start", one["blocks"][0]["slot"])
        self.assertNotIn("order", one["blocks"][0])
        self.assertEqual("角色设定", one["gaps"][0]["anchor"])
        self.assertNotIn("slots", one)
        self.assertNotIn("conflicts", one)

        r = await self.client.get("/api/presets")
        data = await r.json()
        self.assertIn("p1", [x["id"] for x in data["items"]])

        r = await self.client.delete("/api/presets/p1", headers=self.csrf())
        self.assertEqual(r.status, 200, await r.text())
        self.assertNotIn("p1", [x["id"] for x in (await r.json())["items"]])

    async def test_bad_preset_name_is_rejected(self):
        r = await self.client.post("/api/presets", json={"id": "../x"},
                                   headers=self.csrf())
        self.assertEqual(r.status, 400)

    async def test_select_writes_config_and_current_cannot_be_deleted(self):
        await self.client.post("/api/presets", json={"id": "p3"},
                               headers=self.csrf())
        raw = self.root / "config.yaml"
        r = await self.client.post("/api/presets/select", json={"id": "p3"},
                                   headers=self.csrf())
        self.assertEqual(r.status, 200, await r.text())
        data = await r.json()
        self.assertEqual("p3", data["loaded"])
        self.assertIn("p3", raw.read_text(encoding="utf-8"))

        # 切过去之后就不能删了（路由读的是 config.load()，夹具里就是 self.cfg）。
        self.cfg["prompt"]["id"] = "p3"
        r = await self.client.delete("/api/presets/p3", headers=self.csrf())
        self.assertEqual(r.status, 400)
        self.assertIn("当前预设", (await r.json())["error"])

    async def test_pack_export_and_import_roundtrip(self):
        (self.root / "persona" / "presets" / "p2").mkdir(parents=True)
        (self.root / "persona" / "presets" / "p2" / "preset.yaml").write_text(
            "name: p2\nblocks:\n- id: h\n  slot: start\n  text: 你好\n",
            encoding="utf-8")
        r = await self.client.get("/api/pack/export?kind=preset&id=p2")
        self.assertEqual(r.status, 200)
        self.assertEqual("application/zip", r.headers["Content-Type"])
        blob = await r.read()

        r = await self.client.post("/api/pack/import?kind=preset&overwrite=1",
                                   data=blob, headers=self.csrf())
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual("p2", (await r.json())["id"])
        imported = (self.root / "persona" / "presets" / "p2"
                    / "preset.yaml").read_text(encoding="utf-8")
        self.assertIn("你好", imported)

    async def test_role_pack_export_downloads_zip(self):
        folder = self.root / "models" / "测试形象"
        folder.mkdir(parents=True)
        (folder / "m.model3.json").write_text(json.dumps(MODEL3), encoding="utf-8")
        (folder / "m.moc3").write_bytes(MOC)
        (folder / "t.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
        self.ui._catalog.rescan()
        aid = self.ui._catalog.selected().id
        role = self.root / "persona" / "roles" / aid
        role.mkdir(parents=True)
        (role / "persona.md").write_text("我是谁", encoding="utf-8")

        r = await self.client.get(f"/api/pack/export?kind=role&id={aid}")
        body = await r.read()
        self.assertEqual(r.status, 200, body[:400].decode("utf-8", "replace"))
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = archive.namelist()
        self.assertIn("manifest.json", names)
        self.assertIn("role/persona.md", names)
        self.assertTrue(any(n.startswith("avatar/测试形象/") for n in names))

    async def test_pack_export_missing_id_is_404(self):
        r = await self.client.get("/api/pack/export?kind=preset&id=nope")
        self.assertEqual(r.status, 404)


if __name__ == "__main__":
    unittest.main()
