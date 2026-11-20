from __future__ import annotations

import base64
import io
import time
import unittest
from unittest import mock

from PIL import Image

from sidekick import config
from sidekick.input.capture import Frame, encode_jpeg
from sidekick.input.composer import (
    FramePackMeta, _draw_grid, _select_keyframes, plan, render,
)


def _jpeg(color=(40, 80, 120), size=(96, 54)) -> bytes:
    return encode_jpeg(Image.new("RGB", size, color))


def _frames(n: int) -> list[Frame]:
    return [Frame(1000.0 + i * 5.0, _jpeg((i * 7 % 255, 40, 80)),
                  bytes(32 * 24), 0.1 * i) for i in range(n)]


def _cfg(*, mode="active"):
    cfg = config.defaults()
    cfg["capture"].update({
        "mode": mode,
        "clarity_percent": 66.6,
        "resolution": "1920x1080",
        "max_frames": 28 if mode == "active" else 6,
    })
    return cfg


class KeyframeTests(unittest.TestCase):
    def test_budget_one_never_exceeds_one_frame(self):
        frames = _frames(20)
        picked = _select_keyframes(frames, 1)
        self.assertEqual(1, len(picked))

    def test_selects_peak_per_time_bucket_and_keeps_first(self):
        frames = _frames(20)
        for i, frame in enumerate(frames):
            frame.delta_score = 0.0
        frames[3].delta_score = 99
        frames[15].delta_score = 88
        picked = _select_keyframes(frames, 5)
        self.assertIs(frames[0], picked[0])
        self.assertIn(frames[3], picked)
        self.assertIn(frames[15], picked)
        self.assertEqual(5, len(picked))


class GridPlanTests(unittest.TestCase):
    def test_game_boundaries_cover_expected_layouts(self):
        expected = {
            0: 0, 1: 1, 2: 2, 3: 3,
            4: 2, 5: 2, 6: 3, 10: 4,
            11: 5, 14: 5, 15: 6, 16: 5,
            17: 5, 18: 4, 20: 6, 23: 6,
            25: 6, 26: 6, 27: 5, 28: 5, 30: 5,
        }
        for n, count in expected.items():
            meta = plan(_frames(n), _cfg())
            self.assertEqual(count, len(meta.images), n)
            self.assertEqual(n, meta.source_count)
            self.assertTrue(meta.images[0].current if n else True, n)
            if n:
                self.assertEqual((n - 1,), meta.high_res, n)

    def test_video_boundaries_cap_history_at_eight(self):
        for n in (0, 1, 2, 3, 4, 5, 6, 10, 11, 14, 15, 19, 25):
            meta = plan(_frames(n), _cfg(mode="calm"))
            self.assertLessEqual(len(meta.images), 3, n)
            for image in meta.images:
                self.assertLessEqual(max(image.cols, image.rows), 3)
                self.assertNotEqual((image.cols, image.rows), (4, 4))

    def test_grid_is_always_used_and_current_is_only_high_res(self):
        frames = _frames(30)
        meta = plan(frames, _cfg())
        self.assertEqual(5, len(meta.images))
        self.assertEqual((29,), meta.high_res)
        self.assertTrue(meta.images[0].current)
        self.assertTrue(all(not image.current for image in meta.images[1:]))
        for image in meta.images[1:]:
            self.assertLessEqual(max(image.cols, image.rows), 3)
            self.assertNotEqual((image.cols, image.rows), (4, 4))

    def test_quick_capture_adds_second_high_res_before_grids(self):
        frames = _frames(30)
        frames[28].extra_jpeg = _jpeg((200, 20, 20))
        meta = plan(frames, _cfg())
        self.assertEqual((29, 28), meta.high_res)
        self.assertTrue(meta.images[0].current)
        self.assertFalse(meta.images[1].current)
        self.assertEqual((29, 28), meta.selected[:2])
        self.assertEqual(6, len(meta.images))
        self.assertIn("2 张高清", meta.summary)

    def test_quick_highs_survive_tiny_max_frames(self):
        frames = _frames(3)
        frames[1].extra_jpeg = _jpeg((200, 20, 20))
        cfg = _cfg()
        cfg["capture"]["max_frames"] = 1
        meta = plan(frames, cfg)
        self.assertEqual((2, 1), meta.high_res)
        self.assertEqual({1, 2}, set(meta.selected))

    def test_mode_max_frames_caps_history_but_keeps_source_count(self):
        cfg = _cfg(mode="calm")
        cfg["capture"]["max_frames"] = 5
        meta = plan(_frames(6), cfg)
        self.assertEqual(6, meta.source_count)
        self.assertLessEqual(len(meta.selected), 5)
        self.assertTrue(meta.images[0].current)
        self.assertEqual(2, len(meta.images))

    def test_plan_does_not_decode_or_encode(self):
        with mock.patch("sidekick.input.composer.Image.open",
                        side_effect=AssertionError("decode")), \
                mock.patch("sidekick.input.composer.encode_jpeg",
                           side_effect=AssertionError("encode")):
            meta = plan(_frames(10), _cfg())
        self.assertIsInstance(meta, FramePackMeta)
        self.assertEqual(4, len(meta.images))


class GridRenderTests(unittest.TestCase):
    def test_render_makes_valid_jpegs_and_labels(self):
        frames = _frames(4)
        meta = plan(frames, _cfg())
        latest = Image.new("RGB", (128, 72), (200, 200, 200))
        out = render(meta, frames, latest)
        self.assertEqual(2, len(out))
        for label, b64 in out:
            self.assertTrue(label.startswith("["))
            raw = base64.b64decode(b64)
            with Image.open(io.BytesIO(raw)) as img:
                self.assertEqual("JPEG", img.format)
        self.assertIn("当前画面", out[0][0])
        self.assertIn("历史画面", out[-1][0])

    def test_render_can_use_explicit_full_image_mapping(self):
        frames = _frames(4)
        meta = plan(frames, _cfg())
        latest = Image.new("RGB", (128, 72), (200, 20, 20))
        out = render(meta, frames, None,
                     full_images={meta.high_res[0]: latest})
        self.assertEqual(2, len(out))
        with Image.open(io.BytesIO(base64.b64decode(out[0][1]))) as img:
            self.assertEqual((128, 72), img.size)
            pixel = img.getpixel((0, 0))
            self.assertLess(sum(abs(a - b) for a, b in zip(
                (200, 20, 20), pixel)), 8, pixel)

    def test_render_quick_extra_high_res(self):
        frames = _frames(2)
        frames[0].extra_jpeg = _jpeg((20, 20, 240), size=(1280, 720))
        meta = plan(frames, _cfg())
        out = render(meta, frames, Image.new("RGB", (1280, 720)))
        self.assertEqual(2, len(out))
        self.assertIn("当前画面", out[0][0])
        self.assertIn("上一张高清", out[1][0])
        with Image.open(io.BytesIO(base64.b64decode(out[1][1]))) as img:
            self.assertEqual((1280, 720), img.size)

    def test_history_tier_sequence_matches_active_policy(self):
        # 17 张历史：2×2 + 2×2 + 3×3；25 张历史：4 个 2×2 + 3×3；
        # 26 张历史：2 个 2×2 + 2 个 3×3。
        for total, want in (
                (18, [("2x2", 4), ("2x2", 4), ("3x3", 9)]),
                (26, [("2x2", 4), ("2x2", 4), ("2x2", 4), ("2x2", 4), ("3x3", 9)]),
                (27, [("2x2", 4), ("2x2", 4), ("3x3", 9), ("3x3", 9)]),
        ):
            meta = plan(_frames(total), _cfg())
            got = [(f"{img.cols}x{img.rows}", len(img.indices))
                   for img in meta.images[1:]]
            self.assertEqual(want, got, total)

    def test_clarity_scales_high_only_and_grids_stay_1920(self):
        for pct, size in ((66.6, (1280, 720)), (83.3, (1600, 900)),
                          (100, (1920, 1080))):
            cfg = _cfg()
            cfg["capture"]["clarity_percent"] = pct
            meta = plan(_frames(4), cfg)
            self.assertEqual(size, (meta.images[0].width, meta.images[0].height))
            grid = meta.images[1]
            self.assertEqual((1920, 1080), (grid.width, grid.height))

    def test_non_16_9_and_small_sources_do_not_upscale(self):
        cases = (
            ((1728, 1080), (1152, 720), (1728, 1080), (864, 540)),
            ((1440, 1080), (960, 720), (1440, 1080), (720, 540)),
            ((1920, 804), (1280, 536), (1920, 804), (960, 402)),
            ((1280, 720), (1280, 720), (1280, 720), (640, 360)),
        )
        for source, high, canvas, cell2 in cases:
            frames = _frames(4)
            for frame in frames:
                frame.size = source
            meta = plan(frames, _cfg())
            self.assertEqual(high, (meta.images[0].width,
                                    meta.images[0].height), source)
            grid = meta.images[1]
            self.assertEqual(canvas, (grid.width, grid.height), source)
            self.assertEqual((cell2[0] * 2, cell2[1] * 2),
                             (grid.width, grid.height), source)

    def test_small_4_3_source_uses_canonical_box_without_upscale(self):
        frames = _frames(4)
        for frame in frames:
            frame.size = (1024, 768)
        low = plan(frames, _cfg())
        self.assertEqual((960, 720), (low.images[0].width,
                                     low.images[0].height))
        cfg = _cfg()
        cfg["capture"]["clarity_percent"] = 100
        high = plan(frames, cfg)
        self.assertEqual((1024, 768), (high.images[0].width,
                                      high.images[0].height))

    def test_high_plan_size_matches_rendered_size(self):
        frames = _frames(4)
        for frame in frames:
            frame.size = (1280, 720)
        meta = plan(frames, _cfg())
        out = render(meta, frames, Image.new("RGB", (1280, 720)))
        with Image.open(io.BytesIO(base64.b64decode(out[0][1]))) as img:
            self.assertEqual((meta.images[0].width, meta.images[0].height),
                             img.size)

    def test_history_tail_single_is_upscaled_to_minimum_box(self):
        frames = _frames(2)
        meta = plan(frames, _cfg())
        out = render(meta, frames, None)
        self.assertEqual(2, len(out))
        with Image.open(io.BytesIO(base64.b64decode(out[-1][1]))) as img:
            self.assertEqual((960, 540), img.size)

    def test_empty_cells_are_not_pure_black(self):
        frames = _frames(4)             # 3 历史 + 当前：2×2 有一个空位
        meta = plan(frames, _cfg())
        grid = [im for im in meta.images if im.kind == "grid"][0]
        self.assertEqual(4, grid.cols * grid.rows)
        canvas = _draw_grid(frames, grid)
        cell_w, cell_h = grid.width // grid.cols, grid.height // grid.rows
        # 第 4 格是空位；底色 #1E1E1E，不能是纯黑。
        self.assertNotEqual((0, 0, 0),
                            canvas.getpixel((cell_w + cell_w // 2,
                                             cell_h + cell_h // 2)))

    def test_render_19_frames_finishes_within_budget(self):
        frames = _frames(19)
        meta = plan(frames, _cfg())
        latest = Image.new("RGB", (1280, 720), (80, 90, 100))
        started = time.monotonic()
        out = render(meta, frames, latest)
        elapsed = time.monotonic() - started
        self.assertEqual(5, len(out))
        self.assertLess(elapsed, 0.5, elapsed)


if __name__ == "__main__":
    unittest.main()
