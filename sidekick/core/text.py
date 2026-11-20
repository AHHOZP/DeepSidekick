"""文本处理：切句 + 情感标签白名单。

切句是给 TTS 用的：一段话拆成几句，**每句合成完立刻播**，不整段等。
所以切得越准，第一次出声越快。

标签白名单是因为模型会自己往正文里插 MiMo 的括号标签（"（小声）诶～？"）。
那些标签是**表演指令**，要原样留给 TTS；但模型也可能写出"（转身坐下）"这种
不能发声的动作描写，那就得剔掉——MiMo 明确只认能发出声音的内容。
"""
from __future__ import annotations

import json
import re
import unicodedata

# ── 关键词 ────────────────────────────────────────────
# 这些词太泛，做召回键只会把无关条目全捞出来。
GENERIC_KEYS = {
    "游戏", "任务", "用户", "直播", "剧情", "记忆", "故事", "这个", "那个",
    "今天", "刚才", "现在", "然后", "还是", "就是", "可以", "什么", "怎么",
    "我们", "你们", "他们", "自己", "东西", "地方", "时候", "已经", "因为",
    "所以", "但是", "如果", "没有", "一个", "一下", "感觉", "觉得",
    "聊天", "开心", "好玩", "厉害",
    "对话", "观众", "内容", "事情", "情绪", "时间", "画面", "声音",
    "问题", "话题", "朋友", "角色", "过程", "反应",
}


def json_list(value) -> list:
    """把 JSON 字符串/列表统一成 list；坏数据返回空列表。"""
    if isinstance(value, list):
        return value
    try:
        data = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def key_ok(k: str, min_len: int) -> bool:
    """关键词是否值得做召回键：非泛词，且中文 ≥ min_len、西文 ≥ min_len+1。"""
    k = str(k or "").strip()
    if not k or k in GENERIC_KEYS:
        return False
    cjk = sum(1 for ch in k if "\u4e00" <= ch <= "\u9fff")
    if cjk:
        return len(k) >= max(2, int(min_len))
    return len(k) >= max(3, int(min_len) + 1)


# ── 切句 ──────────────────────────────────────────────
_HARD_BREAK = re.compile(r"(?<=[。！？…～；])")
_CLAUSE = re.compile(r"(?<=[，,、])")


def _is_cjk(ch: str) -> bool:
    return bool(ch) and ("\u3400" <= ch <= "\u9fff"
                         or "\u3040" <= ch <= "\u30ff"
                         or "\uac00" <= ch <= "\ud7af")


_CJK_PUNCT = set("，。！？；：、（）【】《》「」『』…～")


def squeeze_ws(text: str) -> str:
    """去掉中文之间/标点旁的空白；英文单词之间保留一个空格。

    Fish 的情感标签是 `[in a hurry tone]` 这种自然语言，带空格；
    方括号内部一个字都不动。中文正文里的空白照旧删掉，英文回复
    （以及中英混排）不能把 `I think` 拼成 `Ithink`。
    """
    source = str(text or "")
    out: list[str] = []
    depth = 0
    i = 0
    while i < len(source):
        ch = source[i]
        if ch == "[":
            depth += 1
        elif ch == "]" and depth:
            depth -= 1
        if ch.isspace() and depth == 0:
            j = i + 1
            while j < len(source) and source[j].isspace():
                j += 1
            prev = out[-1] if out else ""
            nxt = source[j] if j < len(source) else ""
            if (prev and nxt and (_is_cjk(prev) or _is_cjk(nxt)
                                  or prev in _CJK_PUNCT or nxt in _CJK_PUNCT)):
                i = j
                continue
            if out and out[-1] != " ":
                out.append(" ")
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out).strip()


def collapse_spaces(text: str) -> str:
    """连续空格/制表符压成一个空格；换行不动。"""
    return re.sub(r"[ \t]{2,}", " ", str(text or ""))


def split_list(value, *, limit: int = 0, unique: bool = True,
               squeeze: bool = True, strip_quotes: bool = True,
               max_len: int = 0) -> list[str]:
    """把逗号/顿号/分号/竖线分隔的字符串或列表统一成词表。

    memory 的关键词、worldbook 的 keys、WebUI 的输入框都走这里；
    以前三处各有一份 `_list / _as_list / _kw_list`。
    """
    if isinstance(value, str):
        parts = re.split(r"[,，、;；|/]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = [str(x) for x in value]
    else:
        return []
    out: list[str] = []
    for raw in parts:
        p = re.sub(r"\s+", " ", str(raw)) if squeeze else str(raw)
        p = p.strip()
        if strip_quotes:
            p = p.strip("「」\"'")
        if not p:
            continue
        if max_len > 0:
            p = p[:max_len]
        if unique and p in out:
            continue
        out.append(p)
        if limit > 0 and len(out) >= limit:
            break
    return out


def normalize_key(text: str) -> str:
    """关键词匹配用的规范形：全半角/大小写/标点都不敏感。"""
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    return re.sub(r"[\s，。！？、,.!?;；:：\"'“”‘’（）()\[\]【】<>《》|/\\]+", "", s)


def split_sentences(text: str, max_chars: int = 45, min_chars: int = 4) -> list[str]:
    """按句末标点切，太短的并进下一句，太长的在逗号处再切一刀。"""
    t = squeeze_ws(str(text or "")).strip()
    if not t:
        return []

    parts = [p for p in _HARD_BREAK.split(t) if p]
    if len(parts) <= 1:
        parts = [t]

    # 太长的句子按逗号再切
    out: list[str] = []
    for p in parts:
        if len(p) <= max_chars:
            out.append(p)
            continue
        buf = ""
        for piece in _CLAUSE.split(p):
            if not piece:
                continue
            if buf and len(buf) + len(piece) > max_chars:
                out.append(buf)
                buf = piece
            else:
                buf += piece
        if buf:
            out.append(buf)

    # 太短的并进相邻句
    merged: list[str] = []
    for p in out:
        if merged and len(merged[-1]) < min_chars:
            merged[-1] += p
        else:
            merged.append(p)
    if len(merged) >= 2 and len(merged[-1]) < min_chars:
        merged[-2] += merged.pop()

    return [m for m in merged if m.strip()]


# ── 情感标签 ──────────────────────────────────────────
# 词表由各 TTS provider 自己声明（见 sidekick/output/tts/*.py）；
# 这里不再维护一份通用默认词表。

# 括号里的内容（中英文、全半角、方圆括号都算）。用扫描器而不是限长正则：
# 模型写出 25 字动作描写或漏掉右括号时，也必须整段丢掉。
_BRACKETS = {"（": "）", "(": ")", "【": "】", "[": "]"}


def clean_tags(text: str, allowed: set[str] | None = None,
               keep_square: set[str] | None = None) -> str:
    """留下白名单里的发声标签，剔掉动作描写和不认识的。

    `allowed` 让 provider 决定自己的词表：Fish 的中文词表比 MiMo 大，
    `（紧张）` 这种不能因为不在 MiMo 白名单里就被提前删掉。

    `keep_square` 里的标签原样保留（包括空格和方括号），交给 provider
    自己的 `adapt_text()` 处理；Fish 的 `[whispering]` / `[in a hurry tone]`
    走这条路。
    """
    allowed_set = {str(x) for x in (allowed or ())}
    keep = {str(x).strip().lower() for x in (keep_square or ())}
    source = str(text or "")
    out: list[str] = []
    i = 0
    while i < len(source):
        opener = source[i]
        closer = _BRACKETS.get(opener)
        if closer is None:
            out.append(opener)
            i += 1
            continue
        end = source.find(closer, i + 1)
        if end < 0:
            # 未闭合：从开括号到结尾全部丢掉，避免半截动作描写被念出。
            break
        inner = source[i + 1:end].strip()
        # 方括号 + provider 认识的标签：原样保留，别把自然语言压成词表
        if opener in "[【" and inner.lower() in keep:
            out.append(source[i:end + 1])
        else:
            # 逗号分隔的多标签，逐个判
            words = [w.strip() for w in re.split(r"[，,、\s]+", inner)
                     if w.strip()]
            kept = [w for w in words
                    if w.lower() in allowed_set or w in allowed_set]
            if kept:
                # 保留原来的开闭括号：MiniMax 只认 ASCII `(laughs)`，
                # 统一改成全角会被厂商当成正文念出来。
                out.append(f"{opener}{', '.join(kept)}{closer}")
        i = end + 1
    return "".join(out)


def strip_fence(text: str) -> str:
    """去掉模型包在 JSON 外面的 Markdown 代码围栏。"""
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def strip_tags(text: str) -> str:
    """纯文本版：所有括号内容一律去掉。用于日志/记录，不用于合成。"""
    source = str(text or "")
    out: list[str] = []
    i = 0
    while i < len(source):
        opener = source[i]
        closer = _BRACKETS.get(opener)
        if closer is None:
            out.append(opener)
            i += 1
            continue
        end = source.find(closer, i + 1)
        if end < 0:
            break
        i = end + 1
    return "".join(out).strip()


def merge_systems(messages: list[dict]) -> list[dict]:
    """把多条 system 合并到最前面，给只接受一条 system 的网关用。"""
    systems = [str(m.get("content") or "") for m in messages
               if m.get("role") == "system"
               and str(m.get("content") or "").strip()]
    rest = [m for m in messages if m.get("role") != "system"]
    if not systems:
        return rest
    return [{"role": "system", "content": "\n\n".join(systems)}] + rest
