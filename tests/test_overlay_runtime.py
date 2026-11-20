"""Overlay 运行时 smoke：用 Node 真正执行 app.js，而不只做源码字符串断言。"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "未安装 Node.js，跳过前端运行时 smoke")
class OverlayRuntimeTests(unittest.TestCase):
    def test_overlay_boots_without_browser_globals(self):
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "tests" / "overlay_smoke.js")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30)
        self.assertEqual(
            0, result.returncode,
            (result.stdout or "") + (result.stderr or ""))
        self.assertIn("形象不可用", result.stderr)


if __name__ == "__main__":
    unittest.main()
