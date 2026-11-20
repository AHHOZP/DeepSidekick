"""游戏资料包与世界书条目。

人设文本/角色资料在 `routes/avatar.py`；用户人设在 `routes/users.py`。
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from aiohttp import web

from sidekick.core import atomic
from sidekick import config
from sidekick.brain.worldbook import read_entry, write_entry
from sidekick.webui.http import MAX_TEXT, error, json_response, kw_list, valid_gid
from sidekick.webui.context import WebContext


class WorldbookRoutes:
    def __init__(self, ctx: WebContext) -> None:
        self.ctx = ctx
        self.root = ctx.root

    # ── 游戏路径 ──────────────────────────────────────
    def game_dir(self, gid: str) -> Path | None:
        gid = str(gid or "").strip()
        if not valid_gid(gid):
            return None
        base = (self.root / "persona" / "games").resolve()
        path = (base / gid).resolve()
        try:
            path.relative_to(base)
        except ValueError:
            return None
        return path

    def game_core(self, gid: str) -> Path | None:
        d = self.game_dir(gid)
        if d is None:
            return None
        core = d / "core.md"
        return core if core.exists() else None

    def games(self) -> list[dict]:
        current = config.resolve_game_id(self.ctx.cfg())
        out = []
        base = self.root / "persona" / "games"
        if not base.exists():
            return out
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            core = d / "core.md"
            if not core.exists():
                continue
            text = core.read_text(encoding="utf-8")
            title = _first_line(text) or d.name
            ents = (list((d / "entries").glob("*.md"))
                    if (d / "entries").is_dir() else [])
            out.append({"id": d.name, "title": title,
                        "chars": len(text), "entries": len(ents),
                        "current": d.name == current,
                        "mtime": core.stat().st_mtime})
        return out

    async def games_get(self, _request):
        return json_response({"ok": True, "games": self.games()})

    async def game_create(self, request):
        body = await request.json()
        gid = str((body or {}).get("id") or "").strip()
        d = self.game_dir(gid)
        if d is None:
            return error("游戏 ID 只能包含字母、数字、点、下划线、短横线")
        if d.exists():
            return error("同名游戏资料已存在")
        (d / "entries").mkdir(parents=True)
        (d / "core.md").write_text(f"[{gid}]\n\n", encoding="utf-8")
        return json_response({"ok": True, "id": gid})

    async def game_get(self, request):
        gid = request.match_info.get("gid", "")
        p = self.game_core(gid)
        if p is None or not p.exists():
            return error("游戏资料不存在", 404)
        entries = []
        d = self.game_dir(gid)
        if d and (d / "entries").is_dir():
            for ep in sorted((d / "entries").glob("*.md")):
                e = read_entry(ep)
                if e:
                    entries.append(e)
        content = p.read_text(encoding="utf-8")
        return json_response({"ok": True, "id": gid, "content": content,
                              "worldbook": bool(entries), "entries": entries})

    async def game_put(self, request):
        gid = request.match_info.get("gid", "")
        d = self.game_dir(gid)
        if d is None:
            return error("游戏 ID 不合法")
        body = await request.json()
        text = str((body or {}).get("content") or "")
        if len(text) > MAX_TEXT:
            return error("文本太长")
        d.mkdir(parents=True, exist_ok=True)
        (d / "entries").mkdir(exist_ok=True)
        p = d / "core.md"
        atomic.write_text(p, text)
        return json_response({"ok": True, "chars": len(text)})

    async def game_delete(self, request):
        if request.query.get("confirm") != "1":
            return error("需要 confirm=1")
        gid = request.match_info.get("gid", "")
        d = self.game_dir(gid)
        if d is None:
            return error("游戏 ID 不合法")
        current = config.resolve_game_id(self.ctx.cfg())
        if gid == current:
            return error("这是当前游戏；先切换到别的游戏再删，避免 config 指向不存在的文件")
        if not d.is_dir():
            return error("游戏资料不存在", 404)
        await asyncio.to_thread(shutil.rmtree, d)
        return json_response({"ok": True})

    # ── 世界书条目 ────────────────────────────────────
    def entry_path(self, gid: str, eid: str) -> Path | None:
        d = self.game_dir(gid)
        if d is None or not valid_gid(eid):
            return None
        return d / "entries" / (eid + ".md")

    async def game_entry_put(self, request):
        gid = request.match_info.get("gid", "")
        eid = request.match_info.get("eid", "")
        p = self.entry_path(gid, eid)
        if p is None:
            return error("条目 ID 不合法")
        d = self.game_dir(gid)
        (d / "entries").mkdir(parents=True, exist_ok=True)
        if not (d / "core.md").exists():
            (d / "core.md").write_text(f"[{gid}]\n\n", encoding="utf-8")
        b = await request.json()
        try:
            priority = int((b or {}).get("priority") or 0)
        except (TypeError, ValueError):
            return error("priority 必须是整数")
        e = write_entry(
            p, title=str((b or {}).get("title") or eid),
            content=str((b or {}).get("content") or ""),
            keys=kw_list((b or {}).get("keys")),
            aliases=kw_list((b or {}).get("aliases")),
            always=bool((b or {}).get("always", False)),
            chapter=str((b or {}).get("chapter") or ""),
            spoiler=bool((b or {}).get("spoiler", False)),
            priority=priority)
        return json_response({"ok": True, "item": e})

    async def game_entry_delete(self, request):
        p = self.entry_path(request.match_info.get("gid", ""),
                            request.match_info.get("eid", ""))
        if p is None or not p.exists():
            return error("条目不存", 404)
        p.unlink()
        return json_response({"ok": True})

    async def game_select(self, request):
        body = await request.json()
        gid = str((body or {}).get("id") or "").strip()
        p = self.game_core(gid)
        if p is None or not p.exists():
            return error("游戏资料不存在", 404)
        async with self.ctx.config_lock:
            raw = config.load_raw()
            if config.last_error:
                return error(
                    f"config.yaml 解析失败，已拒绝覆盖保存：{config.last_error}。"
                    "请先修复文件再切换游戏。", 409)
            game = raw.setdefault("game", {})
            game.pop("path", None)          # 旧字段：只保留 game.id
            # 页面不再管启用开关（在概览页），没传就保持原值
            if "enabled" in (body or {}):
                game["enabled"] = bool(body["enabled"])
            else:
                game.setdefault("enabled", True)
            game["id"] = gid
            config.save_raw(raw)
        self.ctx.get_memory(force=True)
        return json_response({"ok": True, "id": gid})


def add_routes(router: web.UrlDispatcher, ctx: WebContext) -> None:
    routes = WorldbookRoutes(ctx)
    router.add_get("/api/games", routes.games_get)
    router.add_post("/api/games", routes.game_create)
    router.add_get("/api/games/{gid}", routes.game_get)
    router.add_put("/api/games/{gid}", routes.game_put)
    router.add_delete("/api/games/{gid}", routes.game_delete)
    router.add_post("/api/games/select", routes.game_select)
    router.add_put("/api/games/{gid}/entries/{eid}", routes.game_entry_put)
    router.add_delete("/api/games/{gid}/entries/{eid}",
                      routes.game_entry_delete)


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip().strip("[]【】")
    return ""
