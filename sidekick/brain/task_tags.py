"""主回复里的隐藏任务标签解析。

    <TASK>{"title":"找钥匙","detail":"铁匠让你从北门进"}</TASK>
    <TASKCOMPLETE>{"title":"找钥匙","keywords":["铁匠","钥匙"]}</TASKCOMPLETE>

活跃任务只存标题 + 详情；完成任务归档为游戏记忆，才带关键词。
解析发生在写历史、动作提取、guard、TTS 之前；标签绝不进入历史/气泡/TTS。
"""
from __future__ import annotations

import json
import re
from typing import Any

from sidekick.core.text import collapse_spaces, split_list

_TAG = re.compile(r"<TASK>(.*?)(?:</TASK>|$)", re.I | re.S)
_COMPLETE = re.compile(r"<TASKCOMPLETE>(.*?)(?:</TASKCOMPLETE>|$)",
                       re.I | re.S)


def _kw(v) -> list[str]:
    return split_list(v, unique=False)


def _obj(body: str) -> dict | None:
    try:
        value = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _one(obj: Any, action: str) -> dict | None:
    if not isinstance(obj, dict):
        return None
    title = re.sub(r"\s+", " ", str(obj.get("title") or "")).strip()
    if not title:
        return None
    if action == "complete":
        return {"action": "complete", "title": title,
                "keywords": _kw(obj.get("keywords"))}
    detail = re.sub(r"\s+", " ", str(obj.get("detail") or "")).strip()
    return {"action": "add", "title": title, "detail": detail}


def extract(text: str) -> tuple[str, list[dict]]:
    """返回 (剥掉任务标签的文本, 任务列表)。"""
    raw = str(text or "")
    tasks: list[dict] = []

    def add(m: re.Match) -> str:
        obj = _obj(m.group(1).strip())
        task = _one(obj, "add")
        if task:
            tasks.append(task)
        return ""

    def complete(m: re.Match) -> str:
        obj = _obj(m.group(1).strip())
        task = _one(obj, "complete")
        if task:
            tasks.append(task)
        return ""

    raw = _TAG.sub(add, raw)
    raw = _COMPLETE.sub(complete, raw)
    return collapse_spaces(raw).strip(), tasks
