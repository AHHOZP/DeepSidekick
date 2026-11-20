"""世界书包：`persona/games/<id>/` 整个带走（core.md + entries/*.md）。"""
from __future__ import annotations

import re
from pathlib import Path

from sidekick.pack.common import Pack, PackError, make_zip, read_tree

GAMES_REL = ("persona", "games")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def games_dir(root) -> Path:
    return Path(root).joinpath(*GAMES_REL)


def game_dir(root, game_id: str) -> Path:
    return games_dir(root) / str(game_id or "")


def valid_id(game_id: str) -> bool:
    gid = str(game_id or "")
    return (bool(_ID_RE.match(gid)) and gid == gid.strip(" .")
            and gid not in (".", ".."))


def export(root, game_id: str) -> bytes:
    gid = str(game_id or "").strip().replace("\\", "/").split("/")[-1]
    if not valid_id(gid):
        raise PackError(f"游戏 id 不合法：{game_id!r}")
    base = game_dir(root, gid)
    if not base.is_dir():
        raise PackError(f"世界书不存在：{gid}")
    files = read_tree(base)
    if not files:
        raise PackError(f"世界书是空的：{gid}")
    manifest = {"kind": "worldbook", "version": 1, "id": gid,
                "files": len(files)}
    return make_zip(files, manifest)


def import_(root, pack: Pack, *, overwrite: bool = False) -> dict:
    gid = str(pack.manifest.get("id") or "").strip()
    if not valid_id(gid):
        raise PackError(f"包里的游戏 id 不合法：{gid!r}")
    files = {k: v for k, v in pack.files.items() if k != "manifest.json"}
    if not files:
        raise PackError("包里没有世界书文件")
    if not any(k == "core.md" or k.startswith("entries/") for k in files):
        raise PackError("包里没有 core.md 或 entries/")
    base = game_dir(root, gid)
    if base.exists() and any(base.iterdir()) and not overwrite:
        raise PackError(f"世界书已存在：{gid}（换一个名字或勾选覆盖）")
    base.mkdir(parents=True, exist_ok=True)
    for rel, blob in files.items():
        target = base.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    return {"kind": "worldbook", "id": gid, "files": len(files),
            "path": f"persona/games/{gid}"}
