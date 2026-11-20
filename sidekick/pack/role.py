"""角色包：人设 + 动作规则 + 自主驱动配置 + 形象素材，一个 zip 一键可用。

包结构：

    manifest.json
    role/profile.yaml
    role/persona.md
    role/actions.md
    role/motion_profile.json
    avatar/<模型文件夹>/…          Live2D / Spine：整个模型文件夹
    avatar/<图片文件名>            单张图片

导入时形象落到 `models/`，重扫一次取到形象 id，再把角色文件写进
`persona/roles/<形象 id>/`——角色 id 就是形象的内容指纹，这样导入完直接能选。
包里没有形象时（只有人设的角色目录），按 manifest 的 id 落盘。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from sidekick.avatar import roles as roles_mod
from sidekick.pack.common import Pack, PackError, make_zip, read_tree, safe_folder

ROLE_FILES = ("profile.yaml", "persona.md", "actions.md", "motion_profile.json")
AVATAR = "avatar"


def valid_id(value: str) -> bool:
    """角色目录名：允许中文等 Unicode，挡路径分隔符/保留名。"""
    try:
        safe_folder(value)
    except PackError:
        return False
    return True


def _role_files(directory) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for name in ROLE_FILES:
        path = Path(directory) / name
        if path.is_file():
            out[f"role/{name}"] = path.read_bytes()
    return out


def export(root, catalog, role_id: str) -> bytes:
    rid = str(role_id or "").strip()
    avatar = catalog.get(rid) if catalog is not None else None
    if avatar is not None:
        rid = avatar.role_id or avatar.id
    if not valid_id(rid):
        raise PackError(f"角色 id 不合法：{role_id!r}")
    directory = roles_mod.active_dir(avatar, root) if avatar is not None else (
        roles_mod.role_dir(root, rid))
    profile, err = roles_mod.read_profile_dir(directory)
    if err:
        raise PackError(err)
    files = _role_files(directory)
    if not files:
        raise PackError(f"这个角色还没有任何设定文件：{rid}")

    avatar_meta: dict = {}
    if avatar is not None:
        folder = avatar.name
        if avatar.kind == "image":
            path = Path(avatar.root) / avatar.entry
            if path.is_file():
                files[f"{AVATAR}/{path.name}"] = path.read_bytes()
                avatar_meta = {"kind": "image", "folder": "", "file": path.name}
        else:
            tree = read_tree(Path(avatar.root))
            for rel, blob in tree.items():
                files[f"{AVATAR}/{folder}/{rel}"] = blob
            avatar_meta = {"kind": avatar.kind, "folder": folder,
                           "entry": avatar.entry, "files": len(tree)}

    manifest = {
        "kind": "role", "version": 1, "id": rid,
        "name": str(profile.get("name") or rid),
        "avatar": avatar_meta,
    }
    return make_zip(files, manifest)


def _model_dir(root) -> Path:
    return Path(root) / "models"


def _place_avatar(root, pack: Pack, *, overwrite: bool = False) -> tuple[str, str]:
    """把形象文件落到 models/，返回 (model_folder, kind)；没有形象返回 ("", "")。"""
    meta = pack.manifest.get("avatar") or {}
    if not isinstance(meta, dict) or not meta:
        return "", ""
    kind = str(meta.get("kind") or "")
    blobs = {k: v for k, v in pack.files.items() if k.startswith(f"{AVATAR}/")}
    if not blobs:
        return "", ""
    models = _model_dir(root)

    if kind == "image":
        name = Path(str(meta.get("file") or "")).name
        if not name:
            raise PackError("角色包缺少形象文件名")
        safe_folder(Path(name).stem)
        target = models / "images" / name
        if target.exists() and not overwrite:
            raise PackError(f"图片已存在：models/images/{name}（勾选覆盖才能替换）")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(next(iter(blobs.values())))
        return "", "image"

    folder = str(meta.get("folder") or "").strip()
    try:
        safe_folder(folder)
    except PackError as e:
        raise PackError(f"角色包里的形象文件夹不合法：{e}") from e
    base = models / folder
    if base.exists() and any(base.iterdir()):
        # 同一套模型已经在盘上：不重复放，直接用现有的。
        return folder, kind
    base.mkdir(parents=True, exist_ok=True)
    for rel, blob in blobs.items():
        parts = rel.split("/")
        if len(parts) < 3 or parts[1] != folder:
            raise PackError(f"角色包里的形象路径不合法：{rel}")
        target = base.joinpath(*parts[2:])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    return folder, kind


def _predicted_role_id(pack: Pack) -> str:
    meta = pack.manifest.get("avatar") or {}
    if not isinstance(meta, dict) or not meta:
        return str(pack.manifest.get("id") or "").strip()
    if str(meta.get("kind") or "") == "image":
        return Path(str(meta.get("file") or "")).stem
    return str(meta.get("folder") or "").strip()


def import_(root, catalog, pack: Pack, *, overwrite: bool = False) -> dict:
    rid = str(pack.manifest.get("id") or "").strip()
    if not valid_id(rid):
        raise PackError(f"包里的角色 id 不合法：{rid!r}")
    role_blobs = {k[len("role/"):]: v for k, v in pack.files.items()
                  if k.startswith("role/")}
    if not role_blobs:
        raise PackError("包里没有角色设定文件")

    # 先校验目标再落盘：被拒绝的导入不能把形象文件留在 models/。
    predicted = _predicted_role_id(pack) or rid
    try:
        safe_folder(predicted)
    except PackError as e:
        raise PackError(f"包里的角色 id 不合法：{e}") from e
    pre_target = roles_mod.role_dir(root, predicted)
    if pre_target.exists() and any(pre_target.iterdir()) and not overwrite:
        raise PackError(f"角色设定已存在：{predicted}（勾选覆盖才能替换）")

    folder, _kind = _place_avatar(root, pack, overwrite=overwrite)
    avatar_id = ""
    if folder:
        catalog.rescan()
        for avatar in catalog.discover(refresh=True):
            if avatar.name == folder:
                avatar_id = avatar.id
                break
        if not avatar_id:
            raise PackError(f"形象放进了 models/{folder}，但扫描不到可用的模型")
    target_id = ""
    target = None
    if avatar_id:
        found = catalog.get(avatar_id)
        target_id = (found.role_id if found is not None else predicted) or predicted
        target = roles_mod.role_dir(root, target_id)
    if target is None:
        target_id = rid
        target = roles_mod.role_dir(root, target_id)
    if target.exists() and any(target.iterdir()):
        if not overwrite:
            raise PackError(f"角色设定已存在：{target_id}（勾选覆盖才能替换）")
        # 覆盖 = 整目录替换，包外的旧文件不能留下来继续被读取。
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    for name, blob in role_blobs.items():
        if name not in ROLE_FILES:
            continue
        target.joinpath(name).write_bytes(blob)
    if catalog is not None:
        catalog.invalidate()
    return {"kind": "role", "id": target_id, "avatar": folder,
            "files": len(role_blobs),
            "path": f"persona/roles/{target_id}"}
