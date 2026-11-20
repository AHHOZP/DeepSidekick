from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path

import yaml
from aiohttp.test_utils import TestClient, TestServer

from sidekick import config
from sidekick.config import llm_presets, secrets, tts_presets
from sidekick.webui.http import strip_secrets, valid_gid
from sidekick.webui.status_feed import StatusFeed
from webui_fixture import webui


class WebUiConfigSecretTests(QuietIsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg_path = self.root / "config.yaml"
        raw = {
            "memory": {"enabled": False},
            "webui": {"port": 17880},
            "llm": {"source": "foo", "foo": {
                "base_url": "http://example.invalid",
                "model": "m", "api_key": secrets.SEALED,
                "temperature": 0.9,
            }},
            "tts": {"enabled": False, "source": "mimo",
                    "common": {"gain": 0.55, "stream": True,
                               "segments": "sentence", "prefetch": True,
                               "timeout_s": 20},
                    "mimo": {**tts_presets.default_block("mimo"),
                             "api_key": secrets.SEALED}},
            "game": {"enabled": False},
        }
        self.stack = contextlib.ExitStack()
        self.ui = self.stack.enter_context(webui(self.root, raw))
        secrets._warned = False
        secrets.seal({"llm.foo.api_key": "KEEP",
                      "tts.mimo.api_key": "MIMO"})
        self.ui.csrf = "test-token"

        class Sup:
            started = False

            async def status(self):
                return {}

            async def start(self):
                self.started = True
                return {"ok": True}

        self.ui.supervisor = Sup()
        self.ui.feed = StatusFeed(self.ui.supervisor, self.ui.memory_stats,
                                  started_at=self.ui.started_at)
        self.client = TestClient(TestServer(self.ui.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.stack.close()
        self.tmp.cleanup()

    async def test_live_flag_roundtrip(self):
        for live in (True, False):
            r = await self.client.patch(
                "/api/config",
                json={"patch": {"session": {"live": live}}},
                headers={"X-CSRF-Token": "test-token"})
            self.assertEqual(r.status, 200, await r.text())
            self.assertIs(config.load_raw()["session"]["live"], live)

    async def test_concurrent_patches_keep_both_keys(self):
        async def patch(payload):
            return await self.client.patch(
                "/api/config", json={"patch": payload},
                headers={"X-CSRF-Token": "test-token"})

        a, b = await asyncio.gather(
            patch({"webui": {"port": 17881}}),
            patch({"webui": {"auto_open": True}}))
        self.assertEqual(a.status, 200, await a.text())
        self.assertEqual(b.status, 200, await b.text())
        raw = config.load_raw()
        self.assertEqual(raw["webui"]["port"], 17881)
        self.assertTrue(raw["webui"]["auto_open"])

    async def test_patch_does_not_touch_secret_file(self):
        before = secrets.path().read_bytes()
        r = await self.client.patch(
            "/api/config",
            json={"patch": {"webui": {"port": 17881}}},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual(before, secrets.path().read_bytes())
        self.assertEqual(secrets.get("llm.foo.api_key"), "KEEP")
        raw = config.load_raw()
        self.assertEqual(raw["llm"]["foo"]["api_key"], secrets.SEALED)
        self.assertEqual(raw["webui"]["port"], 17881)

    async def test_patch_replaces_headers_and_keeps_secret_refs(self):
        raw = config.load_raw()
        raw["llm"]["foo"]["headers"] = {
            "X-Trace": "t", "Authorization": secrets.SEALED}
        config.save_raw(raw)

        full = config.redacted(config.load())
        full["llm"]["foo"]["headers"].pop("X-Trace")
        r = await self.client.patch(
            "/api/config", json={"patch": full},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 200, await r.text())
        saved = config.load_raw()
        self.assertNotIn("X-Trace", saved["llm"]["foo"]["headers"])
        self.assertEqual(saved["llm"]["foo"]["api_key"], secrets.SEALED)
        self.assertEqual(secrets.get("llm.foo.api_key"), "KEEP")

    async def test_patch_replaces_headers_so_removed_entries_disappear(self):
        raw = config.load_raw()
        raw["llm"]["foo"]["headers"] = {
            "X-Trace": "old", "User-Agent": "UA",
            "Authorization": secrets.SEALED,
        }
        config.save_raw(raw)
        secrets.upsert("llm.foo.headers.Authorization", "AUTH")

        r = await self.client.patch(
            "/api/config",
            json={"patch": {"llm": {"foo": {
                "headers": {"User-Agent": "UA2"}}}}},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual(
            config.load_raw()["llm"]["foo"]["headers"],
            {"Authorization": secrets.SEALED, "User-Agent": "UA2"})

        r = await self.client.patch(
            "/api/config",
            json={"patch": {"llm": {"foo": {"headers": {}}}}},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual(
            config.load_raw()["llm"]["foo"]["headers"],
            {"Authorization": secrets.SEALED})
        self.assertEqual(secrets.get("llm.foo.headers.Authorization"), "AUTH")

    async def test_deleting_profile_clears_only_its_secret(self):
        raw = config.load_raw()
        raw["llm"]["bar"] = {"base_url": "http://example.invalid",
                             "model": "m", "api_key": secrets.SEALED,
                             "temperature": 0.9}
        raw["llm"]["source"] = "bar"
        config.save_raw(raw)
        secrets.upsert("llm.bar.api_key", "BAR")
        r = await self.client.patch(
            "/api/config",
            json={"path": "llm.foo", "delete": True},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 200, await r.text())
        self.assertNotIn("llm.foo.api_key", secrets.load())
        self.assertEqual(secrets.get("llm.bar.api_key"), "BAR")

    def test_game_ids_reject_windows_trailing_dots(self):
        for gid in ("real.", "real..", "...", ".hidden", "real "):
            self.assertFalse(valid_gid(gid), gid)
        self.assertTrue(valid_gid("real"))
        self.assertTrue(valid_gid("real.name"))

    def test_strip_secrets_uses_one_path_rule(self):
        patch = {"llm": {"foo": {"api_key": "K",
                                 "headers": {"Authorization": "B",
                                             "X-Trace": "t"}}},
                 "sessdata": "C", "webui": {"port": 1}}
        self.assertEqual(strip_secrets(patch), {
            "llm": {"foo": {"headers": {"X-Trace": "t"}}},
            "webui": {"port": 1},
        })
        self.assertEqual(
            strip_secrets({"llm": {"foo": {"temperature": 0.9,
                                           "api_key": "K"}}}),
            {"llm": {"foo": {"temperature": 0.9}}})

    async def test_blank_field_falls_back_to_default(self):
        raw = config.load_raw()
        raw.setdefault("memory", {})["history_max"] = 20
        config.save_raw(raw)
        r = await self.client.patch(
            "/api/config",
            json={"patch": {"memory": {"history_max": ""}}},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 200, await r.text())
        self.assertNotIn("history_max", config.load_raw()["memory"])
        self.assertEqual(config.load()["memory"]["history_max"], 40)

    async def test_bad_yaml_blocks_patch_and_keeps_file(self):
        bad = ": not yaml: ["
        self.cfg_path.write_text(bad, encoding="utf-8")
        before = self.cfg_path.read_text(encoding="utf-8")
        r = await self.client.patch(
            "/api/config",
            json={"patch": {"webui": {"port": 17881}}},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 409, await r.text())
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), before)

        self.cfg_path.write_text(yaml.safe_dump(
            {"webui": {"port": 17881}}, allow_unicode=True), encoding="utf-8")
        r = await self.client.patch(
            "/api/config",
            json={"patch": {"webui": {"auto_open": False}}},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 200, await r.text())
        self.assertIsNone(config.last_error)

    async def test_bootstrap_reports_bad_config(self):
        self.cfg_path.write_text(": not yaml: [", encoding="utf-8")
        r = await self.client.get("/api/bootstrap")
        self.assertEqual(r.status, 200, await r.text())
        data = await r.json()
        self.assertIn("config.yaml", data["config_error"] or "")

    async def test_save_rejects_bad_enum_without_touching_file(self):
        before = self.cfg_path.read_bytes()
        r = await self.client.patch(
            "/api/config",
            json={"patch": {"mode": {"manual": "loud"}}},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 400, await r.text())
        self.assertIn("mode.manual", (await r.json())["errors"][0])
        self.assertEqual(before, self.cfg_path.read_bytes())

    async def test_start_allows_custom_profile_without_key(self):
        secrets.clear_secret("llm.foo.api_key")
        r = await self.client.post(
            "/api/runtime/start", json={"mode": "stream"},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 200, await r.text())
        data = await r.json()
        self.assertTrue(data["ok"])
        self.assertTrue(self.ui.supervisor.started)

    async def test_start_blocks_default_preset_without_key(self):
        config.save_raw({"llm": {"source": llm_presets.DEFAULT_ID}})
        r = await self.client.post(
            "/api/runtime/start", json={},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 200, await r.text())
        data = await r.json()
        self.assertFalse(data["ok"])
        self.assertEqual("model", data["page"])
        self.assertIn(llm_presets.PRESETS[llm_presets.DEFAULT_ID]["label"],
                      data["error"])
        self.assertFalse(self.ui.supervisor.started)

    async def test_llm_test_allows_custom_profile_without_key(self):
        from unittest import mock

        async def fake_chat(*args, **kwargs):
            return {"choices": [{"message": {"content": "OK"}}]}

        with mock.patch("sidekick.brain.llm.LLMClient.chat", fake_chat), \
                mock.patch("sidekick.brain.llm.LLMClient.list_models",
                           mock.AsyncMock(return_value=[])):
            r = await self.client.post(
                "/api/llm/test", json={"profile": "foo"},
                headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 200, await r.text())
        data = await r.json()
        self.assertTrue(data["ok"])

    async def test_bootstrap_ships_profile_template_and_presets(self):
        r = await self.client.get("/api/bootstrap")
        self.assertEqual(r.status, 200, await r.text())
        schema = (await r.json())["schema"]
        keys = [f["key"] for f in schema["llm_profile_fields"]]
        self.assertEqual(keys, ["base_url", "model", "api_key", "temperature",
                                "reasoning_effort", "headers"])
        values = {f["key"]: f["value"] for f in schema["llm_profile_fields"]}
        self.assertEqual(values["temperature"], 0.9)
        self.assertEqual(values["reasoning_effort"], "low")
        self.assertEqual(values["api_key"], "")
        self.assertEqual(values["headers"], {})
        self.assertIn(llm_presets.DEFAULT_ID, schema["llm_presets"])
        profiles = {p["name"]: p for p in schema["llm_profiles"]}
        # 只列 config.yaml 里真正保存的 profile；默认值不凭空多出一份。
        self.assertEqual(set(profiles), {"foo"})
        self.assertIn("foo", profiles)
        self.assertEqual(
            [f["path"] for f in profiles["foo"]["fields"]],
            [f"llm.foo.{k}" for k in keys])

    async def test_bootstrap_lists_active_source_even_if_only_defaults_have_it(self):
        config.save_raw({"llm": {"source": llm_presets.DEFAULT_ID}})
        r = await self.client.get("/api/bootstrap")
        self.assertEqual(r.status, 200, await r.text())
        schema = (await r.json())["schema"]
        names = [p["name"] for p in schema["llm_profiles"]]
        self.assertEqual(names, [llm_presets.DEFAULT_ID])

    async def test_bootstrap_lists_default_profile_when_llm_section_is_missing(self):
        config.save_raw({})
        r = await self.client.get("/api/bootstrap")
        self.assertEqual(r.status, 200, await r.text())
        schema = (await r.json())["schema"]
        names = [p["name"] for p in schema["llm_profiles"]]
        self.assertEqual(names, [llm_presets.DEFAULT_ID])

    async def test_set_secret_rejects_missing_profile(self):
        r = await self.client.post(
            "/api/secrets",
            json={"name": "llm.ghost.api_key", "value": "X", "action": "set"},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(r.status, 404)
        self.assertNotIn("llm.ghost.api_key", secrets.load())


if __name__ == "__main__":
    unittest.main()
