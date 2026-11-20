from __future__ import annotations

import re
import unittest

from sidekick.config import tts_presets
from sidekick.config.spec import DEFAULTS

_PLACEHOLDER = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def _walk_strings(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)
    elif isinstance(value, str):
        yield value


class PresetContractTests(unittest.TestCase):
    def test_structure(self):
        self.assertEqual(list(tts_presets.PRESETS)[:2], ["edge", "openai"])
        for preset_id, spec in tts_presets.PRESETS.items():
            with self.subTest(preset=preset_id):
                self.assertTrue(spec.get("label"))
                self.assertTrue(spec.get("desc"))
                self.assertTrue(spec.get("doc"))
                self.assertIn(spec.get("wire"), ("http", "edge"))
                self.assertIn(spec.get("audio", "pcm"), ("pcm", "wav", "mp3"))
                if spec["wire"] == "http":
                    self.assertTrue(spec.get("path"))
                    self.assertIn("base_url", spec["fields"])
                    self.assertIn(spec["response"]["kind"], ("raw", "sse"))
                    if spec.get("audio", "pcm") == "pcm":
                        self.assertGreater(int(spec.get("pcm_sr") or 0), 0)
                else:
                    self.assertNotIn("path", spec)
                    self.assertNotIn("response", spec)

    def test_placeholders_are_declared(self):
        for preset_id, spec in tts_presets.PRESETS.items():
            if spec["wire"] != "http":
                continue
            fields = set(spec["fields"])
            allowed = fields | {"text", "pcm_sr"}
            chunks = [spec.get("path", ""), spec.get("query", {}),
                      spec.get("headers", {}), spec.get("body", {})]
            used = set()
            for chunk in chunks:
                for text in _walk_strings(chunk):
                    used.update(_PLACEHOLDER.findall(text))
            with self.subTest(preset=preset_id):
                self.assertLessEqual(used, allowed)
                body_texts = list(_walk_strings(spec.get("body", {})))
                self.assertTrue(any("{text}" in t for t in body_texts))
                for key in fields:
                    if key in ("base_url", "pcm_sr"):
                        continue
                    self.assertIn(key, used, key)

    def test_field_metadata(self):
        for preset_id, spec in tts_presets.PRESETS.items():
            for key, meta in spec["fields"].items():
                with self.subTest(preset=preset_id, field=key):
                    self.assertIn(meta.get("type"), ("text", "password", "select"))
                    self.assertIn("default", meta)
                    self.assertIsInstance(meta.get("required", False), bool)
                    if meta["type"] == "select":
                        values = [v for v, _ in meta["options"]]
                        self.assertTrue(values)
                        self.assertIn(meta["default"], values)

    def test_response_metadata(self):
        for preset_id, spec in tts_presets.PRESETS.items():
            if spec["wire"] != "http":
                continue
            resp = spec["response"]
            with self.subTest(preset=preset_id):
                if resp["kind"] == "sse":
                    self.assertTrue(resp.get("path"))
                    self.assertIn(resp.get("encoding"), ("base64", "hex"))
                self.assertIsInstance(spec.get("stream", True), bool)

    def test_tag_rules_derive_consistently(self):
        for preset_id, spec in tts_presets.PRESETS.items():
            tags = spec.get("tags")
            if not tags:
                self.assertEqual(tts_presets.prompt_rules(preset_id), "")
                self.assertEqual(tts_presets.allowed_tags(preset_id), frozenset())
                self.assertEqual(tts_presets.preserve_square_tags(preset_id),
                                 frozenset())
                continue
            with self.subTest(preset=preset_id):
                self.assertTrue(tags.get("open"))
                self.assertTrue(tags.get("close"))
                self.assertTrue(tags.get("words"))
                for value in (tags.get("map") or {}).values():
                    self.assertIn(value, tags["words"])
                allowed = tts_presets.allowed_tags(preset_id)
                self.assertTrue(set(tags["words"]) <= allowed)
                if tags["open"] == "[":
                    self.assertTrue(tts_presets.preserve_square_tags(preset_id))
                else:
                    self.assertEqual(tts_presets.preserve_square_tags(preset_id),
                                     frozenset())

    def test_fish_adapts_and_minimax_keeps_ascii(self):
        self.assertEqual(tts_presets.adapt_text("fish", "（小声）别过来"),
                         "[whispering]别过来")
        self.assertEqual(tts_presets.adapt_text("fish", "（不存在）别过来"),
                         "别过来")
        self.assertEqual(tts_presets.adapt_text("minimax", "(laughs)哈哈"),
                         "(laughs)哈哈")

    def test_validate_profile(self):
        self.assertEqual(tts_presets.validate_profile(
            "edge", tts_presets.default_block("edge")), [])
        self.assertIn("preset", " ".join(tts_presets.validate_profile(
            "x", {"preset": "nope"})))
        bad = tts_presets.default_block("doubao")
        bad["voice"] = ""
        self.assertIn("voice", " ".join(tts_presets.validate_profile(
            "doubao", bad)))
        bad = tts_presets.default_block("cartesia")
        bad["voice"] = "v"
        bad["base_url"] = ""
        self.assertIn("base_url", " ".join(tts_presets.validate_profile(
            "cartesia", bad)))
        bad = tts_presets.default_block("edge")
        bad["voice"] = "nope"
        self.assertIn("voice", " ".join(tts_presets.validate_profile(
            "edge", bad)))
        self.assertIn("none", " ".join(tts_presets.validate_profile(
            "none", tts_presets.default_block("edge"))))

    def test_field_order_and_unified_labels(self):
        order = ["base_url", "model", "voice", "rate", "emotion",
                 "instructions", "pcm_sr"]
        for preset_id, spec in tts_presets.PRESETS.items():
            keys = list(spec["fields"])
            with self.subTest(preset=preset_id):
                self.assertEqual(keys, [k for k in order if k in keys])
                for key in ("base_url", "model", "voice"):
                    if key in spec["fields"]:
                        label = spec["fields"][key]["label"]
                        self.assertEqual(
                            label,
                            {"base_url": "Base URL", "model": "模型",
                             "voice": "音色"}[key])
        self.assertNotIn("resource_id", tts_presets.PRESETS["doubao"]["fields"])
        self.assertEqual(tts_presets.PRESETS["doubao"]["fields"]["model"]["label"],
                         "模型")

    def test_default_edge_block_comes_from_preset(self):
        self.assertEqual(DEFAULTS["tts"]["edge"],
                         tts_presets.default_block("edge"))


if __name__ == "__main__":
    unittest.main()
