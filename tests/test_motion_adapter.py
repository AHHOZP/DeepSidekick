from __future__ import annotations

import unittest

from sidekick.motion.adapters.live2d_py import (
    Live2DMotionHost, Live2DParamSource, Live2DWriter,
)


class FakeModel:
    def __init__(self):
        self.values = {
            "ParamAngleX": 0.0, "ParamEyeLOpen": 1.0,
            "ParamMouthOpenY": 0.0,
        }
        self.ranges = {
            "ParamAngleX": (-30.0, 30.0, 0.0),
            "ParamEyeLOpen": (0.0, 1.0, 1.0),
            "ParamMouthOpenY": (0.0, 1.0, 0.0),
        }
        self.prepared = []
        self.motions_stopped = False
        self.saved = {}

    def GetParamIds(self):
        return list(self.values)

    def GetParamMinByIndex(self, index):
        return self.ranges[list(self.values)[index]][0]

    def GetParamMaxByIndex(self, index):
        return self.ranges[list(self.values)[index]][1]

    def GetParamDefaultByIndex(self, index):
        return self.ranges[list(self.values)[index]][2]

    def SetParamByIndex(self, index, value):
        self.values[list(self.values)[index]] = float(value)

    def GetParamValueByIndex(self, index):
        return self.values[list(self.values)[index]]

    def SetSaveParamByIndex(self, index, value):
        self.saved[list(self.values)[index]] = float(value)

    def SetAutoBreath(self, value):
        self.prepared.append(("breath", value))

    def SetAutoBlink(self, value):
        self.prepared.append(("blink", value))

    def StopAllMotions(self):
        self.motions_stopped = True

    def Update(self, dt):
        return None


class AdapterTests(unittest.TestCase):
    def test_source_reads_ranges(self):
        source = Live2DParamSource(FakeModel())
        self.assertEqual(set(source.param_ids()), {
            "ParamAngleX", "ParamEyeLOpen", "ParamMouthOpenY"})
        self.assertEqual(source.param_info("ParamAngleX"), (-30.0, 30.0, 0.0))
        profile = source.profile()
        self.assertEqual(profile.by_channel["head_x"].id, "ParamAngleX")

    def test_prepare_disables_auto_and_keeps_assets(self):
        model = FakeModel()
        writer = Live2DWriter(model, Live2DParamSource(model).profile())
        writer.prepare()
        self.assertIn(("breath", False), model.prepared)
        self.assertIn(("blink", False), model.prepared)
        self.assertFalse(model.motions_stopped)

    def test_physics_inputs_are_saved_for_the_next_update(self):
        model = FakeModel()
        profile = Live2DParamSource(model).profile()
        profile.physics_inputs = {"ParamAngleX"}
        writer = Live2DWriter(model, profile)
        writer.write({"ParamAngleX": 12.0})
        self.assertEqual(model.saved["ParamAngleX"], 12.0)

    def test_physics_output_is_not_written_to_saved_values(self):
        model = FakeModel()
        profile = Live2DParamSource(model).profile()
        profile.physics_inputs = {"ParamAngleX"}
        profile.physics_outputs = {"ParamAngleX"}
        writer = Live2DWriter(model, profile)
        writer.write({"ParamAngleX": 12.0})
        self.assertNotIn("ParamAngleX", model.saved)

    def test_writer_skips_unknown_and_counts_failure(self):
        model = FakeModel()

        def boom(_index, _value):
            raise RuntimeError("bad parameter")

        writer = Live2DWriter(model, Live2DParamSource(model).profile())
        self.assertEqual(writer.write({"ParamAngleX": 3.0, "Nope": 1.0}), 1)
        model.SetParamByIndex = boom
        self.assertEqual(writer.write({"ParamAngleX": 4.0}), 0)
        self.assertEqual(writer.failed, 1)

    def test_host_reports_and_writes_after_tick(self):
        model = FakeModel()
        profile = Live2DParamSource(model).profile()
        host = Live2DMotionHost(model, profile, seed=5)
        params = host.tick(0.0, 0.0)
        self.assertTrue(params)
        self.assertEqual(host.write(params), len(params))
        report = host.report()
        self.assertTrue(report["active"])
        self.assertEqual(report["tier"], "face")

    def test_profile_ownership_crossfade_is_applied(self):
        model = FakeModel()
        profile = Live2DParamSource(model).profile()
        profile.motion_policy = {"ownership": {"crossfade_ms": 500}}
        host = Live2DMotionHost(model, profile, seed=5)
        self.assertAlmostEqual(host.ownership.crossfade_s, 0.5)


if __name__ == "__main__":
    unittest.main()
