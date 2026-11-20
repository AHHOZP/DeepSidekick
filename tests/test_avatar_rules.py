from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sidekick.avatar import rules as avatar_rules


def avatar(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        id="测试形象", name="测试形象", kind="live2d", root=root,
        role_dir=root, display_name="测试形象",
        capabilities={
            "actions": [{"tag": "挥手", "group": "wave"},
                        {"tag": "自拍", "group": "selfie"}],
            "expressions": [{"tag": "开心", "name": "开心兴奋"}],
        },
    )


class RulesFileTests(unittest.TestCase):
    def test_oversized_actions_file_is_truncated_on_read(self):
        with tempfile.TemporaryDirectory() as td:
            a = avatar(Path(td))
            (Path(td) / "actions.md").write_text("x" * 2500, encoding="utf-8")
            text = avatar_rules.load(a)
            self.assertEqual(2000, len(text))
            self.assertTrue(avatar_rules.payload(a)["truncated"])

    def test_save_load_clear_and_limits(self):
        with tempfile.TemporaryDirectory() as td:
            a = avatar(Path(td))
            self.assertEqual(avatar_rules.load(a), "")
            data = avatar_rules.save(a, "挥手规则")
            self.assertTrue(data["exists"])
            self.assertEqual(data["content"], "挥手规则")
            self.assertEqual(avatar_rules.load(a), "挥手规则")
            with self.assertRaises(avatar_rules.RulesError):
                avatar_rules.save(a, "字" * (avatar_rules.MAX_CHARS + 1))
            avatar_rules.save(a, "")
            self.assertFalse((Path(td) / "actions.md").exists())

    def test_unknown_assets(self):
        with tempfile.TemporaryDirectory() as td:
            a = avatar(Path(td))
            text = "<ACT>motion=挥手, expression=乱写</ACT>"
            self.assertEqual(avatar_rules.unknown_tags(text, a.capabilities),
                             ["乱写"])
            self.assertEqual(avatar_rules.unknown_tags(
                "<ACT>motion=selfie</ACT>", a.capabilities), [])
            self.assertEqual(avatar_rules.unknown_tags(
                "<ACT>motion=nope</ACT>", a.capabilities), ["nope"])


class BuildTextTests(unittest.TestCase):
    def test_lists_every_tag_and_only_the_format(self):
        with tempfile.TemporaryDirectory() as td:
            a = avatar(Path(td))
            text = avatar_rules.build_text(a)
        self.assertIn("可用动作：挥手、自拍", text)
        self.assertIn("可用表情：开心", text)
        self.assertIn("<ACT>motion=挥手</ACT>", text)
        self.assertIn("<ACT>expression=开心</ACT>", text)
        self.assertIn("一句最多一个动作加一个表情", text)
        self.assertNotIn("[标签]", text)
        for word in ("测试形象", "副播", "主播", "直播", "陪玩", "什么时候"):
            self.assertNotIn(word, text, word)

    def test_only_expressions(self):
        a = SimpleNamespace(capabilities={
            "actions": [], "expressions": [{"tag": "脸红", "name": "脸红"}]})
        text = avatar_rules.build_text(a)
        self.assertNotIn("可用动作", text)
        self.assertIn("可用表情：脸红", text)
        self.assertIn("<ACT>expression=脸红</ACT>", text)

    def test_generated_text_has_no_unknown_tags(self):
        with tempfile.TemporaryDirectory() as td:
            a = avatar(Path(td))
            text = avatar_rules.build_text(a)
            self.assertEqual(avatar_rules.unknown_tags(text, a.capabilities), [])

    def test_no_tags_raises(self):
        a = SimpleNamespace(capabilities={"actions": [], "expressions": []})
        with self.assertRaises(avatar_rules.RulesError):
            avatar_rules.build_text(a)


if __name__ == "__main__":
    unittest.main()
