"""召回：query、精确关键词、向量、两档阈值、分池配额、去重。

主回复前只做一次本地 query 编码，三个池共用同一个向量；没有第二段
LLM 调用。关键词只做规范化后的精确命中（含 aliases），泛词/过短词
由 `key_ok()` 挡掉；命中个数越多分越高。

两档：
  · 强相关（sim >= recall_strong_score）或关键词命中 → 不受 k 限制，
    每池最多 HARD_MAX_ITEMS 条；
  · 弱相关（sim >= recall_min_score）→ 填满该池 k 条，不足就少给。
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field

import numpy as np

from sidekick.brain import material as material_mod
from sidekick.core.text import key_ok, normalize_key
from sidekick.memory.index import VectorIndex
from sidekick.memory.limits import (HARD_MAX_ITEMS, KEYWORD_HIT_WEIGHT)
from sidekick.memory.timefmt import format_span


def _now() -> float:
    return time.time()


def _day_start(ts: float) -> float:
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _month_start(year: int, month: int) -> float:
    return time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))


def time_window(text: str, now: float | None = None) -> tuple[float, float] | None:
    """识别时间意图并返回本地时间窗；认不出返回 None。

    支持：今天/昨天/前天/大前天/上周/这周/上个月/N天前/N周前。
    """
    t = str(text or "")
    now = _now() if now is None else float(now)
    today = _day_start(now)
    day = 86400.0
    lt = time.localtime(now)
    if "大前天" in t:
        return today - 3 * day, today - 2 * day
    if "前天" in t:
        return today - 2 * day, today - day
    if "昨天" in t:
        return today - day, today
    if "今天" in t:
        return today, now
    if "上个月" in t:
        y, m = (lt.tm_year, lt.tm_mon - 1) if lt.tm_mon > 1 else (lt.tm_year - 1, 12)
        return _month_start(y, m), _month_start(lt.tm_year, lt.tm_mon)
    weekday = lt.tm_wday  # Monday=0
    this_week = today - weekday * day
    if "上周" in t:
        return this_week - 7 * day, this_week
    if "这周" in t:
        return this_week, now
    m = re.search(r"(\d+)\s*天前", t)
    if m:
        n = max(0, int(m.group(1)))
        return today - n * day, today - (n - 1) * day if n else now
    m = re.search(r"(\d+)\s*周前", t)
    if m:
        n = max(0, int(m.group(1)))
        return this_week - n * 7 * day, this_week - (n - 1) * 7 * day if n else now
    return None


def _ascii_word(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]*", text or ""))


def keyword_hits(text: str, keys: list[str], min_len: int = 2) -> list[str]:
    """返回命中的不同关键词（不按出现次数）。"""
    compact = normalize_key(text)
    words = unicodedata.normalize("NFKC", str(text or "")).lower()
    words = re.sub(r"[^\w\u4e00-\u9fff]+", " ", words)
    words = re.sub(r"\s+", " ", words).strip()
    hits: list[str] = []
    for key in dict.fromkeys(str(k).strip() for k in (keys or [])):
        if not key or not key_ok(key, min_len):
            continue
        if _ascii_word(key):
            if any(ch in key for ch in ".-"):
                # normalize_key 会去掉 -/.；S-01 与 S01 视为同一个词。
                nk = normalize_key(key)
                if nk and nk in compact:
                    hits.append(key)
                continue
            pattern = rf"(?<![a-z0-9]){re.escape(key.lower())}(?![a-z0-9])"
            if re.search(pattern, words):
                hits.append(key)
        else:
            nk = normalize_key(key)
            if nk and nk in compact:
                hits.append(key)
    return hits


def build_query(text: str, history: list[dict], label: str = "") -> str:
    """本轮 query：最近 2 条用户发言；太短时补上一句/上一条回复。"""
    lines = material_mod.user_lines(text, label)
    current = "\n".join(lines[-2:]).strip()
    msgs = list(history or [])
    if not current:
        last_assistant = next((str(m.get("content") or "") for m in reversed(msgs)
                               if m.get("role") == "assistant"), "")
        audience = material_mod.audience_lines(text)
        if not last_assistant and not audience:
            raw = str(text or "").strip()
            return raw if raw and "[" not in raw else ""
        return "\n".join(x for x in (last_assistant, audience[-1] if audience else "")
                         if x).strip()
    if len(normalize_key(current)) < 6:
        prev = next((str(m.get("content") or "") for m in reversed(msgs)
                     if m.get("role") == "user"), "")
        if prev:
            # 历史 user 是「[材料摘要] + 段名」：只留用户真正说过的话，
            # 别把上一轮的弹幕/画面/冷场当成这一轮的 query。
            kept: list[str] = []
            for line in prev.splitlines():
                body = line.strip()
                if not body or body.startswith("["):
                    continue
                if body.startswith(("现在：", "弹幕：", "画面：", "冷场：")):
                    continue
                if label and body.startswith(f"{label}："):
                    body = body[len(label) + 1:].strip()
                if body:
                    kept.append(body)
            prev = "\n".join(kept)
            if prev.strip():
                current = prev.strip() + "\n" + current
        if len(normalize_key(current)) < 6:
            last_assistant = next((str(m.get("content") or "") for m in reversed(msgs)
                                   if m.get("role") == "assistant"), "")
            if last_assistant:
                current = current + "\n" + last_assistant
    return current.strip()


def wants_all_tasks(text: str) -> bool:
    body = str(text or "")
    return any(x in body for x in (
        "所有任务", "全部任务", "任务列表", "任务清单", "还有什么任务",
        "还有哪些任务", "哪些任务", "待办", "我还能做什么"))


@dataclass
class RecallItem:
    source: str                 # memory | worldbook
    kind: str                   # summary | plot | npc | quest | worldbook
    id: str
    title: str
    content: str
    score: float
    tier: str                   # strong | weak
    channel: str                # vector | keyword | both
    sim: float = 0.0
    hits: list[str] = field(default_factory=list)
    start_ts: float = 0.0
    end_ts: float = 0.0
    priority: int = 0

    def as_dict(self) -> dict:
        return {"source": self.source, "kind": self.kind, "id": self.id,
                "title": self.title, "score": round(self.score, 4),
                "tier": self.tier, "channel": self.channel,
                "sim": round(self.sim, 4), "keys": list(self.hits),
                "start_ts": self.start_ts, "end_ts": self.end_ts}


@dataclass
class RecallResult:
    episodes: list[RecallItem] = field(default_factory=list)
    game: list[RecallItem] = field(default_factory=list)
    worldbook: list[RecallItem] = field(default_factory=list)
    query: str = ""
    report: dict = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.episodes or self.game or self.worldbook)

    def all_items(self) -> list[RecallItem]:
        return [*self.episodes, *self.game, *self.worldbook]


class RecallEngine:
    def __init__(self, cfg: dict, memory, worldbook, worldbook_index,
                 embedder, profile=None) -> None:
        self.cfg = cfg
        self.memory = memory
        self.worldbook = worldbook
        self.worldbook_index = worldbook_index
        self.embedder = embedder
        self.profile = profile
        self.index = VectorIndex(memory.store, memory, embedder)
        self._read_cfg(cfg)
        self.last_report: dict = {}
        self._prepared_text = ""
        self._prepared_vector = None
        self._prepared = False

    def _read_cfg(self, cfg: dict) -> None:
        m = cfg["memory"]
        self.k_episodes = int(m["recall_episodes"])
        self.k_game = int(m["recall_game"])
        self.k_worldbook = int(m["recall_worldbook"])
        self.recent = int(m["recall_recent"])
        self.min_score = float(m["recall_min_score"])
        self.strong_score = float(m["recall_strong_score"])
        self.min_keyword_len = int(m["min_keyword_len"])

    def reload(self, cfg: dict) -> None:
        self.cfg = cfg
        self._read_cfg(cfg)
        self._prepared = False
        self._prepared_text = ""
        self._prepared_vector = None
        self.index.invalidate()

    # ── query 预热 ────────────────────────────────────
    def _label(self, label: str | None) -> str:
        if label is not None:
            return str(label)
        return str(getattr(self.profile, "name", "") or "")

    @property
    def degraded(self) -> str | None:
        return getattr(self.embedder, "last_degraded", None)

    async def prepare(self, text: str, history: list[dict],
                      label: str | None = None) -> str:
        query = build_query(text, history, self._label(label))
        self._prepared_text = query
        self._prepared_vector = None
        self._prepared = True
        if query:
            self._prepared_vector = await self.embedder.query(query)
        # query 先编码；文档补索引排在后面，避免一次长文档编码拖慢本轮。
        try:
            self.memory.index_pending()
            if self.worldbook_index is not None:
                self.worldbook_index.sync()
        except Exception:
            pass
        return query

    # ── 主入口 ────────────────────────────────────────
    def run(self, text: str, history: list[dict], chapter: str = "",
            label: str | None = None) -> RecallResult:
        if not getattr(self.memory, "enabled", False):
            return RecallResult()
        built = build_query(text, history, self._label(label))
        if self._prepared and built == self._prepared_text:
            query = self._prepared_text
            vector = self._prepared_vector
        else:
            query = built
            vector = None
            self._prepared_vector = None
        # 关键词打分统一读 `_prepared_text`；同步/预览路径也要先对齐。
        self._prepared_text = query
        report = {"query": query, "degraded": self.degraded,
                  "pools": {}, "time_intent": None, "time_fallback": False}
        if not query:
            self.last_report = report
            self.memory.last_recall_report = report
            return RecallResult(report=report)

        window = time_window(query)
        report["time_intent"] = (format_span(window[0], window[1])
                                 if window else None)
        episodes = self._collect_episodes(query, vector, window, report)
        if window is not None and not episodes:
            report["time_fallback"] = True
            episodes = self._collect_episodes(query, vector, None, report,
                                              time_fallback=True)
        game = self._collect_game(query, vector, report)
        world = self._collect_worldbook(query, vector, chapter, report)

        episodes = self._dedupe_episodes(episodes)
        chosen_eps = self._select(episodes, self.k_episodes, "episodes", report)
        chosen_game = self._select(game, self.k_game, "game", report)
        chosen_world = self._select(world, self.k_worldbook, "worldbook", report)
        result = RecallResult(episodes=chosen_eps, game=chosen_game,
                              worldbook=chosen_world, query=query,
                              report=report)
        report["selected"] = [x.as_dict() for x in result.all_items()]
        self.last_report = report
        self.memory.last_recall_report = report
        return result

    # ── 各池 ──────────────────────────────────────────
    def _item(self, row: dict, pool: str, sim: float) -> RecallItem:
        kind = str(row.get("kind") or "")
        keys = list(row.get("keywords") or []) + list(row.get("aliases") or [])
        hits = keyword_hits(self._prepared_text, keys, self.min_keyword_len)
        if hits:
            tier = "strong"
            score = max(float(sim or 0),
                        self.strong_score + KEYWORD_HIT_WEIGHT * len(hits))
            channel = "both" if sim and sim >= self.min_score else "keyword"
        elif sim >= self.strong_score:
            tier, score, channel = "strong", float(sim), "vector"
        elif sim >= self.min_score:
            tier, score, channel = "weak", float(sim), "vector"
        else:
            tier, score, channel = "dropped", float(sim), "vector"
        order = float(row.get("end_ts") or row.get("updated_at")
                      or row.get("created_at") or 0)
        return RecallItem(source="memory", kind=kind, id=str(row.get("id") or ""),
                          title=str(row.get("title") or ""),
                          content=str(row.get("content") or ""),
                          score=float(score), tier=tier, channel=channel,
                          sim=float(sim or 0), hits=hits,
                          start_ts=float(row.get("start_ts") or 0),
                          end_ts=order)

    def _collect_episodes(self, query: str, vector, window, report: dict,
                          time_fallback: bool = False) -> list[RecallItem]:
        rows = self.memory.live_summaries()
        pinned = self.memory.recent_summary_ids()
        sims = self.index.sims("episodes", vector)
        items = []
        for row in rows:
            if row["id"] in pinned:
                continue
            if not time_fallback and window is not None:
                start = float(row.get("start_ts") or 0)
                end = float(row.get("end_ts") or 0)
                if not (start <= window[1] and end >= window[0]):
                    continue
            item = self._item(row, "episodes", sims.get(row["id"], 0.0))
            if item.tier != "dropped":
                items.append(item)
        self._pool_report(report, "episodes", items)
        return items

    def _collect_game(self, query: str, vector, report: dict) -> list[RecallItem]:
        rows = self.memory.game_candidates()
        skip = (self.memory.recent_plot_ids()
                | self.memory.current_npc_ids())
        sims = self.index.sims("game", vector, self.memory.game_id)
        items = []
        for row in rows:
            if row["id"] in skip:
                continue
            item = self._item(row, "game", sims.get(row["id"], 0.0))
            if item.tier != "dropped":
                items.append(item)
        self._pool_report(report, "game", items)
        return items

    def _collect_worldbook(self, query: str, vector, chapter: str,
                           report: dict) -> list[RecallItem]:
        wb = self.worldbook
        if wb is None or not getattr(wb, "enabled", False):
            return []
        entries = wb.entries_snapshot()
        allowed = {e.id for e in entries
                   if not e.always and wb.allowed(e, chapter)}
        sims = (self.worldbook_index.similarities(vector, allowed)
                if (vector is not None and self.worldbook_index is not None)
                else {})
        items = []
        for e in entries:
            if e.id not in allowed:
                continue
            row = {"id": e.id, "title": e.title, "content": e.content,
                   "keywords": e.keys, "aliases": e.aliases,
                   "priority": e.priority}
            item = self._item(row, "worldbook", sims.get(e.id, 0.0))
            item.source = "worldbook"
            item.kind = "worldbook"
            item.priority = int(e.priority or 0)
            if item.tier != "dropped":
                items.append(item)
        self._pool_report(report, "worldbook", items)
        return items

    def _pool_report(self, report: dict, pool: str, items: list[RecallItem]) -> None:
        report["pools"].setdefault(pool, {})
        report["pools"][pool].update({
            "candidates": len(items),
            "strong": sum(1 for x in items if x.tier == "strong"),
            "weak": sum(1 for x in items if x.tier == "weak"),
        })

    # ── 排序 / 配额 ───────────────────────────────────
    @staticmethod
    def _sort_key(x: RecallItem) -> tuple:
        return (-x.score, -sum(len(k) for k in x.hits),
                -x.end_ts, -x.priority, x.id)

    def _select(self, items: list[RecallItem], k: int, pool: str,
                report: dict) -> list[RecallItem]:
        info = report["pools"].setdefault(pool, {})
        if k <= 0:
            info.update({"selected": 0, "omitted": len(items)})
            return []
        strong = sorted((x for x in items if x.tier == "strong"),
                        key=self._sort_key)[:HARD_MAX_ITEMS]
        chosen = list(strong)
        if len(chosen) < k:
            weak = sorted((x for x in items if x.tier == "weak"),
                          key=self._sort_key)
            chosen += weak[: k - len(chosen)]
        info["selected"] = len(chosen)
        info["omitted"] = max(0, len(items) - len(chosen))
        info["omitted_ids"] = [x.id for x in items
                               if x not in chosen][:20]
        return chosen

    def _dedupe_episodes(self, items: list[RecallItem]) -> list[RecallItem]:
        """同一件事的新旧说法：余弦 >0.90 时保留更新的一条。"""
        vectors = self.index.vectors("episodes")
        pinned_ids = self.memory.recent_summary_ids()
        pinned_vecs = [vectors[pid] for pid in pinned_ids if pid in vectors]
        out: list[RecallItem] = []
        for item in sorted(items, key=self._sort_key):
            dup = None
            v = vectors.get(item.id)
            if (v is not None and pinned_vecs
                    and any(float(np.dot(v, pv)) > 0.90
                            for pv in pinned_vecs)):
                continue
            for kept in out:
                w = vectors.get(kept.id)
                if v is None or w is None:
                    continue
                sim = float(np.dot(v, w))
                if sim <= 0.90:
                    continue
                dup = kept
                break
            if dup is None:
                out.append(item)
                continue
            # 保留 end_ts 更晚的；一样晚保留分高的（排序已保证）。
            if item.end_ts > dup.end_ts:
                out[out.index(dup)] = item
        return out

    def status(self) -> dict:
        wb = {}
        if self.worldbook_index is not None and hasattr(self.worldbook_index,
                                                        "status"):
            try:
                wb = self.worldbook_index.status()
            except Exception as e:
                wb = {"error": f"{type(e).__name__}: {e}"}
        return {"worldbook": wb, "degraded": self.degraded,
                "last": self.last_report}
