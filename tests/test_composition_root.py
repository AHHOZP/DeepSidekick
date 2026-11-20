from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sidekick import config
from sidekick.app import lifecycle
from sidekick.app.runtime import build_runtime
from sidekick.avatar import roles
from sidekick.brain import users
from sidekick.core.null import NullEmbedder


class FakeMic:
    enabled = True
    error = None
    is_playing = False
    on_barge = None
    play_ref = None

    def __init__(self, cfg):
        self.cfg = cfg
        self.on_segment = None
        self.calls: list[str] = []

    def start(self):
        self.calls.append("start")

    def close(self):
        self.calls.append("close")


class FakeFrames:
    enabled = True
    last_error = None
    interval = 5
    max_frames = 6

    def __init__(self, cfg):
        self.cfg = cfg
        self.calls: list[str] = []

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")

    def take(self):
        return []


class FakeStage:
    clients = 0

    def __init__(self):
        self.events = []

    def emit(self, event, **kw):
        self.events.append((event, kw))

    def stats(self):
        return {"clients": 0, "sent": 0, "error": None}


class FakeAvatar:
    id = "avatar"
    kind = "live2d"
    capabilities = {}
    broken: list[str] = []
    name = "avatar"
    # 角色设定字段：没配就是 base（和真实 Avatar 的默认值一致）。
    display_name = ""
    role_dir = None
    stage: dict = {}
    profile_error = None

    @property
    def shown_name(self):
        return self.display_name or self.id


class FakeCatalog:
    def __init__(self, root):
        self.root = Path(root)
        self.avatar = FakeAvatar()
        self.pinned = False

    def pin(self):
        self.pinned = True

    def selected(self):
        return self.avatar


class FakeStore:
    def __init__(self, path):
        self.path = Path(path)


class FakeHistory:
    def __init__(self, store, history_max, summarize_every):
        self.store = store
        self.history_max = history_max
        self.summarize_every = summarize_every

    def reload(self, cfg):
        return None

    def close(self):
        return None


class FakeMemory:
    enabled = True
    last_summary_error = None
    last_recall: list = []
    last_recall_report: dict = {}
    game_id = ""

    def __init__(self, cfg, root, store=None):
        self.cfg = cfg
        self.root = root
        self.store = store

    def close(self):
        return None


class FakeStt:
    name = "sherpa/sense-voice"
    model_size = "fake"
    device = "cpu"
    error = None
    load_s = 0.0
    warnings: list[str] = []

    def warmup(self):
        return True

    def close(self):
        return None


class FakeWorker:
    def __init__(self, stt, events, log):
        self.stt = stt
        self.events = events
        self.log = log
        self.closed = False

    def on_segment(self, *_a, **_k):
        return None

    def start(self):
        return None

    def close(self):
        self.closed = True
        return True

    def status(self):
        return {"dead": False, "disabled": False, "error": None}


class FakeSpeaker:
    profile_name = "fake"
    preset_name = "edge"
    prompt_rules = ""
    available = True
    busy = False
    last_request_at = 0.0

    def __init__(self, cfg, *, provider, mic=None):
        self.cfg = cfg
        self.provider = provider
        self.mic = mic
        self.play_ref = (0.0, 0.0)
        self.stopped = 0

    def reload(self, cfg):
        self.reloaded_cfg = cfg

    def stop(self):
        self.stopped += 1

    def snapshot(self):
        return {"profile": self.profile_name, "preset": self.preset_name,
                "error": None}

    async def close(self):
        return None


class FakeBrain:
    def __init__(self, cfg, *, history, memory, worldbook, actions, llm,
                 recall=None, root=None, speech_rules="", log=print):
        self.cfg = cfg
        self.history = history
        self.memory = memory
        self.worldbook = worldbook
        self.actions = actions
        self.llm = llm
        self.recall = recall
        self.root = root
        self.speech_rules = speech_rules
        self.model = "fake"

    def stats(self):
        return {}

    async def warmup(self):
        return None

    def reload_config(self, cfg):
        self.cfg = cfg

    async def close(self):
        return None


class CompositionRootTests(unittest.TestCase):
    def test_build_runtime_exitstack_closes_on_failure(self):
        closed = []

        def fail(cfg, root, stack):
            stack.callback(lambda: closed.append("closed"))
            raise RuntimeError("boom")

        with mock.patch("sidekick.app.runtime._build_runtime", side_effect=fail):
            with self.assertRaises(RuntimeError):
                build_runtime(config.defaults(), root=Path("."))
        self.assertEqual(["closed"], closed)

    def test_build_runtime_success_detaches_callbacks(self):
        closed = []

        def ok(cfg, root, stack):
            stack.callback(lambda: closed.append("closed"))
            return "runtime"

        with mock.patch("sidekick.app.runtime._build_runtime", side_effect=ok):
            self.assertEqual("runtime",
                             build_runtime(config.defaults(), root=Path(".")))
        self.assertEqual([], closed)

    def test_build_runtime_all_features_faked_and_wired(self):
        cfg = config.defaults()
        cfg["danmu_source"] = "none"
        cfg["game"]["enabled"] = False
        mic = FakeMic(cfg)
        frames = FakeFrames(cfg)
        stage = FakeStage()
        catalog = FakeCatalog(Path("."))
        store = FakeStore("db")
        history = FakeHistory(store, 20, 10)
        memory = FakeMemory(cfg, ".")
        stt = FakeStt()
        worker = FakeWorker(stt, None, None)
        provider = SimpleNamespace(name="fake")
        speaker = FakeSpeaker(cfg, provider=provider, mic=mic)

        with tempfile.TemporaryDirectory() as td, \
                mock.patch("sidekick.app.runtime.Mic", return_value=mic), \
                mock.patch("sidekick.app.runtime.FrameQueue", return_value=frames), \
                mock.patch("sidekick.app.runtime.StageClient", return_value=stage), \
                mock.patch("sidekick.app.runtime.AvatarCatalog", return_value=catalog), \
                mock.patch("sidekick.app.runtime.MemoryStore", return_value=store), \
                mock.patch("sidekick.app.runtime.SqliteHistory", return_value=history), \
                mock.patch("sidekick.app.runtime.Memory", return_value=memory), \
                mock.patch("sidekick.app.runtime.embed_mod.build",
                           return_value=NullEmbedder()), \
                mock.patch("sidekick.app.runtime.make_stt", return_value=stt), \
                mock.patch("sidekick.app.runtime.SttWorker", return_value=worker), \
                mock.patch("sidekick.app.runtime.make_tts", return_value=provider) as make_tts, \
                mock.patch("sidekick.app.runtime.Speaker", return_value=speaker), \
                mock.patch("sidekick.app.runtime.Brain", FakeBrain):
            roles.write_profile_dir(roles.role_dir(Path(td), "avatar"), {
                "name": "用户", "bot_name": "小助手", "aliases": ["助手"]})
            users.write(Path(td), "default", {"name": "用户"})
            rt = build_runtime(cfg, root=Path(td))

        self.assertIs(rt.mic, mic)
        self.assertIs(rt.frames, frames)
        self.assertIs(rt.stage, stage)
        self.assertIs(rt.speaker, speaker)
        self.assertIs(rt.stt, stt)
        self.assertIs(rt.stt_worker, worker)
        self.assertIs(rt.history, history)
        self.assertIs(rt.memory, memory)
        self.assertIs(rt.brain.history, history)
        self.assertIs(rt.brain.memory, memory)
        self.assertIs(rt.brain.actions, rt.actions)
        self.assertIs(rt.catalog, catalog)
        self.assertIs(rt.actions.avatar, catalog.avatar)
        self.assertIs(mic.on_segment.__self__, worker)
        self.assertEqual(rt.session.effective()["danmu_source"], "none")
        # 角色钉住 + 角色设定/用户档案只进运行期 cfg，不写回全局配置。
        self.assertTrue(catalog.pinned)
        self.assertEqual(rt.cfg["persona"],
                         {"bot_name": "小助手", "aliases": ["助手"]})
        self.assertEqual(rt.cfg["user"]["name"], "用户")
        # 角色不再覆盖 TTS：声音永远来自全局配置。
        merged = make_tts.call_args.args[0]["tts"]
        self.assertEqual(merged["source"], "edge")
        self.assertNotIn("persona", cfg)
        self.assertEqual(cfg["user"], {"id": "default"})
        self.assertEqual(cfg["game"], {"enabled": False, "id": ""})

    def test_memory_disabled_does_not_create_db_or_load_model(self):
        cfg = config.defaults()
        cfg["danmu_source"] = "none"
        cfg["memory"]["enabled"] = False
        for key in ("mic", "stt", "tts", "capture", "stage", "game"):
            cfg[key]["enabled"] = False
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rt = build_runtime(cfg, root=root)
            self.assertFalse(rt.memory.enabled)
            self.assertEqual(rt.embedder.state, "disabled")
            self.assertFalse((root / "state" / "deepsidekick.db").exists())
            self.assertIsNone(rt.recall)

    def test_barge_is_wired_to_stop_playback_and_free_one_round(self):
        cfg = config.defaults()
        cfg["danmu_source"] = "none"
        cfg["game"]["enabled"] = False
        mic = FakeMic(cfg)
        frames = FakeFrames(cfg)
        stage = FakeStage()
        catalog = FakeCatalog(Path("."))
        store = FakeStore("db")
        history = FakeHistory(store, 20, 10)
        memory = FakeMemory(cfg, ".")
        stt = FakeStt()
        worker = FakeWorker(stt, None, None)
        provider = SimpleNamespace(name="fake")
        speaker = FakeSpeaker(cfg, provider=provider, mic=mic)

        with tempfile.TemporaryDirectory() as td, \
                mock.patch("sidekick.app.runtime.Mic", return_value=mic), \
                mock.patch("sidekick.app.runtime.FrameQueue", return_value=frames), \
                mock.patch("sidekick.app.runtime.StageClient", return_value=stage), \
                mock.patch("sidekick.app.runtime.AvatarCatalog", return_value=catalog), \
                mock.patch("sidekick.app.runtime.MemoryStore", return_value=store), \
                mock.patch("sidekick.app.runtime.SqliteHistory", return_value=history), \
                mock.patch("sidekick.app.runtime.Memory", return_value=memory), \
                mock.patch("sidekick.app.runtime.embed_mod.build",
                           return_value=NullEmbedder()), \
                mock.patch("sidekick.app.runtime.make_stt", return_value=stt), \
                mock.patch("sidekick.app.runtime.SttWorker", return_value=worker), \
                mock.patch("sidekick.app.runtime.make_tts", return_value=provider), \
                mock.patch("sidekick.app.runtime.Speaker", return_value=speaker), \
                mock.patch("sidekick.app.runtime.Brain", FakeBrain):
            rt = build_runtime(cfg, root=Path(td))
            self.assertTrue(callable(mic.on_barge))
            self.assertEqual(mic.play_ref(), (0.0, 0.0))
            # 打断后的下一轮不应被 min_gap 挡住。
            rt.scheduler.min_gap = 6.0
            speaker.last_request_at = time.time()
            self.assertGreater(rt.scheduler.gap_left(), 0.0)
            mic.on_barge()
            self.assertEqual(rt.scheduler.gap_left(), 0.0)

        self.assertEqual(speaker.stopped, 1)

    def test_startup_and_shutdown_order(self):
        order: list[str] = []

        class Said:
            def start(self):
                order.append("said.start")

        class Stt(FakeStt):
            def warmup(self):
                order.append("stt.warmup")
                return True

            def close(self):
                order.append("stt.close")

        class Worker(FakeWorker):
            def start(self):
                order.append("stt.worker.start")

            def close(self):
                order.append("stt.worker.close")
                return True

        class Mic(FakeMic):
            def start(self):
                order.append("mic.start")

            def close(self):
                order.append("mic.close")

        class Frames(FakeFrames):
            def start(self):
                order.append("frames.start")

            def stop(self):
                order.append("frames.stop")

        class Brain:
            model = "fake"
            provider_name = "fake"

            def stats(self):
                return {"preset_id": "", "preset_blocks": 0, "preset_error": None,
                        "persona_chars": 0, "user_chars": 0, "game_id": "",
                        "game_chars": 0, "memory_chars": 0,
                        "prefix_systems": 0, "output_chars": 0,
                        "avatar": "", "game_error": None}

            async def warmup(self):
                order.append("brain.warmup")
                return None

            async def close(self):
                order.append("brain.close")

        class Runner:
            async def cleanup(self):
                order.append("stage.cleanup")

        class Speaker:
            profile_name = "fake"

            async def close(self):
                order.append("speaker.close")

        class History:
            def close(self):
                order.append("history.close")

        class Memory:
            def close(self):
                order.append("memory.close")

        cfg = config.defaults()
        cfg["danmu_source"] = "none"
        cfg["stage"]["enabled"] = True
        cfg["stage"]["mode"] = "live"
        rt = SimpleNamespace(
            cfg=cfg, said=Said(), stt=Stt(), stt_worker=Worker(None, None, None),
            session=SimpleNamespace(live=False),
            mic=Mic(cfg), frames=Frames(cfg), stage=FakeStage(),
            catalog=FakeCatalog(Path(".")), speaker=Speaker(), brain=Brain(),
            history=History(), memory=Memory(), _stt_unavailable=False,
            _danmu_task=None, _stage_runner=None, _desktop_proc=None,
            _closed=False, on_desktop_click=lambda _part: None)

        async def run():
            with mock.patch(
                    "sidekick.app.lifecycle.serve_stage",
                    new=mock.AsyncMock(side_effect=lambda *a, **k: (
                        order.append("stage.serve") or Runner()))), \
                    mock.patch("sidekick.app.lifecycle.desktop_launcher.stop") as stop, \
                    mock.patch("sidekick.app.lifecycle.log"):
                await lifecycle.startup(rt)
                await lifecycle.shutdown(rt)
            stop.assert_called_once()

        asyncio.run(run())
        for earlier, later in (
                ("stt.warmup", "mic.start"),
                ("mic.start", "frames.start"),
                ("frames.start", "stage.serve"),
                ("stage.serve", "brain.warmup"),
                ("mic.close", "frames.stop"),
                ("frames.stop", "stt.close"),
                ("stt.close", "speaker.close")):
            self.assertLess(order.index(earlier), order.index(later),
                            (earlier, later, order))
        self.assertIn("stage.cleanup", order)
        self.assertIn("brain.close", order)
        self.assertIn("history.close", order)
        self.assertIn("memory.close", order)


if __name__ == "__main__":
    unittest.main()
