"""共享的真实 WebUI 测试夹具。

测试用 `WebUI.__init__` 真正构造对象，只把仓库根/配置/密钥库指到临时目录；
不要再 `object.__new__(WebUI)` 手工补字段，否则构造接线坏了测试也不会失败。
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterator
from unittest import mock

import yaml

from sidekick import config
from sidekick.config import secrets
from sidekick.webui.app import WebUI


@contextlib.contextmanager
def webui(root: str | Path, raw: dict | None = None) -> Iterator[WebUI]:
    """在临时根上构造一个真实 WebUI；退出时关闭它并还原所有 patch。"""
    root = Path(root)
    cfg_path = root / "config.yaml"
    patches = [
        mock.patch("sidekick.webui.app.ROOT", root),
        mock.patch.object(config, "CONFIG_PATH", cfg_path),
        mock.patch.object(secrets, "ROOT", root),
    ]
    for patch in patches:
        patch.start()
    config._load_cache = None
    config.last_error = None
    secrets._warned = False
    try:
        if raw is not None:
            cfg_path.write_text(
                yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        ui = WebUI()
        try:
            yield ui
        finally:
            ui.close()
    finally:
        for patch in reversed(patches):
            patch.stop()
        config._load_cache = None
        config.last_error = None
