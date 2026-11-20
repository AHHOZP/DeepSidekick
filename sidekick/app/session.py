"""会话状态：直播模式、配置热重载、身份资料重读。

`session.live` 决定要不要接弹幕：关 = 单人陪玩（弹幕源强制 none），
开 = 直播陪玩。这是一个重启级开关。

热重载只覆盖**提示词相关**的配置：预设、用户档案、游戏/世界书和角色名/别名。
调度、TTS、麦克风、截屏、记忆容量、形象层等设置都按“改完重启 runtime”处理，
避免每 15 秒把整份配置、人设、世界书和召回索引全部重建一次。
"""
from __future__ import annotations

import time

from sidekick import config

REFRESH_S = 15.0


class SessionState:
    def __init__(self, cfg: dict, *, resolve=None) -> None:
        self.cfg = cfg
        self.live = bool((cfg.get("session") or {}).get("live", False))
        self.resolve = resolve
        self.danmu_base = str(cfg["danmu_source"])
        self._cfg_ts = 0.0
        # 基线存"生效后"的配置，否则第一次 refresh 会误判成有变化。
        self.cfg = self.effective()

    def mark_loaded(self, now: float | None = None) -> None:
        """组合根刚注入 cfg 时调用，避免下一轮立刻重复读盘。"""
        self._cfg_ts = time.time() if now is None else float(now)

    def effective(self, cfg: dict | None = None) -> dict:
        """返回运行期配置副本：直播模式的派生值在这里合成，不改共享 dict。"""
        base = self.cfg if cfg is None else cfg
        out = dict(base)
        out["danmu_source"] = self.danmu_base if self.live else "none"
        out["session"] = {**(base.get("session") or {}), "live": self.live}
        return out

    # ── 配置热重载 ────────────────────────────────────
    def refresh(self, now: float, *, loader=None) -> dict | None:
        """最多 15 秒看一次盘；配置有变化才返回新 cfg。

        返回的 cfg 只用于 prompt 相关字段；Runtime._reload() 不会重建
        scheduler/TTS/麦克风/截屏等重启级模块。
        """
        if now - self._cfg_ts < REFRESH_S:
            return None
        self._cfg_ts = now
        raw = (loader or config.load)()
        if config.last_error:
            # 非 WebUI 途径把文件改坏时，继续用当前配置；下次修好再 reload。
            return None
        if self.resolve is not None:
            raw = self.resolve(raw)
        cfg = self.effective(raw)
        if cfg == self.cfg:
            return None
        self.cfg = cfg
        return cfg
