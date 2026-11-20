"""只读排查端点：status / peek / log。听诊器，不是遥控器。"""
from __future__ import annotations

import asyncio
import json

from sidekick.core.log import log, tail

DEBUG_PORT = 17890


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


async def serve_debug(rt, port: int = DEBUG_PORT) -> object:
    from aiohttp import web

    async def on_status(_):
        data = await asyncio.to_thread(rt.status)
        return web.json_response(data, dumps=_dumps)

    async def on_peek(req):
        secs = float(req.query.get("seconds", 90))
        material = await asyncio.to_thread(rt.peek, secs)
        return web.json_response({"material": material}, dumps=_dumps)

    async def on_log(req):
        n = int(req.query.get("n", 80))
        lines = await asyncio.to_thread(tail, n)
        return web.json_response({"lines": lines}, dumps=_dumps)

    app = web.Application()
    app.add_routes([web.get("/status", on_status),
                    web.get("/peek", on_peek),
                    web.get("/log", on_log)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    log(f"排查端点已开：http://127.0.0.1:{port}/status（只读）")
    return runner
