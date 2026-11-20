"""对话预设：`persona/presets/<id>/preset.yaml` 的读写。

预设是提示词里回答「怎么做」的那一层：这是什么场合、为什么开口、材料怎么读、
输出成什么样、直播时追加什么。它和角色人设（这个人是谁）、世界书（游戏资料）
正交，换角色不用重写规则，换预设也不用改人设。

一个预设 = 一组条目。每条声明三件事：

  slot    落在哪两个锚点之间（封闭小集合，见 `SLOTS`；锚点是 prompt 里的固定内容）
  role    system / user（assistant 是历史的位置，预设不占）
  live    true 的条目只在直播模式注入；单人陪玩时一条都不出现

**文件里的书写顺序就是同一个 `slot` 里的先后**：网页把这些固定内容画成锚点，条目拖到
哪两个锚点之间，保存时就记哪个 `slot`；同一个 `slot` 里按书写顺序排，网页会把条目按
prompt 顺序写回文件。锚点自己不能移动。

固定内容由 `PromptBuilder` 填，顺序是：

    角色设定 → 用户设定 → 游戏资料（背景）→ 记忆（常驻）
    → 历史信息 → 记忆（召回）→ 游戏资料（召回条目）→ 本轮材料 → 动作/TTS 规则
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from sidekick.core import atomic

PRESETS_REL = ("persona", "presets")
FILE = "preset.yaml"
HEADER = ("# 对话预设：同一个 slot 里的条目按文件里的书写顺序；"
          "网页按锚点位置决定 slot。\n"
          "# slot: start（角色设定之前）/ after_persona（用户设定之前）/ after_user（游戏资料之前）/\n"
          "#       after_worldbook（长期记忆之前）/ after_memory（交互历史之前）/\n"
          "#       after_history（召回记忆之前）/ after_recall_memory（召回游戏资料之前）/\n"
          "#       after_recall_worldbook（本轮互动消息之前）/ after_material（本轮互动消息之后）\n"
          "# role: system / user；live: true 的条目只在直播模式注入。\n")

#: 网页上的锚点和它们之间的缝隙。锚点是 prompt 里的一块固定内容，条目只能落在锚点
#: 之间：`slot` 是「`anchor` 之前」的缝隙，`start` 是第一个锚点之前，
#: `after_material` 是最后一个锚点之后（没有 anchor）。锚点自己不能移动。
GAPS = (
    ("start", "角色设定"),
    ("after_persona", "用户设定"),
    ("after_user", "游戏资料"),
    ("after_worldbook", "长期记忆"),
    ("after_memory", "交互历史"),
    ("after_history", "召回记忆"),
    ("after_recall_memory", "召回游戏资料"),
    ("after_recall_worldbook", "本轮互动消息"),
    ("after_material", ""),
)
SLOTS = tuple(slot for slot, _anchor in GAPS)
ROLES = ("system", "user")

_ID_RE = re.compile(r"^[\w.-]{1,40}$")


def presets_dir(root) -> Path:
    return Path(root).joinpath(*PRESETS_REL)


def preset_dir(root, preset_id: str) -> Path:
    return presets_dir(root) / str(preset_id or "")


def preset_path(root, preset_id: str) -> Path:
    return preset_dir(root, preset_id) / FILE


def valid_id(preset_id: str) -> bool:
    pid = str(preset_id or "")
    return bool(_ID_RE.match(pid)) and pid not in (".", "..")


@dataclass(frozen=True)
class Block:
    id: str
    title: str
    slot: str
    role: str
    live: bool
    text: str


@dataclass(frozen=True)
class Preset:
    id: str
    name: str = ""
    desc: str = ""
    blocks: tuple[Block, ...] = ()
    error: str | None = None

    def blocks_for(self, slot: str, *, live: bool = False) -> list[Block]:
        """取一个缝隙里的条目：先筛 slot，单人陪玩时再剔掉直播条目。

        文件里的书写顺序就是同一个 slot 里的先后。
        """
        return [b for b in self.blocks
                if b.slot == slot and (live or not b.live)]

    def messages(self, slot: str, *, live: bool = False) -> list[dict]:
        """缝隙 → messages；空缝隙不占一条。"""
        out = []
        for block in self.blocks_for(slot, live=live):
            if block.text.strip():
                out.append({"role": block.role, "content": block.text})
        return out


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def normalize_blocks(raw) -> tuple[tuple[Block, ...], list[str]]:
    """按书写顺序归一；坏条目跳过并记一条原因，不阻塞其它条目。"""
    blocks: list[Block] = []
    errors: list[str] = []
    if raw is None:
        return (), errors
    if not isinstance(raw, list):
        return (), ["blocks 必须是列表"]
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"第 {i + 1} 条不是字典")
            continue
        text = str(item.get("text") or "")
        if not text.strip():
            continue
        slot = str(item.get("slot") or "").strip().lower()
        if not slot:
            errors.append(f"第 {i + 1} 条缺少 slot")
            continue
        if slot not in SLOTS:
            errors.append(f"第 {i + 1} 条的 slot 不认识：{slot}")
            continue
        role = str(item.get("role") or "system").strip().lower()
        if role not in ROLES:
            errors.append(f"第 {i + 1} 条的 role 只能是 system/user：{role}")
            continue
        bid = str(item.get("id") or "").strip() or f"{slot}-{i + 1}"
        if bid in seen:
            bid = f"{bid}-{i + 1}"
        seen.add(bid)
        title = str(item.get("title") or "").strip() or bid
        blocks.append(Block(id=bid, title=title, slot=slot, role=role,
                            live=_as_bool(item.get("live")),
                            text=text.strip("\n")))
    return tuple(blocks), errors


def read(root, preset_id: str) -> Preset:
    """读预设；文件不存在就是空预设，格式错只在 `error` 里说，不抛。"""
    pid = str(preset_id or "").strip()
    path = preset_path(root, pid)
    if not path.is_file():
        return Preset(id=pid, error=f"预设不存在：{path.as_posix()}")
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as e:
        return Preset(id=pid, error=f"{FILE} 读取失败：{type(e).__name__}: {e}")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return Preset(id=pid, error=f"{FILE} 解析失败：{e}")
    if data is None:
        return Preset(id=pid)
    if not isinstance(data, dict):
        return Preset(id=pid, error=f"{FILE} 顶层必须是字典")
    blocks, errors = normalize_blocks(data.get("blocks"))
    return Preset(
        id=pid,
        name=str(data.get("name") or "").strip(),
        desc=str(data.get("desc") or "").strip(),
        blocks=blocks,
        error="；".join(errors) or None,
    )


def list_ids(root) -> list[str]:
    base = presets_dir(root)
    if not base.is_dir():
        return []
    try:
        return sorted(p.name for p in base.iterdir()
                      if p.is_dir() and (p / FILE).is_file() and valid_id(p.name))
    except OSError:
        return []


def _scalar(value) -> str:
    """单个标量的 YAML 写法（该加引号就加）。"""
    text = yaml.safe_dump(value, allow_unicode=True, width=1000,
                          default_flow_style=False)
    return text.rstrip("\n").removesuffix("\n...").strip()


def _block_text(text: str) -> list[str]:
    """正文用 `|-` 块标量写：预设是给人改的文件，不要转义成一长串单引号。"""
    if not text:
        return ["  text: ''"]
    body = text.splitlines()
    if not body:
        return ["  text: ''"]
    return ["  text: |-"] + ["    " + line for line in body]


def dump(preset_id: str, name: str, desc: str, blocks) -> str:
    lines = [f"name: {_scalar(str(name or '').strip())}",
             f"desc: {_scalar(str(desc or '').strip())}",
             "blocks:"]
    if not blocks:
        lines[-1] = "blocks: []"
    for b in blocks:
        lines.append(f"- id: {_scalar(b.id)}")
        lines.append(f"  title: {_scalar(b.title)}")
        lines.append(f"  slot: {b.slot}")
        lines.append(f"  role: {b.role}")
        lines.append(f"  live: {'true' if b.live else 'false'}")
        lines.extend(_block_text(b.text))
    return HEADER + "\n".join(lines) + "\n"


def write(root, preset_id: str, name: str, desc: str, blocks) -> Path:
    """原子写整份预设；条目全空也照写（空预设是合法状态）。"""
    pid = str(preset_id or "").strip()
    if not valid_id(pid):
        raise ValueError(f"预设名只能用字母/数字/下划线/短横线/点：{pid!r}")
    path = preset_path(root, pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, dump(pid, name, desc, blocks))
    return path


def delete(root, preset_id: str) -> None:
    path = preset_path(root, preset_id)
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


@dataclass
class PresetSource:
    """按 mtime 热重读的预设：WebUI 改完下一轮生效，不用重启。"""
    root: Path
    preset_id: str
    preset: Preset = field(default_factory=lambda: Preset(id=""))
    _fingerprint: tuple = ()

    def reload(self) -> None:
        self.preset = read(self.root, self.preset_id)
        self._fingerprint = self._mtime()

    def _mtime(self) -> tuple:
        path = preset_path(self.root, self.preset_id)
        try:
            st = path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return (0, 0)

    def refresh(self) -> Preset:
        if self._mtime() != self._fingerprint:
            self.preset = read(self.root, self.preset_id)
            self._fingerprint = self._mtime()
        return self.preset
