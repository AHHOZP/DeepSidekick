from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from identity_fixture import runtime_cfg
from sidekick import config
from sidekick.brain.material import build_round
from sidekick.core.events import DANMU, Event, EventBus
from sidekick.input.capture import Frame, FrameQueue
from sidekick.input.window_context import (
    PRIVACY_NOTE,
    NullWindowContext,
    WindowContext,
    WindowSnapshot,
)
from sidekick.output.said_log import SaidLog
from sidekick.scheduling.mode import ModeMachine
from sidekick.scheduling.scheduler import Scheduler


def _cfg(**capture) -> dict:
    cfg = config.defaults()
    cfg["capture"].update({"enabled": True, "interval_s": 1.0,
                           "max_frames": 6, "resolution": "1280x720"})
    cfg["capture"].update(capture)
    return cfg


class WindowContextTests(unittest.TestCase):
    def test_privacy_hit_takes_precedence_over_title(self):
        ctx = WindowContext(
            _cfg(privacy_processes=["keepassxc.exe"]),
            probe=lambda: WindowSnapshot("工资表.xlsx", "KeePassXC.exe"))
        snap = ctx.sample()
        self.assertEqual("KeePassXC.exe", snap.process)
        self.assertTrue(ctx.privacy_active)
        self.assertEqual(("", PRIVACY_NOTE), ctx.material_lines())
        self.assertTrue(ctx.stats()["privacy_active"])

    def test_title_is_sent_automatically_and_truncated(self):
        title = "《CLANNAD》全流程 - bilibili " + "x" * 80
        ctx = WindowContext(
            _cfg(privacy_processes=[]),
            probe=lambda: WindowSnapshot(title, "chrome.exe"))
        ctx.sample()
        line, note = ctx.material_lines()
        self.assertEqual("", note)
        self.assertTrue(line.startswith("[前台窗口] 《CLANNAD》"))
        self.assertIn("(chrome.exe)", line)
        self.assertNotIn("x" * 41, line)
        self.assertEqual(40, len(line.split("] ", 1)[1].split(" (", 1)[0]))

    def test_browser_is_not_private_unless_listed(self):
        ctx = WindowContext(
            _cfg(privacy_processes=["keepass.exe"]),
            probe=lambda: WindowSnapshot("视频", "msedge.exe"))
        ctx.sample()
        self.assertFalse(ctx.privacy_active)
        self.assertNotEqual("", ctx.material_lines()[0])

    def test_probe_error_keeps_previous_state(self):
        private = WindowContext(
            _cfg(privacy_processes=["keepass.exe"]),
            probe=lambda: WindowSnapshot("", "keepass.exe"))
        private.sample()
        private._probe = mock.Mock(side_effect=OSError("no window"))
        self.assertIsNone(private.sample())
        self.assertTrue(private.privacy_active)
        self.assertIn("no window", private.stats()["error"])

    def test_missing_foreground_clears_stale_title_but_keeps_privacy(self):
        ctx = WindowContext(
            _cfg(privacy_processes=[]),
            probe=lambda: WindowSnapshot("旧标题", "chrome.exe"))
        ctx.sample()
        self.assertIn("旧标题", ctx.material_lines()[0])
        ctx._probe = lambda: None
        ctx.sample()
        self.assertEqual(("", ""), ctx.material_lines())

        private = WindowContext(
            _cfg(privacy_processes=["keepass.exe"]),
            probe=lambda: WindowSnapshot("密码库", "keepass.exe"))
        private.sample()
        private._probe = lambda: None
        private.sample()
        self.assertTrue(private.privacy_active)
        self.assertEqual(("", PRIVACY_NOTE), private.material_lines())

    def test_null_context_has_same_contract(self):
        ctx = NullWindowContext()
        self.assertIsNone(ctx.sample())
        self.assertEqual(("", ""), ctx.material_lines())
        self.assertFalse(ctx.privacy_active)


class PrivacyCaptureTests(unittest.TestCase):
    def test_privacy_hit_clears_queue_and_skips_grab(self):
        ctx = WindowContext(
            _cfg(privacy_processes=["keepass.exe"]),
            probe=lambda: WindowSnapshot("密码库", "keepass.exe"))
        q = FrameQueue(_cfg(), window_context=ctx)
        q._q.append(Frame(1.0, b"thumb", b"sig"))
        with mock.patch.object(q.cap, "_grab_array",
                               side_effect=AssertionError("不应抓屏")):
            q._tick()
        self.assertTrue(q.privacy_active)
        self.assertEqual(0, q.peek())
        self.assertEqual(0.0, q.latest_ts())
        st = q.stats()
        self.assertTrue(st["privacy_active"])
        self.assertIsNone(st["error"])

    def test_browser_continues_normal_capture(self):
        ctx = WindowContext(
            _cfg(privacy_processes=["keepass.exe"]),
            probe=lambda: WindowSnapshot("B站", "chrome.exe"))
        q = FrameQueue(_cfg(), window_context=ctx)
        with mock.patch.object(q.cap, "_grab_array",
                               return_value=Image.new("RGB", (640, 480))):
            q._tick()
        self.assertFalse(q.privacy_active)
        self.assertEqual(1, q.peek())

    def test_reload_updates_privacy_list(self):
        ctx = WindowContext(
            _cfg(privacy_processes=[]),
            probe=lambda: WindowSnapshot("标题", "chrome.exe"))
        ctx.sample()
        self.assertIn("标题", ctx.material_lines()[0])
        cfg = _cfg(privacy_processes=["chrome.exe"])
        ctx.reload(cfg)
        ctx.sample()
        self.assertTrue(ctx.privacy_active)
        self.assertEqual(("", PRIVACY_NOTE), ctx.material_lines())

    def test_capture_now_reports_false_while_privacy_active(self):
        q = FrameQueue(_cfg())
        q._th = mock.Mock(is_alive=lambda: True)
        q.enabled = True
        q.privacy_active = True
        self.assertFalse(q.capture_now(0.01))


class SchedulerPrivacyTests(unittest.TestCase):
    def test_round_drops_frames_if_privacy_flips_after_take(self):
        class PrivateWindow:
            privacy_active = False

            def material_lines(self):
                return "", PRIVACY_NOTE

            def reload(self, cfg):
                return None

        class Clock:
            last_request_at = 0.0
            busy = False

        with tempfile.TemporaryDirectory() as td:
            cfg = runtime_cfg(td)
            cfg["scheduler"].update({"min_gap_s": 0, "batch_events": 1})
            cfg["capture"]["interval_s"] = 0
            bus = EventBus(20)
            bus.push(Event(kind=DANMU, name="观众", text="在吗"))
            said = SaidLog(str(Path(td) / "said.jsonl"))
            said.start()
            taken: list[int] = []

            def take_frames():
                taken.append(1)
                return [Frame(1.0, b"thumb", b"sig")]

            sched = Scheduler(
                cfg, bus, said, Clock(), ModeMachine(),
                take_frames=take_frames, window=PrivateWindow())
            snap = sched.take_round()
            self.assertIsNotNone(snap)
            self.assertEqual(1, len(taken))
            self.assertEqual([], snap["frames"])
            self.assertEqual(0, snap["pack"].source_count)
            self.assertIn(PRIVACY_NOTE, snap["text"])


class WindowMaterialTests(unittest.TestCase):
    def _material(self, **kw) -> str:
        with tempfile.TemporaryDirectory() as td:
            said = SaidLog(str(Path(td) / "said.jsonl"))
            text, _ = build_round(
                EventBus(20), said, "normal", seconds=1, frame_ts=[],
                name="阿明", reason="冷场", live=False, **kw)
            return text

    def test_privacy_note_replaces_frame_line(self):
        text = self._material(privacy_note=PRIVACY_NOTE)
        self.assertIn(f"[画面]{PRIVACY_NOTE}。", text)
        self.assertNotIn("这一轮没有截图", text)

    def test_window_line_sits_before_frame_line(self):
        text = self._material(window_line="[前台窗口] 游戏 (game.exe)")
        self.assertLess(text.index("[前台窗口]"), text.index("[画面]"))

    def test_business_layers_do_not_branch_on_title_switch(self):
        root = Path(__file__).resolve().parents[1]
        for rel in ("sidekick/scheduling/scheduler.py", "sidekick/app/runtime.py"):
            source = (root / rel).read_text(encoding="utf-8")
            self.assertNotIn("include_window_title", source, rel)


if __name__ == "__main__":
    unittest.main()
