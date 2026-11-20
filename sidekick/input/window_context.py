"""前台窗口上下文与隐私护栏。

`WindowContext` / `NullWindowContext` 成对实现：组合根按平台选一个注入，
`Scheduler` / `Runtime` 不判断窗口标题是否开启；非隐私窗口自动注入。

隐私护栏是**进程级**黑名单：命中后 capture 线程跳过截屏并清空队列。
它覆盖不到浏览器里的网银、邮箱、私聊页面，标题关键词也不能承诺全防住。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import threading
from dataclasses import dataclass
from pathlib import Path

PRIVACY_NOTE = "当前是隐私保护窗口，没有截图"

#: 标题只发前 40 个字符；再长就可能把表格名、医院报告之类带进云端。
TITLE_MAX = 40


@dataclass(frozen=True)
class WindowSnapshot:
    """一次前台窗口采样；只读数据，不含窗口句柄。"""

    title: str
    process: str
    fullscreen: bool = False


class NullWindowContext:
    """无窗口上下文（非 Windows/测试）：不注入标题，也不触发隐私停抓。"""

    privacy_active = False

    def sample(self):
        return None

    def material_lines(self) -> tuple[str, str]:
        return "", ""

    def reload(self, cfg: dict) -> None:
        return None

    def stats(self) -> dict:
        return {"privacy_active": False, "process": "", "fullscreen": False,
                "error": None}


class WindowContext:
    """Win32 前台窗口采样；每 2 秒由 capture 线程调一次 `sample()`。"""

    def __init__(self, cfg: dict, *, probe=None) -> None:
        c = cfg.get("capture") or {}
        monitor = max(0, int(c.get("monitor") or 0))
        self._privacy = _privacy_set(c.get("privacy_processes"))
        self._probe = probe or (lambda: _probe_win32(monitor))
        self._lock = threading.Lock()
        self._snapshot: WindowSnapshot | None = None
        self._privacy_active = False
        self._error: str | None = None

    @property
    def privacy_active(self) -> bool:
        with self._lock:
            return self._privacy_active

    def sample(self):
        """采一次前台窗口；异常只记错误，沿用上一次状态。"""
        try:
            snapshot = self._probe()
        except Exception as e:
            with self._lock:
                self._error = f"{type(e).__name__}: {e}"
            return None
        if snapshot is None:
            # 不拿旧标题冒充当前前台窗口；隐私态保留（失败时宁可继续停抓）。
            with self._lock:
                self._snapshot = None
            return None
        if not isinstance(snapshot, WindowSnapshot):
            return None
        private = _is_private(snapshot.process, self._privacy)
        with self._lock:
            self._snapshot = snapshot
            self._privacy_active = private
            self._error = None
        return snapshot

    def material_lines(self) -> tuple[str, str]:
        """给材料的 (标题行, 隐私提示)；隐私命中时只返回提示。"""
        with self._lock:
            private = self._privacy_active
            snapshot = self._snapshot
        if private:
            return "", PRIVACY_NOTE
        if snapshot is None:
            return "", ""
        title = _one_line(snapshot.title)[:TITLE_MAX]
        process = _one_line(snapshot.process)
        if title and process:
            body = f"{title} ({process})"
        else:
            body = title or process
        return (f"[前台窗口] {body}", "") if body else ("", "")

    def reload(self, cfg: dict) -> None:
        c = cfg.get("capture") or {}
        with self._lock:
            self._privacy = _privacy_set(c.get("privacy_processes"))
        # probe 的 monitor 参数在创建时绑定；reload 只更新隐私名单。
        # 显示器编号运行中不热切，避免 dxcam 与窗口上下文各看一块屏。

    def stats(self) -> dict:
        with self._lock:
            snapshot = self._snapshot
            return {
                "privacy_active": self._privacy_active,
                "process": snapshot.process if snapshot else "",
                "fullscreen": bool(snapshot.fullscreen) if snapshot else False,
                "error": self._error,
            }


def _one_line(value: str) -> str:
    return " ".join(str(value or "").split())


def _privacy_set(value) -> set[str]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = []
    out = set()
    for item in items:
        name = _process_name(item)
        if name:
            out.add(name)
    return out


def _process_name(value) -> str:
    name = Path(str(value or "").strip().replace("\\", "/")).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def _is_private(process: str, names: set[str]) -> bool:
    name = _process_name(process)
    return bool(name and name in names)


def _probe_win32(monitor: int) -> WindowSnapshot | None:
    if os.name != "nt":
        return None
    api = _win32()
    if api is None:
        return None
    user32, kernel32 = api
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None

    title = _window_title(user32, hwnd)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process = _process_path(kernel32, pid.value)
    fullscreen = _is_fullscreen(user32, hwnd, monitor)
    return WindowSnapshot(title=title, process=process,
                          fullscreen=fullscreen)


def _window_title(user32, hwnd) -> str:
    """SendMessageTimeoutW：无响应窗口最多等 50ms，不拖住 capture 线程。"""
    buf = ctypes.create_unicode_buffer(512)
    result = ctypes.c_size_t()
    ok = user32.SendMessageTimeoutW(
        hwnd, 0x000D, len(buf), ctypes.addressof(buf), 0x0002, 50,
        ctypes.byref(result))
    return buf.value if ok else ""


def _process_path(kernel32, pid: int) -> str:
    if not pid:
        return ""
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        kernel32.CloseHandle(handle)


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


_MONITORENUMPROC = (ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
    ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    if hasattr(ctypes, "WINFUNCTYPE") else None)


def _is_fullscreen(user32, hwnd, monitor: int) -> bool:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    target = _target_monitor(user32, monitor)
    if target is None:
        return False
    tx, ty, tr, tb = target
    ix = max(0, min(rect.right, tr) - max(rect.left, tx))
    iy = max(0, min(rect.bottom, tb) - max(rect.top, ty))
    area = max(1, (tr - tx) * (tb - ty))
    return (ix * iy) / area >= 0.98


def _target_monitor(user32, index: int):
    monitors: list[tuple[int, int, int, int]] = []

    def collect(hmon, _hdc, _rect, _data):
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            monitors.append((r.left, r.top, r.right, r.bottom))
        return True

    callback = _MONITORENUMPROC(collect)
    user32.EnumDisplayMonitors(None, None, callback, 0)
    if not monitors:
        return None
    return monitors[max(0, min(int(index), len(monitors) - 1))]


def _win32():
    """懒加载并固定函数签名；只在 Windows 调用。"""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        user32.SendMessageTimeoutW.restype = wintypes.LPARAM
        user32.GetWindowRect.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.EnumDisplayMonitors.argtypes = [
            wintypes.HDC, ctypes.POINTER(wintypes.RECT),
            _MONITORENUMPROC, wintypes.LPARAM]
        user32.GetMonitorInfoW.argtypes = [
            wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL

        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        return user32, kernel32
    except (AttributeError, OSError):
        return None
