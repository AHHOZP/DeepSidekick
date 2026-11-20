"""Prompt 预演：在 WebUI 里看一眼这一轮真正会发出去的 messages。

实现在 `sidekick/preview`；这里只负责起一个短命子进程去拼（拼一次要建 Runtime、开记忆库、
加载召回模型，放在 WebUI 进程里会一直占着），拿到输出后截掉启动日志。

子进程的输出走**临时文件**而不是管道：Windows 下管道要端口，容易被安全软件/沙箱挡掉，
写文件更稳，也不占内存。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from aiohttp import web

from sidekick.preview import trim
from sidekick.webui.context import WebContext
from sidekick.webui.http import error, json_response

#: 拼一次要加载语义模型，给足时间；超时就报错，不无限等。
TIMEOUT_S = 120.0
_LOCK = asyncio.Lock()
_STEM = "prompt-preview"


def _paths(root) -> tuple[Path, Path]:
    base = Path(root) / "temp"
    base.mkdir(parents=True, exist_ok=True)
    paths = []
    for suffix in (".out.txt", ".err.txt"):
        fd, name = tempfile.mkstemp(
            prefix=f"{_STEM}.", suffix=suffix, dir=str(base))
        os.close(fd)
        paths.append(Path(name))
    return paths[0], paths[1]


async def _cleanup(paths: tuple[Path, ...]) -> None:
    """Windows 下子进程退出后文件句柄可能晚一拍释放，清理时短暂重试。"""
    for path in paths:
        for _ in range(6):
            try:
                path.unlink(missing_ok=True)
                break
            except PermissionError:
                await asyncio.sleep(0.05)


async def _run(root, *argv: str) -> tuple[int, str, str]:
    """跑一次预演子进程，返回 (退出码, 标准输出, 标准错误)。"""
    out_path, err_path = _paths(root)
    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        with out_path.open("wb") as out, err_path.open("wb") as err:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(root), stdout=out, stderr=err, env=env)
            try:
                await asyncio.wait_for(proc.wait(), timeout=TIMEOUT_S)
                code = int(proc.returncode or 0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                code = -1
            except asyncio.CancelledError:
                # 用户关页面/取消请求时不能把子进程留在后台继续加载模型。
                proc.kill()
                await proc.wait()
                raise
        text = out_path.read_text(encoding="utf-8", errors="replace")
        detail = err_path.read_text(encoding="utf-8", errors="replace")
        return code, text, detail
    finally:
        await _cleanup((out_path, err_path))


def add_routes(router: web.UrlDispatcher, ctx: WebContext) -> None:
    async def preview(request):
        want = str(request.query.get("live") or "").strip()
        argv = [sys.executable, "-m", "sidekick.preview"]
        if want in ("1", "true"):
            argv.append("--live")
        elif want in ("0", "false"):
            argv.append("--solo")
        async with _LOCK:
            try:
                code, text, detail = await _run(ctx.root, *argv)
            except OSError as e:
                return error(f"起不了预演进程：{e}", 500)
        if code == -1:
            return error(f"预演超时（{int(TIMEOUT_S)} 秒）", 504)
        if code != 0:
            reason = (detail.strip() or text.strip() or f"退出码 {code}")
            return error(f"预演失败：{reason[-800:]}", 500)
        return json_response({"ok": True, "text": trim(text)})

    router.add_get("/api/prompt/preview", preview)
