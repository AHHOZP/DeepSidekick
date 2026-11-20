"""语义记忆模型的装配入口。

组合根只调 `build()`；关闭记忆/关闭语义时返回空对象，业务层不写开关。
"""
from __future__ import annotations

from pathlib import Path

from sidekick.memory.embed.catalog import Entry, all_entries, get
from sidekick.memory.embed.worker import EmbeddingService


def build(cfg: dict, root: str | Path, log=print):
    if not bool(cfg["memory"].get("enabled", True)):
        from sidekick.core.null import NullEmbedder

        return NullEmbedder()
    if not bool(cfg["memory"].get("embed_enabled", True)):
        from sidekick.core.null import NullEmbedder

        return NullEmbedder()
    return EmbeddingService(cfg, root, log)


__all__ = ["Entry", "EmbeddingService", "all_entries", "build", "get"]
