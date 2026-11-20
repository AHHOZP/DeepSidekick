from __future__ import annotations

import unittest

from sidekick import config
from sidekick.config import llm_presets, tts_presets
from sidekick.config.spec import NUM_RULES


def _add_profile(cfg: dict, name: str, preset: str, **values) -> dict:
    block = tts_presets.default_block(preset)
    block.update(values)
    block.setdefault("api_key", "")
    cfg["tts"][name] = block
    cfg["tts"]["source"] = name
    return block


class ConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config.defaults()

    def test_llm_source_rejects_reserved_keys(self):
        self.cfg["llm"]["source"] = "common"
        self.assertIn("llm.source", " ".join(config.validate(self.cfg)))

    def test_llm_source_requires_profile_and_fields(self):
        self.cfg["llm"]["source"] = "ghost"
        self.cfg["llm"]["ghost"] = {"base_url": "", "model": ""}
        errs = " ".join(config.validate(self.cfg))
        self.assertIn("llm.ghost.base_url", errs)
        self.assertIn("llm.ghost.model", errs)

    def test_llm_temperature_must_be_a_number_in_range(self):
        name = llm_presets.DEFAULT_ID
        for value in ("hot", -0.1, 2.1):
            cfg = config.defaults()
            cfg["llm"][name]["temperature"] = value
            self.assertIn(f"llm.{name}.temperature",
                          " ".join(config.validate(cfg)), value)

    def test_llm_headers_and_body_must_be_dicts(self):
        name = llm_presets.DEFAULT_ID
        for key, value in (("headers", ["X-A"]), ("body", "top_p=1")):
            cfg = config.defaults()
            cfg["llm"][name][key] = value
            self.assertIn(f"llm.{name}.{key}",
                          " ".join(config.validate(cfg)), key)

    def test_llm_profile_names_are_case_sensitive(self):
        cfg = config.defaults()
        cfg["llm"]["MyGateway"] = dict(cfg["llm"][llm_presets.DEFAULT_ID])
        cfg["llm"]["source"] = "MyGateway"
        self.assertEqual(config.validate(cfg), [])
        cfg["llm"]["source"] = "mygateway"
        self.assertIn("llm.source", " ".join(config.validate(cfg)))

    def test_bad_regex_is_rejected(self):
        self.cfg["guard"]["extra_patterns"] = ["("]
        self.assertIn("guard.extra_patterns", " ".join(config.validate(self.cfg)))

    def test_stage_gap_allows_negative_but_not_unbounded(self):
        self.cfg["stage"]["bubble"]["gap"] = -10
        self.assertEqual(config.validate(self.cfg), [])
        self.cfg["stage"]["bubble"]["gap"] = -500
        self.assertIn("stage.bubble.gap", " ".join(config.validate(self.cfg)))

    def test_autonomy_switches_have_no_generation_temperature(self):
        self.assertTrue(self.cfg["stage"]["autonomy"]["enabled"])
        self.assertTrue(self.cfg["stage"]["actions"]["enabled"])
        self.assertNotIn("temperature", self.cfg["stage"]["autonomy"])

    def test_num_rules_point_at_default_fields(self):
        for path, *_ in NUM_RULES:
            self.assertIsNotNone(config.get_path(self.cfg, path), path)

    def test_tts_profiles_validate_preset_fields(self):
        self.assertEqual(config.validate(self.cfg), [])

        fish = _add_profile(self.cfg, "fish", "fish")
        self.assertEqual(config.validate(self.cfg), [])
        self.assertEqual(fish["voice"], "")

        bad = config.defaults()
        _add_profile(bad, "fish", "fish")
        bad["tts"]["fish"]["preset"] = "nope"
        self.assertIn("tts.fish.preset", " ".join(config.validate(bad)))

        bad = config.defaults()
        _add_profile(bad, "doubao", "doubao", voice="")
        self.assertIn("tts.doubao.voice", " ".join(config.validate(bad)))

        bad = config.defaults()
        _add_profile(bad, "cartesia", "cartesia", voice="v")
        bad["tts"]["cartesia"]["base_url"] = ""
        self.assertIn("tts.cartesia.base_url", " ".join(config.validate(bad)))

        bad = config.defaults()
        bad["tts"]["edge"]["voice"] = "nope"
        self.assertIn("tts.edge.voice", " ".join(config.validate(bad)))

    def test_tts_source_must_point_at_a_profile(self):
        self.cfg["tts"]["source"] = "common"
        self.assertIn("tts.source", " ".join(config.validate(self.cfg)))
        self.cfg["tts"]["source"] = "ghost"
        self.assertIn("tts.source", " ".join(config.validate(self.cfg)))

    def test_all_allowed_enum_choices_validate(self):
        from sidekick.config.spec import ENUM_RULES

        for path, allowed, _insensitive in ENUM_RULES:
            for value in allowed:
                cfg = config.defaults()
                _add_profile(cfg, "fish", "fish")
                config.set_path(cfg, path, value)
                self.assertEqual(config.validate(cfg), [], (path, value))

    def test_capture_mode_enum(self):
        self.cfg["capture"]["mode"] = "cinema"
        self.assertIn("capture.mode", " ".join(config.validate(self.cfg)))
        for mode in ("active", "calm"):
            self.cfg["capture"]["mode"] = mode
            self.assertEqual([], config.validate(self.cfg))
        for pct in ("66.6", "83.3", "100"):
            cfg = config.defaults()
            cfg["capture"]["clarity_percent"] = pct
            self.assertEqual([], config.validate(cfg))
        cfg = config.defaults()
        cfg["capture"]["clarity_percent"] = "70"
        self.assertIn("capture.clarity_percent", " ".join(config.validate(cfg)))

    def test_mode_manual_enum(self):
        self.cfg["mode"]["manual"] = "loud"
        self.assertIn("mode.manual", " ".join(config.validate(self.cfg)))
        self.cfg["mode"]["manual"] = "silent"
        self.assertEqual(config.validate(self.cfg), [])

    def test_tts_common_ranges_and_enum_are_enforced(self):
        cases = [
            ("tts.common.segments", "page"),
            ("tts.common.gain", 2.5),
            ("tts.common.timeout_s", 0),
        ]
        for path, value in cases:
            cfg = config.defaults()
            config.set_path(cfg, path, value)
            self.assertIn(path, " ".join(config.validate(cfg)), path)

    def test_history_window_must_keep_pairs(self):
        for history, every in ((4, 4), (5, 2), (20, 11)):
            cfg = config.defaults()
            cfg["memory"]["history_max"] = history
            cfg["memory"]["summarize_every"] = every
            errs = config.validate(cfg)
            self.assertTrue(any("memory." in e for e in errs), (history, every))
        cfg = config.defaults()
        cfg["memory"]["history_max"] = 20
        cfg["memory"]["summarize_every"] = 10
        self.assertEqual(config.validate(cfg), [])

    def test_string_numbers_are_coerced_or_rejected(self):
        c = config.defaults()
        c["memory"]["summarize_every"] = "10"
        c["memory"]["history_max"] = 20
        self.assertEqual([], config.validate(c))
        c["memory"]["summarize_every"] = "not-a-number"
        errs = config.validate(c)
        self.assertIn("memory.summarize_every 必须是数字", errs)

    def test_stage_host_is_loopback_only(self):
        c = config.defaults()
        c["stage"]["host"] = "0.0.0.0"
        self.assertTrue(any("stage.host" in e for e in config.validate(c)))


if __name__ == "__main__":
    unittest.main()
