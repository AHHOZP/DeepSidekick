"""无窗口顶点差分探针：用隐藏 GL 上下文刷新 Cubism 网格。

`live2d-py` 的 ``Model.Update()`` 只推进动作/呼吸/物理，真正调用 Cubism
``csmUpdateModel`` 的入口在 ``Model.Draw()`` 开头。因此无 renderer 时顶点永远
停在加载值；``Draw()`` 又必须在有效 GL 上下文中运行。这里按上游固定顺序：

    pygame.OPENGL | pygame.HIDDEN -> live2d.init() -> live2d.glInit()
        -> LoadModelJson(create_renderer=True) -> Resize(1, 1)
        -> SetParamById -> Draw -> GetDrawableVertices

探针只读模型局部坐标，不依赖 cdi3，也不修改模型文件。顶点坐标是模型画布单位，
归一化用基准状态下所有顶点 ``2%..98%`` 分位包围盒，避免画布留白和远处特效块
把身体压扁。

审计子进程单独创建一次 1x1 隐藏上下文；桌面加载期复用已经存在的
GL 上下文，只探左右同名的手臂参数。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import numpy as np

#: 少于 3 个顶点的 Drawable 不参与包围盒/质心统计。
MIN_VERTICES = 3
#: 小于这个位移（模型画布单位）视为没有动。
MOVE_EPS = 1e-4
#: 归一化 x 超过这个值才算屏幕左/右侧。
SIDE_EPS = 0.12
#: 包围盒分位数：屏蔽画布留白和远处装饰。
BBOX_LOW = 2.0
BBOX_HIGH = 98.0
#: 死区测试走 25% 行程。
DEAD_ZONE_FRACTION = 0.25


class ProbeUnavailable(RuntimeError):
    """当前环境无法创建隐藏 GL 上下文，或模型没有可读顶点。"""


@dataclass(frozen=True)
class BoundingBox:
    x_mid: float
    y_min: float
    width: float
    height: float

    @property
    def scale(self) -> float:
        return max(self.width, self.height, MOVE_EPS)


def _as_vertices(raw: Any) -> np.ndarray:
    values = np.asarray(raw, dtype=np.float32)
    if values.ndim != 1 or values.size < 2:
        return np.empty((0, 2), dtype=np.float32)
    if values.size % 2:
        values = values[:-1]
    return values.reshape(-1, 2)


def bounding_box(vertices: Iterable[np.ndarray]) -> BoundingBox:
    """用可见顶点分位数计算角色主体包围盒。"""
    usable = [row for row in vertices
              if isinstance(row, np.ndarray) and len(row) >= MIN_VERTICES]
    if not usable:
        raise ProbeUnavailable("模型没有可读的 Drawable 顶点")
    points = np.concatenate(usable, axis=0)
    if points.size == 0 or not np.isfinite(points).all():
        raise ProbeUnavailable("模型顶点包含非有限值")
    x_min, x_max = np.percentile(points[:, 0], [BBOX_LOW, BBOX_HIGH])
    y_min, y_max = np.percentile(points[:, 1], [BBOX_LOW, BBOX_HIGH])
    return BoundingBox(
        x_mid=float(0.5 * (x_min + x_max)),
        y_min=float(y_min),
        width=max(MOVE_EPS, float(x_max - x_min)),
        height=max(MOVE_EPS, float(y_max - y_min)),
    )


def _snapshot(model: Any, drawable_count: int) -> list[np.ndarray]:
    model.Draw()
    return [
        _as_vertices(model.GetDrawableVertices(index))
        for index in range(drawable_count)
    ]


def _max_displacement(base: list[np.ndarray],
                      current: list[np.ndarray]) -> float:
    maximum = 0.0
    for before, after in zip(base, current):
        if before.shape != after.shape or before.size == 0:
            continue
        distance = np.linalg.norm(after - before, axis=1)
        if distance.size:
            maximum = max(maximum, float(distance.max()))
    return maximum


def _displacement_metrics(base: list[np.ndarray], current: list[np.ndarray],
                          box: BoundingBox, direction: float) -> dict[str, Any]:
    total_weight = 0.0
    weighted_x = 0.0
    weighted_y = 0.0
    dx_sum = 0.0
    dy_sum = 0.0
    max_disp = 0.0
    moved = 0
    for before, after in zip(base, current):
        if before.shape != after.shape or before.size == 0:
            continue
        diff = after - before
        distance = np.linalg.norm(diff, axis=1)
        if not distance.size:
            continue
        local_max = float(distance.max())
        if local_max <= MOVE_EPS:
            continue
        moved += 1
        max_disp = max(max_disp, local_max)
        weight = float(distance.sum())
        total_weight += weight
        norm_x = (before[:, 0] - box.x_mid) / (0.5 * box.width)
        norm_y = (before[:, 1] - box.y_min) / box.height
        weighted_x += float((distance * norm_x).sum())
        weighted_y += float((distance * norm_y).sum())
        dx_sum += float(diff[:, 0].sum()) * direction
        dy_sum += float(diff[:, 1].sum()) * direction

    if total_weight <= MOVE_EPS or max_disp <= MOVE_EPS:
        return {
            "active_vertices": False,
            "moved_drawables": 0,
            "span_ratio": 0.0,
        }

    cx = max(-1.5, min(1.5, weighted_x / total_weight))
    cy = max(-0.2, min(1.2, weighted_y / total_weight))
    side = ("left" if cx < -SIDE_EPS
            else ("right" if cx > SIDE_EPS else "center"))
    # dx/dy 是单位位移加权后的平均方向；除以总权重后仍是画布单位。
    return {
        "active_vertices": True,
        "moved_drawables": moved,
        "cx": round(cx, 3),
        "cy": round(cy, 3),
        "dx": round(dx_sum / total_weight, 4),
        "dy": round(dy_sum / total_weight, 4),
        "span_ratio": round(max_disp / box.scale, 4),
        "screen_side": side,
    }


def _reset_parameters(model: Any, param_ids: Iterable[str]) -> None:
    for pid in param_ids:
        model.SetParamById(str(pid), float(model.GetParamDefaultById(str(pid))))


def probe_model(model: Any, param_ids: Iterable[str] | None = None
                ) -> dict[str, dict[str, Any]]:
    """测量每个参数对顶点/质心的直接几何作用。

    调用方必须已经创建有效 renderer 和 GL 上下文（``hidden_model`` 或桌面
    宿主）；本函数内部调用 ``model.Draw()``，无 renderer 时会在原生层崩溃。

    返回 ``{param_id: {...}}``。``cx`` 是位移加权质心，``-1`` 为屏幕左、
    ``+1`` 为屏幕右；``cy`` 为 0 脚底到 1 头顶；``span_ratio`` 是最大位移
    除以主体包围盒尺度；``q_ratio`` 是 25% 行程位移占满幅位移的比例。
    只读顶点，不读 Drawable 透明度/渲染顺序；纯贴图/透明度开关会得到
    ``active_vertices == False``。
    """
    try:
        all_ids = [str(x) for x in (model.GetParamIds() or [])]
        known = set(all_ids)
        drawable_count = len(model.GetDrawableIds() or [])
        selected = ([str(x) for x in param_ids]
                    if param_ids is not None else all_ids)
        selected = [pid for pid in dict.fromkeys(selected) if pid in known]
        if not selected:
            return {}
        _reset_parameters(model, all_ids)
        base = _snapshot(model, drawable_count)
        box = bounding_box(base)
    except ProbeUnavailable:
        raise
    except (AttributeError, IndexError, RuntimeError, TypeError, ValueError) as exc:
        raise ProbeUnavailable(
            f"顶点探针初始化失败：{type(exc).__name__}: {exc}") from exc

    results: dict[str, dict[str, Any]] = {}
    try:
        for pid in selected:
            minimum = float(model.GetParamMinById(pid))
            maximum = float(model.GetParamMaxById(pid))
            default = float(model.GetParamDefaultById(pid))
            positive = abs(maximum - default)
            negative = abs(default - minimum)
            if max(positive, negative) <= MOVE_EPS:
                results[pid] = {
                    "active_vertices": False,
                    "moved_drawables": 0,
                    "span_ratio": 0.0,
                }
                continue
            target = maximum if positive >= negative else minimum
            direction = 1.0 if target >= default else -1.0

            model.SetParamById(pid, target)
            current = _snapshot(model, drawable_count)
            row = _displacement_metrics(base, current, box, direction)
            if row.get("active_vertices"):
                quarter = default + DEAD_ZONE_FRACTION * (target - default)
                model.SetParamById(pid, quarter)
                quarter_vertices = _snapshot(model, drawable_count)
                q_max = _max_displacement(base, quarter_vertices)
                full = max(MOVE_EPS, float(row["span_ratio"]) * box.scale)
                row["q_ratio"] = round(min(1.5, q_max / full), 3)
            results[pid] = row

            model.SetParamById(pid, default)
            base = _snapshot(model, drawable_count)
    except (AttributeError, IndexError, RuntimeError, TypeError, ValueError) as exc:
        raise ProbeUnavailable(
            f"顶点探针读取失败：{type(exc).__name__}: {exc}") from exc
    finally:
        try:
            _reset_parameters(model, all_ids)
            model.Draw()
        except (AttributeError, IndexError, RuntimeError, TypeError, ValueError):
            # 收尾失败不能盖住探测结果；调用方会立即销毁模型。
            pass
    return results


def _import_pygame():
    try:
        import pygame
    except ImportError as exc:
        raise ProbeUnavailable(
            "未安装 pygame，无法创建隐藏 GL 上下文") from exc
    return pygame


@contextmanager
def hidden_model(model_path: str | Path) -> Iterator[Any]:
    """创建一个 1x1 隐藏 GL 窗口并加载模型；退出时销毁。"""
    try:
        import live2d
    except ImportError as exc:
        raise ProbeUnavailable("未安装 live2d-py") from exc

    pygame = _import_pygame()
    model = None
    try:
        try:
            pygame.init()
            flags = pygame.OPENGL | getattr(pygame, "HIDDEN", 0)
            pygame.display.set_mode((1, 1), flags)
        except (pygame.error, OSError, RuntimeError) as exc:
            raise ProbeUnavailable(
                f"无法创建隐藏 GL 窗口：{type(exc).__name__}: {exc}") from exc
        try:
            live2d.init()
            live2d.glInit()
            live2d.enableLog(False)
            model = live2d.Model()
            model.LoadModelJson(str(model_path), create_renderer=True)
            model.Resize(1, 1)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ProbeUnavailable(
                f"无法加载模型几何：{type(exc).__name__}: {exc}") from exc
        yield model
    finally:
        if model is not None:
            try:
                model.DestroyRenderer()
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass
        try:
            pygame.display.quit()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
        try:
            pygame.quit()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
