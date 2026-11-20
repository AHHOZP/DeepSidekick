"""Emotion presets, blending, decay, and a small text lexicon.

Emotion is a *bias*, not a pose.  It is kept as a weighted sum of presets and
decays back to neutral, so a single happy line does not leave the character
smiling for the rest of the stream.  The face and the body consume the same
weights but different channels; see :func:`EmotionState.pose`.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .types import Pose, clamp


@dataclass(frozen=True)
class EmotionPreset:
    name: str
    pose: dict[str, float] = field(default_factory=dict)
    eye_squint: float = 0.0
    duration: float = 9.0
    aliases: tuple[str, ...] = ()
    #: 情绪对说话韵律的调制：兴奋更大，低落/害羞更小。
    prosody_scale: float = 1.0


#: Hand-tuned, model-agnostic presets.  Values are semantic channels, so an
#: unknown model only receives the subset it actually exposes.
DEFAULT_EMOTIONS: dict[str, EmotionPreset] = {
    "neutral": EmotionPreset("neutral", {}, 0.0, 6.0, ("平静", "普通", "neutral", "calm")),
    "happy": EmotionPreset("happy", {
        "head_y": 0.06, "head_z": 0.09, "body_z": 0.055, "body_y": 0.03,
        "arm_l": 0.06, "arm_r": 0.08, "shoulder_l": 0.04, "shoulder_r": 0.05,
        "mouth_form": 0.55, "mouth_open": 0.04,
        "eye_l_smile": 0.50, "eye_r_smile": 0.50,
        "eye_l_open": 0.03, "eye_r_open": 0.03,
        "brow_l_y": 0.20, "brow_r_y": 0.20, "cheek": 0.34,
    }, 0.08, 8.0, ("开心", "高兴", "喜", "快乐", "happy", "joy", "glad"),
        prosody_scale=1.18),
    "excited": EmotionPreset("excited", {
        "head_y": 0.10, "head_z": 0.12, "body_x": 0.05, "body_y": 0.06,
        "body_z": 0.08, "arm_l": 0.12, "arm_r": 0.15,
        "shoulder_l": 0.08, "shoulder_r": 0.10,
        "mouth_form": 0.60, "mouth_open": 0.12,
        "eye_l_smile": 0.62, "eye_r_smile": 0.62,
        "eye_l_open": 0.06, "eye_r_open": 0.06,
        "brow_l_y": 0.30, "brow_r_y": 0.30, "cheek": 0.42,
    }, 0.10, 7.0, ("兴奋", "激动", "excited", "thrilled"),
        prosody_scale=1.32),
    "sad": EmotionPreset("sad", {
        "head_y": -0.12, "head_z": -0.09, "body_x": -0.03, "body_y": -0.06,
        "body_z": -0.05, "shoulder_l": -0.10, "shoulder_r": -0.10,
        "arm_l": -0.04, "arm_r": -0.04,
        "mouth_form": -0.44, "eye_l_open": -0.10, "eye_r_open": -0.10,
        "brow_l_y": -0.14, "brow_r_y": -0.14,
        "brow_l_angle": 0.42, "brow_r_angle": 0.42,
    }, 0.06, 11.0, ("难过", "悲伤", "哀", "sad", "sorrow", "upset"),
        prosody_scale=0.70),
    "angry": EmotionPreset("angry", {
        "head_y": -0.05, "body_x": 0.09, "body_y": 0.04, "body_z": -0.02,
        "arm_l": 0.05, "arm_r": 0.08, "shoulder_l": 0.06, "shoulder_r": 0.06,
        "mouth_form": -0.34, "eye_l_open": -0.05, "eye_r_open": -0.05,
        "brow_l_y": -0.24, "brow_r_y": -0.24,
        "brow_l_angle": -0.55, "brow_r_angle": -0.55,
        "brow_l_form": -0.20, "brow_r_form": -0.20, "cheek": 0.10,
    }, 0.04, 8.0, ("生气", "怒", "angry", "mad", "annoyed"),
        prosody_scale=1.22),
    "shy": EmotionPreset("shy", {
        "head_y": -0.06, "head_z": 0.13, "body_z": 0.06, "body_y": -0.03,
        "shoulder_l": 0.05, "shoulder_r": 0.05,
        "mouth_form": 0.20, "eye_l_open": -0.18, "eye_r_open": -0.18,
        "brow_l_y": 0.10, "brow_r_y": 0.10, "cheek": 0.72,
    }, 0.12, 10.0, ("害羞", "羞", "shy", "blush", "embarrassed"),
        prosody_scale=0.76),
    "think": EmotionPreset("think", {
        "head_y": 0.05, "head_x": -0.18, "head_z": 0.08,
        "body_y": 0.04, "body_x": -0.04,
        "eye_x": -0.35, "eye_y": 0.28,
        "mouth_form": -0.08, "mouth_open": 0.0,
        "brow_l_y": 0.12, "brow_r_y": 0.12,
        "brow_l_angle": 0.20, "brow_r_angle": 0.12,
    }, 0.02, 7.0, ("思考", "想", "think", "thinking", "hmm", "ponder"),
        prosody_scale=0.82),
    "surprised": EmotionPreset("surprised", {
        "head_y": 0.12, "body_x": 0.05, "body_y": 0.03,
        "shoulder_l": 0.08, "shoulder_r": 0.08,
        "mouth_open": 0.22, "mouth_form": 0.08,
        "eye_l_open": 0.12, "eye_r_open": 0.12,
        "brow_l_y": 0.45, "brow_r_y": 0.45,
        "brow_l_angle": 0.25, "brow_r_angle": 0.25,
    }, 0.0, 4.5, ("惊讶", "惊", "surprised", "shock", "whoa"),
        prosody_scale=1.24),
    "confused": EmotionPreset("confused", {
        "head_x": 0.08, "head_y": -0.03, "head_z": 0.15,
        "body_x": 0.04, "body_z": 0.03,
        "eye_x": 0.15, "eye_y": 0.05,
        "mouth_form": -0.10,
        "brow_l_y": 0.10, "brow_r_y": -0.12,
        "brow_l_angle": -0.30, "brow_r_angle": 0.35,
    }, 0.02, 6.0, ("困惑", "疑惑", "confused", "puzzled", "huh"),
        prosody_scale=0.85),
    "awkward": EmotionPreset("awkward", {
        "head_x": -0.14, "head_y": 0.02, "head_z": -0.12,
        "body_x": -0.04, "body_z": -0.04, "body_y": -0.03,
        "shoulder_l": -0.05, "shoulder_r": -0.05,
        "mouth_form": -0.20, "eye_l_open": -0.10, "eye_r_open": -0.10,
        "brow_l_y": -0.08, "brow_r_y": -0.08, "cheek": 0.18,
    }, 0.04, 6.0, ("尴尬", "无语", "awkward", "cringe", "speechless"),
        prosody_scale=0.72),
}

_EMOTION_ALIASES: dict[str, str] = {}
for _name, _preset in DEFAULT_EMOTIONS.items():
    _EMOTION_ALIASES[_name.lower()] = _name
    for _alias in _preset.aliases:
        _EMOTION_ALIASES[str(_alias).lower()] = _name


def resolve_emotion(name: str) -> str | None:
    key = str(name or "").strip().lower()
    return _EMOTION_ALIASES.get(key)


@dataclass
class EmotionState:
    """Weighted, decaying emotion layer."""

    presets: dict[str, EmotionPreset] = field(default_factory=lambda: dict(DEFAULT_EMOTIONS))
    weights: dict[str, float] = field(default_factory=dict)
    targets: dict[str, float] = field(default_factory=dict)
    deadline: dict[str, float] = field(default_factory=dict)
    clock: float = 0.0

    def set(self, name: str, intensity: float = 1.0, duration: float | None = None,
            *, now: float | None = None) -> bool:
        canonical = resolve_emotion(name)
        if canonical is None or canonical == "neutral":
            return False
        if canonical not in self.presets:
            return False
        weight = clamp(float(intensity), 0.0, 1.0)
        if weight <= 0.0:
            return False
        self.targets[canonical] = max(self.targets.get(canonical, 0.0), weight)
        ttl = float(duration if duration is not None else self.presets[canonical].duration)
        self.deadline[canonical] = (self.clock if now is None else float(now)) + max(0.4, ttl)
        return True

    def clear(self) -> None:
        self.targets.clear()
        self.deadline.clear()

    def update(self, dt: float, *, now: float | None = None) -> None:
        dt = max(0.0, min(0.2, float(dt)))
        if now is not None:
            self.clock = float(now)
        else:
            self.clock += dt
        for name in list(self.targets):
            if self.clock >= self.deadline.get(name, 0.0):
                # Fade out over roughly 1.6 s; remove when imperceptible.
                self.targets[name] = max(0.0, self.targets.get(name, 0.0) - dt / 1.6)
                if self.targets[name] <= 0.001:
                    self.targets.pop(name, None)
                    self.deadline.pop(name, None)

        # Attack is faster than release, like a facial expression.
        for name in set(self.weights) | set(self.targets):
            target = self.targets.get(name, 0.0)
            current = self.weights.get(name, 0.0)
            tau = 0.22 if target > current else 0.85
            alpha = 1.0 - math.exp(-dt / max(1e-4, tau))
            value = current + (target - current) * alpha
            if value < 0.001:
                self.weights.pop(name, None)
            else:
                self.weights[name] = value

    def pose(self) -> Pose:
        out = Pose()
        for name, weight in self.weights.items():
            preset = self.presets.get(name)
            if preset is None or weight <= 0.0:
                continue
            out.merge(Pose(dict(preset.pose)), weight)
        # Neutral is the absence of a preset, not a preset with zeroes.
        return out

    @property
    def eye_squint(self) -> float:
        total = 0.0
        for name, weight in self.weights.items():
            preset = self.presets.get(name)
            if preset:
                total += preset.eye_squint * weight
        return clamp(total, 0.0, 1.0)

    @property
    def prosody_scale(self) -> float:
        """情绪对说话韵律的倍率；没有情绪时保持 1.0。"""
        weight_sum = 0.0
        total = 0.0
        for name, weight in self.weights.items():
            preset = self.presets.get(name)
            if preset is None or weight <= 0.0:
                continue
            weight_sum += weight
            total += preset.prosody_scale * weight
        if weight_sum <= 1e-6:
            return 1.0
        return clamp(total / weight_sum, 0.65, 1.35)

    @property
    def dominant(self) -> str:
        if not self.weights:
            return "neutral"
        return max(self.weights, key=self.weights.get)

    def report(self) -> dict:
        return {
            "dominant": self.dominant,
            "weights": {k: round(v, 3) for k, v in sorted(self.weights.items())},
        }


# ── text fallback ─────────────────────────────────────
# The LLM should emit an explicit <ACT> tag.  When it does not, this lexicon
# still catches the obvious cases without a model call.  Chinese matching is
# substring-based; English matching is whole-word to avoid "sad" in "saddle".
_LEXICON: list[tuple[str, tuple[str, ...]]] = [
    ("happy", ("开心", "高兴", "太好了", "哈哈", "笑死", "好耶", "喜欢", "爱了", "nice", "great", "love", "awesome", "haha")),
    ("excited", ("兴奋", "激动", "冲", "太强了", "牛", "amazing", "insane", "hype")),
    ("sad", ("难过", "伤心", "可惜", "唉", "遗憾", "输了", "sad", "unfortunately", "pity")),
    ("angry", ("生气", "气死", "烦", "离谱", "怎么这样", "angry", "annoying", "ridiculous")),
    ("shy", ("害羞", "不好意思", "脸红", "别夸", "shy", "blush")),
    ("think", ("让我想想", "可能", "也许", "估计", "思考", "hmm", "maybe", "perhaps")),
    ("surprised", ("居然", "竟然", "没想到", "真的假的", "哇", "surprised", "wow", "really")),
    ("confused", ("啊？", "什么？", "不懂", "困惑", "confused", "huh")),
    ("awkward", ("尴尬", "无语", "呃", "awkward", "cringe")),
]

_WORD_SPLIT = re.compile(r"[^a-z']+")


def infer_emotion(text: str) -> tuple[str, float]:
    """Best-effort text emotion for when the LLM omits a tag."""
    source = str(text or "")
    lowered = source.lower()
    words = {w for w in _WORD_SPLIT.split(lowered) if w}
    best = "neutral"
    best_hits = 0
    for name, cues in _LEXICON:
        hits = 0
        for cue in cues:
            if cue.isascii():
                if cue in words:
                    hits += 1
            elif cue in source:
                hits += 1
        if hits > best_hits:
            best, best_hits = name, hits
    if best_hits == 0:
        if "?" in source or "？" in source:
            return "confused", 0.45
        if "!" in source or "！" in source:
            return "excited", 0.40
        return "neutral", 0.0
    intensity = 1.0 - 0.55 ** best_hits
    if "!" in source or "！" in source:
        intensity += 0.12
    return best, clamp(intensity, 0.0, 1.0)

