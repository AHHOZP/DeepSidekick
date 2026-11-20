"""用户人设：`persona/users/<id>/profile.yaml`。

和角色资料一样是"id 在 config、内容在专门文件夹"：

  name       用户名字（材料标签、默认称呼都用这个）
  persona    用户自己的设定文本（称呼、代词、偏好都写在这里）

`persona/users/` 是个人素材，不入库；没有档案就是空，不自动创建。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from sidekick.config.spec import DEFAULTS
from sidekick.core import atomic

USERS_REL = ("persona", "users")
PROFILE = "profile.yaml"
#: 默认用户 id 也只在 spec 里写一次。
DEFAULT_ID = str((DEFAULTS.get("user") or {}).get("id") or "")
HEADER = "# 用户人设：这是坐在你旁边那个人自己的资料，不是角色的人设。\n"
_BAD_CHARS = set('\\/:*?"<>|')


def users_dir(root) -> Path:
    return Path(root).joinpath(*USERS_REL)


def profile_path(root, user_id: str) -> Path:
    return users_dir(root) / str(user_id or "") / PROFILE


def valid_id(user_id: str) -> bool:
    """文件夹名规则：不能带路径分隔符/Windows 保留字符，不能是 . / ..。"""
    s = str(user_id or "")
    if not s or len(s) > 80 or s in (".", ".."):
        return False
    if any(ch in _BAD_CHARS or ord(ch) < 32 for ch in s):
        return False
    return s == s.strip(" .")


def normalize(data: dict) -> dict:
    out: dict = {}
    name = data.get("name")
    if isinstance(name, str) and name.strip():
        out["name"] = name.strip()
    persona = data.get("persona")
    if isinstance(persona, str) and persona.strip():
        out["persona"] = persona.strip()
    return out


def read(root, user_id: str) -> tuple[dict, str | None]:
    path = profile_path(root, user_id)
    if not path.is_file():
        return {}, None
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as e:
        return {}, f"{path.name} 读取失败：{type(e).__name__}: {e}"
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return {}, f"{path.name} 解析失败：{e}"
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return {}, f"{path.name} 顶层必须是字典"
    return normalize(data), None


def write(root, user_id: str, data: dict) -> None:
    if not valid_id(user_id):
        raise ValueError("用户 id 不合法")
    path = profile_path(root, user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, HEADER + yaml.safe_dump(
        normalize(data or {}), allow_unicode=True, sort_keys=False, width=1000))


def list_ids(root) -> list[str]:
    base = users_dir(root)
    if not base.is_dir():
        return []
    return sorted(p.parent.name for p in base.glob(f"*/{PROFILE}")
                  if p.is_file())
