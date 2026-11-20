from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sidekick.avatar import act_tags
from sidekick.avatar.autonomy import validate
from sidekick.motion import (
    ActionLibrary, Context, Intent, MotionEngine, Pose, StateSpec,
    StateController, introspect, read_display_channels, read_display_states,
)
from sidekick.motion.compositor import apply_silhouette_guard
from sidekick.motion.profile import apply_overrides
from types import SimpleNamespace


class FakeSource:
    def __init__(self, data):
        self.data = data

    def param_ids(self):
        return list(self.data)

    def param_info(self, param_id):
        return self.data[param_id]


BASE = {
    "ParamAngleX": (-30, 30, 0), "ParamAngleY": (-30, 30, 0),
    "ParamAngleZ": (-30, 30, 0),
    "ParamEyeLOpen": (0, 1, 1), "ParamEyeROpen": (1, 1, 1),
    "ParamMouthOpenY": (0, 1, 0), "ParamMouthForm": (-1, 1, 0),
    "ParamBodyAngleX": (-10, 10, 0), "ParamBodyAngleY": (-10, 10, 0),
    "ParamBodyAngleZ": (-10, 10, 0), "ParamHipAngleX": (-30, 30, 0),
    "ParamArmRA": (0, 1, 0), "ParamArmRB": (0, 1, 0),
    "ParamArmLA": (0, 1, 0), "ParamArmLB": (0, 1, 0),
    "ParamHandR": (-1, 1, 0), "ParamHandL": (-1, 1, 0),
    "Param50": (0, 1, 0), "Param53": (0, 1, 0), "Param54": (0, 1, 0),
    "LeanInput": (-30, 30, 0),
    "Param_Angle_Rotation84": (-45, 45, 0),
    "Param_Angle_Rotation85": (-45, 45, 0),
    "Param_Angle_Rotation86": (-45, 45, 0),
    "Param_Angle_Rotation87": (-45, 45, 0),
    "Param_Angle_Rotation88": (-45, 45, 0),
    "Param_Angle_Rotation89": (-45, 45, 0),
    "Clothes1": (0, 1, 1), "Movement2": (0, 1, 0),
}


class V4DisplayTests(unittest.TestCase):
    def _model3(self, td: str) -> Path:
        root = Path(td)
        cdi = root / "model.cdi3.json"
        cdi.write_text(json.dumps({"Parameters": [
            {"Id": "Param50", "Name": "Lean Y"},
            {"Id": "Param53", "Name": "Lean Y 2"},
            {"Id": "Param54", "Name": "Lean Y 3"},
            {"Id": "LeanInput", "Name": "Lean Input"},
            {"Id": "Param_Angle_Rotation84", "Name": "[0]尾巴"},
            {"Id": "Param_Angle_Rotation85", "Name": "[1]尾巴"},
            {"Id": "Param_Angle_Rotation86", "Name": "[2]尾巴"},
            {"Id": "Param_Angle_Rotation87", "Name": "[3]尾巴"},
            {"Id": "Param_Angle_Rotation88", "Name": "[4]尾巴"},
            {"Id": "Param_Angle_Rotation89", "Name": "[5]尾巴"},
            {"Id": "Clothes1", "Name": "1. 长短裙切换"},
            {"Id": "Movement2", "Name": "2. 抚胸"},
        ]}, ensure_ascii=False), encoding="utf-8")
        model3 = root / "model.model3.json"
        model3.write_text(json.dumps({"FileReferences": {
            "DisplayInfo": cdi.name}}), encoding="utf-8")
        return model3

    def test_lean_and_chain_followers(self):
        with tempfile.TemporaryDirectory() as td:
            model3 = self._model3(td)
            channels = read_display_channels(model3)
            states = read_display_states(model3)
        self.assertEqual(channels["Param50"], "lean_y")
        self.assertEqual(channels["Param53"], "lean_y")
        self.assertEqual(channels["Param54"], "lean_y")
        self.assertEqual(channels["LeanInput"], "lean_y")
        self.assertEqual(channels["Param_Angle_Rotation84"], "tail")
        self.assertIn("long_skirt", states)
        self.assertIn("hand_chest", states)
        profile = introspect(
            FakeSource(BASE), display_channels=channels,
            physics_outputs={"Param50", "Param53", "Param54",
                             "Param_Angle_Rotation84", "Param_Angle_Rotation89"},
            states=states)
        self.assertEqual(profile.by_channel["lean_y"].id, "Param50")
        self.assertEqual([pid for pid, _ in profile.followers["lean_y"]],
                         ["Param53", "Param54", "LeanInput"])
        self.assertEqual(profile.by_channel["tail"].id, "Param_Angle_Rotation84")
        self.assertEqual(len(profile.followers["tail"]), 5)
        self.assertEqual(profile.states["long_skirt"].active_value, 0.0)
        self.assertEqual(profile.states["long_skirt"].inactive_value, 1.0)
        self.assertTrue(profile.capabilities["can_lean_y"])
        self.assertTrue(profile.capabilities["can_tail"])

    def test_body_detail_parameters_feed_hip_chest_skirt_tail(self):
        data = {
            **BASE,
            "ParamBodyBreechX": (-10, 10, 0),
            "ParamLegR1Z": (-30, 30, 0),
            "ParamLegR1X": (-30, 30, 0),
            "ParamSkirtL1": (-1, 1, 0),
            "ParamBreastX": (-1, 1, 0),
            "ParamTailX1": (-1, 1, 0),
            "ParamHairBackAL0": (-1, 1, 0),
            "ParamHairBackAL1": (-1, 1, 0),
        }
        data.pop("ParamHipAngleX", None)
        profile = introspect(
            FakeSource(data),
            physics_outputs={"ParamSkirtL1", "ParamBreastX", "ParamTailX1"},
        )
        self.assertEqual(profile.by_channel["hip"].id, "ParamBodyBreechX")
        self.assertEqual(profile.by_channel["skirt_x"].id, "ParamSkirtL1")
        self.assertEqual(profile.by_channel["chest_x"].id, "ParamBreastX")
        self.assertEqual(profile.by_channel["tail"].id, "ParamTailX1")
        self.assertEqual(profile.by_channel["hair_back"].id, "ParamHairBackAL0")
        self.assertIn(("ParamHairBackAL1", 0.92),
                      profile.followers["hair_back"])
        self.assertEqual(profile.by_channel["leg_r_x"].id, "ParamLegR1X")


class V4ActionTests(unittest.TestCase):
    def setUp(self):
        channels = {
            "Param50": "lean_y", "Param53": "lean_y", "Param54": "lean_y",
            "LeanInput": "lean_y", "Movement2": "hand_style_chest",
        }
        channels.update({
            f"Param_Angle_Rotation{index}": "tail"
            for index in range(84, 90)
        })
        self.profile = introspect(FakeSource(BASE), display_channels=channels)

    def test_new_actions_use_real_capabilities(self):
        library = ActionLibrary()
        hand = library.resolve_for(library.resolve("hand_chest"), self.profile)
        self.assertGreater(hand.pose["arm_l"], 0.5)
        tail = library.resolve_for(library.resolve("wag_tail"), self.profile)
        self.assertGreater(tail.pose["tail"], 0.0)
        sway = library.resolve_for(library.resolve("sway_hip"), self.profile)
        self.assertGreater(sway.pose["hip"], 0.0)
        bow = library.resolve_for(library.resolve("bow"), self.profile)
        self.assertGreater(bow.pose["lean_y"], 0.5)

    def test_leg_shift_uses_both_legs_and_feet(self):
        data = {
            **BASE,
            "ParamLegL1Z": (-30, 30, 0), "ParamLegL1Y": (-30, 30, 0),
            "ParamLegL2Z": (-30, 30, 0), "ParamLegL3Z": (-30, 30, 0),
            "ParamLegL2Y": (-30, 30, 0), "ParamLegL3Y": (-30, 30, 0),
            "ParamLegR1Z": (-30, 30, 0), "ParamLegR1Y": (-30, 30, 0),
            "ParamLegR2Z": (-30, 30, 0), "ParamLegR3Z": (-30, 30, 0),
            "ParamLegR2Y": (-30, 30, 0), "ParamLegR3Y": (-30, 30, 0),
            "ParamFootLX": (-30, 30, 0), "ParamFootRX": (-30, 30, 0),
            "ParamLegLF": (-1, 1, 0),
            "ParamBodyBreechX": (-10, 10, 0),
        }
        profile = introspect(FakeSource(data))
        self.assertTrue(profile.supports("leg_l"))
        self.assertTrue(profile.supports("knee_r"))
        self.assertTrue(profile.supports("ankle_r"))
        self.assertTrue(profile.supports("foot_swing"))
        self.assertTrue(profile.supports("hip"))
        spec = ActionLibrary().resolve("leg_shift")
        resolved = ActionLibrary().resolve_for(spec, profile)
        self.assertGreater(resolved.pose["leg_l"], 0.0)
        self.assertLess(resolved.pose["leg_r"], 0.0)
        self.assertGreater(resolved.pose["foot_l"], 0.0)
        self.assertGreater(resolved.pose["knee_l"], 0.0)
        self.assertGreater(resolved.pose["ankle_r"], 0.0)
        self.assertGreater(resolved.pose["foot_swing"], 0.0)
        self.assertIn("leg_shift", MotionEngine(profile, seed=4)._idle_actions)

    def test_compose_keeps_legs_and_feet_alive_at_idle(self):
        data = {
            **BASE,
            "ParamLegL1Z": (-30, 30, 0), "ParamLegR1Z": (-30, 30, 0),
            "ParamLegR2Z": (-30, 30, 0), "ParamLegR3Z": (-30, 30, 0),
            "ParamFootLX": (-30, 30, 0), "ParamFootRX": (-30, 30, 0),
            "ParamLegLF": (-1, 1, 0),
            "ParamBodyBreechX": (-10, 10, 0),
        }
        profile = introspect(FakeSource(data))
        engine = MotionEngine(profile, seed=4)
        frame = None
        for index in range(60):
            frame = engine.tick(1.2 + index / 60, 1 / 60,
                                context=Context())
        self.assertIsNotNone(frame)
        self.assertGreater(abs(frame.pose.get("leg_l")), 0.005)
        self.assertGreater(abs(frame.pose.get("foot_l")), 0.001)
        self.assertGreater(abs(frame.pose.get("knee_r")), 0.001)
        self.assertGreater(abs(frame.pose.get("ankle_r")), 0.001)
        self.assertGreater(abs(frame.pose.get("foot_swing")), 0.001)
        self.assertGreater(abs(frame.pose.get("hip")), 0.001)

    def test_capability_card_mentions_legs_and_chest(self):
        lines = act_tags.capability_lines(
            {"leg_r", "foot_r", "chest_x", "hip"})
        text = "\n".join(lines)
        self.assertIn("腿部", text)
        self.assertIn("胸部", text)
        self.assertIn("扭腰", text)

    def test_stretch_bends_elbows_and_does_not_look_like_tpose(self):
        spec = ActionLibrary().resolve("stretch")
        self.assertGreater(spec.pose["arm_l_bend"], 0.0)
        self.assertGreater(spec.pose["arm_r_bend"], 0.0)

    def test_silhouette_guard_bends_negative_forearm(self):
        pose = Pose({"arm_r": 0.8, "arm_r_bend": -0.2,
                     "arm_l": 0.1, "arm_l_bend": 0.0})
        apply_silhouette_guard(pose)
        self.assertGreaterEqual(pose.get("arm_r_bend"), 0.28 * 0.8 - 1e-6)

    def test_lean_reaches_the_three_leany_params(self):
        engine = MotionEngine(self.profile, seed=6)
        engine.submit(Intent("action", "lean", 1.0, source="test"))
        peak = {"Param50": 0.0, "Param53": 0.0, "Param54": 0.0}
        for i in range(75):
            frame = engine.tick(i / 60, 1 / 60, context=Context())
            for pid in peak:
                peak[pid] = max(peak[pid], frame.params.get(pid, 0.0))
        self.assertGreaterEqual(min(peak.values()), 0.75)

    def test_hand_chest_activates_texture_and_bend(self):
        engine = MotionEngine(self.profile, seed=7)
        engine.submit(Intent("action", "hand_chest", 1.0, source="test"))
        peak = {"Movement2": 0.0, "ParamArmLB": 0.0}
        for i in range(90):
            frame = engine.tick(i / 60, 1 / 60, context=Context())
            for pid in peak:
                peak[pid] = max(peak[pid], frame.params.get(pid, 0.0))
        self.assertGreaterEqual(peak["Movement2"], 0.75)
        self.assertGreaterEqual(peak["ParamArmLB"], 0.5)

    def test_wag_tail_writes_all_six_segments(self):
        engine = MotionEngine(self.profile, seed=4)
        engine.submit(Intent("action", "wag_tail", 1.0, source="test"))
        peak = {}
        for i in range(75):
            frame = engine.tick(i / 60, 1 / 60, context=Context())
            for pid in (f"Param_Angle_Rotation{index}" for index in range(84, 90)):
                peak[pid] = max(peak.get(pid, 0.0), abs(frame.params.get(pid, 0.0)))
        self.assertTrue(peak)
        self.assertTrue(all(value > 5.0 for value in peak.values()), peak)

    def test_custom_action_from_profile_is_playable(self):
        data = {
            "schema_version": 1,
            "actions": {
                "custom_wave": {
                    "aliases": ["自定义挥手"],
                    "pose": {"arm_r": 0.6},
                    "duration": 1.0,
                },
            },
        }
        apply_overrides(self.profile, data)
        self.assertIn("custom_wave", self.profile.custom_actions)
        engine = MotionEngine(self.profile, seed=5)
        self.assertTrue(engine.actions.play("custom_wave", self.profile, now=0.0))
        engine.tick(0.0, 0.0, context=Context())
        frame = engine.tick(0.3, 0.3, context=Context())
        self.assertGreater(frame.pose.get("arm_r"), 0.1)


class V4LegTests(unittest.TestCase):
    def test_leg_chains_and_foot_are_bound(self):
        data = {
            "ParamLegL1Z": (-30, 30, 0), "ParamLegL2Z": (-30, 30, 0),
            "ParamLegL3Z": (-30, 30, 0),
            "ParamLegR1Z": (-30, 30, 0), "ParamLegR2Z": (-30, 30, 0),
            "ParamLegR3Z": (-30, 30, 0),
            "ParamLegR1X": (-30, 30, 0),
            "ParamLegR1Y": (-30, 30, 0), "ParamLegR2Y": (-30, 30, 0),
            "ParamLegR3Y": (-30, 30, 0),
            "ParamFootRX": (-30, 30, 0), "ParamFootLX": (-30, 30, 0),
            "ParamLegLF": (-1, 1, 0),
        }
        profile = introspect(FakeSource(data))
        self.assertEqual(profile.by_channel["leg_r"].id, "ParamLegR1Z")
        self.assertEqual(profile.by_channel["knee_r"].id, "ParamLegR2Z")
        self.assertEqual(profile.by_channel["ankle_r"].id, "ParamLegR3Z")
        self.assertEqual(profile.by_channel["leg_r_x"].id, "ParamLegR1X")
        self.assertEqual(profile.by_channel["leg_r_y"].id, "ParamLegR1Y")
        self.assertEqual(profile.by_channel["knee_r_y"].id, "ParamLegR2Y")
        self.assertEqual(profile.by_channel["foot_r_y"].id, "ParamLegR3Y")
        self.assertEqual(profile.by_channel["foot_r"].id, "ParamFootRX")
        self.assertEqual(profile.by_channel["foot_swing"].id, "ParamLegLF")
        self.assertNotIn("leg_r", profile.followers)
        kick = ActionLibrary().resolve_for(
            ActionLibrary().resolve("kick"), profile)
        self.assertGreater(kick.pose["leg_r"], 0.5)
        self.assertGreater(kick.pose["knee_r"], 0.3)
        self.assertGreater(kick.pose["ankle_r"], 0.2)
        self.assertGreater(kick.pose["leg_r_y"], 0.2)
        self.assertGreater(kick.pose["foot_r_y"], 0.1)
        self.assertGreater(kick.pose["foot_r"], 0.2)


class V4BodyInputTests(unittest.TestCase):
    def test_split_physics_body_inputs_follow_body_not_head(self):
        data = {
            "ParamAngleX": (-30, 30, 0), "ParamAngleY": (-30, 30, 0),
            "ParamAngleZ": (-30, 30, 0),
            "ParamAngleX4": (-30, 30, 0), "ParamAngleX5": (-30, 30, 0),
            "ParamAngleY4": (-30, 30, 0), "ParamAngleY5": (-30, 30, 0),
            "ParamAngleZ4": (-30, 30, 0), "ParamAngleZ5": (-30, 30, 0),
            "ParamBodyAngleX": (-30, 30, 0),
            "ParamBodyAngleXX": (-30, 30, 0),
            "ParamBodyAngleY": (-30, 30, 0),
            "ParamBodyAngleZ": (-30, 30, 0),
        }
        profile = introspect(FakeSource(data))
        self.assertEqual(
            [pid for pid, _ in profile.followers["body_x"]],
            ["ParamAngleX4", "ParamAngleX5", "ParamBodyAngleXX"])
        self.assertNotIn("ParamAngleX4",
                         [pid for pid, _ in profile.followers.get("head_x", [])])
        self.assertIn("ParamAngleY4", [pid for pid, _ in profile.followers["body_y"]])
        self.assertIn("ParamAngleZ5", [pid for pid, _ in profile.followers["body_z"]])


class V4StateTests(unittest.TestCase):
    def test_persistent_skirt_and_timed_hand_style(self):
        specs = {
            "long_skirt": StateSpec(
                "long_skirt", "Clothes1", aliases=("长裙",),
                active_value=0.0, inactive_value=1.0, mode="persistent"),
            "hand_chest": StateSpec(
                "hand_chest", "Movement2", aliases=("手放胸口",),
                active_value=1.0, mode="timed", hold_seconds=0.2,
                mutex_group="hand"),
        }
        controller = StateController(specs)
        self.assertTrue(controller.request("长裙"))
        controller.update(0.1)
        self.assertAlmostEqual(controller.parameters()["Clothes1"], 0.0)
        self.assertTrue(controller.request("手放胸口"))
        controller.update(0.1)
        self.assertEqual(controller.parameters()["Movement2"], 1.0)
        controller.update(0.2)
        self.assertIn("Movement2", controller.parameters())
        controller.update(0.2)
        self.assertNotIn("Movement2", controller.parameters())
        controller.update(0.1)
        self.assertAlmostEqual(controller.parameters()["Clothes1"], 0.0)
        controller.request("long_skirt", False)
        controller.update(0.1)
        self.assertAlmostEqual(controller.parameters()["Clothes1"], 1.0)

    def test_normal_body_action_is_a_persistent_reverse_state(self):
        library = ActionLibrary()
        spec = library.resolve("正常比例")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.states, {"chibi_mode": False})

    def test_deep_bend_comfort_guard_reduces_upper_arm(self):
        from sidekick.motion.compositor import apply_arm_comfort_guard

        pose = Pose({"arm_l": 0.9, "arm_l_bend": 1.1,
                     "hand_l": 0.6})
        apply_arm_comfort_guard(pose)
        self.assertLessEqual(pose.get("arm_l"), 0.46)
        self.assertLessEqual(pose.get("hand_l"), 0.37)


class V4LanguageTests(unittest.TestCase):
    def test_fuzzy_synonyms_are_not_dropped(self):
        parsed = act_tags.parse(
            "<ACT>action=看旁边</ACT><ACT>action=晃晃尾巴</ACT>"
            "<ACT>action=扭扭腰</ACT><ACT>action=向前倾</ACT>")
        self.assertEqual([item["name"] for item in parsed.items],
                         ["look_away", "wag_tail", "sway_hip", "lean"])
        self.assertEqual(parsed.warnings, [])

    def test_text_fallback_skips_negation_and_description(self):
        self.assertIsNone(act_tags.infer_action("我没有尾巴"))
        self.assertIsNone(act_tags.infer_action("我有一条毛茸茸的尾巴"))
        self.assertEqual(act_tags.infer_action("好呀，看我摇尾巴～"), "wag_tail")

    def test_capability_card_only_lists_bound_parts(self):
        text = act_tags.prompt_rules(
            channels={"tail", "lean_y"}, states={"long_skirt"})
        self.assertIn("尾巴", text)
        self.assertIn("前倾", text)
        self.assertIn("长裙", text)
        self.assertNotIn("Q 版", text)


class V4AvatarTests(unittest.TestCase):
    def test_capability_card_and_custom_alias_from_profile(self):
        from sidekick.avatar.actions import AvatarActions
        from sidekick.avatar.autonomy import store

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            avatar = SimpleNamespace(
                id="deepseek", kind="live2d", name="deepseek", root=root,
                role_dir=root, capabilities={})
            store.save(avatar, {
                "schema_version": 1,
                "channel_bindings": {"tail": "Param_Angle_Rotation84",
                                     "lean_y": "Param50"},
                "states": {"long_skirt": {"param_id": "Clothes1"}},
                "actions": {
                    "custom_wave": {
                        "aliases": ["自定义挥手"],
                        "pose": {"arm_r": 0.6},
                    },
                },
                "prompt": {"rules": "只在句尾加"},
            })
            actions = AvatarActions(avatar)
            prompt = actions.prompt_rules()
            parsed = actions.parse("<ACT>action=自定义挥手</ACT>")
        self.assertIn("尾巴", prompt)
        self.assertIn("前倾", prompt)
        self.assertIn("长裙", prompt)
        self.assertEqual(parsed.items[0]["name"], "custom_wave")


class V4ValidationTests(unittest.TestCase):
    def test_custom_action_survives_profile_validation(self):
        from sidekick.avatar.autonomy.fallback import fallback_profile

        dossier = {"params": [{"id": "ParamArmRA"}],
                   "by_channel": {}, "states": {}, "tier": "face"}
        prof = fallback_profile(dossier)
        prof["actions"]["custom_wave"] = {
            "aliases": ["自定义挥手"],
            "pose": {"arm_r": 0.8},
            "duration": 1.2,
        }
        clean, issues = validate.validate(prof, dossier)
        self.assertIn("custom_wave", clean["actions"])
        self.assertFalse([issue for issue in issues if issue.fatal])

    def test_act_at_end_of_text_does_not_raise(self):
        from sidekick.output.speaker import _page_acts

        parsed = act_tags.parse("你好。<ACT>action=挥手</ACT>")
        self.assertEqual(_page_acts(parsed.items, ["你好。"]), [
            {"kind": "action", "name": "wave", "intensity": 1.0,
             "offset": 1.0, "page": 0},
        ])
        self.assertEqual(_page_acts(parsed.items, []), [])


if __name__ == "__main__":
    unittest.main()
