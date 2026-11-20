"""配置、密钥、模型连接测试与 TTS 试听。"""
from __future__ import annotations

import asyncio
import copy
import io
import wave

import numpy as np
from aiohttp import web

from sidekick import config
from sidekick.app import identity
from sidekick.config import secrets as secretstore
from sidekick.brain.llm import LLMClient, LLMError
from sidekick.webui import form as schema
from sidekick.webui.http import error, json_response, strip_secrets
from sidekick.webui.context import WebContext


def _replace_headers(raw: dict, patch: dict) -> None:
    """headers 在表单里是整块 JSON：保存时整块替换，删掉的项才真的删除。

    密钥型 header 的引用仍从旧配置保留：值由 /api/secrets 管理，普通保存
    不能把槽位抹掉；它们的删除是既有边界。
    """
    for section in ("llm", "tts"):
        patch_block = patch.get(section)
        raw_block = raw.get(section)
        if not isinstance(patch_block, dict) or not isinstance(raw_block, dict):
            continue
        for owner, pblock in patch_block.items():
            if owner in ("source", "common", "enabled"):
                continue
            if not isinstance(pblock, dict):
                continue
            headers = pblock.get("headers")
            if not isinstance(headers, dict):
                continue
            old_block = raw_block.get(owner)
            old = old_block.get("headers") if isinstance(old_block, dict) else None
            kept = {
                str(name): value for name, value in (old or {}).items()
                if secretstore.is_secret_header(str(name))
            }
            kept.update({str(name): value for name, value in headers.items()})
            pblock["headers"] = kept
            if isinstance(old_block, dict):
                old_block["headers"] = {}


def _drop_blank_fields(dst: dict, patch: dict) -> None:
    """表单清空 = 用默认值：删掉该键，让 config.load() 的默认值生效。

    ``reasoning_effort`` 例外：空串是“不传”的显式值，不能被默认块补回 low。
    """
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _drop_blank_fields(dst[k], v)
        elif v == "" and k != "reasoning_effort":
            dst.pop(k, None)


class ConfigRoutes:
    def __init__(self, ctx: WebContext) -> None:
        self.ctx = ctx

    async def bootstrap(self, _request):
        cfg = self.ctx.cfg()
        snap = await self.ctx.feed.snapshot(force=True) or {}
        return json_response({
            "ok": True,
            "csrf": self.ctx.csrf,
            "config": config.redacted(cfg),
            "schema": await asyncio.to_thread(
                schema.build_schema, self.ctx.catalog, self.ctx.root),
            "secrets_error": secretstore.last_error,
            "config_error": config.last_error,
            "config_notice": self.ctx.config_notice,
            "status": snap.get("runtime") or {},
            "memory": snap.get("memory") or {},
        })

    async def patch_config(self, request):
        async with self.ctx.config_lock:
            return await self._patch_config(request)

    async def _patch_config(self, request):
        body = await request.json()
        if not isinstance(body, dict):
            return error("body 必须是 JSON 对象")
        before = config.load_raw()
        if config.last_error:
            return error(
                f"config.yaml 解析失败，已拒绝覆盖保存：{config.last_error}。"
                "请先修复文件再保存。", 409)
        if "path" in body:
            raw = copy.deepcopy(before)
            path = str(body.get("path") or "")
            if not path:
                return error("缺少 path")
            bits = [x.lower() for x in path.split(".") if x]
            if (path.lower() == "sessdata"
                    or any(x in ("api_key", "sessdata", "headers") for x in bits)
                    or secretstore.is_secret_path(path)):
                return error("密钥/敏感 header 请走 /api/secrets，或保存整份配置")
            if body.get("delete"):
                config.delete_path(raw, path)
            else:
                value = body.get("value")
                if isinstance(value, dict):
                    value = strip_secrets(value)
                config.set_path(raw, path, value)
        else:
            patch = strip_secrets(body.get("patch", body))
            raw = copy.deepcopy(before)
            _replace_headers(raw, patch)
            config.deep_update(raw, patch)
            _drop_blank_fields(raw, patch)
        removed_scopes = secretstore.scopes(before) - secretstore.scopes(raw)
        errs = config.validate_raw(raw)
        if errs:
            return json_response({"ok": False, "errors": errs}, 400)
        try:
            await asyncio.to_thread(config.save_raw, raw)
        except secretstore.SecretsError as e:
            return error(f"密钥写入失败，配置未保存：{e}", 409)
        for scope in sorted(removed_scopes):
            await asyncio.to_thread(secretstore.delete_prefix, scope)
        await asyncio.to_thread(self.ctx.get_memory, True)
        return json_response({"ok": True, "config": config.redacted()})

    def mark_secret_ref(self, name: str, present: bool) -> None:
        """把 config.yaml 里的引用改成 sealed / 空，不写明文。"""
        slot = secretstore.parse_slot(name)
        if slot is None:
            raise ValueError(f"不认识的密钥位置：{name}")
        raw = config.load_raw()
        if config.last_error:
            raise secretstore.SecretsError(
                f"config.yaml 解析失败，未写入密钥引用：{config.last_error}")
        val = secretstore.SEALED if present else ""
        if slot.is_sessdata:
            raw["sessdata"] = val
        else:
            block = (raw.setdefault(slot.section, {})
                     .setdefault(slot.owner, {}))
            if slot.key == "api_key":
                block["api_key"] = val
            else:
                block.setdefault("headers", {})[slot.header] = val
        config.save_raw(raw)

    async def set_secret(self, request):
        async with self.ctx.config_lock:
            return await self._set_secret(request)

    async def _set_secret(self, request):
        body = await request.json()
        if not isinstance(body, dict):
            return error("body 必须是 JSON 对象")
        name = str(body.get("name") or "").strip()
        action = str(body.get("action") or "set").lower()
        slot = secretstore.parse_slot(name)
        if slot is None:
            return error("不认识的密钥位置")
        raw = config.load_raw()
        if config.last_error:
            return error(
                f"config.yaml 解析失败，不能写入密钥：{config.last_error}。"
                "请先修复文件。", 409)
        if not slot.is_sessdata:
            block = (raw.get(slot.section) or {}).get(slot.owner)
            if not isinstance(block, dict):
                label = "模型 profile" if slot.section == "llm" else "TTS profile"
                return error(f"{label}不存在", 404)
        if action == "clear":
            secretstore.clear_secret(name)
            self.mark_secret_ref(name, False)
            return json_response({"ok": True, "configured": False})
        value = str(body.get("value") or "").strip()
        if not value:
            return error("密钥不能为空；要删除请用 clear")
        secretstore.set_secret(name, value)
        self.mark_secret_ref(name, True)
        return json_response({"ok": True, "configured": True})

    def llm_client(self, name: str, body: dict) -> LLMClient:
        """profile 块 + 草稿 + 密钥 → 共享的 LLMClient。失败抛 ValueError。"""
        cfg = self.ctx.cfg()
        llm = cfg["llm"]
        if name not in llm or not isinstance(llm[name], dict):
            raise ValueError("profile 不存在")
        block = {**llm["common"], **copy.deepcopy(llm[name])}
        draft = (body or {}).get("draft") or {}
        if isinstance(draft, dict):
            block.update({k: v for k, v in draft.items()
                          if k != "api_key" and not isinstance(v, dict)
                          and not (k == "temperature"
                                   and not str(v).strip())})
            if isinstance(draft.get("headers"), dict):
                block["headers"] = draft["headers"]
        key = str((body or {}).get("api_key") or
                  (draft or {}).get("api_key") or "").strip()
        if not key or key.lower() == secretstore.SEALED:
            key = secretstore.get(f"llm.{name}.api_key")
        if not key:
            key = str(block.get("api_key") or "").strip()
        if key.strip().lower() == secretstore.SEALED:
            key = ""
        hs = block.get("headers")
        if isinstance(hs, dict):
            for hk, hv in list(hs.items()):
                if secretstore.is_secret_header(hk):
                    hs[hk] = secretstore.get(f"llm.{name}.headers.{hk}") or hv
        return LLMClient(cfg, block=block, key=key)

    async def llm_test(self, request):
        body = await request.json()
        name = str((body or {}).get("profile") or "").strip()
        try:
            client = self.llm_client(name, body)
        except ValueError as e:
            return error(str(e))
        try:
            res = await client.probe(timeout=30.0)
            if res.get("ok"):
                # 连上了顺手把可用模型带回去，前端就能填下拉
                try:
                    res["models"] = await client.list_models(timeout=15.0)
                except LLMError as e:
                    res["models"] = []
                    res["models_error"] = str(e)[:200]
            return json_response(res)
        finally:
            await client.close()

    async def tts_preview(self, request):
        body = await request.json()
        profile = str((body or {}).get("profile") or "").strip()
        preset_id = str((body or {}).get("preset") or "").strip()
        block = (body or {}).get("block")
        if not isinstance(block, dict):
            return error("block 必须是 JSON 对象")
        cfg = preview_tts_cfg(self.ctx.cfg(), profile, preset_id, block,
                              (body or {}).get("api_key"))
        resolved = await asyncio.to_thread(
            identity.resolve, self.ctx.cfg(), self.ctx.root)
        bot = str(resolved.cfg["persona"]["bot_name"]).strip()
        default_text = (f"嗨，我是{bot}，能听见吗？" if bot
                        else "嗨，能听见吗？")
        text = str((body or {}).get("text") or default_text)[:120]
        try:
            from sidekick.output.tts import make as make_tts

            provider_obj = make_tts(cfg)
            try:
                res = await asyncio.wait_for(provider_obj.synth(text), timeout=35)
            finally:
                try:
                    await provider_obj.close()
                except Exception:
                    pass
            return web.Response(body=_wav_bytes(res.pcm, res.sr),
                                content_type="audio/wav",
                                headers={"Cache-Control": "no-store"})
        except SystemExit as e:
            return error(f"TTS 配置错误：{e}", 400)
        except Exception as e:
            return error(f"{type(e).__name__}: {e}", 400)


def add_routes(router: web.UrlDispatcher, ctx: WebContext) -> None:
    routes = ConfigRoutes(ctx)
    router.add_get("/api/bootstrap", routes.bootstrap)
    router.add_patch("/api/config", routes.patch_config)
    router.add_post("/api/secrets", routes.set_secret)
    router.add_post("/api/llm/test", routes.llm_test)
    router.add_post("/api/tts/preview", routes.tts_preview)


def preview_tts_cfg(cfg: dict, profile: str, preset_id: str, block: dict,
                    api_key: str | None) -> dict:
    """把试听草稿合并成一份**只用于本次试听**的配置。

    `block` 是当前表单值；`api_key` 优先用未保存的输入，其次用
    `tts.<profile>.api_key`。`sealed` 永远不能当字面量 key 发给厂商。
    """
    merged = {k: v for k, v in block.items() if k != "api_key"}
    merged["preset"] = preset_id
    incoming = str(api_key or block.get("api_key") or "").strip()
    if incoming and incoming.lower() != secretstore.SEALED:
        merged["api_key"] = incoming
    else:
        stored = secretstore.get(f"tts.{profile}.api_key") if profile else ""
        if stored:
            merged["api_key"] = stored
        else:
            existing = str(((cfg["tts"].get(profile) or {})
                            .get("api_key") or "")).strip()
            if existing and existing.lower() != secretstore.SEALED:
                merged["api_key"] = existing
    return {"tts": {
        "source": "__preview__",
        "common": dict(cfg["tts"]["common"]),
        "__preview__": merged,
    }}


def _wav_bytes(pcm, sr: int) -> bytes:
    arr = np.asarray(pcm, dtype=np.float32)
    arr = np.clip(arr, -1.0, 1.0)
    data = (arr * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr or 24000))
        w.writeframes(data)
    return buf.getvalue()
