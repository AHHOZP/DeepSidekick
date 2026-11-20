"""手工冒烟：把一个 Spine 模型放进真实的 Pygame/OpenGL 窗口跑 N 帧，存图并打印统计。

    .venv\\Scripts\\python scripts\\spine_smoke.py --model <模型目录> --frames 60 --shot temp\\spine.png
    .venv\\Scripts\\python scripts\\spine_smoke.py --model <模型目录> --names

`--names` 只跑动画名探针（不开窗、不建 GL），用来确认 `.skel` 能不能读。
这是"桌面渲染没退化"的唯一人工入口：默认套件不碰 GL/硬件。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sidekick.avatar.builder import build_spine  # noqa: E402
from sidekick.spine import names as spine_names  # noqa: E402


def _entry(model: Path) -> Path:
    for suffix in (".skel", ".json"):
        cand = model / f"{model.name}{suffix}"
        if cand.is_file():
            return cand
    for suffix in (".skel", ".json"):
        found = sorted(model.glob(f"*{suffix}"))
        if found:
            return found[0]
    raise SystemExit(f"没找到骨架：{model}")


def _print_names(entry: Path) -> int:
    names, error = spine_names.read_names(entry)
    print(f"model={entry}")
    print(f"animations={names}")
    if error:
        print(f"error={error}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="模型目录")
    parser.add_argument("--width", type=int, default=420)
    parser.add_argument("--height", type=int, default=680)
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--shot", default="")
    parser.add_argument("--hit", default="", help="逗号分隔的 x,y 命中测试点")
    parser.add_argument("--names", action="store_true", help="只读动画名，不开窗")
    args = parser.parse_args()

    model = Path(args.model).resolve()
    entry = _entry(model)
    if args.names:
        return _print_names(entry)

    import pygame
    from OpenGL.GL import (
        GL_COLOR_BUFFER_BIT, GL_DEPTH_BUFFER_BIT, GL_RGBA, GL_UNSIGNED_BYTE,
        glClear, glClearColor, glReadPixels,
    )

    from sidekick.desktop.spine import SpineRenderer

    avatar = build_spine(model, entry)
    if avatar.broken:
        print("broken=" + "；".join(avatar.broken))
    pygame.init()
    pygame.display.gl_set_attribute(pygame.GL_DEPTH_SIZE, 24)
    pygame.display.gl_set_attribute(pygame.GL_DOUBLEBUFFER, 1)
    pygame.display.set_caption("DeepSidekick Spine smoke")
    pygame.display.set_mode((args.width, args.height),
                            pygame.OPENGL | pygame.DOUBLEBUF, vsync=0)
    glClearColor(0.0, 0.0, 0.0, 1.0)
    renderer = SpineRenderer(avatar, args.width, args.height)
    avatar.idle = renderer.idle
    print(f"spine={renderer._inst.version} animations={len(renderer.motion_groups)} "
          f"idle={renderer.idle!r} premultiplied={renderer._texture.premultiplied}")

    frames = max(1, args.frames)
    drawn = 0
    for _ in range(frames):
        pygame.event.pump()
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        renderer.update()
        renderer.draw()
        if renderer._inst.tris is not None and renderer._inst.tris.size:
            drawn += 1
        pygame.display.flip()

    geo = renderer._inst.geo
    print(f"vertices={0 if geo is None else geo.shape[0]} "
          f"triangles={0 if renderer._inst.tris is None else renderer._inst.tris.size // 3} "
          f"frames_with_geometry={drawn}/{frames}")

    if args.hit:
        for point in args.hit.split(";"):
            x, y = (float(v) for v in point.split(","))
            print(f"hit({x:g},{y:g})={renderer.hit_part(x, y)!r}")

    if args.shot:
        raw = glReadPixels(0, 0, args.width, args.height, GL_RGBA, GL_UNSIGNED_BYTE)
        surface = pygame.image.frombuffer(raw, (args.width, args.height), "RGBA")
        out = Path(args.shot)
        out.parent.mkdir(parents=True, exist_ok=True)
        pygame.image.save(pygame.transform.flip(surface, False, True), str(out))
        print(f"shot={out}")

    renderer.close()
    pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
