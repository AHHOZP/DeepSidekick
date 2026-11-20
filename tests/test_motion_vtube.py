from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sidekick.motion.introspect import introspect
from sidekick.motion.profile import profile_from_dossier
from sidekick.motion.types import ParamSpec
from sidekick.motion.vtube import parse_vtube_config


class FakeSource:
    def __init__(self, data):
        self.data = data

    def param_ids(self):
        return list(self.data)

    def param_info(self, param_id):
        return self.data[param_id]


def write_model(root: Path, settings: list[dict], hotkeys: list[dict],
                model: str = "test.model3.json") -> Path:
    path = root / model
    path.write_text(json.dumps({
        "FileReferences": {
            "Model": model,
            "IdleAnimation": "idle.motion3.json",
        },
    }), encoding="utf-8")
    (root / "test.vtube.json").write_text(json.dumps({
        "FileReferences": {
            "Model": model,
            "IdleAnimation": "idle.motion3.json",
        },
        "ParameterSettings": settings,
        "Hotkeys": hotkeys,
    }, ensure_ascii=False), encoding="utf-8")
    return path


class VTubeParseTests(unittest.TestCase):
    def test_reads_output_channel_and_hotkeys(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = write_model(root, [
                {
                    "Input": "FaceAngleX",
                    "InputRangeLower": -30, "InputRangeUpper": 30,
                    "OutputRangeLower": -30, "OutputRangeUpper": 30,
                    "OutputLive2D": "ParamAngleX",
                    "Smoothing": 15,
                },
                {
                    "Input": "EyeRightX",
                    "InputRangeLower": -0.6, "InputRangeUpper": 0.6,
                    "OutputRangeLower": 1.0, "OutputRangeUpper": -1.0,
                    "OutputLive2D": "ParamEyeBallX",
                },
                {
                    "Input": "FaceAngleY",
                    "InputRangeLower": 40, "InputRangeUpper": -40,
                    "OutputRangeLower": -30, "OutputRangeUpper": 30,
                    "OutputLive2D": "ParamBodyAngleY",
                },
            ], [
                {"Name": "害羞", "Action": "ToggleExpression",
                 "File": "害羞.exp3.json", "Folder": "表情"},
                {"Name": "挥手", "Action": "TriggerAnimation",
                 "File": "wave.motion3.json", "Folder": "动作"},
            ])
            evidence = parse_vtube_config(model)
        self.assertIsNotNone(evidence)
        assert evidence is not None
        by_output = evidence.by_output()
        self.assertEqual(by_output["ParamAngleX"][0].channel, "head_x")
        self.assertFalse(by_output["ParamAngleX"][0].inverted)
        self.assertTrue(by_output["ParamEyeBallX"][0].inverted)
        self.assertTrue(by_output["ParamBodyAngleY"][0].inverted)
        self.assertEqual(len(evidence.hotkeys), 2)
        self.assertEqual(evidence.idle, "idle.motion3.json")

    def test_missing_or_broken_file_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = root / "x.model3.json"
            model.write_text("{}", encoding="utf-8")
            self.assertIsNone(parse_vtube_config(model))
            (root / "x.vtube.json").write_text("{", encoding="utf-8")
            self.assertIsNone(parse_vtube_config(model))


class VTubeBindingTests(unittest.TestCase):
    def test_vtube_evidence_binds_numeric_parameter_with_polarity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = write_model(root, [
                {
                    "Input": "EyeRightX",
                    "InputRangeLower": -1, "InputRangeUpper": 1,
                    "OutputRangeLower": 1, "OutputRangeUpper": -1,
                    "OutputLive2D": "Param99",
                },
            ], [])
            evidence = parse_vtube_config(model)
        self.assertIsNotNone(evidence)
        profile = introspect(
            FakeSource({"Param99": (-1.0, 1.0, 0.0)}),
            vtube=evidence)
        spec = profile.by_channel["eye_x"]
        self.assertEqual(spec.id, "Param99")
        self.assertEqual(spec.polarity, -1.0)
        self.assertIn("vtube", spec.evidence)
        self.assertGreaterEqual(spec.confidence, 0.55)
        self.assertAlmostEqual(spec.to_raw(1.0), -1.0)
        self.assertAlmostEqual(spec.to_raw(-1.0), 1.0)

    def test_dossier_round_trip_keeps_polarity_and_effective_range(self):
        dossier = {
            "params": [{
                "id": "Param99", "channel": "eye_x", "kind": "bipolar",
                "min": -1.0, "max": 1.0, "default": 0.0,
                "polarity": -1.0, "effective_min": -0.5,
                "effective_max": 0.5, "confidence": 0.92,
                "evidence": ["vtube"],
            }],
            "by_channel": {"eye_x": "Param99"},
        }
        profile = profile_from_dossier(dossier)
        spec = profile.by_channel["eye_x"]
        self.assertEqual(spec.polarity, -1.0)
        self.assertEqual(spec.effective_bounds, (-0.5, 0.5))
        self.assertAlmostEqual(spec.to_raw(1.0), -0.5)

    def test_vtube_narrow_effective_range_limits_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = write_model(root, [
                {
                    "Input": "FaceAngleX",
                    "InputRangeLower": -30, "InputRangeUpper": 30,
                    "OutputRangeLower": -12, "OutputRangeUpper": 12,
                    "OutputLive2D": "ParamAngleX",
                },
            ], [])
            evidence = parse_vtube_config(model)
        profile = introspect(
            FakeSource({"ParamAngleX": (-30.0, 30.0, 0.0)}),
            vtube=evidence)
        spec = profile.by_channel["head_x"]
        self.assertEqual(spec.effective_bounds, (-12.0, 12.0))
        self.assertAlmostEqual(spec.to_raw(1.0), 12.0)
        self.assertAlmostEqual(spec.to_raw(-1.0), -12.0)

    def test_vtube_wide_range_is_clamped_to_moc3(self):
        spec = ParamSpec(
            "p", "head_x", -10.0, 10.0, 0.0,
            effective_min=-30.0, effective_max=30.0)
        self.assertEqual(spec.effective_bounds, (-10.0, 10.0))
        self.assertAlmostEqual(spec.to_raw(1.0), 10.0)


if __name__ == "__main__":
    unittest.main()
