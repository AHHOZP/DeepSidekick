"""兜底话术：某个部件坏了，她还能说点什么。

两种坏法不一样，所以这里只管「说哪一句、什么时候说」，怎么出声交给调用方：

  · **脑坏了** —— 语音是好的，能念一句「我卡了」，走正常 TTS
  · **语音坏了** —— 这时候没有合成器可用，只能播**预录的音频**
    （`state/fallback/tts_01.wav` 这种，等角色设定定稿后再录）

三道闸都是为了别让直播间变成复读机：

  · 洗牌袋：10 条打乱依次用，用完重洗，不会连着两次同一句
  · 冷却：同一种故障 40 秒内最多说一句
  · 熔断：连续 3 个 round 都失败之后就不再道歉了，只写日志（`/status` 会标红）
"""
from __future__ import annotations

import random
import time
from pathlib import Path

from sidekick.config import path as resolve_path

# 占位文本。等角色设定定稿，这 20 条会按她的口吻重写一遍，
# 然后各 provider 各录一套 wav（见 clip()）。
POOL: dict[str, list[str]] = {
    "brain": [
        "诶——我刚刚断片了，你再说一遍？",
        "等我一秒，脑子被浪拍了一下。",
        "刚刚那句我听见了，就是……回不上来，你先说。",
        "我卡住了，别急，我缓缓。",
        "啊，我这边抽了一下，你说的我记着呢。",
        "不是我不理你，是我这边短路了一小下。",
        "等我喘口气……好了，刚才说到哪？",
        "刚才脑子空白了一下，你继续，我跟着。",
        "诶？我好像掉线了两秒，你刚说什么来着？",
        "我这边刮了下风，刚缓过来。",
    ],
    "tts": [
        "……我嗓子哑了一下。",
        "等等，我这边声音好像断了。",
        "咳，能听见吗？我这会儿有点说不出话。",
        "声音卡了一下，我缓缓再说。",
        "……刚那口气没上来。",
        "我这嗓子突然不听话了，稍等。",
        "听得见吗？我这边的声音好像飘走了。",
        "别急，我马上就能出声。",
        "嗯……我先清清嗓子。",
        "声音掉了一下，我回来了。",
    ],
}

RECOVER = {
    "brain": "我回来了，刚才脑子飘了一下。",
    "tts": "声音回来了，刚才哑了一下。",
}


class Fallback:
    def __init__(self, cfg: dict, root: str | Path | None = None) -> None:
        e = cfg["errors"]
        self.cooldown = float(e["fallback_cooldown_s"])
        self.max_apologies = int(e["fallback_max_apologies"])
        self.recover_s = float(e["recover_say_s"])
        raw = str(e["fallback_dir"])
        p = Path(raw)
        if p.is_absolute():
            self.dir = p
        elif root is not None:
            self.dir = Path(root) / p
        else:
            self.dir = resolve_path(raw)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        self._streak = {"brain": 0, "tts": 0}
        self._since = {"brain": 0.0, "tts": 0.0}
        self._last_say = {"brain": 0.0, "tts": 0.0}
        self._last_line = {"brain": "", "tts": ""}
        self._clip_i = {"brain": 0, "tts": 0}
        self._bags: dict[str, list[str]] = {k: [] for k in POOL}
        self.played = 0
        self.last_kind: str | None = None
        self.last_at = 0.0

    # ── 失败 / 成功 ────────────────────────────────────
    def record_failure(self, kind: str, *, has_clip: bool = True) -> str | None:
        """记一次失败，返回这一轮该说的道歉词；None 表示闭嘴。"""
        now = time.time()
        if self._streak[kind] == 0:
            self._since[kind] = now
        self._streak[kind] += 1

        if self._streak[kind] > self.max_apologies:
            return None                      # 熔断：不道歉了
        if now - self._last_say[kind] < self.cooldown:
            return None                      # 冷却中
        if kind == "tts" and not has_clip:
            return None                      # 没预录音频，实际没出声，不计数
        line = self._draw(kind)
        if line:
            self._last_say[kind] = now
            self.played += 1
            self.last_kind = kind
            self.last_at = now
        return line

    def record_success(self, kind: str) -> str | None:
        """成功一次。中断够久才说「我回来了」。"""
        if self._streak[kind] == 0:
            return None
        lasted = time.time() - self._since[kind]
        self._streak[kind] = 0
        if lasted < self.recover_s:
            return None
        self._last_say[kind] = time.time()
        self.played += 1
        self.last_kind = kind
        self.last_at = time.time()
        return RECOVER.get(kind)

    # ── 预录音频 ──────────────────────────────────────
    def clip(self, kind: str) -> Path | None:
        """语音挂了的时候，唯一能出声的东西。没有就返回 None。

        文件命名：state/fallback/{kind}_NN.wav（kind 是 brain / tts），轮着播。
        等角色设定定稿，用最终音色把 POOL 里那 20 句各录一条丢进来即可。
        """
        if not self.dir.exists():
            return None
        files = sorted(self.dir.glob(f"{kind}_*.wav"))
        if not files:
            return None
        idx = self._clip_i.get(kind, 0)
        self._clip_i[kind] = idx + 1
        return files[idx % len(files)]

    # ── 内部 ──────────────────────────────────────────
    def _draw(self, kind: str) -> str:
        bag = self._bags[kind]
        if not bag:
            bag = list(POOL.get(kind) or [])
            random.shuffle(bag)
            # 重洗之后第一张可能正好是上一轮那张，挪到最后
            if len(bag) > 1 and bag[-1] == self._last_line[kind]:
                bag.insert(0, bag.pop())
            self._bags[kind] = bag
        if not bag:
            return ""
        line = bag.pop()
        self._last_line[kind] = line
        return line

    def stats(self) -> dict:
        now = time.time()
        return {
            "brain": {"streak": self._streak["brain"],
                      "degraded": self._streak["brain"] > self.max_apologies},
            "tts": {"streak": self._streak["tts"],
                    "degraded": self._streak["tts"] > self.max_apologies},
            "played": self.played,
            "last_kind": self.last_kind,
            "since_last_s": round(now - self.last_at, 1) if self.last_at else None,
            "clips": len(list(self.dir.glob("*.wav"))) if self.dir.exists() else 0,
        }
