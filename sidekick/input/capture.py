"""截屏：一帧一帧抓，攒着，等她说话的时候一次性带走。

dxcam 优先（DX12 全屏独占也能抓），失败退回 PIL ImageGrab。

**关键坑**：dxcam 的 grab() 在"没有新帧"时返回 None——游戏暂停、菜单、掉帧都会。
所以必须带 new_frame_only=False，否则冷场心跳会静默死掉（报告 §2.3 / R5）。

两种用法：

  · `Capturer`   —— 抓一张，给自检脚本/排查用
  · `FrameQueue` —— 后台按场景模式的间隔抓，一轮开始时**整批取走并且释放**（陪玩主循环用）
"""
from __future__ import annotations

import io
import threading
import time

from sidekick.input.window_context import NullWindowContext

_cam_lock = threading.Lock()


def _resolution(value) -> tuple[int, int]:
    w, h = str(value or "1280x720").lower().split("x", 1)
    return max(1, int(w)), max(1, int(h))


def _thumb_size(width: int, height: int) -> tuple[int, int]:
    """缩略图最大框：约定为当前截图框的一半，偶数对齐。"""
    tw = max(2, int(width) // 2)
    th = max(2, int(height) // 2)
    return tw - tw % 2, th - th % 2


def _strip_enabled(capture: dict) -> bool:
    """字幕条带自动判断；开关只用于显式关闭，不再绑定场景模式。"""
    return bool(capture.get("subtitle_strip_enabled"))


#: 条带触发口径：上部 4 行（约 2/3）几乎静止，后 2 行（约 1/3）字幕在翻页。
STRIP_UPPER_MAX = 1.8
STRIP_BOTTOM_MIN = 6.0


def _fit_resolution(img, width: int, height: int):
    """按最大框等比缩小；不放大、不裁切，偶数对齐。

    16:9 / 4:3 / 16:10 都只是源比例：1920×1080 在 1280×720 框里是
    1280×720，1600×1200 是 960×720，1920×1200 是 1152×720。
    """
    if img.size == (width, height):
        return img
    scale = min(width / img.width, height / img.height, 1.0)
    new_w = min(img.width, max(1, round(img.width * scale)))
    new_h = min(img.height, max(1, round(img.height * scale)))
    if new_w > 1:
        new_w -= new_w % 2
    if new_h > 1:
        new_h -= new_h % 2
    if (new_w, new_h) == img.size:
        return img
    from PIL import Image

    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)


def encode_jpeg(img, quality: int = 85, subsampling: int = 0) -> bytes:
    buf = io.BytesIO()
    # 4:4:4 保彩色字幕/UI 描边；q85 比 q100 小很多，视觉 token 不变。
    img.save(buf, format="JPEG", quality=quality,
             subsampling=subsampling, optimize=True)
    return buf.getvalue()


class Capturer:
    def __init__(self, cfg: dict) -> None:
        c = cfg["capture"]
        self.monitor = int(c["monitor"])
        self.width, self.height = _resolution(c["resolution"])
        self.backend = str(c["backend"]).lower()
        self._cam = None
        self._cam_failed = False
        self.last_error: str | None = None
        self.last_size: tuple[int, int] | None = None
        self.source_size: tuple[int, int] | None = None
        self.monitor_rect = self._monitor_rect()

    def _pil_bbox(self):
        """PIL 回退也要抓 capture.monitor 指定的屏，不能永远抓主屏。"""
        try:
            import win32api

            monitors = win32api.EnumDisplayMonitors()
            if not monitors:
                return None
            index = max(0, min(int(self.monitor), len(monitors) - 1))
            return tuple(win32api.GetMonitorInfo(monitors[index][0])["Monitor"])
        except Exception:
            return None

    def _monitor_rect(self):
        """目标显示器在桌面坐标里的 (x, y, w, h)；拿不到返回 None。"""
        try:
            import win32api

            monitors = win32api.EnumDisplayMonitors()
            if not monitors:
                return None
            index = max(0, min(int(self.monitor), len(monitors) - 1))
            left, top, right, bottom = win32api.GetMonitorInfo(
                monitors[index][0])["Monitor"]
            return int(left), int(top), int(right - left), int(bottom - top)
        except Exception:
            return None

    def to_image_rect(self, rect):
        """把桌面坐标的 (x,y,w,h) 映射到当前输出图坐标；拿不到尺寸返回 None。"""
        if not rect or len(rect) != 4 or not self.source_size or not self.last_size:
            return None
        try:
            x, y, w, h = (float(v) for v in rect)
        except (TypeError, ValueError):
            return None
        sw, sh = self.source_size
        ow, oh = self.last_size
        if sw <= 0 or sh <= 0:
            return None
        mx, my = ((self.monitor_rect[0], self.monitor_rect[1])
                  if self.monitor_rect else (0, 0))
        sx, sy = ow / sw, oh / sh
        return ((x - mx) * sx, (y - my) * sy, w * sx, h * sy)

    # ── 后端 ──────────────────────────────────────────
    def _dxcam(self):
        if self._cam is not None or self._cam_failed:
            return self._cam
        try:
            import dxcam

            # processor_backend="numpy"：换色用轮子自带的 Cython 内核，
            # 不拉 opencv（默认的 cv2 后端没装 opencv 时会直接抛 ImportError）
            base = {"output_idx": self.monitor, "output_color": "RGB"}
            if self.backend == "winrt":
                base["backend"] = "winrt"
            for extra in ({"processor_backend": "numpy"}, {}):
                try:
                    self._cam = dxcam.create(**base, **extra)
                except Exception:
                    self._cam = None
                if self._cam is not None:
                    break
            if self._cam is None:
                # create 返回 None（没输出/显示器编号不对）也会走这里。
                # 不标记失败的话，每次抓屏都会重试创建，白烧 CPU。
                self._cam_failed = True
                self.last_error = self.last_error or "dxcam.create 返回 None"
        except Exception as e:  # 没装、没显卡、独占模式抓不到……
            self._cam_failed = True
            self.last_error = f"dxcam 不可用：{type(e).__name__}: {e}"
        return self._cam

    def _grab_array(self):
        cam = self._dxcam()
        if cam is not None:
            try:
                with _cam_lock:
                    frame = cam.grab(new_frame_only=False)  # ← 这个参数不能省
                if frame is not None:
                    return frame
            except Exception as e:
                self.last_error = f"dxcam.grab 失败：{e}"
        # 退回 PIL
        try:
            from PIL import ImageGrab

            bbox = self._pil_bbox()
            if bbox is not None:
                img = ImageGrab.grab(bbox=bbox, all_screens=True)
            else:
                img = ImageGrab.grab(all_screens=False)
            return img
        except Exception as e:
            self.last_error = f"PIL 也抓不到：{e}"
            return None

    # ── 对外 ──────────────────────────────────────────
    def grab_pil(self):
        raw = self._grab_array()
        if raw is None:
            return None
        try:
            from PIL import Image

            if isinstance(raw, Image.Image):
                img = raw
            else:  # numpy HxWx3 RGB（dxcam 直接输出 RGB，不走 BGRA→RGBA 那条会炸的路）
                img = Image.fromarray(raw[:, :, :3])
        except Exception as e:
            self.last_error = f"转图失败：{e}"
            return None

        self.source_size = img.size
        img = _fit_resolution(img, self.width, self.height)
        self.last_size = img.size
        # dxcam 不可用后 PIL 回退成功时，不能把“dxcam 不可用”永久挂在状态里。
        self.last_error = None
        return img

    def encode(self, img, quality: int = 85,
               subsampling: int = 0) -> bytes:
        return encode_jpeg(img, quality=quality, subsampling=subsampling)

    def save_png(self, out_path) -> str | None:
        img = self.grab_pil()
        if img is None:
            return None
        img.save(out_path, format="PNG")
        return str(out_path)


# ── 累积式截图 ────────────────────────────────────────
class Frame:
    __slots__ = ("ts", "thumb_jpeg", "sig_blocks", "delta_score",
                 "bottom_jpeg", "bottom_size", "upper_diff", "bottom_diff",
                 "extra_jpeg", "quick", "size")

    def __init__(self, ts: float, thumb_jpeg: bytes, sig_blocks: bytes,
                 delta_score: float = 0.0, *,
                 bottom_jpeg: bytes | None = None,
                 bottom_size: tuple[int, int] | None = None,
                 upper_diff: float = 0.0,
                 bottom_diff: float = 0.0,
                 extra_jpeg: bytes | None = None,
                 quick: bool = False,
                 size: tuple[int, int] | None = None) -> None:
        self.ts = ts
        self.thumb_jpeg = thumb_jpeg
        self.sig_blocks = sig_blocks
        self.delta_score = delta_score
        self.bottom_jpeg = bottom_jpeg
        self.bottom_size = bottom_size
        self.upper_diff = upper_diff
        self.bottom_diff = bottom_diff
        self.extra_jpeg = extra_jpeg
        self.quick = quick
        self.size = size


class FrameQueue:
    """后台按间隔抓，攒成队列；一轮开始时整批取走并清空。

    为什么要「攒」而不是每轮现抓：现抓只有"此刻"，模型不知道刚才发生了什么。
    攒着的一批是"这段时间的连续剧"；队列只留缩略图，最新一张全图单独存一份，
    避免 60 帧全尺寸 base64 常驻。

    攒的两个规矩：
      · 画面没变就合并（菜单/暂停时不会攒出 6 张一样的）
      · 取走即释放，不跨轮复用；选帧/拼图交给 composer
    """

    def __init__(self, cfg: dict, *, pet_rect_provider=None,
                 window_context=None) -> None:
        c = cfg["capture"]
        self.mode = str(c.get("mode") or "active")
        self.interval = max(1.0, float(c["interval_s"]))
        self.max_frames = max(1, int(c["max_frames"]))
        self.dedupe = bool(c["dedupe"])
        self.dedupe_global = float(c["dedupe_global_threshold"])
        self.dedupe_block = float(c["dedupe_block_threshold"])
        self.subtitle_weight = float(c["subtitle_roi_weight"])
        self.capture_now_stale = float(c["capture_now_stale_s"])
        self.subtitle_strip = _strip_enabled(c)
        self.ring = max(8, int(c["ring_size"]))
        self.pet_rect_provider = pet_rect_provider
        self.window = (window_context if window_context is not None
                       else NullWindowContext())
        self.privacy_active = False

        self.cap = Capturer(cfg)
        self.thumb_w, self.thumb_h = _thumb_size(self.cap.width,
                                                 self.cap.height)
        self.enabled = bool(c["enabled"])

        self._q: list[Frame] = []
        self._latest_full_img = None
        self._latest_ts = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._now_event = threading.Event()
        self._grabbed_event = threading.Event()
        self._pending_cfg: dict | None = None
        self._reload_event = threading.Event()
        self._next_capture = 0.0
        self._th: threading.Thread | None = None
        self.grabbed = 0
        self.merged = 0
        self.last_error: str | None = None

    # ── 生命周期 ──────────────────────────────────────
    def start(self) -> None:
        if not self.enabled:
            return
        if self._th is not None and self._th.is_alive():
            return
        if self._reload_event.is_set():
            self._apply_reload()
        self._stop.clear()
        self._wake.clear()
        self._now_event.clear()
        self._grabbed_event.clear()
        self._next_capture = 0.0
        self._th = threading.Thread(target=self._loop, name="frames", daemon=True)
        self._th.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def reload(self, cfg: dict) -> None:
        """投递配置；dxcam 的 Capturer 只在 capture 线程里重建。"""
        self._pending_cfg = cfg
        self._reload_event.set()
        self._wake.set()

    def latest_ts(self) -> float:
        with self._lock:
            return float(self._latest_ts or 0.0)

    def capture_now(self, timeout_s: float = 0.12) -> bool:
        """让 capture 线程立刻抓一张；不另起线程、不抢 dxcam。"""
        if (self.privacy_active or not self.enabled or self._th is None
                or not self._th.is_alive()):
            return False
        self._grabbed_event.clear()
        self._now_event.set()
        self._wake.set()
        return bool(self._grabbed_event.wait(max(0.0, float(timeout_s))))

    def _apply_reload(self) -> None:
        cfg = self._pending_cfg
        self._pending_cfg = None
        self._reload_event.clear()
        if cfg is None:
            return
        c = cfg["capture"]
        width, height = _resolution(c["resolution"])
        rebuild = (width, height) != (self.cap.width, self.cap.height)
        self.mode = str(c.get("mode") or "active")
        self.interval = max(1.0, float(c["interval_s"]))
        self.max_frames = max(1, int(c["max_frames"]))
        self.dedupe = bool(c["dedupe"])
        self.dedupe_global = float(c["dedupe_global_threshold"])
        self.dedupe_block = float(c["dedupe_block_threshold"])
        self.subtitle_weight = float(c["subtitle_roi_weight"])
        self.capture_now_stale = float(c["capture_now_stale_s"])
        self.subtitle_strip = _strip_enabled(c)
        self.ring = max(8, int(c["ring_size"]))
        self.window.reload(cfg)
        self._next_capture = 0.0
        if rebuild:
            with self._lock:
                self._q.clear()
                self._latest_full_img = None
                self._latest_ts = 0.0
            self.cap = Capturer(cfg)
            self.thumb_w, self.thumb_h = _thumb_size(self.cap.width,
                                                     self.cap.height)
        with self._lock:
            if len(self._q) > self.ring:
                del self._q[: len(self._q) - self.ring]

    # ── 采样 ──────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._reload_event.is_set():
                try:
                    self._apply_reload()
                except Exception as e:       # 坏配置不能让 capture 线程静默死掉
                    self._reload_event.clear()
                    self.last_error = f"reload 失败：{type(e).__name__}: {e}"
            now = time.time()
            if self._now_event.is_set() or now >= self._next_capture:
                try:
                    self._tick()
                except Exception as e:       # 抓不到不该弄死整个程序
                    self.last_error = f"{type(e).__name__}: {e}"
                self._next_capture = time.time() + self.interval
                if self._now_event.is_set():
                    self._now_event.clear()
                    self._grabbed_event.set()
            else:
                # 抓屏间隔可能比隐私采样长（chat 6 秒）；独立醒来查窗口，
                # 命中隐私就立刻清队列，不能等下一次真正抓屏。
                try:
                    self._window_ok()
                except Exception as e:
                    self.last_error = f"窗口检查失败：{type(e).__name__}: {e}"
            wait_s = max(0.2, min(2.0, self._next_capture - time.time()))
            self._wake.wait(wait_s)
            self._wake.clear()

    def _tick(self) -> None:
        now = time.time()
        if not self._window_ok():
            return
        with self._lock:
            quick = self._now_event.is_set()
            old_full = self._latest_full_img if quick else None
        img = self.cap.grab_pil()
        if img is None:
            self.last_error = self.cap.last_error or "抓不到画面"
            return
        sig = _signature(img)
        delta = 0.0
        upper_diff = 0.0
        bottom_diff = 0.0
        with self._lock:
            if self._q and (self.dedupe or self.subtitle_strip):
                weights = _block_weights(
                    img.size, self._pet_rect(), self.subtitle_weight)
                (global_diff, max_diff, delta,
                 upper_diff, bottom_diff) = _compare(
                    self._q[-1].sig_blocks, sig, weights)
                keep_subtitle = (self.subtitle_strip
                                 and upper_diff < STRIP_UPPER_MAX
                                 and bottom_diff >= STRIP_BOTTOM_MIN)
                if (self.dedupe and not quick and not keep_subtitle
                        and global_diff < self.dedupe_global
                        and max_diff < self.dedupe_block):
                    # 画面没动：时间推到最新，复用上一张缩略图，不再编码。
                    prev = self._q[-1]
                    self._q[-1] = Frame(
                        now, prev.thumb_jpeg, sig, delta,
                        bottom_jpeg=prev.bottom_jpeg,
                        bottom_size=prev.bottom_size,
                        upper_diff=upper_diff,
                        bottom_diff=bottom_diff,
                        quick=False, size=img.size)
                    self._latest_full_img = img
                    self._latest_ts = now
                    self.merged += 1
                    self.last_error = None
                    return

        thumb_img = _fit_resolution(img, self.thumb_w, self.thumb_h)
        thumb = self.cap.encode(thumb_img)
        bottom_jpeg = None
        bottom_size = None
        if self.subtitle_strip:
            bottom_h = max(1, int(img.height * 0.25))
            bottom = img.crop((0, img.height - bottom_h, img.width, img.height))
            bottom_jpeg = self.cap.encode(bottom)
            bottom_size = bottom.size
        extra_jpeg = (self.cap.encode(old_full)
                      if quick and old_full is not None else None)
        with self._lock:
            if extra_jpeg is not None and self._q:
                # 快速帧保留前一张全图；只保留最近一次快速触发的这一张。
                for frame in self._q:
                    frame.extra_jpeg = None
                self._q[-1].extra_jpeg = extra_jpeg
            self._q.append(Frame(
                now, thumb, sig, delta, bottom_jpeg=bottom_jpeg,
                bottom_size=bottom_size, upper_diff=upper_diff,
                bottom_diff=bottom_diff, quick=quick, size=img.size))
            self._latest_full_img = img
            self._latest_ts = now
            if len(self._q) > self.ring:
                del self._q[: len(self._q) - self.ring]
            self.grabbed += 1
        self.last_error = None

    def _window_ok(self) -> bool:
        """Privacy 命中时清空旧帧并跳过抓屏，避免切走前的画面继续上传。"""
        try:
            self.window.sample()
        except Exception as e:       # 窗口 API 失败不能弄死 capture 线程
            self.last_error = f"窗口采样失败：{type(e).__name__}: {e}"
        if not self.window.privacy_active:
            self.privacy_active = False
            return True
        with self._lock:
            self._q.clear()
            self._latest_full_img = None
            self._latest_ts = 0.0
        self.privacy_active = True
        self.last_error = None
        return False

    def _pet_rect(self):
        if self.pet_rect_provider is None:
            return None
        try:
            return self.cap.to_image_rect(self.pet_rect_provider())
        except Exception:
            return None

    # ── 取走 ──────────────────────────────────────────
    def take(self) -> list[Frame]:
        """这一轮的全部画面，旧→新。取走即释放；选帧交给 composer。"""
        with self._lock:
            items = self._q
            self._q = []
        return items

    def peek(self) -> int:
        with self._lock:
            return len(self._q)

    def preview(self) -> list[Frame]:
        """这一轮**会**发哪些原始帧（不取走）。给 /peek 用。"""
        with self._lock:
            items = list(self._q)
        return items

    def latest_snapshot(self):
        """一次锁内返回 (最新全图, ts)，避免图和时间来自不同帧。"""
        with self._lock:
            return self._latest_full_img, float(self._latest_ts or 0.0)

    def stats(self) -> dict:
        size = self.cap.last_size or (self.cap.width, self.cap.height)
        return {"enabled": self.enabled, "mode": self.mode,
                "interval_s": self.interval,
                "resolution": f"{size[0]}x{size[1]}",
                "pending": self.peek(), "grabbed": self.grabbed,
                "merged": self.merged, "max_frames": self.max_frames,
                "subtitle_strip": self.subtitle_strip,
                "privacy_active": bool(self.privacy_active),
                "window_error": self.window.stats().get("error"),
                "error": self.last_error or self.cap.last_error}


# ── 去重 / 抽稀 ───────────────────────────────────────
def _signature(img) -> bytes:
    """32×24 YCbCr 指纹；按 8×6 个 4×4 区块做加权差值。"""
    from PIL import Image

    return img.convert("YCbCr").resize((32, 24), Image.Resampling.BOX).tobytes()


def _block_diffs(a: bytes, b: bytes):
    """返回 8×6 的每块平均色度敏感差；长度不对返回 None。

    每像素 d = max(|ΔY|, 1.25 × max(|ΔCb|, |ΔCr|))：亮度变化保持原语义，
    等亮度的绿血条→红血条、技能图标变色仍能被检出。
    """
    import numpy as np

    need = 32 * 24 * 3
    if not a or not b or len(a) != need or len(b) != need:
        return None
    x = np.frombuffer(a, dtype=np.uint8).astype(np.int16).reshape(24, 32, 3)
    y = np.frombuffer(b, dtype=np.uint8).astype(np.int16).reshape(24, 32, 3)
    dy = np.abs(x[:, :, 0] - y[:, :, 0]).astype(np.float32)
    dc = np.maximum(np.abs(x[:, :, 1] - y[:, :, 1]),
                    np.abs(x[:, :, 2] - y[:, :, 2])).astype(np.float32)
    diff = np.maximum(dy, 1.25 * dc)
    return diff.reshape(6, 4, 8, 4).mean(axis=(1, 3))


def _block_weights(size, pet_rect, subtitle_weight: float):
    """8×6 权重：桌宠重叠块 0.08；底部两行乘字幕权重（桌宠块不被放大）。"""
    import numpy as np

    width, height = size
    weights = np.ones((6, 8), dtype=float)
    pet = np.zeros((6, 8), dtype=bool)
    bw, bh = width / 8.0, height / 6.0
    if pet_rect and bw > 0 and bh > 0:
        px, py, pw, ph = pet_rect
        px2, py2 = px + pw, py + ph
        for row in range(6):
            for col in range(8):
                bx, by = col * bw, row * bh
                ix = max(0.0, min(bx + bw, px2) - max(bx, px))
                iy = max(0.0, min(by + bh, py2) - max(by, py))
                if ix * iy > 0.3 * bw * bh:
                    pet[row, col] = True
    weights[4:, :] *= max(0.0, float(subtitle_weight))
    # 字幕权重再大也不能把桌宠块重新变成“有效字幕块”。
    weights[pet] = 0.08
    return weights


def _compare(a: bytes, b: bytes, weights):
    """返回 (加权全局差, 加权最大块差, delta_score, upper_diff, bottom_diff)。

    差值是 32×24 YCbCr 的逐像素色度敏感值；upper_diff = 前 4 行（约上部 2/3），
    bottom_diff = 后 2 行（约下部 1/3）。
    """
    import numpy as np

    diff = _block_diffs(a, b)
    if diff is None:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    weighted = diff * weights
    total = float(weights.sum()) or 1.0
    global_diff = float(weighted.sum() / total)
    # 桌宠块权重是 0.08；色度差再大也不能让它单独触发去重。
    active = weights > 0.2
    max_diff = float(np.max(diff * np.where(
        active, np.minimum(weights, 1.5), 0.0)))
    upper_weights = weights[:4, :] * (weights[:4, :] > 0.2)
    # 字幕判定同样忽略桌宠掩码块，避免桌宠变色被当成字幕翻页。
    bottom_mask = weights[4:, :] > 0.2
    bottom_weights = weights[4:, :] * bottom_mask
    upper_total = float(upper_weights.sum()) or 1.0
    bottom_total = float(bottom_weights.sum()) or 1.0
    upper_diff = float((diff[:4, :] * upper_weights).sum() / upper_total)
    bottom_diff = float((diff[4:, :] * bottom_weights).sum() / bottom_total)
    return (global_diff, max_diff, global_diff + 0.35 * max_diff,
            upper_diff, bottom_diff)



