from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from webui_fixture import webui


class WebUiInitTests(QuietIsolatedAsyncioTestCase):
    async def test_real_constructor_serves_index_static_and_overlay(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            overlay = root / "overlay"
            overlay.mkdir()
            (overlay / "index.html").write_text(
                "<html>overlay</html>", encoding="utf-8")
            (overlay / "inspect.js").write_text(
                "window.LMI = {};", encoding="utf-8")
            with webui(root) as ui:
                self.assertEqual(ui.root, root)
                self.assertIsNotNone(ui.supervisor)
                self.assertIsNotNone(ui.feed)

                client = TestClient(TestServer(ui.app()))
                await client.start_server()
                try:
                    r = await client.get("/")
                    self.assertEqual(r.status, 200)
                    self.assertIn("<html", (await r.text()).lower())

                    r = await client.get("/ui/app.js")
                    self.assertEqual(r.status, 200)
                    self.assertGreater(len(await r.read()), 1000)

                    r = await client.get("/ov/index.html")
                    self.assertEqual(r.status, 200)
                    self.assertIn("overlay", await r.text())

                    r = await client.get("/ov/inspect.js")
                    self.assertEqual(r.status, 200)
                    self.assertIn("window.LMI", await r.text())
                finally:
                    await client.close()


if __name__ == "__main__":
    unittest.main()
