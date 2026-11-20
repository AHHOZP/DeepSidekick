"""输出设备形态：耳机 / 扬声器 / 判不出。

只回答"这是什么设备"，不做门限判断，也不碰麦克风采集（`mic.py` 才做那些）。
判据只看设备名字：`mic.mode: auto` 时靠它决定播放期间要不要闭麦。
"""
from __future__ import annotations

KIND_HEADSET = "headset"
KIND_SPEAKER = "speaker"

# 顺序有意义：先匹配耳机关键词，避免 "Headphones (Realtek)" 被 "realtek" 之类误判。
_HEAD = ("headphone", "headset", "earphone", "earbud", "airpods",
         "耳机", "耳麦", "头戴")
_SPK = ("speaker", "扬声器", "显示器", "monitor", "display", "hdmi",
        "nvidia high definition", "thunderbolt", "usb audio dac")


def output_kind(name: str) -> str | None:
    """按设备名判形态；判不出返回 None。"""
    low = str(name or "").lower()
    if not low:
        return None
    if any(word in low for word in _HEAD):
        return KIND_HEADSET
    if any(word in low for word in _SPK):
        return KIND_SPEAKER
    return None


def output_name() -> str:
    """系统默认输出设备名；查不到返回空串。

    查不到设备名不是故障（没有声卡、驱动没起来），调用方按保守分支处理，
    所以这里只吞掉查询异常，不影响麦克风启动。
    """
    try:
        import sounddevice as sd

        return str(sd.query_devices(kind="output").get("name") or "")
    except Exception:
        return ""


def resolve_mode(cfg_mode: str, name: str) -> str:
    """auto → 按名字判，判不出按扬声器（保守：不打断）；headset/speaker 直接返回。"""
    want = str(cfg_mode or "").strip().lower()
    if want in (KIND_HEADSET, KIND_SPEAKER):
        return want
    return output_kind(name) or KIND_SPEAKER
