"""The frame-by-frame driver.

Threading contract
-----------------
* :meth:`MotionEngine.tick` and :meth:`parameters` run only on the render
  thread.  They are the only code allowed to touch the engine's springs.
* :meth:`submit` is the only method another thread may call.  It appends to a
  small locked deque; the render thread drains it at the start of a frame.
* The audio thread never calls the engine.  It pushes :class:`SpeechFrame`
  objects into the host's ring buffer; the render thread passes the newest one
  to :meth:`tick`.

This is intentionally not an async API.  A 60 FPS loop must never await; the
async side only produces intents and compact feature frames.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .actions import ActionLibrary, ActionPlayer, spec_from_dict
from .blink import BlinkConfig, BlinkScheduler, BlinkState
from .breath import Breath
from .compositor import (
    ProsodyState, ProsodyTracker, capability_summary, compose, to_parameters,
)
from .emotion import EmotionPreset, EmotionState
from .expressions import ExpressionMixer
from .gaze import GazeConfig, GazeController
from .introspect import compensate_pose
from .noise import NoiseBank, default_idle_noise
from .posture import PostureAnchor
from .rng import Rng
from .spring import SpringBank, default_springs
from .states import StateController
from .tuning import MotionTuning, apply_tuning
from .types import (
    Context, Intent, ModelProfile, Pose, SpeechFrame, clean_pose,
)


@dataclass
class EngineConfig:
    fps: float = 60.0
    max_intents_per_frame: int = 8
    idle_action_min_gap: float = 4.5
    idle_action_max_gap: float = 9.5
    idle_action_chance: float = 0.75
    auto_gesture_cooldown: float = 3.6
    auto_gesture_min_gap: float = 1.8
    auto_gesture_beat: float = 0.48


@dataclass
class EngineFrame:
    pose: Pose
    params: dict[str, float]
    blink: BlinkState
    breath: float
    action: str
    emotion: str
    speech: SpeechFrame | None
    report: dict = field(default_factory=dict)


_AUTO_GESTURES: dict[str, tuple[str, ...]] = {
    "happy": ("bounce", "laugh", "gesture", "tilt"),
    "excited": ("bounce", "laugh", "gesture", "clap"),
    "angry": ("gesture", "point", "shake", "lean"),
    "sad": ("look_away", "shrug", "tilt", "sob"),
    "shy": ("look_away", "shrug", "tilt"),
    "think": ("think", "tilt", "shrug"),
    "confused": ("think", "tilt", "shrug"),
    "awkward": ("look_away", "shrug", "tilt"),
    "neutral": ("gesture", "nod", "tilt", "lean"),
}


def _idle_action_names(profile: ModelProfile) -> list[str]:
    """随机待机候选：只放模型真实有通道的动作。"""
    names = ["tilt", "look_away", "lean", "think", "nod", "shrug"]
    if profile.supports("tail"):
        names.append("wag_tail")
    if any(profile.supports(ch) for ch in (
            "leg_l", "leg_r", "leg_l_x", "leg_r_x", "knee_l", "knee_r",
            "ankle_l", "ankle_r", "leg_l_y", "leg_r_y", "knee_l_y",
            "knee_r_y", "foot_l", "foot_r", "foot_l_y", "foot_r_y",
            "foot_swing")):
        names.append("leg_shift")
    if profile.supports("hip"):
        names.append("sway_hip")
    return names


class MotionEngine:
    def __init__(
        self,
        profile: ModelProfile,
        *,
        seed: int = 1,
        config: EngineConfig | None = None,
        emotions: dict[str, EmotionPreset] | None = None,
        actions: ActionLibrary | None = None,
        expression_mixer: ExpressionMixer | None = None,
    ) -> None:
        self.profile = profile
        self.expression_mixer = expression_mixer
        self.config = config or EngineConfig()
        self.seed = int(seed)
        self.rng = Rng(seed + 9001)
        self.tuning, tuning_warnings = MotionTuning.from_dict(profile.tuning)
        for warning in tuning_warnings:
            if warning not in profile.warnings:
                profile.warnings.append(warning)
        self.emotion = EmotionState(presets=dict(emotions)) if emotions else EmotionState()
        self.action_library = actions or ActionLibrary()
        custom = {
            name: spec for name, data in (profile.custom_actions or {}).items()
            if (spec := spec_from_dict(name, data)) is not None
        }
        if custom:
            self.action_library.add(custom)
        self.actions = ActionPlayer(self.action_library, seed=seed)
        self.states = StateController(profile.states)
        self._prev_pose: dict[str, float] = {}
        self.gaze = GazeController(seed=seed, config=self._gaze_config())
        self.blink = BlinkScheduler(
            seed=seed,
            config=BlinkConfig(amplitude_scale=self.tuning.blink_scale),
        )
        self.breath = Breath(seed=seed)
        self.idle_noise: NoiseBank = default_idle_noise(seed)
        self.posture = PostureAnchor(seed=seed)
        self.pose_springs: SpringBank = default_springs(self.tuning.spring_scale)
        self.prosody = ProsodyTracker()

        self._intents: deque[Intent] = deque(maxlen=128)
        self._lock = threading.Lock()
        self._now = 0.0
        self._started = False
        self._last_report: dict = {}
        self._last_pose = Pose()
        self._next_idle_action = self.rng.uniform(
            self.config.idle_action_min_gap, self.config.idle_action_max_gap)
        self._idle_actions = _idle_action_names(profile)
        self._last_idle_action = ""
        self._last_stretch = -1.0e9
        self._speaking_for = 0.0
        self._auto_gesture_at = 0.0
        self._breath_lag = 0.0

    def _gaze_config(self) -> GazeConfig:
        return GazeConfig(
            body_gain=0.22,
            eye_limit=self.tuning.gaze_limit,
        )

    # ── producer side ────────────────────────────────
    def submit(self, intent: Intent) -> None:
        """Thread-safe.  Keeps at most 128 pending intents."""
        with self._lock:
            self._intents.append(intent)

    def submit_emotion(self, name: str, intensity: float = 1.0,
                       duration: float | None = None) -> None:
        self.submit(Intent("emotion", name, intensity, duration, "host"))

    def submit_action(self, name: str, intensity: float = 1.0) -> None:
        self.submit(Intent("action", name, intensity, None, "host"))

    # ── render side ──────────────────────────────────
    def _drain_intents(self, now: float) -> None:
        with self._lock:
            items = [self._intents.popleft() for _ in range(
                min(len(self._intents), self.config.max_intents_per_frame))]
        for intent in items:
            self._apply_intent(intent, now)

    def _apply_intent(self, intent: Intent, now: float) -> None:
        kind = str(intent.kind or "").lower()
        name = str(intent.name or "").strip()
        if not name:
            return
        if kind == "emotion":
            self.emotion.set(name, intent.intensity, intent.duration, now=now)
        elif kind == "action":
            if self.actions.play(name, self.profile, now=now,
                                 intensity=intent.intensity):
                self._apply_state_requests()
        elif kind == "gaze":
            points = {
                "left": (-0.55, 0.0), "right": (0.55, 0.0),
                "up": (0.0, 0.55), "down": (0.0, -0.45),
                "center": (0.0, 0.05), "camera": (0.0, 0.08),
                "game": (0.55, 0.10), "chat": (0.72, -0.25),
                "think": (-0.35, 0.55),
            }
            x, y = points.get(name.lower(), (0.0, 0.0))
            self.gaze.look_at(x, y, dwell=float(intent.duration or 2.0), kind=name)

    def _apply_state_requests(self) -> None:
        requests, persistent = self.actions.take_state_requests()
        for name, active in requests:
            self.states.request(name, active)
        for pid, value in persistent.items():
            self.states.set_raw(pid, value)

    def _maybe_auto_gesture(self, dt: float, context: Context,
                            prosody: ProsodyState) -> None:
        """A high-energy beat inside a long answer gets a small gesture."""
        if not context.speaking or self.actions.playing:
            self._speaking_for = 0.0 if not context.speaking else self._speaking_for
            return
        self._speaking_for += max(0.0, float(dt))
        if self._now < self._auto_gesture_at:
            return
        if self._now - self.actions.last_end < self.config.auto_gesture_min_gap:
            return
        if prosody.a_beat < self.config.auto_gesture_beat and self._speaking_for < 2.2:
            return
        choices = _AUTO_GESTURES.get(self.emotion.dominant, _AUTO_GESTURES["neutral"])
        name = self.rng.choice(choices)
        if self.actions.play(name, self.profile, now=self._now,
                             intensity=self.rng.uniform(0.55, 0.75)):
            self._auto_gesture_at = self._now + self.config.auto_gesture_cooldown
            self._speaking_for = 0.0

    def _maybe_idle_action(self, dt: float, context: Context) -> None:
        if context.speaking or self.actions.playing:
            self._next_idle_action = min(
                self._next_idle_action, self.config.idle_action_min_gap)
            return
        self._next_idle_action -= dt
        if self._next_idle_action > 0.0:
            return
        self._next_idle_action = self.rng.uniform(
            self.config.idle_action_min_gap, self.config.idle_action_max_gap)
        if not self.rng.chance(self.config.idle_action_chance):
            return
        choices = [name for name in self._idle_actions if name != self._last_idle_action]
        # 伸懒腰幅度大、占用全身，只在真正冷场且距上次足够久时出现。
        if (context.silence >= 35.0
                and self._now - self._last_stretch >= 90.0
                and "stretch" not in choices):
            choices.append("stretch")
        if not choices:
            return
        name = self.rng.choice(choices)
        if self.actions.play(name, self.profile, now=self._now,
                             intensity=self.rng.uniform(0.55, 0.88)):
            self._last_idle_action = name
            if name == "stretch":
                self._last_stretch = self._now

    def tick(
        self,
        now: float,
        dt: float,
        *,
        speech: SpeechFrame | None = None,
        context: Context | None = None,
    ) -> EngineFrame:
        now = float(now)
        dt = max(0.0, min(0.1, float(dt)))
        if not self._started:
            self._now = now
            self._started = True
            dt = 0.0
        else:
            # Monotonic host clocks should never go backwards; if they do,
            # trust the smaller delta instead of integrating a negative step.
            dt = min(dt, max(0.0, now - self._now))
            self._now = max(self._now, now)
        self._now = max(self._now, now)

        ctx = context or Context()
        ctx.now = self._now
        ctx.speaking = bool(ctx.speaking)

        self._drain_intents(self._now)
        self.emotion.update(dt, now=self._now)
        prosody_state = self.prosody.update(speech, dt, speaking=ctx.speaking)
        self._maybe_auto_gesture(dt, ctx, prosody_state)
        self._maybe_idle_action(dt, ctx)
        self._apply_state_requests()
        self.states.update(dt)

        action_target = self.actions.update(dt, now=self._now)
        if self.tuning.action_scale != 1.0:
            action_target = action_target.scaled(self.tuning.action_scale)

        posture = self.posture.update(dt, ctx)
        gaze = self.gaze.update(dt, ctx)
        blink_state = self.blink.update(
            dt, squint=self.emotion.eye_squint, speaking=ctx.speaking)
        breath_sample = self.breath.update(self._now, dt, speaking=ctx.speaking)
        breath_value = max(0.0, min(1.0, breath_sample.value * self.tuning.breath_scale))
        # 胸比躯干晚约 0.2 s；一个一阶低通即可，且不会在停顿时过冲。
        alpha_breath = 1.0 - math.exp(-max(0.0, dt) / 0.20)
        self._breath_lag += (breath_value - self._breath_lag) * alpha_breath
        idle = Pose(self.idle_noise.sample(self._now))
        if speech is not None:
            ctx.silence = max(ctx.silence, prosody_state.silence)

        pose = compose(
            self.profile, self.emotion, posture, action_target, gaze, idle,
            prosody_state.pose(self.emotion.prosody_scale),
            breath=breath_value,
            blink_l=blink_state.factor_l,
            blink_r=blink_state.factor_r,
            speech=speech,
            speaking=ctx.speaking,
            now=self._now,
            action_name=self.actions.current_name,
            action_strength=self.actions.strength,
            breath_lag=self._breath_lag,
            speech_energy=prosody_state.env_phrase,
        )
        pose = apply_tuning(pose, self.tuning)
        pose = Pose(compensate_pose(pose.values, self.profile))
        # One spring bank owns every continuous posture channel.  It is what
        # makes the head lead the torso, the wrist lag the shoulder, and the
        # release of an action carry past neutral.
        smoothed = self.pose_springs.step(pose.values, dt)
        pose = Pose(clean_pose({**pose.values, **smoothed}))
        # follower 的滞后项按“每帧增量”计算；按每秒速度会被 clamp 到 ±0.40
        # 后整段饱和，速度过零时链式通道会跳变。
        velocity = {
            channel: value - self._prev_pose.get(channel, value)
            for channel, value in pose.values.items()
        }
        self._prev_pose = dict(pose.values)
        params = to_parameters(self.profile, pose, velocity=velocity)
        if self.expression_mixer is not None:
            self.expression_mixer.update(dt)
            params = self.expression_mixer.apply(
                params, self.profile, speaking=ctx.speaking)
        params.update(self.states.parameters())
        for pid, value in self.actions.raw_overrides().items():
            bounds = self.profile.ranges.get(pid)
            if bounds is not None:
                lo, hi, _default = bounds
                params[pid] = max(float(lo), min(float(hi), float(value)))
        report = {
            "tier": self.profile.tier,
            "caps": capability_summary(self.profile),
            "action": self.actions.current_name,
            "emotion": self.emotion.dominant,
            "posture": self.posture.state,
            "blink": blink_state.phase,
            "breath": round(breath_value, 3),
            "speech": bool(speech and speech.openness > 0.01),
            "prosody": round(prosody_state.env_phrase, 3),
            "beat": round(prosody_state.a_beat, 3),
            "gaze": self.gaze.state.target_kind,
            "params": len(params),
            "source": self.profile.source,
            "fallback": self.profile.fallback,
            "degraded": self.actions.last_degraded,
            "states": self.states.report(),
            "warnings": len(self.profile.warnings),
        }
        self._last_report = report
        self._last_pose = pose.copy()
        return EngineFrame(
            pose=pose,
            params=params,
            blink=blink_state,
            breath=breath_sample.value,
            action=self.actions.current_name,
            emotion=self.emotion.dominant,
            speech=speech,
            report=report,
        )

    @property
    def now(self) -> float:
        return self._now

    @property
    def report(self) -> dict:
        return dict(self._last_report)

    @property
    def last_pose(self) -> Pose:
        """只读诊断快照：最近一帧的语义姿态。"""
        return self._last_pose.copy()

    def debug_state(self) -> dict:
        return {
            **self._last_report,
            "emotion": self.emotion.report(),
            "action": self.actions.stats(),
            "posture": {"state": self.posture.state},
            "gaze": {
                "kind": self.gaze.state.target_kind,
                "saccades": self.gaze.state.saccades,
                "fixations": self.gaze.state.fixations,
                "target": (round(self.gaze.state.target_x, 3),
                           round(self.gaze.state.target_y, 3)),
            },
            "blink": {
                "phase": self.blink.state.phase,
                "l": round(self.blink.state.factor_l, 3),
                "r": round(self.blink.state.factor_r, 3),
            },
        }


def monotonic() -> float:
    return time.monotonic()
