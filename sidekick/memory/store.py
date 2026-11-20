"""记忆库：SQLite 存储层。

这里只负责“存/取”，不负责拼 prompt、不负责调模型、不负责算召回。
长期库只存总结与游戏记忆；热窗 messages 在总结后删除。

单库 `state/deepsidekick.db`：`game_id` 隔离游戏，`scope` 区分直播/游戏。
SQLite WAL 下多 ATTACH 库的事务只对每个库原子，不是整体原子；
总结要同时写记忆、写游戏、删历史，所以必须共用一个连接/一个库。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    role    TEXT NOT NULL,
    content TEXT NOT NULL,
    ts      REAL NOT NULL DEFAULT 0,
    game_id TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS memories (
    id           TEXT PRIMARY KEY,
    scope        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    game_id      TEXT DEFAULT '',
    title        TEXT DEFAULT '',
    content      TEXT NOT NULL,
    keywords     TEXT DEFAULT '[]',
    aliases      TEXT DEFAULT '[]',
    enabled      INTEGER DEFAULT 1,
    source       TEXT DEFAULT '',
    vector       BLOB,
    vector_model TEXT DEFAULT '',
    start_ts     REAL DEFAULT 0,
    end_ts       REAL DEFAULT 0,
    last_used_at REAL DEFAULT 0,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, enabled);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(scope, kind, enabled);
CREATE INDEX IF NOT EXISTS idx_memories_game ON memories(game_id, scope, enabled);

CREATE TABLE IF NOT EXISTS worldbook_vectors (
    game_id      TEXT NOT NULL,
    entry_id     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    model        TEXT NOT NULL,
    vector       BLOB NOT NULL,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (game_id, entry_id)
);

CREATE TABLE IF NOT EXISTS game_state (
    game_id        TEXT PRIMARY KEY,
    chapter        TEXT DEFAULT '',
    scene          TEXT DEFAULT '',
    location       TEXT DEFAULT '',
    objective      TEXT DEFAULT '',
    known_facts    TEXT DEFAULT '[]',
    open_threads   TEXT DEFAULT '[]',
    observations   TEXT DEFAULT '[]',
    tasks          TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# 开发期 schema 固定 v1；同版本但旧结构也要拒绝，避免“能打开、写不进去”。
SCHEMA_COLUMNS = {
    "messages": {"id", "role", "content", "ts", "game_id"},
    "memories": {"id", "scope", "kind", "game_id", "title", "content",
                 "keywords", "aliases", "enabled", "source", "vector",
                 "vector_model", "start_ts", "end_ts", "last_used_at",
                 "created_at", "updated_at"},
    "worldbook_vectors": {"game_id", "entry_id", "content_hash", "model",
                          "vector", "updated_at"},
    "game_state": {"game_id", "chapter", "scene", "location", "objective",
                   "known_facts", "open_threads", "observations", "tasks"},
    "meta": {"key", "value"},
}


def _now() -> float:
    return time.time()


SCHEMA_VERSION = 1


def _j(v) -> str:
    return json.dumps(v if v is not None else [], ensure_ascii=False)


def _loads(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def empty_stats() -> dict:
    return {"enabled": False, "messages": 0, "live_summaries": 0,
            "game_plot": 0, "game_npc": 0, "game_quests": 0,
            "schema_version": 0, "chapter_error": None}


class MemoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._in_tx = False
        self._closed = False
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                    timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            tables = {str(r[0]) for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
            if tables and version != SCHEMA_VERSION:
                self.conn.close()
                raise RuntimeError(
                    f"记忆库 schema v{version} 与代码 v{SCHEMA_VERSION} 不匹配；"
                    f"备份并删除 {self.path} 后重启（本项目不做数据库迁移）")
            self.conn.executescript(SCHEMA)
            for table, expected in SCHEMA_COLUMNS.items():
                actual = {str(r[1]) for r in self.conn.execute(
                    f"PRAGMA table_info({table})")}
                if actual != expected:
                    self.conn.close()
                    raise RuntimeError(
                        f"记忆库 {table} 结构与代码 v{SCHEMA_VERSION} 不匹配；"
                        f"请删除 {self.path} 后重启（本项目不做数据库迁移）")
            if version != SCHEMA_VERSION:
                self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.schema_version = SCHEMA_VERSION
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA busy_timeout=8000")
            self.conn.commit()

    def _commit(self) -> None:
        """事务内不提交，交给 transaction() 一次提交。"""
        if not self._in_tx:
            self.conn.commit()

    @contextmanager
    def transaction(self):
        """把一组写操作合成一个事务：要么全落库，要么全回滚。"""
        with self._lock:
            if self._in_tx:
                yield
                return
            self._in_tx = True
            try:
                # IMMEDIATE：先拿写锁再读，避免两个进程各自读到旧快照后互相覆盖。
                self.conn.execute("BEGIN IMMEDIATE")
                yield
                self.conn.commit()
            except Exception:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                self._in_tx = False

    # ── messages ──────────────────────────────────────
    def add_message(self, role: str, content: str,
                    ts: float | None = None, game_id: str = "") -> dict:
        stamp = float(ts if ts is not None else _now())
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO messages(role,content,ts,game_id) VALUES(?,?,?,?)",
                (str(role), str(content), stamp, str(game_id or "")))
            self._commit()
            mid = int(cur.lastrowid)
        return {"id": mid, "role": str(role), "content": str(content),
                "ts": stamp, "game_id": str(game_id or "")}

    def update_message(self, mid: int, content: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE messages SET content=? WHERE id=?",
                              (str(content), int(mid)))
            self._commit()

    def delete_messages(self, ids: Iterable[int]) -> int:
        ids = [int(x) for x in ids]
        if not ids:
            return 0
        q = ",".join("?" for _ in ids)
        with self._lock:
            cur = self.conn.execute(f"DELETE FROM messages WHERE id IN ({q})", ids)
            self._commit()
            return int(cur.rowcount or 0)

    def messages(self, limit: int | None = None, *,
                 oldest: bool = False) -> list[dict]:
        with self._lock:
            if limit:
                order = "ASC" if oldest else "DESC"
                rows = self.conn.execute(
                    f"SELECT * FROM messages ORDER BY id {order} LIMIT ?",
                    (int(limit),)).fetchall()
                if not oldest:
                    rows = list(reversed(rows))
            else:
                rows = self.conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
        return [{"id": int(r["id"]), "role": r["role"], "content": r["content"],
                 "ts": float(r["ts"] or 0), "game_id": r["game_id"] or ""}
                for r in rows]

    def count_messages(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    # ── 版本 ──────────────────────────────────────────
    def vector_version(self) -> tuple[int, int]:
        """向量缓存签名：SQLite 版本 + 持久化 epoch。"""
        with self._lock:
            try:
                other = int(self.conn.execute("PRAGMA data_version").fetchone()[0])
            except sqlite3.Error:
                other = 0
            epoch = int(self.meta_get("vector_epoch", "0") or 0)
            return other, epoch

    def _bump_vector_epoch(self) -> None:
        with self._lock:
            nxt = int(self.meta_get("vector_epoch", "0") or 0) + 1
            self.meta_set("vector_epoch", str(nxt))

    # ── memories ──────────────────────────────────────
    def add_memory(self, *, scope: str, kind: str, content: str,
                   title: str = "", keywords: list[str] | None = None,
                   game_id: str = "", source: str = "",
                   aliases: list[str] | None = None,
                   start_ts: float = 0, end_ts: float = 0,
                   created_at: float | None = None) -> dict:
        mid = uuid.uuid4().hex
        now = _now()
        with self._lock:
            self.conn.execute(
                "INSERT INTO memories(id,scope,kind,game_id,title,content,"
                "keywords,aliases,enabled,source,start_ts,end_ts,created_at,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, scope, kind, game_id, title, content, _j(keywords or []),
                 _j(aliases or []), 1, source, float(start_ts or 0),
                 float(end_ts or 0), float(created_at or now), now))
            self._commit()
        return self.get_memory(mid) or {}

    def get_memory(self, mid: str) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM memories WHERE id=?",
                                    (mid,)).fetchone()
        return self._row_memory(row) if row else None

    def update_memory(self, mid: str, **fields) -> None:
        allowed = {"title", "content", "keywords", "scope", "kind", "game_id",
                   "source", "aliases", "vector", "vector_model", "start_ts",
                   "end_ts", "last_used_at"}
        written = {k: v for k, v in fields.items() if k in allowed}
        if not written:
            return
        # 内容/关键词一变，旧向量就不再对应当前文本；调用方没显式给新向量时清空。
        if ("vector" not in written and "vector_model" not in written
                and any(k in written for k in
                        ("title", "content", "keywords", "aliases",
                         "scope", "kind"))):
            written["vector"] = None
            written["vector_model"] = ""
        sets, vals = [], []
        for k, v in written.items():
            if k in ("keywords", "aliases"):
                v = _j(v)
            elif k == "vector" and v is not None:
                v = sqlite3.Binary(bytes(v))
            sets.append(f"{k}=?")
            vals.append(v)
        sets.append("updated_at=?")
        vals.append(_now())
        vals.append(mid)
        with self._lock:
            self.conn.execute(f"UPDATE memories SET {','.join(sets)} WHERE id=?", vals)
            self._commit()
            self._bump_vector_epoch()

    def set_memory_enabled(self, mid: str, enabled: bool) -> None:
        """内部专用：supersede 等流程改启用状态；WebUI 的 PUT 不开放这个字段。"""
        with self._lock:
            self.conn.execute(
                "UPDATE memories SET enabled=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, _now(), mid))
            self._commit()

    def mark_memories_used(self, ids: Iterable[str], when: float | None = None) -> None:
        ids = [str(x) for x in ids if x]
        if not ids:
            return
        stamp = float(when if when is not None else _now())
        q = ",".join("?" for _ in ids)
        with self._lock:
            self.conn.execute(f"UPDATE memories SET last_used_at=? WHERE id IN ({q})",
                              [stamp, *ids])
            # last_used_at 不属于向量/正文，不能让召回矩阵失效。
            self.conn.commit()

    def delete_memory(self, mid: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM memories WHERE id=?", (mid,))
            self._commit()
            self._bump_vector_epoch()

    def list_memories(self, *, scope: str | None = None, kind: str | None = None,
                      game_id: str | None = None, enabled: bool | None = None,
                      limit: int = 500) -> list[dict]:
        where, vals = [], []
        if scope:
            where.append("scope=?")
            vals.append(scope)
        if kind:
            where.append("kind=?")
            vals.append(kind)
        if game_id is not None:
            where.append("game_id=?")
            vals.append(game_id)
        if enabled is not None:
            where.append("enabled=?")
            vals.append(1 if enabled else 0)
        q = "SELECT * FROM memories"
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY created_at DESC LIMIT ?"
        vals.append(int(limit))
        with self._lock:
            rows = self.conn.execute(q, vals).fetchall()
        return [self._row_memory(r) for r in rows]

    def recent_summaries(self, limit: int = 5) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE scope='live' AND kind='summary' "
                "AND enabled=1 ORDER BY end_ts DESC, created_at DESC, id DESC "
                "LIMIT ?", (int(limit),)).fetchall()
        return [self._row_memory(r) for r in rows]

    def memory_vectors(self, *, scope: str, kind: str,
                       game_id: str | None = None,
                       model: str | None = None) -> list[dict]:
        q = ("SELECT id, vector FROM memories WHERE scope=? AND kind=? "
             "AND enabled=1 AND vector IS NOT NULL")
        vals: list[Any] = [scope, kind]
        if game_id is not None:
            q += " AND game_id=?"
            vals.append(game_id)
        if model is not None:
            q += " AND vector_model=?"
            vals.append(str(model))
        with self._lock:
            rows = self.conn.execute(q, vals).fetchall()
        return [{"id": r["id"], "vector": bytes(r["vector"])} for r in rows]

    def pending_memory_vectors(self, identity: str, limit: int = 64) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE enabled=1 AND "
                "(vector IS NULL OR vector_model<>?) AND "
                "((scope='live' AND kind='summary') OR "
                "(scope='game' AND kind IN ('plot','npc','quests'))) "
                "ORDER BY updated_at LIMIT ?", (str(identity), int(limit))
            ).fetchall()
        return [self._row_memory(r) for r in rows]

    def set_memory_vectors(
            self, items: list[tuple[str, bytes, str, float | None]]) -> None:
        """写回编码结果；带 expected_updated_at 时只写未被编辑过的行。"""
        if not items:
            return
        with self._lock:
            with self.transaction():
                for mid, vector, model, expected in items:
                    if expected is None:
                        self.conn.execute(
                            "UPDATE memories SET vector=?, vector_model=? "
                            "WHERE id=?",
                            (sqlite3.Binary(bytes(vector)), str(model),
                             str(mid)))
                    else:
                        self.conn.execute(
                            "UPDATE memories SET vector=?, vector_model=? "
                            "WHERE id=? AND updated_at=?",
                            (sqlite3.Binary(bytes(vector)), str(model),
                             str(mid), float(expected)))
                self._bump_vector_epoch()

    def _row_memory(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        d["keywords"] = [str(x) for x in _loads(d.get("keywords"), [])
                         if str(x).strip()]
        d["aliases"] = [str(x) for x in _loads(d.get("aliases"), [])
                        if str(x).strip()]
        d["enabled"] = bool(d.get("enabled"))
        d["has_vector"] = d.get("vector") is not None
        d.pop("vector", None)
        return d

    # ── worldbook vectors ─────────────────────────────
    def worldbook_vectors(self, game_id: str, model: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT entry_id, content_hash, vector FROM worldbook_vectors "
                "WHERE game_id=? AND model=?", (str(game_id), str(model))).fetchall()
        return [{"entry_id": r["entry_id"], "content_hash": r["content_hash"],
                 "vector": bytes(r["vector"])} for r in rows]

    def worldbook_hashes(self, game_id: str) -> dict[str, dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT entry_id, content_hash, model FROM worldbook_vectors "
                "WHERE game_id=?", (str(game_id),)).fetchall()
        return {r["entry_id"]: {"content_hash": r["content_hash"],
                                "model": r["model"]} for r in rows}

    def upsert_worldbook_vector(self, *, game_id: str, entry_id: str,
                                content_hash: str, model: str,
                                vector: bytes) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO worldbook_vectors(game_id,entry_id,content_hash,"
                "model,vector,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(game_id,entry_id) DO UPDATE SET "
                "content_hash=excluded.content_hash,model=excluded.model,"
                "vector=excluded.vector,updated_at=excluded.updated_at",
                (game_id, entry_id, content_hash, model,
                 sqlite3.Binary(bytes(vector)), _now()))
            self._commit()
            self._bump_vector_epoch()

    def delete_worldbook_vector(self, game_id: str, entry_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM worldbook_vectors WHERE game_id=? AND entry_id=?",
                (str(game_id), str(entry_id)))
            self._commit()
            self._bump_vector_epoch()

    # ── game state ────────────────────────────────────
    def get_state(self, game_id: str) -> dict:
        with self._lock:
            row = self.conn.execute("SELECT * FROM game_state WHERE game_id=?",
                                    (game_id,)).fetchone()
        return dict(row) if row else {
            "game_id": game_id, "chapter": "", "scene": "", "location": "",
            "objective": "", "known_facts": "[]", "open_threads": "[]",
            "observations": "[]", "tasks": "[]"}

    def upsert_state(self, game_id: str, **fields) -> None:
        with self.transaction():
            cur = self.get_state(game_id)
            chapter = str(fields.get("chapter", cur.get("chapter") or "")).strip()
            scene = str(fields.get("scene", cur.get("scene") or "")).strip()
            location = str(fields.get("location", cur.get("location") or "")).strip()
            objective = str(fields.get("objective", cur.get("objective") or "")).strip()
            known = fields.get("known_facts", _loads(cur.get("known_facts"), []))
            threads = fields.get("open_threads", _loads(cur.get("open_threads"), []))
            obs = fields.get("observations", _loads(cur.get("observations"), []))
            tasks = fields.get("tasks", _loads(cur.get("tasks"), []))
            self.conn.execute(
                "INSERT INTO game_state(game_id,chapter,scene,location,objective,"
                "known_facts,open_threads,observations,tasks) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(game_id) DO UPDATE SET "
                "chapter=excluded.chapter,scene=excluded.scene,"
                "location=excluded.location,objective=excluded.objective,"
                "known_facts=excluded.known_facts,open_threads=excluded.open_threads,"
                "observations=excluded.observations,tasks=excluded.tasks",
                (game_id, chapter, scene, location, objective,
                 _j(known), _j(threads), _j(obs), _j(tasks)))
            self._commit()

    # ── meta ──────────────────────────────────────────
    def meta_get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?",
                                    (str(key),)).fetchone()
        return str(row["value"]) if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), str(value)))
            self._commit()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.conn.close()
            except Exception:
                pass

    # ── 统计 ──────────────────────────────────────────
    def stats(self) -> dict[str, int]:
        with self._lock:
            def one(q, args=()):
                return int(self.conn.execute(q, args).fetchone()[0])
            return {
                "messages": one("SELECT COUNT(*) FROM messages"),
                "live_summaries": one(
                    "SELECT COUNT(*) FROM memories WHERE scope='live' "
                    "AND kind='summary' AND enabled=1"),
                "game_plot": one(
                    "SELECT COUNT(*) FROM memories WHERE scope='game' "
                    "AND kind='plot' AND enabled=1"),
                "game_npc": one(
                    "SELECT COUNT(*) FROM memories WHERE scope='game' "
                    "AND kind='npc' AND enabled=1"),
                "game_quests": one(
                    "SELECT COUNT(*) FROM memories WHERE scope='game' "
                    "AND kind='quests' AND enabled=1"),
                "summary_vectors": one(
                    "SELECT COUNT(*) FROM memories WHERE scope='live' "
                    "AND kind='summary' AND vector IS NOT NULL"),
            }
