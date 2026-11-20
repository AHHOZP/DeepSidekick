"""桌宠形象模型的唯一契约：Live2D / 图片（将来 Spine）都实现它。

`sidekick/desktop/app.py` 只认这个契约，不认识 Cubism 或 Pygame 的具体类型。
新增渲染器 = 新增一个实现 + 组合根里多一个分支，主循环不用改。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection

from sidekick.motion.null import NULL_MOTION_HOST
from sidekick.motion.types import MotionHost

# 命中部件的词表是封闭小集合，与 stage 的 click 协议一致（"头"/"手"/"身子"）。
PART_HEAD, PART_HAND, PART_BODY = "头", "手", "身子"
_HEAD_WORDS = ("头", "脸", "发", "耳", "眼", "眉", "嘴", "鼻", "neck", "head")
_HAND_WORDS = ("手", "臂", "arm", "hand")
_BODY_WORDS = ("身", "胸", "腿", "脚", "裙", "body", "leg", "foot")


def part_from_names(names: list[str]) -> str:
    """把命中到的部件名归一到 头/手/身子；空列表返回空串。"""
    low = [n.lower() for n in names]
    if not low:
        return ""
    if any(w in n for n in low for w in _HEAD_WORDS):
        return PART_HEAD
    if any(w in n for n in low for w in _HAND_WORDS):
        return PART_HAND
    if any(w in n for n in low for w in _BODY_WORDS):
        return PART_BODY
    return PART_BODY


class BasePetModel(ABC):
    """一个可渲染的桌宠形象。

    坐标约定：所有对外坐标都是**窗口客户区像素**，左上原点、y 向下，
    与 `glOrtho(0, w, h, 0)` 和 Pygame 事件坐标一致。
    """

    #: 加载期出的一行提示（口型不可用、图片打不开……），由桌面报到 `/status`；没有就是空串。
    notice: str = ""

    # ── 生命周期 ──────────────────────────────────────
    @abstractmethod
    def close(self) -> None:
        """释放原生资源；必须幂等。"""

    # ── 每帧 ─────────────────────────────────────────
    @abstractmethod
    def update(self, dt: float = 1 / 60) -> None:
        """推进动画/物理，不画、不动投影。dt 单位秒。"""

    @abstractmethod
    def draw(self) -> None:
        """把当前帧画到调用方已绑定的 GL 上下文；自己设状态、自己收尾。"""

    # ── 尺寸与取景 ────────────────────────────────────
    @abstractmethod
    def resize(self, width: int, height: int) -> None: ...

    @abstractmethod
    def set_scale(self, scale: float) -> None: ...

    @abstractmethod
    def set_offset(self, x: float, y: float) -> None: ...

    def look_at(self, x: float, y: float) -> None:
        """鼠标跟随；不支持就空实现。"""

    def reset_drag(self) -> None:
        """关闭跟随鼠标时把视线/头部拉回中心。"""

    # ── 命中 ─────────────────────────────────────────
    @abstractmethod
    def hit_part(self, x: float, y: float) -> str:
        """返回 头/手/身子；透明处返回空串。空串即"没命中"。"""

    # ── 动作 ─────────────────────────────────────────
    @abstractmethod
    def start_motion(self, group: str) -> bool: ...

    def apply_acts(self, acts: list[dict] | None) -> None:
        """播放模型语义动作 [{"kind": "motion"|"expression", "name": ...}]。"""

    def express_emotion(self, emotion: str) -> None:
        """把情绪映射到模型自带 expression；没有映射就空实现。"""

    def set_mouth(self, level: float) -> None:
        """0..1 音量 → 口型；没有口型能力的实现空实现（不要在上层加 if）。"""

    @property
    def motion_host(self) -> MotionHost:
        """程序化驱动宿主；没有能力的实现返回空对象。"""
        return NULL_MOTION_HOST

    # ── WebUI Inspector 远程调参 ──────────────────────
    def apply_controls(self) -> None: ...

    def control_param(self, param_id: str, value) -> None: ...

    def control_part(self, part_id: str, value) -> None: ...

    def control_motion(self, group: str, index=0) -> None: ...

    def control_expression(self, name) -> None: ...

    def control_view(self, zoom=None, off_x=None, off_y=None) -> None: ...

    def reset_controls(self) -> None: ...

    # ── 能力查询（只读，菜单/日志用）────────────────────
    @property
    def aspect(self) -> float:
        """模型自身的宽高比（宽/高）；桌面窗按它定形状。没有这个概念的给 3:4。"""
        return 0.75

    @property
    @abstractmethod
    def motion_groups(self) -> Collection[str]:
        """可播放的动作/动画名。"""

    @property
    @abstractmethod
    def idle(self) -> str:
        """空闲动作名；没有就是空串。"""
