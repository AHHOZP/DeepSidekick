from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import asyncio
import base64
import io
import json
import wave
from unittest import mock

import numpy as np
from aiohttp import web
from aiohttp.test_utils import TestServer

from sidekick.config import tts_presets
from sidekick.output.tts import make
from sidekick.output.tts.edge import EdgeProvider
from sidekick.output.tts.http import HttpProvider, TtsError


def _pcm16(*samples: int) -> bytes:
    return np.asarray(samples, dtype="<i2").tobytes()


def _wav_bytes(samples, sr=24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return buf.getvalue()


class HttpProviderTests(QuietIsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests: list[dict] = []
        app = web.Application()
        app.router.add_post("/audio/speech", self.raw)
        app.router.add_post("/chat/completions", self.mimo)
        app.router.add_post("/v1/t2a_v2", self.minimax)
        app.router.add_post("/v1/tts/synthesize", self.aivis)
        app.router.add_post("/v1/text-to-speech/{voice}/stream", self.eleven)
        app.router.add_post("/v1/tts", self.fish)
        app.router.add_post("/slow", self.slow)
        app.router.add_post("/unauthorized", self.unauthorized)
        app.router.add_post("/mp3", self.mp3)
        self.server = TestServer(app)
        await self.server.start_server()
        self.base = str(self.server.make_url("")).rstrip("/")

    async def asyncTearDown(self):
        await self.server.close()

    def _provider(self, preset_id: str, **values) -> HttpProvider:
        block = tts_presets.default_block(preset_id)
        block["base_url"] = self.base
        block["api_key"] = "KEY"
        block.update(values)
        return HttpProvider(profile="test", preset_id=preset_id,
                            block=block, timeout=2)

    def _record(self, request, body) -> None:
        self.requests.append({
            "path": request.path,
            "headers": dict(request.headers),
            "body": body,
        })

    async def raw(self, request):
        body = await request.json()
        self._record(request, body)
        resp = web.StreamResponse()
        await resp.prepare(request)
        data = _pcm16(1000, -1000)
        await resp.write(data[:1])
        await resp.write(data[1:])
        await resp.write_eof()
        return resp

    async def mimo(self, request):
        body = await request.json()
        self._record(request, body)
        payload = base64.b64encode(_pcm16(1200, -1200)).decode()
        resp = web.StreamResponse()
        await resp.prepare(request)
        await resp.write(
            f'data: {json.dumps({"choices": [{"delta": {"audio": {"data": payload}}}]})}\n\n'
            .encode())
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    async def minimax(self, request):
        body = await request.json()
        self._record(request, body)
        resp = web.StreamResponse()
        await resp.prepare(request)
        await resp.write(
            f'data: {json.dumps({"data": {"audio": "0100feff"}})}\n\n'.encode())
        await resp.write_eof()
        return resp

    async def aivis(self, request):
        body = await request.json()
        self._record(request, body)
        return web.Response(body=_wav_bytes([100, -100]),
                            content_type="audio/wav")

    async def eleven(self, request):
        body = await request.json()
        self._record(request, body)
        return web.Response(body=_pcm16(3000, -3000))

    async def fish(self, request):
        body = await request.json()
        self._record(request, body)
        return web.Response(body=_pcm16(7, 8))

    async def slow(self, request):
        await asyncio.sleep(0.2)
        return web.Response(body=_pcm16(1))

    async def unauthorized(self, request):
        return web.Response(status=401, text='{"error": "bad key"}')

    async def mp3(self, request):
        return web.Response(body=b"ID3fake")

    async def test_raw_stream_and_synth(self):
        provider = self._provider("openai", voice="alloy", model="tts-1")
        chunks = [c async for c in provider.stream("你好")]
        pcm = np.concatenate(chunks)
        self.assertEqual(pcm.tolist(), [1000 / 32768.0, -1000 / 32768.0])
        result = await provider.synth("你好")
        self.assertEqual(len(result.pcm), 2)
        self.assertEqual(result.sr, 24000)
        await provider.close()

    async def test_sse_base64_and_end_marker(self):
        provider = self._provider("mimo", voice="冰糖")
        chunks = [c async for c in provider.stream("你好")]
        self.assertEqual(np.concatenate(chunks).tolist(),
                         [1200 / 32768.0, -1200 / 32768.0])
        await provider.close()

    async def test_sse_hex(self):
        provider = self._provider("minimax", voice="v")
        chunks = [c async for c in provider.stream("你好")]
        self.assertEqual(np.concatenate(chunks).tolist(),
                         [1 / 32768.0, -2 / 32768.0])
        await provider.close()

    async def test_sse_missing_end_marker_is_an_error(self):
        app = web.Application()

        async def no_end(request):
            payload = base64.b64encode(_pcm16(1)).decode()
            return web.Response(
                body=(f'data: {json.dumps({"choices": [{"delta": {"audio": {"data": payload}}}]})}\n\n'
                      .encode()),
                content_type="text/event-stream")

        app.router.add_post("/chat/completions", no_end)
        server = TestServer(app)
        await server.start_server()
        try:
            block = tts_presets.default_block("mimo")
            block.update({"base_url": str(server.make_url("")).rstrip("/"),
                          "api_key": "KEY"})
            provider = HttpProvider(profile="test", preset_id="mimo",
                                    block=block, timeout=2)
            with self.assertRaises(TtsError):
                _ = [c async for c in provider.stream("你好")]
            await provider.close()
        finally:
            await server.close()

    async def test_wav_is_whole_decoded_and_not_streaming(self):
        provider = self._provider("aivis", voice="uuid")
        self.assertFalse(provider.supports_stream)
        result = await provider.synth("こんにちは")
        self.assertEqual(result.sr, 24000)
        self.assertEqual(len(result.pcm), 2)
        await provider.close()

    async def test_mp3_uses_decode_any(self):
        spec = {
            "label": "test", "desc": "", "doc": "", "wire": "http",
            "auth": {"kind": "bearer"}, "path": "/mp3",
            "body": {"text": "{text}"}, "response": {"kind": "raw"},
            "audio": "mp3", "fields": {
                "base_url": {"label": "url", "type": "text",
                             "default": self.base},
            },
        }
        with mock.patch.dict(tts_presets.PRESETS, {"_mp3": spec}), \
                mock.patch("sidekick.output.tts.http.decode_any",
                           return_value=(np.zeros(3, dtype=np.float32), 24000)) as dec:
            block = tts_presets.default_block("_mp3")
            block.update({"base_url": self.base, "api_key": "K"})
            provider = HttpProvider(profile="test", preset_id="_mp3",
                                    block=block, timeout=2)
            result = await provider.synth("你好")
            self.assertEqual(len(result.pcm), 3)
            dec.assert_called_once_with(b"ID3fake", "mp3")
            await provider.close()

    async def test_http_401_maps_to_chinese_without_headers(self):
        block = tts_presets.default_block("openai")
        block.update({"base_url": self.base, "api_key": "KEY"})
        block["base_url"] = self.base
        # 用同形状的 openai preset，把 path 指到 /unauthorized
        spec = dict(tts_presets.PRESETS["openai"])
        spec["path"] = "/unauthorized"
        with mock.patch.dict(tts_presets.PRESETS, {"_bad": spec}):
            provider = HttpProvider(profile="test", preset_id="_bad",
                                    block=block, timeout=2)
            with self.assertRaises(TtsError) as ctx:
                await provider.synth("你好")
            msg = str(ctx.exception)
            self.assertIn("密钥无效", msg)
            self.assertIn("bad key", msg)
            self.assertNotIn("KEY", msg)
            await provider.close()

    async def test_request_shape_and_optional_fields(self):
        provider = self._provider("fish", voice="", model="s2.1-pro-free")
        await provider.synth("你好{model}")
        body = self.requests[-1]["body"]
        headers = self.requests[-1]["headers"]
        self.assertEqual(body["text"], "你好{model}")
        self.assertNotIn("reference_id", body)
        self.assertEqual(body["sample_rate"], 24000)
        self.assertIsInstance(body["sample_rate"], int)
        self.assertEqual(headers["model"], "s2.1-pro-free")
        self.assertEqual(headers["Authorization"], "Bearer KEY")
        await provider.close()

    async def test_voice_path_and_query_and_static_header(self):
        provider = self._provider("elevenlabs", voice="v-123")
        await provider.synth("hello")
        req = self.requests[-1]
        self.assertEqual(req["path"], "/v1/text-to-speech/v-123/stream")
        self.assertEqual(req["headers"]["xi-api-key"], "KEY")
        self.assertEqual(req["body"]["model_id"], "eleven_multilingual_v2")
        await provider.close()

    async def test_url_join_does_not_duplicate_path(self):
        block = tts_presets.default_block("openai")
        block.update({"base_url": self.base + "/audio/speech",
                      "api_key": "K"})
        provider = HttpProvider(profile="test", preset_id="openai",
                                block=block, timeout=2)
        self.assertEqual(provider._url("x"), self.base + "/audio/speech")
        await provider.close()

        block["base_url"] = self.base + "/"
        provider = HttpProvider(profile="test", preset_id="openai",
                                block=block, timeout=2)
        self.assertEqual(provider._url("x"), self.base + "/audio/speech")
        await provider.close()

    async def test_pcm_sr_field_overrides_preset(self):
        provider = self._provider("openai", voice="alloy",
                                  pcm_sr="44100")
        self.assertEqual(provider.stream_sr, 44100)
        await provider.close()

    async def test_timeout_maps_to_connection_message(self):
        block = tts_presets.default_block("openai")
        block.update({"base_url": self.base, "api_key": "K"})
        spec = dict(tts_presets.PRESETS["openai"])
        spec["path"] = "/slow"
        with mock.patch.dict(tts_presets.PRESETS, {"_slow": spec}):
            provider = HttpProvider(profile="test", preset_id="_slow",
                                    block=block, timeout=0.05)
            with self.assertRaises(TtsError) as ctx:
                await provider.synth("你好")
            self.assertIn("连不上服务", str(ctx.exception))
            await provider.close()

    async def test_make_dispatches_wire(self):
        cfg = {"tts": {"source": "edge", "common": {"timeout_s": 1},
                       "edge": tts_presets.default_block("edge")}}
        provider = make(cfg)
        self.assertIsInstance(provider, EdgeProvider)
        self.assertEqual(provider.profile, "edge")
        self.assertEqual(provider.preset, "edge")

        cfg = {"tts": {"source": "openai", "common": {"timeout_s": 1},
                       "openai": {**tts_presets.default_block("openai"),
                                  "base_url": self.base, "api_key": "K"}}}
        provider = make(cfg)
        self.assertIsInstance(provider, HttpProvider)
        self.assertEqual(provider.profile, "openai")
        await provider.close()


if __name__ == "__main__":
    import unittest

    unittest.main()
