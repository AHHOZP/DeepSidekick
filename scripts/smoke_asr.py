"""离线自检：识别模型能不能加载、能不能解码。

不联网、不说话、不合成 TTS —— 只回答一个问题：
「现在这台的语音识别，能不能在开播前就确认是好的？」

    python scripts/smoke_asr.py
    python scripts/smoke_asr.py --model firered-ctc

开播前单独跑；要听真声音请用 scripts/smoke_stt.py。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from sidekick import config


def main() -> int:
    argv = sys.argv[1:]
    cfg = config.load()
    st = cfg.get("stt") or {}

    if "--model" in argv:
        i = argv.index("--model")
        if i + 1 < len(argv):
            st = dict(st)
            st["model"] = argv[i + 1]
            cfg["stt"] = st

    print(f"model={st.get('model')} threads={st.get('num_threads', 4)}")

    from sidekick.input.stt.engines.registry import load_specs
    from sidekick.input.stt.engines.sherpa import SherpaEngine

    try:
        specs = load_specs(cfg, config.ROOT)
    except Exception as e:
        print(f"[X] 读 stt.models 失败：{e}")
        return 1
    mid = str(st.get("model") or "")
    spec = specs.get(mid)
    if spec is None:
        print(f"[X] stt.model={mid!r} 不在 stt.models 里；已配置："
              f"{', '.join(specs) or '（空）'}")
        return 1
    miss = spec.missing()
    if miss:
        print("[X] 模型文件不齐，缺：")
        for p in miss:
            print("     ", p)
        print(f"    放到 {spec.dir} 下面，或在 WebUI 的「听话（STT）」页点下载")
        return 1
    eng = SherpaEngine(spec, st)

    t0 = time.time()
    if not eng.warmup():
        print(f"[X] 加载失败：{eng.error}")
        return 1
    load_s = time.time() - t0

    # 1 秒 440Hz 正弦，当"有声但不是语音"的输入，只测解码通不通
    sr = 16000
    t = np.arange(sr, dtype=np.float32) / sr
    x = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    try:
        eng.transcribe(x, sr)
        if eng.error:
            raise RuntimeError(eng.error)
    except Exception as e:
        print(f"[X] 解码失败：{type(e).__name__}: {e}")
        return 1

    t0 = time.time()
    for _ in range(3):
        eng.transcribe(x, sr)
        if eng.error:
            print(f"[X] 解码失败：{eng.error}")
            return 1
    per = (time.time() - t0) / 3 * 1000

    print(f"[OK] {eng.name} / {eng.model_size}"
          f" 加载 {load_s:.2f}s，1 秒音频解码 {per:.0f} ms"
          f"（RTF {per / 1000:.3f}）")
    try:
        eng.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
