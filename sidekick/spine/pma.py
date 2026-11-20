"""图集是否预乘（PMA）判定。

Spine 的页图有两种来源：图集里写了 `pma: true` 的（4.x 导出会写），以及没写、只能看
像素的。没写时按"违例比例"判：预乘的不变式是 RGB <= A，真预乘素材里也会有千分之一
左右的编码噪声，而真正非预乘的图，半透明像素几乎全部违例（实测 3%~5%）。

浏览器侧不再自己判：它以 `/config.json` 里的 `pma` 为准。原因是在 canvas 上
`getImageData` 拿回来的像素在软件渲染下可能是预乘过的，判出来永远是"预乘"，
棕色尘埃 2 那两个模型就是这么被判错、渲染出一圈白边的。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

from sidekick.spine.atlas import parse_atlas, parse_pma


def looks_premultiplied(images) -> bool:
    """按违例比例判（阈值 1%）；没有半透明像素时按预乘处理。"""
    bad = 0
    semi = 0
    for image in images:
        pixels = np.asarray(image, dtype=np.uint8)
        rgb = pixels[..., :3].max(axis=2).astype(np.int16)
        alpha = pixels[..., 3].astype(np.int16)
        mask = alpha > 0
        semi += int(mask.sum())
        bad += int(((rgb - alpha) > 2)[mask].sum())
    if semi == 0:
        return True
    return bad / semi <= 0.01


def _load_pages(atlas_path: Path) -> list[Image.Image]:
    text = atlas_path.read_text(encoding="utf-8-sig", errors="ignore")
    pages = [page["image"] for page in parse_atlas(text)]
    images = []
    for name in pages:
        p = atlas_path.parent / name
        if not p.is_file():
            return []
        with Image.open(p) as image:
            images.append(image.convert("RGBA"))
    return images


@lru_cache(maxsize=32)
def _pma_cached(atlas: str, mtime_ns: int) -> bool | None:
    path = Path(atlas)
    text = path.read_text(encoding="utf-8-sig", errors="ignore")
    declared = parse_pma(text)
    if declared is not None:
        return declared
    images = _load_pages(path)
    if not images:
        return None
    return looks_premultiplied(images)


def pma_of(atlas_path: Path | str | None) -> bool | None:
    """`None` 表示判不了（图集缺失/页图缺失），调用方按预乘处理。"""
    if not atlas_path:
        return None
    path = Path(atlas_path)
    try:
        stat = path.stat()
    except OSError:
        return None
    return _pma_cached(str(path), stat.st_mtime_ns)
