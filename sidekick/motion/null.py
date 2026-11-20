"""关闭自主驱动时的空对象：与真实宿主同契约，业务层不写开关判断。"""
from __future__ import annotations


class NullMotionHost:
    #: 与真实宿主同契约：业务层直接读这个属性，不能因为“关闭”就缺字段。
    enabled = False
    active = False
    error: str | None = None

    def start(self, profile_source=None) -> None:
        return None

    def submit(self, intents) -> None:
        return None

    def tick(self, now: float, dt: float, speech=None, context=None) -> dict:
        return {}

    def write(self, params: dict[str, float]) -> int:
        return 0

    def report(self) -> dict:
        return {"enabled": False, "active": False, "source": "off",
                "params": 0, "owned_params": 0, "warnings": 0, "error": None}


NULL_MOTION_HOST = NullMotionHost()
