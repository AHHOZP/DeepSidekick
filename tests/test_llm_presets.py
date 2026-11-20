from __future__ import annotations

import unittest

from sidekick.config import llm_presets, secrets


class LlmPresetCatalogTests(unittest.TestCase):
    def test_each_preset_has_required_connection_fields(self):
        for preset_id, spec in llm_presets.PRESETS.items():
            self.assertTrue(str(spec["label"]).strip(), preset_id)
            self.assertTrue(str(spec["base_url"]).strip(), preset_id)
            self.assertTrue(str(spec["model"]).strip(), preset_id)
            scheme = str(spec["base_url"]).split(":", 1)[0]
            self.assertIn(scheme, ("http", "https"), preset_id)
            self.assertIsInstance(spec.get("headers") or {}, dict, preset_id)
            self.assertIsInstance(spec.get("needs_key", True), bool, preset_id)

    def test_preset_headers_are_not_secret(self):
        for preset_id, spec in llm_presets.PRESETS.items():
            for name in (spec.get("headers") or {}):
                self.assertFalse(secrets.is_secret_header(name),
                                 (preset_id, name))

    def test_default_id_and_generation_defaults(self):
        self.assertIn(llm_presets.DEFAULT_ID, llm_presets.PRESETS)
        self.assertEqual(set(llm_presets.DEFAULT_GEN),
                         {"temperature", "reasoning_effort"})

    def test_profile_block_is_a_complete_independent_copy(self):
        for preset_id in llm_presets.PRESETS:
            block = llm_presets.profile_block(preset_id)
            self.assertEqual(list(block), ["base_url", "model", "api_key",
                                           "temperature", "reasoning_effort",
                                           "headers", "body"])
            self.assertEqual(block["temperature"], 0.9)
            self.assertEqual(block["reasoning_effort"], "low")
            self.assertEqual(block["api_key"], "")
            self.assertEqual(block["body"], {})
            block["headers"]["mutated"] = "1"
            self.assertNotIn("mutated",
                             llm_presets.profile_block(preset_id)["headers"])

    def test_get_is_case_insensitive(self):
        for preset_id in llm_presets.PRESETS:
            self.assertIsNotNone(llm_presets.get(preset_id.upper()))
        self.assertIsNone(llm_presets.get("nope"))
        self.assertIsNone(llm_presets.get(""))


if __name__ == "__main__":
    unittest.main()
