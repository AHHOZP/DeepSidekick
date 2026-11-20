"""三件套导入导出：预设 / 世界书 / 角色。

- `GET  /api/pack/export?kind=&id=`  直接下 zip（浏览器另存为）
- `POST /api/pack/import?kind=&overwrite=1`  请求体是 zip 原始字节

上传体可能很大（角色包带模型），所以按 chunk 读、总量封顶，不用 multipart。
"""
from __future__ import annotations

import asyncio

from aiohttp import web

from sidekick.pack import KINDS, PackError
from sidekick.pack import export as pack_export
from sidekick.pack import import_ as pack_import
from sidekick.webui.context import WebContext
from sidekick.webui.http import error, json_response

#: 单个包最多收这么大；角色模型一般几十 MB，留足余量。
MAX_UPLOAD = 400 * 1024 * 1024


def _kind(request) -> str:
    kind = str(request.query.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise PackError(f"不认识的包类型：{kind!r}")
    return kind


def add_routes(router: web.UrlDispatcher, ctx: WebContext) -> None:
    async def export(request):
        try:
            kind = _kind(request)
            ident = str(request.query.get("id") or "").strip()
            if not ident:
                return error("缺少 id")
            blob, filename = await asyncio.to_thread(
                pack_export, kind, ctx.root, ident, catalog=ctx.catalog)
        except PackError as e:
            return error(str(e), 404)
        except OSError as e:
            return error(f"打包失败：{e}", 500)
        return web.Response(
            body=blob, content_type="application/zip",
            headers={"Content-Disposition":
                     f'attachment; filename="{filename}"',
                     "Cache-Control": "no-store"})

    async def import_(request):
        try:
            kind = _kind(request)
        except PackError as e:
            return error(str(e))
        overwrite = str(request.query.get("overwrite") or "") in ("1", "true")
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.content.iter_chunked(256 * 1024):
            total += len(chunk)
            if total > MAX_UPLOAD:
                return error("包太大（超过 400 MB）", 413)
            chunks.append(chunk)
        if not total:
            return error("没有收到文件内容")
        try:
            result = await asyncio.to_thread(
                pack_import, kind, ctx.root, b"".join(chunks),
                catalog=ctx.catalog, overwrite=overwrite)
        except PackError as e:
            return error(str(e))
        except OSError as e:
            return error(f"导入失败：{e}", 500)
        return json_response({"ok": True, **result})

    router.add_get("/api/pack/export", export)
    router.add_post("/api/pack/import", import_)
