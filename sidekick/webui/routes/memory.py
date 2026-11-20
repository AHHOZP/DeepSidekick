"""长期记忆、归档任务与语义模型管理的 WebUI 接口。"""
from __future__ import annotations

import asyncio
import secrets as pysecrets
import shutil
import time
from pathlib import Path

from aiohttp import web

from sidekick import config
from sidekick.core.atomic import dir_size
from sidekick.memory.embed import catalog, download
from sidekick.webui.http import error, json_response, kw_list
from sidekick.webui.context import WebContext

# scope 和 kind 不是自由组合：写错组合的行读取方永远看不到。
MEMORY_KINDS = {
    "live": ("summary",),
    "game": ("plot", "npc", "quests"),
}


class MemoryRoutes:
    def __init__(self, ctx: WebContext) -> None:
        self.ctx = ctx

    def check_scope_kind(self, scope: str, kind: str) -> str | None:
        if scope not in MEMORY_KINDS:
            return "scope 只能是 live / game"
        if kind not in MEMORY_KINDS[scope]:
            return f"{scope} 的 kind 只能是 " + " / ".join(MEMORY_KINDS[scope])
        return None

    def store(self):
        svc = self.ctx.get_memory()
        return svc.store if svc is not None else None

    def no_memory(self):
        err = self.ctx.memory_error()
        if err:
            return error(f"记忆库不可用：{err}", 503)
        return error("记忆已关闭（memory.enabled=false）", 409)

    def current_gid(self) -> str:
        return config.resolve_game_id(self.ctx.cfg())

    # ── 记忆 ──────────────────────────────────────────
    async def memories(self, request):
        s = self.store()
        if s is None:
            return self.no_memory()
        q = request.query
        kind = q.get("kind") or None
        scope = q.get("scope") or None
        game_id = q.get("game_id")
        if scope == "game" and game_id is None:
            game_id = self.current_gid()
        try:
            limit = int(q.get("limit", 1000))
        except (TypeError, ValueError):
            return error("limit 必须是整数")
        items = await asyncio.to_thread(
            s.list_memories, scope=scope, kind=kind,
            game_id=game_id if game_id is not None else None,
            enabled=True, limit=limit)
        return json_response({"ok": True, "items": items})

    async def memory_add(self, request):
        s = self.store()
        if s is None:
            return self.no_memory()
        b = await request.json()
        content = str((b or {}).get("content") or "").strip()
        if not content:
            return error("content 不能为空")
        scope = str((b or {}).get("scope") or "live")
        kind = str((b or {}).get("kind") or "summary")
        bad = self.check_scope_kind(scope, kind)
        if bad:
            return error(bad)
        game_id = str((b or {}).get("game_id") or "")
        if scope == "live":
            game_id = ""
        elif scope == "game" and not game_id:
            game_id = self.current_gid()
        item = await asyncio.to_thread(
            s.add_memory,
            scope=scope, kind=kind, content=content,
            title=str((b or {}).get("title") or ""),
            keywords=kw_list((b or {}).get("keywords")),
            game_id=game_id, source="manual")
        return json_response({"ok": True, "item": item})

    async def memory_put(self, request):
        s = self.store()
        if s is None:
            return self.no_memory()
        b = await request.json()
        fields = {}
        # 记忆常驻/关键词由运行时自动决定，WebUI 不再允许改。
        for k in ("title", "content", "keywords", "scope", "kind",
                  "game_id"):
            if k in (b or {}):
                fields[k] = b[k]
        if "keywords" in fields:
            fields["keywords"] = kw_list(fields["keywords"])
        if "scope" in fields or "kind" in fields:
            bad = self.check_scope_kind(
                str(fields.get("scope") or "live"),
                str(fields.get("kind") or "summary"))
            if bad:
                return error(bad)
        if "scope" in fields:
            if str(fields["scope"]) == "live":
                fields["game_id"] = ""
            elif not fields.get("game_id"):
                fields["game_id"] = self.current_gid()
        await asyncio.to_thread(
            s.update_memory, request.match_info["mid"], **fields)
        return json_response({"ok": True})

    async def memory_delete(self, request):
        s = self.store()
        if s is None:
            return self.no_memory()
        await asyncio.to_thread(s.delete_memory, request.match_info["mid"])
        return json_response({"ok": True})

    # ── 语义模型 ──────────────────────────────────────
    def embed_root(self) -> Path:
        mem = self.ctx.cfg()["memory"]
        rel = Path(str(mem.get("embed_dir") or "state/embed"))
        return rel if rel.is_absolute() else self.ctx.root / rel

    def under_root(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.embed_root().resolve())
        except ValueError:
            return False
        return path.resolve() != self.embed_root().resolve()

    @staticmethod
    def _cleanup_jobs(jobs: dict) -> None:
        for jid, old in list(jobs.items()):
            if old.get("status") != "running" and len(jobs) > 20:
                jobs.pop(jid, None)

    async def model_status(self, _request):
        mem = self.ctx.cfg()["memory"]
        current = str(mem.get("embed_model") or "")
        root = self.embed_root()
        out = []
        for entry in catalog.all_entries():
            dest = root / entry.id
            miss = entry.missing(dest)
            out.append({
                "id": entry.id, "name": entry.name, "bytes": entry.bytes,
                "size": entry.size, "note": entry.note,
                "max_context": entry.max_context,
                "langs": entry.langs,
                "latency": entry.latency, "mem": entry.mem,
                "cmteb": entry.cmteb, "mteb": entry.mteb,
                "mmteb": entry.mmteb,
                "downloaded": not miss, "missing": miss,
                "disk": await asyncio.to_thread(dir_size, dest),
                "current": entry.id == current,
            })
        out.sort(key=lambda x: x["bytes"])
        jobs = [j for j in self.ctx.jobs.values()
                if j.get("kind") == "embed"]
        return json_response({"ok": True, "models": out, "current": current,
                              "root": str(root), "jobs": jobs[-5:]})

    async def model_download(self, request):
        b = await request.json()
        model = str((b or {}).get("model") or "")
        if not bool((b or {}).get("confirm")):
            return error("下载前需要在 UI 里确认")
        entry = catalog.get(model)
        if entry is None:
            return error("不支持的模型")
        self._cleanup_jobs(self.ctx.jobs)
        if any(j.get("status") == "running" and j.get("kind") == "embed"
               and j.get("model") == model for j in self.ctx.jobs.values()):
            return error("该模型正在下载，请等当前任务结束", 409)
        dest = self.embed_root() / entry.id
        if not self.under_root(dest):
            return error("模型目录不在 embed_dir 下，拒绝下载", 409)
        if not entry.missing(dest):
            return json_response({"ok": True, "already": True, "model": model})

        jid = pysecrets.token_hex(6)
        job = {"id": jid, "model": model, "kind": "embed", "status": "running",
               "stage": "下载", "source": "", "bytes": 0,
               "total": entry.bytes, "error": "", "file": "",
               "started": time.time()}
        self.ctx.jobs[jid] = job

        async def run():
            try:
                await asyncio.to_thread(download.install, entry, dest, job)
                job["status"] = "done"
                job["stage"] = "完成"
            except Exception as e:
                job["status"] = "failed"
                job["error"] = f"{type(e).__name__}: {e}"
            finally:
                job["bytes"] = await asyncio.to_thread(dir_size, dest)

        asyncio.create_task(run())
        return json_response({"ok": True, "job": job})

    async def model_job(self, request):
        job = self.ctx.jobs.get(request.match_info.get("jid", ""))
        if not job or job.get("kind") != "embed":
            return error("任务不存在", 404)
        return json_response({"ok": True, "job": job})

    async def model_delete(self, request):
        body = await request.json()
        model = str((body or {}).get("model") or "")
        if not bool((body or {}).get("confirm")):
            return error("删除前需要在 UI 里确认")
        if model == str(self.ctx.cfg()["memory"].get("embed_model") or ""):
            return error("当前正在使用的模型不能删除；先切换模型再删", 409)
        try:
            runtime = (await self.ctx.supervisor.status()).get("runtime") or {}
            running_model = (runtime.get("embedding") or {}).get("model")
        except Exception:
            running_model = None
        if running_model and model == str(running_model):
            return error("runtime 仍在用这个模型；先停止 runtime 再删", 409)
        if any(j.get("status") == "running" and j.get("kind") == "embed"
               and j.get("model") == model for j in self.ctx.jobs.values()):
            return error("该模型正在下载，等任务结束再删", 409)
        entry = catalog.get(model)
        if entry is None:
            return error("模型不存在", 404)
        dest = self.embed_root() / entry.id
        if not dest.is_dir():
            return error("模型目录不存在", 404)
        if not self.under_root(dest):
            return error("模型目录不在 embed_dir 下，拒绝删除", 409)
        try:
            await asyncio.to_thread(shutil.rmtree, dest)
        except OSError as e:
            return error(f"删除失败：{type(e).__name__}: {e}", 500)
        return json_response({"ok": True, "model": model})


def add_routes(router: web.UrlDispatcher, ctx: WebContext) -> None:
    routes = MemoryRoutes(ctx)
    router.add_get("/api/memories", routes.memories)
    router.add_post("/api/memories", routes.memory_add)
    router.add_put("/api/memories/{mid}", routes.memory_put)
    router.add_delete("/api/memories/{mid}", routes.memory_delete)
    router.add_get("/api/memory/model", routes.model_status)
    router.add_post("/api/memory/model/download", routes.model_download)
    router.add_get("/api/memory/model/download/{jid}", routes.model_job)
    router.add_post("/api/memory/model/delete", routes.model_delete)
