"""形象体检：只判断“这个模型能不能加载”，不做扫描、选择或展示。"""
from __future__ import annotations

import struct
from pathlib import Path

from sidekick.spine import atlas as spine_atlas
from sidekick.spine.binding import SUPPORTED, dll_for, skeleton_version

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
# Cubism Core 头文件里 csmMocVersion 目前到 5；官方 5-r.5 样例里已出现 6。
_CORE_MOC_VERSION = 6


def _texture_list(value) -> list[str]:
    """Textures 可能是数组，也可能是对象（魂器学院那种非标准写法）。"""
    if isinstance(value, list):
        return [x for x in value if isinstance(x, str)]
    if isinstance(value, dict):
        out: list[str] = []
        for sub in value.values():
            if isinstance(sub, list):
                out += [x for x in sub if isinstance(x, str)]
            elif isinstance(sub, str):
                out.append(sub)
        return out
    return []


def validate(root: Path, model3: dict) -> tuple[list[str], list[str]]:
    """返回 (致命问题, 警告)。只读，不解码图片。"""
    broken: list[str] = []
    warns: list[str] = []
    refs = model3.get("FileReferences")
    if not isinstance(refs, dict):
        return ["FileReferences 缺失或类型不对，不是 model3.json"], warns

    moc = refs.get("Moc")
    moc = moc.strip() if isinstance(moc, str) else ""
    if not moc:
        broken.append("FileReferences.Moc 缺失")
    elif not moc.lower().endswith(".moc3"):
        broken.append(f"Moc 指向 {moc}，不是 .moc3（Cubism 2.1 要另一套内核）")
    else:
        p = root / moc.replace("\\", "/")
        if not p.is_file():
            broken.append(f"moc3 不存在：{moc}")
        else:
            try:
                with p.open("rb") as f:
                    head = f.read(8)
            except OSError as e:
                broken.append(f"moc3 打不开：{moc}（{e}）")
            else:
                if len(head) < 8:
                    broken.append("moc3 文件为空或过短")
                elif head[:4] != b"MOC3":
                    broken.append(f"moc3 魔数不对：{moc}（{head[:4]!r}）")
                else:
                    ver = struct.unpack("<I", head[4:8])[0]
                    if ver > _CORE_MOC_VERSION:
                        warns.append(f"moc3 v{ver} 可能高于当前 Core（已知最高 v{_CORE_MOC_VERSION}）")

    tex_raw = refs.get("Textures")
    textures = _texture_list(tex_raw)
    if isinstance(tex_raw, dict):
        warns.append("Textures 是对象而不是数组（非标准写法），已展开")
    if not textures:
        broken.append("Textures 为空：没有任何贴图可绑定，整模不可见")
    for t in textures:
        p = root / t.replace("\\", "/")
        if not p.is_file():
            broken.append(f"纹理缺失：{t}（该层部件会整片消失）")
            continue
        try:
            with p.open("rb") as f:
                head = f.read(24)
        except OSError as e:
            broken.append(f"纹理打不开：{t}（{e}）")
            continue
        if not head.startswith(_PNG_SIG):
            broken.append(f"纹理不是 PNG：{t}")

    for key in ("Physics", "Pose", "UserData", "DisplayInfo"):
        v = refs.get(key)
        if isinstance(v, str) and v.strip() and not (root / v.replace("\\", "/")).is_file():
            warns.append(f"{key} 指向的文件不存在：{v}（只影响物理/姿态，不影响外形）")
    return broken, warns


# ── Spine ──────────────────────────────────────────────
def validate_spine(root: Path, entry: Path) -> tuple[list[str], list[str]]:
    """Spine 能不能加载：版本 / 运行时 / 图集页 / JSON 里会让原生层崩溃的 curve 值。"""
    broken: list[str] = []
    warns: list[str] = []
    try:
        version = skeleton_version(_read_head(entry))
    except OSError as exc:
        return [f"骨架读不到：{exc}"], warns
    except ValueError as exc:
        return [str(exc)], warns

    if version not in SUPPORTED:
        return [f"不支持 Spine {version}（只支持 {' / '.join(SUPPORTED)}）"], warns
    try:
        dll_for(version)
    except OSError as exc:
        broken.append(str(exc))

    atlas_path = entry.with_suffix(".atlas")
    try:
        pages = spine_atlas.parse_atlas(atlas_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError) as exc:
        broken.append(f"图集读不到：{exc}")
        pages = []
    if not pages:
        broken.append("图集里没有 page 段")
    for page in pages:
        if not (root / page["image"]).is_file():
            broken.append(f"图集页缺失：{page['image']}")

    if entry.suffix.lower() == ".json":
        broken += _spine_curve_problems(entry)
    return broken, warns


def _read_head(entry: Path) -> bytes:
    """版本号在文件头；二进制骨架只读前 4 KB，JSON 要整份（后面还要查 curve）。"""
    if entry.suffix.lower() == ".json":
        return entry.read_bytes()
    with entry.open("rb") as fp:
        return fp.read(4096)


def _spine_curve_problems(entry: Path) -> list[str]:
    """4.x 的 readCurve 只认 "stepped" 或 4 点贝塞尔数组。

    写成字符串（例如 "linear"）会让原生解析器空指针崩溃，把整个桌宠进程带走，
    所以必须在扫描阶段拦住——线性插值要靠省略 curve 字段表达。
    """
    from sidekick.avatar.scan import read_json

    doc = read_json(entry)
    if not doc:
        return ["骨架 JSON 解析失败或为空"]
    bad: list[str] = []

    def walk(node, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{trail}.{key}" if trail else key
                if key == "curve" and isinstance(value, str) and value != "stepped":
                    bad.append(here)
                walk(value, here)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{trail}[{i}]")

    walk(doc, "")
    if not bad:
        return []
    return [f"{len(bad)} 处非法 curve 值（首个在 {bad[0]}）：线性插值请省略 curve 字段"]
