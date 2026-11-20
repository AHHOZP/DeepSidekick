"""预设包：`persona/presets/<id>/preset.yaml` 整个带走。"""
from __future__ import annotations

from sidekick.brain import preset as preset_mod
from sidekick.pack.common import Pack, PackError, make_zip


def export(root, preset_id: str) -> bytes:
    pid = str(preset_id or "").strip()
    if not preset_mod.valid_id(pid):
        raise PackError(f"预设名不合法：{pid!r}")
    path = preset_mod.preset_path(root, pid)
    if not path.is_file():
        raise PackError(f"预设不存在：{pid}")
    got = preset_mod.read(root, pid)
    manifest = {"kind": "preset", "version": 1, "id": pid,
                "name": got.name or pid, "desc": got.desc}
    return make_zip({preset_mod.FILE: path.read_bytes()}, manifest)


def import_(root, pack: Pack, *, overwrite: bool = False) -> dict:
    pid = str(pack.manifest.get("id") or "").strip()
    if not preset_mod.valid_id(pid):
        raise PackError(f"包里的预设名不合法：{pid!r}")
    blob = pack.files.get(preset_mod.FILE)
    if blob is None:
        raise PackError("包里没有 preset.yaml")
    target = preset_mod.preset_path(root, pid)
    if target.exists() and not overwrite:
        raise PackError(f"预设已存在：{pid}（换一个名字或勾选覆盖）")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)
    got = preset_mod.read(root, pid)
    return {"kind": "preset", "id": pid,
            "name": got.name or pid, "blocks": len(got.blocks),
            "path": preset_mod.FILE, "error": got.error}
