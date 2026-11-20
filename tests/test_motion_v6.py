from __future__ import annotations

import unittest

from sidekick.motion import Context, MotionEngine, introspect
from sidekick.motion.actions import ActionLibrary, ActionPlayer
from sidekick.motion.compositor import compose
from sidekick.motion.emotion import EmotionState
from sidekick.motion.gaze import GazeController
from sidekick.motion.types import Pose


class FakeSource:
    def __init__(self, data):
        self.data = data

    def param_ids(self):
        return list(self.data)

    def param_info(self, param_id):
        return self.data[param_id]


LEGS = {
    "ParamAngleX": (-30, 30, 0), "ParamAngleY": (-30, 30, 0),
    "ParamAngleZ": (-30, 30, 0),
    "ParamEyeLOpen": (0, 1, 1), "ParamEyeROpen": (0, 1, 1),
    "ParamMouthOpenY": (0, 1, 0), "ParamMouthForm": (-1, 1, 0),
    "ParamBodyAngleX": (-10, 10, 0), "ParamBodyAngleY": (-10, 10, 0),
    "ParamBodyAngleZ": (-10, 10, 0),
    "ChestX1": (-10, 10, 0), "ChestY1": (-10, 10, 0),
    "EyeSquint": (0, 1, 0),
    "LegL1Z": (-30, 30, 0), "LegR1Z": (-30, 30, 0),
    "LegL2Z": (-30, 30, 0), "LegR2Z": (-30, 30, 0),
    "LegL3Z": (-30, 30, 0), "LegR3Z": (-30, 30, 0),
    "FootLX": (-20, 20, 0), "FootRX": (-20, 20, 0),
    "LegLF": (-1, 1, 0),
}


def profile():
    return introspect(FakeSource(LEGS), model_name="legs")


class DuckingTests(unittest.TestCase):
    def test_action_envelope_ducks_posture_and_idle(self):
        pose = compose(
            profile(), EmotionState(), Pose({"body_x": 1.0}),
            Pose(), Pose(), Pose({"head_x": 1.0}), Pose(),
            breath=0.5, blink_l=1.0, blink_r=1.0, speech=None,
            speaking=False, action_strength=0.0)
        ducked = compose(
            profile(), EmotionState(), Pose({"body_x": 1.0}),
            Pose(), Pose(), Pose({"head_x": 1.0}), Pose(),
            breath=0.5, blink_l=1.0, blink_r=1.0, speech=None,
            speaking=False, action_strength=1.0)
        self.assertLess(ducked.get("body_x"), pose.get("body_x"))
        self.assertLess(ducked.get("head_x"), pose.get("head_x"))

    def test_action_player_exposes_envelope_strength(self):
        player = ActionPlayer(ActionLibrary(), seed=1)
        self.assertTrue(player.play("kick", profile(), now=0.0))
        for _ in range(20):
            player.update(1 / 60, now=0.4)
        self.assertGreater(player.strength, 0.0)
        player.cancel()
        player.update(1 / 60, now=0.5)
        self.assertEqual(player.strength, 0.0)


class LegDynamicsTests(unittest.TestCase):
    def _pose(self, emotion=None):
        return compose(
            profile(), emotion or EmotionState(),
            Pose({"body_z": 0.8, "hip": 0.4}), Pose(), Pose(), Pose(), Pose(),
            breath=0.5, blink_l=1.0, blink_r=1.0, speech=None, speaking=False)

    def test_weight_shift_bends_one_knee_and_plants_the_other_foot(self):
        pose = self._pose()
        self.assertGreater(pose.get("leg_l"), 0.0)
        self.assertLess(pose.get("leg_r"), 0.0)
        self.assertGreater(pose.get("knee_l"), 0.0)
        self.assertEqual(pose.get("knee_r"), 0.0)
        self.assertLess(pose.get("ankle_l"), 0.0)
        self.assertGreater(pose.get("ankle_r"), 0.0)

    def test_idle_foot_swing_is_small_but_happy_foot_swing_is_visible(self):
        quiet = self._pose()
        happy = EmotionState()
        happy.set("happy", 1.0)
        happy.update(1.0, now=1.0)
        active = self._pose(happy)
        self.assertLessEqual(abs(quiet.get("foot_swing")), 0.05)
        self.assertGreater(abs(active.get("foot_swing")), 0.10)

    def test_chest_breath_lags_behind_body(self):
        early = compose(
            profile(), EmotionState(), Pose(), Pose(), Pose(), Pose(), Pose(),
            breath=1.0, breath_lag=0.5, blink_l=1.0, blink_r=1.0,
            speech=None, speaking=False)
        late = compose(
            profile(), EmotionState(), Pose(), Pose(), Pose(), Pose(), Pose(),
            breath=1.0, breath_lag=1.0, blink_l=1.0, blink_r=1.0,
            speech=None, speaking=False)
        self.assertLess(early.get("chest_y"), late.get("chest_y"))
        self.assertAlmostEqual(early.get("body_y"), late.get("body_y"))


class SquintChannelTests(unittest.TestCase):
    def test_emotion_squint_reaches_a_dedicated_parameter(self):
        emotion = EmotionState()
        emotion.set("happy", 1.0)
        emotion.update(1.0, now=1.0)
        pose = compose(
            profile(), emotion, Pose(), Pose(), Pose(), Pose(), Pose(),
            breath=0.5, blink_l=1.0, blink_r=1.0, speech=None,
            speaking=False)
        self.assertGreater(pose.get("eye_squint"), 0.0)


class SpeakingFaceTests(unittest.TestCase):
    def test_speaking_energy_adds_eye_smile_without_emotion(self):
        pose = compose(
            profile(), EmotionState(), Pose(), Pose(), Pose(), Pose(), Pose(),
            breath=0.5, blink_l=1.0, blink_r=1.0, speech=None,
            speaking=True, speech_energy=0.8)
        self.assertGreater(pose.get("eye_l_smile"), 0.0)
        self.assertGreater(pose.get("eye_r_smile"), 0.0)

    def test_gaze_returns_to_camera_when_speech_ends(self):
        gaze = GazeController(seed=3)
        context = Context(now=0.0, speaking=True)
        for i in range(90):
            context.now = i / 60
            gaze.update(1 / 60, context)
        context.speaking = False
        gaze.update(1 / 60, context)
        self.assertEqual(gaze.state.target_kind, "camera")
        self.assertAlmostEqual(gaze.state.target_x, 0.0, places=2)


class EngineBreathLagTests(unittest.TestCase):
    def test_engine_chest_trails_body_during_inhale(self):
        engine = MotionEngine(profile(), seed=4)
        samples = []
        for i in range(180):
            frame = engine.tick(i / 60, 1 / 60, context=Context(now=i / 60))
            samples.append((frame.pose.get("body_y"), frame.pose.get("chest_y")))
        # Both move; chest should not lead every single frame.
        self.assertTrue(any(chest > body for body, chest in samples))
        self.assertTrue(any(chest < body for body, chest in samples))


if __name__ == "__main__":
    unittest.main()
