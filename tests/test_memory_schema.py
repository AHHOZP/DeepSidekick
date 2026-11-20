from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from sidekick.memory.store import SCHEMA_VERSION, MemoryStore, empty_stats


class SchemaVersionTests(unittest.TestCase):
    def test_new_db_gets_current_version(self):
        with tempfile.TemporaryDirectory() as td:
            store = MemoryStore(Path(td) / "m.db")
            try:
                self.assertEqual(store.schema_version, SCHEMA_VERSION)
                row = store.conn.execute("PRAGMA user_version").fetchone()
                self.assertEqual(int(row[0]), SCHEMA_VERSION)
            finally:
                store.conn.close()

    def test_old_db_is_rejected_with_clear_message(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "m.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT)")
            conn.execute("PRAGMA user_version=0")
            conn.commit()
            conn.close()

            with self.assertRaises(RuntimeError) as ctx:
                MemoryStore(db)
            self.assertIn("不做数据库迁移", str(ctx.exception))
            self.assertIn(str(db), str(ctx.exception))

    def test_same_version_old_structure_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "m.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE game_state (game_id TEXT PRIMARY KEY, "
                "chapter TEXT, scene TEXT, location TEXT, objective TEXT, "
                "known_facts TEXT, open_threads TEXT, observations TEXT, "
                "updated_at REAL NOT NULL)")
            conn.execute("PRAGMA user_version=1")
            conn.commit()
            conn.close()

            with self.assertRaises(RuntimeError) as ctx:
                MemoryStore(db)
            self.assertIn("结构", str(ctx.exception))
            self.assertIn(str(db), str(ctx.exception))

    def test_new_db_has_memory_vectors_and_timestamps(self):
        with tempfile.TemporaryDirectory() as td:
            store = MemoryStore(Path(td) / "m.db")
            try:
                msg = {r[1] for r in store.conn.execute(
                    "PRAGMA table_info(messages)")}
                self.assertEqual(msg, {"id", "role", "content", "ts", "game_id"})
                mem = {r[1] for r in store.conn.execute(
                    "PRAGMA table_info(memories)")}
                for col in ("vector", "vector_model", "start_ts", "end_ts",
                            "last_used_at"):
                    self.assertIn(col, mem)
                gs = {r[1] for r in store.conn.execute(
                    "PRAGMA table_info(game_state)")}
                self.assertIn("tasks", gs)
                tables = {r[0] for r in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertNotIn("quests", tables)
            finally:
                store.conn.close()

    def test_empty_stats_shape_is_shared(self):
        stats = empty_stats()
        for key in ("enabled", "messages", "live_summaries",
                    "game_plot", "game_npc", "game_quests",
                    "schema_version"):
            self.assertIn(key, stats)
        self.assertNotIn("live_permanent", stats)
        self.assertNotIn("quests_active", stats)
        self.assertNotIn("schema_warning", stats)


if __name__ == "__main__":
    unittest.main()
