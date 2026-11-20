"""桌宠窗口的本地状态：透明度、置顶、左键穿透、鼠标跟随、缩放、位置。"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULTS = {
    "opacity": 1.0,
    "topmost": True,
    "click_through": False,
    "follow_mouse": True,
    "scale": 1.0,
    "x": None,
    "y": None,
}


def load(path: str | Path | None) -> dict:
    out = dict(DEFAULTS)
    if not path:
        return out
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return out
    if not isinstance(data, dict):
        return out
    for key in DEFAULTS:
        if key in data:
            out[key] = data[key]
    return out


def save(path: str | Path | None, state: dict) -> None:
    if not path:
        return
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {key: state.get(key, DEFAULTS[key]) for key in DEFAULTS},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
