"""单张静态图的桌面渲染：和 Live2D / Spine 共用窗口、气泡、菜单。

解码交给 SDL_image：png / jpg / webp / bmp / gif 都读得进，透明背景的 alpha 原样
保留（gif 只取第一帧）。SVG 走它内置的 nanosvg，那颗库只认标签属性，这里补两件事：
把 `<style>` 里的类/id 规则摊成行内属性（Illustrator / Figma 导出的 `fill` 全在
类里，直接交给它就是一片黑），再按目标尺寸包一层 `scale()`（它不认 `width`/
`height`，只会把画布放大、图形原地不动，等于白白糊一遍）。
"""
from __future__ import annotations

import io
import re
from collections.abc import Collection
from pathlib import Path
from xml.etree import ElementTree

import pygame

from sidekick.avatar.model import Avatar
from sidekick.desktop.overlay import SurfaceTexture
from sidekick.desktop.pet_model import BasePetModel, PART_BODY

_SVG_NS = "http://www.w3.org/2000/svg"
_SVG = f"{{{_SVG_NS}}}"
# 边框里可能出现 @media 之类的嵌套块，按"大括号里不含大括号"切，正好只取扁平的规则。
_CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
# 只认最朴素的 .class / #id：后代、伪类、组合选择器一律跳过，不猜。
_CSS_SELECTOR = re.compile(r"[.#][\w-]+")
_NUMBER = re.compile(r"\s*(-?[0-9.]+)")
# 序列化时用回默认命名空间：nanosvg 只认裸标签名，带 ns0: 前缀整份都读不了。
# 这是 ElementTree 的进程级注册表，此处是唯一的写入方。
ElementTree.register_namespace("", _SVG_NS)


def decode(path: Path) -> pygame.Surface:
    """读一张静态图，统一成带 alpha 的 32 位表面。

    调色板 gif 的透明是 colorkey，24 位 jpg 干脆没有 alpha；blit 到 SRCALPHA 上
    一次补齐，之后缩放/命中检测都只有一种情况。
    """
    surf = pygame.image.load(str(path))
    if surf.get_flags() & pygame.SRCALPHA:
        return surf
    out = pygame.Surface(surf.get_size(), pygame.SRCALPHA)
    out.blit(surf, (0, 0))
    return out


def fit(surf: pygame.Surface, width: int, height: int) -> pygame.Surface:
    """等比缩到能放进 width×height 的最大尺寸：不拉伸、不裁剪、不留黑边。"""
    sw, sh = surf.get_size()
    k = min(width / max(1, sw), height / max(1, sh))
    return pygame.transform.smoothscale(
        surf, (max(1, round(sw * k)), max(1, round(sh * k))))


def render_svg(path: Path, width: int, height: int) -> pygame.Surface:
    """SVG → 目标尺寸的位图；按目标尺寸光栅化，所以放大也不会糊。"""
    root = ElementTree.parse(str(path)).getroot()
    _inline_css(root)
    box = _svg_box(root)
    if box is None:
        raise ValueError("SVG 里没有 viewBox 也没有可用的 width/height")
    left, top, box_w, box_h = box
    k = min(width / box_w, height / box_h)
    # 自己算 viewBox → 画布的映射，再把这个属性摘掉：nanosvg 那套映射既不做缩放、
    # 又只认 viewBox 的横向偏移，留着它会和下面的 transform 叠加成错位/裁切。
    root.attrib.pop("viewBox", None)
    group = ElementTree.Element(f"{_SVG}g", {
        "transform": f"scale({k:.6f}) translate({-left:.6f} {-top:.6f})"})
    for child in list(root):
        root.remove(child)
        group.append(child)
    root.append(group)
    root.set("width", str(max(1, round(box_w * k))))
    root.set("height", str(max(1, round(box_h * k))))
    return pygame.image.load(io.BytesIO(ElementTree.tostring(root, encoding="utf-8")))


def _svg_box(root) -> tuple[float, float, float, float] | None:
    """图形自己的坐标系：viewBox 优先，没有才退回 width/height 属性。"""
    try:
        nums = [float(v) for v in re.split(r"[\s,]+", (root.get("viewBox") or "").strip()) if v]
    except ValueError:
        nums = []
    if len(nums) == 4 and nums[2] > 0 and nums[3] > 0:
        return nums[0], nums[1], nums[2], nums[3]
    w, h = _length(root.get("width")), _length(root.get("height"))
    return (0.0, 0.0, w, h) if w > 0 and h > 0 else None


def _length(value: str | None) -> float:
    """`377.1px` / `377.1` 这类带单位的长度取数字；百分比量不出像素，按缺失处理。"""
    text = value or ""
    if "%" in text:
        return 0.0
    m = _NUMBER.match(text)
    return float(m.group(1)) if m else 0.0


def _inline_css(root) -> None:
    """把 `<style>` 里的类/id 规则摊到每个元素自己的 `style` 属性上。

    元素已经写了的属性优先（和 CSS 的层叠方向一致：行内压类）。
    """
    rules: dict[str, list[str]] = {}
    for style in root.iter(f"{_SVG}style"):
        for selectors, body in _CSS_RULE.findall(style.text or ""):
            decls = [d.strip() for d in body.split(";") if d.strip()]
            for sel in selectors.split(","):
                sel = sel.strip()
                if _CSS_SELECTOR.fullmatch(sel):
                    rules.setdefault(sel, []).extend(decls)
    if not rules:
        return
    for el in root.iter():
        keys = [f".{c}" for c in (el.get("class") or "").split()]
        if el.get("id"):
            keys.append(f"#{el.get('id')}")
        decls = [d for key in keys for d in rules.get(key, ())]
        if not decls:
            continue
        own = [d.strip() for d in (el.get("style") or "").split(";") if d.strip()]
        have = {d.split(":", 1)[0].strip() for d in own}
        extra = [d for d in decls if d.split(":", 1)[0].strip() not in have]
        if extra:
            el.set("style", "; ".join(own + extra))


class ImageRenderer(BasePetModel):
    """把一张图片铺满窗口；没有动作、表情、口型和部件。"""

    def __init__(self, avatar: Avatar, width: int, height: int) -> None:
        self.avatar = avatar
        self.texture = SurfaceTexture()
        self.surface: pygame.Surface | None = None
        self.rect = pygame.Rect(0, 0, 0, 0)
        self.width = 1
        self.height = 1
        self._decoded: pygame.Surface | None = None
        self._decoded_key: tuple[str, int, int] | None = None
        self.resize(width, height)

    @property
    def motion_groups(self) -> Collection[str]:
        return ()

    @property
    def idle(self) -> str:
        return ""

    @property
    def aspect(self) -> float:
        """图片自身的宽高比：桌面窗按它定形状，图片不会被拉伸或留黑边。"""
        if self.rect.height:
            return self.rect.width / self.rect.height
        return 0.75

    def resize(self, width: int, height: int) -> None:
        width = max(1, int(width))
        height = max(1, int(height))
        self.width, self.height = width, height
        canvas = pygame.Surface((width, height), pygame.SRCALPHA)
        try:
            img = self._picture(width, height)
        except (OSError, ValueError, ElementTree.ParseError, pygame.error) as exc:
            self.surface = None
            self.rect = pygame.Rect(0, 0, 0, 0)
            self.notice = f"图片打不开：{self.avatar.entry}（{exc}）"
        else:
            self.rect = img.get_rect(midbottom=(width // 2, height))
            canvas.blit(img, self.rect)
            self.surface = canvas
            self.notice = ""
        self.texture.upload(canvas)

    def _picture(self, width: int, height: int) -> pygame.Surface:
        """按窗口尺寸出图：SVG 每次重新光栅化，位图只解码一次。"""
        path = Path(self.avatar.root) / self.avatar.entry
        if path.suffix.lower() == ".svg":
            return render_svg(path, width, height)
        return fit(self._source(path), width, height)

    def _source(self, path: Path) -> pygame.Surface:
        """原图按「路径 + mtime + 大小」缓存：滚轮每格都会 resize 一次。"""
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if key != self._decoded_key:
            self._decoded = decode(path)
            self._decoded_key = key
        return self._decoded

    def set_scale(self, _scale: float) -> None:
        return None

    def set_offset(self, _x: float, _y: float) -> None:
        return None

    def start_motion(self, _group: str) -> bool:
        return False

    def update(self, _dt: float = 1 / 60) -> None:
        return None

    def draw(self) -> None:
        self.texture.draw(self.width, self.height)

    def hit_part(self, x: float, y: float) -> str:
        if self.surface is None or not self.rect.collidepoint(int(x), int(y)):
            return ""
        try:
            if self.surface.get_at((int(x), int(y))).a <= 8:
                return ""
        except (IndexError, pygame.error):
            return ""
        return PART_BODY

    def close(self) -> None:
        return None
