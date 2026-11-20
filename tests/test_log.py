from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

from sidekick.core import log as logmod


class CrashHookTests(unittest.TestCase):
    def test_configure_accepts_custom_filename(self):
        with tempfile.TemporaryDirectory() as td:
            old_path, old_file = logmod._PATH, logmod._FILE
            try:
                logmod.configure(Path(td), "webui.log")
                self.assertEqual(Path(td) / "state" / "webui.log",
                                 logmod._PATH)
            finally:
                logmod._PATH, logmod._FILE = old_path, old_file

    def test_hook_writes_run_log_and_stderr(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old_path, old_file = logmod._PATH, logmod._FILE
            logmod._PATH = root / "state" / "run.log"
            logmod._FILE = None
            try:
                try:
                    raise ValueError("boom")
                except ValueError:
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        logmod._hook(*sys.exc_info())
                text = (root / "state" / "run.log").read_text(encoding="utf-8")
                self.assertIn("ValueError: boom", text)
                self.assertIn("ValueError: boom", err.getvalue())
            finally:
                logmod._PATH, logmod._FILE = old_path, old_file


if __name__ == "__main__":
    unittest.main()
