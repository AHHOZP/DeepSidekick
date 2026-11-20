"""程序化 Live2D 驱动：归一化姿态、分层合成、参数自省和宿主适配。

典型用法::

    from sidekick.motion import MotionEngine, Context, SpeechFrame
    from sidekick.motion.adapters.live2d_py import Live2DParamSource, Live2DWriter

    profile = Live2DParamSource(model).profile()
    engine = MotionEngine(profile, seed=7)
    writer = Live2DWriter(model, profile)
    writer.prepare()

    # 60 FPS 循环：Update → tick → write → Draw
"""
from .actions import ActionLibrary, ActionPlayer, ActionSpec, DEFAULT_ACTIONS
from .blink import BlinkConfig, BlinkScheduler
from .breath import Breath, BreathConfig, breath_pose, breath_value
from .compositor import ProsodyState, ProsodyTracker, compose, to_parameters
from .emotion import EmotionPreset, EmotionState, infer_emotion, resolve_emotion
from .engine import EngineConfig, EngineFrame, MotionEngine
from .expressions import ExpressionMixer
from .gaze import GazeConfig, GazeController
from .introspect import (
    ParamSource, compensate_pose, introspect, match_channel,
    physics_additive_specs, read_display_channels, read_display_states,
)
from .lipsync import (
    RHUBARB_SHAPES, SpeechAnalyzer, VOWEL_TARGETS, analyze_pcm,
    classify_vowel, find_formants, nearest_shape,
)
from .noise import NoiseBank, default_idle_noise, fbm, value_noise
from .null import NULL_MOTION_HOST, NullMotionHost
from .ownership import MotionOwnership
from .posture import POSTURE_STATES, PostureAnchor
from .profile import apply_overrides, profile_from_dossier
from .rng import Rng
from .spring import SecondOrder, SpringBank, default_springs
from .speech import SpeechRing
from .states import StateController
from .tuning import TUNING_RANGES, MotionTuning, apply_tuning
from .types import (
    CHANNEL_RANGES, Context, Intent, ModelProfile, ParamSpec, Pose, SpeechFrame,
    StateSpec,
)

__all__ = [
    "ActionLibrary", "ActionPlayer", "ActionSpec", "DEFAULT_ACTIONS",
    "BlinkConfig", "BlinkScheduler", "Breath", "BreathConfig", "breath_pose",
    "breath_value",
    "ProsodyState", "ProsodyTracker", "compose", "to_parameters",
    "EmotionPreset", "EmotionState", "infer_emotion", "resolve_emotion",
    "EngineConfig", "EngineFrame", "MotionEngine", "ExpressionMixer",
    "GazeConfig", "GazeController",
    "ParamSource", "compensate_pose", "introspect", "match_channel",
    "physics_additive_specs", "read_display_channels", "read_display_states",
    "RHUBARB_SHAPES", "SpeechAnalyzer", "VOWEL_TARGETS", "analyze_pcm",
    "classify_vowel", "find_formants", "nearest_shape",
    "NoiseBank", "default_idle_noise", "fbm", "value_noise",
    "NULL_MOTION_HOST", "NullMotionHost",
    "MotionOwnership", "POSTURE_STATES", "PostureAnchor",
    "apply_overrides", "profile_from_dossier",
    "Rng", "SecondOrder", "SpringBank", "default_springs",
    "SpeechRing", "StateController",
    "TUNING_RANGES", "MotionTuning", "apply_tuning",
    "CHANNEL_RANGES", "Context", "Intent", "ModelProfile", "ParamSpec", "Pose",
    "SpeechFrame", "StateSpec",
]
