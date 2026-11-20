"""Breathing curve and occasional sighs.

Breathing is the slowest visible signal that a character is alive.  It is kept
separate from the noise layer because it should stay *roughly* periodic; the
noise is what breaks the exact periodicity.  A sigh is a rarer, deeper cycle
that reads as emotion without requiring an expression change.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .rng import Rng
from .types import Pose


def breath_pose(value: float) -> Pose:
    """呼吸的躯干/肩/头联动；``value`` 是 0..1 的呼吸相位。

    胸部不在这一层：它要比躯干晚约 0.2 s，由 compositor 用滞后的呼吸值单独
    写入，否则胸和身体会像同一根刚体一起抬。
    """
    curve = (float(value) - 0.5)
    return Pose({
        "body_y": curve * 0.18,
        "body_z": curve * 0.16,
        "body_x": curve * 0.025,
        "shoulder_l": curve * 0.14,
        "shoulder_r": curve * 0.14,
        "head_y": curve * 0.05,
    })


def breath_value(now: float, period: float = 4.2, inhale_ratio: float = 0.4) -> float:
    """Half-cosine inhale, half-cosine exhale, in ``0..1``.

    ``inhale_ratio`` is the fraction of the cycle spent inhaling.  Real
    breathing is not symmetric: inhale is usually a little shorter.
    """
    period = max(0.8, float(period))
    phase = (float(now) % period) / period
    return breath_phase(phase, inhale_ratio)


def breath_phase(phase: float, inhale_ratio: float = 0.4) -> float:
    """按 0..1 相位算呼吸值；累加相位时用它，避免切换周期跳变。"""
    phase = float(phase) % 1.0
    ratio = min(0.85, max(0.15, float(inhale_ratio)))
    if phase < ratio:
        return 0.5 - 0.5 * math.cos(math.pi * phase / ratio)
    return 0.5 + 0.5 * math.cos(math.pi * (phase - ratio) / (1.0 - ratio))


@dataclass
class BreathConfig:
    period: float = 4.2
    inhale_ratio: float = 0.4
    speech_scale: float = 0.72
    sigh_min_gap: float = 26.0
    sigh_max_gap: float = 58.0
    sigh_duration: float = 2.4


@dataclass
class BreathSample:
    value: float
    period: float
    sigh: float = 0.0


class Breath:
    def __init__(self, seed: int = 1, config: BreathConfig | None = None) -> None:
        self.config = config or BreathConfig()
        self.rng = Rng(seed + 17)
        self._next_sigh = self.rng.uniform(self.config.sigh_min_gap, self.config.sigh_max_gap)
        self._sigh_left = 0.0
        self._sigh_total = 0.0
        self._period = self.config.period * self.rng.uniform(0.92, 1.08)
        self._phase = 0.0
        self._last = BreathSample(0.0, self._period)

    def update(self, now: float, dt: float, *, speaking: bool = False) -> BreathSample:
        dt = max(0.0, min(0.2, float(dt)))
        self._next_sigh -= dt
        if self._next_sigh <= 0.0 and self._sigh_left <= 0.0:
            self._sigh_left = self.config.sigh_duration
            self._sigh_total = self.config.sigh_duration
            self._next_sigh = self.rng.uniform(
                self.config.sigh_min_gap, self.config.sigh_max_gap)

        period = self._period
        scale = 1.0
        if speaking:
            period *= 0.92
            scale = self.config.speech_scale

        # 累加相位：说话/静音切换周期时不会因为 now % period 跳变。
        self._phase = (self._phase + dt / max(1e-6, period)) % 1.0
        value = breath_phase(self._phase, self.config.inhale_ratio) * scale
        sigh = 0.0
        if self._sigh_left > 0.0:
            self._sigh_left = max(0.0, self._sigh_left - dt)
            # One raised-cosine hump; it adds a deeper inhale and a slower
            # release without changing the underlying rhythm.
            phase = 1.0 - self._sigh_left / max(1e-6, self._sigh_total)
            sigh = math.sin(math.pi * max(0.0, min(1.0, phase))) ** 1.4
            value += 0.28 * sigh

        self._last = BreathSample(max(0.0, min(1.0, value)), period, sigh)
        return self._last

    @property
    def sample(self) -> BreathSample:
        return self._last
