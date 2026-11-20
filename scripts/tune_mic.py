"""麦克风验收：看门限是否符合预期，而不是"照着建议填阈值"。

门限现在由「固定基线 + 环境噪声/峰值 × 门限百分比」现算，配置里没有绝对阈值可填。这个脚本用来确认
现在的门限确实落在"人声之上、噪声之下"：

    python scripts\\tune_mic.py --silent 5     # 第 1 步：你闭嘴 5 秒（游戏/音乐照常放）
    python scripts\\tune_mic.py --voice 5      # 第 2 步：正常说话 5 秒

它会打印噪声底、门限、说话段平均能量、信噪比和一句判断，
并按线上同一套切句规则回放一遍（能看到哪几句被切出来）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidekick import config
from sidekick.input.vad import (
    NoiseFloor, VadParams, continue_gate_of, segment_levels, start_gate_of,
)


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


def block_levels(pcm: np.ndarray, sr: int) -> tuple[list[float], float]:
    block = max(256, int(sr * 0.03))
    n = len(pcm) // block
    vals = [float(np.sqrt(np.mean(np.square(pcm[i * block:(i + 1) * block],
                                            dtype=np.float64))))
            for i in range(n)]
    return vals, 1000.0 * block / sr


def noise_of(levels: list[float], block_ms: float) -> tuple[float | None, float | None]:
    """用线上同一个估计器算噪声底和 p90 峰值（warmup 关掉，短录音也能出值）。"""
    est = NoiseFloor(block_ms=block_ms, warmup_s=0.0)
    for level in levels:
        est.feed(level, usable=True)
    return est.value(), est.value_high()


def snr_db(voice: float, noise: float) -> float:
    return 20.0 * np.log10(max(voice, 1e-9) / max(noise, 1e-9))


def judgement(db: float) -> str:
    if db >= 12.0:
        return "信噪比合适，门限没问题"
    if db >= 6.0:
        return "信噪比偏低：把门限百分比调低（更灵敏），或者换个更近的麦"
    return "信噪比太低：环境噪声盖过人声，先处理噪声（风扇/音响/键盘）"


def main() -> int:
    argv = sys.argv[1:]
    silent = float(argv[argv.index("--silent") + 1]) if "--silent" in argv else 0
    voice = float(argv[argv.index("--voice") + 1]) if "--voice" in argv else 0
    if not silent and not voice:
        print(__doc__)
        return 1

    cfg = config.load()
    m = cfg["mic"]
    params = VadParams.from_cfg(cfg)
    gate_percent = int(m.get("gate_percent", 100))
    try:
        import sounddevice as sd

        dev_name = str(sd.query_devices(kind="input").get("name") or "默认输入")
    except Exception:
        dev_name = "默认输入"
    print(f"输入设备=系统默认（{dev_name}）  门限百分比={gate_percent}%  "
          f"silence={m['silence_ms']}ms  min_speech={m['min_speech_ms']}ms\n")

    if silent:
        print(f"【第 1 步】请闭嘴 {silent:.0f} 秒（游戏/音乐照常放）……", flush=True)
        pcm, sr = record(silent)
        levels, block_ms = block_levels(pcm, sr)
        if not levels:
            print("没录到东西")
            return 1
        noise, high = noise_of(levels, block_ms)
        gate = start_gate_of(noise, high, gate_percent)
        print(f"  块数 {len(levels)}  均值 {np.mean(levels):.4f}  "
              f"中位 {np.median(levels):.4f}  峰值 {np.max(levels):.4f}")
        print(f"  噪声底 {noise:.4f}  环境峰值 {high:.4f} → "
              f"开口门限 {gate:.4f}  续接 {continue_gate_of(gate, m['continue_ratio']):.4f}")
        over = sum(1 for x in levels if x > gate)
        print(f"  按这个门限：静音期 {over * 100.0 / len(levels):.0f}% 的块会被当成「你在说话」")
        if over:
            print("  → 不是 0% 说明环境噪声离门限太近；先 --voice 看说话能不能切出来，"
                  "不行就把门限百分比调高（更保守）")
        print()

    if voice:
        print(f"【第 2 步】请正常说话 {voice:.0f} 秒（游戏照常放）……", flush=True)
        pcm, sr = record(voice)
        levels, block_ms = block_levels(pcm, sr)
        if not levels:
            print("没录到东西")
            return 1
        # 线上估 p90 时会排除"已判定为说话"的块；离线先按固定基线切一遍，
        # 把说话段排除后再估环境峰值，避免把说话峰值当成门限。
        rough_noise, _ = noise_of(levels, block_ms)
        rough_gate = start_gate_of(rough_noise, None, gate_percent)
        rough_segs = segment_levels(levels, block_ms,
                                    params.with_threshold(rough_gate))
        silent = []
        pos = 0
        for seg in rough_segs:
            silent.extend(levels[pos:seg.start])
            pos = seg.start + seg.speech_blocks
        silent.extend(levels[pos:])
        if len(silent) < max(10, len(levels) // 10):
            # 连续说话时 VAD 会包住整段，剩下的"静音"太少；拿最低 1/4 的块
            # 当环境，别把说话峰值算回去。
            silent = sorted(levels)[:max(10, len(levels) // 4)]
        noise, high = noise_of(silent, block_ms)
        gate = start_gate_of(noise, high, gate_percent)
        segs = segment_levels(levels, block_ms, params.with_threshold(gate))
        voiced = [x for s in segs
                  for x in levels[s.start:s.start + s.speech_blocks]]
        print(f"  噪声底 {noise:.4f}  环境峰值 {high:.4f}  "
              f"开口门限 {gate:.4f}  续接 {continue_gate_of(gate, m['continue_ratio']):.4f}")
        print(f"  切出 {len(segs)} 段 "
              f"{[round(s.speech_blocks * block_ms / 1000.0, 1) for s in segs]}")
        if voiced:
            mean = float(np.mean(voiced))
            print(f"  说话段平均能量 {mean:.4f}  信噪比 {snr_db(mean, noise):.1f} dB")
            print(f"  → {judgement(snr_db(mean, noise))}")
        else:
            print("  → 一段都没切出来：门限太高或说话太轻。"
                  "先把门限百分比调低（更灵敏）再跑一次")

        print("\n  回放：不同门限下会切出几段")
        for f in (0.5, 0.75, 1.0, 1.5, 2.0):
            cand = gate * f
            s = segment_levels(levels, block_ms, params.with_threshold(cand))
            mark = "  ← 当前门限" if f == 1.0 else ""
            print(f"  门限 {cand:.4f}（×{f:.2f}）→ {len(s):2d} 段"
                  f" {[round(x.speech_blocks * block_ms / 1000.0, 1) for x in s]}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
