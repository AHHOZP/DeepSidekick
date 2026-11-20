from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from sidekick import config
from sidekick.config import secrets


class ConfigCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg_path = self.root / "config.yaml"
        self.patches = [
            mock.patch.object(config, "CONFIG_PATH", self.cfg_path),
            mock.patch.object(secrets, "ROOT", self.root),
        ]
        for p in self.patches:
            p.start()
        config._load_cache = None
        config.last_error = None
        secrets.last_error = None
        self.cfg_path.write_text(yaml.safe_dump(
            {"webui": {"port": 17880}}), encoding="utf-8")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        config._load_cache = None
        config.last_error = None
        secrets.last_error = None
        self.tmp.cleanup()

    def test_load_is_cached_but_returns_independent_copy(self):
        with mock.patch.object(config, "_read_yaml",
                               wraps=config._read_yaml) as spy:
            first = config.load()
            second = config.load()
        self.assertEqual(spy.call_count, 1)
        first["webui"]["port"] = 1
        self.assertEqual(second["webui"]["port"], 17880)
        self.assertEqual(config.load()["webui"]["port"], 17880)

    def test_bad_yaml_is_reported_and_survives_cache_hit(self):
        config._load_cache = None
        self.cfg_path.write_text(": not yaml: [", encoding="utf-8")
        config.load()
        self.assertIsNotNone(config.last_error)
        # 文件没变时走缓存；缓存命中不能把错误洗成“没问题”。
        config.load()
        self.assertIsNotNone(config.last_error)


if __name__ == "__main__":
    unittest.main()
