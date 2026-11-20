from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidekick.avatar.autonomy.audit import build_dossier, run_audit
from sidekick.avatar.model import Avatar


class FakeModel:
    ranges = {
        "ParamAngleX": (-30, 30, 0),
        "ParamEyeLOpen": (0, 1, 1),
        "ParamEyeROpen": (0, 1, 1),
        "ParamMouthOpenY": (0, 1, 0),
        "ParamBodyAngleZ": (-10, 10, 0),
        "Param12": (0, 1, 0),
    }

    def GetParamIds(self):
        return list(self.ranges)

    def GetParamMinByIndex(self, index):
        return self.ranges[list(self.ranges)[index]][0]

    def GetParamMaxByIndex(self, index):
        return self.ranges[list(self.ranges)[index]][1]

    def GetParamDefaultByIndex(self, index):
        return self.ranges[list(self.ranges)[index]][2]

    def GetCanvasSize(self):
        return 1.0, 1.5

    def GetPartIds(self):
        return ["PartHead", "PartBody"]


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "motions").mkdir()
        (self.root / "motions" / "idle.motion3.json").write_text(json.dumps({
            "Meta": {"Duration": 2.0, "Loop": True},
            "Curves": [{"Target": "Parameter", "Id": "ParamAngleX",
                        "Segments": [0.0, 0.0, 1.0, 5.0]}],
        }), encoding="utf-8")
        (self.root / "expressions").mkdir()
        (self.root / "expressions" / "happy.exp3.json").write_text(json.dumps({
            "Parameters": [{"Id": "ParamMouthOpenY", "Value": 1.0}]}),
            encoding="utf-8")
        self.model3 = {
            "Version": 3,
            "FileReferences": {
                "Motions": {"idle": [{"File": "motions/idle.motion3.json"}]},
                "Expressions": [{"Name": "开心",
                                 "File": "expressions/happy.exp3.json"}],
                "Physics": "model.physics3.json",
                "Pose": "model.pose3.json",
            },
            "Groups": [
                {"Target": "Parameter", "Name": "EyeBlink",
                 "Ids": ["ParamEyeLOpen", "ParamEyeROpen"]},
                {"Target": "Parameter", "Name": "LipSync",
                 "Ids": ["ParamMouthOpenY"]},
            ],
        }
        self.avatar = Avatar(
            id="abc123", name="test", root=self.root,
            entry="test.model3.json", model3=self.model3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dossier_is_compact_and_relative(self):
        data = build_dossier(self.avatar, FakeModel(), self.model3)
        self.assertEqual(data["avatar_id"], "abc123")
        self.assertEqual(data["model"]["canvas"], [1.0, 1.5])
        self.assertEqual(data["tier"], "core")
        self.assertEqual(data["by_channel"]["head_x"], "ParamAngleX")
        self.assertEqual(data["unmapped"]["count"], 1)
        self.assertTrue(data["physics"]["present"])
        self.assertTrue(data["pose"]["present"])
        self.assertEqual(data["groups"]["LipSync"], ["ParamMouthOpenY"])
        self.assertEqual(data["motions"][0]["param_count"], 1)
        self.assertEqual(data["expressions"][0]["param_count"], 1)
        self.assertIsNone(data["proportions"]["head_ratio"])
        self.assertNotIn(str(self.root), json.dumps(data, ensure_ascii=False))

    def test_persistent_expression_records_controls(self):
        model = FakeModel()
        model.ranges["Clothes1"] = (0.0, 1.0, 1.0)
        (self.root / "expressions" / "裙子切换.exp3.json").write_text(
            json.dumps({"Parameters": [
                {"Id": "Clothes1", "Value": -1.0, "Blend": "Add"},
            ]}), encoding="utf-8")
        self.model3["FileReferences"]["Expressions"].append({
            "Name": "裙子切换", "File": "expressions/裙子切换.exp3.json",
        })
        data = build_dossier(self.avatar, model, self.model3)
        row = next(x for x in data["expressions"] if x["name"] == "裙子切换")
        self.assertEqual(row["kind"], "persistent")
        self.assertEqual(row["controls"][0]["value"], 0.0)
        self.assertEqual(row["controls"][0]["default"], 1.0)
        self.assertIn("持久换装/配件表情", " ".join(data["warnings"]))

    def test_unmapped_sample_prefers_known_words(self):
        model = FakeModel()
        model.ranges["ParamWeird"] = (0, 1, 0)
        model.ranges["ParamTailAngleX"] = (0, 1, 0)
        data = build_dossier(self.avatar, model, self.model3)
        self.assertEqual(data["unmapped"]["sample"][0], "ParamTailAngleX")
        self.assertIn("Param12", data["unmapped"]["sample"])

    def test_dossier_records_probe_and_screen_side(self):
        probe = {
            "ParamAngleX": {
                "active_vertices": True, "cx": 0.0, "cy": 0.9,
                "span_ratio": 0.1, "q_ratio": 0.25,
                "screen_side": "center",
            },
        }
        data = build_dossier(self.avatar, FakeModel(), self.model3,
                             probe=probe)
        self.assertTrue(data["probe"]["available"])
        self.assertEqual(data["probe"]["params"]["ParamAngleX"]["cx"], 0.0)
        self.assertEqual(data["screen_side"]["ParamAngleX"], "center")
        head = next(row for row in data["params"] if row["id"] == "ParamAngleX")
        self.assertEqual(head["screen_side"], "center")

    def test_dossier_marks_virtual_physics_inputs(self):
        (self.root / "test.model3.json").write_text(
            json.dumps(self.model3), encoding="utf-8")
        (self.root / "model.physics3.json").write_text(json.dumps({
            "PhysicsSettings": [{
                "Input": [{"Source": {"Id": "ParamAngleX"}}],
                "Output": [{"Destination": {"Id": "ParamBodyAngleZ"}}],
            }],
        }), encoding="utf-8")
        data = build_dossier(
            self.avatar, FakeModel(), self.model3,
            probe={"ParamAngleX": {"active_vertices": False}})
        self.assertTrue(data["probe"]["params"]["ParamAngleX"]["virtual_input"])
        self.assertEqual(data["physics"]["links"], [{
            "output": "ParamBodyAngleZ", "inputs": ["ParamAngleX"]}])

    def test_dossier_keeps_probe_error_when_unavailable(self):
        data = build_dossier(self.avatar, FakeModel(), self.model3,
                             probe_error="ProbeUnavailable: no display")
        self.assertFalse(data["probe"]["available"])
        self.assertIn("no display", data["probe"]["error"])
        self.assertEqual(data["screen_side"], {})

    def test_run_audit_adds_root_to_subprocess_pythonpath(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            out = Path(cmd[cmd.index("--out") + 1])
            out.write_text(json.dumps({"avatar_id": "x"}), encoding="utf-8")
            captured.update(kwargs)
            return mock.Mock(returncode=0)

        with mock.patch(
                "sidekick.avatar.autonomy.audit.subprocess.run",
                side_effect=fake_run), mock.patch.dict(
                os.environ, {"PYTHONPATH": "site-packages"}, clear=False):
            data = run_audit(self.root, self.root / "x.model3.json")
        self.assertEqual(data["avatar_id"], "x")
        parts = captured["env"]["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(parts[0], str(self.root.resolve()))
        self.assertIn("site-packages", parts)

    def test_audit_cache_skips_second_subprocess(self):
        def fake_run(cmd, **kwargs):
            out = Path(cmd[cmd.index("--out") + 1])
            out.write_text(json.dumps({"avatar_id": "x"}), encoding="utf-8")
            return mock.Mock(returncode=0)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = root / "x.model3.json"
            model.write_text(json.dumps({
                "FileReferences": {"Moc": "x.moc3"}}), encoding="utf-8")
            (root / "x.moc3").write_bytes(b"MOC3")
            with mock.patch(
                    "sidekick.avatar.autonomy.audit.subprocess.run",
                    side_effect=fake_run):
                first = run_audit(root, model)
            self.assertEqual("x", first["avatar_id"])
            with mock.patch(
                    "sidekick.avatar.autonomy.audit.subprocess.run",
                    side_effect=AssertionError("cache miss")):
                second = run_audit(root, model)
            self.assertEqual(first, second)

    def test_audit_cache_invalidates_when_expression_changes(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            out = Path(cmd[cmd.index("--out") + 1])
            out.write_text(json.dumps({"avatar_id": "x", "n": len(calls)}),
                           encoding="utf-8")
            return mock.Mock(returncode=0)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = root / "x.model3.json"
            model.write_text(json.dumps({
                "FileReferences": {"Moc": "x.moc3"}}), encoding="utf-8")
            (root / "x.moc3").write_bytes(b"MOC3")
            with mock.patch(
                    "sidekick.avatar.autonomy.audit.subprocess.run",
                    side_effect=fake_run):
                run_audit(root, model)
                (root / "x.exp3.json").write_text('{"a": 1}', encoding="utf-8")
                second = run_audit(root, model)
        self.assertEqual(2, len(calls))
        self.assertEqual(2, second["n"])


if __name__ == "__main__":
    unittest.main()
