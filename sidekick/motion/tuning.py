"""可调参数：默认值、安全范围、合成后的统一应用点。

`motion_profile.json` 只保存差异；运行时先由自省得到模型事实，再把这里的
值覆盖上去。所有字段在一个地方声明范围，WebUI/schema/校验共用。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields

from .types import Pose, clamp


#: 键 → (默认值, 安全下限, 安全上限)。
TUNING_RANGES: dict[str, tuple[float, float, float]] = {
    "head_gain": (1.05, 0.55, 1.45),
    "head_roll_gain": (1.10, 0.50, 1.50),
    "body_gain": (1.00, 0.50, 1.50),
    "eye_gain": (1.00, 0.70, 1.30),
    "mouth_open_gain": (1.00, 0.60, 1.50),
    "mouth_form_gain": (1.00, 0.60, 1.40),
    "blink_scale": (1.00, 0.70, 1.30),
    "breath_scale": (1.10, 0.50, 1.60),
    "spring_scale": (1.00, 0.70, 1.60),
    "action_scale": (1.10, 0.60, 1.50),
    "gaze_limit": (0.95, 0.70, 1.00),
    "arm_gain": (1.05, 0.60, 1.50),
    "leg_gain": (1.00, 0.50, 1.50),
}

#: 低于这个值的 body_gain 是 V1 的“注视身体分摊”旧语义，不是全局倍率。
_LEGACY_BODY_GAIN = 0.40


@dataclass(frozen=True)
class MotionTuning:
    head_gain: float = 1.05
    head_roll_gain: float = 1.10
    body_gain: float = 1.00
    eye_gain: float = 1.00
    mouth_open_gain: float = 1.00
    mouth_form_gain: float = 1.00
    blink_scale: float = 1.00
    breath_scale: float = 1.10
    spring_scale: float = 1.00
    action_scale: float = 1.10
    gaze_limit: float = 0.95
    arm_gain: float = 1.05
    leg_gain: float = 1.00

    @classmethod
    def from_dict(cls, data: dict | None) -> tuple["MotionTuning", list[str]]:
        """从 profile 读 tuning；越界值收敛并返回 warning。"""
        warnings: list[str] = []
        values = {}
        for field in fields(cls):
            default, low, high = TUNING_RANGES[field.name]
            raw = (data or {}).get(field.name, default)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                warnings.append(f"tuning.{field.name} 不是数字，已用默认值")
                value = default
            if not math.isfinite(value):
                warnings.append(f"tuning.{field.name} 不是有限数字，已用默认值")
                value = default
            if field.name == "body_gain" and value < _LEGACY_BODY_GAIN:
                warnings.append(
                    f"tuning.body_gain={value:g} 低于 {low:g}，已用默认值")
                value = default
            clamped = clamp(value, low, high)
            if clamped != value:
                warnings.append(
                    f"tuning.{field.name}={value:g} 超出 {low:g}..{high:g}，已收敛")
            values[field.name] = clamped
        return cls(**values), warnings

    def to_dict(self) -> dict:
        return asdict(self)

    def merge(self, data: dict | None) -> "MotionTuning":
        parsed, _ = self.from_dict({**self.to_dict(), **(data or {})})
        return parsed

def apply_tuning(pose: Pose, tuning: MotionTuning) -> Pose:
    """把 profile 的增益作用在最终语义姿态上，只作用一次。

    ``body_gain`` 与 ``head_gain`` 一样是全局倍率（1.0 = 引擎原始幅度）。
    注视层自己只负责“视线分多少给身体”，不再在这个倍率里重复计算。
    """
    out = pose.copy()
    for channel in ("head_x", "head_y"):
        out.add(channel, out.get(channel) * (tuning.head_gain - 1.0))
    out.add("head_z", out.get("head_z") * (tuning.head_roll_gain - 1.0))
    for channel in ("body_x", "body_y", "body_z", "lean_y",
                    "chest_x", "chest_y", "skirt_x", "skirt_y", "tail",
                    "sleeve_l_x", "sleeve_l_y", "sleeve_r_x", "sleeve_r_y",
                    "ribbon_l_x", "ribbon_l_y", "ribbon_r_x", "ribbon_r_y"):
        out.add(channel, out.get(channel) * (tuning.body_gain - 1.0))
    for channel in ("eye_x", "eye_y"):
        out.add(channel, out.get(channel) * (tuning.eye_gain - 1.0))
    out.set("mouth_open", clamp(out.get("mouth_open") * tuning.mouth_open_gain,
                                0.0, 1.0))
    out.set("mouth_form", clamp(out.get("mouth_form") * tuning.mouth_form_gain,
                                -1.0, 1.0))
    for channel in ("arm_l", "arm_r", "arm_l_bend", "arm_r_bend",
                    "hand_l", "hand_r", "shoulder_l", "shoulder_r"):
        out.add(channel, out.get(channel) * (tuning.arm_gain - 1.0))
    for channel in ("leg_l", "leg_r", "leg_l_x", "leg_r_x",
                    "knee_l", "knee_r", "ankle_l", "ankle_r",
                    "leg_l_y", "leg_r_y", "knee_l_y", "knee_r_y",
                    "foot_l_y", "foot_r_y", "foot_l", "foot_r",
                    "foot_swing", "hip"):
        out.add(channel, out.get(channel) * (tuning.leg_gain - 1.0))
    return out
