"""页面与静态资源：WebUI 自身、overlay 资源、形象预览。"""
from __future__ import annotations

from pathlib import Path

from aiohttp import web

from sidekick.avatar.stage import PreviewHandlers
from sidekick.webui.http import safe_rel
from sidekick.webui.context import WebContext

STATIC = Path(__file__).resolve().parents[1] / "static"


def add_routes(router: web.UrlDispatcher, ctx: WebContext) -> None:
    preview = PreviewHandlers(ctx.catalog, ctx.cfg)
    overlay = ctx.root / "overlay"

    async def index(_request):
        p = STATIC / "index.html"
        return web.Response(body=p.read_bytes(), content_type="text/html",
                            charset="utf-8",
                            headers={"Cache-Control": "no-store"})

    async def favicon(_request):
        return web.Response(status=204)

    async def static(request):
        p = safe_rel(request.match_info.get("path", ""), STATIC)
        if p is None or not p.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Cache-Control": "no-store"})

    async def overlay_file(request):
        p = safe_rel(request.match_info.get("path", ""), overlay)
        if p is None or not p.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Cache-Control": "no-cache"})

    router.add_get("/", index)
    router.add_get("/favicon.ico", favicon)
    router.add_get("/ui/{path:.*}", static)
    router.add_get("/ov/{path:.*}", overlay_file)
    router.add_get("/preview", preview.index)
    router.add_get("/preview-config.json", preview.config)
    router.add_get("/m/{id}/{path:.*}", preview.model)
