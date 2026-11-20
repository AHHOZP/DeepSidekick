"""VTube Studio `.vtube.json` evidence: parameter intent, polarity and hotkeys.

A `.vtube.json` is written by the artist or rigger who wired the model in VTube
Studio.  Unlike a raw ``Param36`` name it states which *input* drives an output
parameter, the output range the rigger actually works in, and which expression
or animation the author expects a viewer to trigger.  That is model-authored
evidence, not a guess.

The reader is intentionally read-only and tolerant: missing, malformed or
foreign files return ``None``.  The caller decides whether to warn.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sidekick.avatar.scan import read_json
from .types import CHANNEL_RANGES, clamp

#: VTube Studio's input vocabulary is stable across models; the output parameter
#: names are not.  This is the only place where an input name becomes a channel.
_INPUT_CHANNELS: dict[str, str] = {
    "FaceAngleX": "head_x", "FaceAngleY": "head_y", "FaceAngleZ": "head_z",
    "BodyAngleX": "body_x", "BodyAngleY": "body_y", "BodyAngleZ": "body_z",
    "FacePositionX": "body_x", "FacePositionY": "body_y",
    "FacePositionZ": "body_z",
    "EyeOpenLeft": "eye_l_open", "EyeOpenRight": "eye_r_open",
    "EyeLeftX": "eye_x", "EyeRightX": "eye_x", "EyeX": "eye_x",
    "EyeLeftY": "eye_y", "EyeRightY": "eye_y", "EyeY": "eye_y",
    "EyeSmileLeft": "eye_l_smile", "EyeSmileRight": "eye_r_smile",
    "MouthSmile": "mouth_form",
    "MouthOpen": "mouth_open", "VoiceVolume": "mouth_open",
    "JawOpen": "mouth_open",
    "BrowLeftY": "brow_l_y", "BrowRightY": "brow_r_y", "Brows": "brow_l_y",
    "CheekPuff": "cheek", "Cheek": "cheek",
    "HandLeft": "hand_l", "HandRight": "hand_r",
    "ArmLeft": "arm_l", "ArmRight": "arm_r",
}

#: Hotkey actions we can expose as assets.  Other VTube actions are UI-only.
_HOTKEY_ACTIONS = frozenset({
    "ToggleExpression", "TriggerAnimation", "ChangeIdleAnimation",
    "RemoveAllExpressions",
})


@dataclass(frozen=True)
class VTubeParam:
    """One ``ParameterSettings`` row that targets a Live2D parameter."""

    output: str
    input: str
    channel: str
    inverted: bool
    output_low: float
    output_high: float
    smoothing: float
    breathing: bool
    file: str

    @property
    def effective_range(self) -> tuple[float, float] | None:
        """The output span the author actually works in, if it is meaningful."""
        low, high = sorted((float(self.output_low), float(self.output_high)))
        if high - low <= 1e-6:
            return None
        return low, high


@dataclass(frozen=True)
class VTubeHotkey:
    name: str
    action: str
    file: str
    folder: str


@dataclass(frozen=True)
class VTubeEvidence:
    file: str
    params: tuple[VTubeParam, ...]
    hotkeys: tuple[VTubeHotkey, ...]
    idle: str = ""
    idle_tracking_lost: str = ""

    def by_output(self) -> dict[str, tuple[VTubeParam, ...]]:
        grouped: dict[str, list[VTubeParam]] = {}
        for row in self.params:
            grouped.setdefault(row.output, []).append(row)
        return {key: tuple(rows) for key, rows in grouped.items()}

    def channels(self) -> dict[str, str]:
        """One best-effort semantic channel per output parameter."""
        out: dict[str, str] = {}
        for output, rows in self.by_output().items():
            for row in rows:
                if row.channel:
                    out[output] = row.channel
                    break
        return out


def _number(value, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number == number and abs(number) != float("inf") else fallback


def _model_ref(data: dict) -> str:
    refs = data.get("FileReferences")
    if not isinstance(refs, dict):
        return ""
    return str(refs.get("Model") or "").replace("\\", "/")


def _pick_file(root: Path, entry: str) -> Path | None:
    files = sorted(root.rglob("*.vtube.json"))
    if not files:
        return None
    want = Path(str(entry or "")).name.casefold()
    for path in files:
        data = read_json(path)
        if want and Path(_model_ref(data)).name.casefold() == want:
            return path
    return files[0]


def _output_channel(output: str, input_name: str) -> str:
    from .introspect import match_channel

    found = match_channel(output)
    if found is not None and found[0] in CHANNEL_RANGES:
        return found[0]
    return _INPUT_CHANNELS.get(str(input_name or "").strip(), "")


def parse_vtube_config(model3_path: str | Path | None) -> VTubeEvidence | None:
    """Read the `.vtube.json` next to *model3_path*.

    Returns ``None`` when there is no file, no readable JSON, or no usable
    parameter/hotkey evidence.  A file copied from another model is still
    parsed; per-parameter filtering happens against the live model later.
    """
    if not model3_path:
        return None
    model3 = Path(model3_path)
    root = model3.parent
    if not root.is_dir():
        return None
    path = _pick_file(root, model3.name)
    if path is None:
        return None
    data = read_json(path)
    if not data:
        return None
    rel_name = path.name
    rows: list[VTubeParam] = []
    for item in data.get("ParameterSettings") or []:
        if not isinstance(item, dict):
            continue
        output = str(item.get("OutputLive2D") or "").strip()
        if not output:
            continue
        input_name = str(item.get("Input") or "").strip()
        # 不认识的 Input/Output 也保留为“作者声明过但没语义映射”的证据；
        # 审计要能解释 40 条里哪些用上了，而不是静默丢掉 14 条。
        channel = _output_channel(output, input_name)
        out_low = _number(item.get("OutputRangeLower"))
        out_high = _number(item.get("OutputRangeUpper"))
        in_low = _number(item.get("InputRangeLower"))
        in_high = _number(item.get("InputRangeUpper"))
        # Either side can be authored in the opposite direction.  The two
        # reversals cancel, which is why both are compared.
        out_reversed = out_high < out_low
        in_reversed = in_high < in_low
        inverted = bool(
            (out_reversed or in_reversed) and not (out_reversed and in_reversed))
        rows.append(VTubeParam(
            output=output,
            input=input_name,
            channel=channel,
            inverted=inverted,
            output_low=out_low,
            output_high=out_high,
            smoothing=clamp(_number(item.get("Smoothing")), 0.0, 100.0),
            breathing=bool(item.get("UseBreathing")),
            file=rel_name,
        ))
    hotkeys: list[VTubeHotkey] = []
    for item in data.get("Hotkeys") or []:
        if not isinstance(item, dict):
            continue
        action = str(item.get("Action") or "")
        name = str(item.get("Name") or "").strip()
        file = str(item.get("File") or "").replace("\\", "/").strip()
        if action not in _HOTKEY_ACTIONS or not name:
            continue
        hotkeys.append(VTubeHotkey(
            name=name, action=action, file=file,
            folder=str(item.get("Folder") or "").strip(),
        ))
    refs = data.get("FileReferences")
    refs = refs if isinstance(refs, dict) else {}
    idle = str(refs.get("IdleAnimation") or "").replace("\\", "/")
    lost = str(refs.get("IdleAnimationWhenTrackingLost") or "").replace("\\", "/")
    if not rows and not hotkeys and not idle:
        return None
    return VTubeEvidence(
        file=rel_name,
        params=tuple(rows),
        hotkeys=tuple(hotkeys),
        idle=idle,
        idle_tracking_lost=lost,
    )
