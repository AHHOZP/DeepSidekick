"""形象动作规则（actions.md）：读、写、校验、按形象能力生成。

规则是数据，不是代码：手写或一键生成，保存进角色文件夹；
生成只列资产标签和输出格式，**不规定什么时候用**（那是模型自己的判断），
也**不自动写文件**。本地标签只有 `<ACT>`。
"""
from __future__ import annotations

from pathlib import Path

from sidekick.avatar import act_tags
from sidekick.avatar import files as avatar_files

MAX_CHARS = avatar_files.RULES_MAX


class RulesError(ValueError):
    """可展示给 WebUI 的规则错误（缺标签、超长、不可写）。"""


def path(avatar) -> Path:
    return avatar_files.path(avatar)


def _capabilities(avatar) -> dict:
    caps = getattr(avatar, "capabilities", None)
    return caps if isinstance(caps, dict) else {}


def _action_tags(caps: dict) -> list[str]:
    return [str(x.get("tag")) for x in (caps.get("actions") or [])
            if isinstance(x, dict) and x.get("tag")]


def _expr_tags(caps: dict) -> list[str]:
    return [str(x.get("tag")) for x in (caps.get("expressions") or [])
            if isinstance(x, dict) and x.get("tag")]


def load(avatar) -> str:
    return avatar_files.read(avatar)


def exists(avatar) -> bool:
    return path(avatar).is_file()


def payload(avatar) -> dict:
    text = load(avatar)
    caps = _capabilities(avatar)
    return {
        "content": text,
        "exists": exists(avatar),
        "path": str(path(avatar)),
        "chars": len(text),
        "max_chars": MAX_CHARS,
        "error": avatar_files.last_error,
        "truncated": avatar_files.last_truncated,
        "capabilities": {
            "actions": _action_tags(caps),
            "expressions": _expr_tags(caps),
        },
        "unknown_tags": unknown_tags(text, caps),
    }


def save(avatar, text: str) -> dict:
    """原子写；空内容 = 删除文件。目录不可写时抛 RulesError。"""
    text = str(text or "").replace("\r\n", "\n").strip()
    if len(text) > MAX_CHARS:
        raise RulesError(f"规则最长 {MAX_CHARS} 字（现在 {len(text)} 字）")
    if not text:
        clear(avatar)
        return payload(avatar)
    try:
        avatar_files.write(avatar, text)
    except OSError as e:
        raise RulesError(f"规则写不进去：{e}") from e
    return payload(avatar)


def clear(avatar) -> None:
    try:
        avatar_files.clear(avatar)
    except OSError as e:
        raise RulesError(f"规则删不掉：{e}") from e


# ── 生成 ──────────────────────────────────────────────

def _known_assets(caps: dict) -> set[tuple[str, str]]:
    motions: set[str] = set()
    for item in caps.get("actions") or []:
        if isinstance(item, dict):
            for key in ("tag", "group"):
                if item.get(key):
                    motions.add(str(item[key]).casefold())
    expressions: set[str] = set()
    for item in caps.get("expressions") or []:
        if isinstance(item, dict):
            for key in ("tag", "name"):
                if item.get(key):
                    expressions.add(str(item[key]).casefold())
    return ({("motion", x) for x in motions}
            | {("expression", x) for x in expressions})


def unknown_tags(text: str, caps: dict) -> list[str]:
    known = _known_assets(caps)
    out: list[str] = []
    for kind, name in act_tags.raw_asset_names(text):
        if (kind, name.casefold()) not in known and name not in out:
            out.append(name)
    return out


def build_text(avatar) -> str:
    """按形象能力直接拼一份规则：只列资产词表和输出格式，不规定什么时候用。

    动作/表情是 capabilities 里的死数据，内容完全确定；交给模型写只会多一个
    失败面。格式统一走 `<ACT>motion=...` / `<ACT>expression=...`。
    """
    caps = _capabilities(avatar)
    actions = _action_tags(caps)
    exprs = _expr_tags(caps)
    if not actions and not exprs:
        raise RulesError("这个形象没有可用的动作/表情标签")
    used = []
    if actions:
        used.append(f"<ACT>motion={actions[0]}</ACT>")
    if exprs:
        used.append(f"<ACT>expression={exprs[0]}</ACT>")
    lines = ["模型自带动作：需要时在句尾加 " + "；表情用 ".join(used) + "。"]
    if actions:
        lines.append("可用动作：" + "、".join(actions))
    if exprs:
        lines.append("可用表情：" + "、".join(exprs))
    lines.append("一句最多一个动作加一个表情；标签不会被念出来，也不要写动作描写。")
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        raise RulesError(f"标签太多，生成的规则超过 {MAX_CHARS} 字")
    return text
