"""WebUI 路由共用的 JSON、路径和表单小工具。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from aiohttp import web

from sidekick.config import secrets as secretstore
from sidekick.core.http_json import json_response as _json_response
from sidekick.brain.prompt import PERSONA_FILE
from sidekick.core.text import split_list

MAX_TEXT = 200_000
#: 角色人设只有一个文件；预设/世界书都有自己的接口，不走这里。
PERSONA_NAMES = (PERSONA_FILE,)


def json_response(data: Any, status: int = 200) -> web.Response:
    return _json_response(data, status)


def error(msg: str, status: int = 400) -> web.Response:
    return json_response({"ok": False, "error": str(msg)}, status)


def strip_secrets(patch: dict, prefix: str = "") -> dict:
    """配置 API 绝不接收/写回 secret 字段；密钥走 /api/secrets。

    注意这里是**删除**秘密键，而不是保留 `sealed`：前端保存的是整份
    redacted 配置，若把 `sealed` 写回，会把明文 key 覆盖成“有引用、
    没密文”。删掉后这次 patch 完全不会碰该字段。

    哪些路径是密钥只查 `secrets.is_secret_path`，这里不再维护第二份名单。
    """
    if not isinstance(patch, dict):
        return {}
    out = {}
    for k, v in patch.items():
        path = f"{prefix}.{k}" if prefix else str(k)
        if secretstore.is_secret_path(path):
            continue
        if isinstance(v, dict):
            out[k] = strip_secrets(v, path)
        elif isinstance(v, list):
            out[k] = [strip_secrets(x, path) if isinstance(x, dict) else x
                      for x in v]
        else:
            out[k] = v
    return out


def safe_rel(rel: str, base: Path) -> Path | None:
    p = (base / rel).resolve()
    try:
        p.relative_to(base.resolve())
    except ValueError:
        return None
    return p


def kw_list(v) -> list[str]:
    return split_list(v, unique=False, squeeze=False, strip_quotes=False)


def valid_gid(gid: str) -> bool:
    gid = str(gid or "")
    if not re.match(r"^[A-Za-z0-9._-]{1,80}$", gid):
        return False
    # Windows 会把尾点/尾空格吃掉；`real.` 会解析成 `real`，必须拒绝。
    return gid == gid.strip(" .") and gid not in (".", "..")
