from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from sidekick import config
from sidekick.memory.embed import download
from sidekick.memory.embed.catalog import Entry, FileSpec, all_entries
from sidekick.memory.embed.worker import EmbeddingService
from sidekick.memory.long_term import Memory
from sidekick.memory.store import MemoryStore
from sidekick.core.null import NullEmbedder


class FakeEncoder:
    def __init__(self, delay=0.0):
        self.delay = delay
        self.dim = 2
        self.loaded = False

    def load(self):
        self.loaded = True

    def identity(self):
        return "fake|d2"

    def query(self, text):
        if self.delay:
            time.sleep(self.delay)
        return np.asarray([1.0, 0.0], dtype=np.float32)

    def documents(self, texts):
        return [np.asarray([0.0, 1.0], dtype=np.float32) for _ in texts]


def _cfg(root: Path) -> dict:
    cfg = config.defaults()
    cfg["memory"].update({
        "enabled": True, "db_path": str(root / "m.db"),
        "embed_enabled": True, "embed_model": "fake",
        "embed_dir": str(root / "embed"), "embed_threads": 1,
        "embed_timeout_ms": 250,
    })
    return cfg


class EmbedServiceTests(QuietIsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _cfg(self.root)
        self.entry = Entry(id="fake", model_name="fake", name="fake",
                           files=(), hf_repo="fake/repo", ms_repo="fake/repo",
                           dim=2, max_context=8192, template="fake")

    def tearDown(self):
        self.tmp.cleanup()

    async def _ready(self, service):
        service.start()
        for _ in range(100):
            if service.ready or service.state == "unavailable":
                return
            await asyncio.sleep(0.01)

    async def test_load_and_query(self):
        service = EmbeddingService(self.cfg, self.root, log=lambda *a: None,
                                   entry=self.entry, encoder=FakeEncoder())
        try:
            await self._ready(service)
            self.assertEqual(service.state, "ready")
            vec = await service.query("你好")
            self.assertIsNotNone(vec)
            self.assertAlmostEqual(float(np.linalg.norm(vec)), 1.0)
        finally:
            service.close()

    async def test_query_timeout_degrades(self):
        service = EmbeddingService(self.cfg, self.root, log=lambda *a: None,
                                   entry=self.entry,
                                   encoder=FakeEncoder(delay=0.2))
        try:
            await self._ready(service)
            vec = await service.query("你好", timeout_ms=20)
            self.assertIsNone(vec)
            self.assertEqual(service.last_degraded, "timeout")
        finally:
            service.close()

    async def test_docs_callback_receives_vectors(self):
        service = EmbeddingService(self.cfg, self.root, log=lambda *a: None,
                                   entry=self.entry, encoder=FakeEncoder())
        got = []
        try:
            await self._ready(service)
            fut = service.submit_docs(["a", "b"], got.append)
            self.assertIsNotNone(fut)
            for _ in range(200):
                if got:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(got[0]), 2)
        finally:
            service.close()

    def test_null_embedder_is_disabled(self):
        null = NullEmbedder()
        self.assertFalse(null.ready)
        self.assertEqual(null.stats()["state"], "disabled")
        self.assertIsNone(asyncio.run(null.query("x")))


class RecordingEmbedder:
    enabled = True
    ready = True
    state = "ready"
    identity = "fake|d2"
    model = "fake"
    error = None
    last_degraded = None

    def __init__(self):
        self.texts = []

    async def query(self, text, timeout_ms=None):
        return None

    def prefetch(self, text):
        return None

    def submit_docs(self, texts, callback=None):
        self.texts.extend(texts)
        if callback:
            callback([np.asarray([1.0, 0.0], dtype=np.float32)
                      for _ in texts])
        return None

    def stats(self):
        return {}


class CatalogTests(unittest.TestCase):
    def test_curated_models_have_templates_and_sizes(self):
        ids = {e.id for e in all_entries()}
        self.assertIn("qwen3-embedding-0.6b-q", ids)
        self.assertIn("jina-embeddings-v3", ids)
        self.assertIn("bge-m3", ids)
        self.assertNotIn("nomic-embed-text-v1.5-q", ids)
        for e in all_entries():
            self.assertTrue(e.hf_repo and e.ms_repo)
            self.assertGreater(e.dim, 0)
            self.assertGreater(e.bytes, 0)
            self.assertTrue(e.template)
            self.assertNotIn("≈", e.mem)
            self.assertNotIn("≈", e.latency)
            for score in (e.cmteb, e.mteb, e.mmteb):
                self.assertNotIn("(", score)
                self.assertNotIn("（", score)
            for f in e.files:
                self.assertGreater(f.bytes, 0)


class DownloadFallbackTests(unittest.TestCase):
    def test_hf_twice_then_modelscope(self):
        entry = Entry(
            id="x", model_name="x", name="x",
            files=(FileSpec("a.bin", 3),),
            hf_repo="fake/hf", ms_repo="fake/ms",
            dim=2, max_context=8192, template="x")
        calls = []

        def fake_fetch(url, dest, **kwargs):
            calls.append(url)
            if "huggingface" in url:
                raise RuntimeError("hf down")
            Path(dest).write_bytes(b"abc")
            return 3

        with tempfile.TemporaryDirectory() as td:
            job = {}
            with mock.patch("sidekick.memory.embed.download.fetch_file",
                            side_effect=fake_fetch):
                download.install(entry, Path(td) / "model", job)
            self.assertEqual(len([x for x in calls if "huggingface" in x]), 2)
            self.assertEqual(len([x for x in calls if "modelscope" in x]), 1)
            self.assertEqual(job["source"], "ModelScope")
            self.assertTrue((Path(td) / "model" / "a.bin").is_file())


class MemoryIndexTests(unittest.TestCase):
    def test_index_pending_writes_vectors(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = config.defaults()
            cfg["memory"].update({"enabled": True,
                                  "db_path": str(root / "m.db")})
            cfg["game"].update({"enabled": True, "id": "g"})
            store = MemoryStore(root / "m.db")
            try:
                row = store.add_memory(scope="live", kind="summary",
                                       content="要被编码的总结。")
                embedder = RecordingEmbedder()
                memory = Memory(cfg, root, store=store, embedder=embedder)
                memory.index_pending()
                self.assertTrue(embedder.texts)
                got = store.get_memory(row["id"])
                self.assertTrue(got["has_vector"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
