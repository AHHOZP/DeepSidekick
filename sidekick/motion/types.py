"""Shared value types for the procedural Live2D driver.

All semantic channels are normalised before they reach a model.  A channel is
either *bipolar* (``-1..1``), *unit* (``0..1``) or an *eye-open* channel
(``0..1.3``).  Conversion to a model's real parameter range happens once, in
:class:`ParamSpec.to_raw`, so every layer can be composed without knowing the
model.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Protocol

ChannelKind = Literal["bipolar", "unit", "eye"]

#: Valid semantic range for every channel the engine can emit.
CHANNEL_RANGES: dict[str, tuple[float, float]] = {
    # Head: x = yaw (positive right), y = pitch (positive up), z = roll.
    "head_x": (-1.0, 1.0),
    "head_y": (-1.0, 1.0),
    "head_z": (-1.0, 1.0),
    # Body: x = yaw/lean, y = pitch, z = roll.
    "body_x": (-1.0, 1.0),
    "body_y": (-1.0, 1.0),
    "body_z": (-1.0, 1.0),
    # Eyes.
    "eye_x": (-1.0, 1.0),
    "eye_y": (-1.0, 1.0),
    "eye_l_open": (0.0, 1.3),
    "eye_r_open": (0.0, 1.3),
    "eye_l_smile": (0.0, 1.0),
    "eye_r_smile": (0.0, 1.0),
    "eye_squint": (0.0, 1.0),
    # Brows.
    "brow_l_y": (-1.0, 1.0),
    "brow_r_y": (-1.0, 1.0),
    "brow_l_angle": (-1.0, 1.0),
    "brow_r_angle": (-1.0, 1.0),
    "brow_l_form": (-1.0, 1.0),
    "brow_r_form": (-1.0, 1.0),
    # Mouth and face.
    "mouth_open": (0.0, 1.0),
    "mouth_form": (-1.0, 1.0),
    "mouth_x": (-1.0, 1.0),
    "cheek": (0.0, 1.0),
    "breath": (0.0, 1.0),
    # Arms and hands.  ``*_bend`` is a separate forearm channel so a model
    # with only an upper-arm parameter still gets a useful raise.
    "arm_l": (-1.0, 1.0),
    "arm_r": (-1.0, 1.0),
    "arm_l_bend": (-1.0, 1.0),
    "arm_r_bend": (-1.0, 1.0),
    "hand_l": (-1.0, 1.0),
    "hand_r": (-1.0, 1.0),
    "finger_l": (-1.0, 1.0),
    "finger_r": (-1.0, 1.0),
    "arm_l_persp": (-1.0, 1.0),
    "arm_r_persp": (-1.0, 1.0),
    "shoulder_l": (-1.0, 1.0),
    "shoulder_r": (-1.0, 1.0),
    # Chest / skirt / tail are secondary deformers in many game-rip models;
    # they are driven from body motion and breath when the model exposes them.
    "chest_x": (-1.0, 1.0),
    "chest_y": (-1.0, 1.0),
    "skirt_x": (-1.0, 1.0),
    "skirt_y": (-1.0, 1.0),
    # 身体前倾插值通道（LeanY / LeanY2 / LeanY3 / LeanInput）。
    "lean_y": (0.0, 1.0),
    "tail": (-1.0, 1.0),
    "sleeve_l_x": (-1.0, 1.0),
    "sleeve_l_y": (-1.0, 1.0),
    "sleeve_r_x": (-1.0, 1.0),
    "sleeve_r_y": (-1.0, 1.0),
    "ribbon_l_x": (-1.0, 1.0),
    "ribbon_l_y": (-1.0, 1.0),
    "ribbon_r_x": (-1.0, 1.0),
    "ribbon_r_y": (-1.0, 1.0),
    # Binary hand-texture switches (dog-tail grass, hand on chest, index finger).
    "hand_style_grass": (0.0, 1.0),
    "hand_style_chest": (0.0, 1.0),
    "hand_style_point": (0.0, 1.0),
    # 图层/无缝切换开关：抬手时打开，避免手臂留在头发/身体后层。
    "arm_gate": (0.0, 1.0),
    "hand_gate": (0.0, 1.0),
    # Legs and hips.  ``leg_*`` is the hip/whole-leg swing (R1Z), ``knee_*``
    # is the lower leg (R2Z), ``ankle_*`` is the foot pivot (R3Z), and the
    # ``*_y`` chain is the forward/back axis (R1Y/R2Y/R3Y).  ``foot_*`` is the
    # side pivot (FootX) while ``foot_swing`` is the shared LegLF pendulum.
    "leg_l": (-1.0, 1.0),
    "leg_r": (-1.0, 1.0),
    "leg_l_x": (-1.0, 1.0),
    "leg_r_x": (-1.0, 1.0),
    "knee_l": (-1.0, 1.0),
    "knee_r": (-1.0, 1.0),
    "ankle_l": (-1.0, 1.0),
    "ankle_r": (-1.0, 1.0),
    "leg_l_y": (-1.0, 1.0),
    "leg_r_y": (-1.0, 1.0),
    "knee_l_y": (-1.0, 1.0),
    "knee_r_y": (-1.0, 1.0),
    "foot_l_y": (-1.0, 1.0),
    "foot_r_y": (-1.0, 1.0),
    "foot_l": (-1.0, 1.0),
    "foot_r": (-1.0, 1.0),
    "foot_swing": (-1.0, 1.0),
    "hip": (-1.0, 1.0),
    # Secondary hair channels.
    "hair_front": (-1.0, 1.0),
    "hair_side": (-1.0, 1.0),
    "hair_back": (-1.0, 1.0),
}

def clamp(value: float, low: float, high: float) -> float:
    """Clamp *value* to the inclusive range ``low..high``."""
    if value < low:
        return low
    if value > high:
        return high
    return value


def clamp_channel(channel: str, value: float) -> float:
    """Clamp a semantic channel to its declared range."""
    low, high = CHANNEL_RANGES.get(channel, (-1.0, 1.0))
    return clamp(float(value), low, high)


def soft_saturate(value: float, knee: float = 0.82, softness: float = 0.18) -> float:
    """Compress values in the last ``softness`` of the range, then hard clamp.

    A plain ``clamp`` makes two stacked layers suddenly stop moving at the
    same value.  A soft knee keeps the last few percent elastic while still
    guaranteeing the model never receives an out-of-range value.
    """
    if softness <= 0.0:
        return value
    sign = -1.0 if value < 0.0 else 1.0
    magnitude = abs(value)
    if magnitude <= knee:
        return value
    limit = knee + softness
    magnitude = knee + softness * math.tanh((magnitude - knee) / softness)
    return sign * min(magnitude, limit)


@dataclass
class Pose:
    """A sparse set of normalised semantic channels."""

    values: dict[str, float] = field(default_factory=dict)

    def get(self, channel: str, default: float = 0.0) -> float:
        return float(self.values.get(channel, default))

    def set(self, channel: str, value: float) -> "Pose":
        self.values[channel] = float(value)
        return self

    def add(self, channel: str, value: float, weight: float = 1.0) -> "Pose":
        self.values[channel] = self.get(channel) + float(value) * float(weight)
        return self

    def merge(self, other: "Pose", weight: float = 1.0) -> "Pose":
        for channel, value in other.values.items():
            self.add(channel, value, weight)
        return self

    def scaled(self, weight: float) -> "Pose":
        return Pose({k: v * float(weight) for k, v in self.values.items()})

    def clamped(self) -> "Pose":
        return Pose({k: clamp_channel(k, v) for k, v in self.values.items()})

    def copy(self) -> "Pose":
        return Pose(dict(self.values))

    def __bool__(self) -> bool:
        return bool(self.values)


@dataclass(frozen=True)
class ParamSpec:
    """One resolved binding between a semantic channel and a model parameter."""

    id: str
    channel: str
    minimum: float
    maximum: float
    default: float
    kind: ChannelKind = "bipolar"
    score: int = 0
    match: str = ""
    #: 死区跨越指数；1.0 为线性。``q_ratio < 0.12`` 的通道由几何探针设置。
    gamma: float = 1.0
    #: 模型输出方向与语义方向相反时取 ``-1``（VTube 的输入/输出区间反相）。
    polarity: float = 1.0
    #: 画师在 `.vtube.json` 里实际使用的窄区间；None 表示使用模型导出范围。
    effective_min: float | None = None
    effective_max: float | None = None
    #: 多源证据的置信度与来源，供审计/WebUI 解释“为什么绑这个参数”。
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # A malformed model must not make the driver emit NaN / inverted ranges.
        object.__setattr__(self, "minimum", float(self.minimum))
        object.__setattr__(self, "maximum", float(self.maximum))
        object.__setattr__(self, "default", float(self.default))
        if not math.isfinite(self.minimum) or not math.isfinite(self.maximum):
            object.__setattr__(self, "minimum", 0.0)
            object.__setattr__(self, "maximum", 1.0)
        if self.maximum < self.minimum:
            object.__setattr__(self, "minimum", self.maximum)
            object.__setattr__(self, "maximum", self.minimum)
        if not math.isfinite(self.default) or not (self.minimum <= self.default <= self.maximum):
            object.__setattr__(self, "default", (self.minimum + self.maximum) / 2.0)
        if not math.isfinite(float(self.gamma)):
            object.__setattr__(self, "gamma", 1.0)
        else:
            object.__setattr__(self, "gamma", min(2.0, max(0.25, float(self.gamma))))
        object.__setattr__(self, "polarity", -1.0 if float(self.polarity) < 0.0 else 1.0)
        lo = (None if self.effective_min is None
              else float(self.effective_min))
        hi = (None if self.effective_max is None
              else float(self.effective_max))
        if lo is None and hi is None:
            object.__setattr__(self, "effective_min", None)
            object.__setattr__(self, "effective_max", None)
        else:
            lo = self.minimum if lo is None else lo
            hi = self.maximum if hi is None else hi
            if not math.isfinite(lo) or not math.isfinite(hi):
                lo, hi = self.minimum, self.maximum
            if hi < lo:
                lo, hi = hi, lo
            # 作者区间可能比 moc3 导出范围还宽；绝不能因此写出模型外值。
            lo = max(self.minimum, lo)
            hi = min(self.maximum, hi)
            # 模型默认值必须留在有效区间里，否则“中性”会被画师的工作区间
            # 顶到边界，一加载就歪。
            lo = min(lo, self.default)
            hi = max(hi, self.default)
            object.__setattr__(self, "effective_min", lo)
            object.__setattr__(self, "effective_max", hi)
        confidence = float(self.confidence)
        object.__setattr__(self, "confidence",
                           min(1.0, max(0.0, confidence))
                           if math.isfinite(confidence) else 0.0)
        object.__setattr__(self, "evidence",
                           tuple(str(x) for x in (self.evidence or ()) if str(x)))

    @property
    def effective_bounds(self) -> tuple[float, float]:
        return (self.minimum if self.effective_min is None else self.effective_min,
                self.maximum if self.effective_max is None else self.effective_max)

    @property
    def positive_span(self) -> float:
        _minimum, maximum = self.effective_bounds
        return max(0.0, maximum - self.default)

    @property
    def negative_span(self) -> float:
        minimum, _maximum = self.effective_bounds
        return max(0.0, self.default - minimum)

    def to_raw(self, value: float) -> float:
        """Convert a normalised semantic value to the model's raw range.

        * ``unit`` channels use the full ``min..max`` span (0 is fully closed).
        * ``eye`` channels multiply the model's neutral openness, so a model
          whose neutral is ``1.5`` is not forced shut by a normalised ``1``.
        * ``bipolar`` channels use the neutral as the origin and preserve the
          asymmetric raw range (important for e.g. ``ParamMouthForm`` whose
          neutral is ``-0.5``).
        """
        v = float(value)
        if not math.isfinite(v):
            return self.default
        if self.polarity < 0.0:
            v = -v
        if self.gamma != 1.0 and v:
            # gamma < 1 把小幅输入推过模型前段死区；保持符号和端点不变。
            # av ** gamma 在 0 的导数为无穷大；过零处线性化到 knee，避免
            # head_x/y 平滑穿过中线时单帧角速度被放大数倍。
            av = abs(v)
            knee = 0.08
            if self.gamma < 1.0 and av < knee:
                v = math.copysign((av / knee) * (knee ** self.gamma), v)
            else:
                v = math.copysign(av ** self.gamma, v)
        minimum, maximum = self.effective_bounds
        default = clamp(self.default, minimum, maximum)
        if self.kind == "unit":
            v = clamp(v, 0.0, 1.0)
            # ``0`` is the model's neutral value, not necessarily its minimum:
            # some models use ``-1..1`` for eye-smile or cheek, where ``-1``
            # would be a strong negative expression rather than "off".
            base = default
            return clamp(base + v * (maximum - base), minimum, maximum)
        if self.kind == "eye":
            v = clamp(v, 0.0, 1.3)
            return clamp(default * v, minimum, maximum)
        v = clamp(v, -1.0, 1.0)
        if v >= 0.0:
            return clamp(default + v * max(0.0, maximum - default),
                         minimum, maximum)
        return clamp(default + v * max(0.0, default - minimum),
                     minimum, maximum)

    def report(self) -> dict:
        out = {
            "id": self.id,
            "channel": self.channel,
            "kind": self.kind,
            "min": self.minimum,
            "max": self.maximum,
            "default": self.default,
            "score": self.score,
            "match": self.match,
        }
        if self.gamma != 1.0:
            out["gamma"] = self.gamma
        if self.polarity < 0.0:
            out["polarity"] = -1.0
        if self.effective_min is not None and self.effective_max is not None:
            out["effective_min"] = self.effective_min
            out["effective_max"] = self.effective_max
        if self.confidence:
            out["confidence"] = round(self.confidence, 3)
        if self.evidence:
            out["evidence"] = list(self.evidence)
        return out


@dataclass(frozen=True)
class StateSpec:
    """一个离散参数开关（换装、体型、手型贴图）。

    ``persistent`` 状态跨轮保持；``timed`` 状态在动作结束后自动释放。
    参数本身仍是模型原始参数，StateController 每帧把它写回。
    """

    name: str
    param_id: str
    aliases: tuple[str, ...] = ()
    active_value: float = 1.0
    inactive_value: float = 0.0
    mode: str = "persistent"
    hold_seconds: float = 4.0
    mutex_group: str = ""
    coupled_params: dict[str, float] = field(default_factory=dict)

    def report(self) -> dict:
        return {
            "name": self.name,
            "param_id": self.param_id,
            "aliases": list(self.aliases),
            "active_value": self.active_value,
            "inactive_value": self.inactive_value,
            "mode": self.mode,
            "hold_seconds": self.hold_seconds,
            "mutex_group": self.mutex_group,
            "coupled_params": dict(self.coupled_params),
        }


@dataclass
class ModelProfile:
    """The result of introspecting one unknown Live2D model."""

    params: dict[str, ParamSpec] = field(default_factory=dict)
    by_channel: dict[str, ParamSpec] = field(default_factory=dict)
    tier: str = "none"
    capabilities: dict[str, bool] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    model_name: str = ""
    #: profile 覆盖与来源；运行时自省仍是事实来源。
    tuning: dict[str, float] = field(default_factory=dict)
    source: str = "introspection"
    fallback: bool = False
    archetype: str = "unknown"
    actions: dict[str, dict] = field(default_factory=dict)
    motion_policy: dict = field(default_factory=dict)
    expressions: dict[str, str] = field(default_factory=dict)
    prompt: dict = field(default_factory=dict)
    #: 所有原始参数范围，供 profile alias 绑定未映射参数。
    ranges: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    #: 同语义的级联参数：channel -> [(param_id, coupling_scale), ...]。
    followers: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    #: 由模型 physics3 声明的输出参数；引擎绝不主动写，避免锁死头发/衣服物理。
    physics_outputs: set[str] = field(default_factory=set)
    #: 由 physics3 声明的输入参数；桌面必须在下一帧 Update 前保存，物理才会先读到。
    physics_inputs: set[str] = field(default_factory=set)
    #: 离散状态：换装、体型、手型贴图；由 StateController 持久/定时保持。
    states: dict[str, StateSpec] = field(default_factory=dict)
    #: profile 自定义动作配方（已校验的动作字典），由 MotionEngine 编译。
    custom_actions: dict[str, dict] = field(default_factory=dict)
    #: 绑定参数的实际屏幕侧（left/right/center）；几何探针不可用时为空。
    screen_side: dict[str, str] = field(default_factory=dict)
    #: 双眼中心（屏幕归一化 x，0 脚底到 1 头顶的 y）；桌面鼠标注视原点。
    pointer_origin: tuple[float, float] | None = None

    def supports(self, channel: str) -> bool:
        return channel in self.by_channel

    def capability(self, name: str) -> bool:
        return bool(self.capabilities.get(name, False))

    @property
    def channels(self) -> set[str]:
        return set(self.by_channel)

    def report(self) -> dict:
        return {
            "model": self.model_name,
            "tier": self.tier,
            "capabilities": dict(self.capabilities),
            "bindings": {k: v.report() for k, v in sorted(self.by_channel.items())},
            "followers": {
                channel: [pid for pid, _scale in rows]
                for channel, rows in sorted(self.followers.items())
            },
            "physics_outputs_count": len(self.physics_outputs),
            "physics_inputs_count": len(self.physics_inputs),
            "states": {name: spec.report() for name, spec in sorted(self.states.items())},
            "custom_actions": sorted(self.custom_actions),
            "screen_side": dict(sorted(self.screen_side.items())),
            "pointer_origin": (list(self.pointer_origin)
                               if self.pointer_origin else None),
            "unmapped_count": len(self.unmapped),
            "warnings": list(self.warnings),
            "source": self.source,
            "fallback": self.fallback,
        }


@dataclass(frozen=True)
class Intent:
    """One semantic instruction produced by text or a host event."""

    kind: str  # "action" | "emotion" | "gaze"
    name: str
    intensity: float = 1.0
    duration: float | None = None
    source: str = "text"

    def report(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "intensity": self.intensity,
            "duration": self.duration,
            "source": self.source,
        }


@dataclass(frozen=True)
class SpeechFrame:
    """One compact audio-analysis frame.

    The host sends these at 25-50 Hz; raw PCM never crosses a process boundary.
    ``form`` is ``-1`` (rounded/puckered) to ``+1`` (wide/spread).
    """

    t: float
    level: float = 0.0
    openness: float = 0.0
    form: float = 0.0
    voiced: bool = False
    vowel: str = ""
    f1: float = 0.0
    f2: float = 0.0

    def clamped(self) -> "SpeechFrame":
        return SpeechFrame(
            t=self.t,
            level=clamp(self.level, 0.0, 1.0),
            openness=clamp(self.openness, 0.0, 1.0),
            form=clamp(self.form, -1.0, 1.0),
            voiced=bool(self.voiced),
            vowel=self.vowel,
            f1=max(0.0, self.f1),
            f2=max(0.0, self.f2),
        )


@dataclass
class Context:
    """Per-frame host context that is not a pose channel."""

    speaking: bool = False
    user_speaking: bool = False
    silence: float = 0.0
    game_motion: float = 0.0
    chat_activity: float = 0.0
    now: float = 0.0
    #: 鼠标在模型窗口里的归一化位置；None 表示没有指针输入。
    pointer: tuple[float, float] | None = None


class MotionHost(Protocol):
    """桌面宿主契约：只有这个对象可以推进引擎并写模型参数。"""

    def submit(self, intents) -> None: ...

    def tick(self, now: float, dt: float, speech: SpeechFrame | None = None,
             context: Context | None = None) -> dict[str, float]: ...

    def write(self, params: dict[str, float]) -> int: ...

    def report(self) -> dict: ...


def clean_pose(values: dict[str, float]) -> dict[str, float]:
    """Drop non-finite values and clamp a plain dict."""
    out: dict[str, float] = {}
    for key, value in values.items():
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out[key] = clamp_channel(key, v)
    return out
