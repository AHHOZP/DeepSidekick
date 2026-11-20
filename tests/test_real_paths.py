from __future__ import annotations

import unittest
from pathlib import Path

from sidekick import config
from sidekick.config import secrets
from sidekick.config.spec import DEFAULTS
from sidekick.input import bili
from sidekick.webui.routes import page

ROOT = Path(__file__).resolve().parents[1]


class RealPathTests(unittest.TestCase):
    def test_roots_and_vendor_are_repo_root_and_importable(self):
        self.assertEqual(config.ROOT, ROOT)
        self.assertEqual(config.CONFIG_PATH, ROOT / "config.yaml")
        self.assertEqual(secrets.path(), ROOT / "state" / "secrets.bin")
        self.assertEqual(page.STATIC, ROOT / "sidekick" / "webui" / "static")
        self.assertTrue((page.STATIC / "index.html").is_file())
        self.assertTrue((ROOT / "overlay" / "index.html").is_file())
        self.assertEqual(bili._VENDOR, ROOT / "vendor")
        self.assertTrue((bili._VENDOR / "blivedm").is_dir())

        import blivedm
        self.assertTrue(Path(blivedm.__file__).resolve().is_relative_to(
            bili._VENDOR))

        # 运行态固定落在仓库根目录的 state/，不跟着代码包走。
        self.assertEqual(config.path(DEFAULTS["memory"]["db_path"]),
                         ROOT / "state" / "deepsidekick.db")


if __name__ == "__main__":
    unittest.main()
