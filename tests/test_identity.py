"""角色设定 / 用户人设 / 身份解析：id 在 config，内容在 persona/<kind>/<id>/。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sidekick import config
from sidekick.app.identity import resolve
from sidekick.avatar import roles
from sidekick.brain import users
from sidekick.brain.prompt import load_persona


def make_role(root: Path, role_id: str, texts: dict[str, str]) -> Path:
    pack = root / "persona" / "roles" / role_id
    pack.mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        (pack / name).write_text(text, encoding="utf-8")
    return pack


class RoleProfileTests(unittest.TestCase):
    def test_write_read_roundtrip_and_empty_clears(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            roles.write_profile_dir(roles.role_dir(root, "abc"), {
                "name": "用户", "bot_name": "小助手", "aliases": ["助手"],
                "stage": {"cps": 5.2,
                          "bubble": {"gap": 0.6, "max_width": 70,
                                     "auto_gap": False}},
            })
            data, err = roles.read_profile(root, "abc")
            self.assertIsNone(err)
            self.assertEqual("用户", data["name"])
            self.assertEqual(["助手"], data["aliases"])
            self.assertEqual(5.2, data["stage"]["cps"])
            self.assertFalse(data["stage"]["bubble"]["auto_gap"])
            roles.write_profile_dir(roles.role_dir(root, "abc"), {})
            self.assertEqual(({}, None), roles.read_profile(root, "abc"))

    def test_persona_file_is_empty_without_role_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual("", roles.read_text(roles.role_dir(root, "abc"), "persona.md"))
            roles.write_text(roles.role_dir(root, "abc"), "persona.md", "角色核心")
            self.assertEqual("角色核心",
                             roles.read_text(roles.role_dir(root, "abc"), "persona.md"))
            roles.write_text(roles.role_dir(root, "abc"), "persona.md", "  ")
            self.assertEqual("", roles.read_text(roles.role_dir(root, "abc"), "persona.md"))

    def test_empty_new_dir_does_not_shadow_legacy_role(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            roles.write_text(roles.role_dir(root, "fp"), "persona.md", "旧设定")
            primary = root / "persona" / "roles" / "new"
            primary.mkdir(parents=True)
            avatar = SimpleNamespace(id="fp", name="new", role_id="new")
            got = roles.ensure_dir(avatar, root)
            self.assertEqual(primary, got)
            self.assertEqual("旧设定", roles.read_text(roles.role_dir(root, "new"), "persona.md"))
            profile, _ = roles.read_profile(root, "new")
            self.assertEqual("new", profile.get("model"))
            self.assertFalse((root / "persona" / "roles" / "fp").exists())

    def test_unknown_stage_fields_are_dropped(self):
        """外观只认当前字段；旧版本残留一律丢掉，不写回 profile。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            roles.write_profile_dir(roles.role_dir(root, "abc"), {
                "stage": {"cps": 5.0, "scale": 0.9, "bubble_gap": 0.5,
                          "avatar_size": 80, "avatar_lift": 5,
                          "desktop": {"width": 420, "height": 700}}})
            data, _ = roles.read_profile(root, "abc")
            self.assertEqual({"stage": {"cps": 5.0}}, data)

    def test_merge_stage_overrides_only_appearance(self):
        base = {"enabled": True, "mode": "desktop", "host": "127.0.0.1",
                "port": 17891, "cps": 4.6,
                "bubble": {"gap": 0.4, "max_width": 60, "auto_gap": True}}
        got = roles.merge_stage(base, {
            "cps": 6,
            "bubble": {"max_width": 80, "auto_gap": False}})
        self.assertEqual(6, got["cps"])
        self.assertEqual(0.4, got["bubble"]["gap"])
        self.assertEqual(80, got["bubble"]["max_width"])
        self.assertFalse(got["bubble"]["auto_gap"])
        self.assertEqual(17891, got["port"])


class UserStoreTests(unittest.TestCase):
    def test_valid_id(self):
        for ok in ("user", "用户", "a.b-c_1"):
            self.assertTrue(users.valid_id(ok), ok)
        for bad in ("", ".", "..", "a/b", "a\\b", "a:b", "x "):
            self.assertFalse(users.valid_id(bad), bad)

    def test_write_read_list_and_no_auto_default(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual([], users.list_ids(root))
            users.write(root, "user", {
                "name": "用户", "persona": "喜欢恐怖片"})
            self.assertEqual(["user"], users.list_ids(root))
            data, err = users.read(root, "user")
            self.assertIsNone(err)
            self.assertEqual("用户", data["name"])
            self.assertEqual("喜欢恐怖片", data["persona"])

    def test_profile_fields_are_empty_without_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(({}, None), users.read(root, "nobody"))
            users.write(root, "u", {"name": "阿明"})
            data, _ = users.read(root, "u")
            self.assertEqual({"name": "阿明"}, data)


class IdentityTests(unittest.TestCase):
    def test_role_and_user_are_merged_into_cfg(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            make_role(root, "rid", {"persona.md": "角色人设"})
            roles.write_profile_dir(roles.role_dir(root, "rid"), {
                "name": "用户", "bot_name": "小助手",
                "aliases": ["助手"], "stage": {"cps": 5}})
            users.write(root, "user", {"name": "用户"})
            cfg = config.defaults()
            cfg["role"]["id"] = "rid"
            cfg["user"]["id"] = "user"
            got = resolve(cfg, root)
            self.assertEqual("小助手", got.cfg["persona"]["bot_name"])
            self.assertEqual(["助手"], got.cfg["persona"]["aliases"])
            self.assertEqual("用户", got.cfg["user"]["name"])
            self.assertEqual("rid", got.role_id)
            self.assertTrue(Path(got.cfg["role"]["role_dir"]).is_dir())
            self.assertEqual("用户", got.cfg["role"]["name"])
            self.assertEqual(5, got.cfg["stage"]["cps"])
            # enabled/mode/host/port 仍是全局值，不随角色变。
            self.assertEqual("desktop", got.cfg["stage"]["mode"])

    def test_missing_role_profile_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = config.defaults()
            got = resolve(cfg, root)
            self.assertEqual("", got.cfg["persona"]["bot_name"])
            self.assertEqual([], got.cfg["persona"]["aliases"])
            self.assertEqual("", got.cfg["role"]["name"])
            self.assertEqual("deepseek", got.role_id)

    def test_missing_user_file_is_empty_and_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = config.defaults()
            cfg["user"]["id"] = "nobody"
            got = resolve(cfg, root)
            self.assertEqual("", got.cfg["user"]["name"])
            self.assertIn("用户人设不存在", got.cfg["user"]["error"])
            self.assertTrue(any("用户人设不存在" in w for w in got.warnings))

    def test_broken_user_file_reports_error_without_raising(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "persona" / "users" / "bad" / "profile.yaml"
            path.parent.mkdir(parents=True)
            path.write_text("name: [坏", encoding="utf-8")
            cfg = config.defaults()
            cfg["user"]["id"] = "bad"
            got = resolve(cfg, root)
            self.assertIn("解析失败", got.cfg["user"]["error"])
            self.assertEqual("", got.cfg["user"]["name"])

    def test_explicit_role_id_wins_over_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = config.defaults()
            cfg["role"]["id"] = "from-config"
            got = resolve(cfg, root, role_id="pinned")
            self.assertEqual("pinned", got.role_id)


class PersonaTextTests(unittest.TestCase):
    def test_only_persona_md_is_loaded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pack = make_role(root, "rid", {
                "persona.md": "角色核心",
                "20-live.txt": "旧文件不该被读",
                "40-output.txt": "旧输出也不该被读",
            })
            text = load_persona(root, pack)
            self.assertEqual("角色核心", text)
            self.assertNotIn("旧文件", text)

    def test_comment_only_persona_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pack = make_role(root, "rid", {"persona.md": "# 还没写\n\n"})
            self.assertEqual("", load_persona(root, pack))

    def test_no_role_dir_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual("", load_persona(Path(td), None))


class PersonaDirReloadTests(unittest.TestCase):
    def test_role_dir_created_later_is_picked_up_without_rebuild(self):
        from sidekick.brain.prompt import PromptBuilder

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = resolve(config.defaults(), root, role_id="rid").cfg
            pb = PromptBuilder(cfg, root)
            self.assertEqual("", pb.build_system())

            roles.write_text(roles.role_dir(root, "rid"), "persona.md", "角色核心")
            pb.reload(resolve(config.defaults(), root, role_id="rid").cfg)
            self.assertIn("角色核心", pb.build_system())


if __name__ == "__main__":
    unittest.main()
