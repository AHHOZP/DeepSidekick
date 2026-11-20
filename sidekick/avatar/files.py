"""`actions.md`（动作规则）的唯一读写入口。

动作规则属于**角色设定**，跟人设文本一起放在 `persona/roles/<role_id>/`；
形象扫描、动作提取和 WebUI 编辑都走这里，避免几处各自拼路径/读文件。
"""
from __future__ import annotations

from pathlib import Path

from sidekick.core import atomic

RULES_MAX = 2000
_FILE = "actions.md"
last_error: str | None = None
last_truncated = False


def path(avatar) -> Path:
    base = getattr(avatar, "role_dir", None)
    return Path(base) / _FILE if base is not None else Path(_FILE)


def read(avatar) -> str:
    global last_error, last_truncated
    last_error = None
    last_truncated = False
    if getattr(avatar, "role_dir", None) is None:
        return ""
    try:
        raw = path(avatar).read_text(encoding="utf-8-sig")
        last_truncated = len(raw) > RULES_MAX
        if last_truncated:
            return raw[:RULES_MAX].strip()
        return raw.strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as e:
        last_error = f"{type(e).__name__}: {e}"
        return ""


def write(avatar, text: str) -> None:
    global last_error, last_truncated
    last_error = None
    last_truncated = False
    target = path(avatar)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(target, str(text).replace("\r\n", "\n").strip() + "\n")


def clear(avatar) -> None:
    path(avatar).unlink(missing_ok=True)
