"""HTTP 下载：带证书上下文、进度、大小/SHA256 校验、临时文件替换。

STT 模型包和语义记忆模型都走这一份；WebUI 只负责 HTTP 和 job。
"""
from __future__ import annotations

import hashlib
import os
import urllib.request
from pathlib import Path


def ssl_context():
    """优先用 certifi 的 CA。系统证书穿代理/自签环境时容易验证失败。"""
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def http_open(url: str, timeout: float = 60.0):
    req = urllib.request.Request(url, headers={"User-Agent": "DeepSidekick/1.0"})
    return urllib.request.urlopen(req, timeout=timeout,
                                  context=ssl_context())


def fetch_file(url: str, dest: str | Path, *, expected_bytes: int = 0,
               sha256: str = "", on_progress=None,
               timeout: float = 60.0) -> int:
    """下载到同目录 `.part`，校验后原子替换；返回字节数。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    got = 0
    try:
        with http_open(url, timeout=timeout) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            if on_progress is not None:
                on_progress(0, total)
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                digest.update(chunk)
                got += len(chunk)
                if on_progress is not None:
                    on_progress(got, total)
        if expected_bytes and got != expected_bytes:
            raise RuntimeError(
                f"下载大小不符：收到 {got} / 期望 {expected_bytes} 字节")
        expected_hash = str(sha256 or "").strip().lower()
        if expected_hash and digest.hexdigest() != expected_hash:
            raise RuntimeError(
                f"SHA256 不符：收到 {digest.hexdigest()} / 期望 {expected_hash}")
        os.replace(tmp, dest)
        return got
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
