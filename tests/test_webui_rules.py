from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from sidekick import config
from sidekick.avatar import roles
from sidekick.avatar.autonomy import fallback as autonomy_fallback
from sidekick.avatar.autonomy import store as autonomy_store
from sidekick.avatar.catalog import AvatarCatalog
from sidekick.avatar.scan import model_id
from sidekick.webui.status_feed import StatusFeed
from webui_fixture import webui

DOSSET = {
    "params": [{"id": "ParamAngleX"}, {"id": "ParamEyeLOpen"}],
    "by_channel": {"head_x": "ParamAngleX", "eye_l_open": "ParamEyeLOpen"},
    "motions": [{"group": "idle"}],
    "expressions": [{"name": "开心"}],
    "proportions": {"archetype_guess": "unknown"},
    "model": {"name": "测试形象", "entry": "m.model3.json", "canvas": [1, 1]},
    "warnings": [],
}

MODEL3 = {
    "FileReferences": {
        "Moc": "m.moc3",
        "Textures": ["t.png"],
        "Motions": {"idle": [{"File": "idle.motion3.json"}]},
    },
}
MOC = b"MOC3" + (0).to_bytes(4, "little")


class WebUIRoleTests(QuietIsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.model = self.root / "models" / "测试形象"
        self.model.mkdir(parents=True)
        (self.model / "m.model3.json").write_text(
            json.dumps(MODEL3, ensure_ascii=False), encoding="utf-8")
        (self.model / "m.moc3").write_bytes(MOC)
        (self.model / "t.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
        self.aid = model_id(self.model, self.model / "m.model3.json", MODEL3)

        role = self.root / "persona" / "roles" / self.aid
        role.mkdir(parents=True)
        (role / "persona.md").write_text("角色核心", encoding="utf-8")

        self.cfg = config.defaults()
        self.cfg["memory"]["enabled"] = False
        self.cfg["role"]["id"] = self.aid
        self.stack = contextlib.ExitStack()
        self.ui = self.stack.enter_context(webui(self.root, self.cfg))
        self.role_id = self.ui._catalog.selected().role_id or self.aid
        self.ui._catalog = AvatarCatalog(self.root)
        self.ui._catalog.rescan()
        self.ui.csrf = "test-token"

        class Sup:
            async def status(self):
                return {}

        self.ui.supervisor = Sup()
        self.ui.feed = StatusFeed(self.ui.supervisor, self.ui.memory_stats,
                                  started_at=self.ui.started_at)
        self.patch = mock.patch("sidekick.webui.app.config.load", return_value=self.cfg)
        self.patch.start()
        self.client = TestClient(TestServer(self.ui.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.patch.stop()
        self.stack.close()
        self.tmp.cleanup()

    def url(self, path: str) -> str:
        return path

    def csrf(self) -> dict:
        return {"X-CSRF-Token": "test-token"}

    # ── 角色设定 ──────────────────────────────────────
    async def test_bootstrap_carries_role_users_and_persona_files(self):
        r = await self.client.get("/api/bootstrap")
        self.assertEqual(r.status, 200)
        payload = (await r.json())["schema"]
        items = {a["id"]: a for a in payload["avatars"]}
        self.assertIn(self.aid, items)
        item = items[self.aid]
        for key in ("display_name", "profile_error"):
            self.assertIn(key, item)
        self.assertNotIn("扫描失败", item["label"])
        self.assertEqual(payload["model_dir_rel"], "models")
        self.assertEqual(payload["role"]["id"], self.aid)
        self.assertEqual(["persona.md"], payload["persona_files"])
        self.assertIn("users", payload)
        self.assertEqual(payload["users"]["current"], "default")

    async def test_bootstrap_does_not_create_user_files(self):
        default = self.root / "persona" / "users" / "default" / "profile.yaml"
        r = await self.client.get("/api/bootstrap")
        self.assertEqual(r.status, 200)
        self.assertFalse(default.exists(), "bootstrap 不应该自动建用户档案")

    async def test_role_profile_roundtrip(self):
        r = await self.client.get(f"/api/roles/{self.aid}/profile")
        self.assertEqual(r.status, 200, await r.text())
        data = await r.json()
        self.assertEqual(data["profile"], {})

        r = await self.client.put(
            f"/api/roles/{self.aid}/profile",
            json={"name": "用户", "bot_name": "小助手", "aliases": ["助手"],
                  "stage": {"cps": 5.2}},
            headers=self.csrf())
        self.assertEqual(r.status, 200, await r.text())
        data = await r.json()
        self.assertEqual("用户", data["profile"]["name"])
        self.assertEqual(5.2, data["profile"]["stage"]["cps"])
        self.assertIn(f"persona/roles/{self.role_id}", data["role_dir"])

        response = await self.client.get("/api/bootstrap")
        payload = (await response.json())["schema"]
        item = {a["id"]: a for a in payload["avatars"]}[self.aid]
        self.assertEqual("用户", item["display_name"])

    async def test_role_persona_save_and_delete(self):
        url = f"/api/roles/{self.aid}/persona/persona.md"
        r = await self.client.get(url)
        data = await r.json()
        self.assertEqual("角色核心", data["content"])

        r = await self.client.put(url, json={"content": "新核心"},
                                  headers=self.csrf())
        self.assertEqual(r.status, 200)
        data = await (await self.client.get(url)).json()
        self.assertEqual("新核心", data["content"])

        r = await self.client.put(url, json={"content": "  "},
                                  headers=self.csrf())
        self.assertEqual(r.status, 200)
        data = await (await self.client.get(url)).json()
        self.assertEqual("", data["content"])

    async def test_roles_select_writes_config_and_requires_restart(self):
        r = await self.client.post("/api/roles/select",
                                   json={"id": self.aid},
                                   headers=self.csrf())
        self.assertEqual(r.status, 200, await r.text())
        data = await r.json()
        self.assertTrue(data["needs_restart"])
        self.assertEqual(self.aid, data["selected"])
        self.assertEqual(self.role_id, config.load_raw()["role"]["id"])

    async def test_scan_returns_payload(self):
        r = await self.client.post("/api/roles/scan", json={},
                                   headers=self.csrf())
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertTrue(data["ok"])
        self.assertIn("role", data)
        self.assertIn("avatars", data)

    # ── 动作规则（角色目录）────────────────────────────
    async def test_rules_get_put_and_generate(self):
        rules = (self.root / "persona" / "roles" / self.aid / "actions.md")
        rules.parent.mkdir(parents=True, exist_ok=True)
        rules.write_text("旧规则", encoding="utf-8")

        r = await self.client.get(f"/api/avatars/{self.aid}/rules")
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertEqual(data["content"], "旧规则")
        self.assertEqual(data["capabilities"]["actions"], ["idle"])

        r = await self.client.put(
            f"/api/avatars/{self.aid}/rules",
            json={"content": "新规则[idle]"}, headers=self.csrf())
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["content"], "新规则[idle]")

        with mock.patch("sidekick.webui.routes.avatar.avatar_rules.build_text",
                        new=mock.Mock(return_value="生成<ACT>motion=乱写</ACT>")):
            r = await self.client.post(
                f"/api/avatars/{self.aid}/rules/generate", json={},
                headers=self.csrf())
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertEqual(data["content"], "生成<ACT>motion=乱写</ACT>")
        self.assertEqual(data["unknown_tags"], ["乱写"])

        r = await self.client.put(
            f"/api/avatars/{self.aid}/rules", json={"content": ""},
            headers=self.csrf())
        self.assertEqual(r.status, 200)
        self.assertFalse(rules.exists())

    async def test_bad_encoding_rules_drops_rules_only(self):
        rules = self.root / "persona" / "roles" / self.aid / "actions.md"
        rules.parent.mkdir(parents=True, exist_ok=True)
        rules.write_bytes("旧规则".encode("gbk"))
        r = await self.client.get(f"/api/avatars/{self.aid}/rules")
        self.assertEqual(r.status, 200, await r.text())
        data = await r.json()
        self.assertEqual(data["content"], "")
        self.assertIn("UnicodeDecodeError", data["error"])

    # ── 自主驱动配置 ──────────────────────────────────
    async def test_autonomy_audit_generate_apply_delete(self):
        r = await self.client.get(f"/api/avatars/{self.aid}/autonomy")
        self.assertEqual(r.status, 200)
        self.assertFalse((await r.json())["exists"])

        with mock.patch(
                "sidekick.webui.routes.avatar.run_audit", return_value=DOSSET):
            r = await self.client.post(
                f"/api/avatars/{self.aid}/autonomy/audit", json={},
                headers=self.csrf())
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["summary"]["tier"], "none")

        preview = autonomy_fallback.fallback_profile(DOSSET)

        async def fake_stream(avatar, cfg, *, preferences="", project_root=None,
                              progress=None):
            await progress({"type": "status", "stage": "thinking"})
            await progress({"type": "status", "stage": "outputting"})
            await progress({"type": "delta", "text": '{"profile":'})
            return {"profile": preview, "fallback": False, "issues": [],
                    "dossier": {"tier": "face"}}

        with mock.patch(
                "sidekick.webui.routes.avatar.autonomy_generate.generate_profile",
                new=fake_stream):
            r = await self.client.post(
                f"/api/avatars/{self.aid}/autonomy/generate/stream", json={},
                headers=self.csrf())
            text = await r.text()
        self.assertEqual(r.status, 200)
        self.assertIn('"type": "delta"', text)
        self.assertIn('"type": "result"', text)

        with mock.patch(
                "sidekick.webui.routes.avatar.run_audit", return_value=DOSSET):
            r = await self.client.put(
                f"/api/avatars/{self.aid}/autonomy",
                json={"profile": preview}, headers=self.csrf())
        self.assertEqual(r.status, 200, await r.text())
        self.assertTrue((await r.json())["needs_restart"])
        stored, error = autonomy_store.load(
            self.ui._catalog.get(self.aid))
        self.assertIsNone(error)
        self.assertEqual(stored["archetype"], "unknown")

        r = await self.client.delete(
            f"/api/avatars/{self.aid}/autonomy", headers=self.csrf())
        self.assertEqual(r.status, 200)

    async def test_autonomy_rejects_spine_and_bad_profile(self):
        avatar = self.ui._catalog.get(self.aid)
        avatar.kind = "spine"
        r = await self.client.get(f"/api/avatars/{self.aid}/autonomy")
        self.assertEqual(r.status, 400)
        avatar.kind = "live2d"

        with mock.patch(
                "sidekick.webui.routes.avatar.run_audit", return_value=DOSSET):
            r = await self.client.put(
                f"/api/avatars/{self.aid}/autonomy",
                json={"profile": {"schema_version": 99}}, headers=self.csrf())
        self.assertEqual(r.status, 400)
        self.assertTrue((await r.json())["issues"])

    # ── 用户设定 ──────────────────────────────────────
    async def test_users_crud_and_select(self):
        r = await self.client.post("/api/users", json={"id": "user"},
                                   headers=self.csrf())
        self.assertEqual(r.status, 200, await r.text())
        data = await r.json()
        self.assertEqual({"user"}, {u["id"] for u in data["users"]["items"]})

        r = await self.client.put("/api/users/user",
                                  json={"name": "用户", "persona": "喜欢恐怖片"},
                                  headers=self.csrf())
        self.assertEqual(r.status, 200)
        profile = (await r.json())["profile"]
        self.assertEqual(profile["name"], "用户")
        self.assertEqual(profile["persona"], "喜欢恐怖片")

        r = await self.client.post("/api/users", json={"id": "other"},
                                   headers=self.csrf())
        self.assertEqual(r.status, 200)

        r = await self.client.post("/api/users/select", json={"id": "user"},
                                   headers=self.csrf())
        self.assertEqual(r.status, 200)
        self.assertEqual("user", (await r.json())["current"])
        self.assertEqual("user", config.load_raw()["user"]["id"])
        self.cfg["user"]["id"] = "user"   # 模拟下一次 config.load()

        # 当前用户不能删；别的可以。
        r = await self.client.delete("/api/users/user?confirm=1",
                                     headers=self.csrf())
        self.assertEqual(r.status, 400)
        r = await self.client.delete("/api/users/other?confirm=1",
                                     headers=self.csrf())
        self.assertEqual(r.status, 200)
        ids = {u["id"] for u in (await r.json())["users"]["items"]}
        self.assertEqual({"user"}, ids)

    async def test_users_reject_bad_ids(self):
        r = await self.client.post("/api/users", json={"id": "a/b"},
                                   headers=self.csrf())
        self.assertEqual(r.status, 400)
        # 单用户没有 GET（页面用 bootstrap 里的列表）；PUT 才是读写口。
        r = await self.client.put("/api/users/a%2Fb", json={"name": "x"},
                                  headers=self.csrf())
        self.assertEqual(r.status, 400)

    # ── 其它 ──────────────────────────────────────────
    async def test_preview_config_uses_role_stage(self):
        roles.write_profile_dir(roles.role_dir(self.root, self.aid), {
            "stage": {"cps": 6,
                      "bubble": {"max_width": 80, "auto_gap": False}}})
        self.ui._catalog.invalidate()
        r = await self.client.get("/preview-config.json?avatar=" + self.aid)
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertEqual(data["layout"]["maxWidth"], 80)
        self.assertFalse(data["layout"]["autoGap"])

    async def test_preview_config_uses_shared_layout(self):
        r = await self.client.get("/preview-config.json?avatar=" + self.aid)
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertEqual(data["avatar"], self.aid)
        self.assertIn("layout", data)
        self.assertIn("gap", data["layout"])

    async def test_split_domains_bootstrap_status_and_disabled_memory(self):
        for path in ("/api/bootstrap", "/api/status"):
            r = await self.client.get(path)
            self.assertEqual(r.status, 200, path)
        for path in ("/api/memories",):
            r = await self.client.get(path)
            self.assertEqual(r.status, 409, path)
            self.assertIn("记忆已关闭", (await r.json())["error"])

    async def test_status_does_not_create_memory_db(self):
        db = self.root / "state" / "status-only.db"
        self.cfg["memory"].update({"enabled": True, "db_path": str(db)})
        r = await self.client.get("/api/status?refresh=1")
        self.assertEqual(r.status, 200)
        self.assertFalse(db.exists())

    async def test_game_select_refuses_to_overwrite_bad_config(self):
        game = self.root / "persona" / "games" / "foo"
        game.mkdir(parents=True)
        (game / "core.md").write_text("【Foo】\n", encoding="utf-8")
        with mock.patch.object(config, "last_error", "bad yaml"), \
                mock.patch("sidekick.config.load_raw", return_value={}), \
                mock.patch("sidekick.config.save_raw") as save:
            r = await self.client.post(
                "/api/games/select", json={"id": "foo"},
                headers=self.csrf())
        self.assertEqual(r.status, 409, await r.text())
        save.assert_not_called()

    async def test_config_endpoint_is_write_only(self):
        # /api/config 只剩 PATCH；GET 不再提供。
        r = await self.client.get("/api/config")
        self.assertEqual(r.status, 405)


if __name__ == "__main__":
    unittest.main()
