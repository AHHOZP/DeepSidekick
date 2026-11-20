"""大脑：一次调用，一句话。

**这里没有 agent 循环。** 材料进来、一句话出去，函数结束。

实时回路是程序里的 `while`（`Runtime.main_loop`），模型只是一次函数调用。
Brain 只做回答编排：组包、调 LLM、处理任务标签/沉默、写历史、触发总结；
prompt 拼装在 `sidekick/brain/prompt.py`，HTTP 在 `sidekick/brain/llm.py`，压缩在 `sidekick/memory/summary.py`。
"""
from __future__ import annotations

import asyncio
import copy
import re
import time
from pathlib import Path

from sidekick.brain import task_tags
from sidekick.brain import scene_tag
from sidekick.brain.llm import LLMError, parse_completion
from sidekick.core.ports import (AvatarActions, LLM, LongTermMemory,
                             ShortTermHistory)
from sidekick.brain.prompt import PromptBuilder
from sidekick.brain.material import attach_scene, material_digest
from sidekick.memory.summary import Summarizer
from sidekick.memory.limits import SUMMARY_HISTORY_EXTRA
from sidekick.core.text import strip_tags
from sidekick.brain.user import UserProfile

SILENT = {"略", "略。", "略！", "略…", "略～", "", "none", "null"}
_SILENT_RE = re.compile(r"^略[。！？…～\s]*$")


class Brain:
    def __init__(self, cfg: dict, *, history: ShortTermHistory,
                 memory: LongTermMemory, worldbook, actions: AvatarActions,
                 llm: LLM, recall=None, root: str | Path | None = None,
                 speech_rules: str = "", log=print) -> None:
        self.history = history
        self.memory = memory
        self.worldbook = worldbook
        self.recall = recall
        self.llm = llm
        self.log = log
        self._mem_sig = copy.deepcopy((cfg.get("memory"), cfg.get("game")))
        self.root = Path(root) if root is not None else Path.cwd()
        self.profile = UserProfile.from_cfg(cfg)
        self._identity = self._identity_ids(cfg)
        self._last_identity = self._load_identity()
        self.prompt = PromptBuilder(
            cfg, self.root, profile=self.profile, worldbook=worldbook,
            history=history, memory=memory, actions=actions, recall=recall,
            speech_rules=speech_rules)
        self.summary = Summarizer(
            cfg, profile=self.profile, worldbook=worldbook, history=history,
            memory=memory, llm=llm, log=log)
        self.last_tasks: list[dict] = []
        self.last_ms: int | None = None
        self.last_error: str | None = None
        self.last_silent = False
        self.calls = 0
        self.fail_rounds = 0
        self._pending_exchange: dict[int, tuple[dict, dict]] = {}
        e = cfg["errors"]
        self.retry_delay = float(e["retry_delay_ms"]) / 1000.0

    # ── 配置派生 ──────────────────────────────────────
    @staticmethod
    def _identity_ids(cfg: dict) -> tuple[str, str]:
        return (str((cfg.get("role") or {}).get("id") or ""),
                str((cfg.get("user") or {}).get("id") or ""))

    def _load_identity(self) -> tuple[str, str] | None:
        load = getattr(self.memory, "last_identity", None)
        if load is None:
            return None
        try:
            return load()
        except Exception as e:
            self.log(f"[identity] 读取上次身份失败：{type(e).__name__}: {e}")
            return None

    def _identity_marker(self) -> str:
        previous, current = self._last_identity, self._identity
        if previous is None or previous == current:
            return ""
        changes = []
        if previous[0] != current[0]:
            changes.append(f"角色设定：{previous[0] or '未设置'} → "
                           f"{current[0] or '未设置'}")
        if previous[1] != current[1]:
            changes.append(f"用户设定：{previous[1] or '未设置'} → "
                           f"{current[1] or '未设置'}")
        if not changes:
            return ""
        return ("[身份切换]\n" + "；".join(changes)
                + "。\n上一轮和这一轮分属不同设定，不要把两边的人设混在一起；"
                  "按当前设定回应。")

    def _remember_identity(self) -> None:
        self._last_identity = self._identity
        save = getattr(self.memory, "set_last_identity", None)
        if save is None:
            return
        try:
            save(*self._identity)
        except Exception as e:
            self.log(f"[identity] 记录身份失败：{type(e).__name__}: {e}")

    @property
    def model(self) -> str:
        return self.llm.model

    @property
    def source(self) -> str:
        return self.llm.source

    @property
    def effort(self) -> str:
        return self.llm.effort

    @property
    def effort_dropped(self) -> bool:
        return self.llm.effort_dropped

    @property
    def worldbook_error(self) -> str | None:
        return getattr(self.worldbook, "error", None)

    @property
    def live(self) -> bool:
        return self.prompt.live

    def reload_config(self, cfg: dict) -> None:
        """热重载：人设、用户档案、世界书、游戏 ID/开关、记忆条数。LLM/TTS 仍需重启。"""
        new_mem_sig = copy.deepcopy((cfg.get("memory"), cfg.get("game")))
        self.profile = UserProfile.from_cfg(cfg)
        self._identity = self._identity_ids(cfg)
        if new_mem_sig != self._mem_sig:
            if hasattr(self.history, "reload"):
                self.history.reload(cfg)
            # 空 id 和 game.enabled 都要同步：切走后不能继续用上一局的记忆。
            self.memory.reload(cfg)
        self.prompt.reload(cfg)
        self.summary.reload(cfg, profile=self.profile)
        self._mem_sig = new_mem_sig

    # ── 预览 API：主回复走 reply()；脚本直接用 prompt / llm。──
    def build_body(self, material: str,
                   images: list[tuple[str, str]] | None = None) -> dict:
        body = self.prompt.build_body(
            material, images, identity_marker=self._identity_marker())
        return self.llm.request_body(body["messages"])

    # ── 主入口 ────────────────────────────────────────
    async def reply(self, material: str,
                    images: list[tuple[str, str]] | None = None,
                    round_id: int | None = None) -> str | None:
        """返回要念的那句话；None 表示这一轮没说。

        `last_error is None` → 她选择沉默；非空 → 真失败（该走兜底话术）。
        """
        self.last_error = None
        self.last_silent = False
        self.last_tasks = []
        if not self.llm.ready:
            self.last_error = "模型 API 没配好（Base URL/模型）"
            self.fail_rounds += 1
            return None
        self.prompt.refresh_persona()
        if self.recall is not None:
            try:
                await self.recall.prepare(material, self.history.messages(),
                                          label=self.profile.name)
            except Exception as e:
                # 召回/索引失败不能挡住主回复；本轮降级成关键词。
                self.log(f"[memory] 召回准备失败（降级关键词）："
                         f"{type(e).__name__}: {e}")

        budget = 2
        t0 = time.time()
        while budget > 0:
            budget -= 1
            body = self.build_body(material, images)
            try:
                data = await self.llm.chat(
                    body["messages"],
                    temperature=float(body.get("temperature", self.llm.temp)),
                    attempts=1)
            except asyncio.CancelledError:
                raise
            except LLMError as e:
                self.last_error = str(e)
                if e.status is not None and 400 <= e.status < 500 and e.status != 429:
                    break
                if budget:
                    await asyncio.sleep(self.retry_delay)
                continue

            self.last_ms = round((time.time() - t0) * 1000)
            self.calls += 1
            try:
                text, finish_reason = parse_completion(data)
            except (AttributeError, IndexError, TypeError):
                self.last_error = "返回解析不了"
                if budget:
                    await asyncio.sleep(self.retry_delay)
                continue

            if finish_reason == "length":
                # 不传 max_tokens，正常不会走到这里；走到了就是网关自己截的。
                self.log(f"[{round_id}] 输出被网关截断（finish_reason=length）")
            if not text:
                self.last_error = "返回空内容"
                if budget:
                    await asyncio.sleep(self.retry_delay)
                continue

            text, scene = scene_tag.extract(text)
            text = _clean_output(text)
            text, task_list = task_tags.extract(text)
            if task_list:
                try:
                    self.last_tasks = self.memory.apply_tasks(task_list)
                except Exception as e:
                    # 任务落库失败不能挡住这一轮说话。
                    self.log(f"[memory] 任务写入失败（忽略）："
                             f"{type(e).__name__}: {e}")
            if _is_silent(text):
                self.last_error = None
                self.last_silent = True
                return None

            self.last_error = None
            digest = material_digest(material, self.profile.name)
            if scene and self.prompt.visual_memory_tag:
                digest = attach_scene(digest, scene)
            stored = ("[材料摘要]\n" + digest) if digest else str(material or "")
            # 历史里不能留 <ACT>：它属于输出层，先按当前形象剥离再落库。
            history_text = text
            parser = getattr(self.prompt.actions, "parse", None)
            if parser is not None:
                history_text = parser(text).text
            try:
                user, assistant = self.history.add_exchange(
                    stored, strip_tags(history_text), game_id=self.memory.game_id)
            except Exception as e:
                # 历史写失败只会丢这一轮的记忆，不该让已经生成的回复消失。
                self.log(f"[memory] 历史写入失败（忽略）："
                         f"{type(e).__name__}: {e}")
                return text
            if round_id is not None:
                self._pending_exchange[round_id] = (user, assistant)
            self._remember_identity()
            return text

        self.fail_rounds += 1
        return None

    # ── 真正说出口之后，再落历史 ────────────────────────
    def mark_spoken(self, round_id: int, spoken: str | None, *,
                    original: str | None = None) -> None:
        """历史只保留实际播出的版本；空字符串表示这句没出声，整对删掉。

        `original` 由出口层传入已经过 guard 清洗的显示文本；不传时才回退到
        模型原文。这样 emoji/括号标签被清洗后不会被误判成“说到一半截断”。
        """
        pair = self._pending_exchange.pop(round_id, None)
        if pair is not None:
            user, assistant = pair
            if not spoken:
                self.history.drop_pair(user, assistant)
            else:
                text = str(spoken)
                source = (original if original is not None
                          else assistant.get("content") or "")
                original_text = strip_tags(str(source)).strip()
                actions = getattr(getattr(self, "prompt", None), "actions", None)
                if actions is not None:
                    original_text = actions.parse(original_text).text.strip()
                if (text and len(text) < len(original_text)
                        and original_text.startswith(text)
                        and not text.rstrip().endswith(("。", "！", "？", "…", "～"))):
                    text = text.rstrip() + "…"
                self.history.update_assistant(assistant, text)
        self.summary.maybe_start()

    def mark_failed(self, round_id: int) -> None:
        pair = self._pending_exchange.pop(round_id, None)
        if pair is not None:
            self.history.drop_pair(*pair)

    # ── 预热 / 缓存 ───────────────────────────────────
    async def warmup(self) -> str | None:
        if not self.llm.warmup_enabled or not self.llm.ready:
            return None
        messages = self.prompt.build_warmup_messages()
        try:
            await self.llm.chat(messages, temperature=0.0, attempts=1)
        except Exception as e:
            return f"预热失败：{e}"
        return self.llm.cache_line()

    def cache_line(self) -> str:
        return self.llm.cache_line()

    @property
    def last_usage(self) -> dict:
        """最近一次成功 chat 的 usage + 请求指标；只读。"""
        return self.llm.last_usage

    # ── 状态 ──────────────────────────────────────────
    def stats(self) -> dict:
        # /status 只读：人设刷新由主循环的 prompt 拼装做，这里用缓存。
        ps = self.prompt.stats()
        try:
            history_n = self.history.count() // 2
            self.history_error = None
        except Exception as e:
            history_n = 0
            self.history_error = f"{type(e).__name__}: {e}"
        return {
            "source": self.source, "model": self.model, "calls": self.calls,
            "last_ms": self.last_ms, "error": self.last_error,
            "effort": self.effort, "effort_dropped": self.effort_dropped,
            "fail_rounds": self.fail_rounds, "silent": self.last_silent,
            "history_error": getattr(self, "history_error", None),
            "summary_error": self.summary.error or self.memory.last_summary_error,
            "summary_failures": self.summary.failures,
            "summary_dropping": self.summary.dropping_history,
            "summary_history_extra": SUMMARY_HISTORY_EXTRA,
            "history_max": int(getattr(self.history, "history_max", 0) or 0),
            "summary_empty": self.summary.empty,
            "summary_keyword_audits": self.summary.keyword_audits,
            "audit_error": self.summary.audit.last_error,
            "last_recall": self.memory.last_recall,
            "last_report": self.prompt.last_report,
            "last_tasks": self.last_tasks,
            "history": history_n,
            **ps,
        }

    async def close(self) -> None:
        await self.summary.close()
        await self.llm.close()


def _is_silent(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return True
    plain = strip_tags(t).strip()
    return plain in SILENT or bool(_SILENT_RE.match(plain))


def _clean_output(text: str) -> str:
    """模型偶尔会加引号/前缀，剃掉。"""
    t = text.strip()
    for q in ('"', "'", "“", "”", "「", "」"):
        if t.startswith(q) and t.endswith(q) and len(t) > 2:
            t = t[1:-1].strip()
    for pre in ("好的，", "好的。", "明白，"):
        if t.startswith(pre) and len(t) > len(pre) + 6:
            t = t[len(pre):]
    return t.strip()
