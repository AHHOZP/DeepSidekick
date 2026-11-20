"""API key / cookie 的加密存放（Windows DPAPI）。

`config.yaml` 只留 `api_key: sealed`，真值在 `state/secrets.bin`：
用 Windows 自带的 DPAPI（`CryptProtectData`）加密，只有同一台机器上的
同一个 Windows 用户能解开。

只支持一种配置格式：`llm.<profile>.api_key` / `tts.<provider>.api_key` /
`llm.<profile>.headers.<Header>` / 顶层 `sessdata`。

写入是按 id 的 upsert：锁内重新读文件，只改目标 id，再原子替换；
A 更新不会覆盖 B。
"""
from __future__ import annotations

import ctypes
import json
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from sidekick.core.atomic import write_bytes

# 不 import config：那边会 import 本模块，会成环
ROOT = Path(__file__).resolve().parents[2]

SEALED = "sealed"
_NAME = "state/secrets.bin"

# 这些 header 名字的值按密钥处理：UI 不回显，导出不包含。
SECRET_HEADERS = {
    "authorization", "api-key", "x-api-key", "x-auth-token",
    "cookie", "set-cookie", "proxy-authorization",
}
_SECRET_WORDS = ("token", "key", "auth", "secret", "cookie")


def is_secret_header(name: str) -> bool:
    n = str(name or "").strip().lower()
    return n in SECRET_HEADERS or any(w in n for w in _SECRET_WORDS)

# 有密钥槽位的两个配置段；`llm.source/common` 不是 provider/profile。
SECTIONS = ("llm", "tts")
RESERVED_OWNERS = ("source", "common")

_lock = threading.RLock()
_warned = False
# 最近一次解密失败的原因；`/status` 用它区分“没填 key”和“密钥库坏了”。
last_error: str | None = None


class SecretsError(RuntimeError):
    """secrets.bin 读取失败；写入方不要用空字典覆盖旧文件。"""


# ── DPAPI ────────────────────────────────────────────
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))   # 必须活到调用结束
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI 只在 Windows 上可用")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_in, keep = _blob(data)
    blob_out = _BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    args = [ctypes.byref(blob_in)]
    if not protect:
        args.append(None)                    # ppszDataDescr
    else:
        args.append(ctypes.c_wchar_p("deepsidekick"))
    args += [None, None, None,
             0x1 if not protect else 0,     # UI_FORBIDDEN，别弹窗
             ctypes.byref(blob_out)]
    if not fn(*args):
        raise OSError(f"{'CryptProtectData' if protect else 'CryptUnprotectData'} "
                      f"失败：{ctypes.get_last_error()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
        del keep


# ── 读写 ─────────────────────────────────────────────
def path() -> Path:
    return ROOT / _NAME


def _read(strict: bool = False) -> dict[str, str]:
    global _warned, last_error
    p = path()
    try:
        data = json.loads(_dpapi(p.read_bytes(), False).decode("utf-8"))
        result = {str(k): str(v) for k, v in data.items() if v}
        last_error = None
        return result
    except FileNotFoundError:
        last_error = None
        return {}
    except Exception as e:
        last_error = (f"{_NAME} 解不开：{type(e).__name__}: {e}；"
                      f"换机器/账号后需要在 WebUI 重新填 key")[:300]
        if strict:
            raise SecretsError(
                f"{_NAME} 解不开（{type(e).__name__}: {e}）；"
                f"如果换了机器或 Windows 账号，需要重新在 WebUI 里填 key") from e
        if not _warned:
            _warned = True
            print(f"[secrets] {_NAME} 解不开（{type(e).__name__}: {e}）；"
                  f"如果换了机器或 Windows 账号，需要重新在 WebUI 里填 key",
                  file=sys.stderr)
        return {}


def _write(values: dict[str, str]) -> Path:
    global last_error
    raw = json.dumps(values, ensure_ascii=False).encode("utf-8")
    p = write_bytes(path(), _dpapi(raw, True), mode=0o600)
    last_error = None
    return p


def seal(values: dict[str, str]) -> Path:
    """显式全量写入（测试/初始化用）。业务写入走 upsert。"""
    with _lock:
        return _write({str(k): str(v) for k, v in values.items() if v})


def load() -> dict[str, str]:
    with _lock:
        return _read()


def get(name: str) -> str:
    return load().get(str(name), "")


def upsert(name: str, value: str | None) -> None:
    """只更新一个 id；锁内重新读取，其他 API 的记录保持不变。"""
    name = str(name)
    val = str(value or "").strip()
    with _lock:
        values = _read(strict=True)
        if val:
            values[name] = val
        else:
            values.pop(name, None)
        _write(values)


def delete(name: str) -> None:
    upsert(name, None)


def delete_prefix(prefix: str) -> None:
    prefix = str(prefix).strip()
    if not prefix:
        return
    with _lock:
        values = _read(strict=True)
        for key in list(values):
            if key == prefix or key.startswith(prefix + "."):
                values.pop(key, None)
        _write(values)


def set_secret(name: str, value: str | None) -> None:
    upsert(name, value)


def clear_secret(name: str) -> None:
    delete(name)


# ── 配置里的固定槽位 ─────────────────────────────────
@dataclass(frozen=True)
class Slot:
    """一个密钥槽位：`section.owner.api_key` 或 `section.owner.headers.<H>`。"""

    section: str = ""
    owner: str = ""
    key: str = ""
    header: str = ""

    @property
    def name(self) -> str:
        if self.is_sessdata:
            return "sessdata"
        if self.key == "api_key":
            return f"{self.section}.{self.owner}.api_key"
        return f"{self.section}.{self.owner}.headers.{self.header}"

    @property
    def is_sessdata(self) -> bool:
        return self.key == "sessdata"


def parse_slot(name: str) -> Slot | None:
    """把点分路径解析成槽位；不认识的返回 None。"""
    parts = str(name or "").split(".")
    if parts == ["sessdata"]:
        return Slot(key="sessdata")
    if len(parts) >= 3 and parts[0] in SECTIONS \
            and parts[1] not in RESERVED_OWNERS:
        if parts[2] == "api_key" and len(parts) == 3:
            return Slot(section=parts[0], owner=parts[1], key="api_key")
        if parts[2] == "headers" and len(parts) >= 4:
            header = ".".join(parts[3:])
            if is_secret_header(header):
                return Slot(section=parts[0], owner=parts[1],
                            key="headers", header=header)
    return None


def is_secret_path(dotted: str) -> bool:
    """这个点分配置路径是不是密钥；所有读写方都查这一处。

    除合法槽位外，任何层级出现 `api_key` / `sessdata` / 敏感 header 名
    也按密钥处理，避免旧格式或手写 patch 绕过。
    """
    if parse_slot(dotted) is not None:
        return True
    parts = [p.lower() for p in str(dotted or "").split(".") if p]
    if any(p in ("api_key", "sessdata") for p in parts):
        return True
    # 只有 headers 段里的名字才按敏感 header 判断；否则 max_tokens 这种
    # 带 "token" 的普通字段会被误删。
    if "headers" in parts:
        idx = parts.index("headers")
        return any(is_secret_header(p) for p in parts[idx + 1:])
    return False


def _owners(cfg: dict):
    """yield (section, owner, 当前块)；只认当前嵌套格式。"""
    for sec in SECTIONS:
        block = cfg.get(sec)
        if not isinstance(block, dict):
            continue
        for owner, sub in block.items():
            if owner in RESERVED_OWNERS or not isinstance(sub, dict):
                continue
            yield sec, owner, sub


def _slots(cfg: dict):
    """yield (id, 当前值)；只认当前嵌套格式。"""
    for sec, owner, sub in _owners(cfg):
        if "api_key" in sub:
            yield f"{sec}.{owner}.api_key", str(sub.get("api_key") or "")
        headers = sub.get("headers")
        if isinstance(headers, dict):
            for hk, hv in list(headers.items()):
                if is_secret_header(hk):
                    yield f"{sec}.{owner}.headers.{hk}", str(hv or "")
    if "sessdata" in cfg:
        yield "sessdata", str(cfg.get("sessdata") or "")


def scopes(cfg: dict) -> set[str]:
    """当前配置里存在的 `llm.<profile>` / `tts.<provider>` 作用域。"""
    return {f"{sec}.{owner}" for sec, owner, _sub in _owners(cfg)}


def drop_plaintext(cfg: dict) -> None:
    """原地清掉配置里的明文密钥；只保留 sealed / 空。

    运行时本来就不接受明文（`fill` 会忽略），写盘时也必须清掉，
    否则整份保存会把旧明文继续留在 config.yaml。
    """
    for _sec, _owner, sub in _owners(cfg):
        if "api_key" in sub:
            value = str(sub.get("api_key") or "").strip()
            if value and value.lower() != SEALED:
                sub["api_key"] = ""
        headers = sub.get("headers")
        if isinstance(headers, dict):
            for hk, hv in list(headers.items()):
                if not is_secret_header(hk):
                    continue
                value = str(hv or "").strip()
                if value and value.lower() != SEALED:
                    headers[hk] = ""
    if "sessdata" in cfg:
        value = str(cfg.get("sessdata") or "").strip()
        if value and value.lower() != SEALED:
            cfg["sessdata"] = ""


def redact(cfg: dict) -> None:
    """原地把密钥槽位改成 sealed/空，供 WebUI 回显。"""
    for _sec, _owner, sub in _owners(cfg):
        if "api_key" in sub:
            sub["api_key"] = SEALED if str(sub.get("api_key") or "").strip() else ""
        headers = sub.get("headers")
        if isinstance(headers, dict):
            for hk, hv in list(headers.items()):
                if is_secret_header(hk):
                    headers[hk] = SEALED if str(hv or "").strip() else ""
    if "sessdata" in cfg:
        cfg["sessdata"] = (SEALED if str(cfg.get("sessdata") or "").strip()
                           else "")


_PLAIN_WARNED: set[str] = set()


def _warn_plain(name: str) -> None:
    if name in _PLAIN_WARNED:
        return
    _PLAIN_WARNED.add(name)
    print(f"[secrets] {name} 是明文，已忽略：密钥只能从 WebUI 写入并加密（sealed）",
          file=sys.stderr)


def _fill_one(target: dict, key: str, name: str, getv) -> None:
    raw = str(target.get(key) or "").strip()
    if raw.lower() == SEALED:
        target[key] = getv(name)
    elif raw:
        _warn_plain(name)
        target[key] = ""


def status(cfg: dict) -> dict[str, bool]:
    """只返回“有没有配置”，不返回值。cfg 是原始配置。

    只认加密库里的记录：config 里的明文值不算“已配置”，运行时也会被忽略。
    """
    values = load()
    out = {"sessdata": False}
    for name, _value in _slots(cfg):
        out[name] = bool(values.get(name))
    return {n: out[n] for n in sorted(out)}


def fill(cfg: dict) -> None:
    """把 `api_key: sealed` / secret header / sessdata 用真值补上（只在内存）。

    密钥唯一来源是 `state/secrets.bin`（WebUI 写入、DPAPI 加密）。
    config 里的明文值、环境变量等一律忽略：宁可报“没 key”，也不接受第二来源。
    """
    values: dict[str, str] | None = None

    def getv(name: str) -> str:
        nonlocal values
        if values is None:
            values = load()
        return values.get(name, "")

    for sec, owner, sub in _owners(cfg):
        if "api_key" in sub:
            _fill_one(sub, "api_key", f"{sec}.{owner}.api_key", getv)
        headers = sub.get("headers")
        if isinstance(headers, dict):
            for hk in list(headers):
                if is_secret_header(hk):
                    _fill_one(headers, hk, f"{sec}.{owner}.headers.{hk}", getv)

    if "sessdata" in cfg:
        _fill_one(cfg, "sessdata", "sessdata", getv)

