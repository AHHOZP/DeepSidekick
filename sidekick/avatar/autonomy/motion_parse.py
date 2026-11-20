"""只读模型资产：motion3/exp3 写到哪些参数、播多久、幅度多大。

用于参数所有权、生成 prompt 的证据摘要和 `.exp3.json` 持久换装分类。只解析
文件里的声明，不推断语义；文件缺失或损坏返回 ``(None, 原因)``，调用方决定
是否告警，不抛异常。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from sidekick.avatar.scan import read_json


@dataclass(frozen=True)
class CurveStat:
    """一个 motion3 参数曲线的活动范围。"""

    id: str
    minimum: float
    maximum: float
    mean: float
    std: float
    discrete: bool

    @property
    def span(self) -> float:
        return max(0.0, self.maximum - self.minimum)


@dataclass(frozen=True)
class MotionAsset:
    name: str
    file: str
    duration: float
    loop: bool
    params: frozenset[str]
    curves: tuple[CurveStat, ...] = ()

    @property
    def top_params(self) -> tuple[str, ...]:
        """变化最大的几个参数，供 LLM 判断这段动作在演什么。"""
        ranked = sorted(self.curves, key=lambda row: (-row.span, row.id))
        return tuple(row.id for row in ranked if row.span > 1e-6)[:3]


@dataclass(frozen=True)
class ExpressionAsset:
    name: str
    file: str
    params: frozenset[str]
    #: ``(Id, Value, Blend)``；用于区分情绪表情和持久换装开关。
    values: tuple[tuple[str, float, str], ...] = ()


def _model3(avatar) -> dict:
    data = getattr(avatar, "model3", None)
    if isinstance(data, dict):
        return data
    return read_json(Path(avatar.root) / avatar.entry)


def _refs(avatar) -> dict:
    data = _model3(avatar)
    refs = data.get("FileReferences")
    return refs if isinstance(refs, dict) else {}


def _safe_path(avatar, rel: str) -> Path | None:
    base = Path(avatar.root).resolve()
    try:
        path = (base / str(rel or "")).resolve()
        path.relative_to(base)
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def _motion_file(avatar, group: str, index: int) -> str:
    motions = _refs(avatar).get("Motions")
    if not isinstance(motions, dict):
        return ""
    items = motions.get(str(group))
    if not isinstance(items, list):
        return ""
    try:
        item = items[int(index)]
    except (IndexError, TypeError, ValueError):
        return ""
    return str(item.get("File") or "") if isinstance(item, dict) else ""


def _expression_file(avatar, name: str) -> str:
    items = _refs(avatar).get("Expressions")
    if not isinstance(items, list):
        return ""
    for item in items:
        if isinstance(item, dict) and str(item.get("Name") or "") == str(name):
            return str(item.get("File") or "")
    return ""


def _segments(values: list) -> tuple[list[float], float]:
    """解出 motion3 的控制点 y 值和最后一个时间戳。

    Cubism 的编码是 ``x0, y0, [type, x, y, (c1x, c1y, c2x, c2y)?]...``；
    type 0/2/3 取两个数，type 1（贝塞尔）取六个。解析失败时停在上一个点，
    不抛异常。
    """
    if not isinstance(values, list) or len(values) < 2:
        return [], 0.0
    points: list[float] = []
    try:
        points.append(float(values[1]))
    except (TypeError, ValueError):
        pass
    last_x = 0.0
    index = 2
    while index < len(values):
        try:
            kind = int(values[index])
        except (TypeError, ValueError):
            break
        index += 1
        if kind == 1:
            if index + 6 > len(values):
                break
            chunk = values[index:index + 6]
            index += 6
            try:
                # Cubism 布局：[c1x, c1y, c2x, c2y, x, y]
                c1y, c2y, x, y = (float(chunk[1]), float(chunk[3]),
                                  float(chunk[4]), float(chunk[5]))
            except (TypeError, ValueError):
                continue
            last_x = max(last_x, x)
            points.extend([y, c1y, c2y])
        else:
            if index + 2 > len(values):
                break
            chunk = values[index:index + 2]
            index += 2
            try:
                x, y = float(chunk[0]), float(chunk[1])
            except (TypeError, ValueError):
                continue
            last_x = max(last_x, x)
            points.append(y)
    return [value for value in points if math.isfinite(value)], last_x


def _curve_stat(pid: str, values: list[float]) -> CurveStat | None:
    if not values:
        return None
    minimum = min(values)
    maximum = max(values)
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    discrete = all(abs(value) <= 0.01 or abs(value - 1.0) <= 0.01
                   for value in values)
    return CurveStat(
        id=pid, minimum=minimum, maximum=maximum, mean=mean,
        std=math.sqrt(max(0.0, variance)), discrete=discrete,
    )


def parse_motion(path: Path, name: str = "") -> MotionAsset:
    data = read_json(path)
    params: set[str] = set()
    curves: list[CurveStat] = []
    computed_duration = 0.0
    for curve in data.get("Curves") or []:
        if not isinstance(curve, dict) or curve.get("Target") != "Parameter":
            continue
        pid = str(curve.get("Id") or "")
        if pid:
            params.add(pid)
        values, last_x = _segments(curve.get("Segments"))
        computed_duration = max(computed_duration, last_x)
        stat = _curve_stat(pid, values) if pid else None
        if stat is not None:
            curves.append(stat)
    meta = data.get("Meta") if isinstance(data.get("Meta"), dict) else {}
    try:
        duration = float(meta.get("Duration", computed_duration))
    except (TypeError, ValueError):
        duration = computed_duration
    if not math.isfinite(duration) or duration <= 0.0:
        duration = max(0.1, computed_duration)
    return MotionAsset(
        name=str(name),
        file=path.name,
        duration=duration,
        loop=bool(meta.get("Loop", False)),
        params=frozenset(params),
        curves=tuple(curves),
    )


def parse_expression(path: Path, name: str = "") -> ExpressionAsset:
    data = read_json(path)
    params: set[str] = set()
    values: list[tuple[str, float, str]] = []
    for item in data.get("Parameters") or []:
        if not isinstance(item, dict) or not item.get("Id"):
            continue
        pid = str(item["Id"])
        params.add(pid)
        try:
            value = float(item.get("Value", 0.0))
        except (TypeError, ValueError):
            continue
        values.append((pid, value, str(item.get("Blend") or "Add")))
    return ExpressionAsset(str(name), path.name, frozenset(params), tuple(values))


def motion_asset(avatar, group: str, index: int = 0):
    rel = _motion_file(avatar, group, index)
    path = _safe_path(avatar, rel) if rel else None
    if path is None:
        return None, f"motion {group} 找不到文件"
    asset = parse_motion(path, group)
    return asset, ""


def expression_asset(avatar, name: str):
    rel = _expression_file(avatar, name)
    path = _safe_path(avatar, rel) if rel else None
    if path is None:
        return None, f"expression {name} 找不到文件"
    return parse_expression(path, name), ""


def build_expression_mixer(avatar, names, ranges):
    """Build a layered mixer from the expressions present on *avatar*."""
    from sidekick.motion.expressions import ExpressionMixer

    assets = {}
    for name in dict.fromkeys(str(x) for x in (names or ()) if str(x).strip()):
        asset, _error = expression_asset(avatar, name)
        if asset is not None:
            assets[name] = asset
    return ExpressionMixer(assets, ranges)


def read_pose_groups(model3_path: str | Path | None) -> list[list[str]]:
    """读 `.pose3.json` 的互斥部件组；没有文件时返回空列表。"""
    if not model3_path:
        return []
    path = Path(model3_path)
    model3 = read_json(path)
    refs = model3.get("FileReferences") if isinstance(model3, dict) else {}
    rel = refs.get("Pose") if isinstance(refs, dict) else ""
    if not isinstance(rel, str) or not rel.strip():
        return []
    pose = read_json(path.parent / rel)
    groups: list[list[str]] = []
    for group in pose.get("Groups") or []:
        if not isinstance(group, dict):
            continue
        ids = [str(x) for x in (group.get("PartIds") or group.get("Parts") or [])
               if str(x).strip()]
        if ids:
            groups.append(ids)
    return groups
