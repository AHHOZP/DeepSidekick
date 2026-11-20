"""视觉 token 估算：先查实测表，未知尺寸按像素面积在相邻档之间插值。

表来自 2026-11-09 本机 opencode profile 实测（同文字无图 41 token，差值即
单张图）。它只是估算；真实尺寸最终以 `usage.prompt_tokens` 为准，不要拿
16:9 的值冒充 4:3 / 16:10。
"""
from __future__ import annotations

# 文档里的实测档位；宽高是实际发送尺寸，不是最大框。
_TABLE = {
    (1920, 1080): 968,
    (1600, 900): 882,
    (1280, 720): 578,
    (960, 540): 314,
    (1920, 1200): 962,
    (1680, 1050): 962,
    (1440, 900): 794,
    (1280, 800): 642,
    (1600, 1200): 1001,
    (1440, 1080): 938,
    (1280, 960): 738,
    (1024, 768): 496,
    (3440, 1440): 982,
    (2560, 1080): 982,
    (1280, 1024): 802,
}

_POINTS = sorted((w * h, w, h, tok) for (w, h), tok in _TABLE.items())


def estimate_visual_tokens(width: int, height: int) -> int:
    """单张图的估算 token；未知比例按面积插值，非法尺寸返回 0。"""
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        return 0
    if w <= 0 or h <= 0:
        return 0
    exact = _TABLE.get((w, h))
    if exact is not None:
        return exact

    area = w * h
    first, last = _POINTS[0], _POINTS[-1]
    if area <= first[0]:
        return max(1, round(first[3] * area / first[0]))
    if area >= last[0]:
        return last[3]
    for (a1, _w1, _h1, t1), (a2, _w2, _h2, t2) in zip(_POINTS, _POINTS[1:]):
        if area <= a2:
            ratio = (area - a1) / (a2 - a1)
            return round(t1 + (t2 - t1) * ratio)
    return last[3]
