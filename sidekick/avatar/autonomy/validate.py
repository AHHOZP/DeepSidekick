"""motion_profile.json 校验/修复边界。

只做数据校验：JSON 形状、范围、ID 是否在 dossier、词表是否存在、prompt 是否像
代码/URL。返回的 clean 结果可直接给 WebUI 预览或运行时；issues 用字段路径描述，
供 repair prompt 只改相关字段。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace

from sidekick.avatar import act_tags
from sidekick.motion.actions import RECIPE_KEYS
from sidekick.avatar.autonomy.schema import (
    ACTION_CATALOG, ARCHETYPES, FORBIDDEN_KEYS, GAZE_TAGS, SCHEMA_VERSION,
    tuning_defaults,
)
from sidekick.motion.emotion import resolve_emotion
from sidekick.motion.tuning import TUNING_RANGES
from sidekick.motion.types import CHANNEL_RANGES

_BAD_PROMPT = re.compile(r"https?://|```|<script|javascript:", re.IGNORECASE)


@dataclass(frozen=True)
class Issue:
    field: str
    message: str
    #: 语法/版本错误为 true；可丢弃/可 clamp 的字段为 false。
    fatal: bool = True

    def text(self) -> str:
        return f"{self.field}: {self.message}"


def _num(value, low: float | None = None, high: float | None = None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def _allowed_ids(dossier: dict | None) -> set[str]:
    if not isinstance(dossier, dict):
        return set()
    ids = {str(p.get("id")) for p in dossier.get("params") or []
           if isinstance(p, dict) and p.get("id")}
    ids.update(str(x) for x in (dossier.get("by_channel") or {}).values())
    ids.update(str(row.get("param_id"))
               for row in (dossier.get("states") or {}).values()
               if isinstance(row, dict) and row.get("param_id"))
    # 未映射但真实存在的参数也允许 LLM 用 alias 绑定；否则眼睛/头发/hip 的
    # 高价值候选会被误判成“参数不在模型里”。
    for row in ((dossier.get("unmapped") or {}).get("rows") or []):
        if isinstance(row, dict) and row.get("id"):
            ids.add(str(row["id"]))
    for item in dossier.get("expressions") or []:
        if not isinstance(item, dict):
            continue
        ids.update(str(control.get("id"))
                   for control in item.get("controls") or []
                   if isinstance(control, dict) and control.get("id"))
    return ids


def _motion_groups(dossier: dict | None) -> set[str]:
    return {str(m.get("group")) for m in (dossier or {}).get("motions") or []
            if isinstance(m, dict) and m.get("group")}


def _expressions(dossier: dict | None) -> set[str]:
    return {str(e.get("name")) for e in (dossier or {}).get("expressions") or []
            if (isinstance(e, dict) and e.get("name")
                and str(e.get("kind") or "expression") == "expression")}


def _validate_tuning(raw, issues: list[Issue], archetype: str) -> dict:
    out = tuning_defaults(archetype)
    if not isinstance(raw, dict):
        issues.append(Issue("tuning", "必须是对象，已用默认值"))
        return out
    for key, (default, low, high) in TUNING_RANGES.items():
        if key not in raw:
            continue
        if key == "body_gain":
            try:
                raw_body = float(raw[key])
            except (TypeError, ValueError):
                raw_body = None
            if raw_body is not None and raw_body < 0.40:
                issues.append(Issue(
                    f"tuning.{key}",
                    "低于全局倍率下限，已用默认值", fatal=False))
                out[key] = default
                continue
        value = _num(raw[key], low, high)
        if value is None:
            issues.append(Issue(f"tuning.{key}", "不是数字，已用默认值"))
            continue
        if value != float(raw[key]):
            issues.append(Issue(f"tuning.{key}", f"超出 {low}..{high}，已 clamp"))
        out[key] = value
    for key in raw:
        if key not in out:
            issues.append(Issue(f"tuning.{key}", "未知键，已忽略"))
    return out


def _action_recipe(name: str, settings: dict, dossier, issues: list[Issue]) -> dict:
    row: dict = {}
    pose = settings.get("pose")
    if isinstance(pose, dict):
        clean_pose = {}
        for channel, value in pose.items():
            channel = str(channel)
            if channel not in CHANNEL_RANGES:
                issues.append(Issue(
                    f"actions.{name}.pose.{channel}", "未知通道，已丢弃", fatal=False))
                continue
            number = _num(value, -1.5, 1.5)
            if number is None:
                continue
            clean_pose[channel] = number
        row["pose"] = clean_pose
    loops = settings.get("loops")
    if isinstance(loops, dict):
        clean_loops = {}
        for channel, value in loops.items():
            channel = str(channel)
            if channel not in CHANNEL_RANGES:
                continue
            if not isinstance(value, (list, tuple)) or len(value) < 3:
                issues.append(Issue(
                    f"actions.{name}.loops.{channel}", "需要 [幅度, 频率, 相位]",
                    fatal=False))
                continue
            try:
                amp, hz, phase = float(value[0]), float(value[1]), float(value[2])
            except (TypeError, ValueError):
                continue
            if not all(math.isfinite(x) for x in (amp, hz, phase)):
                continue
            clean_loops[channel] = [
                max(-1.5, min(1.5, amp)),
                max(0.05, min(8.0, hz)), phase,
            ]
        row["loops"] = clean_loops
    for key in ("states", "raw_overrides", "persistent_raw"):
        values = settings.get(key)
        if not isinstance(values, dict):
            continue
        if key == "states":
            row[key] = {str(k): bool(v) for k, v in values.items()}
        else:
            allowed = _allowed_ids(dossier)
            row[key] = {
                str(k): float(v) for k, v in values.items()
                if (not dossier or str(k) in allowed)
                and _num(v) is not None
            }
    aliases = settings.get("aliases")
    if isinstance(aliases, (list, tuple)):
        row["aliases"] = [str(x)[:30] for x in aliases if str(x).strip()][:12]
    degrade = settings.get("degrade")
    if isinstance(degrade, dict):
        clean_degrade: dict[str, dict[str, float]] = {}
        for channel, targets in degrade.items():
            channel = str(channel)
            if channel not in CHANNEL_RANGES or not isinstance(targets, dict):
                continue
            clean_degrade[channel] = {
                str(target): max(-2.0, min(2.0, float(factor)))
                for target, factor in targets.items()
                if target in CHANNEL_RANGES and _num(factor) is not None
            }
        row["degrade"] = clean_degrade
    for key, low, high in (("duration", 0.12, 10.0), ("attack", 0.04, 3.0),
                           ("hold", 0.02, 8.0), ("release", 0.05, 5.0),
                           ("priority", 0.0, 10.0), ("cooldown", 0.0, 60.0)):
        if key in settings:
            value = _num(settings[key], low, high)
            if value is not None:
                row[key] = value
    return row


def _validate_actions(raw, issues: list[Issue], dossier=None) -> dict:
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for name, settings in raw.items():
        raw_name = str(name)
        canonical = act_tags.resolve_action(raw_name)
        recipe = (any(key in settings for key in RECIPE_KEYS)
                  if isinstance(settings, dict) else False)
        if canonical is None and not recipe:
            issues.append(Issue(f"actions.{raw_name}", "不在动作目录，已丢弃",
                                fatal=False))
            continue
        name = canonical or raw_name
        if not isinstance(settings, dict):
            issues.append(Issue(f"actions.{name}", "必须是对象，已丢弃",
                                fatal=False))
            continue
        row = {"enabled": bool(settings.get("enabled", True))}
        if "intensity" in settings:
            value = _num(settings["intensity"], 0.0, 1.0)
            if value is None:
                issues.append(Issue(f"actions.{name}.intensity", "不是数字，已丢弃"))
            else:
                row["intensity"] = value
        if "duration_scale" in settings:
            value = _num(settings["duration_scale"], 0.5, 2.0)
            if value is None:
                issues.append(Issue(
                    f"actions.{name}.duration_scale", "不是数字，已丢弃"))
            else:
                row["duration_scale"] = value
        if recipe:
            row.update(_action_recipe(name, settings, dossier, issues))
        out[name] = row
    return out


def _validate_states(raw, dossier, issues: list[Issue]) -> dict:
    """状态是模型事实；LLM/手写 profile 都不能凭空造。"""
    facts = (dossier or {}).get("states")
    if not isinstance(facts, dict):
        if isinstance(raw, dict) and raw:
            issues.append(Issue("states", "没有模型审计事实，已丢弃", fatal=False))
        return {}
    return {str(k): dict(v) for k, v in facts.items()
            if isinstance(v, dict)}


def _validate_bindings(raw, dossier, issues: list[Issue]) -> dict:
    out: dict[str, str] = {}
    allowed = _allowed_ids(dossier)
    used: set[str] = set()
    if not isinstance(raw, dict):
        return out
    for channel, pid in raw.items():
        channel, pid = str(channel), str(pid)
        if channel not in CHANNEL_RANGES:
            issues.append(Issue(f"channel_bindings.{channel}", "未知通道，已丢弃"))
            continue
        if dossier is not None and pid not in allowed:
            issues.append(Issue(
                f"channel_bindings.{channel}", f"参数 {pid} 不在模型里，已丢弃"))
            continue
        if pid in used:
            issues.append(Issue(
                f"channel_bindings.{channel}", f"参数 {pid} 重复绑定，已丢弃"))
            continue
        used.add(pid)
        out[channel] = pid
    return out


def _validate_aliases(raw, dossier, issues: list[Issue]) -> dict:
    out: dict[str, str] = {}
    allowed = _allowed_ids(dossier)
    bound = set((dossier or {}).get("by_channel") or {})
    if not isinstance(raw, dict):
        return out
    bad_targets: list[str] = []
    used_channels = set(bound)
    for pid, channel in raw.items():
        pid, channel = str(pid), str(channel)
        if dossier is not None and pid not in allowed:
            issues.append(Issue(f"aliases.{pid}", "参数不在模型里，已丢弃"))
            continue
        if channel not in CHANNEL_RANGES:
            bad_targets.append(pid)
            continue
        if channel in used_channels:
            issues.append(Issue(
                f"aliases.{pid}",
                f"通道 {channel} 已有主绑定或前面的 alias，已丢弃",
                fatal=False))
            continue
        out[pid] = channel
        used_channels.add(channel)
    if bad_targets:
        sample = "、".join(bad_targets[:4])
        issues.append(Issue(
            "aliases",
            f"{len(bad_targets)} 个 alias 的目标不是语义通道，已丢弃（如 {sample}）；"
            "alias 应写成 参数ID → 语义通道，不是 参数ID → 参数ID",
            fatal=False))
    return out


def _validate_policy(raw, dossier, issues: list[Issue]) -> dict:
    out = {
        "mode": "auto",
        "model_motions": {},
        "ownership": {"crossfade_ms": 320, "mouth_priority": "lipsync",
                      "expression_priority": "model"},
    }
    if not isinstance(raw, dict):
        issues.append(Issue("motion_policy", "必须是对象，已用默认值"))
        return out
    if str(raw.get("mode") or "auto") != "auto":
        issues.append(Issue("motion_policy.mode", "只支持 auto，已收敛"))
    groups = _motion_groups(dossier)
    motions = raw.get("model_motions")
    if isinstance(motions, dict):
        for group, settings in motions.items():
            group = str(group)
            if dossier is not None and group not in groups:
                issues.append(Issue(
                    f"motion_policy.model_motions.{group}", "motion 不存在，已丢弃"))
                continue
            if not isinstance(settings, dict):
                issues.append(Issue(
                    f"motion_policy.model_motions.{group}", "必须是对象，已丢弃"))
                continue
            row = {"enabled": bool(settings.get("enabled", True))}
            action = str(settings.get("action") or "")
            if action:
                if action not in ACTION_CATALOG:
                    issues.append(Issue(
                        f"motion_policy.model_motions.{group}.action",
                        "动作不在目录，已清空"))
                else:
                    row["action"] = action
            if "cooldown" in settings:
                value = _num(settings["cooldown"], 0.0, 60.0)
                if value is not None:
                    row["cooldown"] = value
            out["model_motions"][group] = row
    ownership = raw.get("ownership")
    if isinstance(ownership, dict):
        crossfade = _num(ownership.get("crossfade_ms", 320), 100.0, 600.0)
        out["ownership"]["crossfade_ms"] = int(crossfade if crossfade is not None else 320)
    return out


def _validate_expressions(raw, dossier, issues: list[Issue]) -> dict:
    out: dict[str, str] = {}
    allowed = _expressions(dossier)
    if not isinstance(raw, dict):
        return out
    for raw_emotion, name in raw.items():
        emotion = resolve_emotion(str(raw_emotion))
        if not emotion:
            issues.append(Issue(f"expressions.{raw_emotion}", "未知情绪，已丢弃"))
            continue
        name = str(name)
        if dossier is not None and name not in allowed:
            issues.append(Issue(
                f"expressions.{emotion}", f"表情 {name} 不存在，已丢弃"))
            continue
        out[emotion] = name
    return out


def _validate_prompt(raw, issues: list[Issue]) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    emotions = [str(x) for x in raw.get("emotion_tags") or [] if str(x).strip()]
    valid_emotions = []
    for name in emotions:
        canonical = resolve_emotion(name)
        if canonical:
            valid_emotions.append(canonical)
        else:
            issues.append(Issue(f"prompt.emotion_tags.{name}", "未知情绪，已丢弃"))
    out["emotion_tags"] = valid_emotions[:24]
    actions = [str(x) for x in raw.get("action_tags") or [] if str(x).strip()]
    valid_actions = []
    for name in actions:
        canonical = act_tags.resolve_action(name)
        if canonical:
            valid_actions.append(canonical)
        else:
            issues.append(Issue(f"prompt.action_tags.{name}", "未知动作，已丢弃"))
    out["action_tags"] = valid_actions[:32]
    gazes = [str(x).lower() for x in raw.get("gaze_tags") or []]
    valid_gazes = []
    for name in gazes:
        if name in GAZE_TAGS:
            valid_gazes.append(name)
        else:
            issues.append(Issue(f"prompt.gaze_tags.{name}", "未知视线，已丢弃"))
    out["gaze_tags"] = valid_gazes[:8]
    raw_rules = raw.get("rules")
    if isinstance(raw_rules, (list, tuple)):
        rules = "；".join(str(x).strip() for x in raw_rules if str(x).strip())
    else:
        rules = str(raw_rules or "").strip()
    if _BAD_PROMPT.search(rules):
        issues.append(Issue("prompt.rules", "含 URL/代码/HTML，已丢弃"))
        rules = ""
    out["rules"] = rules[:2000]
    raw_usage = raw.get("usage")
    usage = ([str(x).strip() for x in raw_usage if str(x).strip()]
             if isinstance(raw_usage, (list, tuple)) else [])
    if any(_BAD_PROMPT.search(x) for x in usage):
        issues.append(Issue("prompt.usage", "含 URL/代码/HTML，已丢弃"))
        usage = []
    if usage:
        out["usage"] = [x[:1000] for x in usage][:20]
    vocab = act_tags.vocabulary()
    if not out["emotion_tags"]:
        out["emotion_tags"] = [x["name"] for x in vocab["emotions"]]
        issues.append(Issue("prompt.emotion_tags", "为空，已补通用情绪词表",
                            fatal=False))
    if not out["action_tags"]:
        out["action_tags"] = [x["name"] for x in vocab["actions"]]
        issues.append(Issue("prompt.action_tags", "为空，已补通用动作词表",
                            fatal=False))
    if not out["gaze_tags"]:
        out["gaze_tags"] = list(GAZE_TAGS)
        issues.append(Issue("prompt.gaze_tags", "为空，已补通用视线词表",
                            fatal=False))
    if not any(out.values()):
        issues.append(Issue("prompt", "词表/规则为空，已用通用词表", fatal=False))
    return out


def validate(draft, dossier: dict | None = None) -> tuple[dict, list[Issue]]:
    """返回 ``(clean, issues)``；issues 非空时调用方走 repair/fallback。"""
    issues: list[Issue] = []
    if not isinstance(draft, dict):
        return {}, [Issue("", "顶层必须是 JSON 对象")]
    for key in FORBIDDEN_KEYS:
        if key in draft:
            issues.append(Issue(key, "不允许的字段，已丢弃"))
    allowed_keys = {
        "schema_version", "generated_by", "archetype", "archetype_confidence",
        "tuning", "channel_bindings", "aliases", "actions", "states",
        "motion_policy", "expressions", "prompt", "warnings",
    }
    for key in draft:
        if key not in allowed_keys:
            issues.append(Issue(str(key), "未知字段，已忽略"))
    for key in ("tuning", "actions", "motion_policy", "prompt"):
        if key not in draft:
            issues.append(Issue(key, "缺少必填字段"))
    if draft.get("schema_version") != SCHEMA_VERSION:
        issues.append(Issue("schema_version", f"必须是 {SCHEMA_VERSION}"))
    archetype = str(draft.get("archetype") or "unknown")
    if archetype not in ARCHETYPES:
        issues.append(Issue("archetype", "未知类型，已收敛到 unknown"))
        archetype = "unknown"
    confidence = _num(draft.get("archetype_confidence", 0.0), 0.0, 1.0)
    generated = draft.get("generated_by")
    if not isinstance(generated, dict):
        issues.append(Issue("generated_by", "必须是对象"))
        generated = {}
    generated = {
        "provider": str(generated.get("provider") or ""),
        "model": str(generated.get("model") or ""),
        "at": str(generated.get("at") or ""),
        "fallback": bool(generated.get("fallback", False)),
    }
    prompt = _validate_prompt(draft.get("prompt"), issues)
    clean = {
        "schema_version": SCHEMA_VERSION,
        "generated_by": generated,
        "archetype": archetype,
        "archetype_confidence": confidence if confidence is not None else 0.0,
        "tuning": _validate_tuning(draft.get("tuning"), issues, archetype),
        "channel_bindings": _validate_bindings(
            draft.get("channel_bindings"), dossier, issues),
        "aliases": _validate_aliases(draft.get("aliases"), dossier, issues),
        "actions": _validate_actions(draft.get("actions"), issues, dossier),
        "states": _validate_states(draft.get("states"), dossier, issues),
        "motion_policy": _validate_policy(
            draft.get("motion_policy"), dossier, issues),
        "expressions": _validate_expressions(
            draft.get("expressions"), dossier, issues),
        "prompt": prompt,
        "warnings": [
            str(x)[:200] for x in (draft.get("warnings") or []) if str(x).strip()
        ][:50],
    }
    clean["warnings"].extend(
        i.text() for i in issues if not _is_fatal(i))
    return clean, [_mark_fatal(i) for i in issues]


def _is_fatal(issue: Issue) -> bool:
    """只有结构不可用才需要整份 fallback；其余字段可丢弃/收敛后继续。"""
    if issue.field == "schema_version":
        return True
    if issue.message == "顶层必须是 JSON 对象":
        return True
    if issue.message == "缺少必填字段" and issue.field in (
            "tuning", "actions", "motion_policy", "prompt"):
        return True
    return False


def _mark_fatal(issue: Issue) -> Issue:
    return replace(issue, fatal=_is_fatal(issue))


def main(argv: list[str] | None = None) -> int:
    """人工校验：按模型 dossier 检查角色目录里的 motion_profile.json。"""
    import argparse
    import json
    from pathlib import Path

    from sidekick.avatar.autonomy.audit import find_avatar, run_audit
    from sidekick.avatar.autonomy.store import load, path

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--root", default="")
    parser.add_argument("--profile", default="")
    parser.add_argument("--no-cache", action="store_true",
                        help="跳过审计缓存，强制重新扫描模型")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else Path.cwd()
    avatar = find_avatar(Path(args.model).resolve(), root)
    dossier = run_audit(root, Path(args.model).resolve(),
                        use_cache=not args.no_cache)
    target = Path(args.profile) if args.profile else path(avatar)
    try:
        draft = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False))
        return 2
    clean, issues = validate(draft, dossier)
    print(json.dumps({"profile": clean,
                      "issues": [x.text() for x in issues],
                      "ok": not issues}, ensure_ascii=False, indent=2))
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
