"""导入导出包：对话预设 / 世界书 / 角色，各自一个 zip。

每个包根目录都有 `manifest.json`（kind / version / id / name），其余文件按 kind 排布：

    preset       manifest.json + preset.yaml
    worldbook    manifest.json + core.md + entries/*.md
    role         manifest.json + role/{profile.yaml,persona.md,actions.md,
                 motion_profile.json} + avatar/<模型文件…>

zip 是真实外部输入，所以解包只认**普通文件**：拒绝绝对路径、`..`、符号链接，
并限制条目数与解压后总字节；超限整包拒绝，不做部分导入。
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

MAX_ENTRIES = 5000
MAX_BYTES = 400 * 1024 * 1024
MANIFEST = "manifest.json"
KINDS = ("preset", "worldbook", "role")
_BAD_NAME_CHARS = set('/\\:*?"<>|')
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} \
    | {f"lpt{i}" for i in range(1, 10)}


class PackError(ValueError):
    """可展示给用户的导入/导出错误。"""


def safe_folder(name: str) -> str:
    """一个目录名（形象文件夹 / 角色目录）：挡路径分隔符和 Windows 保留名。"""
    text = str(name or "")
    if not text or len(text) > 80:
        raise PackError(f"名字长度不对：{name!r}")
    if any(c in _BAD_NAME_CHARS or ord(c) < 32 for c in text):
        raise PackError(f"名字里有非法字符：{name!r}")
    if text != text.strip(" .") or text in (".", ".."):
        raise PackError(f"名字不合法：{name!r}")
    if text.split(".")[0].lower() in _RESERVED:
        raise PackError(f"这个名字 Windows 不认：{name!r}")
    return text


@dataclass(frozen=True)
class Pack:
    kind: str
    manifest: dict
    files: dict[str, bytes]      # 相对路径 → 内容（已过滤目录项）


def _safe_name(name: str) -> str:
    """解包后的相对路径：只允许普通文件名/子目录。"""
    rel = str(name or "").replace("\\", "/").lstrip("/")
    if not rel or rel.endswith("/"):
        raise PackError(f"包里的路径不合法：{name!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise PackError(f"包里的路径不合法：{name!r}")
    if any(":" in p for p in parts):
        raise PackError(f"包里的路径不合法：{name!r}")
    return "/".join(parts)


def read_zip(data: bytes) -> Pack:
    """读一个包：校验大小/条目/路径，返回 manifest + 文件表。"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as e:
        raise PackError(f"不是有效的 zip：{e}") from e
    files: dict[str, bytes] = {}
    total = 0
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > MAX_ENTRIES:
            raise PackError(f"包里的文件太多（{len(infos)} 个）")
        for info in infos:
            rel = _safe_name(info.filename)
            blob = archive.read(info)
            # 只信真实解压结果：ZipInfo.file_size 是中央目录声明值，可伪造。
            total += len(blob)
            if total > MAX_BYTES:
                raise PackError("包解压后太大（超过 400 MB）")
            files[rel] = blob
    raw = files.get(MANIFEST)
    if raw is None:
        raise PackError("包里没有 manifest.json")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as e:
        raise PackError(f"manifest.json 读不了：{e}") from e
    if not isinstance(manifest, dict):
        raise PackError("manifest.json 顶层必须是对象")
    kind = str(manifest.get("kind") or "")
    if kind not in KINDS:
        raise PackError(f"不认识的包类型：{kind!r}")
    return Pack(kind=kind, manifest=manifest, files=files)


def make_zip(files: dict[str, bytes], manifest: dict) -> bytes:
    """打一个包；manifest 永远写在最前面。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST,
                         json.dumps(manifest, ensure_ascii=False, indent=2))
        for rel, blob in files.items():
            archive.writestr(_safe_name(rel), blob)
    return buf.getvalue()


def write_files(base: Path, files: dict[str, bytes]) -> list[Path]:
    """把文件表落到 base 下；只写普通文件，父目录按需创建。"""
    out: list[Path] = []
    for rel, blob in files.items():
        target = base.joinpath(*_safe_name(rel).split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        out.append(target)
    return out


def read_tree(base: Path, *, skip=("__pycache__",)) -> dict[str, bytes]:
    """把一个目录读成文件表（跳过隐藏文件与缓存目录）。"""
    out: dict[str, bytes] = {}
    if not base.is_dir():
        return out
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if any(part.startswith(".") or part in skip for part in rel.parts):
            continue
        out[rel.as_posix()] = path.read_bytes()
    return out
