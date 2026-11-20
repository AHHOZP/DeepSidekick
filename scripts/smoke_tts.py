"""听一句 —— 走的是和直播**完全相同**的那条路。

    Speaker.say(...)  →  出口过滤  →  provider 的标签适配
                      →  流式（stream）或 分段（segments）  →  播到系统默认输出

     python scripts/smoke_tts.py                          # 默认一句
     python scripts/smoke_tts.py "（小声）诶嘿～"           # 说指定的
     python scripts/smoke_tts.py --devices                # 列音频设备
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidekick import config
from sidekick.output.speaker import Speaker
from sidekick.output.tts import make as make_tts

DEFAULT = "（小声）诶嘿，你终于来了～"


def list_devices() -> None:
    import sounddevice as sd

    for i, d in enumerate(sd.query_devices()):
        io = []
        if d.get("max_output_channels"):
            io.append("out")
        if d.get("max_input_channels"):
            io.append("in")
        mark = ""
        try:
            if i == sd.default.device[0]:
                mark += " [默认输出]"
            if i == sd.default.device[1]:
                mark += " [默认输入]"
        except Exception:
            pass
        print(f"  {i:3} {'/'.join(io):7} {d['name']}{mark}")


async def run(text: str) -> int:
    cfg = config.load()
    v = Speaker(cfg, provider=make_tts(cfg))
    print(f"profile={v.profile_name}  preset={v.preset_name}  "
          f"stream={v.stream}  segments={v.segments}  gain={v.gain}")

    t0 = time.time()
    res = await v.say(text)
    wall = (time.time() - t0) * 1000

    if not res.ok:
        print(f"[!!] 没出声：{res.reason}")
        print(f"     provider 报错：{v.profile_name} / {v.last_error}")
        await v.close()
        return 1

    ms = res.ms or {}
    print(f"[OK] 实念：{res.spoken}")
    print(f"     段数 {len(res.pages)}")
    if ms.get("mode") == "stream":
        print(f"     首块 {ms.get('first_audio_ms')} ms · "
              f"{ms.get('chunks')} 块 · 播完 {ms.get('total_ms')} ms")
    else:
        print(f"     首段 {ms.get('first_sentence_ms')} ms · "
              f"播完 {ms.get('total_ms')} ms")
    print(f"     墙钟 {wall:.0f} ms")
    await v.close()
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--devices" in argv:
        list_devices()
        return 0
    text = " ".join(a for a in argv if not a.startswith("--")) or DEFAULT
    print(f"要说：{text}")
    return asyncio.run(run(text))


if __name__ == "__main__":
    raise SystemExit(main())
