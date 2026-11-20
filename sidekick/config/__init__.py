"""读 / 写 config.yaml。

WebUI 是正常的写入方；运行时仍然每轮调 `load()`，所以改完保存后
下一次 `Runtime._refresh()` 就能看到（进程级设置仍需重启）。

设计约束：
  · `load()` 返回**合并了默认值、并且已经把 sealed key 解密到内存**的配置。
    这个对象绝不能直接发给浏览器。
  · 发给 WebUI 的一律走 `redacted()`：密钥只保留 "sealed" / 空，另给
    `secrets.status()` 告诉前端哪些已经配置。
  · 写盘走 `atomic.write_text`；写到一半崩溃不会留下半截 YAML。
  · 用 ruamel 做 round-trip，尽量保留现有注释。没有 ruamel 就退回 PyYAML。

`config.yaml` 里的 `api_key: sealed` 在 `load()` 里换成真值（DPAPI 解密，只在内存里）。
只支持当前嵌套格式。
"""
from __future__ import annotations

import copy
import re
import threading
from pathlib import Path
from typing import Any

import yaml

from sidekick.core import atomic
from sidekick.config import secrets, tts_presets
from sidekick.config.spec import (
    DEFAULTS as _DEFAULTS,
    ENUM_RULES,
    NUM_RULES,
    default_document,
    resolve_capture_config,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.yaml"

# 最近一次解析失败的原因；`/status` 用它暴露“配置坏了”，不是静默吃默认值。
last_error: str | None = None
_load_lock = threading.RLock()
_load_cache: tuple[tuple, dict, str | None] | None = None


def deep_update(dst: dict, src: dict) -> dict:
    """把嵌套 patch 合并进 dst（原地修改，返回 dst）。

    默认值合并和 WebUI 保存整份配置都走这一个实现。
    """
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_update(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)
    return dst


def _plain(obj):
    """ruamel 的 CommentedMap/Seq 转成普通 dict/list，方便 JSON 序列化。"""
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def _read_yaml() -> dict:
    """读原始 YAML（不合并默认值、不解密）。文件坏了返回空 dict 并记录原因。"""
    global last_error
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        if raw is None:
            raise ValueError("空文件或只有注释")
        if not isinstance(raw, dict):
            raise ValueError("顶层不是 YAML 字典")
        last_error = None
        return _plain(raw)
    except FileNotFoundError:
        last_error = "config.yaml 不存在"
        return {}
    except Exception as e:
        last_error = f"config.yaml 读不了：{type(e).__name__}: {e}"[:300]
        return {}


def _file_sig(p: Path) -> tuple[str, int, int]:
    try:
        st = p.stat()
        return str(p), int(st.st_mtime_ns), int(st.st_size)
    except OSError:
        return str(p), 0, 0


def _load_key() -> tuple:
    return (_file_sig(CONFIG_PATH), _file_sig(secrets.path()))


# ── 读 ────────────────────────────────────────────────
def defaults() -> dict:
    """深拷贝默认值，给 WebUI 的 schema 和“恢复默认”用。"""
    return default_document()


def _seal_existing_api_keys(raw: dict) -> None:
    """生成默认配置时，把已解密的既有 API key 槽位写成 sealed。

    `sessdata` 不在这里处理：它是 B 站 Cookie，不是 API key，生成时留空。
    secrets.bin 不存在或解不开时 load() 返回空，所有槽位保持空。
    """
    values = secrets.load()
    if secrets.last_error:
        return
    for section in secrets.SECTIONS:
        block = raw.get(section)
        if not isinstance(block, dict):
            continue
        for owner, sub in block.items():
            if owner in secrets.RESERVED_OWNERS or not isinstance(sub, dict):
                continue
            if "api_key" not in sub:
                continue
            if values.get(f"{section}.{owner}.api_key"):
                sub["api_key"] = secrets.SEALED


def ensure_default_config() -> tuple[bool, str | None]:
    """config.yaml 不存在时生成默认文件；已存在绝不覆盖。

    返回 (是否生成, 错误)。生成失败只记录错误，不抛异常：WebUI 还要启动
    起来给用户看提示。生成内容不带注释，键序由 config_spec 固定。
    """
    global last_error
    if CONFIG_PATH.exists():
        return False, None
    raw = default_document()
    _seal_existing_api_keys(raw)
    try:
        save_raw(raw)
        last_error = None
        return True, None
    except Exception as e:
        last_error = f"生成默认 config.yaml 失败：{type(e).__name__}: {e}"[:300]
        return False, last_error


def load_raw() -> dict:
    """读原始配置（不解密、不合并默认值）。"""
    return _read_yaml()


def load() -> dict[str, Any]:
    """读一次完整配置（合并默认值 + 解密 key）。运行期用这个。

    按 config.yaml / secrets.bin 的 mtime+size 缓存；读出来仍是独立副本，
    调用方可以放心修改。文件一变，下一次 load 自动重读。
    """
    global _load_cache, last_error
    key = _load_key()
    with _load_lock:
        if _load_cache is not None and _load_cache[0] == key:
            last_error = _load_cache[2]
            return copy.deepcopy(_load_cache[1])
    out = deep_update(copy.deepcopy(_DEFAULTS), load_raw())
    secrets.fill(out)          # `api_key: sealed` 只在内存里换成真值
    error = last_error
    with _load_lock:
        _load_cache = (key, copy.deepcopy(out), error)
    return out


def path(rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else (ROOT / p)


def resolve_game_id(cfg: dict | None) -> str:
    """唯一一处“当前游戏 id”解析；只认 game.id，并归一化为文件夹名。"""
    g = (cfg or {}).get("game") or {}
    raw = str(g.get("id") or "").strip().replace("\\", "/")
    return raw.rsplit("/", 1)[-1].strip()


def ensure_dirs(c: dict[str, Any] | None = None,
                root: str | Path | None = None) -> None:
    c = c or load()
    base = Path(root) if root is not None else ROOT

    def resolve(rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (base / p)

    db_path = str((c.get("memory") or {}).get("db_path") or "")
    resolve(db_path).parent.mkdir(parents=True, exist_ok=True)


# ── 写 ────────────────────────────────────────────────
def _roundtrip_doc():
    """尽量用 ruamel 保留注释；没装就返回 None。"""
    try:
        from ruamel.yaml import YAML

        y = YAML()
        y.preserve_quotes = True
        y.width = 1000
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                return y, y.load(f)
    except Exception:
        return None
    return None


def _sync_doc(doc, new: dict) -> None:
    """把 new 同步进 ruamel 的 doc：已有的键保留注释，新键追加，删掉的键真正删除。"""
    for k in list(doc.keys()):
        if k not in new:
            try:
                del doc[k]
            except KeyError:
                pass
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(doc.get(k), dict):
            _sync_doc(doc[k], v)
        else:
            doc[k] = copy.deepcopy(v)


def _dump_raw(raw: dict) -> str:
    rt = _roundtrip_doc()
    if rt is not None:
        try:
            y, doc = rt
            if doc is None:
                doc = {}
            _sync_doc(doc, raw)
            import io

            buf = io.StringIO()
            y.dump(doc, buf)
            return buf.getvalue()
        except Exception:
            pass
    return yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, width=1000)


def save_raw(raw: dict) -> Path:
    """原子写 config.yaml。返回写入路径。"""
    global _load_cache
    raw = copy.deepcopy(raw or {})
    secrets.drop_plaintext(raw)
    _redact_tree(raw, clear=True)
    p = atomic.write_text(CONFIG_PATH, _dump_raw(raw))
    with _load_lock:
        _load_cache = None
    return p


def get_path(obj: dict, dotted: str, default=None):
    cur = obj
    for part in str(dotted or "").split("."):
        if not part:
            continue
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_path(obj: dict, dotted: str, value) -> None:
    parts = [p for p in str(dotted or "").split(".") if p]
    if not parts:
        return
    cur = obj
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = copy.deepcopy(value)


def delete_path(obj: dict, dotted: str) -> None:
    parts = [p for p in str(dotted or "").split(".") if p]
    if not parts:
        return
    cur = obj
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            return
        cur = cur[p]
    cur.pop(parts[-1], None)


# ── 校验 / 脱敏 ───────────────────────────────────────
def validate(raw: dict | None = None) -> list[str]:
    """返回错误列表；空列表表示可以保存。只做防呆，不试图穷举。"""
    c = raw if raw is not None else load()
    errs: list[str] = []

    def check_num(label, v, lo=None, hi=None, integer=False):
        if v is None:
            return
        try:
            n = int(v) if integer else float(v)
        except (TypeError, ValueError):
            errs.append(f"{label} 必须是数字")
            return
        if lo is not None and n < lo:
            errs.append(f"{label} 不能小于 {lo}")
        if hi is not None and n > hi:
            errs.append(f"{label} 不能大于 {hi}")

    def num(dotted, lo=None, hi=None, integer=False):
        check_num(dotted, get_path(c, dotted), lo, hi, integer)

    for dotted, lo, hi, integer in NUM_RULES:
        num(dotted, lo, hi, integer)
    def _int_or_none(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    summarize_every = _int_or_none(get_path(c, "memory.summarize_every"))
    history_max = _int_or_none(get_path(c, "memory.history_max"))
    if summarize_every is not None and summarize_every % 2:
        errs.append("memory.summarize_every 必须是偶数")
    if history_max is not None and history_max % 2:
        errs.append("memory.history_max 必须是偶数")
    if (summarize_every is not None and history_max is not None
            and summarize_every >= history_max):
        errs.append("memory.summarize_every 必须小于 history_max")
    llm = c.get("llm") or {}
    source = str(get_path(c, "llm.source") or "").strip()
    profile = llm.get(source)
    if source in ("source", "common") or not isinstance(profile, dict):
        errs.append("llm.source 必须指向一个 profile 配置块")
    else:
        for key in ("base_url", "model"):
            if not str(profile.get(key) or "").strip():
                errs.append(f"llm.{source}.{key} 不能为空")
    for name, block in llm.items():
        if name in ("source", "common") or not isinstance(block, dict):
            continue
        temperature = block.get("temperature")
        if temperature is None:
            errs.append(f"llm.{name}.temperature 不能为空")
        else:
            check_num(f"llm.{name}.temperature", temperature, 0, 2, False)
        for key in ("headers", "body"):
            if key in block and not isinstance(block[key], dict):
                errs.append(f"llm.{name}.{key} 必须是字典")

    # TTS：profile 形状、preset 字段和当前 source 都在 tts_presets 里校验。
    tts = c.get("tts") or {}
    source = str(tts.get("source") or "").strip()
    source_block = tts.get(source)
    if source in ("source", "common") or not isinstance(source_block, dict):
        errs.append("tts.source 必须指向一个 profile 配置块")
    for name, block in tts.items():
        if name in ("enabled", "source", "common") or not isinstance(block, dict):
            continue
        errs.extend(tts_presets.validate_profile(name, block))
    for dotted, allowed, insensitive in ENUM_RULES:
        value = str(get_path(c, dotted) or "")
        if (value.lower() if insensitive else value) not in allowed:
            errs.append(f"{dotted} 只能是 {' / '.join(allowed)}")
    for pat in get_path(c, "guard.extra_patterns") or []:
        try:
            re.compile(str(pat))
        except re.error as e:
            errs.append(f"guard.extra_patterns 有非法正则：{pat}（{e}）")
    return errs


def validate_raw(raw: dict) -> list[str]:
    """校验一份还没写盘的原始配置（合并默认值后校验）。"""
    return validate(deep_update(copy.deepcopy(_DEFAULTS), raw or {}))


def _redact_tree(node, prefix: str = "", *, clear: bool = False) -> None:
    """递归处理任意层级/列表里的密钥路径；declared 槽位已由 secrets 处理。"""
    if isinstance(node, dict):
        for key, value in list(node.items()):
            path = f"{prefix}.{key}" if prefix else str(key)
            if secrets.is_secret_path(path) and str(value).strip():
                if clear and str(value).strip().lower() == secrets.SEALED:
                    continue
                node[key] = "" if clear else secrets.SEALED
            else:
                _redact_tree(value, path, clear=clear)
    elif isinstance(node, list):
        for item in node:
            _redact_tree(item, prefix, clear=clear)


def redacted(raw: dict | None = None) -> dict:
    """给 WebUI 的配置：密钥位置只留 sealed/空，绝不返回真值。"""
    c = copy.deepcopy(raw if raw is not None else load())
    for sec in ("llm", "tts"):
        block = c.get(sec)
        if isinstance(block, dict):
            # 规格要求"明文配置里的密钥一律无效、不回显"：手改 config.yaml
            # 塞进来的顶层 api_key/headers 也不返回浏览器。
            block.pop("api_key", None)
            block.pop("headers", None)
    secrets.redact(c)
    _redact_tree(c)
    return c

