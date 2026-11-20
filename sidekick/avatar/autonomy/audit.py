"""模型审计器：把模型目录变成只含事实的 ModelDossier。

必须在子进程里跑（`python -m sidekick.avatar.autonomy.audit --model ...`），
因为父进程 WebUI 不应该 import live2d。子进程先尝试 1x1 隐藏 GL 顶点探针，
无显示环境时回退到无 renderer 的词法自省；两种路径都不把模型或截图外发。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from sidekick.avatar import roles, scan
from sidekick.avatar.autonomy.motion_parse import (
    expression_asset, motion_asset, read_pose_groups,
)
from sidekick.avatar.builder import build_avatar
from sidekick.avatar.model import Avatar
from sidekick.core import atomic
from sidekick.motion.adapters.live2d_py import Live2DParamSource
from sidekick.motion.introspect import (
    read_display_names, read_physics_graph, read_physics_links,
)
from sidekick.motion.vtube import parse_vtube_config
from sidekick.motion import probe as vertex_probe

_KIND_WORDS = re.compile(
    r"angle|eye|mouth|arm|hand|leg|hip|head|body|brow|breath|cheek|ear|hair",
    re.IGNORECASE,
)

#: 审计事实的格式版本；introspect 规则变化时递增，旧缓存自然失效。
AUDIT_SCHEMA_VERSION = 1
_AUDIT_ASSET_SUFFIXES = (".json", ".moc3")


def _refs(model3: dict) -> dict:
    refs = model3.get("FileReferences")
    return refs if isinstance(refs, dict) else {}


def _canvas(model) -> list[float]:
    try:
        width, height = model.GetCanvasSize()
        return [round(float(width), 4), round(float(height), 4)]
    except (AttributeError, TypeError, ValueError):
        return []


def _groups(model3: dict) -> dict:
    out: dict[str, list[str]] = {}
    for group in model3.get("Groups") or []:
        if not isinstance(group, dict):
            continue
        name = str(group.get("Name") or "")
        if name.lower() not in ("lipsync", "eyeblink"):
            continue
        out[name] = [str(x) for x in (group.get("Ids") or []) if str(x).strip()]
    return out


def _parts(model, model3: dict, root: Path) -> list[str]:
    """部件样本：优先 cdi3 的显示名，读不到就用模型内部 Part ID。"""
    display = _refs(model3).get("DisplayInfo")
    if isinstance(display, str) and display.strip():
        try:
            data = json.loads((Path(root) / display).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError):
            data = {}
        names = [
            str(item.get("Name") or item.get("Id") or "")
            for item in (data.get("Parts") or [])
            if isinstance(item, dict) and (item.get("Name") or item.get("Id"))
        ]
        if names:
            return names
    try:
        return [str(x) for x in (model.GetPartIds() or [])]
    except (AttributeError, TypeError, ValueError):
        return []


def _motion_rows(avatar: Avatar, model3: dict) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    warnings: list[str] = []
    motions = _refs(model3).get("Motions")
    if not isinstance(motions, dict):
        return rows, warnings
    for group in sorted(str(k) for k in motions):
        items = motions.get(group)
        for index in range(len(items) if isinstance(items, list) else 0):
            asset, error = motion_asset(avatar, group, index)
            if asset is None:
                warnings.append(f"motion {group}[{index}]：{error}")
                continue
            item = items[index] if isinstance(items, list) else {}
            rows.append({
                "group": group,
                "index": index,
                "file": str(item.get("File") or "").replace("\\", "/"),
                "duration": round(asset.duration, 4),
                "loop": bool(asset.loop),
                "params": sorted(asset.params),
                "param_count": len(asset.params),
                "top_params": list(asset.top_params),
                "curves": [
                    {
                        "id": row.id,
                        "min": round(row.minimum, 4),
                        "max": round(row.maximum, 4),
                        "std": round(row.std, 4),
                        "discrete": bool(row.discrete),
                    }
                    for row in asset.curves
                ],
            })
    return rows, warnings


def _motion_activity(rows: list[dict]) -> dict[str, dict]:
    """Aggregate each parameter's activity across every motion asset."""
    out: dict[str, dict] = {}
    for row in rows:
        for curve in row.get("curves") or []:
            pid = str(curve.get("id") or "")
            if not pid:
                continue
            item = out.setdefault(pid, {
                "min": float(curve.get("min", 0.0)),
                "max": float(curve.get("max", 0.0)),
                "std": 0.0, "motions": 0, "discrete": False,
            })
            item["min"] = min(item["min"], float(curve.get("min", 0.0)))
            item["max"] = max(item["max"], float(curve.get("max", 0.0)))
            item["std"] = max(item["std"], float(curve.get("std", 0.0)))
            item["motions"] += 1
            item["discrete"] = item["discrete"] or bool(curve.get("discrete"))
    return out


#: 资产名里的持久换装/配件语义；中文和英文都覆盖，不依赖某个模型。
_PERSISTENT_CUES = (
    "裙", "衣服", "外套", "脱鞋", "鞋", "兔牙", "眼镜", "钢盆", "耳",
    "尾巴", "q版", "q 版", "体型", "前倾", "加载", "换装", "手", "食指",
    "狗尾草", "气泡", "滑动变阻器", "token", "skirt", "outfit", "shoe", "glasses",
    "ear", "tail", "chibi", "prop", "item",
)
_RESET_CUES = ("默认", "重置", "复位", "reset", "default")


def _expression_kind(name: str, controls: list[dict]) -> str:
    low = str(name).casefold()
    if any(cue in low for cue in _RESET_CUES):
        return "reset"
    if any(str(row.get("id") or "").startswith(("Clothes", "Outfit", "Suit"))
           for row in controls):
        return "persistent"
    if any(cue in low for cue in _PERSISTENT_CUES):
        return "persistent"
    return "expression"


def _expression_rows(avatar: Avatar, model3: dict,
                     ranges: dict[str, tuple[float, float, float]]
                     ) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    warnings: list[str] = []
    items = _refs(model3).get("Expressions")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or Path(str(item.get("File") or "")).stem)
        asset, error = expression_asset(avatar, name)
        if asset is None:
            warnings.append(f"expression {name}：{error}")
            continue
        controls: list[dict] = []
        for pid, value, blend in asset.values:
            bounds = ranges.get(pid)
            if bounds is None:
                continue
            minimum, maximum, default = bounds
            target = default + value if str(blend).lower() == "add" else value
            target = max(minimum, min(maximum, target))
            controls.append({
                "id": pid, "value": target, "default": default,
                "min": minimum, "max": maximum, "blend": str(blend),
            })
        rows.append({
            "name": name,
            "file": str(item.get("File") or "").replace("\\", "/"),
            "params": sorted(asset.params),
            "param_count": len(asset.params),
            "kind": _expression_kind(name, controls),
            "controls": controls,
        })
    rows.sort(key=lambda x: (x["name"], x["file"]))
    return rows, warnings


def _bound_params(profile) -> list[dict]:
    rows = []

    def row_for(spec, channel: str, **extra) -> dict:
        row = {
            "id": spec.id,
            "min": spec.minimum,
            "max": spec.maximum,
            "default": spec.default,
            "kind": spec.kind,
            "channel": channel,
            "score": spec.score,
        }
        if spec.gamma != 1.0:
            row["gamma"] = spec.gamma
        if spec.polarity < 0.0:
            row["polarity"] = -1.0
        if spec.effective_min is not None and spec.effective_max is not None:
            row["effective_min"] = spec.effective_min
            row["effective_max"] = spec.effective_max
        if spec.confidence:
            row["confidence"] = round(spec.confidence, 3)
        if spec.evidence:
            row["evidence"] = list(spec.evidence)
        side = profile.screen_side.get(spec.id, "")
        if side:
            row["screen_side"] = side
        row.update(extra)
        return row

    for channel, spec in sorted(profile.by_channel.items()):
        rows.append(row_for(spec, channel))
    for channel, followers in sorted(profile.followers.items()):
        primary = profile.by_channel.get(channel)
        for pid, scale in followers:
            spec = profile.params.get(pid)
            if spec is None:
                continue
            rows.append(row_for(
                spec, channel, follower=True,
                follower_of=primary.id if primary else "", coupling=scale))
    return rows


def _vtube_report(evidence) -> dict:
    if evidence is None:
        return {"present": False}
    return {
        "present": True,
        "file": evidence.file,
        "idle": evidence.idle,
        "idle_tracking_lost": evidence.idle_tracking_lost,
        "params": [
            {
                "id": row.output,
                "input": row.input,
                "channel": row.channel,
                "polarity": -1.0 if row.inverted else 1.0,
                "output_range": [row.output_low, row.output_high],
                "effective_range": list(row.effective_range or ()),
                "smoothing": row.smoothing,
                "breathing": row.breathing,
            }
            for row in evidence.params
        ],
        "hotkeys": [
            {"name": row.name, "action": row.action, "file": row.file,
             "folder": row.folder}
            for row in evidence.hotkeys
        ],
    }


def _state_map(profile) -> dict[str, dict]:
    return {
        name: spec.report() for name, spec in sorted(profile.states.items())
    }


def _follower_map(profile) -> dict[str, list[dict]]:
    return {
        channel: [{"id": pid, "scale": scale} for pid, scale in rows]
        for channel, rows in sorted(profile.followers.items())
    }


def _unmapped_sample(unmapped: list[str], limit: int = 40) -> list[str]:
    ranked = sorted(unmapped, key=lambda pid: (
        0 if _KIND_WORDS.search(pid) else 1, pid))
    return ranked[:max(0, int(limit))]


def _unmapped_rows(profile, display_names: dict[str, str],
                   vtube_channels: dict[str, str],
                   probe: dict[str, dict] | None,
                   physics_outputs: set[str],
                   limit: int = 60) -> list[dict]:
    """High-value unmapped parameters, with the evidence needed to judge them.

    Physics outputs are filtered out: the engine must not drive a hair/cloth
    simulator's result, no matter how interesting the parameter name looks.
    """
    probe_rows = probe or {}
    rows: list[dict] = []
    for pid in profile.unmapped:
        if pid in physics_outputs:
            continue
        name = str(display_names.get(pid) or "")
        vtube_channel = str(vtube_channels.get(pid) or "")
        geometry = probe_rows.get(pid) if isinstance(probe_rows.get(pid), dict) else {}
        if not (name or vtube_channel or geometry.get("active_vertices")):
            continue
        row = {"id": pid}
        if name:
            row["name"] = name
        if vtube_channel:
            row["vtube_channel"] = vtube_channel
        minimum, maximum, default = profile.ranges.get(pid, (0.0, 0.0, 0.0))
        row.update({"min": minimum, "max": maximum, "default": default})
        if geometry.get("active_vertices"):
            for key in ("cx", "cy", "span_ratio"):
                if key in geometry:
                    row[key] = geometry[key]
            if geometry.get("screen_side"):
                row["screen_side"] = geometry["screen_side"]
        rows.append(row)

    def rank(row: dict) -> tuple:
        return (
            0 if row.get("vtube_channel") else 1,
            0 if row.get("name") else 1,
            0 if row.get("span_ratio", 0.0) >= 0.02 else 1,
            0 if _KIND_WORDS.search(str(row["id"])) else 1,
            -float(row.get("span_ratio", 0.0)),
            str(row["id"]),
        )

    rows.sort(key=rank)
    return rows[:max(0, int(limit))]


def build_dossier(avatar: Avatar, model, model3: dict, *,
                  probe: dict[str, dict] | None = None,
                  probe_error: str = "") -> dict:
    """从已加载的 live2d-py 模型和有效 model3 生成 dossier。

    ``probe`` 是隐藏 GL 上下文里的顶点差分结果；不可用时传 ``None``，
    dossier 仍包含词法自省，不阻断审计。
    """
    entry = Path(avatar.root) / avatar.entry
    physics_inputs, physics_outputs = read_physics_graph(entry)
    physics_links = read_physics_links(entry)
    vtube = parse_vtube_config(entry)
    motions, motion_warnings = _motion_rows(avatar, model3)
    motion_activity = _motion_activity(motions)
    profile = Live2DParamSource(model, entry).profile(
        probe=probe, vtube=vtube, motion_activity=motion_activity)
    probe_rows = {str(pid): dict(row) for pid, row in (probe or {}).items()
                  if isinstance(row, dict)}
    for pid in physics_inputs:
        if pid in probe_rows and not probe_rows[pid].get("active_vertices"):
            # 不动顶点但在 .physics3 里当输入源：虚拟物理输入，不是废参数。
            probe_rows[pid]["virtual_input"] = True
    warnings = list(profile.warnings)
    expressions, expression_warnings = _expression_rows(
        avatar, model3, profile.ranges)
    warnings.extend(motion_warnings)
    warnings.extend(expression_warnings)
    persistent = [row["name"] for row in expressions
                  if row.get("kind") == "persistent"]
    if persistent:
        warnings.append("持久换装/配件表情：" + "、".join(persistent[:8]))
    refs = _refs(model3)
    physics = bool(refs.get("Physics"))
    pose = bool(refs.get("Pose"))
    pose_groups = read_pose_groups(entry) if pose else []
    hit_areas = model3.get("HitAreas")
    if hit_areas:
        warnings.append("模型有 HitAreas，但审计未做网格采样；head_ratio 保持 null")
    else:
        warnings.append("模型没有 HitAreas/Layout，head_ratio 无法可靠计算")
    return {
        "avatar_id": avatar.id,
        "model": {
            "name": avatar.name,
            "entry": avatar.entry,
            "canvas": _canvas(model),
            "version": int(model3.get("Version") or 0),
            "pointer_origin": (list(profile.pointer_origin)
                               if profile.pointer_origin else None),
        },
        "params": _bound_params(profile),
        "by_channel": {channel: spec.id for channel, spec in sorted(profile.by_channel.items())},
        "followers": _follower_map(profile),
        "states": _state_map(profile),
        "tier": profile.tier,
        "capabilities": dict(sorted(profile.capabilities.items())),
        "unmapped": {
            "count": len(profile.unmapped),
            "sample": _unmapped_sample(profile.unmapped),
            "rows": _unmapped_rows(
                profile, read_display_names(entry),
                vtube.channels() if vtube is not None else {},
                probe, physics_outputs),
        },
        "motions": motions,
        "expressions": expressions,
        "groups": _groups(model3),
        "parts": _parts(model, model3, Path(avatar.root))[:40],
        "physics": {"present": physics, "inputs": sorted(physics_inputs),
                    "outputs": sorted(physics_outputs),
                    "links": physics_links},
        "physics_outputs_count": len(physics_outputs),
        "vtube": _vtube_report(vtube),
        "probe": {
            "available": probe is not None,
            "error": str(probe_error or ""),
            "params": probe_rows,
        },
        "screen_side": dict(sorted(profile.screen_side.items())),
        "pose": {"present": pose, "groups": pose_groups},
        "proportions": {
            "head_ratio": None,
            "head_width_ratio": None,
            "archetype_guess": "unknown",
        },
        "warnings": warnings,
    }


def find_avatar(path: Path, root: Path) -> Avatar:
    """只构建目标模型；审计单模型不应该扫描整个 models/。"""
    avatar = build_avatar(path.parent, path)
    roles.apply(avatar, root)
    return avatar


def audit_path(path: str | Path, root: str | Path | None = None) -> dict:
    import live2d

    model_path = Path(path)
    project_root = Path(root or Path(__file__).resolve().parents[3])
    avatar = find_avatar(model_path, project_root)
    effective = (avatar.model3 if avatar.model3 is not None
                 else scan.read_json(model_path))
    avatar = replace(avatar, model3=effective)
    probe_error = ""
    try:
        with vertex_probe.hidden_model(model_path) as model:
            initial = Live2DParamSource(model, model_path).profile()
            # 语义参数之外，换装/手型状态参数也要测：Clothes*/Movement* 常是
            # 纯透明度/贴图开关，active_vertices=False 本身就是事实。
            ids = set(initial.params)
            ids.update(spec.param_id for spec in initial.states.values())
            probe = vertex_probe.probe_model(model, sorted(ids))
            return build_dossier(avatar, model, effective, probe=probe)
    except vertex_probe.ProbeUnavailable as exc:
        probe_error = f"{type(exc).__name__}: {exc}"

    live2d.init()
    model = live2d.Model()
    # 无显示环境回退：用原始 model3 加载参数/画布，不用合成后的 JSON；
    # live2d-py 对 LoadFromJsonString 在某些 Unicode/补全模型上会原生崩溃，
    # 动作/表情的参数集合本来就从 effective model3 的另一条路径解析。
    model.LoadModelJson(str(model_path), create_renderer=False)
    return build_dossier(avatar, model, effective, probe_error=probe_error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--root", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        data = audit_path(args.model, args.root or None)
    except Exception as exc:  # noqa: BLE001 - 子进程边界，父进程只解析结果
        print(f"[audit] {type(exc).__name__}: {exc}", file=sys.stderr)
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False))
        return 2
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


def _audit_cache_key(model: Path) -> str:
    """模型目录里所有 dossier 可能读取的 JSON/moc3 的指纹。

    不能只看 model3/moc3：表情、动作、物理、pose、cdi3 和 vtube 改了，
    dossier 也会变；漏掉它们会一直命中旧审计结果。
    """
    try:
        st = model.stat()
    except OSError:
        return ""
    parts = [f"audit:{AUDIT_SCHEMA_VERSION}",
             f"model:{st.st_mtime_ns}:{st.st_size}"]
    for path in sorted(model.parent.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _AUDIT_ASSET_SUFFIXES:
            continue
        rel_path = path.relative_to(model.parent)
        if any(part in {"state", ".git", "__pycache__"}
               for part in rel_path.parts):
            continue
        try:
            s = path.stat()
        except OSError:
            continue
        parts.append(f"{rel_path.as_posix()}:{s.st_mtime_ns}:{s.st_size}")
    return "|".join(parts)


def run_audit(root: str | Path, model_path: str | Path,
              timeout: float = 30.0, *, use_cache: bool = True) -> dict:
    """父进程入口：子进程审计，返回 dossier 或抛 RuntimeError。

    走 `--out` 临时文件而不是 stdout：live2d-py 启动时会往 stdout 打横幅，
    直接解析 stdout 会把日志当成 JSON。
    """
    root = Path(root)
    model = Path(model_path)
    if not model.is_absolute():
        model = root / model
    cache_key = _audit_cache_key(model) if use_cache else ""
    cache_file = (root / "state" / "audit_cache"
                  / f"{scan.digest(model) or model.stem}.json")
    if cache_key and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            dossier = cached.get("dossier") if isinstance(cached, dict) else None
            if (isinstance(cached, dict)
                    and cached.get("_cache_key") == cache_key
                    and isinstance(dossier, dict)):
                return dossier
        except (OSError, ValueError):
            pass
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "dossier.json"
        stdout_path = Path(td) / "stdout.log"
        stderr_path = Path(td) / "stderr.log"
        cmd = [sys.executable, "-m", "sidekick.avatar.autonomy.audit",
               "--model", str(model), "--root", str(root),
               "--out", str(out)]
        # WebUI 的 sys.executable 可能是 base Python（run.bat 就是这么起的），
        # 只靠 cwd 不保证 `sidekick` 在 sys.path；显式把项目根放进子进程的
        # PYTHONPATH。保留现有 site-packages 路径。
        env = os.environ.copy()
        parts = [str(root.resolve())]
        if env.get("PYTHONPATH"):
            parts.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(parts)
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            with open(stdout_path, "wb") as so, open(stderr_path, "wb") as se:
                proc = subprocess.run(
                    cmd, cwd=str(root), stdout=so, stderr=se,
                    timeout=timeout, env=env, **kwargs)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"审计子进程失败：{type(exc).__name__}: {exc}") from exc
        if proc.returncode != 0 or not out.is_file():
            detail = ""
            try:
                detail = (stderr_path.read_text(encoding="utf-8", errors="replace")
                          or stdout_path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
            detail = " ".join(detail.split())
            raise RuntimeError(
                f"审计失败（exit {proc.returncode}）：{detail[:300]}")
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"审计结果不是 JSON：{exc}") from exc
        if not isinstance(data, dict) or data.get("error"):
            raise RuntimeError(str(data.get("error") or "审计结果为空"))
        if cache_key:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                atomic.write_text(cache_file, json.dumps(
                    {"_cache_key": cache_key, "dossier": data},
                    ensure_ascii=False))
            except OSError:
                pass
        return data


if __name__ == "__main__":
    raise SystemExit(main())

