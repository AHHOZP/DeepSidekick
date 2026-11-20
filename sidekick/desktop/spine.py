"""Spine 形象在桌面窗里的渲染器：图集合成、GL 绘制、命中、取景。

坐标系与 Live2D 一致：窗口客户区像素，左上原点、y 向下（C 层 `spBone_setYDown(1)`）。
GL 只用固定管线 + 客户端顶点数组，且**每帧自己设一遍状态**——live2d-py 的绘制会留下
它自己的 program/blend，气泡层又会改混合模式。
"""
from __future__ import annotations

import math
import time
from collections.abc import Collection
from pathlib import Path

import numpy as np
from OpenGL.GL import (
    GL_BLEND, GL_CLAMP_TO_EDGE, GL_COLOR_ARRAY, GL_CULL_FACE, GL_DEPTH_TEST,
    GL_DST_COLOR, GL_FLOAT, GL_LINEAR, GL_LINEAR_MIPMAP_LINEAR, GL_MODELVIEW,
    GL_MODULATE, GL_ONE, GL_ONE_MINUS_SRC_ALPHA, GL_ONE_MINUS_SRC_COLOR,
    GL_PROJECTION, GL_RGBA, GL_SRC_ALPHA, GL_TEXTURE_2D, GL_TEXTURE_COORD_ARRAY,
    GL_TEXTURE_ENV, GL_TEXTURE_ENV_MODE, GL_TEXTURE_MAG_FILTER,
    GL_TEXTURE_MAX_LEVEL, GL_TEXTURE_MIN_FILTER, GL_TEXTURE_WRAP_S,
    GL_TEXTURE_WRAP_T, GL_TRIANGLES, GL_UNSIGNED_BYTE, GL_UNSIGNED_SHORT,
    GL_UNPACK_ALIGNMENT, GL_VERTEX_ARRAY, GLError, glBindTexture, glBlendFunc,
    glColorPointer, glDisable, glDisableClientState, glDrawElements, glEnable,
    glEnableClientState, glGenTextures, glGenerateMipmap, glLoadIdentity,
    glMatrixMode, glMultMatrixf, glOrtho, glPixelStorei, glPopMatrix,
    glPushMatrix, glScalef, glTexCoordPointer, glTexEnvi, glTexImage2D,
    glTexParameterf, glTexParameteri, glTranslatef, glUseProgram,
    glVertexPointer,
)

from sidekick.avatar.builder import idle_of
from sidekick.avatar.model import Avatar
from sidekick.desktop.pet_model import BasePetModel, part_from_names
from sidekick.motion.spring import SecondOrder
from sidekick.spine.binding import SpineInstance
from sidekick.spine.pma import looks_premultiplied

# Spine blendMode -> GL 混合因子，[非预乘, 预乘] 两套（与官方 GL 表一致）
_BLEND = {
    0: ((GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA), (GL_ONE, GL_ONE_MINUS_SRC_ALPHA)),
    1: ((GL_SRC_ALPHA, GL_ONE), (GL_ONE, GL_ONE)),
    2: ((GL_DST_COLOR, GL_ONE_MINUS_SRC_ALPHA), (GL_DST_COLOR, GL_ONE_MINUS_SRC_ALPHA)),
    3: ((GL_ONE, GL_ONE_MINUS_SRC_COLOR), (GL_ONE, GL_ONE_MINUS_SRC_COLOR)),
}
_FIT_MARGIN = 0.88
_FIT_BOTTOM = 10.0
# GL_TEXTURE_MAX_ANISOTROPY（EXT_texture_filter_anisotropic 的核心名，PyOpenGL 没导出常量）
_TEXTURE_MAX_ANISOTROPY = 0x84FE


class SpineRenderer(BasePetModel):
    """一个 Spine 骨架：加载、播动画、取景、绘制、命中。"""

    def __init__(self, avatar: Avatar, width: int, height: int) -> None:
        self.avatar = avatar
        root = Path(avatar.root)
        entry = root / avatar.entry
        self._inst = SpineInstance(entry, entry.with_suffix(".atlas"))
        self._texture = _AtlasTexture(root, self._inst.pages, hint=self._inst.pma)
        self._inst.set_texture_layout(self._texture.offsets, self._texture.widths,
                                      self._texture.heights,
                                      self._texture.atlas_w, self._texture.atlas_h)
        self._animations = tuple(self._inst.animations)
        idle = str(avatar.idle or "").strip()
        # 扫描阶段给的 idle 可能不在动画表里（换模型/手写配置），以模型自身为准接住；
        # 一条待机都挑不到时退到第一个动画——桌宠空着窗口比动作不自然更糟。
        self._idle = (idle if idle in self._animations
                      else (idle_of(list(self._animations))
                            or (self._animations[0] if self._animations else "")))
        self.scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self._ref = self._sample_bounds()
        self._action_until = 0.0
        self._current = self._idle
        self._t_last = time.perf_counter()
        self._talk_target = 0.0
        self._talk_spring = SecondOrder(frequency=3.2, damping=0.45,
                                        response=0.20)
        self._talk_silence = 1.0
        self._talk_name = self._find_talk_animation()
        self._talk_active = False
        self._place()
        if self._idle:
            self._inst.play(self._idle, loop=True)

    # ── 每帧 ──────────────────────────────────────────
    def _find_talk_animation(self) -> str:
        names = {str(name).casefold(): str(name) for name in self._animations}
        for candidate in ("talk", "speak", "idle_talk"):
            if candidate in names:
                return names[candidate]
        if self._idle:
            suffixed = (self._idle + "_talk").casefold()
            if suffixed in names:
                return names[suffixed]
        for name in self._animations:
            if str(name).casefold().endswith("_talk"):
                return str(name)
        return ""

    def set_mouth(self, level: float) -> None:
        """说话能量驱动弹性缩放；同时切到可用的 talk 循环。"""
        self._talk_target = max(0.0, min(1.0, float(level)))
        now = time.perf_counter()
        if self._talk_target > 0.02:
            self._talk_silence = 0.0
            if (self._talk_name and not self._talk_active
                    and now >= self._action_until
                    and self._inst.play(self._talk_name, loop=True)):
                self._talk_active = True
                self._current = self._talk_name

    def update(self, _dt: float = 1 / 60) -> None:
        now = time.perf_counter()
        delta = min(0.05, max(0.0, now - self._t_last))
        self._t_last = now
        self._talk_spring.update(self._talk_target, delta)
        if self._talk_target <= 0.02:
            self._talk_silence += delta
            if (self._talk_active and self._talk_silence > 0.24
                    and now >= self._action_until):
                self._talk_active = False
                if self._idle:
                    self._inst.play(self._idle, loop=True)
                    self._current = self._idle
        self._inst.update(delta)
        if self._action_until and now >= self._action_until:
            self._action_until = 0.0
            self._talk_active = False
            if self._idle:
                self._inst.play(self._idle, loop=True)
                self._current = self._idle
            else:
                self._inst.clear()
                self._current = ""

    def draw(self) -> None:
        geo, attrs, tris, segments = self._inst.build(*self._fit)
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        glOrtho(0, self.width, self.height, 0, -1, 1)   # 左上原点、y 向下
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()
        glUseProgram(0)
        glDisable(GL_DEPTH_TEST)
        glDisable(GL_CULL_FACE)
        glEnable(GL_BLEND)
        glEnable(GL_TEXTURE_2D)
        glTexEnvi(GL_TEXTURE_ENV, GL_TEXTURE_ENV_MODE, GL_MODULATE)
        glBindTexture(GL_TEXTURE_2D, self._texture.tex)
        if geo.shape[0] and tris.size:
            glPushMatrix()
            glTranslatef(float(self._origin[0] + self.offset_x),
                         float(self._origin[1] + self.offset_y), 0.0)
            self._apply_talk_transform()
            uv = np.ascontiguousarray(attrs[:, 0:2])
            color = np.ascontiguousarray(attrs[:, 2:6])
            glEnableClientState(GL_VERTEX_ARRAY)
            glEnableClientState(GL_TEXTURE_COORD_ARRAY)
            glEnableClientState(GL_COLOR_ARRAY)
            glVertexPointer(2, GL_FLOAT, 0, geo)
            glTexCoordPointer(2, GL_FLOAT, 0, uv)
            glColorPointer(4, GL_FLOAT, 0, color)
            for blend, start, end in segments:
                if end <= start:
                    continue
                pair = _BLEND.get(blend, _BLEND[0])
                src, dst = pair[1] if self._texture.premultiplied else pair[0]
                glBlendFunc(src, dst)
                glDrawElements(GL_TRIANGLES, int((end - start) * 3),
                               GL_UNSIGNED_SHORT,
                               np.ascontiguousarray(tris[start * 3:end * 3]))
            glDisableClientState(GL_COLOR_ARRAY)
            glDisableClientState(GL_TEXTURE_COORD_ARRAY)
            glDisableClientState(GL_VERTEX_ARRAY)
            glPopMatrix()

    def _apply_talk_transform(self) -> None:
        """脚底锚点的体积守恒挤压拉伸 + 微剪切，不碰骨架内部。"""
        level = max(0.0, min(1.0, self._talk_spring.position))
        if level < 0.002:
            return
        min_x, min_y, max_x, max_y = self._ref
        zoom = self._fit[2]
        bottom = max(1.0, (max_y - min_y) * zoom)
        center_x = max(1.0, (max_x - min_x) * zoom) / 2.0
        sy = 1.0 + 0.022 * level
        sx = 1.0 - 0.011 * level
        sway = max(-1.0, min(1.0, self._talk_spring.velocity * 0.35))
        shear = sway * 0.018
        glTranslatef(center_x, bottom, 0.0)
        glScalef(sx, sy, 1.0)
        glTranslatef(-center_x, -bottom, 0.0)
        # x' = x + shear * (y' - bottom)
        glMultMatrixf([
            1.0, 0.0, 0.0, 0.0,
            shear, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            -shear * bottom, 0.0, 0.0, 1.0,
        ])

    # ── 尺寸与取景 ────────────────────────────────────
    def resize(self, width: int, height: int) -> None:
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self._place()

    def set_scale(self, scale: float) -> None:
        self.scale = max(0.2, min(4.0, float(scale)))
        self._place()

    def set_offset(self, x: float, y: float) -> None:
        self.offset_x = float(x)
        self.offset_y = float(y)
        # C 层的命中/拖动也读放置点；只改 Python 字段会让偏移后点不准。
        self._place()

    def _place(self) -> None:
        """把参考包围盒按窗口和 scale 摆到底部居中，并把放置点记进 C 层。"""
        min_x, min_y, max_x, max_y = self._ref
        box_w = max(1e-6, max_x - min_x)
        box_h = max(1e-6, max_y - min_y)
        zoom = min(self.width * _FIT_MARGIN / box_w,
                   self.height * _FIT_MARGIN / box_h) * self.scale
        self._fit = (min_x, min_y, zoom)
        self._origin = ((self.width - box_w * zoom) / 2.0,
                        self.height - _FIT_BOTTOM - box_h * zoom)
        self._inst.set_offset(self._origin[0] + self.offset_x,
                              self._origin[1] + self.offset_y)

    def _sample_bounds(self) -> tuple[float, float, float, float]:
        """按"待机动画播一遍"的真实可见网格算取景框。

        不用 `collectBounds` 的附件包围盒：游戏拆包模型里常有大量隐藏/空附件和特效框，
        按它取景会出现"框很大、人很小"或者干脆空窗口。这里直接看 build 出来的三角形
        （zoom=1、min=0 时就是世界坐标）。

        待机动画按完整时长采样，避免播到一半撑出窗口；不这样做，逐帧范围变化会让模型
        在窗里一胀一缩。
        """
        anim = self._idle or (self._animations[0] if self._animations else "")
        if not anim:
            return (-1.0, -1.0, 1.0, 1.0)
        self._inst.play(anim, loop=True)
        duration = self._inst.duration(anim)
        frames = min(120, max(12, int(duration * 30) if duration else 12))
        step = duration / frames if duration else 1.0 / 30.0
        low = [1e9, 1e9, -1e9, -1e9]
        for _ in range(frames):
            self._inst.update(step)
            geo, _attrs, _tris, _segments = self._inst.build(0.0, 0.0, 1.0)
            if not len(geo):
                continue
            low[0] = min(low[0], float(geo[:, 0].min()))
            low[1] = min(low[1], float(geo[:, 1].min()))
            low[2] = max(low[2], float(geo[:, 0].max()))
            low[3] = max(low[3], float(geo[:, 1].max()))
        if low[0] > 1e8:
            return (-1.0, -1.0, 1.0, 1.0)
        return (low[0], low[1], low[2], low[3])  # type: ignore[return-value]

    # ── 命中 ─────────────────────────────────────────
    def hit_part(self, x: float, y: float) -> str:
        slot = self._inst.hit_part(float(x), float(y))
        return part_from_names([slot]) if slot else ""

    # ── 动作 ─────────────────────────────────────────
    @property
    def motion_groups(self) -> Collection[str]:
        return self._animations

    @property
    def idle(self) -> str:
        return self._idle

    @property
    def current_animation(self) -> str:
        return self._current

    @property
    def version(self) -> str:
        return str(self._inst.version)

    @property
    def aspect(self) -> float:
        """取景框的宽高比：桌面窗按它定形状，宽模型和窄模型都不会浪费窗口。"""
        min_x, min_y, max_x, max_y = self._ref
        return (max(1e-6, max_x - min_x) / max(1e-6, max_y - min_y))

    def start_motion(self, group: str) -> bool:
        name = str(group or "")
        if not name or name not in self._animations:
            return False
        loop = name == self._idle
        if not self._inst.play(name, loop=loop):
            return False
        self._current = name
        self._action_until = 0.0 if loop else time.perf_counter() + self._inst.duration(name)
        return True

    def apply_acts(self, acts: list[dict] | None) -> None:
        for act in acts or []:
            if not isinstance(act, dict):
                continue
            if str(act.get("kind") or "") != "motion":
                continue                      # Spine 没有表情能力
            name = str(act.get("name") or "")
            if name not in self._animations:
                continue
            if self._inst.play(name, loop=False):
                self._current = name
                self._talk_active = False
                self._action_until = time.perf_counter() + self._inst.duration(name)

    def control_motion(self, group: str, index=0) -> None:
        self.start_motion(group)

    def control_view(self, zoom=None, off_x=None, off_y=None) -> None:
        if zoom is not None:
            self.set_scale(float(zoom))
        if off_x is not None or off_y is not None:
            self.set_offset(
                (float(off_x) if off_x is not None else 0.0) * self.width,
                (float(off_y) if off_y is not None else 0.0) * self.height)

    def reset_controls(self) -> None:
        self.scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self._place()
        if self._idle:
            self.start_motion(self._idle)

    def close(self) -> None:
        self._inst.close()


class _AtlasTexture:
    """把 atlas 的所有 page 横排成一张 RGBA 纹理，并给出 C 层要的偏移/页宽高。

    用 PIL 读图而不是 `pygame.image.load().convert_alpha()`：Spine 图集常常是预乘的，
    走 SDL 的格式转换会把 RGB 再拆一次；PIL 的 `convert("RGBA")` 只搬字节。

    是否预乘优先信 atlas 里的 `pma:` 声明；没有声明才看像素。
    """

    def __init__(self, root: Path, pages: list[str], hint: bool | None = None) -> None:
        from PIL import Image

        if not pages:
            raise ValueError("atlas 里没有 page")
        images = []
        for name in pages:
            with Image.open(root / name) as image:
                images.append(image.convert("RGBA"))
        self.widths = [im.width for im in images]
        self.heights = [im.height for im in images]
        self.atlas_w = sum(self.widths)
        self.atlas_h = max(self.heights)
        self.offsets: list[int] = []
        canvas = Image.new("RGBA", (self.atlas_w, self.atlas_h), (0, 0, 0, 0))
        x = 0
        for image in images:
            self.offsets.append(x)
            canvas.paste(image, (x, 0))
            x += image.width
        self.premultiplied = (hint if hint is not None
                              else looks_premultiplied(images))
        if not self.premultiplied:
            # 直通 alpha 的图里，透明区域的 RGB 通常是黑的。双线性 / mipmap 采样会把
            # 那片黑混进边缘，形成一圈"越缩越粗的描边"（经典的 non-PMA 黑边）。
            # 自己转成预乘再按预乘混合，就能把黑边去掉。
            canvas = _premultiply(canvas)
            self.premultiplied = True

        self.tex = int(glGenTextures(1))
        glBindTexture(GL_TEXTURE_2D, self.tex)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        # 逐行 top→bottom 上传，正好对上 Spine UV 的 v=0 在顶部
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, self.atlas_w, self.atlas_h, 0,
                     GL_RGBA, GL_UNSIGNED_BYTE, canvas.tobytes())
        self._set_filter()

    def _set_filter(self) -> None:
        """缩小时用 mipmap + 各向异性，放大时才谈得上"清晰"。

        没有 mipmap 时，2048 的立绘被压到 200 px 宽，每个屏幕像素要采 10 个纹素，
        线性过滤只采 4 个 → 边缘闪烁、发脆（就是"缩小时明显锯齿"）。
        """
        levels = _mip_levels(self.atlas_w, self.atlas_h)
        try:
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAX_LEVEL, levels)
            glGenerateMipmap(GL_TEXTURE_2D)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER,
                            GL_LINEAR_MIPMAP_LINEAR)
        except GLError:
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        try:
            glTexParameterf(GL_TEXTURE_2D, _TEXTURE_MAX_ANISOTROPY, 8.0)
        except GLError:
            pass


def _mip_levels(width: int, height: int) -> int:
    """多页图集是横向拼在一起的，mip 层数太深会把相邻页糊进来，这里封顶 4 级。"""
    smallest = max(1, min(width, height))
    return max(0, min(4, smallest.bit_length() - 1))


def _premultiply(canvas):
    """把直通 alpha 的图转成预乘（RGB *= A）。"""
    from PIL import Image

    pixels = np.asarray(canvas, dtype=np.uint16)
    alpha = pixels[..., 3:4]
    rgb = (pixels[..., :3] * alpha + 127) // 255
    out = np.concatenate([rgb, alpha], axis=2).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


