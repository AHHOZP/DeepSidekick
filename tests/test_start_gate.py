from __future__ import annotations

import unittest
from unittest import mock

from sidekick import config
from sidekick.config import llm_presets, tts_presets
from sidekick.webui.routes.runtime import missing_config, tts_problem

DEFAULT = llm_presets.DEFAULT_ID


def _profile(cfg: dict, name: str, preset: str, *, key: str = "") -> dict:
    block = tts_presets.default_block(preset)
    block["api_key"] = key
    cfg["tts"][name] = block
    cfg["tts"]["source"] = name
    return block


class StartGateTests(unittest.TestCase):
    def test_default_preset_without_key_is_blocked(self):
        cfg = config.defaults()
        self.assertEqual(missing_config(cfg), [f"llm.{DEFAULT}.api_key"])
        cfg["llm"][DEFAULT]["api_key"] = "brain-key"
        self.assertEqual(missing_config(cfg), [])

    def test_missing_base_url_and_model_are_reported(self):
        cfg = config.defaults()
        cfg["llm"][DEFAULT].update({"api_key": "k",
                                    "base_url": "", "model": " "})
        self.assertEqual(missing_config(cfg),
                         [f"llm.{DEFAULT}.base_url",
                          f"llm.{DEFAULT}.model"])

    def test_custom_profile_without_key_is_not_blocked(self):
        cfg = config.defaults()
        cfg["llm"]["source"] = "local"
        cfg["llm"]["local"] = {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "m",
            "api_key": "",
            "temperature": 0.9,
            "reasoning_effort": "",
            "headers": {},
            "body": {},
        }
        self.assertEqual(missing_config(cfg), [])

    def test_preset_with_needs_key_false_is_not_blocked(self):
        local = {
            "label": "Local",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "m",
            "needs_key": False,
        }
        with mock.patch.dict(llm_presets.PRESETS, {"local": local}):
            cfg = config.defaults()
            cfg["llm"]["source"] = "local"
            cfg["llm"]["local"] = llm_presets.profile_block("local")
            self.assertEqual(missing_config(cfg), [])

    def test_secret_header_counts_as_configured_key(self):
        cfg = config.defaults()
        cfg["llm"][DEFAULT]["headers"] = {"X-Api-Key": "abc"}
        self.assertEqual(missing_config(cfg), [])

    def test_tts_key_gate_is_unchanged(self):
        cfg = config.defaults()
        cfg["llm"][DEFAULT]["api_key"] = "brain-key"
        _profile(cfg, "fish", "fish")
        self.assertEqual(missing_config(cfg), ["tts.fish.api_key"])
        _profile(cfg, "mimo", "mimo")
        self.assertEqual(missing_config(cfg), ["tts.mimo.api_key"])
        _profile(cfg, "fish", "fish", key="k")
        self.assertEqual(missing_config(cfg), [])

        cfg = config.defaults()
        cfg["llm"][DEFAULT]["api_key"] = "brain-key"
        _profile(cfg, "fish", "fish")
        cfg["tts"]["enabled"] = False
        self.assertEqual(missing_config(cfg), [])

        cfg = config.defaults()
        cfg["llm"][DEFAULT]["api_key"] = "brain-key"
        cfg["sessdata"] = "some-cookie"
        self.assertEqual(missing_config(cfg), [])

    def test_broken_tts_source_is_reported_before_start(self):
        cfg = config.defaults()
        cfg["tts"]["source"] = "ghost"
        self.assertIn("tts.source", tts_problem(cfg) or "")

        cfg = config.defaults()
        _profile(cfg, "fish", "fish")
        cfg["tts"]["fish"]["preset"] = "nope"
        self.assertIn("tts.source", tts_problem(cfg) or "")

        cfg = config.defaults()
        cfg["tts"]["enabled"] = False
        cfg["tts"]["source"] = "ghost"
        self.assertIsNone(tts_problem(cfg))


if __name__ == "__main__":
    unittest.main()

