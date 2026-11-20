"""原子写文件：先写同目录临时文件，再 `os.replace`。

配置、密钥、世界书、形象选择、动作规则都走这一份实现；
写到一半崩溃不会留下半截文件，也不会让读方看到中间态。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path


def _replace(path: Path, write) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def write_text(path: str | Path, text: str, *, encoding: str = "utf-8",
               mode: int | None = None) -> Path:
    p = Path(path)

    def _write(tmp: Path) -> None:
        tmp.write_text(text, encoding=encoding)
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass

    return _replace(p, _write)


def write_bytes(path: str | Path, data: bytes, *, mode: int | None = None) -> Path:
    p = Path(path)

    def _write(tmp: Path) -> None:
        tmp.write_bytes(data)
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass

    return _replace(p, _write)


def dir_size(path: str | Path) -> int:
    """目录占盘字节数；不存在/读不了返回 0。"""
    p = Path(path)
    if not p.exists():
        return 0
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        pass
    return total
