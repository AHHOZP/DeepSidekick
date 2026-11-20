from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sidekick.avatar.actions import AvatarActions, summarize
from sidekick.core.null import NullAvatarActions


def live2d_avatar(root: Path | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id="whale", kind="live2d", name="whale", root=root, role_dir=root,
        capabilities={
            "actions": [{"tag": "自拍", "group": "selfie"},
                        {"tag": "挥手", "group": "wave"}],
            "expressions": [{"tag": "开心", "name": "开心兴奋"}],
            "mode": "split",
        })


class AvatarActionsTests(unittest.TestCase):
    def test_rules_come_from_avatar_file_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "actions.md").write_text("【动作】规则", encoding="utf-8")
            actions = AvatarActions(live2d_avatar(root))
            self.assertEqual(actions.rules(), "【动作】规则")
        self.assertEqual(AvatarActions(None).rules(), "")

    def test_parse_assets_and_semantics(self):
        actions = AvatarActions(live2d_avatar())
        parsed = actions.parse(
            "诶嘿<ACT>motion=自拍, expression=开心, emotion=害羞, "
            "action=挥手:0.8</ACT>")
        self.assertEqual(parsed.text, "诶嘿")
        self.assertEqual([a["kind"] for a in parsed.items],
                         ["motion", "expression", "emotion", "action"])
        self.assertEqual(parsed.items[0]["name"], "selfie")

    def test_actions_can_be_disabled_independently(self):
        actions = AvatarActions(live2d_avatar(), actions_enabled=False)
        self.assertEqual(actions.rules(), "")
        self.assertEqual(actions.prompt_rules(), "")
        parsed = actions.parse("你好<ACT>action=挥手</ACT>")
        self.assertEqual(parsed.text, "你好")
        self.assertEqual(parsed.items, [])

    def test_disabled_builtin_action_is_hidden_everywhere(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "motion_profile.json").write_text(json.dumps({
                "schema_version": 1,
                "actions": {"wave": {"enabled": False}},
            }), encoding="utf-8")
            actions = AvatarActions(live2d_avatar(root))
            names = [a["name"] for a in actions.vocabulary()["actions"]]
            self.assertNotIn("wave", names)
            parsed = actions.parse("<ACT>action=挥手</ACT>")
            self.assertEqual([], parsed.items)
            inferred = actions.parse("看我挥手")
            self.assertEqual([], inferred.items)

    def test_provider_square_tags_are_untouched(self):
        actions = AvatarActions(live2d_avatar())
        parsed = actions.parse("别怕[whispering]<ACT>action=挥手</ACT>")
        self.assertEqual(parsed.text, "别怕[whispering]")
        self.assertEqual(parsed.items[0]["name"], "wave")

    def test_no_avatar_strips_tags(self):
        actions = NullAvatarActions()
        self.assertEqual(actions.rules(), "")
        self.assertEqual(actions.broken, [])
        parsed = actions.parse("<ACT>emotion=开心</ACT>你好")
        self.assertEqual(parsed.text, "你好")
        self.assertEqual(parsed.items[0]["kind"], "emotion")
        self.assertEqual(parsed.items[0]["name"], "happy")
        self.assertEqual(summarize([{"kind": "action", "name": "wave"}]),
                         "action:wave")

    def test_broken_snapshot_follows_bound_avatar(self):
        avatar = live2d_avatar()
        avatar.broken = ["坏模型"]
        actions = AvatarActions(avatar)
        self.assertEqual(actions.broken, ["坏模型"])
        actions.bind(live2d_avatar())
        self.assertEqual(actions.broken, [])

    def test_spine_motion_names_are_assets(self):
        spine = SimpleNamespace(
            id="sp", kind="spine", root=None, role_dir=None,
            capabilities={"actions": [{"tag": "Attack", "group": "Attack"}],
                          "expressions": [], "idle": "idle", "lipSync": []})
        parsed = AvatarActions(spine).parse("<ACT>motion=attack</ACT>")
        self.assertEqual(parsed.text, "")
        self.assertEqual(parsed.items, [{
            "kind": "motion", "name": "Attack", "intensity": 1.0}])

    def test_vocabulary_and_prompt(self):
        actions = AvatarActions(live2d_avatar())
        vocab = actions.vocabulary()
        self.assertTrue(vocab["emotions"])
        self.assertIn("挥手", [x["label"] for x in vocab["actions"]])
        prompt = actions.prompt_rules()
        self.assertIn("<ACT>", prompt)
        self.assertIn("可用情绪", prompt)

    def test_profile_can_prefer_model_motion_for_action(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            avatar = live2d_avatar(root)
            from sidekick.avatar.autonomy import store

            store.save(avatar, {
                "schema_version": 1,
                "motion_policy": {"model_motions": {
                    "wave": {"enabled": True, "action": "wave"}}},
            })
            parsed = AvatarActions(avatar).parse(
                "<ACT>action=挥手</ACT>")
        self.assertEqual(parsed.items, [{
            "kind": "motion", "name": "wave", "intensity": 1.0}])

    def test_profile_disabled_motion_is_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            avatar = live2d_avatar(root)
            from sidekick.avatar.autonomy import store

            store.save(avatar, {
                "schema_version": 1,
                "motion_policy": {"model_motions": {
                    "wave": {"enabled": False}}},
            })
            parsed = AvatarActions(avatar).parse("<ACT>motion=wave</ACT>")
        self.assertEqual(parsed.items, [])
        self.assertTrue(any("禁用" in x for x in parsed.warnings))

    def test_profile_does_not_hot_reload_on_bind(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            avatar = live2d_avatar(root)
            from sidekick.avatar.autonomy import store

            store.save(avatar, {"schema_version": 1, "prompt": {
                "emotion_tags": ["happy"], "rules": "旧规则"}})
            actions = AvatarActions(avatar)
            store.save(avatar, {"schema_version": 1, "prompt": {
                "emotion_tags": ["sad"], "rules": "新规则"}})
            actions.bind(avatar)
        self.assertIn("旧规则", actions.prompt_rules())
        self.assertNotIn("新规则", actions.prompt_rules())

    def test_rules_only_profile_prompt_gets_generic_vocabulary(self):
        with tempfile.TemporaryDirectory() as td:
            avatar = live2d_avatar(Path(td))
            from sidekick.avatar.autonomy import store

            store.save(avatar, {"schema_version": 1, "prompt": {
                "emotion_tags": [], "action_tags": [], "gaze_tags": [],
                "rules": "只看语气"}})
            prompt = AvatarActions(avatar).prompt_rules()
        self.assertIn("可用情绪：", prompt)
        self.assertIn("可用动作：", prompt)
        self.assertIn("只看语气", prompt)

    def test_disabled_action_is_not_taught_by_profile_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            avatar = live2d_avatar(root)
            from sidekick.avatar.autonomy import store

            store.save(avatar, {
                "schema_version": 1,
                "actions": {"wave": {"enabled": False}},
                "prompt": {"action_tags": ["wave"], "rules": "保持"},
            })
            prompt = AvatarActions(avatar).prompt_rules()
        self.assertNotIn("挥手", prompt)
        self.assertNotIn("wave", prompt)

    def test_custom_alias_wins_over_builtin_alias(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            avatar = live2d_avatar(root)
            from sidekick.avatar.autonomy import store

            store.save(avatar, {
                "schema_version": 1,
                "actions": {"on": {"aliases": ["前倾"]}},
            })
            parsed = AvatarActions(avatar).parse("<ACT>action=前倾</ACT>")
        self.assertEqual(parsed.items[0]["name"], "on")

    def test_fractional_action_priority_is_not_truncated(self):
        from sidekick.motion.actions import spec_from_dict

        spec = spec_from_dict("wave", {
            "pose": {"head_x": 0.1}, "priority": 0.6,
            "duration": 1.0, "attack": 0.1, "hold": 0.5, "release": 0.4,
            "cooldown": 1.0,
        })
        self.assertEqual(spec.priority, 0.6)

    def test_profile_prompt_replaces_generic_vocabulary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            avatar = live2d_avatar(root)
            from sidekick.avatar.autonomy import store

            store.save(avatar, {
                "schema_version": 1,
                "prompt": {
                    "emotion_tags": ["happy", "shy"],
                    "action_tags": ["wave"],
                    "gaze_tags": ["chat"],
                    "rules": "只在句尾加，不确定就不加。",
                },
            })
            prompt = AvatarActions(avatar).prompt_rules()
        self.assertIn("可用情绪：开心、害羞", prompt)
        self.assertIn("可用动作：挥手", prompt)
        self.assertIn("可用视线：chat", prompt)
        self.assertIn("不确定就不加", prompt)


if __name__ == "__main__":
    unittest.main()
