"""STT 模型下载：tar.bz2 → 解压 → 上提到目标目录。

从 WebUI 路由移出来，WebUI 只做 HTTP；脚本/测试也能直接调。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
from pathlib import Path

from sidekick.core.fetch import http_open


def fetch_and_extract(entry, dest: Path, mirror: bool, job: dict) -> Path:
    """下载 tar.bz2 → 解压 → 上提到 dest。

    发布包里都裹了一层同名目录，所以解压到临时目录再“上提”，
    不用追着每个包的目录名改 strip-components。
    """
    dest = Path(dest)
    meta = dest.parent / ".download"
    meta.mkdir(parents=True, exist_ok=True)
    arc = meta / entry.file
    job["stage"] = "下载"

    url = entry.url(mirror)
    digest = hashlib.sha256()
    with http_open(url, timeout=60) as r, open(arc, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        job["total"] = total
        got = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            digest.update(chunk)
            got += len(chunk)
            job["bytes"] = got
    if total and got != total:
        raise RuntimeError(f"下载不完整：收到 {got} / {total} 字节")
    expected = int(getattr(entry, "bytes", 0) or 0)
    if expected and got != expected:
        raise RuntimeError(f"下载包大小不符：收到 {got} / 期望 {expected} 字节")
    expected_hash = str(getattr(entry, "sha256", "") or "").strip().lower()
    if expected_hash and digest.hexdigest() != expected_hash:
        raise RuntimeError(
            f"下载包 SHA256 不符：收到 {digest.hexdigest()} / 期望 {expected_hash}")

    job["stage"] = "解压"
    staging = dest.parent / (".tmp-" + entry.id)
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    with tarfile.open(arc, "r:bz2") as tar:
        # 项目只支持 Python 3.12+；filter="data" 阻止 tar 路径穿越。
        tar.extractall(staging, filter="data")

    items = list(staging.iterdir())
    src = items[0] if len(items) == 1 and items[0].is_dir() else staging
    backup = dest.parent / (dest.name + ".old")
    shutil.rmtree(backup, ignore_errors=True)
    if dest.exists():
        os.replace(dest, backup)
    try:
        shutil.move(str(src), str(dest))
    except Exception:
        if backup.exists() and not dest.exists():
            os.replace(backup, dest)
        raise
    shutil.rmtree(backup, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    try:
        arc.unlink()
    except OSError:
        pass
    # 每个模型自带 2~10 MB 测试音频，播的时候用不上
    shutil.rmtree(dest / "test_wavs", ignore_errors=True)
    job["stage"] = "校验"
    return dest
