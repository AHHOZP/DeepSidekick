"""Second-order dynamics (spring-damper) used by every moving channel.

The recurrence is the one popularised by t3ssel8r:

    omega = 2*pi*f
    k1 = zeta / (pi*f)          == 2*zeta/omega
    k2 = 1 / omega^2
    k3 = r*zeta / (2*pi*f)      == r*zeta/omega

    v += dt * (target + k3*target_velocity - x - k1*v) / k2
    x += dt * v

The three intuitive parameters are:

* ``frequency`` ``f``: how eagerly the value chases its target.
* ``damping`` ``zeta``: 1 is critical; below 1 overshoots.
* ``response`` ``r``: feed-forward of target velocity.  ``0`` lags, ``1``
  reacts immediately, ``>1`` kicks, ``<0`` anticipates by pulling back.

The explicit update is only conditionally stable, so :class:`SecondOrder`
substeps at ``1/120 s`` and clamps the frame time.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class SecondOrder:
    frequency: float = 2.0
    damping: float = 0.9
    response: float = 0.0
    position: float = 0.0
    velocity: float = 0.0
    max_substep: float = 1.0 / 120.0
    snap_position: float = 0.0015
    snap_velocity: float = 0.0015

    def reset(self, position: float = 0.0) -> None:
        self.position = float(position)
        self.velocity = 0.0

    def update(self, target: float, dt: float, target_velocity: float = 0.0) -> float:
        dt = float(dt)
        if not math.isfinite(dt) or dt <= 0.0:
            return self.position
        # A backgrounded window or a debugger pause must not launch the spring.
        dt = min(dt, 0.1)
        target = float(target)
        if not math.isfinite(target):
            return self.position

        frequency = max(0.01, float(self.frequency))
        damping = max(0.0, float(self.damping))
        response = float(self.response)
        omega = 2.0 * math.pi * frequency
        k1 = damping / (math.pi * frequency)
        k2 = 1.0 / (omega * omega)
        k3 = response * damping / (2.0 * math.pi * frequency)

        steps = max(1, int(math.ceil(dt / self.max_substep)))
        step = dt / steps
        x = self.position
        v = self.velocity
        for _ in range(steps):
            # Semi-implicit Euler: update velocity first, then position.
            v += step * (target + k3 * target_velocity - x - k1 * v) / k2
            x += step * v

        if not math.isfinite(x) or not math.isfinite(v):
            # A pathological model/parameter must not poison every later frame.
            self.position = target
            self.velocity = 0.0
            return self.position

        if abs(target - x) < self.snap_position and abs(v) < self.snap_velocity:
            x = target
            v = 0.0
        self.position = x
        self.velocity = v
        return x


@dataclass
class SpringBank:
    """One second-order spring per named channel."""

    specs: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    springs: dict[str, SecondOrder] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, (frequency, damping, response) in self.specs.items():
            self.springs[name] = SecondOrder(frequency=frequency, damping=damping, response=response)

    def step(self, targets: dict[str, float], dt: float) -> dict[str, float]:
        out: dict[str, float] = {}
        for name, spring in self.springs.items():
            target = float(targets.get(name, 0.0))
            if (target == 0.0 and abs(spring.position) < 1e-6
                    and abs(spring.velocity) < 1e-6):
                out[name] = 0.0
                continue
            out[name] = spring.update(target, dt)
        return out

    def reset(self) -> None:
        for spring in self.springs.values():
            spring.reset(0.0)


def default_springs(frequency_scale: float = 1.0) -> SpringBank:
    """Per-channel inertia for a human-sized upper body.

    The cascade is deliberately under-damped: eyes answer first, hands next,
    the head has a light overshoot, and the torso follows about 120 ms later.
    That visible lag is what turns a stack of sliders into a moving body.
    ``frequency_scale`` comes from profile.tuning.
    """
    scale = max(0.1, float(frequency_scale))
    return SpringBank({
        "eye_x": (6.5 * scale, 0.78, 0.60),
        "eye_y": (6.5 * scale, 0.78, 0.60),
        "head_x": (1.48 * scale, 0.60, 0.45),
        "head_y": (1.48 * scale, 0.60, 0.45),
        "head_z": (1.25 * scale, 0.56, 0.40),
        "body_x": (0.88 * scale, 0.65, 0.30),
        "body_y": (0.88 * scale, 0.65, 0.30),
        "body_z": (0.88 * scale, 0.65, 0.30),
        "lean_y": (0.92 * scale, 0.62, 0.30),
        "arm_l": (1.65 * scale, 0.55, 0.50),
        "arm_r": (1.65 * scale, 0.55, 0.50),
        "arm_l_bend": (1.85 * scale, 0.54, 0.52),
        "arm_r_bend": (1.85 * scale, 0.54, 0.52),
        "hand_l": (1.90 * scale, 0.52, 0.55),
        "hand_r": (1.90 * scale, 0.52, 0.55),
        "finger_l": (2.20 * scale, 0.55, 0.45),
        "finger_r": (2.20 * scale, 0.55, 0.45),
        "arm_l_persp": (1.80 * scale, 0.58, 0.45),
        "arm_r_persp": (1.80 * scale, 0.58, 0.45),
        "shoulder_l": (1.90 * scale, 0.52, 0.55),
        "shoulder_r": (1.90 * scale, 0.52, 0.55),
        "chest_x": (1.10 * scale, 0.68, 0.25),
        "chest_y": (1.10 * scale, 0.68, 0.25),
        "skirt_x": (1.30 * scale, 0.66, 0.25),
        "skirt_y": (1.30 * scale, 0.66, 0.25),
        "tail": (1.20 * scale, 0.64, 0.30),
        "sleeve_l_x": (1.35 * scale, 0.62, 0.30),
        "sleeve_l_y": (1.35 * scale, 0.62, 0.30),
        "sleeve_r_x": (1.35 * scale, 0.62, 0.30),
        "sleeve_r_y": (1.35 * scale, 0.62, 0.30),
        "ribbon_l_x": (1.50 * scale, 0.58, 0.35),
        "ribbon_l_y": (1.50 * scale, 0.58, 0.35),
        "ribbon_r_x": (1.50 * scale, 0.58, 0.35),
        "ribbon_r_y": (1.50 * scale, 0.58, 0.35),
        "leg_l": (1.15 * scale, 0.64, 0.30),
        "leg_r": (1.15 * scale, 0.64, 0.30),
        "leg_l_x": (1.10 * scale, 0.64, 0.30),
        "leg_r_x": (1.10 * scale, 0.64, 0.30),
        "knee_l": (1.35 * scale, 0.62, 0.34),
        "knee_r": (1.35 * scale, 0.62, 0.34),
        "ankle_l": (1.55 * scale, 0.56, 0.38),
        "ankle_r": (1.55 * scale, 0.56, 0.38),
        "leg_l_y": (1.15 * scale, 0.64, 0.30),
        "leg_r_y": (1.15 * scale, 0.64, 0.30),
        "knee_l_y": (1.30 * scale, 0.62, 0.34),
        "knee_r_y": (1.30 * scale, 0.62, 0.34),
        "foot_l_y": (1.45 * scale, 0.58, 0.38),
        "foot_r_y": (1.45 * scale, 0.58, 0.38),
        "foot_l": (1.45 * scale, 0.58, 0.38),
        "foot_r": (1.45 * scale, 0.58, 0.38),
        "foot_swing": (0.95 * scale, 0.66, 0.28),
        "hip": (1.05 * scale, 0.66, 0.28),
        "brow_l_y": (2.60 * scale, 0.72, 0.40),
        "brow_r_y": (2.60 * scale, 0.72, 0.40),
    })
