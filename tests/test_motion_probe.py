from __future__ import annotations

import unittest

import numpy as np

from sidekick.motion.introspect import introspect
from sidekick.motion.probe import ProbeUnavailable, bounding_box, probe_model
from sidekick.motion.profile import profile_from_dossier
from sidekick.motion.types import ParamSpec


class FakeSource:
    def __init__(self, data):
        self.data = data

    def param_ids(self):
        return list(self.data)

    def param_info(self, param_id):
        return self.data[param_id]


class FakeModel:
    """三个 Drawable + 五个参数，位移规则固定，足以验证质心和死区。"""

    ranges = {
        "ParamArmR": (0.0, 1.0, 0.0),
        "ParamArmL": (0.0, 1.0, 0.0),
        "ParamPerspR": (0.0, 1.0, 0.0),
        "ParamPerspL": (0.0, 1.0, 0.0),
        "ParamDead": (0.0, 1.0, 0.0),
        "ParamStatic": (0.0, 1.0, 0.0),
    }

    def __init__(self):
        self.values = {pid: row[2] for pid, row in self.ranges.items()}
        self.draws = 0

    def GetParamIds(self):
        return list(self.ranges)

    def GetParamMinById(self, pid):
        return self.ranges[pid][0]

    def GetParamMaxById(self, pid):
        return self.ranges[pid][1]

    def GetParamDefaultById(self, pid):
        return self.ranges[pid][2]

    def SetParamById(self, pid, value):
        self.values[pid] = float(value)

    def GetDrawableIds(self):
        return ["right", "left", "static"]

    def Draw(self):
        self.draws += 1

    def GetDrawableVertices(self, index):
        if index == 0:
            shift = 0.6 * self.values["ParamArmR"] + 0.05 * self.values["ParamPerspR"]
            return [0.30 + shift, 0.40, 0.40 + shift, 0.40, 0.35 + shift, 0.60]
        if index == 1:
            shift = -0.6 * self.values["ParamArmL"] - 0.05 * self.values["ParamPerspL"]
            return [-0.30 + shift, 0.40, -0.40 + shift, 0.40, -0.35 + shift, 0.60]
        dead = max(0.0, (self.values["ParamDead"] - 0.3) / 0.7) * 0.3
        return [-0.05, -0.10 + dead, 0.05, -0.10 + dead, 0.0, 0.0 + dead]


class ProbeTests(unittest.TestCase):
    def test_probe_reports_side_and_ignores_static(self):
        model = FakeModel()
        result = probe_model(model, ["ParamArmR", "ParamArmL", "ParamStatic"])
        self.assertEqual(result["ParamArmR"]["screen_side"], "right")
        self.assertEqual(result["ParamArmL"]["screen_side"], "left")
        self.assertGreater(result["ParamArmR"]["cx"], 0.2)
        self.assertLess(result["ParamArmL"]["cx"], -0.2)
        self.assertFalse(result["ParamStatic"]["active_vertices"])
        self.assertEqual(result["ParamStatic"]["moved_drawables"], 0)

    def test_probe_measures_dead_zone_ratio(self):
        result = probe_model(FakeModel(), ["ParamDead"])
        self.assertTrue(result["ParamDead"]["active_vertices"])
        self.assertEqual(result["ParamDead"]["q_ratio"], 0.0)

    def test_bounding_box_uses_percentiles(self):
        points = [np.array([[x, 0.0] for x in np.linspace(-1.0, 1.0, 101)])]
        points.append(np.array([[100.0, 100.0]]))
        box = bounding_box(points)
        self.assertLess(box.width, 3.0)
        self.assertGreater(box.width, 1.8)

    def test_gamma_zero_crossing_is_bounded(self):
        spec = ParamSpec("p", "head_x", -30.0, 30.0, 0.0, gamma=0.58)
        raw0 = spec.to_raw(0.0)
        raw_small = spec.to_raw(0.02)
        raw_knee = spec.to_raw(0.08)
        self.assertAlmostEqual(raw0, 0.0)
        self.assertGreater(raw_small, raw0)
        self.assertLess(raw_small, raw_knee)
        self.assertLess(raw_small, 3.0)

    def test_gamma_expands_small_input(self):
        linear = ParamSpec("p", "head_x", -30.0, 30.0, 0.0)
        curved = ParamSpec("p", "head_x", -30.0, 30.0, 0.0, gamma=0.5)
        self.assertAlmostEqual(linear.to_raw(0.25), 7.5)
        self.assertGreater(curved.to_raw(0.25), linear.to_raw(0.25))
        self.assertAlmostEqual(curved.to_raw(0.0), 0.0)
        self.assertAlmostEqual(curved.to_raw(1.0), 30.0)

    def test_introspect_uses_probe_to_pair_perspective_arms(self):
        source = FakeSource({
            "Param36": (-1.0, 1.0, 0.0),
            "Param62": (-1.0, 1.0, 0.0),
            "Param39": (-1.0, 1.0, 0.0),
            "Param59": (-1.0, 1.0, 0.0),
        })
        probe = {
            "Param36": {"active_vertices": True, "cx": 0.74, "screen_side": "right"},
            "Param62": {"active_vertices": True, "cx": -0.81, "screen_side": "left"},
            "Param39": {"active_vertices": True, "cx": 0.73, "screen_side": "right"},
            "Param59": {"active_vertices": True, "cx": -0.79, "screen_side": "left"},
        }
        profile = introspect(
            source, probe=probe,
            display_channels={
                "Param36": "arm_r", "Param62": "arm_l",
                # 故意把两个同名透视都先绑到右侧。
                "Param39": "arm_r_persp", "Param59": "arm_r_persp",
            })
        self.assertEqual(profile.by_channel["arm_l_persp"].id, "Param59")
        self.assertEqual(profile.by_channel["arm_r_persp"].id, "Param39")
        self.assertEqual(profile.screen_side["Param59"], "left")

    def test_introspect_sets_gamma_for_dead_zone(self):
        source = FakeSource({"ParamAngleX": (-30.0, 30.0, 0.0)})
        probe = {
            "ParamAngleX": {
                "active_vertices": True, "q_ratio": 0.05,
                "span_ratio": 0.2, "screen_side": "center",
            },
        }
        profile = introspect(source, probe=probe)
        self.assertEqual(profile.by_channel["head_x"].gamma, 0.58)
        self.assertGreater(
            profile.by_channel["head_x"].to_raw(0.25), 7.5)

    def test_profile_from_dossier_keeps_gamma_and_side(self):
        profile = profile_from_dossier({
            "params": [{
                "id": "ParamAngleX", "channel": "head_x",
                "min": -30, "max": 30, "default": 0,
                "gamma": 0.58,
            }],
            "screen_side": {"ParamAngleX": "center"},
        })
        self.assertEqual(profile.by_channel["head_x"].gamma, 0.58)
        self.assertEqual(profile.screen_side["ParamAngleX"], "center")

    def test_bounding_box_rejects_empty_vertices(self):
        with self.assertRaises(ProbeUnavailable):
            bounding_box([])

    def test_probe_rejects_model_without_drawables(self):
        class Empty(FakeModel):
            def GetDrawableIds(self):
                return []

            def Draw(self):
                self.draws += 1

        with self.assertRaises(ProbeUnavailable):
            probe_model(Empty(), ["ParamArmR"])


if __name__ == "__main__":
    unittest.main()
