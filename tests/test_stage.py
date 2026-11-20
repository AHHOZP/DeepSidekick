from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from sidekick import config
from sidekick.avatar.stage import (
    StageClient, config_payload, serve_stage, stage_url,
)


class StageUrlTests(unittest.TestCase):
    def test_ipv6_host_gets_brackets(self):
        cfg = config.defaults()
        cfg["stage"]["host"] = "::1"
        self.assertEqual(stage_url(cfg), "http://[::1]:17891/")


class StageQueueTests(QuietIsolatedAsyncioTestCase):
    async def test_newest_control_survives_a_full_queue(self):
        client = StageClient()
        q = asyncio.Queue(maxsize=2)
        client.attach(q)
        for i in range(3):
            client.emit("say", pages=[i])
        kept = [q.get_nowait(), q.get_nowait()]
        self.assertEqual([m["pages"] for m in kept], [[1], [2]])


class StageReportTests(unittest.TestCase):
    def test_report_only_keeps_known_fields_and_truncates_text(self):
        client = StageClient()
        client.set_report({
            "t": "report", "renderer": "live2d", "evil": "x",
            "err": "e" * 300, "level": 0.4, "poseSeq": 9,
            "poseParams": 3, "poseWrites": 4, "autonomy": 1,
            "huge": 10 ** 400,
        })
        self.assertEqual(client.report["renderer"], "live2d")
        self.assertEqual(client.report["poseSeq"], 9)
        self.assertEqual(client.report["poseParams"], 3)
        self.assertEqual(client.report["poseWrites"], 4)
        self.assertEqual(client.report["autonomy"], 1)
        self.assertNotIn("t", client.report)
        self.assertNotIn("evil", client.report)
        self.assertNotIn("huge", client.report)
        self.assertEqual(len(client.report["err"]), 200)

    def test_report_drops_non_finite_numbers(self):
        client = StageClient()
        client.set_report({"renderer": "live2d", "level": float("nan"),
                           "mouth": float("inf"), "poseSeq": 2})
        self.assertEqual("live2d", client.report["renderer"])
        self.assertEqual(2, client.report["poseSeq"])
        self.assertNotIn("level", client.report)
        self.assertNotIn("mouth", client.report)


class StageHostTests(unittest.TestCase):
    def test_serve_stage_rejects_non_loopback_host(self):
        cfg = config.defaults()
        cfg["stage"]["host"] = "0.0.0.0"
        catalog = SimpleNamespace(root=Path("."))
        with self.assertRaises(RuntimeError):
            asyncio.run(serve_stage(StageClient(), cfg, catalog=catalog))


class StageConfigTests(unittest.TestCase):
    def test_config_reports_autonomy_availability(self):
        avatar = SimpleNamespace(
            kind="live2d", id="aid", stage={},
            to_dict=lambda: {"id": "aid", "kind": "live2d"})
        catalog = SimpleNamespace(root=Path("."), selected=lambda: avatar)
        cfg = config.defaults()
        cfg["stage"]["autonomy"]["active"] = False
        data = config_payload(catalog, cfg)
        self.assertTrue(data["autonomy"]["enabled"])
        self.assertFalse(data["autonomy"]["active"])
        cfg["stage"]["autonomy"]["enabled"] = True
        cfg["stage"]["autonomy"]["active"] = True
        data = config_payload(catalog, cfg)
        self.assertTrue(data["autonomy"]["enabled"])
        self.assertTrue(data["autonomy"]["active"])


if __name__ == "__main__":
    unittest.main()
