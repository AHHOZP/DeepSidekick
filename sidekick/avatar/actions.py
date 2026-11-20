"""形象动作能力：绑定当前形象，读 `actions.md`，解析 `<ACT>`。

`actions.md` 和 `<ACT>` 是同一份资产词表的两种呈现：
  · `actions.md`（角色目录）→ 注入 prompt；
  · `capabilities`（model3 / Spine 动画名）→ 校验资产名、驱动前端。

本地表演标签只有 `<ACT>`；provider 的方括号/圆括号标签原样交给 TTS。
"""
from __future__ import annotations

from sidekick.avatar import files as avatar_files
from sidekick.avatar import act_tags
from sidekick.avatar.autonomy import store as profile_store


def _caps(avatar) -> dict:
    cap = getattr(avatar, "capabilities", None)
    return cap if isinstance(cap, dict) else {}


def _action_map(caps: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in caps.get("actions") or []:
        if isinstance(item, dict) and item.get("tag") and item.get("group"):
            out[str(item["tag"])] = str(item["group"])
    return out


def _expr_map(caps: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in caps.get("expressions") or []:
        if isinstance(item, dict) and item.get("tag") and item.get("name"):
            out[str(item["tag"])] = str(item["name"])
    return out


class AvatarActions:
    def __init__(self, avatar=None, *, actions_enabled: bool = True) -> None:
        self.avatar = avatar
        self.actions_enabled = bool(actions_enabled)
        self._profile_prompt = ""
        self._action_to_motion: dict[str, str] = {}
        self._disabled_motions: set[str] = set()
        self._channels: set[str] = set()
        self._states: set[str] = set()
        self._custom_actions: dict[str, dict] = {}
        self._disabled_actions: set[str] = set()
        self._prompt_data: dict = {}
        self._rebuild(load_profile=True)

    def bind_motion_profile(self, profile) -> None:
        """把运行时自省结果接到能力卡；profile 文件可能比模型旧。"""
        if profile is None:
            return
        self._channels = set(getattr(profile, "by_channel", {}) or {})
        self._states = set(getattr(profile, "states", {}) or {})
        self._custom_actions = {
            str(name): dict(data)
            for name, data in (getattr(profile, "custom_actions", {}) or {}).items()
        }
        if self._prompt_data:
            self._profile_prompt = act_tags.prompt_rules_from_profile(
                self._prompt_data, channels=self._channels,
                states=self._states,
                extra_actions=self._extra_action_labels())

    def bind(self, avatar) -> None:
        """热切换形象：重新绑定，规则读新形象的 actions.md。

        profile 不热重载：同一个 avatar.id 上每 15 秒的 bind 只刷新能力/规则。
        """
        old_id = self.avatar_id
        self.avatar = avatar
        self._rebuild(load_profile=(self.avatar_id != old_id))

    def _load_profile(self) -> None:
        self._profile_prompt = ""
        self._action_to_motion = {}
        self._disabled_motions = set()
        self._channels = set()
        self._states = set()
        self._custom_actions = {}
        self._disabled_actions = set()
        self._prompt_data = {}
        if self.avatar is None:
            return
        data, _error = profile_store.load(self.avatar)
        if not data:
            return
        version = data.get("schema_version")
        if version is not None and version != 1:
            return
        bindings = data.get("channel_bindings")
        if isinstance(bindings, dict):
            self._channels = {str(k) for k in bindings}
        states = data.get("states")
        if isinstance(states, dict):
            self._states = {str(k) for k in states}
        actions = data.get("actions")
        if isinstance(actions, dict):
            for name, settings in actions.items():
                if not isinstance(settings, dict):
                    continue
                if settings.get("enabled") is False:
                    self._disabled_actions.add(str(name))
                    continue
                if any(key in settings for key in ("pose", "loops", "aliases")):
                    self._custom_actions[str(name)] = settings
        if isinstance(data.get("prompt"), dict):
            self._prompt_data = dict(data["prompt"])
            self._profile_prompt = act_tags.prompt_rules_from_profile(
                self._prompt_data, channels=self._channels, states=self._states,
                extra_actions=self._extra_action_labels(),
                disabled_actions=self._disabled_actions)
        policy = data.get("motion_policy")
        motions = policy.get("model_motions") if isinstance(policy, dict) else None
        if not isinstance(motions, dict):
            return
        for group, settings in motions.items():
            if not isinstance(settings, dict):
                continue
            if not settings.get("enabled", True):
                self._disabled_motions.add(str(group).casefold())
                continue
            action = str(settings.get("action") or "")
            if action and str(group) in self._actions.values():
                self._action_to_motion[action] = str(group)

    def _rebuild(self, *, load_profile: bool = False) -> None:
        caps = _caps(self.avatar)
        self._actions = _action_map(caps)
        self._expressions = _expr_map(caps)
        self._broken = list(getattr(self.avatar, "broken", None) or [])
        # 模型资产只接受带 motion=/expression= 前缀的名字；这里同时认 tag 和
        # group/name，大小写不敏感，输出仍用模型真实名字。
        self._motions = {}
        for tag, group in self._actions.items():
            self._motions[tag.casefold()] = group
            self._motions[group.casefold()] = group
        self._exprs = {}
        for tag, name in self._expressions.items():
            self._exprs[tag.casefold()] = name
            self._exprs[name.casefold()] = name
        if load_profile:
            self._load_profile()

    def _extra_action_labels(self) -> list[str]:
        labels = []
        for name, settings in self._custom_actions.items():
            if name in self._disabled_actions:
                continue
            aliases = settings.get("aliases") or ()
            label = next((str(x) for x in aliases if str(x).strip()), name)
            if label not in labels:
                labels.append(label)
        if any(ch in self._channels for ch in (
                "leg_l", "leg_r", "leg_l_x", "leg_r_x", "knee_l", "knee_r",
                "ankle_l", "ankle_r", "leg_l_y", "leg_r_y", "knee_l_y",
                "knee_r_y", "foot_l", "foot_r", "foot_l_y", "foot_r_y",
                "foot_swing")):
            if "活动腿" not in labels:
                labels.append("活动腿")
        if "chibi_mode" in self._states and "正常比例" not in labels:
            labels.append("正常比例")
        return labels

    def _actions_for_parse(self) -> dict:
        from sidekick.motion.actions import DEFAULT_ACTIONS

        # 自定义动作先放：profile 的别名要能覆盖内建动作的同名别名
        # （例如自定义“打开”的别名“前倾”，不能被内建 lean 抢走）。
        out: dict = {}
        for name, settings in self._custom_actions.items():
            if name in self._disabled_actions:
                continue
            out[name] = {
                "aliases": tuple(settings.get("aliases") or ()),
            }
        for name, spec in DEFAULT_ACTIONS.items():
            if name not in self._disabled_actions:
                out.setdefault(name, spec)
        return out

    @property
    def avatar_id(self) -> str:
        return str(getattr(self.avatar, "id", "") or "")

    @property
    def broken(self) -> list[str]:
        return list(self._broken)

    def rules(self) -> str:
        if self.avatar is None or not self.actions_enabled:
            return ""
        return avatar_files.read(self.avatar)

    def vocabulary(self) -> dict:
        vocab = act_tags.vocabulary()
        vocab["actions"] = [
            item for item in vocab.get("actions") or []
            if item.get("name") not in self._disabled_actions
        ]
        if self._custom_actions:
            for name, settings in self._custom_actions.items():
                if name in self._disabled_actions:
                    continue
                aliases = [str(x) for x in (settings.get("aliases") or ())
                           if str(x).strip()]
                vocab["actions"].append({
                    "name": name,
                    "label": aliases[0] if aliases else name,
                    "aliases": aliases,
                })
        if self._states:
            return vocab
        hidden = {"long_skirt", "short_skirt", "chibi_mode", "normal_body"}
        vocab["actions"] = [
            item for item in vocab.get("actions") or []
            if item.get("name") not in hidden
        ]
        return vocab

    def prompt_rules(self) -> str:
        if not self.actions_enabled:
            return ""
        if self._profile_prompt:
            return self._profile_prompt
        return act_tags.prompt_rules(
            self.vocabulary(), channels=self._channels, states=self._states)

    def parse(self, text: str) -> act_tags.ActParse:
        parsed = act_tags.parse(
            text, motions=self._motions, expressions=self._exprs,
            actions=self._actions_for_parse())
        if self._disabled_actions:
            parsed.items = [
                item for item in parsed.items
                if not (item.get("kind") == "action"
                        and item.get("name") in self._disabled_actions)
            ]
        if not self.actions_enabled:
            # 规则关闭：标签仍从正文剥掉，但不产生任何表演意图。
            parsed.items = []
            parsed.warnings = []
            return parsed
        if "chibi_mode" in self._states:
            for item in parsed.items:
                if (item.get("kind") == "expression"
                        and any(cue in str(item.get("name") or "")
                                for cue in ("默认体型", "正常体型"))):
                    item["kind"] = "action"
                    item["name"] = "normal_body"
                    parsed.warnings.append(
                        "expression 默认体型已改为持久状态动作 normal_body")
        if not any(item.get("kind") == "action" for item in parsed.items):
            inferred = act_tags.infer_action(parsed.text)
            if inferred and inferred not in self._disabled_actions:
                parsed.items.append({
                    "kind": "action", "name": inferred, "intensity": 1.0,
                })
        if self._action_to_motion:
            rewritten = []
            for item in parsed.items:
                group = (self._action_to_motion.get(str(item.get("name") or ""))
                         if item.get("kind") == "action" else "")
                if group:
                    item = {**item, "kind": "motion", "name": group}
                rewritten.append(item)
            parsed.items = rewritten
        if self._disabled_motions:
            kept = []
            for item in parsed.items:
                if (item.get("kind") == "motion"
                        and str(item.get("name") or "").casefold()
                        in self._disabled_motions):
                    parsed.warnings.append(
                        f"motion {item.get('name')} 已被 profile 禁用，已丢弃")
                    continue
                kept.append(item)
            parsed.items = kept
        return parsed

    def summarize(self, acts: list[dict]) -> str:
        return summarize(acts)


def summarize(acts: list[dict]) -> str:
    """给日志用的一句话；资产和语义都写 kind/name。"""
    if not acts:
        return ""
    return " ".join(
        f"{a.get('kind')}:{a.get('name')}" if a.get("kind") else str(a.get("name") or "")
        for a in acts)
