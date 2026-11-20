"""本地 HTTP 小工具：JSON 响应 + 本机 Origin 校验（stage 与 WebUI 共用）。"""
from __future__ import annotations

import json
import re

_LOOPBACK_ORIGIN = re.compile(
    r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")

#: WebUI 服务端代理 → stage 的内部控制头。普通本机页面带自定义头会触发
#: CORS 预检，而 stage 不返回 CORS 允许头，挡掉“任意 localhost 页面控制形象”。
CONTROL_HEADER = "X-DeepSidekick-Control"


def json_response(data, status: int = 200, *, headers: dict | None = None):
    from aiohttp import web

    return web.json_response(
        data, status=status, headers=headers,
        dumps=lambda x: json.dumps(x, ensure_ascii=False))


def is_loopback_origin(origin: str) -> bool:
    """Origin 是否来自本机；空 Origin 由调用方决定放不放行。"""
    return bool(_LOOPBACK_ORIGIN.match(str(origin or "")))
