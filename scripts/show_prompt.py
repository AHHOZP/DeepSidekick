"""把一次真实请求打出来：`# 预设 …` 摘要 + 请求体（key 不在请求体里）。

逻辑在 `sidekick/preview`，和 WebUI「对话预设 → 查看完整提示词」是同一个入口；
这里只是命令行的薄壳，方便在终端里看。

    python scripts/show_prompt.py
    python scripts/show_prompt.py --live
    python scripts/show_prompt.py --solo
    python scripts/show_prompt.py --role deepseek
    python scripts/show_prompt.py --system      # 只打 messages
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sidekick.preview.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
