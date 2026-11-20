"""Semantic actions, choreography, and graceful degradation.

An action is a short additive pose with a four-stage envelope: anticipation
(the body moves the opposite way before the gesture), an accelerated attack
with a small overshoot, a breathing hold, and a release that lets the spring
carry the momentum.  Multi-joint actions also declare per-channel loops with
phase offsets, so the shoulder leads the upper arm, the forearm lags, and the
wrist trails last: that is the difference between a wave and a stick rotating
at the shoulder.

If a model has no arm parameters, an arm action is not discarded.  Each action
declares a *degradation* mapping (for example ``arm_r -> body_z + head_z``), so
the same intent still reads as a body gesture on a face-only model.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from .noise import fbm
from .rng import Rng
from .types import ModelProfile, Pose, clamp


@dataclass(frozen=True)
class ActionSpec:
    name: str
    pose: dict[str, float]
    aliases: tuple[str, ...] = ()
    duration: float = 1.8
    attack: float = 0.18
    hold: float = 0.85
    release: float = 0.55
    #: 反向蓄力幅度；0 关闭。
    anticipation: float = 0.16
    #: channel -> (幅度, 频率 Hz, 相位 rad)；用来做肩→臂→腕的鞭打滞后。
    loops: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    degrade: dict[str, dict[str, float]] = field(default_factory=dict)
    #: 动作触发时请求的离散状态：状态名 -> 是否激活。
    states: dict[str, bool] = field(default_factory=dict)
    #: 动作期间直接写模型原始参数（profile 自定义配方）。
    raw_overrides: dict[str, float] = field(default_factory=dict)
    #: 动作触发后持久保持的原始参数；由 StateController 跨帧写回。
    persistent_raw: dict[str, float] = field(default_factory=dict)
    priority: float = 1.0
    cooldown: float = 3.0

    @property
    def total(self) -> float:
        return max(0.12, float(self.duration))

    def normalized_timings(self) -> tuple[float, float, float]:
        """Return attack/hold/release that fit inside ``duration``."""
        attack = max(0.04, min(float(self.attack), self.total * 0.45))
        release = max(0.05, min(float(self.release), self.total * 0.55))
        hold = max(0.02, self.total - attack - release)
        return attack, hold, release


def choreo_envelope(t: float, attack: float, hold: float, release: float,
                    anticipation: float = 0.16) -> float:
    """Four-stage envelope with anticipation and an 8% attack overshoot."""
    t = max(0.0, float(t))
    attack = max(1e-4, float(attack))
    hold = max(0.0, float(hold))
    release = max(1e-4, float(release))
    if t >= attack + hold + release:
        return 0.0
    pre = min(0.11, attack * 0.32)
    if t < pre:
        return -float(anticipation) * math.sin(math.pi * t / pre)
    if t < attack:
        u = clamp((t - pre) / max(1e-6, attack - pre), 0.0, 1.0)
        return u * u * (3.0 - 2.0 * u) + 0.08 * math.sin(math.pi * u)
    if t < attack + hold:
        return 1.0 + 0.035 * math.sin(2.0 * math.pi * 1.2 * (t - attack))
    u = clamp((t - attack - hold) / release, 0.0, 1.0)
    return 1.0 - u * u * (3.0 - 2.0 * u)


#: The built-in vocabulary.  It is intentionally small, legible, and easy for
#: an LLM to choose from.  Extra actions can be added in the JSON manifest.
DEFAULT_ACTIONS: dict[str, ActionSpec] = {
    "wave": ActionSpec(
        "wave",
        # 默认用角色右手（屏幕左，即模型的 Param62/L 大臂z）。
        # 只有另一侧手臂的模型由 resolve_for() 自动镜像。
        {"arm_l": 0.62, "arm_l_bend": 0.35, "hand_l": 0.45, "finger_l": 0.30,
         "arm_l_persp": 0.38, "shoulder_l": 0.34, "arm_gate": 1.0,
         "head_z": 0.36, "head_x": 0.16, "body_z": 0.24, "body_x": 0.12,
         "body_y": 0.06, "mouth_form": 0.35,
         "eye_l_smile": 0.30, "eye_r_smile": 0.30},
        ("挥手", "拜拜", "打招呼", "hello", "hi", "wave", "bye"),
        duration=1.94, attack=0.24, hold=1.25, release=0.45,
        loops={
            "shoulder_l": (0.10, 2.4, 0.0),
            "arm_l": (0.06, 2.4, -0.45),
            "arm_l_bend": (0.38, 2.4, -1.15),
            "hand_l": (0.45, 2.4, -1.80),
            "finger_l": (0.25, 2.4, -2.05),
            "arm_l_persp": (0.10, 2.4, -1.40),
            "head_z": (0.12, 2.4, -0.60),
            "body_z": (0.09, 2.4, -0.95),
        },
        degrade={"arm_l": {"body_z": 0.46, "head_z": 0.34, "body_x": 0.30},
                 "arm_l_bend": {"body_z": 0.18, "head_z": 0.12},
                 "hand_l": {"arm_l_bend": 0.75},
                 "shoulder_l": {"body_y": 0.18, "body_z": 0.14}},
        cooldown=5.0, priority=2,
    ),
    "nod": ActionSpec(
        "nod",
        {"head_y": -0.32, "head_z": 0.10, "head_x": 0.04,
         "body_y": -0.18, "body_x": 0.06,
         "arm_r": 0.03, "arm_l": 0.02,
         "shoulder_l": -0.02, "shoulder_r": -0.02},
        ("点头", "同意", "嗯", "nod", "yes"),
        duration=1.05, attack=0.18, hold=0.55, release=0.32,
        loops={"head_y": (0.32, 3.0, 0.0), "body_y": (0.16, 3.0, -0.55),
               "head_z": (0.08, 1.5, -0.40), "arm_r": (0.10, 3.0, -0.70)},
        degrade={}, cooldown=1.2, priority=1,
    ),
    "shake": ActionSpec(
        "shake",
        {"head_x": 0.0, "body_x": 0.04, "body_z": 0.04,
         "arm_l": 0.05, "arm_r": 0.05},
        ("摇头", "不对", "不是", "shake", "no"),
        duration=1.12, attack=0.16, hold=0.62, release=0.34,
        loops={"head_x": (0.50, 3.1, 0.0), "head_z": (0.20, 3.1, -0.50),
               "body_x": (0.20, 3.1, -0.65), "hand_r": (0.24, 3.1, -0.40),
               "hand_l": (0.24, 3.1, -0.40)},
        degrade={}, cooldown=1.5, priority=1,
    ),
    "tilt": ActionSpec(
        "tilt",
        {"head_z": 0.54, "head_x": 0.16, "head_y": 0.06,
         "body_z": -0.22, "body_x": 0.10, "body_y": 0.04,
         "shoulder_r": 0.22, "shoulder_l": -0.12,
         "arm_r": 0.06, "brow_r_y": 0.22},
        ("歪头", "疑惑", "tilt", "head tilt"),
        duration=1.45, attack=0.26, hold=0.75, release=0.42,
        loops={"head_z": (0.08, 1.4, 0.0), "hand_r": (0.12, 1.4, -0.6)},
        degrade={"head_z": {"body_z": 0.30}},
        cooldown=3.0, priority=1,
    ),
    "think": ActionSpec(
        "think",
        {"head_x": -0.22, "head_y": 0.08, "head_z": 0.12,
         "body_x": -0.08, "body_y": 0.05,
         "eye_x": -0.40, "eye_y": 0.34,
         "brow_l_y": 0.18, "brow_r_y": 0.10,
         "brow_l_angle": 0.20, "brow_r_angle": 0.08,
         "arm_r": 0.10, "arm_r_bend": 0.14, "hand_r": 0.10,
         "mouth_form": -0.10},
        ("思考", "想想", "琢磨", "think", "hmm"),
        duration=3.0, attack=0.38, hold=1.9, release=0.72,
        loops={"head_z": (0.05, 0.8, 0.0), "hand_r": (0.08, 1.2, -0.5)},
        degrade={"eye_x": {"head_x": -0.20}, "eye_y": {"head_y": 0.16},
                 "arm_r": {"body_z": 0.16, "head_z": 0.12}},
        cooldown=7.0, priority=1,
    ),
    "shrug": ActionSpec(
        "shrug",
        {"shoulder_l": 0.72, "shoulder_r": 0.72,
         "arm_l": 0.55, "arm_r": 0.55,
         "arm_l_bend": 0.60, "arm_r_bend": 0.60,
         "hand_l": 0.50, "hand_r": 0.50,
         "finger_l": 0.30, "finger_r": 0.30,
         "head_z": 0.30, "head_y": -0.08, "body_y": 0.16,
         "brow_l_y": 0.35, "brow_r_y": 0.35, "mouth_form": -0.12},
        ("耸肩", "不知道", "shrug", "dunno"),
        duration=1.55, attack=0.24, hold=0.72, release=0.52,
        loops={"hand_l": (0.14, 2.0, 0.0), "hand_r": (0.14, 2.0, 0.3)},
        degrade={"shoulder_l": {"body_y": 0.20, "body_z": 0.12},
                 "shoulder_r": {"body_y": 0.20, "body_z": 0.12},
                 "arm_l": {"body_z": 0.20, "head_z": 0.12},
                 "arm_r": {"body_z": 0.20, "head_z": 0.12}},
        cooldown=5.0, priority=1,
    ),
    "laugh": ActionSpec(
        "laugh",
        {"body_x": 0.10, "body_y": 0.20, "body_z": 0.10,
         "head_y": 0.18, "head_z": 0.16,
         "mouth_open": 0.30, "mouth_form": 0.62,
         "eye_l_smile": 0.75, "eye_r_smile": 0.75, "cheek": 0.50,
         "arm_l": 0.10, "arm_r": 0.12,
         "finger_l": 0.10, "finger_r": 0.10,
         "shoulder_l": 0.06, "shoulder_r": 0.07},
        ("笑", "大笑", "哈哈", "laugh", "lol"),
        duration=1.75, attack=0.20, hold=1.05, release=0.50,
        loops={"body_y": (0.28, 3.6, 0.0), "shoulder_l": (0.20, 3.6, -0.2),
               "shoulder_r": (0.20, 3.6, -0.2), "head_y": (0.22, 3.6, -0.45),
               "head_z": (0.12, 1.8, -0.6), "arm_l": (0.16, 3.6, -0.5),
               "arm_r": (0.16, 3.6, -0.5)},
        degrade={}, cooldown=4.0, priority=1,
    ),
    "point": ActionSpec(
        "point",
        {"arm_r": 0.86, "arm_r_bend": 0.18, "hand_r": 0.30, "finger_r": 0.88,
         "arm_r_persp": 0.40, "hand_style_point": 1.0, "hand_gate": 1.0,
         "head_x": 0.16, "head_z": 0.06,
         "eye_x": 0.40, "body_x": 0.08, "body_z": 0.06,
         "mouth_form": 0.10},
        ("指", "那边", "看这个", "point", "look"),
        duration=1.75, attack=0.20, hold=0.85, release=0.55,
        loops={"hand_r": (0.10, 2.0, -0.4),
               "finger_r": (0.08, 2.0, -0.5)},
        degrade={"arm_r": {"body_x": 0.34, "head_x": 0.24, "eye_x": 0.20},
                 "arm_r_bend": {"body_x": 0.10},
                 "finger_r": {"hand_r": 0.75}},
        cooldown=4.0, priority=2,
    ),
    "clap": ActionSpec(
        "clap",
        {"arm_l": 0.68, "arm_r": 0.68, "arm_l_bend": 0.78, "arm_r_bend": 0.78,
         "hand_l": 0.35, "hand_r": 0.35,
         "finger_l": 0.20, "finger_r": 0.20,
         "body_x": 0.04, "body_y": 0.10, "head_y": 0.10, "head_z": 0.16,
         "eye_l_smile": 0.50, "eye_r_smile": 0.50, "mouth_form": 0.40},
        ("鼓掌", "拍手", "clap", "applause"),
        duration=1.38, attack=0.18, hold=0.82, release=0.38,
        loops={"arm_l": (0.26, 4.2, 0.0), "arm_r": (0.26, 4.2, 0.25),
               "hand_l": (0.35, 4.2, -0.4), "hand_r": (0.35, 4.2, -0.15),
               "body_y": (0.14, 4.2, -0.5), "head_z": (0.12, 2.1, 0.0)},
        degrade={"arm_l": {"body_x": 0.18, "body_z": 0.18},
                 "arm_r": {"body_x": 0.18, "body_z": 0.18}},
        cooldown=4.0, priority=2,
    ),
    "kick": ActionSpec(
        "kick",
        {"leg_r": 1.0, "leg_r_x": 0.10, "knee_r": 0.55, "ankle_r": 0.35,
         "leg_r_y": 0.55, "foot_r_y": 0.20, "foot_r": 0.35,
         "hip": 0.40, "body_y": -0.18, "body_x": -0.10,
         "body_z": 0.18, "skirt_x": 0.25, "skirt_y": 0.15,
         "head_z": 0.12, "head_y": -0.06,
         "arm_l": 0.04, "arm_r": 0.05},
        ("踢腿", "踢", "kick"),
        duration=1.30, attack=0.20, hold=0.38, release=0.62,
        loops={"leg_r": (0.22, 2.6, 0.0), "leg_r_y": (0.12, 2.6, -0.2),
               "knee_r": (0.18, 2.6, -0.1), "ankle_r": (0.12, 2.6, -0.3),
               "foot_r_y": (0.08, 2.6, -0.4), "foot_r": (0.10, 2.6, -0.5),
               "body_z": (0.10, 2.6, -0.5),
               "head_z": (0.08, 2.6, -0.7)},
        degrade={"leg_r": {"leg_l": 1.0, "hip": 0.40, "body_x": 0.34,
                           "body_z": 0.26, "head_z": 0.16},
                 "leg_r_x": {"leg_r": 0.35, "hip": 0.15},
                 "knee_r": {"leg_r": 0.55, "hip": 0.15},
                 "ankle_r": {"leg_r": 0.35, "foot_r": 0.30},
                 "leg_r_y": {"leg_r": 0.45, "hip": 0.20},
                 "foot_r_y": {"foot_r": 0.40, "leg_r": 0.15},
                 "foot_r": {"leg_r": 0.25}},
        cooldown=3.0, priority=2,
    ),
    "leg_shift": ActionSpec(
        "leg_shift",
        {"leg_l": 0.24, "leg_r": -0.18, "leg_l_x": 0.06, "leg_r_x": -0.04,
         "knee_l": 0.12, "knee_r": -0.10, "ankle_l": -0.10, "ankle_r": 0.08,
         "leg_l_y": 0.10, "leg_r_y": -0.08, "foot_l_y": 0.06,
         "foot_r_y": -0.05, "foot_l": 0.12, "foot_r": -0.08,
         "foot_swing": 0.10, "hip": 0.12, "body_z": 0.08, "head_z": -0.04},
        ("换重心", "挪腿", "活动腿", "腿动", "跺脚", "leg shift", "shift weight"),
        duration=2.6, attack=0.35, hold=1.55, release=0.70,
        loops={"leg_l": (0.10, 1.0, 0.0), "leg_r": (0.10, 1.0, -0.3),
               "knee_l": (0.08, 1.0, -0.2), "knee_r": (0.08, 1.0, -0.5),
               "ankle_l": (0.06, 1.0, -0.4), "ankle_r": (0.06, 1.0, -0.7),
               "foot_l": (0.06, 1.0, -0.2), "foot_r": (0.06, 1.0, -0.5),
               "foot_swing": (0.08, 1.0, 0.0)},
        degrade={"leg_l": {"body_z": 0.24, "body_x": 0.14},
                 "leg_r": {"body_z": 0.24, "body_x": 0.14},
                 "knee_l": {"leg_l": 0.40}, "knee_r": {"leg_r": 0.40},
                 "ankle_l": {"leg_l": 0.30}, "ankle_r": {"leg_r": 0.30},
                 "foot_l_y": {"foot_l": 0.40}, "foot_r_y": {"foot_r": 0.40},
                 "foot_l": {"leg_l": 0.20}, "foot_r": {"leg_r": 0.20},
                 "foot_swing": {"foot_l": 0.50, "foot_r": 0.50}},
        cooldown=6.0, priority=1,
    ),
    "lean": ActionSpec(
        "lean",
        {"lean_y": 0.95, "body_y": 0.38, "body_z": 0.18, "body_x": 0.06,
         "chest_y": 0.35, "skirt_y": 0.25,
         "head_z": -0.14, "head_y": -0.18,
         "arm_l": 0.04, "arm_r": 0.03, "shoulder_l": 0.03, "shoulder_r": 0.02},
        ("靠", "前倾", "向前倾", "凑近", "lean", "lean in"),
        duration=2.0, attack=0.38, hold=1.0, release=0.62,
        loops={"body_z": (0.04, 0.8, 0.0)},
        degrade={"body_z": {"head_z": 0.22}},
        cooldown=6.0, priority=1,
    ),
    "stretch": ActionSpec(
        "stretch",
        {"arm_l": 0.58, "arm_r": 0.62, "arm_l_bend": 0.65, "arm_r_bend": 0.65,
         "arm_l_persp": 0.30, "arm_r_persp": 0.30,
         "body_y": 0.45, "body_x": 0.08, "head_y": 0.28, "head_z": -0.22,
         "chest_y": 0.35, "shoulder_l": 0.28, "shoulder_r": 0.28,
         "eye_l_open": -0.55, "eye_r_open": -0.55, "mouth_open": 0.25},
        ("伸懒腰", "伸展", "stretch"),
        duration=2.75, attack=0.55, hold=1.3, release=0.80,
        degrade={"arm_l": {"body_y": 0.30, "body_x": 0.18},
                 "arm_r": {"body_y": 0.30, "body_x": 0.18}},
        cooldown=90.0, priority=1,
    ),
    "hand_chest": ActionSpec(
        "hand_chest",
        {"hand_style_chest": 1.0, "arm_l": 0.72, "arm_l_bend": 0.88,
         "hand_l": 0.55, "arm_r": 0.18, "head_z": -0.22, "head_y": -0.10,
         "body_y": 0.16, "eye_l_smile": 0.45, "eye_r_smile": 0.45},
        ("手放胸口", "抚胸", "拍胸口", "捂胸口", "按住胸口", "安心",
         "放心交给我", "心跳", "hand on chest"),
        duration=3.5, attack=0.30, hold=2.45, release=0.55,
        loops={"body_y": (0.08, 1.5, 0.0), "hand_l": (0.10, 1.5, -0.4)},
        degrade={"arm_l": {"body_z": 0.20, "head_z": 0.14},
                 "arm_l_bend": {"body_z": 0.10},
                 "hand_l": {"arm_l_bend": 0.60},
                 "hand_style_chest": {}},
        cooldown=4.0, priority=2,
    ),
    "wag_tail": ActionSpec(
        "wag_tail",
        {"tail": 0.35, "hip": 0.22, "body_z": 0.14, "head_z": -0.16,
         "skirt_x": 0.18, "eye_l_smile": 0.40, "eye_r_smile": 0.40},
        ("摇尾巴", "晃尾巴", "晃晃尾巴", "尾巴摇摇", "甩尾巴", "动动尾巴",
         "wag tail"),
        duration=2.6, attack=0.22, hold=1.75, release=0.55,
        loops={"tail": (0.82, 2.6, 0.0), "hip": (0.28, 2.6, -0.5),
               "skirt_x": (0.24, 2.6, -0.7), "body_z": (0.14, 2.6, -0.9)},
        degrade={"tail": {"hip": 0.55, "body_z": 0.35, "skirt_x": 0.25}},
        cooldown=4.0, priority=2,
    ),
    "sway_hip": ActionSpec(
        "sway_hip",
        {"hip": 0.30, "body_x": 0.20, "body_z": 0.24, "chest_x": 0.25,
         "skirt_x": 0.35, "arm_l": 0.25, "arm_r": 0.25},
        ("扭腰", "扭扭腰", "左右摇摆", "摇晃身体", "律动", "跳舞", "sway hip"),
        duration=3.2, attack=0.28, hold=2.25, release=0.60,
        loops={"hip": (0.55, 1.8, 0.0), "body_z": (0.38, 1.8, -0.4),
               "body_x": (0.32, 1.8, -0.8), "head_z": (0.26, 1.8, 2.5),
               "skirt_x": (0.48, 1.8, -0.6), "chest_x": (0.32, 1.8, -0.5)},
        degrade={"hip": {"body_z": 0.45, "body_x": 0.25},
                 "skirt_x": {"body_z": 0.30}},
        cooldown=5.0, priority=2,
    ),
    "bow": ActionSpec(
        "bow",
        {"lean_y": 0.95, "body_y": -0.55, "head_y": -0.25,
         "chest_y": 0.40, "skirt_y": 0.30, "arm_l": 0.15, "arm_r": 0.15},
        ("鞠躬", "凑近", "往前凑", "靠近看", "探身", "俯身", "弯腰", "bow"),
        duration=2.2, attack=0.42, hold=1.15, release=0.55,
        loops={"head_y": (0.08, 1.2, 0.0)},
        degrade={"lean_y": {"body_y": 0.45, "head_y": -0.20},
                 "body_y": {"head_y": -0.20}},
        cooldown=6.0, priority=2,
    ),
    "cheer": ActionSpec(
        "cheer",
        {"arm_l": 0.68, "arm_r": 0.72, "arm_l_bend": 0.55, "arm_r_bend": 0.55,
         "body_y": 0.35, "head_y": 0.22, "mouth_open": 0.40,
         "mouth_form": 0.65, "eye_l_smile": 0.70, "eye_r_smile": 0.70},
        ("欢呼", "举手", "耶", "万岁", "好耶", "太棒了", "cheer", "yay"),
        duration=2.0, attack=0.20, hold=1.40, release=0.40,
        loops={"body_y": (0.25, 2.8, 0.0), "arm_l": (0.20, 2.8, -0.3),
               "arm_r": (0.20, 2.8, -0.1)},
        degrade={"arm_l": {"body_y": 0.20}, "arm_r": {"body_y": 0.20}},
        cooldown=4.0, priority=2,
    ),
    "peek": ActionSpec(
        "peek",
        {"lean_y": 0.75, "head_x": -0.35, "head_z": -0.42,
         "body_x": -0.25, "body_z": 0.22, "eye_x": 0.25},
        ("探头", "歪头看", "盯着看", "凑近盯", "观察", "peek"),
        duration=2.6, attack=0.35, hold=1.65, release=0.55,
        loops={"head_z": (0.06, 1.1, 0.0)},
        degrade={"lean_y": {"body_y": 0.30, "body_x": -0.15}},
        cooldown=6.0, priority=1,
    ),
    "look_away": ActionSpec(
        "look_away",
        {"head_x": 0.34, "head_y": 0.05, "head_z": 0.12,
         "eye_x": 0.34, "eye_y": 0.05, "body_x": 0.06,
         "body_z": 0.05, "shoulder_l": 0.02},
        ("移开视线", "看别处", "看旁边", "别过头", "撇头", "不看你",
         "移开目光", "转开视线", "往旁边看", "look away"),
        duration=1.55, attack=0.28, hold=0.75, release=0.50,
        degrade={"head_x": {"body_z": 0.22}},
        cooldown=4.0, priority=0,
    ),
    "long_skirt": ActionSpec(
        "long_skirt",
        {},
        ("穿长裙", "换长裙", "长裙", "long skirt"),
        duration=0.2, states={"long_skirt": True}, cooldown=1.0, priority=3,
    ),
    "short_skirt": ActionSpec(
        "short_skirt",
        {},
        ("穿短裙", "换短裙", "短裙", "short skirt"),
        duration=0.2, states={"long_skirt": False}, cooldown=1.0, priority=3,
    ),
    "chibi_mode": ActionSpec(
        "chibi_mode",
        {},
        ("变Q版", "Q版", "缩小", "chibi"),
        duration=0.2, states={"chibi_mode": True}, cooldown=1.0, priority=3,
    ),
    "normal_body": ActionSpec(
        "normal_body",
        {},
        ("恢复正常比例", "正常比例", "恢复原样", "正常体型", "默认体型",
         "变回来", "取消Q版", "恢复体型", "normal size"),
        duration=0.2, states={"chibi_mode": False}, cooldown=1.0, priority=3,
    ),
    "bounce": ActionSpec(
        "bounce",
        {"body_y": 0.34, "body_x": 0.05, "head_y": 0.16,
         "arm_l": 0.12, "arm_r": 0.12, "finger_l": 0.10, "finger_r": 0.10,
         "hand_style_grass": 1.0,
         "leg_l": 0.20, "leg_r": 0.20,
         "shoulder_l": 0.14, "shoulder_r": 0.14},
        ("蹦", "跳", "bounce", "hop"),
        duration=1.25, attack=0.16, hold=0.82, release=0.32,
        loops={"body_y": (0.36, 2.8, 0.0), "head_y": (0.26, 2.8, -0.55),
               "head_z": (0.18, 1.4, 0.0), "arm_l": (0.10, 2.8, -0.75),
               "arm_r": (0.10, 2.8, -0.75),
               "shoulder_l": (0.20, 2.8, -0.4),
               "shoulder_r": (0.20, 2.8, -0.4)},
        degrade={}, cooldown=3.0, priority=1,
    ),
    "gesture": ActionSpec(
        "gesture",
        {"arm_r": 0.82, "arm_r_bend": 0.62, "hand_r": 0.45, "finger_r": 0.35,
         "arm_r_persp": 0.30,
         "arm_l": 0.36, "arm_l_bend": 0.36,
         "head_x": 0.22, "head_z": -0.20, "head_y": 0.05,
         "body_x": 0.16, "body_y": 0.16, "body_z": 0.14,
         "shoulder_r": 0.14},
        ("用手比划", "比划", "手势", "gesture", "hand talk"),
        duration=2.25, attack=0.26, hold=1.35, release=0.60,
        loops={"arm_r": (0.26, 2.1, 0.0), "arm_r_bend": (0.34, 2.1, -0.8),
               "hand_r": (0.40, 2.1, -1.4), "finger_r": (0.28, 2.1, -1.7),
               "arm_r_persp": (0.14, 2.1, -1.1),
               "arm_l": (0.20, 2.1, 1.8),
               "head_z": (0.16, 2.1, -0.5), "body_z": (0.12, 2.1, -0.9)},
        degrade={"arm_r": {"body_z": 0.40, "head_z": 0.28, "body_x": 0.26},
                 "arm_r_bend": {"body_z": 0.14, "head_z": 0.10},
                 "hand_r": {"arm_r_bend": 0.75},
                 "finger_r": {"hand_r": 0.75}},
        cooldown=3.0, priority=1,
    ),
    "sob": ActionSpec(
        "sob",
        {"head_y": -0.20, "head_x": -0.06, "head_z": -0.08,
         "body_x": -0.10, "body_y": -0.10, "body_z": -0.05,
         "mouth_form": -0.35, "mouth_open": 0.06,
         "eye_l_open": -0.12, "eye_r_open": -0.12,
         "brow_l_y": -0.12, "brow_r_y": -0.12,
         "brow_l_angle": 0.35, "brow_r_angle": 0.35,
         "hand_l": 0.35, "hand_r": 0.35,
         "hand_style_chest": 1.0,
         "arm_l": 0.06, "arm_r": 0.06},
        ("哭", "流泪", "大哭", "sob", "cry"),
        duration=2.7, attack=0.32, hold=1.55, release=0.82,
        loops={"body_x": (0.035, 4.5, 0.0), "head_y": (0.04, 4.5, -0.4),
               "shoulder_l": (0.06, 4.5, -0.2),
               "shoulder_r": (0.06, 4.5, -0.2)},
        degrade={"hand_l": {"head_z": 0.08}, "hand_r": {"head_z": -0.08},
                 "arm_l": {"body_z": 0.18, "head_z": 0.12},
                 "arm_r": {"body_z": 0.18, "head_z": 0.12}},
        cooldown=8.0, priority=2,
    ),
}


def spec_from_dict(name: str, data: dict) -> ActionSpec | None:
    """把 profile 的动作配方编译成 ActionSpec；坏字段整条丢弃。"""
    if not isinstance(data, dict):
        return None
    try:
        pose = {str(k): float(v) for k, v in (data.get("pose") or {}).items()}
        loops = {
            str(k): (float(v[0]), float(v[1]), float(v[2]))
            for k, v in (data.get("loops") or {}).items()
            if isinstance(v, (list, tuple)) and len(v) >= 3
        }
        states = {str(k): bool(v) for k, v in (data.get("states") or {}).items()}
        degrade = {
            str(k): {str(target): float(factor)
                     for target, factor in (v or {}).items()}
            for k, v in (data.get("degrade") or {}).items()
            if isinstance(v, dict)
        }
        raw = {str(k): float(v) for k, v in (data.get("raw_overrides") or {}).items()}
        persistent = {
            str(k): float(v)
            for k, v in (data.get("persistent_raw") or {}).items()
        }
        aliases = tuple(str(x).strip() for x in (data.get("aliases") or [])
                        if str(x).strip())
        duration = max(0.12, float(data.get("duration", 1.8)))
        attack = max(0.04, float(data.get("attack", 0.18)))
        hold = max(0.02, float(data.get("hold", 0.85)))
        release = max(0.05, float(data.get("release", 0.55)))
        priority = float(data.get("priority", 1))
        cooldown = max(0.0, float(data.get("cooldown", 3.0)))
    except (TypeError, ValueError):
        return None
    if not (pose or loops or states or persistent):
        return None
    return ActionSpec(
        name=str(name), pose=pose, aliases=aliases, duration=duration,
        attack=attack, hold=hold, release=release, loops=loops,
        degrade=degrade,
        states=states, raw_overrides=raw, persistent_raw=persistent,
        priority=priority, cooldown=cooldown,
    )


#: profile 动作配方允许出现的键；校验与运行时读同一份，避免两边口径漂移。
RECIPE_KEYS = (
    "pose", "loops", "states", "raw_overrides", "persistent_raw",
    "aliases", "duration", "attack", "hold", "release", "priority",
    "cooldown", "degrade",
)

_MIRROR_CHANNELS = {
    "arm_l": "arm_r", "arm_r": "arm_l",
    "arm_l_bend": "arm_r_bend", "arm_r_bend": "arm_l_bend",
    "hand_l": "hand_r", "hand_r": "hand_l",
    "finger_l": "finger_r", "finger_r": "finger_l",
    "arm_l_persp": "arm_r_persp", "arm_r_persp": "arm_l_persp",
    "shoulder_l": "shoulder_r", "shoulder_r": "shoulder_l",
}


def _mirror_target(channel: str, profile: ModelProfile) -> str:
    other = _MIRROR_CHANNELS.get(channel, "")
    return other if other and profile.supports(other) else ""


def _alias_map(actions: dict[str, ActionSpec]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, spec in actions.items():
        out[name.lower()] = name
        for alias in spec.aliases:
            out[str(alias).strip().lower()] = name
    return out


@dataclass
class ResolvedAction:
    spec: ActionSpec
    pose: dict[str, float]
    loops: dict[str, tuple[float, float, float]]
    degraded: bool = False
    dropped: tuple[str, ...] = ()


class ActionLibrary:
    def __init__(self, actions: dict[str, ActionSpec] | None = None) -> None:
        self.actions = dict(actions or DEFAULT_ACTIONS)
        self.aliases = _alias_map(self.actions)

    def add(self, extra: dict[str, ActionSpec]) -> None:
        for name, spec in extra.items():
            self.actions[str(name)] = spec
        self.aliases = _alias_map(self.actions)

    def resolve(self, name: str) -> ActionSpec | None:
        key = str(name or "").strip().lower()
        canonical = self.aliases.get(key)
        return self.actions.get(canonical) if canonical else None

    def names(self) -> list[str]:
        return sorted(self.actions)

    def resolve_for(self, spec: ActionSpec, profile: ModelProfile) -> ResolvedAction:
        pose: dict[str, float] = {}
        dropped: list[str] = []
        degraded = False

        def fold(channel: str, value: float) -> None:
            nonlocal degraded
            if profile.supports(channel):
                pose[channel] = pose.get(channel, 0.0) + value
                return
            mirrored = _mirror_target(channel, profile)
            if mirrored:
                pose[mirrored] = pose.get(mirrored, 0.0) + value
                degraded = True
                return
            fallback = spec.degrade.get(channel)
            if not fallback:
                dropped.append(channel)
                return
            used = False
            for target, factor in fallback.items():
                if profile.supports(target):
                    pose[target] = pose.get(target, 0.0) + value * float(factor)
                    used = True
            if used:
                degraded = True
            else:
                dropped.append(channel)

        for channel, value in spec.pose.items():
            fold(channel, value)

        loops: dict[str, tuple[float, float, float]] = {}
        for channel, (amplitude, frequency, phase) in spec.loops.items():
            target = channel
            scale = 1.0
            if not profile.supports(target):
                mirrored = _mirror_target(channel, profile)
                if mirrored:
                    target = mirrored
                    degraded = True
                else:
                    fallback = spec.degrade.get(channel, {})
                    target = next((k for k in fallback if profile.supports(k)), "")
                    scale = 0.8
                    if target:
                        degraded = True
            if target:
                previous = loops.get(target, (0.0, frequency, phase))
                loops[target] = (
                    previous[0] + float(amplitude) * scale,
                    frequency, phase)
        return ResolvedAction(
            spec=spec,
            pose=pose,
            loops=loops,
            degraded=degraded,
            dropped=tuple(dropped),
        )


@dataclass
class _Playing:
    resolved: ResolvedAction
    elapsed: float = 0.0
    intensity: float = 1.0
    seed: int = 1

    @property
    def spec(self) -> ActionSpec:
        return self.resolved.spec


class ActionPlayer:
    """One primary action at a time, with priority and cooldown."""

    def __init__(self, library: ActionLibrary | None = None, seed: int = 1) -> None:
        self.library = library or ActionLibrary()
        self.rng = Rng(seed + 71)
        self._current: _Playing | None = None
        self._cooldowns: dict[str, float] = {}
        self.last_degraded: bool = False
        self._last_end = 0.0
        self._state_requests: list[tuple[str, bool]] = []
        self._persistent_requests: dict[str, float] = {}
        self._strength = 0.0

    @property
    def playing(self) -> bool:
        return self._current is not None

    @property
    def current_name(self) -> str:
        return self._current.spec.name if self._current else ""

    @property
    def last_end(self) -> float:
        return self._last_end

    @property
    def strength(self) -> float:
        """当前动作包络强度（0..1）；compositor 用它做姿态让位。"""
        return float(self._strength)

    def cooldown_left(self, name: str, now: float) -> float:
        return max(0.0, self._cooldowns.get(name, 0.0) - float(now))

    def play(self, name: str, profile: ModelProfile, *, now: float,
             intensity: float = 1.0, force: bool = False) -> bool:
        spec = self.library.resolve(name)
        if spec is None:
            return False
        settings = (getattr(profile, "actions", None) or {}).get(spec.name, {})
        if not settings.get("enabled", True):
            return False
        if not force and self.cooldown_left(spec.name, now) > 0.0:
            return False
        if self._current is not None and not force:
            if spec.priority < self._current.spec.priority:
                return False
        try:
            gain = float(settings.get("intensity", 1.0))
            duration_scale = float(settings.get("duration_scale", 1.0))
        except (TypeError, ValueError):
            gain, duration_scale = 1.0, 1.0
        intensity = clamp(float(intensity) * gain, 0.0, 1.0)
        if duration_scale != 1.0:
            spec = replace(spec, duration=spec.duration * duration_scale)
        resolved = self.library.resolve_for(spec, profile)
        if (not resolved.pose and not resolved.loops
                and not spec.raw_overrides):
            # 只改变离散状态/持久参数的动作（换长裙、Q版）没有姿态。
            if not (spec.states or spec.persistent_raw):
                return False
            if spec.states and not any(
                    name in profile.states for name in spec.states):
                return False
            self._state_requests.extend(spec.states.items())
            self._persistent_requests.update(spec.persistent_raw)
            self._cooldowns[spec.name] = (
                float(now) + min(1.0, max(0.2, float(spec.cooldown))))
            self._last_end = float(now)
            return True
        if spec.states:
            self._state_requests.extend(spec.states.items())
        if spec.persistent_raw:
            self._persistent_requests.update(spec.persistent_raw)
        if self._current is not None:
            prev = self._current.spec
            self._cooldowns[prev.name] = (
                float(now) + max(0.2, float(prev.cooldown) * 0.5))
        self._current = _Playing(
            resolved=resolved,
            intensity=clamp(float(intensity), 0.0, 1.0),
            seed=self.rng.next_u64() & 0x7FFFFFFF,
        )
        self._last_end = float(now)
        self.last_degraded = resolved.degraded
        return True

    def cancel(self) -> None:
        self._current = None
        self._strength = 0.0
        self._state_requests.clear()
        self._persistent_requests.clear()

    def take_state_requests(self) -> tuple[list[tuple[str, bool]], dict[str, float]]:
        requests = self._state_requests
        persistent = self._persistent_requests
        self._state_requests = []
        self._persistent_requests = {}
        return requests, persistent

    def raw_overrides(self) -> dict[str, float]:
        if self._current is None:
            return {}
        return dict(self._current.spec.raw_overrides)

    def update(self, dt: float, *, now: float) -> Pose:
        dt = max(0.0, min(0.1, float(dt)))
        playing = self._current
        if playing is None:
            self._strength = 0.0
            return Pose()
        playing.elapsed += dt
        spec = playing.spec
        attack, hold, release = spec.normalized_timings()
        t = playing.elapsed
        if t >= attack + hold + release:
            self._cooldowns[spec.name] = float(now) + spec.cooldown
            self._last_end = float(now)
            self._current = None
            self._strength = 0.0
            return Pose()

        envelope = choreo_envelope(
            t, attack, hold, release, spec.anticipation) * playing.intensity
        self._strength = max(0.0, min(1.0, float(envelope)))

        out = Pose()
        # During the hold, the pose keeps a small oscillation plus noise.  A
        # mathematically constant hold is the single most common source of the
        # "frozen puppet" look.
        micro = 0.0
        if attack <= t < attack + hold:
            micro = 0.012 * fbm(t, 0.75, playing.seed + 11)
        for channel, value in playing.resolved.pose.items():
            out.add(channel, value * envelope + micro)

        for channel, (amplitude, frequency, phase) in playing.resolved.loops.items():
            wave = math.sin(2.0 * math.pi * frequency * t + phase)
            out.add(channel, wave * amplitude * envelope)

        return out.clamped()

    def stats(self) -> dict:
        return {
            "playing": self.current_name,
            "degraded": self.last_degraded,
            "cooldowns": {k: round(v, 2) for k, v in self._cooldowns.items()},
        }
