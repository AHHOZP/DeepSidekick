"""测试用运行期配置：把 config.defaults() 的身份字段解析好。

生产路径由组合根调用 `sidekick.app.identity.resolve()`；测试里直接构造
PromptBuilder / Brain / Scheduler 时也需要同样的 cfg（persona/user 字段）。
"""
from __future__ import annotations

from pathlib import Path

from sidekick import config
from sidekick.app.identity import resolve


def runtime_cfg(root) -> dict:
    return resolve(config.defaults(), Path(root)).cfg
