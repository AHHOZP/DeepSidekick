"""总调度：决定"什么时候把攒下来的东西交给大脑"。

Scheduler 只负责两道闸门，按顺序：

  1. **静音模式** —— 直接不起（在最前面，后面那些都有副作用）
  2. **最小间隔** —— 上一轮**把 TTS 请求发出去**之后，至少再过 `min_gap_s` 秒

**TTS 排队不在这里**：`Runtime.main_loop` 在起下一轮发言任务前 `await` 上一轮
`talk`，所以“上一轮没播完，这一轮发不出去”由 runtime 保证；Scheduler 不持有
播放状态，也不应被误认为会自己防 TTS 重入。

第 2 条是为了治"连续短句"：你说一句短的 → 一轮 → 她回一句 -> 你再说一句短的。
中间不留间隔的话，每次回复都很短、很碎，而且每轮只带一张截图。
6 秒的窗口里，你说的话和弹幕会**攒着**，下一轮一起发出去。

`min_gap` 还会被截图间隔抬起来：间隔必须 ≥ `capture.interval_s`，
否则这一轮可能一张新图都没有。

触发条件（任一成立，且上面三道闸都过了）：
  · 我说话了        → 等 0 / 0.8 秒（短句多等）
  · 弹幕攒够 4 条   → 立刻
  · 或攒够 12 秒    → 发
  · 什么都没有      → 冷场阈值（按档位，60/45/30 秒）后发
"""
from __future__ import annotations

import asyncio
import time

from sidekick.brain import material
from sidekick.input import composer
from sidekick.input.window_context import NullWindowContext
from sidekick.core.events import GUARD_BUY, ME, SC, EventBus
from sidekick.scheduling.mode import SILENT
from sidekick.core.ports import MicSource, VoiceClock
from sidekick.output.said_log import SaidLog
from sidekick.brain.user import UserProfile


class Scheduler:
    def __init__(self, cfg: dict, bus: EventBus, said: SaidLog,
                 clock: VoiceClock, modes=None, take_frames=None,
                 mic: MicSource | None = None, capture_now=None,
                 latest_frame_ts=None, window=None) -> None:
        self.cfg = cfg
        self.bus = bus
        self.said = said
        self.clock = clock
        self.mic = mic
        self._mm = modes or _default_modes()
        self.window = window if window is not None else NullWindowContext()
        # 取这一轮的画面。**取走即释放**，不跨轮复用。
        self.take_frames = take_frames
        self.capture_now = capture_now
        self.latest_frame_ts = latest_frame_ts
        self.reload(cfg)

        self._consumed = time.time()
        self._consumed_seq = 0        # 已消费到第几条事件（按序号，不按时间）
        self._last_round = 0.0
        self._barge_free = False      # 被打断后，下一次取材料免 min_gap（只一次）
        self.rounds = 0
        self.last_reason: str | None = None
        self._due_since = 0.0              # 材料从什么时候开始"该说了"（等闸门用的）

    # ── 内部 ──────────────────────────────────────────
    def reload_identity(self, cfg: dict) -> None:
        """只同步运行期身份；调度参数仍按启动快照。"""
        for key in ("persona", "user", "role"):
            if key in cfg:
                self.cfg[key] = cfg[key]

    def reload(self, cfg: dict) -> None:
        """热重载：改 config 后不用重启就能生效。"""
        self.cfg = cfg
        s = cfg["scheduler"]      # config.load() 已合并默认值
        self.min_gap = float(s["min_gap_s"])
        self.capture_gap = (float(cfg["capture"]["interval_s"])
                            if bool(cfg["capture"]["enabled"]) else 0.0)
        self.batch_s = float(s["batch_s"])
        self.batch_events = int(s["batch_events"])
        self.me_wait_short = float(s["me_wait_short_ms"]) / 1000.0
        self.me_short_chars = int(s["me_short_chars"])
        self.window.reload(cfg)

    def _pending(self) -> list:
        return self.bus.since_seq(self._consumed_seq)

    def _cold_base(self) -> float:
        """冷场计时的基准点：她上次说完话 / 上一轮开始 / 上一轮消耗点。"""
        last_speech = self.said.last_ts or 0.0
        return max(self._consumed, self._last_round, last_speech)

    def _cold_wait(self, now: float, since: float,
                   idle: float) -> float | None:
        """距离冷场放行还差多久；正在说话时无法估计，返回 None。

        除了 idle，还要考虑“3 秒内出过声”和 2.5 倍保险丝；
        否则 `/status` 会在被 hold 的时候假报 0。
        """
        last_voice = float(self.mic.last_voice_at or 0.0) if self.mic else 0.0
        recent_voice = bool(last_voice) and (now - last_voice) < 3.0
        talking = self._user_talking()
        # 未经 STT 确认的“在说话”最多把冷场拖到 min(2.5×idle, idle+6s)；
        # 只有真正产出 ME 事件后，take_round 才会重置 _cold_base。
        limit = min(idle * 2.5, idle + 6.0)
        if (recent_voice or talking) and since < limit:
            if talking:
                return None
            return max(0.0, idle - since, 3.0 - (now - last_voice))
        return max(0.0, idle - since)

    def _due_state(self, now: float) -> tuple[bool, str, float | None]:
        """材料到没到点：返回 (due, reason, 还差几秒)。

        `_due()` 与 `/status` 的 `material_due_in_s` 共用这一份判断；
        `status` 只读，不推进模式状态机。
        """
        # 触发只数**能接话**的那几种。「有人进了直播间」不该自己触发一轮
        # （那一轮没什么可说的），但它仍然留在材料里，冷场时她会顺口提一句。
        talkable = [e for e in self._pending() if e.is_speakable()]
        if not talkable:
            # 你正在说话时不起冷场 —— 但这条判断放在 _cold_wait 里，
            # 那里有 2.5 倍 idle 的保险丝：麦克风一直有底噪、被误判成"在说"时，
            # 最多拖到 idle 的 2.5 倍也必须开口，不能永远等下去。
            idle = self._idle(self.current_mode)
            left = self._cold_wait(now, now - self._cold_base(), idle)
            if left == 0.0:
                return True, "冷场", 0.0
            return False, "", left

        mine = [e for e in talkable if e.kind == ME]
        if mine:
            me_ts = max(e.ts for e in mine)
            # 一句话被切成两半的情况：识别出来字很少，多半还有下半句，就多等一会儿。
            # 只要又来了新的「我」，me_ts 会往后挪，等待自动重新计时、两半并成一轮。
            wait = (self.me_wait_short
                    if len((mine[-1].text or "")) < self.me_short_chars else 0.0)
            left = max(0.0, wait - (now - me_ts))
            if left <= 0:
                n = len(mine)
                reason = ("我说话" if n == 1
                          else f"我说话（{n} 段并成一轮）")
                if any(e.kind in (SC, GUARD_BUY) for e in talkable):
                    reason += "（含醒目留言/上舰）"
                return True, reason, 0.0
            return False, "", left

        oldest = min(e.ts for e in talkable)
        if len(talkable) >= self.batch_events:
            return True, f"弹幕攒够 {len(talkable)} 条", 0.0
        left = max(0.0, self.batch_s - (now - oldest))
        if left <= 0:
            return True, f"弹幕攒够 {int(now - oldest)} 秒", 0.0
        return False, "", left

    def _due(self) -> tuple[bool, str]:
        due, reason, _left = self._due_state(time.time())
        return due, reason if due else ""

    def _user_talking(self) -> bool:
        """你现在是不是**真的在说**（麦克风判定的，不等 STT）。

        要求已经连续说了 300ms 以上：一个咔哒声、椅子响不该把冷场挡掉。
        """
        if self.mic is None:
            return False
        return float(self.mic.speech_ms or 0.0) >= 300.0

    def _need_gap(self) -> float:
        """最小间隔：配置值和截图间隔取大的那个。"""
        return max(self.min_gap, self.capture_gap)

    def note_barge(self) -> None:
        """被用户打断后，下一次取材料不受 min_gap 限制（只一次）。

        不打断时这一轮本该说完；你插嘴后继续说完的那句应当马上得到回应，
        否则要等 `scheduler.min_gap_s`（从上一轮 TTS 请求算起）才能接话。
        """
        self._barge_free = True

    def gap_left(self) -> float:
        """还要等多久才允许下一轮（秒）；0 = 现在就可以。"""
        if self._barge_free:
            return 0.0
        base = max(self._last_round,
                   float(getattr(self.clock, "last_request_at", 0.0) or 0.0))
        if not base:
            return 0.0
        return max(0.0, self._need_gap() - (time.time() - base))

    def _gate(self) -> bool:
        # 注意：这里**不再拦"她还在说话"**。
        # 她还在播的时候，下一轮可以先把脑子动起来（思考），只是 TTS 请求要排队
        # 等她播完才发出去（由 runtime 那边 await 上一个 talk 任务保证）。
        return self.gap_left() <= 0

    # ── 对外 ──────────────────────────────────────────
    @property
    def current_mode(self) -> str:
        """最近一次决策出的模式；只读，不推进滞后计时。"""
        return self._mm.current

    def take_round(self) -> dict | None:
        """现在能不能起一轮；能就返回这一轮的材料。

        **静音模式必须在最前面就返回**：后面的取图、清事件、推 `_consumed`
        都有副作用，先做再丢的话，这一段的截图和「我说话」就永远消失了。
        """
        mode = self._mode()
        if mode == SILENT:
            return None

        # 先看材料到没到点，再看闸门——顺序反过来的话，
        # "材料早就在等了、只是一直被 6 秒闸门挡着"这件事就看不见了。
        due, reason = self._due()
        if not due:
            self._due_since = 0.0
            return None
        if not self._due_since:
            self._due_since = time.time()
        if not self._gate():
            return None
        waited = time.time() - self._due_since
        if waited >= 0.5:
            trigger = "间隔到期"          # 真正让它开口的是闸门
        else:
            trigger = reason              # 材料一到就说

        now = time.time()
        # 距"上一轮 TTS 请求"（或上一轮开始）过了多久 —— 日志里要打出来，
        # 否则两轮之间到底隔了几秒，看日志是看不出来的。
        base = max(self._last_round,
                   float(getattr(self.clock, "last_request_at", 0.0) or 0.0))
        gap_s = (now - base) if base else None
        window = max(0.5, now - self._consumed)
        # 先取序号，再取事件，并且只收序号以内的：
        # STT 线程可能在两行之间 push 一条 ME，按"现在的序号"标记会把它
        # 永久吞掉；反过来先取序号再过滤，它就会留给下一轮。
        seq_at = self.bus.seq
        evs = [e for e in self._pending() if e.seq <= seq_at]
        p = self.cfg["persona"]
        profile = UserProfile.from_cfg(self.cfg)
        name = profile.name
        bot_names = [str(x) for x in p["aliases"] if str(x).strip()]

        # 画面和事件用**同一个窗口**：都是"上一轮之后发生的"。
        # 取走即释放，所以这一批只会被这一轮用一次。
        frames = ([] if self.window.privacy_active
                  else list(self.take_frames() if self.take_frames else []))
        window_line, privacy_note = self.window.material_lines()
        if privacy_note:
            # 取图与读窗口状态之间可能刚好切到隐私窗口：这一轮不能再带图。
            frames = []
            pack = composer.plan([], self.cfg)
        else:
            pack = composer.plan(frames, self.cfg)
        text, meta = material.build_round(self.bus, self.said, mode,
                                        seconds=window, evs=evs,
                                        frame_ts=[f.ts for f in frames],
                                        name=name, reason=reason,
                                        bot_names=bot_names,
                                        live=bool(self.cfg["session"]["live"]),
                                        pack=pack,
                                        window_line=window_line,
                                        privacy_note=privacy_note)

        # 用**取材料那一刻**的序号：build_round 可能要几十毫秒，
        # 期间新到的弹幕/「我说话」如果按"现在的序号"标记，会被永久吞掉。
        self._consumed = now
        self._consumed_seq = seq_at
        self._last_round = self._consumed
        self._barge_free = False           # 放行用掉了
        self.rounds += 1
        self.last_reason = reason

        return {
            "text": text,
            "meta": meta,
            "frames": frames,
            "pack": pack,
            "reason": reason,
            "trigger": trigger,
            "waited_s": round(waited, 1),
            "events": len(evs),
            "round": self.rounds,
            "gap_s": round(gap_s, 1) if gap_s is not None else None,
        }

    async def wait(self, timeout_s: float = 25.0) -> dict | None:
        """一直等到该起一轮（或超时）。主循环每圈调一次。

        `ME` / 打断触发前先看尾帧是否太旧；旧了就唤醒 capture 线程现抓一张，
        但抓屏阻塞只等 `capture_now_stale_s` 对应的 0.12s 超时，不拖住事件循环。
        """
        deadline = time.time() + max(1.0, timeout_s)
        while time.time() < deadline:
            due, reason = self._due()
            if (due and self.capture_now is not None
                    and (reason.startswith("我说话") or self._barge_free)):
                stale = float(self.cfg["capture"]["capture_now_stale_s"])
                latest = 0.0
                if self.latest_frame_ts is not None:
                    try:
                        latest = float(self.latest_frame_ts() or 0.0)
                    except Exception:
                        latest = 0.0
                if latest <= 0 or time.time() - latest > stale:
                    try:
                        await asyncio.to_thread(self.capture_now)
                    except Exception:
                        pass
            snap = self.take_round()
            if snap is not None:
                return snap
            await _sleep(0.2)
        return None

    def material_due_in_s(self) -> float | None:
        """材料还差多久才"该说"（秒）；0 = 现在就该说，None = 无法估计。

        对应文档里的 `material_due_in_s`：短句那 0.7/0.8 秒、弹幕那 12 秒、
        冷场那 60/45/30 秒。和 `gap_left_s` 是两件事：一个看材料，一个看闸门。
        """
        _due, _reason, left = self._due_state(time.time())
        return None if left is None else round(left, 2)

    def stats(self) -> dict:
        mode = self.current_mode
        idle = self._idle(mode)
        now = time.time()
        cold_left = self._cold_wait(now, now - self._cold_base(), idle)
        gap_left = self.gap_left()
        return {
            "rounds": self.rounds,
            "last_reason": self.last_reason,
            "pending": len(self._pending()),
            "since_last_round_s": round(time.time() - self._last_round, 1)
            if self._last_round else None,
            "voice_busy": self.clock.busy,
            "idle_threshold_s": idle,
            "cold_left_s": (None if cold_left is None else round(cold_left, 1)),
            "cold_fuse_s": round(idle * 2.5, 1),
            "min_gap_s": round(self._need_gap(), 1),
            "gap_left_s": round(gap_left, 1),
            "gate": (f"间隔不足（还差 {gap_left:.1f}s）" if gap_left > 0
                     else ""),
            "material_due_in_s": self.material_due_in_s(),
        }

    # ── 小工具 ────────────────────────────────────────
    def _mode(self) -> str:
        mcfg = self.cfg["mode"]
        # 弹幕数只算观众侧：把主播自己的「我说话」算进来，一个人自说自话
        # 也能把模式从 quiet 推到 hype。solo 只影响自动降档，不影响 manual。
        return self._mm.decide(
            self.bus.viewers, self.bus.count_audience(300), mcfg,
            solo=not bool(self.cfg["session"]["live"]))

    def _idle(self, mode: str) -> float:
        from sidekick.scheduling.mode import SILENT, cadence

        # silent 没有冷场节奏：返回 0 让 /status 不崩，take_round 仍会先返回。
        return 0.0 if mode == SILENT else cadence(mode, self.cfg["cadence"])


async def _sleep(s: float) -> None:
    import asyncio

    await asyncio.sleep(s)


def _default_modes():
    from sidekick.scheduling.mode import ModeMachine

    return ModeMachine()

