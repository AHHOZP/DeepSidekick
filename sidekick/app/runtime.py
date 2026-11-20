"""功能装配入口 + 陪玩主循环。

`sidekick/app/runtime.py` 是唯一做“启用/构造/注入”的文件；其它模块不互相 new：

  · 麦克风 / STT / 截屏 / 弹幕 / 形象层 / TTS / 记忆 的开关都在这里判断；
  · 关掉的 feature 用 `sidekick/core/null.py` 的空对象，不写 `if enabled` 到业务里；
  · 数据层（memory/worldbook/avatars）接收注入的 root；
  · 进程入口是 `python -m sidekick`（`sidekick/main.py`），WebUI 仍是独立进程。

出口路径见 `sidekick/output/reply.py`：首音事件驱动气泡，TTS 失败/缺失走 `auto_show` 兜底。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import time
from pathlib import Path

from sidekick.avatar import actions as avatar_actions
from sidekick.avatar import roles as avatar_roles
from sidekick import config
from sidekick.app import identity as identity_mod
from sidekick.app import lifecycle
from sidekick.scheduling import mode as pacing
from sidekick.output import reply
from sidekick.app import status as status_mod
from sidekick.avatar.catalog import AvatarCatalog
from sidekick.brain.brain import Brain
from sidekick.input import composer
from sidekick.input.capture import FrameQueue
from sidekick.output.display import BubbleDisplay
from sidekick.core.events import GIFT, GUARD_BUY, ME, SC, Event, EventBus
from sidekick.output.fallback import Fallback
from sidekick.memory.history import SqliteHistory
from sidekick.brain.llm import LLMClient
from sidekick.core.log import log, one_line
from sidekick.memory.long_term import Memory
from sidekick.memory.recall import RecallEngine
from sidekick.memory.store import MemoryStore
from sidekick.memory.worldbook_index import WorldbookIndex
from sidekick.memory import embed as embed_mod
from sidekick.input.mic import Mic
from sidekick.core.null import (
    InMemoryHistory,
    NullAvatarActions,
    NullEmbedder,
    NullFrames,
    NullLongTerm,
    NullMic,
    NullSpeaker,
    NullStage,
    NullStt,
)
from sidekick.core.ports import (
    AvatarActions,
    Display,
    FrameSource,
    LongTermMemory,
    MicSource,
    ShortTermHistory,
    Speaker as SpeakerPort,
    Stage,
    Transcriber,
)
from sidekick.brain.material import round_why
from sidekick.scheduling.scheduler import Scheduler
from sidekick.app.session import REFRESH_S, SessionState
from sidekick.output.speaker import Speaker
from sidekick.output.said_log import SaidLog
from sidekick.avatar.stage import StageClient
from sidekick.avatar.autonomy import store as autonomy_store
from sidekick.avatar.autonomy import validate as autonomy_validate
from sidekick.avatar.autonomy.audit import run_audit
from sidekick.motion import Context, SpeechRing, apply_overrides, profile_from_dossier
from sidekick.motion.adapters.stage import StageMotionHost
from sidekick.input.stt import make as make_stt
from sidekick.input.stt.worker import SttWorker
from sidekick.output.tts import make as make_tts
from sidekick.brain.worldbook import Worldbook


class Runtime:
    def __init__(self, *, cfg: dict, root, events: EventBus, said: SaidLog,
                 frames: FrameSource, mic: MicSource, stt: Transcriber,
                 stage: Stage, display: Display, speaker: SpeakerPort,
                 scheduler: Scheduler, brain: Brain,
                 actions: AvatarActions, catalog: AvatarCatalog | None,
                 fallback: Fallback, history: ShortTermHistory,
                 memory: LongTermMemory, session: SessionState,
                 recall=None, embedder=None,
                 stt_worker: SttWorker | None = None,
                 stage_motion=None, speech_ring=None,
                 stage_motion_error: str | None = None,
                 window=None) -> None:
        self.cfg = cfg
        self.root = root
        self.events = events
        self.said = said
        self.frames = frames
        self.mic = mic
        self.stt = stt
        self.stage = stage
        self.display = display
        self.speaker = speaker
        self.scheduler = scheduler
        self.brain = brain
        self.actions = actions
        self.catalog = catalog
        self.fb = fallback
        self.history = history
        self.memory = memory
        self.recall = recall
        self.embedder = embedder
        self.session = session
        self.stt_worker = stt_worker
        self.stage_motion = stage_motion
        self.speech_ring = speech_ring
        self.window = window
        self._closed = False
        self._danmu_task: asyncio.Task | None = None
        self._stage_runner = None
        self._desktop_proc = None
        self._stage_motion_task: asyncio.Task | None = None
        self._stage_motion_error: str | None = stage_motion_error
        self._motion_silence = 0.0
        self._stage_error: str | None = None
        self._danmu_error: str | None = None
        self._stt_unavailable = False
        self._last_avatar_refresh = 0.0
        self.rounds = 0
        self.silent_after_me = 0
        self.last_round_frames = 0
        self.last_round_images = 0
        self.last_round_payload_kb = 0.0
        events.on_event = self.on_bus_event

    # ── 总线旁路：礼物/上舰/SC 让形象反应 ───────────────
    def on_bus_event(self, ev) -> None:
        if self.stage is None or not getattr(self.stage, "clients", 0):
            return
        if ev.kind in (GIFT, SC, GUARD_BUY):
            self.stage.emit("event", kind=ev.kind, name=ev.name, text=ev.text,
                            price=ev.price)
            if self.stage_motion is not None:
                from sidekick.motion.types import Intent

                self.stage_motion.submit([
                    Intent("emotion", "happy", 0.9, None, "event"),
                    Intent("action", "laugh", 0.85, None, "event"),
                ])

    def on_desktop_click(self, part: str) -> None:
        """桌宠左键点击进入 brain 材料，像“我摸了摸她的头”。"""
        label = part if part in ("头", "手", "身子") else "身子"
        self.events.push(Event(kind=ME, name="我",
                               text=f"（我摸了摸她的{label}）"))

    # ── 会话/配置 ──────────────────────────────────────
    def _reload(self, cfg: dict) -> None:
        """热重载 prompt、截图模式和调度参数；其余仍按重启处理。"""
        cfg = config.resolve_capture_config(cfg)
        for key in ("prompt", "user", "role", "persona", "game",
                    "capture", "scheduler"):
            if key in cfg:
                self.cfg[key] = cfg[key]
        self.scheduler.reload(cfg)
        if hasattr(self.scheduler, "reload_identity"):
            self.scheduler.reload_identity(cfg)
        self.frames.reload(cfg)
        self.brain.reload_config(cfg)

    async def _refresh(self) -> None:
        now = time.time()
        if (self.catalog is not None
                and now - self._last_avatar_refresh >= REFRESH_S):
            self._last_avatar_refresh = now
            try:
                avatar = await asyncio.to_thread(self.catalog.selected)
                self.actions.bind(avatar)
            except Exception as e:
                log(f"角色刷新失败（沿用当前角色）：{type(e).__name__}: {e}")
        cfg = self.session.refresh(now)
        if cfg is None:
            return
        self._reload(cfg)

    def current_mode(self) -> str:
        return self.scheduler.current_mode

    # ── 排查端点读的公开状态（不碰私有成员）────────────
    @property
    def stt_unavailable(self) -> bool:
        return self._stt_unavailable

    @property
    def stage_runner(self):
        return self._stage_runner

    @property
    def stage_error(self) -> str | None:
        return self._stage_error

    # ── 浏览器自主驱动 ────────────────────────────────
    async def _stage_motion_loop(self) -> None:
        host = self.stage_motion
        if host is None:
            return
        last = time.monotonic()
        while not self._closed:
            now = time.monotonic()
            dt = min(0.1, max(0.0, now - last))
            last = now
            speaking = bool(getattr(self.speaker, "speaking", False))
            if speaking:
                self._motion_silence = 0.0
            else:
                self._motion_silence += dt
            speech = (self.speech_ring.latest(now)
                      if speaking and self.speech_ring is not None else None)
            context = Context(
                speaking=speaking,
                user_speaking=bool(getattr(self.mic, "user_speaking", False)),
                silence=self._motion_silence,
                chat_activity=min(1.0, self.events.count_audience(300) / 10.0),
            )
            try:
                host.tick(now, dt, speech=speech, context=context)
            except Exception as e:
                self._stage_motion_error = f"{type(e).__name__}: {e}"
                host.error = self._stage_motion_error
                log(f"[!!] 自主驱动 tick 失败：{self._stage_motion_error}")
                await asyncio.sleep(1.0)
                continue
            await asyncio.sleep(max(0.0, 1.0 / 60.0 - (time.monotonic() - now)))

    # ── 生命周期 ──────────────────────────────────────
    async def startup(self) -> None:
        await lifecycle.startup(self)
        if self.stage_motion is not None:
            self._stage_motion_task = asyncio.create_task(
                self._stage_motion_loop())

    async def shutdown(self) -> None:
        await lifecycle.shutdown(self)

    # ── 陪玩主循环 ────────────────────────────────────
    async def main_loop(self) -> None:
        log(f"陪玩循环启动：脑={self.brain.model}  "
            f"TTS={self.speaker.profile_name}")
        talk: asyncio.Task | None = None
        active: asyncio.Task | None = None
        try:
            while not self._closed:
                try:
                    await self._refresh()
                    snap = await self.scheduler.wait(15.0)
                    if snap is None:
                        continue
                    try:
                        decided = await self._think(snap)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log(f"本轮思考失败（继续跑）：{type(e).__name__}: {e}")
                        try:
                            self.brain.mark_failed(snap.get("round"))
                        except Exception:
                            pass
                        await self._apologize("brain")
                        continue
                    if talk is not None:
                        active, talk = talk, None
                        try:
                            await active
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            # 上一轮说失败不能连累这一轮已经想好的回复。
                            log(f"上一轮说话任务失败：{type(e).__name__}: {e}")
                        active = None
                    if decided is not None:
                        out, t0 = decided
                        talk = asyncio.create_task(self._talk(snap, out, t0))
                        talk.add_done_callback(self._talk_done)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log(f"陪玩循环出错（继续跑）：{type(e).__name__}: {e}")
                    await asyncio.sleep(1.0)
        finally:
            pending = [t for t in (active, talk) if t is not None]
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _think(self, snap: dict) -> tuple[str, float] | None:
        rid = snap["round"]
        self.rounds = rid
        frames = snap.get("frames") or []
        pack = snap.get("pack")
        images = None
        if pack is not None:
            latest_full, latest_ts = ((self.frames.latest_snapshot()
                                       if hasattr(self.frames, "latest_snapshot")
                                       else (None, 0.0))
                                      if frames else (None, 0.0))
            # take() 清空队列后抓屏线程可能又抓了新帧；不能用“未来”画面
            # 冒充这一轮的当前帧。
            if (latest_full is not None and frames
                    and float(latest_ts) > float(frames[-1].ts) + 1e-6):
                latest_full = None
            # 额外高清帧由 Frame.extra_jpeg 自己带；这里只映射当前帧。
            high_res = getattr(pack, "high_res", ())
            current_idx = (high_res[0] if high_res
                           else (len(frames) - 1 if frames else None))
            full_images = ({current_idx: latest_full}
                           if latest_full is not None
                           and current_idx is not None else None)
            try:
                images = await asyncio.to_thread(
                    composer.render, pack, frames, latest_full,
                    full_images=full_images)
            except Exception as e:
                log(f"[{rid}] 拼图失败，退回独立缩略图："
                    f"{type(e).__name__}: {e}")
        if images is None:
            images = []
            for frame in frames:
                jpeg = frame.thumb_jpeg
                if jpeg:
                    images.append((
                        f"[画面 {time.strftime('%H:%M:%S', time.localtime(frame.ts))}]",
                        base64.b64encode(jpeg).decode("ascii")))
        self.last_round_frames = len(frames)
        self.last_round_images = len(images)
        self.last_round_payload_kb = 0.0

        gap = snap.get("gap_s")
        log(f"[{rid}] 开始想（{round_why(snap)}"
            + (f"，距上次 TTS 请求 {gap:.1f}s" if gap is not None else "")
            + f"，材料 {snap.get('events', 0)} 条）")

        t0 = time.time()
        self.display.status("思考中")
        try:
            out = await self.brain.reply(snap["text"], images, round_id=rid)
        except Exception:
            self.display.status("")
            raise
        if out is not None or not self.brain.last_error:
            usage = getattr(self.brain, "last_usage", {}) or {}
            self.last_round_payload_kb = round(float(
                usage.get("payload_kb") or 0.0), 1)
        for t in (self.brain.last_tasks or []):
            log(f"[{rid}] 任务{t.get('action')}：{t.get('title')}")
        if out is not None:
            # 先说清楚她“想说什么”，和气泡一样独立于 TTS 成败。
            log(f"[{rid}] 想好：{one_line(out)}")

        if out is None:
            self.display.status("")
            if self.brain.last_error:
                log(f"[{rid}] 脑失败：{self.brain.last_error[:110]}")
                await self._apologize("brain")
            else:
                if snap["meta"].get("must_reply"):
                    self.silent_after_me += 1
                log(f"[{rid}] 她这回没说话（{snap['reason']}）")
            return None
        return out, t0

    # ── 说出去：首音驱动气泡 + 失败兜底 ─────────────────
    def _talk_done(self, task: asyncio.Task) -> None:
        """不能在下一轮才看 talk 的异常；先记日志，主循环仍会 await。"""
        if task.cancelled():
            self.display.status("")
            return
        try:
            err = task.exception()
        except Exception as e:
            log(f"说话任务状态异常：{type(e).__name__}: {e}")
            self.display.status("")
            return
        if err is not None:
            self.display.status("")
            log(f"说话任务异常：{type(err).__name__}: {err}")

    async def _talk(self, snap: dict, out: str, t0: float) -> None:
        self.display.status("组织语言中")
        try:
            await reply.talk(self, snap, out, t0)
        finally:
            # 只剩标签/guard 拒发/首音前被打断时没有 say 事件，
            # 阶段提示不能一直挂在气泡上。
            self.display.status("")

    async def _apologize(self, kind: str) -> None:
        await reply.apologize(self, kind)

    # ── 排查用（只读）──────────────────────────────────
    def features(self) -> dict[str, dict]:
        return status_mod.features(self)

    def status(self) -> dict:
        return status_mod.status(self)

    def peek(self, seconds: float = 90.0) -> str:
        return status_mod.peek(self, seconds)


# ── 单一功能装配入口 ──────────────────────────────────
def _build_stage_motion(root, cfg: dict, stage, avatar, speaker):
    """live 模式：runtime 内跑引擎，审计模型事实，推原始参数给浏览器。"""
    if (avatar is None or getattr(avatar, "kind", "") != "live2d"
            or not bool((cfg.get("stage") or {}).get("enabled", True))
            or str((cfg.get("stage") or {}).get("mode", "")).lower() != "live"
            or not bool((((cfg.get("stage") or {}).get("autonomy") or {})
                         .get("enabled", True)))):
        return None, None, None
    model_path = Path(avatar.root) / avatar.entry
    try:
        dossier = run_audit(root, model_path)
    except RuntimeError as e:
        log(f"[!!] 自主驱动审计失败：{e}")
        return None, None, str(e)
    profile = profile_from_dossier(dossier)
    data, read_err = autonomy_store.load(avatar)
    if data is not None:
        if data.get("schema_version") != 1:
            profile.warnings.append("profile schema_version 不是 1，已忽略")
        else:
            clean, issues = autonomy_validate.validate(data, dossier)
            apply_overrides(profile, clean)
            profile.warnings.extend(f"profile: {x.text()}" for x in issues)
    elif read_err:
        profile.warnings.append(f"profile 读取失败：{read_err}")
    host = StageMotionHost(stage, profile, avatar=avatar)
    ring = SpeechRing()
    if hasattr(speaker, "mouth_sink"):
        speaker.mouth_sink = ring.push
        speaker.mouth_reset = ring.clear
    log(f"自主驱动：浏览器宿主已启动（tier={profile.tier}，"
        f"绑定 {len(profile.by_channel)}，源={profile.source}）")
    return host, ring, None


def build_runtime(cfg: dict | None = None, *, root=None) -> Runtime:
    with contextlib.ExitStack() as stack:
        rt = _build_runtime(cfg, root, stack)
        stack.pop_all()
        return rt


def _build_runtime(cfg: dict | None, root, stack: contextlib.ExitStack) -> Runtime:
    cfg = cfg or config.load()
    cfg = config.resolve_capture_config(cfg)
    root = root or config.ROOT
    config.ensure_dirs(cfg, root)
    window = lifecycle.window_context(cfg)

    events = EventBus(int(cfg["state"]["ring_size"]))
    said = SaidLog(str(cfg["state"]["say_log"]), root=root)
    frames = (FrameQueue(
        cfg, pet_rect_provider=lifecycle.pet_rect_provider(cfg),
        window_context=window)
        if bool(cfg["capture"]["enabled"]) else NullFrames())
    pacing_machine = pacing.ModeMachine()
    fallback = Fallback(cfg, root)
    stage = (StageClient() if bool(cfg["stage"]["enabled"])
             else NullStage())

    mic = (Mic(cfg) if bool(cfg["mic"]["enabled"])
           else NullMic())
    stt_on = bool(cfg["stt"]["enabled"])
    if stt_on:
        try:
            stt = make_stt(cfg, root)
        except Exception as e:
            raise RuntimeError(f"feature stt 不可用：{type(e).__name__}: {e}") from e
    else:
        stt = NullStt()
    if hasattr(stt, "close"):
        stack.callback(stt.close)
    stt_worker = SttWorker(stt, events, log) if stt_on else None
    if stt_on and stt_worker is not None:
        mic.on_segment = stt_worker.on_segment

    # 记忆：关闭时不建 SQLite；ShortTermHistory 与 LongTerm 共用同一个 store。
    worldbook = Worldbook(cfg, root)
    if bool(cfg["memory"]["enabled"]):
        db = str(cfg["memory"]["db_path"])
        store = MemoryStore(Path(root) / db)
        if hasattr(store, "close"):
            stack.callback(store.close)
        history = SqliteHistory(
            store,
            history_max=int(cfg["memory"]["history_max"]),
            summarize_every=int(cfg["memory"]["summarize_every"]))
        embedder = embed_mod.build(cfg, root, log)
        if hasattr(embedder, "close"):
            stack.callback(embedder.close)
        memory = Memory(cfg, root, store=store, embedder=embedder)
        if hasattr(memory, "close"):
            stack.callback(memory.close)
        worldbook_index = WorldbookIndex(store, worldbook, embedder)
        recall = RecallEngine(cfg, memory, worldbook, worldbook_index,
                              embedder)
    else:
        history = InMemoryHistory(int(cfg["memory"]["history_max"]))
        memory = NullLongTerm()
        embedder = NullEmbedder()
        recall = None

    catalog = (AvatarCatalog(
        root, selection=lambda: str((cfg.get("role") or {}).get("id") or ""))
        if bool(cfg["stage"]["enabled"]) else None)
    stage_error = ""
    if catalog is not None:
        # 钉住启动时的选择：换角色要重启，运行中不跟着 config.role.id 漂。
        catalog.pin()
    try:
        avatar = catalog.selected() if catalog is not None else None
    except LookupError:
        avatar = None
        stage_error = "models 目录下没有可用形象"
    actions_enabled = bool(
        ((cfg.get("stage") or {}).get("actions") or {}).get("enabled", True))
    # stage 开着但 models 为空时也要先构造真实的 AvatarActions(None)：
    # 之后放入模型，_refresh 才能用 bind() 接上，而不是一直卡在 Null。
    actions = (avatar_actions.AvatarActions(
        avatar, actions_enabled=actions_enabled)
        if avatar is not None or bool(cfg["stage"]["enabled"])
        else NullAvatarActions())

    # 配置只存 role.id / user.id；名字、别名、人设目录在文件里。
    pinned_role = (getattr(avatar, "role_id", "") or getattr(avatar, "id", "")
                   if avatar is not None else None)
    pinned_dir = getattr(avatar, "role_dir", None) if avatar is not None else None
    identity = identity_mod.resolve(cfg, root, role_id=pinned_role,
                                    role_dir=pinned_dir)
    run_cfg = identity.cfg
    for warning in identity.warnings:
        log(f"[角色/用户] {warning}")

    if bool(run_cfg["tts"]["enabled"]):
        try:
            provider = make_tts(run_cfg)
            speaker = Speaker(run_cfg, provider=provider, mic=mic)
        except (Exception, SystemExit) as e:
            raise RuntimeError(f"feature tts 不可用：{type(e).__name__}: {e}") from e
    else:
        speaker = NullSpeaker(cps=run_cfg["stage"]["cps"])
    if hasattr(mic, "is_playing"):
        mic.is_playing = lambda: bool(getattr(speaker, "speaking", False))

    stage_motion, speech_ring, motion_error = _build_stage_motion(
        root, run_cfg, stage, avatar, speaker)
    if stage_motion is not None and hasattr(actions, "bind_motion_profile"):
        actions.bind_motion_profile(stage_motion.profile)

    scheduler = Scheduler(
        run_cfg, events, said, speaker, pacing_machine,
        take_frames=frames.take, mic=mic,
        capture_now=getattr(frames, "capture_now", None),
        latest_frame_ts=getattr(frames, "latest_ts", None),
        window=window)

    # 打断：输入层只发回调，停播与调度放行都在这里接（输入不 import 出口）。
    if hasattr(mic, "on_barge"):
        mic.play_ref = lambda: getattr(speaker, "play_ref", (0.0, 0.0))

        def _on_barge() -> None:
            speaker.stop()
            scheduler.note_barge()

        mic.on_barge = _on_barge
    llm = LLMClient(run_cfg)
    brain = Brain(run_cfg, history=history, memory=memory, worldbook=worldbook,
                  actions=actions, llm=llm, recall=recall, root=root,
                  speech_rules=getattr(speaker, "prompt_rules", ""), log=log)
    if stt_worker is not None:
        stt_worker.on_final = embedder.prefetch
    display = BubbleDisplay(stage, cps=float(run_cfg["stage"]["cps"]))

    def _resolve(raw: dict) -> dict:
        # 角色 id 钉在启动值上；目录每次按当前 avatar 重新解析，避免迁移后抱死旧路径。
        cur_avatar = getattr(actions, "avatar", None) or avatar
        role_dir = (avatar_roles.active_dir(cur_avatar, root)
                    if cur_avatar is not None else identity.role_dir)
        return identity_mod.resolve(raw, root, role_id=identity.role_id,
                                    role_dir=role_dir).cfg

    session = SessionState(run_cfg, resolve=_resolve)
    session.mark_loaded()              # 刚构建完，先按注入的 cfg 跑，15 秒后再看盘
    rt = Runtime(
        cfg=session.effective(), root=root, events=events, said=said,
        frames=frames, mic=mic,
        stt=stt, stage=stage, display=display, speaker=speaker,
        scheduler=scheduler, brain=brain, actions=actions, catalog=catalog,
        fallback=fallback, history=history, memory=memory, session=session,
        recall=recall, embedder=embedder, stt_worker=stt_worker,
        stage_motion=stage_motion, speech_ring=speech_ring,
        stage_motion_error=motion_error, window=window)
    if stage_error:
        rt._stage_error = stage_error
    return rt
