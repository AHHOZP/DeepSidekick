from __future__ import annotations

import unittest

from sidekick.motion.actions import ActionLibrary, ActionPlayer
from sidekick.motion.introspect import introspect


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
    "ParamMouthOpenY": (0, 1, 0), "ParamMouthForm": (-1, 1, 0),
    "ParamBodyAngleX": (-10, 10, 0), "ParamBodyAngleZ": (-10, 10, 0),
    "ParamArmRA": (0, 1, 0), "ParamArmRB": (0, 1, 0),
    "ParamArmLA": (0, 1, 0), "ParamArmLB": (0, 1, 0),
    "ParamHandR": (-1, 1, 0), "ParamHandL": (-1, 1, 0),
    "ParamLegR": (-1, 1, 0),
}

FACE = {
    "ParamAngleX": (-30, 30, 0), "ParamAngleY": (-30, 30, 0),
    "ParamAngleZ": (-30, 30, 0),
    "ParamEyeLOpen": (0, 1, 1), "ParamEyeROpen": (0, 1, 1),
    "ParamMouthOpenY": (0, 1, 0), "ParamMouthForm": (-1, 1, 0),
}

LEFT_LEG = {
    **FACE,
    "ParamBodyAngleX": (-10, 10, 0),
    "LEGAngleZ2": (-30, 30, 0),
}


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.full = introspect(FakeSource(FULL))
        self.face = introspect(FakeSource(FACE))
        self.library = ActionLibrary()

    def test_wave_has_attack_hold_release(self):
        player = ActionPlayer(self.library, seed=1)
        self.assertTrue(player.play("wave", self.full, now=0.0))
        values = []
        for i in range(150):
            pose = player.update(1 / 60, now=i / 60)
            values.append(pose.get("arm_l"))
        self.assertLess(values[0], 0.2)
        self.assertGreater(max(values), 0.6)
        self.assertLess(values[-1], 0.1)
        self.assertGreater(max(values), min(values))

    def test_hold_is_not_frozen(self):
        player = ActionPlayer(self.library, seed=2)
        player.play("wave", self.full, now=0.0)
        sample = []
        for i in range(30, 75):
            pose = player.update(1 / 60, now=i / 60)
            sample.append(pose.get("arm_l"))
        self.assertGreater(max(sample) - min(sample), 0.01)

    def test_action_degrades_to_body_on_face_model(self):
        player = ActionPlayer(self.library, seed=3)
        self.assertTrue(player.play("wave", self.face, now=0.0))
        pose = player.update(0.3, now=0.3)
        self.assertGreater(abs(pose.get("body_z")) + abs(pose.get("head_z")), 0.01)
        self.assertEqual(pose.get("arm_r"), 0.0)

    def test_kick_uses_the_only_leg_when_right_leg_is_missing(self):
        profile = introspect(FakeSource(LEFT_LEG))
        resolved = self.library.resolve_for(
            self.library.resolve("kick"), profile)
        self.assertGreater(resolved.pose.get("leg_l", 0.0), 0.0)

    def test_cooldown_blocks_immediate_replay(self):
        player = ActionPlayer(self.library, seed=4)
        self.assertTrue(player.play("wave", self.full, now=0.0))
        for i in range(180):
            player.update(1 / 60, now=i / 60)
        self.assertFalse(player.play("wave", self.full, now=3.0))
        self.assertTrue(player.play("wave", self.full, now=10.0))

    def test_unknown_action_is_rejected(self):
        player = ActionPlayer(self.library, seed=5)
        self.assertFalse(player.play("not_a_real_action", self.full, now=0.0))

    def test_interrupting_an_action_sets_a_partial_cooldown(self):
        player = ActionPlayer(self.library, seed=7)
        self.assertTrue(player.play("wave", self.full, now=0.0))
        self.assertTrue(player.play("nod", self.full, now=0.1, force=True))
        self.assertFalse(player.play("wave", self.full, now=0.2))

    def test_force_interrupts(self):
        player = ActionPlayer(self.library, seed=6)
        player.play("wave", self.full, now=0.0)
        self.assertTrue(player.play("nod", self.full, now=0.1, force=True))
        self.assertEqual(player.current_name, "nod")


if __name__ == "__main__":
    unittest.main()

