from __future__ import annotations

import unittest

from sidekick.motion import Context, MotionEngine, introspect
from sidekick.motion.gaze import GazeController, pointer_target


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
    "ParamBodyAngleX": (-10, 10, 0), "ParamBodyAngleY": (-10, 10, 0),
    "ParamBodyAngleZ": (-10, 10, 0),
}


class GazeTests(unittest.TestCase):
    def test_gaze_stays_in_range(self):
        gaze = GazeController(seed=2)
        context = Context(now=0.0)
        for i in range(60 * 40):
            context.now = i / 60
            pose = gaze.update(1 / 60, context)
            for channel in ("head_x", "head_y", "head_z", "eye_x", "eye_y",
                            "body_x", "body_y", "body_z"):
                self.assertGreaterEqual(pose.get(channel), -1.0)
                self.assertLessEqual(pose.get(channel), 1.0)

    def test_saccades_happen(self):
        gaze = GazeController(seed=4)
        context = Context(now=0.0)
        for i in range(60 * 30):
            context.now = i / 60
            gaze.update(1 / 60, context)
        self.assertGreater(gaze.state.saccades, 3)
        self.assertGreater(gaze.state.fixations, 2)

    def test_manual_look_at_triggers_saccade(self):
        gaze = GazeController(seed=8)
        context = Context(now=0.0)
        for i in range(30):
            context.now = i / 60
            gaze.update(1 / 60, context)
        before = gaze.state.saccades
        gaze.look_at(-0.8, 0.5, dwell=2.0)
        self.assertGreater(gaze.state.saccades, before)

    def test_eyes_lead_head_on_a_large_turn(self):
        engine = MotionEngine(introspect(FakeSource(FULL)), seed=12)
        # Settle on camera.
        for i in range(120):
            engine.tick(i / 60, 1 / 60, context=Context(now=i / 60))
        engine.gaze.look_at(0.9, 0.0, dwell=3.0)
        lead_frames = 0
        for i in range(30):
            now = (120 + i) / 60
            frame = engine.tick(now, 1 / 60, context=Context(now=now))
            if abs(frame.pose.get("eye_x")) > abs(frame.pose.get("head_x")):
                lead_frames += 1
        self.assertGreater(lead_frames, 0)

    def test_pointer_origin_uses_eye_center(self):
        self.assertEqual(pointer_target(500, 500, 1000, 1000), (0.0, 0.0))
        # 眼睛在 81% 高度时，窗口中心应变成向下 0.62，而不是直视。
        x, y = pointer_target(500, 500, 1000, 1000, (0.0, 0.81))
        self.assertAlmostEqual(x, 0.0)
        self.assertAlmostEqual(y, -0.62)
        x, y = pointer_target(500, 190, 1000, 1000, (0.0, 0.81))
        self.assertAlmostEqual(x, 0.0)
        self.assertAlmostEqual(y, 0.0, places=2)

    def test_speaking_prefers_camera(self):
        gaze = GazeController(seed=6)
        context = Context(now=0.0, speaking=True)
        for i in range(120):
            context.now = i / 60
            gaze.update(1 / 60, context)
        # The target stays in the camera region while speaking most of the time.
        self.assertIn(gaze.state.target_kind, ("camera", "game", "chat", "think", "wander"))


if __name__ == "__main__":
    unittest.main()

