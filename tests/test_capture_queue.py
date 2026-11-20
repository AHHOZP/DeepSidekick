from __future__ import annotations

import unittest

from PIL import Image

from sidekick import config
from sidekick.input.capture import FrameQueue, _signature


class FakeCapturer:
    def __init__(self, colors):
        self.colors = list(colors)
        self.i = 0

    def grab_pil(self):
        color = self.colors[min(self.i, len(self.colors) - 1)]
        self.i += 1
        return Image.new("RGB", (32, 18), color)

    def encode(self, img):
        return bytes([img.getpixel((0, 0))[0]])


class FrameQueueTests(unittest.TestCase):
    def test_dedupe_thinning_and_take_releases(self):
        cfg = config.defaults()
        cfg["capture"].update({"enabled": True, "interval_s": 0.1,
                               "max_frames": 3, "dedupe": True,
                               "dedupe_global_threshold": 2.0,
                               "dedupe_block_threshold": 10.0,
                               "ring_size": 8})
        q = FrameQueue(cfg)
        self.assertEqual(q.interval, 1.0)
        q.cap = FakeCapturer([(0, 0, 0), (0, 0, 0), (40, 0, 0),
                              (80, 0, 0), (120, 0, 0), (160, 0, 0)])
        for _ in range(2):
            q._tick()
        self.assertEqual(q.peek(), 1)
        self.assertEqual(q.merged, 1)
        self.assertEqual(len(q.take()), 1)
        self.assertEqual(q.peek(), 0)

        q.cap = FakeCapturer([(0, 0, 0), (40, 0, 0), (80, 0, 0),
                              (120, 0, 0), (160, 0, 0)])
        for _ in range(5):
            q._tick()
        self.assertEqual(q.peek(), 5)
        last_sig = _signature(Image.new("RGB", (32, 18), (160, 0, 0)))
        frames = q.take()
        self.assertEqual(len(frames), 5)
        self.assertEqual(frames[-1].sig_blocks, last_sig)
        self.assertEqual(q.peek(), 0)


if __name__ == "__main__":
    unittest.main()
