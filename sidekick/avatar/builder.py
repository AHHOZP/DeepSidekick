"""把 model3 / 单张图片变成 `Avatar`：推断动作、体检、构造能力清单。

角色设定（名字/别名/人设/排版）不在这里读：那是 `sidekick/avatar/roles.py` 的事，
扫描完成后再由 `AvatarCatalog` 合并进来。
"""
from __future__ import annotations

import copy
from pathlib import Path

from sidekick.avatar.model import Avatar
from sidekick.avatar.scan import digest, guess_id, model_id, read_json, rel
from sidekick.avatar.validation import validate, validate_spine

_MOTION_SUFFIX = ".motion3.json"
_EXPR_SUFFIX = ".exp3.json"
#: 能当形象用的静态图：SDL_image 解得了的常见格式 + 自带光栅化的 svg（见 `sidekick/desktop/image.py`）。
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".svg"}


def _ids_of(group: dict) -> list[str]:
    return [str(x) for x in (group.get("Ids") or []) if str(x).strip()]


def _fill_group(groups: list, by_name: dict, name: str, ids: list[str]) -> bool:
    """组不存在 → 补；组存在但 Ids 为空 → 也补。返回是否改动。"""
    cur = by_name.get(name.lower())
    if cur is None:
        g = {"Target": "Parameter", "Name": name, "Ids": list(ids)}
        groups.append(g)
        by_name[name.lower()] = g
        return True
    if not _ids_of(cur) and ids:
        cur["Ids"] = list(ids)
        return True
    return False


def _infer_groups(root: Path, model3: dict) -> tuple[dict, list[dict], list[str]]:
    """读 model3 的 Motions/Expressions/Groups；缺失的从文件目录补齐。

    返回 (motions, expressions, lipSync 参数)。函数会原地补全 model3。
    """
    refs = model3.setdefault("FileReferences", {})
    motions = refs.get("Motions")
    if not isinstance(motions, dict):
        motions = {}
    expressions = refs.get("Expressions")
    if not isinstance(expressions, list):
        expressions = []

    # 有些模型的 model3 只声明 Moc/Textures，动作和表情放在 Animation/、Exp3/ 里。
    if not motions:
        for f in sorted(root.rglob("*" + _MOTION_SUFFIX)):
            if not f.is_file():
                continue
            rel_path = rel(f, root)
            stem = f.name[:-len(_MOTION_SUFFIX)]
            group = "idle" if stem.lower() in ("idle", "wait", "stand", "待机") else stem
            base = group
            n = 2
            while group in motions:
                group = f"{base}-{n}"
                n += 1
            motions[group] = [{
                "File": rel_path,
                "FadeInTime": 0.35,
                "FadeOutTime": 0.5,
            }]

    known_files = {str(x.get("File") or "").replace("\\", "/") for x in expressions
                   if isinstance(x, dict)}
    known_names = {str(x.get("Name") or "") for x in expressions
                   if isinstance(x, dict)}
    for f in sorted(root.rglob("*" + _EXPR_SUFFIX)):
        if not f.is_file():
            continue
        rel_path = rel(f, root)
        if rel_path in known_files:
            continue
        name = f.name[:-len(_EXPR_SUFFIX)] or "Expression"
        base = name
        n = 2
        while name in known_names:
            name = f"{base}-{n}"
            n += 1
        expressions.append({"Name": name, "File": rel_path})
        known_files.add(rel_path)
        known_names.add(name)

    if motions:
        refs["Motions"] = motions
    if expressions:
        refs["Expressions"] = expressions

    groups = model3.setdefault("Groups", [])
    if not isinstance(groups, list):
        groups = []
        model3["Groups"] = groups
    by_name = {str(g.get("Name") or "").lower(): g for g in groups if isinstance(g, dict)}

    # LipSync 不补：Cubism 默认就认 ParamMouthOpenY，前端也兜了一层；
    # 补进 model3 只是让"合成后的清单"多一段假数据。它仍作为能力清单上报。
    lip: list[str] = []
    for g in groups:
        if str(g.get("Name") or "").lower() == "lipsync":
            lip = _ids_of(g) if isinstance(g, dict) else []
    if not lip:
        lip = ["ParamMouthOpenY"]
    # 眨眼必须补：引擎的 getEyeBlinkParameters() 只读 Groups，不会自己找参数。
    blink: list[str] = []
    for g in groups:
        if str(g.get("Name") or "").lower() == "eyeblink":
            blink = _ids_of(g) if isinstance(g, dict) else []
    if not blink:
        blink = ["ParamEyeLOpen", "ParamEyeROpen"]
    _fill_group(groups, by_name, "EyeBlink", blink)
    return motions, expressions, lip


def _heuristic_idle(motions: dict) -> str:
    """挑待机动作：先精确匹配，再找名字里含 idle 的（多个取第一个）。

    游戏拆包的待机名千奇百怪（Idle1 / idle_loop / loop_idle），只认精确值会让
    桌宠停在第一帧；含 idle 就认已经够用，不再往上叠启发式。
    """
    names = [str(name) for name in motions]
    for name in names:
        if name.lower() in ("idle", "wait", "stand", "待机"):
            return name
    for name in names:
        if "idle" in name.lower():
            return name
    return ""


def idle_of(names: list[str]) -> str:
    """从动画/动作名里挑待机；挑不到返回空串（调用方自己决定兜底）。"""
    return _heuristic_idle(dict.fromkeys(names))


def _action_items(motions: dict) -> list[dict]:
    """一个 motion group 就是一个动作标签，组名即标签。"""
    return [{"tag": str(name), "group": str(name)} for name in motions]


def _expression_items(expressions: list[dict]) -> list[dict]:
    out = []
    for x in expressions:
        if not isinstance(x, dict):
            continue
        name = str(x.get("Name") or "").strip()
        if not name and x.get("File"):
            name = Path(str(x["File"])).stem
        if name:
            out.append({"tag": name, "name": name})
    return out


def build_avatar(root: Path, model3_path: Path) -> Avatar:
    model3 = read_json(model3_path)
    avatar_id = model_id(root, model3_path, model3 or {})
    if not model3:
        return Avatar(
            id=avatar_id, name=root.name, kind="live2d", root=root,
            entry=rel(model3_path, root),
            broken=["model3.json 解析失败或为空，无法读取动作/表情"])
    before = copy.deepcopy(model3)
    motions, expressions, lip = _infer_groups(root, model3)
    auto = model3 != before
    actions = _action_items(motions)
    exprs = _expression_items(expressions)
    # motion= 与 expression= 是两个命名空间，同名不需要互斥。entry
    idle = _heuristic_idle(motions)

    broken, warns = validate(root, model3)
    caps = {
        "actions": actions,
        "expressions": exprs,
        "idle": idle,
        "lipSync": lip,
    }
    return Avatar(
        id=avatar_id,
        name=root.name,
        kind="live2d",
        root=root,
        entry=rel(model3_path, root),
        capabilities=caps,
        model3=model3 if auto else None,
        model_version=int(model3_path.stat().st_mtime) if model3_path.exists() else 0,
        broken=broken,
        warnings=warns,
        idle=idle,
    )


def build_spine(root: Path, entry: Path) -> Avatar:
    """Spine 形象：扫描期只体检，动画名等这个形象被用到时再读（见 catalog）。

    `.json` 骨架顺手读顶层 `animations`（体检本来就要解析它）；`.skel` 是二进制，
    名字得问原生库，所以先留空。
    """
    broken, warns = validate_spine(root, entry)
    names = _json_animation_names(entry) if entry.suffix.lower() == ".json" else []
    idle = idle_of(names)
    capabilities = {
        "actions": [{"tag": name, "group": name} for name in names],
        "expressions": [],
        "idle": idle,
        "lipSync": [],
    }
    return Avatar(
        id=spine_id(root, entry),
        name=root.name,
        kind="spine",
        root=root,
        entry=rel(entry, root),
        capabilities=capabilities,
        model_version=int(entry.stat().st_mtime) if entry.exists() else 0,
        broken=broken,
        warnings=warns,
        idle=idle,
    )


def _json_animation_names(entry: Path) -> list[str]:
    """JSON 骨架的动画名（保持文件里的顺序，不排序、不去重）。"""
    animations = read_json(entry).get("animations")
    return [str(k) for k in animations] if isinstance(animations, dict) else []


def spine_id(root: Path, entry: Path) -> str:
    """Spine 角色 id：骨架内容指纹，和 Live2D 一样与文件夹名无关。"""
    return digest(entry) or guess_id(root)


def build_image(root: Path, path: Path) -> Avatar:
    """单张静态图（见 `IMAGE_SUFFIXES`）：只显示，不提供动作能力。"""
    fingerprint = digest(path)
    return Avatar(
        id=fingerprint or f"broken-{path.stem}",
        name=path.stem,
        kind="image",
        root=root,
        entry=rel(path, root),
        capabilities={"actions": [], "expressions": [], "idle": "",
                      "lipSync": []},
        model_version=int(path.stat().st_mtime) if path.exists() else 0,
        broken=[] if fingerprint else ["图片读取失败，无法计算内容指纹"],
    )
