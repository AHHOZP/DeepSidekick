"""语义记忆模型下载：逐文件下载、校验、原子替换。

优先 HF；单个文件失败 2 次后切 ModelScope，再失败 2 次才报错。
不打包、不解压：FastEmbed 的 `specific_model_path` 直接读这个目录。
"""
from __future__ import annotations

from pathlib import Path

from sidekick.core.fetch import fetch_file

SOURCES = ("HF", "ModelScope")
TRIES_PER_SOURCE = 2
TIMEOUT_S = 60.0


def verify(entry, dest: str | Path) -> list[str]:
    return entry.missing(dest)


def install(entry, dest: str | Path, job: dict) -> Path:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    job["total"] = entry.bytes
    done = 0
    for spec in entry.files:
        target = dest / spec.path
        if target.is_file() and target.stat().st_size == spec.bytes:
            done += spec.bytes
            job["bytes"] = done
            continue
        job["file"] = spec.path
        last_error = None
        ok = False
        for source in SOURCES:
            for attempt in range(1, TRIES_PER_SOURCE + 1):
                job["source"] = source
                job["stage"] = f"下载（{source} {attempt}/{TRIES_PER_SOURCE}）"

                def progress(got: int, _total: int, base: int = done) -> None:
                    job["bytes"] = base + got

                try:
                    fetch_file(entry.url(spec, source), target,
                               expected_bytes=spec.bytes,
                               sha256=spec.sha256,
                               on_progress=progress,
                               timeout=TIMEOUT_S)
                    ok = True
                    break
                except Exception as e:
                    last_error = f"{source}: {type(e).__name__}: {e}"
            if ok:
                break
        if not ok:
            raise RuntimeError(f"下载失败（HF/ModelScope 各试 "
                               f"{TRIES_PER_SOURCE} 次）：{last_error}")
        done += spec.bytes
        job["bytes"] = done
    missing = verify(entry, dest)
    if missing:
        raise RuntimeError("下载后仍缺文件：" + "、".join(missing))
    job["stage"] = "完成"
    return dest
