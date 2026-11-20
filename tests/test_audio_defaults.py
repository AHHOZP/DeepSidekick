from __future__ import annotations

import queue
import threading
import unittest

from sidekick import config
from sidekick.output.player import StreamPlayer
from sidekick.output.speaker import Speaker


class FakeProvider:
    name = "fake"
    profile = "fake"
    preset = "edge"
    has_key = True


class AudioDefaultsTests(unittest.TestCase):
    def test_speaker_guard_defaults_are_not_a_second_copy(self):
        cfg = config.defaults()
        speaker = Speaker(cfg, provider=FakeProvider())
        self.assertEqual(speaker.max_chars, cfg["guard"]["max_chars"])
        self.assertEqual(speaker.min_chars, cfg["guard"]["min_chars"])

    def test_speaker_cps_survives_unrelated_reload(self):
        cfg = config.defaults()
        speaker = Speaker(cfg, provider=FakeProvider())
        speaker.cps = 8.0
        speaker.reload(config.defaults())
        self.assertEqual(speaker.cps, 8.0)
        changed = config.defaults()
        changed["stage"]["cps"] = 5.0
        speaker.reload(changed)
        self.assertEqual(speaker.cps, 5.0)

    def test_stream_player_exits_on_stop_without_sentinel(self):
        player = StreamPlayer(mute=True)
        q: queue.Queue = queue.Queue()
        stop = threading.Event()
        th = threading.Thread(target=player.run,
                              args=(q, object(), stop), daemon=True)
        th.start()
        stop.set()
        th.join(1.0)
        self.assertFalse(th.is_alive())

if __name__ == "__main__":
    unittest.main()
