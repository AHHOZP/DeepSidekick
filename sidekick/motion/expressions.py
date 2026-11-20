"""Layered expression mixer.

`Live2D`'s native expression manager can only have one expression active at a
time and switches it hard.  This mixer keeps several `.exp3` assets active with
independent fade-in, hold and fade-out weights, then applies their parameter
recipes on top of the engine's raw parameter frame.

The mixer is deliberately model-agnostic: it only reads the `Parameters` rows
(`Id`, `Value`, `Blend`) that the expression file already declared.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .types import ModelProfile, clamp

if TYPE_CHECKING:
    from sidekick.avatar.autonomy.motion_parse import ExpressionAsset


@dataclass
class _Active:
    asset: "ExpressionAsset"
    weight: float = 0.0
    hold: float = 6.0
    fade_in: float = 0.35
    fade_out: float = 0.55


class ExpressionMixer:
    """Multiple expression assets with independent weights."""

    def __init__(
        self,
        assets: dict[str, "ExpressionAsset"],
        ranges: dict[str, tuple[float, float, float]] | None = None,
        *,
        hold: float = 6.0,
        fade_in: float = 0.35,
        fade_out: float = 0.55,
    ) -> None:
        self.assets = {str(name): asset for name, asset in assets.items()}
        self.ranges = dict(ranges or {})
        self.hold = max(0.2, float(hold))
        self.fade_in = max(0.04, float(fade_in))
        self.fade_out = max(0.04, float(fade_out))
        self._active: dict[str, _Active] = {}
        self._lookup = {str(name).casefold(): str(name) for name in self.assets}

    @property
    def active(self) -> dict[str, float]:
        return {name: round(row.weight, 3)
                for name, row in self._active.items() if row.weight > 0.001}

    def clear(self) -> None:
        """淡出所有表情；调用方仍会在下一帧看到 residual weight。"""
        for row in self._active.values():
            row.hold = 0.0

    def reset(self) -> None:
        self._active.clear()

    def play(self, name: str, *, hold: float | None = None) -> bool:
        key = self._lookup.get(str(name or "").casefold())
        if key is None:
            return False
        row = self._active.get(key)
        if row is None:
            row = _Active(
                asset=self.assets[key],
                hold=max(0.2, float(hold if hold is not None else self.hold)),
                fade_in=self.fade_in,
                fade_out=self.fade_out,
            )
            self._active[key] = row
        else:
            row.hold = max(0.2, float(hold if hold is not None else self.hold))
        return True

    def update(self, dt: float) -> None:
        step = max(0.0, min(0.2, float(dt)))
        for name, row in list(self._active.items()):
            if row.hold > 0.0:
                row.hold = max(0.0, row.hold - step)
            target = 1.0 if row.hold > 0.0 else 0.0
            if target > 0.0:
                row.weight = min(1.0, row.weight + step / max(0.04, row.fade_in))
            else:
                row.weight = max(0.0, row.weight - step / max(0.04, row.fade_out))
            if target == 0.0 and row.weight <= 0.001:
                self._active.pop(name, None)

    def apply(self, params: dict[str, float], profile: ModelProfile,
              *, speaking: bool = False) -> dict[str, float]:
        """Return a new raw parameter frame with active expressions layered on."""
        if not self._active:
            return params
        out = dict(params)
        mouth = profile.by_channel.get("mouth_open")
        mouth_id = mouth.id if mouth is not None else ""
        mouth_ids = {mouth_id} if mouth_id else set()
        mouth_ids.update(
            str(pid) for pid, _scale in
            (profile.followers.get("mouth_open") or ()))
        for row in self._active.values():
            weight = clamp(row.weight, 0.0, 1.0)
            if weight <= 0.001:
                continue
            for pid, value, blend in row.asset.values:
                if speaking and pid in mouth_ids:
                    continue
                bounds = self.ranges.get(pid)
                if bounds is None:
                    continue
                minimum, maximum, default = bounds
                current = float(out.get(pid, default))
                mode = str(blend).lower()
                if mode == "multiply":
                    target = current * (1.0 + (float(value) - 1.0) * weight)
                elif mode == "overwrite":
                    target = current + (float(value) - current) * weight
                else:
                    target = current + float(value) * weight
                out[pid] = max(minimum, min(maximum, target))
        return out
