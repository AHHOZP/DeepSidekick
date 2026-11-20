from __future__ import annotations

import math
import unittest

from sidekick.motion import (
    Context, MotionEngine, MotionOwnership, MotionTuning, Pose, apply_tuning,
    introspect,
)


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
    "ParamMouthOpenY": (0, 1, 0), "ParamMouthForm": (-2, 1, -0.5),
    "ParamBodyAngleX": (-10, 10, 0), "ParamBodyAngleZ": (-10, 10, 0),
    "ParamBreath": (0, 1, 0),
    "ParamArmRA": (0, 1, 0), "ParamArmRB": (0, 1, 0),
    "ParamHandR": (-1, 1, 0), "ParamLegR": (-1, 1, 0),
    "ParamBrowLY": (-1, 1, 0), "ParamCheek": (0, 1, 0),
}


class TuningTests(unittest.TestCase):
    def test_out_of_range_is_clamped_with_warning(self):
        tuning, warnings = MotionTuning.from_dict(
            {"head_gain": 9, "blink_scale": "bad"})
        self.assertEqual(tuning.head_gain, 1.45)
        self.assertEqual(tuning.blink_scale, 1.0)
        self.assertEqual(len(warnings), 2)

    def test_apply_tuning_scales_once_and_clamps(self):
        tuning = MotionTuning(head_gain=1.2, arm_gain=0.5,
                              mouth_open_gain=1.5)
        pose = apply_tuning(Pose({
            "head_x": 0.5, "arm_r": 0.4, "mouth_open": 0.8,
            "body_z": 0.2,
        }), tuning)
        self.assertAlmostEqual(pose.get("head_x"), 0.6)
        self.assertAlmostEqual(pose.get("arm_r"), 0.2)
        self.assertAlmostEqual(pose.get("mouth_open"), 1.0)
        self.assertAlmostEqual(pose.get("body_z"), 0.2)

    def test_legacy_body_gain_falls_back_to_full_scale(self):
        tuning, warnings = MotionTuning.from_dict({"body_gain": 0.13})
        self.assertEqual(tuning.body_gain, 1.0)
        self.assertTrue(any("body_gain" in warning for warning in warnings))

    def test_profile_tuning_reaches_engine(self):
        profile = introspect(FakeSource(FULL))
        profile.tuning = {"head_gain": 1.2, "spring_scale": 1.1}
        engine = MotionEngine(profile, seed=1)
        self.assertEqual(engine.tuning.head_gain, 1.2)
        self.assertAlmostEqual(engine.pose_springs.springs["head_x"].frequency,
                               1.48 * 1.1)
        self.assertEqual(engine.gaze.config.body_gain, 0.22)


class OwnershipTests(unittest.TestCase):
    def test_claim_suppresses_then_hands_over(self):
        own = MotionOwnership(crossfade_s=0.32)
        own.claim(["P"], now=0.0, hold_s=1.0)
        self.assertEqual(own.filter({"P": 1.0}, 0.5), {})
        # 资产结束的那一帧从模型最后值开始交接。
        first = own.filter({"P": 1.0}, 1.0, read_raw=lambda _pid: 0.9)
        self.assertAlmostEqual(first["P"], 0.9)
        middle = own.filter({"P": 1.0}, 1.16, read_raw=lambda _pid: 0.9)
        self.assertAlmostEqual(middle["P"], 0.95)
        end = own.filter({"P": 1.0}, 1.4, read_raw=lambda _pid: 0.9)
        self.assertAlmostEqual(end["P"], 1.0)

    def test_protected_parameter_always_uses_engine(self):
        own = MotionOwnership()
        own.claim(["ParamMouthOpenY", "ParamAngleX"], now=0.0, hold_s=2.0)
        out = own.filter({"ParamMouthOpenY": 0.4, "ParamAngleX": 5.0}, 0.5,
                         protected={"ParamMouthOpenY"})
        self.assertEqual(out, {"ParamMouthOpenY": 0.4})

    def test_release_and_read_failure(self):
        own = MotionOwnership()
        own.claim(["A", "B"], now=0.0, hold_s=2.0)
        own.release(["A"])
        out = own.filter({"A": 0.2, "B": 0.3}, 0.1)
        self.assertEqual(out, {"A": 0.2})
        expired = own.filter({"B": 0.3}, 2.0, read_raw=lambda _pid: None)
        self.assertEqual(expired, {"B": 0.3})


class PointerTests(unittest.TestCase):
    def test_pointer_is_a_continuous_high_priority_target(self):
        profile = introspect(FakeSource(FULL))
        engine = MotionEngine(profile, seed=3)
        ctx = Context(pointer=(0.5, -0.4))
        signed = 0.0
        for i in range(180):
            frame = engine.tick(i / 60, 1 / 60, context=ctx)
            signed += frame.pose.get("eye_x")
        self.assertEqual(engine.gaze.state.target_kind, "pointer")
        self.assertGreater(signed, 0.0)
        self.assertTrue(math.isfinite(signed))

    def test_pointer_none_keeps_virtual_gaze(self):
        profile = introspect(FakeSource(FULL))
        engine = MotionEngine(profile, seed=4)
        for i in range(120):
            engine.tick(i / 60, 1 / 60, context=Context())
        self.assertNotEqual(engine.gaze.state.target_kind, "pointer")


if __name__ == "__main__":
    unittest.main()
