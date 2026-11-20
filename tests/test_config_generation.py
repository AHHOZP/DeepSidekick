from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from sidekick import config
from sidekick.config import llm_presets, secrets
from sidekick.config.spec import DEFAULTS, DEFAULT_ORDER, default_document
from sidekick.webui.form import leaf_paths


class DefaultConfigGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg_path = self.root / "config.yaml"
        self.patches = [
            mock.patch.object(config, "CONFIG_PATH", self.cfg_path),
            mock.patch.object(secrets, "ROOT", self.root),
        ]
        for p in self.patches:
            p.start()
        config._load_cache = None
        config.last_error = None
        secrets.last_error = None

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        config._load_cache = None
        config.last_error = None
        secrets.last_error = None
        self.tmp.cleanup()

    def test_missing_config_is_generated_in_page_order_without_comments(self):
        created, err = config.ensure_default_config()
        self.assertTrue(created)
        self.assertIsNone(err)
        text = self.cfg_path.read_text(encoding="utf-8")
        self.assertNotIn("#", text)
        raw = yaml.safe_load(text)
        self.assertEqual(leaf_paths(DEFAULTS), leaf_paths(raw))
        self.assertEqual(list(default_document()), list(raw))
        self.assertEqual(raw["sessdata"], "")
        order = list(raw)
        self.assertLess(order.index("stt"), order.index("tts"))
        self.assertEqual(order[0], "llm")
        self.assertLess(order.index("capture"), order.index("session"))
        self.assertLess(order.index("game"), order.index("brain"))
        self.assertLess(order.index("brain"), order.index("memory"))

    def test_default_order_covers_every_default_key(self):
        self.assertEqual(set(DEFAULT_ORDER), set(DEFAULTS))

    def test_existing_config_is_not_overwritten(self):
        self.cfg_path.write_text("webui:\n  port: 17999\n", encoding="utf-8")
        created, err = config.ensure_default_config()
        self.assertFalse(created)
        self.assertIsNone(err)
        self.assertIn("17999", self.cfg_path.read_text(encoding="utf-8"))

    def test_existing_api_keys_are_referenced_as_sealed(self):
        key_name = f"llm.{llm_presets.DEFAULT_ID}.api_key"
        with mock.patch.object(secrets, "load", return_value={
                key_name: "D",
                "tts.fish.api_key": "F",
                "sessdata": "COOKIE"}):
            created, _err = config.ensure_default_config()
        self.assertTrue(created)
        raw = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["llm"][llm_presets.DEFAULT_ID]["api_key"],
                         secrets.SEALED)
        # 默认配置只有内置 Edge；其他厂商由 WebUI 新建。
        self.assertEqual(set(raw["tts"]), {"enabled", "source", "common", "edge"})
        self.assertEqual(raw["sessdata"], "")

    def test_default_llm_profile_comes_from_preset_catalog(self):
        config.ensure_default_config()
        raw = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["llm"]["source"], llm_presets.DEFAULT_ID)
        self.assertEqual(
            set(raw["llm"]),
            {"source", "common", llm_presets.DEFAULT_ID})
        block = raw["llm"][llm_presets.DEFAULT_ID]
        self.assertEqual(block["temperature"], 0.9)
        self.assertEqual(block["reasoning_effort"], "low")
        self.assertEqual(block["api_key"], "")
        self.assertEqual(block["body"], {})
        for legacy in ("user_agent", "session_header", "session_id"):
            self.assertNotIn(legacy, block)

    def test_unreadable_secrets_leave_all_keys_empty(self):
        with mock.patch.object(secrets, "load", return_value={}), \
                mock.patch.object(secrets, "last_error", "bad bin"):
            config.ensure_default_config()
        raw = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["llm"][llm_presets.DEFAULT_ID]["api_key"], "")
        self.assertNotIn("fish", raw["tts"])

    def test_save_drops_plaintext_secrets(self):
        raw = {
            "llm": {"foo": {"api_key": "sk-PLAINTEXT",
                            "headers": {"Authorization": "Bearer PLAIN"}}},
            "sessdata": "COOKIE",
        }
        config.save_raw(raw)
        saved = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["llm"]["foo"]["api_key"], "")
        self.assertEqual(saved["llm"]["foo"]["headers"]["Authorization"], "")
        self.assertEqual(saved["sessdata"], "")

    def test_empty_file_is_an_error_not_a_missing_file(self):
        self.cfg_path.write_text("", encoding="utf-8")
        created, _err = config.ensure_default_config()
        self.assertFalse(created)
        config.load()
        self.assertIn("空文件", config.last_error or "")
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
