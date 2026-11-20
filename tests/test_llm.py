from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidekick import config
from identity_fixture import runtime_cfg
from sidekick.brain.brain import Brain
from sidekick.brain.llm import LLMClient, LLMError
from sidekick.brain.worldbook import Worldbook
from sidekick.core.null import InMemoryHistory, NullAvatarActions, NullLongTerm


class FakeResponse:
    def __init__(self, status, raw):
        self.status = status
        self.raw = raw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self.raw


class FakeSession:
    closed = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.headers: list[dict] = []
        self.bodies: list[dict] = []

    def post(self, *args, **kwargs):
        self.calls += 1
        self.headers.append(kwargs.get("headers") or {})
        self.bodies.append(kwargs.get("json") or {})
        return self.responses.pop(0)

    def get(self, *args, **kwargs):
        self.calls += 1
        self.headers.append(kwargs.get("headers") or {})
        return self.responses.pop(0)


def _cfg(**values) -> dict:
    profile = {
        "base_url": "https://api.example.com/v1",
        "model": "m",
        "api_key": "",
        "temperature": 0.9,
        "reasoning_effort": "low",
        "headers": {},
        "body": {},
    }
    profile.update(values)
    cfg = config.defaults()
    cfg["llm"]["source"] = "p"
    cfg["llm"]["p"] = profile
    return cfg


def _reply(text: str = "ok") -> FakeResponse:
    return FakeResponse(200, json.dumps({
        "choices": [{"message": {"content": text}}]}))


class LLMProfileTests(unittest.TestCase):
    def test_profile_without_key_is_ready_and_sends_no_authorization(self):
        client = LLMClient(_cfg())
        self.assertTrue(client.ready)
        self.assertEqual(client.model, "m")
        self.assertEqual(client.base_url, "https://api.example.com/v1")
        self.assertNotIn("Authorization", client.headers())

    def test_ready_requires_base_url_and_model(self):
        self.assertFalse(LLMClient(_cfg(base_url="")).ready)
        self.assertFalse(LLMClient(_cfg(model="  ")).ready)

    def test_profile_name_is_matched_case_sensitively(self):
        cfg = _cfg()
        cfg["llm"]["MyGateway"] = cfg["llm"].pop("p")
        cfg["llm"]["source"] = "MyGateway"
        client = LLMClient(cfg)
        self.assertEqual(client.source, "MyGateway")
        self.assertEqual(client.model, "m")

    def test_request_body_carries_no_max_tokens(self):
        client = LLMClient(_cfg(), key="k")
        body = client.request_body([{"role": "user", "content": "hi"}])
        self.assertNotIn("max_tokens", body)
        self.assertEqual(body["model"], "m")
        self.assertEqual(body["temperature"], 0.9)
        self.assertIs(body["stream"], False)


class LLMBodyTests(unittest.TestCase):
    def test_temperature_can_be_overridden_per_call(self):
        client = LLMClient(_cfg(temperature=1.2), key="k")
        body = client.request_body([{"role": "user", "content": "hi"}],
                                   temperature=0)
        self.assertEqual(body["temperature"], 0.0)

    def test_reasoning_effort_can_be_overridden_per_call(self):
        client = LLMClient(_cfg(reasoning_effort="low"), key="k")
        body = client.request_body([{"role": "user", "content": "hi"}],
                                   temperature=1.0,
                                   reasoning_effort="max")
        self.assertEqual(body["temperature"], 1.0)
        self.assertEqual(body["reasoning_effort"], "max")
        self.assertEqual(client.body_defaults()["reasoning_effort"], "low")

    def test_empty_reasoning_effort_is_not_sent(self):
        client = LLMClient(_cfg(reasoning_effort=""), key="k")
        self.assertNotIn("reasoning_effort", client.body_defaults())

    def test_reasoning_effort_is_trimmed_and_sent(self):
        client = LLMClient(_cfg(reasoning_effort=" high "), key="k")
        self.assertEqual(client.effort, "high")
        self.assertEqual(client.body_defaults()["reasoning_effort"], "high")

    def test_extra_body_keys_merge_but_program_fields_win(self):
        client = LLMClient(_cfg(body={
            "top_p": 0.8, "max_tokens": 64,
            "model": "other", "stream": True, "temperature": 0.1,
            "reasoning_effort": "high", "messages": [],
        }), key="k")
        defaults = client.body_defaults()
        self.assertEqual(defaults["top_p"], 0.8)
        self.assertEqual(defaults["max_tokens"], 64)
        self.assertEqual(defaults["model"], "m")
        self.assertIs(defaults["stream"], False)
        self.assertEqual(defaults["temperature"], 0.9)
        self.assertEqual(defaults["reasoning_effort"], "low")

    def test_body_messages_are_not_used(self):
        client = LLMClient(_cfg(body={"messages": [
            {"role": "system", "content": "x"}]}), key="k")
        messages = [{"role": "user", "content": "hi"}]
        self.assertEqual(client.request_body(messages)["messages"], messages)


class LLMHeaderTests(unittest.TestCase):
    def test_default_headers_and_bearer(self):
        client = LLMClient(_cfg(), key="k")
        h = client.headers()
        self.assertEqual(h["Content-Type"], "application/json")
        self.assertEqual(h["Accept"], "application/json")
        self.assertEqual(h["Authorization"], "Bearer k")

    def test_custom_headers_override_case_insensitively(self):
        client = LLMClient(_cfg(headers={
            "authorization": "Bearer X",
            "User-Agent": "UA",
            "x-opencode-session": "deepsidekick-main",
        }), key="k")
        h = client.headers()
        self.assertEqual(h["authorization"], "Bearer X")
        self.assertEqual([k for k in h if k.lower() == "authorization"],
                         ["authorization"])
        self.assertEqual(h["User-Agent"], "UA")
        self.assertEqual(h["x-opencode-session"], "deepsidekick-main")

    def test_blank_and_sealed_headers_are_skipped(self):
        client = LLMClient(_cfg(headers={"X-A": " ", "X-B": "sealed",
                                         "X-C": "v"}))
        h = client.headers()
        self.assertNotIn("X-A", h)
        self.assertNotIn("X-B", h)
        self.assertEqual(h["X-C"], "v")


class LLMUrlTests(unittest.TestCase):
    def test_base_without_path(self):
        client = LLMClient(_cfg(base_url="https://api.deepseek.com"))
        self.assertEqual(client.url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(client.models_url, "https://api.deepseek.com/models")

    def test_base_with_v1_and_trailing_slash(self):
        client = LLMClient(_cfg(base_url="https://api.openai.com/v1/"))
        self.assertEqual(client.url,
                         "https://api.openai.com/v1/chat/completions")
        self.assertEqual(client.models_url, "https://api.openai.com/v1/models")

    def test_full_endpoint_is_kept_with_query(self):
        client = LLMClient(_cfg(
            base_url="https://host/v1/chat/completions?api-version=2024-10-21"))
        self.assertEqual(
            client.url,
            "https://host/v1/chat/completions?api-version=2024-10-21")
        self.assertEqual(client.models_url, "https://host/v1/models")

    def test_query_on_base_is_kept_for_chat(self):
        client = LLMClient(_cfg(base_url="https://host/v1?x=1"))
        self.assertEqual(client.url, "https://host/v1/chat/completions?x=1")

    def test_invalid_base_has_no_models_url(self):
        self.assertIsNone(LLMClient(_cfg(base_url="api.example.com")).models_url)


class LLMModelsTests(QuietIsolatedAsyncioTestCase):
    async def test_list_models_parses_ids_without_sending_a_key(self):
        client = LLMClient(_cfg())
        session = FakeSession([FakeResponse(200, json.dumps(
            {"data": [{"id": "a"}, {"id": " b "}, {"x": 1}, "c"]}))])
        client._session = session
        self.assertEqual(await client.list_models(), ["a", "b", "c"])
        self.assertNotIn("Authorization", session.headers[0])

    async def test_list_models_rejects_a_non_http_base(self):
        client = LLMClient(_cfg(base_url="api.example.com"))
        with self.assertRaises(LLMError):
            await client.list_models()


class LLMRetryTests(QuietIsolatedAsyncioTestCase):
    async def test_5xx_retries_4xx_does_not(self):
        client = LLMClient(_cfg(), key="k")
        session = FakeSession([FakeResponse(500, "boom"), _reply()])
        client._session = session
        with mock.patch("sidekick.brain.llm.asyncio.sleep", new=mock.AsyncMock()):
            data = await client.chat([{"role": "user", "content": "hi"}],
                                     temperature=0, attempts=2)
        self.assertEqual(client.text_of(data), "ok")
        self.assertEqual(session.calls, 2)

        session = FakeSession([FakeResponse(400, "bad")])
        client._session = session
        with mock.patch("sidekick.brain.llm.asyncio.sleep", new=mock.AsyncMock()):
            with self.assertRaises(LLMError) as ctx:
                await client.chat([{"role": "user", "content": "hi"}],
                                  temperature=0, attempts=2)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(session.calls, 1)

    async def test_named_reasoning_effort_is_dropped_and_retried(self):
        client = LLMClient(_cfg(), key="k")
        session = FakeSession([
            FakeResponse(400, "unsupported value: reasoning_effort"), _reply()])
        client._session = session
        await client.chat([{"role": "user", "content": "hi"}], attempts=1)
        self.assertTrue(client.effort_dropped)
        self.assertEqual(client.effort, "")
        self.assertEqual(session.calls, 2)
        self.assertNotIn("reasoning_effort", session.bodies[1])

    def test_single_system_probe_expires_after_ten_minutes(self):
        client = LLMClient(_cfg(system_mode="auto"), key="k")
        client._single_system = True
        client._single_system_at = 100.0
        with mock.patch("sidekick.brain.llm.time.monotonic",
                        return_value=200.0):
            self.assertTrue(client.single_system)
        with mock.patch("sidekick.brain.llm.time.monotonic",
                        return_value=701.0):
            self.assertFalse(client.single_system)

    async def test_request_cap_stops_extra_probes(self):
        client = LLMClient(_cfg(reasoning_effort="high"), key="k")
        session = FakeSession([
            FakeResponse(400, "unsupported value: reasoning_effort"),
            FakeResponse(400, "only one system message allowed"),
            _reply(),
        ])
        client._session = session
        with self.assertRaises(LLMError):
            await client.chat([{"role": "user", "content": "hi"}],
                              attempts=1)
        self.assertEqual(2, session.calls)

    async def test_network_failure_clears_previous_http_status(self):
        class FlakySession(FakeSession):
            def post(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return FakeResponse(500, "boom")
                raise OSError("network down")

        client = LLMClient(_cfg(), key="k")
        client._session = FlakySession([])
        with mock.patch("sidekick.brain.llm.asyncio.sleep", new=mock.AsyncMock()):
            with self.assertRaises(LLMError) as ctx:
                await client.chat([{"role": "user", "content": "hi"}],
                                  attempts=2)
        self.assertIsNone(ctx.exception.status)
        self.assertIsNone(client.last_status)
        self.assertEqual(client.last_raw, "")

    async def test_request_cap_is_hard_across_network_failures(self):
        client = LLMClient(_cfg(reasoning_effort="high"), key="k")
        session = FakeSession([
            FakeResponse(400, "unsupported value: reasoning_effort"),
            FakeResponse(400, "only one system message allowed"),
            FakeResponse(500, "boom"),
            FakeResponse(500, "boom"),
            _reply(),
        ])
        client._session = session
        with self.assertRaises(LLMError):
            await client.chat([{"role": "user", "content": "hi"}],
                              attempts=2)
        self.assertEqual(3, session.calls)

    async def test_other_4xx_is_not_retried(self):
        client = LLMClient(_cfg(), key="k")
        session = FakeSession([FakeResponse(400, "bad parameter foo")])
        client._session = session
        with self.assertRaises(LLMError):
            await client.chat([{"role": "user", "content": "hi"}], attempts=2)
        self.assertEqual(session.calls, 1)
        self.assertFalse(client.effort_dropped)

    async def test_multiple_systems_are_merged_and_retried(self):
        client = LLMClient(_cfg(), key="k")
        session = FakeSession([
            FakeResponse(400, "only one system message allowed"), _reply()])
        client._session = session
        messages = [{"role": "system", "content": "a"},
                    {"role": "system", "content": "b"},
                    {"role": "user", "content": "hi"}]
        await client.chat(messages, attempts=1)
        self.assertEqual(session.calls, 2)
        sent = session.bodies[1]["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "user"])
        self.assertIn("a", sent[0]["content"])
        self.assertIn("b", sent[0]["content"])


class LLMUsageMetricsTests(QuietIsolatedAsyncioTestCase):
    async def test_usage_carries_request_metrics(self):
        client = LLMClient(_cfg(), key="k")
        session = FakeSession([FakeResponse(200, json.dumps({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 578, "completion_tokens": 1}}))])
        client._session = session
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
        ]}]
        await client.chat(messages, temperature=0, attempts=1)
        usage = client.last_usage
        self.assertEqual(578, usage["prompt_tokens"])
        self.assertEqual(1, usage["image_count"])
        self.assertGreater(usage["payload_kb"], 0)
        self.assertGreaterEqual(usage["latency_ms"], 0)


class BrainNoBudgetTests(QuietIsolatedAsyncioTestCase):
    async def test_empty_response_retries_once(self):
        class LLM:
            ready = True
            model = "m"
            source = "fake"
            effort = ""
            effort_dropped = False
            temp = 0.0
            warmup_enabled = False
            calls = 0

            def body_defaults(self):
                return {"model": self.model, "temperature": 0.0, "stream": False}

            def request_body(self, messages, temperature=None):
                return {"messages": messages, **self.body_defaults()}

            async def chat(self, messages, **kw):
                self.calls += 1
                if self.calls == 1:
                    return {"choices": [{"message": {"content": ""},
                                         "finish_reason": "length"}]}
                return {"choices": [{"message": {"content": "ok"},
                                     "finish_reason": "stop"}]}

            def cache_line(self):
                return ""

            async def close(self):
                return None

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = runtime_cfg(root)
            cfg["game"]["enabled"] = False
            llm = LLM()
            brain = Brain(
                cfg, history=InMemoryHistory(4), memory=NullLongTerm(),
                worldbook=Worldbook(cfg, root), actions=NullAvatarActions(),
                llm=llm, root=root, log=lambda *a, **k: None)
            self.assertEqual(await brain.reply("材料"), "ok")
            self.assertEqual(llm.calls, 2)


if __name__ == "__main__":
    unittest.main()
