from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from sidekick.motion import (
    Context, MotionEngine, NULL_MOTION_HOST, Pose, ProsodyTracker, SpeechFrame,
    compose, compensate_pose, default_springs, introspect, to_parameters,
)
from sidekick.motion.actions import ActionLibrary, choreo_envelope
from sidekick.motion.emotion import EmotionState
from sidekick.motion.introspect import read_display_channels, read_display_states
from sidekick.motion.posture import PostureAnchor


class FakeSource:
    def __init__(self, data):
        self.data = data

    def param_ids(self):
        return list(self.data)

    def param_info(self, param_id):
        return self.data[param_id]


PHYSICS = {
    "ParamAngleX": (-30, 30, 0),
    "AngleX": (-30, 30, 0),
    "AngleXbs": (-30, 30, 0),
    "ParamAngleY": (-30, 30, 0),
    "ParamAngleZ": (-30, 30, 0),
    "ParamEyeLOpen": (0, 1, 1), "ParamEyeROpen": (0, 1, 1),
    "ParamEyeBallX": (-1, 1, 0), "ParamEyeBallY": (-1, 1, 0),
    "ParamMouthOpenY": (0, 1, 0), "ParamMouthForm": (-1, 1, 0),
    "ParamBodyAngleZ": (-30, 30, 0),
    "ParamBodyAngleZ1": (-30, 30, 0),
    "ParamHairFront": (-1, 1, 0),
}
PHYSICS_OUT = {
    "ParamAngleX", "AngleXbs", "ParamAngleY", "ParamAngleZ",
    "ParamBodyAngleZ", "ParamHairFront",
}


class DisplayChannelTests(unittest.TestCase):
    def test_generic_ids_use_cdi3_names_and_side_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cdi = root / "model.cdi3.json"
            cdi.write_text(json.dumps({"Parameters": [
                {"Id": "Param36", "Name": "R大臂z"},
                {"Id": "Param39", "Name": "大臂透视"},
                {"Id": "Param41", "Name": "R小臂 Z"},
                {"Id": "Param42", "Name": "R手腕 Z"},
                {"Id": "Param43", "Name": "R 食指"},
                {"Id": "Param62", "Name": "L 大臂z"},
                {"Id": "Param59", "Name": "大臂透视"},
                {"Id": "Param60", "Name": "L小臂 Z"},
                {"Id": "Param61", "Name": "L手腕 Z"},
                {"Id": "ChestX1", "Name": "胸X1"},
                {"Id": "SkirtX1", "Name": "裙X1"},
                {"Id": "Param_Angle_Rotation84", "Name": "[0]尾巴"},
                {"Id": "ParamSleeveLY", "Name": "左袖子Y1"},
                {"Id": "ParamChest", "Name": "21.胸部X1"},
                {"Id": "ParamSkirt", "Name": "1.裙子X1"},
                {"Id": "ParamTailOff", "Name": "1.尾巴消失"},
                {"Id": "Movement3", "Name": "3. 竖食指"},
                {"Id": "Clothes5", "Name": "5. 尾巴"},
            ]}, ensure_ascii=False), encoding="utf-8")
            (root / "model.model3.json").write_text(json.dumps({
                "FileReferences": {"DisplayInfo": cdi.name},
            }), encoding="utf-8")
            channels = read_display_channels(root / "model.model3.json")
            states = read_display_states(root / "model.model3.json")
        self.assertEqual(channels["Param36"], "arm_r")
        self.assertEqual(channels["Param39"], "arm_r_persp")
        self.assertEqual(channels["Param59"], "arm_l_persp")
        self.assertEqual(channels["Param62"], "arm_l")
        self.assertEqual(channels["Param43"], "finger_r")
        self.assertEqual(channels["ChestX1"], "chest_x")
        self.assertEqual(channels["SkirtX1"], "skirt_x")
        self.assertEqual(channels["Param_Angle_Rotation84"], "tail")
        self.assertEqual(channels["ParamSleeveLY"], "sleeve_l_y")
        self.assertEqual(channels["ParamChest"], "chest_x")
        self.assertEqual(channels["ParamSkirt"], "skirt_x")
        self.assertEqual(channels["Movement3"], "hand_style_point")
        self.assertNotIn("ParamTailOff", channels)
        self.assertNotIn("Clothes5", channels)
        self.assertIn("tail_outfit", states)
        self.assertNotEqual(states["tail_outfit"]["param_id"],
                            "Param_Angle_Rotation84")

    def test_display_channel_beats_numeric_fallback(self):
        profile = introspect(
            FakeSource({"Param36": (-1.0, 1.0, 0.0),
                        "ParamAngleX": (-30.0, 30.0, 0.0)}),
            display_channels={"Param36": "arm_r"})
        self.assertEqual(profile.by_channel["arm_r"].id, "Param36")
        self.assertEqual(profile.by_channel["arm_r"].score, 130)


class PhysicsBindingTests(unittest.TestCase):
    def setUp(self):
        self.profile = introspect(
            FakeSource(PHYSICS), physics_outputs=PHYSICS_OUT)

    def test_null_host_has_the_same_enabled_contract(self):
        self.assertFalse(NULL_MOTION_HOST.enabled)
        self.assertFalse(NULL_MOTION_HOST.report()["enabled"])

    def test_hair_physics_output_is_never_bound(self):
        self.assertNotIn("hair_front", self.profile.by_channel)

    def test_core_angle_uses_physics_output_but_follows_alt(self):
        self.assertEqual(self.profile.by_channel["head_x"].id, "ParamAngleX")
        self.assertIn(("AngleX", 0.85), self.profile.followers["head_x"])
        self.assertNotIn("AngleXbs", [pid for pid, _ in
                                      self.profile.followers["head_x"]])

    def test_body_coupled_parameters_follow(self):
        self.assertEqual(self.profile.by_channel["body_z"].id, "ParamBodyAngleZ")
        self.assertIn("ParamBodyAngleZ1",
                      [pid for pid, _ in self.profile.followers["body_z"]])

    def test_followers_are_written_with_their_own_range(self):
        pose = Pose({"head_x": 0.5, "body_z": 0.4, "mouth_open": 0.0})
        params = to_parameters(self.profile, pose)
        self.assertAlmostEqual(params["ParamAngleX"], 15.0)
        self.assertAlmostEqual(params["AngleX"], 12.75)


class PostureTests(unittest.TestCase):
    def test_anchor_switches_and_contrapposto(self):
        anchor = PostureAnchor(seed=3)
        seen = {anchor.state}
        opposite = False
        for i in range(900):
            pose = anchor.update(1 / 60, Context(now=i / 60))
            seen.add(anchor.state)
            if pose.get("head_z") * pose.get("body_z") < -0.001:
                opposite = True
        self.assertGreaterEqual(len(seen), 3)
        self.assertTrue(opposite)

    def test_underdamped_cascade_is_ordered(self):
        bank = default_springs()
        self.assertAlmostEqual(bank.springs["head_x"].damping, 0.60)
        self.assertLess(bank.springs["arm_r"].damping, 0.62)
        self.assertGreater(bank.springs["eye_x"].frequency,
                           bank.springs["head_x"].frequency)
        self.assertGreater(bank.springs["head_x"].frequency,
                           bank.springs["body_x"].frequency)


class ProsodyTests(unittest.TestCase):
    def test_phrase_envelope_moves_head_and_body(self):
        tracker = ProsodyTracker()
        lo = {"head_x": 9.0, "head_z": 9.0, "body_x": 9.0, "body_z": 9.0}
        hi = {key: -9.0 for key in lo}
        for i in range(180):
            now = i / 60
            openness = 0.35 + 0.45 * abs(math.sin(now * 9.0))
            state = tracker.update(
                SpeechFrame(now, level=openness, openness=openness, voiced=True),
                1 / 60, speaking=True)
            pose = state.pose()
            for key in lo:
                lo[key] = min(lo[key], pose.get(key))
                hi[key] = max(hi[key], pose.get(key))
        self.assertGreater(hi["head_x"] - lo["head_x"], 0.35)
        self.assertGreater(hi["head_z"] - lo["head_z"], 0.35)
        self.assertGreater(hi["body_x"] - lo["body_x"], 0.10)
        self.assertGreater(hi["body_z"] - lo["body_z"], 0.10)

    def test_engine_moves_head_and_torso_during_speech(self):
        profile = introspect(FakeSource(PHYSICS), physics_outputs=PHYSICS_OUT)
        engine = MotionEngine(profile, seed=5)
        lo = {"head_x": 9.0, "head_z": 9.0, "body_z": 9.0}
        hi = {key: -9.0 for key in lo}
        for i in range(180):
            now = i / 60
            openness = 0.35 + 0.45 * abs(math.sin(now * 9.0))
            frame = engine.tick(
                now, 1 / 60,
                speech=SpeechFrame(now, level=openness,
                                   openness=openness, voiced=True),
                context=Context(speaking=True))
            for key in lo:
                lo[key] = min(lo[key], frame.pose.get(key))
                hi[key] = max(hi[key], frame.pose.get(key))
        self.assertGreater(hi["head_x"] - lo["head_x"], 0.40)
        self.assertGreater(hi["head_z"] - lo["head_z"], 0.35)
        self.assertGreater(hi["body_z"] - lo["body_z"], 0.15)

    def test_expression_reaches_engine_body(self):
        profile = introspect(FakeSource(PHYSICS), physics_outputs=PHYSICS_OUT)
        emotion = EmotionState()
        emotion.set("excited", 1.0)
        emotion.update(0.2)
        pose = compose(
            profile, emotion, Pose(), Pose(), Pose(), Pose(), Pose(),
            breath=0.5, blink_l=1.0, blink_r=1.0, speech=None, speaking=False)
        self.assertGreater(pose.get("arm_r"), 0.0)


class ActionChoreoTests(unittest.TestCase):
    def test_anticipation_moves_opposite_first(self):
        self.assertLess(choreo_envelope(0.04, 0.24, 1.25, 0.45), 0.0)
        self.assertGreater(choreo_envelope(0.18, 0.24, 1.25, 0.45), 0.0)

    def test_whip_phases_lag_shoulder_to_hand(self):
        spec = ActionLibrary().resolve("wave")
        self.assertGreater(spec.loops["arm_l"][2], spec.loops["arm_l_bend"][2])
        self.assertGreater(spec.loops["arm_l_bend"][2], spec.loops["hand_l"][2])
        self.assertGreater(spec.loops["shoulder_l"][2], spec.loops["arm_l"][2])

    def test_missing_arms_degrade_to_visible_body_swing(self):
        profile = introspect(FakeSource({
            "ParamAngleX": (-30, 30, 0), "ParamAngleY": (-30, 30, 0),
            "ParamAngleZ": (-30, 30, 0),
            "ParamEyeLOpen": (0, 1, 1), "ParamEyeROpen": (0, 1, 1),
            "ParamMouthOpenY": (0, 1, 0), "ParamMouthForm": (-1, 1, 0),
        }))
        pose = compensate_pose({"arm_r": 0.8, "head_z": 0.1}, profile)
        self.assertGreater(pose.get("head_z"), 0.1)
        self.assertLessEqual(abs(pose.get("head_z")), 1.0)


if __name__ == "__main__":
    unittest.main()
