"""语音转文字 —— 门面（管道）。

对外只承诺三件事：`warmup()` / `analyze()` / `error`。
真正的引擎在 `sidekick/input/stt/engines/` 下，换模型不改这里。

管道做的事：
  · 按 `stt.model` 选引擎，加载失败就按 `stt.fallback` 依次试
  · 后处理（幻觉过滤，见 engines/postprocess.py）
  · 统计（每段的耗时，p50/p95 给 /status 和日志用）

线程模型：`analyze()` 不保证线程安全，只允许一个线程串行调用。
`sidekick/input/stt/worker.py` 满足；别在她播放的线程里调。
"""
from __future__ import annotations

import time
from pathlib import Path

from sidekick import config as cfgmod
from sidekick.input.stt.engines import postprocess
from sidekick.input.stt.engines.base import AsrResult


class SttPipeline:
    def __init__(self, cfg: dict, root=None) -> None:
        self.cfg = cfg
        self.root = Path(root) if root is not None else cfgmod.ROOT
        self.engine = None
        self.error: str | None = None
        self._load_s = 0.0
        self._ms: list[float] = []
        self._n = 0
        self._last_ms = 0.0

    # ── 给 /status 和日志看的只读属性 ─────────────────────
    @property
    def model_size(self) -> str:
        return str(getattr(self.engine, "model_size", "") or "?")

    @property
    def name(self) -> str:
        return str(getattr(self.engine, "name", "") or "?")

    @property
    def load_s(self) -> float:
        return self._load_s

    @property
    def dead(self) -> bool:
        """引擎连续失败、正在冷却。冷却期间识别返回空，30 秒后自动重试。"""
        return bool(getattr(self.engine, "dead", False))

    @property
    def disabled(self) -> bool:
        """引擎已经彻底停用（复活次数用完），只有重启 runtime 才会再加载。"""
        return bool(getattr(self.engine, "disabled", False))

    @property
    def revivals(self) -> int:
        """已经用掉的复活次数（0~3）。"""
        return int(getattr(self.engine, "revivals", 0) or 0)

    @property
    def engine_error(self) -> str | None:
        """引擎自己的错误（识别阶段崩掉的），和门面的 error 是两回事。"""
        return getattr(self.engine, "error", None)

    # ── 生命周期 ──────────────────────────────────────
    def warmup(self) -> bool:
        """加载模型；失败返回 False 并把原因写进 self.error。

        注意顺序：**先 warmup 再开麦克风**（runtime 已经这么排）。反过来
        的话加载那几秒里麦克风在切段、8 格队列一满就丢段。
        """
        try:
            cands = list(self._candidates())
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            return False
        if not cands:
            self.error = self._no_engine_hint()
            return False

        errs: list[str] = []
        for eng in cands:
            t0 = time.time()
            try:
                ok = eng.warmup()
            except Exception as e:              # 引擎自己的锅，别把启动带走
                ok = False
                eng.error = f"{type(e).__name__}: {e}"
            if ok:
                self.engine = eng
                self._load_s = time.time() - t0
                self.error = None
                return True
            errs.append(f"{getattr(eng, 'model_size', '?')}: "
                        f"{getattr(eng, 'error', 'warmup failed')}")
            try:
                eng.close()
            except Exception:
                pass
        self.error = " | ".join(errs) or "所有引擎都加载失败"
        return False

    def _no_engine_hint(self) -> str:
        st = self.cfg["stt"]
        mid = str(st["model"])
        try:
            from sidekick.input.stt.engines.registry import load_specs
            have = ", ".join(load_specs(self.cfg, self.root)) or "（空）"
        except Exception:
            have = "（读取失败）"
        return (f"stt.models 里没有选中的模型 {mid!r}；"
                f"已配置：{have}。检查 config.yaml 的 stt.model / stt.models")

    def _candidates(self):
        """按 stt.model 和 stt.fallback 的顺序给出引擎；全失败就没语音输入。"""
        from sidekick.input.stt.engines.sherpa import SherpaEngine
        from sidekick.input.stt.engines.registry import load_specs

        st = self.cfg["stt"]
        specs = load_specs(self.cfg, self.root)
        order = [st["model"]] + list(st["fallback"])
        for mid in order:
            mid = str(mid or "").strip()
            if mid and mid in specs:
                yield SherpaEngine(specs[mid], st)

    # ── 识别 ──────────────────────────────────────────
    def analyze(self, pcm, sr) -> AsrResult:
        """识别 + 后处理。引擎没起来时返回空结果，不抛。"""
        if self.engine is None:
            return AsrResult(text="")

        t0 = time.perf_counter()
        try:
            res = self.engine.transcribe(pcm, sr)
        except Exception as e:                  # 引擎崩了也不能带走识别线程
            self.error = f"{type(e).__name__}: {e}"
            return AsrResult(text="")
        res.text = postprocess.clean(res, self.cfg)
        self.error = None
        self._record((time.perf_counter() - t0) * 1000.0)
        return res

    def snapshot(self) -> dict:
        """给 /status 的稳定字段；调用方不再逐个 getattr 引擎内部。"""
        return {"model": self.model_size, "engine": self.name,
                "error": self.engine_error or self.error,
                "dead": self.dead, "disabled": self.disabled,
                "revivals": self.revivals,
                "dropped_dead": int(getattr(self.engine, "dropped_dead", 0))}

    # ── 统计 ──────────────────────────────────────────
    def _record(self, ms: float) -> None:
        self._n += 1
        self._last_ms = ms
        self._ms.append(ms)
        if len(self._ms) > 200:
            self._ms = self._ms[-200:]

    def stats(self) -> dict:
        ms = sorted(self._ms)
        n = len(ms)
        p50 = ms[n // 2] if n else 0.0
        p95 = ms[min(n - 1, int(n * 0.95))] if n else 0.0
        return {"n": self._n, "window": n, "last_ms": self._last_ms,
                "p50_ms": p50, "p95_ms": p95}

    def close(self) -> None:
        if self.engine is not None:
            try:
                self.engine.close()
            except Exception:
                pass
            self.engine = None


def make(cfg: dict, root=None) -> SttPipeline:
    return SttPipeline(cfg, root)
