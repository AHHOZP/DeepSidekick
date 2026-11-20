"""角色设定：`persona/roles/<角色 id>/` 的读写。

一个角色 = 形象（`models/` 里那套模型）+ 角色设定（这里的 `profile.yaml`、
`persona.md`、`actions.md`、`motion_profile.json`）。角色 id 是
`persona/roles/<role_id>/` 的目录名，默认取模型文件夹名；绑定关系写在
`profile.yaml` 的 `model` 字段，所以模型文件夹改名会自然变成新角色，
角色目录改名则仍能靠 `model` 找回模型。

`profile.yaml` 字段（空/文件不存在就是空，没有全局人设回退）：

  model      绑定的模型文件夹名（扫描时用它找回角色；不写就按目录名）
  model_digest 模型内容指纹；同名模型靠它区分角色目录
  name       显示名；空 = role.id
  bot_name   prompt 里的 {{bot}}
  aliases    弹幕"叫你"的判定词
  stage      这套形象自己的外观（cps / bubble）

角色人设只有 `persona.md` 一个文件，只回答「我是谁」；场合、开口、材料读法、
输出契约、去八股、直播追加都在对话预设里。动作规则 `actions.md` 缺文件就是空。
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

from sidekick.core import atomic

ROLES_REL = ("persona", "roles")
PROFILE = "profile.yaml"
HEADER = "# 角色设定：只写这个角色自己的值；不写就是空，不从别处继承。\n"

STAGE_FLOAT = ("cps",)
STAGE_BUBBLE = ("gap", "max_width")


def roles_dir(root) -> Path:
    return Path(root).joinpath(*ROLES_REL)


def role_dir(root, role_id: str) -> Path:
    return roles_dir(root) / str(role_id or "")


def profile_path(root, role_id: str) -> Path:
    return role_dir(root, role_id) / PROFILE


def _read(path: Path) -> tuple[dict, str | None]:
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


def _text(data: dict, key: str) -> str:
    value = data.get(key)
    return str(value).strip() if isinstance(value, str) else ""


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_stage(raw) -> dict:
    """只留角色可调的外观字段；数值非法就当没写。"""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key in STAGE_FLOAT:
        value = _float(raw.get(key))
        if value is not None:
            out[key] = value
    bubble = raw.get("bubble")
    if isinstance(bubble, dict):
        clean = {key: value for key in STAGE_BUBBLE
                 if (value := _float(bubble.get(key))) is not None}
        for key in ("auto_gap",):
            if isinstance(bubble.get(key), bool):
                clean[key] = bubble[key]
        if clean:
            out["bubble"] = clean
    return out


def normalize(data: dict) -> dict:
    """只留已知字段并做类型归一；未知字段忽略。"""
    out: dict = {}
    for key in ("model", "model_digest", "name", "bot_name"):
        value = _text(data, key)
        if value:
            out[key] = value
    aliases = data.get("aliases")
    if isinstance(aliases, list):
        clean = [str(x).strip() for x in aliases if str(x).strip()]
        if clean:
            out["aliases"] = clean
    stage = normalize_stage(data.get("stage"))
    if stage:
        out["stage"] = stage
    return out


def read_profile(root, role_id: str) -> tuple[dict, str | None]:
    return _read(profile_path(root, role_id))


def read_profile_dir(directory) -> tuple[dict, str | None]:
    """按实际目录读（兼容旧的内容指纹目录）。"""
    return _read(Path(directory) / PROFILE)


def model_index(root) -> dict[str, str]:
    """模型文件夹名 → 角色目录名。

    绑定关系写在角色自己的 `profile.yaml`：目录改名不影响绑定；模型文件夹
    改名后旧 profile 匹配不到，自然变成一个新角色。旧 profile 没有 model
    字段时退回“目录名就是模型名”的约定。
    """
    base = roles_dir(root)
    out: dict[str, str] = {}
    if not base.is_dir():
        return out
    for directory in sorted(base.iterdir()):
        if not directory.is_dir():
            continue
        profile, _error = read_profile_dir(directory)
        model = _text(profile, "model") or directory.name
        if model and model not in out:
            out[model] = directory.name
    return out


def _write_file(path: Path, data: dict) -> None:
    clean = normalize(data or {})
    if not clean:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, HEADER + yaml.safe_dump(
        clean, allow_unicode=True, sort_keys=False, width=1000))


def write_profile_dir(directory, data: dict) -> None:
    _write_file(Path(directory) / PROFILE, data)


def profile_matches(root, role_id: str, avatar) -> bool:
    """角色目录是否属于这个模型；没有 model_digest 的旧文件按 model 名兼容。"""
    if avatar is None:
        return False
    profile, error = read_profile_dir(role_dir(root, role_id))
    if error:
        return False
    model = _text(profile, "model")
    if model and model != str(getattr(avatar, "name", "") or ""):
        return False
    digest = _text(profile, "model_digest")
    return not digest or digest == str(getattr(avatar, "id", "") or "")


def active_dir(avatar, root) -> Path:
    """返回已有内容的那份角色目录；新目录为空时回退旧的内容指纹目录。"""
    rid = str(getattr(avatar, "role_id", "") or getattr(avatar, "id", "")
              or getattr(avatar, "name", ""))
    primary = role_dir(root, rid)
    if primary.is_dir() and any(primary.iterdir()):
        return primary
    legacy = role_dir(root, getattr(avatar, "id", ""))
    if legacy.is_dir() and any(legacy.iterdir()):
        profile, _error = read_profile_dir(legacy)
        model = _text(profile, "model")
        # 旧 profile 没写 model 时继续按内容指纹兜底；写了 model 但和当前
        # 模型文件夹对不上，说明模型已改名，旧目录必须自然失效。
        if not model or model == str(getattr(avatar, "name", "") or ""):
            return legacy
    return primary


def ensure_dir(avatar, root) -> Path:
    """返回这个角色实际使用的目录，并把旧指纹目录迁到 role_id 名下。

    旧版本用模型内容指纹做目录名；第一次写文件时顺手改名，避免用户
    已经写好的角色设定失联。改名失败（被占用/跨盘）就继续用旧目录。
    """
    rid = str(getattr(avatar, "role_id", "") or getattr(avatar, "id", "")
              or getattr(avatar, "name", ""))
    primary = role_dir(root, rid)
    old = active_dir(avatar, root)
    if old != primary and old.is_dir():
        try:
            # 空目录不是“已有新目录”，不能让它把旧设定顶掉。
            if primary.is_dir() and not any(primary.iterdir()):
                primary.rmdir()
        except OSError:
            pass
        if not primary.exists():
            try:
                old.rename(primary)
            except OSError:
                pass
        if primary.is_dir():
            avatar.role_dir = primary
        else:
            avatar.role_dir = old
    else:
        primary.mkdir(parents=True, exist_ok=True)
        avatar.role_dir = primary
    bind_model(Path(avatar.role_dir), str(getattr(avatar, "name", "") or ""),
               str(getattr(avatar, "id", "") or ""))
    return Path(avatar.role_dir)


def bind_model(directory, model: str, digest: str = "") -> None:
    """第一次写角色文件时把 model/内容指纹补进 profile，之后改名也能找回。"""
    model = str(model or "").strip()
    digest = str(digest or "").strip()
    if not model:
        return
    profile, error = read_profile_dir(directory)
    if error:
        return
    old_digest = _text(profile, "model_digest")
    if old_digest and digest and old_digest != digest:
        # 目录已被另一份同名模型占用；宁可保留旧绑定，也不覆盖别人的
        # 人设/动作配置。
        return
    if (_text(profile, "model") == model
            and (not digest or old_digest == digest)):
        return
    profile["model"] = model
    if digest:
        profile["model_digest"] = digest
    try:
        write_profile_dir(directory, profile)
    except OSError:
        # 绑定写不进去不阻塞人设/规则保存；下一次保存再试。
        pass


def apply(avatar, root) -> None:
    """把角色设定搬到 `Avatar` 上（扫描时调用一次；不碰模型能力）。"""
    directory = active_dir(avatar, root)
    role, err = read_profile_dir(directory)
    avatar.role_dir = directory
    avatar.profile_error = err
    avatar.display_name = _text(role, "name")
    stage = role.get("stage")
    avatar.stage = dict(stage) if isinstance(stage, dict) else {}


def merge_stage(base: dict, role_stage: dict | None) -> dict:
    """角色外观覆盖到全局默认上；enabled/mode/host/port 不在角色设定里。"""
    out = copy.deepcopy(base or {})
    role_stage = role_stage or {}
    for key in STAGE_FLOAT:
        if key in role_stage:
            out[key] = role_stage[key]
    for group, keys in (("bubble", STAGE_BUBBLE),):
        current = out.get(group)
        current = dict(current) if isinstance(current, dict) else {}
        for key in keys:
            if key in (role_stage.get(group) or {}):
                current[key] = role_stage[group][key]
        if "auto_gap" in (role_stage.get(group) or {}):
            current["auto_gap"] = role_stage[group]["auto_gap"]
        out[group] = current
    return out


# ── 人设文本（只读当前角色目录）──────────────────────
def read_text(directory: Path, name: str) -> str:
    p = directory / name
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""


def write_text(directory, name: str, text: str) -> None:
    """按实际目录写角色文件；空内容 = 删除。"""
    path = Path(directory) / name
    text = str(text or "")
    if not text.strip():
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, text)
