"""TTS 组合根：按 `tts.source` 找 profile，再按 preset 的 `wire` 分派。

新增同形状厂商只改 `sidekick/config/tts_presets.py`；新增协议形状才在
`make()` 里加一条分支。
"""
from __future__ import annotations

from sidekick.config import tts_presets
from sidekick.output.tts.base import Provider

__all__ = ["make"]


def make(cfg: dict) -> Provider:
    tts = cfg["tts"]
    source = str(tts.get("source") or "").strip()
    block = tts.get(source)
    if not isinstance(block, dict):
        raise SystemExit(f"tts.source 必须指向一个 profile 配置块：{source or '（空）'}")
    preset_id = str(block.get("preset") or "").strip()
    preset = tts_presets.get(preset_id)
    if preset is None:
        raise SystemExit(f"不认识的 tts preset：{preset_id or '（空）'}")
    if preset["wire"] == "edge":
        from sidekick.output.tts.edge import EdgeProvider

        return EdgeProvider(profile=source, block=block)
    if preset["wire"] == "http":
        from sidekick.output.tts.http import HttpProvider

        return HttpProvider(
            profile=source, preset_id=preset_id, block=block,
            timeout=float(tts["common"]["timeout_s"]))
    raise SystemExit(f"不认识的 tts wire：{preset['wire']}")
