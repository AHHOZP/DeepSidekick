"""live2d-py 模型包装：加载、动作、表情、程序化姿态、口型、点击部位。"""
from __future__ import annotations

import json
import math
import time
from collections.abc import Collection
from pathlib import Path

import live2d

from sidekick.avatar.autonomy.motion_parse import (
    build_expression_mixer, motion_asset,
)
from sidekick.avatar.autonomy import store as profile_store
from sidekick.avatar.autonomy import validate as profile_validate
from sidekick.avatar.autonomy.schema import SCHEMA_VERSION
from sidekick.avatar.model import Avatar
from sidekick.desktop.pet_model import BasePetModel, part_from_names
from sidekick.motion import apply_overrides
from sidekick.motion.adapters.live2d_py import Live2DParamSource, Live2DMotionHost
from sidekick.motion.null import NULL_MOTION_HOST
from sidekick.motion.gaze import pointer_target
from sidekick.motion.probe import ProbeUnavailable, probe_model

#: 表情没有时长；先让模型 expression 拥有自己的参数一段时间，再交还引擎。
EXPRESSION_HOLD_S = 6.0


def _side_probe_ids(profile) -> list[str]:
    """桌面加载期只探少量参数：左右手臂配对和鼠标注视原点。"""
    ids: list[str] = []
    for channel in ("eye_x", "eye_l_open", "head_x", "head_y",
                    "arm_l", "arm_r", "arm_l_persp", "arm_r_persp"):
        spec = profile.by_channel.get(channel)
        if spec is not None and spec.id not in ids:
            ids.append(spec.id)
    return ids


def _part_labels(avatar: Avatar) -> dict[str, str]:
    try:
        model3 = avatar.model3
        if not isinstance(model3, dict):
            model3 = json.loads(
                (Path(avatar.root) / avatar.entry).read_text(encoding="utf-8"))
        rel = (model3.get("FileReferences") or {}).get("DisplayInfo")
        if not rel:
            return {}
        data = json.loads(
            (Path(avatar.root) / str(rel)).read_text(encoding="utf-8"))
        return {
            str(item.get("Id")): str(item.get("Name") or "")
            for item in (data.get("Parts") or [])
            if isinstance(item, dict) and item.get("Id")
        }
    except (OSError, ValueError, TypeError):
        return {}


class Live2DRenderer(BasePetModel):
    def __init__(self, avatar: Avatar, width: int, height: int, *,
                 autonomy: bool = True) -> None:
        self.avatar = avatar
        self.autonomy_enabled = bool(autonomy)
        self.model = live2d.Model()
        path = Path(avatar.root) / avatar.entry
        # 用服务端补全过的 model3（含 Animation/Exp3 虚拟引用），而不是直接读原文件；
        # 否则 live2d-py 看不到从目录自动补的 Motions/Expressions。
        if isinstance(avatar.model3, dict):
            data = json.dumps(avatar.model3, ensure_ascii=False, indent=2)
            self.model.LoadFromJsonString(
                data, create_renderer=False, root_path=str(avatar.root))
        else:
            self.model.LoadModelJson(str(path), create_renderer=False)
        self.model.CreateRenderer(128)
        self.model.Resize(width, height)
        self.labels = _part_labels(avatar)
        self.part_ids = [str(x) for x in (self.model.GetPartIds() or [])]
        self.remote_params: dict[str, float] = {}
        self.remote_parts: dict[str, float] = {}
        self._motion_counts = {
            str(k): int(v) for k, v in (self.model.GetMotionGroups() or {}).items()
            if int(v) > 0
        }
        self.expressions = [str(x) for x in (self.model.GetExpressions() or [])]
        self.param_ids = frozenset(str(x) for x in (self.model.GetParamIds() or []))
        # 参数自省和引擎宿主：从这一层开始，程序化层拥有未被模型资产占用的通道。
        self.profile = Live2DParamSource(
            self.model, Path(avatar.root) / avatar.entry).profile()
        self._expression_mixer = build_expression_mixer(
            avatar, self.expressions, self.profile.ranges)
        self.probe_error = ""
        if self.autonomy_enabled:
            ids = _side_probe_ids(self.profile)
            if ids:
                try:
                    measured = probe_model(self.model, ids)
                    if measured:
                        self.profile = Live2DParamSource(
                            self.model, Path(avatar.root) / avatar.entry
                        ).profile(probe=measured)
                except ProbeUnavailable as exc:
                    self.probe_error = f"几何探针不可用：{exc}"
        self.profile_data, self.profile_error = profile_store.load(avatar)
        if self.autonomy_enabled and self.profile_data is not None:
            if self.profile_data.get("schema_version") != SCHEMA_VERSION:
                self.profile_error = (
                    f"profile schema_version 不是 {SCHEMA_VERSION}，已忽略")
            else:
                clean, issues = profile_validate.validate(self.profile_data)
                apply_overrides(self.profile, clean)
                for issue in issues:
                    warning = f"profile: {issue.text()}"
                    if warning not in self.profile.warnings:
                        self.profile.warnings.append(warning)
        self._idle = str(avatar.idle or "").strip()
        has_idle = bool(self._idle and self._idle in self._motion_counts)
        if self.autonomy_enabled:
            # 程序化引擎接管后，SDK 内置 motion 不能继续抢参数。
            try:
                self.model.StopAllMotions()
            except (RuntimeError, TypeError, ValueError):
                pass
            self._motion_host = Live2DMotionHost(
                self.model, self.profile, seed=7,
                expression_mixer=self._expression_mixer)
        else:
            self._motion_host = NULL_MOTION_HOST
            # 保留模型原生呼吸激励；不要把“某一模型尾巴不动”变成特例。
            # idle 循环边界的单帧角度漏出由 draw 前的窄范围角度毛刺保护处理。
            self.model.SetAutoBreath(True)
            self.model.SetAutoBlink(True)
        self.asset_warnings: list[str] = []
        self._expression_until = 0.0
        # 口型不是必须的：没有 ParamMouthOpenY 的模型照样动、照样说话，只是嘴不动。
        # 加载时试写一次，失败就把口型关掉并把原因写进 notice（桌面报到 /status），
        # 这样每帧的 set_mouth 不用再 try —— 真出错就是真 bug，该暴露。
        self.notice = self.probe_error
        self.mouth_param = next(
            (p for p in ("ParamMouthOpenY", "ParamMouthOpen", "PARAM_MOUTH_OPEN_Y")
             if p in self.param_ids), "")
        if not self.mouth_param:
            self.notice = (self.notice + "；" if self.notice else "") + \
                "模型没有口型参数（ParamMouthOpenY），口型关闭"
        else:
            try:
                self.model.SetParamById(self.mouth_param, 0.0)
            except (RuntimeError, TypeError, ValueError) as exc:
                self.notice = (self.notice + "；" if self.notice else "") + \
                    f"口型参数写不进（{type(exc).__name__}），口型关闭"
                self.mouth_param = ""
        if self.profile_error:
            self.notice = (self.notice + "；" if self.notice else "") + \
                f"自主驱动配置读取失败：{self.profile_error}"
        # 自主驱动关闭时恢复现状：模型自带 idle motion 负责待机。
        if not self.autonomy_enabled and has_idle:
            self.model.StartRandomMotion(
                self._idle, live2d.MotionPriority.IDLE)
        self.pointer: tuple[float, float] | None = None
        self._follow_target: tuple[float, float] | None = None
        self._follow_pointer = (0.0, 0.0)
        self._smooth_dt = 1.0 / 60.0
        ordered = [str(x) for x in (self.model.GetParamIds() or [])]
        self._param_order = tuple(ordered)
        self._physics_out_indices = tuple(
            (idx, max(1.0, float(self.profile.ranges.get(pid, (-30.0, 30.0, 0.0))[1])
                      - float(self.profile.ranges.get(pid, (-30.0, 30.0, 0.0))[0])))
            for idx, pid in enumerate(ordered)
            if pid in self.profile.physics_outputs
        )
        self._tail_prev: dict[int, float] = {}
        self._tail_vel: dict[int, float] = {}
        head_ids = {
            str(self.profile.by_channel[ch].id)
            for ch in ("head_x", "head_y", "head_z")
            if ch in self.profile.by_channel
        }
        self._head_guard_indices = tuple(
            (idx, max(1.0, float(self.profile.ranges.get(pid, (-30.0, 30.0, 0.0))[1])
                      - float(self.profile.ranges.get(pid, (-30.0, 30.0, 0.0))[0])))
            for idx, pid in enumerate(ordered) if pid in head_ids
        )
        self._head_prev: dict[int, float] = {}
        self._head_vel: dict[int, float] = {}
        self.scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.width = width
        self.height = height

    @property
    def motion_host(self):
        return self._motion_host

    # ── 尺寸 / 变换 ───────────────────────────────────
    def resize(self, width: int, height: int) -> None:
        self.width, self.height = int(width), int(height)
        self.model.Resize(self.width, self.height)

    def set_scale(self, scale: float) -> None:
        self.scale = max(0.2, min(4.0, float(scale)))
        self.model.SetScale(self.scale)

    def set_offset(self, x: float, y: float) -> None:
        self.offset_x = float(x)
        self.offset_y = float(y)
        self.model.SetOffset(self.offset_x, self.offset_y)

    def look_at(self, x: float, y: float) -> None:
        """鼠标位置转成归一化指针，交给自主引擎当高权重注视目标。

        autonomy 关闭时才回退到 Cubism Drag。这里不直接写 ParamAngle*，
        避免和引擎每帧写入抢同一批参数。
        """
        if self.width > 0 and self.height > 0:
            self.pointer = pointer_target(
                x, y, self.width, self.height,
                self.profile.pointer_origin)
            self._follow_target = self.pointer
        # 不再调用 Cubism Drag：它只覆盖标准 ParamAngle*，且会和
        # update() 后的 _apply_follow_pose 形成两套头部角度打架。

    def reset_drag(self) -> None:
        """关闭鼠标跟随时把视点交还虚拟注视。"""
        self.pointer = None
        if self._motion_host.enabled:
            self._follow_target = None
            return
        # 非自主模式没有引擎回写，直接把目标平滑归零；否则头/身/眼会停在
        # 上一次鼠标位置，直到 idle 自己碰巧覆盖到。
        if self._follow_target is not None or self._follow_pointer != (0.0, 0.0):
            self._follow_target = (0.0, 0.0)

    def _apply_follow_pose(self, dt: float) -> None:
        """autonomy 关闭时补齐 SDK Drag 不覆盖的 head/body 参数。"""
        if self._follow_target is None:
            return
        tx, ty = self._follow_target
        alpha = 1.0 - math.exp(-max(0.0, float(dt)) / 0.10)
        px = self._follow_pointer[0] + (tx - self._follow_pointer[0]) * alpha
        py = self._follow_pointer[1] + (ty - self._follow_pointer[1]) * alpha
        self._follow_pointer = (px, py)
        pose = {
            "head_x": px * 0.58,
            "head_y": py * 0.58,
            "head_z": -px * 0.075,
            "body_x": px * 0.22,
            "body_y": py * 0.22,
            "body_z": px * 0.08,
            "eye_x": px * 0.90,
            "eye_y": py * 0.90,
        }
        for channel, value in pose.items():
            spec = self.profile.by_channel.get(channel)
            if spec is None:
                continue
            try:
                raw = spec.to_raw(value)
                self.model.SetParamById(spec.id, raw)
                # 下一帧 Model.Update 开头会 LoadParameters()；保存跟随值，
                # 否则 physics/动作边界会把头身相位拉回默认。
                save = getattr(self.model, "SetSaveParamById", None)
                if (save is not None
                        and spec.id not in self.profile.physics_outputs):
                    save(spec.id, raw)
            except (RuntimeError, TypeError, ValueError):
                pass
        if abs(px) + abs(py) < 0.002:
            self._follow_pointer = (0.0, 0.0)
            self._follow_target = None

    @property
    def motion_groups(self) -> Collection[str]:
        return tuple(self._motion_counts)

    @property
    def aspect(self) -> float:
        """模型画布的宽高比：桌面窗按它定形状（Cubism 的 canvas 就是设计尺寸）。"""
        try:
            width, height = self.model.GetCanvasSize()
        except (AttributeError, TypeError, ValueError):
            return 0.75
        return float(width) / float(height) if width and height else 0.75

    @property
    def idle(self) -> str:
        return self._idle

    # ── 动作 / 表情 / 口型 ─────────────────────────────
    def _claim_motion(self, group: str, index: int = 0,
                      now: float | None = None) -> None:
        asset, error = motion_asset(self.avatar, group, index)
        if asset is None:
            if error and error not in self.asset_warnings:
                self.asset_warnings.append(error)
            return
        hold = asset.duration * (2.0 if asset.loop else 1.0) + 0.2
        if self.autonomy_enabled:
            self._motion_host.claim_asset(
                asset.params, time.monotonic() if now is None else now, hold)

    def _apply_expression(self, name: str) -> None:
        if self.autonomy_enabled and self._expression_mixer is not None:
            self._expression_mixer.play(name)
            return
        if name in self.expressions:
            self._expression_until = time.monotonic() + EXPRESSION_HOLD_S
            self.model.SetExpression(name)

    def _clear_expression(self) -> None:
        self._expression_until = 0.0
        # 非自主模式虽然也建了 mixer（供资产解析），实际播放走 SDK SetExpression。
        if self.autonomy_enabled and self._expression_mixer is not None:
            self._expression_mixer.clear()
            return
        try:
            self.model.ResetExpression()
        except (RuntimeError, TypeError, ValueError):
            pass

    def _motion_enabled(self, group: str) -> bool:
        policy = getattr(self.profile, "motion_policy", None) or {}
        motions = policy.get("model_motions") if isinstance(policy, dict) else None
        if not isinstance(motions, dict):
            return True
        settings = motions.get(str(group))
        if not isinstance(settings, dict):
            return True
        return bool(settings.get("enabled", True))

    def apply_acts(self, acts: list[dict] | None) -> None:
        for act in acts or []:
            if not isinstance(act, dict):
                continue
            name = str(act.get("name") or "")
            kind = str(act.get("kind") or "")
            if kind == "expression":
                self._apply_expression(name)
            elif name in self._motion_counts and self._motion_enabled(name):
                index = int(act.get("index") or 0)
                if name == self._idle:
                    self._clear_expression()
                self._claim_motion(name, index)
                self.model.StartMotion(
                    name, index, live2d.MotionPriority.FORCE)

    def express_emotion(self, emotion: str) -> None:
        name = str(getattr(self.profile, "expressions", {}).get(emotion) or "")
        if name:
            self._apply_expression(name)

    def start_motion(self, group: str) -> bool:
        # 右键菜单/手动动作不归 motion_policy 管：那个开关只约束自主驱动的
        # 自动资产播放，否则用户点“动作”永远没反应。
        if group not in self._motion_counts:
            return False
        if group == self._idle:
            self._clear_expression()
        self._claim_motion(group)
        self.model.StartRandomMotion(
            group, live2d.MotionPriority.FORCE)
        return True

    def set_mouth(self, level: float) -> None:
        # 必须在 model.Update() 之后调用：Update 开头会 LoadParameters()，
        # 之前写入的 ParamMouthOpenY 会被清回默认值。
        if not self.mouth_param or self.mouth_param in self.remote_params:
            return
        self.model.SetParamById(
            self.mouth_param, max(0.0, min(1.0, float(level))))

    # ── WebUI 预览远程控制 ─────────────────────────────
    def control_param(self, param_id: str, value) -> None:
        if value is None:
            self.remote_params.pop(str(param_id), None)
        else:
            self.remote_params[str(param_id)] = float(value)
        # 手动覆盖变化时清掉毛刺保护的历史；下一帧从新值重新起算，
        # 避免把 Inspector 的大跳变当成模型毛刺削掉。
        self._head_prev.clear()
        self._head_vel.clear()
        self._tail_prev.clear()
        self._tail_vel.clear()

    def control_part(self, part_id: str, value) -> None:
        if value is None:
            self.remote_parts.pop(str(part_id), None)
        else:
            self.remote_parts[str(part_id)] = max(
                0.0, min(1.0, float(value)))

    def control_motion(self, group: str, index=0) -> None:
        if group in self._motion_counts:
            if group == self._idle:
                self._clear_expression()
            self._claim_motion(group, int(index or 0))
            self.model.StartMotion(
                group, int(index or 0), live2d.MotionPriority.FORCE)

    def control_expression(self, name) -> None:
        self._apply_expression(str(name))

    def control_view(self, zoom=None, off_x=None, off_y=None) -> None:
        if zoom is not None:
            self.set_scale(float(zoom))
        if off_x is not None or off_y is not None:
            # live2d-py 的 SetOffset 是投影归一化坐标（[-1,1]），不是像素；
            # Inspector 传来的 normX/normY 必须直接使用。
            self.set_offset(
                float(off_x) if off_x is not None else 0.0,
                float(off_y) if off_y is not None else 0.0)

    def reset_controls(self) -> None:
        self.remote_params.clear()
        self.remote_parts.clear()
        self._clear_expression()
        try:
            self.model.StopAllMotions()
        except (RuntimeError, TypeError, ValueError):
            pass
        # 非自主模式的待机靠 SDK 的 idle motion；复位不能把它一起停掉。
        if (not self.autonomy_enabled and self._idle
                and self._idle in self._motion_counts):
            self.model.StartRandomMotion(
                self._idle, live2d.MotionPriority.IDLE)
        self.set_scale(1.0)
        self.set_offset(0.0, 0.0)

    def apply_controls(self) -> None:
        # Inspector 是外部输入，可能传任意 id：先在已知集合里筛，不拿 try 当校验。
        for param_id, value in self.remote_params.items():
            if param_id in self.param_ids:
                self.model.SetParamById(param_id, float(value))
                # 只有**物理输入**才写 saved 值：覆盖物理输出参数的 saved 值
                # 会每帧改动物理状态，表现为头/头发局部一闪一闪。
                if (param_id in getattr(self.profile, "physics_inputs", set())
                        and param_id not in self.profile.physics_outputs):
                    set_save = getattr(self.model, "SetSaveParamById", None)
                    if set_save is not None:
                        try:
                            set_save(param_id, float(value))
                        except (RuntimeError, TypeError, ValueError):
                            pass
        for part_id, value in self.remote_parts.items():
            if part_id in self.part_ids:
                self.model.SetPartOpacity(
                    self.part_ids.index(part_id), float(value))

    def update(self, dt: float = 1 / 60) -> None:
        # 只有 SDK SetExpression 这条路径会设置 _expression_until；mixer 路径
        # 自己按 fade-out 收，不需要在这里清。
        if self._expression_until and time.monotonic() >= self._expression_until:
            self._clear_expression()
        # 固定每帧只调一次 Update；不拆子步（拆子步会让 LoadParameters/
        # SaveParameters 次数失配）。这里只平滑 dt，抑制物理显式积分的抖动。
        raw_dt = max(0.0, min(0.10, float(dt)))
        smooth = getattr(self, "_smooth_dt", 1.0 / 60.0)
        smooth += (raw_dt - smooth) * 0.25
        self._smooth_dt = smooth
        step = max(1 / 75.0, min(1 / 45.0, smooth))
        self.model.Update(step)
        if (not getattr(self, "_motion_host", NULL_MOTION_HOST).enabled
                and getattr(self, "_follow_target", None) is not None):
            self._apply_follow_pose(raw_dt)

    def _guard_head_angle_spikes(self) -> None:
        """非自主模式只削 head_x/y/z 的单帧毛刺。

        呼吸在 idle 循环边界漏出一帧角度时，这里把“单帧加速度异常且
        幅度明显”的值拉回；跟随鼠标时 head 由每帧程序化写入，跳过。
        状态记录未过滤的原始值/速度，避免 clip 后自己锁死。
        """
        if getattr(self, "_follow_target", None) is not None:
            return
        get_val = getattr(self.model, "GetParamValueByIndex", None)
        set_val = getattr(self.model, "SetParamByIndex", None)
        if get_val is None or set_val is None:
            return
        remote = set(getattr(self, "remote_params", {}))
        order = getattr(self, "_param_order", ())
        for idx, span in getattr(self, "_head_guard_indices", ()):
            if 0 <= idx < len(order) and order[idx] in remote:
                self._head_prev.pop(idx, None)
                self._head_vel.pop(idx, None)
                continue
            try:
                raw = float(get_val(idx))
            except (RuntimeError, TypeError, ValueError):
                continue
            prev = self._head_prev.get(idx)
            if prev is not None:
                diff = raw - prev
                prev_v = self._head_vel.get(idx, 0.0)
                if (abs(diff - prev_v) > span * 0.08
                        and abs(diff) > span * 0.06):
                    clipped = prev + max(-span * 0.035,
                                         min(span * 0.035, diff * 0.35))
                    try:
                        set_val(idx, clipped)
                    except (RuntimeError, TypeError, ValueError):
                        pass
                # 记录未过滤的原始值和速度；否则 clip 后会把自己当成新的
                # 起点，下一帧继续越界，形成持续改写。
                self._head_vel[idx] = diff
            self._head_prev[idx] = raw

    def _guard_tail_physics_spikes(self) -> None:
        """只削物理输出（尾巴等）的单帧毛刺；记录原始值，避免持续改写。"""
        get_val = getattr(self.model, "GetParamValueByIndex", None)
        set_val = getattr(self.model, "SetParamByIndex", None)
        if get_val is None or set_val is None:
            return
        remote = set(getattr(self, "remote_params", {}))
        order = getattr(self, "_param_order", ())
        for idx, span in getattr(self, "_physics_out_indices", ()):
            if 0 <= idx < len(order) and order[idx] in remote:
                self._tail_prev.pop(idx, None)
                self._tail_vel.pop(idx, None)
                continue
            try:
                raw = float(get_val(idx))
            except (RuntimeError, TypeError, ValueError):
                continue
            prev = self._tail_prev.get(idx)
            if prev is not None:
                diff = raw - prev
                prev_v = self._tail_vel.get(idx, 0.0)
                if (abs(diff - prev_v) > span * 0.08
                        and abs(diff) > span * 0.06):
                    clipped = prev + max(-span * 0.035,
                                         min(span * 0.035, diff * 0.35))
                    try:
                        set_val(idx, clipped)
                    except (RuntimeError, TypeError, ValueError):
                        pass
                self._tail_vel[idx] = diff
            self._tail_prev[idx] = raw

    def draw(self) -> None:
        if not getattr(self, "_motion_host", NULL_MOTION_HOST).enabled:
            self._guard_head_angle_spikes()
        self._guard_tail_physics_spikes()
        self.model.Draw()

    # ── 点击部位 ──────────────────────────────────────
    def hit_part(self, x: float, y: float) -> str:
        """返回“头”/“手”/“身子”；透明处返回空串。"""
        ids = self.model.HitPart(float(x), float(y), True) or []
        if not ids:
            return ""
        return part_from_names([self.labels.get(str(pid), "") for pid in ids])

    def close(self) -> None:
        # 退出路径：renderer 可能没建过、模型也可能已经处于半销毁状态，
        # 这里再抛异常只会盖住真正的退出原因，所以吞掉。
        try:
            self.model.DestroyRenderer()
        except (RuntimeError, TypeError, ValueError):
            pass
