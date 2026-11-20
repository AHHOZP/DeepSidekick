"""端口：orchestration 只依赖这里的协议，不依赖具体适配器。

这不是插件框架：只有现有这几个可替换部件，够用就好。
`runtime.py` 决定用哪个实现；`brain/speech/display` 只认协议。

`SayResult` / `SpeechUpdate` / `FeatureStatus` 是跨模块的值对象，
用 dataclass 而不是 dict，改字段时类型检查器和测试会先发现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(slots=True)
class SayResult:
    """一次 `Speaker.say()` 的结果。

    started 表示**第一块 PCM 真的写进了声卡**（静音自检时按已开始算），
    而不是“合成开始了”。气泡必须等它，否则会先出字后出声。
    """

    ok: bool = False
    started: bool = False
    text: str = ""
    #: 未经 TTS provider.adapt_text 的显示文本；首音前失败时气泡/历史用这份。
    display: str = ""
    spoken: str = ""
    upto: int = 1
    reason: str | None = None
    failed: bool = False
    rejected: bool = False
    #: 用户打断（barge-in）：不是故障，不道歉、不算 fail_rounds。
    interrupted: bool = False
    pages: list[str] = field(default_factory=list)
    sync: bool = False
    ms: dict | None = None


@dataclass(slots=True)
class SpeechUpdate:
    """播放过程中回给 Runtime 的事件，Runtime 再转给 Display。"""

    kind: str                       # started | page | finished
    pages: list[str] = field(default_factory=list)
    index: int = 0
    upto: int = 1
    acts: list[dict] = field(default_factory=list)
    sync: bool = False
    cps: float = 0.0
    duration: float = 0.0
    level: float = 0.0
    openness: float = 0.0
    form: float = 0.0
    voiced: bool = False


@dataclass(frozen=True)
class FeatureStatus:
    """feature 四态：disabled / unavailable / degraded / healthy。

    `enabled=false` 是用户主动关掉，不是故障；`available=false` 才是
    “打开了但模块/模型没装”，必须让人看见。
    """

    name: str
    enabled: bool = True
    available: bool = True
    healthy: bool = True
    degraded: bool = False
    error: str | None = None

    @property
    def state(self) -> str:
        if not self.enabled:
            return "disabled"
        if not self.available:
            return "unavailable"
        if self.degraded or not self.healthy:
            return "degraded"
        return "healthy"

    def to_dict(self) -> dict:
        return {
            "name": self.name, "state": self.state, "enabled": self.enabled,
            "available": self.available, "healthy": self.healthy,
            "degraded": self.degraded, "error": self.error,
        }


class Speaker(Protocol):
    available: bool
    speaking: bool
    busy: bool
    last_request_at: float
    profile_name: str
    preset_name: str
    prompt_rules: str

    async def say(self, text: str, acts: list[dict] | None = None, *,
                  on_event=None) -> SayResult: ...

    async def play_clip(self, path: Path) -> bool: ...

    def snapshot(self) -> dict: ...

    def reload(self, cfg: dict) -> None: ...

    def stop(self) -> None: ...

    async def close(self) -> None: ...


class Display(Protocol):
    cps: float

    @property
    def active(self) -> bool: ...

    def start(self, pages: list[str], acts: list[dict] | None = None,
              sync: bool = False, cps: float | None = None,
              duration: float = 0.0) -> None: ...

    def page(self, i: int, duration: float = 0.0) -> None: ...

    def finish(self, upto: int) -> None: ...

    def status(self, text: str) -> None: ...

    async def auto_show(self, text: str, acts: list[dict] | None = None) -> None: ...


class Transcriber(Protocol):
    def warmup(self) -> bool: ...
    def analyze(self, pcm, sr) -> Any: ...

    @property
    def disabled(self) -> bool: ...

    def snapshot(self) -> dict: ...

    def stats(self) -> dict: ...

    def close(self) -> None: ...


class MicSource(Protocol):
    enabled: bool
    user_speaking: bool
    speech_ms: float
    last_voice_at: float
    level: float
    peak: float
    error: str | None
    restarts: int
    mode: str                     # 实际生效的设备形态（headset / speaker）
    mode_cfg: str                 # 配置值（auto / headset / speaker）
    gate_percent: int
    noise: float                  # 估计的环境噪声底
    gate: float                   # 当前开口门限
    barge_ms: float               # 当前连续满足打断条件的毫秒数
    barge_triggers: int           # 本次运行打断次数
    on_barge: Any                 # 命中打断时的回调；组合根接到 speaker.stop

    def start(self) -> None: ...
    def close(self) -> None: ...


class FrameSource(Protocol):
    enabled: bool
    last_error: str | None

    def reload(self, cfg: dict) -> None: ...
    def take(self) -> list: ...
    def preview(self) -> list: ...
    def latest_snapshot(self): ...
    def latest_ts(self) -> float: ...
    def capture_now(self, timeout_s: float = 0.12) -> bool: ...
    def peek(self) -> int: ...
    def stats(self) -> dict: ...


class Stage(Protocol):
    def emit(self, event: str, **kw) -> None: ...

    def stats(self) -> dict: ...

    @property
    def clients(self) -> int: ...


class VoiceClock(Protocol):
    last_request_at: float
    busy: bool


class ShortTermHistory(Protocol):
    history_max: int
    summarize_every: int

    def reload(self, cfg: dict) -> None: ...
    def messages(self) -> list[dict]: ...
    def set_summary_failures(self, count: int) -> None: ...
    def add_exchange(self, user: str, assistant: str,
                     game_id: str = "") -> tuple[dict, dict]: ...
    def drop_pair(self, user: dict, assistant: dict) -> None: ...
    def update_assistant(self, assistant: dict, text: str) -> None: ...
    def needs_summary(self) -> bool: ...
    def batch(self) -> list[dict]: ...
    def drop_batch(self) -> None: ...
    def count(self) -> int: ...
    def close(self) -> None: ...


class LongTermMemory(Protocol):
    last_recall: list[dict]
    last_summary_error: str | None
    last_recall_report: dict
    game_id: str

    def reload(self, cfg: dict) -> None: ...
    def last_identity(self) -> tuple[str, str] | None: ...
    def set_last_identity(self, role_id: str, user_id: str) -> None: ...
    def index_pending(self) -> None: ...
    def recent_live_block(self) -> str: ...
    def game_state_block(self, *, expand_tasks: bool = False,
                         game_id: str | None = None) -> str: ...
    def recent_summary_ids(self) -> set[str]: ...
    def recent_plot_ids(self) -> set[str]: ...
    def current_npc_ids(self) -> set[str]: ...
    def live_summaries(self) -> list[dict]: ...
    def game_candidates(self) -> list[dict]: ...
    def mark_recalled(self, candidates: list[dict]) -> None: ...
    def apply_tasks(self, tasks: list[dict]) -> list[dict]: ...
    def commit_summary(self, payload: dict, batch: list,
                       game_id: str | None = None,
                       recent_ids: list[str] | None = None) -> list[str] | None: ...
    def current_chapter(self) -> str: ...
    def stats(self) -> dict: ...
    def close(self) -> None: ...


class Embedder(Protocol):
    ready: bool
    state: str
    identity: str
    model: str
    error: str | None
    last_degraded: str | None

    def start(self) -> None: ...

    async def query(self, text: str, timeout_ms: int | None = None): ...

    def prefetch(self, text: str) -> None: ...

    def submit_docs(self, texts: list[str], callback=None): ...

    def stats(self) -> dict: ...

    def close(self) -> None: ...


class LLM(Protocol):
    """Brain / Summary / 动作规则生成需要的模型契约。"""

    model: str
    source: str
    effort: str
    effort_dropped: bool
    ready: bool
    temp: float
    warmup_enabled: bool

    @property
    def single_system(self) -> bool: ...

    def body_defaults(self) -> dict: ...

    def request_body(self, messages: list[dict],
                     temperature: float | None = None,
                     reasoning_effort: str | None = None) -> dict: ...

    def headers(self) -> dict: ...

    async def chat(self, messages: list[dict], *,
                   temperature: float | None = None,
                   reasoning_effort: str | None = None,
                   timeout: float | None = None,
                   attempts: int) -> dict: ...

    def cache_line(self) -> str: ...

    async def close(self) -> None: ...


class AvatarActions(Protocol):
    @property
    def avatar_id(self) -> str: ...

    def rules(self) -> str: ...

    def prompt_rules(self) -> str: ...

    def parse(self, text: str): ...

    def summarize(self, acts: list[dict]) -> str: ...
