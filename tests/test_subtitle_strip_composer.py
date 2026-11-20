from __future__ import annotations

import base64
import io
import unittest
from unittest import mock

from PIL import Image, ImageDraw

from sidekick import config
from sidekick.input.capture import Frame, FrameQueue, encode_jpeg
from sidekick.input.composer import plan, render


def _cfg(**capture) -> dict:
    cfg = config.defaults()
    cfg["capture"].update({
        "enabled": True, "mode": "active", "interval_s": 1.0,
        "max_frames": 9, "resolution": "1600x900", "dedupe": True,
        "dedupe_global_threshold": 1.8, "dedupe_block_threshold": 8.0,
        "subtitle_strip_enabled": True,
    })
    cfg["capture"].update(capture)
    return cfg


def _thumb(color=(10, 10, 10)) -> bytes:
    return encode_jpeg(Image.new("RGB", (320, 180), color))


def _frame(index: int, *, width: int = 1600, height: int = 225,
           upper: float = 0.0, bottom: float = 7.0,
           with_bottom: bool = True) -> Frame:
    part = Image.new("RGB", (width, height), (index * 30, 20, 20))
    draw = ImageDraw.Draw(part)
    draw.text((20, 20), f"台词第 {index} 句", fill=(255, 255, 255))
    return Frame(
        float(index), _thumb(), b"", delta_score=5.0,
        bottom_jpeg=encode_jpeg(part) if with_bottom else None,
        bottom_size=(width, height) if with_bottom else None,
        upper_diff=upper, bottom_diff=bottom)


def _full(color=(20, 20, 20)) -> Image.Image:
    return Image.new("RGB", (1600, 900), color)


class SubtitleStripPlanTests(unittest.TestCase):
    def test_four_history_frames_become_one_strip(self):
        frames = [_frame(i) for i in range(5)]
        pack = plan(frames, _cfg())
        self.assertEqual("strip", pack.images[1].kind)
        self.assertEqual((1280, 720), (pack.images[1].width,
                                       pack.images[1].height))
        self.assertEqual(4, len(pack.images[1].indices))
        self.assertEqual((4, 0, 1, 2, 3), pack.selected)
        self.assertGreater(pack.tokens, 700)
        self.assertIn("底部台词条带", pack.summary)

    def test_six_history_frames_stay_within_token_gate(self):
        frames = [_frame(i) for i in range(7)]
        pack = plan(frames, _cfg())
        self.assertEqual((1280, 1080),
                         (pack.images[1].width, pack.images[1].height))
        self.assertGreater(pack.tokens, 900)

    def test_six_strip_follows_clarity_width_and_splits_when_needed(self):
        frames = [_frame(i, width=1920, height=270) for i in range(7)]
        one = plan(frames, _cfg(resolution="1920x1080",
                                clarity_percent=66.6))
        self.assertEqual((1280, 1080),
                         (one.images[1].width, one.images[1].height))
        self.assertEqual(2, len(one.images))  # 高清 + 1 张 6 句条带

        split = plan(frames, _cfg(resolution="1920x1080",
                                  clarity_percent=100))
        strips = [img for img in split.images if img.kind == "strip"]
        self.assertEqual(2, len(strips))
        self.assertEqual([(1920, 810), (1920, 810)],
                         [(img.width, img.height) for img in strips])
        self.assertEqual([3, 3], [len(img.indices) for img in strips])

    def test_more_than_six_history_frames_take_latest_six(self):
        frames = [_frame(i) for i in range(9)]
        pack = plan(frames, _cfg())
        self.assertEqual((8, 2, 3, 4, 5, 6, 7), pack.selected)
        self.assertEqual(6, len(pack.images[1].indices))

    def test_strip_uses_raw_consecutive_frames_not_keyframes(self):
        frames = [_frame(i) for i in range(19)]
        pack = plan(frames, _cfg())
        self.assertEqual("strip", pack.images[1].kind)
        self.assertEqual((18, 12, 13, 14, 15, 16, 17), pack.selected)
        self.assertEqual(19, pack.source_count)

    def test_background_switch_falls_back_to_grid(self):
        frames = [_frame(i) for i in range(5)]
        frames[2].upper_diff = 2.5
        pack = plan(frames, _cfg())
        self.assertNotIn("strip", [image.kind for image in pack.images])
        self.assertEqual({"grid", "single"},
                         {image.kind for image in pack.images})

    def test_current_frame_scene_switch_falls_back_to_grid(self):
        frames = [_frame(i) for i in range(5)]
        frames[-1].upper_diff = 2.5
        pack = plan(frames, _cfg())
        self.assertNotIn("strip", [image.kind for image in pack.images])

    def test_width_mismatch_and_missing_bottom_fall_back(self):
        frames = [_frame(i) for i in range(5)]
        frames[1] = _frame(1, width=1440)
        self.assertNotIn("strip", [image.kind for image in plan(frames, _cfg()).images])

        frames = [_frame(i) for i in range(5)]
        frames[1] = _frame(1, with_bottom=False)
        self.assertNotIn("strip", [image.kind for image in plan(frames, _cfg()).images])

    def test_strip_auto_detects_in_active_mode(self):
        frames = [_frame(i) for i in range(5)]
        pack = plan(frames, _cfg(mode="active"))
        self.assertEqual("strip", pack.images[1].kind)

    def test_disabled_flag_falls_back_to_grid(self):
        frames = [_frame(i) for i in range(5)]
        pack = plan(frames, _cfg(subtitle_strip_enabled=False))
        self.assertNotIn("strip", [image.kind for image in pack.images])

    def test_render_produces_strip_and_current_image(self):
        frames = [_frame(i) for i in range(5)]
        cfg = _cfg()
        pack = plan(frames, cfg)
        out = render(pack, frames, _full())
        self.assertEqual(2, len(out))
        self.assertIn("当前画面", out[0][0])
        self.assertIn("历史台词条带", out[1][0])
        self.assertTrue(out[1][1])
        with Image.open(io.BytesIO(base64.b64decode(out[0][1]))) as img:
            self.assertEqual((1280, 720), img.size)
        with Image.open(io.BytesIO(base64.b64decode(out[1][1]))) as img:
            self.assertEqual((1280, 720), img.size)


class StripCaptureTests(unittest.TestCase):
    def test_default_is_on(self):
        self.assertTrue(config.defaults()["capture"]["subtitle_strip_enabled"])

    def _queue(self, **capture) -> FrameQueue:
        cfg = _cfg(resolution="640x360", **capture)
        q = FrameQueue(cfg)
        q.cap.monitor_rect = (0, 0, 640, 480)
        return q

    def _image(self, bottom_color) -> Image.Image:
        img = Image.new("RGB", (640, 480), (30, 30, 30))
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 360, 640, 480), fill=bottom_color)
        return img

    def test_video_mode_stores_bottom_jpeg_and_keeps_subtitle_change(self):
        q = self._queue()
        with mock.patch.object(q.cap, "_grab_array",
                               return_value=self._image((0, 0, 0))):
            q._tick()
        with mock.patch.object(q.cap, "_grab_array",
                               return_value=self._image((255, 255, 255))):
            q._tick()
        self.assertEqual(2, q.peek())
        first, second = q._q
        self.assertEqual((480, 90), second.bottom_size)
        self.assertIsNotNone(first.bottom_jpeg)
        self.assertLess(second.upper_diff, 1.8)
        self.assertGreaterEqual(second.bottom_diff, 6.0)
        self.assertTrue(q.stats()["subtitle_strip"])

    def test_disabled_does_not_keep_bottom_jpeg(self):
        q = self._queue(subtitle_strip_enabled=False, dedupe=False)
        with mock.patch.object(q.cap, "_grab_array",
                               return_value=self._image((0, 0, 0))):
            q._tick()
        self.assertIsNone(q._q[0].bottom_jpeg)
        self.assertFalse(q.stats()["subtitle_strip"])

    def test_sixty_bottom_strips_stay_under_memory_budget(self):
        part = Image.new("RGB", (1600, 225), (20, 20, 20))
        data = encode_jpeg(part)
        frames = [Frame(float(i), _thumb(), b"", bottom_jpeg=data,
                        bottom_size=(1600, 225)) for i in range(60)]
        total = sum(len(f.bottom_jpeg) for f in frames)
        self.assertLess(total, 12 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
