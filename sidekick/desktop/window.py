"""Windows 透明窗/置顶/点击穿透，以及左键穿透时的全局鼠标钩子。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading

import win32api
import win32con
import win32gui

_WH_MOUSE_LL = 14
_WM_MOUSEWHEEL = 0x020A
_WM_QUIT = 0x0012
_WM_ERASEBKGND = 0x0014
_WM_MOUSEACTIVATE = 0x0021
_MA_NOACTIVATE = 3
_GWLP_WNDPROC = -4

# 跨进程命中只认 WS_EX_LAYERED | WS_EX_TRANSPARENT；单 WS_EX_TRANSPARENT
# 只影响同进程兄弟窗口，WM_NCHITTEST/HTTRANSPARENT 也只保证同线程转交。
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_LWA_ALPHA = 0x00000002

_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class _POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", _POINT),
        ("mouseData", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("message", wt.UINT),
        ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM),
        ("time", wt.DWORD),
        ("pt", _POINT),
    ]


class MouseHook:
    """左键穿透时窗口收不到鼠标事件，用低级钩子保留右键和滚轮。"""

    def __init__(self) -> None:
        self.events: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._ok = False
        self._hook = None
        self._proc = None
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return self._ok
        self._ready.clear()
        self._ok = False
        self._thread = threading.Thread(
            target=self._run, name="desktop-mouse-hook", daemon=True)
        self._thread.start()
        self._ready.wait(1.0)
        return self._ok

    def _run(self) -> None:
        user32 = self._user32
        self._thread_id = int(self._kernel32.GetCurrentThreadId())
        hook_proc = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, hook_proc, wt.HINSTANCE, wt.DWORD]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(_MSG), wt.HWND, wt.UINT, wt.UINT]
        user32.GetMessageW.restype = wt.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]

        def callback(n_code, w_param, l_param):
            if n_code != 0:
                return user32.CallNextHookEx(None, n_code, w_param, l_param)
            # 全局钩子对每次鼠标移动都会回调；只解析滚轮，右键由
            # _poll_right() 的按键状态负责。
            if w_param != _WM_MOUSEWHEEL:
                return user32.CallNextHookEx(None, n_code, w_param, l_param)
            info = ctypes.cast(
                l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            delta = ctypes.c_short(
                (int(info.mouseData) >> 16) & 0xFFFF).value
            self.events.put(
                ("wheel", int(delta), int(info.pt.x), int(info.pt.y)))
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        self._proc = hook_proc(callback)
        self._hook = user32.SetWindowsHookExW(
            _WH_MOUSE_LL, self._proc, None, 0)
        self._ok = bool(self._hook)
        self._ready.set()
        if not self._hook:
            return
        msg = _MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            # 线程退出路径：钩子可能已经随线程被系统回收
            try:
                user32.UnhookWindowsHookEx(self._hook)
            except (OSError, TypeError):
                pass
            self._hook = None
            self._ok = False

    def stop(self) -> None:
        if self._thread_id:
            try:
                self._user32.PostThreadMessageW(
                    self._thread_id, _WM_QUIT, 0, 0)
            except (OSError, TypeError):
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        self._thread = None
        self._thread_id = 0
        self._ok = False

    def poll(self) -> list[tuple]:
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out



class _MARGINS(ctypes.Structure):
    _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]


_DWM_EXTEND = ctypes.WinDLL("dwmapi", use_last_error=True)


class Window:
    def __init__(self, hwnd: int) -> None:
        self.hwnd = int(hwnd)
        self.hook = MouseHook()
        self.click_through = False
        self._hit_transparent = False
        self._menu_open = False
        self._suspended = False

        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.CallWindowProcW.argtypes = [
            ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        self._user32.CallWindowProcW.restype = ctypes.c_ssize_t
        self._user32.SetWindowLongPtrW.argtypes = [
            wt.HWND, ctypes.c_int, ctypes.c_void_p]
        self._user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        self._user32.SetLayeredWindowAttributes.argtypes = [
            wt.HWND, wt.DWORD, wt.BYTE, wt.DWORD]
        self._user32.SetLayeredWindowAttributes.restype = wt.BOOL

        # `WS_EX_LAYERED` 是跨进程鼠标穿透的前提；不设置它时
        # `WS_EX_TRANSPARENT` 只影响同进程兄弟窗口。必须立即用
        # `SetLayeredWindowAttributes(LWA_ALPHA)` 初始化，否则 layered
        # 窗口默认完全不可见；这不使用 UpdateLayeredWindow，OpenGL/DWM
        # per-pixel alpha 仍然有效。
        base_style = win32gui.GetWindowLong(
            self.hwnd, win32con.GWL_EXSTYLE)
        win32gui.SetWindowLong(
            self.hwnd, win32con.GWL_EXSTYLE,
            base_style | _WS_EX_TOOLWINDOW | _WS_EX_LAYERED
            | _WS_EX_NOACTIVATE)
        self._layered = bool(self._user32.SetLayeredWindowAttributes(
            wt.HWND(self.hwnd), 0, 255, _LWA_ALPHA))
        if not self._layered:
            # 极端驱动上 layered 初始化失败：先摘掉位，避免窗口完全不可见。
            cur = win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(
                self.hwnd, win32con.GWL_EXSTYLE, cur & ~_WS_EX_LAYERED)

        self._wndproc_ref = _WNDPROC(self._on_message)
        self._old_proc = self._user32.SetWindowLongPtrW(
            self.hwnd, _GWLP_WNDPROC,
            ctypes.cast(self._wndproc_ref, ctypes.c_void_p))

    def set_menu_open(self, open_: bool) -> None:
        """菜单打开时整窗可点，否则子菜单/滑块会被穿透掉。"""
        self._menu_open = bool(open_)
        self._sync_exstyle()

    def _sync_exstyle(self) -> None:
        """只改 GWL_EXSTYLE 的穿透位，绝不 SWP_FRAMECHANGED。

        `SWP_FRAMECHANGED` 会让 DWM 重算非客户区/重建重定向表面，在
        OpenGL 透明窗上表现就是整窗抽动一帧。
        """
        want_transparent = (not self._menu_open) and (
            (self.click_through and not self._suspended)
            or self._hit_transparent)
        cur = win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
        new = cur | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE
        if self._layered:
            new |= _WS_EX_LAYERED
        if want_transparent:
            new |= _WS_EX_TRANSPARENT
        else:
            new &= ~_WS_EX_TRANSPARENT
        if new != cur:
            win32gui.SetWindowLong(self.hwnd, win32con.GWL_EXSTYLE, new)

    def _on_message(self, hwnd, msg, wparam, lparam):
        # 透明窗不需要默认背景擦除；让 SDL/DWM 擦一次 HDC 会闪一帧。
        if msg == _WM_ERASEBKGND:
            return 1
        # 点击桌宠不抢前台焦点；鼠标消息仍交给 Pygame，Brain/拖拽正常。
        if msg == _WM_MOUSEACTIVATE:
            return _MA_NOACTIVATE
        # 命中完全交给 layered/transparent 位；这里不再返回 HTTRANSPARENT，
        # 也不在窗口消息路径里调用模型。
        return self._user32.CallWindowProcW(
            ctypes.c_void_p(self._old_proc), hwnd, msg, wparam, lparam)

    def enable_dwm_alpha(self) -> None:
        """让 DWM 按窗口像素的 alpha 合成。

        窗口已启用 `WS_EX_LAYERED` 并用 `SetLayeredWindowAttributes(255)`
        初始化；这与 `DwmExtendFrameIntoClientArea` 的 per-pixel alpha
        可以共存（不使用 UpdateLayeredWindow）。
        """
        margins = _MARGINS(-1, -1, -1, -1)
        _DWM_EXTEND.DwmExtendFrameIntoClientArea(
            wt.HWND(self.hwnd), ctypes.byref(margins))

    def set_topmost(self, on: bool) -> None:
        win32gui.SetWindowPos(
            self.hwnd,
            win32con.HWND_TOPMOST if on else win32con.HWND_NOTOPMOST,
            0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
            | win32con.SWP_NOACTIVATE | win32con.SWP_NOOWNERZORDER)

    def ensure_topmost_if_lost(self, want: bool) -> None:
        """只读检查 WS_EX_TOPMOST；只有真的丢了才补一次 SetWindowPos。"""
        if not want:
            return
        cur = win32gui.GetWindowLong(self.hwnd, win32con.GWL_EXSTYLE)
        if not (cur & win32con.WS_EX_TOPMOST):
            self.set_topmost(True)

    def set_click_through(self, on: bool) -> bool:
        self.click_through = bool(on)
        self._hit_transparent = bool(on)
        self._suspended = False
        if on:
            if not self.hook.start():
                self.click_through = False
                self._hit_transparent = False
                self._sync_exstyle()
                return False
        else:
            self.hook.stop()
        self._sync_exstyle()
        return True

    def set_hit_transparent(self, on: bool) -> None:
        """只改 WS_EX_TRANSPARENT 位；layered 内核会据此跨进程跳过窗口。"""
        on = bool(on)
        if on == self._hit_transparent:
            return
        self._hit_transparent = on
        self._sync_exstyle()

    def suspend_click_through(self) -> bool:
        """打开右键菜单时临时恢复交互，菜单关闭后再恢复穿透。"""
        if not self.click_through:
            return False
        self._suspended = True
        self._sync_exstyle()
        return True

    def resume_click_through(self) -> None:
        if not self.click_through:
            return
        self._suspended = False
        self._sync_exstyle()

    def move(self, x: int, y: int) -> None:
        win32gui.SetWindowPos(
            self.hwnd, 0, int(x), int(y), 0, 0,
            win32con.SWP_NOSIZE | win32con.SWP_NOZORDER)

    def position(self) -> tuple[int, int]:
        rect = win32gui.GetWindowRect(self.hwnd)
        return int(rect[0]), int(rect[1])

    def resize(self, x: int, y: int, width: int, height: int) -> None:
        win32gui.SetWindowPos(
            self.hwnd, 0, int(x), int(y), int(width), int(height),
            win32con.SWP_NOZORDER)

    def work_area(self) -> tuple[int, int, int, int]:
        monitor = win32api.MonitorFromWindow(
            self.hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        return tuple(win32api.GetMonitorInfo(monitor)["Work"])

    def work_area_for(self, x: int, y: int) -> tuple[int, int, int, int]:
        """按屏幕坐标选显示器；用于恢复保存的副屏位置。"""
        try:
            monitor = win32api.MonitorFromPoint(
                (int(x), int(y)), win32con.MONITOR_DEFAULTTONEAREST)
            return tuple(win32api.GetMonitorInfo(monitor)["Work"])
        except Exception:
            return self.work_area()

    def screen_to_client(self, x: int, y: int) -> tuple[int, int]:
        return win32gui.ScreenToClient(self.hwnd, (int(x), int(y)))

    def cursor_client_pos(self) -> tuple[int, int] | None:
        try:
            sx, sy = win32api.GetCursorPos()
            return self.screen_to_client(sx, sy)
        except (OSError, TypeError, ValueError):
            return None

    def close(self) -> None:
        """恢复 WNDPROC、停钩子；窗口销毁前必须调用。"""
        try:
            self.hook.stop()
        except (OSError, TypeError):
            pass
        old = getattr(self, "_old_proc", None)
        if old:
            try:
                self._user32.SetWindowLongPtrW(
                    self.hwnd, _GWLP_WNDPROC,
                    ctypes.c_void_p(int(old)))
            except (OSError, TypeError, ValueError):
                pass
            self._old_proc = None
