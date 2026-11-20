"""日志：控制台 + 文件双写 + 内存环。

普通日志走 stdout；未捕获异常的崩溃栈走 stderr。文件失败绝不影响程序。
"""
from __future__ import annotations

import sys
import threading
import time
from collections import deque
from pathlib import Path

_RING: deque[str] = deque(maxlen=400)
_FILE = None
_CONSOLE_OK = True
_PATH: Path | None = None
_LOCK = threading.Lock()


def configure(root: str | Path | None, filename: str = "run.log") -> None:
    """设置 state/<filename> 的位置；不调用则只写控制台 + 内存。"""
    global _PATH, _FILE
    if root is None:
        return
    _PATH = Path(root) / "state" / filename
    _FILE = None


def _stamp() -> str:
    now = time.time()
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"


def _to_console(text: str) -> None:
    global _CONSOLE_OK
    if not _CONSOLE_OK:
        return
    try:
        print(text, file=sys.stdout, flush=True)
    except Exception:
        _CONSOLE_OK = False


def _to_stderr(text: str) -> None:
    try:
        print(text, file=sys.stderr, flush=True)
    except Exception:
        pass


def _file():
    global _FILE
    if _PATH is None:
        return None
    if _FILE is not None:
        try:
            if _PATH.stat().st_size <= 2_000_000:
                return _FILE
            _FILE.close()
        except OSError:
            pass
        _FILE = None
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        if _PATH.exists() and _PATH.stat().st_size > 2_000_000:
            _PATH.write_text("", encoding="utf-8")
        _FILE = open(_PATH, "a", encoding="utf-8", buffering=1)
        _FILE.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
    except Exception:
        return None
    return _FILE


def log(*args) -> None:
    line = " ".join(str(x) for x in args)
    ts = _stamp()
    with _LOCK:
        _RING.append(f"{ts} {line}")
        try:
            f = _file()
            if f is not None:
                f.write(f"{ts} {line}\n")
        except Exception:
            pass
    _to_console(f"[sidekick {ts}] {line}")


def tail(n: int = 80) -> list[str]:
    with _LOCK:
        return list(_RING)[-max(1, int(n)):]


def one_line(text, limit: int = 300) -> str:
    """日志里放正文时压成一行，避免把时间戳前缀冲断。"""
    s = " ".join(str(text or "").split())
    return s[:limit] + ("…" if len(s) > limit else "")


def _hook(exc_type, exc, tb) -> None:
    """未捕获异常写进 run.log，并打到 stderr；这是崩溃栈的唯一输出。"""
    import traceback

    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        target = _PATH or (Path(__file__).resolve().parents[2]
                           / "state" / "run.log")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 异常 =====\n{text}\n")
    except Exception:
        pass
    _to_stderr(text)


def install_hooks() -> None:
    sys.excepthook = _hook
    try:
        threading.excepthook = lambda a: _hook(
            a.exc_type, a.exc_value, a.exc_traceback)
    except Exception:
        pass


def crash(text: str) -> Path | None:
    target = ((_PATH.parent / "crash.log") if _PATH
              else Path(__file__).resolve().parents[2] / "state" / "crash.log")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(time.strftime("%Y-%m-%d %H:%M:%S\n") + text, encoding="utf-8")
        return target
    except Exception:
        return None
