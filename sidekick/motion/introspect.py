"""Unknown-model introspection and normalisation.

Live2D has no mandatory binding standard.  One model exposes ``ParamAngleX``,
another ``PARAM_ANGLE_X``, a third adds ``ParamBodyAngleZ1`` and a fourth only
has ``ParamMouthOpenY``.  This module maps whatever a model actually exposes
onto the engine's semantic channels.

The matching order is important:

1. exact canonical aliases (``ParamAngleX``, ``ANGLE_X``);
2. the same alias after stripping copy/duplicate suffixes (``AngleXbs``);
3. a small, whole-name regex set (never substring matching);
4. nothing.

Step 4 is deliberate.  A wrong binding is worse than no binding: writing a
random custom parameter named ``Param12`` can move a prop, the background, or
nothing at all, while the character stands frozen.  Unmapped parameters are
reported so a human can add an alias for that model.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from .types import CHANNEL_RANGES, ModelProfile, ParamSpec, StateSpec, clamp
from .vtube import VTubeEvidence


class ParamSource(Protocol):
    """Minimal interface shared by live2d-py and the test fake."""

    def param_ids(self) -> list[str]: ...
    def param_info(self, param_id: str) -> tuple[float, float, float]: ...


# ── normalisation ─────────────────────────────────────
_PREFIX = re.compile(r"^param[\s_\-]*", re.IGNORECASE)
_SEPARATORS = re.compile(r"[\s_\-]+")
_SUFFIX = re.compile(r"(?:copy|mirror|bs|backup|alt)+$", re.IGNORECASE)
_TRAILING_DIGITS = re.compile(r"\d+$")


def canonical(pid: str) -> str:
    s = _PREFIX.sub("", str(pid or "").strip())
    s = _SEPARATORS.sub("", s)
    s = s.lower()
    return s


def canonical_base(pid: str) -> str:
    """``ParamBodyAngleZ1`` -> ``bodyanglez``; used only as a fallback."""
    s = canonical(pid)
    s = _SUFFIX.sub("", s)
    s = _TRAILING_DIGITS.sub("", s)
    return s


# ── exact aliases ─────────────────────────────────────
_ALIASES: dict[str, tuple[str, int]] = {
    # Head
    "anglex": ("head_x", 100), "angle1": ("head_x", 70),
    "angley": ("head_y", 100), "angle2": ("head_y", 70),
    "anglez": ("head_z", 100), "angle3": ("head_z", 70),
    "headanglex": ("head_x", 95), "headangley": ("head_y", 95),
    "headanglez": ("head_z", 95),
    "anglexbs": ("head_x", 90), "angleybs": ("head_y", 90),
    "anglezbs": ("head_z", 90),
    "faceanglex": ("head_x", 90), "faceangley": ("head_y", 90),
    "faceanglez": ("head_z", 90),
    "headx": ("head_x", 80), "heady": ("head_y", 80), "headz": ("head_z", 80),
    "neckx": ("head_x", 80), "necky": ("head_y", 80), "neckz": ("head_z", 80),
    "neckanglex": ("head_x", 85), "neckangley": ("head_y", 85),
    "neckanglez": ("head_z", 85),
    # Body
    "bodyanglex": ("body_x", 100), "bodyangley": ("body_y", 100),
    "bodyanglez": ("body_z", 100),
    # 部分 VTube 拆包模型把物理输入拆成“角度 X/Y/Z + 身体 X/XX/Y/YY/Z/ZZ”；
    # 身体副本不能按 canonical_base 落回头部，否则会写成 head_x 的 follower。
    "anglex4": ("body_x", 68), "anglex5": ("body_x", 68),
    "angley4": ("body_y", 68), "angley5": ("body_y", 68),
    "anglez4": ("body_z", 68), "anglez5": ("body_z", 68),
    "bodyanglexx": ("body_x", 68), "bodyangleyy": ("body_y", 68),
    "bodyanglezz": ("body_z", 68),
    "bodyanglex1": ("body_x", 72), "bodyangley1": ("body_y", 72),
    "bodyanglez1": ("body_z", 72), "bodyanglez2": ("body_z", 72),
    "bodyx": ("body_x", 85), "bodyy": ("body_y", 85), "bodyz": ("body_z", 85),
    "bodyrotationx": ("body_x", 75), "bodyrotationy": ("body_y", 75),
    "bodyrotationz": ("body_z", 75),
    "bodybreechx": ("hip", 70),
    "upperbodyx": ("body_x", 70), "upperbodyy": ("body_y", 70),
    "upperbodyz": ("body_z", 70), "upperbody": ("body_z", 55),
    # Eyes
    "eyeballx": ("eye_x", 100), "eyebally": ("eye_y", 100),
    "eyeopenl": ("eye_l_open", 95), "eyeopenr": ("eye_r_open", 95),
    "eyelopen": ("eye_l_open", 100), "eyeropen": ("eye_r_open", 100),
    "eyelidopenl": ("eye_l_open", 90), "eyelidopenr": ("eye_r_open", 90),
    "eyeleftopen": ("eye_l_open", 90), "eyerightopen": ("eye_r_open", 90),
    "eyesmile": ("eye_l_smile", 80),
    "eyesmiles": ("eye_l_smile", 75),
    "eyesquint": ("eye_squint", 90), "eyesquintl": ("eye_squint", 80),
    "eyesquintr": ("eye_squint", 80),
    "eyelsmile": ("eye_l_smile", 100), "eyersmile": ("eye_r_smile", 100),
    "eyelaugh": ("eye_l_smile", 70), "eyerlaugh": ("eye_r_smile", 70),
    "eyeanger": ("brow_l_angle", 40),
    # Brows
    "browly": ("brow_l_y", 100), "browry": ("brow_r_y", 100),
    "browlangle": ("brow_l_angle", 100), "browrangle": ("brow_r_angle", 100),
    "browlform": ("brow_l_form", 100), "browrform": ("brow_r_form", 100),
    "browly2": ("brow_l_y", 65), "browry2": ("brow_r_y", 65),
    "browly3": ("brow_l_y", 60), "browry3": ("brow_r_y", 60),
    "browinnerup": ("brow_l_y", 55),
    # Mouth
    "mouthopeny": ("mouth_open", 100), "mouthopen": ("mouth_open", 95),
    "jawopen": ("mouth_open", 80), "mouthopenx": ("mouth_x", 80),
    "mouthform": ("mouth_form", 100), "mouthsmile": ("mouth_form", 85),
    "mouthsmilel": ("mouth_form", 70), "mouthsmiler": ("mouth_form", 70),
    "mouthx": ("mouth_x", 90), "mouthdelta": ("mouth_form", 70),
    "mouthpucker": ("mouth_form", 60), "mouthfunnel": ("mouth_form", 60),
    # Cheek / breath
    "cheek": ("cheek", 95), "paramcheek": ("cheek", 90),
    "cheek38": ("cheek", 75), "cheek79": ("cheek", 75),
    "cheekpuff": ("cheek", 85),
    "breath": ("breath", 100),
    # Arms
    "armla": ("arm_l", 100), "arml": ("arm_l", 95), "upperarml": ("arm_l", 90),
    "leftarm": ("arm_l", 85), "armleft": ("arm_l", 85),
    "arml1": ("arm_l", 80), "larma": ("arm_l", 80),
    # 部分中文/游戏拆包模型用 L/R 前缀而不是后缀（LArm/LForearm/LHand）。
    "larm": ("arm_l", 100), "rarm": ("arm_r", 100),
    "lforearm": ("arm_l_bend", 92), "rforearm": ("arm_r_bend", 92),
    "lhand": ("hand_l", 100), "rhand": ("hand_r", 100),
    "armlb": ("arm_l_bend", 100), "larmb": ("arm_l_bend", 85),
    "forearml": ("arm_l_bend", 90),
    "lowerarml": ("arm_l_bend", 85),
    "arml2": ("arm_l_bend", 75), "arml3": ("arm_l_bend", 70),
    "arml4": ("arm_l_bend", 65),
    "armra": ("arm_r", 100), "armr": ("arm_r", 95), "upperarmr": ("arm_r", 90),
    "rightarm": ("arm_r", 85), "armright": ("arm_r", 85),
    "armr1": ("arm_r", 80), "rarma": ("arm_r", 80),
    "armrb": ("arm_r_bend", 100), "rarmb": ("arm_r_bend", 85),
    "forearmr": ("arm_r_bend", 90),
    "lowerarmr": ("arm_r_bend", 85),
    "armr2": ("arm_r_bend", 75), "armr3": ("arm_r_bend", 70),
    "armr4": ("arm_r_bend", 65),
    "handl": ("hand_l", 100), "lefthand": ("hand_l", 90),
    "handr": ("hand_r", 100), "righthand": ("hand_r", 90),
    "shoulderl": ("shoulder_l", 100), "leftshoulder": ("shoulder_l", 90),
    "lshoulder": ("shoulder_l", 90),
    "shoulderr": ("shoulder_r", 100), "rightshoulder": ("shoulder_r", 90),
    "rshoulder": ("shoulder_r", 90),
    # Legs / hips
    "legl": ("leg_l", 100), "leftleg": ("leg_l", 90), "thighl": ("leg_l", 85),
    "lleg": ("leg_l", 90),
    "legr": ("leg_r", 100), "rightleg": ("leg_r", 90), "thighr": ("leg_r", 85),
    "rleg": ("leg_r", 90),
    "leganglez": ("leg_l", 72), "leganglez2": ("leg_l", 70),
    "hip": ("hip", 90), "hipanglex": ("hip", 70), "hipangley": ("hip", 65),
    "hipanglez": ("hip", 75), "hipanglez2": ("hip", 72),
    "hipx": ("hip", 55), "pelvis": ("hip", 70),
    # Hair
    "hairfront": ("hair_front", 95), "hairside": ("hair_side", 95),
    "hairback": ("hair_back", 95),
}

# 关节命名的通用写法：ThighL / LThigh / LowerLegR / RAnkle / FootL / ToeL。
# 这里没有模型 ID 表；同一套词表对任何使用这些常见英文名的模型生效。
_JOINT_WORDS: dict[str, str] = {
    "thigh": "leg", "upperleg": "leg", "legupper": "leg",
    "knee": "knee", "lowerleg": "knee", "calf": "knee", "shin": "knee",
    "ankle": "ankle",
    "foot": "foot",
    "toe": "foot_y",
}
for _part, _channel in _JOINT_WORDS.items():
    for _side in ("l", "r"):
        _target = (f"foot_{_side}_y" if _channel == "foot_y"
                   else f"{_channel}_{_side}")
        _ALIASES.setdefault(f"{_part}{_side}", (_target, 80))
        _ALIASES.setdefault(f"{_side}{_part}", (_target, 80))

# Whole-name regex fallbacks.  They intentionally require the name to be made
# of known words, so ``Param12`` and ``Paramcol_1`` stay unmapped.
_PATTERNS: tuple[tuple[re.Pattern[str], str, int], ...] = (
    (re.compile(r"^(?:head|face)?angle([xyz])$"), "head", 75),
    (re.compile(r"^bodyangle([xyz])$"), "body", 75),
    (re.compile(r"^eyeball([xy])$"), "eye_axis", 75),
    (re.compile(r"^eye(l|r)(open|smile)$"), "eye_part", 70),
    (re.compile(r"^(brow)(l|r)(y|angle|form)$"), "brow", 70),
    (re.compile(r"^mouth(open|form|x)$"), "mouth", 65),
    (re.compile(r"^leg([lr])([123])([yz])$"), "leg_axis", 82),
    (re.compile(r"^leg([lr])([123])x$"), "leg_axis_x", 66),
    (re.compile(r"^leg([lr])f$"), "foot_swing", 72),
    (re.compile(r"^foot([lr])([xy])$"), "foot_axis", 82),
    (re.compile(r"^skirt(l|r|side)(\d+)?$"), "skirt", 78),
    (re.compile(r"^tailx(\d+)?$"), "tail", 80),
    (re.compile(r"^breast([xy])(\d+)?$"), "chest", 72),
    (re.compile(r"^hair(back|front|braid)([a-z]*)(\d+)?$"), "hair", 78),
    (re.compile(r"^(arm|hand|leg|shoulder)(l|r)(a|b)?$"), "limb", 60),
)


def _pattern_match(base: str) -> tuple[str, int, str] | None:
    for pattern, kind, score in _PATTERNS:
        match = pattern.match(base)
        if not match:
            continue
        groups = match.groups()
        if kind == "head":
            return f"head_{groups[0]}", score, "regex"
        if kind == "body":
            return f"body_{groups[0]}", score, "regex"
        if kind == "eye_axis":
            return f"eye_{groups[0]}", score, "regex"
        if kind == "eye_part":
            side = "l" if groups[0] == "l" else "r"
            return f"eye_{side}_{groups[1]}", score, "regex"
        if kind == "brow":
            side = "l" if groups[1] == "l" else "r"
            return f"brow_{side}_{groups[2]}", score, "regex"
        if kind == "mouth":
            return f"mouth_{groups[0]}", score, "regex"
        if kind == "leg_axis":
            side = "l" if groups[0] == "l" else "r"
            index = int(groups[1])
            axis = str(groups[2]).lower()
            if axis == "z":
                joint = "leg" if index == 1 else ("knee" if index == 2 else "ankle")
                return f"{joint}_{side}", score, "regex"
            joint = "leg" if index == 1 else ("knee" if index == 2 else "foot")
            return f"{joint}_{side}_y", score, "regex"
        if kind == "leg_axis_x":
            side = "l" if groups[0] == "l" else "r"
            return f"leg_{side}_x", score, "regex"
        if kind == "foot_swing":
            return "foot_swing", score, "regex"
        if kind == "foot_axis":
            side = "l" if groups[0] == "l" else "r"
            axis = str(groups[1]).lower()
            return (f"foot_{side}" if axis == "x" else f"foot_{side}_y"), score, "regex"
        if kind == "skirt":
            return "skirt_x", score, "regex"
        if kind == "tail":
            return "tail", score, "regex"
        if kind == "chest":
            return f"chest_{groups[0]}", score, "regex"
        if kind == "hair":
            name = str(groups[0]).lower()
            if name == "braid":
                return "hair_side", score, "regex"
            return f"hair_{name}", score, "regex"
        if kind == "limb":
            part, side_raw, segment = groups
            side = "l" if side_raw == "l" else "r"
            if part == "arm" and segment == "b":
                return f"arm_{side}_bend", score, "regex"
            return f"{part}_{side}", score, "regex"
    return None


def match_channel(pid: str) -> tuple[str, int, str] | None:
    """Return ``(channel, score, how)`` or ``None``."""
    key = canonical(pid)
    if key in _ALIASES:
        channel, score = _ALIASES[key]
        # ``ParamAngleX`` is the standard spelling; prefer it over a bare
        # ``AngleX`` when a model exposes both.
        if str(pid).lower().startswith("param"):
            score += 5
        return channel, score, "alias"
    base = canonical_base(pid)
    if base in _ALIASES:
        channel, score = _ALIASES[base]
        # A duplicate/copy parameter is usable only if no standard name wins.
        return channel, max(30, score - 30), "suffix"
    found = _pattern_match(base)
    return found


# ── parameter kind ───────────────────────────────────
_UNIT_CHANNELS = {
    "mouth_open", "cheek", "breath", "eye_l_smile", "eye_r_smile",
    "eye_squint", "hand_style_grass", "hand_style_chest", "hand_style_point",
    "arm_gate", "hand_gate",
}
_EYE_CHANNELS = {"eye_l_open", "eye_r_open"}
_BIPOLAR_CHANNELS = {
    "head_x", "head_y", "head_z", "body_x", "body_y", "body_z",
    "eye_x", "eye_y", "brow_l_y", "brow_r_y", "brow_l_angle",
    "brow_r_angle", "brow_l_form", "brow_r_form", "mouth_form", "mouth_x",
    "arm_l", "arm_r", "arm_l_bend", "arm_r_bend", "hand_l", "hand_r",
    "finger_l", "finger_r", "arm_l_persp", "arm_r_persp",
    "shoulder_l", "shoulder_r", "leg_l", "leg_r", "leg_l_x", "leg_r_x",
    "knee_l", "knee_r", "ankle_l", "ankle_r", "leg_l_y", "leg_r_y",
    "knee_l_y", "knee_r_y", "foot_l_y", "foot_r_y",
    "foot_l", "foot_r", "foot_swing", "hip",
    "chest_x", "chest_y", "skirt_x", "skirt_y", "tail",
    "sleeve_l_x", "sleeve_l_y", "sleeve_r_x", "sleeve_r_y",
    "ribbon_l_x", "ribbon_l_y", "ribbon_r_x", "ribbon_r_y",
    "hair_front", "hair_side", "hair_back",
}


def kind_for_channel(channel: str, minimum: float, maximum: float,
                     default: float) -> str:
    if channel in _EYE_CHANNELS:
        return "eye"
    if channel in _UNIT_CHANNELS:
        return "unit"
    if channel in _BIPOLAR_CHANNELS:
        return "bipolar"
    if minimum >= 0.0 and maximum <= 1.0:
        return "unit"
    return "bipolar"


# ── introspection ────────────────────────────────────
def read_physics_links(model3_path: str | Path | None) -> list[dict]:
    """读取 physics3 的 ``输出 → 输入源`` 边，按 PhysicsSettings 顺序。

    一个 setting 可以有多个输入，但通常只有一个输出。返回的是作者声明，
    不做传递闭包；运行时真正消费的是 :func:`read_physics_graph` 的节点集合。
    """
    if not model3_path:
        return []
    path = Path(model3_path)
    try:
        model3 = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return []
    if not isinstance(model3, dict):
        return []
    refs = model3.get("FileReferences")
    rel = refs.get("Physics") if isinstance(refs, dict) else ""
    if not isinstance(rel, str) or not rel.strip():
        return []
    try:
        physics = json.loads((path.parent / rel).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return []
    if not isinstance(physics, dict):
        return []
    links: list[dict] = []
    for setting in physics.get("PhysicsSettings") or []:
        if not isinstance(setting, dict):
            continue
        inputs = []
        for item in setting.get("Input") or []:
            if not isinstance(item, dict):
                continue
            source = item.get("Source")
            pid = (str(source.get("Id") or "").strip()
                   if isinstance(source, dict) else "")
            if pid:
                inputs.append(pid)
        outputs = []
        for item in setting.get("Output") or []:
            if not isinstance(item, dict):
                continue
            destination = item.get("Destination")
            pid = (str(destination.get("Id") or "").strip()
                   if isinstance(destination, dict) else "")
            if pid:
                outputs.append(pid)
        for output in outputs:
            links.append({"output": output, "inputs": list(dict.fromkeys(inputs))})
    return links


def read_physics_graph(
        model3_path: str | Path | None) -> tuple[set[str], set[str]]:
    """读取 physics3 的 ``(inputs, outputs)`` 参数 ID 集合。

    ``inputs`` 是物理摆锤的驱动源；它们必须在 physics.evaluate 之前写入，
    否则桌面 Update 之后写入不会影响当帧物理。``outputs`` 是摆锤结果。
    """
    inputs: set[str] = set()
    outputs: set[str] = set()
    for link in read_physics_links(model3_path):
        outputs.add(str(link["output"]))
        inputs.update(str(pid) for pid in link["inputs"])
    return inputs, outputs


def read_display_names(model3_path: str | Path | None) -> dict[str, str]:
    """Read ``cdi3`` display names in model3 order.

    Game rips often expose controls as ``Param36`` / ``Param59``; the numeric
    ID says nothing, but the Chinese display name does.  Order matters because
    some names (``大臂透视``) are duplicated for the left and right arm.
    """
    if not model3_path:
        return {}
    path = Path(model3_path)
    try:
        model3 = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(model3, dict):
        return {}
    refs = model3.get("FileReferences")
    rel = refs.get("DisplayInfo") if isinstance(refs, dict) else ""
    if not isinstance(rel, str) or not rel.strip():
        return {}
    try:
        data = json.loads((path.parent / rel).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for row in data.get("Parameters") or []:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("Id") or "").strip()
        name = str(row.get("Name") or "").strip()
        if pid and name:
            out[pid] = name
    return out


def _display_key(name: str) -> str:
    return re.sub(r"[\s_\-:：·,，.。/\\()（）\[\]【】]+", "", str(name or "")).lower()


def _axis_stem(base: str) -> str:
    """``左袖子y1`` / ``胸部x1`` 的轴字符在数字后缀前。"""
    return re.sub(r"\d+$", "", base)


_DISPLAY_EXACT: dict[str, str] = {
    "r大臂z": "arm_r", "l大臂z": "arm_l",
    "r小臂z": "arm_r_bend", "l小臂z": "arm_l_bend",
    "r手腕z": "hand_r", "l手腕z": "hand_l",
    "r食指": "finger_r", "l食指": "finger_l",
    "狗尾草": "hand_style_grass",
    "抚胸": "hand_style_chest",
    "竖食指": "hand_style_point",
}


def read_display_channels(model3_path: str | Path | None) -> dict[str, str]:
    """Map generic numeric parameter IDs to semantic channels via cdi3 names."""
    names = read_display_names(model3_path)
    out: dict[str, str] = {}
    side = ""
    for pid, raw_name in names.items():
        key = _display_key(raw_name)
        base = key.lstrip("0123456789.")
        if str(pid).lower().startswith("clothes"):
            continue
        if key.startswith(("r", "右")):
            side = "r"
        elif key.startswith(("l", "左")):
            side = "l"
        channel = _DISPLAY_EXACT.get(base, "")
        if "大臂透视" in base and side:
            channel = f"arm_{side}_persp"
        elif base.startswith("leany") or base in ("leaninput", "前倾", "身体前倾"):
            channel = "lean_y"
        elif "双手开关" in base or "动画双手" in base:
            channel = "arm_gate"
        elif "竖手指" in base and "无缝" in base:
            channel = "hand_gate"
        elif side and any(word in base for word in ("大腿", "上腿")):
            channel = f"leg_{side}"
        elif side and any(word in base for word in ("小腿", "膝盖")):
            channel = f"knee_{side}"
        elif side and any(word in base for word in ("脚腕", "脚踝")):
            channel = f"ankle_{side}"
        elif side and any(word in base for word in ("脚尖", "脚趾")):
            channel = f"foot_{side}_y"
        elif side and any(word in base for word in ("脚掌", "脚")):
            channel = f"foot_{side}"
        elif base.startswith("胸x") or ("胸部" in base and _axis_stem(base).endswith("x")):
            channel = "chest_x"
        elif base.startswith("胸y") or ("胸部" in base and _axis_stem(base).endswith("y")):
            channel = "chest_y"
        elif base.startswith(("裙x", "裙子层级x")) or (
                ("裙子" in base or "裙摆" in base) and _axis_stem(base).endswith("x")):
            channel = "skirt_x"
        elif base.startswith(("裙y", "裙子层级y")) or (
                ("裙子" in base or "裙摆" in base) and _axis_stem(base).endswith("y")):
            channel = "skirt_y"
        elif "宽袖" in base or "袖子" in base:
            if side:
                channel = (f"sleeve_{side}_y"
                           if _axis_stem(base).endswith("y")
                           else f"sleeve_{side}_x")
        elif base.startswith(("左飘带x",)):
            channel = "ribbon_l_x"
        elif base.startswith(("左飘带y",)):
            channel = "ribbon_l_y"
        elif base.startswith(("右飘带x",)):
            channel = "ribbon_r_x"
        elif base.startswith(("右飘带y",)):
            channel = "ribbon_r_y"
        elif "尾巴" in base and not any(
                word in base for word in ("翻面", "消失", "显示", "隐藏",
                                          "装束", "切换")):
            channel = "tail"
        if channel:
            out[pid] = channel
    return out


_STATE_RULES: tuple[tuple[str, tuple[str, ...], str, str, float, str], ...] = (
    # (语义名, 名称关键词, 模式, 互斥组, 保持秒数, 说明)
    ("long_skirt", ("长短裙", "裙子切换", "skirt toggle"), "persistent", "", 0.0,
     "切换长裙/短裙"),
    ("chibi_mode", ("大小变", "q版", "chibi", "体型"), "persistent", "", 0.0,
     "切换Q版体型"),
    ("outfit_chest", ("胸口衣服", "外套", "outfit"), "persistent", "", 0.0,
     "切换胸口衣服"),
    ("tail_outfit", ("尾巴", "tail outfit", "tail toggle"), "persistent", "", 0.0,
     "切换尾巴装束"),
    ("hand_chest", ("抚胸", "手放胸口"), "timed", "hand_style", 3.5, "手放胸口"),
    ("hand_point", ("竖食指", "食指"), "timed", "hand_style", 3.0, "竖食指"),
    ("hand_grass", ("狗尾草", "草"), "timed", "hand_style", 4.5, "拿狗尾草"),
)


def read_display_states(model3_path: str | Path | None) -> dict[str, dict]:
    """从 cdi3 显示名发现换装/手型开关。

    只返回声明，不猜默认值；默认值在 introspect() 拿到参数范围后计算。
    """
    names = read_display_names(model3_path)
    tail_params = {pid for pid, channel in read_display_channels(model3_path).items()
                   if channel == "tail"}
    out: dict[str, dict] = {}
    for pid, raw_name in names.items():
        key = _display_key(raw_name)
        for name, cues, mode, group, hold, label in _STATE_RULES:
            if not any(cue in key for cue in cues):
                continue
            if name == "tail_outfit" and (pid in tail_params or "翻面" in key):
                continue
            if name in out:
                continue
            out[name] = {
                "param_id": str(pid),
                "aliases": (label,),
                "mode": mode,
                "mutex_group": group,
                "hold_seconds": hold,
                "toggle": name in ("long_skirt", "chibi_mode", "outfit_chest",
                                   "tail_outfit"),
            }
            break
    return out


def _tier(caps: dict[str, bool], by_channel: dict[str, ParamSpec] | None = None) -> str:
    by_channel = by_channel or {}
    full_limbs = (all(c in by_channel for c in ("arm_l", "arm_r"))
                  and all(c in by_channel for c in ("leg_l", "leg_r")))
    if (full_limbs and caps["head"] and caps["eyes"] and caps["mouth"]
            and caps["body"]):
        return "full"
    if caps["head"] and caps["eyes"] and caps["mouth"] and caps["body"] and caps["arms"]:
        return "upper"
    if caps["head"] and caps["eyes"] and caps["mouth"] and caps["body"]:
        return "core"
    if caps["head"] and (caps["eyes"] or caps["mouth"]):
        return "face"
    if caps["eyes"] or caps["mouth"]:
        return "minimal"
    return "none"


#: 这些通道即使只找到 physics 输出也允许显式驱动：某些拆包模型的肢体角度
#: 本身就是物理摆锤的输出参数，不驱动就完全没有手/腿。头发/衣服输出永远不碰。
LIMB_CHANNELS = frozenset({
    "arm_l", "arm_r", "arm_l_bend", "arm_r_bend", "hand_l", "hand_r",
    "finger_l", "finger_r", "arm_l_persp", "arm_r_persp",
    "shoulder_l", "shoulder_r", "leg_l", "leg_r", "leg_l_x", "leg_r_x",
    "knee_l", "knee_r", "ankle_l", "ankle_r", "leg_l_y", "leg_r_y",
    "knee_l_y", "knee_r_y", "foot_l_y", "foot_r_y",
    "foot_l", "foot_r", "foot_swing", "hip",
})
#: 头部/躯干/四肢角度即使被模型声明为 physics 输出，也允许显式驱动：不少
#: 拆包模型（例如 my-model）把 ParamAngle*/ParamBodyAngle* 整体交给
#: 物理摆锤，禁写就等于把整个上半身焊死。头发/衣服输出仍然绝不碰。
DRIVABLE_PHYSICS_CHANNELS = LIMB_CHANNELS | frozenset({
    "head_x", "head_y", "head_z", "body_x", "body_y", "body_z",
    "chest_x", "chest_y", "skirt_x", "skirt_y", "tail", "lean_y",
    "sleeve_l_x", "sleeve_l_y", "sleeve_r_x", "sleeve_r_y",
    "ribbon_l_x", "ribbon_l_y", "ribbon_r_x", "ribbon_r_y",
})
#: 这些次级通道的多个物理分身要一起写，否则只动一层裙子/胸/尾巴。
SECONDARY_PHYSICS_CHANNELS = frozenset({
    "chest_x", "chest_y", "skirt_x", "skirt_y", "tail", "lean_y",
    "sleeve_l_x", "sleeve_l_y", "sleeve_r_x", "sleeve_r_y",
    "ribbon_l_x", "ribbon_l_y", "ribbon_r_x", "ribbon_r_y",
})
#: 允许进入 followers 的物理输出。躯干的 X2/Y2 是模型做级联用的副本，
#: 不写会让一部分物理/变形链路停在默认值。
CASCADE_CHANNELS = SECONDARY_PHYSICS_CHANNELS | frozenset({
    "body_x", "body_y", "body_z",
})
#: 多节链（尾巴/裙子/胸/袖/飘带/前倾）允许更多 follower，让尾尖也参与。
CHAIN_CHANNELS = frozenset({
    "tail", "skirt_x", "skirt_y", "chest_x", "chest_y", "lean_y",
    "sleeve_l_x", "sleeve_l_y", "sleeve_r_x", "sleeve_r_y",
    "ribbon_l_x", "ribbon_l_y", "ribbon_r_x", "ribbon_r_y",
    "hair_front", "hair_side", "hair_back",
})
HAIR_CHANNELS = frozenset({"hair_front", "hair_side", "hair_back"})
FOLLOWER_MIN_SCORE = 65
FOLLOWER_LIMIT = 4
CHAIN_FOLLOWER_LIMIT = 8

_NUMERIC_TAIL = re.compile(r"\[(\d+)\]|(\d+)\s*$")
_LEG_SEGMENT = re.compile(r"leg[_\s-]?[lr](\d)", re.IGNORECASE)


def _follower_index(pid: str) -> int:
    """``[0]尾巴`` -> 0，``ChestY2`` -> 2，``ParamLegL3Z`` -> 3。"""
    text = str(pid)
    match = _LEG_SEGMENT.search(text)
    if match:
        return int(match.group(1))
    match = _NUMERIC_TAIL.search(text)
    if not match:
        return 1_000_000
    return int(match.group(1) or match.group(2))


_STATE_ALIASES: dict[str, tuple[str, ...]] = {
    "long_skirt": ("长裙", "穿长裙", "换长裙", "换裙子"),
    "chibi_mode": ("Q版", "变Q版", "缩小"),
    "outfit_chest": ("换衣服", "胸口衣服", "外套"),
    "tail_outfit": ("尾巴装束", "换尾巴"),
    "hand_chest": ("抚胸", "手放胸口", "拍胸口"),
    "hand_point": ("竖食指", "指"),
    "hand_grass": ("狗尾草", "拿狗尾草"),
}

#: 顶点探针的死区判定：25% 行程的位移不到满幅 12% 视为明显非线性。
DEAD_ZONE_Q = 0.12
DEAD_ZONE_GAMMA = 0.58
DEAD_ZONE_MIN_SPAN = 0.05
#: 同侧手臂配对时，两个主臂质心太近就不强行改左右。
SIDE_SWITCH_MARGIN = 0.05


def _probe_cx(probe: dict[str, dict] | None, pid: str) -> float | None:
    row = (probe or {}).get(str(pid))
    if not isinstance(row, dict) or not row.get("active_vertices"):
        return None
    try:
        return float(row.get("cx"))
    except (TypeError, ValueError):
        return None


def _resolve_probe_sides(specs: list[ParamSpec],
                         probe: dict[str, dict] | None) -> None:
    """用探针质心给 ``大臂透视`` 这类同名参数配左右。

    cdi3 里两个 ``大臂透视`` 可能没有 L/R 前缀；先找主臂 ``arm_l``/``arm_r``
    的质心，再把透视参数分给更近的一侧。主臂缺失时才退回 ``cx`` 正负。
    """
    if not probe:
        return
    arms: dict[str, float] = {}
    for side in ("l", "r"):
        candidates = [s for s in specs if s.channel == f"arm_{side}"]
        if not candidates:
            continue
        candidates.sort(key=lambda s: (-s.score, len(s.id), s.id))
        cx = _probe_cx(probe, candidates[0].id)
        if cx is not None:
            arms[side] = cx
    if not arms:
        return
    for index, spec in enumerate(specs):
        if spec.channel not in ("arm_l_persp", "arm_r_persp"):
            continue
        cx = _probe_cx(probe, spec.id)
        if cx is None or abs(cx) < SIDE_SWITCH_MARGIN:
            continue
        if len(arms) == 2 and abs(abs(cx - arms["l"]) - abs(cx - arms["r"])) < SIDE_SWITCH_MARGIN:
            continue
        if len(arms) == 2:
            side = "r" if abs(cx - arms["r"]) < abs(cx - arms["l"]) else "l"
        else:
            side = "r" if cx > 0 else "l"
        target = f"arm_{side}_persp"
        if target != spec.channel:
            specs[index] = replace(spec, channel=target)


def _gamma_for(probe: dict[str, dict] | None, pid: str,
               kind: str) -> float:
    row = (probe or {}).get(str(pid))
    if not isinstance(row, dict) or not row.get("active_vertices"):
        return 1.0
    try:
        ratio = float(row.get("q_ratio", 1.0))
        span = float(row.get("span_ratio", 0.0))
    except (TypeError, ValueError):
        return 1.0
    if kind != "bipolar" or ratio >= DEAD_ZONE_Q or span < DEAD_ZONE_MIN_SPAN:
        return 1.0
    return DEAD_ZONE_GAMMA


def _probe_cy(probe: dict[str, dict] | None, pid: str) -> float | None:
    row = (probe or {}).get(str(pid))
    if not isinstance(row, dict) or not row.get("active_vertices"):
        return None
    try:
        return float(row.get("cy"))
    except (TypeError, ValueError):
        return None


def _reorder_leg_chain(specs: list[ParamSpec],
                       probe: dict[str, dict] | None,
                       warnings: list[str]) -> None:
    """用质心高度校验 ``leg/knee/ankle`` 的上下顺序。

    命名可能来自不同语言的直译；髋关节的质点必然高于膝，膝高于踝。只有三个
    关节都单独绑定且高度差足够明显时才重排，避免把同一条腿的两个候选参数
    当成关节链。
    """
    if not probe:
        return
    for side in ("l", "r"):
        channels = [f"leg_{side}", f"knee_{side}", f"ankle_{side}"]
        chosen: list[ParamSpec] = []
        for channel in channels:
            candidates = [spec for spec in specs if spec.channel == channel]
            if len(candidates) != 1:
                break
            chosen.append(candidates[0])
        else:
            heights = []
            for spec in chosen:
                cy = _probe_cy(probe, spec.id)
                if cy is None:
                    break
                heights.append(cy)
            else:
                ordered = sorted(chosen, key=lambda spec: -float(
                    _probe_cy(probe, spec.id) or 0.0))
                if [spec.channel for spec in ordered] == channels:
                    continue
                if max(heights) - min(heights) < 0.06:
                    continue
                mapping = dict(zip(channels, ordered))
                for channel, spec in mapping.items():
                    kind = kind_for_channel(
                        channel, spec.minimum, spec.maximum, spec.default)
                    specs[specs.index(spec)] = replace(
                        spec, channel=channel, kind=kind,
                        gamma=_gamma_for(probe, spec.id, kind),
                        confidence=min(1.0, spec.confidence + 0.05),
                        evidence=tuple(dict.fromkeys(
                            (*spec.evidence, "probe-chain"))),
                    )
                warnings.append(
                    f"{side} 腿关节按探针高度重排："
                    + ", ".join(f"{spec.id}->{channel}"
                                for channel, spec in mapping.items()))


def _probe_screen_sides(probe: dict[str, dict] | None,
                        ranges: dict[str, tuple[float, float, float]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pid, row in (probe or {}).items():
        if pid not in ranges or not isinstance(row, dict) or not row.get("active_vertices"):
            continue
        side = str(row.get("screen_side") or "")
        if side in ("left", "right", "center"):
            out[str(pid)] = side
    return out


def _probe_pointer_origin(probe: dict[str, dict] | None,
                          by_channel: dict[str, ParamSpec]
                          ) -> tuple[float, float] | None:
    """用眼部/头部通道的探针质心估计鼠标注视原点。"""
    for channel in ("eye_x", "eye_l_open", "head_x", "head_y"):
        spec = by_channel.get(channel)
        if spec is None:
            continue
        row = (probe or {}).get(spec.id)
        if not isinstance(row, dict) or not row.get("active_vertices"):
            continue
        try:
            cx = float(row.get("cx", 0.0))
            cy = float(row.get("cy", 0.0))
        except (TypeError, ValueError):
            continue
        if -1.5 <= cx <= 1.5 and -0.2 <= cy <= 1.2:
            return cx, cy
    return None


#: 这些通道带左右语义，探针质心可以校验命名是否与画面一致。
_SIDED_PREFIXES = ("arm_", "leg_", "knee_", "ankle_", "foot_", "hand_",
                   "shoulder_", "finger_")
EVIDENCE_CONFIDENCE_FLOOR = 0.55


def _expected_side(channel: str) -> str:
    if not str(channel).startswith(_SIDED_PREFIXES):
        return ""
    match = re.search(r"(?:^|_)([lr])(?:_|$)", str(channel))
    if match is None:
        return ""
    return "left" if match.group(1) == "l" else "right"


def _vtube_row(vtube: VTubeEvidence | None, pid: str, channel: str):
    if vtube is None:
        return None
    for row in vtube.by_output().get(str(pid), ()):
        if row.channel == channel:
            return row
    return None


def _vtube_candidate(vtube: VTubeEvidence | None, pid: str):
    if vtube is None:
        return None
    rows = vtube.by_output().get(str(pid), ())
    for row in rows:
        if row.channel:
            return row
    return rows[0] if rows else None


def _motion_span(activity: dict[str, dict] | None, pid: str) -> float:
    row = (activity or {}).get(str(pid))
    if not isinstance(row, dict):
        return 0.0
    try:
        return max(0.0, float(row.get("max", 0.0)) - float(row.get("min", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _evidence(pid: str, display: str, lexical: tuple[str, int, str] | None,
              vtube: VTubeEvidence | None,
              activity: dict[str, dict] | None,
              probe: dict[str, dict] | None,
              warnings: list[str]) -> tuple[str, int, float, tuple[str, ...]] | None:
    """Fuse source evidence for one parameter.

    A lexical name wins over a VTube input hint because the output parameter is
    more specific: VTube reuses ``FaceAngleX`` for both the head and the body
    copies, while the output name distinguishes ``AngleX4`` from ``AngleX``.
    """
    chosen = ""
    score = 0
    confidence = 0.0
    sources: list[str] = []

    if display in CHANNEL_RANGES:
        chosen = display
        score += 130
        confidence = max(confidence, 0.85)
        sources.append("cdi3")
    if lexical is not None:
        channel, lexical_score, _how = lexical
        if chosen and channel != chosen:
            warnings.append(
                f"{pid}: cdi3 指向 {chosen}，参数名指向 {channel}，保留 cdi3")
        elif not chosen:
            chosen = channel
        if channel == chosen:
            # 词法分保留参数之间的排序差异，但不再作为唯一事实。
            score += 100 + max(0, lexical_score - 60) * 0.25
            confidence = max(confidence, 0.80)
            sources.append("name")

    candidate = _vtube_candidate(vtube, pid)
    if not chosen and candidate is not None and candidate.channel:
        chosen = candidate.channel
        score += 135
        confidence = max(confidence, 0.92)
        sources.append("vtube")
    vtube_row = _vtube_row(vtube, pid, chosen) if chosen else None
    if vtube_row is not None and "vtube" not in sources:
        score += 135
        confidence = max(confidence, 0.92)
        sources.append("vtube")

    if not chosen:
        # 没有语言/显示名证据时不靠纯共变猜语义：相关不等于同义。
        return None

    if _motion_span(activity, pid) > 1e-6:
        score += 20
        confidence = min(1.0, confidence + 0.04)
        sources.append("motion")

    row = (probe or {}).get(str(pid))
    if isinstance(row, dict) and row.get("active_vertices"):
        try:
            span = float(row.get("span_ratio", 0.0))
        except (TypeError, ValueError):
            span = 0.0
        if span >= 0.02:
            score += 15
            confidence = min(1.0, confidence + 0.05)
            sources.append("probe")
        expected = _expected_side(chosen)
        actual = str(row.get("screen_side") or "")
        if (expected and actual in ("left", "right") and actual != expected
                and not str(chosen).endswith("_persp")):
            score -= 50
            # 证据互相矛盾时不能仍以 0.80 通过 0.55 阈值：扣到阈值以下。
            confidence = max(0.0, confidence - 0.40)
            sources.append("probe-side-conflict")

    if confidence < EVIDENCE_CONFIDENCE_FLOOR:
        warnings.append(
            f"{pid}: 多源证据置信度 {confidence:.2f} 低于阈值，未绑定")
        return None
    return chosen, score, confidence, tuple(sources)


def introspect(source: ParamSource, *, model_name: str = "",
               physics_outputs: set[str] | None = None,
               physics_inputs: set[str] | None = None,
               display_channels: dict[str, str] | None = None,
               states: dict[str, dict] | None = None,
               probe: dict[str, dict] | None = None,
               vtube: VTubeEvidence | None = None,
               motion_activity: dict[str, dict] | None = None) -> ModelProfile:
    """Build a :class:`ModelProfile` from any parameter source.

    Physics outputs are excluded from normal binding so the engine cannot
    overwrite hair/cloth simulation after ``model.Update()``.  The exception is
    a limb channel whose only candidates are physics outputs (common in game
    rips): driving the limb angle is then the only way to move the arm/leg, so
    it is bound with an explicit warning.

    ``probe`` 是 ``sidekick.motion.probe.probe_model`` 的几何结果：有它时用质心
    校正同名左右参数，并给前段死区参数设置 ``gamma``。
    """
    physics = {str(x) for x in (physics_outputs or set())}
    physics_in = {str(x) for x in (physics_inputs or set())}
    specs: list[ParamSpec] = []
    unmapped: list[str] = []
    warnings: list[str] = []
    ranges: dict[str, tuple[float, float, float]] = {}
    seen_ids = set(source.param_ids())

    for pid in sorted(seen_ids):
        try:
            minimum, maximum, default = source.param_info(pid)
        except Exception:
            warnings.append(f"参数 {pid} 读取范围失败，已跳过")
            continue
        ranges[pid] = (float(minimum), float(maximum), float(default))
        display = (display_channels or {}).get(pid, "")
        lexical = match_channel(pid)
        evidence = _evidence(pid, display, lexical, vtube, motion_activity,
                             probe, warnings)
        if evidence is None:
            unmapped.append(pid)
            continue
        channel, score, confidence, sources = evidence
        kind = kind_for_channel(channel, float(minimum), float(maximum), float(default))
        vtube_row = _vtube_row(vtube, pid, channel)
        effective = vtube_row.effective_range if vtube_row is not None else None
        specs.append(ParamSpec(
            id=pid, channel=channel, minimum=minimum, maximum=maximum,
            default=default, kind=kind, score=score,
            match=f"{sources[-1] if sources else 'evidence'}:{pid}",
            gamma=_gamma_for(probe, pid, kind),
            polarity=-1.0 if vtube_row is not None and vtube_row.inverted else 1.0,
            effective_min=effective[0] if effective else None,
            effective_max=effective[1] if effective else None,
            confidence=confidence, evidence=sources,
        ))

    # live2d 的 GetParamIds() 偶尔返回带尾随空格的重复 ID；同一 ID 只留第一条。
    unique: dict[str, ParamSpec] = {}
    for spec in specs:
        unique.setdefault(spec.id, spec)
    specs = list(unique.values())
    _resolve_probe_sides(specs, probe)
    _reorder_leg_chain(specs, probe, warnings)
    if vtube is not None:
        warnings.append(
            f".vtube.json: {len(vtube.params)} 条参数证据，"
            f"{len(vtube.hotkeys)} 个热键")
    by_channel: dict[str, ParamSpec] = {}
    followers: dict[str, list[tuple[str, float]]] = {}
    alternatives: dict[str, list[ParamSpec]] = {}
    for spec in specs:
        alternatives.setdefault(spec.channel, []).append(spec)

    for channel, candidates in alternatives.items():
        normal = [s for s in candidates if s.id not in physics]
        physical = [s for s in candidates if s.id in physics]
        if channel in DRIVABLE_PHYSICS_CHANNELS:
            allowed = list(candidates)
        elif channel in HAIR_CHANNELS:
            allowed = normal
        else:
            allowed = normal
        if not allowed:
            if physical:
                warnings.append(
                    f"{channel}: 候选 {', '.join(s.id for s in physical[:3])} 是物理输出，已跳过")
            continue
        # Prefer the highest score, then chain order (``[0]尾巴`` before
        # ``[5]尾巴``), then the shortest / most standard name.
        if channel == "lean_y":
            # LeanInput 是物理输入本身；写它让物理算出 LeanY 链，
            # 不能再把同一语义写到物理输出上叠一遍。
            allowed.sort(key=lambda s: (
                0 if (s.id in physics_in and s.id not in physics) else 1,
                -s.score, _follower_index(s.id), len(s.id), s.id))
        else:
            allowed.sort(key=lambda s: (-s.score, _follower_index(s.id),
                                        len(s.id), s.id))
        winner = allowed[0]
        by_channel[channel] = winner
        limit = (CHAIN_FOLLOWER_LIMIT if channel in CHAIN_CHANNELS
                 else FOLLOWER_LIMIT)
        rows: list[tuple[str, float]] = []
        for index, spec in enumerate(allowed[1:], start=1):
            if spec.id in physics and channel not in CASCADE_CHANNELS:
                continue
            if spec.score < FOLLOWER_MIN_SCORE or len(rows) >= limit:
                continue
            if channel == "lean_y":
                # LeanY1/2/3 是同一插值链，必须同幅驱动才能看出前倾。
                scale = 1.0
            elif channel in CHAIN_CHANNELS:
                # 尾尖/裙摆随节数略微衰减，保留根部主导，避免尾尖甩飞。
                scale = max(0.55, 0.92 - 0.04 * (index - 1))
            else:
                scale = 0.80 if _TRAILING_DIGITS.search(canonical(spec.id)) else 0.85
            rows.append((spec.id, scale))
        if rows:
            followers[channel] = rows
            warnings.append(
                f"{channel}: 主参数 {winner.id}，同步驱动 {', '.join(pid for pid, _ in rows)}")
        elif len(candidates) > 1:
            others = ", ".join(s.id for s in candidates[1:4])
            if len(candidates) > 4:
                others += f" 等 {len(candidates) - 1} 个"
            warnings.append(f"{channel}: 使用 {winner.id}，忽略重复候选 {others}")

    conflicts = [
        spec.id for spec in by_channel.values()
        if "probe-side-conflict" in spec.evidence
    ]
    if conflicts:
        warnings.append(
            "主参数方位与左右命名相反（可能是镜像命名）："
            + ", ".join(conflicts[:6]))
    caps = {
        "head": any(c in by_channel for c in ("head_x", "head_y", "head_z")),
        "eyes": any(c in by_channel for c in ("eye_l_open", "eye_r_open")),
        "mouth": any(c in by_channel for c in ("mouth_open", "mouth_form")),
        "body": any(c in by_channel for c in ("body_x", "body_y", "body_z")),
        "arms": any(c in by_channel for c in (
            "arm_l", "arm_r", "arm_l_bend", "arm_r_bend", "hand_l", "hand_r")),
        "legs": any(c in by_channel for c in ("leg_l", "leg_r", "hip")),
        "brows": any(c in by_channel for c in (
            "brow_l_y", "brow_r_y", "brow_l_angle", "brow_r_angle")),
        "hands": any(c in by_channel for c in ("hand_l", "hand_r")),
        "shoulders": any(c in by_channel for c in ("shoulder_l", "shoulder_r")),
        "hair": any(c in by_channel for c in ("hair_front", "hair_side", "hair_back")),
        "blink": all(c in by_channel for c in ("eye_l_open", "eye_r_open")),
        "lip_sync": "mouth_open" in by_channel,
        "gaze": "eye_x" in by_channel or "eye_y" in by_channel,
        "can_wave": "arm_r" in by_channel or "body_z" in by_channel,
        "can_kick": "leg_r" in by_channel or "body_x" in by_channel,
        "can_lean": "lean_y" in by_channel or "body_y" in by_channel,
        "can_lean_y": "lean_y" in by_channel,
        "can_tail": "tail" in by_channel,
        "can_sway_hip": "hip" in by_channel or "body_z" in by_channel,
        "can_nod": "head_y" in by_channel or "body_x" in by_channel,
    }
    if not caps["head"]:
        warnings.append("没有找到头部角度参数；头部动作将退化到身体或直接省略")
    if not caps["eyes"]:
        warnings.append("没有找到眼睛开合参数；眨眼将禁用")
    if not caps["mouth"]:
        warnings.append("没有找到嘴部参数；口型同步将禁用")
    if not caps["body"]:
        warnings.append("没有找到身体参数；身体动作将补偿到头部")

    state_specs: dict[str, StateSpec] = {}
    for name, row in (states or {}).items():
        if not isinstance(row, dict):
            continue
        pid = str(row.get("param_id") or "")
        if pid not in ranges:
            continue
        minimum, maximum, default = ranges[pid]
        if bool(row.get("toggle")):
            # 0/1 换装开关的“另一边”是相对当前默认值而言的。
            active = maximum if default <= minimum else minimum
            inactive = default
        else:
            active = maximum
            inactive = default
        aliases = tuple(_STATE_ALIASES.get(name) or row.get("aliases") or ())
        state_specs[name] = StateSpec(
            name=name, param_id=pid, aliases=aliases,
            active_value=float(active), inactive_value=float(inactive),
            mode=str(row.get("mode") or "persistent"),
            hold_seconds=float(row.get("hold_seconds") or 0.0),
            mutex_group=str(row.get("mutex_group") or ""),
        )
    if state_specs:
        warnings.append("发现离散状态：" + ", ".join(sorted(state_specs)))
    tuned = sorted(
        (spec for spec in specs if spec.gamma != 1.0),
        key=lambda spec: spec.id,
    )
    if tuned:
        warnings.append(
            "几何探针为 " + ", ".join(spec.id for spec in tuned[:6])
            + f" 设置 gamma={DEAD_ZONE_GAMMA}（前段死区）")

    return ModelProfile(
        params={spec.id: spec for spec in specs},
        by_channel=by_channel,
        tier=_tier(caps, by_channel),
        capabilities=caps,
        unmapped=unmapped,
        warnings=warnings,
        model_name=model_name,
        ranges=ranges,
        followers=followers,
        physics_outputs=physics,
        physics_inputs=physics_in,
        states=state_specs,
        screen_side=_probe_screen_sides(probe, ranges),
        pointer_origin=_probe_pointer_origin(probe, by_channel),
    )


def physics_additive_specs(profile: ModelProfile) -> dict[str, ParamSpec]:
    """次级物理输出叶结点：写入时用物理当前值 + 引擎偏移，不直接覆盖。"""
    out: dict[str, ParamSpec] = {}
    for channel in SECONDARY_PHYSICS_CHANNELS:
        spec = profile.by_channel.get(channel)
        if spec is not None and spec.id in profile.physics_outputs:
            out[spec.id] = spec
        if (spec is not None and spec.id in profile.physics_inputs
                and spec.id not in profile.physics_outputs):
            # 主参数已经驱动整条物理链，不能再对物理输出 followers 叠一次。
            continue
        for pid, _scale in profile.followers.get(channel, ()):
            fspec = profile.params.get(pid)
            if fspec is not None and pid in profile.physics_outputs:
                out[pid] = fspec
    return out


# ── degradation ──────────────────────────────────────
def compensate_pose(pose: dict[str, float], profile: ModelProfile) -> dict[str, float]:
    """Move channels the model does not have onto channels it does.

    This is a second line of defence after per-action degradation: a custom
    action or an emotion preset may mention an arm the model never had.
    """
    out = dict(pose)

    def take(channel: str) -> float:
        value = float(out.pop(channel, 0.0))
        return value

    def add(channel: str, value: float) -> None:
        if value and channel in CHANNEL_RANGES:
            out[channel] = out.get(channel, 0.0) + value

    # 手指缺失：折进小臂；小臂也缺失时再折进大臂。
    for side in ("l", "r"):
        if profile.supports(f"hand_{side}"):
            continue
        hand = take(f"hand_{side}")
        if hand:
            target = (f"arm_{side}_bend" if profile.supports(f"arm_{side}_bend")
                      else f"arm_{side}")
            add(target, hand * 0.75)

    arm_l = 0.0
    arm_r = 0.0
    for side in ("l", "r"):
        arm = take(f"arm_{side}") if not profile.supports(f"arm_{side}") else 0.0
        bend = (take(f"arm_{side}_bend")
                if not profile.supports(f"arm_{side}_bend") else 0.0)
        limb = arm + bend * 0.75
        if side == "l":
            arm_l = limb
        else:
            arm_r = limb

    # 无手臂模型：左右不对称驱动躯干侧倾，双臂同抬驱动拔高/前倾。
    if arm_l or arm_r:
        asym = arm_r - arm_l
        sym = 0.5 * (arm_r + arm_l)
        add("body_z", asym * 0.46)
        add("head_z", asym * 0.34)
        add("body_x", asym * 0.30)
        add("body_y", sym * 0.44)
        add("head_y", sym * 0.24)

    shoulder_l = take("shoulder_l") if not profile.supports("shoulder_l") else 0.0
    shoulder_r = take("shoulder_r") if not profile.supports("shoulder_r") else 0.0
    if shoulder_l or shoulder_r:
        add("body_y", 0.5 * (shoulder_l + shoulder_r) * 0.42)
        add("body_z", (shoulder_r - shoulder_l) * 0.28)
        add("head_z", (shoulder_r - shoulder_l) * 0.16)

    leg_l = take("leg_l") if not profile.supports("leg_l") else 0.0
    leg_r = take("leg_r") if not profile.supports("leg_r") else 0.0
    hip = take("hip") if not profile.supports("hip") else 0.0
    if leg_l or leg_r or hip:
        add("body_x", (leg_r - leg_l) * 0.36 + hip * 0.34)
        add("body_z", (leg_r - leg_l) * 0.28 + hip * 0.28)
        add("body_y", 0.5 * (leg_l + leg_r) * 0.28)
    for side in ("l", "r"):
        leg_y = take(f"leg_{side}_y") if not profile.supports(f"leg_{side}_y") else 0.0
        leg_x = take(f"leg_{side}_x") if not profile.supports(f"leg_{side}_x") else 0.0
        knee = take(f"knee_{side}") if not profile.supports(f"knee_{side}") else 0.0
        knee_y = (take(f"knee_{side}_y")
                  if not profile.supports(f"knee_{side}_y") else 0.0)
        ankle = take(f"ankle_{side}") if not profile.supports(f"ankle_{side}") else 0.0
        foot = take(f"foot_{side}") if not profile.supports(f"foot_{side}") else 0.0
        foot_y = (take(f"foot_{side}_y")
                  if not profile.supports(f"foot_{side}_y") else 0.0)
        if leg_x:
            add(f"leg_{side}", leg_x * 0.45)
        if leg_y:
            add(f"leg_{side}", leg_y * 0.45)
        if knee:
            add(f"leg_{side}", knee * 0.55)
            add(f"leg_{side}_y", knee * 0.20)
        if knee_y:
            add(f"leg_{side}_y", knee_y * 0.40)
        if ankle:
            add(f"leg_{side}", ankle * 0.35)
            add(f"foot_{side}", ankle * 0.30)
        if foot_y:
            add(f"foot_{side}", foot_y * 0.40)
            add(f"leg_{side}_y", foot_y * 0.20)
        if foot:
            add(f"leg_{side}", foot * 0.30)
            add(f"leg_{side}_y", foot * 0.20)
    swing = take("foot_swing") if not profile.supports("foot_swing") else 0.0
    if swing:
        add("foot_l", swing * 0.45)
        add("foot_r", swing * 0.45)

    # 手指/大臂透视缺失：折进手/大臂，避免动作幅度凭空消失。
    for side in ("l", "r"):
        if not profile.supports(f"finger_{side}"):
            value = take(f"finger_{side}")
            target = (f"hand_{side}" if profile.supports(f"hand_{side}")
                      else (f"arm_{side}_bend" if profile.supports(f"arm_{side}_bend")
                            else f"arm_{side}"))
            add(target, value * 0.70)
        if not profile.supports(f"arm_{side}_persp"):
            add(f"arm_{side}", take(f"arm_{side}_persp") * 0.40)

    # 次级变形器缺失：只把一小部分折回躯干，避免重复放大。
    if not profile.supports("chest_x"):
        add("body_x", take("chest_x") * 0.20)
    if not profile.supports("chest_y"):
        add("body_y", take("chest_y") * 0.20)
    if not profile.supports("skirt_x"):
        add("body_z", take("skirt_x") * 0.25)
    if not profile.supports("skirt_y"):
        add("body_y", take("skirt_y") * 0.20)
    if not profile.supports("lean_y"):
        add("body_y", take("lean_y") * 0.55)
    if not profile.supports("tail"):
        add("hip", take("tail") * 0.30)
    if not profile.supports("sleeve_l_x"):
        add("body_x", take("sleeve_l_x") * 0.15)
    if not profile.supports("sleeve_r_x"):
        add("body_x", take("sleeve_r_x") * 0.15)
    if not profile.supports("sleeve_l_y"):
        add("body_y", take("sleeve_l_y") * 0.15)
    if not profile.supports("sleeve_r_y"):
        add("body_y", take("sleeve_r_y") * 0.15)
    for side in ("l", "r"):
        if not profile.supports(f"ribbon_{side}_x"):
            add("body_z", take(f"ribbon_{side}_x") * 0.12)
        if not profile.supports(f"ribbon_{side}_y"):
            add("body_y", take(f"ribbon_{side}_y") * 0.12)

    if not profile.supports("body_x") and profile.supports("head_x"):
        add("head_x", take("body_x") * 0.55)
    if not profile.supports("body_y") and profile.supports("head_y"):
        add("head_y", take("body_y") * 0.55)
    if not profile.supports("body_z") and profile.supports("head_z"):
        add("head_z", take("body_z") * 0.55)

    if not profile.supports("head_x") and profile.supports("body_x"):
        add("body_x", take("head_x") * 0.65)
    if not profile.supports("head_y") and profile.supports("body_y"):
        add("body_y", take("head_y") * 0.65)
    if not profile.supports("head_z") and profile.supports("body_z"):
        add("body_z", take("head_z") * 0.65)

    return {k: clamp(v, *CHANNEL_RANGES.get(k, (-1.0, 1.0)))
            for k, v in out.items() if abs(v) > 1e-9}
