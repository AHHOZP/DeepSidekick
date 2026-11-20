from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidekick.webui.supervisor import Supervisor


class FakeProc:
    """子进程立即退出：start() 必须返回错误，而不是在锁里等自己。"""

    pid = 999_999_999
    returncode = 1

    async def wait(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15


class LivingProc:
    """不发退出、只等被取消的假 runtime，用来覆盖 ready 成功路径。"""

    pid = 999_999_999
    returncode = None

    async def wait(self):
        await asyncio.Event().wait()

    def terminate(self):
        self.returncode = -15


class SupervisorTests(QuietIsolatedAsyncioTestCase):
    async def test_start_failure_does_not_deadlock_and_cleans_state(self):
        with tempfile.TemporaryDirectory() as td:
            sup = Supervisor(Path(td))
            old = open(sup.console_log, "ab", buffering=0)
            sup._log_fp = old
            with mock.patch(
                    "sidekick.webui.supervisor.asyncio.create_subprocess_exec",
                    new=mock.AsyncMock(return_value=FakeProc())):
                res = await asyncio.wait_for(sup.start(), timeout=2.0)
            self.assertFalse(res["ok"])
            self.assertIn("退出", res["error"])
            self.assertIsNone(sup.proc)
            self.assertFalse(sup.pid_file.exists())
            self.assertTrue(old.closed)
            self.assertIsNone(sup._log_fp)

    async def test_start_returns_ok_after_ready_file(self):
        with tempfile.TemporaryDirectory() as td:
            sup = Supervisor(Path(td))
            proc = LivingProc()

            def fake_start(*_args, **_kwargs):
                sup.ready_file.write_text(str(proc.pid), encoding="utf-8")
                return proc

            with mock.patch(
                    "sidekick.webui.supervisor.asyncio.create_subprocess_exec",
                    side_effect=fake_start):
                res = await asyncio.wait_for(sup.start(), timeout=2.0)
            self.assertTrue(res["ok"])
            self.assertIs(proc, sup.proc)
            # 不走 stop() 的 taskkill 分支；直接结束假进程并清理。
            proc.returncode = 0
            await sup.shutdown()
            self.assertIsNone(sup.proc)

    async def test_pid_file_rejects_json_scalars(self):
        with tempfile.TemporaryDirectory() as td:
            sup = Supervisor(Path(td))
            sup.pid_file.write_text("123", encoding="utf-8")
            self.assertIsNone(sup._pid())
            sup.pid_file.write_text("{bad", encoding="utf-8")
            self.assertIsNone(sup._pid())
            sup.pid_file.write_text('{"pid": 123}', encoding="utf-8")
            self.assertEqual(sup._pid(), 123)

    async def test_write_pid_is_atomic_and_keeps_shape(self):
        with tempfile.TemporaryDirectory() as td:
            sup = Supervisor(Path(td))
            sup._write_pid(456)
            data = json.loads(sup.pid_file.read_text(encoding="utf-8"))
            self.assertEqual(data["pid"], 456)
            self.assertIn("started_at", data)
            self.assertEqual(list(sup.state.glob("*.tmp")), [])

    async def test_pid_owned_rejects_reused_pid(self):
        with tempfile.TemporaryDirectory() as td:
            sup = Supervisor(Path(td))
            pid = os.getpid()
            sup._write_pid(pid)
            self.assertTrue(sup._pid_owned(pid))
            data = json.loads(sup.pid_file.read_text(encoding="utf-8"))
            data["create_time"] = 0
            sup.pid_file.write_text(json.dumps(data), encoding="utf-8")
            self.assertFalse(sup._pid_owned(pid))

    async def test_stop_without_process_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            sup = Supervisor(Path(td))
            self.assertEqual({"ok": True, "already": True}, await sup.stop())
            self.assertEqual({"ok": True, "already": True}, await sup.stop())

    async def test_stop_cleans_dead_proc_and_open_log(self):
        with tempfile.TemporaryDirectory() as td:
            sup = Supervisor(Path(td))
            sup.proc = FakeProc()
            sup.started_at = 123.0
            old = open(sup.console_log, "ab", buffering=0)
            sup._log_fp = old
            res = await sup.stop()
            self.assertTrue(res.get("already"))
            self.assertIsNone(sup.proc)
            self.assertEqual(sup.started_at, 0.0)
            self.assertIsNone(sup._log_fp)
            self.assertTrue(old.closed)

    async def test_start_uses_package_entry(self):
        with tempfile.TemporaryDirectory() as td:
            sup = Supervisor(Path(td))
            fake = mock.AsyncMock(return_value=FakeProc())
            with mock.patch(
                    "sidekick.webui.supervisor.asyncio.create_subprocess_exec",
                    new=fake):
                await asyncio.wait_for(sup.start(), timeout=2.0)
            args = fake.await_args.args
            kwargs = fake.await_args.kwargs
            self.assertIn("-m", args)
            self.assertIn("sidekick", args)
            self.assertNotIn("sidekick.app.runtime", args)
            self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
            self.assertIsNotNone(kwargs["stderr"])

    async def test_start_puts_runtime_in_kill_on_close_job(self):
        """关窗口/结束 WebUI 时，runtime 和它拉起的桌宠要跟着走（见 sidekick/core/winjob.py）。"""
        with tempfile.TemporaryDirectory() as td:
            sup = Supervisor(Path(td))
            fake = mock.AsyncMock(return_value=FakeProc())
            with mock.patch(
                    "sidekick.webui.supervisor.asyncio.create_subprocess_exec",
                    new=fake):
                await asyncio.wait_for(sup.start(), timeout=2.0)
            self.assertIsNotNone(sup._job)
            await sup.shutdown()
            self.assertIsNone(sup._job)

    async def test_tail_reads_run_log_only(self):
        with tempfile.TemporaryDirectory() as td:
            sup = Supervisor(Path(td))
            run = sup.root / "state" / "run.log"
            run.write_text("\n".join(f"run{i}" for i in range(200)) + "\n",
                           encoding="utf-8")
            sup.console_log.write_text(
                "\n".join(f"con{i}" for i in range(50)) + "\n", encoding="utf-8")
            got = sup.tail(180)
            self.assertEqual(len(got), 180)
            self.assertEqual(got[0], "run20")
            self.assertEqual(got[-1], "run199")
            self.assertNotIn("con0", got)

    async def test_tail_falls_back_to_console_when_run_log_empty(self):
        with tempfile.TemporaryDirectory() as td:
            sup = Supervisor(Path(td))
            sup.console_log.write_text(
                "\n".join(f"con{i}" for i in range(10)) + "\n", encoding="utf-8")
            self.assertEqual(sup.tail(5), ["con5", "con6", "con7", "con8", "con9"])

    async def test_start_reopens_log_and_clears_old_logs(self):
        with tempfile.TemporaryDirectory() as td:
            sup = Supervisor(Path(td))
            run = sup.root / "state" / "run.log"
            run.write_text("OLD_RUN\n", encoding="utf-8")
            sup.console_log.write_text("OLD_CON\n", encoding="utf-8")
            old = open(sup.console_log, "ab", buffering=0)
            sup._log_fp = old
            with mock.patch(
                    "sidekick.webui.supervisor.asyncio.create_subprocess_exec",
                    new=mock.AsyncMock(return_value=FakeProc())):
                res = await asyncio.wait_for(sup.start(), timeout=2.0)
            self.assertFalse(res["ok"])
            self.assertTrue(old.closed)
            self.assertFalse(run.exists() and "OLD_RUN" in run.read_text(
                encoding="utf-8"))
            self.assertNotIn("OLD_CON", sup.console_log.read_text(encoding="utf-8"))
            self.assertIn("WebUI 启动", sup.console_log.read_text(encoding="utf-8"))

    async def test_watch_notifies_only_current_generation(self):
        with tempfile.TemporaryDirectory() as td:
            sup = Supervisor(Path(td))
            hits: list[int] = []
            sup.on_exit = lambda: hits.append(1)
            proc = FakeProc()
            sup._generation = 3
            await sup._watch(proc, 2)
            self.assertEqual(hits, [])
            await sup._watch(proc, 3)
            self.assertEqual(hits, [1])


if __name__ == "__main__":
    unittest.main()
