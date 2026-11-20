"""Continuous, non-periodic noise for idle drift.

``sin(t)`` is periodic and reads as a machine.  Two octaves of smooth value
noise are enough to make a head wander without visible repetition: the slow
octave provides the destination, the quiet faster octave provides life.  This
is deliberately dependency-free and deterministic (same time + seed -> same
value), so a dropped frame cannot change the character of the motion.
"""
from __future__ import annotations

import math


def _hash01(n: int, seed: int) -> float:
    """Integer hash -> ``[0, 1)``.  Stable across platforms."""
    h = (int(n) & 0xFFFFFFFF) ^ ((int(seed) & 0xFFFFFFFF) * 0x9E3779B1)
    h &= 0xFFFFFFFF
    h = ((h ^ (h >> 16)) * 0x21F0AAAD) & 0xFFFFFFFF
    h = ((h ^ (h >> 15)) * 0x735A2D97) & 0xFFFFFFFF
    h ^= h >> 15
    return (h & 0xFFFFFFFF) / 4294967296.0


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, float(t)))
    return t * t * (3.0 - 2.0 * t)


def value_noise(time: float, frequency: float, seed: int) -> float:
    """Smooth 1-D value noise in ``[-1, 1]`` with lattice spacing ``1/f``."""
    x = float(time) * float(frequency)
    i = math.floor(x)
    f = x - i
    a = _hash01(i, seed) * 2.0 - 1.0
    b = _hash01(i + 1, seed) * 2.0 - 1.0
    return a + (b - a) * smoothstep(f)


def fbm(
    time: float,
    frequency: float,
    seed: int,
    *,
    octaves: int = 2,
    persistence: float = 0.28,
    lacunarity: float = 2.1,
) -> float:
    """Fractal Brownian motion built from :func:`value_noise`.

    The first octave carries most of the amplitude; later octaves are quiet
    on purpose.  A loud fast octave is high-frequency wobble, not life.
    """
    total = 0.0
    amplitude = 1.0
    norm = 0.0
    freq = float(frequency)
    for octave in range(max(1, int(octaves))):
        total += amplitude * value_noise(time, freq, seed + octave * 7919)
        norm += amplitude
        amplitude *= float(persistence)
        freq *= float(lacunarity)
    return total / norm if norm else 0.0


class NoiseChannel:
    """One signed, seeded noise channel with a configurable frequency."""

    __slots__ = ("frequency", "seed", "octaves", "amplitude")

    def __init__(self, frequency: float, seed: int, *, amplitude: float = 1.0, octaves: int = 2) -> None:
        self.frequency = float(frequency)
        self.seed = int(seed)
        self.octaves = int(octaves)
        self.amplitude = float(amplitude)

    def at(self, time: float) -> float:
        return self.amplitude * fbm(time, self.frequency, self.seed, octaves=self.octaves)


class NoiseBank:
    """A named collection of independent noise channels."""

    def __init__(self, channels: dict[str, NoiseChannel]) -> None:
        self.channels = dict(channels)

    def sample(self, time: float) -> dict[str, float]:
        return {name: channel.at(time) for name, channel in self.channels.items()}


def default_idle_noise(seed: int = 1) -> NoiseBank:
    """The idle spectrum used by :class:`livemotion.engine.MotionEngine`.

    Frequencies are in Hz.  Left/right head movement is allowed more amplitude
    than pitch: a head that keeps looking up and down reads as restless.
    """
    specs = {
        "head_x": (0.18, 0.085, 101),
        "head_y": (0.13, 0.040, 202),
        "head_z": (0.11, 0.055, 303),
        "body_x": (0.09, 0.030, 404),
        "body_y": (0.07, 0.018, 505),
        "body_z": (0.08, 0.045, 606),
        "eye_x": (0.55, 0.020, 707),
        "eye_y": (0.43, 0.014, 808),
        "arm_l": (0.16, 0.030, 909),
        "arm_r": (0.16, 0.030, 1001),
        "arm_l_bend": (0.18, 0.022, 1110),
        "arm_r_bend": (0.18, 0.022, 1211),
        "hand_l": (0.20, 0.015, 1312),
        "hand_r": (0.20, 0.015, 1413),
        "shoulder_l": (0.14, 0.020, 1514),
        "shoulder_r": (0.14, 0.020, 1615),
        "finger_l": (0.22, 0.012, 1716),
        "finger_r": (0.22, 0.012, 1817),
        "arm_l_persp": (0.10, 0.016, 1918),
        "arm_r_persp": (0.10, 0.016, 2019),
        "chest_x": (0.06, 0.018, 2120),
        "chest_y": (0.06, 0.018, 2221),
        # 腿/脚/胯是很多游戏模型最灵活的部分；没有这些通道时
        # compose/to_parameters 会自动忽略，不会凭空造参数。
        "leg_l": (0.13, 0.055, 3433),
        "leg_r": (0.13, 0.055, 3534),
        "leg_l_x": (0.10, 0.040, 3635),
        "leg_r_x": (0.10, 0.040, 3736),
        "knee_l": (0.11, 0.045, 3837),
        "knee_r": (0.11, 0.045, 3938),
        "ankle_l": (0.13, 0.050, 4039),
        "ankle_r": (0.13, 0.050, 4139),
        "leg_l_y": (0.10, 0.040, 4239),
        "leg_r_y": (0.10, 0.040, 4339),
        "knee_l_y": (0.08, 0.035, 4439),
        "knee_r_y": (0.08, 0.035, 4539),
        "foot_l_y": (0.09, 0.040, 4639),
        "foot_r_y": (0.09, 0.040, 4739),
        "foot_l": (0.12, 0.045, 4839),
        "foot_r": (0.12, 0.045, 4939),
        "foot_swing": (0.10, 0.030, 5039),
        "hip": (0.08, 0.030, 5139),
        "skirt_x": (0.10, 0.022, 2322),
        "skirt_y": (0.08, 0.018, 2423),
        "tail": (0.14, 0.020, 2524),
        "sleeve_l_x": (0.10, 0.020, 2625),
        "sleeve_l_y": (0.08, 0.018, 2726),
        "sleeve_r_x": (0.10, 0.020, 2827),
        "sleeve_r_y": (0.08, 0.018, 2928),
        "ribbon_l_x": (0.12, 0.024, 3029),
        "ribbon_l_y": (0.10, 0.020, 3130),
        "ribbon_r_x": (0.12, 0.024, 3231),
        "ribbon_r_y": (0.10, 0.020, 3332),
        "hair_front": (0.75, 0.035, 1102),
        "hair_side": (0.63, 0.030, 1203),
        "hair_back": (0.55, 0.030, 5203),
    }
    return NoiseBank({
        name: NoiseChannel(freq, seed + offset, amplitude=amp)
        for name, (freq, amp, offset) in specs.items()
    })
