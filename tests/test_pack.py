"""三件套导入导出：预设 / 世界书 / 角色（含形象）。"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from sidekick.avatar import roles
from sidekick.avatar.catalog import AvatarCatalog
from sidekick.avatar.scan import model_id
from sidekick.brain import preset as preset_mod
from sidekick.pack import PackError
from sidekick.pack import export as pack_export
from sidekick.pack import import_ as pack_import

MODEL3 = {"FileReferences": {"Moc": "m.moc3", "Textures": ["t.png"]}}
MOC = b"MOC3" + (0).to_bytes(4, "little")


def make_model(root: Path, name: str = "测试形象") -> str:
    folder = root / "models" / name
    folder.mkdir(parents=True, exist_ok=True)
    entry = folder / "m.model3.json"
    entry.write_text(json.dumps(MODEL3), encoding="utf-8")
    (folder / "m.moc3").write_bytes(MOC)
    (folder / "t.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
    return model_id(folder, entry, MODEL3)


class PresetPackTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            src, dst = Path(a), Path(b)
            preset_mod.write(src, "p1", "名字", "说明", [
                preset_mod.Block("x", "条目", "start", "system", False, "正文")])
            blob, filename = pack_export("preset", src, "p1")
            self.assertTrue(filename.endswith(".zip"))
            result = pack_import("preset", dst, blob)
            self.assertEqual("p1", result["id"])
            got = preset_mod.read(dst, "p1")
            self.assertEqual("正文", got.blocks[0].text)
            self.assertEqual("名字", got.name)

    def test_existing_is_not_overwritten_without_flag(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            src, dst = Path(a), Path(b)
            preset_mod.write(src, "p1", "新", "", [
                preset_mod.Block("x", "条目", "start", "system", False, "新的")])
            blob, _ = pack_export("preset", src, "p1")
            preset_mod.write(dst, "p1", "旧", "", [
                preset_mod.Block("x", "条目", "start", "system", False, "旧的")])
            with self.assertRaises(PackError):
                pack_import("preset", dst, blob)
            self.assertEqual("旧的", preset_mod.read(dst, "p1").blocks[0].text)
            pack_import("preset", dst, blob, overwrite=True)
            self.assertEqual("新的", preset_mod.read(dst, "p1").blocks[0].text)


class WorldbookPackTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            src, dst = Path(a), Path(b)
            game = src / "persona" / "games" / "g1"
            (game / "entries").mkdir(parents=True)
            (game / "core.md").write_text("[总览]\n旧城。", encoding="utf-8")
            (game / "entries" / "smith.md").write_text(
                "---\ntitle: 铁匠\nkeys: [铁匠]\n---\n老铁。", encoding="utf-8")
            blob, _ = pack_export("worldbook", src, "g1")
            result = pack_import("worldbook", dst, blob)
            self.assertEqual("g1", result["id"])
            self.assertEqual(2, result["files"])
            self.assertTrue((dst / "persona" / "games" / "g1" / "core.md").is_file())
            self.assertTrue((dst / "persona" / "games" / "g1" / "entries" / "smith.md").is_file())

    def test_empty_worldbook_is_rejected(self):
        with tempfile.TemporaryDirectory() as a:
            src = Path(a)
            (src / "persona" / "games" / "empty").mkdir(parents=True)
            with self.assertRaises(PackError):
                pack_export("worldbook", src, "empty")


class RolePackTests(unittest.TestCase):
    def test_roundtrip_carries_avatar_and_role_files(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            src, dst = Path(a), Path(b)
            aid = make_model(src, "测试形象")
            roles.write_profile_dir(roles.role_dir(src, aid), {"name": "小鲸", "bot_name": "鲸"})
            roles.write_text(roles.role_dir(src, aid), "persona.md", "我是小鲸。")
            roles.write_text(roles.role_dir(src, aid), "actions.md", "可用动作：idle")
            catalog = AvatarCatalog(src)
            blob, filename = pack_export("role", src, aid, catalog=catalog)
            self.assertTrue(filename.startswith("role-"))

            other = AvatarCatalog(dst)
            result = pack_import("role", dst, blob, catalog=other)
            self.assertEqual("测试形象", result["avatar"])
            self.assertTrue(result["id"])
            self.assertTrue((dst / "models" / "测试形象" / "m.model3.json").is_file())
            got, err = roles.read_profile(dst, result["id"])
            self.assertIsNone(err)
            self.assertEqual("小鲸", got["name"])
            self.assertEqual("我是小鲸。",
                             roles.read_text(roles.role_dir(dst, result["id"]), "persona.md"))
            self.assertEqual("可用动作：idle",
                             roles.read_text(roles.role_dir(dst, result["id"]), "actions.md"))

    def test_role_without_files_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(PackError):
                pack_export("role", root, "nobody", catalog=None)


class ZipSafetyTests(unittest.TestCase):
    def test_traversal_and_wrong_kind_are_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("manifest.json",
                             json.dumps({"kind": "preset", "id": "x"}))
            archive.writestr("../evil.yaml", "x")
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(PackError):
                pack_import("preset", Path(td), buf.getvalue())

    def test_missing_manifest(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("preset.yaml", "blocks: []")
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(PackError):
                pack_import("preset", Path(td), buf.getvalue())

    def test_kind_mismatch(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("manifest.json",
                             json.dumps({"kind": "worldbook", "id": "x"}))
            archive.writestr("core.md", "x")
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(PackError):
                pack_import("preset", Path(td), buf.getvalue())


if __name__ == "__main__":
    unittest.main()
