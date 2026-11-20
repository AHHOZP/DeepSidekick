"""短期历史：每轮必用的 messages。

和长期记忆分开之后，两个开关不再互相绑架：
`memory.enabled=false` 用 `InMemoryHistory`，**不打开 SQLite**；
SQLite 模式下 `SqliteHistory` 与 `Memory` 共用同一个 `MemoryStore`，
总结事务才能在“写长期记忆 + 删这批历史”之间保持原子性。

热窗里存的是“本轮材料摘要 + 她说的话”，不是整包材料；
`ts` 只用于总结的时间范围和召回的时间意图，`game_id` 只用于切游戏分组，都不进 prompt。
"""
from __future__ import annotations

from sidekick.memory.limits import (SUMMARY_HISTORY_EXTRA,
                                SUMMARY_MAX_FAILURES)
from sidekick.memory.store import MemoryStore


class SqliteHistory:
    def __init__(self, store: MemoryStore, *, history_max: int = 40,
                 summarize_every: int = 16) -> None:
        self.store = store
        self._batch_ids: list[int] = []
        self._summary_failures = 0
        self.reload({"memory": {"history_max": history_max,
                                "summarize_every": summarize_every}})

    def reload(self, cfg: dict) -> None:
        m = cfg["memory"]
        history_max = max(2, int(m["history_max"]))
        if history_max % 2:
            history_max += 1
        summarize_every = max(2, int(m["summarize_every"]))
        if summarize_every % 2:
            summarize_every += 1
        if summarize_every >= history_max:
            history_max = summarize_every + 2
        self.history_max = history_max
        self.summarize_every = summarize_every

    # ── 读 ────────────────────────────────────────────
    def count(self) -> int:
        return self.store.count_messages()

    def messages(self) -> list[dict]:
        limit = self.history_max
        if 0 < self._summary_failures < SUMMARY_MAX_FAILURES:
            limit += SUMMARY_HISTORY_EXTRA
        rows = self.store.messages(limit=limit)
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    def set_summary_failures(self, count: int) -> None:
        self._summary_failures = max(0, int(count))

    def needs_summary(self) -> bool:
        return self.count() >= self.history_max

    # ── 写 ────────────────────────────────────────────
    def add_exchange(self, user: str, assistant: str,
                     game_id: str = "") -> tuple[dict, dict]:
        with self.store.transaction():
            u = self.store.add_message("user", user, game_id=game_id)
            a = self.store.add_message("assistant", assistant, game_id=game_id)
        return u, a

    def drop_pair(self, user: dict, assistant: dict) -> None:
        ids = [x.get("id") for x in (user, assistant) if isinstance(x, dict)]
        self.store.delete_messages([x for x in ids if x is not None])

    def update_assistant(self, assistant: dict, text: str) -> None:
        if not isinstance(assistant, dict) or assistant.get("id") is None:
            return
        content = " ".join(str(text or "").split())
        assistant["content"] = content
        self.store.update_message(int(assistant["id"]), content)

    # ── 总结窗口 ──────────────────────────────────────
    def batch(self) -> list[dict]:
        rows = self.store.messages(self.summarize_every, oldest=True)
        if not rows:
            self._batch_ids = []
            return []
        # 一批只处理最老的一个游戏段：切游戏后不会被新游戏“抢走”归属。
        gid = str(rows[0].get("game_id") or "")
        cut = len(rows)
        for i, row in enumerate(rows):
            if str(row.get("game_id") or "") != gid:
                cut = i
                break
        rows = rows[:cut]
        self._batch_ids = [int(r["id"]) for r in rows]
        return rows

    def drop_batch(self) -> None:
        ids = self._batch_ids
        self._batch_ids = []
        if ids:
            self.store.delete_messages(ids)

    def close(self) -> None:
        # store 由组合根统一关闭（和长期记忆共用）
        return None
