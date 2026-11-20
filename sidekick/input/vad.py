"""VAD：把 RMS 块切成一段段「你在说话」。

线上 `mic.py` 和两个调参脚本共用这一份规则：

  · 起手要连续 `start_blocks` 块超过门限（挡枪响/爆音）；
  · 开口后续接门槛降到门限的 `continue_ratio`（句中的轻声字不切断）；
  · 连续静音 `silence_ms` 才算一句说完，尾部静音不送进识别；
  · 纯说话时长不足 `min_speech_ms` 的段丢掉；
  · 一直说个不停时，超过 `max_segment_s` 硬切一段。

门限有两个来源：线上用 `start_gate_of(噪声底 p10, 环境峰值 p90, 门限百分比)` 现算
（固定基线 + 自适应 + 百分比），离线脚本/测试可以用 `VadParams.threshold` 直接给绝对值。

参数只从 `config.defaults()` 合并后的 `mic` 块读，不在调用方另写 fallback。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, replace

import numpy as np

# 门限的绝对上下限（float32 -1..1 的 RMS）。
GATE_FLOOR = 0.0025
GATE_CEIL = 0.12
# 固定开口基线：声音从麦克风到 VAD 这一层，先按这台机器的实测值定一个地板。
# 0.010 来自测试用 USB 麦克风在系统音量 100/50、正常/小声/大声几种情况下的实测：
# 小声平均约 0.015–0.019、正常约 0.021–0.022、大声约 0.027–0.028；
# 静音/游戏 p90 接近 0，峰值大多 < 0.012。
START_FLOOR = 0.010
# 噪声底要抬到基线的几倍才开始压过固定地板；这里的 3 是旧版门限映射的中档。
NOISE_MULT = 3.0
# 环境峰值（最近 10 秒 p90）参与开口门限的系数；游戏/电脑人声持续时抬高。
HIGH_MARGIN = 1.15
# 用户可调的门限百分比：100% = 固定值，50% 更灵敏，150% 更保守。
GATE_PERCENT_DEFAULT = 100
GATE_PERCENT_MIN = 50
GATE_PERCENT_MAX = 150
# 稳态长段是否算环境底噪：短时 RMS 包络的变异系数低于它，才允许 force_reseed。
STEADY_NOISE_CV_MAX = 0.28


def gate_percent_of(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return GATE_PERCENT_DEFAULT
    return min(GATE_PERCENT_MAX, max(GATE_PERCENT_MIN, n))


def base_gate_of(noise: float | None, high: float | None) -> float:
    """固定地板、噪声底、环境峰值三者取最大，作为 100% 门限。"""
    gate = START_FLOOR
    if noise is not None:
        gate = max(gate, float(noise) * NOISE_MULT)
    if high is not None:
        gate = max(gate, float(high) * HIGH_MARGIN)
    return min(GATE_CEIL, max(GATE_FLOOR, gate))


def start_gate_of(noise: float | None, high: float | None,
                  percent: int = GATE_PERCENT_DEFAULT) -> float:
    """开口门限：100% 基线 × 用户百分比，夹在绝对上下限之间。"""
    gate = base_gate_of(noise, high) * (gate_percent_of(percent) / 100.0)
    return min(GATE_CEIL, max(GATE_FLOOR, gate))


def continue_gate_of(start_gate: float, continue_ratio: float) -> float:
    """开口后的续接门限；与 `Segmenter` 内部算法保持一致，供状态端点显示。"""
    return max(0.0, float(start_gate) * float(continue_ratio))


class NoiseFloor:
    """最近 30 秒的 p10 作为环境噪声，最近 10 秒的 p90 作为环境峰值。

    启动 2 秒内不生效（`value()` 返回 None）。

    播放期与说话期的块不进估计（`usable=False`）：她的声音和你的人声都会
    抬高噪声底，跟着涨的话门限会自己跑掉。
    """

    def __init__(self, window_s: float = 30.0, block_ms: float = 30.0,
                 warmup_s: float = 2.0, high_window_s: float = 10.0) -> None:
        every = max(0.001, block_ms)
        self._buf: deque[float] = deque(
            maxlen=max(1, int(window_s * 1000.0 / every)))
        self._high_buf: deque[float] = deque(
            maxlen=max(1, int(high_window_s * 1000.0 / every)))
        self._warmup_blocks = max(0, int(warmup_s * 1000.0 / every))
        # 每 1 秒重算一次：逐块算 percentile 的抖动不值得，CPU 也没必要。
        self._every = max(1, int(1000.0 / max(0.001, block_ms)))
        self._blocks = 0
        self._since = 0
        self._value: float | None = None
        self._high: float | None = None

    def feed(self, level: float, *, usable: bool) -> None:
        """usable=False 的块不进估计（播放期、已判定为说话的块）。"""
        self._blocks += 1
        if not usable:
            return
        self._buf.append(float(level))
        self._high_buf.append(float(level))
        self._since += 1
        if self._since < self._every:
            return
        self._since = 0
        self._refresh()

    def _refresh(self) -> None:
        if self._blocks < self._warmup_blocks or not self._buf:
            self._value = None
            self._high = None
            return
        vals = list(self._buf)
        p = float(np.percentile(vals, 10))
        high = float(np.percentile(list(self._high_buf), 90))
        # EMA 平滑：0.8 旧 + 0.2 新。
        self._value = p if self._value is None else 0.8 * self._value + 0.2 * p
        self._high = (high if self._high is None
                      else 0.8 * self._high + 0.2 * high)

    def value(self) -> float | None:
        return self._value

    def value_high(self) -> float | None:
        """最近 10 秒可用块的 p90：环境里的持续峰值（游戏/电脑人声）。"""
        return self._high

    def force_reseed(self, levels: list[float]) -> None:
        """把一段平稳环境声直接注入估计，用于长时间没有可用块的自锁恢复。"""
        vals = [float(x) for x in list(levels)[-200:] if math.isfinite(float(x))]
        if not vals:
            return
        self._buf.extend(vals)
        self._high_buf.extend(vals)
        self._blocks = max(self._blocks, self._warmup_blocks)
        self._refresh()


@dataclass(frozen=True)
class VadParams:
    continue_ratio: float
    start_blocks: int
    silence_ms: float
    min_speech_ms: float
    max_segment_s: float
    #: 离线路径的绝对门限；线上由 `start_gate_of()` 现算，不走这里。
    threshold: float = START_FLOOR

    @classmethod
    def from_cfg(cls, cfg: dict) -> "VadParams":
        """从 `config.load()` 返回的配置读；默认值已经合并，不再兜底。"""
        m = cfg["mic"]
        return cls(
            continue_ratio=float(m["continue_ratio"]),
            start_blocks=max(1, int(m["start_blocks"])),
            silence_ms=float(m["silence_ms"]),
            min_speech_ms=float(m["min_speech_ms"]),
            max_segment_s=float(m["max_segment_s"]),
        )

    def with_threshold(self, threshold: float) -> "VadParams":
        return replace(self, threshold=float(threshold))


@dataclass(frozen=True)
class Segment:
    """一段话的块范围。`start`/`end` 是连续流里的绝对块下标，`end` 不含。"""

    start: int
    end: int
    speech_blocks: int
    weakest: float
    below_gate: int = 0


class Segmenter:
    """在线切句：每来一块 RMS 调一次 `feed()`，完成一段时返回 `Segment`。"""

    def __init__(self, params: VadParams, block_ms: float) -> None:
        self.params = params
        self.block_ms = float(block_ms)
        self.index = 0
        self.speaking = False
        self._pre = 0
        self._tail = 0
        self._start = 0
        self._weakest = 0.0
        self._below_gate = 0

    @property
    def start(self) -> int:
        """当前这一段的第一块（只在 speaking 时有意义）。"""
        return self._start

    def reset(self) -> None:
        """丢掉当前状态（例如播放期间整段不采信）。块下标继续增长。"""
        self.speaking = False
        self._pre = 0
        self._tail = 0
        self._below_gate = 0

    def feed(self, level: float, gate: float | None = None) -> Segment | None:
        """gate 为 None 时用 `params.threshold`（离线脚本/测试用）。"""
        p = self.params
        g = p.threshold if gate is None else float(gate)
        i = self.index
        self.index += 1
        gate_now = g if not self.speaking else g * p.continue_ratio

        if level > gate_now:
            self._tail = 0
            if not self.speaking:
                self._pre += 1
                if self._pre < p.start_blocks:
                    return None
                self.speaking = True
                self._start = i - self._pre + 1
                self._weakest = level
                self._below_gate = 0
                self._pre = 0
                return None
            if level < self._weakest:
                self._weakest = level
            # 一直说个不停：硬切一段，当前块留给下一段。
            if (i + 1 - self._start) * self.block_ms > p.max_segment_s * 1000.0:
                seg = Segment(self._start, i, i - self._start,
                              self._weakest, self._below_gate)
                self._start = i
                self._weakest = level
                self._below_gate = 0
                return seg
            return None

        self._pre = 0
        if not self.speaking:
            return None
        self._tail += 1
        self._below_gate += 1
        if level < self._weakest:
            self._weakest = level
        if self._tail * self.block_ms >= p.silence_ms:
            speech_blocks = max(0, i + 1 - self._tail - self._start)
            seg = Segment(self._start, i + 1, speech_blocks,
                          self._weakest, self._below_gate)
            self.speaking = False
            self._tail = 0
            self._below_gate = 0
            return seg
        return None


def segment_levels(levels, block_ms: float, params: VadParams) -> list[Segment]:
    """离线切句：喂一串 RMS 块，返回达到 `min_speech_ms` 的段。"""
    vad = Segmenter(params, block_ms)
    out: list[Segment] = []

    def take(seg: Segment | None) -> None:
        if seg is not None and seg.speech_blocks * block_ms >= params.min_speech_ms:
            out.append(seg)

    for level in levels:
        take(vad.feed(float(level)))
    # 离线调参录满就停，不会自带尾部静音；补一段静音把最后一句切出来。
    if vad.speaking:
        tail_blocks = max(1, math.ceil(params.silence_ms / max(0.001, block_ms)) + 1)
        for _ in range(tail_blocks):
            take(vad.feed(0.0))
            if not vad.speaking:
                break
    return out
