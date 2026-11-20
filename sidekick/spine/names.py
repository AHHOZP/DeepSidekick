"""读一个 Spine 模型的动画名（`.skel` 必须问原生库，`.json` 不需要走这里）。

父进程只负责起子进程并解析 JSON；真正的解析在子进程里做——`sidekick/avatar` 那条链路跑在
runtime 进程里，一旦进程内加载 DLL，原生库崩溃就会带走整个 WebUI。

进程内按「路径 + mtime + 大小」记忆，只缓存真正被用到的模型。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TIMEOUT_S = 20.0

_MEMO: dict[str, tuple[int, int, list[str], str]] = {}


def read_names(entry: Path) -> tuple[list[str], str]:
    """返回 `(动画名, 错误说明)`；错误非空表示读不到（DLL 缺失 / 版本不支持 / 文件坏）。"""
    entry = Path(entry)
    try:
        stat = entry.stat()
    except OSError as exc:
        return [], f"骨架读不到：{type(exc).__name__}: {exc}"

    cached = _MEMO.get(str(entry))
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return list(cached[2]), cached[3]

    names, error = _run([entry]).get(str(entry), ([], "子进程没有返回结果"))
    _MEMO[str(entry)] = (stat.st_mtime_ns, stat.st_size, list(names), error)
    return names, error


def _run(entries: list[Path]) -> dict[str, tuple[list[str], str]]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "sidekick.spine.names", *[str(e) for e in entries]],
            cwd=str(REPO), capture_output=True, timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        error = f"动画名探针启动失败：{type(exc).__name__}: {exc}"
        return {str(e): ([], error) for e in entries}

    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not out:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        error = f"动画名探针异常退出（code {proc.returncode}）{('：' + detail) if detail else ''}"
        return {str(e): ([], error) for e in entries}
    try:
        raw = json.loads(out)
    except ValueError:
        error = f"动画名探针输出不是 JSON：{out[:120]!r}"
        return {str(e): ([], error) for e in entries}

    result: dict[str, tuple[list[str], str]] = {}
    for key, value in raw.items():
        names = value.get("names") if isinstance(value, dict) else None
        error = str((value or {}).get("error") or "") if isinstance(value, dict) else ""
        result[key] = ([str(n) for n in names] if isinstance(names, list) else [], error)
    return result


# ── 子进程入口 ─────────────────────────────────────────
def main(argv: list[str]) -> int:
    """`python -m sidekick.spine.names <骨架> ...` → 一行 JSON；退出码恒为 0。"""
    from sidekick.spine.binding import SpineInstance

    sys.stdout.reconfigure(encoding="utf-8")   # 父进程按 UTF-8 解，别跟随系统代码页
    out: dict[str, dict] = {}
    for raw in argv:
        entry = Path(raw)
        atlas = entry.with_suffix(".atlas")
        try:
            if not atlas.is_file():
                raise OSError(f"缺少同名图集 {atlas.name}")
            inst = SpineInstance(entry, atlas, names_only=True)
            try:
                out[str(entry)] = {"names": inst.animations, "error": ""}
            finally:
                inst.close()
        except (OSError, ValueError, RuntimeError) as exc:
            out[str(entry)] = {"names": [], "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
