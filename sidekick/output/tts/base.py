"""TTS provider 契约 + 音频解码。

所有 provider 只做一件事：**文本 → float32 单声道 PCM**。
分句、排队、播放、打断、响度归一化都在 speaker.py，provider 不管这些。

厂商差异在 `sidekick/config/tts_presets.py` 的数据里；需要新协议形状时才继承
`Provider` 写一个 wire。
"""
from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import AsyncIterator

import numpy as np
import soxr


@dataclass(slots=True)
class SynthResult:
    pcm: np.ndarray      # float32 mono, -1..1
    sr: int              # 采样率


class Provider:
    """换 TTS 服务商就继承这个（只在新协议形状时需要）。"""

    #: profile 名（`tts.<profile>`），用于状态和报错。
    profile = ""
    #: preset id，用于 WebUI / 状态展示。
    preset = ""
    #: 能不能把 PCM 一块块吐出来（厂商原生流式）。
    supports_stream = False
    #: 流式 PCM 的采样率；不支持流式时为 0。
    stream_sr = 0
    #: 该 provider 的“发声标签”规则，注入主对话的输出约束；默认空。
    prompt_rules = ""
    #: 输出侧白名单：模型仍可能忽略 prompt，所以这里必须再过滤一次。
    allowed_tags: frozenset[str] = frozenset()
    #: 方括号标签原样保留，交给 provider.adapt_text()。
    preserve_square_tags: frozenset[str] = frozenset()

    @property
    def has_key(self) -> bool:
        """是否具备发声条件；不要 key 的 provider（edge）恒为 True。"""
        return True

    def adapt_text(self, text: str) -> str:
        """把出口标签改成厂商语法；默认原样返回。"""
        return text

    async def synth(self, text: str) -> SynthResult:
        raise NotImplementedError

    def stream(self, text: str) -> AsyncIterator[np.ndarray]:
        """真流式；不支持流式的 provider 不实现（`supports_stream=False`）。"""
        raise NotImplementedError

    async def close(self) -> None:
        return None


# ── 解码 ──────────────────────────────────────────────
def _to_f32(raw: np.ndarray) -> np.ndarray:
    if raw.dtype == np.int16:
        return raw.astype(np.float32) / 32768.0
    if raw.dtype == np.int32:
        return raw.astype(np.float32) / 2147483648.0
    return raw.astype(np.float32)


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """标准 PCM wav。MiMo 默认返回这个。"""
    with wave.open(io.BytesIO(data), "rb") as w:
        ch, sr, width = w.getnchannels(), w.getframerate(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width == 2:
        pcm = np.frombuffer(frames, dtype="<i2")
    elif width == 4:
        pcm = np.frombuffer(frames, dtype="<i4")
    elif width == 1:
        pcm = (np.frombuffer(frames, dtype=np.uint8).astype(np.int16) - 128) << 8
    else:
        raise ValueError(f"不支持的位深 {width * 8} bit")
    x = _to_f32(pcm)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def decode_mp3(data: bytes) -> tuple[np.ndarray, int]:
    """mp3 走 miniaudio（纯轮子，不需要 ffmpeg）。FishAudio / edge-tts 都是 mp3。"""
    import miniaudio

    dec = miniaudio.decode(
        data,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=24000,
        dither=miniaudio.DitherMode.NONE,
    )
    pcm = np.frombuffer(dec.samples.tobytes(), dtype=np.int16)
    return _to_f32(pcm), int(dec.sample_rate)


def from_pcm16(data: bytes, sr: int, channels: int = 1) -> tuple[np.ndarray, int]:
    """裸 PCM16LE（MiMo stream 模式返回这个）。"""
    x = _to_f32(np.frombuffer(data, dtype="<i2"))
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    return x, sr


def decode_any(data: bytes, hint: str) -> tuple[np.ndarray, int]:
    hint = (hint or "").lower()
    if hint in ("wav", "wave"):
        return decode_wav(data)
    if hint in ("mp3", "mpeg"):
        return decode_mp3(data)
    # 猜一下：RIFF 开头是 wav，否则当 mp3
    if data[:4] == b"RIFF":
        return decode_wav(data)
    return decode_mp3(data)


def resample(pcm: np.ndarray, src: int, dst: int) -> np.ndarray:
    """采样率转换。用 soxr（带抗混叠滤波），线性插值下采样会混叠。"""
    x = np.asarray(pcm, dtype=np.float32)
    if src == dst or x.size == 0:
        return x
    return soxr.resample(x, int(src), int(dst)).astype(np.float32)
