"""edge-tts（微软，免费，不要 key）。

没有情感控制，只用来兜底；返回 mp3，整段解码、不支持流式。
"""
from __future__ import annotations

import edge_tts

from sidekick.config import tts_presets
from sidekick.output.tts.base import Provider, SynthResult, decode_mp3


class EdgeProvider(Provider):
    preset = "edge"
    supports_stream = False

    def __init__(self, *, profile: str, block: dict) -> None:
        self.profile = profile
        block = {**tts_presets.default_block("edge"), **dict(block)}
        self.voice = str(block.get("voice") or "")
        self.rate = str(block.get("rate") or "")

    async def synth(self, text: str) -> SynthResult:
        com = edge_tts.Communicate(text, self.voice, rate=self.rate)
        buf = bytearray()
        async for chunk in com.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                buf.extend(chunk["data"])
        if not buf:
            raise RuntimeError("edge-tts 没返回音频")
        pcm, sr = decode_mp3(bytes(buf))
        return SynthResult(pcm=pcm, sr=sr)
