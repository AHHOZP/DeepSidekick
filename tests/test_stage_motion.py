from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from sidekick.app.runtime import _build_stage_motion
from sidekick.motion import Context, SpeechFrame, SpeechRing, profile_from_dossier
from sidekick.motion.adapters.stage import StageMotionHost

DOSSET = {
    "tier": "face",
    "capabilities": {"head": True, "eyes": True, "mouth": True},
    "params": [
        {"id": "ParamAngleX", "min": -30, "max": 30, "default": 0,
         "kind": "bipolar", "channel": "head_x", "score": 100},
        {"id": "ParamEyeLOpen", "min": 0, "max": 1, "default": 1,
         "kind": "eye", "channel": "eye_l_open", "score": 100},
        {"id": "ParamEyeROpen", "min": 0, "max": 1, "default": 1,
         "kind": "eye", "channel": "eye_r_open", "score": 100},
        {"id": "ParamMouthOpenY", "min": 0, "max": 1, "default": 0,
         "kind": "unit", "channel": "mouth_open", "score": 100},
    ],
    "by_channel": {
        "head_x": "ParamAngleX",
        "eye_l_open": "ParamEyeLOpen",
        "eye_r_open": "ParamEyeROpen",
        "mouth_open": "ParamMouthOpenY",
    },
    "motions": [],
    "expressions": [],
    "warnings": [],
}


class FakeStage:
    def __init__(self):
        self.events = []

    def emit(self, event, **kw):
        self.events.append((event, kw))


PHYSICS_DOSSIER = {
    **DOSSET,
    "params": [
        *DOSSET["params"],
        {"id": "ChestX1", "min": -10, "max": 10, "default": 0,
         "kind": "bipolar", "channel": "chest_x", "score": 100},
    ],
    "by_channel": {**DOSSET["by_channel"], "chest_x": "ChestX1"},
    "physics": {"present": True, "inputs": [], "outputs": ["ChestX1"]},
}


class StageMotionTests(unittest.TestCase):
    def test_profile_from_dossier(self):
        profile = profile_from_dossier(DOSSET)
        self.assertEqual(profile.tier, "face")
        self.assertEqual(profile.by_channel["head_x"].id, "ParamAngleX")
        self.assertAlmostEqual(profile.params["ParamAngleX"].positive_span, 30)

    def test_tick_emits_raw_pose_and_seq(self):
        stage = FakeStage()
        profile = profile_from_dossier(DOSSET)
        host = StageMotionHost(stage, profile, seed=3)
        params = host.tick(1.0, 1 / 60, context=Context())
        self.assertTrue(params)
        self.assertEqual(stage.events[0][0], "pose")
        self.assertEqual(stage.events[0][1]["seq"], 1)
        self.assertEqual(stage.events[0][1]["ts"], 1.0)
        self.assertEqual(stage.events[0][1]["params"], params)
        self.assertEqual(host.report()["params"], len(params))
        self.assertEqual(host.report()["seq"], 1)

    def test_physics_leaf_is_sent_as_relative_delta(self):
        stage = FakeStage()
        profile = profile_from_dossier(PHYSICS_DOSSIER)
        host = StageMotionHost(stage, profile, seed=3)
        for i in range(90):
            host.tick(1.0 + i / 60, 1 / 60, context=Context())
        event = stage.events[-1][1]
        self.assertNotIn("ChestX1", event["params"])
        self.assertIn("ChestX1", event["add"])
        self.assertGreater(abs(event["add"]["ChestX1"]), 0.0)
        self.assertNotIn("ChestX1", host.tick(2.6, 1 / 60, context=Context()))

    def test_mouth_is_engine_owned_while_speaking(self):
        stage = FakeStage()
        profile = profile_from_dossier(DOSSET)
        host = StageMotionHost(stage, profile, seed=3)
        host.ownership.claim(["ParamMouthOpenY"], now=0.0, hold_s=10.0)
        params = host.tick(
            1.0, 1 / 60,
            speech=SpeechFrame(1.0, 0.6, 0.8, 0.1, True),
            context=Context(speaking=True))
        self.assertIn("ParamMouthOpenY", params)
        self.assertGreater(params["ParamMouthOpenY"], 0.0)

    def test_semantic_acts_enter_engine(self):
        stage = FakeStage()
        profile = profile_from_dossier(DOSSET)
        host = StageMotionHost(stage, profile, seed=3)
        host.tick(0.0, 1 / 60, context=Context())
        host.submit_assets([
            {"kind": "action", "name": "wave", "page": 0},
            {"kind": "emotion", "name": "happy", "page": 0},
            {"kind": "gaze", "name": "left", "page": 0},
        ], page=0)
        host.tick(0.1, 1 / 60, context=Context())
        report = host.report()
        self.assertEqual(report["action"], "wave")
        self.assertEqual(report["emotion"], "happy")

    def test_emotion_expression_control_is_emitted(self):
        stage = FakeStage()
        profile = profile_from_dossier(DOSSET)
        profile.expressions = {"happy": "开心"}
        host = StageMotionHost(stage, profile, avatar=SimpleNamespace())
        asset = SimpleNamespace(params={"ParamMouthForm"})
        with mock.patch(
                "sidekick.motion.adapters.stage.expression_asset",
                return_value=(asset, None)):
            host.submit_assets(
                [{"kind": "emotion", "name": "happy", "page": 0}], page=0)
        self.assertIn(("control", {"op": "expression", "name": "开心"}),
                      stage.events)

    def test_semantic_acts_follow_page(self):
        class Spy:
            def __init__(self):
                self.names = []

            def submit(self, intent):
                self.names.append(intent.name)

        stage = FakeStage()
        profile = profile_from_dossier(DOSSET)
        host = StageMotionHost(stage, profile, seed=3)
        spy = Spy()
        host.engine = spy
        acts = [
            {"kind": "action", "name": "wave", "page": 0},
            {"kind": "action", "name": "nod", "page": 1},
        ]
        host.submit_assets(acts, page=0)
        self.assertEqual(spy.names, ["wave"])
        host.submit_assets(acts, page=1)
        self.assertEqual(spy.names, ["wave", "nod"])

    def test_speech_ring_returns_time_matched_frame(self):
        ring = SpeechRing(maxlen=4)
        ring.push(SpeechFrame(1.0, openness=0.1))
        ring.push(SpeechFrame(2.0, openness=0.7))
        self.assertEqual(ring.latest(1.5).openness, 0.1)
        self.assertEqual(ring.latest(2.0).openness, 0.7)
        ring.clear()
        self.assertIsNone(ring.latest(2.0))

    def test_runtime_builder_only_for_live_live2d(self):
        cfg = {
            "stage": {"enabled": True, "mode": "live",
                      "autonomy": {"enabled": True}},
        }
        avatar = SimpleNamespace(kind="live2d", root=".", entry="x.model3.json")
        speaker = SimpleNamespace(mouth_sink=None, mouth_reset=None)
        with mock.patch("sidekick.app.runtime.run_audit", return_value=DOSSET), \
                mock.patch("sidekick.app.runtime.log"):
            host, ring, error = _build_stage_motion(
                ".", cfg, FakeStage(), avatar, speaker)
        self.assertIsNotNone(host)
        self.assertIsInstance(ring, SpeechRing)
        self.assertIsNone(error)
        self.assertTrue(callable(speaker.mouth_sink))

        cfg["stage"]["autonomy"]["enabled"] = False
        self.assertEqual(_build_stage_motion(
            ".", cfg, FakeStage(), avatar, speaker), (None, None, None))


if __name__ == "__main__":
    unittest.main()
