"""Spine 侧的纯 Python 单测：不加载原生库、不碰 GL、不需要真实模型。

用临时目录现造最小骨架/图集，覆盖"扫描期判定 + 按需读名字"这条链路。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidekick.avatar.builder import build_spine
from sidekick.avatar.catalog import AvatarCatalog
from sidekick.avatar.validation import validate_spine
from sidekick.spine.pma import looks_premultiplied, pma_of
from sidekick.spine import names as spine_names
from sidekick.spine.atlas import parse_atlas, parse_pma
from sidekick.spine.binding import skeleton_version


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _string(text: str) -> bytes:
    """spine 的字符串长度是「字符数 + 1」（NUL 只体现在长度里，文件里不写）。"""
    raw = text.encode("utf-8")
    return _varint(len(raw) + 1) + raw


def make_skel(version: str = "3.8.99") -> bytes:
    """造一个只有文件头的二进制骨架（版本号之后随便跟点字节）。"""
    return _string("xCoNvV33zGf72m9Bb0QCZb8+yS4") + _string(version) + b"\x00" * 32


def make_json(version: str = "4.2.12", animations=("idle", "wave")) -> bytes:
    doc = {"skeleton": {"spine": version},
           "animations": {name: {} for name in animations}}
    return json.dumps(doc).encode("utf-8")


def make_atlas(image: str = "a.png") -> str:
    return f"{image}\nsize: 64, 32\nformat: RGBA8888\nfilter: Linear,Linear\nrepeat: none\n"


class SkeletonVersionTest(unittest.TestCase):
    def test_binary_38(self) -> None:
        self.assertEqual(skeleton_version(make_skel("3.8.99")), "3.8")

    def test_binary_41(self) -> None:
        # 4.x：8 字节 hash + 版本串
        raw = b"\x98\x71\x9c\xe3\x66\xe7\x6b\xc0" + _string("4.1.11") + b"\x00" * 16
        self.assertEqual(skeleton_version(raw), "4.1")

    def test_json(self) -> None:
        self.assertEqual(skeleton_version(make_json("4.2.12")), "4.2")

    def test_garbage(self) -> None:
        with self.assertRaises(ValueError):
            skeleton_version(b"\xff\xff\xff\xffnot a skeleton")


class AtlasParseTest(unittest.TestCase):
    def test_pages(self) -> None:
        text = ("a.png\nsize: 10, 20\nformat: RGBA8888\n"
                "region1\n  rotate: false\n  xy: 1, 2\n  size: 3, 4\n"
                "b.png\nsize: 30, 40\n")
        self.assertEqual(parse_atlas(text),
                         [{"image": "a.png", "w": 10, "h": 20},
                          {"image": "b.png", "w": 30, "h": 40}])

    def test_region_without_size_is_not_page(self) -> None:
        text = "a.png\nsize: 10, 20\njustaregion\n  rotate: false\n"
        self.assertEqual(len(parse_atlas(text)), 1)

    def test_pma_hint(self) -> None:
        self.assertIs(parse_pma("a.png\nsize: 2048,2048\npma:true\n"), True)
        self.assertIs(parse_pma("a.png\nsize: 2,2\npma:false\n"), False)
        self.assertIsNone(parse_pma("a.png\nsize: 2,2\n"))


class ValidateSpineTest(unittest.TestCase):
    def _model(self, tmp: Path, *, version: str, atlas: str | None,
               image: bool = True) -> Path:
        model = tmp / "hero"
        model.mkdir()
        entry = model / "hero.skel"
        entry.write_bytes(make_skel(version))
        if atlas is not None:
            (model / "hero.atlas").write_text(atlas, encoding="utf-8")
        if image:
            (model / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        return entry

    def test_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entry = self._model(Path(tmp), version="3.8.99", atlas=make_atlas())
            broken, _ = validate_spine(entry.parent, entry)
            self.assertEqual(broken, [])

    def test_missing_page_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entry = self._model(Path(tmp), version="3.8.99",
                                atlas=make_atlas(), image=False)
            broken, _ = validate_spine(entry.parent, entry)
            self.assertTrue(any("图集页缺失" in line for line in broken))

    def test_unsupported_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entry = self._model(Path(tmp), version="3.6.53", atlas=make_atlas())
            broken, _ = validate_spine(entry.parent, entry)
            self.assertTrue(any("不支持 Spine 3.6" in line for line in broken))

    def test_json_linear_curve_is_rejected(self) -> None:
        """readCurve 只认 "stepped"，写成字符串会让原生解析器崩进程。"""
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "hero"
            model.mkdir()
            entry = model / "hero.json"
            entry.write_text(json.dumps({
                "skeleton": {"spine": "4.2.12"},
                "animations": {"idle": {"bones": {
                    "root": {"rotate": [{"time": 0, "curve": "linear"}]}}}},
            }), encoding="utf-8")
            (model / "hero.atlas").write_text(make_atlas(), encoding="utf-8")
            (model / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            broken, _ = validate_spine(model, entry)
            self.assertTrue(any("curve" in line for line in broken))


class BuildSpineTest(unittest.TestCase):
    def test_json_reads_animation_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "hero"
            model.mkdir()
            entry = model / "hero.json"
            entry.write_bytes(make_json(animations=("idle", "wave")))
            (model / "hero.atlas").write_text(make_atlas(), encoding="utf-8")
            (model / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            avatar = build_spine(model, entry)
            self.assertEqual(avatar.kind, "spine")
            self.assertEqual([a["tag"] for a in avatar.capabilities["actions"]],
                             ["idle", "wave"])
            self.assertEqual(avatar.idle, "idle")

    def test_skel_defers_names(self) -> None:
        """`.skel` 的名字要问原生库，扫描期必须留空（P6 的回归测试）。"""
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "hero"
            model.mkdir()
            entry = model / "hero.skel"
            entry.write_bytes(make_skel())
            (model / "hero.atlas").write_text(make_atlas(), encoding="utf-8")
            (model / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            avatar = build_spine(model, entry)
            self.assertEqual(avatar.capabilities["actions"], [])
            self.assertEqual(avatar.idle, "")


class EnsureCapsTest(unittest.TestCase):
    def _avatar(self, tmp: Path):
        model = tmp / "hero"
        model.mkdir()
        entry = model / "hero.skel"
        entry.write_bytes(make_skel())
        (model / "hero.atlas").write_text(make_atlas(), encoding="utf-8")
        (model / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        return build_spine(model, entry)

    def test_names_are_filled_lazily(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            avatar = self._avatar(Path(tmp))
            catalog = AvatarCatalog(tmp)
            with mock.patch.object(spine_names, "read_names",
                                   return_value=(["idle", "wave"], "")) as probe:
                catalog._ensure_caps(avatar)
                catalog._ensure_caps(avatar)     # 第二次不应再探
            self.assertEqual(probe.call_count, 1)
            self.assertEqual([a["tag"] for a in avatar.capabilities["actions"]],
                             ["idle", "wave"])
            self.assertEqual(avatar.idle, "idle")

    def test_error_goes_to_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            avatar = self._avatar(Path(tmp))
            catalog = AvatarCatalog(tmp)
            with mock.patch.object(spine_names, "read_names",
                                   return_value=([], "缺少运行时")):
                catalog._ensure_caps(avatar)
            self.assertIn("缺少运行时", avatar.warnings)
            self.assertEqual(avatar.capabilities["actions"], [])


class PremultipliedGuessTest(unittest.TestCase):
    """没有 `pma:` 声明时的兜底判断：按违例比例，而不是"有一处就否"。"""

    def _image(self, rgba):
        from PIL import Image
        image = Image.new("RGBA", (len(rgba), 1), (0, 0, 0, 0))
        for x, color in enumerate(rgba):
            image.putpixel((x, 0), color)
        return image

    def test_premultiplied(self) -> None:
        self.assertTrue(looks_premultiplied([self._image([(128, 0, 0, 128)])]))

    def test_straight(self) -> None:
        self.assertFalse(looks_premultiplied([self._image([(255, 0, 0, 128)])]))

    def test_few_noisy_pixels_are_tolerated(self) -> None:
        pixels = [(128, 0, 0, 128)] * 99 + [(200, 0, 0, 100)]
        self.assertTrue(looks_premultiplied([self._image(pixels)]))


class PmaOfAtlasTest(unittest.TestCase):
    """服务端给浏览器的预乘结论：先信 atlas 的 pma，没写才看像素。"""

    def _model(self, tmp: str, pma_line: str, color) -> Path:
        from PIL import Image
        root = Path(tmp)
        Image.new("RGBA", (2, 1), color).save(root / "a.png")
        lines = ["a.png", "size: 2,1"]
        if pma_line:
            lines.append(pma_line)
        lines += ["r", "  bounds: 0,0,2,1"]
        (root / "a.atlas").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return root / "a.atlas"

    def test_declared_true_wins_even_if_pixels_look_straight(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            atlas = self._model(td, "pma: true", (255, 0, 0, 128))
            self.assertTrue(pma_of(atlas))

    def test_undeclared_falls_back_to_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            atlas = self._model(td, "", (255, 0, 0, 128))
            self.assertFalse(pma_of(atlas))

    def test_missing_atlas_returns_none(self) -> None:
        self.assertIsNone(pma_of(Path("nope") / "missing.atlas"))


class ReadNamesProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        spine_names._MEMO.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.entry = Path(self._tmp.name) / "hero.skel"
        self.entry.write_bytes(b"\x00" * 16)

    def test_parses_child_json(self) -> None:
        payload = json.dumps({str(self.entry): {"names": ["idle"], "error": ""}})
        proc = mock.Mock(returncode=0, stdout=payload.encode("utf-8"), stderr=b"")
        with mock.patch.object(spine_names.subprocess, "run", return_value=proc):
            names, error = spine_names.read_names(self.entry)
        self.assertEqual((names, error), (["idle"], ""))

    def test_bad_exit(self) -> None:
        proc = mock.Mock(returncode=1, stdout=b"", stderr="boom".encode())
        with mock.patch.object(spine_names.subprocess, "run", return_value=proc):
            names, error = spine_names.read_names(self.entry)
        self.assertEqual(names, [])
        self.assertIn("boom", error)

    def test_memo_skips_second_run(self) -> None:
        payload = json.dumps({str(self.entry): {"names": ["idle"], "error": ""}})
        proc = mock.Mock(returncode=0, stdout=payload.encode("utf-8"), stderr=b"")
        with mock.patch.object(spine_names.subprocess, "run", return_value=proc) as run:
            spine_names.read_names(self.entry)
            spine_names.read_names(self.entry)
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
