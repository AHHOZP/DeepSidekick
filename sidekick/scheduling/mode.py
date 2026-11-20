"""模式状态机：quiet / normal / hype。

规则来自 config.yaml：

* 升级**立刻**生效（来人了马上热情）
* 降级要连续 hysteresis_s 秒都满足才降（免得人一走就不理人）
* mode.manual 非空时覆盖自动；silent 永远最高优先

为什么自动：手动切模式的前提是主人记得切，而他在打 BOSS 的时候不会记得。
"""
from __future__ import annotations

import time

QUIET = "quiet"
NORMAL = "normal"
HYPE = "hype"
SILENT = "silent"

RANK = {QUIET: 0, NORMAL: 1, HYPE: 2}
LABEL = {
    QUIET: "陪看（安静，只在冷场和大事件开口）",
    NORMAL: "正常（坐旁边那位）",
    HYPE: "热闹（人多，多接话多叫人）",
    SILENT: "静音（不说话，也不报错）",
}

# 给模型看的说法：不带内部术语，也不带"该怎么做"的命令（怎么做由人设决定）。
PROMPT_LABEL = {
    QUIET: "安静陪看",
    NORMAL: "正常",
    HYPE: "热闹",
    SILENT: "静音",
}


class ModeMachine:
    def __init__(self) -> None:
        self._current = NORMAL
        self._down_since: float | None = None

    @property
    def current(self) -> str:
        return self._current

    def decide(self, viewers: int, danmu_5min: int, mcfg: dict,
               *, solo: bool = False) -> str:
        # manual 永远最高优先：solo 下设了 silent 也必须静音。
        manual = str(mcfg["manual"] or "").strip().lower()
        if manual in (QUIET, NORMAL, HYPE, SILENT):
            self._current = manual
            self._down_since = None
            return manual

        # 离开手动静音：silent 不是“更低的档位”，不能走降级滞后。
        left_silent = self._current == SILENT
        if left_silent:
            self._current = NORMAL
            self._down_since = None

        # solo 没有观众，不按人数掉进 quiet；用正常陪玩节奏。
        if solo:
            self._current = NORMAL
            self._down_since = None
            return NORMAL

        if not mcfg["auto"]:
            return self._current

        if (
            viewers <= int(mcfg["quiet_max_viewers"])
            and danmu_5min < int(mcfg["quiet_max_danmu_5min"])
        ):
            target = QUIET
        elif (
            viewers > int(mcfg["hype_min_viewers"])
            or danmu_5min > int(mcfg["hype_min_danmu_5min"])
        ):
            target = HYPE
        else:
            target = NORMAL

        cur = self._current
        if left_silent or RANK[target] >= RANK.get(cur, 1):
            self._current = target
            self._down_since = None
            return target

        # 降级：要连续满足够久
        now = time.time()
        if self._down_since is None:
            self._down_since = now
            return cur
        if now - self._down_since >= float(mcfg["hysteresis_s"]):
            self._current = target
            self._down_since = None
            return target
        return cur


def cadence(mode: str, ccfg: dict) -> float:
    """这一档的冷场阈值（秒）。弹幕批量窗口统一看 scheduler.batch_s。"""
    row = ccfg[mode]
    return float(row["idle_s"])
