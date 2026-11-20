"""游戏世界书：core + entries 的可插拔资料包。

设计原则：
  · 世界书是**用户编辑的资料**，不是运行期记忆；
  · core.md 常驻前缀；entries/*.md 按 keys/always 注入；
  · 只读 persona/games/<id>/core.md 和同目录 entries/；没有显式路径。
  · 常驻/关键词（always/keys）、章节/剧透由作者控制，运行时也可以改；
  · 不在这里做记忆、重要性、冷却——召回由 `sidekick/memory/recall.py` 统一打分。

目录：
  persona/games/<id>/
    core.md
    entries/*.md
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from sidekick.core import atomic
from sidekick import config as configmod
from sidekick.core.text import split_list


def _as_list(v) -> list[str]:
    return split_list(v, unique=False)


@dataclass
class Entry:
    id: str
    title: str
    content: str
    keys: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    always: bool = False
    chapter: str = ""
    spoiler: bool = False
    priority: int = 0

    @classmethod
    def from_file(cls, p: Path) -> "Entry":
        meta, content = _front_matter(p.read_text(encoding="utf-8"))
        return cls(
            # id 就是文件名：WebUI 的编辑/删除都按文件名拼 URL，
            # front matter 里的 id 只作为旧数据保留，不参与寻址。
            id=p.stem,
            title=str(meta.get("title") or p.stem).strip(),
            content=content,
            keys=_as_list(meta.get("keys")),
            aliases=_as_list(meta.get("aliases")),
            always=bool(meta.get("always", False)),
            chapter=str(meta.get("chapter") or ""),
            spoiler=bool(meta.get("spoiler", False)),
            priority=_as_int(meta.get("priority")),
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title,
            "keys": list(self.keys), "aliases": list(self.aliases),
            "always": self.always, "chapter": self.chapter,
            "spoiler": self.spoiler, "priority": self.priority,
            "content": self.content,
        }


def _front_matter(text: str) -> tuple[dict, str]:
    text = str(text or "").lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text.strip()
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return {}, text.strip()
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"front matter 不是合法 YAML：{e}") from e
    return (meta if isinstance(meta, dict) else {}), "\n".join(lines[end + 1:]).strip()


def _chapter_num(v) -> int | None:
    m = re.search(r"\d+", str(v or ""))
    return int(m.group(0)) if m else None


def _as_int(v, default: int = 0) -> int:
    if v is None or str(v).strip() == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def read_entry(path: str | Path) -> dict | None:
    """WebUI 用：读取一个 entry；读不到/解析不了就跳过。"""
    p = Path(path)
    try:
        return Entry.from_file(p).as_dict()
    except (OSError, UnicodeError, ValueError):
        return None


def write_entry(path: str | Path, *, title: str, content: str,
                keys: list[str] | None = None, aliases: list[str] | None = None,
                always: bool = False, chapter: str = "",
                spoiler: bool = False, priority: int = 0) -> dict:
    """WebUI 用：写一个 entry；返回读回的结果。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "title": title or p.stem,
        "keys": list(keys or []), "aliases": list(aliases or []),
        "always": bool(always), "chapter": str(chapter or ""),
        "spoiler": bool(spoiler), "priority": int(priority or 0),
    }
    body = "---\n" + yaml.safe_dump(meta, allow_unicode=True,
                                    sort_keys=False).strip() + "\n---\n\n" + str(content or "").strip() + "\n"
    atomic.write_text(p, body)
    return read_entry(p) or {}


class Worldbook:
    def __init__(self, cfg: dict, root: str | Path | None = None) -> None:
        self.cfg = cfg or {}
        self.root = Path(root) if root is not None else Path.cwd()
        self.enabled = False
        self.id = ""
        self.path: Path | None = None
        self.error: str | None = None
        self._core = ""
        self._entries: list[Entry] = []
        self._fingerprint: tuple = ()
        self.reload(cfg)

    # ── 路径 ──────────────────────────────────────────
    def _resolve(self, cfg: dict) -> Path | None:
        g = cfg["game"]
        gid = str(g.get("id") or "").strip()
        if not gid:
            return None
        gid = gid.replace("\\", "/").split("/")[-1].strip()
        if not gid:
            return None
        base = self.root / "persona" / "games"
        d = base / gid
        return d if d.is_dir() else None

    def _fp(self) -> tuple:
        if self.path is None:
            return ()
        files: list[Path] = [self.path / "core.md"]
        files += (sorted((self.path / "entries").glob("*.md"))
                  if (self.path / "entries").is_dir() else [])
        out = []
        for p in files:
            try:
                st = p.stat()
                out.append((str(p), st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((str(p), 0, 0))
        return tuple(out)

    def reload(self, cfg: dict | None = None) -> None:
        if cfg is not None:
            self.cfg = cfg
        g = self.cfg["game"]
        self.enabled = bool(g["enabled"])
        self.id = configmod.resolve_game_id(self.cfg)
        self.path = self._resolve(self.cfg) if self.enabled else None
        self.error = (f"游戏资料不存在：{self.id}"
                      if self.enabled and self.id and self.path is None else None)
        self._fingerprint = ()
        self._core = ""
        self._entries = []
        self._read()

    # ── 读取 ──────────────────────────────────────────
    def _read(self) -> None:
        if not self.enabled or self.path is None:
            return
        if not self.path.is_dir():
            self._core = ""
            self._entries = []
            self.error = f"游戏资料不存在：{self.id}"
            return
        fp = self._fp()
        if fp == self._fingerprint:
            return
        self._fingerprint = fp
        self.error = None
        try:
            self._read_dir()
        except (OSError, UnicodeError) as e:
            self._core = ""
            self._entries = []
            self.error = f"世界书读不到：{type(e).__name__}: {e}"

    def _read_dir(self) -> None:
        assert self.path is not None
        core = self.path / "core.md"
        try:
            self._core = (core.read_text(encoding="utf-8").strip()
                          if core.is_file() else "")
        except (OSError, UnicodeError) as e:
            self._core = ""
            self.error = f"core.md 读不到：{type(e).__name__}: {e}"
        entries: list[Entry] = []
        entry_errors: list[str] = []
        edir = self.path / "entries"
        if edir.is_dir():
            for p in sorted(edir.glob("*.md")):
                try:
                    entry = Entry.from_file(p)
                except (OSError, UnicodeError, ValueError) as e:
                    reason = " ".join(str(e).split())[:120]
                    entry_errors.append(f"{p.name}: {type(e).__name__}: {reason}")
                    continue
                if entry.content:
                    entries.append(entry)
        self._entries = entries
        if entry_errors:
            msg = "；".join(entry_errors[:3])
            more = len(entry_errors) - 3
            if more > 0:
                msg += f"；另有 {more} 个坏条目"
            self.error = f"{self.error}；{msg}" if self.error else msg

    # ── 对外 ──────────────────────────────────────────
    def core_text(self, *, refresh: bool = True) -> str:
        if refresh:
            self._read()
        return self._core

    def stats(self, *, refresh: bool = True) -> dict:
        if refresh:
            self._read()
        return {"enabled": self.enabled, "id": self.id,
                "path": str(self.path) if self.path else "",
                "core_chars": len(self._core), "entries": len(self._entries),
                "error": self.error}

    def allowed(self, e: Entry, chapter: str) -> bool:
        """章节/剧透过滤；`always` 也会用它。"""
        if not e.spoiler:
            return True
        cur = _chapter_num(chapter)
        ent = _chapter_num(e.chapter)
        if cur is None or ent is None:
            return False
        return ent <= cur

    def entries_snapshot(self) -> list[Entry]:
        """当前世界书的条目快照；给关键词/向量索引用，不在这里打分。"""
        self._read()
        return list(self._entries)

    def always_entries(self, chapter: str = "") -> list[dict]:
        self._read()
        out = []
        for e in self._entries:
            if e.always and self.allowed(e, chapter):
                out.append(self._entry_dict(e, 0, []))
        return out

    def _entry_dict(self, e: Entry, score: int, matched: list[str]) -> dict:
        return {"kind": "worldbook", "id": e.id, "title": e.title,
                "content": e.content, "score": score, "keys": matched,
                "always": e.always, "chapter": e.chapter,
                "priority": e.priority}
