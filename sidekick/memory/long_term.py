"""长期记忆：summary / 游戏剧情 / NPC / 任务。

短期 messages 在 `sidekick/memory/history.py`；这里不提供 history/messages/batch，
也不让 Brain 碰 `store`。文本解析与总结提示词在 `sidekick/memory/parse.py`，
召回在 `sidekick/memory/recall.py`。

没有 permanent / facts 层：稳定身份在 `persona/users/<id>/profile.yaml`，
其余“关于他的事”写进 summary 正文，按需召回。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from sidekick import config as configmod
from sidekick.memory.parse import _keywords, _list, clean_summary
from sidekick.memory.store import MemoryStore, empty_stats
from sidekick.memory.timefmt import format_span
from sidekick.core.text import json_list, normalize_key


def _title(content: str, title: str = "") -> str:
    # 标题只是列表/注入用的短标签，不承载正文；正文永远原样保存。
    return str(title or "").strip() or str(content or "").strip()[:24]


class Memory:
    last_chapter_error: str | None = None

    def __init__(self, cfg: dict, root: str | Path | None = None,
                 store: MemoryStore | None = None,
                 embedder=None) -> None:
        self.enabled = bool(cfg["memory"]["enabled"])
        self._read_cfg(cfg)
        self.embedder = embedder
        self._indexing = False
        self._index_suspended = False

        g = cfg["game"]
        self.game_enabled = bool(g["enabled"])
        self.game_id = configmod.resolve_game_id(cfg)

        base = Path(root) if root is not None else Path.cwd()
        db = str(cfg["memory"]["db_path"])
        self.store = store if store is not None else MemoryStore(base / db)

        self.last_summary_error: str | None = None
        self.last_recall_error: str | None = None
        self.last_recall: list[dict] = []
        self.last_recall_report: dict = {}

    def _read_cfg(self, cfg: dict) -> None:
        m = cfg["memory"]
        self.recent_live = int(m["recall_recent"])
        self.recall_episodes = int(m["recall_episodes"])
        self.game_recent_plot = int(m["game_recent_plot"])
        self.game_npc_max = int(m["game_npc_max"])
        self.task_max = int(m["task_max"])

    # ── 生命周期 ──────────────────────────────────────
    def reload(self, cfg: dict) -> None:
        """game.* / 记忆参数热更：世界书和记忆必须同时切换/关闭。"""
        g = cfg["game"]
        self.game_enabled = bool(g["enabled"])
        self.game_id = configmod.resolve_game_id(cfg)
        self._read_cfg(cfg)
        self.last_chapter_error = None
        self.last_recall = []
        self.last_recall_report = {}

    # ── 身份切换 ──────────────────────────────────────
    def last_identity(self) -> tuple[str, str] | None:
        raw = self.store.meta_get("last_identity")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return (str(data.get("role") or ""), str(data.get("user") or ""))

    def set_last_identity(self, role_id: str, user_id: str) -> None:
        self.store.meta_set("last_identity", json.dumps({
            "role": str(role_id or ""), "user": str(user_id or ""),
        }, ensure_ascii=False))

    def current_chapter(self) -> str:
        try:
            self.last_chapter_error = None
            return str(self.store.get_state(self.game_id).get("chapter") or "")
        except Exception as e:
            self.last_chapter_error = f"{type(e).__name__}: {e}"
            return ""

    # ── 陪玩记忆（全局）──────────────────────────────
    def recent_live_block(self) -> str:
        if not self.enabled:
            return ""
        sums = self.store.recent_summaries(self.recent_live)
        if not sums:
            return ""
        lines = []
        for x in reversed(sums):
            span = format_span(x.get("start_ts") or 0, x.get("end_ts") or 0)
            if span == "时间不详":
                lines.append(f"- {x['content']}")
            else:
                lines.append(f"- {span}｜{x['content']}")
        return "[最近记得的]（越靠后越近）\n" + "\n".join(lines)

    # ── 游戏记忆（当前世界书）────────────────────────
    def _gid(self, game_id: str | None) -> str:
        return self.game_id if game_id is None else str(game_id)

    def recent_plot(self, limit: int | None = None,
                    game_id: str | None = None) -> list[dict]:
        xs = self.store.list_memories(scope="game", kind="plot",
                                      game_id=self._gid(game_id),
                                      enabled=True, limit=500)
        xs.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
        return xs[: (self.game_recent_plot if limit is None else limit)]

    def current_npcs(self, game_id: str | None = None) -> list[dict]:
        xs = self.store.list_memories(scope="game", kind="npc",
                                      game_id=self._gid(game_id),
                                      enabled=True, limit=500)
        xs.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
        return xs[: self.game_npc_max]

    def _all_tasks(self, game_id: str | None = None) -> list[dict]:
        raw = json_list(self.store.get_state(self._gid(game_id)).get("tasks"))
        tasks = []
        for x in raw:
            if not isinstance(x, dict):
                continue
            title = str(x.get("title") or "").strip()
            if title:
                tasks.append({"title": title,
                              "detail": str(x.get("detail") or "").strip()})
        return tasks

    def active_tasks(self, game_id: str | None = None,
                     limit: int | None = None) -> list[dict]:
        tasks = self._all_tasks(game_id)
        limit = self.task_max if limit is None else int(limit)
        return tasks[-limit:] if limit > 0 else []

    def _save_tasks(self, game_id: str, tasks: list[dict]) -> None:
        self.store.upsert_state(game_id, tasks=tasks)

    def _add_task(self, title: str, detail: str, game_id: str) -> str | None:
        with self.store.transaction():
            tasks = self._all_tasks(game_id)
            if any(x["title"] == title for x in tasks):
                return None
            tasks.append({"title": title, "detail": detail})
            self._save_tasks(game_id, tasks)
            return title

    def _update_task(self, title: str, game_id: str, *,
                     new_title: str = "", detail: str = "") -> str | None:
        with self.store.transaction():
            tasks = self._all_tasks(game_id)
            for t in tasks:
                if t["title"] != title:
                    continue
                if new_title and new_title != title and any(
                        x["title"] == new_title for x in tasks):
                    return None
                if new_title:
                    t["title"] = new_title
                if detail:
                    t["detail"] = detail
                self._save_tasks(game_id, tasks)
                return t["title"]
        return None

    def _complete_task(self, title: str, game_id: str,
                       keywords: list[str] | None = None) -> str | None:
        with self.store.transaction():
            tasks = self._all_tasks(game_id)
            found = [t for t in tasks if t["title"] == title]
            if len(found) != 1:
                return None
            task = found[0]
            tasks = [t for t in tasks if t["title"] != title]
            detail = str(task.get("detail") or "").strip()
            content = title + ("：" + detail if detail else "")
            self._save_tasks(game_id, tasks)
            now = time.time()
            self.store.add_memory(
                scope="game", kind="quests", game_id=game_id,
                title=title, content=content,
                keywords=_keywords(keywords) or [title], source="task",
                start_ts=now, end_ts=now)
            return title

    def game_state_block(self, *, expand_tasks: bool = False,
                         game_id: str | None = None) -> str:
        gid = self._gid(game_id)
        if not self.enabled or not self.game_enabled:
            return ""
        parts = []
        st = self.store.get_state(gid)
        facts = json_list(st.get("known_facts"))
        threads = json_list(st.get("open_threads"))
        observations = json_list(st.get("observations"))
        state_lines = []
        for label, key in (("章节", "chapter"), ("场景", "scene"),
                           ("地点", "location"), ("目标", "objective")):
            v = str(st.get(key) or "").strip()
            if v:
                state_lines.append(f"{label}：{v}")
        if state_lines:
            parts.append("[当前游戏]\n" + "\n".join(state_lines))
        if facts:
            parts.append("[已知事实]\n" + "\n".join(f"- {x}" for x in facts[-12:]))
        if threads:
            parts.append("[未解问题]\n" + "\n".join(f"- {x}" for x in threads[-8:]))
        if observations:
            parts.append("[观察]\n" + "\n".join(f"- {x}" for x in observations[-8:]))

        limit = 20 if expand_tasks else self.task_max
        tasks = self.active_tasks(gid, limit)
        if tasks:
            lines = [f"- {t['title']}"
                     + (f"：{t['detail']}" if t.get("detail") else "")
                     for t in reversed(tasks)]
            head = "[当前任务]"
            if expand_tasks:
                done = self.store.list_memories(
                    scope="game", kind="quests", game_id=gid,
                    enabled=True, limit=5)
                if done:
                    lines.append("")
                    lines.append("[最近完成]")
                    lines += [f"- {x['title']}" for x in done]
                head += "（用户问的是任务清单，可以逐条回答）"
            parts.append(head + "\n" + "\n".join(lines))

        beats = self.recent_plot(game_id=gid)
        if beats:
            parts.append("[最近剧情]（越靠后越近；行首 id= 用于 supersedes）\n" + "\n".join(
                f"- id={str(x['id'])[:8]}｜"
                f"{format_span(x.get('start_ts') or 0, x.get('end_ts') or 0)}"
                f"｜{x['content']}" if (x.get("start_ts") or x.get("end_ts"))
                else f"- id={str(x['id'])[:8]}｜{x['content']}" for x in reversed(beats)))

        npcs = self.current_npcs(gid)
        if npcs:
            parts.append("[当前人物]\n" + "\n".join(
                f"- {x['title']}：{x['content']}" for x in npcs))
        return "\n\n".join(parts)

    # ── 召回用的数据 ──────────────────────────────────
    def recent_summary_ids(self) -> set[str]:
        return {x["id"] for x in self.store.recent_summaries(self.recent_live)}

    def recent_plot_ids(self) -> set[str]:
        return {x["id"] for x in self.recent_plot()}

    def current_npc_ids(self) -> set[str]:
        return {x["id"] for x in self.current_npcs()}

    def live_summaries(self) -> list[dict]:
        xs = self.store.list_memories(scope="live", kind="summary",
                                      game_id="", enabled=True, limit=5000)
        xs.sort(key=lambda x: (x.get("end_ts") or 0,
                               x.get("created_at") or 0), reverse=True)
        return xs

    def game_candidates(self) -> list[dict]:
        if not self.enabled or not self.game_enabled:
            return []
        out = []
        out.extend(self.store.list_memories(
            scope="game", kind="plot", game_id=self.game_id,
            enabled=True, limit=5000))
        out.extend(self.store.list_memories(
            scope="game", kind="npc", game_id=self.game_id,
            enabled=True, limit=5000))
        out.extend(self.store.list_memories(
            scope="game", kind="quests", game_id=self.game_id,
            enabled=True, limit=500))
        return out

    def mark_recalled(self, candidates: list[dict]) -> None:
        """这一轮真正注入的记忆；`/status` 与调试只看这一个入口写的值。"""
        self.last_recall = list(candidates or [])
        ids = [x.get("id") for x in self.last_recall
               if x.get("kind") in ("summary", "plot", "npc", "quests")]
        try:
            self.store.mark_memories_used(ids)
            self.last_recall_error = None
        except Exception as e:
            self.last_recall_error = (f"更新记忆使用时间失败："
                                      f"{type(e).__name__}: {e}")

    # ── 任务 ──────────────────────────────────────────
    def apply_tasks(self, tasks: list[dict]) -> list[dict]:
        applied = []
        if not self.enabled or not self.game_enabled:
            return applied
        for t in tasks or []:
            action = str(t.get("action") or "add").lower()
            title = str(t.get("title") or "").strip()
            if not title:
                continue
            if action == "complete":
                done = self._complete_task(title, self.game_id,
                                           _keywords(t.get("keywords")))
                if done:
                    applied.append({"action": "complete", "title": done})
                continue
            added = self._add_task(title, str(t.get("detail") or ""),
                                   self.game_id)
            if added:
                applied.append({"action": "add", "title": added})
        if applied:
            self.index_pending()
        return applied

    # ── 总结落库 ──────────────────────────────────────
    @staticmethod
    def _resolve_target(target: str, recent: set[str]) -> str | None:
        target = str(target or "").strip()
        if not target:
            return None
        if target in recent:
            return target
        hits = [x for x in recent if x.startswith(target)] if len(target) >= 6 else []
        return hits[0] if len(hits) == 1 else None

    def commit_summary(self, payload: dict, batch: list,
                       game_id: str | None = None,
                       recent_ids: list[str] | None = None) -> list[str] | None:
        """把总结写进长期记忆，并在**同一个事务**里删掉这批历史。

        `batch` 是带 `ts` 的消息行；返回本次新增/合并的 memory id，
        失败返回 None（整批回滚、历史保留、下一轮重试）。
        `segments: []` 是合法空段，返回空列表。
        """
        payload = payload or {}
        segments = payload.get("segments")
        if not isinstance(segments, list):
            return None
        ids = [int(x["id"]) for x in (batch or [])
               if isinstance(x, dict) and x.get("id") is not None]
        if not ids:
            return None
        for seg in segments:
            if not isinstance(seg, dict) or not clean_summary(
                    seg.get("content") or ""):
                self.last_summary_error = "总结段 content 为空，整批不提交"
                return None
        stamps = [float(x.get("ts") or 0) for x in batch]
        start_ts = min(stamps) if stamps else 0.0
        end_ts = max(stamps) if stamps else 0.0
        game = payload.get("game") or {}
        if game_id is None:
            game_id = str((batch or [{}])[0].get("game_id") or "") or self.game_id
        gid = str(game_id)
        recent = {str(x) for x in (recent_ids or [])}
        changed: list[str] = []
        seen_targets: set[str] = set()
        try:
            with self.store.transaction():
                for seg in segments:
                    content = clean_summary(seg.get("content") or "")
                    op = str(seg.get("op") or "new").lower()
                    target = self._resolve_target(seg.get("target") or "", recent)
                    if op == "merge" and target:
                        if target in seen_targets:
                            op, target = "new", ""
                        else:
                            seen_targets.add(target)
                    if op == "merge" and target:
                        cur = self.store.get_memory(target)
                        if (cur and cur.get("scope") == "live"
                                and cur.get("kind") == "summary"):
                            self.store.update_memory(
                                target,
                                title=_title(content, seg.get("title")),
                                content=content,
                                keywords=_keywords(seg.get("keywords"))[:8],
                                end_ts=max(float(cur.get("end_ts") or 0), end_ts),
                                vector=None, vector_model="")
                            changed.append(target)
                            continue
                    row = self.store.add_memory(
                        scope="live", kind="summary", game_id="",
                        title=_title(content, seg.get("title")),
                        content=content,
                        keywords=_keywords(seg.get("keywords"))[:8],
                        source="summary", start_ts=start_ts, end_ts=end_ts)
                    changed.append(row["id"])
                if isinstance(game, dict) and game and self.game_enabled:
                    self._commit_game(game, gid, start_ts=start_ts,
                                      end_ts=end_ts)
                self.store.delete_messages(ids)
        except Exception as e:
            self.last_summary_error = f"总结事务失败：{type(e).__name__}: {e}"[:300]
            return None
        self.last_summary_error = None
        return changed

    def _commit_game(self, game: dict, game_id: str | None = None, *,
                     start_ts: float = 0.0, end_ts: float = 0.0) -> None:
        gid = str(game_id if game_id is not None else self.game_id)
        state = game.get("state") if isinstance(game.get("state"), dict) else {}
        cur = self.store.get_state(gid)
        st = {}
        for k in ("chapter", "scene", "location", "objective"):
            if k in state:
                st[k] = str(state.get(k) or "").strip()
        facts = json_list(cur.get("known_facts"))
        threads = json_list(cur.get("open_threads"))
        obs = json_list(cur.get("observations"))
        for x in state.get("known_facts_add") or []:
            x = str(x).strip()
            if x and x not in facts:
                facts.append(x)
        for x in state.get("open_threads_add") or []:
            x = str(x).strip()
            if x and x not in threads:
                threads.append(x)
        for x in state.get("open_threads_resolve") or []:
            x = str(x).strip()
            if x:
                threads = [t for t in threads if x not in t and t not in x]
        for x in state.get("observations_add") or []:
            x = str(x).strip()
            if x and x not in obs:
                obs.append(x)
        for x in state.get("observations_resolve") or []:
            x = str(x).strip()
            if x:
                obs = [o for o in obs if x not in o and o not in x]
        resolved = bool(state.get("open_threads_resolve")
                        or state.get("observations_resolve"))
        if facts or threads or obs or st or resolved:
            self.store.upsert_state(gid, **st, known_facts=facts,
                                    open_threads=threads, observations=obs)

        existing_beats = {
            normalize_key(x.get("content") or "")
            for x in self.store.list_memories(
                scope="game", kind="plot", game_id=gid, enabled=True, limit=500)
        }
        for b in game.get("beats") or []:
            if not isinstance(b, dict):
                continue
            text = str(b.get("text") or "").strip()
            if not text:
                continue
            if normalize_key(text) in existing_beats:
                continue
            old = str(b.get("supersedes") or "").strip()
            if old:
                # 完整 id 精确查，不受 500 行扫描窗口限制；短 id/标题才回退扫描。
                exact = self.store.get_memory(old)
                if (exact and exact.get("scope") == "game"
                        and exact.get("kind") == "plot"):
                    self.store.set_memory_enabled(old, False)
                else:
                    for x in self.store.list_memories(
                            scope="game", kind="plot", game_id=gid, limit=500):
                        if (x.get("title") == old
                                or (len(old) >= 6
                                    and x["id"].startswith(old))):
                            self.store.set_memory_enabled(x["id"], False)
            keys = _keywords(b.get("keywords"))
            for c in _list(b.get("characters")):
                if c and c not in keys:
                    keys.append(c)
            b_start = float(b.get("start_ts") or start_ts)
            b_end = float(b.get("end_ts") or end_ts)
            self.store.add_memory(
                scope="game", kind="plot", game_id=gid,
                title=_title(text, b.get("title")),
                content=text, keywords=keys, source="summary",
                start_ts=b_start, end_ts=b_end)
            existing_beats.add(normalize_key(text))

        for n in game.get("npcs") or []:
            if not isinstance(n, dict):
                continue
            name = str(n.get("name") or "").strip()
            desc = str(n.get("description") or "").strip()
            relation = str(n.get("relation") or "").strip()
            if relation and relation not in desc:
                desc = (desc + f"（关系：{relation}）").strip()
            if not name or not desc:
                continue
            aliases = _list(n.get("aliases"))
            keys = _keywords(n.get("keywords")) or [name]
            existing = []
            for x in self.store.list_memories(scope="game", kind="npc",
                                              game_id=gid, limit=500):
                names = {x.get("title") or ""} | set(x.get("aliases") or [])
                if name in names or bool(names & set(aliases)):
                    existing.append(x)
            if existing:
                self.store.update_memory(
                    existing[0]["id"], title=name, content=desc,
                    keywords=keys, aliases=aliases, game_id=gid)
            else:
                self.store.add_memory(
                    scope="game", kind="npc", game_id=gid,
                    title=name, content=desc, keywords=keys, aliases=aliases,
                    source="summary")

        for q in game.get("quests") or []:
            if not isinstance(q, dict):
                continue
            action = str(q.get("action") or "add").lower()
            title = str(q.get("title") or "").strip()
            if not title or action == "none":
                continue
            if action == "complete":
                self._complete_task(title, gid, _keywords(q.get("keywords")))
                continue
            if action == "update":
                self._update_task(title, gid,
                                  new_title=str(q.get("new_title") or ""),
                                  detail=str(q.get("detail") or ""))
                continue
            self._add_task(title, str(q.get("detail") or ""), gid)

    # ── 向量索引（后台）──────────────────────────────
    def _doc_text(self, row: dict) -> str:
        if row.get("kind") == "summary":
            return str(row.get("content") or "")
        return f"{row.get('title') or ''}\n{row.get('content') or ''}".strip()

    def suspend_indexing(self) -> None:
        """预演等只读进程暂停后台写向量，避免和 runtime 抢 SQLite 写锁。"""
        self._index_suspended = True

    def resume_indexing(self) -> None:
        self._index_suspended = False

    def index_pending(self) -> None:
        """把缺向量的 summary/plot/npc/归档任务丢给后台编码；不阻塞。"""
        embedder = self.embedder
        if not self.enabled or embedder is None or not embedder.ready:
            return
        if self._indexing or self._index_suspended:
            return
        identity = embedder.identity
        if self.store.meta_get("embedding_identity") != identity:
            self.store.meta_set("embedding_identity", identity)
        mems = self.store.pending_memory_vectors(identity, 64)
        if not mems:
            return
        self._indexing = True
        texts = [self._doc_text(x) for x in mems]

        def done(vectors) -> None:
            try:
                if vectors is None or len(vectors) != len(mems):
                    return
                self.store.set_memory_vectors(
                    [(row["id"], bytes(vec), identity, row.get("updated_at"))
                     for row, vec in zip(mems, vectors)])
            finally:
                self._indexing = False

        if embedder.submit_docs(texts, done) is None:
            self._indexing = False

    # ── 状态 ──────────────────────────────────────────
    def stats(self) -> dict:
        st = self.store.stats()
        if self.game_enabled:
            for key, kind in (("game_plot", "plot"),
                              ("game_npc", "npc"),
                              ("game_quests", "quests")):
                st[key] = len(self.store.list_memories(
                    scope="game", kind=kind, game_id=self.game_id,
                    enabled=True, limit=5000))
        return {
            **st,
            "enabled": self.enabled,
            "summaries": st.get("live_summaries", 0),
            "summary_error": self.last_summary_error,
            "recall_error": self.last_recall_error,
            "last_recall": self.last_recall,
            "recall_report": self.last_recall_report,
            "game_id": self.game_id,
            "tasks_active": len(self._all_tasks()),
            "chapter_error": getattr(self, "last_chapter_error", None),
            "limits": {"recall_recent": self.recent_live,
                       "recall_episodes": self.recall_episodes},
            "embedding": (self.embedder.stats()
                          if self.embedder is not None else None),
            "embedding_identity": self.store.meta_get("embedding_identity"),
            "db": str(self.store.path),
            "schema_version": self.store.schema_version,
        }

    def close(self) -> None:
        self.store.close()


class MemoryFacade:
    """WebUI 用的记忆入口：关闭记忆时不打开 SQLite。"""

    def __init__(self, cfg: dict, root: str | Path | None = None) -> None:
        enabled = bool(cfg["memory"]["enabled"])
        self.memory = Memory(cfg, root) if enabled else None

    @property
    def store(self):
        return self.memory.store if self.memory is not None else None

    def stats(self) -> dict:
        if self.memory is not None:
            return self.memory.stats()
        st = empty_stats()
        st.update({"summary_error": None, "last_recall": [],
                   "recall_report": {}, "game_id": "", "tasks_active": 0})
        return st

    def close(self) -> None:
        if self.memory is not None:
            self.memory.close()
            self.memory = None
