"""Prompt 预演：按真实请求的样子拼一遍，把 messages 和请求体打印出来。

和 `Brain.reply()` 共用 `PromptBuilder` / `LLMClient`：`build_runtime()` 组装同一套对象，
材料用一份固定的样例（一条「我说话」+ 两条弹幕 + 一段时间），所以看到的就是会发出去的东西
（只差真正那次 POST）。

WebUI「对话预设」页的「查看完整提示词」和 `python -m sidekick.preview` 走的是这一个入口。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
from pathlib import Path

from sidekick import config
from sidekick.app import identity
from sidekick.app.runtime import build_runtime
from sidekick.brain import material
from sidekick.brain.user import UserProfile
from sidekick.core.events import DANMU, ME, Event, EventBus
from sidekick.input.tokens import estimate_visual_tokens

#: 预演用的样例材料：15 秒里他说了一句、弹幕来了两条（单人陪玩会自动丢掉弹幕）。
_SAMPLE_ME = "诶这游戏看着挺难的啊，你觉得我能打过吗"
_SAMPLE_LAST_SAY = "先躲一下嘛，血条都快没了"


class _Said:
    """`material.build_round` 需要的最小接口；不落盘、不碰 state/。"""

    def __init__(self, last_say: str, age_s: float = 26.0) -> None:
        self._last = str(last_say or "")
        self._age = float(age_s)

    def session_minutes(self) -> int:
        return 0

    def gap(self) -> float:
        return self._age

    def recent(self, n: int = 1) -> list[str]:
        return [self._last][:max(0, n)]


def sample_material(*, live: bool, name: str,
                    bot_names: list[str]) -> tuple[str, dict]:
    """造一段像样的材料：一条「我说话」+ 两条弹幕 + 15 秒窗口（不带截图）。"""
    bus = EventBus()
    now = time.time()
    bus.set_viewers(12)
    bus.push(Event(kind=DANMU, ts=now - 11, name="小张", text="这boss好肉啊"))
    bus.push(Event(kind=DANMU, ts=now - 6, name="阿蓝", text="血不多了"))
    bus.push(Event(kind=ME, ts=now - 3, name="我", text=_SAMPLE_ME))
    return material.build_round(
        bus, _Said(_SAMPLE_LAST_SAY), "normal", seconds=15.0,
        evs=bus.since_seq(0), frame_ts=[], name=name, reason="我说话",
        bot_names=bot_names, live=live)


def _tok(messages: list[dict], images: int,
         image_size: tuple[int, int] = (1280, 720)) -> str:
    """粗估：中文 1 字 ≈ 1 token，图按实际发送尺寸估算。"""
    sys_len = sum(len(str(m.get("content") or "")) for m in messages
                  if m.get("role") == "system")
    hist = sum(len(str(m.get("content") or "")) for m in messages[:-1]
               if m.get("role") != "system")
    last = messages[-1]["content"] if messages else ""
    text_len = 0
    if isinstance(last, list):
        text_len = sum(len(p.get("text") or "") for p in last
                       if p.get("type") == "text")
    else:
        text_len = len(str(last))
    per = estimate_visual_tokens(*image_size)
    suffix = f"（图按 {per}/张算）" if images else ""
    return f"{sys_len + hist + text_len + images * per:,}{suffix}"


def _report(brain, messages: list[dict], body: dict, *,
            material_text: str, persona: str) -> str:
    preset_id, preset_error = brain.prompt.preset_ids()
    systems = [m for m in messages if m.get("role") == "system"]
    sys_chars = sum(len(str(m.get("content") or "")) for m in systems)
    round_msgs = sum(1 for m in messages
                     if m.get("role") in ("user", "assistant")) - 1
    lines = [
        f"# 预设 {preset_id or '（未设置）'}"
        f"{' · ' + preset_error if preset_error else ''} · "
        f"直播模式={'开' if brain.live else '关'}",
        f"# 人设 {len(persona)} 字 · "
        f"用户 {len(brain.prompt.build_user_system())} 字 · "
        f"游戏 {len(brain.prompt.build_game_system())} 字",
        f"# 全部 system {len(systems)} 条，共 {sys_chars} 字",
        f"# 历史 {round_msgs} 条 | 本轮材料 {len(material_text)} 字 | "
        f"图 0 张 | 预计 {_tok(messages, 0)} tok",
        "",
        json.dumps(body, ensure_ascii=False, indent=2),
    ]
    return "\n".join(lines)


def trim(text: str) -> str:
    """只要 `# 预设 …` 之后的部分（前面可能是启动日志）。"""
    marker = "\n# 预设 "
    idx = text.find(marker)
    if text.startswith("# 预设 "):
        return text
    if idx >= 0:
        return text[idx + 1:]
    return text


def _snapshot_db(source: Path, directory: Path) -> Path:
    """把记忆库复制到预演专用临时目录；预演不碰正式库的 last_used/向量。"""
    target = Path(directory) / "preview.db"
    if not source.is_file():
        return target
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


async def render(root=None, *, role: str = "", live: bool | None = None,
                 system_only: bool = False) -> str:
    """拼一次请求，返回给人看的文本（不复用上一次结果，每次重新算）。"""
    root = Path(root) if root is not None else config.ROOT
    info = identity.resolve(config.load(), root, role_id=role or None)
    cfg = info.cfg
    if role:
        # 预演按用户指定的角色目录/模型名重新选形象；否则 build_runtime 会 pin 当前角色。
        cfg.setdefault("role", {})["id"] = str(role)
    if live is True:
        cfg["session"]["live"] = True
    elif live is False:
        cfg["session"]["live"] = False

    profile = UserProfile.from_cfg(cfg)
    bot_names = [str(x) for x in ((cfg.get("persona") or {}).get("aliases") or [])
                 if str(x).strip()]
    mat, _meta = sample_material(
        live=bool(cfg["session"]["live"]), name=profile.name,
        bot_names=bot_names)

    # 预演只用记忆库快照：召回会写 last_used_at / 世界书向量，直接开正式库
    # 会和 runtime 抢写锁，也会把“看过预览”当成真实使用。
    tempdir = tempfile.TemporaryDirectory(prefix="deepsidekick-preview-")
    rt = None
    try:
        if bool(cfg["memory"]["enabled"]):
            source = Path(str(cfg["memory"]["db_path"]))
            if not source.is_absolute():
                source = root / source
            cfg["memory"]["db_path"] = str(
                _snapshot_db(source, Path(tempdir.name)))
        rt = build_runtime(cfg, root=root)
        brain = rt.brain
        if brain.recall is not None:
            # 预演要和真实一轮一样带向量召回；等模型 ready，最多 20 秒。
            embedder = getattr(rt, "embedder", None)
            if embedder is not None and hasattr(embedder, "start"):
                embedder.start()
                for _ in range(200):
                    if bool(getattr(embedder, "ready", False)):
                        break
                    await asyncio.sleep(0.1)
            await brain.recall.prepare(mat, brain.history.messages(),
                                       label=brain.profile.name)
        messages = brain.prompt.build_messages(mat)
        body = brain.llm.request_body(messages)
        persona = brain.prompt.build_system()
        if system_only:
            return "\n\n".join(
                f"===== {i + 1}. {m['role']} =====\n{m['content']}"
                for i, m in enumerate(messages))
        return _report(brain, messages, body, material_text=mat,
                       persona=persona)
    finally:
        try:
            if rt is not None:
                await rt.shutdown()
        finally:
            tempdir.cleanup()
