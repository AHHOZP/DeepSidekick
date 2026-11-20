"""用户人设：`persona/users/<id>/profile.yaml` 的增删改与切换。

和游戏/角色一样的约定：config.yaml 只存 `user.id`，内容在专门文件夹。
"""
from __future__ import annotations

import asyncio
import shutil

from aiohttp import web

from sidekick import config
from sidekick.brain import users
from sidekick.webui import form as schema
from sidekick.webui.http import error, json_response
from sidekick.webui.context import WebContext


def _payload(ctx: WebContext) -> dict:
    return {"users": schema.users_payload(ctx.root, config.load())}


def add_routes(router: web.UrlDispatcher, ctx: WebContext) -> None:
    async def create_user(request):
        body = await request.json()
        uid = str((body or {}).get("id") or "").strip()
        if not users.valid_id(uid):
            return error("用户 id 不能带路径分隔符或系统保留字符")
        if users.profile_path(ctx.root, uid).is_file():
            return error("同名用户人设已存在")
        try:
            await asyncio.to_thread(users.write, ctx.root, uid, {})
        except (OSError, ValueError) as e:
            return error(f"用户人设建不出来：{e}", 500)
        return json_response({"ok": True, "id": uid, **_payload(ctx)})

    async def put_user(request):
        uid = request.match_info.get("uid", "")
        if not users.valid_id(uid):
            return error("用户 id 不合法")
        body = await request.json()
        try:
            await asyncio.to_thread(users.write, ctx.root, uid, body or {})
        except (OSError, ValueError) as e:
            return error(f"用户人设写不进去：{e}", 500)
        data, err = await asyncio.to_thread(users.read, ctx.root, uid)
        return json_response({"ok": True, "id": uid, "profile": data,
                              "error": err})

    async def delete_user(request):
        if request.query.get("confirm") != "1":
            return error("需要 confirm=1")
        uid = request.match_info.get("uid", "")
        if not users.valid_id(uid):
            return error("用户 id 不合法")
        current = str((config.load().get("user") or {}).get("id") or "")
        if uid == current:
            return error("这是当前用户；先切换到别的用户再删")
        path = users.profile_path(ctx.root, uid)
        if not path.is_file():
            return error("用户人设不存在", 404)
        try:
            await asyncio.to_thread(shutil.rmtree, path.parent)
        except OSError as e:
            return error(f"用户人设删不掉：{e}", 500)
        return json_response({"ok": True, **_payload(ctx)})

    async def select_user(request):
        body = await request.json()
        uid = str((body or {}).get("id") or "").strip()
        if not users.valid_id(uid):
            return error("用户 id 不合法")
        if not users.profile_path(ctx.root, uid).is_file():
            return error(f"用户人设不存在：{uid}", 404)
        async with ctx.config_lock:
            raw = config.load_raw()
            if config.last_error:
                return error(
                    f"config.yaml 解析失败，已拒绝覆盖保存：{config.last_error}。"
                    "请先修复文件再切换用户。", 409)
            raw.setdefault("user", {})["id"] = uid
            config.save_raw(raw)
        return json_response({"ok": True, "current": uid, **_payload(ctx)})

    router.add_post("/api/users", create_user)
    router.add_put("/api/users/{uid}", put_user)
    router.add_delete("/api/users/{uid}", delete_user)
    router.add_post("/api/users/select", select_user)
