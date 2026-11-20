"""Spine 运行时的 ctypes 绑定：`spR_*` 的签名声明 + 一个骨架实例。

`spR_*` 是 renpy-spine-plugin 提供的稳定 ABI（见 `tools/spine/spine_renpy.c`），
跨 3.8/4.1/4.2 是同一套导出，差别只在加载哪一份 DLL。

**只有这个模块碰 ctypes**：桌面渲染器拿它画，`sidekick/spine/names.py` 的子进程拿它读动画名。
DLL 在 `SpineInstance` 构造时才 LoadLibrary，模块 import 不加载。
"""
from __future__ import annotations

import ctypes
import json
import re
from pathlib import Path

from sidekick.spine.atlas import parse_pma

# C 层 buildMeshEx 的"段数组不够"哨兵值，判决顺序见 build()
SP_R_NEED_SEGMENTS = -0x10000000

VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "spine"

# 只发这四个版本：3.5/3.6 的许可（v2.5）不允许分发，3.7/4.3 没有验证过就不开口子。
DLL_BY_VERSION = {
    "3.8": "spine38.dll",
    "4.0": "spine40.dll",
    "4.1": "spine41.dll",
    "4.2": "spine42.dll",
}
SUPPORTED = tuple(DLL_BY_VERSION)

_VERSION_RE = re.compile(r"^\d+\.\d+")


def dll_for(version: str) -> Path:
    """按骨架版本取 DLL 路径；不支持的版本抛 ValueError（调用方当 broken 处理）。"""
    name = DLL_BY_VERSION.get(str(version).strip())
    if not name:
        raise ValueError(f"不支持 Spine {version}（只支持 {', '.join(DLL_BY_VERSION)}）")
    path = VENDOR / name
    if not path.is_file():
        raise OSError(f"缺少运行时 {path}")
    return path


def skeleton_version(raw: bytes) -> str:
    """从骨架字节读 `主.次`。JSON 读 `skeleton.spine`；二进制读文件头。

    二进制文件头有两种：3.x 是「varint 长度 + hash 串 + varint 长度 + 版本串」，
    4.x 是「8 字节 hash + varint 长度 + 版本串」。两种都试，认出版本号的那个算数。
    """
    head = raw.lstrip(b" \t\r\n\xef\xbb\xbf")
    if head[:1] == b"{":
        doc = json.loads(head.decode("utf-8"))
        version = str(((doc.get("skeleton") or {}).get("spine")) or "")
    else:
        version = _binary_version(head)
    match = _VERSION_RE.match(version)
    if not match:
        raise ValueError(f"骨架版本无法识别：{version!r}")
    return match.group(0)


def _binary_version(head: bytes) -> str:
    # 3.x：两个字符串（hash、版本号）
    try:
        _hash, i = _read_string(head, 0)
        version, _ = _read_string(head, i)
        if _VERSION_RE.match(version):
            return version
    except (IndexError, ValueError):
        pass
    # 4.x：8 字节 hash + 一个版本串
    try:
        version, _ = _read_string(head, 8)
    except (IndexError, ValueError) as exc:
        raise ValueError("骨架版本无法识别（文件头不是 3.x/4.x 的二进制格式）") from exc
    if _VERSION_RE.match(version):
        return version
    raise ValueError("骨架版本无法识别（文件头不是 3.x/4.x 的二进制格式）")


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7
        if shift > 28:
            raise ValueError("varint 过长")


def _read_string(buf: bytes, i: int) -> tuple[str, int]:
    """spine 的 `readString`：varint 长度**含结尾的 NUL**，实际字符数是 size-1。"""
    size, i = _varint(buf, i)
    if size <= 0:
        return "", i
    size -= 1
    if size > 4096 or i + size > len(buf):
        raise ValueError("字符串越界")
    return buf[i:i + size].decode("utf-8", "replace"), i + size


class _DrawItem(ctypes.Structure):
    """与 `spine_renpy.c` 的 `spRDrawItem` 一一对应（只用于命中时定位 slot）。"""

    _fields_ = [
        ("slotIndex", ctypes.c_int),
        ("texIndex", ctypes.c_int),
        ("vertices", ctypes.c_float * 8),
        ("uvs", ctypes.c_float * 8),
        ("color", ctypes.c_float * 4),
        ("vertsCount", ctypes.c_int),
        ("trianglesCount", ctypes.c_int),
        ("vertsOffset", ctypes.c_int),
        ("trisOffset", ctypes.c_int),
    ]


def _bind(path: Path) -> ctypes.CDLL:
    lib = ctypes.CDLL(str(path))
    lib.spR_version.restype = ctypes.c_char_p
    lib.spR_createMem.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_float]
    lib.spR_createMem.restype = ctypes.c_void_p
    lib.spR_error.argtypes = [ctypes.c_void_p]
    lib.spR_error.restype = ctypes.c_char_p
    lib.spR_dispose.argtypes = [ctypes.c_void_p]
    lib.spR_update.argtypes = [ctypes.c_void_p, ctypes.c_float]
    lib.spR_setAnimation.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.spR_setAnimation.restype = ctypes.c_int
    lib.spR_clearTrack.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.spR_getAnimationDuration.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.spR_getAnimationDuration.restype = ctypes.c_float
    lib.spR_getAnimationCount.argtypes = [ctypes.c_void_p]
    lib.spR_getAnimationCount.restype = ctypes.c_int
    lib.spR_getAnimationName.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.spR_getPageCount.argtypes = [ctypes.c_void_p]
    lib.spR_getPageCount.restype = ctypes.c_int
    lib.spR_getPageName.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.spR_getSlotCount.argtypes = [ctypes.c_void_p]
    lib.spR_getSlotCount.restype = ctypes.c_int
    lib.spR_getSlotName.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.spR_collectDrawItems.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_DrawItem), ctypes.c_int]
    lib.spR_collectDrawItems.restype = ctypes.c_int
    lib.spR_getMeshBufs.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_float)),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_float)),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ushort)),
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int)]
    lib.spR_buildMeshEx.argtypes = [
        ctypes.c_void_p, ctypes.c_float, ctypes.c_float, ctypes.c_float,
        ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_float), ctypes.c_int,
        ctypes.c_float, ctypes.c_float,
        ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
        ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.spR_buildMeshEx.restype = ctypes.c_int
    return lib


class SpineInstance:
    """一个骨架实例：加载、播动画、推进、取本帧合并 mesh、命中判定。

    只想读动画名时用 `names_only=True`：不分配 mesh 缓冲，加载完直接 `close()`。
    """

    def __init__(self, skeleton: Path, atlas: Path, *, scale: float = 1.0,
                 names_only: bool = False, max_verts: int = 16384) -> None:
        skeleton = Path(skeleton)
        atlas = Path(atlas)
        raw = skeleton.read_bytes()
        self.version = skeleton_version(raw)
        self._lib = _bind(dll_for(self.version))
        # C 层不接管这两块内存，必须自己抱住，直到 dispose
        # C 层的 atlas 解析器把"第一条空行"当成结束：有些游戏的 .atlas 开头带空行，
        # 这里统一去掉 BOM 与前导空行（纯文本，不改内容）。
        atlas_text = (atlas.read_bytes().lstrip(b"\xef\xbb\xbf").lstrip(b"\r\n"))
        self._skel_buf = ctypes.create_string_buffer(raw)
        self._atlas_buf = ctypes.create_string_buffer(atlas_text)
        ctx = self._lib.spR_createMem(
            ctypes.cast(self._skel_buf, ctypes.c_void_p), len(raw),
            ctypes.cast(self._atlas_buf, ctypes.c_void_p), len(atlas_text),
            float(scale))
        if not ctx:
            self._ctx = None
            raise RuntimeError(f"spR_createMem 返回空指针（Spine {self.version}）")
        error = self._lib.spR_error(ctx)
        if error:
            self._lib.spR_dispose(ctx)
            self._ctx = None
            raise RuntimeError(error.decode("utf-8", "replace"))
        self._ctx = ctypes.c_void_p(ctx)

        self.animations = self._names(self._lib.spR_getAnimationName,
                                      self._lib.spR_getAnimationCount(self._ctx))
        self.slot_names = self._names(self._lib.spR_getSlotName,
                                      self._lib.spR_getSlotCount(self._ctx))
        self.pages = self._names(self._lib.spR_getPageName,
                                 self._lib.spR_getPageCount(self._ctx))
        self.pma = parse_pma(atlas_text.decode("utf-8", "replace"))
        self._items = (_DrawItem * 256)()
        self._layout: tuple[float, float, list[int], list[int], list[int]] | None = None
        self._view = (0.0, 0.0, 1.0)
        self._offset = (0.0, 0.0)
        self._cap = 0
        self._seg_cap = 16
        self.geo = None
        self.tris = None
        if not names_only:
            self._alloc(max_verts)

    # ── 生命周期 ────────────────────────────────────────
    def close(self) -> None:
        if getattr(self, "_ctx", None):
            self._lib.spR_dispose(self._ctx)
            self._ctx = None

    # ── 内部 ────────────────────────────────────────────
    def _names(self, fn, count: int) -> list[str]:
        out = []
        buf = ctypes.create_string_buffer(512)
        for i in range(int(count)):
            if fn(self._ctx, i, buf, len(buf)):
                out.append(buf.value.decode("utf-8", "replace"))
        return out

    def _alloc(self, cap: int) -> None:
        self._cap = max(1024, int(cap))
        self._geo = (ctypes.c_float * (self._cap * 2))()
        self._attrs = (ctypes.c_float * (self._cap * 6))()
        self._tris = (ctypes.c_ushort * (self._cap * 6))()
        self._segs = (ctypes.c_int * self._seg_cap)()
        self._starts = (ctypes.c_int * (self._seg_cap + 1))()

    # ── 播放 ────────────────────────────────────────────
    def play(self, name: str, loop: bool = True) -> bool:
        if not name or self._ctx is None:
            return False
        return bool(self._lib.spR_setAnimation(
            self._ctx, 0, name.encode("utf-8"), 1 if loop else 0))

    def clear(self) -> None:
        if self._ctx is not None:
            self._lib.spR_clearTrack(self._ctx, 0)

    def duration(self, name: str) -> float:
        if not name or self._ctx is None:
            return 0.0
        return max(0.0, float(self._lib.spR_getAnimationDuration(
            self._ctx, name.encode("utf-8"))))

    def update(self, delta: float) -> None:
        if self._ctx is not None:
            self._lib.spR_update(self._ctx, float(delta))

    # ── 取 mesh ─────────────────────────────────────────
    def set_texture_layout(self, offsets: list[int], widths: list[int],
                           heights: list[int], atlas_w: int,
                           atlas_h: int) -> None:
        """喂给 `buildMeshEx`：每个 page 在合成图里的水平偏移 + 页宽高 + 合成图总尺寸。"""
        self._layout = (float(atlas_w), float(atlas_h), list(offsets),
                        list(widths), list(heights))

    def build(self, min_x: float, min_y: float, zoom: float):
        """把本帧可见附件合并成一份屏幕空间 mesh。

        返回 `(geo, attrs, tris, segments)`：
            geo      (nv,2) float32，已减 min 并乘 zoom
            attrs    (nv,6) float32，u,v,r,g,b,a
            tris     (nt*3,) uint16
            segments [(blend_mode, tri_start, tri_end), ...]
        numpy 数组是 ctypes 缓冲的视图，**下一次 build 时失效**，不要跨帧持有。
        """
        if self._layout is None:
            raise RuntimeError("没有调用 set_texture_layout()")
        atlas_w, atlas_h, offsets, widths, heights = self._layout
        n_pages = max(1, len(self.pages))
        c_off = (ctypes.c_float * n_pages)()
        c_w = (ctypes.c_float * n_pages)()
        c_h = (ctypes.c_float * n_pages)()
        for i in range(n_pages):
            c_off[i] = float(offsets[i] if i < len(offsets) else 0)
            c_w[i] = float(widths[i] if i < len(widths) else 0)
            c_h[i] = float(heights[i] if i < len(heights) else 0)

        while True:
            nt = ctypes.c_int()
            seg_count = ctypes.c_int()
            r = self._lib.spR_buildMeshEx(
                self._ctx, float(min_x), float(min_y), float(zoom),
                c_off, c_w, c_h, n_pages, atlas_w, atlas_h,
                self._geo, self._attrs, self._tris,
                self._cap, self._cap * 2, ctypes.byref(nt),
                self._segs, self._starts, self._seg_cap, ctypes.byref(seg_count))
            # 判决顺序：先段数组不足，再顶点不足，最后才是成功
            if r == SP_R_NEED_SEGMENTS:
                self._seg_cap *= 2
                self._segs = (ctypes.c_int * self._seg_cap)()
                self._starts = (ctypes.c_int * (self._seg_cap + 1))()
                continue
            if r >= 0:
                nv = int(r)
                break
            self._alloc(-r * 2)

        import numpy as np

        geo = np.ctypeslib.as_array(self._geo)[: nv * 2].reshape(nv, 2)
        attrs = np.ctypeslib.as_array(self._attrs)[: nv * 6].reshape(nv, 6)
        tris = np.ctypeslib.as_array(self._tris)[: int(nt.value) * 3]
        self.geo, self.tris = geo, tris
        self._view = (float(min_x), float(min_y), float(zoom))
        segments = [(int(self._segs[i]), int(self._starts[i]),
                     int(self._starts[i + 1])) for i in range(int(seg_count.value))]
        if not segments:
            segments = [(0, 0, int(nt.value))]
        return geo, attrs, tris, segments

    # ── 屏幕放置与命中 ──────────────────────────────────
    def set_offset(self, x: float, y: float) -> None:
        """模型左上角在窗口里的像素偏移（与 glOrtho 的像素坐标一致）。"""
        self._offset = (float(x), float(y))

    def screen_to_world(self, x: float, y: float) -> tuple[float, float]:
        min_x, min_y, zoom = self._view
        return ((x - self._offset[0]) / zoom + min_x,
                (y - self._offset[1]) / zoom + min_y)

    def hit_test(self, x: float, y: float) -> bool:
        """像素级命中：在 build() 出的屏幕空间三角形上判定，零 C 调用。"""
        if self.geo is None or self.tris is None or self.tris.size == 0:
            return False
        return _point_hits_mesh(x - self._offset[0], y - self._offset[1],
                                self.geo, self.tris)

    def hit_part(self, x: float, y: float) -> str:
        """命中部位（slot 名）。只在确实命中后才走 C 侧的逐附件收集。"""
        if not self.hit_test(x, y):
            return ""
        wx, wy = self.screen_to_world(x, y)
        return self._slot_at(wx, wy)

    def _slot_at(self, wx: float, wy: float) -> str:
        n = 0
        while True:
            n = self._lib.spR_collectDrawItems(self._ctx, self._items,
                                               len(self._items))
            if n >= 0:
                break
            self._items = (_DrawItem * (-n))()
        if n <= 0:
            return ""
        import numpy as np

        vbuf = ctypes.POINTER(ctypes.c_float)()
        ubuf = ctypes.POINTER(ctypes.c_float)()
        tbuf = ctypes.POINTER(ctypes.c_ushort)()
        vc, uc, tc = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        self._lib.spR_getMeshBufs(
            self._ctx, ctypes.byref(vbuf), ctypes.byref(ubuf), ctypes.byref(tbuf),
            ctypes.byref(vc), ctypes.byref(uc), ctypes.byref(tc))
        verts = (np.ctypeslib.as_array(vbuf, shape=(max(1, vc.value),))
                 if vc.value else None)
        tris = (np.ctypeslib.as_array(tbuf, shape=(max(1, tc.value),))
                if tc.value else None)
        for i in range(n - 1, -1, -1):
            item = self._items[i]
            slot = (self.slot_names[item.slotIndex]
                    if 0 <= item.slotIndex < len(self.slot_names) else "")
            if item.vertsCount and verts is not None and tris is not None:
                pts = verts[item.vertsOffset:item.vertsOffset + item.vertsCount]
                pts = pts.reshape(-1, 2)
                if pts[:, 0].min() > wx or pts[:, 0].max() < wx:
                    continue
                if pts[:, 1].min() > wy or pts[:, 1].max() < wy:
                    continue
                tri = tris[item.trisOffset:item.trisOffset + item.trianglesCount]
                if _point_hits_mesh(wx, wy, pts, tri):
                    return slot
            else:
                v = item.vertices
                if _point_in_quad(wx, wy, v):
                    return slot
        return ""


def _point_in_quad(px: float, py: float, v) -> bool:
    xs = (v[0], v[2], v[4], v[6])
    ys = (v[1], v[3], v[5], v[7])
    if px < min(xs) or px > max(xs) or py < min(ys) or py > max(ys):
        return False
    return (_point_in_tri(px, py, v[0], v[1], v[2], v[3], v[4], v[5])
            or _point_in_tri(px, py, v[0], v[1], v[4], v[5], v[6], v[7]))


def _point_in_tri(px, py, ax, ay, bx, by, cx, cy) -> bool:
    d1 = (px - bx) * (ay - by) - (ax - bx) * (py - by)
    d2 = (px - cx) * (by - cy) - (bx - cx) * (py - cy)
    d3 = (px - ax) * (cy - ay) - (cx - ax) * (py - ay)
    return not (((d1 < 0) or (d2 < 0) or (d3 < 0))
                and ((d1 > 0) or (d2 > 0) or (d3 > 0)))


def _point_hits_mesh(px: float, py: float, pts, tri) -> bool:
    """对一整块 mesh 做向量化的三角形包含测试（重心符号法）。"""
    import numpy as np

    k = tri.shape[0] // 3 * 3
    if k == 0:
        return False
    idx = tri[:k].reshape(-1, 3).astype(np.int32)
    a = pts[idx[:, 0]]
    b = pts[idx[:, 1]]
    c = pts[idx[:, 2]]
    d1 = ((px - b[:, 0]) * (a[:, 1] - b[:, 1])
          - (a[:, 0] - b[:, 0]) * (py - b[:, 1]))
    d2 = ((px - c[:, 0]) * (b[:, 1] - c[:, 1])
          - (b[:, 0] - c[:, 0]) * (py - c[:, 1]))
    d3 = ((px - a[:, 0]) * (c[:, 1] - a[:, 1])
          - (c[:, 0] - a[:, 0]) * (py - a[:, 1]))
    neg = (d1 < 0) | (d2 < 0) | (d3 < 0)
    pos = (d1 > 0) | (d2 > 0) | (d3 > 0)
    return bool(np.any(~(neg & pos)))
