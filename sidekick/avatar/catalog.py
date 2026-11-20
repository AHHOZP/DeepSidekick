"""形象目录：扫描、缓存、选择；不推断模型细节。

选择（`role.id`）只存在 `config.yaml` 里，由组合根注入（`selection` 回调）；
本模块不写任何状态文件。运行中的进程在启动时 `pin()` 钉住当时的选择。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from sidekick.avatar import roles
from sidekick.avatar.builder import (
    IMAGE_SUFFIXES, build_avatar, build_image, build_spine, idle_of,
)
from sidekick.avatar.model import Avatar
from sidekick.avatar.scan import find_model3, find_spine, read_json, rel
from sidekick.spine import names as spine_names

# 页面每 5 秒拉一次 /config.json；缓存至少覆盖这个轮询周期，
# 否则每次请求都会 rglob + 体检，阻塞 stage 事件循环。
_CACHE_TTL = 30.0

#: 单张图片也可以统一收在这个子目录里（根目录同样认，别的地方不认）。
IMAGE_SUBDIR = "images"


class AvatarCatalog:
    """一个根目录下的形象目录。缓存 30 秒，显式 refresh 跳过缓存。"""

    def __init__(self, root: str | Path, *,
                 selection: Callable[[], str] | None = None) -> None:
        self.root = Path(root)
        self.models_dir = self.root / "models"
        self._selection = selection
        self._cache: tuple[float, list[Avatar]] | None = None
        self._pinned = ""          # 启动时钉住的选择；换角色下次启动才生效
        self._pinned_avatar: Avatar | None = None
        self._lock = threading.RLock()

    def discover(self, *, refresh: bool = False) -> list[Avatar]:
        now = time.monotonic()
        with self._lock:
            if (not refresh and self._cache is not None
                    and now - self._cache[0] < _CACHE_TTL):
                return list(self._cache[1])

        avatars: list[Avatar] = []
        by_id: dict[str, Avatar] = {}
        by_role: dict[str, Avatar] = {}
        role_models = roles.model_index(self.root)
        if self.models_dir.is_dir():
            for m3 in find_model3(self.models_dir):
                model_root = m3.parent
                if self._skip(model_root):
                    continue
                try:
                    a = build_avatar(model_root, m3)
                except Exception as e:
                    a = Avatar(id=f"broken-{model_root.name}",
                               name=model_root.name, kind="live2d",
                               root=model_root, entry=rel(m3, model_root),
                               broken=[f"解析失败：{type(e).__name__}: {e}"])
                _push(avatars, by_id, by_role, self.root, a, role_models)

        # Spine：和 Live2D 一样，扫描只回答"有哪些、能不能加载"，不读动画名。
        if self.models_dir.is_dir():
            for entry in find_spine(self.models_dir):
                model_root = entry.parent
                if self._skip(model_root):
                    continue
                try:
                    a = build_spine(model_root, entry)
                except Exception as e:
                    a = Avatar(id=f"broken-{model_root.name}",
                               name=model_root.name, kind="spine",
                               root=model_root, entry=rel(entry, model_root),
                               broken=[f"解析失败：{type(e).__name__}: {e}"])
                _push(avatars, by_id, by_role, self.root, a, role_models)

        # 单张图片：models/ 根目录和 models/images/ 两处都认，别的地方不扫——
        # 免得把模型内部的纹理当成独立形象。
        for folder in (self.models_dir, self.models_dir / IMAGE_SUBDIR):
            if not folder.is_dir():
                continue
            for path in sorted(folder.iterdir()):
                if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                try:
                    a = build_image(self.models_dir, path)
                except OSError as e:
                    a = Avatar(id=f"broken-{path.stem}", name=path.stem,
                               kind="image", root=self.models_dir,
                               entry=rel(path, self.models_dir),
                               broken=[f"图片读取失败：{e}"])
                _push(avatars, by_id, by_role, self.root, a, role_models)

        with self._lock:
            self._cache = (now, avatars)
        return list(avatars)

    def invalidate(self) -> None:
        """角色设定/模型目录变了：下次 discover 重新扫描。"""
        with self._lock:
            self._cache = None

    def rescan(self) -> list[Avatar]:
        try:
            self.models_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return self.discover(refresh=True)

    def get(self, avatar_id: str) -> Avatar | None:
        aid = str(avatar_id or "").strip()
        for a in self.discover():
            if a.id == aid or a.role_id == aid or a.name == aid:
                self._ensure_caps(a)
                return a
        return None

    def _skip(self, model_root: Path) -> bool:
        try:
            parts = model_root.relative_to(self.models_dir).parts
        except ValueError:
            return True
        return any(part in {"node_modules", ".git", "__pycache__"} for part in parts)

    def _ensure_caps(self, avatar: Avatar | None) -> None:
        """Spine 的动画名在“这个形象被用到”时才读。

        `.skel` 是二进制，名字只能问原生库；而那条调用跑在子进程里（`sidekick/spine/names`），
        所以没被点过的模型不会产生任何开销，也不会把 30G 素材一次性读进内存。
        """
        if avatar is None or avatar.kind != "spine":
            return
        with self._lock:
            if avatar.capabilities.get("actions"):
                return
        names, error = spine_names.read_names(avatar.root / avatar.entry)
        with self._lock:
            if avatar.capabilities.get("actions"):
                return
            if not names:
                if error and error not in avatar.warnings:
                    avatar.warnings.append(error)
                return
            avatar.capabilities["actions"] = [{"tag": n, "group": n}
                                               for n in names]
            avatar.idle = avatar.capabilities["idle"] = idle_of(names)

    def wanted(self) -> str:
        """启动时钉住的选择优先；没有就按配置里的 role.id。"""
        if self._pinned:
            return self._pinned
        if self._selection is None:
            return ""
        try:
            return str(self._selection() or "").strip()
        except Exception:
            return ""

    def pin(self) -> None:
        """把启动时的选择钉住。

        人设、形象、动作能力在启动时一次性装配；换角色要重启，
        否则会出现"新脸旧人设"这种直播里很难排查的混合态。
        """
        try:
            chosen = self.selected()
            self._pinned = chosen.role_id
            self._pinned_avatar = chosen
        except LookupError:
            self._pinned = ""
            self._pinned_avatar = None

    def selected(self) -> Avatar:
        all_avatars = self.discover()
        if not all_avatars:
            if self._pinned_avatar is not None:
                return self._pinned_avatar
            raise LookupError("models 里没有可用的形象")
        by_id = {a.id: a for a in all_avatars}
        by_role = {a.role_id: a for a in all_avatars if a.role_id}
        if self._pinned:
            pinned = by_role.get(self._pinned) or by_id.get(self._pinned)
            if pinned is None:
                # 运行中模型被移走/改名：继续用启动时钉住的快照，不偷偷换脸。
                if self._pinned_avatar is not None:
                    return self._pinned_avatar
            elif not pinned.usable:
                # 模型还在但坏了：也不能拿另一张脸顶上；保留坏状态让 /status
                # 和 WebUI 看到，等用户修好或重启。
                if self._pinned_avatar is not None:
                    return self._pinned_avatar
                return pinned
            else:
                self._ensure_caps(pinned)
                return pinned
        key = self.wanted()
        if key in by_role and by_role[key].usable:
            chosen = by_role[key]
        elif key in by_id and by_id[key].usable:
            # 旧配置里 role.id 还是内容指纹：先沿用，下一次保存会写成 role_id。
            chosen = by_id[key]
        else:
            usable = [a for a in all_avatars if a.usable]
            if usable:
                chosen = usable[0]
            elif key in by_role:
                chosen = by_role[key]
            else:
                chosen = all_avatars[0]
        self._ensure_caps(chosen)
        return chosen

    def effective_model3(self, avatar: Avatar) -> dict:
        if avatar.model3 is not None:
            return avatar.model3
        return read_json(avatar.root / avatar.entry)


def _assign_role_id(avatar: Avatar, by_role: dict[str, Avatar],
                    model_roles: dict[str, str], root: Path) -> None:
    """模型 → 角色目录：优先读 profile.yaml 的 model 绑定，其次用初始目录名。

    目录名只是角色 id 的默认值，不是绑定依据；模型文件夹改名后旧 profile
    找不到它，这个形象就按新角色处理。
    """
    base = str(avatar.name or avatar.id).strip() or avatar.id
    mapped = model_roles.get(base, "")
    if mapped and not roles.profile_matches(root, mapped, avatar):
        # profile 绑到了另一个同名模型；不能把这张脸接过去。
        mapped = ""
    rid = mapped or base
    base_dir = roles.role_dir(root, base)
    stale_base = (rid == base and base_dir.is_dir()
                  and not roles.profile_matches(root, base, avatar))
    if rid in by_role or stale_base:
        # 旧 profile 没有 model_digest 时，先把当前占用 base 的模型指纹补进去，
        # 否则删掉它之后，幸存者会再次把 base 当成自己的角色目录。
        owner = by_role.get(rid)
        if rid == base and owner is not None:
            roles.bind_model(roles.role_dir(root, rid),
                             str(getattr(owner, "name", "") or ""),
                             str(getattr(owner, "id", "") or ""))
        # 同名文件夹用内容指纹前 4 位消歧，删模型/重扫后不会因序号回退串台。
        digest = str(getattr(avatar, "id", "") or "")
        if digest and not digest.startswith("broken-"):
            candidate = f"{base}-{digest[:4]}"
            if candidate not in by_role:
                rid = candidate
        if rid in by_role:
            n = 2
            while f"{base}-{n}" in by_role:
                n += 1
            rid = f"{base}-{n}"
    avatar.role_id = rid
    by_role[rid] = avatar


def _push(avatars: list[Avatar], by_id: dict[str, Avatar],
          by_role: dict[str, Avatar], root: Path, avatar: Avatar,
          model_roles: dict[str, str]) -> None:
    """内容完全相同的两份模型会撞 id；后扫到的加后缀，各自独立。"""
    base = avatar.id
    n = 2
    while avatar.id in by_id:
        avatar.id = f"{base}-{n}"
        n += 1
    _assign_role_id(avatar, by_role, model_roles, root)
    roles.apply(avatar, root)
    by_id[avatar.id] = avatar
    avatars.append(avatar)
