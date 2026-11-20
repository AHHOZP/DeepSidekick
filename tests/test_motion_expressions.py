from __future__ import annotations

import unittest

from sidekick.avatar.autonomy.motion_parse import ExpressionAsset
from sidekick.motion.engine import MotionEngine
from sidekick.motion.expressions import ExpressionMixer
from sidekick.motion.introspect import introspect
from sidekick.motion.types import Context


class FakeSource:
    def __init__(self, data):
        self.data = data

    def param_ids(self):
        return list(self.data)

    def param_info(self, param_id):
        return self.data[param_id]


PROFILE = {
    "ParamAngleX": (-30, 30, 0),
    "ParamEyeLOpen": (0, 1, 1),
    "ParamEyeROpen": (0, 1, 1),
    "ParamMouthOpenY": (0, 1, 0),
    "ParamMouthForm": (-1, 1, 0),
}


def asset(name, values):
    return ExpressionAsset(
        name=name, file=f"{name}.exp3.json",
        params=frozenset(pid for pid, _v, _b in values),
        values=tuple(values))


class ExpressionMixerTests(unittest.TestCase):
    def test_multiple_expressions_stack_and_fade(self):
        profile = introspect(FakeSource(PROFILE))
        mixer = ExpressionMixer({
            "脸红": asset("脸红", [("ParamMouthForm", 0.8, "Add")]),
            "爱心眼": asset("爱心眼", [("ParamMouthForm", 1.0, "Overwrite")]),
        }, profile.ranges)
        self.assertTrue(mixer.play("脸红"))
        self.assertTrue(mixer.play("爱心眼"))
        for _ in range(30):
            mixer.update(1 / 60)
        params = mixer.apply({"ParamMouthForm": 0.0}, profile)
        self.assertGreater(params["ParamMouthForm"], 0.0)
        self.assertEqual(set(mixer.active), {"脸红", "爱心眼"})
        mixer.clear()
        for _ in range(120):
            mixer.update(1 / 60)
        self.assertEqual(mixer.active, {})

    def test_mouth_open_is_skipped_while_speaking(self):
        profile = introspect(FakeSource(PROFILE))
        mixer = ExpressionMixer({
            "惊讶": asset("惊讶", [
                ("ParamMouthOpenY", 1.0, "Overwrite"),
                ("ParamMouthForm", 1.0, "Overwrite"),
            ]),
        }, profile.ranges)
        mixer.play("惊讶")
        for _ in range(30):
            mixer.update(1 / 60)
        params = mixer.apply({"ParamMouthOpenY": 0.2,
                              "ParamMouthForm": 0.0},
                             profile, speaking=True)
        self.assertEqual(params["ParamMouthOpenY"], 0.2)
        self.assertGreater(params["ParamMouthForm"], 0.0)

    def test_mouth_follower_is_also_skipped_while_speaking(self):
        profile = introspect(FakeSource({**PROFILE, "JawOpen": (0, 1, 0)}))
        profile.followers = {"mouth_open": [("JawOpen", 1.0)]}
        mixer = ExpressionMixer({
            "惊讶": asset("惊讶", [("JawOpen", 1.0, "Overwrite")]),
        }, profile.ranges)
        mixer.play("惊讶")
        for _ in range(30):
            mixer.update(1 / 60)
        params = mixer.apply({"JawOpen": 0.0}, profile, speaking=True)
        self.assertEqual(params["JawOpen"], 0.0)

    def test_engine_applies_mixer_before_persistent_states(self):
        profile = introspect(FakeSource(PROFILE))
        mixer = ExpressionMixer({
            "脸红": asset("脸红", [("ParamMouthForm", 1.0, "Overwrite")]),
        }, profile.ranges)
        engine = MotionEngine(profile, seed=3, expression_mixer=mixer)
        mixer.play("脸红")
        for i in range(30):
            frame = engine.tick(i / 60, 1 / 60, context=Context())
        self.assertGreater(frame.params["ParamMouthForm"], 0.2)


if __name__ == "__main__":
    unittest.main()
