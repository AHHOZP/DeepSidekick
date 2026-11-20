"""截图自检：抓几张，看出去重、抽稀、token 估算对不对。

    python scripts/smoke_frames.py            # 真实抓 6 张（每 0.6 秒一张）
    python scripts/smoke_frames.py --thin     # 只测抽稀算法（不抓屏）

静态桌面会把所有帧合并成 1 张 —— 这是设计，不是 bug：省 2/3 token，
也不会让她把静止画面说成动画。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidekick import config
from sidekick.input.capture import Capturer, Frame, FrameQueue
from sidekick.input.composer import _select_keyframes, plan


def thin_demo() -> None:
    print("关键帧选择（12 张 → 最多 6 张，保留首帧 + 时间桶峰值）")
    frames = [Frame(ts=1000 + i * 5, thumb_jpeg=b"", sig_blocks=bytes([i]))
              for i in range(12)]
    picked = _select_keyframes(frames, 6)
    print("  原始：每 5 秒一张，共 12 张（跨 55 秒）")
    print("  选中（往回数几秒）：",
          [int(frames[-1].ts - f.ts) for f in picked])
    print(f"  → 顺序是旧→新：{picked[-1] is frames[-1]}")
    print()


def real_grab() -> None:
    cfg = config.load()
    cap = Capturer(cfg)
    q = FrameQueue(cfg)
    q.interval = 0.6                    # 自检不用等 5 秒
    print(f"抓 6 张（间隔 {q.interval}s，{cap.width}×{cap.height}）…")
    t0 = time.time()
    for _ in range(6):
        q._tick()
        time.sleep(q.interval)
    st = q.stats()
    print(f"  {st}")
    print(f"  用时 {round(time.time() - t0, 1)}s")

    pending = q.peek()
    frames = q.take()
    pack = plan(frames, cfg)
    print(f"\n  队列里 {pending} 张 → 取走 {len(frames)} 张（取走后剩 {q.peek()}）")
    for f in frames:
        age = round(time.time() - f.ts, 1)
        print(f"    {time.strftime('%H:%M:%S', time.localtime(f.ts))}  {age}s 前  "
              f"{len(f.thumb_jpeg) // 1024} KB 缩略图")
    print(f"\n  这一轮附图 {len(pack.images)} 张，估算 {pack.tokens} tok；"
          f"原始 {pack.source_count} 帧")


def main() -> int:
    thin_demo()
    if "--thin" not in sys.argv[1:]:
        real_grab()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
