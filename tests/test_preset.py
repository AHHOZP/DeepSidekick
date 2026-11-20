"""对话预设：读写、锚点缝隙、直播条目、solo 与直播两种排布。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from identity_fixture import runtime_cfg
from sidekick.brain import preset as preset_mod
from sidekick.brain.prompt import PromptBuilder


def block(bid, slot="start", role="system", live=False, text="") -> dict:
    return {"id": bid, "title": bid, "slot": slot,
            "role": role, "live": live, "text": text or f"[{bid}]"}


def write_preset(root: Path, preset_id: str, blocks: list[dict]) -> None:
    got = [preset_mod.Block(id=b["id"], title=b.get("title") or b["id"],
                            slot=b["slot"], role=b["role"],
                            live=b["live"], text=b["text"])
           for b in blocks]
    preset_mod.write(root, preset_id, preset_id, "", got)


def persona(root: Path, role_id: str, text: str) -> None:
    base = root / "persona" / "roles" / role_id
    base.mkdir(parents=True, exist_ok=True)
    (base / "persona.md").write_text(text, encoding="utf-8")


class PresetFileTests(unittest.TestCase):
    def test_read_write_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_preset(root, "p1", [
                block("a", "start"),
                block("b", "after_user", role="user"),
                block("c", "start", live=True),
            ])
            got = preset_mod.read(root, "p1")
            self.assertIsNone(got.error)
            by_id = {b.id: b for b in got.blocks}
            self.assertEqual(("after_user", "user"),
                             (by_id["b"].slot, by_id["b"].role))
            self.assertTrue(by_id["c"].live)
            self.assertEqual(["a", "c"],
                             [b.id for b in got.blocks_for("start", live=True)])
            self.assertEqual(["a"], [b.id for b in got.blocks_for("start")])
            self.assertEqual([{"role": "user", "content": "[b]"}],
                             got.messages("after_user"))

    def test_file_order_is_prompt_order_in_same_slot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_preset(root, "p", [
                block("first", "start"),
                block("second", "start"),
                block("third", "start"),
            ])
            got = preset_mod.read(root, "p")
            self.assertEqual(["first", "second", "third"],
                             [b.id for b in got.blocks_for("start")])

    def test_live_and_normal_entries_mix_by_file_order(self):
        """同一个缝隙里直播/普通条目按书写顺序混排；单人陪玩只剔掉直播条目。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_preset(root, "p", [
                block("n1", "start"),
                block("l1", "start", live=True),
                block("n2", "start"),
            ])
            got = preset_mod.read(root, "p")
            self.assertEqual(["n1", "l1", "n2"],
                             [b.id for b in got.blocks_for("start", live=True)])
            self.assertEqual(["n1", "n2"],
                             [b.id for b in got.blocks_for("start")])

    def test_gaps_cover_every_slot_with_fixed_anchors(self):
        self.assertEqual(
            ("start", "after_persona", "after_user", "after_worldbook",
             "after_memory", "after_history", "after_recall_memory",
             "after_recall_worldbook", "after_material"),
            preset_mod.SLOTS)
        self.assertEqual(
            ["角色设定", "用户设定", "游戏资料", "长期记忆",
             "交互历史", "召回记忆", "召回游戏资料", "本轮互动消息"],
            [anchor for _slot, anchor in preset_mod.GAPS if anchor])
        self.assertEqual("", preset_mod.GAPS[-1][1])

    def test_unknown_slot_and_role_are_skipped_with_reason(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_preset(root, "p", [
                block("ok", "start"),
                {**block("bad-slot"), "slot": "middle"},
                {**block("bad-role"), "role": "assistant"},
                block("empty", "start", text="   "),
            ])
            got = preset_mod.read(root, "p")
            self.assertEqual(["ok"], [b.id for b in got.blocks])
            self.assertEqual(2, got.error.count("条"))

    def test_missing_preset_is_empty_not_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            got = preset_mod.read(Path(td), "nope")
            self.assertEqual((), got.blocks)
            self.assertIn("不存在", got.error or "")

    def test_invalid_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            for bad in ("../x", "", "a/b", "."):
                self.assertFalse(preset_mod.valid_id(bad), bad)
            self.assertTrue(preset_mod.valid_id("default"))

    def test_text_is_written_as_block_scalar(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_preset(root, "p", [block("a", "start", text="第一行\n第二行")])
            raw = preset_mod.preset_path(root, "p").read_text(encoding="utf-8")
            self.assertIn("text: |-", raw)
            self.assertIn("\n    第二行", raw)


class FakeHistory:
    history_max = 40

    def messages(self):
        return [{"role": "user", "content": "历史消息"}]


class FakeWorldbook:
    id = "g"
    error = None
    enabled = True

    def core_text(self, refresh=True):
        return "游戏核心"

    def always_entries(self, chapter=""):
        return []

    def stats(self, refresh=False):
        return {"entries": 0}


class FakeMemory:
    enabled = True

    def current_chapter(self):
        return ""

    def game_state_block(self, *, expand_tasks=False):
        return "[当前游戏]"

    def recent_live_block(self):
        return ""

    def recent_summary_ids(self):
        return set()

    def mark_recalled(self, candidates):
        pass


class FakeRecall:
    def run(self, material, history, chapter="", label=None):
        from sidekick.memory.recall import RecallItem, RecallResult

        return RecallResult(
            episodes=[RecallItem(source="memory", kind="summary", id="s1",
                                 title="", content="旧事", score=0.6,
                                 tier="strong", channel="vector",
                                 start_ts=1, end_ts=2)],
            game=[],
            worldbook=[RecallItem(source="worldbook", kind="worldbook", id="w1",
                                  title="北门", content="北门会关", score=0.7,
                                  tier="strong", channel="vector")],
            query="铁匠",
            report={"pools": {}, "time_intent": None, "time_fallback": False,
                    "degraded": None})


class FakeActions:
    avatar_id = ""

    def rules(self):
        return "【动作】wave"

    def prompt_rules(self):
        return ""


class PresetLayoutTests(unittest.TestCase):
    def _builder(self, root: Path, live: bool) -> PromptBuilder:
        cfg = runtime_cfg(root)
        cfg["session"]["live"] = live
        cfg["prompt"]["id"] = "p"
        cfg["user"]["name"] = "阿明"
        cfg["user"]["persona"] = "程序员。"
        persona(root, cfg["role"]["id"], "人设")
        return PromptBuilder(cfg, root, history=FakeHistory(), memory=FakeMemory(),
                             worldbook=FakeWorldbook(), recall=FakeRecall(),
                             actions=FakeActions())

    def _all_slots(self, root: Path) -> None:
        write_preset(root, "p", [
            block(f"s-{slot}", slot, text=f"<{slot}>")
            for slot in preset_mod.SLOTS
        ])

    def test_every_slot_lands_between_its_neighbours(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._all_slots(root)
            text = "\n".join(str(m["content"]) for m in
                             self._builder(root, False).build_messages("@@MATERIAL@@"))
            order = ["<start>", "人设", "<after_persona>", "[和你一起的人]",
                     "<after_user>", "游戏核心", "<after_worldbook>",
                     "[当前游戏]", "<after_memory>", "历史消息",
                     "<after_history>", "今天是", "<after_recall_memory>",
                     "[世界书]", "<after_recall_worldbook>",
                     "@@MATERIAL@@", "<after_material>", "【动作】wave"]
            positions = [text.index(x) for x in order]
            self.assertEqual(positions, sorted(positions), order)

    def test_live_entries_only_in_live_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_preset(root, "p", [
                block("n", "start"),
                block("s", "start", live=True, text="[直播追加]"),
            ])
            solo = "\n".join(str(m["content"]) for m in
                             self._builder(root, False).build_messages("[材料]"))
            self.assertIn("[n]", solo)
            self.assertNotIn("[直播追加]", solo)
            live = "\n".join(str(m["content"]) for m in
                             self._builder(root, True).build_messages("[材料]"))
            self.assertIn("[直播追加]", live)
            self.assertLess(live.index("[n]"), live.index("[直播追加]"))

    def test_solo_prompt_has_no_live_wording(self):
        """单人陪玩是主线：没开直播时，一条直播内容都不许出现在 prompt 里。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_preset(root, "p", [
                block("h", "start", text="[场合]只有你们俩。"),
                block("s", "start", live=True,
                      text="[直播]观众在弹幕里，礼物要谢，上舰要看。"),
            ])
            text = "\n".join(str(m["content"]) for m in
                             self._builder(root, False).build_messages("[材料]"))
            for word in ("直播", "观众", "弹幕", "礼物", "上舰"):
                self.assertNotIn(word, text, word)

    def test_preset_text_gets_persona_placeholders(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_preset(root, "p", [
                block("h", "start", text="你是{{bot}}，陪{{user}}玩。")])
            cfg = runtime_cfg(root)
            cfg["prompt"]["id"] = "p"
            cfg["persona"]["bot_name"] = "阿鲸"
            builder = PromptBuilder(cfg, root)
            first = builder.build_messages("[材料]")[0]["content"]
            self.assertIn("你是阿鲸，陪", first)
            self.assertNotIn("{{bot}}", first)

    def test_preset_hot_reloads_on_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_preset(root, "p", [block("h", "start", text="旧")])
            builder = self._builder(root, False)
            self.assertIn("旧", builder.build_messages("[材料]")[0]["content"])
            write_preset(root, "p", [block("h", "start", text="新")])
            self.assertIn("新", builder.build_messages("[材料]")[0]["content"])


if __name__ == "__main__":
    unittest.main()
