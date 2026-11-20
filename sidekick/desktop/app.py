"""Pygame 桌宠主循环：渲染、气泡、点击、右键菜单、缩放和穿透。"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import time

import pygame
import win32api
import win32con
from OpenGL.GL import (
    GL_BLEND, GL_COLOR_BUFFER_BIT, GL_DEPTH_BUFFER_BIT, GL_DEPTH_TEST,
    GL_MODELVIEW, GL_PROJECTION, GL_QUADS, GL_RGBA, GL_SRC_ALPHA,
    GL_TEXTURE_2D, GL_UNSIGNED_BYTE, GL_ZERO,
    glBegin, glBlendFunc, glClear, glClearColor, glColor4f, glDisable,
    glEnable, glEnd, glLoadIdentity, glMatrixMode, glOrtho, glReadPixels,
    glVertex2f, glViewport,
)

import live2d
try:                                   # pygame<2.6 或装坏了：缩放靠它改窗口尺寸
    from pygame._sdl2.video import Window as SdlWindow
except ImportError as exc:             # 换上下文重建 GL 会把模型贴图全弄丢，不能降级
    raise RuntimeError(
        "桌面窗需要 pygame 2.6+（缺 pygame._sdl2.video），请按 requirements.txt 重装依赖"
    ) from exc

from sidekick import config
from sidekick.avatar.catalog import AvatarCatalog
from sidekick.avatar.model import Avatar
from sidekick.desktop import state as state_mod
from sidekick.desktop.bubble import Bubble
from sidekick.desktop.events import StageEvents
from sidekick.desktop.image import ImageRenderer
from sidekick.desktop.menu import Item, Menu
from sidekick.desktop.overlay import SurfaceTexture
from sidekick.desktop.live2d import Live2DRenderer
from sidekick.desktop.pet_model import BasePetModel
from sidekick.desktop.spine import SpineRenderer
from sidekick.desktop.window import Window
from sidekick.motion import Context, Intent, SpeechFrame

FLAGS = pygame.DOUBLEBUF | pygame.OPENGL | pygame.NOFRAME
# 桌宠窗的**高度**基准（px）。宽度不写死：按形象自身的宽高比算——窗口去适配形象，
# 而不是形象去挤一个固定窗口。滚轮只改大小（乘在高度上），不改形状。
BASE_H = 680
MIN_W, MIN_H = 220, 300
PROVISIONAL_W = 380
# 整窗透明度的下限：再低就看不清、也容易误以为自己把桌宠弄丢了
OPACITY_MIN = 0.2


class PetApp:
    def __init__(self, avatar, args, state: dict, *, fallback=None) -> None:
        self.avatar = avatar
        self.args = args
        self.state = state
        self.fallback = fallback         # Spine 起不来时顶上的图片（可空）
        self.width = PROVISIONAL_W
        self.height = BASE_H
        self.running = True
        self.reload_requested = False
        pygame.init()
        self.bubble = Bubble()
        self.menu = Menu()
        self.overlay = SurfaceTexture()
        self.events = StageEvents(args.url)
        self.speaking = False
        self.speech: SpeechFrame | None = None
        self.speech_at = 0.0
        self.pointer: tuple[float, float] | None = None
        self._silence = 0.0
        self._last_frame = time.monotonic()
        self.drag_candidate = None
        self.dragging = False
        self.drag_start = (0, 0)
        self.drag_window = (0, 0)
        self.menu_suspended = False
        self.sdl_window = None
        self.overlay_surface = None
        self.overlay_visible = False
        self.overlay_dirty = True
        self._right_prev = False
        self.live2d_ready = False
        self._last_report = 0.0
        self._alpha_rect = None
        self._last_rect_at = 0.0
        self.rect_path = str(getattr(args, "rect", "") or "")
        self._menu_prev = False
        self._leave_model_at = 0.0
        self.autonomy_enabled = self._autonomy_enabled()
        # 需要告诉 WebUI 的一行提示（口型不可用等），连上 stage 后发一次
        self._notice = ""

    @staticmethod
    def _autonomy_enabled() -> bool:
        try:
            stage = config.load().get("stage") or {}
            return bool((stage.get("autonomy") or {}).get("enabled", True))
        except (OSError, ValueError, TypeError):
            return True

    # ── 启动 ──────────────────────────────────────────
    def start(self) -> None:
        # 必须在 SDL 创建窗口之前声明；窗口建好后再调通常会失败或只影响后续窗口。
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass
        pygame.init()
        pygame.display.gl_set_attribute(pygame.GL_DEPTH_SIZE, 24)
        pygame.display.gl_set_attribute(pygame.GL_DOUBLEBUFFER, 1)
        # 逐像素透明靠 DWM 合成窗口像素的 alpha，帧缓冲必须带 alpha（SDL 默认不给）。
        pygame.display.gl_set_attribute(pygame.GL_ALPHA_SIZE, 8)
        pygame.display.set_caption("DeepSidekick")
        pygame.display.set_mode((self.width, self.height), FLAGS, vsync=1)
        self.sdl_window = SdlWindow.from_display_module()
        self.window = Window(pygame.display.get_wm_info()["window"])
        self.window.enable_dwm_alpha()
        self.model = self._make_model()
        self._notice = str(self.model.notice or "")
        # 模型自身的缩放只由 Inspector 的「整体大小」控制；窗口大小（滚轮）改的是
        # 窗口尺寸，模型会按新尺寸重新自适应——两边都乘一遍就会撑出窗口。
        self.model.set_scale(1.0)
        self._fit_window(self._scale(), persist=False)
        self._set_topmost(bool(self.state.get("topmost", True)))
        self._place_initial()
        if bool(self.state.get("click_through", False)):
            if not self.window.set_click_through(True):
                self.state["click_through"] = False
                self._save()
        self.events.start()
        glClearColor(0.0, 0.0, 0.0, 0.0)     # 全透明底：DWM 按 alpha 合成到桌面

    def _make_model(self) -> BasePetModel:
        """组合根：按 kind 装配渲染器；Spine 起不来就退回 models 里另一张能显示的图片。"""
        if self.avatar.kind == "live2d":
            if not getattr(self, "_live2d_init_done", False):
                live2d.init()
                live2d.glInit()
                self._live2d_init_done = True
            self.live2d_ready = True
            return Live2DRenderer(self.avatar, self.width, self.height,
                                  autonomy=bool(self.autonomy_enabled))
        self.live2d_ready = False
        if self.avatar.kind == "spine":
            try:
                return SpineRenderer(self.avatar, self.width, self.height)
            except (OSError, RuntimeError, ValueError) as exc:
                if self.fallback is None:
                    raise
                print(f"[desktop] Spine 加载失败，改用图片 {self.fallback.name}：{exc}")
                return ImageRenderer(self.fallback, self.width, self.height)
        return ImageRenderer(self.avatar, self.width, self.height)

    def _reload_model(self) -> None:
        """同一窗口/GL 上下文里重建模型；不关闭进程或窗口。"""
        old = self.model
        try:
            old.close()
        except (RuntimeError, OSError, TypeError, ValueError):
            pass
        self.model = self._make_model()
        self._notice = str(self.model.notice or "")
        self.model.set_scale(1.0)
        self._fit_window(self._scale(), persist=False)
        self.pointer = None
        self.speech = None
        self.drag_candidate = None
        self.dragging = False
        self._silence = 0.0
        self._last_frame = time.monotonic()

    def _scale(self) -> float:
        try:
            return max(0.2, min(4.0, float(self.state.get("scale", 1.0))))
        except (TypeError, ValueError):
            return 1.0

    def _opacity(self) -> float:
        try:
            value = float(self.state.get("opacity", 1.0))
        except (TypeError, ValueError):
            return 1.0
        return max(OPACITY_MIN, min(1.0, value))

    def _follow_mouse(self) -> bool:
        return bool(self.state.get("follow_mouse", True))

    def _set_topmost(self, on: bool) -> None:
        # SDL 自己也会维护 ALWAYS_ON_TOP；不同步的话它可能把窗口改回
        # HWND_NOTOPMOST（表现为置顶偶尔失效）。
        if self.sdl_window is not None:
            try:
                self.sdl_window.always_on_top = bool(on)
            except (AttributeError, TypeError, ValueError):
                pass
        self.window.set_topmost(bool(on))

    def _place_initial(self) -> None:
        x = self.state.get("x")
        y = self.state.get("y")
        if isinstance(x, int) and isinstance(y, int):
            work = self.window.work_area_for(
                x + self.width // 2, y + self.height // 2)
        else:
            work = self.window.work_area()
        if not isinstance(x, int) or not isinstance(y, int):
            x = work[2] - self.width - 24
            y = work[3] - self.height - 24
        x = max(work[0], min(x, work[2] - self.width))
        y = max(work[1], min(y, work[3] - self.height))
        self.window.move(x, y)

    # ── 主循环 ────────────────────────────────────────
    def run(self) -> None:
        clock = pygame.time.Clock()
        self._last_frame = time.monotonic()
        try:
            while self.running:
                self._handle_events()
                self._poll_menu_outside()
                self._poll_right()
                self._handle_hook()
                self._handle_stage()
                if self.reload_requested:
                    self.reload_requested = False
                    self._reload_model()
                self._handle_mouth()
                self._update_pointer_follow()
                self._draw_frame()
                # 菜单开着时先把模型压暗再画菜单：拖动滑块能实时看到变化，
                # 菜单本身保持不透明才点得准。
                if self.menu.active:
                    self._dim(self._opacity())
                self._draw_overlay()
                self._present()
                # 样式切换放在 flip 之后，避免 DWM 正在采样表面时改 EXSTYLE。
                self._handle_hit_through()
                if bool(self.state.get("topmost", True)):
                    self.window.ensure_topmost_if_lost(True)
                clock.tick(60)
        finally:
            self.events.close()
            # 退出路径：模型可能已经处于半销毁状态，这里再抛只会盖住真正的退出原因
            try:
                self.model.close()
            except (RuntimeError, OSError, ValueError):
                pass
            try:
                self.window.close()
            except (AttributeError, OSError, TypeError, ValueError):
                pass
            if self.live2d_ready:
                live2d.dispose()
            pygame.quit()
            self._save()

    # ── 输入 ──────────────────────────────────────────
    def _handle_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
                continue
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                if self.menu.active:
                    self._close_menu()
                else:
                    self.running = False
                continue
            if self.menu.active:
                action = self.menu.handle(event)
                if event.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN):
                    self.overlay_dirty = True
                if action:
                    self._apply_action(*action)
                elif not self.menu.active:
                    self._after_menu()
                continue
            if event.type == pygame.MOUSEBUTTONDOWN:
                self._on_down(event)
            elif event.type == pygame.MOUSEBUTTONUP:
                self._on_up(event)
            elif event.type == pygame.MOUSEMOTION:
                self._on_motion(event)
            elif event.type == pygame.MOUSEWHEEL:
                self._on_wheel(event.y, pygame.mouse.get_pos())

    def _on_down(self, event: pygame.event.Event) -> None:
        if event.button == 3:
            if self._effective_hit_part(*event.pos):
                self._open_menu(event.pos)
            return
        if event.button != 1:
            return
        part = self._effective_hit_part(*event.pos)
        if not part:
            return
        self.drag_candidate = part
        self.dragging = False
        self.drag_start = win32api.GetCursorPos()
        self.drag_window = self.window.position()

    def _on_up(self, event: pygame.event.Event) -> None:
        if event.button != 1 or self.drag_candidate is None:
            return
        part = self.drag_candidate
        self.drag_candidate = None
        if self.dragging:
            self.dragging = False
            try:
                self.state["x"], self.state["y"] = self.window.position()
            except Exception:
                pass
            self._save()
            return
        self.events.send({"t": "click", "part": part})

    def _on_motion(self, event: pygame.event.Event) -> None:
        # 鼠标跟随不在这里做：WS_EX_TRANSPARENT 会让 Pygame 收不到
        # MOUSEMOTION，必须每帧用 Win32 光标位置更新，见 _update_pointer_follow。
        if self.drag_candidate is not None:
            mouse_x, mouse_y = win32api.GetCursorPos()
            dx = mouse_x - self.drag_start[0]
            dy = mouse_y - self.drag_start[1]
            if abs(dx) + abs(dy) > 4:
                self.dragging = True
            if self.dragging:
                self.window.move(
                    self.drag_window[0] + dx, self.drag_window[1] + dy)

    def _update_pointer_follow(self) -> None:
        """每帧从 Win32 取光标，避免透明窗口截断 Pygame 鼠标移动事件。"""
        if not self._follow_mouse() or self.window is None:
            self.pointer = None
            return
        pos = self.window.cursor_client_pos()
        if pos is None:
            return
        cx, cy = pos
        self.model.look_at(cx, cy)
        model_ptr = getattr(self.model, "pointer", None)
        if model_ptr is not None:
            self.pointer = model_ptr
        else:
            w = max(1, self.width)
            h = max(1, self.height)
            self.pointer = (
                max(-1.0, min(1.0, (cx / w) * 2.0 - 1.0)),
                max(-1.0, min(1.0, 1.0 - (cy / h) * 2.0)),
            )

    def _on_wheel(self, delta: int, pos: tuple[int, int]) -> None:
        if not self._effective_hit_part(*pos):
            return
        self._set_scale(self._scale() * (1.1 if delta > 0 else 1 / 1.1))

    def _poll_menu_outside(self) -> None:
        """菜单开着但点在窗口外时，Pygame 收不到事件；用全局按键兜底关闭。"""
        if not self.menu.active:
            self._menu_prev = False
            return
        pressed = bool(
            win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000
            or win32api.GetAsyncKeyState(win32con.VK_RBUTTON) & 0x8000)
        if pressed and not self._menu_prev:
            sx, sy = win32api.GetCursorPos()
            x, y = self.window.screen_to_client(sx, sy)
            if not (0 <= x < self.width and 0 <= y < self.height):
                self._close_menu()
        self._menu_prev = pressed

    def _poll_right(self) -> None:
        """左键穿透时窗口收不到右键，用全局按键状态保留右键菜单。"""
        if not self.window.click_through:
            self._right_prev = False
            return
        pressed = bool(
            win32api.GetAsyncKeyState(win32con.VK_RBUTTON) & 0x8000)
        if pressed and not self._right_prev and not self.menu.active:
            sx, sy = win32api.GetCursorPos()
            x, y = self.window.screen_to_client(sx, sy)
            if 0 <= x < self.width and 0 <= y < self.height:
                self.menu_suspended = self.window.suspend_click_through()
                self._open_menu((x, y))
        self._right_prev = pressed

    def _handle_hook(self) -> None:
        if not self.window.click_through:
            return
        for item in self.window.hook.poll():
            kind = item[0]
            if kind != "wheel":
                continue
            delta, sx, sy = item[1], item[2], item[3]
            x, y = self.window.screen_to_client(sx, sy)
            if self._effective_hit_part(x, y):
                self._set_scale(
                    self._scale() * (1.1 if delta > 0 else 1 / 1.1))

    # ── 菜单 ──────────────────────────────────────────
    def _open_menu(self, pos: tuple[int, int]) -> None:
        self.window.set_hit_transparent(False)
        self.menu.open(pos, self._menu_items())
        self.window.set_menu_open(True)
        self.overlay_dirty = True

    def _menu_items(self) -> list[Item]:
        opacity = [Item("", "opacity", slider=self._opacity(),
                        slider_min=OPACITY_MIN)]
        groups = sorted(self.model.motion_groups)
        motions = [Item(g, "motion", g) for g in groups[:12]]
        items = [
            Item("透明度", "noop", children=opacity),
            Item("置顶", "topmost", checked=bool(self.state.get("topmost", True))),
            Item("跟随鼠标", "follow_mouse", checked=self._follow_mouse()),
            Item("左键穿透", "click_through",
                 checked=bool(self.state.get("click_through", False))),
        ]
        if motions:
            items.append(Item("动作", "noop", children=motions))
        items.append(Item("退出", "quit"))
        return items

    def _apply_action(self, action: str, value) -> None:
        if action == "opacity":
            # 滑块连续拖动：只改状态、不关菜单，松手或菜单关闭时再落盘
            value = max(OPACITY_MIN, min(1.0, float(value)))
            self.state["opacity"] = value
            self.overlay_dirty = True
            return
        elif action == "topmost":
            self.state["topmost"] = not bool(self.state.get("topmost", True))
            self._set_topmost(self.state["topmost"])
        elif action == "follow_mouse":
            self.state["follow_mouse"] = not self._follow_mouse()
            if not self.state["follow_mouse"]:
                self.pointer = None
                self.model.reset_drag()
        elif action == "click_through":
            self.state["click_through"] = not bool(
                self.state.get("click_through", False))
            self._save()
            if self.state["click_through"]:
                self.menu_suspended = False
                if not self.window.set_click_through(True):
                    self.state["click_through"] = False
                    self._save()
            else:
                self.window.set_click_through(False)
            self._close_menu()
            return
        elif action == "motion":
            self.model.start_motion(str(value))
        elif action == "quit":
            self.running = False
        self._save()
        self._close_menu()

    def _close_menu(self) -> None:
        self.menu.close()
        self.overlay_dirty = True
        self._save()          # 滑块是连续改的，关菜单时统一落盘
        self._after_menu()

    def _after_menu(self) -> None:
        self.window.set_menu_open(False)
        self._save()  # 菜单内部关闭（点外面/右键）也要落盘
        if self.menu_suspended:
            self.menu_suspended = False
            self.window.resume_click_through()

    # ── stage 事件 / 气泡 ─────────────────────────────
    def _handle_stage(self) -> None:
        while True:
            try:
                data = self.events.queue.get_nowait()
            except queue.Empty:
                return
            kind = data.get("t")
            if kind == "connected":
                if self._notice:
                    self.events.send({"t": "notice", "text": self._notice})
                # stage 重启/断线后不会再补 end：清掉说话态，气泡进入停留。
                self.speaking = False
                self.speech = None
                self.bubble.finish(1)
                self._send_report(force=True)
                continue
            if kind == "status":
                self.bubble.status(str(data.get("text") or ""))
                self.speaking = False
                self.overlay_dirty = True
            elif kind == "say":
                try:
                    duration = float(data.get("duration") or 0.0)
                except (TypeError, ValueError):
                    duration = 0.0
                self.bubble.show(
                    data.get("pages") or [], data.get("cps", 4.6),
                    sync=bool(data.get("sync", False)), duration=duration)
                self.speaking = True
                self.speech = None
                acts = data.get("acts")
                self.model.apply_acts(acts)
                self._submit_acts(acts, page=0)
                self.overlay_dirty = True
            elif kind == "page":
                acts = data.get("acts")
                if acts:
                    self.model.apply_acts(acts)
                    self._submit_acts(acts, page=int(data.get("idx") or 0))
                try:
                    duration = float(data.get("duration") or 0.0)
                except (TypeError, ValueError):
                    duration = 0.0
                self.bubble.page(int(data.get("idx") or 0), duration)
                self.overlay_dirty = True
            elif kind == "end":
                self.speaking = False
                self.speech = None
                self.bubble.finish(data.get("upto"))
                self.overlay_dirty = True
            elif kind == "mouth":
                self._set_mouth(data)
            elif kind == "level":
                # 保留协议；桌面口型已经走 50 Hz mouth 帧。
                pass
            elif kind == "event":
                self.model.motion_host.submit([
                    Intent("emotion", "happy", 0.9, None, "event"),
                    Intent("action", "laugh", 0.85, None, "event"),
                ])
            elif kind == "control":
                try:
                    self._apply_control(data)
                except (TypeError, ValueError):
                    # /control 是外部输入，坏类型只丢这一帧，不能带崩桌宠。
                    pass

    def _submit_acts(self, acts, *, page: int) -> None:
        intents = []
        for act in acts or []:
            if not isinstance(act, dict):
                continue
            kind = str(act.get("kind") or "")
            if kind not in ("emotion", "action", "gaze"):
                continue
            if int(act.get("page") or 0) != page:
                continue
            try:
                intensity = float(act.get("intensity", 1.0))
            except (TypeError, ValueError):
                intensity = 1.0
            intents.append(Intent(kind, str(act.get("name") or ""),
                                  intensity, None, "text"))
            if kind == "emotion":
                self.model.express_emotion(str(act.get("name") or ""))
        if intents:
            self.model.motion_host.submit(intents)

    def get_pet_alpha_rect(self):
        """当前可见像素的桌面坐标包围盒；只读，读不到返回 None。"""
        return self._alpha_rect

    def _update_alpha_rect(self, now: float) -> None:
        """低频读回帧缓冲 alpha，算可见包围盒并写给 runtime 的 capture 线程。"""
        rect_path = getattr(self, "rect_path", "")
        if not rect_path or now - getattr(self, "_last_rect_at", 0.0) < 1.0:
            return
        self._last_rect_at = now
        try:
            import numpy as np

            raw = glReadPixels(0, 0, self.width, self.height,
                               GL_RGBA, GL_UNSIGNED_BYTE)
            alpha = np.asarray(raw, dtype=np.uint8).reshape(
                self.height, self.width, 4)[:, :, 3]
            ys, xs = np.nonzero(alpha > 8)
            if len(xs) == 0:
                rect = None
            else:
                left, right = int(xs.min()), int(xs.max())
                bottom, top = int(ys.max()), int(ys.min())
                x, y = self.window.position()
                rect = [int(x) + left, int(y) + (self.height - 1 - bottom),
                        right - left + 1, bottom - top + 1]
            self._alpha_rect = tuple(rect) if rect else None
            tmp = self.rect_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "rect": rect}, f)
            os.replace(tmp, self.rect_path)
        except Exception:
            # 读回失败不影响桌宠渲染；capture 会退化为不降权。
            return

    def _send_report(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_report < 2.0:
            return
        self._last_report = now
        try:
            report = dict(self.model.motion_host.report())
        except Exception as exc:
            report = {"enabled": bool(self.autonomy_enabled), "active": False,
                      "error": f"{type(exc).__name__}: {exc}"}
        report["mouth"] = round(
            float(self.speech.openness) if self.speech is not None else 0.0, 3)
        self.events.send({
            "t": "report",
            "renderer": type(self.model).__name__,
            "animation": str(getattr(self.model, "current_animation", "") or ""),
            "version": str(getattr(self.model, "version", "") or ""),
            "autonomy": report,
        })

    def _set_mouth(self, data: dict) -> None:
        try:
            now = time.monotonic()
            self.speech = SpeechFrame(
                t=now,
                level=max(0.0, min(1.0, float(data.get("v") or 0.0))),
                openness=max(0.0, min(1.0, float(data.get("open") or 0.0))),
                form=max(-1.0, min(1.0, float(data.get("form") or 0.0))),
                voiced=bool(data.get("voiced")),
            )
            self.speech_at = now
        except (TypeError, ValueError):
            return

    def _apply_control(self, data: dict) -> None:
        op = str(data.get("op") or "")
        if op == "param":
            self.model.control_param(data.get("id"), data.get("value"))
        elif op == "part":
            self.model.control_part(data.get("id"), data.get("value"))
        elif op == "motion":
            self.model.control_motion(
                str(data.get("group") or ""), data.get("index") or 0)
        elif op == "expression":
            self.model.control_expression(data.get("name"))
        elif op == "view":
            self.model.control_view(
                data.get("zoom"), data.get("offX"), data.get("offY"))
        elif op == "reset":
            self.model.reset_controls()
        elif op == "reload":
            # 在同一个 PetApp/GL 上下文里重建模型，不关闭窗口。
            self.reload_requested = True

    def _handle_mouth(self) -> None:
        if self.speech is not None and time.monotonic() - self.speech_at > 0.199:
            self.speech = None

    # ── 命中 / 穿透 ───────────────────────────────────
    def _effective_hit_part(self, x: float, y: float) -> str:
        """鼠标事件用的命中部位；没有命中网格时返回空串。"""
        return self.model.hit_part(x, y)

    def _handle_hit_through(self) -> None:
        """智能穿透：模型内可点，空白跨进程穿透。

        窗口已常驻 WS_EX_LAYERED；只切换 WS_EX_TRANSPARENT 位，不调用
        SWP_FRAMECHANGED。离开模型后延迟 120ms 才穿透，避免角色呼吸/发梢
        边缘让命中在真/假之间反复跳变（这是鼠标划过抽动的直接来源）。
        """
        if self.window.click_through:
            return
        if self.menu.active or self.dragging or self.drag_candidate is not None:
            self._leave_model_at = 0.0
            self.window.set_hit_transparent(False)
            return

        pos = self.window.cursor_client_pos()
        if pos is None:
            self._leave_model_at = 0.0
            self.window.set_hit_transparent(True)
            return
        cx, cy = pos
        in_client = 0 <= cx < self.width and 0 <= cy < self.height
        hit_model = in_client and bool(self.model.hit_part(cx, cy))
        now = time.monotonic()
        if hit_model:
            self._leave_model_at = 0.0
            self.window.set_hit_transparent(False)
        elif self._leave_model_at <= 0.0:
            self._leave_model_at = now
        elif now - self._leave_model_at >= 0.12:
            self.window.set_hit_transparent(True)

    # ── 绘制 / 缩放 ───────────────────────────────────
    def _draw_frame(self) -> None:
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        if self.live2d_ready:
            live2d.clearBuffer()
        now = time.monotonic()
        dt = min(0.1, max(0.0, now - getattr(self, "_last_frame", now)))
        self._last_frame = now
        if self.speaking:
            self._silence = 0.0
        else:
            self._silence += dt
        # 1. SDK 先推进 motion/expression/physics。
        self.model.update(dt)
        # 2. 程序化引擎只写归一化姿态，宿主内部完成参数所有权过滤。
        context = Context(
            speaking=bool(self.speaking),
            silence=self._silence,
            pointer=self.pointer if self._follow_mouse() else None,
        )
        params = self.model.motion_host.tick(
            now, dt, speech=self.speech, context=context)
        self.model.motion_host.write(params)
        if not self.model.motion_host.enabled:
            # 自主驱动关闭：回到单参数口型；仍然是 Update 后、Draw 前。
            self.model.set_mouth(
                self.speech.openness if self.speech is not None else 0.0)
        # Inspector 最后覆盖；模型写入必须在 Update 之后、Draw 之前。
        self.model.apply_controls()
        self.model.draw()
        self._update_alpha_rect(now)
        self._send_report()

    def _present(self) -> None:
        """整窗透明度在这里乘进帧缓冲，然后交给 DWM 合成。"""
        if not self.menu.active:
            # 菜单开着时已经在画菜单之前压过暗了，这里不能再乘一次
            self._dim(self._opacity())
        pygame.display.flip()

    def _dim(self, k: float) -> None:
        """把已经画好的像素整体乘 k（RGB 和 alpha 一起），实现整窗透明。"""
        if k >= 0.999:
            return
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        glOrtho(0.0, 1.0, 1.0, 0.0, -1.0, 1.0)
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()
        glDisable(GL_DEPTH_TEST)
        glDisable(GL_TEXTURE_2D)
        glEnable(GL_BLEND)
        glBlendFunc(GL_ZERO, GL_SRC_ALPHA)     # dst *= k
        glColor4f(0.0, 0.0, 0.0, k)
        glBegin(GL_QUADS)
        glVertex2f(0.0, 0.0)
        glVertex2f(1.0, 0.0)
        glVertex2f(1.0, 1.0)
        glVertex2f(0.0, 1.0)
        glEnd()

    def _draw_overlay(self) -> None:
        if self.bubble.visible and self.bubble.tick():
            self.overlay_dirty = True
        if self.bubble.visible and self.bubble.animating:
            self.overlay_dirty = True
        if self.bubble.visible and self.bubble.expired():
            self.bubble.visible = False
            self.overlay_dirty = True
        if not (self.bubble.visible or self.menu.active):
            self.overlay_visible = False
            return
        if self.overlay_dirty or self.overlay_surface is None:
            surface = pygame.Surface(
                (self.width, self.height), pygame.SRCALPHA)
            self.bubble.draw(surface)
            self.menu.draw(surface)
            self.overlay.upload(surface)
            self.overlay_surface = surface
            self.overlay_dirty = False
            self.overlay_visible = bool(
                self.bubble.visible or self.menu.active)
        if self.overlay_visible:
            self.overlay.draw(self.width, self.height)

    def _set_scale(self, scale: float) -> None:
        self._fit_window(scale)

    def _fit_window(self, scale: float, *, persist: bool = True) -> None:
        """把窗口形状改成形象的形状，再按 scale 缩放。

        「形象适配窗口」的反面：窗口去适配形象。宽模型不会被裁、窄模型也不会
        在两侧留两条黑边——素材的宽高比是什么，窗户就是什么比例。
        """
        scale = max(0.2, min(4.0, float(scale)))
        aspect = max(0.2, min(4.0, float(getattr(self.model, "aspect", 0.75))))
        sx, sy = self.state.get("x"), self.state.get("y")
        if isinstance(sx, int) and isinstance(sy, int):
            work = self.window.work_area_for(
                sx + self.width // 2, sy + self.height // 2)
        else:
            work = self.window.work_area()
        max_w = max(MIN_W, work[2] - work[0] - 32)
        max_h = max(MIN_H, work[3] - work[1] - 32)
        want_h = BASE_H * scale
        want_w = want_h * aspect
        fit = min(1.0, max_w / max(1.0, want_w), max_h / max(1.0, want_h))
        # 被工作区/最小窗口夹住时，实际生效的缩放是 scale*fit；最小窗口
        # 本身也有一个下限，否则缩到最小后继续滚会攒下“按了没反应”的隐藏缩放。
        min_scale = max(MIN_H / BASE_H, MIN_W / max(1.0, BASE_H * aspect))
        effective = max(min_scale, min(4.0, scale * fit))
        new_w = max(MIN_W, round(want_w * fit))
        new_h = max(MIN_H, round(want_h * fit))
        if (new_w, new_h) == (self.width, self.height):
            if persist and abs(effective - self._scale()) > 0.001:
                self.state["scale"] = effective
                self._save()
            return
        old_w, old_h = self.width, self.height
        old_x, old_y = self.window.position()
        new_x = old_x + (old_w - new_w) // 2
        new_y = old_y + old_h - new_h
        new_x = max(work[0], min(new_x, work[2] - new_w))
        new_y = max(work[1], min(new_y, work[3] - new_h))
        self.sdl_window.size = (new_w, new_h)
        self.window.resize(new_x, new_y, new_w, new_h)
        self.width, self.height = new_w, new_h
        glViewport(0, 0, new_w, new_h)
        self.model.resize(new_w, new_h)
        self.overlay_surface = None
        self.overlay_dirty = True
        if persist:
            self.state["scale"] = effective
            self.state["x"], self.state["y"] = new_x, new_y
            self._save()

    # ── 状态 ──────────────────────────────────────────
    def _save(self) -> None:
        state_mod.save(self.args.state, self.state)


def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--avatar", default="")
    parser.add_argument("--state", default="")
    parser.add_argument("--rect", default="")
    return parser.parse_args(argv)


def _fallback_image(catalog) -> Avatar | None:
    """Spine 起不来时的替补：models 里第一张能显示的图片；一张都没有就返回 None。"""
    return next((a for a in catalog.discover()
                 if a.kind == "image" and a.usable), None)


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    catalog = AvatarCatalog(
        config.ROOT,
        selection=lambda: str((config.load().get("role") or {}).get("id") or ""))
    try:
        avatar = catalog.get(args.avatar) if args.avatar else catalog.selected()
    except LookupError as exc:
        print(f"[desktop] {exc}（models/ 里至少要放一套形象）")
        return 2
    if avatar is None or avatar.kind not in ("live2d", "spine", "image"):
        print(f"[desktop] 当前形象不可用：{getattr(avatar, 'id', '?')}")
        return 2
    state = state_mod.load(args.state)
    app = PetApp(
        avatar, args, state,
        fallback=(_fallback_image(catalog) if avatar.kind == "spine"
                  else None))
    # 进程边界：这里的宽捕获是为了把任何崩溃变成"堆栈 + 退出码 1"，
    # 否则桌宠会静默消失，runtime 那边只看得到一个突然结束的子进程。
    try:
        app.start()
        app.run()
    except Exception:
        import traceback

        traceback.print_exc()
        return 1
    return 0
