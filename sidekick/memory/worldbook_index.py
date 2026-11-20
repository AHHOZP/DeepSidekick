"""世界书向量缓存：文件仍是唯一真源，SQLite 只存可重建的向量。

每轮 `sync()` 对比条目指纹：
  · 新条目/内容变化/换模型 → 入队后台编码；
  · 删除的条目 → 删缓存；
  · 还没编码完的条目本轮只走关键词，不阻塞。
"""
from __future__ import annotations

import hashlib

import numpy as np


def entry_text(entry) -> str:
    keys = "、".join(entry.keys or [])
    return f"{entry.title}\n{keys}\n{entry.content}".strip()


def content_hash(entry, identity: str) -> str:
    raw = "|".join([
        str(entry.title or ""),
        "、".join(entry.keys or []),
        "、".join(entry.aliases or []),
        str(entry.content or ""),
        str(identity or ""),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class WorldbookIndex:
    def __init__(self, store, worldbook, embedder) -> None:
        self.store = store
        self.worldbook = worldbook
        self.embedder = embedder
        self._pending: dict[str, str] = {}
        self.last_error: str | None = None

    @property
    def game_id(self) -> str:
        return str(getattr(self.worldbook, "id", "") or "")

    @property
    def identity(self) -> str:
        return str(getattr(self.embedder, "identity", "") or "")

    def sync(self) -> None:
        if (not self.embedder.ready or not self.game_id
                or not getattr(self.worldbook, "enabled", False)
                or getattr(self.worldbook, "path", None) is None):
            # game 关闭/目录暂时读不到时不要删缓存：entries 为空不等于世界书被删。
            return
        read_error = getattr(self.worldbook, "error", None)
        if read_error:
            # 一次坏文件/瞬时读失败时 entries 也会变空；这时删缓存会把整本
            # 世界书的向量清掉，等文件修好只能全部重编码。
            self.last_error = f"世界书读取失败，跳过向量同步：{read_error}"
            return
        try:
            entries = {e.id: e for e in self.worldbook.entries_snapshot()
                       if not e.always}
            game_id = self.game_id
            identity = self.identity
            existing = self.store.worldbook_hashes(game_id)
            for eid in set(existing) - set(entries):
                self.store.delete_worldbook_vector(game_id, eid)
            todo = []
            for eid, entry in entries.items():
                digest = content_hash(entry, identity)
                old = existing.get(eid)
                if (old and old["content_hash"] == digest
                        and old["model"] == identity):
                    continue
                if self._pending.get(eid) == digest:
                    continue
                todo.append((eid, entry, digest))
            if todo:
                self._submit(game_id, identity, todo)
            self.last_error = None
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"

    def _submit(self, game_id: str, identity: str, todo: list) -> None:
        for eid, _entry, digest in todo:
            self._pending[eid] = digest
        texts = [entry_text(e) for _eid, e, _d in todo]

        def done(vectors) -> None:
            if vectors is None or len(vectors) != len(todo):
                for eid, _e, _d in todo:
                    self._pending.pop(eid, None)
                return
            for (eid, _e, digest), vec in zip(todo, vectors):
                if self._pending.get(eid) != digest:
                    continue
                try:
                    self.store.upsert_worldbook_vector(
                        game_id=game_id, entry_id=eid, content_hash=digest,
                        model=identity, vector=bytes(vec))
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"
                finally:
                    self._pending.pop(eid, None)

        if self.embedder.submit_docs(texts, done) is None:
            for eid, _entry, _digest in todo:
                self._pending.pop(eid, None)

    def similarities(self, query, allowed_ids: set[str]) -> dict[str, float]:
        if query is None or not self.embedder.ready or not self.game_id:
            return {}
        rows = self.store.worldbook_vectors(self.game_id, self.identity)
        if not rows:
            return {}
        q = np.asarray(query, dtype=np.float32).reshape(-1)
        out: dict[str, float] = {}
        for row in rows:
            if row["entry_id"] not in allowed_ids:
                continue
            v = np.frombuffer(row["vector"], dtype=np.float32)
            if v.shape != q.shape:
                continue
            out[row["entry_id"]] = float(np.dot(q, v))
        return out

    def status(self) -> dict:
        try:
            entries = [e for e in self.worldbook.entries_snapshot()
                       if not e.always]
        except Exception:
            entries = []
        total = len(entries)
        try:
            stored = len(self.store.worldbook_vectors(self.game_id,
                                                       self.identity))
        except Exception:
            stored = 0
        return {"indexed": stored, "total": total,
                "pending": max(len(self._pending), max(0, total - stored)),
                "identity": self.identity,
                "error": self.last_error}
