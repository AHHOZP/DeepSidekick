from __future__ import annotations

import unittest
from unittest import mock

from PIL import Image

from sidekick import config
from sidekick.input.capture import Capturer


class CaptureErrorTests(unittest.TestCase):
    def test_successful_pil_fallback_clears_backend_error(self):
        cap = Capturer(config.defaults())
        cap.last_error = "dxcam 不可用：ModuleNotFoundError"
        image = Image.new("RGB", (32, 18), "black")
        with mock.patch.object(cap, "_grab_array", return_value=image):
            self.assertIsNotNone(cap.grab_pil())
        self.assertIsNone(cap.last_error)

    def test_contain_preserves_ratio_and_never_upscales(self):
        cfg = config.defaults()
        cfg["capture"]["resolution"] = "1280x720"
        cap = Capturer(cfg)
        cases = [
            ((1920, 1080), (1280, 720)),   # 16:9
            ((1600, 1200), (960, 720)),    # 4:3
            ((1920, 1200), (1152, 720)),   # 16:10
            ((3440, 1440), (1280, 536)),   # 21:9，不裁切
            ((800, 600), (800, 600)),      # 不放大
        ]
        for src, want in cases:
            image = Image.new("RGB", src, "green")
            image.paste("red", (0, 0, src[0], max(1, src[1] // 10)))
            with mock.patch.object(cap, "_grab_array", return_value=image):
                out = cap.grab_pil()
            self.assertEqual(out.size, want, src)
            self.assertEqual(cap.last_size, want)
            # 顶边仍是红色：没有居中裁切。
            self.assertGreater(out.getpixel((want[0] // 2, 0))[0], 200)

    def test_screen_rect_maps_to_output_image(self):
        cap = Capturer(config.defaults())
        cap.monitor_rect = (100, 200, 1920, 1080)
        cap.source_size = (1920, 1080)
        cap.last_size = (1280, 720)
        self.assertEqual((0.0, 0.0, 1280.0, 720.0),
                         cap.to_image_rect((100, 200, 1920, 1080)))
        self.assertEqual((640.0, 360.0, 640.0, 360.0),
                         cap.to_image_rect((1060, 740, 960, 540)))


if __name__ == "__main__":
    unittest.main()
