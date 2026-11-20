from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from pathlib import Path

from identity_fixture import runtime_cfg
from sidekick import config
from sidekick.app.runtime import build_runtime
from sidekick.config.spec import CAPTURE_MODE_PRESETS, resolve_capture_config
from sidekick.input.capture import Frame, FrameQueue


class CaptureModePresetTests(unittest.TestCase):
    def test_default_is_active_and_presets_are_data(self):
        self.assertEqual("active", config.defaults()["capture"]["mode"])
        self.assertEqual({"active", "calm"}, set(CAPTURE_MODE_PRESETS))

    def test_mode_preset_overrides_local_fields(self):
        for mode, preset in CAPTURE_MODE_PRESETS.items():
            cfg = config.defaults()
            cfg["capture"]["mode"] = mode
            cfg["capture"]["resolution"] = "960x540"
            got = resolve_capture_config(cfg)
            self.assertEqual(preset["interval_s"], got["capture"]["interval_s"])
            self.assertEqual(preset["max_frames"], got["capture"]["max_frames"])
            self.assertEqual(preset["resolution"], got["capture"]["resolution"])
            self.assertEqual("960x540", cfg["capture"]["resolution"])


class CaptureReloadTests(unittest.TestCase):
    def _queue(self, mode="active", resolution="1920x1080") -> FrameQueue:
        cfg = config.defaults()
        cfg["capture"].update({"enabled": True, "mode": mode,
                               "interval_s": 5, "max_frames": 28,
                               "resolution": resolution})
        return FrameQueue(cfg)

    def test_reload_applies_interval_and_keeps_queue_when_resolution_same(self):
        q = self._queue()
        old_cap = q.cap
        q._q.append(Frame(1.0, "thumb", b"sig"))
        cfg = config.defaults()
        cfg["capture"]["mode"] = "calm"
        q.reload(resolve_capture_config(cfg))
        self.assertIs(old_cap, q.cap)
        self.assertEqual(5, q.interval)
        self.assertEqual(1, q.peek())
        self.assertTrue(q._reload_event.is_set())

        q._apply_reload()
        self.assertFalse(q._reload_event.is_set())
        self.assertEqual("calm", q.mode)
        self.assertEqual(6.0, q.interval)
        self.assertEqual(6, q.max_frames)
        self.assertEqual(1920, q.cap.width)
        self.assertEqual(1, q.peek())

    def test_interval_only_reload_keeps_pending_frames(self):
        q = self._queue()
        q._q.append(Frame(1.0, "thumb", b"sig"))
        cfg = config.defaults()
        cfg["capture"]["mode"] = "calm"
        q.reload(resolve_capture_config(cfg))
        q._apply_reload()
        self.assertEqual(6.0, q.interval)
        self.assertEqual(1, q.peek())
        self.assertEqual("calm", q.stats()["mode"])


class RuntimeModeSwitchTests(unittest.TestCase):
    def test_active_to_calm_reloads_interval_and_density(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = runtime_cfg(td)
            for key in ("memory", "mic", "stt", "tts", "stage", "game"):
                cfg[key]["enabled"] = False
            cfg["capture"].update({"enabled": True, "mode": "active"})
            rt = build_runtime(cfg, root=Path(td))
            try:
                self.assertEqual(1.0, rt.frames.interval)
                self.assertEqual(28, rt.frames.max_frames)


                newer = copy.deepcopy(cfg)
                newer["capture"]["mode"] = "calm"
                rt._reload(newer)
                self.assertEqual("calm", rt.cfg["capture"]["mode"])

                self.assertTrue(rt.frames._reload_event.is_set())
                rt.frames._q.append(Frame(1.0, "thumb", b"sig"))
                rt.frames._apply_reload()
                self.assertEqual(6.0, rt.frames.interval)
                self.assertEqual(6, rt.frames.max_frames)
                self.assertEqual(1920, rt.frames.cap.width)
                self.assertEqual(1, rt.frames.peek())
            finally:
                asyncio.run(rt.shutdown())


if __name__ == "__main__":
    unittest.main()
