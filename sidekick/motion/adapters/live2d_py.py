"""live2d-py adapter: parameter census, frame order, and writes.

The important detail is the frame order.  ``live2d.Model.Update()`` starts with
``LoadParameters()`` (restoring the saved/neutral values), then applies motion,
blink, expression, breath, physics and pose.  Writing a parameter *before*
``Update()`` is therefore erased.  The only correct order is::

    model.Update(dt)   # advance motion/physics/expressions
    engine.tick(...)   # compute this frame
    write(params)      # override the parameters this frame
    model.Draw()       # render the overridden values

The model's own automatic breath/blink and idle motion must be disabled;
otherwise they keep writing the same parameters before the engine does.  The
engine deliberately keeps the model's physics (hair, clothes) alive, so a
one-frame lag between the engine pose and the physics input is acceptable and
usually invisible.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..engine import MotionEngine
from ..introspect import (
    ParamSource, introspect, physics_additive_specs, read_display_channels,
    read_display_states, read_physics_graph,
)
from ..ownership import MotionOwnership
from ..vtube import parse_vtube_config
from ..types import Context, Intent, ModelProfile


class Live2DParamSource(ParamSource):
    """Read parameter ranges from a live2d-py model without a renderer."""

    def __init__(self, model: Any, model3_path: str | Path | None = None) -> None:
        self.model = model
        self.model3_path = model3_path
        self._ids = [str(x) for x in (model.GetParamIds() or [])]
        self._index = {pid: i for i, pid in enumerate(self._ids)}
        self._info: dict[str, tuple[float, float, float]] = {}
        for pid, index in self._index.items():
            self._info[pid] = (
                float(model.GetParamMinByIndex(index)),
                float(model.GetParamMaxByIndex(index)),
                float(model.GetParamDefaultByIndex(index)),
            )

    def param_ids(self) -> list[str]:
        return list(self._ids)

    def param_info(self, param_id: str) -> tuple[float, float, float]:
        return self._info[param_id]

    def profile(self, probe: dict[str, dict] | None = None, *,
                motion_activity: dict[str, dict] | None = None,
                vtube=None) -> ModelProfile:
        physics_inputs, physics_outputs = read_physics_graph(self.model3_path)
        if vtube is None:
            vtube = parse_vtube_config(self.model3_path)
        return introspect(
            self,
            physics_outputs=physics_outputs,
            physics_inputs=physics_inputs,
            display_channels=read_display_channels(self.model3_path),
            states=read_display_states(self.model3_path),
            probe=probe,
            vtube=vtube,
            motion_activity=motion_activity,
        )


@dataclass
class Live2DWriter:
    """Write a parameter dict through indexed live2d-py calls."""

    model: Any
    profile: ModelProfile
    failed: int = 0
    _index: dict[str, int] | None = None
    _additive: dict[str, Any] | None = None
    _physics_inputs: set[str] | None = None
    _physics_outputs: set[str] | None = None

    def __post_init__(self) -> None:
        if self._index is None:
            ids = [str(x) for x in (self.model.GetParamIds() or [])]
            self._index = {pid: i for i, pid in enumerate(ids)}
        if self._physics_inputs is None:
            self._physics_inputs = set(getattr(self.profile, "physics_inputs", set()))
        if self._physics_outputs is None:
            self._physics_outputs = set(
                getattr(self.profile, "physics_outputs", set()))
        if self._additive is None:
            # 物理叶结点不能直接用语义值覆盖，否则胸/裙/尾的原生摆锤会被锁死。
            # 写入时取 Update(dt) 刚算出的物理值，再叠加引擎偏移。
            self._additive = physics_additive_specs(self.profile)

    def prepare(self) -> None:
        """关掉会和引擎抢同一批参数的内建行为；不停止模型动作。"""
        for name in ("SetAutoBreath", "SetAutoBlink"):
            method = getattr(self.model, name, None)
            if method is not None:
                method(False)

    def write(self, params: dict[str, float]) -> int:
        written = 0
        index = self._index or {}
        for pid, value in params.items():
            i = index.get(pid)
            if i is None:
                continue
            try:
                final = float(value)
                spec = (self._additive or {}).get(pid)
                if spec is not None:
                    physical = float(self.model.GetParamValueByIndex(i))
                    delta = final - float(spec.default)
                    final = max(spec.minimum,
                                min(spec.maximum, physical + delta * 0.80))
                self.model.SetParamByIndex(i, final)
                # Update() 下一帧会 LoadParameters()；物理输入必须先写进
                # saved values，physics.evaluate 才能看到它。
                # 级联物理链的中间节点可能同时是 input/output；把 output
                # 写进 savedParameters 会让物理摆锤跨帧正反馈抖动。
                if (pid in (self._physics_inputs or set())
                        and pid not in (self._physics_outputs or set())):
                    save = getattr(self.model, "SetSaveParamByIndex", None)
                    if save is not None:
                        save(i, final)
                written += 1
            except (RuntimeError, TypeError, ValueError):
                # 单个参数坏掉不能停掉整张脸；失败数由宿主报到状态。
                self.failed += 1
        return written

    def read_raw(self, param_id: str) -> float | None:
        index = self._index or {}
        i = index.get(str(param_id))
        if i is None:
            return None
        try:
            return float(self.model.GetParamValueByIndex(i))
        except (RuntimeError, TypeError, ValueError):
            return None

class Live2DMotionHost:
    """桌面宿主：唯一允许写参数的入口。

    ``tick`` 读模型当前值来算所有权交接，必须在 ``model.Update()`` 之后；
    ``write`` 必须在 ``model.Draw()`` 之前。
    """

    def __init__(self, model: Any, profile: ModelProfile, *,
                 seed: int = 7, expression_mixer=None) -> None:
        self.model = model
        self.profile = profile
        self.writer = Live2DWriter(model, profile)
        self.engine = MotionEngine(
            profile, seed=seed, expression_mixer=expression_mixer)
        crossfade = 320.0
        policy = getattr(profile, "motion_policy", None) or {}
        ownership = policy.get("ownership") if isinstance(policy, dict) else None
        if isinstance(ownership, dict):
            try:
                crossfade = float(ownership.get("crossfade_ms", crossfade))
            except (TypeError, ValueError):
                crossfade = 320.0
        self.ownership = MotionOwnership(
            crossfade_s=max(0.1, min(0.6, crossfade / 1000.0)))
        self.enabled = True
        self.error: str | None = None
        self.failed_writes = 0
        self._source = profile.source
        self.writer.prepare()

    def submit(self, intents) -> None:
        for intent in intents or []:
            if isinstance(intent, Intent):
                self.engine.submit(intent)

    def claim_asset(self, param_ids, now: float, hold_s: float) -> None:
        self.ownership.claim(param_ids, now, hold_s)

    def tick(self, now: float, dt: float, speech=None, context=None) -> dict:
        frame = self.engine.tick(now, dt, speech=speech,
                                 context=context or Context())
        protected = set()
        if context is not None and context.speaking:
            mouth = self.profile.by_channel.get("mouth_open")
            if mouth is not None:
                protected.add(mouth.id)
            protected.update(
                str(pid) for pid, _scale in
                (self.profile.followers.get("mouth_open") or ()))
        return self.ownership.filter(
            frame.params, now, read_raw=self.writer.read_raw, protected=protected)

    def write(self, params: dict[str, float]) -> int:
        written = self.writer.write(params)
        self.failed_writes += self.writer.failed
        self.writer.failed = 0
        return written

    def report(self) -> dict:
        engine = self.engine.report
        return {
            "enabled": bool(self.enabled),
            "active": bool(self.enabled and engine),
            "source": self._source,
            "tier": self.profile.tier,
            "archetype": self.profile.archetype,
            "params": int(engine.get("params", 0)),
            "action": str(engine.get("action") or ""),
            "emotion": str(engine.get("emotion") or "neutral"),
            "owned_params": int(self.ownership.owned),
            "fallback": bool(engine.get("fallback")),
            "warnings": int(engine.get("warnings", 0)),
            "failed_writes": int(self.failed_writes),
            "error": self.error,
        }
