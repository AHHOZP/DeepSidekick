from __future__ import annotations

import unittest
from unittest import mock

from sidekick import config
from sidekick.webui import form as schema


class RedactedTests(unittest.TestCase):
    def test_old_flat_secret_fields_are_not_returned(self):
        cfg = config.defaults()
        cfg["llm"]["api_key"] = "sk-old"
        cfg["tts"]["api_key"] = "tts-old"
        cfg["llm"]["headers"] = {"Authorization": "Bearer old"}
        red = config.redacted(cfg)
        self.assertNotIn("api_key", red["llm"])
        self.assertNotIn("api_key", red["tts"])
        self.assertNotIn("headers", red["llm"])

    def test_nested_and_list_secrets_are_redacted(self):
        from sidekick.config import secrets

        cfg = config.defaults()
        cfg["llm"]["custom"] = {
            "body": {"api_key": "sk-body"},
            "headers": {"Authorization": "Bearer nested"},
            "extra": [{"api_key": "sk-list"}],
        }
        red = config.redacted(cfg)
        custom = red["llm"]["custom"]
        self.assertEqual(secrets.SEALED, custom["body"]["api_key"])
        self.assertEqual(secrets.SEALED, custom["headers"]["Authorization"])
        self.assertEqual(secrets.SEALED, custom["extra"][0]["api_key"])


class CleanTagsTests(unittest.TestCase):
    def test_ascii_brackets_are_kept_as_written(self):
        from sidekick.core.text import clean_tags

        self.assertEqual(clean_tags("你好(laughs)", {"laughs"}),
                         "你好(laughs)")
        self.assertEqual(clean_tags("你好（笑）", {"笑"}), "你好（笑）")


class LoopbackOriginTests(unittest.TestCase):
    def test_accepts_loopback_and_rejects_foreign(self):
        from sidekick.core.http_json import is_loopback_origin

        for ok in ("http://127.0.0.1:17891", "https://localhost",
                   "http://[::1]:17891"):
            self.assertTrue(is_loopback_origin(ok), ok)
        for bad in ("http://evil.example", "http://127.0.0.1.evil.example",
                    "file:///c:/x", ""):
            self.assertFalse(is_loopback_origin(bad), bad)


class SchemaTests(unittest.TestCase):
    def test_every_webui_field_has_a_label(self):
        from sidekick.config.spec import GROUPS, LABELS

        fields = [p for g in GROUPS for s in g["sections"] for p in s["fields"]]
        self.assertEqual([], [p for p in fields if p not in LABELS])

    def test_every_default_covered_or_intentionally_hidden(self):
        # 用默认配置做覆盖检查，不读本机 config.yaml（否则开发机有配置才绿）。
        with mock.patch.object(config, "load_raw",
                               return_value=config.defaults()):
            covered = schema.all_schema_paths()
        missing = schema.leaf_paths(config.defaults()) - covered
        llm = config.defaults()["llm"]
        profile_prefixes = tuple(
            f"llm.{name}." for name in llm
            if name not in ("source", "common"))
        missing = {p for p in missing
                   if not p.startswith(profile_prefixes)
                   and not p.startswith("stt.models.")}
        hidden = {
            "role.id",
            "user.id",
            "barge.cooldown_ms",
            "llm.common.warmup",
            "llm.common.system_mode",
            "llm.common.timeout_s",
            "game.id",
            "capture.monitor", "capture.backend", "capture.resolution",
            "capture.interval_s", "capture.max_frames", "capture.dedupe",
            "capture.dedupe_global_threshold",
            "capture.dedupe_block_threshold", "capture.subtitle_roi_weight",
            "capture.capture_now_stale_s", "capture.ring_size",
            "capture.subtitle_strip_enabled", "capture.privacy_processes",
            "scheduler.min_gap_s",
            "memory.db_path", "memory.min_keyword_len",
            "memory.embed_enabled", "memory.embed_dir",
            "memory.embed_threads", "memory.embed_timeout_ms",
            "stt.model_dir", "stt.fallback", "stt.max_chars_per_sec",
            "errors.retry_delay_ms",
            "errors.fallback_cooldown_s", "errors.fallback_max_apologies",
            "errors.recover_say_s", "errors.fallback_dir",
            "state.ring_size", "state.say_log",
            "webui.host", "webui.port", "webui.auto_open",
        }
        self.assertEqual(missing, hidden)


if __name__ == "__main__":
    unittest.main()
