"""motion_profile.json 的原子读写；只放在角色目录，不写模型目录。"""
from __future__ import annotations

import json
from pathlib import Path

from sidekick.avatar.autonomy.schema import MAX_BYTES
from sidekick.core import atomic

PROFILE_NAME = "motion_profile.json"


class ProfileError(ValueError):
    """可展示给 WebUI 的 profile 读写错误。"""


def path(avatar) -> Path:
    role_dir = getattr(avatar, "role_dir", None)
    if role_dir is None:
        raise ProfileError("角色没有 role_dir，无法保存自主驱动配置")
    return Path(role_dir) / PROFILE_NAME


def load(avatar) -> tuple[dict | None, str | None]:
    """读 profile；不存在返回 (None, None)，坏文件返回 (None, 原因)。"""
    try:
        target = path(avatar)
    except ProfileError as e:
        return None, str(e)
    try:
        if not target.is_file():
            return None, None
        if target.stat().st_size > MAX_BYTES:
            return None, f"profile 超过 {MAX_BYTES} 字节"
        text = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as e:
        return None, f"profile 读取失败：{type(e).__name__}: {e}"
    try:
        data = json.loads(text)
    except ValueError as e:
        return None, f"profile 不是 JSON：{e}"
    if not isinstance(data, dict):
        return None, "profile 顶层不是对象"
    return data, None


def save(avatar, data: dict) -> Path:
    if not isinstance(data, dict):
        raise ProfileError("profile 顶层必须是对象")
    target = path(avatar)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ProfileError(f"profile 超过 {MAX_BYTES} 字节")
    try:
        return atomic.write_text(target, text)
    except OSError as e:
        raise ProfileError(f"profile 写不进去：{e}") from e


def delete(avatar) -> None:
    try:
        path(avatar).unlink(missing_ok=True)
    except OSError as e:
        raise ProfileError(f"profile 删不掉：{e}") from e
