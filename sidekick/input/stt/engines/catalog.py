"""已知模型清单：给 WebUI 的"模型"表格和下载用。

一条记录 = 一个 sherpa-onnx 官网发布的 tar.bz2。下载 → 解压到
`model_dir/<id>` → 按 kind 需要的文件校验（见 registry.REQUIRED_FILES）。

    github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/<file>
    国内镜像：modelscope.cn/models/csukuangfj/asr-models/resolve/master/<file>

指标说明（CPU 数字跟机器强相关，只作横向参考）：
  · RTF / 峰值内存 / CER / WER 来自各模型论文与模型卡；
    AISHELL-1 是中文测试集 CER，LibriSpeech 是英文 clean/other WER。
  · "—" 表示官方没给，不是 0。
  · 下载大小用发布包的字节数（排序也按它），跟解压后占盘不是一回事。
"""
from __future__ import annotations

from dataclasses import dataclass

GITHUB = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
MIRROR = "https://modelscope.cn/models/csukuangfj/asr-models/resolve/master"


@dataclass(frozen=True, slots=True)
class Entry:
    id: str            # 与 config.yaml 的 stt.models.<id> 对应
    kind: str          # registry.build 里的 kind
    name: str          # 给人看的名字
    file: str          # 发布包文件名
    bytes: int         # 下载包字节数（排序用）
    sha256: str = ""   # 官方发布包 SHA256；留空表示暂未钉死
    langs: str = ""
    rtf: str = "—"          # CPU 实时率
    mem: str = "—"          # 峰值内存
    aishell1: str = "—"     # 中文 CER
    librispeech: str = "—"  # 英文 WER
    note: str = ""          # 只写特殊性：方言、热词、多语种之类

    def url(self, mirror: bool = False) -> str:
        return f"{MIRROR if mirror else GITHUB}/{self.file}"

    @property
    def size(self) -> str:
        return f"{self.bytes / 1e6:.0f} MB"


# 按下载包从小到到大。UI 也会再排一次，这里先排好是为了接口输出稳定。
# SHA256 来自同目录发布资产 checksum.txt：
# github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/checksum.txt
CATALOG: tuple[Entry, ...] = (
    Entry(
        id="sensevoice-small", kind="sense-voice", name="SenseVoice-Small",
        file="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
        bytes=163002883,
        sha256="7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e",
        langs="zh, en, ja, ko, yue",
        rtf="0.007 – 0.018", mem="600 MB",
        aishell1="3.0%", librispeech="2.1 / 4.8%",
        note="非自回归；阿里 FunAudioLLM；自带标点、情绪识别、声音事件",
    ),
    Entry(
        id="paraformer-zh", kind="paraformer", name="Paraformer-zh",
        file="sherpa-onnx-paraformer-zh-int8-2025-10-07.tar.bz2",
        bytes=228262632,
        sha256="a071ee5419e14adb34d7f970ab98105a45e6608018b168f023ca2e4810744abe",
        langs="zh, en",
        rtf="0.015 – 0.035", mem="600 – 800 MB",
        aishell1="3.12%", librispeech="4.8 / 10.5%",
        note="非自回归（CIF）；阿里达摩院；工业级中文识别，带时间戳",
    ),
    Entry(
        id="firered-ctc", kind="fire-red-asr-ctc", name="FireRedASR2-CTC",
        file="sherpa-onnx-fire-red-asr2-ctc-zh_en-int8-2026-02-25.tar.bz2",
        bytes=520516278,
        sha256="1da8b737ecc5e29f36759a4460c754863e7c919a4ba325aea187331fbfc83274",
        langs="zh, en",
        rtf="0.06 – 0.17", mem="1 GB",
        aishell1="3.20%", librispeech="3.1 / 7.2%",
        note="CTC 架构；小红书；针对 20+ 方言与口音优化，抗噪出色",
    ),
    Entry(
        id="whisper-turbo", kind="whisper", name="Whisper-large-v3-turbo",
        file="sherpa-onnx-whisper-turbo.tar.bz2",
        bytes=563790207,
        sha256="b11acbbcd660b44a8e0df33724feb5aaa709cf65668f2823d59f656312544f22",
        langs="99 语种",
        rtf="0.15 – 0.28", mem="1.8 – 2.5 GB",
        aishell1="5.6%", librispeech="1.82 / 4.15%",
        note="自回归精简版；OpenAI；Decoder 缩到 4 层，可替代原版 v3",
    ),
    Entry(
        id="whisper-small", kind="whisper", name="Whisper-small",
        file="sherpa-onnx-whisper-small.tar.bz2",
        bytes=639387718,
        sha256="486a46afbb7ba798507190ffe02fea2dd726049af212e774537efac6afb210a6",
        langs="99 语种",
        rtf="0.12 – 0.20", mem="800 MB",
        aishell1="8.7%", librispeech="3.5 / 7.6%",
        note="自回归（Encoder-Decoder）；OpenAI；多语种通用性强，中文稍弱",
    ),
    Entry(
        id="firered-aed", kind="fire-red-asr", name="FireRedASR2-AED",
        file="sherpa-onnx-fire-red-asr2-zh_en-int8-2026-02-26.tar.bz2",
        bytes=838589068,
        sha256="43015b3f1643a5688b4821e8ed323473d38b798c4ec291471fe00df1bcfc4f1c",
        langs="zh, en",
        rtf="0.35 – 0.65", mem="2 – 3 GB",
        aishell1="3.05%", librispeech="2.4 / 5.8%",
        note="自回归（AED）；小红书；长句精度高",
    ),
    Entry(
        id="qwen3-asr", kind="qwen3-asr", name="Qwen3-ASR-0.6B",
        file="sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2",
        bytes=878702423,
        sha256="393f8a14e2f5fb96746aaab342997a40641001fbd5bf9592a080a8329178ee96",
        langs="zh, en, yue",
        rtf="0.08 – 0.15", mem="1.5 – 2 GB",
        aishell1="3.42%", librispeech="2.11 / 4.55%",
        note="自回归（Audio-LLM）；阿里 Qwen；支持长上下文与热词（人名/术语）",
    ),
)

_BY_ID = {e.id: e for e in CATALOG}


def all_entries() -> tuple[Entry, ...]:
    return CATALOG


def get(mid: str) -> Entry | None:
    return _BY_ID.get(str(mid or ""))
