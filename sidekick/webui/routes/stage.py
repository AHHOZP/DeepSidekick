"""WebUI → stage 的实时预览控制代理。"""
from __future__ import annotations

import asyncio

import aiohttp
from aiohttp import web

from sidekick.core.http_json import CONTROL_HEADER
from sidekick.webui.context import WebContext


def add_routes(router: web.UrlDispatcher, ctx: WebContext) -> None:
    async def control(request):
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return web.json_response({"ok": False, "error": "invalid json"},
                                     status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "invalid control"},
                                     status=400)
        try:
            stage = ctx.cfg().get("stage") or {}
            host = str(stage.get("host") or "127.0.0.1")
            port = int(stage.get("port") or 0)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=503)
        if not port:
            return web.json_response({"ok": False, "error": "stage 未配置"},
                                     status=503)
        timeout = aiohttp.ClientTimeout(total=2)
        url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                        f"http://{url_host}:{port}/control", json=body,
                        headers={CONTROL_HEADER: "1"}) as resp:
                    text = await resp.text()
                    return web.json_response(
                        {"ok": resp.status == 200, "stage": text},
                        status=200 if resp.status == 200 else 502)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return web.json_response(
                {"ok": False, "error": "stage 未运行或不可达"}, status=502)

    router.add_post("/api/stage/control", control)
