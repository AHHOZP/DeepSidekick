from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from sidekick import config
from sidekick.brain.worldbook import Worldbook
from sidekick.memory.store import MemoryStore
from sidekick.memory.worldbook_index import WorldbookIndex


class FakeEmbedder:
    ready = True
    identity = "fake|d2"
    model = "fake"

    def __init__(self):
        self.texts = []

    def submit_docs(self, texts, callback=None):
        self.texts.extend(texts)
        if callback:
            callback([np.asarray([1.0, 0.0], dtype=np.float32)
                      for _ in texts])
        return None


class WorldbookIndexTests(unittest.TestCase):
    def test_sync_diffs_and_queries(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            game = root / "persona" / "games" / "g"
            (game / "entries").mkdir(parents=True)
            (game / "core.md").write_text("总览", encoding="utf-8")
            entry = game / "entries" / "north.md"
            entry.write_text(
                "---\ntitle: 北门\nkeys: [北门]\n---\n北门晚上会关。\n",
                encoding="utf-8")
            cfg = config.defaults()
            cfg["game"].update({"enabled": True, "id": "g"})
            store = MemoryStore(root / "m.db")
            try:
                wb = Worldbook(cfg, root)
                emb = FakeEmbedder()
                index = WorldbookIndex(store, wb, emb)
                index.sync()
                self.assertEqual(len(emb.texts), 1)
                self.assertIn("北门", emb.texts[0])
                rows = store.worldbook_hashes("g")
                self.assertIn("north", rows)
                sims = index.similarities(
                    np.asarray([1.0, 0.0], dtype=np.float32), {"north"})
                self.assertAlmostEqual(sims["north"], 1.0)

                entry.write_text(
                    "---\ntitle: 北门\nkeys: [北门]\n---\n北门白天也关。\n",
                    encoding="utf-8")
                index.sync()
                self.assertEqual(len(emb.texts), 2)
                entry.unlink()
                index.sync()
                self.assertNotIn("north", store.worldbook_hashes("g"))
            finally:
                store.close()

    def test_transient_read_error_keeps_vectors(self):
        with tempfile.TemporaryDirectory() as td:
            store = MemoryStore(Path(td) / "m.db")
            try:
                store.upsert_worldbook_vector(
                    game_id="g", entry_id="north", content_hash="h",
                    model="fake|d2",
                    vector=np.asarray([1.0, 0.0], dtype=np.float32).tobytes())
                wb = SimpleNamespace(id="g", enabled=True, path=Path(td),
                                     error="读不到", entries_snapshot=lambda: [])
                index = WorldbookIndex(store, wb, FakeEmbedder())
                index.sync()
                self.assertIn("north", store.worldbook_hashes("g"))
                self.assertIn("读不到", index.last_error or "")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
