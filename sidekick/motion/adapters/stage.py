"""浏览器宿主：runtime 内跑同一引擎，按 60 Hz 推原始参数。

浏览器只负责按 ID 写参数；参数自省、分层合成、所有权和口型都在 Python。
"""
from __future__ import annotations

import time

from sidekick.avatar.autonomy.motion_parse import expression_asset, motion_asset
from sidekick.motion.engine import MotionEngine
from sidekick.motion.introspect import physics_additive_specs
from sidekick.motion.ownership import MotionOwnership
from sidekick.motion.types import Context, Intent, ModelProfile

EXPRESSION_HOLD_S = 6.0


class StageMotionHost:
    def __init__(self, stage, profile: ModelProfile, *, avatar=None,
                 seed: int = 7) -> None:
        self.stage = stage
        self.profile = profile
        self.avatar = avatar
        self.engine = MotionEngine(profile, seed=seed)
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
        self.seq = 0
        self._source = profile.source
        self._last_mouth = 0.0
        self._last_added = 0
        self._asset_cache: dict[tuple[str, str, int], object | None] = {}
        # 与桌面 Live2DWriter 相同：胸/裙/尾等物理输出不能覆盖，只能送增量，
        # 让浏览器在 physics.evaluate 之后用当前物理值 + delta 写回。
        self._additive = physics_additive_specs(profile)

    def submit(self, intents) -> None:
        for intent in intents or []:
            self.engine.submit(intent)

    def submit_assets(self, acts, *, page: int) -> None:
        """浏览器会自己播资产；runtime 先把资产参数让出来。

        语义 emotion/action/gaze 必须和桌面一样进入引擎；page 是当前页。
        """
        now = time.monotonic()
        for act in acts or []:
            if not isinstance(act, dict):
                continue
            kind = str(act.get("kind") or "")
            name = str(act.get("name") or "")
            if kind in ("emotion", "action", "gaze"):
                if int(act.get("page") or 0) != page:
                    continue
                try:
                    intensity = float(act.get("intensity", 1.0))
                except (TypeError, ValueError):
                    intensity = 1.0
                self.engine.submit(Intent(kind, name, intensity, None, "text"))
                if kind == "emotion" and self.avatar is not None:
                    mapped = (self.profile.expressions or {}).get(name, "")
                    if mapped:
                        asset, _error = expression_asset(self.avatar, mapped)
                        if asset is not None:
                            self.ownership.claim(asset.params, now,
                                                 EXPRESSION_HOLD_S)
                            try:
                                self.stage.emit(
                                    "control", op="expression", name=mapped)
                            except (RuntimeError, ValueError, TypeError) as exc:
                                self.error = f"{type(exc).__name__}: {exc}"
                continue
            if self.avatar is None:
                continue
            if kind == "expression":
                key = ("e", name, 0)
                if key not in self._asset_cache:
                    self._asset_cache[key] = expression_asset(
                        self.avatar, name)[0]
                asset = self._asset_cache[key]
                if asset is not None:
                    self.ownership.claim(asset.params, now, EXPRESSION_HOLD_S)
            elif kind == "motion":
                index = int(act.get("index") or 0)
                key = ("m", name, index)
                if key not in self._asset_cache:
                    self._asset_cache[key] = motion_asset(
                        self.avatar, name, index)[0]
                asset = self._asset_cache[key]
                if asset is not None:
                    hold = asset.duration * (2.0 if asset.loop else 1.0) + 0.2
                    self.ownership.claim(asset.params, now, hold)

    def tick(self, now: float, dt: float, speech=None,
             context: Context | None = None) -> dict[str, float]:
        ctx = context or Context()
        frame = self.engine.tick(now, dt, speech=speech, context=ctx)
        protected = set()
        if ctx.speaking:
            mouth = self.profile.by_channel.get("mouth_open")
            if mouth is not None:
                protected.add(mouth.id)
            protected.update(
                str(pid) for pid, _scale in
                (self.profile.followers.get("mouth_open") or ()))
        params = self.ownership.filter(frame.params, now, protected=protected)
        added = self._split_additive(params)
        self._last_added = len(added)
        self._last_mouth = float(speech.openness) if speech is not None else 0.0
        self.seq += 1
        try:
            payload = {pid: round(float(value), 3) for pid, value in params.items()}
            add_payload = {pid: round(float(value), 4)
                           for pid, value in added.items()}
            # 事件类型字段是 t，时间戳必须用 ts；否则会把 "pose" 覆盖成数字。
            self.stage.emit("pose", params=payload, add=add_payload,
                            ts=round(float(now), 3), seq=self.seq)
        except (RuntimeError, ValueError, TypeError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        return params

    def _split_additive(self, params: dict[str, float]) -> dict[str, float]:
        """把物理叶结点拆成相对默认值的 delta，交给 JS 侧叠加。"""
        added: dict[str, float] = {}
        for pid, spec in self._additive.items():
            raw = params.pop(pid, None)
            if raw is None:
                continue
            delta = float(raw) - float(spec.default)
            if abs(delta) > 1e-6:
                added[pid] = delta
        return added

    def write(self, params: dict[str, float]) -> int:
        return 0

    def report(self) -> dict:
        engine = self.engine.report
        return {
            "enabled": True,
            "active": True,
            "source": self._source,
            "tier": self.profile.tier,
            "archetype": self.profile.archetype,
            "fallback": self.profile.fallback,
            "params": int(engine.get("params", 0)),
            "physics_add": int(self._last_added),
            "action": str(engine.get("action") or ""),
            "emotion": str(engine.get("emotion") or "neutral"),
            "mouth": round(self._last_mouth, 3),
            "owned_params": int(self.ownership.owned),
            "warnings": len(self.profile.warnings),
            "seq": self.seq,
            "error": self.error,
        }
