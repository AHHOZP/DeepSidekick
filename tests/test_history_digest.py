from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from sidekick import config
from sidekick.app.identity import resolve
from sidekick.brain import users
from sidekick.brain.brain import Brain
from sidekick.brain.material import material_digest
from sidekick.brain.worldbook import Worldbook
from sidekick.core.null import (
    InMemoryHistory, NullAvatarActions, NullLongTerm,
)
from sidekick.memory.history import SqliteHistory
from sidekick.memory.store import MemoryStore


class FakeLLM:
    source = "fake"
    model = "fake"
    effort = ""
    effort_dropped = False
    ready = True
    temp = 0.3
    warmup_enabled = False

    def request_body(self, messages, temperature=None):
        return {"messages": messages}

    async def chat(self, messages, *, temperature=None, timeout=None,
                   attempts=1):
        return {"choices": [{"message": {"content": "好"},
                             "finish_reason": "stop"}]}

    async def close(self):
        return None


class HistoryDigestTests(unittest.TestCase):
    def test_digest_keeps_full_text_and_structure(self):
        long = "这句原话很长" * 200
        text = (f"[现在]20:00:00｜陪了 1 分钟\n"
                f"[阿明说的话]\n  20:00:01 {long}\n"
                f"[弹幕]2 条\n  20:00:02 小张：来了\n"
                f"  20:00:03 阿蓝：冲")
        digest = material_digest(text, "阿明")
        self.assertIn(long, digest)
        self.assertIn("阿明：", digest)
        self.assertIn("弹幕：", digest)
        self.assertNotIn("[现在]", digest)

    def test_digest_without_known_sections_returns_full_text(self):
        text = "一段没有段名的材料" * 200
        self.assertEqual(material_digest(text, "阿明"), text)

    def test_batch_rows_carry_ts(self):
        with tempfile.TemporaryDirectory() as td:
            store = MemoryStore(Path(td) / "m.db")
            try:
                history = SqliteHistory(store, history_max=4, summarize_every=2)
                history.add_exchange("u1", "a1")
                history.add_exchange("u2", "a2")
                rows = history.batch()
                self.assertEqual([r["role"] for r in rows], ["user", "assistant"])
                self.assertTrue(all(r["ts"] > 0 for r in rows))
            finally:
                store.close()

    def test_failed_summary_extends_window_until_cap(self):
        with tempfile.TemporaryDirectory() as td:
            store = MemoryStore(Path(td) / "m.db")
            try:
                history = SqliteHistory(store, history_max=40,
                                        summarize_every=16)
                for i in range(21):
                    history.add_exchange(f"u{i}", f"a{i}")
                self.assertEqual(len(history.messages()), 40)
                history.set_summary_failures(1)
                self.assertEqual(len(history.messages()), 42)
                history.set_summary_failures(4)
                self.assertEqual(len(history.messages()), 42)
                history.set_summary_failures(5)
                self.assertEqual(len(history.messages()), 40)
                history.set_summary_failures(0)
                self.assertEqual(len(history.messages()), 40)
            finally:
                store.close()

    def test_brain_stores_digest_not_raw_material(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            users.write(root, "default", {"name": "阿明"})
            cfg = resolve(config.defaults(), root).cfg
            cfg["memory"]["enabled"] = False
            cfg["game"]["enabled"] = False
            history = InMemoryHistory(10)
            brain = Brain(cfg, history=history, memory=NullLongTerm(),
                          worldbook=Worldbook(cfg, root),
                          actions=NullAvatarActions(), llm=FakeLLM(),
                          root=root, log=lambda *a, **k: None)
            material = ("[现在]20:00:00\n[阿明说的话]\n  20:00:01 "
                        "铁匠让我去旧城找钥匙\n[弹幕]1 条\n  20:00:02 小张：冲")
            out = asyncio.run(brain.reply(material))
            self.assertEqual(out, "好")
            user = history.messages()[0]["content"]
            self.assertTrue(user.startswith("[材料摘要]"))
            self.assertIn("阿明：", user)
            self.assertIn("铁匠让我去旧城找钥匙", user)
            self.assertNotIn("[现在]", user)


if __name__ == "__main__":
    unittest.main()
