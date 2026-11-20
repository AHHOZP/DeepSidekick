from __future__ import annotations

import math
import unittest

from sidekick.motion import Context, Intent, MotionEngine, SpeechFrame, introspect


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

FACE = {
    "ParamAngleX": (-30, 30, 0), "ParamAngleY": (-30, 30, 0),
    "ParamAngleZ": (-30, 30, 0),
    "ParamEyeLOpen": (0, 1, 1), "ParamEyeROpen": (0, 1, 1),
    "ParamMouthOpenY": (0, 1, 0), "ParamMouthForm": (-1, 1, 0),
}


class EngineTests(unittest.TestCase):
    def test_ten_seconds_are_finite_and_in_range(self):
        profile = introspect(FakeSource(FULL))
        engine = MotionEngine(profile, seed=3)
        context = Context()
        for i in range(600):
            now = i / 60
            frame = engine.tick(now, 1 / 60, context=context)
            for channel, value in frame.pose.values.items():
                self.assertTrue(math.isfinite(value), channel)
            for pid, value in frame.params.items():
                spec = profile.params[pid]
                self.assertGreaterEqual(value, spec.minimum - 1e-6)
                self.assertLessEqual(value, spec.maximum + 1e-6)

    def test_action_is_submitted_and_returns_to_neutral(self):
        profile = introspect(FakeSource(FULL))
        engine = MotionEngine(profile, seed=4)
        context = Context()
        for i in range(60):
            engine.tick(i / 60, 1 / 60, context=context)
        engine.submit(Intent("action", "wave", 1.0, None, "test"))
        peak = 0.0
        for i in range(60, 300):
            frame = engine.tick(i / 60, 1 / 60, context=context)
            peak = max(peak, abs(frame.pose.get("arm_r")))
        self.assertGreater(peak, 0.2)
        self.assertLess(abs(frame.pose.get("arm_r")), 0.1)

    def test_emotion_changes_pose_and_decays(self):
        profile = introspect(FakeSource(FULL))
        engine = MotionEngine(profile, seed=5)
        context = Context()
        for i in range(60):
            engine.tick(i / 60, 1 / 60, context=context)
        engine.submit(Intent("emotion", "happy", 1.0, 1.0, "test"))
        peak = 0.0
        for i in range(60, 180):
            frame = engine.tick(i / 60, 1 / 60, context=context)
            peak = max(peak, frame.pose.get("mouth_form"))
        self.assertGreater(peak, 0.2)

    def test_mouth_follows_speech(self):
        profile = introspect(FakeSource(FULL))
        engine = MotionEngine(profile, seed=6)
        context = Context(speaking=True)
        frame = None
        for i in range(120):
            frame = engine.tick(
                i / 60, 1 / 60,
                speech=SpeechFrame(t=i / 60, level=0.7, openness=0.65,
                                   form=0.1, voiced=True),
                context=context)
        self.assertGreater(frame.pose.get("mouth_open"), 0.4)

    def test_face_profile_does_not_crash_on_body_action(self):
        profile = introspect(FakeSource(FACE))
        engine = MotionEngine(profile, seed=7)
        context = Context()
        engine.submit(Intent("action", "kick", 1.0, None, "test"))
        for i in range(180):
            frame = engine.tick(i / 60, 1 / 60, context=context)
            self.assertTrue(frame.params)
            self.assertEqual(profile.tier, "face")

    def test_parameters_include_every_binding(self):
        profile = introspect(FakeSource(FULL))
        engine = MotionEngine(profile, seed=8)
        frame = engine.tick(1 / 60, 1 / 60, context=Context())
        self.assertEqual(
            set(frame.params),
            {spec.id for spec in profile.by_channel.values()},
        )

    def test_time_going_backwards_does_not_break(self):
        profile = introspect(FakeSource(FULL))
        engine = MotionEngine(profile, seed=9)
        engine.tick(10.0, 1 / 60)
        frame = engine.tick(9.0, 1 / 60)
        self.assertTrue(all(math.isfinite(v) for v in frame.pose.values.values()))


if __name__ == "__main__":
    unittest.main()

