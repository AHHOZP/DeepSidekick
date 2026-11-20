from __future__ import annotations

import io
import random
import unittest
from unittest import mock

from PIL import Image, ImageDraw, JpegImagePlugin

from sidekick import config
from sidekick.input.capture import Capturer, Frame, FrameQueue


def _detailed_image() -> Image.Image:
    img = Image.new("RGB", (1280, 720), (18, 24, 36))
    draw = ImageDraw.Draw(img)
    rnd = random.Random(3)
    for y in range(0, 720, 4):
        draw.rectangle((0, y, 1280, y + 4),
                       fill=(y % 200, (y * 3) % 255, (y * 7) % 255))
    for _ in range(120):
        x, y = rnd.randrange(1280), rnd.randrange(720)
        draw.rectangle((x, y, x + rnd.randrange(30, 180),
                        y + rnd.randrange(10, 90)),
                       outline=(rnd.randrange(255), rnd.randrange(255),
                                rnd.randrange(255)), width=2)
    return img


class CaptureEncodingTests(unittest.TestCase):
    def test_encode_is_444_and_smaller_than_q100_420(self):
        cfg = config.defaults()
        cap = Capturer(cfg)
        img = _detailed_image()
        q100 = cap.encode(img, quality=100, subsampling=2)
        q85 = cap.encode(img, quality=85, subsampling=0)
        with Image.open(io.BytesIO(q85)) as out:
            self.assertEqual(0, JpegImagePlugin.get_sampling(out))
        self.assertLess(len(q85), len(q100) * 0.75,
                        (len(q85), len(q100)))

    def test_queue_keeps_only_one_full_frame_and_60_thumbs(self):
        cfg = config.defaults()
        cfg["capture"]["dedupe"] = False
        q = FrameQueue(cfg)
        img = _detailed_image()
        with mock.patch.object(q.cap, "_grab_array", return_value=img):
            for _ in range(60):
                q._tick()
        self.assertEqual(60, q.peek())
        frames = q._q
        self.assertEqual(60, len(frames))
        self.assertIsNotNone(q._latest_full_img)
        memory = sum(len(f.thumb_jpeg) + len(f.sig_blocks) for f in frames)
        memory += q._latest_full_img.width * q._latest_full_img.height * 3
        self.assertLess(memory, 15 * 1024 * 1024, memory)
        self.assertIsInstance(q.take()[0], Frame)


if __name__ == "__main__":
    unittest.main()
