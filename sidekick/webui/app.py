"""DeepSidekick WebUI：配置中心 + 记忆编辑 + 运行时启停。

路由按 domain 拆在 `sidekick/webui/routes/`：
  · config    配置 / 密钥 / 模型测试 / TTS 试听
  · runtime   进程启停 / status / 日志
  · avatar    形象扫描 / 选择 / 角色设定 / actions.md
  · users     用户人设
  · stage     形象层状态 / 控制
  · worldbook 游戏资料 / 世界书条目
  · memory    长期记忆 / 任务 / 游戏状态
  · stt       STT 模型清单 / 下载
  · presets   对话预设（块编辑）
  · packages  预设/世界书/角色的导入导出
  · preview   把这一轮真正会发出去的 messages 拼一遍
  · page      页面 / 静态资源 / 预览

所有写操作要求 CSRF token；密钥只写不读；runtime 作为子进程由 Supervisor 管理。
"""
from __future__ import annotations

import asyncio
import json
import secrets as pysecrets
import threading
import time

from aiohttp import web

from sidekick import config
from sidekick.avatar.catalog import AvatarCatalog
from sidekick.core.http_json import is_loopback_origin
from sidekick.core.log import configure as configure_log
from sidekick.core.log import log
from sidekick.memory.long_term import MemoryFacade
from sidekick.webui.routes import avatar as avatar_routes, config as config_routes, memory as memory_routes, page as page_routes, packages as packages_routes, presets as presets_routes, preview as preview_routes, runtime as runtime_routes, stage as stage_routes, stt as stt_routes, users as users_routes, worldbook as worldbook_routes
from sidekick.webui.http import json_response
from sidekick.webui.context import WebContext
from sidekick.webui.status_feed import StatusFeed
from sidekick.webui.supervisor import Supervisor

ROOT = config.ROOT


def _role_id() -> str:
    return str((config.load().get("role") or {}).get("id") or "")


class WebUI:
    def __init__(self, *, config_notice: str | None = None) -> None:
        self.root = ROOT
        self.config_notice = config_notice
        self.supervisor = Supervisor(ROOT)
        self.csrf = pysecrets.token_urlsafe(32)
        self._memory: MemoryFacade | None = None
        self._memory_key: tuple | None = None
        self._memory_error: str | None = None
        # get_memory 会被事件循环和 asyncio.to_thread 同时调用；读写/关闭
        # facade 必须串行，否则 status 采样可能撞上旧库 close。
        self._memory_lock = threading.RLock()
        self._catalog = AvatarCatalog(ROOT, selection=_role_id)
        self.jobs: dict[str, dict] = {}
        self.started_at = time.time()
        self.feed = StatusFeed(self.supervisor, self.memory_stats,
                               started_at=self.started_at)
        self.supervisor.on_exit = self.feed.wake

    # ── 记忆对象 ──────────────────────────────────────
    def get_memory(self, force: bool = False) -> MemoryFacade | None:
        with self._memory_lock:
            try:
                cfg = config.load()
                if config.last_error:
                    self._memory_error = f"config.yaml: {config.last_error}"
                    old = self._memory
                    self._memory = None
                    self._memory_key = None
                    if old is not None:
                        old.close()
                    return None
                key = (
                    str(cfg["memory"]["db_path"]),
                    config.resolve_game_id(cfg),
                    bool(cfg["memory"]["enabled"]),
                    json.dumps(cfg["memory"], sort_keys=True,
                               ensure_ascii=False, default=str),
                )
                if self._memory is not None and self._memory_key == key:
                    return self._memory
                new = MemoryFacade(cfg, getattr(self, "root", ROOT))
            except Exception as e:
                # 打开失败时不要把旧库继续当成新库可写；返回 None，让路由报 503。
                self._memory_error = f"{type(e).__name__}: {e}"
                old = self._memory
                self._memory = None
                self._memory_key = None
                if old is not None:
                    old.close()
                log(f"记忆服务打开失败：{self._memory_error}")
                return None
            old = self._memory
            self._memory = new
            self._memory_key = key
            self._memory_error = None
            if old is not None:
                old.close()
            return self._memory

    def memory_stats(self) -> dict:
        """状态端点只读；不构造 MemoryFacade。"""
        with self._memory_lock:
            out = self._memory.stats() if self._memory is not None else {}
            err = getattr(self, "_memory_error", None)
            if err:
                out = {**out, "error": err}
            return out

    def close(self) -> None:
        self.feed.close()
        with self._memory_lock:
            if self._memory is not None:
                self._memory.close()
                self._memory = None

    # ── 启动 ──────────────────────────────────────────
    def app(self) -> web.Application:
        web_app = web.Application(client_max_size=4 * 1024 * 1024,
                                  middlewares=[self._guard, self._errors])
        ctx = WebContext(
            root=getattr(self, "root", ROOT),
            cfg=config.load,
            catalog=self._catalog,
            supervisor=self.supervisor,
            get_memory=self.get_memory,
            memory_error=lambda: getattr(self, "_memory_error", None),
            feed=self.feed,
            csrf=self.csrf,
            config_notice=getattr(self, "config_notice", "") or "",
            jobs=self.jobs,
        )
        r = web_app.router
        page_routes.add_routes(r, ctx)
        config_routes.add_routes(r, ctx)
        runtime_routes.add_routes(r, ctx)
        avatar_routes.add_routes(r, ctx)
        users_routes.add_routes(r, ctx)
        stage_routes.add_routes(r, ctx)
        worldbook_routes.add_routes(r, ctx)
        memory_routes.add_routes(r, ctx)
        stt_routes.add_routes(r, ctx)
        presets_routes.add_routes(r, ctx)
        packages_routes.add_routes(r, ctx)
        preview_routes.add_routes(r, ctx)
        return web_app

    # ── 中间件 ────────────────────────────────────────
    @web.middleware
    async def _errors(self, request: web.Request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:
            import traceback

            traceback.print_exc()
            return json_response(
                {"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        raw_host = request.host or ""
        if raw_host.startswith("["):
            host = raw_host[1:raw_host.find("]")].lower()
        else:
            host = raw_host.rsplit(":", 1)[0].lower() if ":" in raw_host else raw_host.lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise web.HTTPForbidden(text="只允许本机访问")
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            token = request.headers.get("X-CSRF-Token", "")
            if not pysecrets.compare_digest(token, self.csrf):
                raise web.HTTPForbidden(text="CSRF token 不匹配")
            origin = request.headers.get("Origin", "")
            if origin and not is_loopback_origin(origin):
                raise web.HTTPForbidden(text="Origin 不匹配")
        return await handler(request)


async def serve() -> None:
    created, config_error = config.ensure_default_config()
    cfg = config.load()
    notice = ""
    if created:
        notice = "已生成默认 config.yaml，请先配置模型/TTS 的 key，再点启动。"
    elif config_error:
        notice = f"未生成默认 config.yaml：{config_error}"
    w = cfg["webui"]
    host = str(w["host"])
    port = int(w["port"])
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("WebUI 只能绑定 loopback（127.0.0.1 / localhost / ::1）")
    ui = WebUI(config_notice=notice)
    ui.get_memory()
    app = ui.app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    url = f"http://{url_host}:{port}/"
    print(f"[webui] {url}", flush=True)
    if bool(w["auto_open"]):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        await asyncio.Event().wait()
    finally:
        ui.close()
        await ui.supervisor.shutdown()
        await runner.cleanup()


def main() -> None:
    configure_log(ROOT, "webui.log")
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
