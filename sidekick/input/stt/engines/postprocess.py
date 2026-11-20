"""识别结果的纯文本后处理。

这一步的目的只有一个：**别把幻觉送进大脑**。

CTC 类模型（SenseVoice / FireRed-CTC / Paraformer）在噪声段和静音段的
典型幻觉长这样：

    啊啊啊啊啊啊啊
    的的的的的的
    。。。。。。。。
    （音乐）（音乐）（音乐）

它们不是"识别错了"，而是"这段没人说话，但解码器总得吐点什么"。
所以判据不是看文本像不像话，而是看**密度**：
  · 单位时长字符数（人话不可能每秒 12 个字以上）
  · 自重复（同一片段连出 4 次以上）
  · 字符多样性（8 个字里不同的字不到 12%）
"""
from __future__ import annotations

import re

from sidekick.input.stt.engines.base import AsrResult

_WS = re.compile(r"[ \t\u3000]+")
_REPEAT = re.compile(r"(.{2,8}?)\1{3,}")     # 同一片段连续 4 次以上
_SAME = re.compile(r"(.)\1{3,}")             # 同一个字连出 4 次以上（啊啊啊啊）
_PUNCT = re.compile(r"[\s\.,!?;:，。！？；：、~～…·]+")


def _is_repeat(text: str) -> bool:
    """整段基本就是一个片段在打转，才算复读。

    不能一见到"哈哈哈哈哈"就丢 —— 那是真人在笑。
    "哈哈哈哈哈哈哈哈"该丢，"哈哈哈哈笑死我了"该留，
    差别在复读占整句的比例，不在有没有复读。
    """
    body = _PUNCT.sub("", text)
    if not body:                 # 整段都是标点，"。。。。。。"
        return True
    if len(body) < 4:
        return False
    need = max(4, -(-len(body) * 6 // 10))      # 向上取整的 60%
    for rx in (_SAME, _REPEAT):
        m = rx.search(body)
        if m and (m.end() - m.start()) >= need:
            return True
    return False


def _unit_count(text: str) -> int:
    """CJK 按字、ASCII 连续字母数字按词；英文正常语速不再按字母数误杀。"""
    return len(re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9]+",
                          _PUNCT.sub(" ", text)))


def clean(result: AsrResult, cfg: dict) -> str:
    """返回干净的文本；判定为幻觉则返回空串。"""
    # 默认值只在 spec.py 维护；调用方传的是 config.load() 合并后的完整配置。
    st = cfg["stt"]
    min_chars = int(st["min_chars"])
    max_cps = float(st["max_chars_per_sec"])

    text = _WS.sub(" ", (result.text or "")).strip()
    # 字数按"真字"算，标点不算：否则静音段吐个"没。"就能混过 min_chars=2
    if _unit_count(text) < min_chars:
        return ""

    # 语速上限：每秒 12 个汉字已经是极速说唱，超过必然是幻觉
    if (result.duration > 0.3
            and _unit_count(text) / result.duration > max_cps):
        return ""
    # 复读：CTC 在噪声段容易吐"啊啊啊啊啊""的的的的的""的的是的的是"
    if _is_repeat(text):
        return ""
    # 字符多样性：低于 12% 直接丢（"。。。。。。"）
    if len(text) >= 8 and len(set(text)) / len(text) < 0.12:
        return ""
    return text
