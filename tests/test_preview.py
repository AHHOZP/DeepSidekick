"""Prompt 预演：材料样例、日志截断、子进程输出管线。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from sidekick.preview import _snapshot_db, sample_material, trim
from sidekick.memory.store import MemoryStore
from sidekick.webui.routes import preview as preview_route


class PreviewImportTests(unittest.TestCase):
    def test_asyncio_is_imported_for_render(self):
        import sidekick.preview as preview

        self.assertTrue(hasattr(preview, "asyncio"))


class PreviewSnapshotTests(unittest.TestCase):
    def test_memory_snapshot_is_independent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = MemoryStore(root / "source.db")
            source.add_message("user", "原话")
            source.close()
            copy = _snapshot_db(root / "source.db", root)
            self.assertTrue(copy.is_file())
            opened = MemoryStore(copy)
            try:
                self.assertEqual(opened.messages()[0]["content"], "原话")
                opened.add_message("user", "预演写入")
            finally:
                opened.close()
            reopened = MemoryStore(root / "source.db")
            try:
                self.assertEqual(len(reopened.messages()), 1)
            finally:
                reopened.close()


class TrimTests(unittest.TestCase):
    def test_keeps_text_from_preset_header(self):
        text = "[12:00:00] 启动完成\n麦克风策略：headset\n# 预设 default · 直播模式=关\n{\n}\n"
        out = trim(text)
        self.assertTrue(out.startswith("# 预设 default"))
        self.assertNotIn("麦克风策略", out)

    def test_already_trimmed_text_is_unchanged(self):
        text = "# 预设 default\n{}"
        self.assertEqual(text, trim(text))

    def test_text_without_header_is_returned_as_is(self):
        self.assertEqual("纯文本", trim("纯文本"))


class SampleMaterialTests(unittest.TestCase):
    def test_solo_has_no_audience_section(self):
        text, _meta = sample_material(live=False, name="用户", bot_names=["小鲸"])
        self.assertIn("[现在]", text)
        self.assertIn("[用户说的话]", text)
        for word in ("弹幕", "在线", "互动"):
            self.assertNotIn(word, text, word)

    def test_stream_has_audience_section(self):
        text, _meta = sample_material(live=True, name="用户", bot_names=["小鲸"])
        self.assertIn("[弹幕]", text)
        self.assertIn("在线", text)


class PreviewRunnerTests(unittest.TestCase):
    """`_run` 用临时文件接子进程输出；这条管线坏了整页就只剩报错。"""

    def test_captures_stdout_and_stderr(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            code, out, err = asyncio.run(preview_route._run(
                root, sys.executable, "-c",
                "import sys; print('你好'); print('bad', file=sys.stderr)"))
            self.assertEqual(0, code)
            self.assertIn("你好", out)
            self.assertIn("bad", err)
            # 跑完不留垃圾
            self.assertEqual([], list((root / "temp").glob("prompt-preview.*")))

    def test_nonzero_exit_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            code, _out, err = asyncio.run(preview_route._run(
                Path(td), sys.executable, "-c", "raise SystemExit(3)"))
            self.assertEqual(3, code)

    def test_cancel_kills_child_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            async def case():
                task = asyncio.create_task(preview_route._run(
                    root, sys.executable, "-c",
                    "import time; time.sleep(10)"))
                await asyncio.sleep(0.2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            asyncio.run(case())
            self.assertEqual([], list((root / "temp").glob("prompt-preview.*")))


if __name__ == "__main__":
    unittest.main()
