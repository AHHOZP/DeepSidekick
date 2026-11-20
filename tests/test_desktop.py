from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sidekick import config
from sidekick.desktop import launcher
from sidekick.desktop.app import PetApp


class FakeProc:
    pid = 123

    def __init__(self, exit_code=None):
        self.exit_code = exit_code

    def wait(self, timeout=None):
        if self.exit_code is not None:
            return self.exit_code
        raise subprocess.TimeoutExpired("sidekick.desktop", timeout)

    def poll(self):
        return self.exit_code


class DesktopLaunchTests(unittest.TestCase):
    def test_immediate_exit_is_reported_not_treated_as_started(self):
        logs = []
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(launcher, "ROOT", Path(td)), \
                mock.patch.object(launcher.subprocess, "Popen",
                                  return_value=FakeProc(1)):
            got = launcher.launch(config.defaults(), logs.append)
        self.assertIsNone(got)
        self.assertTrue(any("立即退出" in line for line in logs))

    def test_running_process_is_returned(self):
        logs = []
        proc = FakeProc()
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(launcher, "ROOT", Path(td)), \
                mock.patch.object(launcher.subprocess, "Popen",
                                  return_value=proc) as popen:
            got = launcher.launch(config.defaults(), logs.append,
                                  avatar_id="my-model")
        self.assertIs(got, proc)
        args = popen.call_args.args[0]
        self.assertIn("-m", args)
        self.assertIn("sidekick.desktop", args)
        self.assertIn("my-model", args)
        self.assertTrue(any("已启动" in line for line in logs))


class DesktopWindowTests(unittest.TestCase):
    def test_close_stops_hook_and_restores_wndproc(self):
        from sidekick.desktop.window import Window

        calls = []
        window = object.__new__(Window)
        window.hwnd = 1
        window.hook = SimpleNamespace(stop=lambda: calls.append("hook"))
        window._user32 = SimpleNamespace(
            SetWindowLongPtrW=lambda *a: calls.append("proc"))
        window._old_proc = 123
        window.close()
        self.assertEqual(["hook", "proc"], calls)
        self.assertIsNone(window._old_proc)


class DesktopControlTests(unittest.TestCase):
    def test_reload_control_requests_model_rebuild(self):
        app = object.__new__(PetApp)
        app.reload_requested = False
        app.running = True
        app.model = SimpleNamespace()
        app._apply_control({"op": "reload"})
        self.assertTrue(app.reload_requested)
        self.assertTrue(app.running)

    def test_reload_model_replaces_model_in_place(self):
        closed = []
        new = SimpleNamespace(notice="n", set_scale=lambda value: None)
        app = object.__new__(PetApp)
        app.model = SimpleNamespace(close=lambda: closed.append(True))
        app._make_model = lambda: new
        app._fit_window = lambda *_a, **_k: None
        app._scale = lambda: 1.0
        app.pointer = (0.5, 0.5)
        app.speech = object()
        app.drag_candidate = "头"
        app.dragging = True
        app._silence = 2.0
        app._last_frame = 0.0
        app._reload_model()
        self.assertTrue(closed)
        self.assertIs(app.model, new)
        self.assertEqual(app._notice, "n")
        self.assertIsNone(app.pointer)
        self.assertIsNone(app.speech)
        self.assertIsNone(app.drag_candidate)
        self.assertFalse(app.dragging)


class DesktopInputTests(unittest.TestCase):
    def test_click_sends_part_and_drag_does_not(self):
        sent = []

        class Events:
            def send(self, data):
                sent.append(data)

        app = object.__new__(PetApp)
        app.events = Events()
        app._save = lambda: None
        app.drag_candidate = "头"
        app.dragging = False
        app._on_up(SimpleNamespace(button=1))
        self.assertEqual(sent, [{"t": "click", "part": "头"}])

        app.drag_candidate = "头"
        app.dragging = True
        app._on_up(SimpleNamespace(button=1))
        self.assertEqual(len(sent), 1)


    def test_drag_uses_global_cursor_not_window_relative_pos(self):
        class W:
            def __init__(self):
                self.moved = []

            def move(self, x, y):
                self.moved.append((x, y))

        app = object.__new__(PetApp)
        app.drag_candidate = "头"
        app.drag_start = (100, 100)
        app.drag_window = (500, 500)
        app.dragging = False
        app.window = W()
        app.model = SimpleNamespace(look_at=lambda *a: None)
        with mock.patch("sidekick.desktop.app.win32api.GetCursorPos",
                        return_value=(120, 130)):
            app._on_motion(SimpleNamespace(pos=(5, 5)))
        self.assertEqual(app.window.moved, [(520, 530)])

    def test_pointer_uses_model_calibrated_value(self):
        class Model:
            pointer = (0.2, -0.3)

            def look_at(self, *pos):
                self.seen = pos

        app = object.__new__(PetApp)
        app.model = Model()
        app.window = SimpleNamespace(cursor_client_pos=lambda: (10, 20))
        app.width = 100
        app.height = 100
        app.drag_candidate = None
        app._follow_mouse = lambda: True
        app._update_pointer_follow()
        self.assertEqual(app.pointer, (0.2, -0.3))

    def test_hit_through_outside_window_is_transparent(self):
        calls = []
        app = object.__new__(PetApp)
        app.window = SimpleNamespace(
            click_through=False,
            cursor_client_pos=lambda: None,
            set_hit_transparent=lambda on: calls.append(on))
        app.menu = SimpleNamespace(active=False)
        app.drag_candidate = None
        app.dragging = False
        app._leave_model_at = 0.0
        app._handle_hit_through()
        self.assertEqual([True], calls)

    def test_hit_through_outside_window_is_debounced(self):
        calls = []
        app = object.__new__(PetApp)
        app.window = SimpleNamespace(
            click_through=False,
            cursor_client_pos=lambda: (200, 200),
            set_hit_transparent=lambda on: calls.append(on))
        app.menu = SimpleNamespace(active=False)
        app.drag_candidate = None
        app.dragging = False
        app.width = 100
        app.height = 100
        app.model = SimpleNamespace(hit_part=lambda x, y: "")
        app._leave_model_at = 0.0
        with mock.patch("sidekick.desktop.app.time.monotonic", return_value=10.0):
            app._handle_hit_through()
        self.assertEqual([], calls)
        with mock.patch("sidekick.desktop.app.time.monotonic", return_value=10.2):
            app._handle_hit_through()
        self.assertEqual([True], calls)

    def test_hit_through_model_is_immediately_interactive(self):
        calls = []
        app = object.__new__(PetApp)
        app.window = SimpleNamespace(
            click_through=False,
            cursor_client_pos=lambda: (10, 10),
            set_hit_transparent=lambda on: calls.append(on))
        app.menu = SimpleNamespace(active=False)
        app.drag_candidate = None
        app.dragging = False
        app._leave_model_at = 0.0
        app.width = 100
        app.height = 100
        app.model = SimpleNamespace(hit_part=lambda x, y: "头")
        app._handle_hit_through()
        self.assertEqual([False], calls)

    def test_effective_hit_part_uses_model_hit(self):
        app = object.__new__(PetApp)
        app.model = SimpleNamespace(hit_part=lambda x, y: "头")
        self.assertEqual("头", app._effective_hit_part(10, 10))
        app.model = SimpleNamespace(hit_part=lambda x, y: "")
        self.assertEqual("", app._effective_hit_part(10, 10))

    def test_menu_outside_click_closes_menu(self):
        closed = []
        app = object.__new__(PetApp)
        app.menu = SimpleNamespace(active=True)
        app.window = SimpleNamespace(
            screen_to_client=lambda x, y: (200, 200))
        app.width = 100
        app.height = 100
        app._menu_prev = False
        app._close_menu = lambda: closed.append(True)
        with mock.patch("sidekick.desktop.app.win32api.GetAsyncKeyState",
                        return_value=0x8000), \
                mock.patch("sidekick.desktop.app.win32api.GetCursorPos",
                           return_value=(250, 250)):
            app._poll_menu_outside()
        self.assertEqual([True], closed)

    def test_click_through_polling_still_opens_right_menu(self):
        opened = []

        class W:
            click_through = True

            def screen_to_client(self, x, y):
                return 10, 10

            def suspend_click_through(self):
                return True

        app = object.__new__(PetApp)
        app.window = W()
        app.menu = SimpleNamespace(active=False)
        app.width = 100
        app.height = 100
        app.menu_suspended = False
        app._right_prev = False
        app._open_menu = opened.append
        with mock.patch(
                "sidekick.desktop.app.win32api.GetAsyncKeyState",
                return_value=0x8000), \
                mock.patch("sidekick.desktop.app.win32api.GetCursorPos",
                           return_value=(50, 50)):
            app._poll_right()
        self.assertEqual(opened, [(10, 10)])
        self.assertTrue(app.menu_suspended)

    def test_mouth_is_written_after_model_update(self):
        calls = []

        class Host:
            enabled = True

            def tick(self, now, dt, speech=None, context=None):
                calls.append("tick")
                return {"ParamAngleX": 1.0}

            def write(self, params):
                calls.append(("write", params))
                return len(params)

            def report(self):
                return {"enabled": True, "active": True}

        class Events:
            def send(self, data):
                return None

        class R:
            motion_host = Host()

            def update(self, dt=1 / 60):
                calls.append(("update", round(dt, 3)))

            def apply_controls(self):
                calls.append("controls")

            def set_mouth(self, level):
                calls.append(("mouth", level))

            def draw(self):
                calls.append("draw")

        app = object.__new__(PetApp)
        app.live2d_ready = False
        app.model = R()
        app.state = {"follow_mouse": True}
        app.speaking = False
        app.speech = None
        app.pointer = None
        app._silence = 0.0
        app._last_frame = 1.0
        app._last_report = 1.0
        app.autonomy_enabled = True
        app.events = Events()
        with mock.patch("sidekick.desktop.app.glClear"), \
                mock.patch("sidekick.desktop.app.time.monotonic", return_value=1.016):
            app._draw_frame()
        self.assertEqual(
            [c[0] if isinstance(c, tuple) else c for c in calls],
            ["update", "tick", "write", "controls", "draw"])
        self.assertEqual(calls[2], ("write", {"ParamAngleX": 1.0}))

    def test_mouth_event_becomes_speech_frame(self):
        app = object.__new__(PetApp)
        app.speech = None
        app.speech_at = 0.0
        with mock.patch("sidekick.desktop.app.time.monotonic", return_value=10.0):
            app._set_mouth({"v": 0.5, "open": 0.8, "form": -0.2,
                            "voiced": True})
        self.assertEqual(app.speech.openness, 0.8)
        self.assertEqual(app.speech.form, -0.2)
        self.assertTrue(app.speech.voiced)
        self.assertEqual(app.speech_at, 10.0)
        with mock.patch("sidekick.desktop.app.time.monotonic", return_value=10.2):
            app._handle_mouth()
        self.assertIsNone(app.speech)

    def test_semantic_acts_are_submitted_to_motion_host(self):
        class Host:
            def __init__(self):
                self.intents = []

            def submit(self, intents):
                self.intents.extend(intents)

        class R:
            motion_host = Host()

            def __init__(self):
                self.expressed = []

            def express_emotion(self, name):
                self.expressed.append(name)

        app = object.__new__(PetApp)
        app.model = R()
        app._submit_acts([
            {"kind": "emotion", "name": "happy", "intensity": 0.8},
            {"kind": "motion", "name": "selfie"},
            {"kind": "gaze", "name": "chat"},
        ], page=0)
        self.assertEqual([x.kind for x in R.motion_host.intents],
                         ["emotion", "gaze"])
        self.assertEqual(R.motion_host.intents[0].name, "happy")
        self.assertEqual(app.model.expressed, ["happy"])

    def test_remote_control_dispatches_to_model(self):
        class R:
            def __init__(self):
                self.got = []

            def control_param(self, *a):
                self.got.append(("param", a))

            def control_part(self, *a):
                self.got.append(("part", a))

            def control_motion(self, *a):
                self.got.append(("motion", a))

            def control_expression(self, *a):
                self.got.append(("expression", a))

            def control_view(self, *a):
                self.got.append(("view", a))

            def reset_controls(self):
                self.got.append(("reset", ()))

        app = object.__new__(PetApp)
        app.model = R()
        for data in (
                {"op": "param", "id": "ParamAngleX", "value": 0.5},
                {"op": "part", "id": "PartArmL", "value": 0},
                {"op": "motion", "group": "wave", "index": 1},
                {"op": "expression", "name": "开心"},
                {"op": "view", "zoom": 1.2, "offX": 0.1, "offY": -0.2},
                {"op": "reset"}):
            app._apply_control(data)
        self.assertEqual([x[0] for x in app.model.got],
                         ["param", "part", "motion", "expression", "view",
                          "reset"])


if __name__ == "__main__":
    unittest.main()
