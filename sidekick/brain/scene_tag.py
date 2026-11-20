"""主回复里的隐藏画面场景标签。

    <scene>打女武神二阶段，血瓶剩2个</scene>

标签只用于下一轮历史里的 `[场景]` 行；在写历史、动作/任务标签、guard、TTS
和气泡之前剥掉。模型漏写、写多行、写进句子中间时都静默降级，不破坏主回复。
"""
from __future__ import annotations

import re

SCENE_MAX = 25

_SCENE = re.compile(r"<scene\b[^>]*>(.*?)</scene\s*>", re.I | re.S)
_UNCLOSED = re.compile(r"<scene\b[^>]*>(.*)$", re.I | re.S)
_SCENE_CLOSE = re.compile(r"</scene\s*>", re.I)
_SCENE_OPEN = re.compile(r"<scene\b[^>]*>", re.I)


def extract(text: str) -> tuple[str, str]:
    """返回 (剥掉标签的正文, 最后一个非空场景)；场景超过 25 字直接截断。

    未闭合标签单独处理：如果它从行首开始，按“到文末都是场景”剥掉；
    如果写在正文中间，只删标签、保留后面的字，避免一条坏标签吞掉整句回复。
    """
    raw = str(text or "")
    scenes: list[str] = []

    def take(match: re.Match) -> str:
        body = " ".join(match.group(1).split()).strip()
        if body:
            scenes.append(body[:SCENE_MAX])
        return ""

    clean = _SCENE.sub(take, raw)

    def take_unclosed(match: re.Match) -> str:
        prefix = clean[:match.start()]
        body = " ".join(match.group(1).split()).strip()
        if not prefix.strip() or prefix.endswith("\n"):
            if body:
                scenes.append(body[:SCENE_MAX])
            return ""
        return match.group(1)

    clean = _UNCLOSED.sub(take_unclosed, clean)
    clean = _SCENE_CLOSE.sub("", clean)
    clean = _SCENE_OPEN.sub("", clean)
    return clean.strip(), (scenes[-1] if scenes else "")
