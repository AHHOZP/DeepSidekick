from __future__ import annotations

import unittest

from sidekick.avatar.act_tags import (
    GAZE_POINTS, parse, prompt_rules, prompt_rules_from_profile,
    raw_asset_names, vocabulary,
)


class ActSyntaxTests(unittest.TestCase):
    def test_emotion_action_and_asset_prefix(self):
        parsed = parse(
            "这波稳了。<ACT>emotion=开心, action=挥手:0.7, "
            "motion=自拍, expression=开心</ACT>",
            motions={"自拍": "selfie"}, expressions={"开心": "开心兴奋"})
        self.assertEqual(parsed.text, "这波稳了。")
        self.assertEqual([x["kind"] for x in parsed.items],
                         ["emotion", "action", "motion", "expression"])
        self.assertEqual(parsed.items[0]["name"], "happy")
        self.assertAlmostEqual(parsed.items[1]["intensity"], 0.7)

    def test_bare_token_emotion_wins_then_action(self):
        parsed = parse("<ACT>害羞</ACT><ACT>点头</ACT>")
        self.assertEqual(parsed.text, "")
        self.assertEqual(parsed.items[0]["kind"], "emotion")
        self.assertEqual(parsed.items[0]["name"], "shy")
        self.assertEqual(parsed.items[1]["kind"], "action")
        self.assertEqual(parsed.items[1]["name"], "nod")

    def test_assets_require_prefix(self):
        parsed = parse("<ACT>自拍</ACT>", motions={"自拍": "selfie"})
        self.assertEqual(parsed.items, [])
        self.assertTrue(parsed.warnings)

    def test_asset_names_may_contain_spaces(self):
        parsed = parse(
            "<ACT>motion=举手 切换入, expression=体型1</ACT>",
            motions={"举手 切换入": "举手 切换入"},
            expressions={"体型1": "体型1"})
        self.assertEqual([x["kind"] for x in parsed.items],
                         ["motion", "expression"])
        self.assertEqual(parsed.items[0]["name"], "举手 切换入")
        self.assertEqual(raw_asset_names(
            "<ACT>motion=举手 切换入</ACT>"),
            [("motion", "举手 切换入")])

    def test_space_separated_key_values(self):
        parsed = parse("<ACT>emotion=开心 action=挥手:0.6</ACT>")
        self.assertEqual([x["name"] for x in parsed.items], ["happy", "wave"])
        self.assertAlmostEqual(parsed.items[1]["intensity"], 0.6)

    def test_unknown_names_are_stripped_and_reported(self):
        parsed = parse("<ACT>emotion=不存在, action=也不存在</ACT>你好")
        self.assertEqual(parsed.text, "你好")
        self.assertEqual(parsed.items, [])
        self.assertEqual(len(parsed.warnings), 2)

    def test_unclosed_tag_discards_to_end(self):
        parsed = parse("别念我<ACT>emotion=开心 这句话也不该出现")
        self.assertEqual(parsed.text, "别念我")
        self.assertTrue(parsed.warnings)

    def test_case_insensitive_and_chinese_separators(self):
        parsed = parse("<act>EMOTION=HAPPY；ACTION=Wave：0.5</act>")
        self.assertEqual(parsed.text, "")
        self.assertEqual(parsed.items[0]["name"], "happy")
        self.assertEqual(parsed.items[1]["name"], "wave")
        self.assertAlmostEqual(parsed.items[1]["intensity"], 0.5)

    def test_gaze_and_spaces(self):
        parsed = parse("<ACT> gaze=chat </ACT>")
        self.assertEqual(parsed.items, [{
            "kind": "gaze", "name": "chat", "intensity": 1.0}])
        self.assertIn("chat", GAZE_POINTS)

    def test_per_tag_limit_and_dedupe(self):
        parsed = parse(
            "<ACT>emotion=开心, action=挥手, action=点头, gaze=chat, "
            "action=歪头</ACT>")
        self.assertEqual(len(parsed.items), 4)
        self.assertIn("4 个意图", " ".join(parsed.warnings))

    def test_multi_page_cap_is_six(self):
        parsed = parse("甲<ACT>action=挥手</ACT>乙<ACT>action=点头</ACT>"
                       "丙<ACT>action=歪头</ACT>丁<ACT>action=鼓掌</ACT>"
                       "戊<ACT>action=踢腿</ACT>己<ACT>action=伸懒腰</ACT>"
                       "庚<ACT>action=比划</ACT>")
        self.assertEqual(len(parsed.items), 6)
        self.assertGreater(parsed.items[1].get("offset", 0.0), 0.0)

    def test_offsets_map_to_page_start(self):
        from sidekick.output.speaker import _page_acts
        parsed = parse("第一句。<ACT>action=挥手</ACT>"
                       "第二句，很长。<ACT>action=点头</ACT>第三句。")
        acts = _page_acts(parsed.items, ["第一句。", "第二句，很长。", "第三句。"])
        self.assertEqual([item["page"] for item in acts], [1, 2])

    def test_provider_tags_and_json_are_untouched(self):
        parsed = parse('别怕[whispering] {"a": 1}')
        self.assertEqual(parsed.text, '别怕[whispering] {"a": 1}')
        self.assertEqual(parsed.items, [])

    def test_multiple_tags_are_all_stripped(self):
        parsed = parse("甲<ACT>action=挥手</ACT>乙<ACT>gaze=left</ACT>丙")
        self.assertEqual(parsed.text, "甲乙丙")
        self.assertEqual([x["kind"] for x in parsed.items],
                         ["action", "gaze"])

    def test_raw_asset_names(self):
        self.assertEqual(
            raw_asset_names("<ACT>motion=自拍:0.5, expression=开心</ACT>"),
            [("motion", "自拍"), ("expression", "开心")])

    def test_profile_rules_cannot_deny_runtime_capabilities(self):
        prompt = {
            "emotion_tags": ["开心"],
            "action_tags": ["wave"],
            "gaze_tags": ["camera"],
            "rules": "模型没有尾巴，不能摇尾巴；语气自然一点。",
        }
        text = prompt_rules_from_profile(prompt, channels={"tail"})
        self.assertIn("尾巴：你可以自由摆动尾巴", text)
        self.assertNotIn("没有尾巴", text)
        self.assertIn("语气自然一点", text)

    def test_no_expression_rule_cannot_disable_procedural_emotion(self):
        prompt = {
            "emotion_tags": ["happy"],
            "action_tags": ["wave"],
            "gaze_tags": ["camera"],
            "rules": "没有可用的模型表情标签，不要输出 emotion 表情标签；用动作表达。",
        }
        text = prompt_rules_from_profile(prompt)
        self.assertIn("可用情绪", text)
        self.assertNotIn("不要输出 emotion", text)
        self.assertIn("用动作表达", text)

    def test_profile_usage_block_renders_lines(self):
        prompt = {
            "emotion_tags": ["happy"],
            "usage": ["第一行用法。", "第二行用法。"],
        }
        text = prompt_rules_from_profile(prompt)
        self.assertIn("怎么用这些标签：\n第一行用法。\n第二行用法。", text)

    def test_vocabulary_and_prompt(self):
        vocab = vocabulary()
        self.assertTrue(vocab["emotions"])
        self.assertTrue(vocab["actions"])
        prompt = prompt_rules(vocab)
        self.assertIn("<ACT>emotion=名字", prompt)
        self.assertIn("平时一次最多 1 个情绪加 1 个动作", prompt)


if __name__ == "__main__":
    unittest.main()
