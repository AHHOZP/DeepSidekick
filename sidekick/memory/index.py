"""分池向量矩阵：按 `vector_version` 重载，写后由下一次查询自然刷新。

只服务召回；不负责编码（`embed/worker.py`）、不负责打分（`recall.py`）。
"""
from __future__ import annotations

import numpy as np


class VectorIndex:
    def __init__(self, store, memory, embedder) -> None:
        self.store = store
        self.memory = memory
        self.embedder = embedder
        self._cache: dict[str, tuple[tuple, list[str], np.ndarray]] = {}

    def invalidate(self) -> None:
        self._cache.clear()

    def _rows(self, pool: str, game_id: str) -> list[dict]:
        model = str(getattr(self.embedder, "identity", "") or "")
        if pool == "episodes":
            return self.store.memory_vectors(scope="live", kind="summary",
                                             model=model)
        return (self.store.memory_vectors(scope="game", kind="plot",
                                          game_id=game_id, model=model)
                + self.store.memory_vectors(scope="game", kind="npc",
                                            game_id=game_id, model=model)
                + self.store.memory_vectors(scope="game", kind="quests",
                                            game_id=game_id, model=model))

    def _load(self, pool: str, game_id: str):
        version = self.store.vector_version()
        sig = (pool, game_id, version,
               str(getattr(self.embedder, "identity", "") or ""))
        cached = self._cache.get(pool)
        if cached is not None and cached[0] == sig:
            return cached[1], cached[2]
        ids: list[str] = []
        vecs: list[np.ndarray] = []
        for row in self._rows(pool, game_id):
            vec = np.frombuffer(row["vector"], dtype=np.float32)
            if not vec.size:
                continue
            if vecs and vec.size != vecs[0].size:
                continue
            ids.append(str(row["id"]))
            vecs.append(vec)
        mat = (np.stack(vecs) if vecs
               else np.empty((0, 0), dtype=np.float32))
        self._cache[pool] = (sig, ids, mat)
        return ids, mat

    def vectors(self, pool: str, game_id: str = "") -> dict[str, np.ndarray]:
        ids, mat = self._load(pool, game_id)
        return {mid: mat[i] for i, mid in enumerate(ids)}

    def sims(self, pool: str, query, game_id: str = "") -> dict[str, float]:
        if query is None:
            return {}
        ids, mat = self._load(pool, game_id)
        if not ids:
            return {}
        q = np.asarray(query, dtype=np.float32).reshape(-1)
        if mat.shape[1] != q.shape[0]:
            return {}
        return {mid: float(score) for mid, score in zip(ids, mat @ q)}
