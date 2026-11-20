"""Discrete whole-body posture anchors and Contrapposto.

A real person does not hold the neck at exact zero: they settle into a small
number of comfortable resting poses (chin slightly tilted, weight on one hip,
head turned toward chat), hold each for a few seconds, then shift.  This module
chooses those anchors; the engine's springs make the transition read as weight
moving through the body instead of a slider jump.

Each anchor is a normalized whole-body target.  Head roll and body roll are
deliberately opposite-signed in the listening/sway states: that S-curve is the
2D equivalent of Contrapposto and is what keeps a tilt from looking like a
broken neck.
"""
from __future__ import annotations

from dataclasses import dataclass

from .rng import Rng
from .types import Context, Pose, clamp


POSTURE_STATES: dict[str, dict[str, float]] = {
    "center_relax": {
        "head_x": 0.04, "head_y": -0.03, "head_z": 0.09,
        "body_x": -0.03, "body_y": -0.02, "body_z": -0.06,
        "arm_l": -0.02, "arm_r": 0.01,
        "shoulder_l": -0.01, "shoulder_r": 0.01,
    },
    "lean_in_focus": {
        "head_x": 0.08, "head_y": 0.12, "head_z": -0.14,
        "body_x": 0.14, "body_y": 0.26, "body_z": 0.12,
        "arm_l": 0.03, "arm_r": 0.04,
        "arm_l_bend": 0.03, "arm_r_bend": 0.04,
        "shoulder_l": 0.03, "shoulder_r": 0.04,
    },
    "tilt_listen_l": {
        "head_x": -0.24, "head_y": 0.06, "head_z": -0.34,
        "body_x": -0.12, "body_y": 0.06, "body_z": 0.18,
        "arm_l": 0.04, "arm_r": -0.01,
        "shoulder_l": -0.02, "shoulder_r": 0.03,
    },
    "tilt_listen_r": {
        "head_x": 0.24, "head_y": 0.06, "head_z": 0.34,
        "body_x": 0.12, "body_y": 0.06, "body_z": -0.18,
        "arm_l": -0.01, "arm_r": 0.04,
        "shoulder_l": 0.03, "shoulder_r": -0.02,
    },
    "sway_casual_l": {
        "head_x": -0.32, "head_y": -0.06, "head_z": 0.22,
        "body_x": -0.22, "body_y": -0.08, "body_z": -0.26,
        "arm_l": 0.06, "arm_r": 0.02, "hip": -0.18,
    },
    "sway_casual_r": {
        "head_x": 0.32, "head_y": -0.06, "head_z": -0.22,
        "body_x": 0.22, "body_y": -0.08, "body_z": 0.26,
        "arm_l": 0.02, "arm_r": 0.06, "hip": 0.18,
    },
    "look_chat": {
        "head_x": 0.42, "head_y": -0.12, "head_z": 0.18,
        "body_x": 0.26, "body_y": 0.04, "body_z": 0.20,
        "arm_l": 0.02, "arm_r": 0.08, "arm_r_bend": 0.04,
    },
}

_IDLE_WEIGHTS = {
    "center_relax": 0.30, "lean_in_focus": 0.12,
    "tilt_listen_l": 0.12, "tilt_listen_r": 0.12,
    "sway_casual_l": 0.12, "sway_casual_r": 0.12,
    "look_chat": 0.10,
}
_LISTEN_WEIGHTS = {
    "tilt_listen_l": 0.35, "tilt_listen_r": 0.35,
    "lean_in_focus": 0.20, "center_relax": 0.10,
}
_SPEAK_WEIGHTS = {
    "lean_in_focus": 0.30, "sway_casual_l": 0.20, "sway_casual_r": 0.20,
    "tilt_listen_l": 0.15, "tilt_listen_r": 0.15,
}


def _weighted(rng: Rng, weights: dict[str, float]) -> str:
    total = sum(max(0.0, w) for w in weights.values())
    pick = rng.uniform(0.0, total)
    chosen = next(iter(weights))
    for name, weight in weights.items():
        pick -= max(0.0, weight)
        if pick <= 0.0:
            chosen = name
            break
    return chosen


@dataclass
class PostureAnchor:
    """Pick a whole-body anchor every 1.4–5.2 s, shaped by context."""

    seed: int = 1
    state: str = "center_relax"
    _left: float = 0.0
    _amplitude: float = 1.0
    _pose: Pose = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.rng = Rng(int(self.seed) + 511)
        self._pose = self._scaled(self.state)
        self._left = self.rng.uniform(2.4, 5.2)

    def _scaled(self, name: str) -> Pose:
        values = POSTURE_STATES.get(name, {})
        return Pose({channel: value * self._amplitude
                     for channel, value in values.items()})

    def _weights(self, context: Context) -> dict[str, float]:
        if context.user_speaking:
            weights = dict(_LISTEN_WEIGHTS)
        elif context.speaking:
            weights = dict(_SPEAK_WEIGHTS)
        else:
            weights = dict(_IDLE_WEIGHTS)
        if clamp(context.chat_activity, 0.0, 1.0) > 0.3:
            weights["look_chat"] = weights.get("look_chat", 0.0) + 0.45
        return weights

    def _dwell(self, context: Context) -> float:
        if context.speaking:
            return self.rng.uniform(1.4, 2.8)
        if context.user_speaking:
            return self.rng.uniform(2.0, 3.8)
        return self.rng.uniform(2.4, 5.2)

    def _switch(self, context: Context) -> None:
        weights = self._weights(context)
        # Force a weight move: the same anchor twice looks like a frozen body.
        weights.pop(self.state, None)
        if not weights:
            weights = {"center_relax": 1.0}
        self.state = _weighted(self.rng, weights)
        self._amplitude = self.rng.uniform(0.85, 1.15)
        self._pose = self._scaled(self.state)
        self._left = self._dwell(context)

    def update(self, dt: float, context: Context) -> Pose:
        self._left -= max(0.0, float(dt))
        if self._left <= 0.0:
            self._switch(context)
        return self._pose.copy()

    def reset(self) -> None:
        self.state = "center_relax"
        self._amplitude = 1.0
        self._pose = self._scaled(self.state)
        self._left = self.rng.uniform(2.4, 5.2)
