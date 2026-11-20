"""Layer stacking, dual-band prosody, and conversion to model parameters.

The composition order is fixed and every layer is additive unless it owns a
special blend mode:

    emotion (slow posture)
      + posture (discrete whole-body anchor)
      + action (choreographed intent)
      + gaze (virtual point)
      + idle (fBm + breath)
      + speech prosody (phrase envelope + syllable beats)
      -> eyes multiply blink
      -> mouth overwritten by lip-sync while speaking
      -> clamp / soft-saturate
      -> conversion to raw model parameters

No layer ever writes the model.  Only :func:`to_parameters` does, and only the
render thread calls it.  That is what prevents two producers from tearing the
same parameter in the same frame.

All *posture* channels are targets here; :class:`sidekick.motion.engine.MotionEngine`
runs them through one :class:`~live.motion.spring.SpringBank`.  The mouth, eye
openness and breath are deliberately excluded from that slow spring.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .emotion import EmotionState
from .introspect import CHAIN_CHANNELS
from .types import ModelProfile, Pose, SpeechFrame, clamp, clean_pose, soft_saturate


#: Channels whose value is an offset from a neutral of 1.0 rather than from 0.
EYE_OPEN_CHANNELS = ("eye_l_open", "eye_r_open")


@dataclass(frozen=True)
class ProsodyState:
    """One frame of the dual-band speech analysis."""

    env_phrase: float = 0.0
    a_beat: float = 0.0
    phi: float = 0.0
    silence: float = 0.0
    speaking: bool = False

    def pose(self, emotion_scale: float = 1.0) -> Pose:
        ep = clamp(self.env_phrase * emotion_scale, 0.0, 1.35)
        ab = clamp(self.a_beat * emotion_scale, -1.1, 1.1)
        phi = self.phi
        positive = max(0.0, ab)
        negative = max(0.0, -ab)
        out = Pose()
        # Head: syllable nod + a slower phrase sway at 2x the phase rate.
        out.add("head_y", -ab * 0.42 + ep * 0.14 * math.sin(2.0 * phi + 0.4))
        out.add("head_x", ep * 0.22 * math.sin(phi) + ab * 0.14)
        out.add("head_z", ep * 0.24 * math.sin(phi - 0.65) + ab * 0.16)
        # Torso: lean forward while speaking, lag the head laterally.
        out.add("body_y", ep * 0.12 + positive * 0.10)
        out.add("body_x", ep * 0.10 * math.sin(phi - 0.75) + ab * 0.06)
        out.add("body_z", ep * 0.12 * math.sin(phi - 0.90) - ab * 0.07)
        # Arms: the phrase envelope only adds a small relaxed swing.  Big
        # co-speech arms come from the auto beat-gesture, not from a permanent
        # half-T-pose.
        out.add("arm_r", ep * (0.04 + 0.04 * math.sin(phi + 0.3)) + positive * 0.06)
        out.add("arm_r_bend", ep * (0.03 + 0.03 * math.cos(phi)) + positive * 0.05)
        out.add("hand_r", ep * 0.04 * math.sin(2.0 * phi - 0.4) + ab * 0.04)
        out.add("arm_l", ep * (0.03 + 0.03 * math.sin(phi + 2.2)) + negative * 0.05)
        out.add("arm_l_bend", ep * (0.02 + 0.02 * math.cos(phi + 1.9)) + negative * 0.04)
        out.add("hand_l", ep * 0.03 * math.sin(2.0 * phi + 1.5))
        out.add("shoulder_r", ep * 0.03 + positive * 0.06)
        out.add("shoulder_l", ep * 0.02 + negative * 0.05)
        out.add("hip", ep * 0.14 * math.sin(phi - 1.1))
        out.add("brow_l_y", ab * 0.30)
        out.add("brow_r_y", ab * 0.30)
        return out


class ProsodyTracker:
    """Extract a slow phrase envelope and a signed syllable beat.

    The spring bank downstream is a second-order low-pass, so the beat is fed
    unsmoothed; only the *envelope* is asymmetric and slow.  The phase freezes
    when the phrase envelope reaches zero, which means the body does not keep
    nodding after the voice stops and does not jump when the next sentence
    starts from the stored phase.
    """

    def __init__(self) -> None:
        self.env_phrase = 0.0
        self.env_fast = 0.0
        self.env_mid = 0.0
        self.phi = 0.0
        self.silence = 0.0
        self._was_speaking = False

    def update(self, frame: SpeechFrame | None, dt: float, *,
               speaking: bool) -> ProsodyState:
        dt = max(0.0, min(0.2, float(dt)))
        if speaking and frame is not None:
            openness = clamp(frame.openness, 0.0, 1.0)
            level = clamp(frame.level, 0.0, 1.0)
            u = clamp(0.65 * openness + 0.55 * level, 0.0, 1.0)
        else:
            u = 0.0
            self.silence += dt

        self._was_speaking = u > 0.02

        # Fast attack, slow release: short gaps between words do not collapse
        # the phrase envelope.
        tau = 0.14 if u > self.env_phrase else 0.42
        alpha = 1.0 - math.exp(-dt / max(1e-4, tau))
        self.env_phrase += (u - self.env_phrase) * alpha

        alpha_fast = 1.0 - math.exp(-dt / 0.035)
        alpha_mid = 1.0 - math.exp(-dt / 0.22)
        self.env_fast += (u - self.env_fast) * alpha_fast
        self.env_mid += (u - self.env_mid) * alpha_mid
        a_beat = clamp((self.env_fast - self.env_mid) * 3.2, -0.85, 1.0)

        if u > 0.04:
            self.silence = 0.0
        if self.env_phrase > 0.02:
            rate = 2.0 * math.pi * (0.55 + 0.95 * self.env_phrase)
            self.phi += rate * dt
            if self.phi > 2.0 * math.pi:
                self.phi -= 2.0 * math.pi * math.floor(self.phi / (2.0 * math.pi))
        if self.env_phrase < 0.01:
            self.env_phrase = 0.0
            self._was_speaking = False

        return ProsodyState(
            env_phrase=clamp(self.env_phrase, 0.0, 1.0),
            a_beat=a_beat,
            phi=self.phi,
            silence=self.silence,
            speaking=bool(speaking and u > 0.02),
        )


#: 这些动作本来就该双手同时抬高，剪影守护器不能把它们压成单手。
_TWO_HAND_ACTIONS = frozenset({"clap", "cheer", "shrug", "stretch", "sob"})


def apply_silhouette_guard(pose: Pose, action: str = "") -> None:
    """在送进弹簧之前修掉最容易看出“纸片人崩坏”的两类姿态。

    1. 大臂抬起时小臂反向绷直（僵尸手 / T-pose）；
    2. 非双手动作里左右大臂对称平举（体操运动员）。
    """
    for side in ("l", "r"):
        arm = pose.get(f"arm_{side}")
        bend = pose.get(f"arm_{side}_bend")
        if arm > 0.30 and bend < 0.28 * arm:
            pose.set(f"arm_{side}_bend", 0.28 * arm)
    if action in _TWO_HAND_ACTIONS:
        return
    left, right = pose.get("arm_l"), pose.get("arm_r")
    if left > 0.40 and right > 0.40 and abs(left - right) < 0.15:
        pose.set("arm_l", left * 0.45)
        pose.set("hand_l", pose.get("hand_l") * 0.65)


def apply_arm_comfort_guard(pose: Pose) -> None:
    """大臂抬得过高时压住上臂/手腕，避免手贴胸/拿草时整条手臂拧成麻花。

    肘部弯得越深，上臂越应贴近躯干；否则上臂和手腕同时打满，2D 网格会在
    肩/肘处堆叠。这个约束只作用于“深弯肘”姿态，不影响挥手、指、欢呼。
    """
    for side in ("l", "r"):
        bend = pose.get(f"arm_{side}_bend")
        if bend <= 0.82:
            continue
        arm_cap = max(0.35, 0.55 - (bend - 0.82) * 0.35)
        hand_cap = max(0.20, 0.48 - (bend - 0.82) * 0.40)
        if pose.get(f"arm_{side}") > arm_cap:
            pose.set(f"arm_{side}", arm_cap)
        if pose.get(f"hand_{side}") > hand_cap:
            pose.set(f"hand_{side}", hand_cap)
        if pose.get(f"arm_{side}_persp") > 0.30:
            pose.set(f"arm_{side}_persp", 0.30)


def compose(
    profile: ModelProfile,
    emotion: EmotionState,
    posture: Pose,
    action: Pose,
    gaze: Pose,
    idle: Pose,
    prosody: Pose,
    *,
    breath: float,
    blink_l: float,
    blink_r: float,
    speech: SpeechFrame | None,
    speaking: bool,
    now: float = 0.0,
    action_name: str = "",
    action_strength: float = 0.0,
    breath_lag: float | None = None,
    speech_energy: float = 0.0,
) -> Pose:
    """Stack every layer into one clamped semantic pose."""
    from .breath import breath_pose

    # 显式动作期间让姿态锚点和待机噪声平滑让位，避免 bow 与 sway 互相抵消。
    duck = clamp(float(action_strength), 0.0, 1.0)
    pose = Pose()
    pose.merge(emotion.pose(), 0.95)
    pose.merge(posture, 0.90 * (1.0 - 0.65 * duck))
    pose.merge(action, 1.0)
    pose.merge(gaze, 0.90)
    pose.merge(idle, 0.38 * (1.0 - 0.50 * duck))
    pose.merge(prosody, 1.0 * (1.0 - 0.35 * duck))
    pose.merge(breath_pose(breath), 1.0)
    pose.set("breath", float(breath))

    # Secondary deformers follow the body, not the audio.  A model that has
    # chest/skirt/tail channels gets several extra layers of motion for free;
    # a model without them simply drops these values.
    body_x = pose.get("body_x")
    body_y = pose.get("body_y")
    body_z = pose.get("body_z")
    hip = pose.get("hip")
    # LeanY is a model-authored interpolation chain, not a rotation.  A small
    # positive body pitch should visibly bring the upper body toward the
    # camera even on models whose BodyAngleY axis is inverted.
    if profile.supports("lean_y") and pose.get("lean_y", 0.0) <= 1e-6:
        pose.add("lean_y", max(0.0, body_y) * 1.25)
    chest_breath = float(breath if breath_lag is None else breath_lag)
    pose.add("chest_x", body_x * 0.42 + body_z * 0.25
             + (chest_breath - 0.5) * 0.10
             + prosody.get("body_x") * 0.35)
    pose.add("chest_y", body_y * 0.38 + (chest_breath - 0.5) * 0.30
             + prosody.get("body_y") * 0.40)
    pose.add("skirt_x", body_x * 0.85 + body_z * 0.55 + hip * 0.65)
    pose.add("skirt_y", body_y * 0.55 + abs(body_z) * 0.25)
    # 腿不是只在 kick 时才有存在感：很慢的重心偏移让一条腿承重、另一条腿
    # 微屈，脚踝反向踩地。左右腿绝不一起同向画圆；没有腿/脚通道的模型会被
    # to_parameters 自动过滤，不会凭空造参数。
    weight = clamp(
        0.58 * math.sin(2.0 * math.pi * 0.13 * float(now))
        + 0.50 * posture.get("body_z")
        + 0.45 * posture.get("hip")
        + 0.18 * body_z,
        -1.0, 1.0,
    )
    leg_l = weight * 0.12
    leg_r = -weight * 0.12
    knee_l = max(0.0, weight) * 0.25
    knee_r = max(0.0, -weight) * 0.25
    ankle_l = -leg_l * 0.55 - knee_l * 0.35
    ankle_r = -leg_r * 0.55 - knee_r * 0.35
    happy_feet = (max(emotion.weights.get("happy", 0.0),
                      emotion.weights.get("excited", 0.0)) > 0.5
                  or action_name in ("bounce", "cheer", "sway_hip",
                                     "leg_shift", "hand_chest", "wag_tail",
                                     "gesture"))
    foot_swing = (0.32 if happy_feet else 0.03) * math.sin(
        2.0 * math.pi * 0.16 * float(now) + 0.7)
    for channel, value in (
        ("leg_l", leg_l), ("leg_r", leg_r),
        ("leg_l_x", body_z * 0.10), ("leg_r_x", body_z * 0.10),
        ("knee_l", knee_l), ("knee_r", knee_r),
        ("ankle_l", ankle_l), ("ankle_r", ankle_r),
        ("leg_l_y", body_y * 0.12), ("leg_r_y", body_y * 0.12),
        ("knee_l_y", body_y * 0.06), ("knee_r_y", body_y * 0.06),
        ("foot_l_y", body_y * 0.05), ("foot_r_y", body_y * 0.05),
        ("foot_l", -leg_l * 0.35), ("foot_r", -leg_r * 0.35),
        ("foot_swing", foot_swing), ("hip", weight * 0.16),
    ):
        if profile.supports(channel):
            pose.add(channel, value)
    # 头发不是独立动作，但应跟着头和躯干晚半拍。物理输出参数只有在
    # 模型允许的通道上才会存在；没有绑定时这里不会产生任何写入。
    head_x = pose.get("head_x")
    head_z = pose.get("head_z")
    if profile.supports("hair_front"):
        pose.add("hair_front", -head_x * 0.10 + head_z * 0.06)
    if profile.supports("hair_side"):
        pose.add("hair_side", -head_z * 0.10)
    if profile.supports("hair_back"):
        pose.add("hair_back", -head_x * 0.14 + body_z * 0.05)
    # Even an idle tail has a slow, low-amplitude wag; action layers add to it.
    if profile.supports("tail"):
        tail_idle = 0.28 * math.sin(2.0 * math.pi * 0.35 * float(now))
        pose.add("tail", tail_idle + body_z * 0.80 + hip * 0.60
                 + prosody.get("body_z") * 0.50)
    pose.add("finger_l", pose.get("hand_l") * 0.25)
    pose.add("finger_r", pose.get("hand_r") * 0.25)
    pose.add("arm_l_persp", pose.get("arm_l") * 0.35 + pose.get("shoulder_l") * 0.20)
    pose.add("arm_r_persp", pose.get("arm_r") * 0.35 + pose.get("shoulder_r") * 0.20)
    if (profile.supports("arm_gate")
            and max(abs(pose.get("arm_l")), abs(pose.get("arm_r"))) > 0.25):
        pose.add("arm_gate", 1.0)
    pose.add("sleeve_l_x", pose.get("arm_l") * 0.50 + body_x * 0.20)
    pose.add("sleeve_l_y", pose.get("arm_l_bend") * 0.40 + body_y * 0.20)
    pose.add("sleeve_r_x", pose.get("arm_r") * 0.50 + body_x * 0.20)
    pose.add("sleeve_r_y", pose.get("arm_r_bend") * 0.40 + body_y * 0.20)
    pose.add("ribbon_l_x", body_z * 0.50 + pose.get("arm_l") * 0.20)
    pose.add("ribbon_l_y", body_y * 0.40)
    pose.add("ribbon_r_x", body_z * 0.50 + pose.get("arm_r") * 0.20)
    pose.add("ribbon_r_y", body_y * 0.40)

    # 防 T-pose：大臂抬起时小臂必须有自然弯曲；双手对称高抬时压住副手。
    apply_silhouette_guard(pose, action_name)
    apply_arm_comfort_guard(pose)

    squint = clamp(float(emotion.eye_squint), 0.0, 1.0)
    if squint > 0.0:
        # 有些模型有独立的眼眯参数；没有绑定的模型会在 to_parameters 丢弃。
        pose.add("eye_squint", squint * 0.80)

    # 说话时上半张脸也要参与：语调能量高时眼角微弯，思考/困惑时眉毛一高一低。
    # 这不是替代情绪，只是打破“下半张脸在动、上半张脸像面具”的割裂。
    energy = clamp(float(speech_energy), 0.0, 1.0)
    if speaking and energy > 0.02:
        if emotion.dominant not in ("sad", "angry"):
            smile = 0.05 + 0.12 * energy
            pose.add("eye_l_smile", smile)
            pose.add("eye_r_smile", smile)
        if emotion.dominant in ("think", "confused"):
            pose.add("brow_l_y", 0.06 * energy)
            pose.add("brow_r_y", -0.04 * energy)

    # Eyes: neutral 1.0 * blink * emotion offset.  This is the only
    # multiplicative layer; adding eye-open values would double-count.
    base_l = 1.0 + pose.get("eye_l_open", 0.0)
    base_r = 1.0 + pose.get("eye_r_open", 0.0)
    pose.set("eye_l_open", clamp(base_l * max(0.0, float(blink_l)), 0.0, 1.3))
    pose.set("eye_r_open", clamp(base_r * max(0.0, float(blink_r)), 0.0, 1.3))

    # Mouth: while speech is active the audio owns openness and form.  Even
    # when the current frame is a pause (openness 0), the mouth must close
    # instead of falling back to the emotion's open mouth.
    if speaking and speech is not None:
        pose.set("mouth_open", clamp(speech.openness, 0.0, 1.0))
        pose.set("mouth_form", clamp(speech.form * 0.82 + pose.get("mouth_form", 0.0) * 0.35,
                                    -1.0, 1.0))
    elif speaking:
        pose.set("mouth_open", 0.0)
        pose.set("mouth_form", clamp(pose.get("mouth_form", 0.0) * 0.35,
                                    -1.0, 1.0))
    else:
        pose.set("mouth_open", clamp(pose.get("mouth_open", 0.0), 0.0, 1.0))
        pose.set("mouth_form", clamp(pose.get("mouth_form", 0.0), -1.0, 1.0))

    # Soft-saturate only the large body/head channels; small channels such as
    # blink or mouth form should not be bent by a nonlinearity.
    values = {}
    for channel, value in pose.values.items():
        if channel in ("head_x", "head_y", "head_z", "body_x", "body_y", "body_z",
                       "eye_x", "eye_y", "arm_l", "arm_r", "arm_l_bend",
                       "arm_r_bend", "hand_l", "hand_r", "shoulder_l",
                       "shoulder_r", "leg_l", "leg_r", "leg_l_x", "leg_r_x",
                       "knee_l", "knee_r", "ankle_l", "ankle_r",
                       "leg_l_y", "leg_r_y", "knee_l_y", "knee_r_y",
                       "foot_l", "foot_r", "foot_l_y", "foot_r_y",
                       "foot_swing", "hip", "finger_l",
                       "finger_r", "arm_l_persp", "arm_r_persp", "chest_x",
                       "chest_y", "skirt_x", "skirt_y", "lean_y", "tail", "sleeve_l_x",
                       "sleeve_l_y", "sleeve_r_x", "sleeve_r_y", "ribbon_l_x",
                       "ribbon_l_y", "ribbon_r_x", "ribbon_r_y"):
            value = soft_saturate(value)
        values[channel] = value
    return Pose(clean_pose(values))


def to_parameters(profile: ModelProfile, pose: Pose,
                  velocity: dict[str, float] | None = None) -> dict[str, float]:
    """Convert one semantic pose to raw values for every bound parameter.

    Every bound channel is written, not only the ones present in the pose, so
    a previous action cannot leave a stale value behind.  ``followers`` are
    converted through their *own* ParamSpec range and direction; the primary's
    raw number is never copied directly, because the two parameters often have
    different minima/neutral values.
    """
    out: dict[str, float] = {}
    for channel, spec in profile.by_channel.items():
        if channel in EYE_OPEN_CHANNELS:
            value = pose.get(channel, 1.0)
        else:
            value = pose.get(channel, 0.0)
        raw = spec.to_raw(value)
        if raw == raw and raw not in (float("inf"), float("-inf")):
            out[spec.id] = raw
    speed = velocity or {}
    for channel, rows in profile.followers.items():
        value = pose.get(channel, 0.0)
        if channel in EYE_OPEN_CHANNELS:
            value = pose.get(channel, 1.0)
        change = max(-0.40, min(0.40, float(speed.get(channel, 0.0))))
        for index, (pid, scale) in enumerate(rows, start=1):
            spec = profile.params.get(pid)
            if spec is None:
                continue
            follower_value = value * float(scale)
            if channel in CHAIN_CHANNELS and change:
                # 链式运动：越靠尾端，越晚跟随根部的速度变化，形成 S 形行波。
                follower_value -= change * (0.025 * index)
            raw = spec.to_raw(follower_value)
            if raw == raw and raw not in (float("inf"), float("-inf")):
                out[pid] = raw
    return out


def capability_summary(profile: ModelProfile) -> str:
    """One line for logs / the WebUI."""
    caps = profile.capabilities
    parts = [name for name in ("head", "eyes", "mouth", "body", "arms", "legs")
             if caps.get(name)]
    return f"{profile.tier}: " + ("/".join(parts) if parts else "no standard parameters")
