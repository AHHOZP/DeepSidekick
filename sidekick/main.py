"""进程入口：`python -m sidekick`。

功能装配仍在 `sidekick/app/runtime.py` 的 `build_runtime()`；这里只处理 argv、调试端点、崩溃日志。
依赖方向是单向的 `main → app.runtime`，runtime 不认识入口文件。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from sidekick import config
from sidekick.diagnostics.server import serve_debug
from sidekick.core.log import configure as configure_log
from sidekick.core.log import crash, install_hooks, log
from sidekick.app.runtime import build_runtime


def _ready_file() -> Path:
    return config.ROOT / "state" / "runtime.ready"


async def run() -> None:
    no_debug = "--no-debug" in sys.argv
    configure_log(config.ROOT)
    if not config.CONFIG_PATH.exists():
        print("[sidekick] config.yaml 不存在；请先启动 WebUI 生成默认配置。",
              file=sys.stderr, flush=True)
        raise SystemExit(2)
    cfg = config.load()
    if config.last_error:
        print(f"[sidekick] 配置读取失败，runtime 未启动：{config.last_error}",
              file=sys.stderr, flush=True)
        raise SystemExit(2)
    rt = build_runtime(cfg)
    runner = None
    try:
        await rt.startup()
        # 就绪标记：WebUI 用它判断启动成功，不依赖可选的排查端口。
        ready = _ready_file()
        ready.parent.mkdir(parents=True, exist_ok=True)
        ready.write_text(str(os.getpid()), encoding="utf-8")
        if not no_debug:
            try:
                runner = await serve_debug(rt)
            except Exception as e:
                log(f"排查端点没起来（不影响说话）：{type(e).__name__}: {e}")
        await rt.main_loop()
    finally:
        try:
            _ready_file().unlink(missing_ok=True)
        except OSError:
            pass
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception as e:
                log(f"排查端点关闭失败：{type(e).__name__}: {e}")
        try:
            await rt.shutdown()
        except Exception as e:
            log(f"runtime 关闭失败：{type(e).__name__}: {e}")


def main() -> None:
    install_hooks()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except BaseException:
        import traceback

        tb = traceback.format_exc()
        out = crash(tb)
        if out is not None:
            print(f"[sidekick] 崩溃信息已写入 {out}")
        raise
