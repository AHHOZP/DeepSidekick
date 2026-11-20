from __future__ import annotations

import tempfile
import unittest

from sidekick.config import tts_presets
from sidekick.output.guard import gate
from identity_fixture import runtime_cfg
from sidekick.brain.prompt import PromptBuilder


class TtsPromptRulesTests(unittest.TestCase):
    def test_preset_prompts_match_their_syntax(self):
        self.assertEqual(tts_presets.prompt_rules("edge"), "")
        self.assertEqual(tts_presets.allowed_tags("edge"), frozenset())

        mimo = tts_presets.prompt_rules("mimo")
        self.assertIn("[发声标签（MiMo）]", mimo)
        self.assertIn("（小声）", mimo)
        self.assertEqual(tts_presets.allowed_tags("mimo"),
                         frozenset(tts_presets.PRESETS["mimo"]["tags"]["words"]))
        self.assertNotIn("中文圆括号标签", mimo)

        fish = tts_presets.prompt_rules("fish")
        self.assertIn("[发声标签（Fish S2）]", fish)
        self.assertIn("[whispering]", fish)
        self.assertIn("[chuckling]", fish)
        self.assertIn("不要用中文圆括号当标签", fish)

        minimax = tts_presets.prompt_rules("minimax")
        self.assertIn("[发声标签（MiniMax）]", minimax)
        self.assertIn("(laughs)", minimax)

    def test_prompt_builder_appends_only_selected_preset_rules(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = runtime_cfg(td)
            plain = PromptBuilder(cfg, td)
            self.assertNotIn("发声标签", plain.build_tail_block())

            mimo = PromptBuilder(
                cfg, td, speech_rules=tts_presets.prompt_rules("mimo"))
            self.assertIn("[发声标签（MiMo）]", mimo.build_tail_block())
            self.assertNotIn("Fish S2", mimo.build_tail_block())

            fish = PromptBuilder(
                cfg, td, speech_rules=tts_presets.prompt_rules("fish"))
            self.assertIn("[发声标签（Fish S2）]", fish.build_tail_block())

    def test_edge_output_strips_tags(self):
        clean, why = gate("（小声）你好", 70, 2, [], allowed_tags=frozenset())
        self.assertIsNone(why)
        self.assertEqual(clean, "你好")

    def test_long_and_unclosed_action_tags_are_removed(self):
        long_tag = "（这是一个超过二十四个字的非常非常长的动作描写内容啊）你好"
        clean, why = gate(long_tag, 100, 2, [])
        self.assertIsNone(why)
        self.assertEqual(clean, "你好")
        clean, why = gate("（未闭合的动作描写 你好", 100, 2, [])
        self.assertEqual(clean, "")
        self.assertIsNotNone(why)

    def test_windows_paths_are_rejected_before_sanitize(self):
        for text in (r"C:\Users\test\Desktop\secret.txt",
                     "C:/Users/test/Desktop/secret.txt",
                     r"\\server\share\secret.txt",
                     "//server/share/secret.txt"):
            clean, why = gate(text, 100, 2, [])
            self.assertIsNotNone(why, text)
            self.assertIn("路径", why)

    def test_fish_keeps_natural_language_square_tags(self):
        clean, why = gate("你好[pause]", 70, 2, [],
                          allowed_tags=tts_presets.allowed_tags("fish"),
                          keep_square=tts_presets.preserve_square_tags("fish"))
        self.assertIsNone(why)
        self.assertEqual(clean, "你好[pause]")

    def test_minimax_ascii_brackets_are_preserved(self):
        clean, why = gate("你好(laughs)", 70, 2, [],
                          allowed_tags=tts_presets.allowed_tags("minimax"))
        self.assertIsNone(why)
        self.assertEqual(clean, "你好(laughs)")

    def test_fish_adapts_chinese_tags_to_english(self):
        text = tts_presets.adapt_text("fish", "（小声）别过来（叹气）")
        self.assertEqual(text, "[whispering]别过来[sighing]")
        self.assertEqual(tts_presets.adapt_text("mimo", "（小声）别过来"),
                         "（小声）别过来")


if __name__ == "__main__":
    unittest.main()
