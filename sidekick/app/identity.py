"""把 `config.yaml` 里的 `role.id` / `user.id` 解析成运行期身份字段。

配置只存 id，名字/别名/人设目录/用户档案都在文件里：

  role   → persona/roles/<role_id>/profile.yaml（不写就是空，没有 base 回退）
  user   → persona/users/<user_id>/profile.yaml

解析结果是一个 cfg 副本：`persona`（bot_name/aliases）和 `user`（完整档案）都填好了，
调度器、PromptBuilder、材料、状态端点看到的都是同一份。原始 config 不被修改，
WebUI 保存时仍然只写 id。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

from sidekick.avatar import roles
from sidekick.brain import users
from sidekick.config.spec import DEFAULT_ROLE_ID

#: 配置里没有 role.id 时用 spec 的默认角色设定目录。
DEFAULT_ROLE = DEFAULT_ROLE_ID


@dataclass
class Identity:
    cfg: dict                    # 填好 persona/user 的运行期配置副本
    role_id: str
    role_dir: Path | None = None
    warnings: list[str] = field(default_factory=list)


def resolve(cfg: dict, root, role_id: str | None = None,
            role_dir: Path | None = None) -> Identity:
    rid = str(role_id or (cfg.get("role") or {}).get("id") or "").strip() \
        or DEFAULT_ROLE
    warnings: list[str] = []

    directory = Path(role_dir) if role_dir is not None else roles.role_dir(root, rid)
    role, role_err = roles.read_profile_dir(directory)
    if role_err:
        warnings.append(role_err)

    bot_name = str(role.get("bot_name") or "").strip()
    raw_aliases = role.get("aliases") or []
    aliases = [str(x).strip() for x in raw_aliases if str(x).strip()]

    uid = str((cfg.get("user") or {}).get("id") or users.DEFAULT_ID).strip() \
        or users.DEFAULT_ID
    profile, user_err = users.read(root, uid)
    user_error = user_err
    if user_err:
        warnings.append(user_err)
    elif not profile and not users.profile_path(root, uid).is_file():
        user_error = (
            f"用户人设不存在：persona/users/{uid}/profile.yaml（用户字段为空）")
        warnings.append(user_error)

    role_dir = directory
    out = copy.deepcopy(cfg)
    out["role"] = {"id": rid,
                   "name": str(role.get("name") or "").strip(),
                   "stage": dict(role.get("stage") or {}),
                   "role_dir": str(role_dir)}
    out["stage"] = roles.merge_stage(cfg.get("stage") or {},
                                     role.get("stage"))
    out["persona"] = {"bot_name": bot_name, "aliases": aliases}
    out["user"] = {
        "id": uid,
        "name": str(profile.get("name") or ""),
        "persona": str(profile.get("persona") or ""),
        "error": user_error or "",
    }
    return Identity(cfg=out, role_id=rid, role_dir=role_dir,
                    warnings=warnings)
