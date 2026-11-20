from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from sidekick import config
from identity_fixture import runtime_cfg
from sidekick.brain.brain import Brain
from sidekick.core.null import InMemoryHistory, NullAvatarActions
from sidekick.memory.long_term import Memory
from sidekick.brain.worldbook import Worldbook


class GameIdResolutionTests(unittest.TestCase):
    def test_only_game_id_is_used(self):
        cfg = {"game": {"id": "custom"}}
        self.assertEqual(config.resolve_game_id(cfg), "custom")
        self.assertEqual(config.resolve_game_id({"game": {"id": ""}}), "")

    def test_worldbook_and_memory_use_same_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            game = root / "persona" / "games" / "foo"
            game.mkdir(parents=True)
            (game / "core.md").write_text("【Foo】\n世界", encoding="utf-8")
            cfg = config.defaults()
            cfg["game"].update({"enabled": True, "id": "foo"})
            wb = Worldbook(cfg, root)
            self.assertEqual(wb.id, "foo")
            self.assertIn("世界", wb.core_text())
            mem = Memory(cfg, root)
            try:
                self.assertEqual(mem.game_id, "foo")
            finally:
                mem.close()

    def test_unrelated_config_does_not_reload_memory_or_recall(self):
        class LLM:
            source = "fake"
            model = "fake"
            effort = ""
            effort_dropped = False
            ready = True
            warmup_enabled = False

            async def close(self):
                return None

        class Recall:
            calls = 0

            def reload(self, cfg):
                self.calls += 1

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = runtime_cfg(root)
            memory = Memory(cfg, root)
            recall = Recall()
            brain = Brain(
                cfg, history=InMemoryHistory(4),
                memory=memory, worldbook=Worldbook(cfg, root),
                actions=NullAvatarActions(), llm=LLM(), root=root,
                recall=recall, log=lambda *a, **k: None)
            calls: list[str] = []
            brain.history.reload = lambda cfg: calls.append("history")
            brain.memory.reload = lambda cfg: calls.append("memory")
            try:
                unrelated = copy.deepcopy(cfg)
                unrelated["scheduler"]["min_gap_s"] += 1
                brain.reload_config(unrelated)
                self.assertEqual([], calls)
                self.assertEqual(0, recall.calls)

                changed = copy.deepcopy(unrelated)
                changed["game"]["id"] = "another"
                brain.reload_config(changed)
                self.assertEqual(["history", "memory"], calls)
                self.assertEqual(1, recall.calls)
            finally:
                memory.close()

    def test_brain_clears_game_when_id_is_empty(self):
        class LLM:
            source = "fake"
            model = "fake"
            effort = ""
            effort_dropped = False
            ready = True
            warmup_enabled = False

            async def close(self):
                return None

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = runtime_cfg(root)
            cfg["game"].update({"id": "foo"})
            memory = Memory(cfg, root)
            brain = Brain(
                cfg, history=InMemoryHistory(4),
                memory=memory, worldbook=Worldbook(cfg, root),
                actions=NullAvatarActions(), llm=LLM(), root=root,
                log=lambda *a, **k: None)
            try:
                brain.reload_config(cfg)
                self.assertEqual(brain.memory.game_id, "foo")
                cfg["game"].update({"id": ""})
                brain.reload_config(cfg)
                self.assertEqual(brain.memory.game_id, "")
            finally:
                memory.close()


if __name__ == "__main__":
    unittest.main()
