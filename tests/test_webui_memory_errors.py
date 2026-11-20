from __future__ import annotations

import contextlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

from sidekick import config
from webui_fixture import webui


class MemoryOpenFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.cfg_path = self.root / "config.yaml"
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch("sidekick.webui.app.log"))
        self.ui = self.stack.enter_context(webui(self.root))
        config._load_cache = None

    def tearDown(self):
        self.stack.close()
        config._load_cache = None
        self.tmp.cleanup()

    def _write_db_path(self, db_path: str) -> None:
        self.cfg_path.write_text(yaml.safe_dump({
            "memory": {"enabled": True, "db_path": db_path},
            "game": {"enabled": False},
        }, allow_unicode=True), encoding="utf-8")
        time.sleep(0.01)
        config._load_cache = None

    def test_bad_path_drops_old_service_and_reports_error(self):
        self._write_db_path("state/deepsidekick.db")
        old = self.ui.get_memory()
        self.assertIsNotNone(old)
        self.assertNotIn("error", self.ui.memory_stats())

        self._write_db_path("state")
        self.assertIsNone(self.ui.get_memory(force=True))
        self.assertIn("OperationalError", self.ui.memory_stats()["error"])

    def test_force_same_config_keeps_existing_service(self):
        self._write_db_path("state/deepsidekick.db")
        old = self.ui.get_memory()
        self.assertIsNotNone(old)
        self.assertIs(old, self.ui.get_memory(force=True))

    def test_bad_path_without_old_service_returns_none(self):
        self._write_db_path("state")
        self.assertIsNone(self.ui.get_memory())
        self.assertIn("error", self.ui.memory_stats())

    def test_fixed_path_recovers_and_clears_error(self):
        self._write_db_path("state")
        self.assertIsNone(self.ui.get_memory())
        self._write_db_path("state/deepsidekick.db")
        fixed = self.ui.get_memory(force=True)
        self.assertIsNotNone(fixed)
        self.assertIsNone(self.ui._memory_error)
        self.assertNotIn("error", self.ui.memory_stats())


if __name__ == "__main__":
    unittest.main()
