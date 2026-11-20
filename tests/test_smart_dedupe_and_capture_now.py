from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from sidekick import config
from sidekick.desktop import launcher
from sidekick.input.capture import FrameQueue


def _base() -> Image.Image:
    img = Image.new("RGB", (640, 480), (20, 20, 20))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 640, 80), fill=(100, 20, 20))
    draw.text((20, 20), "HUD 100/100", fill=(255, 255, 255))
    draw.text((40, 420), "字幕第一句", fill=(255, 255, 255))
    return img


def _queue(*, subtitle_weight: float = 1.0,
           pet_rect=None) -> FrameQueue:
    cfg = config.defaults()
    cfg["capture"].update({
        "enabled": True, "interval_s": 1.0, "max_frames": 6,
        "resolution": "1280x720", "dedupe": True,
        "dedupe_global_threshold": 2.0, "dedupe_block_threshold": 10.0,
        "subtitle_roi_weight": subtitle_weight,
    })
    q = FrameQueue(cfg, pet_rect_provider=(lambda: pet_rect))
    q.cap.monitor_rect = (0, 0, 640, 480)
    return q


class SmartDedupeTests(unittest.TestCase):
    def test_pet_area_change_is_masked_and_merged(self):
        q = _queue(pet_rect=(400, 320, 240, 160))
        base = _base()
        pet = base.copy()
        ImageDraw.Draw(pet).rectangle((400, 320, 640, 480),
                                      fill=(255, 0, 255))
        with mock.patch.object(q.cap, "_grab_array", return_value=base):
            q._tick()
        with mock.patch.object(q.cap, "_grab_array", return_value=pet):
            q._tick()
        self.assertEqual(1, q.peek())
        self.assertEqual(1, q.merged)

    def test_pet_mask_survives_high_subtitle_weight(self):
        q = _queue(subtitle_weight=4.0, pet_rect=(400, 320, 240, 160))
        base = _base()
        pet = base.copy()
        ImageDraw.Draw(pet).rectangle((400, 320, 640, 480),
                                      fill=(255, 0, 255))
        with mock.patch.object(q.cap, "_grab_array", return_value=base):
            q._tick()
        with mock.patch.object(q.cap, "_grab_array", return_value=pet):
            q._tick()
        self.assertEqual(1, q.peek())
        self.assertEqual(1, q.merged)

    def test_subtitle_change_enqueues_even_when_global_is_small(self):
        q = _queue()
        base = _base()
        sub = base.copy()
        draw = ImageDraw.Draw(sub)
        draw.rectangle((40, 400, 600, 450), fill=(0, 0, 0))
        draw.text((60, 410), "字幕第二句：换了一句", fill=(255, 255, 255))
        with mock.patch.object(q.cap, "_grab_array", return_value=base):
            q._tick()
        with mock.patch.object(q.cap, "_grab_array", return_value=sub):
            q._tick()
        self.assertEqual(2, q.peek())
        self.assertEqual(0, q.merged)
        self.assertGreater(q._q[-1].delta_score, 0.0)

    def test_ycbcr_signature_is_three_channels(self):
        from sidekick.input.capture import _signature
        self.assertEqual(32 * 24 * 3,
                         len(_signature(Image.new("RGB", (320, 180)))))

    def test_local_equal_luma_color_change_is_detected(self):
        q = _queue()
        base = Image.new("RGB", (640, 480), (20, 20, 20))
        changed = base.copy()
        ImageDraw.Draw(base).rectangle((100, 180, 400, 300),
                                       fill=(255, 0, 0))
        ImageDraw.Draw(changed).rectangle((100, 180, 400, 300),
                                          fill=(0, 130, 0))
        with mock.patch.object(q.cap, "_grab_array", return_value=base):
            q._tick()
        with mock.patch.object(q.cap, "_grab_array", return_value=changed):
            q._tick()
        self.assertEqual(2, q.peek())
        self.assertEqual(0, q.merged)

    def test_dark_static_noise_still_merges(self):
        q = _queue()
        base = Image.new("RGB", (640, 480), (4, 4, 4))
        noisy = Image.new("RGB", (640, 480), (5, 5, 5))
        with mock.patch.object(q.cap, "_grab_array", return_value=base):
            q._tick()
        with mock.patch.object(q.cap, "_grab_array", return_value=noisy):
            q._tick()
        self.assertEqual(1, q.peek())
        self.assertEqual(1, q.merged)


class PetRectFileTests(unittest.TestCase):
    def test_fresh_rect_is_read_and_stale_rect_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rect.json"
            path.write_text(json.dumps({
                "ts": time.time(), "rect": [10, 20, 30, 40]}),
                encoding="utf-8")
            with mock.patch.object(launcher, "_rect_path", return_value=path):
                self.assertEqual((10, 20, 30, 40),
                                 launcher.read_pet_rect(config.defaults()))
            path.write_text(json.dumps({
                "ts": time.time() - 10, "rect": [10, 20, 30, 40]}),
                encoding="utf-8")
            with mock.patch.object(launcher, "_rect_path", return_value=path):
                self.assertIsNone(launcher.read_pet_rect(config.defaults()))


class CaptureNowTests(unittest.TestCase):
    def test_quick_capture_keeps_previous_full_frame(self):
        q = _queue()
        first = _base()
        second = _base()
        ImageDraw.Draw(second).rectangle((0, 0, 640, 80), fill=(20, 100, 20))
        with mock.patch.object(q.cap, "_grab_array", return_value=first):
            q._tick()
        with mock.patch.object(q.cap, "_grab_array", return_value=second):
            q._now_event.set()
            q._tick()
        self.assertEqual(2, q.peek())
        self.assertTrue(q._q[1].quick)
        self.assertIsNotNone(q._q[0].extra_jpeg)
        self.assertIsNone(q._q[1].extra_jpeg)

    def test_quick_capture_is_not_merged_away(self):
        q = _queue()
        first = _base()
        same = _base()
        with mock.patch.object(q.cap, "_grab_array", return_value=first):
            q._tick()
        with mock.patch.object(q.cap, "_grab_array", return_value=same):
            q._now_event.set()
            q._tick()
        self.assertEqual(2, q.peek())
        self.assertTrue(q._q[-1].quick)
        self.assertIsNotNone(q._q[-2].extra_jpeg)

    def test_capture_now_wakes_capture_thread(self):
        q = _queue()
        q._tick = lambda: None
        q.start()
        try:
            self.assertTrue(q.capture_now(1.0))
        finally:
            q.stop()

    def test_capture_now_degrades_when_grab_blocks(self):
        q = _queue()
        q._tick = lambda: time.sleep(0.5)
        q.start()
        try:
            started = time.monotonic()
            self.assertFalse(q.capture_now(0.12))
            self.assertLess(time.monotonic() - started, 0.3)
        finally:
            q.stop()


if __name__ == "__main__":
    unittest.main()
