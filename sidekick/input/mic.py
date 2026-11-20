"""麦克风：判"你在说话" + 把你说的一整句切出来。

两个用途：
  1. `user_speaking` / `speech_ms` —— 让调度器知道你正在说，冷场先让路
  2. 录音分段 —— 说完一段就交给 stt 转文字，变成一条「我」的事件

开口门限不是配置里写死的绝对 RMS，而是"自适应噪声底 × 灵敏度"再乘一个
两段式的开口倍率，并参考环境 p90 峰值：换麦、换房间、开游戏都不用重新量
（见 `sidekick/input/vad.py` 的 `start_gate_of`）。

播放期怎么处理：
  播放期 —— 不进 VAD（避免把 TTS 认成用户），但打断判决在吞之前独立执行
  尾音期 —— 播放结束后再吞 150ms，避免 TTS 余响被当成第一声
设备形态（`sidekick/input/devices.py`）仍用来决定打断是否可用/怎么判；
设备被拔掉/切走时会自己重开，重开时重新判定形态。
"""
from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np

from sidekick.core.log import log
from sidekick.input import devices
from sidekick.input.barge import BargeGate
from sidekick.input.devices import KIND_SPEAKER
from sidekick.input.vad import (GATE_CEIL, STEADY_NOISE_CV_MAX, NoiseFloor,
                            Segmenter, VadParams, continue_gate_of,
                            gate_percent_of, start_gate_of)


class Mic:
    def __init__(self, cfg: dict, on_segment=None) -> None:
        self.enabled = bool(cfg["mic"]["enabled"])
        # 切句参数只存一份；线上与两个调参脚本共用 sidekick/input/vad.py 的规则。
        self.params = VadParams.from_cfg(cfg)
        self.on_segment = on_segment
        # 她正在播的时候返回 True：音箱会把她的声音灌进麦克风，
        # 这段音频整段不采信（否则她说什么都会被当成"你在说"）。
        self.is_playing = None
        # 她正在播的那一块能量 (rms, monotonic_ts)，扬声器模式下打断要用它当参考。
        # 整体赋值保证读到的是一对，不会读到拆开的旧值。
        self.play_ref = None
        # 命中打断时通知出口（组合根接到 speaker.stop）；输入层不 import 出口。
        self.on_barge = None

        self.mode_cfg = str(cfg["mic"]["mode"]).lower()      # auto | headset | speaker
        self.mode = KIND_SPEAKER                             # 实际生效值
        self.gate_percent = gate_percent_of(cfg["mic"]["gate_percent"])
        self._noise = NoiseFloor()
        self.barge = BargeGate(cfg)
        self.gate = 0.0                 # 当前开口门限，给 /status
        self.gate_continue = 0.0        # 开口后的续接门限，给 /status
        self.noise = 0.0                # 当前噪声底，给 /status
        self.noise_high = 0.0           # 噪声/环境峰值的 p90，给 /status

        self._speech_started = 0.0      # 当前这一段从什么时候开始；0 = 没在说
        self._speaking_since = 0.0      # monotonic；用于打破噪声估计自锁
        self._thread: threading.Thread | None = None
        self._closed = False
        self.error: str | None = None
        self.level = 0.0                # 最近一块的 RMS，给 /status 看
        self.restarts = 0               # 设备重开了几次
        # 给 /status、回归脚本和冷场避让用的统计
        self.last_voice_at = 0.0        # 最后一次超过门限的时刻（不等 STT）
        self.peak = 0.0                 # 本次运行见过的最大 RMS
        self.sent = 0                   # 切出去交给 STT 的段数
        self.dropped_short = 0          # 太短被丢掉的段数
        self.dropped_s = 0.0            # 被丢掉的段总时长（秒）

    # ── 状态 ──────────────────────────────────────────
    @property
    def user_speaking(self) -> bool:
        """现在是不是**正说着**：从第一块超门限开始，到切句为止。"""
        return self._speech_started > 0

    @property
    def speech_ms(self) -> float:
        """这一段已经连续说了多久（毫秒）；没在说返回 0。"""
        return (time.time() - self._speech_started) * 1000.0 if self._speech_started else 0.0

    @property
    def barge_ms(self) -> float:
        """当前连续满足打断条件的毫秒数（给 /status）。"""
        return self.barge.last_ms

    @property
    def barge_triggers(self) -> int:
        return self.barge.triggers

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="mic", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed = True

    # ── 主循环：设备断了就重开 ─────────────────────────
    def _loop(self) -> None:
        """采集线程的外壳。里面一旦抛异常（拔耳机、切设备、独占冲突），
        睡两秒重新开流——以前是直接死掉，语音输入得重启程序才能恢复。"""
        while not self._closed:
            try:
                self._capture()
            except Exception as e:
                if self._closed:
                    break
                self.restarts += 1
                self._speech_started = 0.0
                self.error = f"麦克风中断：{type(e).__name__}: {e}（2 秒后重开）"
                time.sleep(2.0)

    def _emit(self, blocks: list[np.ndarray], sr: int) -> None:
        """把攒下来的块拼成一段交出去。回调出错不能把采集线程带死。"""
        if not blocks or self.on_segment is None:
            return
        self.sent += 1
        try:
            self.on_segment(np.concatenate(blocks).astype(np.float32), sr)
        except Exception as e:
            self.error = f"分段回调出错：{type(e).__name__}: {e}"

    def _capture(self) -> None:
        """开一次输入流，一直读到出错或关闭。"""
        import sounddevice as sd

        # 始终跟随 Windows「系统 → 声音 → 输入」的默认设备：
        # 不读 mic.device，也不按下标/名字选设备（数字字符串会被 sounddevice
        # 当成名字子串，匹配到多个设备时直接抛 ValueError）。
        info = sd.query_devices(kind="input")
        sr = int(info.get("default_samplerate") or 48000)
        block = max(256, int(sr * 0.03))          # 30 ms 一块
        block_ms = 1000.0 * block / sr            # 一块多少毫秒（44.1k 下正好 30.0）

        # 每次开流判一次设备形态；拔耳机/切设备会触发重开，自然重新判。
        out_name = devices.output_name()
        self.mode = devices.resolve_mode(self.mode_cfg, out_name)
        log(f"麦克风策略：{self.mode}（输出设备 {out_name or '未知'}）")
        mode_checked = time.monotonic()

        vad = Segmenter(self.params, block_ms)
        # 换设备后旧噪声底/旧说话状态都不可信，重新估计。
        self._noise = NoiseFloor()
        self._speaking_since = 0.0
        # 只留「当前段 + 最近的预攒块」；长静音不会把内存撑大。
        audio: deque[np.ndarray] = deque()
        audio_start = 0
        gate_logged = False
        seg_rms: list[float] = []

        with sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                            blocksize=block) as stream:
            self.error = None                      # 流开起来了，之前的错误清掉
            while not self._closed:
                data, _ = stream.read(block)
                mono = data[:, 0].copy()
                rms = float(np.sqrt(np.mean(np.square(mono, dtype=np.float64))))
                playing = bool(self.is_playing and self.is_playing())
                # 电平在播放期也照实更新（观测量要真实），但不更新"最后出声时刻"。
                self.level = rms
                if rms > self.peak:
                    self.peak = rms

                now_mono = time.monotonic()
                # 输出设备中途换到耳机/扬声器时，barge 判据要跟着换；
                # 用户不一定拔插麦克风触发重开。
                if now_mono - mode_checked >= 10.0:
                    mode_checked = now_mono
                    new_mode = devices.resolve_mode(
                        self.mode_cfg, devices.output_name())
                    if new_mode != self.mode:
                        self.mode = new_mode
                        log(f"麦克风策略变化：{new_mode}")

                # 噪声底：播放期与说话期的块不采信，否则门限会跟着涨。
                # 例外：长段包络很平（CV 低）时是环境底噪，强制回灌估计；
                # 包络起伏明显（真人长句）绝不污染底噪。
                if vad.speaking:
                    seg_rms.append(rms)
                    if len(seg_rms) > 400:
                        del seg_rms[:200]
                recent = seg_rms[-200:]
                cv_ok = False
                if len(recent) >= 200:
                    mean_r = float(np.mean(recent))
                    if mean_r > 0:
                        cv_ok = (float(np.std(recent)) / mean_r
                                 < STEADY_NOISE_CV_MAX)
                steady_reseed = (
                    vad.speaking and not playing
                    and self._speaking_since > 0
                    and now_mono - self._speaking_since
                    > max(10.0, float(self.params.max_segment_s))
                    and len(recent) >= 200 and cv_ok
                )
                if steady_reseed:
                    self._noise.force_reseed(recent)
                    vad.reset()
                    self._speech_started = 0.0
                    self._speaking_since = 0.0
                    seg_rms.clear()
                force_estimate = (
                    self._noise.value() is None
                    and vad.speaking and not playing
                    and self._speaking_since > 0
                    and now_mono - self._speaking_since > 10.0
                    and cv_ok
                )
                self._noise.feed(
                    rms, usable=not playing and (not vad.speaking
                                                 or force_estimate))
                self.noise = self._noise.value() or 0.0
                self.noise_high = self._noise.value_high() or 0.0
                self.gate = start_gate_of(self._noise.value(),
                                          self._noise.value_high(),
                                          self.gate_percent)
                self.gate_continue = continue_gate_of(
                    self.gate, self.params.continue_ratio)
                if not gate_logged and self._noise.value() is not None:
                    gate_logged = True
                    log(f"麦克风门限：噪声底 {self.noise:.4f} "
                        f"环境峰值 {self.noise_high:.4f} "
                        f"开口 {self.gate:.4f} 续接 {self.gate_continue:.4f}")

                # 打断判决用独立计数器（barge_ms），与 speech_ms 无关。
                ref, ref_ts = (self.play_ref() if self.play_ref is not None
                               else (0.0, 0.0))
                ref_age = ((now_mono - float(ref_ts)) if ref_ts else 1e9)
                hit = self.barge.feed(
                    rms, playing=playing, ref=float(ref),
                    ref_age=ref_age,
                    gate=self.gate, mic_mode=self.mode, block_ms=block_ms,
                    now=now_mono)
                if hit and self.on_barge is not None:
                    try:
                        self.on_barge()
                    except Exception as e:
                        self.error = f"打断回调出错：{type(e).__name__}: {e}"

                # 播放期照常判 VAD：TTS 的漏音由 barge 的独立判决和
                # 起手门限处理，不再整段吞掉（旧版临时手段已删除）。

                gate_now = (self.gate * self.params.continue_ratio
                            if vad.speaking else self.gate)
                if rms > gate_now:
                    self.last_voice_at = time.time()

                effective_gate = self.gate
                # 回声门限只用于“起新段”：已经开口后仍按正常续接门限，
                # 否则播放期间的小声人声会被当成静音，句子被切断。
                if (not vad.speaking and playing
                        and self.mode == KIND_SPEAKER and ref_age < 0.3):
                    # 无硬件 AEC 时，外放尾音至少要比当前播放块高过同一条比例线。
                    effective_gate = min(GATE_CEIL, max(
                        effective_gate,
                        float(ref) * self.barge.ratio * 0.85))
                idx = vad.index
                was_speaking = vad.speaking
                seg = vad.feed(rms, effective_gate)
                now = time.time()

                if not audio:
                    audio_start = idx
                audio.append(mono)

                if vad.speaking and not was_speaking:
                    self._speech_started = now
                    self._speaking_since = now_mono

                if seg is not None:
                    # 先取音频再裁剪：段首可能早于「最近 start_blocks 块」。
                    blocks = [audio[k - audio_start]
                              for k in range(seg.start, seg.start + seg.speech_blocks)]
                    dur_ms = len(blocks) * block_ms
                    if dur_ms >= self.params.min_speech_ms:
                        self._emit(blocks, sr)
                    else:
                        self.dropped_short += 1
                        self.dropped_s += max(0.0, dur_ms) / 1000.0
                    if not vad.speaking:
                        self._speech_started = 0.0
                        self._speaking_since = 0.0
                    else:
                        # 硬切后当前块是下一段的开头。
                        self._speech_started = now

                if not vad.speaking:
                    seg_rms.clear()

                # 没开口时只留最近 start_blocks 块作预攒；开口后从段首留起。
                keep_from = (vad.start if vad.speaking
                             else vad.index - self.params.start_blocks)
                while audio and audio_start < keep_from:
                    audio.popleft()
                    audio_start += 1
