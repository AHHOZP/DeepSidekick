"""Pygame 自绘右键菜单（支持二级动作菜单与竖滑块）。"""
from __future__ import annotations

from dataclasses import dataclass, field

import pygame


@dataclass
class Item:
    label: str
    action: str
    value: object = None
    children: list["Item"] = field(default_factory=list)
    checked: bool = False
    # 非 None 时这一行是竖滑块：最上是 1.0、最下是 slider_min，拖动时连续回调 action。
    slider: float | None = None
    slider_min: float = 0.0


class Menu:
    W = 150
    ITEM_H = 28
    PAD = 6
    SLIDER_H = 170
    SLIDER_W = 78

    def __init__(self) -> None:
        self.items: list[Item] = []
        self.pos = (0, 0)
        self.active = False
        self.hover = -1
        self.sub_open = False
        self.sub_hover = -1
        self.rect = pygame.Rect(0, 0, 0, 0)
        self.sub_rect = pygame.Rect(0, 0, 0, 0)
        self._dragging = False
        self.font = pygame.font.SysFont("microsoftyahei", 16)
        self.small = pygame.font.SysFont("microsoftyahei", 14)

    def open(self, pos: tuple[int, int], items: list[Item]) -> None:
        self.items = items
        self.pos = pos
        self.active = True
        self.hover = -1
        self.sub_open = False
        self.sub_hover = -1
        self._dragging = False

    def close(self) -> None:
        self.active = False
        self.sub_open = False
        self._dragging = False

    # ── 布局 ──────────────────────────────────────────
    def _h(self, item: Item) -> int:
        return self.SLIDER_H if item.slider is not None else self.ITEM_H

    def _rows(self, rect: pygame.Rect, items: list[Item]):
        """逐行给出 (index, item, row_rect)。"""
        y = rect.top + self.PAD
        for i, item in enumerate(items):
            h = self._h(item)
            yield i, item, pygame.Rect(rect.left + 2, y, rect.width - 4, h)
            y += h

    def _panel(self, x: int, y: int, items: list[Item],
               width: int) -> pygame.Rect:
        h = self.PAD * 2 + sum(self._h(item) for item in items)
        return pygame.Rect(x, y, width, h)

    def _layout(self, surface: pygame.Surface) -> None:
        x, y = self.pos
        x = max(4, min(x, surface.get_width() - self.W - 4))
        self.rect = self._panel(x, y, self.items, self.W)
        if self.rect.bottom > surface.get_height() - 4:
            self.rect.y = max(4, surface.get_height() - self.rect.height - 4)
        if not self.sub_open or self.hover < 0:
            self.sub_rect = pygame.Rect(0, 0, 0, 0)
            return
        children = self.items[self.hover].children
        if not children:
            self.sub_rect = pygame.Rect(0, 0, 0, 0)
            return
        slider = children[0].slider is not None and len(children) == 1
        sw = self.SLIDER_W if slider else 170
        sx = self.rect.right
        if sx + sw > surface.get_width() - 4:
            sx = self.rect.left - sw
        sx = max(4, min(sx, surface.get_width() - sw - 4))
        sy = self.rect.top + self.PAD + sum(
            self._h(item) for item in self.items[:self.hover])
        self.sub_rect = self._panel(sx, sy, children, sw)
        if self.sub_rect.bottom > surface.get_height() - 4:
            self.sub_rect.y = max(
                4, surface.get_height() - self.sub_rect.height - 4)
        if self.sub_rect.top < 4:
            self.sub_rect.y = 4

    # ── 绘制 ──────────────────────────────────────────
    def _draw_panel(self, surface: pygame.Surface, rect: pygame.Rect,
                    items: list[Item], hover: int) -> None:
        box = pygame.Surface(rect.size, pygame.SRCALPHA)
        pygame.draw.rect(box, (20, 25, 33, 245), box.get_rect(),
                         border_radius=7)
        pygame.draw.rect(box, (80, 105, 130, 235), box.get_rect(),
                         width=1, border_radius=7)
        for i, item, row in self._rows(rect, items):
            local = row.move(-rect.left, -rect.top)
            if item.slider is not None:
                self._draw_slider(box, local, item)
                continue
            if i == hover:
                pygame.draw.rect(box, (43, 70, 92, 230), local,
                                 border_radius=5)
            mark = "√ " if item.checked else ""
            text = self.font.render(mark + item.label, True, (225, 235, 245))
            box.blit(text, (10, local.y + (self.ITEM_H - text.get_height()) // 2))
            if item.children:
                arrow = self.small.render("›", True, (140, 165, 185))
                box.blit(arrow, (rect.width - 18,
                                 local.y + (self.ITEM_H - arrow.get_height()) // 2))
        surface.blit(box, rect.topleft)

    def _track(self, row: pygame.Rect) -> pygame.Rect:
        return pygame.Rect(row.x + 14, row.y + 10, 8, row.height - 20)

    def _draw_slider(self, box: pygame.Surface, row: pygame.Rect,
                     item: Item) -> None:
        low = max(0.0, min(0.99, float(item.slider_min)))
        value = max(low, min(1.0, float(item.slider or low)))
        frac = (value - low) / (1.0 - low)
        track = self._track(row)
        pygame.draw.rect(box, (60, 78, 96, 235), track, border_radius=4)
        knob_y = int(track.bottom - frac * track.height)
        filled = pygame.Rect(track.x, knob_y, track.width, track.bottom - knob_y)
        pygame.draw.rect(box, (96, 176, 240, 255), filled, border_radius=4)
        pygame.draw.circle(box, (225, 240, 255), (track.centerx, knob_y), 6)
        text = self.small.render(f"{int(round(value * 100))}%", True,
                                 (225, 235, 245))
        box.blit(text, (track.right + 10,
                        max(2, min(row.height - text.get_height() - 2,
                                   knob_y - text.get_height() // 2))))

    def draw(self, surface: pygame.Surface) -> None:
        if not self.active:
            return
        self._layout(surface)
        self._draw_panel(surface, self.rect, self.items, self.hover)
        if self.sub_open and self.sub_rect.width and self.hover >= 0:
            children = self.items[self.hover].children
            self._draw_panel(surface, self.sub_rect, children, self.sub_hover)

    # ── 命中 ──────────────────────────────────────────
    def _item_at(self, rect: pygame.Rect, items: list[Item],
                 pos: tuple[int, int]) -> int:
        if not rect.collidepoint(pos):
            return -1
        for i, _item, row in self._rows(rect, items):
            if pos[1] < row.bottom:
                return i
        return -1

    def _slider_at(self, rect: pygame.Rect, items: list[Item],
                   pos: tuple[int, int]) -> tuple[int, float] | None:
        if not rect.collidepoint(pos):
            return None
        for i, item, row in self._rows(rect, items):
            if item.slider is None or pos[1] >= row.bottom:
                continue
            track = self._track(row)
            frac = (track.bottom - pos[1]) / max(1, track.height)
            low = max(0.0, min(0.99, float(item.slider_min)))
            value = low + max(0.0, min(1.0, frac)) * (1.0 - low)
            return i, value
        return None

    # ── 事件 ──────────────────────────────────────────
    def handle(self, event: pygame.event.Event):
        if not self.active:
            return None
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.close()
            return None
        children = (self.items[self.hover].children
                    if 0 <= self.hover < len(self.items) else [])
        in_sub = (self.sub_open and self.sub_rect.width > 0
                  and self.sub_rect.collidepoint(event.pos)
                  if hasattr(event, "pos") else False)

        if event.type == pygame.MOUSEMOTION:
            pos = event.pos
            if self.sub_open and self.sub_rect.width and children:
                hit = self._slider_at(self.sub_rect, children, pos)
                if hit is not None and self._dragging:
                    item = children[hit[0]]
                    item.slider = hit[1]
                    return item.action, hit[1]
            if in_sub:
                self.sub_hover = self._item_at(self.sub_rect, children, pos)
                return None
            i = self._item_at(self.rect, self.items, pos)
            if i != self.hover:
                self.hover = i
                self.sub_hover = -1
                self.sub_open = bool(i >= 0 and self.items[i].children)
            return None

        if event.type == pygame.MOUSEBUTTONUP:
            self._dragging = False
            return None

        if event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 3:
                self.close()
                return None
            if event.button != 1:
                return None
            pos = event.pos
            if in_sub and children:
                hit = self._slider_at(self.sub_rect, children, pos)
                if hit is not None:
                    item = children[hit[0]]
                    item.slider = hit[1]
                    self._dragging = True      # 滑块不关菜单，接着拖
                    return item.action, hit[1]
                i = self._item_at(self.sub_rect, children, pos)
                if i >= 0:
                    item = children[i]
                    self.close()
                    return item.action, item.value
            i = self._item_at(self.rect, self.items, pos)
            if i < 0:
                self.close()
                return None
            item = self.items[i]
            if item.children:
                self.hover = i
                self.sub_open = True
                self.sub_hover = -1
                return None
            self.close()
            return item.action, item.value
        return None
