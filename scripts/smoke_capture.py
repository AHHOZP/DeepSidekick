"""抓一帧存 PNG —— 用眼睛验，别用代码验。

HDR 泛白、全黑、多屏拼在一起，只有人眼看得出来。

    python scripts/smoke_capture.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidekick import config
from sidekick.input.capture import Capturer


def main() -> int:
    cfg = config.load()
    cap = Capturer(cfg)
    out = ROOT / "state" / "smoke_capture.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    path = cap.save_png(out)
    dt = (time.time() - t0) * 1000

    if not path:
        print("[!!] 抓不到画面：", cap.last_error or "未知原因")
        return 1

    from PIL import Image

    img = Image.open(path)
    print(f"[OK] {path}")
    print(f"     尺寸 {img.width}x{img.height}，耗时 {dt:.0f} ms")
    print("     **现在打开它看一眼**：颜色正不正、是不是只有一块黑、是不是拼了好几个屏")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
