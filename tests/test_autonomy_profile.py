from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sidekick.avatar.autonomy import fallback, store, validate
from sidekick.avatar.autonomy.schema import SCHEMA_VERSION
from sidekick.motion import introspect
from sidekick.motion.actions import ActionPlayer
from sidekick.motion.profile import apply_overrides


class FakeSource:
    data = {
        "ParamAngleX": (-30, 30, 0), "ParamAngleY": (-30, 30, 0),
        "ParamAngleZ": (-30, 30, 0),
        "ParamEyeLOpen": (0, 1, 1), "ParamEyeROpen": (0, 1, 1),
        "ParamMouthOpenY": (0, 1, 0), "ParamMouthForm": (-1, 1, 0),
        "ParamBodyAngleX": (-10, 10, 0), "ParamBodyAngleZ": (-10, 10, 0),
        "ParamCustomX": (-5, 5, 0), "ParamCustomY": (-5, 5, 0),
    }

    def param_ids(self):
        return list(self.data)

    def param_info(self, param_id):
        return self.data[param_id]


DOSSET = {
    "params": [{"id": pid} for pid in FakeSource.data],
    "by_channel": {"head_x": "ParamAngleX", "head_y": "ParamAngleY",
                   "head_z": "ParamAngleZ", "eye_l_open": "ParamEyeLOpen",
                   "eye_r_open": "ParamEyeROpen",
                   "mouth_open": "ParamMouthOpenY",
                   "mouth_form": "ParamMouthForm",
                   "body_x": "ParamBodyAngleX",
                   "body_z": "ParamBodyAngleZ"},
    "motions": [{"group": "idle"}, {"group": "wave"}],
    "expressions": [{"name": "开心"}, {"name": "难过"}],
    "proportions": {"archetype_guess": "chibi"},
}


def profile_dict() -> dict:
    data = fallback.fallback_profile(DOSSET)
    return data


class ProfileStoreTests(unittest.TestCase):
    def test_roundtrip_and_delete(self):
        with tempfile.TemporaryDirectory() as td:
            avatar = SimpleNamespace(id="x", role_dir=Path(td))
            self.assertEqual(store.load(avatar), (None, None))
            store.save(avatar, {"schema_version": 1})
            data, error = store.load(avatar)
            self.assertIsNone(error)
            self.assertEqual(data["schema_version"], 1)
            store.delete(avatar)
            self.assertEqual(store.load(avatar), (None, None))

    def test_bad_json_and_large_file(self):
        with tempfile.TemporaryDirectory() as td:
            avatar = SimpleNamespace(id="x", role_dir=Path(td))
            path = Path(td) / "motion_profile.json"
            path.write_text("{", encoding="utf-8")
            data, error = store.load(avatar)
            self.assertIsNone(data)
            self.assertIn("JSON", error)
            path.write_text("{}" + " " * (300 * 1024), encoding="utf-8")
            data, error = store.load(avatar)
            self.assertIsNone(data)
            self.assertIn("超过", error)


class ValidateTests(unittest.TestCase):
    def test_fallback_is_valid(self):
        clean, issues = validate.validate(profile_dict(), DOSSET)
        self.assertEqual([x.text() for x in issues], [])
        self.assertEqual(clean["schema_version"], SCHEMA_VERSION)
        self.assertEqual(clean["archetype"], "chibi")
        self.assertIn("actions", clean)
        self.assertTrue(clean["motion_policy"]["model_motions"]["wave"]["enabled"])

    def test_legacy_body_gain_resets_to_global_scale(self):
        draft = profile_dict()
        draft["tuning"]["body_gain"] = 0.13
        clean, issues = validate.validate(draft, DOSSET)
        self.assertEqual(clean["tuning"]["body_gain"], 1.0)
        self.assertTrue(any("body_gain" in x.text() for x in issues))

    def test_unknown_ids_and_bad_prompt_are_reported(self):
        draft = profile_dict()
        draft["channel_bindings"] = {"head_x": "ParamNope"}
        draft["aliases"] = {"ParamNope": "head_y"}
        draft["actions"]["dance"] = {"enabled": True}
        draft["motion_policy"]["model_motions"]["dance"] = {"enabled": True}
        draft["expressions"]["happy"] = "不存在"
        draft["prompt"]["rules"] = "see https://example.com ```"
        draft["tuning"]["head_gain"] = 99
        clean, issues = validate.validate(draft, DOSSET)
        text = " ".join(x.text() for x in issues)
        self.assertIn("ParamNope", text)
        self.assertIn("dance", text)
        self.assertIn("不存在", text)
        self.assertIn("URL", text)
        self.assertEqual(clean["tuning"]["head_gain"], 1.45)
        self.assertNotIn("head_x", clean["channel_bindings"])
        self.assertNotIn("dance", clean["actions"])

    def test_action_timing_keys_are_kept(self):
        draft = profile_dict()
        draft["actions"]["wave"] = {
            "attack": 0.2, "hold": 0.3, "release": 0.4,
            "priority": 0.6, "cooldown": 1.2, "degrade": {"arm_l": 0.5},
        }
        clean, issues = validate.validate(draft, DOSSET)
        got = clean["actions"]["wave"]
        self.assertAlmostEqual(0.2, got["attack"])
        self.assertAlmostEqual(0.3, got["hold"])
        self.assertAlmostEqual(0.4, got["release"])
        self.assertAlmostEqual(0.6, got["priority"])
        self.assertAlmostEqual(1.2, got["cooldown"])
        self.assertFalse(any("wave" in x.text() for x in issues))

    def test_action_aliases_are_canonicalised(self):
        draft = profile_dict()
        draft["actions"] = {"挥手": {"enabled": False}}
        clean, issues = validate.validate(draft, DOSSET)
        self.assertFalse(clean["actions"]["wave"]["enabled"])
        self.assertFalse(any(x.fatal for x in issues))

    def test_schema_version_and_required_fields(self):
        draft = {"schema_version": 99}
        _clean, issues = validate.validate(draft, DOSSET)
        text = " ".join(x.text() for x in issues)
        self.assertIn("schema_version", text)
        for key in ("tuning", "actions", "motion_policy", "prompt"):
            self.assertIn(key, text)

    def test_empty_prompt_gets_generic_vocabulary(self):
        draft = profile_dict()
        draft["prompt"] = {}
        clean, issues = validate.validate(draft, DOSSET)
        self.assertTrue(clean["prompt"]["emotion_tags"])
        self.assertTrue(clean["prompt"]["action_tags"])
        self.assertFalse(any(x.fatal for x in issues))

    def test_rules_only_prompt_also_gets_vocabulary(self):
        draft = profile_dict()
        draft["prompt"] = {"emotion_tags": [], "action_tags": [],
                           "gaze_tags": [], "rules": "只看语气"}
        clean, issues = validate.validate(draft, DOSSET)
        self.assertTrue(clean["prompt"]["emotion_tags"])
        self.assertTrue(clean["prompt"]["action_tags"])
        self.assertTrue(clean["prompt"]["gaze_tags"])
        self.assertFalse(any(x.fatal for x in issues))


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.profile = introspect(FakeSource())
        self.data = {
            "schema_version": 1,
            "generated_by": {"provider": "x", "model": "m", "at": "",
                             "fallback": False},
            "archetype": "chibi",
            "tuning": {"head_gain": 1.1},
            "channel_bindings": {"head_x": "ParamCustomX"},
            "aliases": {"ParamCustomY": "arm_r"},
            "actions": {"wave": {"enabled": False},
                        "nod": {"duration_scale": 1.5, "intensity": 0.5}},
            "motion_policy": {"mode": "auto"},
            "expressions": {"happy": "开心"},
            "prompt": {"emotion_tags": ["happy"]},
        }

    def test_overrides_are_applied(self):
        warnings = apply_overrides(self.profile, self.data)
        self.assertEqual(warnings, [])
        self.assertEqual(self.profile.by_channel["head_x"].id, "ParamCustomX")
        self.assertEqual(self.profile.by_channel["arm_r"].id, "ParamCustomY")
        self.assertEqual(self.profile.source, "profile")
        self.assertEqual(self.profile.archetype, "chibi")
        self.assertEqual(self.profile.actions["wave"]["enabled"], False)

    def test_action_settings_control_player(self):
        apply_overrides(self.profile, self.data)
        player = ActionPlayer()
        self.assertFalse(player.play("wave", self.profile, now=0.0))
        self.assertTrue(player.play("nod", self.profile, now=0.0))
        self.assertGreater(player._current.spec.duration, 1.0)  # noqa: SLF001
        self.assertAlmostEqual(player._current.intensity, 0.5)  # noqa: SLF001

    def test_unknown_binding_is_warned_not_added(self):
        warnings = apply_overrides(
            self.profile, {**self.data, "channel_bindings": {
                "head_x": "Nope"}})
        self.assertTrue(any("Nope" in x for x in warnings))


if __name__ == "__main__":
    unittest.main()
