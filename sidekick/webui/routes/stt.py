"""STT 模型清单、下载任务与进度。"""
from __future__ import annotations

import asyncio
import secrets as pysecrets
import shutil
import time
from pathlib import Path

from aiohttp import web

from sidekick.core.atomic import dir_size
from sidekick.input.stt.engines import catalog, registry
from sidekick.input.stt.engines.download import fetch_and_extract
from sidekick.webui.http import error, json_response
from sidekick.webui.context import WebContext


def add_routes(router: web.UrlDispatcher, ctx: WebContext) -> None:
    def stt_cfg() -> dict:
        return ctx.cfg().get("stt") or {}

    def stt_root() -> Path:
        rel = str(stt_cfg()["model_dir"])
        p = Path(rel)
        return p if p.is_absolute() else ctx.root / p

    def load_specs() -> dict[str, registry.ModelSpec]:
        try:
            return registry.load_specs(ctx.cfg(), ctx.root)
        except Exception:
            return {}

    def spec_of(mid: str, kind: str, specs: dict[str, registry.ModelSpec]):
        """配置里有就用配置的（args 可能写了文件名），没有就按目录纯推断。"""
        return specs.get(mid) or registry.ModelSpec(
            id=mid, kind=kind, dir=stt_root() / mid)

    def under_root(path: Path) -> bool:
        try:
            path.resolve().relative_to(stt_root().resolve())
        except ValueError:
            return False
        return path.resolve() != stt_root().resolve()

    async def stt_models(_request):
        st = stt_cfg()
        root = stt_root()
        current = str(st.get("model") or "")
        spec_error = None
        try:
            specs = registry.load_specs(ctx.cfg(), ctx.root)
        except Exception as e:
            specs = {}
            spec_error = f"{type(e).__name__}: {e}"

        def rel_missing(spec: registry.ModelSpec) -> list[str]:
            out = []
            for p in spec.missing():
                try:
                    out.append(str(Path(p).relative_to(spec.dir)))
                except ValueError:
                    out.append(str(p))
            return out

        out = []
        for e in catalog.all_entries():
            spec = spec_of(e.id, e.kind, specs)
            miss = rel_missing(spec)
            out.append({
                "id": e.id, "name": e.name, "kind": e.kind,
                "size": e.size, "bytes": e.bytes,
                "rtf": e.rtf, "mem": e.mem,
                "aishell1": e.aishell1, "librispeech": e.librispeech,
                "note": e.note, "langs": e.langs,
                "downloaded": not miss, "missing": miss,
                "configured": e.id in specs,
                "downloadable": True,
                "disk": await asyncio.to_thread(dir_size, spec.dir),
                "current": e.id == current,
            })
        # 手写进 stt.models、但不在下载清单里的模型也要能看到装好没。
        seen = {e.id for e in catalog.all_entries()}
        for mid, spec in specs.items():
            if mid in seen:
                continue
            miss = rel_missing(spec)
            out.append({
                "id": mid, "name": mid, "kind": spec.kind,
                "size": "本机", "bytes": 0,
                "rtf": "—", "mem": "—", "aishell1": "—", "librispeech": "—",
                "note": "手写配置（没有下载入口）", "langs": "",
                "downloaded": not miss, "missing": miss,
                "configured": True,
                "downloadable": False,
                "disk": await asyncio.to_thread(dir_size, spec.dir),
                "current": mid == current,
            })
        out.sort(key=lambda m: (m["bytes"] or 10**12))
        jobs = [j for j in ctx.jobs.values()
                if j.get("kind") != "embed"
                and j.get("model") in {m["id"] for m in out}]
        jobs.sort(key=lambda j: j.get("started") or 0)
        return json_response({"ok": True, "models": out, "root": str(root),
                              "current": current, "error": spec_error,
                              "jobs": jobs[-5:]})

    async def stt_download(request):
        b = await request.json()
        model = str((b or {}).get("model") or "")
        mirror = bool((b or {}).get("mirror"))
        if not bool((b or {}).get("confirm")):
            return error("下载前需要在 UI 里确认")

        entry = catalog.get(model)
        if entry is None:
            return error("不支持的模型")

        # 只留最近 20 个已结束任务，避免 jobs 无限增长。
        for jid, old in list(ctx.jobs.items()):
            if old.get("status") != "running" and len(ctx.jobs) > 20:
                ctx.jobs.pop(jid, None)
        if any(j.get("status") == "running" and j.get("model") == model
               for j in ctx.jobs.values()):
            return error("该模型正在下载，请等当前任务结束", 409)

        try:
            specs = load_specs()
        except Exception:
            specs = {}
        spec = spec_of(entry.id, entry.kind, specs)
        dest = spec.dir
        if not under_root(dest):
            return error("模型目录不在 model_dir 下，拒绝下载", 409)
        if registry.REQUIRED_FILES.get(entry.kind) and not spec.missing():
            return json_response({"ok": True, "already": True, "model": model})

        jid = pysecrets.token_hex(6)
        job = {"id": jid, "model": model, "status": "running", "stage": "下载",
               "bytes": 0, "total": entry.bytes, "error": "",
               "started": time.time()}
        ctx.jobs[jid] = job

        async def run():
            try:
                await asyncio.to_thread(fetch_and_extract, entry, dest, mirror, job)
                miss = spec.missing()
                if miss:
                    raise RuntimeError("解压后没找到需要的模型文件，检查下载包是否完整")
                job["status"] = "done"
                job["stage"] = "完成"
            except Exception as e:
                job["status"] = "failed"
                job["error"] = f"{type(e).__name__}: {e}"
            finally:
                job["bytes"] = await asyncio.to_thread(dir_size, dest)

        asyncio.create_task(run())
        return json_response({"ok": True, "job": job})

    async def stt_job(request):
        job = ctx.jobs.get(request.match_info.get("jid", ""))
        if not job:
            return error("任务不存在", 404)
        return json_response({"ok": True, "job": job})

    async def stt_delete(request):
        body = await request.json()
        model = str((body or {}).get("model") or "")
        if not bool((body or {}).get("confirm")):
            return error("删除前需要在 UI 里确认")
        if model == str(stt_cfg().get("model") or ""):
            return error("当前正在使用的模型不能删除；先切换模型再删", 409)
        try:
            runtime = (await ctx.supervisor.status()).get("runtime") or {}
            running_model = (runtime.get("stt") or {}).get("model")
        except Exception:
            running_model = None
        if running_model and model == str(running_model):
            return error("runtime 仍在用这个模型；先停止 runtime 再删", 409)
        if any(j.get("status") == "running" and j.get("model") == model
               for j in ctx.jobs.values()):
            return error("该模型正在下载，等任务结束再删", 409)
        entry = catalog.get(model)
        specs = load_specs()
        if entry is None and model not in specs:
            return error("模型不存在", 404)
        spec = spec_of(model, entry.kind if entry else specs[model].kind, specs)
        dest = spec.dir
        if not dest.is_dir():
            return error("模型目录不存在", 404)
        if not under_root(dest):
            return error("模型目录不在 model_dir 下，拒绝删除", 409)
        try:
            await asyncio.to_thread(shutil.rmtree, dest)
        except OSError as e:
            return error(f"删除失败：{type(e).__name__}: {e}", 500)
        return json_response({"ok": True, "model": model})

    router.add_get("/api/stt/models", stt_models)
    router.add_post("/api/stt/download", stt_download)
    router.add_get("/api/stt/download/{jid}", stt_job)
    router.add_post("/api/stt/delete", stt_delete)
