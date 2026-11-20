from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LauncherScriptTests(unittest.TestCase):
    def test_run_bat_is_the_only_root_launcher(self):
        bat = ROOT / "run.bat"
        self.assertTrue(bat.is_file())
        self.assertFalse((ROOT / "run.ps1").exists())

        raw = bat.read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        text = raw.decode("utf-8")
        self.assertIn("chcp 65001", text)
        self.assertIn('cd /d "%~dp0"', text)
        self.assertIn(".venv\\Scripts\\python.exe", text)
        self.assertIn("-m sidekick.webui", text)
        self.assertIn("pause", text)

    def test_entry_docs_use_run_bat(self):
        for rel in ("README.md", "docs/DeepSidekick.md"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("run.bat", text, rel)
            self.assertNotIn("run.ps1", text, rel)

    def test_bat_keeps_crlf_for_cmd(self):
        attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.bat text eol=crlf", attrs)


if __name__ == "__main__":
    unittest.main()
