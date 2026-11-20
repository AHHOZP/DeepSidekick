"""空对象：feature 关掉时用 Null，而不是到处写 `if enabled`。

Null 与“存在但失败”的适配器返回同一种结果契约（例如 `SayResult.started=False`），
由同一条兜底路径处理；这样“没装”和“坏了”共用代码，但状态可区分。
"""
from __future__ import annotations

import time
from collections import deque
from pathlib import Path

from sidekick.memory.store import empty_stats
from sidekick.core.ports import SayResult


class NullSpeaker:
    """TTS 未启用：不合成、不出声，Runtime 走 Display.auto_show。"""

    available = False
    speaking = False
    busy = False
    last_request_at = 0.0
    profile_name = "none"
    preset_name = ""
    prompt_rules = ""
    play_ref = (0.0, 0.0)
    interrupted = False

    def __init__(self, *, cps: float) -> None:
        self.cps = float(cps)

    async def say(self, text: str, acts: list[dict] | None = None, *,
                  on_event=None) -> SayResult:
        return SayResult(ok=False, started=False, text=str(text or ""),
                         reason="TTS 未启用", failed=False)

    async def play_clip(self, path: Path) -> bool:
        return False

    def snapshot(self) -> dict:
        return {"profile": self.profile_name, "preset": self.preset_name,
                "stream": False, "segments": "",
                "speaking": False, "available": False, "cps": self.cps,
                "fail_rounds": 0, "last": None, "error": None}

    def reload(self, cfg: dict) -> None:
        return None

    def stop(self) -> None:
        return None

    async def close(self) -> None:
        return None


class NullStt:
    """stt.enabled=false：麦克风仍可让路/打断，但不产生 `ME`。"""

    name = "none"
    model_size = "-"
    dead = False
    disabled = False

    def warmup(self) -> bool:
        return True

    def analyze(self, pcm, sr):
        from sidekick.input.stt.engines.base import AsrResult

        return AsrResult(text="")

    def snapshot(self) -> dict:
        return {"model": self.model_size, "engine": self.name,
                "error": None, "dead": False, "disabled": False, "revivals": 0,
                "dropped_dead": 0, "queue": 0, "drops": 0}

    def stats(self) -> dict:
        return {"n": 0, "window": 0, "last_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0}

    def close(self) -> None:
        return None


class NullMic:
    enabled = False
    user_speaking = False
    speech_ms = 0.0
    last_voice_at = 0.0
    level = 0.0
    peak = 0.0
    error: str | None = None
    restarts = 0
    mode = ""
    mode_cfg = ""
    gate_percent = 100
    noise = 0.0
    noise_high = 0.0
    gate = 0.0
    gate_continue = 0.0
    barge_ms = 0.0
    barge_triggers = 0
    on_barge = None
    play_ref = None

    def start(self) -> None:
        return None

    def close(self) -> None:
        return None


class NullFrames:
    enabled = False
    last_error: str | None = None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def reload(self, cfg: dict) -> None:
        return None

    def take(self) -> list:
        return []

    def preview(self) -> list:
        return []

    def latest_snapshot(self):
        return None, 0.0

    def latest_ts(self) -> float:
        return 0.0

    def capture_now(self, timeout_s: float = 0.12) -> bool:
        return False

    def peek(self) -> int:
        return 0

    def stats(self) -> dict:
        return {"enabled": False, "mode": "active", "interval_s": 0.0,
                "resolution": "", "pending": 0, "grabbed": 0, "merged": 0,
                "max_frames": 0, "subtitle_strip": False,
                "privacy_active": False, "window_error": None, "error": None}


class NullStage:
    clients = 0

    def emit(self, event: str, **kw) -> None:
        return None

    def stats(self) -> dict:
        return {"clients": 0, "sent": 0, "last_event": "", "report": {}}


class NullEmbedder:
    """embed 关闭/记忆关闭：与真实实现同签名，永远没有向量。"""

    enabled = False
    ready = False
    state = "disabled"
    identity = ""
    model = ""
    error: str | None = None
    last_degraded: str | None = None

    def start(self) -> None:
        return None

    async def query(self, text: str, timeout_ms: int | None = None):
        return None

    def prefetch(self, text: str) -> None:
        return None

    def submit_docs(self, texts: list[str], callback=None):
        return None

    def stats(self) -> dict:
        return {"state": "disabled", "enabled": False, "model": "",
                "identity": "", "dim": 0, "threads": 0, "dir": "",
                "error": None, "last_error": None, "last_degraded": None,
                "queries": 0, "docs": 0, "last_query_ms": 0.0,
                "query_p95_ms": 0.0, "pending": 0, "cache": 0}

    def close(self) -> None:
        return None


class NullLongTerm:
    """memory.enabled=false：长期记忆全部为空，不建 SQLite。"""

    enabled = False
    last_recall: list[dict] = []
    last_summary_error: str | None = None
    last_recall_report: dict = {}
    game_id = ""

    def reload(self, cfg: dict) -> None:
        return None

    def last_identity(self) -> tuple[str, str] | None:
        return None

    def set_last_identity(self, role_id: str, user_id: str) -> None:
        return None

    def index_pending(self) -> None:
        return None

    def suspend_indexing(self) -> None:
        return None

    def resume_indexing(self) -> None:
        return None

    def recent_live_block(self) -> str:
        return ""

    def game_state_block(self, *, expand_tasks: bool = False,
                         game_id: str | None = None) -> str:
        return ""

    def recent_summary_ids(self) -> set[str]:
        return set()

    def recent_plot_ids(self) -> set[str]:
        return set()

    def current_npc_ids(self) -> set[str]:
        return set()

    def live_summaries(self) -> list[dict]:
        return []

    def game_candidates(self) -> list[dict]:
        return []

    def mark_recalled(self, candidates: list[dict]) -> None:
        return None

    def apply_tasks(self, tasks: list[dict]) -> list[dict]:
        return []

    def commit_summary(self, payload: dict, batch: list,
                       game_id: str | None = None,
                       recent_ids: list[str] | None = None):
        return None

    def current_chapter(self) -> str:
        return ""

    def stats(self) -> dict:
        return empty_stats()

    def close(self) -> None:
        return None


class InMemoryHistory:
    """memory.enabled=false 时的短期历史：只活在内存里，不碰 SQLite。"""

    def __init__(self, keep: int = 40) -> None:
        keep = max(2, int(keep))
        if keep % 2:
            keep += 1
        self.history_max = keep
        self.summarize_every = 2
        self._rows: deque[dict] = deque(maxlen=keep)
        self._next_id = 1

    def reload(self, cfg: dict) -> None:
        keep = max(2, int(cfg["memory"]["history_max"]))
        if keep % 2:
            keep += 1
        self.history_max = keep
        self._rows = deque(self._rows, maxlen=keep)

    def _add(self, role: str, content: str, game_id: str = "") -> dict:
        row = {"id": self._next_id, "role": role, "content": str(content),
               "ts": time.time(), "game_id": str(game_id or "")}
        self._next_id += 1
        self._rows.append(row)
        return row

    def messages(self) -> list[dict]:
        return [{"role": r["role"], "content": r["content"]} for r in self._rows]

    def set_summary_failures(self, count: int) -> None:
        return None

    def add_exchange(self, user: str, assistant: str,
                     game_id: str = "") -> tuple[dict, dict]:
        return self._add("user", user, game_id), self._add(
            "assistant", assistant, game_id)

    def drop_pair(self, user: dict, assistant: dict) -> None:
        ids = {x.get("id") for x in (user, assistant) if isinstance(x, dict)}
        self._rows = deque((r for r in self._rows if r["id"] not in ids),
                           maxlen=self._rows.maxlen)

    def update_assistant(self, assistant: dict, text: str) -> None:
        if not isinstance(assistant, dict):
            return
        mid = assistant.get("id")
        content = " ".join(str(text or "").split())
        for r in self._rows:
            if r["id"] == mid:
                r["content"] = content
                assistant["content"] = content
                return

    def needs_summary(self) -> bool:
        return False

    def batch(self) -> list[dict]:
        return []

    def drop_batch(self) -> None:
        return None



    def count(self) -> int:
        return len(self._rows)

    def close(self) -> None:
        self._rows.clear()


class NullAvatarActions:
    """无形象/形象层关闭：不注入规则，仍然剥掉 `<ACT>`。"""

    avatar_id = ""
    broken: list[str] = []

    def rules(self) -> str:
        return ""

    def prompt_rules(self) -> str:
        return ""

    def parse(self, text: str):
        from sidekick.avatar.act_tags import parse

        return parse(text)

    def summarize(self, acts: list[dict]) -> str:
        return ""
