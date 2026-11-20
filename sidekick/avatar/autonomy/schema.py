"""`motion_profile.json` 的单一契约：版本、允许键、默认 tuning、动作目录。"""
from __future__ import annotations

from sidekick.avatar.act_tags import GAZE_POINTS
from sidekick.motion.actions import DEFAULT_ACTIONS
from sidekick.motion.tuning import TUNING_RANGES, MotionTuning

SCHEMA_VERSION = 1
MAX_BYTES = 256 * 1024
ARCHETYPES = ("chibi", "standard", "realistic", "creature", "unknown")

_BASE_TUNING = MotionTuning().to_dict()
DEFAULT_TUNING: dict[str, dict] = {
    "chibi": {
        **_BASE_TUNING,
        "head_gain": 0.85, "head_roll_gain": 0.95, "body_gain": 0.90,
        "spring_scale": 1.20, "mouth_open_gain": 1.20, "action_scale": 1.05,
        "arm_gain": 1.0, "leg_gain": 0.9,
    },
    "standard": dict(_BASE_TUNING),
    "realistic": {
        **_BASE_TUNING,
        "head_gain": 1.0, "head_roll_gain": 1.0, "body_gain": 1.05,
        "spring_scale": 0.95, "mouth_open_gain": 0.98, "gaze_limit": 0.95,
    },
    "creature": dict(_BASE_TUNING),
    "unknown": dict(_BASE_TUNING),
}

ACTION_CATALOG: dict[str, dict] = {
    name: {
        "aliases": list(spec.aliases),
        "duration": round(spec.duration, 3),
        "channels": sorted(spec.pose),
        "degrade": {k: dict(v) for k, v in spec.degrade.items()},
        "priority": spec.priority,
        "cooldown": spec.cooldown,
    }
    for name, spec in DEFAULT_ACTIONS.items()
}

GAZE_TAGS = tuple(GAZE_POINTS)

FORBIDDEN_KEYS = ("model_hash", "layers", "version", "profile_version")

PROFILE_SCHEMA = {
    "type": "object",
    "required": ["schema_version", "generated_by", "archetype", "tuning",
                 "actions", "motion_policy", "prompt"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "generated_by": {"type": "object"},
        "archetype": {"enum": list(ARCHETYPES)},
        "tuning": {"type": "object",
                   "keys": sorted(TUNING_RANGES)},
        "channel_bindings": {"type": "object"},
        "aliases": {"type": "object"},
        "actions": {"type": "object",
                    "keys": sorted(ACTION_CATALOG),
                    "additionalProperties": True},
        "states": {"type": "object"},
        "motion_policy": {
            "type": "object",
            "properties": {
                "mode": {"const": "auto"},
                "model_motions": {"type": "object"},
                "ownership": {"type": "object"},
            },
        },
        "expressions": {"type": "object"},
        "prompt": {"type": "object"},
        "warnings": {"type": "array"},
    },
}


def tuning_defaults(archetype: str) -> dict:
    return dict(DEFAULT_TUNING.get(str(archetype or "unknown"), _BASE_TUNING))


def catalog_for_prompt() -> dict:
    """给 LLM 的紧凑动作目录（不含参数实现细节之外的数值）。"""
    return {
        "actions": ACTION_CATALOG,
        "tuning_ranges": {
            key: {"default": low_high[0], "min": low_high[1], "max": low_high[2]}
            for key, low_high in TUNING_RANGES.items()
        },
        "archetypes": list(ARCHETYPES),
        "gaze": list(GAZE_TAGS),
    }
