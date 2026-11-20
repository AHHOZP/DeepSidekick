"""确定性 baseline profile：LLM 不可用、输出坏或漏项时的兜底。

它也是 V6 生成链路的骨架：绑定、状态、可用动作和模型事实先由程序写满，
LLM 只在这个骨架上做增量选择。因此这里不能只“能跑”，还必须保留模型真实
存在的腿、尾巴、前倾和换装能力。
"""
from __future__ import annotations

import time

from sidekick.avatar.act_tags import GAZE_POINTS, vocabulary
from sidekick.avatar.autonomy.schema import (
    ACTION_CATALOG, DEFAULT_TUNING, SCHEMA_VERSION, tuning_defaults,
)

_FUZZY = (
    ("happy", ("开心", "高兴", "快乐", "笑", "happy", "joy", "smile")),
    ("excited", ("兴奋", "激动", "excited")),
    ("sad", ("难过", "悲伤", "哭", "sad", "sorrow")),
    ("angry", ("生气", "怒", "angry", "mad")),
    ("shy", ("害羞", "脸红", "shy", "blush")),
    ("think", ("思考", "想", "think")),
    ("surprised", ("惊讶", "惊", "surprised", "shock")),
    ("confused", ("困惑", "疑惑", "confused")),
    ("awkward", ("尴尬", "无语", "awkward")),
)

_STATE_ACTIONS: dict[str, str] = {
    "long_skirt": "long_skirt", "short_skirt": "long_skirt",
    "chibi_mode": "chibi_mode", "normal_body": "chibi_mode",
}


def _fuzzy_expressions(dossier: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in dossier.get("expressions") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "expression") != "expression":
            continue
        name = str(item.get("name") or "")
        low = name.lower()
        for emotion, cues in _FUZZY:
            if emotion in out:
                continue
            if any(str(cue).lower() in low for cue in cues):
                out[emotion] = name
                break
    return out


def _channels(dossier: dict) -> set[str]:
    return {str(channel) for channel in (dossier.get("by_channel") or {})}


def _leg_channels(channels: set[str]) -> bool:
    return any(channel.startswith((
        "leg_", "knee_", "ankle_", "foot_")) for channel in channels)


def _enabled_actions(dossier: dict) -> dict[str, dict]:
    channels = _channels(dossier)
    states = set((dossier.get("states") or {}))
    out: dict[str, dict] = {}
    for name in sorted(ACTION_CATALOG):
        state = _STATE_ACTIONS.get(name)
        if state:
            enabled = state in states
        elif name == "kick":
            enabled = _leg_channels(channels)
        elif name == "leg_shift":
            enabled = _leg_channels(channels)
        elif name == "wag_tail":
            enabled = "tail" in channels
        elif name == "sway_hip":
            enabled = "hip" in channels
        elif name in ("lean", "bow"):
            enabled = "lean_y" in channels or "body_y" in channels
        else:
            enabled = True
        out[name] = {"enabled": bool(enabled), "intensity": 0.8}
    return out


def _persistent_actions(dossier: dict) -> dict[str, dict]:
    """把持久换装/配件表情变成可开、可关的原始参数动作。

    expression 本身会自动回弹；把控制参数写成 ``persistent_raw`` 才能让
    “穿长裙/脱鞋/戴眼镜”跨轮保持，并给 LLM 一个真正能关掉它的动作。
    """
    actions: dict[str, dict] = {}
    state_ids = {
        str(row.get("param_id"))
        for row in (dossier.get("states") or {}).values()
        if isinstance(row, dict) and row.get("param_id")
    }
    index = 0
    for item in dossier.get("expressions") or []:
        if not isinstance(item, dict) or item.get("kind") != "persistent":
            continue
        controls = [row for row in item.get("controls") or []
                    if isinstance(row, dict) and row.get("id")]
        if not controls:
            continue
        # 已经由 StateController 持久化的换装参数不要再造一套动作。
        if all(str(row["id"]) in state_ids for row in controls):
            continue
        name = str(item.get("name") or f"expression_{index}")
        on = {str(row["id"]): float(row.get("value", 1.0)) for row in controls}
        off = {str(row["id"]): float(row.get("default", 0.0))
               for row in controls}
        key = f"expr_{index}_on"
        actions[key] = {
            "enabled": True, "aliases": [name],
            "persistent_raw": on, "duration": 0.2, "cooldown": 0.2,
        }
        actions[f"expr_{index}_off"] = {
            "enabled": True,
            "aliases": [f"关闭{name}", f"关{name}", f"收起{name}"],
            "persistent_raw": off, "duration": 0.2, "cooldown": 0.2,
        }
        index += 1
    return actions


def _state_actions(dossier: dict) -> dict[str, dict]:
    """给没有内置动作的离散状态补上可调用的状态动作。

    ``long_skirt``/``chibi_mode`` 已有内置动作；换外套、尾巴装束、狗尾草这些
    只有状态、没有动作名，LLM 和用户就无法用 ``action=`` 触发。这里用
    ``states`` 配方补上，timed 状态由 StateController 自动释放。
    """
    actions: dict[str, dict] = {}
    handled = set(_STATE_ACTIONS.values()) | {"hand_chest", "hand_point"}
    for name, row in (dossier.get("states") or {}).items():
        name = str(name)
        if name in handled or not isinstance(row, dict):
            continue
        aliases = [str(x) for x in (row.get("aliases") or []) if str(x).strip()]
        if not aliases:
            continue
        actions[f"state_{name}_on"] = {
            "enabled": True, "aliases": aliases[:4],
            "states": {name: True}, "duration": 0.2, "cooldown": 0.2,
        }
        if str(row.get("mode") or "persistent") == "persistent":
            actions[f"state_{name}_off"] = {
                "enabled": True,
                "aliases": [f"关闭{alias}" for alias in aliases[:2]]
                + [f"收起{alias}" for alias in aliases[:1]],
                "states": {name: False}, "duration": 0.2, "cooldown": 0.2,
            }
    return actions


def _model_motions(dossier: dict) -> dict[str, dict]:
    groups = []
    for item in (dossier or {}).get("motions") or []:
        if isinstance(item, dict) and item.get("group"):
            group = str(item["group"])
            if group not in groups:
                groups.append(group)
    model_motions = {}
    for group in groups:
        low = group.lower()
        if low in ("idle", "wait", "stand", "待机") or "idle" in low:
            model_motions[group] = {"enabled": False}
        else:
            model_motions[group] = {"enabled": True, "cooldown": 8.0}
    return model_motions


def fallback_profile(dossier: dict, *,
                     reason: str = "LLM 未启用，使用默认配置") -> dict:
    proportions = (dossier or {}).get("proportions") or {}
    archetype = str(proportions.get("archetype_guess") or "unknown")
    vocab = vocabulary()
    prompt = {
        "emotion_tags": [x["name"] for x in vocab["emotions"]],
        "action_tags": [x["name"] for x in vocab["actions"]],
        "gaze_tags": list(GAZE_POINTS),
        "rules": ("只用词表里的情绪、动作和视线名；平时一句最多 1 个情绪加 1 个动作，"
                  "整条回复最多 6 个意图；真能帮语气加分再放，拿不准就不加；"
                  "标签放句末标点后面（<scene> 前面）。"),
    }
    actions = _enabled_actions(dossier or {})
    actions.update(_state_actions(dossier or {}))
    actions.update(_persistent_actions(dossier or {}))
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": {
            "provider": "fallback",
            "model": "",
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "fallback": True,
        },
        "archetype": archetype if archetype in DEFAULT_TUNING else "unknown",
        "archetype_confidence": 0.0,
        "tuning": tuning_defaults(archetype),
        "channel_bindings": dict((dossier or {}).get("by_channel") or {}),
        "aliases": {},
        "actions": actions,
        "states": dict((dossier or {}).get("states") or {}),
        "motion_policy": {
            "mode": "auto",
            "model_motions": _model_motions(dossier or {}),
            "ownership": {"crossfade_ms": 320, "mouth_priority": "lipsync",
                          "expression_priority": "model"},
        },
        "expressions": _fuzzy_expressions(dossier or {}),
        "prompt": prompt,
        "warnings": [reason],
    }


def merge_baseline(baseline: dict, draft: dict, dossier: dict) -> dict:
    """Keep the program's facts and let the LLM only add/tune on top.

    ``draft`` is already validated.  Capability channels and states are always
    taken from the baseline; an LLM that forgot or disabled ``kick`` cannot make
    a model with legs lose its legs.
    """
    out = dict(draft)
    extra_warnings: list[str] = []
    out["channel_bindings"] = dict(baseline.get("channel_bindings") or {})
    out["states"] = dict(baseline.get("states") or {})
    out["aliases"] = {**(baseline.get("aliases") or {}),
                      **(draft.get("aliases") or {})}

    actions = {
        name: dict(row)
        for name, row in (baseline.get("actions") or {}).items()
        if isinstance(row, dict)
    }
    for name, row in (draft.get("actions") or {}).items():
        if not isinstance(row, dict):
            continue
        current = actions.setdefault(str(name), {})
        current.update(row)
    critical = {"kick", "leg_shift", "wag_tail", "sway_hip", "lean", "bow",
                "long_skirt", "short_skirt", "chibi_mode", "normal_body"}
    baseline_actions = baseline.get("actions") or {}
    for name in critical:
        if name in actions:
            actions[name]["enabled"] = bool(
                (baseline_actions.get(name) or {}).get("enabled", True))
    out["actions"] = actions

    policy = dict(baseline.get("motion_policy") or {})
    draft_policy = draft.get("motion_policy") or {}
    policy.update({key: value for key, value in draft_policy.items()
                   if key != "model_motions"})
    model_motions = dict(policy.get("model_motions") or {})
    forced = []
    for group, row in (draft_policy.get("model_motions") or {}).items():
        if not isinstance(row, dict):
            continue
        base_row = model_motions.get(str(group))
        if isinstance(base_row, dict) and base_row.get("enabled") is False:
            # 待机 motion 由程序化引擎接管；LLM 不能把它重新打开。
            forced.append(str(group))
            continue
        model_motions[str(group)] = {**model_motions.get(str(group), {}),
                                     **row}
    if forced:
        extra_warnings.append(
            "待机 motion 由自主驱动接管，已保持禁用：" + "、".join(forced))
    policy["model_motions"] = model_motions
    out["motion_policy"] = policy

    expressions = dict(baseline.get("expressions") or {})
    expressions.update(draft.get("expressions") or {})
    out["expressions"] = expressions

    prompt = dict(baseline.get("prompt") or {})
    draft_prompt = dict(draft.get("prompt") or {})
    for key in ("emotion_tags", "action_tags", "gaze_tags"):
        values = []
        for item in (*list(prompt.get(key) or []),
                     *list(draft_prompt.get(key) or [])):
            text = str(item)
            if text and text not in values:
                values.append(text)
        if values:
            draft_prompt[key] = values
    prompt.update(draft_prompt)
    out["prompt"] = prompt
    out["warnings"] = list(dict.fromkeys(
        list(baseline.get("warnings") or []) + list(draft.get("warnings") or [])
        + extra_warnings))
    return out
