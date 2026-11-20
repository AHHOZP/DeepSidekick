from __future__ import annotations

import hashlib
import http.server
import io
import socket
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from sidekick import config
from sidekick.input.stt.engines import catalog, postprocess, registry
from sidekick.input.stt.engines.base import AsrResult
from sidekick.input.stt.engines.sherpa import SherpaEngine


class EngineFailureTests(unittest.TestCase):
    """引擎崩了不能把识别线程带走，也要有条活路和一条死路。"""

    def _engine(self):
        spec = registry.ModelSpec(id="x", kind="sense-voice", dir=Path("X:/nope"))
        return SherpaEngine(spec, {})

    def test_three_failures_enter_cooldown(self):
        eng = self._engine()
        audio = np.zeros(16000, dtype=np.float32)
        with mock.patch.object(registry, "build",
                               side_effect=RuntimeError("boom")) as build:
            for _ in range(3):
                self.assertEqual(eng.transcribe(audio, 16000).text, "")
            self.assertEqual(build.call_count, 3)
            self.assertTrue(eng.dead)
            self.assertFalse(eng.disabled)
            self.assertIsNotNone(eng.error)

            # 冷却期内：直接返回空，不再反复尝试 load
            self.assertEqual(eng.transcribe(audio, 16000).text, "")
            self.assertEqual(build.call_count, 3)
            self.assertEqual(eng.revivals, 0)
            self.assertEqual(eng.dropped_dead, 1)

    def test_permanent_disable_after_three_revivals(self):
        eng = self._engine()
        audio = np.zeros(16000, dtype=np.float32)
        now = [1000.0]
        with mock.patch(
                "sidekick.input.stt.engines.sherpa.time",
                SimpleNamespace(monotonic=lambda: now[0])):
            for _ in range(3):
                eng.transcribe(audio, 16000)
            self.assertTrue(eng.dead)

            # 复活三次，前两次失败回到冷却，第三次失败当场停用（不等第四轮）
            for n in (1, 2):
                now[0] += 31.0
                self.assertFalse(eng.dead)
                eng.transcribe(audio, 16000)
                self.assertEqual(eng.revivals, n)
                self.assertTrue(eng.dead)
                self.assertFalse(eng.disabled)

            now[0] += 31.0
            eng.transcribe(audio, 16000)
            self.assertEqual(eng.revivals, 3)
            self.assertTrue(eng.disabled)
            self.assertFalse(eng.dead)
            self.assertIn("找不到", eng.error or "")   # 保留最后一次真实异常

            # 停用后不再尝试，错误也不再变
            err = eng.error
            self.assertEqual(eng.transcribe(audio, 16000).text, "")
            self.assertEqual(eng.error, err)
            self.assertEqual(eng.revivals, 3)

    def test_decode_failure_enters_cooldown_even_if_reload_succeeds(self):
        """加载成功、解码持续失败也必须累计到冷却，不能每句话重载模型。"""
        class BadRec:
            def create_stream(self):
                return self

            def accept_waveform(self, sr, x):
                return None

            def decode_stream(self, stream):
                raise RuntimeError("decode boom")

        eng = self._engine()
        audio = np.zeros(16000, dtype=np.float32)
        with mock.patch.object(registry, "build", return_value=BadRec()) as build:
            for n in (1, 2, 3):
                eng.transcribe(audio, 16000)
                self.assertEqual(build.call_count, n)
            self.assertTrue(eng.dead)
            self.assertFalse(eng.disabled)

            # 冷却期内不再重载
            eng.transcribe(audio, 16000)
            self.assertEqual(build.call_count, 3)

    def test_short_audio_is_skipped(self):
        eng = self._engine()
        with mock.patch.object(registry, "build") as build:
            self.assertEqual(
                eng.transcribe(np.zeros(100, dtype=np.float32), 16000).text, "")
            self.assertEqual(build.call_count, 0)   # 根本没进引擎


class PostprocessTests(unittest.TestCase):
    """后处理的每条规则都要能被单独说清楚，否则调参数时会互相打架。"""

    cfg = {"stt": {"min_chars": 2, "max_chars_per_sec": 12}}

    def keep(self, text: str, duration: float) -> str:
        return postprocess.clean(AsrResult(text=text, duration=duration), self.cfg)

    def test_normal_sentence_survives(self):
        t = "这boss第二阶段怎么躲啊，我刚才又被他打死了。"
        self.assertEqual(self.keep(t, 4.7), t)

    def test_too_short(self):
        self.assertEqual(self.keep("嗯", 1.0), "")
        self.assertEqual(self.keep("", 1.0), "")

    def test_punctuation_only(self):
        self.assertEqual(self.keep("。。。。。。。。", 2.0), "")
        self.assertEqual(self.keep("没。", 1.0), "")   # 只有一个真字
        self.assertEqual(self.keep("好的好的", 1.0), "好的好的")

    def test_repeat_noise(self):
        self.assertEqual(self.keep("啊啊啊啊啊啊啊啊啊啊", 2.0), "")
        self.assertEqual(self.keep("的的的的的的", 1.5), "")
        self.assertEqual(self.keep("这个这个这个这个", 1.5), "")

    def test_real_laughter_survives(self):
        # 整句在打转才算复读，真人笑一声不算
        self.assertEqual(self.keep("哈哈哈哈哈哈哈哈", 2.0), "")
        self.assertEqual(self.keep("哈哈哈哈笑死我了", 2.0), "哈哈哈哈笑死我了")

    def test_two_repeats_is_fine(self):
        self.assertEqual(self.keep("好的好的", 1.0), "好的好的")
        self.assertEqual(self.keep("非常非常非常", 1.5), "非常非常非常")

    def test_speech_rate_cap(self):
        self.assertEqual(self.keep("一二三四五六七八九十", 0.4), "")
        self.assertEqual(self.keep("就这么办", 0.6), "就这么办")

    def test_english_words_are_not_counted_as_letters(self):
        text = "this is a normal english sentence"
        self.assertEqual(self.keep(text, 2.0), text)

    def test_whitespace_squeezed(self):
        self.assertEqual(self.keep("就这么   办", 1.0), "就这么 办")


class RegistryTests(unittest.TestCase):
    def test_load_specs_defaults(self):
        cfg = config.defaults()
        specs = registry.load_specs(cfg, Path("X:/root"))
        self.assertIn("sensevoice-small", specs)
        spec = specs["sensevoice-small"]
        self.assertEqual(spec.kind, "sense-voice")
        self.assertIn("sensevoice-small", str(spec.dir))

    def test_auto_resolve_by_role(self):
        """文件名不用写死：按角色找，int8 优先。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "small-encoder.onnx").write_bytes(b"x")
            (d / "small-encoder.int8.onnx").write_bytes(b"x")
            (d / "small-decoder.int8.onnx").write_bytes(b"x")
            (d / "small-tokens.txt").write_text("a", encoding="utf-8")
            spec = registry.ModelSpec(id="w", kind="whisper", dir=d)
            self.assertTrue(spec.path_of("encoder").endswith("small-encoder.int8.onnx"))
            self.assertTrue(spec.path_of("decoder").endswith("small-decoder.int8.onnx"))
            self.assertTrue(spec.path_of("tokens").endswith("small-tokens.txt"))
            self.assertEqual(spec.missing(), [])

    def test_auto_resolve_missing_dir(self):
        spec = registry.ModelSpec(id="x", kind="sense-voice", dir=Path("X:/nope"))
        self.assertEqual(spec.path_of("model"), "")
        self.assertEqual(len(spec.missing()), 2)

    def test_unknown_kind_is_caught_at_build(self):
        cfg = {"stt": {
            "model_dir": "state/asr",
            "models": {"x": {"kind": "no-such-kind"}},
        }}
        spec = registry.load_specs(cfg, Path("."))["x"]
        with self.assertRaises(ValueError):
            registry.build(spec, {})

    def test_missing_files_reported(self):
        cfg = {"stt": {
            "model_dir": "state/asr",
            "models": {"x": {"kind": "sense-voice",
                             "args": {"model": "m.onnx", "tokens": "t.txt"}}},
        }}
        spec = registry.load_specs(cfg, Path("X:/definitely/not/here"))["x"]
        self.assertEqual(len(spec.missing()), 2)

    def test_build_is_cpu_only(self):
        """设备不是可选项：配置里写了 device，也只按 CPU 建。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "model.int8.onnx").write_bytes(b"x")
            (d / "tokens.txt").write_text("a", encoding="utf-8")
            spec = registry.ModelSpec(id="x", kind="sense-voice", dir=d)
            with mock.patch(
                    "sherpa_onnx.OfflineRecognizer.from_sense_voice") as build:
                registry.build(spec, {"device": "cuda", "num_threads": 2})
            self.assertEqual(build.call_args.kwargs["provider"], "cpu")
            self.assertEqual(build.call_args.kwargs["num_threads"], 2)


class CatalogHashTests(unittest.TestCase):
    def test_all_download_entries_have_sha256(self):
        for entry in catalog.all_entries():
            self.assertRegex(entry.sha256, r"^[0-9a-f]{64}$", entry.id)

    def test_whisper_large_v3_is_not_offered(self):
        ids = {entry.id for entry in catalog.all_entries()}
        self.assertNotIn("whisper-large-v3", ids)
        self.assertIn("whisper-turbo", ids)
        self.assertIn("whisper-small", ids)

    def test_peak_memory_has_no_approx_marker(self):
        for entry in catalog.all_entries():
            self.assertNotIn("≈", entry.mem, entry.id)


class DownloadTests(unittest.TestCase):
    """WebUI 的"下载 tar.bz2 → 解压 → 校验"必须扛得住发布包的目录结构。"""

    def test_fetch_and_extract_strips_wrapper(self):
        from sidekick.input.stt.engines.download import fetch_and_extract

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            arc = root / "fake.tar.bz2"
            # 官方包的样子：一层同名目录 + 自带的 test_wavs
            with tarfile.open(arc, "w:bz2") as tar:
                for name, data in [
                    ("pkg-2024/model.int8.onnx", b"x" * 2048),
                    ("pkg-2024/tokens.txt", b"a 1\nb 2\n"),
                    ("pkg-2024/test_wavs/1.wav", b"nope" * 64),
                ]:
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))

            class Handler(http.server.SimpleHTTPRequestHandler):
                def __init__(self, *a, **kw):
                    super().__init__(*a, directory=str(root), **kw)

                def log_message(self, *a):
                    pass

            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            time.sleep(0.2)

            class Stub:
                id = "sensevoice-small"
                file = "fake.tar.bz2"
                sha256 = hashlib.sha256(arc.read_bytes()).hexdigest()

                def url(self, mirror=False):
                    return f"http://127.0.0.1:{port}/fake.tar.bz2"

            dest = root / "out" / "sensevoice-small"
            job = {"stage": "", "bytes": 0, "total": 0}
            try:
                fetch_and_extract(Stub(), dest, False, job)
            finally:
                srv.shutdown()
                srv.server_close()

            self.assertTrue((dest / "model.int8.onnx").is_file())
            self.assertTrue((dest / "tokens.txt").is_file())
            self.assertFalse((dest / "test_wavs").exists())
            self.assertFalse((root / "out" / ".download" / "fake.tar.bz2").exists())

    def test_fetch_and_extract_rejects_bad_sha256(self):
        from sidekick.input.stt.engines.download import fetch_and_extract

        data = b"x" * 128

        class Resp(io.BytesIO):
            headers = {"Content-Length": str(len(data))}

        class Stub:
            id = "sensevoice-small"
            file = "fake.tar.bz2"
            bytes = len(data)
            sha256 = "0" * 64

            def url(self, mirror=False):
                return "http://example.invalid/fake.tar.bz2"

        with tempfile.TemporaryDirectory() as td, mock.patch(
                "sidekick.input.stt.engines.download.http_open",
                return_value=Resp(data)):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_and_extract(Stub(), Path(td) / "dest", False, {})
            self.assertIn("SHA256", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
