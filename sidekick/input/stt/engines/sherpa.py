"""sherpa-onnx 引擎：一份代码覆盖十几个模型族，推理固定 CPU。

官方 pip wheel 只有 CPU 版，所以 `provider` 写死在 `registry.build` 里，没有设备开关。

几个容易踩的点：

1. **不要自己重采样**。`stream.accept_waveform(sr, x)` 内部会建一个
   resampler（日志里能看到 `Creating a resampler: 48000 -> 16000`），
   而且是无混叠的。Python 侧再 `np.interp` 一遍既浪费又更差。
   这行日志每段会打一次，是 C++ 侧打的，属正常输出，不是报错。
2. **warmup 要跑一次真解码**。只 load 不 decode 的话，ORT 的内存池
   还没分配，第一段真语音会吃 2~3 倍延迟。
3. **transcribe 必须吞异常**。模型崩了不能把识别线程带走。失败的处理分三级：
   连续 3 次失败 → 冷却 30 秒；冷却结束自动复活一次，再失败再冷却……
   复活 3 次都失败 → **永久停用**（只有重启 runtime 才会重新加载），
   runtime 会为此打一条醒目的日志、WebUI 顶部也会挂红条。
"""
from __future__ import annotations

import re
import threading
import time

import numpy as np

from sidekick.input.stt.engines import registry
from sidekick.input.stt.engines.base import AsrResult

# SenseVoice 会在文本前后塞控制标签：<|zh|><|NEUTRAL|><|Speech|><|withitn|>
_TAG = re.compile(r"<\|[^|]*\|>")
# 有些版本用尖括号不带竖线（<zh> / </s>），一并清掉。只匹配纯字母，
# 不会吃掉正文里的尖括号（中文里基本不出现）。
_TAG2 = re.compile(r"</?[a-zA-Z_]+\|?>")

_MIN_AUDIO_S = 0.15     # 短于这个不送引擎，纯属浪费
_MAX_FAILS = 3          # 连续失败几次进冷却
_RETRY_AFTER_S = 30.0   # 冷却多久后试一次复活
_MAX_REVIVALS = 3       # 复活几次还失败就永久停用（重启 runtime 才能恢复）


class SherpaEngine:
    def __init__(self, spec: registry.ModelSpec, setup: dict) -> None:
        self.spec = spec
        self.setup = dict(setup or {})
        self.name = f"sherpa/{spec.kind}"
        self.model_size = spec.id
        self.error: str | None = None
        self._rec = None
        self._lock = threading.Lock()
        self._fails = 0          # 连续失败次数
        self._dead_at = 0.0      # 进入冷却的时刻
        self._revivals = 0       # 已经用掉的复活机会
        self._disabled = False   # 彻底停用（不再尝试）
        self._dropped_dead = 0   # 冷却/停用期被丢掉的语音段

    # ── 生命周期 ──────────────────────────────────────
    def warmup(self) -> bool:
        """建 recognizer + 空跑一次完整解码，让 ORT 把内存池分配好。"""
        try:
            rec = self._ensure()
            x = np.zeros(16000, dtype=np.float32)
            s = rec.create_stream()
            s.accept_waveform(16000, x)
            rec.decode_stream(s)
            self._reset_failures()
            self.error = None
            return True
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            return False

    def _ensure(self):
        if self._rec is None:
            self._rec = registry.build(self.spec, self.setup)
        return self._rec

    def _reset_failures(self) -> None:
        # 只由“成功解码”/warmup/close 调用；重新 load 成功不等于解码成功，
        # 不能在这里清 _fails，否则解码持续失败会每句话都重新加载模型。
        self._fails = 0
        self._revivals = 0
        self._dead_at = 0.0
        self._disabled = False
        self.error = None

    def close(self) -> None:
        with self._lock:
            self._rec = None
            self._reset_failures()

    @property
    def dead(self) -> bool:
        """true = 正在冷却（这段时间的段会被丢掉，不反复尝试 load）。"""
        return bool(self._dead_at) and (
            time.monotonic() - self._dead_at < _RETRY_AFTER_S)

    @property
    def disabled(self) -> bool:
        """true = 已经彻底停用，只有重启 runtime 才会再试。"""
        return self._disabled

    @property
    def revivals(self) -> int:
        return self._revivals

    @property
    def dropped_dead(self) -> int:
        return self._dropped_dead

    # ── 识别 ──────────────────────────────────────────
    def transcribe(self, pcm, sr: int) -> AsrResult:
        x = np.asarray(pcm, dtype=np.float32)
        if x.ndim > 1:
            x = x.mean(axis=1)
        sr = int(sr or 16000)
        dur = len(x) / float(sr)
        if dur < _MIN_AUDIO_S:
            return AsrResult(text="", duration=dur)

        if self._disabled:
            self._dropped_dead += 1
            return AsrResult(text="", duration=dur)

        if self._dead_at:
            if time.monotonic() - self._dead_at < _RETRY_AFTER_S:
                self._dropped_dead += 1
                return AsrResult(text="", duration=dur)
            # 冷却结束：还有复活机会就再试一次
            self._dead_at = 0.0
            if self._revivals >= _MAX_REVIVALS:
                self._disabled = True          # 兜底：名额已经用光
                return AsrResult(text="", duration=dur)
            self._revivals += 1

        try:
            rec = self._ensure()
            with self._lock:
                s = rec.create_stream()
                s.accept_waveform(sr, x)       # 内部重采样，别再自己 resample
                rec.decode_stream(s)
                r = s.result
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self._fails += 1
            self._rec = None                   # 下次 _ensure 会重新 load
            if self._revivals >= _MAX_REVIVALS:
                # 最后一次复活也失败：直接停用，不再空等第四轮冷却
                self._disabled = True
                self._dead_at = 0.0
            elif self._fails >= _MAX_FAILS:
                self._dead_at = time.monotonic()
            return AsrResult(text="", duration=dur)

        self._reset_failures()
        text = _TAG.sub("", str(getattr(r, "text", "") or ""))
        text = _TAG2.sub("", text).strip()
        return AsrResult(
            text=text,
            lang=str(getattr(r, "lang", "") or "").strip("<>| "),
            emotion=str(getattr(r, "emotion", "") or "").strip("<>| "),
            duration=dur,
        )
