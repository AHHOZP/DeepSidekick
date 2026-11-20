"""导入导出包：对话预设 / 世界书 / 角色三件套。

对外只有 `export(kind, …)` 和 `import_(kind, …)` 两个入口，各自的实现在同名模块里；
zip 的读写与安全边界在 `common.py`。
"""
from __future__ import annotations

from sidekick.pack import preset as _preset
from sidekick.pack import role as _role
from sidekick.pack import worldbook as _worldbook
from sidekick.pack.common import KINDS, PackError, read_zip

__all__ = ["KINDS", "PackError", "export", "import_", "kind_of"]


def kind_of(kind: str) -> str:
    text = str(kind or "").strip().lower()
    if text not in KINDS:
        raise PackError(f"不认识的包类型：{kind!r}")
    return text


def export(kind: str, root, ident: str, *, catalog=None) -> tuple[bytes, str]:
    """返回 (zip 字节, 下载文件名)。"""
    kind = kind_of(kind)
    if kind == "preset":
        return _preset.export(root, ident), f"preset-{ident}.zip"
    if kind == "worldbook":
        return _worldbook.export(root, ident), f"worldbook-{ident}.zip"
    return _role.export(root, catalog, ident), f"role-{ident}.zip"


def import_(kind: str, root, data: bytes, *, catalog=None,
            overwrite: bool = False) -> dict:
    kind = kind_of(kind)
    pack = read_zip(data)
    if pack.kind != kind:
        raise PackError(f"这是 {pack.kind} 包，不是 {kind} 包")
    if kind == "preset":
        return _preset.import_(root, pack, overwrite=overwrite)
    if kind == "worldbook":
        return _worldbook.import_(root, pack, overwrite=overwrite)
    return _role.import_(root, catalog, pack, overwrite=overwrite)
