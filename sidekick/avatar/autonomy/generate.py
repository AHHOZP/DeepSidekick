"""LLM 单阶段生成 motion_profile：审计 → prompt → 校验/repair → fallback。

生成只返回预览，不写盘；写盘由 WebUI 的用户「应用」动作完成。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from sidekick.avatar.autonomy import fallback, prompts, validate
from sidekick.avatar.autonomy.audit import find_avatar, run_audit
from sidekick.brain.llm import LLMClient, LLMError, parse_completion
from sidekick.core.log import log

REPAIRS = 2
#: 生成 profile 的输入比普通聊天大得多；走 llm.common.timeout_s（通常 30s）
#: 会在正常网络下超时。这里给生成/修复更长的单次预算，不额外暴露配置。
GENERATION_TIMEOUT_S = 120.0
#: 配置生成是低频、长输出任务，固定用高随机性 + 最大推理，不跟随聊天 profile。
GENERATION_TEMPERATURE = 1.0
GENERATION_REASONING_EFFORT = "max"
#: 首次生成/修复前留一点静默期，避免连续长请求把网关打满。
GENERATION_WAIT_S = 2.0
GENERATION_REPAIR_WAIT_S = 3.0
#: 只看首个正文 token；收到后就交给上游，不再设总超时。
GENERATION_FIRST_TOKEN_TIMEOUT_S = 30.0
#: 上游在正文前断流时重试一次；已经吐出正文后不重试，避免重复内容。
GENERATION_STREAM_RETRY_WAIT_S = 1.0


class GenerationError(RuntimeError):
    pass


def _generated(provider: str, model: str, *, fallback_used: bool) -> dict:
    return {
        "provider": str(provider or ""),
        "model": str(model or ""),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "fallback": bool(fallback_used),
    }


def _summary(dossier: dict) -> dict:
    return {
        "model": (dossier.get("model") or {}).get("name", ""),
        "entry": (dossier.get("model") or {}).get("entry", ""),
        "canvas": (dossier.get("model") or {}).get("canvas", []),
        "tier": dossier.get("tier", "none"),
        "bound": len(dossier.get("by_channel") or {}),
        "motions": len(dossier.get("motions") or []),
        "expressions": len(dossier.get("expressions") or []),
        "states": len(dossier.get("states") or {}),
        "vtube": bool((dossier.get("vtube") or {}).get("present")),
        "persistent_expressions": sum(
            1 for item in dossier.get("expressions") or []
            if isinstance(item, dict) and item.get("kind") == "persistent"),
        "warnings": list(dossier.get("warnings") or [])[:20],
    }


async def _emit(progress, event: dict) -> None:
    if progress is not None:
        await progress(event)


async def _ask_stream(llm, messages: list[dict], progress) -> dict | None:
    """流式生成：正文前断流重试一次，正文后不重试。

    `ClientPayloadError: Response payload is not completed` 是上游/网关的传输
    截断，不是我们的 JSON 解析问题；它常在一次重试后恢复。已经收到正文后
    再重试会把半截内容变成两份，所以只在 ``first`` 仍为 False 时重试。
    """
    for attempt in range(2):
        parts: list[str] = []
        first = False
        try:
            async for event in llm.stream_chat(
                    messages, temperature=GENERATION_TEMPERATURE,
                    reasoning_effort=GENERATION_REASONING_EFFORT,
                    first_token_timeout=GENERATION_FIRST_TOKEN_TIMEOUT_S):
                kind = event.get("type")
                if kind == "open":
                    await _emit(progress, {"type": "status", "stage": "thinking"})
                elif kind == "thinking":
                    await _emit(progress, {"type": "status", "stage": "thinking"})
                elif kind == "delta":
                    text = str(event.get("text") or "")
                    if not text:
                        continue
                    if not first:
                        first = True
                        await _emit(progress, {"type": "status",
                                               "stage": "outputting"})
                    parts.append(text)
                    await _emit(progress, {"type": "delta", "text": text})
        except LLMError as exc:
            if first or attempt:
                raise
            log(f"[autonomy] 生成流在正文前中断，重试一次：{exc}")
            await asyncio.sleep(GENERATION_STREAM_RETRY_WAIT_S)
            continue
        if not first:
            raise LLMError("等待首字超时（30s）：没有收到正文")
        return prompts.extract_json("".join(parts))
    return None


async def _ask(llm, messages: list[dict], *,
               wait: float | None = None, progress=None) -> dict | None:
    delay = GENERATION_WAIT_S if wait is None else float(wait)
    if delay > 0:
        await asyncio.sleep(delay)
    await _emit(progress, {"type": "status", "stage": "sending"})
    if progress is not None and hasattr(llm, "stream_chat"):
        return await _ask_stream(llm, messages, progress)
    await _emit(progress, {"type": "status", "stage": "thinking"})
    data = await llm.chat(
        messages, temperature=GENERATION_TEMPERATURE,
        reasoning_effort=GENERATION_REASONING_EFFORT,
        timeout=GENERATION_TIMEOUT_S, attempts=2)
    _text, finish = parse_completion(data)
    if finish == "length":
        raise LLMError("模型输出被截断（finish_reason=length）")
    return prompts.extract_json(LLMClient.text_of(data))


def _baseline(dossier: dict) -> dict:
    """程序先算出的完整骨架；LLM 只能在其上追加/调优。"""
    data = fallback.fallback_profile(dossier, reason="")
    data["warnings"] = []
    clean, _issues = validate.validate(data, dossier)
    return clean or data


def _fallback(dossier: dict, reason: str, issues: list[str] | None = None) -> dict:
    data = fallback.fallback_profile(dossier, reason=reason)
    if issues:
        data["warnings"] = list(data.get("warnings") or []) + issues[:20]
    clean, _ = validate.validate(data, dossier)
    return clean


async def generate_profile(avatar, cfg: dict, *, llm=None,
                           preferences: str = "",
                           audit_runner=run_audit,
                           project_root: str | Path | None = None,
                           progress=None) -> dict:
    """生成 profile 预览。返回 ``{profile, fallback, issues, dossier, error}``。

    ``project_root`` 必须是仓库根而不是模型目录；审计子进程要靠它把 `sidekick`
    放进 PYTHONPATH。WebUI/CLI 都显式传入。
    """
    if getattr(avatar, "kind", "") != "live2d":
        raise GenerationError("自主驱动配置只支持 Live2D 形象")
    root = Path(getattr(avatar, "root", "."))
    model_path = root / str(getattr(avatar, "entry", ""))
    audit_root = Path(project_root) if project_root is not None else Path.cwd()
    await _emit(progress, {"type": "status", "stage": "auditing"})
    try:
        dossier = await asyncio.to_thread(audit_runner, audit_root, model_path)
    except Exception as exc:
        raise GenerationError(f"模型审计失败：{type(exc).__name__}: {exc}") from exc
    baseline = _baseline(dossier)

    own_client = llm is None
    if own_client:
        try:
            llm = LLMClient(cfg)
        except (KeyError, TypeError, ValueError) as exc:
            reason = f"LLM 配置不完整：{type(exc).__name__}: {exc}"
            await _emit(progress, {"type": "error", "message": reason})
            return {"profile": _fallback(dossier, reason), "fallback": True,
                    "issues": [], "dossier": _summary(dossier), "error": None}
    try:
        if not bool(getattr(llm, "ready", False)):
            reason = "LLM 不可用，使用默认配置"
            await _emit(progress, {"type": "error", "message": reason})
            return {"profile": _fallback(dossier, reason), "fallback": True,
                    "issues": [], "dossier": _summary(dossier), "error": None}
        provider = str(getattr(llm, "source", ""))
        model_name = str(getattr(llm, "model", ""))
        draft = await _ask(
            llm, prompts.stage1_messages(dossier, preferences, baseline),
            progress=progress)
        if draft is None:
            reason = "LLM 输出不是 JSON，使用默认配置"
            await _emit(progress, {"type": "error", "message": reason})
            return {"profile": _fallback(dossier, reason), "fallback": True,
                    "issues": ["LLM 输出不是 JSON"], "dossier": _summary(dossier),
                    "error": None}
        draft["generated_by"] = _generated(provider, model_name,
                                           fallback_used=False)
        issues: list[validate.Issue] = []
        clean: dict = {}
        for _attempt in range(REPAIRS + 1):
            clean, issues = validate.validate(draft, dossier)
            fatal = [x for x in issues if x.fatal]
            if not fatal:
                # 可选字段被丢弃/clamp 时仍交付可用 profile；issues 只作提示。
                # baseline 合并只补程序事实，不覆盖 LLM 的有效调参/配方。
                merged = fallback.merge_baseline(baseline, clean, dossier)
                return {"profile": merged, "fallback": False,
                        "issues": [x.text() for x in issues],
                        "dossier": _summary(dossier), "error": None}
            if _attempt >= REPAIRS:
                break
            repaired = await _ask(
                llm, prompts.repair_messages(
                    dossier, draft, [x.text() for x in fatal], baseline),
                wait=GENERATION_REPAIR_WAIT_S, progress=progress)
            if repaired is None:
                break
            repaired["generated_by"] = _generated(
                provider, model_name, fallback_used=False)
            draft = repaired
        reason = "LLM 输出校验未通过，使用默认配置"
        await _emit(progress, {"type": "error", "message": reason})
        return {"profile": _fallback(dossier, reason,
                                     [x.text() for x in issues]),
                "fallback": True, "issues": [x.text() for x in issues],
                "dossier": _summary(dossier), "error": None}
    except (LLMError, OSError, ValueError, TypeError, KeyError) as exc:
        reason = f"LLM 生成失败：{type(exc).__name__}: {exc}"
        log(f"[autonomy] {reason}")
        await _emit(progress, {"type": "error", "message": reason})
        return {"profile": _fallback(dossier, reason), "fallback": True,
                "issues": [reason], "dossier": _summary(dossier), "error": None}
    finally:
        if own_client:
            close = getattr(llm, "close", None)
            if close is not None:
                try:
                    await close()
                except (RuntimeError, OSError):
                    pass


async def _cli(args) -> int:
    from sidekick import config

    root = Path(args.root).resolve() if args.root else Path.cwd()
    avatar = find_avatar(Path(args.model).resolve(), root)
    cfg = config.load()
    if args.no_llm:
        dossier = await asyncio.to_thread(
            run_audit, root, Path(args.model).resolve())
        profile = _fallback(dossier, "命令行指定 --no-llm")
        result = {"profile": profile, "fallback": True, "issues": [],
                  "dossier": _summary(dossier), "error": None}
    else:
        llm = None
        try:
            llm = LLMClient(cfg)
            result = await generate_profile(
                avatar, cfg, llm=llm, preferences=args.preferences,
                project_root=root)
        finally:
            if llm is not None:
                await llm.close()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0 if result.get("profile") else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--root", default="")
    parser.add_argument("--preferences", default="")
    parser.add_argument("--no-llm", action="store_true",
                        help="只生成确定性 fallback")
    parser.add_argument("--out", default="")
    return asyncio.run(_cli(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
