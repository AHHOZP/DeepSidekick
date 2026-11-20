"""WebUI 的配置 schema。

字段路径/分组/label/help/选项/范围来自 `config_spec.py`；本模块只把它们
和当前值、密钥状态组合成前端表单。默认值也由 config_spec 生成。
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from sidekick import config
from sidekick.avatar import roles
from sidekick.brain import preset, users
from sidekick.config import llm_presets, secrets, tts_presets
from sidekick.config.spec import GROUPS, HELP, LABELS, NUM_RULES, OPTIONS, PROFILE_FIELDS
from sidekick.memory.embed import catalog as embed_catalog
from sidekick.memory.limits import WORLDBOOK_WARN_CHARS
from sidekick.webui.http import PERSONA_NAMES

MIN_BY_PATH = {path: lo for path, lo, _hi, _integer in NUM_RULES}
_LLM_FIELD_META = {f["key"]: f for f in PROFILE_FIELDS}
_LLM_FIELD_HELP = {
    "temperature": "这个 profile 的采样温度。",
    "reasoning_effort": "留空 = 不传；服务端不认会自动去掉重试。",
}


def _type_for(path: str, value: Any, default: Any) -> str:
    if secrets.is_secret_path(path):
        return "password"
    if isinstance(default, bool):
        return "checkbox"
    if isinstance(default, int):
        return "number"
    if isinstance(default, float):
        return "number"
    if isinstance(default, list):
        return "list"
    if isinstance(default, dict):
        return "json"
    return "text"


def _installed_models() -> list[tuple[str, str]]:
    """stt.model 下拉的选项：只列文件齐了的模型。

    当前选中的那个哪怕没装也留着（标出来），否则一保存就把配置改掉了。
    """
    try:
        from sidekick.input.stt.engines import catalog, registry

        cfg = config.load()
        st = cfg["stt"]
        cur = str(st["model"])
        specs = registry.load_specs(cfg, config.ROOT)
        order = [e.id for e in catalog.all_entries()]
        ids = sorted(specs, key=lambda m: (order.index(m) if m in order else 999, m))
        out: list[tuple[str, str]] = []
        for mid in ids:
            spec = specs[mid]
            if spec.missing():
                if mid == cur:
                    out.append((mid, f"{mid}（没装齐）"))
                continue
            e = catalog.get(mid)
            size = f" · {e.size}" if e else ""
            out.append((mid, f"{e.name if e else mid}{size}"))
        return out
    except Exception:
        return []


def _installed_embed_models(root) -> list[tuple[str, str]]:
    """memory.embed_model 下拉：只列已装齐的；当前选中的即使没装也留着。"""
    from pathlib import Path

    try:
        cfg = config.load()
        mem = cfg["memory"]
        rel = Path(str(mem.get("embed_dir") or "state/embed"))
        base = rel if rel.is_absolute() else Path(root) / rel
        cur = str(mem.get("embed_model") or "")
        out: list[tuple[str, str]] = []
        for entry in embed_catalog.all_entries():
            missing = entry.missing(base / entry.id)
            if missing:
                if entry.id == cur:
                    out.append((entry.id, f"{entry.name}（没装齐）"))
                continue
            out.append((entry.id, f"{entry.name} · {entry.size}"))
        if not out and cur:
            out.append((cur, f"{cur}（没装齐）"))
        return out
    except Exception:
        return []


def _field(path: str, redacted: dict, raw: dict, status: dict[str, bool],
           defaults: dict, root) -> dict:
    default = config.get_path(defaults, path)
    value = config.get_path(redacted, path)
    secret = secrets.is_secret_path(path)
    configured = bool(status.get(path))
    if secret:
        value = ""
    ftype = _type_for(path, value, default)
    opts = OPTIONS.get(path)
    if path == "stt.model":
        # 只列已下载的模型：没下载的选了也起不来，而且会让人以为换模型只要点一下
        opts = _installed_models()
    elif path == "prompt.id":
        opts = [(pid, pid) for pid in preset.list_ids(root)]
        cur = str(value or "")
        if cur and cur not in [v for v, _ in opts]:
            # 指向的预设文件不在了也留着：否则一保存就把配置改掉。
            opts.append((cur, f"{cur}（不存在）"))
    elif path == "memory.embed_model":
        opts = _installed_embed_models(root)
    elif path == "llm.source":
        llm = raw.get("llm") or {}
        profiles = [str(k) for k, v in llm.items()
                    if k not in ("source", "common") and isinstance(v, dict)]
        opts = [(name, name) for name in profiles]
    if opts:
        ftype = "select"
    label = LABELS.get(path) or path.split(".")[-1]
    return {
        "path": path,
        "label": label,
        "type": ftype,
        "value": value,
        "help": HELP.get(path, ""),
        "secret": secret,
        "configured": configured,
        "options": [{"value": v, "label": t} for v, t in (opts or [])],
        "min": MIN_BY_PATH.get(
            path, 0 if default is not None and default >= 0 else None)
        if ftype == "number" else None,
        "step": 0.1 if isinstance(default, float) else (1 if isinstance(default, int) else None),
    }


def build_schema(catalog, root) -> dict:
    defaults = config.defaults()
    cfg = config.load()
    raw = config.load_raw()
    red = config.redacted(cfg)
    status = secrets.status(config.load_raw())
    groups = []
    for g in GROUPS:
        sections = []
        for sec in g["sections"]:
            fields = [_field(p, red, cfg, status, defaults, root)
                      for p in sec["fields"]]
            section = {"title": sec["title"], "fields": fields}
            if sec.get("desc"):
                section["desc"] = sec["desc"]
            if sec.get("provider"):
                section["provider"] = sec["provider"]
            if sec.get("advanced"):
                section["advanced"] = True
            sections.append(section)
        groups.append({"id": g["id"], "title": g["title"],
                       "desc": g.get("desc", ""), "sections": sections})

    # LLM profiles 是动态的：以 config.yaml 里真正保存的为准，默认值只补字段，
    # 不在页面上造出并不存在的 profile。当前 source 指向的 profile 仍要列出来。
    llm = raw.get("llm") or {}
    cfg_llm = cfg.get("llm") or {}
    source = str(cfg_llm.get("source") or "").strip()
    names = [name for name, block in llm.items()
             if name not in ("source", "common") and isinstance(block, dict)]
    if (source and source not in names
            and isinstance(cfg_llm.get(source), dict)):
        names.append(source)
    profiles = []
    for name in names:
        red_block = ((red.get("llm") or {}).get(name) or {})
        profiles.append({
            "name": name,
            "active": name == source,
            "fields": [
                _profile_field(name, f["key"], red_block, status)
                for f in PROFILE_FIELDS
            ],
        })
    return {"groups": groups, "llm_profiles": profiles,
            "llm_presets": llm_preset_payload(),
            "llm_profile_fields": copy.deepcopy(PROFILE_FIELDS),
            "limits": {"text_warn_chars": WORLDBOOK_WARN_CHARS},
            "tts_profiles": tts_profiles(red, raw),
            "tts_presets": tts_preset_payload(),
            **avatar_payload(catalog),
            "users": users_payload(root, cfg),
            "role": role_payload(catalog, root, cfg),
            "presets": preset_payload(root),
            "persona_files": list(PERSONA_NAMES)}


def llm_preset_payload() -> dict:
    """厂商预填数据：新建 profile 时按名字覆盖地址/模型/headers。"""
    return {
        preset_id: {
            "base_url": spec["base_url"],
            "model": spec["model"],
            "headers": dict(spec.get("headers") or {}),
        }
        for preset_id, spec in llm_presets.PRESETS.items()
    }


def _profile_field(profile: str, key: str, block: dict,
                   status: dict[str, bool]) -> dict:
    meta = _LLM_FIELD_META[key]
    path = f"llm.{profile}.{key}"
    secret = meta["type"] == "password"
    return {"path": path, "label": meta["label"], "type": meta["type"],
            "value": "" if secret else block.get(key), "secret": secret,
            "configured": bool(status.get(path)),
            "options": [], "help": _LLM_FIELD_HELP.get(key, ""),
            "min": meta.get("min"), "step": meta.get("step")}


def _avatars(catalog) -> list[dict]:
    try:
        items = catalog.discover()
        return [{"id": a.id, "label": a.shown_name, "kind": a.kind,
                 "path": a.entry, "usable": a.usable,
                 "broken": list(a.broken), "warnings": list(a.warnings),
                 "display_name": a.shown_name,
                 "profile_error": a.profile_error}
                for a in items]
    except Exception as exc:
        return [
            {"id": "error", "label": f"扫描失败：{exc}", "kind": "error",
             "usable": False, "broken": [str(exc)], "warnings": []},
        ]


def avatar_payload(catalog) -> dict:
    """给 WebUI 的角色选择器：模型列表 + 当前角色 + 固定目录。"""
    try:
        items = _avatars(catalog)
        return {
            "avatars": items,
            "selected_avatar": catalog.selected().id,
            "model_dir_rel": "models",
        }
    except Exception:
        return {
            "avatars": _avatars(catalog),
            "selected_avatar": "",
            "model_dir_rel": "models",
        }


def preset_payload(root, preset_id: str | None = None) -> dict:
    """对话预设：下拉列表 + 要看的那一份的条目（默认看 config 里当前那份）。

    `gaps` 是网页画锚点用的顺序表：每个缝隙后面跟一块固定内容（最后一个没有）。
    """
    cfg = config.load()
    current = str((cfg.get("prompt") or {}).get("id") or "")
    loaded = current if preset_id is None else str(preset_id or "").strip()
    items = []
    for pid in preset.list_ids(root):
        got = preset.read(root, pid)
        items.append({"id": pid, "name": got.name or pid, "desc": got.desc,
                      "blocks": len(got.blocks), "error": got.error})
    blocks: list[dict] = []
    error = ""
    name = ""
    desc = ""
    if loaded:
        got = preset.read(root, loaded)
        error = got.error or ""
        name = got.name or loaded
        desc = got.desc
        for b in got.blocks:
            blocks.append({"id": b.id, "title": b.title, "slot": b.slot,
                           "role": b.role, "live": b.live, "text": b.text})
    return {"items": items, "current": current, "loaded": loaded,
            "blocks": blocks, "name": name, "desc": desc, "error": error,
            "gaps": [{"slot": slot, "anchor": anchor}
                     for slot, anchor in preset.GAPS]}


def users_payload(root, cfg: dict) -> dict:
    """用户人设列表：只列 id + 名字；编辑当前那一条。"""
    current = str((cfg.get("user") or {}).get("id") or "")
    items = []
    for uid in users.list_ids(root):
        profile, err = users.read(root, uid)
        items.append({
            "id": uid,
            "name": str(profile.get("name") or ""),
            "persona": str(profile.get("persona") or ""),
            "error": err,
        })
    return {"items": items, "current": current}


def rel_path(root, path) -> str:
    try:
        return Path(path).relative_to(Path(root)).as_posix()
    except (TypeError, ValueError):
        return str(path or "")


def role_payload(catalog, root, cfg: dict) -> dict:
    """当前角色：设定 + 目录；页面只编辑这一个。"""
    try:
        avatar = catalog.selected()
    except Exception as exc:
        return {"id": "", "role_id": "", "display_name": "", "profile": {},
                "role_dir": "", "persona_dir": "", "error": str(exc)}
    if avatar.role_dir is not None:
        profile, _ = roles.read_profile_dir(
            roles.active_dir(avatar, root))
    else:
        profile, _ = roles.read_profile(root, avatar.role_id or avatar.id)
    warnings = [avatar.profile_error] if avatar.profile_error else []
    wanted = str((cfg.get("role") or {}).get("id") or "")
    role_id = avatar.role_id or avatar.id
    if wanted and wanted not in (role_id, avatar.id):
        warnings.append(f"config.role.id={wanted} 找不到，已回退到当前角色")
    directory = roles.active_dir(avatar, root)
    return {
        "id": avatar.id,
        "role_id": role_id,
        "display_name": avatar.shown_name,
        "profile": profile,
        "role_dir": rel_path(root, directory) if directory else "",
        "persona_dir": (rel_path(root, directory)
                        if directory and directory.is_dir() else ""),
        "error": "；".join(warnings) if warnings else None,
    }


def _tts_profile_field(profile: str, key: str, meta: dict, block: dict) -> dict:
    path = f"tts.{profile}.{key}"
    value = block.get(key)
    if value is None:
        value = meta.get("default", "")
    return {
        "path": path,
        "label": meta.get("label", key),
        "type": meta.get("type", "text"),
        "value": value,
        "secret": False,
        "configured": False,
        "options": [{"value": v, "label": t}
                    for v, t in (meta.get("options") or [])],
        "help": meta.get("help", ""),
        "doc": meta.get("doc", ""),
        "required": bool(meta.get("required")),
    }


def tts_profiles(red: dict, raw: dict) -> list[dict]:
    """当前配置里的 TTS profile；字段元数据来自 preset。"""
    tts = raw.get("tts") or {}
    red_tts = red.get("tts") or {}
    source = str(tts.get("source") or "")
    status = secrets.status(raw)
    out: list[dict] = []
    for name, block in tts.items():
        if name in ("enabled", "source", "common") or not isinstance(block, dict):
            continue
        preset_id = str(block.get("preset") or "")
        spec = tts_presets.get(preset_id)
        red_block = red_tts.get(name) if isinstance(red_tts.get(name), dict) else {}
        fields = []
        if spec is not None:
            for key, meta in tts_presets.field_items(preset_id):
                fields.append(_tts_profile_field(name, key, meta, red_block))
            if spec.get("auth"):
                path = f"tts.{name}.api_key"
                fields.append({
                    "path": path, "label": "API Key", "type": "password",
                    "value": "", "secret": True,
                    "configured": bool(status.get(path)),
                    "options": [], "help": "去官网注册/申请后粘贴；不会回显",
                    "doc": spec.get("doc", ""), "required": True,
                })
        out.append({
            "name": name,
            "preset": preset_id,
            "label": (spec or {}).get("label") or "（未知 preset）",
            "active": name == source,
            "fields": fields,
        })
    return out


def tts_preset_payload() -> list[dict]:
    """厂商下拉和新建表单用的 preset 元数据。"""
    out = []
    for preset_id, spec in tts_presets.PRESETS.items():
        fields = []
        for key, meta in tts_presets.field_items(preset_id):
            fields.append({
                "key": key, "label": meta.get("label", key),
                "type": meta.get("type", "text"),
                "default": meta.get("default", ""),
                "options": [{"value": v, "label": t}
                            for v, t in (meta.get("options") or [])],
                "help": meta.get("help", ""), "doc": meta.get("doc", ""),
                "required": bool(meta.get("required")),
            })
        out.append({
            "id": preset_id, "label": spec["label"], "desc": spec["desc"],
            "doc": spec["doc"], "wire": spec["wire"],
            "auth": spec.get("auth"), "fields": fields,
        })
    return out


def all_schema_paths() -> set[str]:
    paths = {p for g in GROUPS for sec in g["sections"] for p in sec["fields"]}
    paths.add("tts.source")
    tts = config.load_raw().get("tts") or {}
    for name, block in tts.items():
        if name in ("enabled", "source", "common") or not isinstance(block, dict):
            continue
        preset_id = str(block.get("preset") or "")
        spec = tts_presets.get(preset_id)
        if spec is None:
            continue
        paths.add(f"tts.{name}.preset")
        for key in spec["fields"]:
            paths.add(f"tts.{name}.{key}")
        if spec.get("auth"):
            paths.add(f"tts.{name}.api_key")
    return paths


def leaf_paths(obj, prefix="") -> set[str]:
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict) and v:
                out |= leaf_paths(v, p)
            else:
                out.add(p)
    return out
