"""画面打包：先 plan（纯数据），再 render（解码/拼图/编码）。

`plan` 不碰 PIL；`/peek` 和 `Scheduler` 可以只看布局与摘要。`render` 在
`Runtime._think()` 里走 `asyncio.to_thread`，避免拼图阻塞事件循环。
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from sidekick.input.capture import (_fit_resolution, encode_jpeg,
                                STRIP_BOTTOM_MIN, STRIP_UPPER_MAX)
from sidekick.input.tokens import estimate_visual_tokens

_EMPTY = (30, 30, 30)
_LINE = (42, 42, 42)
_STRIP_HISTORY_MAX = 6
_STRIP_MAX_PIXELS = 1_690_000


def _strip_images(strip_idx: tuple[int, ...], strip_frames: list,
                  width: int) -> list[PackImage]:
    """把台词条带按目标宽度缩放；超过面积上限就拆成多张。"""
    if not strip_frames:
        return []
    bottom_w, bottom_h = strip_frames[0].bottom_size
    if bottom_w <= 0 or bottom_h <= 0:
        return []
    width = max(2, int(width))
    line_h = max(2, int(round(bottom_h * width / bottom_w)))
    line_h -= line_h % 2
    width -= width % 2
    per = max(1, width * line_h)
    max_lines = max(1, _STRIP_MAX_PIXELS // per)
    out: list[PackImage] = []
    for pos in range(0, len(strip_frames), max_lines):
        chunk = strip_idx[pos:pos + max_lines]
        out.append(PackImage(
            "strip", chunk, 1, len(chunk), width, line_h * len(chunk)))
    return out


@dataclass(frozen=True)
class PackImage:
    kind: str                         # "grid" / "strip" / "single"
    indices: tuple[int, ...]
    cols: int = 1
    rows: int = 1
    width: int = 0
    height: int = 0
    current: bool = False


@dataclass(frozen=True)
class FramePackMeta:
    source_count: int
    selected: tuple[int, ...]
    images: tuple[PackImage, ...] = ()
    summary: str = ""
    tokens: int = 0
    #: 需要用全分辨率图渲染的帧索引；当前只有最新一帧，未来可放多张。
    high_res: tuple[int, ...] = ()


def _size(value) -> tuple[int, int]:
    w, h = str(value or "").lower().split("x", 1)
    return max(1, int(w)), max(1, int(h))


def _select_keyframes(frames: list, budget: int) -> list:
    """按 ts 分桶取峰值；保留首帧，桶内 delta 最大（平局取晚帧）。"""
    if budget <= 0:
        return []
    if len(frames) <= budget:
        return list(frames)
    if budget == 1:
        return [frames[0]]
    first = frames[0]
    rest = list(frames[1:])
    if not rest:
        return [first]
    buckets = max(1, budget - 1)
    start, end = rest[0].ts, rest[-1].ts
    span = end - start
    groups: list[list] = [[] for _ in range(buckets)]
    for frame in rest:
        if span > 0:
            pos = int((frame.ts - start) / span * buckets)
            pos = min(buckets - 1, max(0, pos))
        else:
            pos = min(buckets - 1, len(groups) - 1)
        groups[pos].append(frame)
    picked = [first]
    for group in groups:
        if group:
            best = max(group, key=lambda f: (getattr(f, "delta_score", 0.0),
                                             getattr(f, "ts", 0.0)))
            picked.append(best)
    picked.sort(key=lambda f: getattr(f, "ts", 0.0))
    if len(picked) < budget:
        # 时间分布不均时某些桶为空，用剩余的高 delta 帧补满预算。
        rest = [f for f in frames if f not in picked]
        rest.sort(key=lambda f: (getattr(f, "delta_score", 0.0),
                                 getattr(f, "ts", 0.0)), reverse=True)
        picked.extend(rest[:budget - len(picked)])
        picked.sort(key=lambda f: getattr(f, "ts", 0.0))
    return picked


def _grid(indices, cols, rows, width, height) -> PackImage:
    return PackImage("grid", tuple(indices), cols, rows, width, height)


def _single(index, width, height, current=False) -> PackImage:
    return PackImage("single", (index,), 1, 1, width, height, current)


def _with_required(picked: list, required: list, cap: int) -> list:
    """保留当前/快速帧，再用时间桶结果补满；不改变时间顺序。"""
    out = list(picked)
    for req in required:
        if req is None or req in out:
            continue
        if len(out) >= cap:
            for i, frame in enumerate(out):
                if frame not in required:
                    out[i] = req
                    break
        else:
            out.append(req)
    out.sort(key=lambda f: getattr(f, "ts", 0.0))
    return out


def _extra_index(frames: list) -> int | None:
    """最近一次语音快速截图保留下来的额外全图帧。"""
    for i in range(len(frames) - 2, -1, -1):
        if getattr(frames[i], "extra_jpeg", None):
            return i
    return None


def _contain_size(source: tuple[int, int],
                  box: tuple[int, int]) -> tuple[int, int]:
    """按源比例 contain 到最大框，不放大、不裁切。"""
    sw, sh = max(1, int(source[0])), max(1, int(source[1]))
    bw, bh = max(1, int(box[0])), max(1, int(box[1]))
    scale = min(bw / sw, bh / sh, 1.0)
    w = max(2, int(round(sw * scale)))
    h = max(2, int(round(sh * scale)))
    w -= w % 2
    h -= h % 2
    return w, h


def _clarity_size(source: tuple[int, int], percent) -> tuple[int, int]:
    """清晰度是固定目标框：66.6%→1280×720，83.3%→1600×900，100%→1920×1080。

    与当前屏幕/采集分辨率无关；其他比例 contain 到同一个目标框，
    所以 16:10 在 66.6% 是 1152×720，4:3 是 960×720。
    """
    pct = max(0.1, min(100.0, float(percent)))
    bw = max(2, int(round(1920 * pct / 100)))
    bh = max(2, int(round(1080 * pct / 100)))
    bw += bw % 2
    bh += bh % 2
    return _contain_size(source, (bw, bh))


def _tail_size(source: tuple[int, int]) -> tuple[int, int]:
    return _contain_size(source, (960, 540))


GRID_BOX = (1920, 1080)


def _grid_canvas(source: tuple[int, int]):
    return _contain_size(source, GRID_BOX)


def _tail_single(index: int, size: tuple[int, int]) -> PackImage:
    return _single(index, size[0], size[1], False)


def _append_tail(groups: list[PackImage], indices: list[int],
                 size: tuple[int, int]) -> None:
    """不足 2×2 的尾巴：1 张单张；2 张分开发；3–4 张仍合 2×2。"""
    if not indices:
        return
    if len(indices) == 1:
        groups.append(_tail_single(indices[0], size))
    elif len(indices) == 2:
        groups.extend(_tail_single(i, size) for i in indices)
    else:
        groups.append(_grid(indices, 2, 2, size[0], size[1]))


def _append_twos(groups: list[PackImage], history: list[int],
                 source: tuple[int, int]) -> None:
    canvas = _grid_canvas(source)
    for pos in range(0, len(history), 4):
        chunk = history[pos:pos + 4]
        if len(chunk) < 3:
            _append_tail(groups, chunk, _tail_size(source))
        else:
            groups.append(_grid(chunk, 2, 2, canvas[0], canvas[1]))


def _layout_groups(history: list[int],
                   source: tuple[int, int], mode: str) -> list[PackImage]:
    """历史帧（新→旧）分层：新帧先 2×2（960 格），老帧再 3×3（640 格）。"""
    n = len(history)
    if not n:
        return []
    groups: list[PackImage] = []
    if mode != "active":
        _append_twos(groups, history[:4], source)
        return groups

    if n <= 16:
        _append_twos(groups, history, source)
        return groups

    low_n = 18 if n >= 26 else 9
    high = history[:-low_n]
    low = history[-low_n:]
    _append_twos(groups, high, source)
    canvas = _grid_canvas(source)
    for pos in range(0, len(low), 9):
        chunk = low[pos:pos + 9]
        groups.append(_grid(chunk, 3, 3, canvas[0], canvas[1]))
    return groups


def _summary_pack(high_count: int, groups: list[PackImage]) -> str:
    if high_count >= 2:
        head = "最左 2 张高清（当前帧在前、上一张高清在后）"
    else:
        head = "最左 1 张当前高清"
    layout = " + ".join(
        f"{len(img.indices)} 帧 {img.cols}×{img.rows}"
        if img.kind == "grid" else "1 帧单张"
        for img in groups)
    tail = (f"；之后为历史拼图（{layout}，左上→右下从新到旧）"
            if groups else "")
    return f"{head}{tail}。整体从左到右由新到旧"


def _strip_frames(hist: list, capture: dict, boundary=None) -> list:
    """自动判断字幕条带；开关只用于显式关闭，3–6 张连续帧才触发。"""
    if not bool(capture["subtitle_strip_enabled"]):
        return []
    if len(hist) < 3:
        return []
    chosen = hist[-_STRIP_HISTORY_MAX:]
    if any(not getattr(f, "bottom_jpeg", None)
           or not getattr(f, "bottom_size", None) for f in chosen):
        return []
    sizes = {tuple(f.bottom_size) for f in chosen}
    if len(sizes) != 1:
        return []
    # 当前帧不在条带里，但仍要检查“最后一句 → 当前画面”是否切了场景，
    # 否则会把上一场景的旧字幕配到新画面上。
    if boundary is not None:
        if float(getattr(boundary, "upper_diff", 0.0)) >= STRIP_UPPER_MAX:
            return []
    for frame in chosen[1:]:
        if float(getattr(frame, "upper_diff", 0.0)) >= STRIP_UPPER_MAX:
            return []
        if float(getattr(frame, "bottom_diff", 0.0)) < STRIP_BOTTOM_MIN:
            return []
    return chosen


def plan(frames: list, cfg: dict) -> FramePackMeta:
    """只算布局和摘要；不解码、不编码。

    图片顺序：语音快速高清 → 当前高清 → 2×2 → 3×3 → 2×2 → 后续 3×3。
    没有快速帧时只有当前高清；整体从左到右由新到旧。
    """
    all_frames = list(frames or [])
    capture = cfg.get("capture") or {}
    source_n = len(all_frames)
    if source_n == 0:
        return FramePackMeta(0, (), (), "", 0)
    index_of = {id(f): i for i, f in enumerate(all_frames)}
    current = source_n - 1
    extra = _extra_index(all_frames)
    extra_frame = all_frames[extra] if extra is not None else None
    current_frame = all_frames[current]
    cap = max(1, int(capture["max_frames"]))
    mode = str(capture["mode"])
    box = _size(capture["resolution"])
    source_size = getattr(current_frame, "size", None) or box
    clarity = capture["clarity_percent"]
    high_indices = tuple(i for i in (current, extra) if i is not None)
    # 当前 + 语音快速两张高清是强制的；即使外部把 max_frames 手改成 1，
    # 也不能让 selected/indices 与 high_res 不一致。
    cap = max(cap, len(high_indices))
    high_images = []
    high_tokens = 0
    for idx in high_indices:
        frame_size = getattr(all_frames[idx], "size", None) or source_size
        size = _clarity_size(frame_size, clarity)
        high_images.append(_single(idx, *size, idx == current))
        high_tokens += estimate_visual_tokens(*size)
    # 剧烈 28 = 26 历史 + 当前高清 + 可选快速高清；平缓 6 = 4 历史 + 高清。
    history_limit = 26 if mode == "active" else 4
    effective_cap = min(cap, len(high_images) + history_limit)
    selected = all_frames
    if source_n > effective_cap:
        selected = _with_required(
            _select_keyframes(all_frames, effective_cap),
            [extra_frame, current_frame], effective_cap)

    strip_frames = _strip_frames(
        [f for f in all_frames[:-1] if f is not extra_frame], capture,
        boundary=current_frame)
    if strip_frames and len(strip_frames) + len(high_images) > cap:
        strip_frames = []
    if strip_frames:
        strip_idx = tuple(index_of[id(f)] for f in strip_frames)
        strip_images = _strip_images(strip_idx, strip_frames,
                                     high_images[0].width)
        if not strip_images:
            strip_frames = []
        else:
            images = high_images + strip_images
            sizes = "、".join(f"{img.width}×{img.height}"
                             for img in strip_images)
            summary = _summary_pack(len(high_images), [])
            summary += (f"；历史 {len(strip_frames)} 句底部台词条带"
                        f"（{sizes}，背景未变，仅底部对话框"
                        f"按时间递增）")
            tokens = high_tokens + sum(
                estimate_visual_tokens(img.width, img.height)
                for img in strip_images)
            return FramePackMeta(source_n, high_indices + strip_idx,
                                 tuple(images), summary, tokens,
                                 high_res=high_indices)

    # 历史帧排除两张高清，倒序成新→旧后分组。
    history = [index_of[id(f)] for f in reversed(selected)
               if f is not all_frames[current] and f is not extra_frame]
    groups = _layout_groups(history, source_size, mode)
    images = high_images + groups
    tokens = high_tokens + sum(
        estimate_visual_tokens(img.width, img.height) for img in groups)
    summary = _summary_pack(len(high_images), groups)
    selected_out = high_indices + tuple(
        idx for image in groups for idx in image.indices)
    return FramePackMeta(source_n, selected_out, tuple(images), summary,
                         tokens, high_res=high_indices)


def _font(size: int):
    for name in ("msyh.ttc", "segoeui.ttf"):
        path = "C:/Windows/Fonts/" + name
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _stamp(ts: float) -> str:
    import time

    return time.strftime("%H:%M:%S", time.localtime(ts))


def _decode(frame):
    try:
        with Image.open(io.BytesIO(frame.thumb_jpeg)) as img:
            return img.convert("RGB")
    except Exception:
        return None


def _decode_jpeg(data):
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.convert("RGB")
    except Exception:
        return None


def _draw_strip(frames, image: PackImage):
    """底部条带按目标宽度缩放后纵向拼接；不裁切。"""
    canvas = Image.new("RGB", (image.width, image.height), _EMPTY)
    y = 0
    for idx in image.indices:
        part = _decode_jpeg(getattr(frames[idx], "bottom_jpeg", None))
        if part is None:
            continue
        if part.width != image.width:
            height = max(2, int(round(part.height * image.width
                                      / max(1, part.width))))
            height -= height % 2
            part = part.resize((image.width, height),
                               Image.Resampling.LANCZOS)
        if y + part.height > canvas.height:
            break
        canvas.paste(part, (0, y))
        y += part.height
    return canvas if y > 0 else None


def _draw_grid(frames, image: PackImage):
    canvas = Image.new("RGB", (image.width, image.height), _EMPTY)
    cell_w = image.width // image.cols
    cell_h = image.height // image.rows
    font = _font(18)
    for slot, idx in enumerate(image.indices):
        row, col = divmod(slot, image.cols)
        if row >= image.rows:
            break
        x, y = col * cell_w, row * cell_h
        thumb = _decode(frames[idx])
        if thumb is not None:
            fitted = _fit_resolution(thumb, cell_w, cell_h)
            ox = x + (cell_w - fitted.width) // 2
            oy = y + (cell_h - fitted.height) // 2
            canvas.paste(fitted, (ox, oy))
            label = _stamp(frames[idx].ts)
        else:
            label = "空"
        draw = ImageDraw.Draw(canvas, "RGBA")
        box = draw.textbbox((0, 0), label, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        draw.rectangle((x + 4, y + 4, x + 12 + tw, y + 10 + th),
                       fill=(0, 0, 0, 150))
        draw.text((x + 8, y + 6), label, font=font, fill=(255, 255, 255, 230))
    draw = ImageDraw.Draw(canvas)
    for col in range(1, image.cols):
        x = col * cell_w
        draw.line((x, 0, x, image.height), fill=_LINE, width=2)
    for row in range(1, image.rows):
        y = row * cell_h
        draw.line((0, y, image.width, y), fill=_LINE, width=2)
    # 空位显式写“空”，避免看起来像截图失败。
    slot_count = image.cols * image.rows
    for slot in range(len(image.indices), slot_count):
        row, col = divmod(slot, image.cols)
        x, y = col * cell_w, row * cell_h
        draw.text((x + cell_w // 2 - 8, y + cell_h // 2 - 8), "空",
                  font=font, fill=(120, 120, 120))
    return canvas


def render(meta: FramePackMeta, frames: list,
           latest_full_img=None,
           full_images: dict[int, object] | None = None
           ) -> list[tuple[str, str]]:
    """解码缩略图、拼图、编码；任何一张失败都不抛异常。

    `full_images` 按帧索引提供全分辨率图；当前 Runtime 只传一张（最新帧），
    未来要保留多张高清帧时直接扩这个映射，不改变布局协议。
    """
    frames = list(frames or [])
    full_images = full_images or {}
    out: list[tuple[str, str]] = []
    grids = [img for img in meta.images if img.kind == "grid"]
    total_grids = len(grids)
    grid_seen = 0
    for image in meta.images:
        try:
            if image.kind == "strip":
                canvas = _draw_strip(frames, image)
                if canvas is None:
                    continue
                data = encode_jpeg(canvas)
                first = frames[image.indices[0]].ts
                last = frames[image.indices[-1]].ts
                label = ("[历史台词条带（画面背景未变，仅截取底部对话框"
                         "按从上到下递增）"
                         f" {_stamp(first)}–{_stamp(last)}]")
            elif image.kind == "grid":
                canvas = _draw_grid(frames, image)
                data = encode_jpeg(canvas)
                grid_seen += 1
                first = frames[image.indices[0]].ts
                last = frames[image.indices[-1]].ts
                label = (f"[历史画面 {grid_seen}/{total_grids} "
                         f"({image.cols}×{image.rows}, "
                         f"{_stamp(first)}–{_stamp(last)}，"
                         f"左上→右下从新到旧)]")
            else:
                idx = image.indices[0]
                frame = frames[idx]
                full = full_images.get(idx)
                if full is None and image.current:
                    full = latest_full_img
                if full is None and getattr(frame, "extra_jpeg", None):
                    full = _decode_jpeg(frame.extra_jpeg)
                if full is not None:
                    if idx in meta.high_res:
                        target = (image.width, image.height)
                        if full.size != target:
                            if target[0] <= full.width and target[1] <= full.height:
                                full = full.resize(target, Image.Resampling.LANCZOS)
                            else:
                                full = _fit_resolution(full, *target)
                    data = encode_jpeg(full)
                    if image.current:
                        tag = "语音快速" if getattr(frame, "quick", False) else "此刻最新"
                        label = f"[当前画面 {_stamp(frame.ts)}（{tag}）]"
                    elif getattr(frame, "extra_jpeg", None):
                        label = f"[上一张高清 {_stamp(frame.ts)}]"
                    else:
                        label = (f"[历史画面 {_stamp(frame.ts)}"
                                 f"（高清）]")
                else:
                    thumb = _decode(frame)
                    if thumb is None:
                        continue
                    # 尾巴单张有最小像素目标；等比放大到不超出目标框，
                    # 避免把 640×360 以下的小图直接交给服务端再放大。
                    if not image.current and image.width > 0 and image.height > 0:
                        scale = max(1.0, min(image.width / thumb.width,
                                             image.height / thumb.height))
                        if scale > 1.0:
                            size = (max(2, int(thumb.width * scale)),
                                    max(2, int(thumb.height * scale)))
                            thumb = thumb.resize(size, Image.Resampling.LANCZOS)
                    data = encode_jpeg(thumb)
                    name = "当前画面（缩略）" if image.current else "历史画面"
                    label = f"[{name} {_stamp(frame.ts)}]"
            out.append((label, base64.b64encode(data).decode("ascii")))
        except Exception:
            continue
    return out
