"""桌宠渲染器契约：两个实现都是 BasePetModel 子类，空对象同签名。"""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from sidekick.desktop.image import ImageRenderer
from sidekick.desktop.pet_model import (
    PART_BODY, PART_HAND, PART_HEAD, BasePetModel, part_from_names,
)
from sidekick.desktop.live2d import Live2DRenderer
from sidekick.desktop.spine import SpineRenderer


class _MinimalModel(BasePetModel):
    """只实现抽象方法的最小实现，用来验证基类默认方法都能安全调用。"""

    def close(self) -> None:
        return None

    def update(self, dt: float = 1 / 60) -> None:
        return None

    def draw(self) -> None:
        return None

    def resize(self, width: int, height: int) -> None:
        return None

    def set_scale(self, scale: float) -> None:
        return None

    def set_offset(self, x: float, y: float) -> None:
        return None

    def hit_part(self, x: float, y: float) -> str:
        return ""

    def start_motion(self, group: str) -> bool:
        return False

    @property
    def motion_groups(self):
        return ()

    @property
    def idle(self) -> str:
        return ""


class HeadAngleGuardTests(unittest.TestCase):
    def test_single_frame_head_spike_is_damped(self):
        class Model:
            def __init__(self):
                self.values = [0.0]

            def GetParamValueByIndex(self, index):
                return self.values[index]

            def SetParamByIndex(self, index, value):
                self.values[index] = float(value)

        model = Model()
        renderer = object.__new__(Live2DRenderer)
        renderer.model = model
        renderer._head_guard_indices = ((0, 60.0),)
        renderer._head_prev = {0: 0.0}
        renderer._head_vel = {0: 0.0}
        renderer._follow_target = None
        model.values[0] = 5.0
        renderer._guard_head_angle_spikes()
        self.assertAlmostEqual(model.values[0], 1.75)


class TailPhysicsGuardTests(unittest.TestCase):
    def test_single_frame_tail_spike_is_damped(self):
        class Model:
            def __init__(self):
                self.values = [0.0]

            def GetParamValueByIndex(self, index):
                return self.values[index]

            def SetParamByIndex(self, index, value):
                self.values[index] = float(value)

        model = Model()
        renderer = object.__new__(Live2DRenderer)
        renderer.model = model
        renderer._physics_out_indices = ((0, 60.0),)
        renderer._tail_prev = {0: 0.0}
        renderer._tail_vel = {0: 0.0}
        model.values[0] = 5.0
        renderer._guard_tail_physics_spikes()
        self.assertAlmostEqual(model.values[0], 1.75)
        # 记录未过滤的原始值，避免下一帧把 clip 结果当新起点继续锁死。
        self.assertAlmostEqual(renderer._tail_prev[0], 5.0)

    def test_guard_does_not_latch_after_one_spike(self):
        class Model:
            def __init__(self):
                self.values = [0.0]

            def GetParamValueByIndex(self, index):
                return self.values[index]

            def SetParamByIndex(self, index, value):
                self.values[index] = float(value)

        model = Model()
        renderer = object.__new__(Live2DRenderer)
        renderer.model = model
        renderer._physics_out_indices = ((0, 60.0),)
        renderer._tail_prev = {0: 0.0}
        renderer._tail_vel = {0: 0.0}
        for value in (5.0, 0.0, 0.0, 0.0):
            model.values[0] = value
            renderer._guard_tail_physics_spikes()
        self.assertAlmostEqual(model.values[0], 0.0)
        self.assertAlmostEqual(renderer._tail_prev[0], 0.0)


class Live2DPhysicsSaveTests(unittest.TestCase):
    def test_apply_controls_never_saves_physics_outputs(self):
        calls = []

        class Model:
            def SetParamById(self, pid, value):
                calls.append(("param", pid, value))

            def SetSaveParamById(self, pid, value):
                calls.append(("save", pid, value))

        renderer = object.__new__(Live2DRenderer)
        renderer.model = Model()
        renderer.param_ids = {"Cascade", "Pure"}
        renderer.part_ids = []
        renderer.remote_parts = {}
        renderer.remote_params = {"Cascade": 1.0, "Pure": 2.0}
        renderer.profile = SimpleNamespace(
            physics_inputs={"Cascade", "Pure"},
            physics_outputs={"Cascade"})
        renderer.apply_controls()
        self.assertEqual(
            calls,
            [("param", "Cascade", 1.0), ("param", "Pure", 2.0),
             ("save", "Pure", 2.0)])

    def test_follow_pose_never_saves_physics_outputs(self):
        calls = []

        class Model:
            def SetParamById(self, pid, value):
                calls.append(("param", pid, value))

            def SetSaveParamById(self, pid, value):
                calls.append(("save", pid, value))

        renderer = object.__new__(Live2DRenderer)
        renderer.model = Model()
        renderer.width = 100
        renderer.height = 100
        renderer._follow_target = (1.0, 0.0)
        renderer._follow_pointer = (0.0, 0.0)
        renderer.profile = SimpleNamespace(
            by_channel={"head_x": SimpleNamespace(
                id="ParamAngleX", to_raw=lambda value: value)},
            physics_outputs={"ParamAngleX"})
        renderer._apply_follow_pose(1 / 60)
        self.assertEqual([c[0] for c in calls], ["param"])

class DesktopGuardAndFollowTests(unittest.TestCase):
    def test_remote_param_is_not_clipped_by_tail_guard(self):
        calls = []

        class Model:
            def GetParamValueByIndex(self, index):
                return 50.0

            def SetParamByIndex(self, index, value):
                calls.append((index, value))

        renderer = object.__new__(Live2DRenderer)
        renderer.model = Model()
        renderer._physics_out_indices = ((0, 60.0),)
        renderer._tail_prev = {0: 0.0}
        renderer._tail_vel = {0: 0.0}
        renderer._param_order = ("ParamAngleX",)
        renderer.remote_params = {"ParamAngleX": 50.0}
        renderer._guard_tail_physics_spikes()
        self.assertEqual([], calls)
        self.assertNotIn(0, renderer._tail_prev)

    def test_reset_drag_smoothly_returns_follow_pose_to_zero(self):
        calls = []

        class Model:
            def SetParamById(self, pid, value):
                calls.append((pid, value))

            def SetSaveParamById(self, pid, value):
                pass

        renderer = object.__new__(Live2DRenderer)
        renderer.model = Model()
        renderer.width = 100
        renderer.height = 100
        renderer._motion_host = SimpleNamespace(enabled=False)
        renderer.profile = SimpleNamespace(
            by_channel={"head_x": SimpleNamespace(
                id="ParamAngleX", to_raw=lambda value: value)},
            physics_outputs=set())
        renderer._follow_target = (1.0, 0.0)
        renderer._follow_pointer = (1.0, 0.0)
        renderer.reset_drag()
        self.assertEqual(renderer._follow_target, (0.0, 0.0))
        for _ in range(60):
            renderer._apply_follow_pose(0.05)
        self.assertIsNone(renderer._follow_target)
        self.assertLess(abs(calls[-1][1]), 0.002)

    def test_spine_offset_updates_native_placement(self):
        placed = []

        class Inst:
            def set_offset(self, x, y):
                placed.append((x, y))

        renderer = object.__new__(SpineRenderer)
        renderer.width = 200
        renderer.height = 100
        renderer.scale = 1.0
        renderer.offset_x = 0.0
        renderer.offset_y = 0.0
        renderer._ref = (0.0, 0.0, 100.0, 100.0)
        renderer._inst = Inst()
        renderer.set_offset(10.0, -20.0)
        origin_x, origin_y = renderer._origin
        self.assertEqual(placed[-1], (origin_x + 10.0, origin_y - 20.0))


class Live2DResetControlsTests(unittest.TestCase):
    def test_control_view_uses_normalized_offset(self):
        calls = []

        class Model:
            def SetOffset(self, x, y):
                calls.append((x, y))

        renderer = object.__new__(Live2DRenderer)
        renderer.model = Model()
        renderer.width = 800
        renderer.height = 600
        renderer.scale = 1.0
        renderer.offset_x = 0.0
        renderer.offset_y = 0.0
        renderer.control_view(off_x=0.01, off_y=-0.02)
        self.assertEqual(calls, [(0.01, -0.02)])

    def test_reset_controls_clears_overrides_and_restores_view(self):
        calls = []

        class Model:
            def StopAllMotions(self):
                calls.append("stop")

            def ResetExpression(self):
                calls.append("expr")

            def SetScale(self, value):
                calls.append(("scale", value))

            def SetOffset(self, x, y):
                calls.append(("offset", x, y))

            def StartRandomMotion(self, group, priority):
                calls.append(("idle", group, priority))

        renderer = object.__new__(Live2DRenderer)
        renderer.model = Model()
        renderer.autonomy_enabled = False
        renderer._idle = "idle"
        renderer._motion_counts = {"idle": 1}
        renderer.remote_params = {"ParamAngleX": 1.0}
        renderer.remote_parts = {"PartArmL": 0.2}
        renderer._expression_until = 123.0
        renderer.scale = 2.0
        renderer.offset_x = 10.0
        renderer.offset_y = 20.0
        renderer.reset_controls()
        self.assertEqual(renderer.remote_params, {})
        self.assertEqual(renderer.remote_parts, {})
        self.assertEqual(renderer.scale, 1.0)
        self.assertEqual((renderer.offset_x, renderer.offset_y), (0.0, 0.0))
        self.assertIn("stop", calls)
        self.assertIn("expr", calls)
        self.assertIn("idle", [c[0] for c in calls if isinstance(c, tuple)])
        self.assertIn(("scale", 1.0), calls)
        self.assertIn(("offset", 0.0, 0.0), calls)


class Live2DExpressionResetTests(unittest.TestCase):
    def test_non_autonomy_expiry_resets_sdk_expression(self):
        calls = []
        renderer = object.__new__(Live2DRenderer)
        renderer.autonomy_enabled = False
        renderer._expression_mixer = SimpleNamespace(
            clear=lambda: calls.append("mixer"))
        renderer._expression_until = time.monotonic() - 1.0
        renderer.model = SimpleNamespace(
            ResetExpression=lambda: calls.append("reset"),
            Update=lambda dt: None)
        renderer.update(0.0)
        self.assertEqual(["reset"], calls)

    def test_autonomy_expiry_clears_mixer_not_sdk(self):
        calls = []
        renderer = object.__new__(Live2DRenderer)
        renderer.autonomy_enabled = True
        renderer._expression_mixer = SimpleNamespace(
            clear=lambda: calls.append("mixer"))
        renderer._expression_until = time.monotonic() - 1.0
        renderer.model = SimpleNamespace(
            ResetExpression=lambda: calls.append("reset"),
            Update=lambda dt: None)
        renderer.update(0.0)
        self.assertEqual(["mixer"], calls)


class DesktopModelContractTests(unittest.TestCase):
    def test_both_renderers_implement_the_contract(self):
        for cls in (Live2DRenderer, ImageRenderer):
            self.assertTrue(issubclass(cls, BasePetModel), cls)
            self.assertEqual(set(cls.__abstractmethods__), set(), cls)

    def test_default_methods_are_no_ops(self):
        model = _MinimalModel()
        model.set_mouth(0.5)
        model.look_at(10, 20)
        model.apply_acts([{"kind": "motion", "name": "wave"}])
        model.apply_controls()
        model.control_param("ParamAngleX", 0.5)
        model.control_part("PartArmL", 0.0)
        model.control_motion("wave", 1)
        model.control_expression("开心")
        model.control_view(1.2, 0.1, -0.2)
        model.reset_controls()
        model.reset_drag()
        self.assertEqual(model.start_motion("wave"), False)

    def test_manual_motion_ignores_autoplay_policy(self):
        renderer = object.__new__(Live2DRenderer)
        renderer._motion_counts = {"wave": 1}
        renderer._idle = "idle"
        renderer._motion_enabled = lambda _group: False
        renderer._claim_motion = lambda *_a, **_k: None
        renderer._clear_expression = lambda: None
        class _Model:
            called = False
            def StartRandomMotion(self, *_a):
                self.called = True
        renderer.model = _Model()
        self.assertTrue(renderer.start_motion("wave"))
        self.assertTrue(renderer.model.called)

    def test_part_normalisation(self):
        self.assertEqual(part_from_names(["head"]), PART_HEAD)
        self.assertEqual(part_from_names(["front-arm"]), PART_HAND)
        self.assertEqual(part_from_names(["torso"]), PART_BODY)
        self.assertEqual(part_from_names([]), "")
        self.assertEqual(part_from_names(["unknown"]), PART_BODY)


if __name__ == "__main__":
    unittest.main()
