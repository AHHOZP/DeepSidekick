"""Virtual gaze: interest points, Poisson saccades, and head/eye sharing.

There is no camera.  The character looks at a *virtual* point chosen from the
things a streamer would care about: the viewer, the game, the chat, a thought
in the upper-left, or a random idle direction.  The point is held for a dwell
time, then a new one is chosen.

The controller emits *targets*, not smoothed positions.  The engine's
``SpringBank`` is the single place where inertia is applied, so the eye spring
(6.5 Hz) still arrives before the head spring (1.48 Hz): that velocity
difference is what sells a glance as intentional rather than as a slider being
dragged.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .noise import fbm
from .rng import Rng
from .types import Context, Pose, clamp


def pointer_target(px: float, py: float, width: float, height: float,
                   origin: tuple[float, float] | None = None
                   ) -> tuple[float, float]:
    """把窗口像素坐标换成以双眼中心为原点的归一化注视目标。"""
    w = max(1.0, float(width))
    h = max(1.0, float(height))
    nx = clamp(float(px) / w * 2.0 - 1.0, -1.0, 1.0)
    ny = clamp(1.0 - float(py) / h * 2.0, -1.0, 1.0)
    if origin is not None:
        try:
            cx, cy = float(origin[0]), float(origin[1])
        except (TypeError, ValueError, IndexError):
            cx, cy = 0.0, 0.5
        nx = clamp(nx - cx, -1.0, 1.0)
        # cy 是 0 脚底到 1 头顶；窗口 ny 是 -1 底到 +1 顶。
        ny = clamp(ny - (2.0 * cy - 1.0), -1.0, 1.0)
    return nx, ny


@dataclass
class GazeConfig:
    head_gain: float = 0.58
    #: 视线分给躯干的比例。全局 body_gain 在 apply_tuning 里单独乘一次。
    body_gain: float = 0.22
    head_limit: float = 0.82
    eye_limit: float = 0.96
    saccade_threshold: float = 0.20
    saccade_min: float = 0.038
    saccade_max: float = 0.082
    saccade_overshoot: float = 0.055
    microsaccade_min_gap: float = 0.28
    microsaccade_max_gap: float = 1.10
    microsaccade_amplitude: float = 0.028


@dataclass
class GazeTarget:
    x: float
    y: float
    kind: str = "wander"
    dwell: float = 2.5


@dataclass
class GazeState:
    target_x: float = 0.0
    target_y: float = 0.0
    target_kind: str = "camera"
    saccading: bool = False
    saccade_progress: float = 0.0
    fixations: int = 0
    saccades: int = 0


def _ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


class GazeController:
    """Chooses a virtual interest point and turns it into a pose."""

    def __init__(self, seed: int = 1, config: GazeConfig | None = None) -> None:
        self.config = config or GazeConfig()
        self.rng = Rng(seed + 31)

        self._target = GazeTarget(0.0, 0.08, "camera", 3.0)
        self._target_left = 2.0
        self._prev_target = (0.0, 0.08)
        self._saccade_from = (0.0, 0.0)
        self._saccade_to = (0.0, 0.0)
        self._saccade_t = 0.0
        self._saccade_duration = 0.06
        self._saccading = False
        self._micro_in = 0.6
        self._micro = (0.0, 0.0)
        self._state = GazeState()
        self._was_speaking = False
        self._camera_hold = 0.0

    # ── target selection ─────────────────────────────
    def _weighted_kind(self, context: Context) -> str:
        silence = max(0.0, context.silence)
        speaking = 1.0 if context.speaking else 0.0
        user = 1.0 if context.user_speaking else 0.0
        weights = {
            "camera": 0.34 + 0.26 * speaking + 0.30 * user,
            "game": 0.14 + 0.55 * clamp(context.game_motion, 0.0, 1.0),
            "chat": 0.10 + 0.40 * clamp(context.chat_activity, 0.0, 1.0),
            "think": 0.08 + 0.40 * clamp(silence / 4.0, 0.0, 1.0),
            "wander": 0.22,
        }
        total = sum(weights.values())
        pick = self.rng.uniform(0.0, total)
        for kind, weight in weights.items():
            pick -= weight
            if pick <= 0.0:
                return kind
        return "wander"

    def _new_target(self, context: Context) -> GazeTarget:
        kind = self._weighted_kind(context)
        if kind == "camera":
            return GazeTarget(0.0, 0.08, kind, self.rng.uniform(2.0, 4.6))
        if kind == "game":
            return GazeTarget(
                self.rng.uniform(0.35, 0.78), self.rng.uniform(-0.10, 0.28),
                kind, self.rng.uniform(1.2, 2.8))
        if kind == "chat":
            return GazeTarget(
                self.rng.uniform(0.55, 0.90), self.rng.uniform(-0.42, -0.10),
                kind, self.rng.uniform(1.2, 2.6))
        if kind == "think":
            side = -1.0 if self.rng.chance(0.58) else 1.0
            return GazeTarget(
                side * self.rng.uniform(0.22, 0.55), self.rng.uniform(0.38, 0.72),
                kind, self.rng.uniform(2.0, 4.4))
        return GazeTarget(
            self.rng.uniform(-0.75, 0.75), self.rng.uniform(-0.18, 0.62),
            kind, self.rng.uniform(1.2, 3.2))

    def _begin_saccade(self, x: float, y: float) -> None:
        self._saccade_from = (self._state.target_x, self._state.target_y)
        self._saccade_to = (clamp(x, -1.0, 1.0), clamp(y, -1.0, 1.0))
        distance = math.hypot(
            self._saccade_to[0] - self._saccade_from[0],
            self._saccade_to[1] - self._saccade_from[1])
        self._saccade_duration = clamp(
            self.config.saccade_min + distance * 0.09,
            self.config.saccade_min, self.config.saccade_max)
        self._saccade_t = 0.0
        self._saccading = True
        self._state.saccades += 1

    # ── update ───────────────────────────────────────
    def update(self, dt: float, context: Context) -> Pose:
        dt = max(0.0, min(0.1, float(dt)))
        pointer = context.pointer
        if pointer is not None:
            self._camera_hold = 0.0
            # 鼠标是连续目标：不触发 saccade，也不做微扫视，只让弹簧跟随。
            px = clamp(float(pointer[0]), -1.0, 1.0)
            py = clamp(float(pointer[1]), -1.0, 1.0)
            self._target = GazeTarget(px, py, "pointer", 0.15)
            self._target_left = 0.15
            self._state.target_kind = "pointer"
            self._state.target_x = px
            self._state.target_y = py
            self._saccading = False
            self._state.saccading = False
            self._micro = (0.0, 0.0)
        else:
            # 说完一句时快速回看一眼镜头，制造“确认观众听懂了吗”的眼神交流。
            if self._was_speaking and not context.speaking:
                self._camera_hold = self.rng.uniform(0.40, 0.80)
                self._target = GazeTarget(0.0, 0.08, "camera", 0.4)
                self._target_left = self._camera_hold
                self._state.target_kind = "camera"
                if math.hypot(self._state.target_x,
                              self._state.target_y - 0.08) > 0.12:
                    self._begin_saccade(0.0, 0.08)
            if self._camera_hold > 0.0:
                self._camera_hold = max(0.0, self._camera_hold - dt)
                self._target = GazeTarget(0.0, 0.08, "camera", 0.2)
                # hold 期间不再让 target_left 同步衰减，否则 hold 结束后还要
                # 再等一遍 0.4–0.8 秒才换目标。
                self._target_left = 0.0
                self._state.target_kind = "camera"
                self._state.target_x = 0.0
                self._state.target_y = 0.08
            else:
                self._target_left -= dt

        # A target change is a decision; a saccade is the ballistic part.
        if (pointer is None and self._target_left <= 0.0
                and self._camera_hold <= 0.0):
            new_target = self._new_target(context)
            distance = math.hypot(
                new_target.x - self._target.x, new_target.y - self._target.y)
            self._target = new_target
            self._target_left = new_target.dwell
            self._state.target_kind = new_target.kind
            if distance >= self.config.saccade_threshold:
                self._begin_saccade(new_target.x, new_target.y)
            else:
                self._state.target_x = new_target.x
                self._state.target_y = new_target.y
            if distance < self.config.saccade_threshold:
                self._state.fixations += 1
        elif pointer is None:
            # Even while holding a target, tiny corrective saccades happen.
            self._micro_in -= dt
            if self._micro_in <= 0.0:
                self._micro_in = self.rng.uniform(
                    self.config.microsaccade_min_gap,
                    self.config.microsaccade_max_gap)
                self._micro = (
                    self.rng.gauss(0.0, self.config.microsaccade_amplitude),
                    self.rng.gauss(0.0, self.config.microsaccade_amplitude),
                )
            # Microsaccades decay over roughly 120 ms.
            decay = math.exp(-dt / 0.12)
            self._micro = (self._micro[0] * decay, self._micro[1] * decay)
            self._state.target_x = self._target.x
            self._state.target_y = self._target.y

        if self._saccading:
            self._saccade_t += dt
            progress = 1.0 if self._saccade_duration <= 0 else min(
                1.0, self._saccade_t / self._saccade_duration)
            eased = _ease_out_cubic(progress)
            # Overshoot peaks mid-flight and returns to the target exactly.
            overshoot = self.config.saccade_overshoot * math.sin(math.pi * progress)
            dx = self._saccade_to[0] - self._saccade_from[0]
            dy = self._saccade_to[1] - self._saccade_from[1]
            self._state.target_x = self._saccade_from[0] + dx * eased + dx * overshoot
            self._state.target_y = self._saccade_from[1] + dy * eased + dy * overshoot
            self._state.saccade_progress = progress
            if progress >= 1.0:
                self._saccading = False
                self._state.saccading = False
                self._state.target_x = self._saccade_to[0]
                self._state.target_y = self._saccade_to[1]
                self._state.fixations += 1
        self._state.saccading = self._saccading

        # The point the whole body is trying to look at.  The microsaccade is
        # added to the eye target, not to the head target.
        gaze_x = clamp(self._state.target_x, -1.0, 1.0)
        gaze_y = clamp(self._state.target_y, -1.0, 1.0)
        # A slow drift around the point keeps a held gaze from being frozen.
        gaze_x += fbm(context.now, 0.11, 4101) * 0.012
        gaze_y += fbm(context.now, 0.09, 4202) * 0.010

        cfg = self.config
        head_x = clamp(gaze_x * cfg.head_gain, -cfg.head_limit, cfg.head_limit)
        head_y = clamp(gaze_y * cfg.head_gain, -cfg.head_limit, cfg.head_limit)
        head_z = clamp(-gaze_x * 0.075 + fbm(context.now, 0.07, 4303) * 0.03,
                       -0.32, 0.32)
        body_x = clamp(gaze_x * cfg.body_gain, -0.28, 0.28)
        body_y = clamp(gaze_y * cfg.body_gain, -0.22, 0.22)
        body_z = clamp(gaze_x * 0.08, -0.18, 0.18)

        # Eyes cover the remainder after head and body have turned.  The
        # engine's eye spring is much faster than the head spring, so the eyes
        # still lead the head on a glance.
        eye_x = clamp(
            gaze_x + self._micro[0] - head_x - body_x * 0.22,
            -cfg.eye_limit, cfg.eye_limit)
        eye_y = clamp(
            gaze_y + self._micro[1] - head_y - body_y * 0.22,
            -cfg.eye_limit, cfg.eye_limit)
        self._was_speaking = bool(context.speaking)

        return Pose({
            "head_x": head_x,
            "head_y": head_y,
            "head_z": head_z,
            "body_x": body_x,
            "body_y": body_y,
            "body_z": body_z,
            "eye_x": eye_x,
            "eye_y": eye_y,
        })

    @property
    def state(self) -> GazeState:
        return self._state

    def look_at(self, x: float, y: float, *, dwell: float = 2.0, kind: str = "manual") -> None:
        """Force an interest point (used by host events, e.g. a click)."""
        self._target = GazeTarget(clamp(x, -1.0, 1.0), clamp(y, -1.0, 1.0), kind, dwell)
        self._target_left = dwell
        self._state.target_kind = kind
        self._begin_saccade(self._target.x, self._target.y)
