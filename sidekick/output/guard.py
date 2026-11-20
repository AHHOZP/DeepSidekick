"""出口过滤。

两条路分开：
  * sanitize() —— 把"念出来会怪"的东西清掉（符号、动作描写、markdown），再硬截断。
                  但**保留 MiMo 的发声标签**（"（小声）"这种），那是表演指令。
  * check()    —— 命中硬事实就**整句拒发**（不是替换成别的字）。

替换会造出半成品，拒发至少是安静的。这是和 QQ 那边一致的原则。
"""
from __future__ import annotations

import re

from sidekick.core.log import log
from sidekick.core.text import clean_tags, squeeze_ws

# 命中就拒发。宁可少说一句，也别把这几样念出去。
HARD_PATTERNS: list[tuple[str, str]] = [
    (r"/srv/[\w./-]*", "路径"),
    (r"/home/[\w./-]*", "路径"),
    (r"/etc/[\w./-]*", "路径"),
    (r"/root/[\w./-]*", "路径"),
    (r"[A-Za-z]:[\\/][\w\\.-]*", "Windows 路径"),
    (r"(?:\\\\|//)[^\s/\\]+[\\/][^\s]+", "UNC 路径"),
    (r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "IP"),
    (r"\b(?:sk|oc_sk|abk|ark)-[A-Za-z0-9_-]{8,}\b", "密钥"),
    (r"system\s*prompt", "系统提示词"),
    (r"系统提示", "系统提示词"),
    (r"提示词", "系统提示词"),
    (r"AGENTS\.md", "内部文件"),
    (r"api[ _-]?key", "密钥"),
    (r"(?:API|访问|加密|配置)密钥", "密钥"),
    (r"working\s*directory", "工作目录"),
    (r"工作目录", "工作目录"),
    (r"\bdsh\b", "内部工具"),
    (r"\bdeepsidekick\b", "内部工具"),
    (r"systemd|systemctl", "内部工具"),
    (r"astrbot", "内部工具"),
    (r"ssh\b", "内部工具"),
    (r"(?:部署在|运行在|后台|远程)服务器", "内部信息"),
    (r"群号|QQ群|\bQQ\b", "站外联系方式"),
    # 直播特有
    (r"https?://", "网址"),
    (r"www\.", "网址"),
    (r"[\w-]+\.(?:com|cn|net|org|io|tv|xyz|top)\b", "域名"),
    (r"我是(?:一个|个|只是|就是个|只是个)?\s*(?:AI|人工智能|语言模型|程序|机器人)", "自曝身份"),
    (r"(?:作为|身为)(?:一个|个)?\s*(?:AI|人工智能|语言模型|助手)", "自曝身份"),
]

_HARD = [(re.compile(p, re.I), why) for p, why in HARD_PATTERNS]
_WARNED_PATTERNS: set[str] = set()

# markdown 与"念不出来"的符号
_NOISE = re.compile(r"[*_`#>|\\^<>=+@$&]")
_QUOTES = re.compile(r"[「」『』“”\"'‘’]")
_ASCII_TILDE = re.compile(r"(?<=[\u4e00-\u9fff])~")
_DASH = re.compile(r"——+|—+")
_REPEAT_PUNCT = re.compile(r"([，。！？、])\1+")
_EMOJI = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F000-\U0001F2FF" "\uFE0F" "]+"
)

# 断句点：截断优先落在这些后面
_BREAKS = "。！？～…，、"


def sanitize(text: str, max_chars: int = 60,
             allowed_tags: set[str] | None = None,
             keep_square: set[str] | None = None) -> str:
    """把模型输出变成"能念出来的一句"。

    注意顺序：**先 clean_tags 再洗符号**。clean_tags 只放行 provider 认的发声标签，
    其余括号内容（"（转身坐下）"这种）整段删掉；留下来的标签要一路活到 TTS。
    """
    t = str(text or "").strip()
    if not t:
        return ""
    t = t.replace("\r", " ").replace("\n", " ")
    t = clean_tags(t, allowed_tags, keep_square)  # ← 发声标签保留，动作描写删掉
    t = _DASH.sub("。", t)
    t = _EMOJI.sub("", t)
    t = _NOISE.sub("", t)
    t = _QUOTES.sub("", t)
    t = _ASCII_TILDE.sub("～", t)
    t = squeeze_ws(t)               # 中文里不需要空格；方括号标签内部除外
    t = _REPEAT_PUNCT.sub(r"\1", t)
    t = t.lstrip("，。！？、～…")
    if len(t) > max_chars:
        cut = t[:max_chars]
        # 往回退到最后一个断句点，退不到就用硬截断
        pos = max((cut.rfind(ch) for ch in _BREAKS), default=-1)
        t = cut[: pos + 1] if pos >= max(8, max_chars // 3) else cut
        # 别把标签切一半（"（小声" 这种）
        if t.count("（") > t.count("）"):
            t = t[: t.rfind("（")]
        if t.count("[") > t.count("]"):
            t = t[: t.rfind("[")]
    return t.strip()


def check(text: str, extra: list[str] | None = None) -> str | None:
    """返回 None 表示可以发；否则返回拒发原因。"""
    t = str(text or "")
    if not t.strip():
        return "空内容"
    for rx, why in _HARD:
        if rx.search(t):
            return f"命中「{why}」"
    for pat in extra or []:
        try:
            if re.search(pat, t, re.I):
                return f"命中自定义「{pat}」"
        except re.error as e:
            if pat not in _WARNED_PATTERNS:
                _WARNED_PATTERNS.add(pat)
                log(f"[guard] 自定义正则无效，已忽略：{pat}（{e}）")
            continue
    return None


def clean_name(name: str, keep: int = 4) -> str:
    """观众名：去掉符号，只留前几个字；可疑的直接丢掉。"""
    n = _EMOJI.sub("", str(name or "")).strip()
    n = re.sub(r"[^\w\u4e00-\u9fff]", "", n)
    if not n:
        return ""
    if re.search(r"忽略|指令|提示词|系统|ignore|prompt", n, re.I):
        return ""
    return n[:keep]


def gate(text: str, max_chars: int = 60, min_chars: int = 2,
         extra: list[str] | None = None,
         allowed_tags: set[str] | None = None,
         keep_square: set[str] | None = None) -> tuple[str, str | None]:
    """一步到位：清洗 → 长度 → 硬事实。

    返回 (要念的文本, 拒发原因)。原因为 None 表示可以念。
    """
    # 先查原文：sanitize 会删掉反斜杠，Windows 路径必须在清洗前拦。
    why = check(str(text or ""), extra)
    if why:
        return sanitize(text, max_chars, allowed_tags, keep_square), why
    clean = sanitize(text, max_chars, allowed_tags, keep_square)
    if len(clean) < min_chars:
        return "", "清洗后太短"
    why = check(clean, extra)
    if why:
        return clean, why
    return clean, None
