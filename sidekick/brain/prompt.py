"""Prompt 拼装：预设 → 角色 → 用户 → 游戏 → 记忆 → 历史 → 召回 → 材料 → 输出。

只依赖档案/预设/世界书/记忆/召回/动作这些抽象；不碰 HTTP，不发请求。
`Brain` 和 `sidekick/preview` 用同一个 `PromptBuilder`，看到的就是发出去的。

顺序是一张固定的表：固定内容按「角色 → 用户 → 游戏 → 记忆 → 历史 → 召回 → 材料」
排，预设的每一条声明自己落在哪两个锚点之间（`slot`），同一个缝隙里按文件里的书写顺序。
稳定内容一律在历史之前，每轮变的时间与召回在历史之后。

    预设 start
    角色人设 persona.md
    预设 after_persona
    用户档案
    预设 after_user
    游戏世界书 core
    预设 after_worldbook
    记忆（常驻：当前游戏/任务/剧情/NPC/世界书 always/最近记得的）
    预设 after_memory
    历史（最近 N 条热窗）
    预设 after_history
    记忆（召回：今天 + 你想起的往事 + 游戏里想起的）
    预设 after_recall_memory
    游戏资料（召回条目）
    预设 after_recall_worldbook
    身份切换提示（仅 role.id / user.id 变化时）
    本轮材料（user，含附图）
    预设 after_material
    动作规则 + TTS 标签规则（跟随角色形象与 provider）
"""
from __future__ import annotations

import copy
from pathlib import Path

from sidekick.brain import preset as preset_mod
from sidekick.brain.user import UserProfile
from sidekick.memory.recall import wants_all_tasks
from sidekick.memory.timefmt import format_day, format_span

#: 角色人设只有一个文件：这一层只回答「我是谁」。
PERSONA_FILE = "persona.md"

RECALL_INTRO = ("（这些是以前发生你还记得的事，只在有需要的时候自然提起，"
                "或是作为上下文帮你更好地理解现在发生了什么；"
                "记不清就以他现在说的为准。）")

#: 跨轮视觉记忆：隐藏标签，不念出来，只进下一轮历史。
SCENE_RULES = ("在整条回复的最末尾（放在 `<ACT>` 和 `<TASK>` 后面），另起一行写 "
               "`<scene>当前画面状态</scene>`，这是留给下一轮看的内部笔记：\n"
               "只记眼前真看得见的关键状态（比如在打谁、残血还是死了、开了菜单、"
               "在播过场，或者切到了非游戏窗口），别超过 25 个字，不写评价和解释，"
               "也不要混进正文。\n"
               "要是没有画面或者进了隐私窗口，就写 `<scene>没有画面</scene>`。"
               "就算正文只写了「略」，下一行也要照常补上这行 `<scene>...</scene>`。")


def _read(directory: Path, name: str) -> str:
    p = directory / name
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _blank(text: str) -> bool:
    """strip() 后只剩空行或 `#` 注释行 → 按"这个文件没写"处理。"""
    return all(not line.strip() or line.lstrip().startswith("#")
               for line in text.splitlines())


def _persona_text(pack: Path | None, name: str = PERSONA_FILE) -> str:
    """角色目录里的文件；没写就是空，没有全局回退。"""
    if pack is None:
        return ""
    text = _read(pack, name)
    return "" if _blank(text) else text


def load_persona(root: str | Path, persona_dir: str | Path | None = None) -> str:
    """读当前角色的人设；`persona_dir` 是 persona/roles/<id>/。"""
    return _persona_text(Path(persona_dir) if persona_dir else None)


class PromptBuilder:
    def __init__(self, cfg: dict, root: str | Path, *, profile=None,
                 worldbook=None, history=None, memory=None, actions=None,
                 recall=None, speech_rules: str = "") -> None:
        self.root = Path(root)
        self.pack_persona_dir = self._pack_dir(cfg)
        self.cfg = cfg
        self._mem_sig = copy.deepcopy((cfg.get("memory"), cfg.get("game")))
        self.profile = profile or UserProfile.from_cfg(cfg)
        self.worldbook = worldbook
        self.history = history
        self.memory = memory
        self.recall = recall
        self.actions = actions
        self.speech_rules = str(speech_rules or "").strip()
        self.bot_name = self._bot_name(cfg)
        self.live = bool(cfg["session"]["live"])
        self.preset = preset_mod.PresetSource(self.root, self._preset_id(cfg))
        self._system = ""
        self._load_persona_texts()
        self._fingerprint = self._mtime()
        self.preset.reload()
        self.last_report: dict = {"blocks": [], "recall": [], "tokens": {}}

    @property
    def visual_memory_tag(self) -> bool:
        return bool((self.cfg.get("brain") or {})["visual_memory_tag"])

    # ── 配置 / 人设/预设热重载 ─────────────────────────
    @staticmethod
    def _pack_dir(cfg: dict) -> Path | None:
        raw = (cfg.get("role") or {}).get("role_dir") or ""
        return Path(raw) if str(raw).strip() else None

    @staticmethod
    def _preset_id(cfg: dict) -> str:
        return str((cfg.get("prompt") or {}).get("id") or "").strip()

    @staticmethod
    def _bot_name(cfg: dict) -> str:
        return str(cfg["persona"]["bot_name"]).strip()

    def _mtime(self) -> tuple:
        """人设的指纹；只 stat 当前角色目录里的 persona.md。"""
        if self.pack_persona_dir is None:
            return ()
        p = self.pack_persona_dir / PERSONA_FILE
        try:
            st = p.stat()
            return (PERSONA_FILE, st.st_mtime_ns, st.st_size)
        except OSError:
            return (PERSONA_FILE, 0, 0)

    def _load_persona_texts(self) -> None:
        """读当前角色的人设；没写就是空。"""
        history_max = str(getattr(self.history, "history_max",
                                  self.cfg["memory"]["history_max"]))
        self._system = (load_persona(self.root, self.pack_persona_dir)
                        .replace("{{bot}}", self.bot_name)
                        .replace("{{history_max}}", history_max))

    def refresh_persona(self) -> None:
        m = self._mtime()
        if m != self._fingerprint:
            self._load_persona_texts()
            self._fingerprint = m
        self.preset.refresh()

    def reload(self, cfg: dict) -> None:
        new_mem_sig = copy.deepcopy((cfg.get("memory"), cfg.get("game")))
        self.cfg = cfg
        self.pack_persona_dir = self._pack_dir(cfg)
        self.profile = UserProfile.from_cfg(cfg)
        self.bot_name = self._bot_name(cfg)
        self.live = bool(cfg["session"]["live"])
        self.preset = preset_mod.PresetSource(self.root, self._preset_id(cfg))
        self.preset.reload()
        if self.worldbook is not None:
            self.worldbook.reload(cfg)
        if self.recall is not None and new_mem_sig != self._mem_sig:
            self.recall.reload(cfg)
        self._mem_sig = new_mem_sig
        self._load_persona_texts()
        self._fingerprint = self._mtime()

    # ── 预设缝隙 ──────────────────────────────────────
    def preset_messages(self, slot: str, *, refresh: bool = True) -> list[dict]:
        """取一个缝隙；文本里的 {{bot}}/{{user}}/{{history_max}} 在这里替换。"""
        if refresh:
            self.preset.refresh()
        history_max = str(getattr(self.history, "history_max",
                                  self.cfg["memory"]["history_max"]))
        out: list[dict] = []
        for msg in self.preset.preset.messages(slot, live=self.live):
            text = (self.profile.render(str(msg["content"]))
                    .replace("{{bot}}", self.bot_name)
                    .replace("{{history_max}}", history_max).strip())
            if text:
                out.append({"role": msg["role"], "content": text})
        return out

    def preset_ids(self) -> tuple[str, str]:
        """(当前预设 id, 预设错误)；状态端点和 WebUI 用。"""
        return (self.preset.preset.id, self.preset.preset.error or "")

    # ── 各段 ──────────────────────────────────────────
    def build_system(self, *, refresh: bool = True) -> str:
        if refresh:
            self.refresh_persona()
        return self.profile.render(self._system)

    def build_user_system(self) -> str:
        return self.profile.system_block()

    def build_game_system(self, *, refresh: bool = True) -> str:
        return (self.worldbook.core_text(refresh=refresh)
                if self.worldbook is not None else "")

    def build_tail_block(self) -> str:
        """代码负责的输出补充：动作规则 + 程序化表演 + provider 发声标签。"""
        parts: list[str] = []
        if self.actions is not None:
            rules = str(self.actions.rules() or "").strip()
            if rules:
                parts.append(rules)
            autonomy = str(getattr(self.actions, "prompt_rules", lambda: "")() or "").strip()
            if autonomy:
                parts.append(autonomy)
        if self.speech_rules:
            parts.append(self.speech_rules)
        if self.visual_memory_tag:
            parts.append(SCENE_RULES)
        return "\n\n".join(p for p in parts if p)

    def build_output_messages(self, *, refresh: bool = True) -> list[dict]:
        """材料之后的输出约束：预设 after_material 的条目 + 代码追加的那一段。"""
        messages = self.preset_messages("after_material", refresh=refresh)
        tail = self.build_tail_block()
        if tail:
            messages.append({"role": "system", "content": tail})
        return messages

    def prefix_messages(self, *, refresh: bool = True) -> list[dict]:
        """稳定前缀：预设开头 → 人设 → 用户 → 游戏（不含记忆/历史/召回）。"""
        if refresh:
            self.refresh_persona()
        messages: list[dict] = []
        messages += self.preset_messages("start", refresh=refresh)
        if self.build_system(refresh=False):
            messages.append({"role": "system",
                             "content": self.build_system(refresh=False)})
        messages += self.preset_messages("after_persona", refresh=False)
        if self.build_user_system():
            messages.append({"role": "system",
                             "content": self.build_user_system()})
        messages += self.preset_messages("after_user", refresh=False)
        if self.build_game_system(refresh=refresh):
            messages.append({"role": "system",
                             "content": self.build_game_system(refresh=False)})
        messages += self.preset_messages("after_worldbook", refresh=False)
        return messages

    @staticmethod
    def _format_item(x) -> str:
        title = str(x.title or "").strip()
        content = str(x.content or "").strip()
        if x.kind == "summary":
            span = format_span(x.start_ts, x.end_ts)
            if span != "时间不详":
                return f"- {span}｜{content}"
            return f"- {content}"
        if title and title != content:
            return f"- 【{title}】{content}"
        return f"- {content}"

    def resident_block(self, *, refresh: bool = True,
                       expand_tasks: bool = False) -> str:
        """历史之前的常驻块：当前游戏、世界书常驻、最近陪玩记忆。

        这些内容只在总结/换游戏/改世界书时变，放在历史之前才能吃前缀缓存。
        """
        if refresh:
            self.refresh_persona()
        sections: list[str] = []
        report: list[dict] = []
        chapter = ""
        if self.memory is not None:
            chapter = self.memory.current_chapter()
            state = self.memory.game_state_block(expand_tasks=expand_tasks)
            if state:
                sections.append(state)
                report.append({"kind": "always",
                               "section": "游戏当前/任务/最近剧情",
                               "chars": len(state)})
        wb_always = (self.worldbook.always_entries(chapter)
                     if self.worldbook is not None else [])
        if wb_always:
            txt = "[世界书·常驻]\n" + "\n".join(
                f"- {x['title']}：{x['content']}" for x in wb_always)
            sections.append(txt)
            report.append({"kind": "worldbook", "section": "世界书·常驻",
                           "chars": len(txt), "count": len(wb_always)})
        recent = (self.memory.recent_live_block()
                  if self.memory is not None else "")
        self._resident_has_recent = bool(recent)
        if recent:
            sections.append(RECALL_INTRO)
            sections.append(recent)
            report.append({"kind": "always", "section": "最近记得的",
                           "chars": len(recent)})
        self._resident_report = {"blocks": report, "chapter": chapter}
        return "\n\n".join(s for s in sections if s)

    def recall_sections(self, material: str, *,
                        refresh: bool = True) -> tuple[str, str]:
        """历史之后的两块：记忆召回（含日期）与游戏资料召回。

        一次请求只跑一次召回；两个字符串分别插在两个缝隙的位置上。
        """
        if refresh:
            self.refresh_persona()
        memory_sections = [f"今天是 {format_day()}"]
        worldbook_sections: list[str] = []
        report: list[dict] = []
        chapter = self.memory.current_chapter() if self.memory is not None else ""
        result = None
        recall_error = None
        if self.recall is not None and self.history is not None:
            try:
                result = self.recall.run(material, self.history.messages(),
                                         chapter, label=self.profile.name)
            except Exception as e:
                # 召回/索引失败不能挡住主回复；本轮只保留常驻块。
                recall_error = f"{type(e).__name__}: {e}"
                result = None
        items = result.all_items() if result is not None else []
        intro = (bool(items)
                 and not getattr(self, "_resident_has_recent", False))
        if intro:
            memory_sections.append(RECALL_INTRO)
        if result is not None and not result.empty:
            if result.episodes:
                memory_sections.append("[你想起的往事]\n" + "\n".join(
                    self._format_item(x) for x in result.episodes))
            if result.game:
                memory_sections.append("[游戏里想起的]\n" + "\n".join(
                    self._format_item(x) for x in result.game))
            if result.worldbook:
                worldbook_sections.append("[世界书]\n" + "\n".join(
                    self._format_item(x) for x in result.worldbook))

        if self.memory is not None:
            self.memory.mark_recalled([
                {"kind": x.kind, "id": x.id, "title": x.title,
                 "content": x.content, "score": x.score, "keys": x.hits}
                for x in items if x.source == "memory"])
        for x in items:
            report.append({"kind": x.source, "id": x.id, "title": x.title,
                           "score": round(x.score, 4), "tier": x.tier,
                           "channel": x.channel, "keys": x.hits,
                           "chars": len(x.content)})
        resident = getattr(self, "_resident_report", {})
        self.last_report = {
            "blocks": list(resident.get("blocks") or []) + report,
            "recall": [x.as_dict() for x in items if x.source == "memory"],
            "worldbook": [x.as_dict() for x in items if x.source == "worldbook"],
            "chapter": chapter,
            "query": result.query if result is not None else "",
            "pools": (result.report.get("pools") if result is not None else {}),
            "time_intent": (result.report.get("time_intent")
                            if result is not None else None),
            "time_fallback": (result.report.get("time_fallback")
                              if result is not None else False),
            "degraded": (result.report.get("degraded")
                         if result is not None else None),
            "error": recall_error,
        }
        return ("\n\n".join(s for s in memory_sections if s),
                "\n\n".join(s for s in worldbook_sections if s))

    # ── 请求体 ────────────────────────────────────────
    @staticmethod
    def _content(material: str,
                 images: list[tuple[str, str]] | None) -> object:
        if not images:
            return material
        content: list[dict] = [{"type": "text", "text": material}]
        for label, b64 in images:
            if label:
                content.append({"type": "text", "text": label})
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64," + b64}})
        return content

    def build_messages(self, material: str,
                       images: list[tuple[str, str]] | None = None, *,
                       include_history: bool = True,
                       include_dynamic: bool = True,
                       identity_marker: str = "") -> list[dict]:
        """单次请求的 messages 拼装入口；warmup 复用同一套输出位置。"""
        content = self._content(material, images)
        messages = self.prefix_messages()
        if include_dynamic:
            resident = self.resident_block(
                expand_tasks=wants_all_tasks(material))
            if resident:
                messages.append({"role": "system", "content": resident})
            messages += self.preset_messages("after_memory", refresh=False)
        if include_history and self.history is not None:
            messages.extend(self.history.messages())
        if include_dynamic:
            messages += self.preset_messages("after_history", refresh=False)
            memory_recall, worldbook_recall = self.recall_sections(material)
            if memory_recall:
                messages.append({"role": "system", "content": memory_recall})
            messages += self.preset_messages("after_recall_memory", refresh=False)
            if worldbook_recall:
                messages.append({"role": "system", "content": worldbook_recall})
            messages += self.preset_messages("after_recall_worldbook", refresh=False)
        if identity_marker:
            messages.append({"role": "system", "content": identity_marker})
        messages.append({"role": "user", "content": content})
        messages += self.build_output_messages(refresh=False)
        return messages

    def build_warmup_messages(self) -> list[dict]:
        """只预热稳定前缀 + 输出约束，不带历史/动态材料。"""
        return self.build_messages("只回两个字：收到",
                                   include_history=False, include_dynamic=False)

    def build_body(self, material: str,
                   images: list[tuple[str, str]] | None = None, *,
                   identity_marker: str = "") -> dict:
        return {"messages": self.build_messages(
            material, images, identity_marker=identity_marker)}

    # ── 状态 ──────────────────────────────────────────
    def stats(self) -> dict:
        # 只读快照：不推进人设/预设/世界书的 mtime 缓存，文件刷新留给主循环。
        prefix = self.prefix_messages(refresh=False)
        output = self.build_output_messages(refresh=False)
        states = (self.memory.game_state_block() if self.memory is not None else "")
        recent = (self.memory.recent_live_block() if self.memory is not None else "")
        total = sum(len(str(x.get("content") or "")) for x in prefix)
        out_chars = sum(len(str(x.get("content") or "")) for x in output)
        preset_id, preset_error = self.preset_ids()
        return {
            "preset_id": preset_id,
            "preset_error": preset_error,
            "preset_blocks": len(self.preset.preset.blocks),
            "persona_chars": len(self._system),
            "user_chars": len(self.build_user_system()),
            "prefix_chars": total,
            "system1_chars": len(self.build_system(refresh=False)),
            "game_chars": len(self.build_game_system(refresh=False)),
            "game_id": getattr(self.worldbook, "id", ""),
            "game_error": getattr(self.worldbook, "error", None),
            "worldbook_entries": int((
                self.worldbook.stats(refresh=False).get("entries", 0)
                if self.worldbook is not None else 0) or 0),
            "prefix_systems": len(prefix) + len(output),
            "memory_chars": len(states) + len(recent),
            "output_chars": out_chars,
            "live": self.live,
            "user": self.profile.info(),
            "last_report": self.last_report,
            "avatar": (self.actions.avatar_id if self.actions is not None else ""),
        }
