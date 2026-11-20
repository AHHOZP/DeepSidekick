"""形象文件发现：只回答“哪里有模型/入口”，不做体检或选择。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

#: 角色 id 取内容指纹的前 16 位十六进制：够区分本地几套模型，又不会太长。
ID_LEN = 16


def read_json(p: Path) -> dict:
    try:
        raw = p.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError, UnicodeError):
        return {}


def rel(p: Path, root: Path) -> str:
    return p.relative_to(root).as_posix()


def digest(p: Path) -> str:
    """文件内容的 SHA1 前 16 位；读不了返回空串。"""
    h = hashlib.sha1()
    try:
        with p.open("rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()[:ID_LEN]


def model_id(model_root: Path, model3_path: Path, model3: dict) -> str:
    """Live2D 角色的 id：模型内容指纹，文件夹改名/搬家都不会变。

    优先用 moc3 的字节（用户改 model3 的组名/动作引用不影响 id）；
    moc3 缺失或读不了时退到 model3.json 本身，至少还能区分不同模型。
    """
    refs = model3.get("FileReferences") if isinstance(model3, dict) else None
    moc = refs.get("Moc") if isinstance(refs, dict) else None
    if isinstance(moc, str) and moc.strip():
        got = digest(model_root / moc.strip().replace("\\", "/"))
        if got:
            return got
    got = digest(model3_path)
    return got or guess_id(model_root)


def find_model3(root: Path) -> list[Path]:
    try:
        found = []
        for p in root.rglob("*.model3.json"):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(root).parts
            except ValueError:
                continue
            if any(x.startswith(".") for x in rel):
                continue
            found.append(p)
        return sorted(found)
    except OSError:
        return []


def find_spine(root: Path) -> list[Path]:
    """找 Spine 骨架：与同名 `.atlas` 同目录的 `.skel`（没有就用 `.json`）。

    同一目录两者都有时优先 `.skel`——那是导出工具的默认产物，而 `.json` 有可能是
    同一套资产的另一种导出或别的数据文件。
    """
    try:
        found = []
        for atlas in root.rglob("*.atlas"):
            if not atlas.is_file():
                continue
            try:
                parts = atlas.relative_to(root).parts
            except ValueError:
                continue
            if any(x.startswith(".") for x in parts):
                continue
            for suffix in (".skel", ".json"):
                cand = atlas.with_suffix(suffix)
                if cand.is_file():
                    found.append(cand)
                    break
        return sorted(found)
    except OSError:
        return []


def guess_id(root: Path) -> str:
    """兜底 id：文件夹名（正常路径用内容指纹，见 `model_id`）。"""
    return root.name
