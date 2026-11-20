"""Real-time audio -> mouth shape.

RMS alone gives the classic "jaw flap": every syllable becomes the same
up/down motion.  The mouth also has a *shape*: spread for ``i/e``, rounded for
``o/u``, wide open for ``a``.  This module estimates the first two formants
with LPC, classifies the frame against five cardinal vowels, and emits a
continuous two-dimensional mouth pose (openness + form).

It follows the same signal path as VPresentation's ``LipsyncAnalyser`` and
uses Rhubarb Lip Sync's nine mouth shapes as the readable calibration table.
The output is intentionally continuous: discrete visemes are smoothed, because
a hard switch between mouth drawings at 50 Hz looks like a flipbook.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

try:  # numpy is a DeepSidekick dependency; keep import failure debuggable.
    import numpy as np
except Exception:  # pragma: no cover - only used on a broken install
    np = None

from .types import SpeechFrame, clamp


VOWEL_TARGETS: dict[str, tuple[float, float]] = {
    "a": (730.0, 1090.0),
    "i": (270.0, 2290.0),
    "u": (300.0, 870.0),
    "e": (530.0, 1840.0),
    "o": (570.0, 840.0),
}

#: Rhubarb's A-X mouth shapes projected onto (openness, form).
#: form > 0 = spread/wide, form < 0 = rounded/puckered.
RHUBARB_SHAPES: dict[str, tuple[float, float]] = {
    "A": (0.04, 0.0),   # closed, slight lip pressure
    "B": (0.22, 0.55),  # slightly open, clenched teeth (EE / most consonants)
    "C": (0.55, 0.20),  # open (EH / AE)
    "D": (0.92, 0.0),   # wide open (AA)
    "E": (0.42, -0.35), # slightly rounded (AO / ER)
    "F": (0.22, -0.75), # puckered (UW / OW / W)
    "G": (0.18, 0.40),  # upper teeth on lower lip (F / V)
    "H": (0.62, 0.10),  # long L, tongue up
    "X": (0.0, 0.0),    # idle / pause
}


def _to_mel(hz: float) -> float:
    return 2595.0 * math.log10(1.0 + max(1.0, float(hz)) / 700.0)


def classify_vowel(f1: float, f2: float, sharpness: float = 12.0) -> dict[str, float]:
    """Softmax over mel-space distance to the five cardinal vowels."""
    if f1 <= 0.0 or f2 <= 0.0:
        return {name: 0.0 for name in VOWEL_TARGETS}
    m1, m2 = _to_mel(f1), _to_mel(f2)
    scores: dict[str, float] = {}
    best = -1e9
    for name, (tf1, tf2) in VOWEL_TARGETS.items():
        d1 = m1 - _to_mel(tf1)
        d2 = m2 - _to_mel(tf2)
        score = -math.sqrt(d1 * d1 + d2 * d2)
        scores[name] = score
        best = max(best, score)
    exps = {name: math.exp((score - best) / max(1e-6, sharpness)) for name, score in scores.items()}
    total = sum(exps.values())
    return {name: value / total for name, value in exps.items()}


def form_from_f2(f2: float) -> float:
    """Map F2 to a spread/rounded mouth axis.

    ``i/e`` have a high F2 and a spread mouth; ``o/u`` have a low F2 and a
    rounded one.  The breakpoints are deliberately soft: a hard split makes
    coarticulation sound like a switch.
    """
    f2 = max(1.0, float(f2))
    if f2 <= 850.0:
        return -0.85
    if f2 <= 1050.0:
        return -0.85 + (f2 - 850.0) / 200.0 * 0.85
    if f2 <= 2100.0:
        return (f2 - 1050.0) / 1050.0 * 0.80
    return min(1.0, 0.80 + (f2 - 2100.0) / 1000.0 * 0.20)


def nearest_shape(openness: float, form: float) -> str:
    """Closest Rhubarb shape, for diagnostics and tests."""
    best = "X"
    best_distance = float("inf")
    for name, (o, f) in RHUBARB_SHAPES.items():
        distance = (openness - o) ** 2 + ((form - f) * 0.8) ** 2
        if distance < best_distance:
            best, best_distance = name, distance
    return best


# ── LPC formants ─────────────────────────────────────
def _rms(x) -> float:
    if x is None or len(x) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x))))


def _autocorrelation(x, order: int):
    n = len(x)
    size = 1 << int(math.ceil(math.log2(max(2, 2 * n))))
    spectrum = np.fft.rfft(x, size)
    power = spectrum * np.conjugate(spectrum)
    corr = np.fft.irfft(power, size)[: order + 1]
    if corr[0] <= 0.0:
        return np.zeros(order + 1, dtype=np.float64)
    return corr.astype(np.float64)


def _levinson_durbin(r, order: int):
    a = np.zeros(order + 1, dtype=np.float64)
    a[0] = 1.0
    error = float(r[0]) if len(r) else 0.0
    if error <= 0.0:
        return a, 0.0
    for i in range(1, order + 1):
        acc = float(r[i]) if i < len(r) else 0.0
        for j in range(1, i):
            acc += float(a[j]) * float(r[i - j]) if i - j < len(r) else 0.0
        k = -acc / error
        if not math.isfinite(k):
            return a, 0.0
        tmp = a.copy()
        for j in range(1, i):
            a[j] = tmp[j] + k * tmp[i - j]
        a[i] = k
        error *= 1.0 - k * k
        if error <= 0.0:
            return a, 0.0
    return a, error


def _lpc_order(sample_rate: int) -> int:
    return min(30, max(8, int(round(sample_rate / 1000.0)) + 4))


def find_formants(frame, sample_rate: int) -> tuple[float, float] | None:
    """Estimate F1/F2 from a pre-emphasised, windowed frame."""
    if np is None or frame is None or len(frame) < 64:
        return None
    if _rms(frame) < 1e-4:
        return None
    order = _lpc_order(sample_rate)
    a, error = _levinson_durbin(_autocorrelation(frame, order), order)
    if error <= 0.0:
        return None

    min_hz, max_hz, points = 150.0, 4000.0, 256
    freqs = np.linspace(min_hz, max_hz, points)
    w = 2.0 * math.pi * freqs / float(sample_rate)
    k = np.arange(len(a))
    # A(e^{jw}) = sum a[k] e^{-jwk}
    matrix = np.exp(-1j * np.outer(w, k))
    response = matrix @ a
    magnitude = np.abs(response)
    env = -20.0 * np.log10(np.maximum(magnitude, 1e-10))

    peaks: list[tuple[float, float]] = []
    for i in range(1, points - 1):
        if env[i] > env[i - 1] and env[i] >= env[i + 1]:
            denom = env[i - 1] - 2.0 * env[i] + env[i + 1]
            shift = 0.5 * (env[i - 1] - env[i + 1]) / denom if denom else 0.0
            hz = min_hz + ((i + shift) / (points - 1)) * (max_hz - min_hz)
            peaks.append((float(hz), float(env[i])))
    if not peaks:
        return None
    # F1 is the lowest vocal-tract resonance, not necessarily the strongest
    # one.  Picking the two strongest peaks can mistake a loud F2 harmonic
    # for F1 (notably on /e/), so take the lowest two well-separated peaks.
    candidates = sorted((p for p in peaks if p[0] >= 200.0), key=lambda item: item[0])
    if not candidates:
        return None
    f1 = candidates[0][0]
    f2 = 0.0
    for hz, _db in candidates[1:]:
        if hz >= f1 + 120.0:
            f2 = hz
            break
    if f2 <= 0.0:
        f2 = min(max_hz, f1 * 1.6)
    return f1, f2


# ── analyser ─────────────────────────────────────────
@dataclass
class LipsyncConfig:
    fps: float = 50.0
    window_seconds: float = 0.040
    silence_rms: float = 0.004
    loud_rms: float = 0.13
    attack: float = 0.62
    release: float = 0.30
    form_smoothing: float = 0.35


class SpeechAnalyzer:
    """Streaming PCM -> compact :class:`SpeechFrame` at 50 Hz.

    The analyser keeps only the tail it still needs; a 10-minute stream uses
    a few kilobytes, not a full recording.
    """

    def __init__(self, sample_rate: int, config: LipsyncConfig | None = None) -> None:
        if np is None:
            raise RuntimeError("lipsync 需要 numpy")
        self.sample_rate = int(sample_rate or 24000)
        self.config = config or LipsyncConfig()
        self.hop = max(1, int(round(self.sample_rate / self.config.fps)))
        self.window_size = max(64, int(round(self.config.window_seconds * self.sample_rate)))
        self._window = np.hanning(self.window_size).astype(np.float64)
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start = 0  # absolute index of _buf[0]
        self._total = 0
        self._next_frame = 0
        self._openness = 0.0
        self._form = 0.0
        self._level = 0.0
        self._last = SpeechFrame(t=0.0)

    # ── buffer ───────────────────────────────────────
    def _append(self, chunk) -> None:
        data = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if data.size == 0:
            return
        self._buf = np.concatenate((self._buf, data)) if self._buf.size else data.copy()
        self._total += data.size

    def _slice(self, start: int) -> np.ndarray | None:
        end = start + self.window_size
        if start < self._buf_start or end > self._total:
            return None
        lo = start - self._buf_start
        hi = lo + self.window_size
        if lo < 0 or hi > len(self._buf):
            return None
        return self._buf[lo:hi]

    def _compact(self) -> None:
        # Keep one full window before the next unprocessed frame.
        keep_from = max(0, self._next_frame * self.hop - self.window_size)
        drop = keep_from - self._buf_start
        if drop > self.window_size * 4:
            self._buf = self._buf[drop:]
            self._buf_start += drop

    # ── analysis ─────────────────────────────────────
    def _analyze(self, frame: np.ndarray, t: float) -> SpeechFrame:
        level = _rms(frame)
        zcr = float(np.mean(np.abs(np.diff(np.signbit(frame).astype(np.int8))))) if len(frame) > 1 else 0.0
        if level < self.config.silence_rms:
            target_open = 0.0
            target_form = 0.0
            voiced = False
            vowel = ""
            f1 = f2 = 0.0
        else:
            # Pre-emphasis flattens the glottal tilt before LPC.
            emphasized = np.empty_like(frame)
            emphasized[0] = frame[0]
            emphasized[1:] = frame[1:] - 0.97 * frame[:-1]
            windowed = emphasized * self._window
            formants = find_formants(windowed, self.sample_rate)
            if formants:
                f1, f2 = formants
                weights = classify_vowel(f1, f2)
                vowel = max(weights, key=weights.get)
                target_form = form_from_f2(f2)
                vowel_open = clamp((f1 - 250.0) / 550.0, 0.18, 1.0)
                normalized = clamp((level - self.config.silence_rms) /
                                   max(1e-6, self.config.loud_rms - self.config.silence_rms), 0.0, 1.0)
                target_open = normalized * (0.52 + 0.48 * vowel_open)
            else:
                vowel = ""
                f1 = f2 = 0.0
                # A fricative has no stable formants; keep a small clenched
                # opening instead of closing the mouth completely.
                target_open = 0.16 * clamp(level / max(1e-6, self.config.loud_rms), 0.0, 1.0)
                target_form = 0.25 if zcr > 0.15 else 0.0
            voiced = level >= self.config.silence_rms and zcr < 0.32

        rate = self.config.attack if target_open > self._openness else self.config.release
        self._openness += (target_open - self._openness) * rate
        self._form += (target_form - self._form) * self.config.form_smoothing
        if self._openness < 1e-4:
            self._openness = 0.0
        self._level += (clamp(level / max(1e-6, self.config.loud_rms), 0.0, 1.0) - self._level) * 0.35

        frame_out = SpeechFrame(
            t=float(t),
            level=clamp(self._level, 0.0, 1.0),
            openness=clamp(self._openness, 0.0, 1.0),
            form=clamp(self._form, -1.0, 1.0),
            voiced=voiced,
            vowel=vowel,
            f1=f1,
            f2=f2,
        )
        self._last = frame_out
        return frame_out

    def push(self, chunk) -> list[SpeechFrame]:
        self._append(chunk)
        frames: list[SpeechFrame] = []
        while True:
            start = self._next_frame * self.hop
            if start + self.window_size > self._total:
                break
            window = self._slice(start)
            if window is None:
                break
            frames.append(self._analyze(window, start / float(self.sample_rate)))
            self._next_frame += 1
        self._compact()
        return frames

    def flush(self) -> list[SpeechFrame]:
        """Emit the remaining frames, zero-padding the tail."""
        frames: list[SpeechFrame] = []
        while self._next_frame * self.hop < self._total:
            start = self._next_frame * self.hop
            available = self._slice(start)
            if available is None:
                available = np.zeros(self.window_size, dtype=np.float32)
                lo = max(0, start - self._buf_start)
                tail = self._buf[lo:lo + self.window_size]
                if tail.size:
                    available[:tail.size] = tail
            frames.append(self._analyze(available, start / float(self.sample_rate)))
            self._next_frame += 1
        return frames

    @property
    def latest(self) -> SpeechFrame:
        return self._last


def analyze_pcm(pcm, sample_rate: int, config: LipsyncConfig | None = None) -> list[SpeechFrame]:
    """Batch helper; the streaming and batch paths share the analyser."""
    if np is None:
        return []
    analyser = SpeechAnalyzer(sample_rate, config)
    return [*analyser.push(pcm), *analyser.flush()]
