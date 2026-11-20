"""`python -m sidekick.preview`：把一次真实请求的 messages 和请求体打到标准输出。

    python -m sidekick.preview                 # 用当前配置（含直播模式）
    python -m sidekick.preview --live          # 强制按直播模式拼
    python -m sidekick.preview --solo          # 强制按单人陪玩拼
    python -m sidekick.preview --role deepseek # 只看某份角色设定
    python -m sidekick.preview --system        # 只打 messages，不打请求体
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _flag(argv: list[str], name: str) -> str:
    if name not in argv:
        return ""
    at = argv.index(name)
    return argv[at + 1] if at + 1 < len(argv) else ""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    live = True if "--live" in argv else (False if "--solo" in argv else None)
    from sidekick.preview import render

    try:
        text = asyncio.run(render(
            ROOT, role=_flag(argv, "--role"), live=live,
            system_only="--system" in argv))
    except Exception as e:  # noqa: BLE001 - CLI 边界，把原因打给用户
        print(f"预演失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
