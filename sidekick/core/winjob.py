"""Windows 作业对象：持有它的进程一消失，作业里的进程（含它们的子进程）一起结束。

WebUI 拉起 runtime、runtime 又拉起桌宠，收尾代码只能覆盖"正常退出"：
Ctrl+C 时那些 `finally` 可能来不及跑完，关窗口/任务管理器结束进程时更是直接
TerminateProcess。作业对象把这件事交给内核——WebUI 进程一没，它持有的作业句柄
随之关闭，`KILL_ON_JOB_CLOSE` 让作业里的所有进程一起被杀。

子进程默认继承作业，所以只需要把 runtime 收进来，桌宠自动在里面。
非 Windows 是空对象：调用方照常调用，什么也不做。
"""
from __future__ import annotations

import ctypes
import sys

_KILL_ON_JOB_CLOSE = 0x00002000
_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class KillOnClose:
    """一个作业对象；`close()` 或持它的进程退出时，成员全部被杀。"""

    def __init__(self) -> None:
        self._handle = None
        self._api = None
        if sys.platform != "win32":
            return
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.restype = ctypes.c_void_p
        api.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        api.SetInformationJobObject.restype = ctypes.c_bool
        api.SetInformationJobObject.argtypes = (
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        api.OpenProcess.restype = ctypes.c_void_p
        api.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32)
        api.AssignProcessToJobObject.restype = ctypes.c_bool
        api.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        api.CloseHandle.restype = ctypes.c_bool
        api.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = api.CreateJobObjectW(None, None)
        if not handle:
            return
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(handle, _EXTENDED_LIMIT_INFORMATION,
                                           ctypes.byref(limits), ctypes.sizeof(limits)):
            api.CloseHandle(handle)
            return
        self._api, self._handle = api, handle

    @property
    def active(self) -> bool:
        """作业建起来了没有；非 Windows 或系统拒绝时是 False。"""
        return bool(self._handle)

    def assign(self, pid: int) -> bool:
        """把进程收进作业；收不进来返回 False（只影响"父死子亡"这条兜底）。"""
        if not self._handle:
            return False
        proc = self._api.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
                                     False, int(pid))
        if not proc:
            return False
        try:
            return bool(self._api.AssignProcessToJobObject(self._handle, proc))
        finally:
            self._api.CloseHandle(proc)

    def close(self) -> None:
        if self._handle:
            self._api.CloseHandle(self._handle)
            self._handle = None
