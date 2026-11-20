from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sidekick.desktop import state


class DesktopStateTests(unittest.TestCase):
    def test_roundtrip_keeps_known_fields(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "desktop.json"
            data = dict(state.DEFAULTS, opacity=0.6, topmost=False,
                        click_through=True, follow_mouse=False, scale=1.5,
                        x=10, y=20)
            state.save(path, data)
            got = state.load(path)
        self.assertEqual(got["opacity"], 0.6)
        self.assertFalse(got["topmost"])
        self.assertTrue(got["click_through"])
        self.assertFalse(got["follow_mouse"])
        self.assertEqual(got["scale"], 1.5)
        self.assertEqual((got["x"], got["y"]), (10, 20))


if __name__ == "__main__":
    unittest.main()
