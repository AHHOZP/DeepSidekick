"""唯一本地表演标签：`<ACT>...</ACT>`。

只实现紧凑语法，不接受 JSON；tag 在 TTS 前剥掉，不进入气泡/历史。情绪、动作、
视线是程序化语义；motion/expression 是模型资产，必须写前缀，避免“开心”到底是
表情还是情绪说不清。未知名字只剥离并记 warning，不抛异常。

未闭合的 `<ACT>` 从开口到文本结尾整段丢弃，绝不念出来。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sidekick.motion.actions import DEFAULT_ACTIONS, ActionSpec
from sidekick.motion.emotion import DEFAULT_EMOTIONS, resolve_emotion
from sidekick.motion.types import clamp

ACT_OPEN = re.compile(r"<ACT\b[^>]*>", re.IGNORECASE)
ACT_CLOSE = re.compile(r"</ACT\s*>", re.IGNORECASE)
_SPLIT = re.compile(r"[,，;；]+")
_KEY_BOUNDARY = re.compile(
    r"(?<![\w])(?:emotion|action|motion|expression|gaze|e|a|m|expr|look|g)\s*=",
    re.IGNORECASE)
_CJK = re.compile(r"[\u3400-\u9fff]")
_UNSAFE_PROMPT = re.compile(r"https?://|```|<script|javascript:", re.IGNORECASE)


def _iter_tokens(payload: str):
    """按逗号/分号切块；块内 key= 边界可保留带空格的模型资产名。"""
    for chunk in _SPLIT.split(str(payload or "")):
        chunk = chunk.strip()
        if not chunk:
            continue
        matches = list(_KEY_BOUNDARY.finditer(chunk))
        if not matches:
            for token in chunk.split():
                if token:
                    yield token
            continue
        if matches[0].start() > 0:
            for token in chunk[:matches[0].start()].split():
                if token:
                    yield token
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(chunk)
            token = chunk[match.start():end].strip()
            if not token:
                continue
            # 只有一个 key= 时，非资产类 value 里的空格仍按 token 分隔：
            # `emotion=开心 挥手` = 情绪 + 裸动作；motion/expression 的资产名
            # 允许含空格，原样保留。
            if len(matches) == 1:
                key, _, value = token.partition("=")
                if key.strip().lower() not in ("motion", "expression", "m", "expr"):
                    bits = value.split()
                    if len(bits) > 1:
                        yield f"{key}={bits[0]}"
                        for extra in bits[1:]:
                            yield extra
                        continue
            yield token

#: 视线是虚拟兴趣点，不是模型参数。
GAZE_POINTS: dict[str, tuple[float, float]] = {
    "camera": (0.0, 0.08),
    "game": (0.55, 0.10),
    "chat": (0.72, -0.25),
    "think": (-0.35, 0.55),
    "left": (-0.55, 0.0),
    "right": (0.55, 0.0),
    "up": (0.0, 0.55),
    "down": (0.0, -0.45),
}


def _action_aliases(actions=None) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, spec in (actions or DEFAULT_ACTIONS).items():
        out.setdefault(str(name).lower(), str(name))
        aliases = getattr(spec, "aliases", None)
        if aliases is None and isinstance(spec, dict):
            aliases = spec.get("aliases") or ()
        for alias in aliases or ():
            out.setdefault(str(alias).strip().lower(), str(name))
    return out


#: 精确别名失败后的词根兜底；顺序从具体到宽泛，避免“摇尾巴”落到 shake。
_FUZZY_ROOTS: tuple[tuple[str, str], ...] = (
    ("长裙", "long_skirt"), ("短裙", "short_skirt"), ("q版", "chibi_mode"),
    ("尾巴", "wag_tail"), ("手放胸口", "hand_chest"), ("抚胸", "hand_chest"),
    ("胸口", "hand_chest"), ("扭腰", "sway_hip"), ("扭扭腰", "sway_hip"),
    ("前倾", "lean"), ("凑近", "lean"), ("探身", "lean"), ("鞠躬", "bow"),
    ("看旁边", "look_away"), ("看别处", "look_away"), ("移开", "look_away"),
    ("挥手", "wave"), ("招手", "wave"), ("拜拜", "wave"),
    ("点头", "nod"), ("摇头", "shake"), ("歪头", "tilt"),
    ("踢腿", "kick"), ("抬腿", "kick"), ("伸腿", "kick"),
    ("换重心", "leg_shift"), ("挪腿", "leg_shift"), ("活动腿", "leg_shift"),
    ("跺脚", "leg_shift"),
    ("鼓掌", "clap"), ("欢呼", "cheer"), ("举手", "cheer"),
    ("伸懒腰", "stretch"), ("探头", "peek"), ("比划", "gesture"),
    ("摇", "shake"),
)


def _fuzzy_action(value: str) -> str | None:
    key = str(value or "").strip().lower()
    for root, action in _FUZZY_ROOTS:
        if root in key:
            return action
    return None


def resolve_action(name: str, actions=None) -> str | None:
    key = str(name or "").strip().lower()
    aliases = _action_aliases(actions)
    if key in aliases:
        return aliases[key]
    fuzzy = _fuzzy_action(key)
    if fuzzy:
        return aliases.get(fuzzy, fuzzy)
    return None


def _display(names: tuple[str, ...], fallback: str) -> str:
    for name in names:
        if _CJK.search(str(name)):
            return str(name)
    return str(fallback)


@dataclass
class ActParse:
    text: str
    items: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _add(items: list[dict], warnings: list[str], kind: str, name: str,
         intensity: float, offset: float = 0.0) -> None:
    for item in items:
        if item["kind"] == kind and item["name"].lower() == name.lower():
            item["intensity"] = max(item["intensity"], clamp(intensity, 0.0, 1.0))
            return
    item = {"kind": kind, "name": name,
            "intensity": round(clamp(intensity, 0.0, 1.0), 3)}
    if offset > 0.0:
        item["offset"] = round(float(offset), 4)
    items.append(item)


def _parse_payload(payload: str, items: list[dict], warnings: list[str], *,
                   motions: dict[str, str], expressions: dict[str, str],
                   offset: float = 0.0, actions=None) -> None:
    local = 0
    for raw in _iter_tokens(payload):
        token = raw.strip()
        if not token:
            continue
        if local >= 4:
            warnings.append("一个 <ACT> 最多 4 个意图，多余已丢弃")
            break
        intensity = 1.0
        if ":" in token or "：" in token:
            sep = ":" if ":" in token else "："
            head, _, tail = token.rpartition(sep)
            try:
                intensity = float(tail)
                token = head.strip()
            except ValueError:
                pass
        key = ""
        value = token
        if "=" in token:
            key, _, value = token.partition("=")
            key = key.strip().lower()
            value = value.strip()
        if not value:
            warnings.append("空表演名，已丢弃")
            continue
        kind = ""
        name = ""
        if key in ("emotion", "e", "mood"):
            canonical = resolve_emotion(value)
            if canonical:
                kind, name = "emotion", canonical
        elif key in ("action", "a", "state"):
            canonical = resolve_action(value, actions)
            if canonical:
                kind, name = "action", canonical
        elif key in ("motion", "m"):
            kind, name = "motion", motions.get(value.casefold(), "")
        elif key in ("expression", "expr"):
            kind, name = "expression", expressions.get(value.casefold(), "")
            if not name:
                # 某些模型把“默认体型/正常体型”做成 expression；它不是持久
                # 状态，继续按 expression 播会 6 秒后弹回 Q 版。若同名动作
                # 存在，交给动作层按 StateSpec 处理。
                canonical = resolve_action(value, actions)
                if canonical:
                    kind, name = "action", canonical
        elif key in ("gaze", "look", "g"):
            canonical = value.casefold()
            if canonical in GAZE_POINTS:
                kind, name = "gaze", canonical
        elif key:
            warnings.append(f"未知表演键 {key}，已丢弃")
            continue
        else:
            canonical = resolve_emotion(value)
            if canonical:
                kind, name = "emotion", canonical
            else:
                canonical = resolve_action(value, actions)
                if canonical:
                    kind, name = "action", canonical
        if not kind or not name:
            warnings.append(f"未知表演名 {value}，已丢弃")
            continue
        _add(items, warnings, kind, name, intensity, offset)
        local += 1


def parse(text: str, *, motions=None, expressions=None,
          actions=None) -> ActParse:
    """剥离所有 `<ACT>` 并返回语义/资产意图（最多 6 个）。"""
    source = str(text or "")
    result = ActParse(text="")
    motion_map = {str(k).casefold(): str(v) for k, v in (motions or {}).items()}
    expr_map = {str(k).casefold(): str(v) for k, v in (expressions or {}).items()}
    visible: list[str] = []
    pos = 0
    while pos < len(source):
        opening = ACT_OPEN.search(source, pos)
        if opening is None:
            visible.append(source[pos:])
            break
        visible.append(source[pos:opening.start()])
        closing = ACT_CLOSE.search(source, opening.end())
        if closing is None:
            result.warnings.append("未闭合的 <ACT>，已丢弃到文本结尾")
            pos = len(source)
            break
        _parse_payload(source[opening.end():closing.start()], result.items,
                       result.warnings, motions=motion_map,
                       expressions=expr_map, offset=len("".join(visible)),
                       actions=actions)
        pos = closing.end()

    clean = "".join(visible)
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    result.text = clean
    length = max(1, len(clean))
    for item in result.items:
        if "offset" not in item:
            continue
        raw = float(item.get("offset", 0.0))
        item["offset"] = round(clamp(raw / length, 0.0, 1.0), 4)
    if len(result.items) > 6:
        result.items = result.items[:6]
        result.warnings.append("一条回复最多 6 个意图，多余已丢弃")
    return result


def infer_action(text: str) -> str | None:
    """LLM 忘记写标签时的正文兜底；只认明确的动作祈使词。"""
    value = str(text or "")
    if not value:
        return None
    if any(word in value for word in (
            "没有", "不能", "不会", "没法", "做不到", "无法", "别")):
        return None
    # 只在“表演给你看”这类明确语境里兜底，避免把“我有一条尾巴”当成动作。
    if not any(word in value for word in (
            "看我", "看我的", "给你", "来，", "来~", "这就", "好呀", "当然",
            "马上", "表演", "摇给", "做给", "瞧好")):
        return None
    for root, action in _FUZZY_ROOTS:
        if root in value:
            return action
    return None


def raw_asset_names(text: str) -> list[tuple[str, str]]:
    """只抽出 `<ACT>` 里写过的 motion=/expression= 名字，用于校验未知资产。"""
    source = str(text or "")
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(source):
        opening = ACT_OPEN.search(source, pos)
        if opening is None:
            break
        closing = ACT_CLOSE.search(source, opening.end())
        if closing is None:
            break
        payload = source[opening.end():closing.start()]
        for token in _iter_tokens(payload):
            if "=" not in token:
                continue
            key, _, value = token.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if ":" in value or "：" in value:
                sep = ":" if ":" in value else "："
                value = value.rpartition(sep)[0].strip()
            if key in ("motion", "m") and value:
                out.append(("motion", value))
            elif key in ("expression", "expr") and value:
                out.append(("expression", value))
        pos = closing.end()
    return out


def vocabulary() -> dict:
    """给 prompt/WebUI 的词表：显示名 + 可接受别名。"""
    emotions = []
    for name, preset in DEFAULT_EMOTIONS.items():
        if name == "neutral":
            continue
        emotions.append({
            "name": name,
            "label": _display(preset.aliases, name),
            "aliases": list(preset.aliases),
        })
    actions = []
    for name, spec in DEFAULT_ACTIONS.items():
        actions.append({
            "name": name,
            "label": _display(spec.aliases, name),
            "aliases": list(spec.aliases),
        })
    return {
        "emotions": emotions,
        "actions": actions,
        "gaze": list(GAZE_POINTS),
    }


def capability_lines(channels=None, states=None) -> list[str]:
    """按当前模型真实绑定生成身体能力说明；没绑定的能力绝不出现。"""
    channels = set(channels or ())
    states = set(states or ())
    lines: list[str] = []
    if "tail" in channels:
        lines.append("- 尾巴：你可以自由摆动尾巴（action=摇尾巴）。")
    if "lean_y" in channels or "body_y" in channels:
        lines.append("- 前倾探身：你可以向前倾身凑近屏幕（action=前倾 / 凑近 / 探头）。")
    if "hip" in channels or "body_z" in channels:
        lines.append("- 扭腰律动：你可以扭腰、摇摆身体（action=扭腰）。")
    if any(ch in channels for ch in (
            "leg_l", "leg_r", "leg_l_x", "leg_r_x", "knee_l", "knee_r",
            "ankle_l", "ankle_r", "leg_l_y", "leg_r_y", "knee_l_y",
            "knee_r_y", "foot_l", "foot_r", "foot_l_y", "foot_r_y",
            "foot_swing")):
        lines.append("- 腿部：你可以踢腿、抬腿、换重心、活动脚踝"
                     "（action=踢腿 / 换重心 / 活动腿）。")
    if "chest_x" in channels or "chest_y" in channels:
        lines.append("- 胸部：可以随呼吸、前倾和身体转动起伏（不单独作为一个动作）。")
    if any(ch in channels for ch in ("hair_front", "hair_side", "hair_back")):
        lines.append("- 头发：已经绑定头发通道，会跟随头部和身体摆动；不需要单独写动作。")
    if "hand_style_chest" in channels or "arm_l" in channels:
        lines.append("- 手放胸口：你可以把手放在胸前（action=手放胸口）。")
    if "hand_style_point" in channels or "finger_r" in channels:
        lines.append("- 竖食指：你可以竖起食指指向前方（action=指）。")
    if "long_skirt" in states:
        lines.append("- 服装切换：支持长裙/短裙（action=穿长裙 / 穿短裙）。")
    if "chibi_mode" in states:
        lines.append("- 体型切换：支持 Q 版，也支持恢复（action=变Q版 / action=正常比例）。")
    if not lines:
        return []
    return ["【当前形象的真实身体能力】",
            "以下能力由模型参数真实绑定，用户提到时请积极展示，不要否认：",
            *lines]


def prompt_rules(vocab: dict | None = None, *, channels=None,
                 states=None) -> str:
    """通用表演标签说明；动作/表情资产由 actions.md 另教。"""
    data = vocab or vocabulary()
    emotions = "、".join(x["label"] for x in data.get("emotions") or [])
    action_items = list(data.get("actions") or [])
    if not states:
        hidden = {"long_skirt", "short_skirt", "chibi_mode", "normal_body"}
        action_items = [
            item for item in action_items
            if resolve_action(item.get("name", "")) not in hidden
        ]
    actions = "、".join(x["label"] for x in action_items)
    gaze = "、".join(data.get("gaze") or list(GAZE_POINTS))
    capability = capability_lines(channels, states)
    return "\n".join([
        "【表演标签】",
        "你可以在句尾加一个隐藏标签，用来控制形象。标签不会被念出来。",
        "格式：<ACT>emotion=名字, action=名字:强度</ACT>；强度 0–1，可省略。",
        *capability,
        f"可用情绪：{emotions}",
        f"可用动作：{actions}",
        f"可用视线：{gaze}",
        "怎么用：",
        "只用上面「可用情绪 / 可用动作 / 可用视线」里列出来的名字，"
        "别自己造词，也别在 `<ACT>` 里写 JSON。",
        "平时一次最多 1 个情绪加 1 个动作，真能帮语气加分再放，拿不准就别加；"
        "长回复可以按分句换动作，但整条最多 6 个意图，别每句硬加。",
        "标签放在句号、问号、感叹号后面（`<scene>` 前面），"
        "别插在句子中间，正文里也别用括号写动作描写。",
    ])


_DENIAL_RULES = re.compile(
    r"(没有|不支持|不能|无法|不具备|缺少).{0,10}"
    r"(尾巴|腿|脚|胸|头发|前倾|换装|手型|q版|Q版|动作|表情|情绪)",
    re.IGNORECASE,
)


def _clean_profile_rules(rules: str) -> str:
    """旧 profile 的静态否定句不能覆盖运行时能力卡。"""
    clauses = [part.strip() for part in re.split(r"[；;。\n]+", str(rules or ""))
               if part.strip()]
    kept = [part for part in clauses if not _DENIAL_RULES.search(part)]
    return "；".join(kept)


def prompt_rules_from_profile(prompt: dict, *, channels=None,
                              states=None, extra_actions=None,
                              disabled_actions=None) -> str:
    """把 profile.prompt 的词表转成与通用块相同形状的中文规则。"""
    if not isinstance(prompt, dict):
        return ""
    disabled = {str(x).casefold() for x in (disabled_actions or ())
                if str(x).strip()}
    emotions = []
    for name in prompt.get("emotion_tags") or []:
        canonical = resolve_emotion(str(name))
        if canonical:
            preset = DEFAULT_EMOTIONS.get(canonical)
            label = _display(preset.aliases, canonical) if preset else canonical
            if label not in emotions:
                emotions.append(label)
    actions = []
    for name in prompt.get("action_tags") or []:
        canonical = resolve_action(str(name))
        if canonical and canonical.casefold() not in disabled:
            spec = DEFAULT_ACTIONS.get(canonical)
            label = _display(spec.aliases, canonical) if spec else canonical
            if label not in actions:
                actions.append(label)
    for label in extra_actions or ():
        text = str(label).strip()
        canonical = resolve_action(text)
        if canonical and canonical.casefold() in disabled:
            continue
        if text and text not in actions:
            actions.append(text)
    if not states:
        hidden = {"long_skirt", "short_skirt", "chibi_mode", "normal_body"}
        actions = [
            label for label in actions
            if resolve_action(label) not in hidden
        ]
    gazes = [str(x).lower() for x in prompt.get("gaze_tags") or []
             if str(x).lower() in GAZE_POINTS]
    raw_rules = prompt.get("rules")
    if isinstance(raw_rules, (list, tuple)):
        rules = "；".join(str(x).strip() for x in raw_rules if str(x).strip())
    else:
        rules = str(raw_rules or "").strip()
    usage_raw = prompt.get("usage")
    usage = ([str(x).strip() for x in usage_raw if str(x).strip()]
             if isinstance(usage_raw, (list, tuple)) else [])
    if _UNSAFE_PROMPT.search(rules):
        rules = ""
    else:
        rules = _clean_profile_rules(rules)
    if any(_UNSAFE_PROMPT.search(x) for x in usage):
        usage = []
    if not (emotions or actions or gazes or rules or usage):
        return ""
    # 老生成结果可能只写了 rules，没写词表；运行时补通用词表，避免模型
    # 从 actions.md 的资产名里猜 emotion=/action=。
    if not emotions:
        emotions = [x["label"] for x in vocabulary()["emotions"]]
    if not actions:
        actions = []
        for item in vocabulary()["actions"]:
            label = str(item["label"])
            canonical = resolve_action(label)
            if canonical and canonical.casefold() in disabled:
                continue
            actions.append(label)
    if not states:
        hidden = {"long_skirt", "short_skirt", "chibi_mode", "normal_body"}
        actions = [
            label for label in actions
            if resolve_action(label) not in hidden
        ]
    if not gazes:
        gazes = list(GAZE_POINTS)
    lines = ["【表演标签】",
             "你可以在句尾加一个隐藏标签，用来控制形象。标签不会被念出来。",
             "格式：<ACT>emotion=名字, action=名字:强度</ACT>；强度 0–1，可省略。",
             *capability_lines(channels, states),
             "一条回复最多 6 个意图；长回复可以按分句各放一个。"]
    if emotions:
        lines.append("可用情绪：" + "、".join(emotions))
    if actions:
        lines.append("可用动作：" + "、".join(actions))
    if gazes:
        lines.append("可用视线：" + "、".join(gazes))
    if usage:
        lines.append("怎么用这些标签：")
        lines.extend(usage)
    elif rules:
        lines.append("规则：" + rules)
    return "\n".join(lines)
