from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from sidekick.avatar import files as avatar_files
from sidekick.avatar import roles
from sidekick.avatar.catalog import AvatarCatalog
from sidekick.avatar.scan import model_id

MOC = b"MOC3" + (0).to_bytes(4, "little")


def make_model(models: Path, name: str, moc: bytes = MOC) -> Path:
    folder = models / name
    folder.mkdir(parents=True)
    (folder / "m.model3.json").write_text(json.dumps({
        "FileReferences": {"Moc": "m.moc3", "Textures": ["t.png"]},
    }, ensure_ascii=False), encoding="utf-8")
    (folder / "m.moc3").write_bytes(moc)
    (folder / "t.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
    return folder


class AvatarScanTests(unittest.TestCase):
    def test_id_is_content_fingerprint_and_survives_rename(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir(parents=True)
            folder = make_model(models, "new-model")
            catalog = AvatarCatalog(root)
            items = [a for a in catalog.discover(refresh=True)
                     if a.kind == "live2d"]
            self.assertEqual(1, len(items))
            avatar = items[0]
            self.assertEqual(model_id(folder, folder / "m.model3.json",
                                      {"FileReferences": {"Moc": "m.moc3"}}),
                             avatar.id)
            self.assertTrue(avatar.usable)
            self.assertEqual("new-model", avatar.name)

            folder.rename(models / "renamed")
            again = [a for a in catalog.discover(refresh=True)
                     if a.kind == "live2d"][0]
            self.assertEqual(avatar.id, again.id, "改名不应改变角色 id")
            self.assertEqual("renamed", again.name)

    def test_role_settings_are_read_from_persona_roles(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir(parents=True)
            avatar_dir = make_model(models, "role")
            catalog = AvatarCatalog(root)
            avatar = [a for a in catalog.discover(refresh=True)
                      if a.kind == "live2d"][0]
            self.assertEqual({}, avatar.stage)

            roles.write_profile_dir(roles.role_dir(root, avatar.id), {
                "name": "用户",
                "stage": {"cps": 5.2,
                          "bubble": {"max_width": 70}},
            })
            (root / "persona" / "roles" / avatar.id).mkdir(parents=True,
                                                          exist_ok=True)
            (root / "persona" / "roles" / avatar.id / "actions.md").write_text(
                "挥手规则", encoding="utf-8")
            catalog.invalidate()
            avatar = [a for a in catalog.discover() if a.kind == "live2d"][0]
            self.assertEqual("用户", avatar.display_name)
            self.assertEqual("用户", avatar.shown_name)
            self.assertEqual(5.2, avatar.stage["cps"])
            self.assertEqual(70, avatar.stage["bubble"]["max_width"])
            self.assertTrue(avatar.role_dir.is_dir())
            self.assertEqual("挥手规则", avatar_files.read(avatar))
            self.assertEqual(avatar_dir.name, avatar.name)

    def test_role_profile_model_binding_follows_directories(self):
        """角色目录/模型目录改名：只按 profile.model 重新绑定，不按目录名猜。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir(parents=True)
            make_model(models, "old-model")
            roles.write_profile_dir(roles.role_dir(root, "custom-role"), {
                "model": "old-model", "name": "用户"})
            catalog = AvatarCatalog(root)
            avatar = [a for a in catalog.discover(refresh=True)
                      if a.kind == "live2d"][0]
            self.assertEqual("custom-role", avatar.role_id)
            self.assertEqual("用户", avatar.display_name)

            # 改 persona 目录名：profile.model 还在，绑定仍然有效。
            (root / "persona" / "roles" / "custom-role").rename(
                root / "persona" / "roles" / "renamed-role")
            catalog.invalidate()
            avatar = [a for a in catalog.discover() if a.kind == "live2d"][0]
            self.assertEqual("renamed-role", avatar.role_id)
            self.assertEqual("用户", avatar.display_name)

            # 改模型文件夹：旧 profile 找不到新文件夹，按新角色处理。
            (models / "old-model").rename(models / "new-model")
            catalog.invalidate()
            avatar = [a for a in catalog.discover() if a.kind == "live2d"][0]
            self.assertEqual("new-model", avatar.role_id)
            self.assertEqual("", avatar.display_name)

    def test_selection_comes_from_the_injected_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir(parents=True)
            make_model(models, "one", moc=b"MOC3" + (1).to_bytes(4, "little"))
            make_model(models, "two", moc=b"MOC3" + (2).to_bytes(4, "little"))
            catalog = AvatarCatalog(root)
            ids = sorted(a.id for a in catalog.discover()
                         if a.kind == "live2d")
            self.assertEqual(2, len(ids))
            wanted = {"id": ids[1]}
            catalog = AvatarCatalog(root, selection=lambda: wanted["id"])
            self.assertEqual(ids[1], catalog.selected().id)
            wanted["id"] = ids[0]
            self.assertEqual(ids[0], catalog.selected().id)
            catalog.pin()
            wanted["id"] = ids[1]
            self.assertEqual(ids[0], catalog.selected().id)
            # 新进程（没 pin）才按配置走。
            fresh = AvatarCatalog(root, selection=lambda: wanted["id"])
            self.assertEqual(ids[1], fresh.selected().id)

    def test_pinned_snapshot_survives_model_removal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir(parents=True)
            folder = make_model(models, "one", moc=b"MOC3" + (1).to_bytes(4, "little"))
            make_model(models, "two", moc=b"MOC3" + (2).to_bytes(4, "little"))
            avatar = next(a for a in AvatarCatalog(root).discover()
                          if a.kind == "live2d" and a.name == "one")
            catalog = AvatarCatalog(root, selection=lambda: avatar.id)
            catalog.pin()
            shutil.rmtree(folder)
            catalog.invalidate()
            self.assertEqual(avatar.id, catalog.selected().id)

    def test_same_name_models_get_stable_content_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir(parents=True)
            make_model(models / "a", "dup", moc=b"MOC3" + (1).to_bytes(4, "little"))
            make_model(models / "b", "dup", moc=b"MOC3" + (2).to_bytes(4, "little"))
            items = [a for a in AvatarCatalog(root).discover()
                     if a.kind == "live2d"]
            self.assertEqual(2, len(items))
            roles = [a.role_id for a in items]
            self.assertEqual(2, len(set(roles)))
            self.assertIn("dup", roles)
            self.assertTrue(any(re.fullmatch(r"dup-[0-9a-f]{4}", r)
                                for r in roles))

    def test_pinned_broken_avatar_is_not_replaced_by_another_face(self):
        from types import SimpleNamespace

        pinned = SimpleNamespace(id="p", role_id="p", usable=False)
        other = SimpleNamespace(id="o", role_id="o", usable=True)
        catalog = object.__new__(AvatarCatalog)
        catalog._pinned = "p"
        catalog._pinned_avatar = pinned
        catalog.discover = lambda **_kw: [pinned, other]
        self.assertIs(pinned, catalog.selected())

    def test_same_name_profile_owner_does_not_leak(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir(parents=True)
            folder_a = make_model(
                models / "a", "dup", moc=b"MOC3" + (1).to_bytes(4, "little"))
            folder_b = make_model(
                models / "b", "dup", moc=b"MOC3" + (2).to_bytes(4, "little"))
            id_a = model_id(folder_a, folder_a / "m.model3.json",
                            {"FileReferences": {"Moc": "m.moc3"}})
            id_b = model_id(folder_b, folder_b / "m.model3.json",
                            {"FileReferences": {"Moc": "m.moc3"}})
            roles.write_profile_dir(roles.role_dir(root, "dup"), {
                "model": "dup", "model_digest": id_b})
            items = [a for a in AvatarCatalog(root).discover()
                     if a.kind == "live2d"]
            by_id = {a.id: a for a in items}
            self.assertEqual("dup", by_id[id_b].role_id)
            self.assertNotEqual("dup", by_id[id_a].role_id)

    def test_legacy_same_name_profile_gets_digest_before_split(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir(parents=True)
            folder_a = make_model(
                models / "a", "dup", moc=b"MOC3" + (1).to_bytes(4, "little"))
            folder_b = make_model(
                models / "b", "dup", moc=b"MOC3" + (2).to_bytes(4, "little"))
            id_a = model_id(folder_a, folder_a / "m.model3.json",
                            {"FileReferences": {"Moc": "m.moc3"}})
            id_b = model_id(folder_b, folder_b / "m.model3.json",
                            {"FileReferences": {"Moc": "m.moc3"}})
            catalog = AvatarCatalog(root)
            catalog.discover()
            profile, _error = roles.read_profile(root, "dup")
            owner = profile.get("model_digest")
            self.assertIn(owner, {id_a, id_b})
            shutil.rmtree(folder_a if owner == id_a else folder_b)
            catalog.invalidate()
            items = [a for a in catalog.discover(refresh=True)
                     if a.kind == "live2d"]
            self.assertEqual(1, len(items))
            self.assertNotEqual("dup", items[0].role_id)

    def test_broken_role_profile_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir(parents=True)
            folder = make_model(models, "one")
            rid = model_id(folder, folder / "m.model3.json",
                           {"FileReferences": {"Moc": "m.moc3"}})
            roles.write_profile_dir(roles.role_dir(root, rid), {})
            path = root / "persona" / "roles" / rid / "profile.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("name: [坏", encoding="utf-8")
            avatar = next(a for a in AvatarCatalog(root).discover()
                          if a.kind == "live2d")
            self.assertIn("profile.yaml", avatar.profile_error or "")

    def test_bad_model3_is_reported_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            model = root / "models" / "broken"
            model.mkdir(parents=True)
            (model / "broken.model3.json").write_text("not-json", encoding="utf-8")
            catalog = AvatarCatalog(root)
            broken = [a for a in catalog.discover(refresh=True)
                      if a.kind == "live2d"][0]
            self.assertFalse(broken.usable)
            self.assertTrue(broken.broken)

    def test_selected_falls_back_to_first_usable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir(parents=True)
            make_model(models, "whale")
            catalog = AvatarCatalog(root, selection=lambda: "missing")
            self.assertEqual("whale", catalog.selected().name)

    def test_single_image_and_default_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir(parents=True)
            (models / "whale.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 8)
            catalog = AvatarCatalog(root)
            items = catalog.discover(refresh=True)
            img = next(a for a in items if a.kind == "image")
            self.assertEqual("whale", img.name)
            self.assertEqual(img.id, img.to_dict()["id"])
            self.assertEqual(img.to_dict()["model"].split("?")[0],
                             f"/m/{img.id}/whale.png")

            # 默认形象和普通图片一样放在 models/images/ 下，没有任何内置特例
            (models / "images").mkdir()
            (models / "images" / "deepseek.svg").write_text(
                "<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
            (models / "whale.png").unlink()
            catalog = AvatarCatalog(root)
            fallback = next(a for a in catalog.discover(refresh=True)
                            if a.name == "deepseek")
            self.assertEqual(fallback.kind, "image")
            self.assertEqual(fallback.entry, "images/deepseek.svg")
            self.assertEqual(catalog.selected().id, fallback.id)

    def test_image_in_images_dir_is_scanned(self):
        """单张图片放 models/images/ 也算形象，且 /m/ 地址带子目录。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            images = root / "models" / "images"
            images.mkdir(parents=True)
            (images / "cat.png").write_bytes(b"cat")
            (images / "notes.txt").write_bytes(b"x")
            items = AvatarCatalog(root).discover(refresh=True)
            img = next(a for a in items if a.kind == "image")
            self.assertEqual(img.name, "cat")
            self.assertEqual(img.entry, "images/cat.png")
            self.assertIn("/images/cat.png", img.to_dict()["model"])

    def test_image_suffixes_are_scanned(self):
        """根目录下的静态图都算形象：png / jpg / jpeg / webp / bmp / gif / svg。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models = root / "models"
            models.mkdir(parents=True)
            names = ("a.png", "b.jpg", "c.jpeg", "d.webp", "e.bmp", "f.gif", "g.svg")
            for name in names:
                (models / name).write_bytes(name.encode())
            (models / "skip.txt").write_bytes(b"x")
            got = {a.name for a in AvatarCatalog(root).discover(refresh=True)
                   if a.kind == "image"}
            self.assertEqual(got, {Path(n).stem for n in names})

    def test_lip_sync_uses_model_groups(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = root / "models" / "custom"
            folder.mkdir(parents=True)
            (folder / "custom.model3.json").write_text(json.dumps({
                "FileReferences": {"Moc": "custom.moc3",
                                   "Textures": ["texture.png"]},
                "Groups": [
                    {"Target": "Parameter", "Name": "LipSync",
                     "Ids": ["ParamMouthOpenY", "ParamCustomMouth"]},
                ],
            }), encoding="utf-8")
            catalog = AvatarCatalog(root)
            item = next(a for a in catalog.discover(refresh=True)
                        if a.kind == "live2d")
            self.assertEqual(item.capabilities["lipSync"],
                             ["ParamMouthOpenY", "ParamCustomMouth"])

    def test_existing_lip_sync_group_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = root / "models" / "custom"
            folder.mkdir(parents=True)
            (folder / "custom.model3.json").write_text(json.dumps({
                "FileReferences": {"Moc": "custom.moc3",
                                   "Textures": ["texture.png"]},
                "Groups": [
                    {"Target": "Parameter", "Name": "LipSync",
                     "Ids": ["ParamCustomMouth"]},
                ],
            }), encoding="utf-8")
            catalog = AvatarCatalog(root)
            item = next(a for a in catalog.discover(refresh=True)
                        if a.kind == "live2d")
            groups = catalog.effective_model3(item).get("Groups") or []
            lips = [g for g in groups
                    if str(g.get("Name") or "").lower() == "lipsync"]
            self.assertEqual(len(lips), 1)
            self.assertEqual(lips[0]["Ids"], ["ParamCustomMouth"])

    def test_missing_groups_gets_eyeblink_but_not_lipsync(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = root / "models" / "custom"
            folder.mkdir(parents=True)
            (folder / "custom.model3.json").write_text(json.dumps({
                "FileReferences": {"Moc": "custom.moc3",
                                   "Textures": ["texture.png"]},
            }), encoding="utf-8")
            catalog = AvatarCatalog(root)
            item = next(a for a in catalog.discover(refresh=True)
                        if a.kind == "live2d")
            groups = catalog.effective_model3(item).get("Groups") or []
            names = {str(g.get("Name") or "").lower() for g in groups}
            self.assertIn("eyeblink", names)
            self.assertNotIn("lipsync", names)
            self.assertEqual(item.capabilities["lipSync"], ["ParamMouthOpenY"])


if __name__ == "__main__":
    unittest.main()
