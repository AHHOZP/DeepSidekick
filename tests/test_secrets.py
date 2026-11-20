from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from sidekick.core import atomic
from sidekick.config import secrets


class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(secrets, "ROOT", Path(self.tmp.name))
        self._patch.start()
        secrets._warned = False

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_upsert_only_changes_target_id(self):
        secrets.seal({"a": "1", "b": "2"})
        secrets.upsert("a", "3")
        self.assertEqual(secrets.get("a"), "3")
        self.assertEqual(secrets.get("b"), "2")
        secrets.upsert("a", "4")
        self.assertEqual(secrets.get("b"), "2")

    def test_concurrent_upserts_keep_both_values(self):
        secrets.seal({"a": "old-a", "b": "old-b"})

        def work(name, value):
            for _ in range(8):
                secrets.upsert(name, value)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda x: work(*x), [("a", "new-a"), ("b", "new-b")]))
        loaded = secrets.load()
        self.assertEqual(loaded["a"], "new-a")
        self.assertEqual(loaded["b"], "new-b")

    def test_delete_and_prefix_do_not_touch_others(self):
        secrets.seal({"a": "1", "a.headers.x": "h", "b": "2"})
        secrets.delete("a")
        self.assertEqual(secrets.load(), {"a.headers.x": "h", "b": "2"})
        secrets.delete_prefix("a")
        self.assertEqual(secrets.load(), {"b": "2"})

    def test_failed_seal_keeps_previous_file(self):
        secrets.seal({"a": "old"})
        original = secrets._dpapi

        def fail_on_protect(data, protect):
            if protect:
                raise OSError("boom")
            return original(data, protect)

        with mock.patch.object(secrets, "_dpapi", side_effect=fail_on_protect):
            with self.assertRaises(OSError):
                secrets.upsert("a", "new")
        self.assertEqual(secrets.get("a"), "old")

    def test_failed_replace_keeps_previous_file(self):
        secrets.seal({"a": "old"})
        with mock.patch.object(atomic.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                secrets.upsert("a", "new")
        self.assertEqual(secrets.get("a"), "old")

    def test_status_and_fill_only_nested_format(self):
        secrets.seal({"llm.foo.api_key": "K", "sessdata": "C"})
        cfg = {
            "llm": {"foo": {"api_key": secrets.SEALED}},
            "tts": {"mimo": {"api_key": ""}},
            "sessdata": secrets.SEALED,
        }
        st = secrets.status(cfg)
        self.assertTrue(st["llm.foo.api_key"])
        self.assertFalse(st["tts.mimo.api_key"])
        self.assertTrue(st["sessdata"])
        secrets.fill(cfg)
        self.assertEqual(cfg["llm"]["foo"]["api_key"], "K")
        self.assertEqual(cfg["sessdata"], "C")

    def test_secret_paths_come_from_one_declaration(self):
        self.assertTrue(secrets.is_secret_path("llm.foo.api_key"))
        self.assertTrue(secrets.is_secret_path("tts.mimo.headers.Authorization"))
        self.assertTrue(secrets.is_secret_path("sessdata"))
        self.assertFalse(secrets.is_secret_path("llm.foo.headers.X-Custom"))
        self.assertTrue(secrets.is_secret_path("llm.foo.headers.X-Token"))
        self.assertFalse(secrets.is_secret_path("persona.bot_name"))
        self.assertTrue(secrets.is_secret_path("persona.api_key"))
        slot = secrets.parse_slot("llm.foo.headers.Authorization")
        self.assertEqual(slot.name, "llm.foo.headers.Authorization")

    def test_redact_hides_values_without_changing_shape(self):
        cfg = {
            "llm": {"foo": {"api_key": "K", "headers": {"Authorization": "Bearer K"}}},
            "tts": {"mimo": {"api_key": "M"}},
            "sessdata": "C",
        }
        secrets.redact(cfg)
        self.assertEqual(cfg["llm"]["foo"]["api_key"], secrets.SEALED)
        self.assertEqual(cfg["llm"]["foo"]["headers"]["Authorization"],
                         secrets.SEALED)
        self.assertEqual(cfg["tts"]["mimo"]["api_key"], secrets.SEALED)
        self.assertEqual(cfg["sessdata"], secrets.SEALED)

    def test_plaintext_secret_is_ignored(self):
        cfg = {
            "llm": {"foo": {"api_key": "sk-plain"}},
            "tts": {"mimo": {"api_key": "",
                             "headers": {"Authorization": "Bearer x"}}},
            "sessdata": "PLAIN",
        }
        secrets.fill(cfg)
        self.assertEqual(cfg["llm"]["foo"]["api_key"], "")
        self.assertEqual(cfg["tts"]["mimo"]["headers"]["Authorization"], "")
        self.assertEqual(cfg["sessdata"], "")
        st = secrets.status({"llm": {"foo": {"api_key": "sk-plain"}}})
        self.assertFalse(st["llm.foo.api_key"])


if __name__ == "__main__":
    unittest.main()
