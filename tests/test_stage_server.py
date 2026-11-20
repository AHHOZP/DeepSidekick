from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import asyncio
import tempfile
import unittest
from pathlib import Path

import aiohttp

from sidekick import config
from sidekick.avatar.catalog import AvatarCatalog
from sidekick.avatar.stage import StageClient, serve_stage
from sidekick.core.http_json import CONTROL_HEADER


class StageServerTests(QuietIsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        overlay = root / "overlay"
        overlay.mkdir()
        (overlay / "index.html").write_text(
            '<html><body><script src="/s/app.js"></script></body></html>',
            encoding="utf-8")
        (overlay / "probe.html").write_text("<html>probe</html>",
                                             encoding="utf-8")
        (overlay / "app.js").write_text("console.log('stage')",
                                         encoding="utf-8")
        (overlay / "_private.js").write_text("hidden", encoding="utf-8")
        model = root / "models" / "test"
        model.mkdir(parents=True)
        (model / "test.model3.json").write_text(
            '{"FileReferences": {"Moc": "test.moc3"}}', encoding="utf-8")
        (model / "test.moc3").write_bytes(b"MOC3" + b"\x00" * 8)

        cfg = config.defaults()
        cfg["stage"].update({"host": "127.0.0.1", "port": 0})
        self.cfg = cfg
        self.client = StageClient()
        self.clicks: list[str] = []
        self.runner = await serve_stage(
            self.client, cfg, cfg_provider=lambda: cfg,
            catalog=AvatarCatalog(root), on_click=self.clicks.append)
        port = self.runner.addresses[0][1]
        self.base = f"http://127.0.0.1:{port}/"
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()
        self.tmp.cleanup()

    async def test_static_pages_and_private_files(self):
        async with self.session.get(self.base) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("<html", (await r.text()).lower())
        async with self.session.get(self.base + "probe") as r:
            self.assertEqual(r.status, 200)
        async with self.session.get(self.base + "s/app.js") as r:
            self.assertEqual(r.status, 200)
        async with self.session.get(self.base + "s/_private.js") as r:
            self.assertEqual(r.status, 404)
        for path in ("s/%2e%2e/config.yaml", "s/%5c..%5cconfig.yaml"):
            async with self.session.get(self.base + path) as r:
                self.assertEqual(r.status, 404, path)

    async def test_config_json_has_layout(self):
        async with self.session.get(self.base + "config.json") as r:
            self.assertEqual(r.status, 200)
            self.assertIn("layout", await r.json())

    async def test_ws_hello_say_and_click(self):
        async with self.session.ws_connect(self.base + "ws") as ws:
            self.assertEqual(await ws.receive_json(), {"t": "hello"})
            self.client.emit("say", pages=["a"])
            self.assertEqual(await ws.receive_json(),
                             {"t": "say", "pages": ["a"]})
            await ws.send_json({"t": "click", "part": "头"})
            for _ in range(50):
                if self.clicks:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(self.clicks, ["头"])

    async def test_ws_notice_and_control(self):
        async with self.session.ws_connect(self.base + "ws") as ws:
            self.assertEqual(await ws.receive_json(), {"t": "hello"})
            # 桌宠的一次性提示走独立字段，不与 overlay 的 report 抢位置。
            await ws.send_json({"t": "notice", "text": "模型没有口型参数"})
            for _ in range(50):
                if self.client.stats()["notice"]:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(self.client.stats()["notice"], "模型没有口型参数")

            async with self.session.post(
                    self.base + "control",
                    json={"op": "param", "id": "ParamAngleX",
                          "value": 0.5}) as r:
                self.assertEqual(r.status, 403)
            async with self.session.post(
                    self.base + "control",
                    headers={CONTROL_HEADER: "1"},
                    json={"op": "param", "id": "ParamAngleX",
                          "value": 0.5}) as r:
                self.assertEqual(r.status, 200)
            self.assertEqual(await ws.receive_json(), {
                "t": "control", "op": "param",
                "id": "ParamAngleX", "value": 0.5})

    async def test_origin_guard_rejects_foreign_origin(self):
        with self.assertRaises(aiohttp.WSServerHandshakeError) as ctx:
            await self.session.ws_connect(
                self.base + "ws", headers={"Origin": "http://evil"})
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
