from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from sidekick.motion.introspect import (
    canonical, compensate_pose, introspect, match_channel,
    physics_additive_specs, read_display_channels,
)
from sidekick.motion.types import ModelProfile, ParamSpec


class FakeSource:
    def __init__(self, data):
        self.data = data

    def param_ids(self):
        return list(self.data)

    def param_info(self, param_id):
        return self.data[param_id]


FULL = {
    "ParamAngleX": (-30, 30, 0), "ParamAngleY": (-30, 30, 0),
    "ParamAngleZ": (-30, 30, 0),
    "ParamEyeLOpen": (0, 1, 1), "ParamEyeROpen": (0, 1, 1),
    "ParamEyeBallX": (-1, 1, 0), "ParamEyeBallY": (-1, 1, 0),
    "ParamMouthOpenY": (0, 1, 0), "ParamMouthForm": (-1, 1, 0),
    "ParamBodyAngleX": (-10, 10, 0), "ParamBodyAngleZ": (-10, 10, 0),
    "ParamBreath": (0, 1, 0),
    "ParamArmRA": (0, 1, 0), "ParamArmRB": (0, 1, 0),
    "ParamArmLA": (0, 1, 0), "ParamArmLB": (0, 1, 0),
    "ParamHandR": (-1, 1, 0), "ParamHandL": (-1, 1, 0),
    "ParamLegR": (-1, 1, 0), "ParamLegL": (-1, 1, 0),
    "ParamBrowLY": (-1, 1, 0), "ParamCheek": (0, 1, 0),
}

FACE = {
    "ParamAngleX": (-30, 30, 0), "ParamAngleY": (-30, 30, 0),
    "ParamAngleZ": (-30, 30, 0),
    "ParamEyeLOpen": (0, 1, 1), "ParamEyeROpen": (0, 1, 1),
    "ParamMouthOpenY": (0, 1, 0), "ParamMouthForm": (-1, 1, 0),
}

WEIRD = {
    "ParamBodyAngleZ1": (-10, 10, 0),
    "ParamBodyAngleZ2": (-10, 10, 0),
    "Param12": (-30, 30, 0),
    "Paramcol_1": (0, 1, 0),
    "ParamHF_01": (-30, 30, 0),
}

DUPLICATE = {
    "ParamAngleX": (-30, 30, 0),
    "AngleX": (-30, 30, 0),
    "AngleXbs": (-30, 30, 0),
}

CUSTOM_LIMBS = {
    **FACE,
    "ParamBodyAngleX": (-10, 10, 0),
    "LArm": (-1, 1, 0), "RArm": (-1, 1, 0),
    "LForearm": (-1, 1, 0), "RForearm": (-1, 1, 0),
    "LHand": (-1, 1, 0), "RHand": (-1, 1, 0),
    "LEGAngleZ2": (-30, 30, 0),
}


class PhysicsAdditiveTests(unittest.TestCase):
    def test_input_driven_channel_does_not_add_its_followers(self):
        profile = ModelProfile()
        lean = ParamSpec("LeanInput", "lean_y", -1, 1, 0)
        follower = ParamSpec("LeanY2", "lean_y", -1, 1, 0)
        profile.by_channel["lean_y"] = lean
        profile.params["LeanInput"] = lean
        profile.params["LeanY2"] = follower
        profile.physics_inputs = {"LeanInput"}
        profile.physics_outputs = {"LeanY2"}
        profile.followers = {"lean_y": [("LeanY2", 0.8)]}
        self.assertEqual({}, physics_additive_specs(profile))


class IntrospectionTests(unittest.TestCase):
    def test_canonical_handles_param_prefix_and_case(self):
        self.assertEqual(canonical("ParamAngleX"), "anglex")
        self.assertEqual(canonical("PARAM_ANGLE_X"), "anglex")
        self.assertEqual(canonical("Angle_X"), "anglex")

    def test_exact_aliases(self):
        self.assertEqual(match_channel("ParamEyeLOpen")[0], "eye_l_open")
        self.assertEqual(match_channel("PARAM_MOUTH_OPEN_Y")[0], "mouth_open")
        self.assertEqual(match_channel("ParamBodyAngleZ")[0], "body_z")

    def test_unknown_parameters_are_not_guessed(self):
        self.assertIsNone(match_channel("Param12"))
        self.assertIsNone(match_channel("Paramcol_1"))
        self.assertIsNone(match_channel("ParamHF_01"))

    def test_full_profile_tier_and_bindings(self):
        profile = introspect(FakeSource(FULL), model_name="full")
        self.assertEqual(profile.tier, "full")
        self.assertEqual(profile.by_channel["head_x"].id, "ParamAngleX")
        self.assertEqual(profile.by_channel["mouth_open"].id, "ParamMouthOpenY")
        self.assertTrue(profile.capability("arms"))
        self.assertTrue(profile.capability("legs"))

    def test_face_only_profile_tier(self):
        profile = introspect(FakeSource(FACE), model_name="face")
        self.assertEqual(profile.tier, "face")
        self.assertFalse(profile.capability("body"))
        self.assertTrue(profile.capability("blink"))
        self.assertTrue(profile.capability("lip_sync"))

    def test_duplicate_prefers_standard_param_name(self):
        profile = introspect(FakeSource(DUPLICATE), model_name="dupes")
        self.assertEqual(profile.by_channel["head_x"].id, "ParamAngleX")

    def test_suffix_parameter_is_used_when_it_is_the_only_one(self):
        profile = introspect(FakeSource(WEIRD), model_name="weird")
        self.assertEqual(profile.by_channel["body_z"].id, "ParamBodyAngleZ1")
        self.assertGreaterEqual(len(profile.unmapped), 3)

    def test_prefix_limb_names_from_game_models(self):
        profile = introspect(FakeSource(CUSTOM_LIMBS), model_name="custom")
        self.assertEqual(profile.by_channel["arm_l"].id, "LArm")
        self.assertEqual(profile.by_channel["arm_r"].id, "RArm")
        self.assertEqual(profile.by_channel["arm_l_bend"].id, "LForearm")
        self.assertEqual(profile.by_channel["hand_l"].id, "LHand")
        self.assertEqual(profile.by_channel["leg_l"].id, "LEGAngleZ2")
        self.assertEqual(profile.tier, "upper")

    def test_asymmetric_mouth_form_neutral_is_preserved(self):
        spec = ParamSpec("ParamMouthForm", "mouth_form", -2.0, 1.0, -0.5)
        self.assertAlmostEqual(spec.to_raw(0.0), -0.5)
        self.assertAlmostEqual(spec.to_raw(1.0), 1.0)
        self.assertAlmostEqual(spec.to_raw(-1.0), -2.0)

    def test_unit_neutral_is_zero_not_negative_minimum(self):
        spec = ParamSpec("EyeSmile", "eye_l_smile", -1.0, 1.0, 0.0, kind="unit")
        self.assertAlmostEqual(spec.to_raw(0.0), 0.0)
        self.assertAlmostEqual(spec.to_raw(1.0), 1.0)

    def test_eye_open_uses_neutral_as_scale(self):
        spec = ParamSpec("ParamEyeROpen", "eye_r_open", 0.0, 1.5, 1.5, kind="eye")
        self.assertAlmostEqual(spec.to_raw(1.0), 1.5)
        self.assertAlmostEqual(spec.to_raw(0.0), 0.0)
        self.assertAlmostEqual(spec.to_raw(1.3), 1.5)

    def test_compensation_moves_missing_arm_to_body(self):
        profile = introspect(FakeSource(FACE), model_name="face")
        pose = compensate_pose({"arm_r": 0.8, "head_z": 0.1}, profile)
        self.assertNotIn("arm_r", pose)
        self.assertGreater(pose.get("head_z", 0.0), 0.1)
        self.assertLessEqual(abs(pose.get("head_z", 0.0)), 1.0)

    def test_compensation_drops_legs_on_face_model(self):
        profile = introspect(FakeSource(FACE), model_name="face")
        pose = compensate_pose({"leg_r": 1.0}, profile)
        self.assertNotIn("leg_r", pose)
        self.assertTrue(math.isfinite(sum(pose.values())))

    def test_common_joint_names_are_not_model_specific(self):
        source = FakeSource({
            "ThighL": (-30, 30, 0), "KneeL": (-30, 30, 0),
            "AnkleL": (-30, 30, 0), "FootL": (-30, 30, 0),
            "ToeL": (-30, 30, 0),
            "RThigh": (-30, 30, 0), "RUpperLeg": (-30, 30, 0),
            "RLowerLeg": (-30, 30, 0), "RAnkle": (-30, 30, 0),
        })
        profile = introspect(source)
        self.assertEqual(profile.by_channel["leg_l"].id, "ThighL")
        self.assertEqual(profile.by_channel["knee_l"].id, "KneeL")
        self.assertEqual(profile.by_channel["ankle_l"].id, "AnkleL")
        self.assertEqual(profile.by_channel["foot_l"].id, "FootL")
        self.assertEqual(profile.by_channel["foot_l_y"].id, "ToeL")
        self.assertEqual(profile.by_channel["leg_r"].id, "RThigh")
        self.assertEqual(profile.by_channel["knee_r"].id, "RLowerLeg")
        self.assertEqual(profile.by_channel["ankle_r"].id, "RAnkle")

    def test_probe_eye_centroid_becomes_pointer_origin(self):
        source = FakeSource({"ParamEyeBallX": (-1, 1, 0),
                             "ParamAngleX": (-30, 30, 0)})
        probe = {
            "ParamEyeBallX": {
                "active_vertices": True, "cx": 0.12, "cy": 0.81,
                "span_ratio": 0.08,
            },
        }
        profile = introspect(source, probe=probe)
        self.assertEqual(profile.pointer_origin, (0.12, 0.81))

    def test_probe_height_reorders_mislabelled_leg_joints(self):
        source = FakeSource({
            "ThighL": (-30, 30, 0), "KneeL": (-30, 30, 0),
            "AnkleL": (-30, 30, 0),
        })
        probe = {
            "ThighL": {"active_vertices": True, "cy": 0.18, "span_ratio": 0.1},
            "KneeL": {"active_vertices": True, "cy": 0.42, "span_ratio": 0.1},
            "AnkleL": {"active_vertices": True, "cy": 0.71, "span_ratio": 0.1},
        }
        profile = introspect(source, probe=probe)
        self.assertEqual(profile.by_channel["leg_l"].id, "AnkleL")
        self.assertEqual(profile.by_channel["knee_l"].id, "KneeL")
        self.assertEqual(profile.by_channel["ankle_l"].id, "ThighL")
        self.assertIn("probe-chain", profile.by_channel["leg_l"].evidence)

    def test_cdi3_chinese_joint_names(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cdi = root / "model.cdi3.json"
            cdi.write_text(json.dumps({"Parameters": [
                {"Id": "P1", "Name": "R大腿"},
                {"Id": "P2", "Name": "R小腿"},
                {"Id": "P3", "Name": "R脚腕"},
                {"Id": "P4", "Name": "R脚尖"},
                {"Id": "P5", "Name": "L脚掌"},
            ]}, ensure_ascii=False), encoding="utf-8")
            model3 = root / "model.model3.json"
            model3.write_text(json.dumps({
                "FileReferences": {"DisplayInfo": cdi.name},
            }), encoding="utf-8")
            channels = read_display_channels(model3)
        self.assertEqual(channels["P1"], "leg_r")
        self.assertEqual(channels["P2"], "knee_r")
        self.assertEqual(channels["P3"], "ankle_r")
        self.assertEqual(channels["P4"], "foot_r_y")
        self.assertEqual(channels["P5"], "foot_l")


if __name__ == "__main__":
    unittest.main()

