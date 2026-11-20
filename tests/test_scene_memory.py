from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from sidekick import config
from sidekick.app.identity import resolve
from sidekick.brain import scene_tag, users
from sidekick.brain.brain import Brain
from sidekick.brain.material import attach_scene, material_digest
from sidekick.brain.worldbook import Worldbook
from sidekick.core.null import InMemoryHistory, NullAvatarActions, NullLongTerm


class FakeLLM:
    source = "fake"
    model = "fake"
    effort = ""
    effort_dropped = False
    ready = True
    temp = 0.3
    warmup_enabled = False

    def __init__(self, text: str) -> None:
        self.text = text

    def request_body(self, messages, temperature=None):
        return {"messages": messages}

    async def chat(self, messages, *, temperature=None, timeout=None,
                   attempts=1):
        return {"choices": [{"message": {"content": self.text},
                             "finish_reason": "stop"}]}

    async def close(self):
        return None


def _brain(root: Path, text: str, *, tag: bool = True):
    users.write(root, "default", {"name": "阿明"})
    cfg = resolve(config.defaults(), root).cfg
    cfg["memory"]["enabled"] = False
    cfg["game"]["enabled"] = False
    cfg["brain"]["visual_memory_tag"] = tag
    history = InMemoryHistory(10)
    brain = Brain(cfg, history=history, memory=NullLongTerm(),
                  worldbook=Worldbook(cfg, root),
                  actions=NullAvatarActions(), llm=FakeLLM(text),
                  root=root, log=lambda *a, **k: None)
    return brain, history


MATERIAL = ("[现在]20:00:00｜陪了 1 分钟\n"
            "[画面]共 2 帧（20:00:01 → 20:00:02）：历史 1 帧分 1 张拼图。")


class SceneTagTests(unittest.TestCase):
    def test_strip_and_truncate(self):
        text, scene = scene_tag.extract("好。<scene>" + "打" * 30 + "</scene>")
        self.assertEqual("好。", text)
        self.assertEqual("打" * 25, scene)

    def test_unclosed_and_inline_tags_do_not_break_text(self):
        text, scene = scene_tag.extract("好看<scene>打BOSS二阶段")
        self.assertEqual("好看打BOSS二阶段", text)
        self.assertEqual("", scene)
        text, scene = scene_tag.extract("前<scene>场景</scene>后")
        self.assertEqual("前后", text)
        self.assertEqual("场景", scene)
        text, scene = scene_tag.extract("好。\n<scene>打BOSS二阶段")
        self.assertEqual("好。", text)
        self.assertEqual("打BOSS二阶段", scene)

    def test_missing_tag_is_clean(self):
        self.assertEqual(("好。", ""), scene_tag.extract("好。"))

    def test_digest_keeps_scene_before_frame(self):
        digest = material_digest(MATERIAL, "阿明")
        got = attach_scene(digest, "打女武神二阶段，血瓶剩2个")
        self.assertLess(got.index("[场景]"), got.index("画面："))
        self.assertIn("[场景]打女武神二阶段，血瓶剩2个", got)
        self.assertEqual(digest, material_digest(MATERIAL, "阿明"))


class SceneMemoryBrainTests(unittest.TestCase):
    def test_tag_is_stripped_and_scene_goes_into_history(self):
        with tempfile.TemporaryDirectory() as td:
            brain, history = _brain(
                Path(td), "好。<scene>打女武神二阶段，血瓶剩2个</scene>")
            out = asyncio.run(brain.reply(MATERIAL))
            self.assertEqual("好。", out)
            rows = history.messages()
            self.assertEqual("好。", rows[1]["content"])
            self.assertIn("[场景]打女武神二阶段，血瓶剩2个", rows[0]["content"])
            self.assertIn("画面：", rows[0]["content"])

    def test_disabled_keeps_tag_out_of_history_but_still_strips(self):
        with tempfile.TemporaryDirectory() as td:
            brain, history = _brain(
                Path(td), "好。<scene>打BOSS</scene>", tag=False)
            out = asyncio.run(brain.reply(MATERIAL))
            self.assertEqual("好。", out)
            self.assertNotIn("[场景]", history.messages()[0]["content"])

    def test_output_rule_follows_switch(self):
        with tempfile.TemporaryDirectory() as td:
            on, _ = _brain(Path(td), "好。", tag=True)
            self.assertIn("<scene>", on.prompt.build_tail_block())
        with tempfile.TemporaryDirectory() as td:
            off, _ = _brain(Path(td), "好。", tag=False)
            self.assertNotIn("<scene>", off.prompt.build_tail_block())


if __name__ == "__main__":
    unittest.main()
