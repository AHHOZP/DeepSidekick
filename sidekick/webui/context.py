"""WebUI 路由共享的只读上下文。

路由按 domain 拆成模块后，不互相 import，也不回头拿整个 WebUI 对象；
需要的能力通过这里注入。新增一个字段时，看它是不是多个 domain 都要用。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class WebContext:
    root: Path
    cfg: Callable[[], dict]
    catalog: Any
    supervisor: Any
    get_memory: Callable[..., Any]
    memory_error: Callable[[], str | None]
    feed: Any
    csrf: str
    config_notice: str = ""
    jobs: dict[str, dict] = field(default_factory=dict)
    #: 所有“读整份 config → 改 → 写回”的入口共用；避免并发 PATCH 互相覆盖。
    config_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
