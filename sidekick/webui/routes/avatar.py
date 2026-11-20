"""角色（形象 + 角色设定）相关路由。

约定：config.yaml 只存 `role.id`；名字/别名/人设文本/动作规则都在
`persona/roles/<role_id>/`，没有就是空，不从别处回退。
"""
from __future__ import annotations

import asyncio
import json

from aiohttp import web

from sidekick import config
from sidekick.avatar import roles
from sidekick.avatar import rules as avatar_rules
from sidekick.avatar.autonomy import generate as autonomy_generate
from sidekick.avatar.autonomy import prompts as autonomy_prompts
from sidekick.avatar.autonomy import store as autonomy_store
from sidekick.avatar.autonomy import validate as autonomy_validate
from sidekick.avatar.autonomy.audit import run_audit
from sidekick.webui import form as schema
from sidekick.webui.http import MAX_TEXT, PERSONA_NAMES, error, json_response
from sidekick.webui.context import WebContext

#: 同一角色同时只允许一个生成请求；按钮禁用之外的服务端兜底。
_GENERATING: set[str] = set()


def _payload(ctx: WebContext) -> dict:
    return {**schema.avatar_payload(ctx.catalog),
            "role": schema.role_payload(ctx.catalog, ctx.root, ctx.cfg())}


def add_routes(router: web.UrlDispatcher, ctx: WebContext) -> None:
    def avatar_or_error(avatar_id: str):
        avatar = ctx.catalog.get(str(avatar_id or "").strip())
        if avatar is None:
            return None, error("角色不存在", 404)
        # 旧指纹目录可能比缓存里的新目录先有内容：统一用实际有文件的目录。
        avatar.role_dir = roles.active_dir(avatar, ctx.root)
        return avatar, None

    def live2d_or_error(avatar, err):
        if err is not None:
            return err
        if getattr(avatar, "kind", "") != "live2d":
            return error("自主驱动配置只支持 Live2D 形象", 400)
        return None

    async def autonomy_get(request):
        avatar, err = avatar_or_error(request.match_info.get("aid", ""))
        bad = live2d_or_error(avatar, err)
        if bad is not None:
            return bad
        data, read_err = await asyncio.to_thread(autonomy_store.load, avatar)
        issues = []
        if data is not None:
            _clean, found = autonomy_validate.validate(data)
            issues = [x.text() for x in found]
        return json_response({
            "ok": True,
            "id": avatar.id,
            "exists": data is not None,
            "path": schema.rel_path(ctx.root, autonomy_store.path(avatar)),
            "profile": data,
            "issues": issues,
            "error": read_err,
        })

    async def autonomy_audit(request):
        avatar, err = avatar_or_error(request.match_info.get("aid", ""))
        bad = live2d_or_error(avatar, err)
        if bad is not None:
            return bad
        try:
            dossier = await asyncio.to_thread(
                run_audit, ctx.root, avatar.root / avatar.entry)
        except RuntimeError as e:
            return error(str(e), 502)
        return json_response({
            "ok": True,
            "dossier": autonomy_prompts.compact_dossier(dossier),
            "summary": {
                "tier": dossier.get("tier", "none"),
                "bound": len(dossier.get("by_channel") or {}),
                "motions": len(dossier.get("motions") or []),
                "expressions": len(dossier.get("expressions") or []),
                "warnings": dossier.get("warnings") or [],
                "canvas": (dossier.get("model") or {}).get("canvas") or [],
            },
        })

    async def autonomy_generate_stream(request):
        avatar, err = avatar_or_error(request.match_info.get("aid", ""))
        bad = live2d_or_error(avatar, err)
        if bad is not None:
            return bad
        if avatar.id in _GENERATING:
            return error("这个角色正在生成，请稍候", 409)
        # 先占位再 await：双击时两个请求都在检查之后、登记之前 await，
        # 会把“同一角色只允许一个生成”绕过去。
        _GENERATING.add(avatar.id)
        try:
            body = await request.json()
        except Exception:
            _GENERATING.discard(avatar.id)
            raise
        preferences = str((body or {}).get("preferences") or "").strip()
        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        try:
            await response.prepare(request)
        except Exception:
            _GENERATING.discard(avatar.id)
            raise
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)

        async def emit(event: dict) -> None:
            await queue.put(event)

        async def run() -> None:
            try:
                result = await autonomy_generate.generate_profile(
                    avatar, ctx.cfg(), preferences=preferences,
                    project_root=ctx.root, progress=emit)
                await queue.put({
                    "type": "result",
                    "profile": result["profile"],
                    "fallback": bool(result["fallback"]),
                    "issues": list(result["issues"]),
                    "dossier": result["dossier"],
                    "needs_restart": False,
                })
            except autonomy_generate.GenerationError as exc:
                await queue.put({"type": "error", "message": str(exc)})
            except (OSError, ValueError, TypeError) as exc:
                await queue.put({
                    "type": "error",
                    "message": f"生成失败：{type(exc).__name__}: {exc}",
                })
            except Exception as exc:  # noqa: BLE001 - 流边界，错误转为 SSE 事件
                await queue.put({
                    "type": "error",
                    "message": f"生成异常：{type(exc).__name__}: {exc}",
                })
            finally:
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    # 客户端已断开、队列满：不要再 await，否则协程永久挂住。
                    pass

        task = asyncio.create_task(run())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                await response.write(
                    ("data: " + json.dumps(event, ensure_ascii=False)
                     + "\n\n").encode("utf-8"))
        except (ConnectionResetError, asyncio.CancelledError):
            task.cancel()
            raise
        finally:
            _GENERATING.discard(avatar.id)
            if not task.done():
                task.cancel()
        await response.write_eof()
        return response

    async def autonomy_put(request):
        avatar, err = avatar_or_error(request.match_info.get("aid", ""))
        bad = live2d_or_error(avatar, err)
        if bad is not None:
            return bad
        body = await request.json()
        draft = (body or {}).get("profile")
        if not isinstance(draft, dict):
            return error("缺少 profile 对象")
        try:
            dossier = await asyncio.to_thread(
                run_audit, ctx.root, avatar.root / avatar.entry)
        except RuntimeError as e:
            return error(str(e), 502)
        clean, issues = autonomy_validate.validate(draft, dossier)
        fatal = [x for x in issues if x.fatal]
        if fatal:
            return json_response({
                "ok": False,
                "error": "配置校验未通过",
                "issues": [x.text() for x in issues],
                "profile": clean,
            }, 400)
        try:
            target = await asyncio.to_thread(autonomy_store.save, avatar, clean)
        except autonomy_store.ProfileError as e:
            return error(str(e), 500)
        return json_response({
            "ok": True,
            "path": schema.rel_path(ctx.root, target),
            "profile": clean,
            "issues": [x.text() for x in issues if not x.fatal],
            "needs_restart": True,
        })

    async def autonomy_delete(request):
        avatar, err = avatar_or_error(request.match_info.get("aid", ""))
        bad = live2d_or_error(avatar, err)
        if bad is not None:
            return bad
        try:
            await asyncio.to_thread(autonomy_store.delete, avatar)
        except autonomy_store.ProfileError as e:
            return error(str(e), 500)
        return json_response({"ok": True, "needs_restart": True})

    async def scan(_request):
        """清缓存、重扫 models；不重启 runtime。"""
        await asyncio.to_thread(ctx.catalog.rescan)
        return json_response({"ok": True, **_payload(ctx)})

    async def select(request):
        body = await request.json()
        avatar_id = str((body or {}).get("id") or "").strip()
        if not avatar_id:
            return error("缺少角色 id")
        avatar = ctx.catalog.get(avatar_id)
        if avatar is None:
            return error(f"角色不存在：{avatar_id}", 404)
        async with ctx.config_lock:
            raw = config.load_raw()
            if config.last_error:
                return error(
                    f"config.yaml 解析失败，已拒绝覆盖保存：{config.last_error}。"
                    "请先修复文件再切换角色。", 409)
            raw.setdefault("role", {})["id"] = avatar.role_id or avatar.id
            config.save_raw(raw)
        ctx.catalog.invalidate()
        return json_response({"ok": True,
                              "selected": avatar.id,
                              "selected_role": avatar.role_id or avatar.id,
                              "needs_restart": True, **_payload(ctx)})

    async def role_profile_get(request):
        avatar, err = avatar_or_error(request.match_info.get("aid", ""))
        if err is not None:
            return err
        if avatar.role_dir is not None:
            profile, read_err = await asyncio.to_thread(
                roles.read_profile_dir, roles.active_dir(avatar, ctx.root))
        else:
            profile, read_err = await asyncio.to_thread(
                roles.read_profile, ctx.root, avatar.role_id or avatar.id)
        directory = roles.active_dir(avatar, ctx.root)
        return json_response({
            "ok": True, "id": avatar.id,
            "role_id": avatar.role_id or avatar.id,
            "display_name": avatar.shown_name,
            "profile": profile,
            "role_dir": schema.rel_path(ctx.root, directory),
            "persona_dir": (schema.rel_path(ctx.root, directory)
                            if directory and directory.is_dir() else ""),
            "error": read_err,
        })

    async def role_profile_put(request):
        avatar, err = avatar_or_error(request.match_info.get("aid", ""))
        if err is not None:
            return err
        body = await request.json()
        if not isinstance(body, dict):
            return error("角色设定必须是对象")
        # 绑定写进 profile，而不是靠目录名：目录/模型改名后仍能按这个字段找回。
        body = {**body, "model": avatar.name}
        try:
            directory = await asyncio.to_thread(
                roles.ensure_dir, avatar, ctx.root)
            await asyncio.to_thread(roles.write_profile_dir, directory,
                                    body)
        except OSError as e:
            return error(f"角色设定写不进去：{e}", 500)
        ctx.catalog.invalidate()
        return await role_profile_get(request)

    async def role_persona_get(request):
        avatar, err = avatar_or_error(request.match_info.get("aid", ""))
        if err is not None:
            return err
        name = request.match_info.get("name", "")
        if name not in PERSONA_NAMES:
            return error("人设文件不存在", 404)
        content = await asyncio.to_thread(
            roles.read_text, roles.active_dir(avatar, ctx.root), name)
        return json_response({"ok": True, "name": name, "content": content})

    async def role_persona_put(request):
        avatar, err = avatar_or_error(request.match_info.get("aid", ""))
        if err is not None:
            return err
        name = request.match_info.get("name", "")
        if name not in PERSONA_NAMES:
            return error("人设文件不存在", 404)
        body = await request.json()
        text = str((body or {}).get("content") or "")
        if len(text) > MAX_TEXT:
            return error("文本太长")
        try:
            directory = await asyncio.to_thread(
                roles.ensure_dir, avatar, ctx.root)
            await asyncio.to_thread(roles.write_text, directory, name, text)
        except OSError as e:
            return error(f"人设写不进去：{e}", 500)
        ctx.catalog.invalidate()
        return json_response({"ok": True, "name": name,
                              "chars": len(text)})

    # ── 动作规则（属于角色设定）────────────────────────
    async def rules_get(request):
        avatar, err = avatar_or_error(request.match_info.get("aid", ""))
        if err is not None:
            return err
        return json_response({"ok": True, **avatar_rules.payload(avatar)})

    async def rules_put(request):
        avatar, err = avatar_or_error(request.match_info.get("aid", ""))
        if err is not None:
            return err
        body = await request.json()
        try:
            data = await asyncio.to_thread(
                avatar_rules.save, avatar,
                str((body or {}).get("content") or ""))
        except avatar_rules.RulesError as e:
            return error(str(e), 400)
        return json_response({"ok": True, **data})

    async def rules_generate(request):
        avatar, err = avatar_or_error(request.match_info.get("aid", ""))
        if err is not None:
            return err
        try:
            content = avatar_rules.build_text(avatar)
        except avatar_rules.RulesError as e:
            return error(str(e), 400)
        caps = getattr(avatar, "capabilities", None) or {}
        return json_response({"ok": True, "content": content,
                              "unknown_tags": avatar_rules.unknown_tags(content, caps)})

    router.add_post("/api/roles/scan", scan)
    router.add_post("/api/roles/select", select)
    router.add_get("/api/roles/{aid}/profile", role_profile_get)
    router.add_put("/api/roles/{aid}/profile", role_profile_put)
    router.add_get("/api/roles/{aid}/persona/{name}", role_persona_get)
    router.add_put("/api/roles/{aid}/persona/{name}", role_persona_put)
    router.add_get("/api/avatars/{aid}/rules", rules_get)
    router.add_put("/api/avatars/{aid}/rules", rules_put)
    router.add_post("/api/avatars/{aid}/rules/generate", rules_generate)
    router.add_get("/api/avatars/{aid}/autonomy", autonomy_get)
    router.add_post("/api/avatars/{aid}/autonomy/audit", autonomy_audit)
    router.add_post("/api/avatars/{aid}/autonomy/generate/stream",
                    autonomy_generate_stream)
    router.add_put("/api/avatars/{aid}/autonomy", autonomy_put)
    router.add_delete("/api/avatars/{aid}/autonomy", autonomy_delete)
