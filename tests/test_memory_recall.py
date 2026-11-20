from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from sidekick import config
from sidekick.brain.user import UserProfile
from sidekick.brain.worldbook import Entry
from sidekick.memory.long_term import Memory
from sidekick.memory.recall import RecallEngine, time_window, wants_all_tasks
from sidekick.memory.store import MemoryStore


class FakeEmbedder:
    enabled = True
    ready = True
    state = "ready"
    model = "fake"
    identity = "fake|d2"
    error = None
    last_degraded = None

    def __init__(self, vec=(1.0, 0.0)):
        self.vec = np.asarray(vec, dtype=np.float32)

    async def query(self, text, timeout_ms=None):
        return self.vec

    def prefetch(self, text):
        return None

    def submit_docs(self, texts, callback=None):
        return None

    def stats(self):
        return {"state": "ready"}

    def start(self):
        return None

    def close(self):
        return None


class FakeWorldbookIndex:
    def __init__(self, sims=None):
        self.sims = sims or {}

    def sync(self):
        return None

    def similarities(self, query, allowed_ids):
        return {k: v for k, v in self.sims.items() if k in allowed_ids}


class FakeWorldbook:
    enabled = True

    def __init__(self, entries=()):
        self.entries = list(entries)

    def entries_snapshot(self):
        return list(self.entries)

    def allowed(self, entry, chapter):
        return True


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cfg = config.defaults()
        cfg["memory"].update({
            "enabled": True, "db_path": str(self.root / "m.db"),
            "recall_episodes": 4, "recall_game": 2, "recall_worldbook": 4,
            "recall_recent": 0, "recall_min_score": 0.35,
            "recall_strong_score": 0.55, "min_keyword_len": 2,
        })
        cfg["game"].update({"enabled": True, "id": "g"})
        cfg["persona"] = {"bot_name": "小鲸", "aliases": []}
        self.cfg = cfg
        self.store = MemoryStore(self.root / "m.db")
        self.memory = Memory(cfg, self.root, store=self.store)
        self.wb = FakeWorldbook()
        self.wb_index = FakeWorldbookIndex()
        self.embedder = FakeEmbedder()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _engine(self):
        return RecallEngine(self.cfg, self.memory, self.wb, self.wb_index,
                            self.embedder, profile=UserProfile(name="阿明"))

    def _summary(self, content: str, vec, keywords=(), end_ts=None):
        row = self.store.add_memory(
            scope="live", kind="summary", content=content,
            title=content[:10], keywords=list(keywords),
            start_ts=(end_ts or time.time()) - 60,
            end_ts=end_ts if end_ts is not None else time.time())
        self.store.update_memory(row["id"], vector=np.asarray(
            vec, dtype=np.float32).tobytes(), vector_model="fake|d2")
        return row

    def _run(self, text: str):
        self.memory.reload(self.cfg)
        engine = self._engine()
        asyncio.run(engine.prepare(text, [], label="阿明"))
        return engine.run(text, [], label="阿明")

    @staticmethod
    def _cone(i: int, n: int):
        vec = [0.6] + [0.0] * n
        vec[1 + i] = 0.8
        return tuple(vec)

    def test_strong_overflow_is_not_cut_by_k(self):
        self.cfg["memory"]["recall_episodes"] = 2
        n = 6
        self.embedder.vec = np.asarray([1.0] + [0.0] * n, dtype=np.float32)
        for i in range(n):
            self._summary(f"第{i}件同一件事", self._cone(i, n), ["钩子"])
        result = self._run("[阿明说的话]\n  12:00:00 钩子的事")
        self.assertEqual(len(result.episodes), n)

    def test_weak_candidates_fill_k_only(self):
        self.cfg["memory"]["recall_episodes"] = 2
        self.embedder.vec = np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        vecs = [
            (0.4, 0.9165, 0.0, 0.0), (0.4, -0.9165, 0.0, 0.0),
            (0.4, 0.0, 0.9165, 0.0), (0.4, 0.0, -0.9165, 0.0),
            (0.4, 0.0, 0.0, 0.9165),
        ]
        for i, vec in enumerate(vecs):
            self._summary(f"弱相关{i}", vec)
        result = self._run("[阿明说的话]\n  12:00:00 随便问问")
        self.assertEqual(len(result.episodes), 2)
        self.assertTrue(all(x.tier == "weak" for x in result.episodes))

    def test_keyword_hits_sort_by_distinct_count(self):
        self.cfg["memory"]["recall_episodes"] = 4
        self._summary("只命中一个", (0.0, 1.0), ["铁匠"])
        self._summary("命中两个", (0.0, -1.0), ["铁匠", "钥匙"])
        result = self._run("[阿明说的话]\n  12:00:00 铁匠的钥匙")
        self.assertEqual(result.episodes[0].content, "命中两个")
        self.assertEqual(len(result.episodes[0].hits), 2)
        self.assertGreater(result.episodes[0].score,
                           result.episodes[1].score)

    def test_time_intent_filters_and_falls_back(self):
        now = time.time()
        self._summary("昨天的事", (1.0, 0.0), end_ts=now - 86400)
        self._summary("今天的事", (1.0, 0.0), end_ts=now)
        self.cfg["memory"]["recall_episodes"] = 4
        result = self._run("[阿明说的话]\n  12:00:00 昨天说的那件事")
        self.assertEqual([x.content for x in result.episodes], ["昨天的事"])
        self.assertFalse(result.report["time_fallback"])

    def test_time_intent_falls_back_when_window_empty(self):
        now = time.time()
        self._summary("今天的事", (1.0, 0.0), end_ts=now)
        self.cfg["memory"]["recall_episodes"] = 4
        result = self._run("[阿明说的话]\n  12:00:00 前天的事还记得吗")
        self.assertTrue(result.report["time_fallback"])
        self.assertEqual(len(result.episodes), 1)

    def test_recent_summaries_are_not_repeated_in_recall(self):
        self.cfg["memory"]["recall_recent"] = 2
        self.cfg["memory"]["recall_episodes"] = 4
        n = 4
        self.embedder.vec = np.asarray([1.0] + [0.0] * n, dtype=np.float32)
        for i in range(n):
            self._summary(f"事情{i}", self._cone(i, n),
                          end_ts=time.time() + i)
        result = self._run("[阿明说的话]\n  12:00:00 事情")
        self.assertEqual(len(result.episodes), 2)

    def test_duplicate_summary_keeps_newer(self):
        now = time.time()
        self._summary("旧说法", (1.0, 0.0), end_ts=now - 100)
        b = self._summary("新说法", (1.0, 0.0), end_ts=now)
        result = self._run("[阿明说的话]\n  12:00:00 同一件事")
        self.assertEqual([x.id for x in result.episodes], [b["id"]])

    def test_build_query_drops_digest_sections_and_label(self):
        from sidekick.memory.recall import build_query

        history = [{"role": "user", "content": (
            "[材料摘要]\n现在：03:00\n阿明：上一句\n"
            "弹幕：观众的话\n画面：一张图\n冷场：很久没说话")}]
        query = build_query("[阿明说的话]\n  12:00:00 嗯", history, "阿明")
        self.assertIn("上一句", query)
        self.assertIn("嗯", query)
        for word in ("弹幕", "观众的话", "画面", "一张图", "冷场", "现在"):
            self.assertNotIn(word, query)

    def test_completed_quest_is_recalled(self):
        self.store.add_memory(
            scope="game", kind="quests", game_id="g", title="找钥匙",
            content="找钥匙：铁匠让你去拿", keywords=["钥匙"])
        self.cfg["memory"]["recall_game"] = 2
        engine = self._engine()
        asyncio.run(engine.prepare("[阿明说的话]\n  12:00:00 钥匙拿到了", [],
                                   label="阿明"))
        result = engine.run("[阿明说的话]\n  12:00:00 钥匙拿到了", [],
                            label="阿明")
        self.assertTrue(any("找钥匙" in x.content for x in result.game))

    def test_worldbook_and_memory_are_both_selected(self):
        self._summary("私人记忆", (0.6, 0.8))
        entry = Entry(id="e1", title="北门", content="北门晚上会关。",
                      keys=["北门"])
        self.wb.entries = [entry]
        self.wb_index.sims = {"e1": 0.9}
        self.cfg["memory"]["recall_episodes"] = 1
        self.cfg["memory"]["recall_worldbook"] = 1
        result = self._run("[阿明说的话]\n  12:00:00 北门怎么走")
        self.assertEqual(len(result.episodes), 1)
        self.assertEqual(len(result.worldbook), 1)

    def test_all_tasks_intent(self):
        self.assertTrue(wants_all_tasks("我还有哪些任务"))
        self.assertFalse(wants_all_tasks("今天天气不错"))

    def test_time_window_helpers(self):
        now = time.mktime((2026, 11, 14, 12, 0, 0, 0, 0, -1))
        start, end = time_window("昨天", now)
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(start)),
                         "2026-11-13")
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(end)),
                         "2026-11-14")


if __name__ == "__main__":
    unittest.main()
