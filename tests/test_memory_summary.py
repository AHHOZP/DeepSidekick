from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidekick import config
from sidekick.brain.llm import LLMError
from sidekick.brain.user import UserProfile
from sidekick.memory.history import SqliteHistory
from sidekick.memory.long_term import Memory
from sidekick.memory.store import MemoryStore
from sidekick.memory.summary import Summarizer


class FakeLLM:
    model = "fake"
    source = "fake"
    effort = ""
    effort_dropped = False
    ready = True
    temp = 0.3
    warmup_enabled = False

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def _reply(self):
        text = self.replies.pop(0) if self.replies else ""
        reason = "stop"
        if isinstance(text, tuple):
            text, reason = text
        if isinstance(text, Exception):
            raise text
        return str(text), reason

    async def chat(self, messages, *, temperature=None, reasoning_effort=None,
                   timeout=None, attempts=1):
        self.calls.append({"messages": messages, "temperature": temperature,
                           "reasoning_effort": reasoning_effort})
        text, reason = self._reply()
        return {"choices": [{"message": {"content": text},
                             "finish_reason": reason}]}

    async def stream_chat(self, messages, *, temperature=None,
                          reasoning_effort=None, first_token_timeout=30.0):
        self.calls.append({"messages": messages, "temperature": temperature,
                           "reasoning_effort": reasoning_effort,
                           "first_token_timeout": first_token_timeout})
        text, reason = self._reply()
        yield {"type": "open"}
        yield {"type": "delta", "text": text}
        yield {"type": "finish", "reason": reason}

    async def close(self):
        return None


class SlowStreamLLM(FakeLLM):
    async def stream_chat(self, messages, *, temperature=None,
                          reasoning_effort=None, first_token_timeout=30.0):
        self.calls.append({"messages": messages, "temperature": temperature,
                           "reasoning_effort": reasoning_effort,
                           "first_token_timeout": first_token_timeout})
        yield {"type": "open"}
        await asyncio.sleep(0.2)
        yield {"type": "delta", "text": "{\"segments\":[],\"game\":{}}"}


class FakeWorldbook:
    id = "g"
    error = None

    def core_text(self, refresh=True):
        return ""


class SummaryTests(QuietIsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cfg = config.defaults()
        cfg["memory"].update({"enabled": True, "db_path": str(self.root / "m.db"),
                              "history_max": 4, "summarize_every": 2})
        cfg["game"].update({"enabled": True, "id": "g"})
        cfg["persona"] = {"bot_name": "小鲸", "aliases": []}
        self.cfg = cfg
        self.store = MemoryStore(self.root / "m.db")
        self.history = SqliteHistory(self.store, history_max=4,
                                     summarize_every=2)
        self.memory = Memory(cfg, self.root, store=self.store)
        self.profile = UserProfile(name="阿明")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _fill(self, n=2):
        for i in range(n):
            self.history.add_exchange(
                f"[材料摘要]\n阿明：第 {i} 句，铁匠让我找钥匙", f"好{i}")

    def _summarizer(self, replies, llm=None):
        self.llm = llm or FakeLLM(replies)
        return Summarizer(self.cfg, profile=self.profile,
                          worldbook=FakeWorldbook(), history=self.history,
                          memory=self.memory, llm=self.llm,
                          log=lambda *a, **k: None)

    async def test_summary_uses_high_effort_and_measured_first_token_timeout(self):
        self._fill(2)
        out = json.dumps({"segments": [{
            "op": "new", "content": "这条总结要落库。", "keywords": []}],
            "game": {}}, ensure_ascii=False)
        s = self._summarizer([out])
        await s._summarize()
        call = s.llm.calls[0]
        self.assertEqual(call["reasoning_effort"], "high")
        # 当前网关 high 推理首字实测 8–17 秒；5 秒会稳定失败。
        self.assertGreater(call["first_token_timeout"], 17.27)
        self.assertLessEqual(call["first_token_timeout"], 30.0)

    def test_keyword_prompt_rejects_verbs_and_generic_nouns(self):
        from sidekick.memory.audit import AUDIT_SYSTEM
        from sidekick.memory.parse import build_summary_system

        prompt = build_summary_system(self.profile, "g", "小鲸")
        self.assertIn("专有名词优先", prompt)
        self.assertIn("禁止：动词、形容词", prompt)
        self.assertIn("禁止任何对话里都可能出现的泛名词", prompt)
        self.assertIn("专有名词优先", AUDIT_SYSTEM)
        self.assertIn("动词、形容词", AUDIT_SYSTEM)
        from sidekick.memory.parse import _keywords
        self.assertEqual([], _keywords(["游戏", "观众", "情绪", "时间"]))
        self.assertEqual(["黑魂", "铁匠"], _keywords(["黑魂", "铁匠"]))

    async def test_summary_input_only_covers_the_batch(self):
        self._fill(3)
        out = json.dumps({"segments": [], "game": {}}, ensure_ascii=False)
        s = self._summarizer([out])
        await s._summarize()
        user = s.llm.calls[0]["messages"][-1]["content"]
        self.assertIn("第 0 句", user)
        self.assertNotIn("第 1 句", user)
        self.assertNotIn("第 2 句", user)

    async def test_first_token_timeout_is_reported_as_thinking(self):
        self._fill(2)
        s = self._summarizer([LLMError("等待首字超时（5s）")])
        await s._summarize()
        self.assertIn("思考过长", s.error or "")
        self.assertEqual(s.failures, 1)
        self.assertEqual(self.history.count(), 4)

    async def test_first_token_waits_only_five_seconds_even_with_reasoning(self):
        self._fill(2)
        s = self._summarizer([], llm=SlowStreamLLM([]))
        with mock.patch("sidekick.memory.summary.SUMMARY_FIRST_TOKEN_TIMEOUT_S",
                        0.01):
            await s._summarize()
        self.assertIn("思考过长", s.error or "")
        self.assertEqual(self.history.count(), 4)

    async def test_repeated_failures_salvage_old_batch(self):
        self._fill(2)
        s = self._summarizer([LLMError("boom")])
        s.failures = 4
        await s._summarize()
        self.assertEqual(0, s.failures)
        self.assertEqual(2, self.store.count_messages())
        self.assertEqual(
            1, len(self.store.list_memories(scope="live", kind="summary")))

    async def test_new_batch_resets_previous_failure_streak(self):
        self._fill(2)
        s = self._summarizer([LLMError("boom")])
        self.store.delete_messages([m["id"] for m in self.store.messages()])
        self._fill(2)
        s._failure_batch = (1, 2, 3, 4)
        s.failures = 4
        await s._summarize()
        self.assertEqual(1, s.failures)
        self.assertEqual(4, self.store.count_messages())

    async def test_empty_summary_is_reported(self):
        self._fill(2)
        s = self._summarizer([""])
        await s._summarize()
        self.assertIn("没有返回有效总结", s.error or "")
        self.assertEqual(self.history.count(), 4)

    async def test_new_summary_commits_and_deletes_batch(self):
        self._fill(2)
        long = "很长的原话" * 80
        out = json.dumps({"segments": [{
            "op": "new", "title": "铁匠", "content": long,
            "keywords": ["铁匠", "钥匙"]}], "game": {}}, ensure_ascii=False)
        s = self._summarizer([out])
        await s._summarize()
        self.assertEqual(s.failures, 0)
        self.assertEqual(self.history.count(), 2)
        rows = self.store.list_memories(scope="live", kind="summary")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], long)

    async def test_empty_segments_deletes_history_without_summary(self):
        self._fill(2)
        s = self._summarizer(['{"segments":[],"game":{}}'])
        await s._summarize()
        self.assertEqual(self.history.count(), 2)
        self.assertEqual(self.store.list_memories(scope="live",
                                                  kind="summary"), [])

    async def test_parse_failure_is_fixed_by_audit(self):
        self._fill(2)
        fixed = json.dumps({"segments": [{
            "op": "new", "content": "审核修正后的总结。",
            "keywords": ["铁匠"]}], "game": {}}, ensure_ascii=False)
        s = self._summarizer(["不是 JSON", fixed])
        await s._summarize()
        rows = self.store.list_memories(scope="live", kind="summary")
        self.assertEqual(rows[0]["content"], "审核修正后的总结。")

    async def test_audit_failure_keeps_history(self):
        self._fill(2)
        s = self._summarizer(["不是 JSON", "还是不是 JSON"])
        await s._summarize()
        self.assertIsNotNone(s.error)
        self.assertEqual(self.history.count(), 4)
        self.assertEqual(self.store.list_memories(scope="live",
                                                  kind="summary"), [])

    async def test_truncated_output_is_reaudited(self):
        self._fill(2)
        fixed = json.dumps({"segments": [{
            "op": "new", "content": "截断后补全的总结。",
            "keywords": []}], "game": {}}, ensure_ascii=False)
        s = self._summarizer([("半截", "length"), fixed])
        await s._summarize()
        rows = self.store.list_memories(scope="live", kind="summary")
        self.assertEqual(rows[0]["content"], "截断后补全的总结。")

    async def test_merge_rewrites_existing_summary(self):
        old = self.store.add_memory(
            scope="live", kind="summary", content="旧内容", title="旧标题",
            start_ts=1000, end_ts=2000, source="summary")
        self._fill(2)
        out = json.dumps({"segments": [{
            "op": "merge", "target": old["id"][:8], "title": "新标题",
            "content": "重写后的整条总结。", "keywords": ["铁匠"]}],
            "game": {}}, ensure_ascii=False)
        s = self._summarizer([out])
        await s._summarize()
        got = self.store.get_memory(old["id"])
        self.assertEqual(got["content"], "重写后的整条总结。")
        self.assertEqual(len(self.store.list_memories(scope="live",
                                                      kind="summary")), 1)

    async def test_keyword_over_threshold_is_audited_once(self):
        self._fill(2)
        keys = [f"钩子{i}" for i in range(40)]
        out = json.dumps({"segments": [{
            "op": "new", "content": "关键词太多。", "keywords": keys}],
            "game": {}}, ensure_ascii=False)
        s = self._summarizer([out, json.dumps(["铁匠", "钥匙"],
                                              ensure_ascii=False)])
        await s._summarize()
        rows = self.store.list_memories(scope="live", kind="summary")
        self.assertEqual(rows[0]["keywords"], ["铁匠", "钥匙"])
        self.assertEqual(s.keyword_audits, 1)


if __name__ == "__main__":
    unittest.main()
