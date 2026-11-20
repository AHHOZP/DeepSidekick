"""把 `motion_profile.json` 的覆盖项应用到自省结果。

运行时事实仍以自省为准：profile 只能改已存在参数的绑定、增加别名、调 tuning/
动作开关/资产策略；未知 ID 只记 warning，不凭空造参数。
"""
from __future__ import annotations

from typing import Any

from .actions import DEFAULT_ACTIONS, spec_from_dict
from .introspect import kind_for_channel
from .types import CHANNEL_RANGES, ModelProfile, ParamSpec, StateSpec


def _spec_for(profile: ModelProfile, pid: str, channel: str,
              warnings: list[str]) -> ParamSpec | None:
    if channel not in CHANNEL_RANGES:
        warnings.append(f"profile: 未知语义通道 {channel}")
        return None
    ranges = profile.ranges.get(str(pid))
    if ranges is None:
        warnings.append(f"profile: 参数 {pid} 不在模型里")
        return None
    minimum, maximum, default = ranges
    old = profile.params.get(str(pid))
    return ParamSpec(
        id=str(pid), channel=channel, minimum=minimum, maximum=maximum,
        default=default,
        kind=kind_for_channel(channel, minimum, maximum, default),
        score=200, match=f"profile:{pid}",
        gamma=old.gamma if old is not None else 1.0,
        polarity=old.polarity if old is not None else 1.0,
        effective_min=old.effective_min if old is not None else None,
        effective_max=old.effective_max if old is not None else None,
        confidence=1.0, evidence=("manual",),
    )


def _bind(profile: ModelProfile, binding: dict, warnings: list[str]) -> bool:
    changed = False
    used: set[str] = set()
    for channel, pid in (binding or {}).items():
        channel = str(channel or "")
        pid = str(pid or "")
        if not channel or not pid or pid in used:
            continue
        used.add(pid)
        spec = _spec_for(profile, pid, channel, warnings)
        if spec is None:
            continue
        old = profile.by_channel.get(channel)
        profile.by_channel[channel] = spec
        profile.params[pid] = spec
        if old is not None and old.id != spec.id:
            profile.followers.pop(channel, None)
        changed = True
    return changed


def _aliases(profile: ModelProfile, aliases: dict, warnings: list[str]) -> None:
    for pid, channel in (aliases or {}).items():
        pid = str(pid or "")
        channel = str(channel or "")
        if not pid or not channel:
            continue
        if pid in profile.params and profile.params[pid].channel != channel:
            warnings.append(f"profile: 参数 {pid} 已绑定 {profile.params[pid].channel}，忽略别名")
            continue
        if channel in profile.by_channel:
            warnings.append(f"profile: 通道 {channel} 已有绑定，忽略别名 {pid}")
            continue
        spec = _spec_for(profile, pid, channel, warnings)
        if spec is None:
            continue
        profile.by_channel[channel] = spec
        profile.params[pid] = spec


def _custom_payload(name: str, settings: dict) -> dict | None:
    base = DEFAULT_ACTIONS.get(name)
    payload: dict[str, Any] = {
        "pose": settings.get("pose", base.pose if base else {}),
        "loops": settings.get("loops", base.loops if base else {}),
        "aliases": settings.get("aliases", base.aliases if base else ()),
        "duration": settings.get("duration", base.duration if base else 1.8),
        "attack": settings.get("attack", base.attack if base else 0.18),
        "hold": settings.get("hold", base.hold if base else 0.85),
        "release": settings.get("release", base.release if base else 0.55),
        "priority": settings.get("priority", base.priority if base else 1),
        "cooldown": settings.get("cooldown", base.cooldown if base else 3.0),
        "degrade": settings.get("degrade", base.degrade if base else {}),
        "states": settings.get("states", {}),
        "raw_overrides": settings.get("raw_overrides", {}),
        "persistent_raw": settings.get("persistent_raw", {}),
    }
    spec = spec_from_dict(name, payload)
    return payload if spec is not None else None


def _actions(profile: ModelProfile, actions: dict, warnings: list[str]) -> None:
    clean: dict[str, dict] = {}
    custom: dict[str, dict] = {}
    for name, settings in (actions or {}).items():
        name = str(name or "")
        if not isinstance(settings, dict):
            warnings.append(f"profile: 动作 {name} 配置不是对象，已忽略")
            continue
        recipe = any(key in settings for key in (
            "pose", "loops", "states", "raw_overrides", "persistent_raw",
            "aliases", "duration", "attack", "hold", "release",
            "priority", "cooldown"))
        if name not in DEFAULT_ACTIONS:
            if not recipe:
                warnings.append(f"profile: 未知动作 {name}，已忽略")
                continue
            payload = _custom_payload(name, settings)
            if payload is None:
                warnings.append(f"profile: 自定义动作 {name} 配方无效，已忽略")
                continue
            custom[name] = payload
        elif recipe:
            payload = _custom_payload(name, settings)
            if payload is not None:
                custom[name] = payload
        row: dict[str, Any] = {"enabled": bool(settings.get("enabled", True))}
        for key, low, high in (("intensity", 0.0, 1.0),
                               ("duration_scale", 0.5, 2.0)):
            if key in settings:
                try:
                    value = float(settings[key])
                except (TypeError, ValueError):
                    warnings.append(f"profile: actions.{name}.{key} 不是数字")
                    continue
                row[key] = max(low, min(high, value))
        clean[name] = row
    profile.actions = clean
    profile.custom_actions = custom


def apply_overrides(profile: ModelProfile, data: dict | None) -> list[str]:
    """原地应用 profile；返回 warning 列表。"""
    warnings: list[str] = []
    if not isinstance(data, dict):
        return ["profile: 不是 JSON 对象"]
    tuning = data.get("tuning")
    if isinstance(tuning, dict):
        profile.tuning = {str(k): v for k, v in tuning.items()}
    _bind(profile, data.get("channel_bindings"), warnings)
    _aliases(profile, data.get("aliases"), warnings)
    _actions(profile, data.get("actions"), warnings)
    if isinstance(data.get("motion_policy"), dict):
        profile.motion_policy = dict(data["motion_policy"])
    if isinstance(data.get("expressions"), dict):
        profile.expressions = {str(k): str(v) for k, v in data["expressions"].items()}
    if isinstance(data.get("prompt"), dict):
        profile.prompt = dict(data["prompt"])
    if data.get("archetype"):
        profile.archetype = str(data["archetype"])
    generated = data.get("generated_by")
    if isinstance(generated, dict):
        profile.fallback = bool(generated.get("fallback", False))
    profile.source = "profile"
    for warning in warnings:
        if warning not in profile.warnings:
            profile.warnings.append(warning)
    return warnings


def profile_from_dossier(dossier: dict) -> ModelProfile:
    """把审计 dossier 还原成运行时可用的 ModelProfile（runtime 不 import live2d）。"""
    profile = ModelProfile(
        tier=str(dossier.get("tier") or "none"),
        capabilities=dict(dossier.get("capabilities") or {}),
        warnings=list(dossier.get("warnings") or []),
        model_name=str((dossier.get("model") or {}).get("name") or ""),
        source="introspection",
        physics_outputs={
            str(x) for x in ((dossier.get("physics") or {}).get("outputs") or [])
            if str(x).strip()
        },
        physics_inputs={
            str(x) for x in ((dossier.get("physics") or {}).get("inputs") or [])
            if str(x).strip()
        },
    )
    follower_rows: dict[str, list[tuple[str, float]]] = {}
    raw_followers = dossier.get("followers")
    if isinstance(raw_followers, dict):
        for channel, rows in raw_followers.items():
            for item in rows if isinstance(rows, list) else []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                try:
                    scale = float(item.get("scale", 0.85))
                except (TypeError, ValueError):
                    scale = 0.85
                follower_rows.setdefault(str(channel), []).append(
                    (str(item["id"]), scale))
    for row in dossier.get("params") or []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        channel = str(row.get("channel") or "")
        try:
            minimum = float(row.get("min", 0.0))
            maximum = float(row.get("max", 0.0))
            default = float(row.get("default", 0.0))
        except (TypeError, ValueError):
            continue
        if channel not in CHANNEL_RANGES:
            continue
        try:
            gamma = float(row.get("gamma", 1.0))
        except (TypeError, ValueError):
            gamma = 1.0
        try:
            polarity = float(row.get("polarity", 1.0))
        except (TypeError, ValueError):
            polarity = 1.0
        effective = (row.get("effective_min"), row.get("effective_max"))
        try:
            effective_min = (None if effective[0] is None else float(effective[0]))
            effective_max = (None if effective[1] is None else float(effective[1]))
        except (TypeError, ValueError):
            effective_min = effective_max = None
        try:
            confidence = float(row.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        spec = ParamSpec(
            id=str(row["id"]), channel=channel,
            minimum=minimum, maximum=maximum, default=default,
            kind=str(row.get("kind") or kind_for_channel(
                channel, minimum, maximum, default)),
            score=int(row.get("score") or 0),
            match=f"dossier:{row['id']}",
            gamma=gamma, polarity=polarity,
            effective_min=effective_min, effective_max=effective_max,
            confidence=confidence,
            evidence=tuple(str(x) for x in row.get("evidence") or ()),
        )
        profile.params[spec.id] = spec
        profile.ranges[spec.id] = (minimum, maximum, default)
        if not row.get("follower"):
            profile.by_channel[channel] = spec
    profile.followers = {
        channel: [(pid, scale) for pid, scale in rows
                  if pid in profile.params]
        for channel, rows in follower_rows.items()
    }
    profile.followers = {k: v for k, v in profile.followers.items() if v}
    for name, row in (dossier.get("states") or {}).items():
        if not isinstance(row, dict) or not row.get("param_id"):
            continue
        try:
            profile.states[str(name)] = StateSpec(
                name=str(name),
                param_id=str(row["param_id"]),
                aliases=tuple(str(x) for x in row.get("aliases") or ()),
                active_value=float(row.get("active_value", 1.0)),
                inactive_value=float(row.get("inactive_value", 0.0)),
                mode=str(row.get("mode") or "persistent"),
                hold_seconds=float(row.get("hold_seconds") or 0.0),
                mutex_group=str(row.get("mutex_group") or ""),
                coupled_params={
                    str(k): float(v)
                    for k, v in (row.get("coupled_params") or {}).items()
                },
            )
        except (TypeError, ValueError):
            continue
    profile.screen_side = {
        str(k): str(v) for k, v in (dossier.get("screen_side") or {}).items()
    }
    origin = (dossier.get("model") or {}).get("pointer_origin")
    if isinstance(origin, (list, tuple)) and len(origin) >= 2:
        try:
            profile.pointer_origin = (float(origin[0]), float(origin[1]))
        except (TypeError, ValueError):
            profile.pointer_origin = None
    return profile

