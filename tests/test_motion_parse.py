from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sidekick.avatar.autonomy.motion_parse import (
    build_expression_mixer, expression_asset, motion_asset, parse_expression,
    parse_motion, read_pose_groups,
)
from sidekick.avatar.model import Avatar


class MotionParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "motions").mkdir()
        (self.root / "expressions").mkdir()
        motion = {
            "Meta": {"Duration": 1.8, "Loop": False},
            "Curves": [
                {"Target": "Parameter", "Id": "ParamAngleX",
                 "Segments": [0.0, 0.0, 1.0, 5.0]},
                {"Target": "Parameter", "Id": "ParamMouthForm",
                 "Segments": [0.0, 0.0, 1.0, 0.3]},
                {"Target": "PartOpacity", "Id": "PartArm",
                 "Segments": [0.0, 1.0]},
            ],
        }
        (self.root / "motions" / "wave.motion3.json").write_text(
            json.dumps(motion), encoding="utf-8")
        expression = {"Parameters": [
            {"Id": "ParamMouthForm", "Value": 1.0},
            {"Id": "ParamEyeSmile", "Value": 1.0},
        ]}
        (self.root / "expressions" / "happy.exp3.json").write_text(
            json.dumps(expression), encoding="utf-8")
        self.avatar = Avatar(
            id="x", name="x", kind="live2d", root=self.root,
            entry="x.model3.json",
            model3={"FileReferences": {
                "Motions": {"wave": [{"File": "motions/wave.motion3.json"}]},
                "Expressions": [{"Name": "开心",
                                 "File": "expressions/happy.exp3.json"}],
            }})

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_motion_params_and_duration(self):
        asset = parse_motion(self.root / "motions" / "wave.motion3.json", "wave")
        self.assertEqual(asset.duration, 1.8)
        self.assertEqual(asset.params, frozenset({
            "ParamAngleX", "ParamMouthForm"}))
        self.assertFalse(asset.loop)

    def test_parse_expression(self):
        asset = parse_expression(
            self.root / "expressions" / "happy.exp3.json", "开心")
        self.assertEqual(asset.params, frozenset({
            "ParamMouthForm", "ParamEyeSmile"}))
        self.assertEqual(asset.values,
                         (("ParamMouthForm", 1.0, "Add"),
                          ("ParamEyeSmile", 1.0, "Add")))

    def test_curve_range_and_discrete_flag(self):
        path = self.root / "motions" / "steps.motion3.json"
        path.write_text(json.dumps({
            "Meta": {"Duration": 1.0},
            "Curves": [{
                "Target": "Parameter", "Id": "Clothes1",
                "Segments": [0.0, 0.0, 2, 0.5, 1.0, 2, 1.0, 0.0],
            }],
        }), encoding="utf-8")
        asset = parse_motion(path, "steps")
        curve = asset.curves[0]
        self.assertEqual((curve.minimum, curve.maximum), (0.0, 1.0))
        self.assertTrue(curve.discrete)
        self.assertEqual(asset.top_params, ("Clothes1",))

    def test_bezier_segment_uses_real_endpoint(self):
        path = self.root / "motions" / "curve.motion3.json"
        path.write_text(json.dumps({
            "Meta": {},
            "Curves": [{
                "Target": "Parameter", "Id": "ParamAngleX",
                "Segments": [0.0, 0.0, 1, 0.344, 0.0, 0.689, 0.5,
                             1.033, 0.5],
            }],
        }), encoding="utf-8")
        asset = parse_motion(path, "curve")
        self.assertAlmostEqual(1.033, asset.duration, places=3)
        self.assertEqual(0.5, asset.curves[0].maximum)
        self.assertEqual(0.0, asset.curves[0].minimum)

    def test_expression_mixer_loads_named_assets(self):
        mixer = build_expression_mixer(
            self.avatar, ["开心"], {"ParamMouthForm": (-1, 1, 0)})
        self.assertTrue(mixer.play("开心"))
        self.assertIn("开心", mixer.assets)

    def test_pose_groups_are_read(self):
        (self.root / "x.pose3.json").write_text(json.dumps({
            "Groups": [{"Id": "outfit", "PartIds": ["PartSleeveA",
                                                   "PartSleeveB"]}],
        }), encoding="utf-8")
        self.assertEqual(read_pose_groups(self.root / "x.model3.json"),
                         [])  # model3 文件不存在时安全返回
        (self.root / "x.model3.json").write_text(json.dumps({
            "FileReferences": {"Pose": "x.pose3.json"},
        }), encoding="utf-8")
        self.assertEqual(read_pose_groups(self.root / "x.model3.json"),
                         [["PartSleeveA", "PartSleeveB"]])

    def test_asset_lookup_uses_model3_refs(self):
        motion, error = motion_asset(self.avatar, "wave")
        self.assertEqual(error, "")
        self.assertEqual(motion.params, frozenset({
            "ParamAngleX", "ParamMouthForm"}))
        expr, error = expression_asset(self.avatar, "开心")
        self.assertEqual(error, "")
        self.assertIn("ParamEyeSmile", expr.params)

    def test_missing_and_escaping_files_are_rejected(self):
        motion, error = motion_asset(self.avatar, "nope")
        self.assertIsNone(motion)
        self.assertIn("找不到", error)
        self.avatar.model3["FileReferences"]["Motions"]["bad"] = [
            {"File": "../../outside.motion3.json"}]
        motion, error = motion_asset(self.avatar, "bad")
        self.assertIsNone(motion)
        self.assertIn("找不到", error)


if __name__ == "__main__":
    unittest.main()
