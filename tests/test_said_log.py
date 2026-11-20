from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sidekick.output.said_log import SaidLog


class SaidLogTests(unittest.TestCase):
    def test_count_last_hour_is_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            log = SaidLog(str(Path(td) / "said.jsonl"))
            log.add("一")
            before = list(log._hour)
            log.count_last_hour()
            self.assertEqual(list(log._hour), before)
            self.assertEqual(log.count_last_hour(), 1)

    def test_append_does_not_rotate(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "said.jsonl"
            log = SaidLog(str(path))
            path.write_text("x" * 100, encoding="utf-8")
            log.add("二")
            self.assertTrue(path.exists())
            self.assertEqual(list(Path(td).glob("said-*.jsonl")), [])
            self.assertIn("二", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
