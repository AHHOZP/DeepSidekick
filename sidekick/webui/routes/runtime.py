"""运行时进程与状态：启停、/status、日志 tail/SSE。"""
from __future__ import annotations

import asyncio
import json

from aiohttp import web

from sidekick import config
from sidekick.config import llm_presets, secrets, tts_presets
from sidekick.webui.http import error, json_response
from sidekick.webui.context import WebContext


def tts_problem(cfg: dict) -> str | None:
    """当前 TTS profile 本身坏了（不是缺 key）；坏配置不允许启动。"""
    tts = cfg.get("tts") or {}
    if not bool(tts.get("enabled")):
        return None
    source = str(tts.get("source") or "").strip()
    block = tts.get(source)
    preset = (tts_presets.get(str((block or {}).get("preset") or ""))
              if isinstance(block, dict) else None)
    if not isinstance(block, dict) or preset is None:
        return f"tts.source 配置坏了：{source or '（空）'}"
    return None


def _has_secret_header(profile: dict) -> bool:
    """profile 里是否已配置密钥型 header（例如 X-Api-Key）。"""
    headers = profile.get("headers")
    if not isinstance(headers, dict):
        return False
    return any(secrets.is_secret_header(name) and str(value or "").strip()
               for name, value in headers.items())


def missing_config(cfg: dict) -> list[str]:
    """启动前必须补的配置；本地无鉴权服务、自定义 profile 不要求 key。"""
    out: list[str] = []
    llm = cfg.get("llm") or {}
    source = str(llm.get("source") or "").strip()
    profile = llm.get(source)
    if not isinstance(profile, dict):
        out.append(f"llm.{source or 'source'}")
    else:
        for key in ("base_url", "model"):
            if not str(profile.get(key) or "").strip():
                out.append(f"llm.{source}.{key}")
        preset = llm_presets.get(source)
        if (preset is not None and preset.get("needs_key", True)
                and not str(profile.get("api_key") or "").strip()
                and not _has_secret_header(profile)):
            out.append(f"llm.{source}.api_key")
    tts = cfg.get("tts") or {}
    tts_source = str(tts.get("source") or "").strip()
    block = tts.get(tts_source)
    preset = (tts_presets.get(str((block or {}).get("preset") or ""))
              if isinstance(block, dict) else None)
    if (bool(tts.get("enabled")) and preset is not None
            and preset.get("auth")
            and not str(block.get("api_key") or "").strip()):
        out.append(f"tts.{tts_source}.api_key")
    return out


def _missing_text(cfg: dict, path: str) -> str:
    """把缺失路径翻成「DeepSeek 的 API Key」这种文案。"""
    if path.startswith("tts."):
        return path
    bits = path.split(".")
    source = bits[1] if len(bits) > 1 else path
    label = str((llm_presets.get(source) or {}).get("label") or source)
    what = {"base_url": "Base URL", "model": "模型",
            "api_key": "API Key"}.get(bits[-1] if len(bits) > 2 else "")
    return f"{label} 的 {what}" if what else f"{label} 的配置"


def _start_gate() -> dict | None:
    """WebUI 启动/重启前的门槛；返回错误响应体，None 表示放行。"""
    cfg = config.load()
    if config.last_error:
        return {"ok": False, "error": f"配置读取失败：{config.last_error}"}
    problem = tts_problem(cfg)
    if problem:
        return {"ok": False, "error": problem, "page": "tts"}
    missing = missing_config(cfg)
    if not missing:
        return None
    pages = {("tts" if name.startswith("tts.") else "model") for name in missing}
    page = "tts" if pages == {"tts"} else "model"
    msg = "启动前还缺：" + " / ".join(_missing_text(cfg, n) for n in missing)
    if secrets.last_error:
        msg += f"；另：{secrets.last_error}"
    return {"ok": False, "error": msg, "missing": missing, "page": page}


def add_routes(router: web.UrlDispatcher, ctx: WebContext) -> None:
    async def status(request):
        force = request.query.get("refresh") == "1"
        snap = await ctx.feed.snapshot(force=force) or {}
        return json_response({"ok": True, **snap})

    async def logs(request):
        try:
            n = int(request.query.get("n", 200))
        except (TypeError, ValueError):
            return error("n 必须是整数")
        return json_response({"ok": True, "lines": ctx.supervisor.tail(n)})

    def _event(name: str, data: dict) -> bytes:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return f"event: {name}\ndata: {payload}\n\n".encode("utf-8")

    async def events(request):
        """SSE：一条连接同时收状态和日志。"""
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        q = ctx.feed.subscribe()
        try:
            await resp.write(b"retry: 3000\n\n")
            while True:
                try:
                    name, data = await asyncio.wait_for(q.get(), 15.0)
                except asyncio.TimeoutError:
                    await resp.write(b": keep-alive\n\n")
                    continue
                await resp.write(_event(name, data))
        except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
            pass
        finally:
            ctx.feed.unsubscribe(q)
            try:
                await resp.write_eof()
            except Exception:
                pass
        return resp

    async def runtime_start(_request):
        gate = _start_gate()
        if gate is not None:
            return json_response(gate)
        return json_response(await ctx.supervisor.start())

    async def runtime_stop(_request):
        return json_response(await ctx.supervisor.stop())

    async def runtime_restart(_request):
        gate = _start_gate()
        if gate is not None:
            return json_response(gate)
        return json_response(await ctx.supervisor.restart())

    router.add_get("/api/status", status)
    router.add_get("/api/logs", logs)
    router.add_get("/api/events/stream", events)
    router.add_post("/api/runtime/start", runtime_start)
    router.add_post("/api/runtime/stop", runtime_stop)
    router.add_post("/api/runtime/restart", runtime_restart)
