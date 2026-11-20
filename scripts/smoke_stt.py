"""语音输入回环测试：TTS 合成一句 → STT 听回来 → 对比。

不需要你对着麦克风说话，就能验证"识别模型装好了、音频链路对得上、延迟多少"。

    python scripts/smoke_stt.py
    python scripts/smoke_stt.py --mic 8        # 顺便录 8 秒真麦克风
    python scripts/smoke_stt.py --model firered-ctc   # 临时换模型试（改内存里的配置）
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidekick import config
from sidekick.input.stt import make as make_stt

SAMPLE = "这boss第二阶段怎么躲啊，我刚才又被他打死了。"


def _rich(res) -> str:
    bits = []
    if getattr(res, "lang", ""):
        bits.append(f"语种={res.lang}")
    if getattr(res, "emotion", ""):
        bits.append(f"情绪={res.emotion}")
    if getattr(res, "events", ()):
        bits.append("事件=" + "/".join(res.events))
    return ("｜" + " ".join(bits)) if bits else ""


async def main() -> int:
    argv = sys.argv[1:]
    cfg = config.load()

    if "--model" in argv:
        i = argv.index("--model")
        if i + 1 < len(argv):
            cfg.setdefault("stt", {})["model"] = argv[i + 1]

    st = cfg.get("stt") or {}
    print("=== 1. 加载识别模型 ===")
    print(f"    model={st.get('model')} threads={st.get('num_threads', 4)} "
          f"dir={st.get('model_dir')}")

    stt = make_stt(cfg)
    t0 = time.time()
    if not stt.warmup():
        print(f"[!!] 加载失败：{stt.error}")
        print("     模型文件放在 stt.model_dir 下面；WebUI 的「听话（STT）」页可以下载")
        return 1
    print(f"    OK  {stt.name} / {stt.model_size}，加载 {time.time() - t0:.2f} 秒")

    print("\n=== 2. TTS 合成一句当测试音频 ===")
    import copy

    from sidekick.output.tts import make as make_tts

    tts_cfg = copy.deepcopy(cfg)
    tts_cfg["tts"]["source"] = "edge"
    tts = make_tts(tts_cfg)
    t0 = time.time()
    res = await tts.synth(SAMPLE)
    await tts.close()
    dur = len(res.pcm) / res.sr
    print(f"    OK  {dur:.1f} 秒音频 @{res.sr} Hz，合成 {time.time() - t0:.1f} 秒")

    print("\n=== 3. 识别回来 ===")
    ts = []
    text = ""
    for _ in range(3):                      # 跑 3 次，第一次可能还带冷启动
        t0 = time.time()
        text = stt.analyze(res.pcm, res.sr).text
        if stt.error:
            print(f"[X] 解码失败：{stt.error}")
            return 1
        ts.append((time.time() - t0) * 1000)
    best = sorted(ts)[1]
    print(f"    原话：{SAMPLE}")
    print(f"    识别：{text}")
    print(f"    耗时：{ts[0]:.0f} / {ts[1]:.0f} / {ts[2]:.0f} ms"
          f"（取中位 {best:.0f} ms，RTF {best / 1000 / max(0.1, dur):.3f}x）")

    ok = best / 1000 / max(0.1, dur) < 0.1
    print(f"    RTF 目标 < 0.1 → {'通过' if ok else '偏慢，检查 num_threads / 是否被别的程序抢 CPU'}")

    if "--mic" in argv:
        n = float(argv[argv.index("--mic") + 1]) if len(argv) > argv.index("--mic") + 1 else 6.0
        print(f"\n=== 4. 录 {n:.0f} 秒真麦克风（现在开始说话）===")
        import numpy as np
        import sounddevice as sd

        info = sd.query_devices(kind="input")
        sr = int(info.get("default_samplerate") or 48000)
        print(f"    设备：{info['name']}  {sr} Hz")
        audio = sd.rec(int(n * sr), samplerate=sr, channels=1, dtype="float32")
        sd.wait()
        pcm = np.asarray(audio, dtype=np.float32)[:, 0]
        peak = float(np.max(np.abs(pcm))) if len(pcm) else 0.0
        print(f"    录完，峰值 {peak:.3f}（低于 0.01 就是麦克风没声音）")
        t0 = time.time()
        r = stt.analyze(pcm, sr)
        print(f"    识别：{r.text}{_rich(r)}")
        print(f"    用时 {(time.time() - t0) * 1000:.0f} ms")

    s = stt.stats()
    print(f"\n=== 统计 === 共 {s['n']} 段，p50 {s['p50_ms']:.0f} ms，"
          f"p95 {s['p95_ms']:.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
