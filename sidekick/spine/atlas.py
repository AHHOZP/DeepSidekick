"""`.atlas` 文本解析：只取 page 段（图片文件名 + 页宽高）。

region 段不解析：UV 由 C 层从骨架数据算，我们只需要知道每个 page 对应哪个文件、多大。
顺序即 C 层的 `texIndex`，不能排序。
"""
from __future__ import annotations


def parse_atlas(text: str) -> list[dict]:
    """返回 `[{"image": "a.png", "w": 1024, "h": 512}]`。

    page 名是**顶格行**，紧跟一行 `size: W, H`；region 名虽然也顶格，但没有 size 行。
    """
    lines = [line.rstrip("\r") for line in text.split("\n")]
    pages: list[dict] = []
    for i, line in enumerate(lines[:-1]):
        if not line or line[0] in " \t":
            continue
        size = lines[i + 1].strip()
        if not size.startswith("size:"):
            continue
        dims = _parse_size(size)
        if dims is None:
            continue
        pages.append({"image": line.strip(), "w": dims[0], "h": dims[1]})
    return pages


def _parse_size(line: str) -> tuple[int, int] | None:
    try:
        w, h = line[len("size:"):].split(",")
        return int(w.strip()), int(h.strip())
    except ValueError:
        return None


def parse_pma(text: str) -> bool | None:
    """图集里的 `pma:` 声明；没有这一行返回 None（老版本导出不带）。

    有就以它为准：纹理到底预乘没有，导出器最清楚，比看图猜可靠。
    """
    for line in text.splitlines()[:32]:
        key, _, value = line.partition(":")
        if key.strip().lower() == "pma":
            return value.strip().lower() in ("true", "1")
    return None
