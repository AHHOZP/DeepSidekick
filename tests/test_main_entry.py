from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidekick import config
from sidekick import main


class MainEntryTests(QuietIsolatedAsyncioTestCase):
    async def test_missing_and_broken_config_exit_before_build(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.yaml"
            with mock.patch.object(config, "CONFIG_PATH", path), \
                    mock.patch("sidekick.main.configure_log"), \
                    mock.patch("sidekick.main.build_runtime") as build, \
                    mock.patch("sys.argv", ["sidekick"]):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    with self.assertRaises(SystemExit) as ctx:
                        await main.run()
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("config.yaml 不存在", err.getvalue())
                build.assert_not_called()

            path.write_text("{}", encoding="utf-8")
            with mock.patch.object(config, "CONFIG_PATH", path), \
                    mock.patch.object(config, "load", return_value={}), \
                    mock.patch.object(config, "last_error", "bad yaml"), \
                    mock.patch("sidekick.main.configure_log"), \
                    mock.patch("sidekick.main.build_runtime") as build, \
                    mock.patch("sys.argv", ["sidekick"]):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    with self.assertRaises(SystemExit) as ctx:
                        await main.run()
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("配置读取失败", err.getvalue())
                build.assert_not_called()

    async def test_startup_and_shutdown_order(self):
        order: list[str] = []
        cfg = config.defaults()
        rt = mock.Mock()
        rt.startup = mock.AsyncMock(
            side_effect=lambda: order.append("startup"))
        rt.main_loop = mock.AsyncMock(
            side_effect=lambda: order.append("main_loop"))
        rt.shutdown = mock.AsyncMock(
            side_effect=lambda: order.append("shutdown"))

        runner = mock.Mock()
        runner.cleanup = mock.AsyncMock(
            side_effect=lambda: order.append("debug.cleanup"))

        async def serve_debug(_rt):
            order.append("debug")
            return runner

        def build(_runtime_cfg):
            order.append("build")
            return rt

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(
                    config, "CONFIG_PATH",
                    Path(td) / "config.yaml"), \
                mock.patch.object(config, "load", return_value=cfg), \
                mock.patch.object(config, "last_error", None), \
                mock.patch("sidekick.main.configure_log"), \
                mock.patch("sidekick.main.build_runtime", side_effect=build), \
                mock.patch("sidekick.main.serve_debug",
                           side_effect=serve_debug), \
                mock.patch("sys.argv", ["sidekick"]):
            (Path(td) / "config.yaml").write_text("{}", encoding="utf-8")
            await main.run()

        self.assertEqual(order[0], "build")
        self.assertLess(order.index("startup"), order.index("main_loop"))
        self.assertLess(order.index("main_loop"), order.index("shutdown"))
        self.assertIn("debug", order)
        self.assertLess(order.index("debug"), order.index("main_loop"))
        self.assertLess(order.index("debug.cleanup"), order.index("shutdown"))


if __name__ == "__main__":
    unittest.main()
