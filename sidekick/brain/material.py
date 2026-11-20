"""把"这一轮发生了什么"拼成一段给模型看的话。

模型每轮只看到这一块 + 几张图 + 人设。所以这里的信息密度就是她的全部视野。

材料只写**事实**，不写命令：谁说了什么、画面什么时候、为什么轮到她。
"该不该开口、接哪一路"由人设（persona/20-live.txt、30-material.txt）决定。

  [现在]   时钟、开播时长、气氛、人数、本轮触发原因、距你上次出声
  [你上一句] 你上一次真正说出口的话（防复读、可接自己的话）
  [这一段] 距离上一轮的时间跨度
  [画面]   这一段时间的几张截图；打包附图从左到右由新到旧
  [<用户名>说的话] 用户的语音转文字（标签用配置里的 user.name）
  [弹幕]   期间观众说了什么；叫到你的那几条会标「叫你」
  [冷场]   什么都没有的时候，只剩这句话

为什么叫「<用户名>说的话」而不是「我说的话」：整段材料是写给角色的，
"我"只能是角色本身；旧写法会让模型把主播的话当成自己说的。
"""
from __future__ import annotations

import random
import re
import time

from sidekick.output import guard
from sidekick.core.events import ENTER, ME, EventBus
from sidekick.scheduling.mode import PROMPT_LABEL
from sidekick.output.said_log import SaidLog


def round_why(snap: dict) -> str:
    """这一轮被什么放行的，给日志用。"""
    reason = snap.get("reason") or ""
    trigger = snap.get("trigger") or reason
    waited = snap.get("waited_s") or 0.0
    if trigger and trigger != reason:
        return f"{trigger}（材料：{reason}，等了 {waited:.1f}s）"
    return reason


# 材料段名；material_digest 也按这些段拆历史，避免两处各写一份中文标签。
SECTION_NOW = "现在"
SECTION_ME = "说的话"
SECTION_AUDIENCE = "弹幕"
SECTION_FRAME = "画面"
SECTION_SCENE = "场景"
SECTION_COLD = "冷场"

# 冷场提示词。故意写好几套换着用：同一句话每 25 秒出现一次，她会越说越像机器人。
# 每一套是若干短句，按 stream/solo 选 audience 后再拼；避免维护两份几乎一样的文案。
# {name}/{gap}/{viewers}/{screen}/{user} 由 _cold_line 填。
_COLD = [
    ("已经 {gap} 秒没有说话了", "{audience}", "{screen}"),
    ("安静了 {gap} 秒", "画面里没人开口", "{screen}"),
    ("{gap} 秒没人开口", "{audience}", "{screen}"),
    ("又是 {gap} 秒的沉默", "{audience}", "{screen}"),
    ("{gap} 秒过去了", "画面里的角色还在专心打", "{viewers}", "{screen}"),
    ("这 {gap} 秒里", "{audience}", "{screen}"),
    ("安静得有点过头——已经 {gap} 秒了", "{screen}"),
    ("已经 {gap} 秒没出声", "{audience}", "{screen}"),
    ("{gap} 秒，一个人说话都没有", "画面里的角色大概正打得入神", "{screen}"),
]
# 下标 -> (stream 说法, solo 说法)；solo 的空串表示这句不出现。
_COLD_AUDIENCE = {
    0: ("弹幕也停着", ""),
    2: ("直播间像停电了一样", "房间里只剩游戏声"),
    3: ("弹幕停在最后一条", ""),
    5: ("没人理你，也没人理{user}", "你和{user}都没说话"),
    7: ("弹幕停在最后一条", ""),
}
# 还没说过话时的几种说法（开播头一回冷场）。
_COLD_FIRST = [
    ("开播到现在，还没找到话头", "{audience}", "{screen}"),
    ("第一句话还没出口呢", "画面那边没动静", "{screen}"),
]
_last_cold = -1

# 调度器给的原因是内部说法，材料里换成她看得懂的人话。
_REASON = [
    ("我说话", "{user}刚说完话"),
    ("弹幕攒够", "弹幕攒够了"),
    ("冷场", "安静了一阵"),
    ("手动", "手动触发"),
]


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _gap_text(gap: float | None) -> str:
    if gap is None:
        return "还没说过话"
    if gap < 60:
        return f"距你上次出声 {int(gap)} 秒"
    return f"距你上次出声 {int(gap // 60)} 分 {int(gap % 60)} 秒"


def _ago_text(gap: float | None) -> str:
    if gap is None:
        return "刚才"
    if gap < 10:
        return "刚才"
    if gap < 60:
        return f"{int(gap)} 秒前"
    return f"{int(gap // 60)} 分 {int(gap % 60)} 秒前"


def _reason_text(reason: str, user: str = "") -> str:
    r = (reason or "").strip()
    if not r:
        return ""
    who = user or "对方"
    for key, human in _REASON:
        if r.startswith(key):
            return human.format(user=who)
    return r


def _called(text: str, aliases: list[str]) -> bool:
    body = str(text or "")
    return any(a and a in body for a in aliases)


def _render_event(e, aliases: list[str]) -> str:
    """一行材料：时间 + Event 自己的格式；「叫你」只在弹幕上标。"""
    clock = _clock(e.ts)
    if e.kind == ME:
        body = (e.text or "").replace("\n", " ")
        return f"  {clock} {body}"
    called = e.kind == "danmu" and _called(e.text, aliases)
    who = guard.clean_name(e.name) or "有人"
    return f"  {clock} {e.format_line(called=called, name=who)}"


def _screen_text(n_frames: int) -> str:
    if n_frames >= 2:
        return "画面还在动"
    if n_frames == 1:
        return "画面也停着"
    # 没有截图不等于"画面静止"：她本来就看不见，别让她说出自己没看见的事。
    return "你看不见画面"


def _cold_line(gap: float | None, viewers: int, n_frames: int, name: str,
               span: float = 0.0,
               live: bool = True, *, remember: bool = True) -> str:
    global _last_cold
    screen = _screen_text(n_frames)
    solo = not live
    viewer_text = ("" if solo else
                   (f"{viewers} 个人安静地看着" if viewers
                    else "一个观众都没有"))
    gap_s = int(gap) if gap else int(span)

    def render(clauses: tuple[str, ...], index: int) -> str:
        who = name or "对方"
        audience = _COLD_AUDIENCE.get(index, ("", ""))[1 if solo else 0]
        audience = audience.format(user=who)
        parts = []
        for clause in clauses:
            text = clause.format(
                name=name, gap=gap_s, user=who,
                viewers=viewer_text, screen=screen, audience=audience)
            text = text.strip("，。 ")
            if text:
                parts.append(text)
        return "，".join(parts) + "。"

    if gap is None:
        first = [(i, c) for i, c in enumerate(_COLD_FIRST)
                 if n_frames or not any("画面" in x for x in c)]
        index, clauses = random.choice(first or list(enumerate(_COLD_FIRST)))
        return render(clauses, index)

    # 没有画面时，别用"盯着屏幕""在专心打"这种需要看见才能说的句子。
    pool = [(i, c) for i, c in enumerate(_COLD)
            if n_frames or not any("盯着屏幕" in x or "在专心打" in x
                                   or "画面" in x for x in c)]
    index, clauses = random.choice(pool)
    if remember and len(pool) > 1 and index == _last_cold:
        index, clauses = random.choice([(i, c) for i, c in pool
                                        if i != _last_cold])
    if remember:
        _last_cold = index
    return render(clauses, index)


_SECTION_RE = re.compile(
    r"(?m)^[ \t]*[【\[]([^】\]\n]+)[】\]]\s*(.*?)(?=^[ \t]*[【\[]|\Z)",
    re.S)


def material_digest(text: str, label: str = "") -> str:
    """把一轮材料拆成「谁说了什么」；材料格式的知识只留在这个模块。

    只做结构压缩：丢掉重复的段名、把同一段的多行并起来；
    每段正文原样保留，不按字符截断。
    """
    t = str(text or "").strip()
    if not t:
        return ""
    user_label = f"{label}{SECTION_ME}"
    data: dict[str, list[str]] = {}
    known = {SECTION_NOW, SECTION_AUDIENCE, SECTION_SCENE, SECTION_FRAME,
             SECTION_COLD}
    for m in _SECTION_RE.finditer(t):
        name = m.group(1)
        body = re.sub(r"\s+", " ", m.group(2)).strip()
        if name not in (known | {user_label}):
            continue
        if name == SECTION_AUDIENCE:
            body = re.sub(r"^\d+\s*条\s*", "", body)
        if body:
            data.setdefault(name, []).append(body)

    parts = []
    if data.get(SECTION_NOW):
        parts.append(f"{SECTION_NOW}：" + "；".join(data[SECTION_NOW]))
    mine = data.get(user_label, [])
    if mine:
        parts.append(f"{label}：" + "；".join(mine))
    for section in (SECTION_AUDIENCE, SECTION_SCENE, SECTION_FRAME,
                    SECTION_COLD):
        if data.get(section):
            parts.append(f"{section}：" + "；".join(data[section]))
    return "\n".join(parts) if parts else t


def attach_scene(digest: str, scene: str) -> str:
    """把 `<scene>` 的落库行插到 `[画面]` 前；没有画面就放最前面。"""
    text = str(digest or "").strip()
    value = " ".join(str(scene or "").split()).strip()
    if not value:
        return text
    line = f"[{SECTION_SCENE}]{value}"
    if not text:
        return line
    lines = text.splitlines()
    for index, row in enumerate(lines):
        if row.startswith(f"{SECTION_FRAME}："):
            lines.insert(index, line)
            return "\n".join(lines)
    return line + "\n" + text


def _section_lines(text: str, name: str) -> list[str]:
    """取某个 `[段名]` 下的非空行；只用于 query 预热/回忆，不参与 prompt 拼装。"""
    out: list[str] = []
    for m in _SECTION_RE.finditer(str(text or "")):
        if m.group(1) != name:
            continue
        for line in m.group(2).splitlines():
            line = line.strip()
            if line:
                out.append(line)
    return out


def user_lines(text: str, label: str = "") -> list[str]:
    """本轮材料里用户真正说过的话（去掉行首时间）。"""
    lines = _section_lines(text, f"{label}{SECTION_ME}")
    return [re.sub(r"^\d{2}:\d{2}:\d{2}\s*", "", x).strip()
            for x in lines if x.strip()]


def audience_lines(text: str) -> list[str]:
    """本轮材料里的弹幕原文（去掉行首时间）。"""
    lines = _section_lines(text, SECTION_AUDIENCE)
    return [re.sub(r"^\d{2}:\d{2}:\d{2}\s*", "", x).strip()
            for x in lines if x.strip()]


def build_round(
    bus: EventBus,
    said: SaidLog,
    mode: str,
    seconds: float = 20.0,
    max_items: int = 14,
    evs=None,
    frame_ts=None,
    name: str = "",
    reason: str = "",
    bot_names: list[str] | None = None,
    live: bool = True,
    remember_cold: bool = True,
    pack=None,
    window_line: str = "",
    privacy_note: str = "",) -> tuple[str, dict]:
    """返回 (给模型的文本, 元信息)。

    `evs` 是**这批新材料**（调度器消费掉的那些）。传了就只用它——
    不能用"最近 N 秒"代替，否则同一句话会被重复回应好几轮。

    `seconds` 是这一段的实际跨度（距离上一轮多久），材料开头会照实写。
    `frame_ts` 是这一轮附带的截图时间戳（旧→新），只用来写[画面]那一行；
    传了空列表就写「没有截图」，传 None 表示调用方不关心画面（比如 /peek）。
    `pack` 是 composer.plan 的布局摘要；给了就按它写拼图/独立帧。
    `window_line` 是前台窗口标题行；`privacy_note` 非空时画面段只写隐私提示。
    `reason` 是调度器给的触发原因，会翻译成「本轮 他刚说完话」这类事实。
    `bot_names` 是叫你名字的别名，用来给弹幕标「叫你」。
    """
    evs = bus.recent(seconds) if evs is None else evs
    frames = list(frame_ts or [])
    aliases = [str(x) for x in (bot_names or []) if str(x).strip()]
    solo = not live
    mine = [e for e in evs if e.kind == ME]
    audience = [] if solo else [e for e in evs if e.kind not in (ME, ENTER)]
    enters = [] if solo else [e for e in evs if e.kind == ENTER]

    now = time.time()
    span = max(0.5, float(seconds))
    lines: list[str] = []

    head = [
        _clock(now),
        f"陪了 {int(said.session_minutes())} 分钟",
        f"气氛 {PROMPT_LABEL.get(mode, mode)}",
    ]
    if not solo:
        head += [f"在线 {bus.viewers} 人", f"最近 5 分钟 {bus.count_audience(300)} 条互动"]
    why = _reason_text(reason, name)
    if why:
        head.append(f"本轮 {why}")
    gap = said.gap()
    if gap is not None:
        head.append(_gap_text(gap))
    lines.append(f"[{SECTION_NOW}]" + "｜".join(head))

    last = said.recent(1)
    if last:
        text = last[0].replace("\n", " ")
        lines.append(f"[你上一句]{_ago_text(gap)}：{text}")

    lines.append(f"[这一段]最近 {int(span)} 秒"
                 f"（{_clock(now - span)} → {_clock(now)}）")

    if window_line:
        lines.append(str(window_line))

    if privacy_note:
        lines.append(f"[{SECTION_FRAME}]{privacy_note}。")
    elif pack is not None and getattr(pack, "source_count", 0):
        if frames:
            first, last = _clock(frames[0]), _clock(frames[-1])
            summary = str(getattr(pack, "summary", "") or "").rstrip("。")
            lines.append(
                f"[{SECTION_FRAME}]共 {pack.source_count} 帧"
                f"（{first} → {last}）：{summary}。"
                f"画面边缘的桌宠形象是你自己（不是游戏/视频角色）。")
        else:
            lines.append(f"[{SECTION_FRAME}]这一轮没有截图。")
    elif frames:
        stamps = " / ".join(_clock(t) for t in frames)
        if len(frames) == 1:
            tail = "只有一张，就是现在"
        else:
            tail = "从旧到新，最后一张是现在，见附图"
        lines.append(f"[{SECTION_FRAME}]{len(frames)} 张，{stamps}（{tail}）")
    elif frame_ts is not None:
        lines.append(f"[{SECTION_FRAME}]这一轮没有截图。")

    who = name
    if mine:
        lines.append(f"[{who}{SECTION_ME}]")
        for e in mine[-6:]:
            lines.append(_render_event(e, aliases))

    if audience:
        lines.append(f"[{SECTION_AUDIENCE}]{len(audience)} 条")
        for e in audience[-max_items:]:
            lines.append(_render_event(e, aliases))
        if len(audience) > max_items:
            lines.append(f"  …（还有 {len(audience) - max_items} 条没列出来）")
    elif enters:
        lines.append(f"[{SECTION_AUDIENCE}]没有，只有 {len(enters)} 个人进出直播间")

    if not mine and not audience:
        # 只有"真的冷场"才写这段。手动触发/预览时硬塞一句「安静了 N 秒」，
        # 等于每轮都在催她开口，人设里的"刚说完可以不说"就被材料架空了。
        if reason.startswith("冷场") or reason in ("", "预览"):
            lines.append(f"[{SECTION_COLD}]" + _cold_line(
                gap, bus.viewers, len(frames), name, span, live,
                remember=remember_cold))

    return "\n".join(lines), {"must_reply": bool(mine)}
