from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from identity_fixture import runtime_cfg
from sidekick.app import status
from sidekick.core.null import NullLongTerm
from sidekick.app.runtime import build_runtime


def _fake_runtime():
    return SimpleNamespace(
        cfg={},
        mic=SimpleNamespace(enabled=True, error=None),
        stt=SimpleNamespace(snapshot=lambda: {
            "dead": False, "disabled": False, "error": None}, error=None),
        stt_worker=None,
        stt_unavailable=False,
        frames=SimpleNamespace(enabled=True, last_error=None),
        stage_runner=None,
        stage_error=None,
        speaker=SimpleNamespace(
            profile_name="edge", available=True,
            snapshot=lambda: {"error": None}),
        memory=SimpleNamespace(enabled=True, last_summary_error=None),
        brain=SimpleNamespace(
            worldbook=SimpleNamespace(enabled=True), worldbook_error=None),
        actions=SimpleNamespace(avatar_id="", broken=[]),
    )


class AutonomyStatusTests(unittest.TestCase):
    def test_enabled_report_is_merged(self):
        rt = SimpleNamespace(cfg={"stage": {"autonomy": {"enabled": True}}})
        got = status._autonomy_status(rt, {"report": {"autonomy": {
            "active": True, "source": "profile", "tier": "upper",
            "archetype": "chibi", "fallback": True, "params": 25,
            "action": "wave", "emotion": "happy", "mouth": 0.42,
            "owned_params": 6, "warnings": 2, "error": None}}})
        self.assertTrue(got["active"])
        self.assertEqual(got["source"], "profile")
        self.assertEqual(got["tier"], "upper")
        self.assertEqual(got["mouth"], 0.42)
        self.assertEqual(got["owned_params"], 6)

    def test_disabled_ignores_report(self):
        rt = SimpleNamespace(cfg={"stage": {"autonomy": {"enabled": False}}})
        got = status._autonomy_status(
            rt, {"report": {"autonomy": {"active": True}}})
        self.assertFalse(got["enabled"])
        self.assertFalse(got["active"])
        self.assertEqual(got["source"], "off")


class CaptureStatusTests(unittest.TestCase):
    def test_last_round_and_merge_metrics(self):
        rt = SimpleNamespace(
            frames=SimpleNamespace(stats=lambda: {
                "pending": 2, "grabbed": 6, "merged": 2, "error": None}),
            last_round_frames=4, last_round_images=4,
            last_round_payload_kb=1024.5)
        got = status.capture_status(rt)
        self.assertEqual(4, got["last_round_frames"])
        self.assertEqual(4, got["last_round_images"])
        self.assertEqual(1024.5, got["last_round_payload_kb"])
        self.assertEqual(0.25, got["merge_ratio"])

    def test_capture_stats_error_does_not_hide_round_metrics(self):
        def boom():
            raise RuntimeError("queue gone")

        rt = SimpleNamespace(frames=SimpleNamespace(stats=boom),
                             last_round_frames=3, last_round_images=2,
                             last_round_payload_kb=0.0)
        got = status.capture_status(rt)
        self.assertIn("RuntimeError", got["error"])
        self.assertEqual(3, got["last_round_frames"])
        self.assertEqual(0.0, got["merge_ratio"])


class UserStatusTests(unittest.TestCase):
    def test_user_status_reports_profile_error_and_defaults(self):
        rt = SimpleNamespace(cfg={"user": {
            "id": "bad", "name": "", "persona": "",
            "error": "profile.yaml 解析失败：坏"}})
        got = status.user_status(rt)
        self.assertEqual("bad", got["id"])
        self.assertIn("解析失败", got["error"])
        # 没写就是空，不回退成任何默认名字。
        self.assertEqual("", got["name"])


class FeatureStateTests(unittest.TestCase):
    def test_real_null_objects_are_disabled_even_with_enabled_config(self):
        rt = _fake_runtime()
        rt.cfg = {
            "mic": {"enabled": True}, "capture": {"enabled": True},
            "stt": {"enabled": True}, "tts": {"enabled": True},
            "stage": {"enabled": True}, "memory": {"enabled": True},
            "game": {"enabled": True},
        }
        rt.mic.enabled = False
        rt.frames.enabled = False
        rt.stt_worker = None
        rt.speaker = SimpleNamespace(
            profile_name="none", available=False,
            snapshot=lambda: {"error": None})
        rt.memory = NullLongTerm()
        rt.brain.worldbook.enabled = False
        states = {k: v["state"] for k, v in status.features(rt).items()}
        self.assertEqual(states["mic"], "disabled")
        self.assertEqual(states["capture"], "disabled")
        self.assertEqual(states["stt"], "disabled")
        self.assertEqual(states["tts"], "disabled")
        self.assertEqual(states["stage"], "unavailable")
        self.assertEqual(states["memory"], "disabled")
        self.assertEqual(states["game"], "disabled")

    def test_stt_unavailable_is_not_reported_as_disabled(self):
        rt = _fake_runtime()
        rt.stt_worker = SimpleNamespace(status=lambda: {
            "dead": False, "disabled": False, "error": "load failed"})
        rt.stt_unavailable = True
        stt = status.features(rt)["stt"]
        self.assertTrue(stt["enabled"])
        self.assertFalse(stt["available"])
        self.assertEqual(stt["state"], "unavailable")

        rt.stt_unavailable = False
        self.assertEqual(status.features(rt)["stt"]["state"], "healthy")

        rt.stt_worker = SimpleNamespace(status=lambda: {
            "dead": True, "disabled": False, "error": None})
        self.assertEqual(status.features(rt)["stt"]["state"], "degraded")

    def test_disabled_config_does_not_hide_real_enabled_objects(self):
        rt = _fake_runtime()
        rt.cfg = {
            "mic": {"enabled": False}, "capture": {"enabled": False},
            "stt": {"enabled": False}, "tts": {"enabled": False},
            "stage": {"enabled": False}, "memory": {"enabled": False},
            "game": {"enabled": False},
        }
        rt.stt_worker = SimpleNamespace(status=lambda: {
            "dead": False, "disabled": False, "error": None})
        got = status.features(rt)
        for name in ("mic", "capture", "stt", "tts", "memory", "game"):
            self.assertTrue(got[name]["enabled"], name)
            self.assertNotEqual(got[name]["state"], "disabled", name)

    def test_worldbook_error_is_visible_in_game_feature(self):
        rt = _fake_runtime()
        rt.brain.worldbook.error = "bad.md: YAMLError: bad"
        rt.brain.worldbook_error = "bad.md: YAMLError: bad"
        game = status.features(rt)["game"]
        self.assertEqual(game["state"], "degraded")
        self.assertIn("bad.md", game["error"])

    def test_embedding_four_states(self):
        rt = _fake_runtime()
        rt.embedder = SimpleNamespace(enabled=True, state="ready", error=None)
        self.assertEqual(status.features(rt)["embedding"]["state"], "healthy")

        rt.embedder = SimpleNamespace(enabled=True, state="loading", error=None)
        self.assertEqual(status.features(rt)["embedding"]["state"], "degraded")

        rt.embedder = SimpleNamespace(enabled=True, state="unavailable",
                                      error="missing model")
        self.assertEqual(status.features(rt)["embedding"]["state"],
                         "unavailable")
        self.assertIn("missing", status.features(rt)["embedding"]["error"])

        rt.embedder = SimpleNamespace(enabled=False, state="disabled",
                                      error=None)
        self.assertEqual(status.features(rt)["embedding"]["state"], "disabled")

    def test_heat_reload_keeps_features_on_real_objects(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = runtime_cfg(td)
            for key in ("memory", "capture", "mic", "stt", "tts", "stage", "game"):
                cfg[key]["enabled"] = False
            rt = build_runtime(cfg, root=Path(td))
            newer = copy.deepcopy(cfg)
            newer["capture"]["enabled"] = True
            newer["mic"]["enabled"] = True
            newer["stt"]["enabled"] = True
            rt._reload(newer)
            got = rt.features()
            self.assertEqual(got["capture"]["state"], "disabled")
            self.assertEqual(got["mic"]["state"], "disabled")
            self.assertEqual(got["stt"]["state"], "disabled")
            self.assertFalse(got["capture"]["enabled"])


if __name__ == "__main__":
    unittest.main()
