r"""录音 + 按线上逻辑切一遍，看你的话会不会被切碎 / 被漏掉。

开着游戏、正常音量，跑：

    python scripts\probe_mic_cut.py --seconds 12

然后**正常说 2~3 句话**（中间自然停顿，别刻意放慢）。

它会把结果写到 state/mic_cut.json，同时打印：

  · 每个 30ms 块的电平分布（均值 / P90 / 峰值）
  · 按**当前配置**切出几段、每段多长
  · 每段里最弱的一块是多少（离续接门槛有多近）
  · 一串候选阈值下的段数，方便一眼挑
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidekick import config
from sidekick.input.vad import (NoiseFloor, VadParams, segment_levels,
                            start_gate_of)


def record(seconds: float) -> tuple[np.ndarray, int]:
    import sounddevice as sd

    info = sd.query_devices(kind="input")
    sr = int(info.get("default_samplerate") or 48000)
    block = max(256, int(sr * 0.03))
    frames = []
    with sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                        blocksize=block) as s:
        t0 = time.time()
        while time.time() - t0 < seconds:
            data, _ = s.read(block)
            frames.append(data[:, 0].copy())
    return np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32), sr


def block_rms(pcm: np.ndarray, sr: int) -> tuple[np.ndarray, float]:
    block = max(256, int(sr * 0.03))
    n = len(pcm) // block
    vals = np.array([float(np.sqrt(np.mean(np.square(pcm[i * block:(i + 1) * block],
                                                      dtype=np.float64))))
                     for i in range(n)])
    return vals, 1000.0 * block / sr


def main() -> int:
    argv = sys.argv[1:]
    seconds = float(argv[argv.index("--seconds") + 1]) if "--seconds" in argv else 12.0

    cfg = config.load()
    params = VadParams.from_cfg(cfg)
    gate_percent = int(cfg["mic"]["gate_percent"])

    print(f"请开游戏、正常说 2~3 句话（{seconds:.0f} 秒）……\n", flush=True)

    pcm, sr = record(seconds)
    vals, block_ms = block_rms(pcm, sr)
    if len(vals) == 0:
        print("没录到东西")
        return 1

    # 门限按线上同一套规则现算：录音的 p10 当噪声底，乘灵敏度系数。
    est = NoiseFloor(block_ms=block_ms, warmup_s=0.0)
    for level in vals:
        est.feed(float(level), usable=True)
    thr = start_gate_of(est.value(), None, gate_percent)
    print(f"当前配置：门限百分比 {gate_percent}% → 噪声底 {est.value():.4f}  "
          f"门限 {thr:.4f}  续接 {thr * params.continue_ratio:.4f}  "
          f"连击 {params.start_blocks} 块  "
          f"silence {params.silence_ms:.0f}ms  min {params.min_speech_ms:.0f}ms")

    def durations(segs) -> list[float]:
        return [round(s.speech_blocks * block_ms / 1000.0, 1) for s in segs]

    segs = segment_levels(vals, block_ms, params.with_threshold(thr))
    print(f"录了 {seconds:.0f} 秒，{len(vals)} 块")
    print(f"  整体：均值 {vals.mean():.4f}  中位 {np.median(vals):.4f}  "
          f"P90 {np.percentile(vals, 90):.4f}  峰值 {vals.max():.4f}")
    print(f"\n按当前配置切出 {len(segs)} 段：")
    for k, seg in enumerate(segs, 1):
        print(f"  [{k}] {seg.start * block_ms / 1000:5.1f}s 起，"
              f"长 {seg.speech_blocks * block_ms / 1000:4.1f}s，"
              f"最弱块 {seg.weakest:.4f}（续接门槛 {thr * params.continue_ratio:.4f}），"
              f"低于续接门槛 {seg.below_gate} 块")

    print("\n扫描：门限取当前值的不同倍数会切出几段")
    for f in (0.5, 0.75, 1.0, 1.5, 2.0, 3.0):
        cand = thr * f
        s = segment_levels(vals, block_ms, params.with_threshold(cand))
        mark = "  ← 当前" if f == 1.0 else ""
        print(f"  {cand:.4f}（×{f:.2f}）→ {len(s):2d} 段 {durations(s)}{mark}")

    out = ROOT / "state" / "mic_cut.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "seconds": seconds,
        "cfg": {"thr": thr, "cont": params.continue_ratio,
                "start_blocks": params.start_blocks,
                "silence_ms": params.silence_ms,
                "min_speech_ms": params.min_speech_ms},
        "stats": {"mean": round(float(vals.mean()), 5),
                  "p50": round(float(np.median(vals)), 5),
                  "p90": round(float(np.percentile(vals, 90)), 5),
                  "peak": round(float(vals.max()), 5)},
        "segments": [{"start_s": round(s.start * block_ms / 1000, 2),
                      "dur_s": round(s.speech_blocks * block_ms / 1000, 2),
                      "weakest": round(s.weakest, 5),
                      "below_gate": s.below_gate}
                     for s in segs],
        "blocks": [round(float(x), 5) for x in vals],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细已写到 {out}（含每块电平，需要我分析可以留着）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
