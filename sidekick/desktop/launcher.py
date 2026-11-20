"""桌面透明窗的进程启动/关闭。

真正的窗口在 `sidekick.desktop.app` 里，用 Pygame + live2d-py 原生渲染；
这里只负责从 runtime 拉起子进程，不 import pygame。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from sidekick.config import ROOT
from sidekick.avatar.stage import stage_url


def _state_path(cfg: dict) -> Path:
    port = int(cfg["stage"]["port"])
    return ROOT / "state" / f"desktop-{port}.json"


def _rect_path(cfg: dict) -> Path:
    port = int(cfg["stage"]["port"])
    return ROOT / "state" / f"desktop-{port}.rect.json"


def _log_path(cfg: dict) -> Path:
    port = int(cfg["stage"]["port"])
    return ROOT / "state" / f"desktop-{port}.log"


def read_pet_rect(cfg: dict) -> tuple[int, int, int, int] | None:
    """读桌面进程上报的可见 alpha 包围盒（桌面坐标）；过期/没有返回 None。"""
    try:
        data = json.loads(_rect_path(cfg).read_text(encoding="utf-8"))
        if time.time() - float(data.get("ts") or 0.0) > 5.0:
            return None
        rect = data.get("rect")
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            return None
        return tuple(int(v) for v in rect)  # type: ignore[return-value]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def pet_rect_provider(cfg: dict):
    """给 FrameQueue 的只读回调；capture 不 import desktop。"""
    return lambda: read_pet_rect(cfg)


def launch(cfg: dict, log=print, *, avatar_id: str = "") -> subprocess.Popen | None:
    """启动一个独立的桌宠渲染进程；失败返回 None，不拖垮 runtime。"""
    if sys.platform != "win32":
        log("[desktop] 透明桌宠目前只支持 Windows")
        return None
    url = stage_url(cfg)
    state = _state_path(cfg)
    state.parent.mkdir(parents=True, exist_ok=True)
    log_path = _log_path(cfg)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [
        sys.executable, "-m", "sidekick.desktop",
        "--url", url,
        "--state", str(state),
        "--rect", str(_rect_path(cfg)),
    ]
    if avatar_id:
        cmd += ["--avatar", avatar_id]
    try:
        with open(log_path, "ab") as logf:
            proc = subprocess.Popen(
                cmd, cwd=str(ROOT), env=env,
                stdout=logf, stderr=logf)
    except Exception as exc:
        log(f"[desktop] 启动失败：{type(exc).__name__}: {exc}")
        return None
    try:
        code = proc.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        log(f"[desktop] Pygame 桌宠已启动：{url}（日志 {log_path.name}）")
        return proc
    log(f"[desktop] 桌宠启动后立即退出（code={code}）；"
        f"见 {log_path}")
    return None


def stop(proc: subprocess.Popen | None, log=print) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception as exc:
        log(f"[desktop] 关闭失败：{type(exc).__name__}: {exc}")
