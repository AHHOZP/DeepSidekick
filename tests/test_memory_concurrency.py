from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sidekick.memory.store import MemoryStore
from sidekick.core.text import json_list


class GameStateConcurrencyTests(unittest.TestCase):
    def test_two_connections_do_not_lose_each_others_fields(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "memory.db"
            a = MemoryStore(db)
            b = MemoryStore(db)
            try:
                a.upsert_state("g", known_facts=["旧事实"])
                with ThreadPoolExecutor(max_workers=2) as pool:
                    f1 = pool.submit(a.upsert_state, "g",
                                     known_facts=["旧事实", "新事实"])
                    f2 = pool.submit(b.upsert_state, "g",
                                     observations=["铁匠怕火"])
                    f1.result(timeout=10)
                    f2.result(timeout=10)
                st = a.get_state("g")
                self.assertEqual(json_list(st["known_facts"]),
                                 ["旧事实", "新事实"])
                self.assertEqual(json_list(st["observations"]), ["铁匠怕火"])
            finally:
                a.conn.close()
                b.conn.close()


if __name__ == "__main__":
    unittest.main()
