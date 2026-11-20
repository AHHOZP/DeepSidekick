"""识别引擎的公共类型。

这里只有"数据的形状"，**不 import 任何具体引擎** ——
这样 sherpa / whisper / 将来的 remote 之间互不依赖。

线程模型（重要）：引擎**不要求线程安全**，但必须被单线程串行调用。
`stt_worker.py` 的串行识别线程满足这一点；WebUI 里要并发试听就自己建实例。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AsrResult:
    """所有引擎的统一返回。只有 text 是必填，其余按引擎能力给。

    SenseVoice 会给 lang / emotion，CTC 类一般只给 text。
    多出来的字段 v1 只进日志，不进 prompt。
    """

    text: str = ""
    lang: str = ""                       # zh / en / yue ...
    emotion: str = ""                    # HAPPY / SAD / ANGRY / NEUTRAL ...
    duration: float = 0.0                # 输入音频时长（秒），后处理要用
