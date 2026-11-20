"""对话预设：读、改、新建、删除。

预设是 `persona/presets/<id>/preset.yaml`；页面只编辑下拉选中的那一份，
`prompt.id` 的选择走 `/api/config`（它是普通配置字段）。
"""
from __future__ import annotations

from aiohttp import web

from sidekick import config
from sidekick.brain import preset as preset_mod
from sidekick.webui import form as schema
from sidekick.webui.context import WebContext
from sidekick.webui.http import error, json_response


def add_routes(router: web.UrlDispatcher, ctx: WebContext) -> None:
    def payload(preset_id: str | None = None) -> dict:
        return {"ok": True, **schema.preset_payload(ctx.root, preset_id)}

    async def get_presets(_request):
        return json_response(payload())

    async def get_preset(request):
        pid = request.match_info.get("pid", "")
        if not preset_mod.valid_id(pid):
            return error("预设名不合法", 404)
        return json_response(payload(pid))

    async def create(request):
        body = await request.json()
        pid = str((body or {}).get("id") or "").strip()
        if not preset_mod.valid_id(pid):
            return error("预设名只能用字母/数字/下划线/短横线/点（1–40 字）")
        path = preset_mod.preset_path(ctx.root, pid)
        if path.exists():
            return error(f"预设已存在：{pid}", 409)
        try:
            preset_mod.write(ctx.root, pid, pid, "", ())
        except (OSError, ValueError) as e:
            return error(f"预设建不出来：{e}", 500)
        return json_response(payload(pid))

    async def save(request):
        pid = request.match_info.get("pid", "")
        if not preset_mod.valid_id(pid):
            return error("预设名不合法", 404)
        body = await request.json()
        blocks, errs = preset_mod.normalize_blocks((body or {}).get("blocks"))
        try:
            preset_mod.write(ctx.root, pid,
                             str((body or {}).get("name") or "").strip(),
                             str((body or {}).get("desc") or "").strip(),
                             blocks)
        except (OSError, ValueError) as e:
            return error(f"预设写不进去：{e}", 500)
        got = preset_mod.read(ctx.root, pid)
        return json_response({"ok": True, "id": pid, "blocks": len(got.blocks),
                              "issues": errs, **schema.preset_payload(ctx.root, pid)})

    async def delete(request):
        pid = request.match_info.get("pid", "")
        if not preset_mod.valid_id(pid):
            return error("预设名不合法", 404)
        current = str((config.load().get("prompt") or {}).get("id") or "")
        if pid == current:
            return error("这是当前预设；先切换到别的预设再删")
        try:
            preset_mod.delete(ctx.root, pid)
        except OSError as e:
            return error(f"预设删不掉：{e}", 500)
        return json_response(payload())

    async def select(request):
        body = await request.json()
        pid = str((body or {}).get("id") or "").strip()
        if not preset_mod.valid_id(pid):
            return error("预设名不合法")
        if not preset_mod.preset_path(ctx.root, pid).is_file():
            return error(f"预设不存在：{pid}", 404)
        async with ctx.config_lock:
            raw = config.load_raw()
            if config.last_error:
                return error(
                    f"config.yaml 解析失败，已拒绝覆盖保存：{config.last_error}。"
                    "请先修复文件再切换预设。", 409)
            raw.setdefault("prompt", {})["id"] = pid
            config.save_raw(raw)
        return json_response(payload(pid))

    router.add_get("/api/presets", get_presets)
    router.add_get("/api/presets/{pid}", get_preset)
    router.add_post("/api/presets", create)
    router.add_post("/api/presets/select", select)
    router.add_put("/api/presets/{pid}", save)
    router.add_delete("/api/presets/{pid}", delete)
