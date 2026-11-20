from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidekick import config
from sidekick.config import secrets


def leaf_paths(obj: dict, prefix: str = "") -> set[str]:
    out: set[str] = set()
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out |= leaf_paths(value, path)
        else:
            out.add(path)
    return out


class ConfigLoadTests(unittest.TestCase):
    def test_generated_default_config_roundtrips_through_load(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_path = root / "config.yaml"
            with mock.patch.object(config, "CONFIG_PATH", cfg_path), \
                    mock.patch.object(secrets, "ROOT", root):
                config._load_cache = None
                config.last_error = None
                secrets._warned = False
                created, error = config.ensure_default_config()
                self.assertTrue(created, error)
                cfg = config.load()
                self.assertIsNone(config.last_error)
                self.assertEqual(config.validate(cfg), [])
                self.assertEqual(leaf_paths(cfg), leaf_paths(config.defaults()))
                self.assertIn(cfg["llm"]["source"], cfg["llm"])
                self.assertTrue(all(not v for v in secrets.status(cfg).values()))
                config.ensure_dirs(cfg, root)
                self.assertTrue((root / "state").is_dir())
                config._load_cache = None
                config.last_error = None


if __name__ == "__main__":
    unittest.main()
