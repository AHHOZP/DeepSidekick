"""真无头浏览器 smoke：加载 stage 页面，确认 overlay 能在真实 DOM 里启动。

本机没有 Edge/Chrome 时跳过；不下载浏览器、不访问外网。
"""
from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import asyncio
import functools
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sidekick import config
from sidekick.avatar.catalog import AvatarCatalog
from sidekick.avatar.stage import StageClient, serve_stage

ROOT = Path(__file__).resolve().parents[1]
_CANDIDATES = (
    Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
)


def _find_browser() -> str:
    for path in _CANDIDATES:
        if path.is_file():
            return str(path)
    for name in ("msedge", "chrome", "chromium", "chromium-browser"):
        hit = shutil.which(name)
        if hit:
            return hit
    return ""


@functools.lru_cache(maxsize=1)
def _browser() -> str:
    """浏览器存在不代表能跑（受限环境可能禁止其 IPC/沙箱）。"""
    path = _find_browser()
    if not path:
        return ""
    try:
        (ROOT / "temp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(ROOT / "temp")) as profile:
            result = subprocess.run(
                [path, "--headless=new", "--disable-gpu", "--no-sandbox",
                 f"--user-data-dir={profile}", "--dump-dom",
                 "data:text/html,<body>ok</body>"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return path if result.returncode == 0 and "ok" in result.stdout else ""


@unittest.skipUnless(_browser(), "没有可运行的无头 Edge/Chrome，跳过浏览器 smoke")
class OverlayBrowserTests(QuietIsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copytree(ROOT / "overlay", self.root / "overlay")
        images = self.root / "models" / "images"
        images.mkdir(parents=True)
        (images / "smoke.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
            '<rect width="100" height="100" fill="#3a7"/></svg>',
            encoding="utf-8")
        cfg = config.defaults()
        cfg["stage"].update({"host": "127.0.0.1", "port": 0})
        self.client = StageClient()
        self.runner = await serve_stage(
            self.client, cfg, cfg_provider=lambda: cfg,
            catalog=AvatarCatalog(self.root))
        port = self.runner.addresses[0][1]
        # 这是 stage 页面，配置来自 /config.json；/preview-config.json 只属于
        # WebUI 的 /ov/ 预览，不在 stage 上。
        self.url = f"http://127.0.0.1:{port}/?debug=1"
        # Chrome 的网络沙箱要写 profile；放到工作区 temp，避免系统临时目录
        # 在受限环境里被拒绝。
        (ROOT / "temp").mkdir(exist_ok=True)
        self.profile_tmp = tempfile.TemporaryDirectory(
            dir=str(ROOT / "temp"), prefix="browser-smoke-")
        self.profile = Path(self.profile_tmp.name)

    async def asyncTearDown(self):
        # headless Chrome 退出后可能还留着 keep-alive 连接；先让服务端把
        # 已断开的 transport 收干净，再关 server，避免 Proactor 在 cleanup
        # 过程中打 ConnectionResetError / unclosed socket。
        await asyncio.sleep(0.1)
        await self.runner.cleanup()
        await asyncio.sleep(0.05)
        self.profile_tmp.cleanup()
        self.tmp.cleanup()

    async def test_overlay_starts_in_headless_browser(self):
        args = [
            _browser(), "--headless=new", "--disable-gpu", "--no-sandbox",
            "--disable-dev-shm-usage", "--no-proxy-server",
            "--disable-background-networking", "--disable-component-update",
            "--disable-sync", "--disable-default-apps", "--no-first-run",
            f"--user-data-dir={self.profile}",
            "--virtual-time-budget=3000", "--dump-dom", self.url,
        ]
        result = await asyncio.to_thread(
            subprocess.run, args, cwd=ROOT, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(0, result.returncode, result.stderr[-1000:])
        self.assertIn("r-image", result.stdout)
        self.assertNotIn("deepsidekick-error", result.stdout)


if __name__ == "__main__":
    unittest.main()
