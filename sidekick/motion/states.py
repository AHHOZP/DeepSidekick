"""离散状态机：换装、体型和手型贴图的持久/定时保持。

状态不是语义通道。它们直接对应模型原始参数（``Clothes1``、``Movement2``），
必须在每帧 ``to_parameters`` 之后单独写回，否则动作结束的下一帧就被默认值
覆盖。持久状态跨轮保持；定时状态在保持时间结束后平滑释放。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .types import StateSpec, clamp


@dataclass
class _Active:
    active: bool = False
    left: float = 0.0
    value: float = 0.0


@dataclass
class StateController:
    specs: dict[str, StateSpec] = field(default_factory=dict)
    _active: dict[str, _Active] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._active = {name: _Active() for name in self.specs}

    def clear(self) -> None:
        self._active = {name: _Active() for name in self.specs}

    def resolve(self, name: str) -> str:
        key = str(name or "").strip().lower()
        if key in self.specs:
            return key
        for state, spec in self.specs.items():
            if any(str(alias).strip().lower() == key for alias in spec.aliases):
                return state
        return ""

    def request(self, name: str, active: bool = True) -> bool:
        state = self.resolve(name)
        if not state:
            return False
        spec = self.specs[state]
        row = self._active[state]
        row.active = bool(active)
        if spec.mode == "timed" and active:
            row.left = max(0.0, float(spec.hold_seconds))
        if spec.mutex_group:
            for other, other_spec in self.specs.items():
                if other == state or other_spec.mutex_group != spec.mutex_group:
                    continue
                self._active[other].active = False
                self._active[other].left = 0.0
        return True

    def set_raw(self, param_id: str, value: float) -> None:
        """profile 自定义持久参数：用同名伪状态保存。"""
        key = f"raw:{param_id}"
        spec = StateSpec(
            name=key, param_id=str(param_id), active_value=float(value),
            inactive_value=float(value), mode="persistent",
        )
        self.specs[key] = spec
        self._active[key] = _Active(active=True)

    def update(self, dt: float) -> None:
        step = max(0.0, float(dt))
        for name, row in self._active.items():
            spec = self.specs[name]
            if row.active:
                row.value = min(1.0, row.value + step / 0.08)
                if spec.mode == "timed":
                    row.left = max(0.0, row.left - step)
                    if row.left <= 0.0:
                        row.left = 0.0
                        row.active = False
            elif row.value > 0.0:
                row.value = max(0.0, row.value - step / 0.08)

    def parameters(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for name, row in self._active.items():
            spec = self.specs[name]
            if (not row.active and row.value <= 0.0
                    and spec.mode != "persistent"):
                # 持久状态即使关闭也要每帧写 inactive：Cubism 的
                # LoadParameters() 会把上一帧写过的值带回来，不写就会卡住。
                continue
            blend = clamp(row.value, 0.0, 1.0)
            value = spec.inactive_value + (spec.active_value - spec.inactive_value) * blend
            out[spec.param_id] = float(value)
            for pid, coupled in spec.coupled_params.items():
                out[str(pid)] = float(coupled) * blend
        return out

    def report(self) -> dict:
        return {
            name: {
                "active": row.active,
                "value": round(row.value, 3),
                "left": round(row.left, 2),
            }
            for name, row in sorted(self._active.items())
            if row.active or row.value > 0.0
        }
