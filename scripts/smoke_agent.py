"""端到端自检：不需要麦克风，直接喂一条「我说话」，看能不能走完 调度 → 脑 → 出声。

    python scripts/smoke_agent.py            # 约 40 秒，会出声
    python scripts/smoke_agent.py --mute     # 只跑到合成，不播
    python scripts/smoke_agent.py --tts fish # 换一个语音服务（profile）跑（只在这个脚本里）

改完代码后跑这个，比听声音更早发现问题。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidekick import config
from sidekick.core.events import ME, Event
from sidekick.app.runtime import build_runtime


async def main() -> int:
    argv = sys.argv[1:]
    mute = "--mute" in argv
    tts = ""
    if "--tts" in argv:
        tts = argv[argv.index("--tts") + 1]

    cfg = config.load()
    if tts:
        cfg["tts"] = {**(cfg.get("tts") or {}), "source": tts}
    rt = build_runtime(cfg)
    # 测试不吃麦克风，也不等真实的冷场间隔
    rt.mic.enabled = False
    rt.cfg["danmu_source"] = "none"
    rt.cfg.setdefault("cadence", {}).setdefault("normal", {})["idle_s"] = 7
    rt.scheduler.batch_s = 5
    rt.scheduler.me_wait_short = 0.6
    rt.session._cfg_ts = time.time() + 3600   # 自检期间别让热重载覆盖测试参数
    rt.speaker.mute = mute

    print("=== 启动 ===")
    await rt.startup()
    print(f"脑 {rt.brain.model} / TTS {rt.speaker.profile_name}")

    async def feed() -> None:
        await asyncio.sleep(2.5)
        msg = "诶你在干嘛，这游戏看着挺难的啊"
        rt.events.push(Event(kind=ME, name="我", text=msg))
        print(f"\n>>> 模拟我说话：{msg}\n")
        await asyncio.sleep(3600)   # 别让它先完成，否则主循环会被一起取消

    async def stopper() -> None:
        await asyncio.sleep(40)
        rt._closed = True

    tasks = [asyncio.create_task(rt.main_loop()),
             asyncio.create_task(feed()),
             asyncio.create_task(stopper())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await rt.shutdown()

    print("\n=== 结果 ===")
    print("说过：", rt.said.recent(10))
    print("脑：", rt.brain.stats())
    print("TTS 最后一次：", rt.speaker.last_ms)
    print("记忆：", rt.memory.stats())
    print("截图：", rt.frames.stats())
    print("形象层：", rt.stage.stats())
    if rt.brain.last_error:
        print("[!!] 脑报错：", rt.brain.last_error)
    if rt.speaker.last_error:
        print("[!!] 声音报错：", rt.speaker.last_error)
    return 0 if rt.said.recent(1) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
