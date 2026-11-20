from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from identity_fixture import runtime_cfg
from sidekick.brain.prompt import PromptBuilder
from sidekick.memory.recall import RecallItem, RecallResult


class BotPlaceholderTests(unittest.TestCase):
    def test_persona_uses_configured_bot_name(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "persona" / "roles" / "deepseek").mkdir(parents=True)
            (root / "persona" / "roles" / "deepseek" / "persona.md").write_text(
                "你是{{bot}}。", encoding="utf-8")
            cfg = runtime_cfg(root)
            cfg["persona"]["bot_name"] = "阿鲸"
            builder = PromptBuilder(cfg, root)
            system = builder.build_system(refresh=False)
            self.assertIn("阿鲸", system)
            self.assertNotIn("{{bot}}", system)


class PromptStatsTests(unittest.TestCase):
    def test_stats_report_prefix_sizes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "persona" / "roles" / "deepseek").mkdir(parents=True)
            (root / "persona" / "roles" / "deepseek" / "persona.md").write_text(
                "你是{{bot}}。", encoding="utf-8")
            stats = PromptBuilder(runtime_cfg(root), root).stats()
            self.assertGreaterEqual(stats["prefix_systems"], 1)
            self.assertGreater(stats["prefix_chars"], 0)


class FakeHistory:
    history_max = 40

    def messages(self):
        return [{"role": "user", "content": "历史消息"}]


class FakeWorldbook:
    id = "g"
    error = None
    enabled = True

    def core_text(self, refresh=True):
        return "游戏核心"

    def always_entries(self, chapter=""):
        return [{"title": "常驻", "content": "常驻设定"}]

    def stats(self, refresh=False):
        return {"entries": 1}


class FakeMemory:
    enabled = True
    last_recall: list = []
    last_recall_report: dict = {}

    def current_chapter(self):
        return ""

    def game_state_block(self, *, expand_tasks=False):
        return "[当前游戏]\n章节：第一章"

    def recent_live_block(self):
        return "[最近记得的]（越靠后越近）\n- 11月14日 20:40–21:10｜最近的事"

    def recent_summary_ids(self):
        return set()

    def mark_recalled(self, candidates):
        self.last_recall = list(candidates)


class FakeRecall:
    def __init__(self):
        self.queries = []

    def run(self, material, history, chapter="", label=None):
        self.queries.append(label)
        return RecallResult(
            episodes=[RecallItem(
                source="memory", kind="summary", id="s1", title="",
                content="你想起的旧事", score=0.6, tier="strong",
                channel="vector", start_ts=1, end_ts=2)],
            game=[RecallItem(
                source="memory", kind="plot", id="p1", title="铁匠",
                content="铁匠在旧城", score=0.59, tier="strong",
                channel="keyword", hits=["铁匠"])],
            worldbook=[RecallItem(
                source="worldbook", kind="worldbook", id="w1", title="北门",
                content="北门晚上会关", score=0.7, tier="strong",
                channel="vector")],
            query="铁匠",
            report={"pools": {}, "time_intent": None, "time_fallback": False,
                    "degraded": None})


class PromptOrderTests(unittest.TestCase):
    def test_message_order_and_no_permanent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = runtime_cfg(root)
            builder = PromptBuilder(
                cfg, root, history=FakeHistory(), memory=FakeMemory(),
                worldbook=FakeWorldbook(), recall=FakeRecall())
            text = "\n\n".join(
                str(m.get("content") or "")
                for m in builder.build_messages("[阿明说的话]\n  铁匠在哪"))
            order = ["[当前游戏]", "[世界书·常驻]", "这些是以前发生",
                     "[最近记得的]", "今天是 ", "[你想起的往事]",
                     "[游戏里想起的]", "[世界书]"]
            positions = [text.index(x) for x in order]
            self.assertEqual(positions, sorted(positions))
            self.assertNotIn("你一直记得", text)
            self.assertIn("【铁匠】", text)
            self.assertNotIn("[你一直记得]", builder.prefix_messages()[0]["content"])

    def test_prefix_has_no_memory_block(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = runtime_cfg(root)
            builder = PromptBuilder(cfg, root, memory=FakeMemory())
            prefix = builder.prefix_messages()
            self.assertTrue(all("[最近记得的]" not in x["content"]
                                for x in prefix))

    def test_resident_before_history_volatile_after(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = runtime_cfg(root)
            builder = PromptBuilder(
                cfg, root, history=FakeHistory(), memory=FakeMemory(),
                worldbook=FakeWorldbook(), recall=FakeRecall())
            messages = builder.build_messages("[阿明说的话]\n  铁匠在哪")
            contents = [str(x.get("content") or "") for x in messages]
            resident = next(i for i, x in enumerate(contents)
                            if "[当前游戏]" in x)
            history = next(i for i, x in enumerate(contents)
                           if x == "历史消息")
            volatile = next(i for i, x in enumerate(contents)
                            if x.startswith("今天是 "))
            self.assertLess(resident, history)
            self.assertLess(history, volatile)

    def test_identity_marker_is_system_before_material(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = runtime_cfg(root)
            builder = PromptBuilder(cfg, root, history=FakeHistory(),
                                    memory=FakeMemory())
            messages = builder.build_messages(
                "[阿明说的话]\n  我在", identity_marker="[身份切换]\n角色变了")
            marker = next(i for i, m in enumerate(messages)
                          if str(m.get("content") or "").startswith("[身份切换]"))
            material = next(i for i, m in enumerate(messages)
                            if m.get("role") == "user"
                            and str(m.get("content") or "").startswith("[阿明"))
            self.assertEqual(messages[marker]["role"], "system")
            self.assertLess(marker, material)


if __name__ == "__main__":
    unittest.main()
