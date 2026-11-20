"""模型 motion/expression 与程序化层的参数所有权。

规则只有一条：正在播放的模型资产写过的参数，引擎这一帧不写；资产结束后
从模型最后值平滑交给引擎，避免硬切。过滤粒度是参数 ID，不是整段资产。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .types import clamp


@dataclass
class MotionOwnership:
    crossfade_s: float = 0.32
    _expires: dict[str, float] = field(default_factory=dict)
    _handover_from: dict[str, float] = field(default_factory=dict)
    _handover_at: dict[str, float] = field(default_factory=dict)
    last_owned: int = 0

    def claim(self, param_ids, now: float, hold_s: float) -> None:
        """资产开始/继续播放：这些参数在 ``now + hold_s`` 前归模型。"""
        until = float(now) + max(0.0, float(hold_s))
        for pid in param_ids:
            pid = str(pid or "")
            if not pid:
                continue
            self._expires[pid] = max(self._expires.get(pid, 0.0), until)
            # 重新开始播放时取消上一段交接。
            self._handover_from.pop(pid, None)
            self._handover_at.pop(pid, None)

    def release(self, param_ids=None) -> None:
        if param_ids is None:
            self._expires.clear()
            self._handover_from.clear()
            self._handover_at.clear()
            return
        for pid in param_ids:
            pid = str(pid or "")
            self._expires.pop(pid, None)
            self._handover_from.pop(pid, None)
            self._handover_at.pop(pid, None)

    def filter(self, params: dict[str, float], now: float, *,
               read_raw=None, protected=frozenset()) -> dict[str, float]:
        """返回这一帧真正该写给模型的参数。

        ``read_raw(pid)`` 在资产刚结束时读一次模型当前值，作为 320 ms
        交接的起点；读不到就直接用引擎值，不做假插值。
        """
        now = float(now)
        out: dict[str, float] = {}
        protected = {str(x) for x in protected}
        for pid, value in params.items():
            pid = str(pid)
            if pid in protected:
                out[pid] = float(value)
                continue
            until = self._expires.get(pid)
            if until is not None and now < until:
                continue
            if until is not None:
                # 第一次发现资产结束：记下模型最后值并开始交接。
                expired_at = until
                self._expires.pop(pid, None)
                start = None
                if read_raw is not None:
                    try:
                        start = read_raw(pid)
                    except Exception:
                        start = None
                if start is not None:
                    self._handover_from[pid] = float(start)
                    self._handover_at[pid] = expired_at
            start = self._handover_from.get(pid)
            if start is None:
                out[pid] = float(value)
                continue
            began = self._handover_at.get(pid, now)
            alpha = clamp((now - began) / max(1e-6, self.crossfade_s), 0.0, 1.0)
            alpha = alpha * alpha * (3.0 - 2.0 * alpha)
            out[pid] = float(start) + (float(value) - float(start)) * alpha
            if alpha >= 1.0:
                self._handover_from.pop(pid, None)
                self._handover_at.pop(pid, None)
        self.last_owned = sum(1 for until in self._expires.values() if until > now)
        return out

    @property
    def owned(self) -> int:
        return self.last_owned
