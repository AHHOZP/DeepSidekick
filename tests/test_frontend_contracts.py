from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "sidekick" / "webui" / "static" / "app.js"
OVERLAY = ROOT / "overlay" / "app.js"


def app_js() -> str:
    return APP.read_text(encoding="utf-8")


def overlay_js() -> str:
    return OVERLAY.read_text(encoding="utf-8")


class WebRulesEditorContractTests(unittest.TestCase):
    """静态回归：`caps` 在 renderRules 里必须先声明后使用。"""

    def test_caps_declared_before_use_in_render_rules(self):
        src = app_js()
        start = src.index("function renderRules(data)")
        end = src.index("async function loadAvatarRules", start)
        body = src[start:end]
        decl = re.search(r"\bconst\s+caps\s*=", body)
        self.assertIsNotNone(decl, "renderRules 里找不到 const caps")
        first_use = re.search(r"\bcaps\.", body)
        self.assertIsNotNone(first_use)
        self.assertLess(decl.start(), first_use.start(),
                        "caps 在声明前被使用（TDZ ReferenceError）")


class WebUiBrokenAvatarContractTests(unittest.TestCase):
    """坏模型不进下拉，但要在角色页把它们黄字列出来（不再依赖控制台扫描命令）。"""

    def test_role_tab_has_broken_avatar_warning(self):
        html = (ROOT / "sidekick" / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn('id="role-broken"', html)
        css = (ROOT / "sidekick" / "webui" / "static" / "style.css").read_text(
            encoding="utf-8")
        self.assertIn(".broken-note", css)

        src = app_js()
        start = src.index("function renderRolePicker()")
        end = src.index("function applyRolePayload", start)
        body = src[start:end]
        self.assertIn("$('#role-broken')", body)
        self.assertIn("损坏模型：", body)
        self.assertNotIn("sidekick.avatar --scan", src)


class WebUiLayoutContractTests(unittest.TestCase):
    def test_user_new_comes_before_current_and_persona_is_wide(self):
        index = (ROOT / "sidekick" / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")
        self.assertLess(index.index('id="user-id-input"'),
                        index.index('id="user-list"'))
        # 人设只有一个保存：下拉右侧那个（保存人设 + 当前用户一起）
        self.assertNotIn('id="btn-user-save"', index)

    def test_pace_page_only_exposes_mode_and_clarity(self):
        index = (ROOT / "sidekick" / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn('data-tab="pace"', index)
        self.assertIn('id="tab-pace"', index)
        self.assertIn('id="pace-forms"', index)
        self.assertLess(index.index('data-tab="pace"'),
                        index.index('data-tab="live"'))
        # 左侧顺序：用户设定 → 角色设定 → 游戏资料。
        self.assertLess(index.index('data-tab="user"'),
                        index.index('data-tab="role"'))
        self.assertLess(index.index('data-tab="role"'),
                        index.index('data-tab="game"'))

        from sidekick.config.spec import GROUPS
        groups = {g["id"]: g for g in GROUPS}
        def fields(gid):
            return {f for s in groups[gid]["sections"] for f in s["fields"]}
        live = fields("live")
        pace = fields("pace")
        stt = fields("stt")
        toggles = fields("toggles")
        # 所有「启用 xxx」集中在概览页的启用控制里，各功能页不再重复
        self.assertIn("capture.enabled", toggles)
        self.assertNotIn("capture.enabled", pace)
        self.assertNotIn("capture.enabled", live)
        for key in ("mic.enabled", "stt.enabled", "tts.enabled",
                    "stage.enabled", "memory.enabled", "game.enabled",
                    "brain.visual_memory_tag"):
            self.assertIn(key, toggles, key)
        self.assertNotIn("session.live", toggles)
        self.assertEqual({"capture.mode", "capture.clarity_percent"}, pace)
        self.assertIn("scheduler.batch_s", live)
        self.assertNotIn("scheduler.batch_s", pace)
        self.assertIn("scheduler.me_wait_short_ms", stt)
        self.assertNotIn("scheduler.me_wait_short_ms", live)
        self.assertNotIn("applyResolutionFilter", app_js())

    def test_privacy_page_and_window_title_toggle_are_gone(self):
        index = (ROOT / "sidekick" / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")
        self.assertNotIn('data-tab="privacy"', index)
        self.assertNotIn('id="tab-privacy"', index)
        self.assertNotIn('id="privacy-forms"', index)
        from sidekick.config.spec import GROUPS, HELP, OPTIONS
        groups = {g["id"]: g for g in GROUPS}
        self.assertNotIn("privacy", groups)
        self.assertEqual("", groups["pace"].get("desc", ""))
        self.assertNotIn("单人陪玩也生效", app_js())
        self.assertEqual(
            "剧烈：适合画面经常变化的情况，例如动作游戏，剧情电影；"
            "平缓：适合画面不怎么变动的情况，例如工作，聊天。",
            HELP["capture.mode"])
        self.assertEqual(
            "需要精准识别字幕请调至高清以上，固定以1920×1080为100%，"
            "与屏幕分辨率无关",
            HELP["capture.clarity_percent"])
        pace = {f for s in groups["pace"]["sections"] for f in s["fields"]}
        self.assertEqual({"capture.mode", "capture.clarity_percent"}, pace)
        self.assertEqual(
            ["剧烈（游戏/视频，画面变化快）",
             "平缓（聊天/工作，画面变化慢）"],
            [label for _value, label in OPTIONS["capture.mode"]])
        self.assertEqual(
            ["标准（66.6%，约578 token/pic）",
             "高清（83.3%，约882 token/pic）",
             "原画（100%，约968 token/pic）"],
            [label for _value, label in OPTIONS["capture.clarity_percent"]])
        self.assertNotIn("saveBar($('#privacy-forms')", app_js())

    def test_role_page_order_and_no_base_panel(self):
        index = (ROOT / "sidekick" / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")
        order = ['id="role-list"', 'id="role-profile-form"',
                 'id="role-persona-text"', 'id="stage-preview"',
                 'id="rules-text"', 'id="role-stage-form"']
        for earlier, later in zip(order, order[1:]):
            self.assertLess(index.index(earlier), index.index(later))
        self.assertNotIn("base-persona-text", index)
        self.assertNotIn("base-profile-form", index)
        # 人设文本默认折叠：它对应的 details 没有 open。
        start = index.index('<summary><h2>角色人设</h2>')
        details = index.rindex("<details", 0, start)
        self.assertNotIn("open", index[details:start])

    def test_role_stage_fields_are_per_role(self):
        src = app_js()
        for field in ("cps", "bubble.gap", "bubble.max_width", "bubble.auto_gap"):
            self.assertIn(field, src)
        self.assertIn("stage-global-form", src)


class WebUiModelTableContractTests(unittest.TestCase):
    def test_summary_failure_alert_has_amber_and_red(self):
        src = app_js()
        self.assertIn("summary_failures", src)
        self.assertIn("summary_dropping", src)
        self.assertIn("alert-amber", src)
        self.assertIn("alert-red", src)

    def test_memory_context_column_and_stt_shared_controls(self):
        index = (ROOT / "sidekick" / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn("语音识别模型", index)
        self.assertLess(index.index('id="btn-game-mem-add"'),
                        index.index('id="game-mem-tabs"'))
        src = app_js()
        self.assertIn("m.max_context", src)
        self.assertIn("<th>语言</th>", src)
        self.assertIn("<th>模型</th>", src)
        self.assertIn("<th>下载大小</th>", src)
        self.assertIn("<th>峰值内存</th>", src)
        self.assertIn("<th>C-MTEB</th>", src)
        self.assertIn("<th>MTEB</th>", src)
        self.assertIn("<th>MMTEB</th>", src)
        self.assertIn("<th>状态</th>", src)
        self.assertNotIn("<th>维度</th>", src)
        self.assertNotIn("许可证 / 状态", src)
        self.assertIn("<th>AISHELL-1</th>", src)
        self.assertIn('class="status-line"', src)
        self.assertIn("data-stt-dl", src)
        self.assertIn("data-stt-del", src)
        self.assertIn("pollJob(", src)


class WebUiDraftContractTests(unittest.TestCase):
    def test_overview_cards_use_status_role_and_user(self):
        src = app_js()
        start = src.index("function renderCards()")
        end = src.index("function renderFeatureCards", start)
        body = src[start:end]
        self.assertIn("rt.avatar", body)
        self.assertIn("rt.user", body)
        self.assertNotIn("b.user", body)

    def test_overview_alert_shows_user_profile_error(self):
        src = app_js()
        start = src.index("function renderAlert()")
        end = src.index("function renderStatus", start)
        self.assertIn("user.error", src[start:end])

    def test_app_js_game_switch_and_draft_contracts(self):
        src = app_js()
        start = src.index("async function saveGameSelection()")
        end = src.index("// ── 记忆", start)
        body = src[start:end]
        self.assertIn("S.config.game", body)
        self.assertIn("delete S.config.game.path", body)
        # 下拉只是浏览：切游戏只读正文，不写配置
        self.assertIn("await loadGameText(next)", src)

        start = src.index("async function previewTts()")
        end = src.index("// ── 角色（形象 + 角色设定）", start)
        body = src[start:end]
        self.assertIn("body = {", body)
        self.assertIn("profile: p.name, preset: p.preset, block,", body)
        self.assertIn("S.secretSets[`tts.${p.name}.api_key`]", body)

    def test_app_js_dirty_and_guard_contracts(self):
        src = app_js()
        # 未保存的修改按原因分开记，每类各有一个保存动作
        self.assertIn("const DIRTY = {", src)
        self.assertIn("markDirty('persona')", src)
        self.assertIn("markDirty('game')", src)
        self.assertIn("markDirty('rules')", src)
        self.assertIn("function anyDirty()", src)
        self.assertIn("if (anyDirty())", src)
        self.assertIn("切换会丢失", src)
        self.assertIn("await bootstrap(false);", src)

        start = src.index("if (type === 'list') {")
        end = src.index("let v = el.value;", start)
        body = src[start:end]
        self.assertNotIn("|/", body)
        self.assertIn("[,，、;；]", body)

        self.assertIn("S.secretClears.delete(f.path)", src)
        self.assertIn("delete S.secretSets[k]", src)
        self.assertIn("S.secretClears.delete(k)", src)


class RolePickerContractTests(unittest.TestCase):
    def test_picker_renders_character_info_and_restart_hint(self):
        src = app_js()
        self.assertIn("renderRoleInfo();", src)
        self.assertIn("a.profile_error", src)
        self.assertIn("重启 runtime 后生效", src)
        index = (ROOT / "sidekick" / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn('id="role-info"', index)
        self.assertIn('id="role-list"', index)
        self.assertIn('id="user-list"', index)

    def test_switch_role_syncs_local_config_before_next_save(self):
        """保存角色后本地配置要跟着改，否则下次保存会把 role.id 覆盖回旧角色。"""
        src = app_js()
        self.assertIn(
            "S.config.role = { ...(S.config.role || {}), id: rid }",
            src)


class WebUiTtsContractTests(unittest.TestCase):
    """TTS 页：profile 浏览 + 来源切换 + 保存/删除；没有「设为当前」。"""

    def test_tts_page_has_profile_and_vendor_rows(self):
        index = (ROOT / "sidekick" / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")
        for bid in ("tts-profile", "tts-vendor", "btn-tts-new",
                    "btn-tts-delete", "btn-tts-save", "tts-fields"):
            self.assertIn(f'id="{bid}"', index, bid)
        self.assertNotIn('id="btn-tts-apply"', index)
        self.assertNotIn("设为当前", index)
        self.assertNotIn('id="tts-form"', index)
        self.assertNotIn('id="tts-advanced"', index)
        self.assertNotIn('id="tts-advanced-fields"', index)
        self.assertIn('id="tts-common-form"', index)
        self.assertIn('id="tts-guard-form"', index)

    def test_browsing_does_not_mark_dirty(self):
        src = app_js()
        start = src.index("function browseTtsProfile(")
        end = src.index("function ttsFieldsTouched", start)
        self.assertNotIn("markDirty", src[start:end])

        start = src.index("function renderModel(")
        end = src.index("function selectedLlmProfile", start)
        self.assertNotIn("markDirty", src[start:end])

    def test_app_js_tts_profile_actions_exist(self):
        src = app_js()
        start = src.index("// ── 语音")
        end = src.index("// ── 角色", start)
        body = src[start:end]
        # 删掉的来源开关/「设为当前」不能偷偷回来。
        self.assertNotIn("applyTtsProviderFilter", body)
        self.assertNotIn("设为当前", body)
        self.assertNotIn("advanced", body)
        for fn in ("browseTtsProfile", "switchTtsVendor", "newTts",
                   "deleteTts", "saveTts"):
            self.assertIn(f"function {fn}(", body)
        self.assertIn("function renderTtsSettings(", src)
        start = src.index("function renderTts(")
        end = src.index("function browseTtsProfile", start)
        self.assertNotIn("tts-common-form", src[start:end])

    def test_new_tts_uses_the_name_input(self):
        src = app_js()
        start = src.index("function newTts(")
        end = src.index("async function deleteTts(", start)
        body = src[start:end]
        self.assertIn("$('#tts-profile-name')", body)
        self.assertIn("^[a-z0-9_-]{1,40}$", body)


class WebUiSaveModelContractTests(unittest.TestCase):
    """每页/每模块各自保存；下拉只是浏览，点「保存」才写配置。"""

    def test_each_module_has_its_own_save(self):
        index = (ROOT / "sidekick" / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")
        self.assertNotIn('id="btn-save"', index)
        self.assertNotIn('id="dirty"', index)
        self.assertNotIn("saveAll", app_js())
        src = app_js()
        for call in ("saveBar($('#mic-form')",
                     "saveBar($('#stt-form')", "saveBar($('#pace-forms')",
                     "saveBar($('#live-forms')",
                     "saveBar($('#memory-config')", "saveBar(box, want, '形象层')"):
            self.assertIn(call, src, call)
        # 对话预设是手写的一行（下拉 + 保存/删除/导出/导入），不走 saveBar。
        self.assertIn("$('#btn-preset-save').onclick = savePreset;", src)
        self.assertIn("$('#preset-list').onchange = () => browsePreset(", src)
        self.assertIn("$('#btn-tts-save').onclick = saveTts;", src)
        index = (ROOT / "sidekick" / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")
        for bid in ("btn-role-select", "btn-user-select", "btn-game-select",
                    "btn-profile-save"):
            self.assertIn(f'id="{bid}"', index, bid)

    def test_dropdown_is_browse_and_save_writes_config(self):
        src = app_js()
        self.assertIn("$('#role-list').onchange = () => browseRole(", src)
        self.assertIn("$('#btn-role-select').onclick = saveRoleSelection;", src)
        self.assertIn("$('#btn-user-select').onclick = saveUserAll;", src)
        self.assertIn("$('#btn-game-select').onclick = saveGameSelection;", src)
        # 游戏页的「读取」已删：选中就加载正文
        self.assertNotIn("btn-game-load", src)
        self.assertNotIn("btn-game-load", (ROOT / "sidekick" / "webui" / "static"
                                           / "index.html").read_text(encoding="utf-8"))

    def test_model_page_is_single_view(self):
        index = (ROOT / "sidekick" / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn('id="profile-fields"', index)
        self.assertNotIn('id="profiles"', index)
        src = app_js()
        self.assertIn("function renderProfileFields()", src)
        self.assertIn("function saveModelProfile()", src)
        # 进入页面时回到已生效的 profile
        self.assertIn("const src = String(((S.config || {}).llm || {}).source", src)
        self.assertIn("if (src) sel.value = src;", src)


class WebUiPageLayoutContractTests(unittest.TestCase):
    """页面拆分：对话预设独立页、启用开关集中到概览、模型/游戏页两栏。"""

    def _index(self) -> str:
        return (ROOT / "sidekick" / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")

    def test_preset_page_sits_between_model_and_user(self):
        index = self._index()
        self.assertLess(index.index('data-tab="model"'),
                        index.index('data-tab="prompt"'))
        self.assertLess(index.index('data-tab="prompt"'),
                        index.index('data-tab="user"'))
        self.assertIn('id="tab-prompt"', index)
        self.assertIn('id="preset-list"', index)
        self.assertIn('id="preset-blocks"', index)
        # 预设页按「用户设定」的样子排：预设名 + 新建；当前预设 + 保存/删除/导出/导入。
        self.assertLess(index.index('id="preset-new"'),
                        index.index('id="preset-list"'))
        self.assertLess(index.index('id="preset-list"'),
                        index.index('id="btn-preset-save"'))
        self.assertNotIn('id="prompt-form"', index)
        row_start = index.index('id="preset-list"')
        row_end = index.index('</div>', row_start)
        row = index[row_start:row_end]
        for bid in ("btn-preset-save", "btn-preset-delete",
                    "btn-preset-export", "btn-preset-import"):
            self.assertIn(f'id="{bid}"', row, bid)

        from sidekick.config.spec import DEFAULTS, GROUPS
        self.assertEqual(set(DEFAULTS["prompt"]), {"id"})
        group = next(g for g in GROUPS if g["id"] == "prompt")
        fields = {f for s in group["sections"] for f in s["fields"]}
        self.assertEqual(fields, {"prompt.id"})

    def test_enable_toggles_live_on_overview(self):
        index = self._index()
        self.assertIn('id="toggles-form"', index)
        # 开关在运行状态卡片和日志之间
        self.assertLess(index.index('id="feature-cards"'),
                        index.index('id="toggles-form"'))
        self.assertLess(index.index('id="toggles-form"'), index.index('id="log"'))
        self.assertNotIn("快速说明", index)
        src = app_js()
        self.assertIn("saveBar($('#toggles-form'), groupPaths(['toggles']), '启用控制')",
                      src)

    def test_game_page_has_new_and_current_rows(self):
        index = self._index()
        self.assertLess(index.index('id="game-id-input"'),
                        index.index('id="game-list"'))
        self.assertNotIn("btn-game-load", index)
        # 游戏页不再有启用开关（在概览）
        from sidekick.config.spec import GROUPS
        game = next(g for g in GROUPS if g["id"] == "game")
        self.assertEqual(game["sections"], [])

    def test_model_page_name_row_and_picker_row(self):
        index = self._index()
        for bid in ("profile-name", "btn-add-profile", "model-source",
                    "btn-profile-delete", "btn-profile-test", "btn-profile-save"):
            self.assertIn(f'id="{bid}"', index, bid)
        # 名称+新建 在前，当前 Profile+按钮 一行在后
        self.assertLess(index.index('id="profile-name"'),
                        index.index('id="model-source"'))
        self.assertLess(index.index('id="model-source"'),
                        index.index('id="profile-fields"'))
        self.assertNotIn("Profile 管理", index)
        src = app_js()
        self.assertIn("function modelsRow(", src)
        self.assertIn("function fillModelsSelect(", src)
        self.assertIn("可用模型", src)
        # 超时不再出现在页面
        from sidekick.config.spec import GROUPS
        model = next(g for g in GROUPS if g["id"] == "model")
        fields = {f for s in model["sections"] for f in s["fields"]}
        self.assertNotIn("llm.common.timeout_s", fields)

    def test_role_page_section_order(self):
        index = self._index()
        # 从上到下：形象层 → 当前角色 → 角色设定 → 人设 → 预览 → 动作规则 → 形象设置
        order = ['id="stage-global-form"', 'id="role-list"',
                 'id="role-profile-form"', 'id="role-persona-text"',
                 'id="stage-preview"', 'id="rules-text"', 'id="role-stage-form"']
        positions = [index.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions), order)


class WebUiPresetPageContractTests(unittest.TestCase):
    """对话预设：条目和固定锚点排成一个列表，拖动位置就是 prompt 位置。"""

    def _index(self) -> str:
        return (ROOT / "sidekick" / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")

    def test_entries_are_one_list_with_fixed_anchors(self):
        index = self._index()
        self.assertIn('<h2>条目</h2>', index)
        self.assertIn('id="preset-blocks"', index)
        self.assertIn('id="btn-preset-add">新增<', index)
        # 直播条目不再单独一栏：和普通条目同一个列表，用「仅直播」区分。
        self.assertNotIn('<h2>直播追加</h2>', index)
        self.assertNotIn("preset-live", index)
        self.assertNotIn("新增一块", index)
        self.assertNotIn("<h2>块</h2>", index)
        src = app_js()
        self.assertIn("function presetAnchorEl", src)
        self.assertIn("preset-anchor", src)
        self.assertIn("锚点不能移动", src)
        self.assertIn("function renderPresetList", src)
        self.assertIn("function collectPresetEntries", src)

    def test_each_entry_carries_only_title_role_live_and_text(self):
        src = app_js()
        for field in ('data-field="title"', 'data-field="role"',
                      'data-field="live"', 'data-field="text"'):
            self.assertIn(field, src, field)
        # 插入位置和顺序都不再是表单字段：位置由拖动后落在哪个锚点区间决定。
        for gone in ('data-field="slot"', 'data-field="order"',
                     'order-input', '插入位置', 'function markPresetConflicts',
                     'function slotLabel'):
            self.assertNotIn(gone, src, gone)
        self.assertIn("仅直播", src)
        self.assertIn("live-off", src)

    def test_preview_button_and_panel(self):
        index = self._index()
        self.assertIn('id="btn-prompt-preview"', index)
        self.assertIn('id="prompt-preview-panel" hidden', index)
        self.assertIn('id="prompt-preview" class="log"', index)
        src = app_js()
        self.assertIn("/api/prompt/preview", src)

    def test_pack_buttons_sit_next_to_save(self):
        index = self._index()
        rows = {
            "preset": ('id="preset-list"', ["btn-preset-save",
                                            "btn-preset-delete",
                                            "btn-preset-export",
                                            "btn-preset-import"]),
            "game": ('id="game-list"', ["btn-game-select", "btn-game-delete",
                                        "btn-game-export", "btn-game-import"]),
            "role": ('id="role-list"', ["btn-role-select", "btn-role-export",
                                        "btn-role-import"]),
        }
        for name, (anchor, buttons) in rows.items():
            start = index.index(anchor)
            row = index[start:index.index("</div>", start)]
            for bid in buttons:
                self.assertIn(f'id="{bid}"', row, f"{name}: {bid}")
        # 导入是"先点按钮再选文件"：文件框藏着，由按钮触发。
        for fid in ("preset-import", "game-import", "role-import"):
            self.assertIn(f'id="{fid}" accept=".zip" hidden', index, fid)
        src = app_js()
        self.assertIn("function pickPack(input)", src)
        self.assertIn("input.click();", src)

    def test_user_page_has_no_import_export(self):
        index = self._index()
        self.assertNotIn("btn-user-export", index)
        self.assertNotIn("btn-user-import", index)
        self.assertNotIn("user-import", index)
        src = app_js()
        self.assertNotIn("exportUserPack", src)
        self.assertNotIn("user-import", src)

    def test_buttons_are_short_labels(self):
        index = self._index()
        for text in ("导出角色包", "导入角色包", "导出世界书", "导入世界书",
                     "导出 zip", "导入 zip"):
            self.assertNotIn(text, index, text)
        for text in (">导出<", ">导入<"):
            self.assertIn(text, index, text)


class WebUiModelProfileContractTests(unittest.TestCase):

    def test_original_rows_survive(self):
        index = (ROOT / "sidekick" / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")
        start = index.index('id="tab-model"')
        end = index.index('id="tab-prompt"', start)
        model = index[start:end]
        for bid in ("profile-name", "btn-add-profile", "model-source",
                    "btn-profile-test", "btn-profile-save",
                    "btn-profile-delete", "profile-fields"):
            self.assertIn(f'id="{bid}"', model, bid)
        self.assertLess(model.index('id="profile-name"'),
                        model.index('id="btn-add-profile"'))
        self.assertLess(model.index('id="btn-add-profile"'),
                        model.index('id="model-source"'))
        self.assertLess(model.index('id="model-source"'),
                        model.index('id="profile-fields"'))
        for text in ("服务商", "高级", "折叠"):
            self.assertNotIn(text, model)

    def test_new_profile_uses_backend_template_and_presets(self):
        src = app_js()
        start = src.index("$('#btn-add-profile').onclick")
        end = src.index("$('#modal-close').onclick", start)
        body = src[start:end]
        self.assertIn("S.schema.llm_profile_fields", body)
        self.assertIn("S.schema.llm_presets", body)
        for legacy in ("user_agent", "session_header", "session_id"):
            self.assertNotIn(legacy, body)

        from sidekick.config.spec import PROFILE_FIELDS
        self.assertEqual(
            [f["key"] for f in PROFILE_FIELDS],
            ["base_url", "model", "api_key", "temperature",
             "reasoning_effort", "headers"])
        self.assertEqual(
            [f["label"] for f in PROFILE_FIELDS],
            ["Base URL", "模型", "API Key", "temperature",
             "reasoning_effort", "自定义 Headers（JSON）"])


class WebUiEventStreamContractTests(unittest.TestCase):
    def test_browser_uses_one_event_stream_without_fallback_polling(self):
        src = app_js()
        self.assertIn("new EventSource('/api/events/stream')", src)
        self.assertNotIn("/api/logs/stream", src)
        self.assertNotIn("setInterval(refreshStatus", src)
        self.assertNotIn("S.poll", src)
        self.assertIn("S.logLines", src)
        self.assertIn("addEventListener('status'", src)
        self.assertIn("addEventListener('logs'", src)
        self.assertIn("if (!document.hidden) startEvents();", src)


class WebUiModeContractTests(unittest.TestCase):
    def test_stage_preview_is_lazy(self):
        src = app_js()
        self.assertIn("if (S.tab !== 'role')", src)
        self.assertIn("frame.src = 'about:blank'", src)
        overlay = (ROOT / "overlay" / "app.js").read_text(encoding="utf-8")
        self.assertIn("antialias: true", overlay)
        self.assertIn("resolution: Math.min(2, window.devicePixelRatio || 1)",
                      overlay)
        self.assertIn("app.ticker.maxFPS = 30", overlay)


class StageInspectorContractTests(unittest.TestCase):
    def test_browser_applies_physics_leaf_deltas_after_physics(self):
        overlay = (ROOT / "overlay" / "app.js").read_text(encoding="utf-8")
        self.assertIn("S.poseAdd", overlay)
        self.assertIn("msg.add", overlay)
        self.assertIn("PARAM_INDEX", overlay)
        self.assertIn("paramValue", overlay)
        self.assertIn("physics.evaluate", overlay)
        self.assertIn("prePhysics.push(writeBaseParams)", overlay)
        self.assertIn("writePhysicsAdd", overlay)
        self.assertIn("applyExpressionMixer", overlay)
        self.assertIn("getExpressionParameters", overlay)
        self.assertNotIn("pendingExpressions", overlay)

    def test_preview_inspector_is_always_on(self):
        overlay = (ROOT / "overlay" / "app.js").read_text(encoding="utf-8")
        self.assertIn("document.body.classList.add('preview', 'inspect')",
                      overlay)
        self.assertIn("loadInspector('live2d').then", overlay)
        self.assertNotIn("lmi.toggle()", overlay)
        self.assertNotIn("deepsidekick-inspect", overlay)

        inspect = (ROOT / "overlay" / "inspect.js").read_text(encoding="utf-8")
        self.assertNotIn("ins-close", inspect)
        self.assertNotIn("deepsidekick-inspect-state", inspect)
        self.assertNotIn("toggle:", inspect)
        self.assertIn("deepsidekick-control", inspect)
        self.assertIn("publish('param'", inspect)

        webui = app_js()
        self.assertNotIn("btn-stage-inspect", webui)
        self.assertNotIn("setStageInspectLabel", webui)
        self.assertIn("/api/stage/control", webui)

        index = (ROOT / "sidekick" / "webui" / "static" / "index.html").read_text(
            encoding="utf-8")
        self.assertNotIn('id="btn-stage-inspect"', index)
        self.assertIn("形象预览", index)
        self.assertNotIn("按 I", index)


class SpineOverlayContractTests(unittest.TestCase):
    """浏览器侧 Spine：运行时按版本动态加载，播放器与面板独立成文件。"""
    def test_runtime_is_dynamic_and_versions_covered(self):
        index = (ROOT / "overlay" / "index.html").read_text(encoding="utf-8")
        # 播放器按版本动态加载：index.html 只引样式，不引任何 spine 脚本
        self.assertIn("spine-player.css", index)
        self.assertNotIn("spine-player-", index)
        player = (ROOT / "overlay" / "spine-player.js").read_text(
            encoding="utf-8")
        for ver in ("3.8", "4.0", "4.1", "4.2"):
            self.assertIn(f"'{ver}':", player)
            lib = ROOT / "overlay" / "lib" / f"spine-player-{ver}.js"
            self.assertTrue(lib.is_file(), f"缺播放器：{lib.name}")

    def test_error_layer_escapes_model_text(self):
        """错误层会拼接模型动画名；必须转义，不能把名字当 HTML。"""
        for ver in ("3.8", "4.0", "4.1", "4.2"):
            lib = (ROOT / "overlay" / "lib" / f"spine-player-{ver}.js").read_text(
                encoding="utf-8")
            self.assertIn("function escapeHtml(", lib, ver)
            self.assertIn("escapeHtml(", lib, ver)
            self.assertNotIn(
                "spine-player-error\" style=\"background:#000;color:#fff;"
                "position:absolute;top:0;width:100%;height:100%;display:flex;"
                "justify-content:center;align-items:center;overflow:auto;"
                "z-index:999\">` + message + `</div>",
                lib, ver)

    def test_app_wires_spine_player_and_inspector(self):
        src = (ROOT / "overlay" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function initSpine(", src)
        self.assertIn("spine-player.js", src)
        self.assertIn("loadInspector('spine')", src)
        self.assertIn("window.SP.play(", src)
        self.assertIn("S.renderer === 'spine'", src)
        inspector = (ROOT / "overlay" / "spine-inspector.js").read_text(
            encoding="utf-8")
        self.assertIn("deepsidekick-control", inspector)
        self.assertIn("'motion'", inspector)
        self.assertIn("'view'", inspector)
        css = (ROOT / "overlay" / "style.css").read_text(encoding="utf-8")
        self.assertIn("body.r-spine #spine-stage", css)


class OverlayRecoveryContractTests(unittest.TestCase):
    """舞台页在 runtime 晚启动/autonomy 变化后必须重新初始化。"""

    def test_watch_recovers_from_empty_config_and_autonomy_change(self):
        src = (ROOT / "overlay" / "app.js").read_text(encoding="utf-8")
        self.assertIn("if (now && now !== last) location.reload();", src)
        self.assertIn("c.autonomy && c.autonomy.enabled", src)
        self.assertIn("c.autonomy && c.autonomy.active", src)
        self.assertIn("const suffix = want ? '&avatar='", src)


class OverlaySyncContractTests(unittest.TestCase):
    """静态回归：say 的 sync 必须真正消费，自动翻页只能在非 sync 时启动。"""

    def test_speak_reads_sync_and_disables_cps_timer(self):
        src = (ROOT / "overlay" / "app.js").read_text(encoding="utf-8")
        self.assertIn("S.sync = !!msg.sync", src)
        start = src.index("function showPage(i)")
        end = src.index("function speak(msg)", start)
        body = src[start:end]
        self.assertIn("clearTimeout(S.pageTimer)", body)
        self.assertRegex(
            body,
            r"(?s)if\s*\(!S\.sync.*?\{.*?setTimeout\(.*?showPage")


class PageAndRoleGuardContractTests(unittest.TestCase):
    """静态回归：page 不回退、角色异步读取不串台、TTS 浏览重置 schema。"""

    def test_overlay_page_event_is_monotonic(self):
        src = overlay_js()
        start = src.index("case 'page':")
        end = src.index("case 'level':", start)
        body = src[start:end]
        self.assertIn("const nextIdx = Number(msg.idx) || 0", body)
        self.assertIn("if (nextIdx > S.idx)", body)
        self.assertIn("showPage(nextIdx)", body)

    def test_role_persona_guards_against_stale_load(self):
        src = app_js()
        start = src.index("async function loadRolePersona()")
        end = src.index("async function saveRolePersona", start)
        body = src[start:end]
        self.assertIn("const targetId = S.selectedAvatar", body)
        self.assertIn("S.selectedAvatar !== targetId", body)

    def test_avatar_rules_keeps_id_and_guards_against_stale_load(self):
        src = app_js()
        start = src.index("async function loadAvatarRules(id)")
        end = src.index("async function saveAvatarRules", start)
        body = src[start:end]
        self.assertIn("const targetId = id", body)
        self.assertIn("S.selectedAvatar !== targetId", body)

    def test_new_preset_and_user_check_dirty_first(self):
        src = app_js()
        preset = src[src.index("async function createPreset()"):
                     src.index("async function deletePreset",
                               src.index("async function createPreset()"))]
        self.assertIn("DIRTY.preset", preset)
        user = src[src.index("async function createUser()"):
                   src.index("async function deleteUser",
                             src.index("async function createUser()"))]
        self.assertIn("DIRTY.user", user)

    def test_add_profile_checks_dirty_first(self):
        src = app_js()
        start = src.index("$('#btn-add-profile').onclick")
        body = src[start:src.index("const name =", start)]
        self.assertIn("DIRTY.config", body)

    def test_role_browse_does_not_confirm_config_twice(self):
        src = app_js()
        start = src.index("async function browseRole(id)")
        end = src.index("async function saveRoleSelection", start)
        body = src[start:end]
        self.assertEqual(0, body.count("DIRTY.config"))
        self.assertIn("rolePageDirty()", body)

    def test_overlay_handover_reads_real_core_model_value(self):
        src = overlay_js()
        self.assertIn("S.prevPoseKeys", src)
        self.assertIn("S.handover", src)
        self.assertIn("paramValue", src)

    def test_reset_and_reload_are_shared_controls(self):
        src = overlay_js()
        self.assertIn("S.expressions.clear();", src)
        self.assertIn("stopAllMotions();", src)
        self.assertIn("S.initialView", src)
        self.assertIn("resetView()", src)
        self.assertIn("if (msg.op === 'reload')", src)
        inspector = (ROOT / "overlay" / "inspect.js").read_text(
            encoding="utf-8")
        self.assertIn("publish('reload', {})", inspector)
        self.assertIn("window.LMControl.reset()", inspector)

    def test_overlay_disables_sdk_blink_and_breath_under_autonomy(self):
        src = overlay_js()
        self.assertIn("im.eyeBlink.updateParameters = () => {}", src)
        self.assertIn("im.breath.updateParameters = () => {}", src)
        self.assertIn("mm.stopAllMotions();", src)
        self.assertIn(
            "if (S.autonomy && S.renderer === 'live2d') return;", src)

    def test_role_switch_ignores_stale_response(self):
        src = app_js()
        self.assertIn("S.roleSeq", src)
        self.assertIn("seq !== S.roleSeq", src)

    def test_browse_tts_profile_is_async_and_refreshes_schema(self):
        src = app_js()
        start = src.index("async function browseTtsProfile(name)")
        end = src.index("function ttsFieldsTouched", start)
        body = src[start:end]
        self.assertIn("await refreshSchema()", body)


if __name__ == "__main__":
    unittest.main()
