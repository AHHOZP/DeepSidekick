"""单张图片形象：常见格式、透明通道、SVG 上色与按尺寸光栅化。

不开窗、不碰 GL：渲染器用假纹理顶掉 `SurfaceTexture`，其余全走真解码。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pygame
from PIL import Image

from sidekick.avatar.builder import build_image
from sidekick.desktop.image import ImageRenderer, decode, fit, render_svg
from sidekick.desktop.pet_model import PART_BODY

ROOT = Path(__file__).resolve().parents[1]


class _FakeTexture:
    """只记下最后一次上传的表面，省掉 GL 上下文。"""

    def __init__(self) -> None:
        self.surface: pygame.Surface | None = None

    def upload(self, surface: pygame.Surface) -> None:
        self.surface = surface

    def draw(self, _width: int, _height: int) -> None:
        return None


class _PictureCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def png(self, name: str = "a.png") -> Path:
        """40×30 的蓝块，四周留透明边。"""
        surf = pygame.Surface((40, 30), pygame.SRCALPHA)
        surf.fill((0, 0, 0, 0))
        pygame.draw.rect(surf, (20, 120, 240, 255), (10, 5, 20, 20))
        path = self.dir / name
        pygame.image.save(surf, str(path))
        return path

    def svg(self, name: str, body: str) -> Path:
        """viewBox 100×100、填充色写在 <style> 的类里（Illustrator / Figma 的导法）。"""
        path = self.dir / name
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            f"<style>.cls-1 {{ fill: #4d6bfe; }}</style>{body}</svg>",
            encoding="utf-8")
        return path


class DecodeTests(_PictureCase):
    def test_png_keeps_transparency(self):
        surf = decode(self.png())
        self.assertEqual(surf.get_size(), (40, 30))
        self.assertEqual(tuple(surf.get_at((20, 15))), (20, 120, 240, 255))
        self.assertEqual(surf.get_at((0, 0)).a, 0)

    def test_webp_keeps_transparency(self):
        path = self.dir / "a.webp"
        image = Image.new("RGBA", (40, 30), (0, 0, 0, 0))
        image.paste((20, 120, 240, 255), (10, 5, 30, 25))
        image.save(path, lossless=True)
        surf = decode(path)
        self.assertEqual(surf.get_at((0, 0)).a, 0)
        self.assertEqual(tuple(surf.get_at((20, 15))), (20, 120, 240, 255))

    def test_formats_without_alpha_come_back_opaque(self):
        """jpg / bmp 没有 alpha 通道：必须当成全不透明，而不是留一圈黑。"""
        for name in ("a.jpg", "a.bmp"):
            Image.open(self.png()).convert("RGB").save(self.dir / name)
            surf = decode(self.dir / name)
            self.assertEqual(surf.get_size(), (40, 30), name)
            self.assertEqual(surf.get_at((0, 0)).a, 255, name)

    def test_gif_transparency_becomes_alpha(self):
        path = self.dir / "a.gif"
        image = Image.new("P", (40, 30), 0)
        image.putpalette([0, 0, 0, 20, 120, 240] + [0, 0, 0] * 254)
        image.paste(1, (10, 5, 30, 25))
        image.save(path, transparency=0)
        surf = decode(path)
        self.assertEqual(surf.get_at((0, 0)).a, 0)
        self.assertEqual(surf.get_at((20, 15)).a, 255)


class SvgTests(_PictureCase):
    def test_fill_from_style_block_is_applied(self):
        path = self.svg("a.svg", '<rect class="cls-1" width="100" height="100"/>')
        surf = render_svg(path, 40, 40)
        self.assertEqual(tuple(surf.get_at((20, 20))), (77, 107, 254, 255))

    def test_rasterized_at_target_size(self):
        path = self.svg("a.svg", '<rect class="cls-1" width="100" height="100"/>')
        self.assertEqual(render_svg(path, 60, 200).get_size(), (60, 60))
        self.assertEqual(render_svg(path, 200, 60).get_size(), (60, 60))

    def test_view_box_offset_is_not_cropped(self):
        """viewBox 的 min-x/min-y 不是 0 时也不能错位或裁掉一圈。"""
        path = self.dir / "off.svg"
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="10 20 100 100">'
            '<rect x="10" y="20" width="100" height="100" fill="#00ff00"/></svg>',
            encoding="utf-8")
        self.assertEqual(tuple(render_svg(path, 80, 80).get_bounding_rect()),
                         (0, 0, 80, 80))

    def test_inline_style_wins_over_class(self):
        path = self.svg(
            "a.svg",
            '<rect class="cls-1" style="fill: #ff0000" width="100" height="100"/>')
        self.assertEqual(tuple(render_svg(path, 20, 20).get_at((10, 10)))[:3],
                         (255, 0, 0))

    def test_builtin_fallback_is_not_black(self):
        """回归：nanosvg 不认 <style>，没人摊平 CSS 时整头鲸鱼会被画成纯黑。"""
        surf = render_svg(ROOT / "models" / "images" / "deepseek.svg", 200, 148)
        opaque = [(x, y) for x in range(0, 200, 2) for y in range(0, 148, 2)
                  if surf.get_at((x, y)).a >= 250]
        self.assertTrue(opaque)
        self.assertTrue(all(max(surf.get_at(p)[:3]) > 40 for p in opaque))


class FitTests(unittest.TestCase):
    def test_keeps_aspect_ratio(self):
        surf = pygame.Surface((100, 50), pygame.SRCALPHA)
        self.assertEqual(fit(surf, 40, 40).get_size(), (40, 20))
        self.assertEqual(fit(surf, 400, 40).get_size(), (80, 40))
        self.assertEqual(fit(surf, 0, 30).get_size(), (1, 1))


class RendererTests(_PictureCase):
    """窗口里怎么摆：等比、贴底居中、透明处不算命中。"""

    def renderer(self, entry: Path) -> ImageRenderer:
        avatar = build_image(self.dir, entry)
        with mock.patch("sidekick.desktop.image.SurfaceTexture", _FakeTexture):
            return ImageRenderer(avatar, 200, 200)

    def test_picture_is_fitted_bottom_centered(self):
        model = self.renderer(self.png())
        self.assertEqual(model.rect.size, (200, 150))
        self.assertEqual(model.rect.midbottom, (100, 200))
        self.assertAlmostEqual(model.aspect, 4 / 3, places=3)
        self.assertEqual(model.notice, "")
        self.assertEqual(model.hit_part(100, 120), PART_BODY)
        self.assertEqual(model.hit_part(100, 190), "")
        self.assertEqual(model.hit_part(100, 10), "")

    def test_broken_picture_reports_notice(self):
        bad = self.dir / "bad.png"
        bad.write_text("not a png", encoding="utf-8")
        model = self.renderer(bad)
        self.assertIsNone(model.surface)
        self.assertTrue(model.notice.startswith("图片打不开：bad.png"), model.notice)
        self.assertEqual(model.hit_part(100, 100), "")

    def test_raster_decode_is_cached_across_resizes(self):
        model = self.renderer(self.png())
        model.resize(300, 300)
        model.resize(120, 120)
        self.assertEqual(model.rect.size, (120, 90))

    def test_svg_refits_at_every_size(self):
        path = self.svg("a.svg", '<rect class="cls-1" width="100" height="100"/>')
        model = self.renderer(path)
        self.assertEqual(model.rect.size, (200, 200))
        model.resize(50, 50)
        self.assertEqual(model.rect.size, (50, 50))


if __name__ == "__main__":
    unittest.main()
