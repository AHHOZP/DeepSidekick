from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from sidekick import config
from identity_fixture import runtime_cfg
from sidekick.avatar.actions import AvatarActions
from sidekick.core.null import NullAvatarActions
from sidekick.output.display import BubbleDisplay
from sidekick.app.runtime import build_runtime


class FakeLLM:
    ready = True
    model = "fake-model"
    source = "fake"
    effort = ""
    temp = 0.0
    calls = 0
    system_mode = "auto"
    single_system = False
    last_usage: dict = {}

    def body_defaults(self):
        return {"model": self.model, "temperature": self.temp, "stream": False}

    def request_body(self, messages, temperature=None):
        body = {"messages": messages,
                **self.body_defaults()}
        if temperature is not None:
            body["temperature"] = float(temperature)
        return body

    async def chat(self, messages, **kw):
        self.calls += 1
        return {"choices": [{"message": {"content": "嗨，我在。"}}], "usage": {}}

    def cache_line(self):
        return ""

    async def close(self):
        return None


class FakeStage:
    clients = 0

    def __init__(self):
        self.events = []
        self.sent = 0

    def emit(self, event, **kw):
        self.events.append((event, kw))

    def stats(self):
        return {"clients": 0, "sent": 0}


def disabled_cfg() -> dict:
    cfg = config.defaults()
    cfg["memory"]["enabled"] = False
    cfg["capture"]["enabled"] = False
    cfg["mic"]["enabled"] = False
    cfg["stt"]["enabled"] = False
    cfg["tts"]["enabled"] = False
    cfg["stage"]["enabled"] = False
    cfg["game"]["enabled"] = False
    return cfg


class CompositionTests(unittest.TestCase):
    def test_all_features_disabled_has_no_side_effects(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rt = build_runtime(disabled_cfg(), root=root)
            status = rt.status()
            self.assertEqual({v["state"] for v in status["features"].values()},
                             {"disabled"})
            self.assertEqual(list(root.rglob("*.db")), [])
            names = {t.name for t in threading.enumerate()}
            self.assertFalse(names & {"mic", "stt", "frames"})
            self.assertIsNone(rt.stage_runner)
            # 停用的部件用空对象承接：同契约、不抛，状态可区分。
            self.assertFalse(rt.speaker.snapshot()["available"])
            self.assertEqual(rt.actions.summarize([{"name": "挥手"}]), "")
            self.assertEqual(rt.memory.game_id, "")
            self.assertEqual(rt.stt.snapshot()["engine"], "none")

    def test_empty_models_keeps_actions_for_hot_bind(self):
        cfg = disabled_cfg()
        cfg["stage"]["enabled"] = True
        cfg["danmu_source"] = "none"
        with tempfile.TemporaryDirectory() as td:
            rt = build_runtime(cfg, root=Path(td))
            # 空 models 也要有真实 actions，之后放入模型时 _refresh 才能 bind()。
            self.assertIsInstance(rt.actions, AvatarActions)
            self.assertIn("models", rt._stage_error or "")

    def test_status_and_peek_are_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            rt = build_runtime(disabled_cfg(), root=Path(td))
            with mock.patch.object(
                    rt, "_refresh",
                    side_effect=AssertionError("排查端点触发了配置刷新")):
                rt.status()
                rt.peek()

    def test_status_reports_config_and_secret_errors(self):
        from sidekick import config
        from sidekick.config import secrets

        with tempfile.TemporaryDirectory() as td:
            rt = build_runtime(disabled_cfg(), root=Path(td))
            old_cfg, old_sec = config.last_error, secrets.last_error
            try:
                config.last_error = "bad yaml"
                secrets.last_error = "bad bin"
                st = rt.status()
            finally:
                config.last_error, secrets.last_error = old_cfg, old_sec
            self.assertEqual(st["config_error"], "bad yaml")
            self.assertEqual(st["secrets_error"], "bad bin")

    def test_tts_enabled_but_missing_module_fails_loudly(self):
        cfg = disabled_cfg()
        cfg["tts"]["enabled"] = True
        with tempfile.TemporaryDirectory() as td, \
                mock.patch("sidekick.app.runtime.make_tts",
                           side_effect=SystemExit("bad provider")):
            with self.assertRaises(RuntimeError) as ctx:
                build_runtime(cfg, root=Path(td))
            self.assertIn("tts", str(ctx.exception))
            self.assertIn("bad provider", str(ctx.exception))

    def test_stt_disabled_still_builds_danmu_round(self):
        from sidekick.core.events import DANMU, Event

        with tempfile.TemporaryDirectory() as td:
            cfg = disabled_cfg()
            cfg["session"]["live"] = True
            rt = build_runtime(cfg, root=Path(td))
            self.assertIsNone(rt.stt_worker)
            rt.scheduler.batch_events = 1
            rt.events.push(Event(kind=DANMU, name="观众", text="这boss好肉啊"))
            snap = rt.scheduler.take_round()
            self.assertIsNotNone(snap)
            self.assertIn("弹幕", snap["text"])
            self.assertIn("这boss好肉啊", snap["text"])
            self.assertEqual(rt.features()["stt"]["state"], "disabled")

    def test_desktop_click_enters_brain_material(self):
        from sidekick.core.events import ME

        with tempfile.TemporaryDirectory() as td:
            rt = build_runtime(disabled_cfg(), root=Path(td))
            rt.on_desktop_click("头")
            events = rt.events.since_seq(0)
            self.assertEqual(events[-1].kind, ME)
            self.assertIn("摸了摸她的头", events[-1].text)

    def test_feature_status_four_states(self):
        from sidekick.core.ports import FeatureStatus

        self.assertEqual(FeatureStatus("x", enabled=False).state, "disabled")
        self.assertEqual(FeatureStatus("x", available=False).state, "unavailable")
        self.assertEqual(FeatureStatus("x", degraded=True).state, "degraded")
        self.assertEqual(FeatureStatus("x").state, "healthy")

    def test_prompt_output_includes_action_rules_only_when_present(self):
        from sidekick.brain.prompt import PromptBuilder
        from sidekick.core.null import InMemoryHistory, NullAvatarActions, NullLongTerm

        class WithRules:
            def rules(self):
                return "【动作】wave"

        with tempfile.TemporaryDirectory() as td:
            cfg = runtime_cfg(td)
            cfg["memory"]["enabled"] = False
            empty = PromptBuilder(cfg, td, actions=NullAvatarActions(),
                                  history=InMemoryHistory(4),
                                  memory=NullLongTerm())
            self.assertNotIn("【动作】wave", empty.build_tail_block())
            dynamic = "\n\n".join(
                str(m.get("content") or "")
                for m in empty.build_messages("测试"))
            self.assertIn("今天是", dynamic)
            self.assertNotIn("[最近记得的]", dynamic)

            with_rules = PromptBuilder(cfg, td, actions=WithRules(),
                                       history=InMemoryHistory(4),
                                       memory=NullLongTerm())
            self.assertIn("【动作】wave", with_rules.build_tail_block())


class CompositionIntegrationTests(QuietIsolatedAsyncioTestCase):
    async def test_no_tts_no_stt_danmu_turn_still_bubbles_and_keeps_history(self):
        from sidekick.core.events import DANMU, Event

        with tempfile.TemporaryDirectory() as td:
            rt = build_runtime(disabled_cfg(), root=Path(td))
            rt.brain.llm = FakeLLM()
            stage = FakeStage()
            rt.stage = stage
            rt.display = BubbleDisplay(stage, cps=20)
            rt.scheduler.batch_events = 1
            rt.events.push(Event(kind=DANMU, name="观众", text="这boss怎么打"))
            snap = rt.scheduler.take_round()
            self.assertIsNotNone(snap)
            logs = []
            with mock.patch("sidekick.app.runtime.log", logs.append), \
                    mock.patch("sidekick.output.reply.log", logs.append):
                decided = await rt._think(snap)
                self.assertIsNotNone(decided)
                out, t0 = decided
                await rt._talk(snap, out, t0)
            self.assertTrue(any("想好：嗨，我在。" in line for line in logs))
            kinds = [e[0] for e in stage.events]
            self.assertEqual(kinds, ["status", "status", "say", "end"])
            self.assertEqual([e[1].get("text") for e in stage.events[:2]],
                             ["思考中", "组织语言中"])
            self.assertIn("嗨，我在。", rt.said.recent(3))
            history = rt.history.messages()
            self.assertTrue(any(m["role"] == "assistant" and "嗨，我在。" in m["content"]
                                for m in history))


if __name__ == "__main__":
    unittest.main()
