"""打断（barge-in）判决：她播音频时，检测到你在说话就立刻停。

纯判决，没有 IO：`mic.py` 每块喂一个电平，命中就把结果转成 `on_barge()` 回调。
独立成文件是因为这里有状态机（持续时长、冷却、衰减），塞进采集里会让
"采集"和"判决"两个变更理由混在一起。

判据分两种设备形态：

  耳机   —— 麦克风电平超过开口门限的 1.5 倍并持续 `barge.min_ms`
  扬声器 —— 还要再高过"当前播放块能量 × `barge.ratio`"（她播得越响，你要越大声）

历史教训：旧实现用 `mic.speech_ms` 当打断判据，而 `speech_ms` 在播放期被
ducking 清零，两个功能共用一个状态，打断永远不可能触发。所以这里自己计数。
"""
from __future__ import annotations

from sidekick.input.devices import KIND_HEADSET, KIND_SPEAKER

#: 打断应该比"轮到我说话"更难触发：1.5 倍门限 + 持续 min_ms，咳嗽、碰桌过不去。
GATE_FACTOR = 1.5
#: 播放线程 100ms 推一次参考电平；超过这么久说明播放已停或卡住，不该再用旧值。
REF_MAX_AGE_S = 0.3


def active_for(enabled_mode: str, mic_mode: str) -> bool:
    """`auto` → 耳机才开；扬声器只在显式 `on` 时开。"""
    mode = str(enabled_mode or "").lower()
    if mode == "on":
        return True
    if mode == "off":
        return False
    return str(mic_mode or "").lower() == KIND_HEADSET


class BargeGate:
    def __init__(self, cfg: dict) -> None:
        b = cfg["barge"]
        self.enabled_mode = str(b["enabled"]).lower()   # auto | on | off
        self.min_ms = float(b["min_ms"])
        self.ratio = float(b["ratio"])
        self.cooldown_ms = float(b["cooldown_ms"])
        self._ms = 0.0
        self._cool_until = 0.0
        self._last_ref = 0.0
        self.triggers = 0
        self.last_ms = 0.0

    def active(self, mic_mode: str) -> bool:
        return active_for(self.enabled_mode, mic_mode)

    def feed(self, level: float, *, playing: bool, ref: float, ref_age: float,
             gate: float, mic_mode: str, block_ms: float, now: float) -> bool:
        """喂一块；这一块凑够条件就返回 True（并进入冷却）。"""
        if not self.active(mic_mode) or not playing:
            self._ms = 0.0
            self.last_ms = 0.0
            return False
        if now < self._cool_until:
            self._ms = 0.0
            self.last_ms = 0.0
            return False

        threshold = float(gate) * GATE_FACTOR
        if str(mic_mode or "").lower() == KIND_SPEAKER:
            # 播放线程 100ms 推一次；超过 300ms 未更新说明播放已停/卡住。
            # 文档要求“用前一条”，保留最后一个有效参考，而不是直接放行。
            if ref_age < REF_MAX_AGE_S:
                self._last_ref = float(ref)
            r = self._last_ref
            threshold = max(threshold, r * self.ratio)

        if level > threshold:
            self._ms += block_ms
        else:
            # 衰减而不是清零：正常说话词间有 50–150ms 低谷，清零会让 min_ms 永远攒不满。
            self._ms = max(0.0, self._ms - block_ms * 2.0)
        self.last_ms = self._ms

        if self._ms >= self.min_ms:
            self._ms = 0.0
            self.last_ms = 0.0
            self._cool_until = now + self.cooldown_ms / 1000.0
            self.triggers += 1
            return True
        return False
