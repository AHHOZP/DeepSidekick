"""音频输出：sounddevice 播放线程/一次性播放。

`speech.py` 只管合成、分句、打断与进度；真正碰声卡的代码都在这里，
这样自检（`mute=True`）与测试不需要 import sounddevice。
"""
from __future__ import annotations

import queue
import threading

import numpy as np

from sidekick.output.tts.base import resample


def output_format() -> tuple[int, int]:
    """系统默认输出设备的 (采样率, 声道数)；查不到时按 48k/双声道。"""
    try:
        import sounddevice as sd

        info = sd.query_devices(kind="output")
        sr = int(info.get("default_samplerate") or 48000)
        nch = 2 if int(info.get("max_output_channels") or 2) >= 2 else 1
        return sr, nch
    except Exception:
        return 48000, 2


def _prepare(pcm: np.ndarray, sr: int, dst_sr: int, nch: int,
             gain: float) -> np.ndarray:
    # 先重采样再削波：反过来会把重采样的过冲削平，产生额外谐波。
    data = np.asarray(pcm, dtype=np.float32)
    if dst_sr != sr:
        data = resample(data, sr, dst_sr)
    data = np.clip(data * float(gain), -1.0, 1.0)
    if nch > 1:
        data = np.repeat(data[:, None], nch, axis=1)
    return data


def play_blocking(pcm: np.ndarray, sr: int, *, gain: float = 1.0,
                  stop_event: threading.Event | None = None,
                  on_first=None, on_level=None) -> float:
    """播一段 PCM。返回实际写进声卡的秒数。

    始终用 Windows 默认输出设备；`on_first` / `on_level` 在播放线程里调用。
    """
    src_sr = int(sr or 24000)
    duration = len(np.asarray(pcm)) / float(src_sr or 1)
    if duration <= 0:
        return 0.0
    dst_sr, nch = output_format()
    data = _prepare(pcm, src_sr, dst_sr, nch, gain)
    chunk = max(256, int(dst_sr * 0.02))
    sent_s = 0.0
    first = True
    import sounddevice as sd

    with sd.OutputStream(samplerate=dst_sr, channels=nch, dtype="float32") as stream:
        for i in range(0, len(data), chunk):
            if stop_event is not None and stop_event.is_set():
                break
            piece = data[i:i + chunk]
            if on_level is not None:
                try:
                    on_level(piece, dst_sr)
                except Exception:
                    pass
            stream.write(piece.copy())
            if first:
                first = False
                if on_first is not None:
                    try:
                        on_first()
                    except Exception:
                        pass
            sent_s += len(piece) / float(dst_sr)
    return sent_s


class StreamPlayer:
    """流式播放：一个线程，从队列取块写默认输出设备；记录真正播出去的秒数。"""

    def __init__(self, *, mute: bool = False, src_sr: int = 24000) -> None:
        self.mute = mute
        self.src_sr = int(src_sr or 24000)
        self.played_s = 0.0
        self.error: str | None = None
        self.started = False
        self.dst_sr = self.src_sr
        self.nch = 1

    def configure(self) -> None:
        if self.mute:
            self.dst_sr, self.nch = self.src_sr, 1
            return
        self.dst_sr, self.nch = output_format()

    def run(self, q: "queue.Queue", sentinel, stop_event: threading.Event,
            *, on_first=None, on_level=None) -> None:
        if self.mute:
            while True:
                try:
                    item = q.get(timeout=0.2)
                except queue.Empty:
                    if stop_event.is_set():
                        break
                    continue
                if item is sentinel:
                    break
                if not self.started:
                    self.started = True
                    if on_first is not None:
                        on_first()
                self.played_s += len(item) / float(self.dst_sr or 1)
            return

        import sounddevice as sd

        step = max(256, int(self.dst_sr * 0.02))
        try:
            with sd.OutputStream(samplerate=self.dst_sr, channels=self.nch,
                                 dtype="float32") as st:
                while True:
                    try:
                        item = q.get(timeout=0.2)
                    except queue.Empty:
                        if stop_event.is_set():
                            break
                        continue
                    if item is sentinel:
                        break
                    if stop_event.is_set():
                        continue
                    for i in range(0, len(item), step):
                        piece = item[i:i + step]
                        if on_level is not None:
                            on_level(piece, self.dst_sr)
                        st.write(piece)
                        if not self.started:
                            self.started = True
                            if on_first is not None:
                                on_first()
                        self.played_s += len(piece) / float(self.dst_sr or 1)
        except Exception as e:
            self.error = f"播放失败：{e}"
