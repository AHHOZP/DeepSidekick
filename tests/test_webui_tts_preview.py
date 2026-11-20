from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from aiohttp.test_utils import TestClient, TestServer

from sidekick import config
from sidekick.config import tts_presets
from sidekick.output.tts.base import SynthResult
from sidekick.webui.routes.config import preview_tts_cfg
from sidekick.webui.status_feed import StatusFeed
from webui_fixture import webui


class TtsPreviewDraftTests(unittest.TestCase):
    def _cfg(self):
        cfg = config.defaults()
        block = tts_presets.default_block("fish")
        block["api_key"] = "REAL"
        cfg["tts"]["fish"] = block
        cfg["tts"]["source"] = "fish"
        return cfg

    def test_unsaved_key_wins_and_literal_sealed_is_never_sent(self):
        cfg = self._cfg()
        out = preview_tts_cfg(cfg, "fish", "fish",
                              {"api_key": "sealed"}, "UNSAVED")
        self.assertEqual(out["tts"]["__preview__"]["api_key"], "UNSAVED")

    def test_sealed_without_store_keeps_existing_real_key(self):
        cfg = self._cfg()
        with mock.patch("sidekick.webui.routes.config.secretstore.get",
                        return_value=""):
            out = preview_tts_cfg(cfg, "fish", "fish",
                                  {"api_key": "sealed"}, None)
        self.assertEqual(out["tts"]["__preview__"]["api_key"], "REAL")


class TtsPreviewHttpTests(QuietIsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = config.defaults()
        fish = tts_presets.default_block("fish")
        fish["api_key"] = "REAL"
        self.cfg["tts"]["fish"] = fish
        self.cfg["tts"]["source"] = "fish"
        self.cfg["memory"]["enabled"] = False
        self.stack = contextlib.ExitStack()
        self.ui = self.stack.enter_context(webui(root, self.cfg))
        self.ui.csrf = "test-token"

        class Sup:
            async def status(self):
                return {}

        self.ui.supervisor = Sup()
        self.ui.feed = StatusFeed(self.ui.supervisor, self.ui.memory_stats,
                                  started_at=self.ui.started_at)
        self.load_patch = mock.patch("sidekick.webui.app.config.load",
                                     return_value=self.cfg)
        self.load_patch.start()
        self.secret_patch = mock.patch(
            "sidekick.webui.routes.config.secretstore.get", return_value="STORED")
        self.secret_patch.start()
        self.captured = {}

        def fake_make(cfg):
            self.captured.update(cfg["tts"]["__preview__"])

            class Provider:
                async def synth(self, text):
                    return SynthResult(np.zeros(240, dtype=np.float32), 24000)

                async def close(self):
                    return None

            return Provider()

        self.tts_patch = mock.patch("sidekick.output.tts.make", side_effect=fake_make)
        self.tts_patch.start()
        self.client = TestClient(TestServer(self.ui.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tts_patch.stop()
        self.secret_patch.stop()
        self.load_patch.stop()
        self.stack.close()
        self.tmp.cleanup()

    async def test_preview_uses_form_block_and_store_key(self):
        r = await self.client.post(
            "/api/tts/preview",
            json={"profile": "fish", "preset": "fish", "block": {
                "base_url": "https://api.fish.audio",
                "voice": "new-voice", "api_key": "sealed",
            }, "text": "试听"},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 200)
        self.assertIn("audio/wav", r.headers.get("Content-Type", ""))
        self.assertEqual(self.captured["voice"], "new-voice")
        self.assertEqual(self.captured["api_key"], "STORED")


if __name__ == "__main__":
    unittest.main()
