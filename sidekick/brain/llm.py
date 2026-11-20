"""LLM 客户端：profile 解析、headers、HTTP、重试、JSON 解码。

Brain / WebUI 的「测试连接」/ 动作规则生成共用这一份，避免三处各拼一套
URL、header、body。密钥只在内存里出现，不写日志、不进 prompt。

只认 OpenAI chat/completions 一种 wire；厂商默认值在
`sidekick/config/llm_presets.py`，本模块不 import 它。
"""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from sidekick.core.text import merge_systems

# 请求体里由程序字段负责的键：body 里写了也丢弃，避免第二份来源。
_RESERVED_BODY = {"messages", "model", "stream",
                  "temperature", "reasoning_effort"}


def _count_images(messages: list[dict]) -> int:
    """数实际发出去的 image_url 块，而不是数调用方以为带了几张。"""
    total = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        total += sum(1 for part in content
                     if isinstance(part, dict)
                     and part.get("type") == "image_url")
    return total


def _payload_kb(body: dict) -> float:
    """请求体的 UTF-8 JSON 大小；含 base64 图片，不含 headers。

    与 aiohttp 默认 `json.dumps` 一致（ensure_ascii=True、默认分隔符），
    这样测到的数和真正发出去的 body 对得上。
    """
    raw = json.dumps(body).encode("utf-8")
    return round(len(raw) / 1024.0, 1)


def parse_completion(data: dict) -> tuple[str, str | None]:
    """返回 (正文, finish_reason)；结构不对时抛给调用方统一处理。"""
    choice = (data.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    return str(text), choice.get("finish_reason")


def text_of(data: dict) -> str:
    """从 chat/completions 返回里取正文；给不依赖具体 client 的调用方用。"""
    try:
        return parse_completion(data)[0]
    except (AttributeError, IndexError, TypeError):
        return ""


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None,
                 raw: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.raw = raw


def _profile(cfg: dict) -> tuple[str, dict]:
    """返回 (profile 名, 合并 common 后的配置块)。名字和配置键一样区分大小写。"""
    c = cfg["llm"]
    src = str(c["source"]).strip()
    return src, {**c["common"], **c[src]}


def _endpoint(base: str) -> str:
    """base 或完整 /chat/completions → 实际请求地址；查询参数保留。"""
    raw = str(base or "").strip()
    parts = urlsplit(raw)
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return raw
    return urlunsplit(parts._replace(path=path + "/chat/completions"))


def _models_endpoint(base: str) -> str | None:
    """推导 `GET {base}/models`；base 不是 http(s) 地址时返回 None。"""
    parts = urlsplit(str(base or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]
    return urlunsplit(parts._replace(path=path + "/models", query=""))


class LLMClient:
    def __init__(self, cfg: dict | None = None, *,
                 block: dict | None = None, key: str | None = None) -> None:
        cfg = cfg or {}
        if block is None:
            src, block = _profile(cfg)
        else:
            src = str(cfg["llm"]["source"])
        self.source = src
        self.base_url = str(block.get("base_url") or "").strip()
        self.model = str(block.get("model") or "").strip()
        self._key = str(key if key is not None
                        else block.get("api_key", "")).strip()
        self.effort = str(block.get("reasoning_effort") or "").strip()
        self.effort_dropped = False
        self.temp = float(block["temperature"])
        self.warmup_enabled = bool(block["warmup"])
        self.timeout = float(block["timeout_s"])
        self.system_mode = str(block["system_mode"]).strip().lower()
        self.body = {
            str(k): v for k, v in (block.get("body") or {}).items()
            if str(k) not in _RESERVED_BODY
        }
        self.custom_headers: dict[str, str] = {}
        raw_headers = block.get("headers")
        if isinstance(raw_headers, dict):
            for hk, hv in raw_headers.items():
                hk, hv = str(hk).strip(), str(hv).strip()
                if hk and hv and hv.lower() != "sealed":
                    self.custom_headers[hk] = hv
        self._single_system = False
        self._single_system_at = 0.0
        self._session: aiohttp.ClientSession | None = None
        self.last_status: int | None = None
        self.last_raw = ""
        self.last_usage: dict = {}

    @property
    def ready(self) -> bool:
        """Base URL 和模型都有了就能发请求；key 可以空。"""
        return bool(self.base_url.strip() and self.model.strip())

    @property
    def single_system(self) -> bool:
        """网关已经拒绝过多 system，或配置明确要求单条。"""
        if (self._single_system and self.system_mode != "single"
                and time.monotonic() - self._single_system_at > 600.0):
            self._single_system = False
        return bool(self._single_system or self.system_mode == "single")

    def body_defaults(self) -> dict:
        """Brain 拼展示用 body 与真正请求共用的默认字段。"""
        out = dict(self.body)
        out.update({"model": self.model, "temperature": self.temp,
                    "stream": False})
        if self.effort:
            out["reasoning_effort"] = self.effort
        return out

    def request_body(self, messages: list[dict],
                     temperature: float | None = None,
                     reasoning_effort: str | None = None) -> dict:
        """唯一一份请求体：Brain 预览和真正发送都调这里。

        不传 `max_tokens`：由模型自己决定什么时候说完，截断反而会截掉尾巴。
        ``reasoning_effort`` 仅由调用方显式覆盖；空字符串表示本次删除该字段。
        """
        msgs = merge_systems(messages) if self.single_system else messages
        body: dict = {"messages": msgs, **self.body_defaults()}
        if temperature is not None:
            body["temperature"] = float(temperature)
        if reasoning_effort is not None:
            if reasoning_effort:
                body["reasoning_effort"] = str(reasoning_effort)
            else:
                body.pop("reasoning_effort", None)
        return body

    @property
    def url(self) -> str:
        return _endpoint(self.base_url)

    @property
    def models_url(self) -> str | None:
        return _models_endpoint(self.base_url)

    async def list_models(self, *, timeout: float = 15.0) -> list[str]:
        """问网关要可用模型列表（`GET {base}/models`）；失败抛 LLMError。"""
        models_url = self.models_url
        if models_url is None:
            raise LLMError("Base URL 不是 http(s) 地址，取不了模型列表")
        sess = await self._get_session()
        current = aiohttp.ClientTimeout(total=timeout)
        try:
            async with sess.get(models_url, headers=self.headers(),
                                timeout=current) as r:
                raw = await r.text()
                status = r.status
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise LLMError(f"取模型列表失败：{type(e).__name__}: {e}") from e
        if status >= 400:
            raise LLMError(f"取模型列表失败：HTTP {status} {raw[:200]}", status=status)
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise LLMError(f"模型列表不是 JSON：{raw[:120]}") from e
        items = data.get("data") if isinstance(data, dict) else data
        out: list[str] = []
        for item in items or []:
            mid = item.get("id") if isinstance(item, dict) else item
            if isinstance(mid, str) and mid.strip():
                out.append(mid.strip())
        return out

    def headers(self) -> dict:
        out = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._key:
            out["Authorization"] = f"Bearer {self._key}"
        for name, value in self.custom_headers.items():
            lowered = name.lower()
            for existing in [k for k in out if k.lower() == lowered]:
                out.pop(existing)
            out[name] = value
        return out

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _body(self, messages: list[dict], temperature: float,
              reasoning_effort: str | None = None) -> dict:
        return self.request_body(messages, temperature, reasoning_effort)

    async def chat(self, messages: list[dict], *,
                   temperature: float | None = None,
                   reasoning_effort: str | None = None,
                   timeout: float | None = None,
                   attempts: int) -> dict:
        """发一次 chat/completions，返回解析后的 JSON。

        - 网络错误 / 429 / 5xx：重试；
        - `reasoning_effort` 被拒：自动去掉后重试一次（同一次探测，不占配额）；
          显式传入 `reasoning_effort` 时只影响本次请求，被拒后本次降级；
        - 网关只收一条 system：自动合并后重试一次；
        - 其它 4xx：直接抛 LLMError（重试也白费）。
        """
        self.last_status = None
        self.last_raw = ""
        budget = max(1, int(attempts))
        cap = max(2, int(attempts) + 1)
        sent_count = 0
        temp = float(temperature if temperature is not None else self.temp)
        effort = reasoning_effort
        last_error: str | None = None
        started = time.monotonic()
        while budget > 0 and sent_count < cap:
            budget -= 1
            sent_count += 1
            body = self._body(messages, temp, effort)
            try:
                sess = await self._get_session()
                current = aiohttp.ClientTimeout(total=timeout or self.timeout)
                async with sess.post(self.url, json=body, headers=self.headers(),
                                     timeout=current) as r:
                    raw = await r.text()
                    status = r.status
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 网络失败没有 HTTP 状态；不能把上一次响应的 400/422 带进
                # 最终异常，否则 Brain 会误判成不可重试的 4xx。
                self.last_status = None
                self.last_raw = ""
                last_error = f"调用失败：{type(e).__name__}: {e}"
                if budget:
                    await asyncio.sleep(0.4)
                continue

            self.last_status = status
            self.last_raw = raw
            if status == 200:
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    last_error = f"返回解析不了：{raw[:200]}"
                    if budget:
                        await asyncio.sleep(0.4)
                    continue
                usage = dict(data.get("usage") or {})
                usage["prompt_tokens"] = usage.get("prompt_tokens")
                usage.update({
                    "image_count": _count_images(body["messages"]),
                    "payload_kb": _payload_kb(body),
                    "latency_ms": round((time.monotonic() - started) * 1000),
                })
                self.last_usage = usage
                return data

            low = raw.lower()
            if (status in (400, 422)
                    and (self.effort or effort)
                    and ("reasoning_effort" in low
                         or "reasoning.effort" in low
                         or "reasoning effort" in low)):
                self.effort_dropped = True
                self.effort = ""
                effort = ""
                if sent_count < cap:
                    budget += 1
                continue
            if (status in (400, 422) and not self._single_system
                    and any(hint in low for hint in (
                        "only one system", "multiple system", "single system",
                        "one system message", "system message is not allowed"))):
                self._single_system = True
                self._single_system_at = time.monotonic()
                if sent_count < cap:
                    budget += 1
                continue
            last_error = f"HTTP {status}: {raw[:200]}"
            if 400 <= status < 500 and status != 429:
                raise LLMError(last_error, status=status, raw=raw)
            if budget:
                await asyncio.sleep(0.4)
        raise LLMError(last_error or "调用失败", status=self.last_status,
                       raw=self.last_raw)

    @staticmethod
    def text_of(data: dict) -> str:
        return text_of(data)

    async def stream_chat(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        first_token_timeout: float = 30.0,
    ):
        """流式 chat/completions；只在首个正文 token 上等超时。

        产出 ``{"type": "open"}`` / ``{"type": "delta", "text": ...}``；
        首个正文前超过 ``first_token_timeout`` 抛 ``LLMError``，有正文后
        不再设置总超时（长输出由上游自己结束）。
        """
        temp = float(temperature if temperature is not None else self.temp)
        effort = reasoning_effort
        self.last_status = None
        self.last_raw = ""
        body = self._body(messages, temp, effort)
        body["stream"] = True
        sess = await self._get_session()
        # 连接阶段仍要防呆；读超时不在 aiohttp 里设，首个 token 用 wait_for。
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15.0)
        try:
            async with sess.post(self.url, json=body, headers=self.headers(),
                                 timeout=timeout) as r:
                self.last_status = r.status
                if r.status != 200:
                    raw = await r.text()
                    self.last_raw = raw
                    low = raw.lower()
                    if (r.status in (400, 422)
                            and (self.effort or effort)
                            and ("reasoning_effort" in low
                                 or "reasoning.effort" in low
                                 or "reasoning effort" in low)):
                        # 与 chat() 一致：网关拒绝 effort 时本次降级重试。
                        self.effort_dropped = True
                        self.effort = ""
                        body = self._body(messages, temp, "")
                        body["stream"] = True
                        async with sess.post(self.url, json=body,
                                             headers=self.headers(),
                                             timeout=timeout) as retry:
                            self.last_status = retry.status
                            if retry.status != 200:
                                raw = await retry.text()
                                self.last_raw = raw
                                raise LLMError(
                                    f"HTTP {retry.status}: {raw[:200]}",
                                    status=retry.status, raw=raw)
                            yield {"type": "open"}
                            async for event in self._stream_events(
                                    retry, first_token_timeout):
                                yield event
                            return
                    raise LLMError(f"HTTP {r.status}: {raw[:200]}",
                                   status=r.status, raw=raw)
                yield {"type": "open"}
                async for event in self._stream_events(r, first_token_timeout):
                    yield event
        except asyncio.CancelledError:
            raise
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"流式调用失败：{type(e).__name__}: {e}") from e

    async def _stream_events(self, response, first_token_timeout: float):
        """读取 SSE；首字前后使用不同的等待策略。"""
        first = False
        started = False
        deadline = time.monotonic() + float(first_token_timeout)
        reader = response.content.__aiter__()
        while True:
            remaining = (None if started
                         else max(0.0, deadline - time.monotonic()))
            try:
                raw_line = await asyncio.wait_for(
                    reader.__anext__(), timeout=remaining)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as e:
                raise LLMError(
                    f"等待首字超时（{first_token_timeout:g}s）") from e
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except (TypeError, ValueError):
                continue
            self.last_usage = obj.get("usage") or self.last_usage
            choice = (obj.get("choices") or [{}])[0]
            delta_obj = choice.get("delta") or {}
            reasoning = (delta_obj.get("reasoning_content")
                         or delta_obj.get("reasoning") or "")
            if reasoning:
                if not started:
                    started = True
                    yield {"type": "thinking"}
                continue
            delta = delta_obj.get("content") or ""
            if delta:
                started = True
                first = True
                yield {"type": "delta", "text": str(delta)}
            finish = choice.get("finish_reason")
            if finish:
                yield {"type": "finish", "reason": str(finish)}
        if not first:
            raise LLMError("没有收到正文（只有思考内容或空响应）")

    async def probe(self, *, timeout: float = 30.0) -> dict:
        """WebUI「测试连接」：不抛异常，返回可序列化结果。"""
        t0 = time.time()
        try:
            data = await self.chat([
                {"role": "system", "content": "你是测试助手，只回复 OK 两个字母。"},
                {"role": "user", "content": "测试连接：只回复 OK。"},
            ], temperature=0, timeout=timeout, attempts=1)
        except LLMError as e:
            return {"ok": False, "status": e.status,
                    "ms": round((time.time() - t0) * 1000),
                    "error": str(e)[:300]}
        except Exception as e:
            return {"ok": False, "status": None,
                    "ms": round((time.time() - t0) * 1000),
                    "error": f"{type(e).__name__}: {e}"[:300]}
        return {"ok": True, "status": self.last_status,
                "ms": round((time.time() - t0) * 1000),
                "text": self.text_of(data)[:200],
                "usage": data.get("usage") or {}}

    def cache_line(self) -> str:
        u = self.last_usage or {}
        hit = u.get("prompt_cache_hit_tokens")
        miss = u.get("prompt_cache_miss_tokens")
        if hit is None and miss is None:
            total = u.get("prompt_tokens")
            return f"prompt {total} tok（这路由不报缓存明细）" if total else ""
        tot = int(hit or 0) + int(miss or 0)
        pct = (int(hit or 0) / tot * 100) if tot else 0.0
        return f"缓存 {int(hit or 0)}/{tot} ({pct:.0f}%)"

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
