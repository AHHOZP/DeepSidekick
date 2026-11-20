"""记忆压缩编排：组材料、调模型、解析、审核、在同一事务里落库。

prompt/解析在 `sidekick/memory/parse.py`，落库在 `sidekick/memory/long_term.py`，
异常审核在 `sidekick/memory/audit.py`；Brain 只负责在“这一轮真正说完”之后
调 `maybe_start()`。具体压缩在后台跑，不挡下一轮说话，失败就留着历史
等下一轮重试。
"""
from __future__ import annotations

import asyncio
import json
import time

from sidekick.brain.llm import LLMError, parse_completion
from sidekick.memory.audit import Audit
from sidekick.memory.limits import (KEYWORD_AUDIT_AT, SUMMARY_MAX_FAILURES)
from sidekick.memory.parse import build_summary_system, parse_summary
from sidekick.memory.timefmt import format_span
from sidekick.core.ports import LLM

#: 总结是后台长任务：固定高推理；只等首个正文 token，收到后交给上游结束。
#: 实测同一批材料 high 推理首字 8-17 秒（2026-11-17，当前网关），5 秒必然失败；
#: 放到 25 秒：覆盖实测上界又不会在真断网时挂太久，保留“首字后不再设总超时”。
SUMMARY_REASONING_EFFORT = "high"
SUMMARY_FIRST_TOKEN_TIMEOUT_S = 25.0


class SummaryCallError(RuntimeError):
    """总结请求在首字/传输/结构阶段失败；`kind` 只用于给用户看的分类。"""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(detail or kind)
        self.kind = kind


def _error_text(exc: Exception) -> str:
    if isinstance(exc, SummaryCallError):
        if exc.kind == "timeout":
            return ("总结失败：等待首字超过 "
                    f"{SUMMARY_FIRST_TOKEN_TIMEOUT_S:g} 秒"
                    "（网络过慢或模型思考过长）")
        if exc.kind == "empty":
            return "总结失败：模型没有返回有效总结（空响应）"
        if exc.kind == "bad_response":
            return "总结失败：模型返回结构不对，不是有效总结"
        return f"总结失败：{str(exc)[:220]}"
    if isinstance(exc, LLMError):
        msg = str(exc)
        if "等待首字超时" in msg:
            return _error_text(SummaryCallError("timeout", msg))
        if "没有收到正文" in msg or "只有思考内容" in msg:
            return _error_text(SummaryCallError("empty", msg))
        if exc.status in (401, 403):
            return f"总结失败：鉴权失败（HTTP {exc.status}）"
        if exc.status == 429:
            return "总结失败：请求太频繁（HTTP 429）"
        if exc.status is not None:
            return f"总结失败：网络/网关错误（HTTP {exc.status}）"
        low = msg.lower()
        if any(x in low for x in (
                "timeout", "timed out", "connect", "network", "dns",
                "流式调用失败", "clientconnector", "serverdisconnected",
                "payload", "connection")):
            return f"总结失败：网络错误（{msg[:180]}）"
        return f"总结失败：请求失败（{msg[:180]}）"
    return f"总结失败：{type(exc).__name__}: {str(exc)[:180]}"

# 材料很多时，这些短句/空话不算有效总结，交给统一审核判断。
_WEAK_CONTENT = {"好的", "好", "收到", "嗯", "知道了", "没事", "没什么"}


def _recent_block(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = ["[最近已保存的总结]（只能 merge 到这些 id；id 取前 8 位即可）"]
    for x in rows:
        span = format_span(x.get("start_ts") or 0, x.get("end_ts") or 0)
        title = str(x.get("title") or "").strip()
        body = str(x.get("content") or "").strip()
        lines.append(f"- id={str(x.get('id') or '')[:8]}｜{span}｜{title}")
        if body:
            lines.append(f"  {body}")
    return "\n".join(lines)


def _input_text(batch: list[dict]) -> str:
    """只整理这一批；不把尚未压缩的尾巴混进来。

    尾巴如果也进了 segments，`start_ts/end_ts`（只按 batch 算）就会比正文
    里的时间早，WebUI 的陪玩记忆时间看起来会慢半小时；尾巴等它自己的批次
    再总结。
    """
    lines = ["[这一批要整理的材料摘要 + 你当时说的话]"]
    for m in batch:
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        if m.get("role") == "user":
            lines.append(content if content.startswith("[材料摘要]")
                         else "[材料摘要]\n" + content)
        else:
            lines.append("[你当时说的]\n" + content)
    return "\n\n".join(lines)


def _obvious_problem(segments: list[dict], batch: list[dict]) -> bool:
    if any(not str(s.get("content") or "").strip() for s in segments):
        return True
    if len(segments) > 5:
        return True
    if len(batch) >= 8:
        if not segments:
            return True
        for s in segments:
            text = str(s.get("content") or "").strip()
            if text in _WEAK_CONTENT or len(text) < 4:
                return True
    return False


class Summarizer:
    def __init__(self, cfg: dict, *, profile, worldbook, history, memory,
                 llm: LLM, log=print) -> None:
        self.cfg = cfg
        self.profile = profile
        self.worldbook = worldbook
        self.history = history
        self.memory = memory
        self.llm = llm
        self.log = log
        self.audit = Audit(llm, log)
        self.error: str | None = None
        self.empty = 0
        self.keyword_audits = 0
        self.failures = 0
        self._task: asyncio.Task | None = None
        self._running = False
        self._current_batch: list[dict] | None = None
        self._failure_batch: tuple[int, ...] | None = None

    def reload(self, cfg: dict, *, profile=None) -> None:
        self.cfg = cfg
        if profile is not None:
            self.profile = profile

    @property
    def dropping_history(self) -> bool:
        return self.failures >= SUMMARY_MAX_FAILURES

    def _set_failures(self, count: int) -> None:
        self.failures = max(0, int(count))
        setter = getattr(self.history, "set_summary_failures", None)
        if setter is not None:
            setter(self.failures)

    # ── 触发 ──────────────────────────────────────────
    def maybe_start(self) -> None:
        if self._running:
            return
        if self._task is not None and not self._task.done():
            return
        try:
            if not self.history.needs_summary():
                return
        except Exception as e:
            self.error = f"检查压缩失败：{type(e).__name__}: {e}"
            self.log(f"[summary] {self.error}")
            return
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            await self._summarize()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._fail(f"总结异常，下一轮重试：{type(e).__name__}: {e}")
        finally:
            self._running = False

    async def _summarize(self) -> None:
        if not self.history.needs_summary():
            return
        batch = self.history.batch()
        if not batch:
            return
        # 失败计数属于当前批：换批后从 0 开始，不能让上一批的连续失败
        # 直接触发新批的毒批归档。
        key = tuple(int(x["id"]) for x in batch
                    if isinstance(x, dict) and x.get("id") is not None)
        if self._failure_batch is not None and key != self._failure_batch:
            self._set_failures(0)
        self._failure_batch = key
        self._current_batch = batch
        try:
            await self._summarize_batch(batch)
        finally:
            self._current_batch = None

    async def _summarize_batch(self, batch: list[dict]) -> None:
        summary_game_id = str(batch[0].get("game_id") or "")
        recent = self.memory.store.recent_summaries(5)
        recent_ids = [x["id"] for x in recent]

        bot_name = str(self.cfg["persona"]["bot_name"]).strip()
        systems = [build_summary_system(self.profile,
                                        summary_game_id or "当前游戏",
                                        bot_name)]
        block = _recent_block(recent)
        if block:
            systems.append(block)
        game_block = self.memory.game_state_block(game_id=summary_game_id)
        if game_block:
            systems.append(game_block)
        user_text = _input_text(batch)
        messages = [{"role": "system", "content": s} for s in systems if s]
        messages.append({"role": "user", "content": user_text})

        try:
            out, finish_reason = await self._ask_model(messages)
        except (SummaryCallError, LLMError) as e:
            self._fail(_error_text(e))
            return
        except (AttributeError, IndexError, TypeError):
            self._fail("总结失败：模型返回结构不对，不是有效总结")
            return
        if not str(out or "").strip():
            self._fail(_error_text(SummaryCallError(
                "empty", "模型没有返回有效总结")))
            return

        payload = await self._review_summary(
            messages, out, finish_reason, batch)
        if payload is None:
            return
        payload = await self._review_keywords(messages, payload)
        if payload is None:
            return

        try:
            changed = await asyncio.to_thread(
                self.memory.commit_summary, payload, batch,
                game_id=summary_game_id, recent_ids=recent_ids)
        except Exception as e:
            self._fail(f"总结落库异常，下一轮重试："
                       f"{type(e).__name__}: {e}"[:300])
            return
        if changed is None:
            self._fail(self.memory.last_summary_error
                       or "总结落库失败，下一轮重试")
            return
        self._set_failures(0)
        if not payload.get("segments"):
            self.empty += 1
        self.history.drop_batch()          # 事务里已删；这里清掉批标记
        try:
            self.memory.index_pending()
        except Exception as e:
            # 向量补建失败不影响已提交的总结；下一轮还会扫到这批行。
            self.log(f"[memory] 总结后补向量失败（忽略）："
                     f"{type(e).__name__}: {e}")
        self.error = None
        self.memory.last_summary_error = None

    async def _ask_model(self, messages: list[dict]) -> tuple[str, str | None]:
        """总结请求：固定 high；首字超过上限失败，首字后不再设总超时。

        `stream_chat` 的思考事件不算首字，必须等到第一个正文 delta；
        这样“思考过长”和“网络没响应”都能在同一个首字边界识别出来。
        """
        stream = getattr(self.llm, "stream_chat", None)
        if stream is None:
            try:
                data = await self.llm.chat(
                    messages, temperature=0.3,
                    reasoning_effort=SUMMARY_REASONING_EFFORT, attempts=1)
                return parse_completion(data)
            except (AttributeError, IndexError, TypeError) as e:
                raise SummaryCallError("bad_response", str(e)) from e

        agen = stream(
            messages, temperature=0.3,
            reasoning_effort=SUMMARY_REASONING_EFFORT,
            first_token_timeout=SUMMARY_FIRST_TOKEN_TIMEOUT_S)
        chunks: list[str] = []
        first = False
        finish_reason: str | None = None
        deadline = time.monotonic() + SUMMARY_FIRST_TOKEN_TIMEOUT_S
        try:
            while True:
                if first:
                    event = await agen.__anext__()
                else:
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining <= 0:
                        raise SummaryCallError("timeout", "等待首字超时")
                    try:
                        event = await asyncio.wait_for(
                            agen.__anext__(), timeout=remaining)
                    except asyncio.TimeoutError as e:
                        raise SummaryCallError(
                            "timeout", "等待首字超时") from e
                kind = event.get("type")
                if kind == "delta":
                    text = str(event.get("text") or "")
                    if text:
                        first = True
                        chunks.append(text)
                elif kind == "finish":
                    finish_reason = event.get("reason")
        except StopAsyncIteration:
            pass
        finally:
            close = getattr(agen, "aclose", None)
            if close is not None:
                try:
                    await close()
                except (RuntimeError, GeneratorExit):
                    pass
        if not first:
            raise SummaryCallError("empty", "没有收到正文")
        return "".join(chunks), finish_reason

    async def _review_summary(self, messages: list[dict], out: str,
                              finish_reason: str | None,
                              batch: list[dict]) -> dict | None:
        """返回可提交的 payload；审核失败返回 None。"""
        out = str(out or "").strip()
        need_audit = (not out or finish_reason == "length")
        payload = parse_summary(out) if out and finish_reason != "length" else {
            "segments": None, "game": {}}
        if payload.get("segments") is None:
            need_audit = True
        elif _obvious_problem(payload["segments"], batch):
            need_audit = True

        if not need_audit:
            return payload

        result = await self.audit.review("summary", messages, out)
        if result.status == "failed":
            self._fail(f"总结审核失败，下一轮重试：{result.error or '未知'}")
            return None
        if result.status == "pass":
            if payload.get("segments") is None:
                self._fail("总结解析为空，审核未给出修正，下一轮重试")
                return None
            return payload
        payload = parse_summary(result.text)
        segments = payload.get("segments")
        if segments is None or any(not s.get("content") for s in segments):
            self._fail("总结审核修正后仍解析失败，下一轮重试")
            return None
        return payload

    async def _review_keywords(self, messages: list[dict],
                               payload: dict) -> dict | None:
        segments = payload.get("segments") or []
        target = None
        for seg in segments:
            if len(seg.get("keywords") or []) > KEYWORD_AUDIT_AT:
                target = seg
                break
        if target is None:
            return payload
        self.keyword_audits += 1
        candidate = json.dumps(target.get("keywords") or [], ensure_ascii=False)
        result = await self.audit.review("keywords", messages, candidate)
        if result.status == "failed":
            self._fail("关键词审核失败，下一轮重试："
                       f"{(result.error or result.text)[:160]}")
            return None
        if result.status == "fixed":
            try:
                value = json.loads(result.text)
            except (TypeError, ValueError, json.JSONDecodeError):
                value = None
            if isinstance(value, list) and all(isinstance(x, str) for x in value):
                target["keywords"] = [x for x in value if x.strip()]
        return payload

    def _salvage_poison_batch(self, batch: list[dict]) -> None:
        """连续失败达上限：把最老一批压成兜底总结，解除队列阻塞。"""
        try:
            gid = str(batch[0].get("game_id") or "")
            lines = [str(r.get("content") or "").replace("[材料摘要]", "").strip()
                     for r in batch if r.get("role") == "user"]
            snippet = "；".join(x for x in lines if x)[:160] or "日常交流"
            payload = {
                "segments": [{
                    "op": "new",
                    "target": "",
                    "title": "自动归档片段",
                    "content": f"这段时间进行了交流（总结连续失败自动归档）：{snippet}",
                    "keywords": [],
                }],
                "game": {},
            }
            res = self.memory.commit_summary(payload, batch, game_id=gid)
            if res is not None:
                self._set_failures(0)
                self.history.drop_batch()
                self.error = None
                self.memory.last_summary_error = None
                self.log("[memory] 总结连续失败达上限，已将该批历史降级归档")
            else:
                self.log("[memory] 毒批降级归档失败，保留历史待下次")
        except Exception as e:
            self.log(f"[memory] 毒批降级归档失败：{type(e).__name__}: {e}")

    def _fail(self, message: str) -> None:
        self._set_failures(self.failures + 1)
        self.error = str(message)[:300]
        self.memory.last_summary_error = self.error
        self.log(f"[memory] {self.error}")
        if self.failures >= SUMMARY_MAX_FAILURES and self._current_batch:
            self._salvage_poison_batch(self._current_batch)

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
