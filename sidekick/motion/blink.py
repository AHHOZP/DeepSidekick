"""Asymmetric, non-metronomic blinking.

A real blink is not a sine wave: the lid closes in roughly 70 ms, stays shut
for a few frames, then opens over 140-220 ms.  About one blink in eight is a
double blink, one in eight is incomplete, and the two eyes never move in
perfect lockstep.  Those three details are what separate a character from a
metronome.

The scheduler emits a multiplicative openness factor, so it can be layered on
top of any emotion or expression without owning ``ParamEyeLOpen``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .rng import Rng


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, float(t)))
    return t * t * (3.0 - 2.0 * t)


@dataclass
class BlinkConfig:
    min_gap: float = 1.9
    max_gap: float = 7.2
    mean_gap: float = 4.2
    close_seconds: float = 0.072
    hold_seconds: float = 0.045
    open_seconds: float = 0.165
    double_chance: float = 0.12
    incomplete_chance: float = 0.11
    incomplete_min: float = 0.55
    incomplete_max: float = 0.86
    double_gap_min: float = 0.16
    double_gap_max: float = 0.30
    eye_delay_max: float = 0.026
    #: profile 的 blink_scale：只放大/缩小幅度，间隔不变。
    amplitude_scale: float = 1.0


@dataclass
class BlinkState:
    factor_l: float = 1.0
    factor_r: float = 1.0
    blinking: bool = False
    #: ``idle`` / ``close`` / ``hold`` / ``open``
    phase: str = "idle"


@dataclass
class _Blink:
    amplitude_l: float
    amplitude_r: float
    close: float
    hold: float
    open: float
    delay_l: float
    delay_r: float
    elapsed: float = 0.0

    @property
    def total(self) -> float:
        return max(self.delay_l, self.delay_r) + self.close + self.hold + self.open


class BlinkScheduler:
    """Deterministic blink generator driven by wall-clock deltas."""

    def __init__(self, seed: int = 1, config: BlinkConfig | None = None) -> None:
        self.config = config or BlinkConfig()
        self.rng = Rng(seed)
        self._next_in = self._sample_gap()
        self._current: _Blink | None = None
        self._double_pending = False
        self._next_is_double = False
        self._double_gap = 0.0
        self._last_state = BlinkState()

    # ── schedule ──────────────────────────────────────
    def _sample_gap(self) -> float:
        cfg = self.config
        # Log-normal around mean_gap, clipped.  A plain uniform distribution
        # makes the rhythm feel like a slot machine.
        sigma = 0.34
        mu = math.log(max(0.5, cfg.mean_gap))
        gap = math.exp(mu + self.rng.gauss(0.0, sigma))
        return max(cfg.min_gap, min(cfg.max_gap, gap))

    def _new_blink(self, *, double: bool = False) -> _Blink:
        cfg = self.config
        if double:
            amplitude = self.rng.uniform(0.72, 0.95)
        elif self.rng.chance(cfg.incomplete_chance):
            amplitude = self.rng.uniform(cfg.incomplete_min, cfg.incomplete_max)
        else:
            amplitude = self.rng.uniform(0.92, 1.0)
        # The two eyes get slightly different amplitudes and timings.
        amplitude = min(1.0, amplitude * max(0.0, float(cfg.amplitude_scale)))
        amp_l = amplitude * self.rng.uniform(0.96, 1.0)
        amp_r = amplitude * self.rng.uniform(0.96, 1.0)
        close = cfg.close_seconds * self.rng.uniform(0.86, 1.18)
        hold = cfg.hold_seconds * self.rng.uniform(0.55, 1.2)
        open_ = cfg.open_seconds * self.rng.uniform(0.82, 1.25)
        return _Blink(
            amplitude_l=amp_l,
            amplitude_r=amp_r,
            close=close,
            hold=hold,
            open=open_,
            delay_l=self.rng.uniform(0.0, cfg.eye_delay_max),
            delay_r=self.rng.uniform(0.0, cfg.eye_delay_max),
            elapsed=0.0,
        )

    # ── per-eye curve ─────────────────────────────────
    @staticmethod
    def _curve(blink: _Blink, elapsed: float, delay: float, amplitude: float) -> float:
        t = elapsed - delay
        if t <= 0.0:
            return 1.0
        if t < blink.close:
            return 1.0 - amplitude * _smoothstep(t / blink.close)
        t -= blink.close
        if t < blink.hold:
            return 1.0 - amplitude
        t -= blink.hold
        if t < blink.open:
            return 1.0 - amplitude * (1.0 - _smoothstep(t / blink.open))
        return 1.0

    # ── update ────────────────────────────────────────
    def update(self, dt: float, *, squint: float = 0.0, speaking: bool = False) -> BlinkState:
        dt = max(0.0, min(0.1, float(dt)))
        squint = max(0.0, min(1.0, float(squint)))

        if self._current is None:
            self._next_in -= dt
            if self._next_in <= 0.0:
                double = self._next_is_double
                self._current = self._new_blink(double=double)
                if double:
                    # 双击的第二下不再重抽概率，否则会链成三连眨。
                    self._next_is_double = False
                    self._double_pending = False
                else:
                    self._double_pending = self.rng.chance(
                        self.config.double_chance)
                self._double_gap = self.rng.uniform(
                    self.config.double_gap_min, self.config.double_gap_max)
        else:
            self._current.elapsed += dt
            if self._current.elapsed >= self._current.total:
                self._current = None
                if self._double_pending:
                    self._double_pending = False
                    self._next_is_double = True
                    self._next_in = self._double_gap
                else:
                    self._next_is_double = False
                    self._next_in = self._sample_gap() * (1.0 if not speaking else 1.06)

        if self._current is None:
            # ``eye_squint`` is a sustained squint, not a blink.
            base = 1.0 - 0.38 * squint
            self._last_state = BlinkState(base, base, False, "idle")
            return self._last_state

        blink = self._current
        curve_l = self._curve(blink, blink.elapsed, blink.delay_l, blink.amplitude_l)
        curve_r = self._curve(blink, blink.elapsed, blink.delay_r, blink.amplitude_r)
        # A sustained squint lowers the base the blink returns to; it never
        # closes the eye by itself.
        base = 1.0 - 0.38 * squint
        factor_l = max(0.0, base * curve_l)
        factor_r = max(0.0, base * curve_r)
        phase = "close" if blink.elapsed < blink.close else (
            "hold" if blink.elapsed < blink.close + blink.hold else "open")
        self._last_state = BlinkState(factor_l, factor_r, True, phase)
        return self._last_state

    @property
    def state(self) -> BlinkState:
        return self._last_state
