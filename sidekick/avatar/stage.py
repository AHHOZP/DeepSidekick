"""直播姬里的形象层：本地网页 + WebSocket 推事件。

服务只做三件事：发页面、发模型、推事件。页面不能反向控制说话或配置；
桌面桌宠只允许通过 `/ws` 上报点击，交给 runtime 变成 brain 材料。

形象目录由 `AvatarCatalog` 注入（`runtime.py` 构建），本模块不 import 项目根。
`/config.json` 和 WebUI 预览共用 `config_payload()`，避免两边布局默认值分叉。
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

from sidekick.avatar import roles
from sidekick.core.http_json import CONTROL_HEADER, is_loopback_origin, json_response

#: 高频、可丢的实时帧；say/page/end/control 等控制消息永远保留。
_LIVE_EVENTS = ("level", "mouth", "pose")


class StageClient:
    """speech/display → 页面的事件通道。没连人时 push 是空操作。"""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()
        self.clients = 0
        self.sent = 0
        self.dropped = 0
        self.error: str | None = None
        self.last_event = ""
        self.report: dict = {}
        self.notice = ""

    def attach(self, q: asyncio.Queue) -> None:
        self._queues.add(q)
        self.clients = len(self._queues)

    def detach(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)
        self.clients = len(self._queues)
        if not self._queues:
            self.report = {}

    # overlay/app.js 只发这些字段；其余不进入 /status，避免本地页面注入任意内容。
    _REPORT_FIELDS = ("renderer", "err", "maxMouth", "level", "mouth", "form",
                      "poseSeq", "poseParams", "poseWrites", "animation",
                      "version", "autonomy", "page", "pages", "cps", "w", "h",
                      "avatar")

    def set_report(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        clean = {}
        for key in self._REPORT_FIELDS:
            if key not in data:
                continue
            value = data[key]
            if isinstance(value, (int, float)):
                try:
                    if not math.isfinite(float(value)):
                        continue
                except (OverflowError, ValueError):
                    continue
            clean[key] = value[:200] if isinstance(value, str) else value
        self.report = clean

    def set_notice(self, text: str) -> None:
        """桌宠那边的一次性提示（口型不可用、图片打不开之类），只进 /status 给 WebUI 显示。"""
        self.notice = str(text or "")[:200]

    def emit(self, event: str, **kw) -> None:
        if not self._queues:
            return
        msg = {"t": event, **kw}
        self.last_event = event
        sent_any = False
        for q in list(self._queues):
            if event in _LIVE_EVENTS and q.qsize() > 32:
                self.dropped += 1
                continue
            if q.full():
                _drop_live(q)
            if q.full() and event not in _LIVE_EVENTS:
                # 实时帧不能挤掉控制消息；控制消息太多时丢最旧的一条，
                # 保证最新的 say/page/end 能到达。
                try:
                    q.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(msg)
                sent_any = True
            except asyncio.QueueFull:
                self.dropped += 1
                self.error = f"{event} 队列满，已丢事件"
        if sent_any:
            self.sent += 1
            self.error = None

    def stats(self) -> dict:
        return {"clients": self.clients, "sent": self.sent,
                "dropped": self.dropped, "error": self.error,
                "last_event": self.last_event, "report": self.report,
                "notice": self.notice}


def overlay_dir(catalog) -> Path:
    return Path(catalog.root) / "overlay"


def layout_payload(stage_cfg: dict) -> dict:
    s = stage_cfg
    b = s["bubble"]
    return {
        "gap": max(-50.0, min(50.0, float(b["gap"]))),
        "maxWidth": max(20.0, min(100.0, float(b["max_width"]))),
        "autoGap": bool(b["auto_gap"]),
    }


def config_payload(catalog, cfg: dict, want: str | None = None,
                   autonomy_active: bool | None = None) -> dict:
    """当前（或指定）形象的页面配置；stage 与 WebUI 预览共用。"""
    want = str(want or "").strip()
    hit = catalog.get(want) if want and want != "live2d" else None
    selected = hit or catalog.selected()
    data = selected.to_dict()
    if selected.kind == "spine":
        # 浏览器的 canvas 会把预乘图解预乘后再交回来，自己判永远得到“预乘”，
        # 所以由服务端给一个可信结论（与桌面渲染器共用 sidekick/spine/pma.py）。
        from sidekick.spine.pma import pma_of

        atlas = Path(selected.root) / Path(selected.entry).with_suffix(".atlas").name
        pma = pma_of(atlas)          # None = 无法判定；宁可先按 PMA 处理，避免黑边
        data["pma"] = True if pma is None else bool(pma)
    data.update({
        "avatar": selected.id,
        "autonomy": {
            "enabled": bool(((cfg.get("stage") or {}).get("autonomy") or {})
                            .get("enabled", True)),
            "active": (bool(autonomy_active) if autonomy_active is not None
                       else bool(((cfg.get("stage") or {}).get("autonomy") or {})
                                 .get("active", True))),
        },
        "layout": layout_payload(
            roles.merge_stage(cfg.get("stage") or {}, selected.stage)),
    })
    return data


def resolve_model(catalog, avatar_id: str, rel: str):
    """返回 model3 dict / 文件 Path / None（不存在）。WebUI 与 stage 共用。

    Spine 形象同样走这里：浏览器按 `kind` 决定用哪套播放器，骨架/图集/页图都是普通文件。
    """
    avatar = catalog.get(avatar_id)
    if avatar is None or avatar.kind not in ("live2d", "spine", "image"):
        return None
    if rel == "__avatar.model3.json":
        if avatar.kind != "live2d":
            return None
        return catalog.effective_model3(avatar)
    base = avatar.root.resolve()
    try:
        p = (base / rel).resolve()
        p.relative_to(base)
    except (OSError, ValueError):
        return None
    return p if p.is_file() else None


def preview_html(catalog) -> str:
    """WebUI 预览用：把 /s/ 资源改成 /ov/，并标记 preview。"""
    html = (overlay_dir(catalog) / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/s/', 'href="/ov/')
    html = html.replace('src="/s/', 'src="/ov/')
    return html.replace("<body>", '<body class="preview">')


class PreviewHandlers:
    """WebUI 预览端点：页面、配置、模型。

    stage 与本类共用 `config_payload/resolve_model`；HTTP 包装只留这一份，
    WebUI 只负责挂路由，不再自己拼缓存头和错误处理。
    """

    def __init__(self, catalog, cfg_provider, autonomy_active=None) -> None:
        self.catalog = catalog
        self.cfg_provider = cfg_provider
        self.autonomy_active = autonomy_active

    def _cfg(self) -> dict:
        return self.cfg_provider()

    @staticmethod
    def _json(data: dict, status: int = 200, *, cache: str = "no-store"):
        return json_response(data, status, headers={"Cache-Control": cache})

    async def index(self, _request):
        from aiohttp import web

        html = await asyncio.to_thread(preview_html, self.catalog)
        return web.Response(
            body=html.encode("utf-8"),
            content_type="text/html", charset="utf-8",
            headers={"Cache-Control": "no-store"})

    async def config(self, request):
        cfg = self._cfg()
        want = (request.query.get("avatar") or "").strip()
        active = (self.autonomy_active() if self.autonomy_active is not None
                  else None)
        data = await asyncio.to_thread(
            config_payload, self.catalog, cfg, want, active)
        return self._json(data)

    async def model(self, request):
        from aiohttp import web

        got = await asyncio.to_thread(
            resolve_model, self.catalog,
            request.match_info.get("id", ""),
            request.match_info.get("path", ""))
        if isinstance(got, dict):
            return self._json(got, cache="no-cache")
        if got is None:
            raise web.HTTPNotFound()
        return web.FileResponse(got, headers={"Cache-Control": "no-cache"})


async def serve_stage(client: StageClient, cfg: dict,
                      cfg_provider=None, catalog=None,
                      on_click=None, autonomy_active=None) -> object:
    from aiohttp import web

    if catalog is None:
        raise RuntimeError("stage 需要注入形象目录（AvatarCatalog）")
    overlay = overlay_dir(catalog)

    def current_cfg() -> dict:
        return cfg_provider() if cfg_provider is not None else cfg

    s = cfg["stage"]
    port = int(s["port"])
    host = str(s["host"])
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError(f"stage.host 只允许 loopback，当前 {host!r}")
    handlers = PreviewHandlers(catalog, current_cfg, autonomy_active)
    on_model = handlers.model
    on_config = handlers.config

    async def on_index(_):
        html = (overlay / "index.html").read_text(encoding="utf-8")
        for name in ("app.js", "inspect.js", "style.css",
                     "lib/pixi.min.js", "lib/live2d-engine.min.js",
                     "lib/live2dcubismcore.min.js"):
            f = overlay / name
            if f.exists():
                html = html.replace(f"/s/{name}",
                                    f"/s/{name}?v={int(f.stat().st_mtime)}")
        return web.Response(body=html.encode("utf-8"), content_type="text/html",
                            charset="utf-8", headers={"Cache-Control": "no-store"})

    async def on_probe(_):
        return _file(overlay, "probe.html", "text/html; charset=utf-8")

    async def on_models(_):
        items = await asyncio.to_thread(
            lambda: [a.to_dict() for a in catalog.discover()])
        return web.json_response(items, headers={"Cache-Control": "no-store"})

    async def on_control(request):
        """WebUI 预览面板 → 所有已连接的浏览器/桌宠。"""
        origin = request.headers.get("Origin", "")
        if origin and not is_loopback_origin(origin):
            raise web.HTTPForbidden(text="只允许本机页面控制形象层")
        if request.headers.get(CONTROL_HEADER) != "1":
            raise web.HTTPForbidden(text="只接受 WebUI 代理控制形象层")
        try:
            data = await request.json()
        except (ValueError, TypeError):
            return web.json_response({"ok": False, "error": "invalid json"},
                                     status=400)
        if not isinstance(data, dict):
            return web.json_response({"ok": False, "error": "invalid control"},
                                     status=400)
        op = str(data.get("op") or "")
        if op not in {"param", "part", "motion", "expression", "view",
                      "reset", "reload"}:
            return web.json_response({"ok": False, "error": "unknown op"},
                                     status=400)
        allowed = ("op", "id", "value", "group", "index", "name",
                   "zoom", "offX", "offY")
        client.emit("control", **{k: data[k] for k in allowed if k in data})
        return web.json_response({"ok": True})


    async def on_ws(req):
        origin = req.headers.get("Origin", "")
        if origin and not is_loopback_origin(origin):
            raise web.HTTPForbidden(text="只允许本机页面连接形象层")
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=64 * 1024)
        await ws.prepare(req)
        q: asyncio.Queue = asyncio.Queue(maxsize=128)
        pump = None
        try:
            await ws.send_json({"t": "hello"})
            client.attach(q)
            pump = asyncio.create_task(_pump(ws, q))
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except (ValueError, TypeError):
                    continue
                if isinstance(data, dict) and data.get("t") == "notice":
                    client.set_notice(str(data.get("text") or ""))
                elif isinstance(data, dict) and data.get("t") == "report":
                    client.set_report(data)
                elif (isinstance(data, dict) and data.get("t") == "click"
                      and on_click is not None):
                    on_click(str(data.get("part") or ""))
        finally:
            if pump is not None:
                pump.cancel()
            client.detach(q)
        return ws

    @web.middleware
    async def hide_private(request, handler):
        # Windows 的静态文件系统大小写不敏感，且 %5C 会解成反斜杠；
        # 先归一化再判断，否则 /s/_x 能绕过下划线隐藏。
        path = request.path.replace("\\", "/").lower()
        last = path.rsplit("/", 1)[-1]
        if path.startswith("/s/") and last.startswith("_"):
            raise web.HTTPNotFound()
        return await handler(request)

    app = web.Application(middlewares=[hide_private])
    app.add_routes([
        web.get("/", on_index),
        web.get("/probe", on_probe),
        web.get("/config.json", on_config),
        web.get("/models", on_models),
        web.post("/control", on_control),
        web.get("/ws", on_ws),
        web.static("/s", str(overlay), show_index=False),
        web.get("/m/{id}/{path:.*}", on_model),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except Exception:
        await runner.cleanup()
        raise
    return runner


async def _pump(ws, q: asyncio.Queue) -> None:
    try:
        while True:
            msg = await q.get()
            await ws.send_str(json.dumps(msg, ensure_ascii=False))
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
        return


def _drop_live(q: asyncio.Queue) -> None:
    """队列满时丢掉可丢的实时帧，保留 say/page/end/control。"""
    kept = []
    while True:
        try:
            item = q.get_nowait()
        except asyncio.QueueEmpty:
            break
        if item.get("t") not in _LIVE_EVENTS:
            kept.append(item)
    for item in kept:
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            break


def _file(overlay: Path, name: str, ctype: str):
    from aiohttp import web

    p = overlay / name
    if not p.exists():
        return web.Response(status=404, text=f"缺 {name}")
    return web.Response(body=p.read_bytes(), content_type=ctype.split(";")[0],
                        charset="utf-8", headers={"Cache-Control": "no-store"})


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def stage_url(cfg: dict) -> str:
    s = cfg["stage"]
    return f"http://{_url_host(str(s['host']))}:{int(s['port'])}/"
