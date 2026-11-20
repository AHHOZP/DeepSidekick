"""麦克风自检：列出输入设备，按"当前配置"实跑一遍分段，看能不能切出段。

    python scripts/smoke_mic.py            # 用 Windows 默认输入设备跑 8 秒
    python scripts/smoke_mic.py --list      # 只列设备/当前默认
    python scripts/smoke_mic.py --seconds 5

跑的时候**对着麦克风说话**。看到「切出 1 段」就说明麦克风到分段这一环是好的；
一直 0 段、峰值又接近 0，就是设备/静音/占用的问题，跟程序无关。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidekick import config
from sidekick.input.mic import Mic


def list_devices() -> None:
    import sounddevice as sd

    try:
        d = sd.query_devices(kind="input")
        print(f"默认输入：{d['name']}（{d['max_input_channels']} 通道）")
    except Exception as e:
        print(f"默认输入查不到：{e}")
    print()
    print(f"{'#':>3}  {'通道':>4}  {'默认采样率':>10}  设备")
    for i, x in enumerate(sd.query_devices()):
        if x["max_input_channels"] > 0:
            print(f"{i:>3}  {x['max_input_channels']:>4}  "
                  f"{int(x['default_samplerate']):>10}  {x['name']}")


def main() -> int:
    argv = sys.argv[1:]
    if "--list" in argv:
        list_devices()
        return 0

    cfg = config.load()
    mic_cfg = dict(cfg.get("mic") or {})
    seconds = float(argv[argv.index("--seconds") + 1]) if "--seconds" in argv else 8.0
    try:
        import sounddevice as sd

        dev_name = str(sd.query_devices(kind="input").get("name") or "默认输入")
    except Exception:
        dev_name = "默认输入"

    print(f"设备=系统默认（{dev_name}）  形态={mic_cfg.get('mode')}  "
          f"门限百分比={mic_cfg.get('gate_percent')}%  "
          f"silence_ms={mic_cfg.get('silence_ms')}  "
          f"min_speech_ms={mic_cfg.get('min_speech_ms')}")
    print(f"开始听 {seconds:.0f} 秒 —— 现在对着麦克风说话\n")

    got: list[float] = []
    mic = Mic(cfg, on_segment=lambda pcm, sr: got.append(len(pcm) / sr))
    mic.start()
    t0 = time.time()
    peak_now = 0.0
    while time.time() - t0 < seconds:
        time.sleep(0.2)
        peak_now = max(peak_now, mic.level)

    # 关键：再说一会儿。最后一段要等你停嘴 silence_ms 才会被切出来，
    # 说完立刻关门的话，最后那句话永远不会被送出（第一版就栽在这）。
    drain = float(mic_cfg.get("silence_ms") or 1500) / 1000.0 + 1.0
    print(f"（收尾：再听 {drain:.1f} 秒，让最后一段话切出来）")
    t1 = time.time()
    while time.time() - t1 < drain:
        time.sleep(0.2)
        peak_now = max(peak_now, mic.level)
    mic.close()
    time.sleep(0.3)

    # 落一份到文件：跑完不用复制粘贴，直接读
    import json
    report = {
        "at": time.strftime("%H:%M:%S"),
        "device": "system-default",
        "seconds": seconds,
        "peak": round(mic.peak, 5),
        "level_now": round(mic.level, 5),
        "sent": mic.sent,
        "segments_s": [round(x, 2) for x in got],
        "dropped_short": mic.dropped_short,
        "dropped_s": mic.dropped_s,
        "error": mic.error,
        "mode": mic.mode,
        "noise_floor": round(mic.noise, 5),
        "noise_peak": round(mic.noise_high, 5),
        "gate": round(mic.gate, 5),
        "gate_continue": round(mic.gate_continue, 5),
        "cfg": {k: mic_cfg.get(k) for k in ("mode", "gate_percent", "silence_ms",
                                            "min_speech_ms", "max_segment_s")},
    }
    out = ROOT / "state" / "mic_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"（结果已写到 {out}）")

    print(f"\n这 {seconds:.0f} 秒里：")
    print(f"  峰值 RMS        {mic.peak:.4f}")
    print(f"  噪声底 / 峰值 / 开口 / 续接   "
          f"{mic.noise:.4f} / {mic.noise_high:.4f} / "
          f"{mic.gate:.4f} / {mic.gate_continue:.4f}   （形态 {mic.mode}）")
    print(f"  切出的段        {mic.sent} 段 {[round(x, 2) for x in got]}")
    print(f"  太短丢掉        {mic.dropped_short}")
    print(f"  错误            {mic.error}")
    if mic.sent == 0 and mic.peak < 0.01:
        print("\n→ 一直没信号。按这个顺序查：")
        print("  1. 耳机上的物理静音键 / 麦克风杆有没有翻起来（有些耳麦翻上去就静音）")
        print("  2. Windows 设置 → 隐私 → 麦克风 → 允许桌面应用访问")
        print("  3. 在 Windows 设置 → 系统 → 声音 → 输入 里选对默认设备")
        print("  4. 直播姬 / OBS / Discord 是不是正在占用它")
        return 2
    if mic.sent == 0:
        print("\n→ 有信号但没切出段：多半是说话时长不足 min_speech_ms，或者停顿没超过 silence_ms")
        return 2
    print("\n✓ 麦克风到分段这一环正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
