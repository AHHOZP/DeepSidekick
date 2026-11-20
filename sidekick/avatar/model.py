"""形象的数据形状：只有字段和前端序列化，不做扫描/体检/缓存。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote


@dataclass
class Avatar:
    id: str                            # 模型内容指纹（渲染/缓存键）
    name: str                          # 模型文件夹名 / 图片文件名
    role_id: str = ""                  # 角色设定目录名；空 = name（扫描时分配，重名加 -2）
    kind: str = "live2d"               # live2d | spine | image
    root: Path = Path(".")
    entry: str = ""                    # model3 或图片相对 root
    capabilities: dict = field(default_factory=dict)
    model3: dict | None = None
    model_version: int = 0
    broken: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    idle: str = "idle"
    # ── 角色设定（persona/roles/<id>/）──────────────────
    role_dir: Path | None = None
    display_name: str = ""             # 显示名；空 = 文件夹名
    stage: dict = field(default_factory=dict)          # 这套形象的外观覆盖
    profile_error: str | None = None   # 角色设定读不出来时的原因

    @property
    def usable(self) -> bool:
        return not self.broken

    @property
    def shown_name(self) -> str:
        return self.display_name or self.role_id or self.name

    def to_dict(self) -> dict:
        version = f"?v={self.model_version}" if self.model_version else ""
        rel = ("__avatar.model3.json" if self.kind == "live2d" and self.model3 is not None
               else self.entry)
        url = f"/m/{quote(self.id, safe='')}/{quote(rel, safe='/')}{version}"
        # 只保留前端真正读的字段。
        return {
            "id": self.id,
            "role_id": self.role_id or self.name,
            "name": self.name,
            "display_name": self.shown_name,
            "kind": self.kind,
            "entry": self.entry,
            "model": url,
            "modelVersion": self.model_version,
            "capabilities": self.capabilities,
            "usable": self.usable,
            "broken": list(self.broken),
            "idle": self.idle,
            "profile_error": self.profile_error,
        }
