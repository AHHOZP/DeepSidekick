from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidekick import config
from sidekick.app.runtime import build_runtime
from sidekick.input.stt import make as make_stt


class FakeWorker:
    def __init__(self, stt, events, log):
        self.stt = stt
        self.on_segment = self._on_segment
        self.closed = False

    def _on_segment(self, *_a, **_k):
        return None

    def start(self):
        return None

    def close(self):
        self.closed = True
        return True

    def status(self):
        return {"dead": False, "disabled": False, "error": None}


class SttCompositionTests(unittest.TestCase):
    def test_enabled_stt_warmup_uses_new_engine_package(self):
        cfg = config.defaults()
        with tempfile.TemporaryDirectory() as td, mock.patch(
                "sidekick.input.stt.engines.sherpa.SherpaEngine.warmup",
                return_value=True):
            root = Path(td)
            stt = make_stt(cfg, root=root)
            self.assertTrue(stt.warmup(), stt.error)
            self.assertEqual(stt.name, "sherpa/sense-voice")

            worker = FakeWorker(stt, None, None)
            cfg["memory"]["enabled"] = False
            cfg["capture"]["enabled"] = False
            cfg["mic"]["enabled"] = False
            cfg["tts"]["enabled"] = False
            cfg["stage"]["enabled"] = False
            cfg["game"]["enabled"] = False
            with mock.patch("sidekick.app.runtime.make_stt", return_value=stt), \
                    mock.patch("sidekick.app.runtime.SttWorker", return_value=worker):
                rt = build_runtime(cfg, root=root)
            try:
                self.assertIs(rt.stt, stt)
                self.assertIs(rt.stt_worker, worker)
                self.assertIs(rt.mic.on_segment, worker.on_segment)
            finally:
                stt.close()


if __name__ == "__main__":
    unittest.main()
