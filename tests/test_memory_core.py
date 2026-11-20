from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sidekick import config
from sidekick.memory.history import SqliteHistory
from sidekick.memory.long_term import Memory
from sidekick.memory.parse import parse_summary
from sidekick.memory.store import MemoryStore


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "memory.db"
        cfg = config.defaults()
        cfg["memory"].update({"enabled": True, "db_path": str(self.db)})
        cfg["game"].update({"enabled": True, "id": "test-game"})
        cfg["persona"] = {"bot_name": "小鲸", "aliases": ["小鲸"]}
        self.cfg = cfg
        self.store = MemoryStore(self.db)
        self.history = SqliteHistory(self.store, history_max=40,
                                     summarize_every=16)
        self.memory = Memory(cfg, self.root, store=self.store)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _batch(self, pairs: int = 8, gid: str | None = None) -> list[dict]:
        for i in range(pairs):
            self.history.add_exchange(
                f"[材料摘要]\n阿明：第 {i} 句，铁匠让我找钥匙", f"好，记下了{i}",
                game_id=self.memory.game_id if gid is None else gid)
        return self.history.batch()

    def test_identity_snapshot_roundtrip(self):
        self.assertIsNone(self.memory.last_identity())
        self.memory.set_last_identity("role-a", "user-a")
        self.assertEqual(self.memory.last_identity(), ("role-a", "user-a"))

    def test_add_exchange_rolls_back_both_messages(self):
        original = self.store.add_message
        calls = {"n": 0}

        def fail_second(role, content, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return original(role, content, **kwargs)

        with mock.patch.object(self.store, "add_message", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.history.add_exchange("user", "assistant")
        self.assertEqual(self.store.count_messages(), 0)

    def test_commit_new_segments_and_game(self):
        batch = self._batch(8)
        payload = {
            "segments": [
                {"op": "new", "title": "第一关",
                 "content": "你陪他打过了第一关。他说「有你在我就不困了」。",
                 "keywords": ["第一关"]},
                {"op": "new", "title": "看牙",
                 "content": "他提到周末要去看牙，有点怕。",
                 "keywords": ["看牙", "怕疼"]},
            ],
            "game": {
                "state": {"chapter": "第一章", "location": "旧城",
                          "objective": "找钥匙"},
                "beats": [{"text": "你们在旧城遇到铁匠，他让你去找钥匙。",
                           "keywords": ["旧城", "铁匠", "钥匙"]}],
                "npcs": [{"name": "铁匠", "description": "旧城的铁匠。",
                          "keywords": ["铁匠"]}],
                "quests": [{"action": "add", "title": "找钥匙",
                            "keywords": ["钥匙"]}],
            },
        }
        changed = self.memory.commit_summary(payload, batch)
        self.assertEqual(len(changed), 2)
        self.assertEqual(self.history.count(), 0)
        info = self.memory.stats()
        self.assertEqual(info["live_summaries"], 2)
        self.assertEqual(info["game_plot"], 1)
        self.assertEqual(info["game_npc"], 1)
        self.assertEqual(info["tasks_active"], 1)
        self.assertEqual(info["game_quests"], 0)
        block = self.memory.game_state_block() + self.memory.recent_live_block()
        self.assertIn("铁匠", block)
        self.assertIn("找钥匙", block)
        self.assertIn("第一关", block)

    def test_vector_write_bumps_version_without_touching_updated_at(self):
        row = self.store.add_memory(
            scope="live", kind="summary", content="向量测试",
            title="向量", source="summary")
        before = self.store.get_memory(row["id"])
        v0 = self.store.vector_version()
        self.store.set_memory_vectors(
            [(row["id"], b"\x00\x00\x80?", "fake",
              before["updated_at"])])
        after = self.store.get_memory(row["id"])
        self.assertEqual(before["updated_at"], after["updated_at"])
        self.assertNotEqual(v0, self.store.vector_version())
        self.assertTrue(after["has_vector"])

    def test_worldbook_vector_schema_has_no_unused_dim(self):
        columns = {
            str(row[1]) for row in self.store.conn.execute(
                "PRAGMA table_info(worldbook_vectors)")
        }
        self.assertEqual(
            {"game_id", "entry_id", "content_hash", "model", "vector",
             "updated_at"}, columns)

    def test_normal_message_does_not_invalidate_vector_index(self):
        before = self.store.vector_version()
        self.store.add_message("user", "普通对话")
        self.assertEqual(before, self.store.vector_version())

    def test_vector_write_skips_row_edited_while_encoding(self):
        row = self.store.add_memory(
            scope="live", kind="summary", content="旧正文",
            title="旧", source="summary")
        stale = row["updated_at"]
        time.sleep(0.002)
        self.store.update_memory(row["id"], content="新正文")
        self.store.set_memory_vectors(
            [(row["id"], b"\x00\x00\x80?", "fake", stale)])
        after = self.store.get_memory(row["id"])
        self.assertFalse(after["has_vector"])
        self.assertIn(row["id"], {
            x["id"] for x in self.store.pending_memory_vectors("fake")})

    def test_disabled_rows_do_not_hide_enabled_memories(self):
        old = self.store.add_memory(
            scope="game", kind="plot", game_id="test-game",
            title="活着", content="应被看到")
        for i in range(501):
            row = self.store.add_memory(
                scope="game", kind="plot", game_id="test-game",
                title=f"禁用 {i}", content="不应被看到")
            self.store.set_memory_enabled(row["id"], False)
        rows = self.memory.recent_plot(limit=10)
        self.assertEqual([old["id"]], [x["id"] for x in rows])

    def test_keywords_are_capped_at_eight_on_commit(self):
        batch = self._batch(2)
        payload = {
            "segments": [{
                "op": "new", "title": "关键词",
                "content": "这条总结有很多关键词。",
                "keywords": [f"词{i}" for i in range(12)],
            }],
            "game": {},
        }
        changed = self.memory.commit_summary(payload, batch)
        got = self.store.get_memory(changed[0])
        self.assertEqual(8, len(got["keywords"]))

    def test_same_merge_target_twice_falls_back_to_new(self):
        old = self.store.add_memory(
            scope="live", kind="summary", content="旧内容",
            title="旧", start_ts=1, end_ts=2, source="summary")
        batch = self._batch(2)
        payload = {
            "segments": [
                {"op": "merge", "target": old["id"][:8],
                 "content": "第一次合并。", "keywords": []},
                {"op": "merge", "target": old["id"][:8],
                 "content": "第二次合并。", "keywords": []},
            ],
            "game": {},
        }
        changed = self.memory.commit_summary(
            payload, batch, recent_ids=[old["id"]])
        self.assertEqual(2, len(changed))
        self.assertEqual("第一次合并。",
                         self.store.get_memory(old["id"])["content"])
        rows = self.store.list_memories(scope="live", kind="summary")
        self.assertEqual(2, len(rows))

    def test_merge_rewrites_target_and_clears_vector(self):
        old = self.store.add_memory(
            scope="live", kind="summary", content="旧内容",
            title="旧标题", start_ts=1000, end_ts=2000,
            keywords=["旧"], source="summary")
        self.store.update_memory(old["id"], vector=b"\x00\x00\x80?",
                                 vector_model="fake")
        batch = self._batch(2)
        payload = {
            "segments": [{"op": "merge", "target": old["id"][:8],
                          "title": "新标题",
                          "content": "同一件事的新说法。",
                          "keywords": ["铁匠", "钥匙"]}],
            "game": {},
        }
        changed = self.memory.commit_summary(
            payload, batch, recent_ids=[old["id"]])
        self.assertEqual(changed, [old["id"]])
        got = self.store.get_memory(old["id"])
        self.assertEqual(got["title"], "新标题")
        self.assertEqual(got["content"], "同一件事的新说法。")
        self.assertEqual(got["start_ts"], 1000)
        self.assertGreater(got["end_ts"], 2000)
        self.assertFalse(got["has_vector"])

    def test_empty_segments_deletes_history_without_summary(self):
        batch = self._batch(2)
        changed = self.memory.commit_summary(
            {"segments": [], "game": {}}, batch)
        self.assertEqual(changed, [])
        self.assertEqual(self.history.count(), 0)
        self.assertEqual(self.memory.stats()["live_summaries"], 0)

    def test_empty_segment_content_is_rejected_and_keeps_history(self):
        batch = self._batch(2)
        changed = self.memory.commit_summary(
            {"segments": [{"op": "new", "content": "", "keywords": []}],
             "game": {}}, batch)
        self.assertIsNone(changed)
        self.assertEqual(self.history.count(), 4)

    def test_invalid_merge_target_becomes_new(self):
        batch = self._batch(2)
        changed = self.memory.commit_summary({
            "segments": [{"op": "merge", "target": "not-a-real-id",
                          "content": "这件事还是记一条。", "keywords": []}],
            "game": {}}, batch, recent_ids=[])
        self.assertEqual(len(changed), 1)
        self.assertEqual(self.memory.stats()["live_summaries"], 1)

    def test_parse_new_format_and_empty_segments(self):
        p = parse_summary(
            '{"segments":[{"op":"new","content":"他怕黑。",'
            '"keywords":["怕黑"]}],"game":{}}')
        self.assertEqual(p["segments"][0]["content"], "他怕黑。")
        self.assertEqual(p["segments"][0]["keywords"], ["怕黑"])
        self.assertEqual(parse_summary('{"segments":[]}')["segments"], [])
        self.assertIsNone(parse_summary("not json")["segments"])

    def test_observations_are_stored_and_injected(self):
        batch = self._batch(1)
        payload = parse_summary(
            '{"segments":[],"game":{"state":'
            '{"observations_add":["铁匠怕火"]}}}')
        self.memory.commit_summary(payload, batch)
        block = self.memory.game_state_block()
        self.assertIn("[观察]", block)
        self.assertIn("铁匠怕火", block)

    def test_supersede_disables_old_beat(self):
        old = self.store.add_memory(
            scope="game", kind="plot", game_id="test-game",
            title="旧剧情", content="旧剧情：铁匠还活着。", keywords=["铁匠"])
        batch = self._batch(1)
        payload = parse_summary(
            '{"segments":[],"game":{"beats":['
            '{"text":"铁匠已经死了。","keywords":["铁匠"],'
            f'"supersedes":"{old["id"]}"'
            "}]}}")
        self.memory.commit_summary(payload, batch)
        self.assertFalse(self.store.get_memory(old["id"])["enabled"])

    def test_quest_none_is_ignored(self):
        before = self.memory.active_tasks()
        batch = self._batch(1)
        payload = parse_summary(
            '{"segments":[],"game":{"quests":['
            '{"action":"none","title":"不要新建"}]}}')
        self.memory.commit_summary(payload, batch)
        after = self.memory.active_tasks()
        self.assertEqual(len(after), len(before))
        self.assertFalse(any(q["title"] == "不要新建" for q in after))

    def test_beat_has_no_unused_action_field(self):
        payload = parse_summary(
            '{"segments":[],"game":{"beats":[{"text":"铁匠死了"}]}}')
        self.assertNotIn("action", payload["game"]["beats"][0])

    def test_summary_complete_requires_exact_title(self):
        self.memory.apply_tasks(
            [{"action": "add", "title": "找钥匙", "detail": "铁匠让你去"}])
        batch = self._batch(1)
        payload = parse_summary(
            '{"segments":[],"game":{"quests":['
            '{"action":"complete","title":"找"}]}}')
        self.memory.commit_summary(payload, batch)
        self.assertEqual([x["title"] for x in self.memory.active_tasks()],
                         ["找钥匙"])
        self.assertEqual(self.memory.stats()["game_quests"], 0)

    def test_game_enabled_hot_reload(self):
        self.store.upsert_state("test-game", known_facts=["铁匠在旧城"])
        self.assertIn("铁匠在旧城", self.memory.game_state_block())
        off = config.defaults()
        off["game"].update({"enabled": False, "id": "test-game"})
        self.memory.reload(off)
        self.assertEqual(self.memory.game_state_block(), "")
        on = config.defaults()
        on["game"].update({"enabled": True, "id": "test-game"})
        self.memory.reload(on)
        self.assertIn("铁匠在旧城", self.memory.game_state_block())

    def test_summary_failure_keeps_history(self):
        batch = self._batch(2)
        with mock.patch.object(self.store, "add_memory",
                               side_effect=RuntimeError("db down")):
            changed = self.memory.commit_summary(
                {"segments": [{"op": "new", "content": "总结",
                               "keywords": []}], "game": {}}, batch)
        self.assertIsNone(changed)
        self.assertEqual(self.history.count(), 4)

    def test_task_complete_does_not_guess_when_title_mismatches(self):
        self.memory.apply_tasks(
            [{"action": "add", "title": "找钥匙", "detail": "铁匠让你去"}])
        before = self.memory.active_tasks()
        applied = self.memory.apply_tasks(
            [{"action": "complete", "title": "不存在的任务"}])
        after = self.memory.active_tasks()
        self.assertEqual(applied, [])
        self.assertEqual(len(after), len(before))

    def test_task_immediate_and_summary_correction(self):
        applied = self.memory.apply_tasks(
            [{"action": "add", "title": "找钥匙", "detail": "铁匠让你去"}])
        self.assertEqual(applied[0]["action"], "add")
        self.assertIn("找钥匙", self.memory.game_state_block())

        batch = self._batch(1)
        payload = parse_summary(
            '{"segments":[],"game":{"quests":['
            '{"action":"update","title":"找钥匙",'
            '"new_title":"去旧城找铁匠拿钥匙","detail":"从北门进"}]}}')
        self.memory.commit_summary(payload, batch)
        active = self.memory.active_tasks()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["title"], "去旧城找铁匠拿钥匙")

    def test_task_complete_archives_as_game_memory(self):
        self.memory.apply_tasks(
            [{"action": "add", "title": "找钥匙", "detail": "铁匠让你从北门进"}])
        applied = self.memory.apply_tasks(
            [{"action": "complete", "title": "找钥匙",
              "keywords": ["铁匠", "钥匙"]}])
        self.assertEqual(applied, [{"action": "complete", "title": "找钥匙"}])
        self.assertEqual(self.memory.active_tasks(), [])
        rows = self.store.list_memories(scope="game", kind="quests",
                                        game_id="test-game")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "找钥匙")
        self.assertIn("北门", rows[0]["content"])
        self.assertEqual(rows[0]["keywords"], ["铁匠", "钥匙"])

    def test_game_switch_batch_groups_by_old_game(self):
        self._batch(14)
        cfg_b = config.defaults()
        cfg_b["game"].update({"enabled": True, "id": "new-game"})
        self.memory.reload(cfg_b)
        self._batch(6, gid="new-game")
        batch = self.history.batch()
        self.assertEqual(len(batch), 16)
        self.assertEqual({x["game_id"] for x in batch}, {"test-game"})
        changed = self.memory.commit_summary(
            {"segments": [{"op": "new", "content": "旧游戏的一段。",
                           "keywords": []}],
             "game": {"beats": [{"text": "旧游戏的剧情", "keywords": []}]}},
            batch)
        self.assertEqual(len(changed), 1)
        self.assertEqual(self.history.count(), 24)
        old = self.store.list_memories(scope="game", kind="plot",
                                       game_id="test-game")
        new = self.store.list_memories(scope="game", kind="plot",
                                       game_id="new-game")
        self.assertEqual(len(old), 1)
        self.assertEqual(new, [])

        # 再补 16 条新游戏：最老 16 条里 12 条旧 + 4 条新，只总结旧的那 12 条。
        self._batch(8, gid="new-game")
        batch2 = self.history.batch()
        self.assertEqual(len(batch2), 12)
        self.assertEqual({x["game_id"] for x in batch2}, {"test-game"})
        self.memory.commit_summary(
            {"segments": [{"op": "new", "content": "旧游戏收尾。",
                           "keywords": []}], "game": {}}, batch2)
        self.assertEqual(self.history.count(), 28)

    def test_game_candidates_are_isolated_by_game(self):
        self.store.add_memory(scope="game", kind="plot", game_id="old",
                              title="旧游戏", content="旧游戏的铁匠",
                              keywords=["铁匠"])
        cfg = config.defaults()
        cfg["game"].update({"enabled": True, "id": "new"})
        self.memory.reload(cfg)
        self.assertEqual(self.memory.game_candidates(), [])


if __name__ == "__main__":
    unittest.main()
