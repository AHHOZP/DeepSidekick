"""通用 HTTP TTS wire。

只认 preset 数据：路径 / query / 静态头 / JSON body / 响应种类。厂商差异
全部在 `sidekick/config/tts_presets.py`，这里不出现厂商名。

一次请求两种读法：
  · `audio=pcm`：边收边解（`stream()`），或读到底拼起来（`synth()`）；
  · `audio=wav|mp3`：整段读完再解码，不声明流式。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp
import numpy as np

from sidekick.config import tts_presets
from sidekick.output.tts.base import Provider, SynthResult, decode_any, from_pcm16

_PLACEHOLDER = re.compile(r"\{([a-zA-Z0-9_]+)\}")


class TtsError(RuntimeError):
    """带中文文案的合成失败；试听与运行期共用。"""


def _dig(obj, path: str):
    cur = obj
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _decode(value: str, encoding: str) -> bytes:
    try:
        if encoding == "hex":
            return bytes.fromhex(value)
        return base64.b64decode(value)
    except (ValueError, TypeError) as e:
        raise TtsError(f"音频数据解不开（{encoding}）：{value[:60]}") from e


def _http_error(status: int, raw: bytes) -> str:
    excerpt = raw[:200].decode("utf-8", "replace").strip()
    if status == 401:
        msg = "密钥无效：确认复制了完整 Key，且账号已开通该语音服务"
    elif status == 403:
        msg = "没有权限：账号未开通该服务，或 Key 被限制"
    elif status == 402:
        msg = "余额不足：去厂商控制台充值，或换免费额度模型"
    elif status == 404:
        msg = "接口地址不对：检查 Base URL（到 /v1 或根地址为止）"
    elif status == 429:
        msg = "请求太频繁：稍等再试，或调大 tts.common.timeout_s"
    elif 500 <= status < 600:
        msg = "厂商服务端出错：稍后重试"
    else:
        msg = f"请求失败（HTTP {status}）"
    return f"{msg}：{excerpt}" if excerpt else msg


class HttpProvider(Provider):
    def __init__(self, *, profile: str, preset_id: str, block: dict,
                 timeout: float) -> None:
        self.profile = profile
        self.preset = preset_id
        self.spec = tts_presets.PRESETS[preset_id]
        self.block = {**tts_presets.default_block(preset_id), **dict(block)}
        self.timeout = float(timeout)
        self._key = str(block.get("api_key") or "").strip()
        self._session: aiohttp.ClientSession | None = None
        self.supports_stream = bool(
            self.spec.get("stream", True)
            and self.spec.get("audio", "pcm") == "pcm"
            and self.spec["response"]["kind"] in ("raw", "sse"))
        self.stream_sr = self._pcm_sr() if self.supports_stream else 0
        self.prompt_rules = tts_presets.prompt_rules(preset_id)
        self.allowed_tags = tts_presets.allowed_tags(preset_id)
        self.preserve_square_tags = tts_presets.preserve_square_tags(preset_id)

    # ── 配置读取 ────────────────────────────────────────
    @property
    def has_key(self) -> bool:
        return True if not self.spec.get("auth") else bool(self._key)

    def adapt_text(self, text: str) -> str:
        return tts_presets.adapt_text(self.preset, text)

    def _pcm_sr(self) -> int:
        meta = self.spec["fields"].get("pcm_sr")
        if meta is not None:
            raw = self.block.get("pcm_sr") or meta.get("default")
            try:
                return int(raw)
            except (TypeError, ValueError):
                return int(meta.get("default") or 24000)
        return int(self.spec.get("pcm_sr") or 24000)

    def _audio_kind(self) -> str:
        return str(self.spec.get("audio") or "pcm")

    def _response(self) -> dict:
        return self.spec["response"]

    # ── 请求渲染 ────────────────────────────────────────
    def _values(self, text: str) -> dict:
        values = {k: v for k, v in self.block.items()
                  if k not in ("api_key",) and not isinstance(v, (dict, list))}
        values["text"] = text
        values["pcm_sr"] = self._pcm_sr()
        return values

    @staticmethod
    def _render(value, values: dict):
        if isinstance(value, dict):
            return {k: HttpProvider._render(v, values) for k, v in value.items()}
        if isinstance(value, list):
            return [HttpProvider._render(v, values) for v in value]
        if not isinstance(value, str):
            return value
        exact = _PLACEHOLDER.fullmatch(value)
        if exact and exact.group(1) in values:
            return values[exact.group(1)]
        return _PLACEHOLDER.sub(
            lambda m: str(values.get(m.group(1), m.group(0))), value)

    def _body(self, text: str) -> dict:
        body = self._render(self.spec.get("body") or {}, self._values(text))
        for key in self.spec.get("body_optional") or ():
            if key in body and body[key] in ("", None):
                del body[key]
        return body

    def _url(self, text: str) -> str:
        values = self._values(text)
        base = str(self.block.get("base_url") or "").strip().rstrip("/")
        path = str(self._render(self.spec.get("path") or "", values))
        if not base:
            raise TtsError("缺少 Base URL：在 TTS 页填好并保存")
        parts = urlsplit(base)
        base_path = parts.path.rstrip("/")
        if path and not base_path.endswith(path):
            base_path += path if path.startswith("/") else "/" + path
        query = self._render(self.spec.get("query") or {}, values)
        merged = "&".join(x for x in (parts.query, urlencode(query)) if x)
        return urlunsplit((parts.scheme, parts.netloc, base_path,
                           merged, parts.fragment))

    def _headers(self, text: str) -> dict:
        headers = {"Content-Type": "application/json"}
        auth = self.spec.get("auth") or {}
        if auth:
            if not self._key:
                raise TtsError("缺少 API Key：在 TTS 页填好并保存")
            if auth.get("kind") == "bearer":
                headers["Authorization"] = f"Bearer {self._key}"
            elif auth.get("kind") == "header":
                headers[str(auth["name"])] = self._key
        headers.update(self._render(self.spec.get("headers") or {},
                                    self._values(text)))
        return headers

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=None, sock_connect=10, sock_read=self.timeout))
        return self._session

    @contextlib.asynccontextmanager
    async def _open(self, text: str):
        session = await self._get_session()
        url = self._url(text)
        try:
            resp = await session.post(url, json=self._body(text),
                                      headers=self._headers(text))
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise TtsError(
                "连不上服务：检查网络、代理或 Base URL"
                f"（{type(e).__name__}）") from e
        try:
            if resp.status != 200:
                raw = await resp.read()
                raise TtsError(_http_error(resp.status, raw))
            yield resp
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # resp.read()/iter_chunked 的读超时在 yield 之后才抛，不能只包 post。
            raise TtsError(
                "连不上服务：检查网络、代理或 Base URL"
                f"（{type(e).__name__}）") from e
        finally:
            resp.release()

    # ── 合成 ────────────────────────────────────────────
    async def synth(self, text: str) -> SynthResult:
        if self._audio_kind() != "pcm":
            async with self._open(text) as resp:
                data = await resp.read()
            if not data:
                raise TtsError("厂商没返回音频")
            pcm, sr = decode_any(data, self._audio_kind())
            return SynthResult(pcm=pcm, sr=sr)

        chunks = [chunk async for chunk in self.stream(text)]
        if not chunks:
            raise TtsError("厂商没返回音频")
        return SynthResult(pcm=np.concatenate(chunks), sr=self._pcm_sr())

    async def stream(self, text: str):
        async with self._open(text) as resp:
            if self._response()["kind"] == "raw":
                tail = b""
                async for data in resp.content.iter_chunked(8192):
                    buf = tail + data
                    if len(buf) % 2:
                        tail = buf[-1:]
                        buf = buf[:-1]
                    else:
                        tail = b""
                    if len(buf) >= 2:
                        yield (np.frombuffer(buf, dtype="<i2")
                               .astype(np.float32) / 32768.0)
                return
            async for chunk in self._sse(resp):
                yield chunk

    async def _sse(self, resp):
        spec = self._response()
        path = spec["path"]
        encoding = spec["encoding"]
        end = spec.get("end")
        done = False
        tail = b""
        excerpt = ""
        saw_audio = False
        async for raw_line in resp.content:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            if not excerpt:
                excerpt = payload[:200]
            if end and payload == end:
                done = True
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            value = _dig(obj, path)
            if not value:
                continue
            data = _decode(str(value), encoding)
            if not data:
                continue
            buf = tail + data
            if len(buf) % 2:
                tail = buf[-1:]
                buf = buf[:-1]
            else:
                tail = b""
            if len(buf) >= 2:
                saw_audio = True
                yield from_pcm16(buf, self._pcm_sr())[0]
        if end and not done:
            raise TtsError(
                "流式连接提前结束（没有 "
                f"{end}）" + (f"：{excerpt}" if excerpt else ""))
        if not saw_audio:
            raise TtsError("厂商没返回音频" + (f"：{excerpt}" if excerpt else ""))

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
