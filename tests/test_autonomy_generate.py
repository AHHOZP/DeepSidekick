from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from sidekick.avatar.autonomy import fallback
from sidekick.avatar.autonomy import generate as generate_mod
from sidekick.avatar.autonomy.generate import GenerationError, generate_profile
from sidekick.avatar.autonomy.prompts import (
    compact_dossier, extract_json, stage1_messages,
)


DOSSET = {
    "model": {"name": "test", "entry": "test.model3.json", "canvas": [1.0, 1.5]},
    "params": [{"id": "ParamAngleX"}, {"id": "ParamEyeLOpen"},
               {"id": "ParamEyeROpen"}, {"id": "ParamMouthOpenY"},
               {"id": "ParamBodyAngleZ"}],
    "by_channel": {"head_x": "ParamAngleX", "eye_l_open": "ParamEyeLOpen",
                   "eye_r_open": "ParamEyeROpen",
                   "mouth_open": "ParamMouthOpenY",
                   "body_z": "ParamBodyAngleZ"},
    "motions": [{"group": "idle"}, {"group": "wave"}],
    "expressions": [{"name": "开心"}],
    "proportions": {"archetype_guess": "unknown"},
    "warnings": [],
}

AVATAR = SimpleNamespace(kind="live2d", root=".", entry="test.model3.json")


def valid_profile() -> dict:
    return fallback.fallback_profile(DOSSET)


class FakeLLM:
    ready = True
    source = "fake"
    model = "fake-model"

    def __init__(self, payloads, finish="stop"):
        self.payloads = list(payloads)
        self.finish = finish
        self.calls = 0
        self.closed = False
        self.last_kwargs = {}

    async def chat(self, messages, **kwargs):
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        text = self.payloads.pop(0) if self.payloads else "{}"
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        return {"choices": [{"message": {"content": text},
                             "finish_reason": self.finish}]}

    async def close(self):
        self.closed = True


class StreamLLM(FakeLLM):
    async def stream_chat(self, messages, **kwargs):
        self.last_kwargs = dict(kwargs)
        text = json.dumps(self.payloads.pop(0), ensure_ascii=False)
        yield {"type": "open"}
        yield {"type": "delta", "text": text[:20]}
        yield {"type": "delta", "text": text[20:]}


def audit(_root, _path):
    return DOSSET


class FlakyStreamLLM(StreamLLM):
    async def stream_chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise generate_mod.LLMError(
                "流式调用失败：ClientPayloadError: payload is not completed")
        async for event in super().stream_chat(messages, **kwargs):
            yield event


class GenerateTests(QuietIsolatedAsyncioTestCase):
    def setUp(self):
        self._waits = (generate_mod.GENERATION_WAIT_S,
                       generate_mod.GENERATION_REPAIR_WAIT_S,
                       generate_mod.GENERATION_STREAM_RETRY_WAIT_S)
        generate_mod.GENERATION_WAIT_S = 0.0
        generate_mod.GENERATION_REPAIR_WAIT_S = 0.0
        generate_mod.GENERATION_STREAM_RETRY_WAIT_S = 0.0

    def tearDown(self):
        (generate_mod.GENERATION_WAIT_S,
         generate_mod.GENERATION_REPAIR_WAIT_S,
         generate_mod.GENERATION_STREAM_RETRY_WAIT_S) = self._waits

    async def test_valid_generation(self):
        llm = FakeLLM([valid_profile()])
        result = await generate_profile(AVATAR, {}, llm=llm, audit_runner=audit)
        self.assertFalse(result["fallback"])
        self.assertEqual(result["profile"]["schema_version"], 1)
        self.assertEqual(result["issues"], [])
        self.assertEqual(llm.calls, 1)
        self.assertGreaterEqual(llm.last_kwargs.get("timeout"), 120.0)
        self.assertEqual(llm.last_kwargs.get("temperature"), 1.0)
        self.assertEqual(llm.last_kwargs.get("reasoning_effort"), "max")

    async def test_streaming_reports_stages_and_deltas(self):
        llm = StreamLLM([valid_profile()])
        events = []

        async def progress(event):
            events.append(event)

        result = await generate_profile(
            AVATAR, {}, llm=llm, audit_runner=audit, progress=progress)
        self.assertFalse(result["fallback"])
        stages = [e.get("stage") for e in events if e.get("type") == "status"]
        self.assertIn("sending", stages)
        self.assertIn("thinking", stages)
        self.assertIn("outputting", stages)
        self.assertTrue(any(e.get("type") == "delta" for e in events))
        self.assertEqual(llm.last_kwargs.get("temperature"), 1.0)
        self.assertEqual(llm.last_kwargs.get("reasoning_effort"), "max")
        self.assertEqual(llm.last_kwargs.get("first_token_timeout"), 30.0)

    async def test_stream_retries_once_before_the_first_delta(self):
        async def progress(_event):
            return None

        llm = FlakyStreamLLM([valid_profile()])
        result = await generate_profile(
            AVATAR, {}, llm=llm, audit_runner=audit, progress=progress)
        self.assertFalse(result["fallback"])
        self.assertEqual(llm.calls, 2)

    async def test_stream_does_not_retry_after_a_delta(self):
        class BrokenAfterDelta(StreamLLM):
            async def stream_chat(self, messages, **kwargs):
                self.calls += 1
                yield {"type": "open"}
                yield {"type": "delta", "text": '{"schema_version": 1'}
                raise generate_mod.LLMError("断流")

        async def progress(_event):
            return None

        llm = BrokenAfterDelta([valid_profile()])
        result = await generate_profile(
            AVATAR, {}, llm=llm, audit_runner=audit, progress=progress)
        self.assertTrue(result["fallback"])
        self.assertEqual(llm.calls, 1)

    async def test_optional_bad_fields_are_dropped_without_fallback(self):
        draft = valid_profile()
        draft["expressions"] = {"happy": "不存在的表情"}
        llm = FakeLLM([draft])
        result = await generate_profile(AVATAR, {}, llm=llm, audit_runner=audit)
        self.assertFalse(result["fallback"])
        # LLM 的坏映射被丢弃，但程序 baseline 仍然保留 dossier 里真实的“开心”。
        self.assertEqual(result["profile"]["expressions"].get("happy"), "开心")
        self.assertTrue(result["issues"])

    async def test_project_root_is_forwarded_to_audit(self):
        seen = {}

        def audit_with_root(root, model):
            seen["root"] = root
            seen["model"] = model
            return DOSSET

        llm = FakeLLM([valid_profile()])
        await generate_profile(
            AVATAR, {}, llm=llm, audit_runner=audit_with_root,
            project_root="/tmp/project")
        self.assertEqual(seen["root"], Path("/tmp/project"))
        self.assertTrue(str(seen["model"]).endswith("test.model3.json"))

    async def test_repair_loop(self):
        bad = valid_profile()
        bad["schema_version"] = 99
        good = valid_profile()
        llm = FakeLLM([bad, good])
        result = await generate_profile(AVATAR, {}, llm=llm, audit_runner=audit)
        self.assertFalse(result["fallback"])
        self.assertEqual(llm.calls, 2)
        self.assertIn("ParamAngleX", result["profile"]["channel_bindings"].values())

    async def test_bad_json_falls_back(self):
        llm = FakeLLM(["not json"])
        result = await generate_profile(AVATAR, {}, llm=llm, audit_runner=audit)
        self.assertTrue(result["fallback"])
        self.assertTrue(result["profile"]["generated_by"]["fallback"])
        self.assertEqual(result["profile"]["schema_version"], 1)

    async def test_hallucinations_fall_back_after_repairs(self):
        bad = valid_profile()
        bad["schema_version"] = 99
        llm = FakeLLM([bad, bad, bad])
        result = await generate_profile(AVATAR, {}, llm=llm, audit_runner=audit)
        self.assertTrue(result["fallback"])
        self.assertEqual(llm.calls, 3)
        self.assertTrue(result["issues"])

    async def test_not_ready_uses_fallback(self):
        llm = FakeLLM([])
        llm.ready = False
        result = await generate_profile(AVATAR, {}, llm=llm, audit_runner=audit)
        self.assertTrue(result["fallback"])
        self.assertTrue(result["profile"]["prompt"]["emotion_tags"])

    async def test_truncated_output_falls_back(self):
        llm = FakeLLM([valid_profile()], finish="length")
        result = await generate_profile(AVATAR, {}, llm=llm, audit_runner=audit)
        self.assertTrue(result["fallback"])
        self.assertIn("截断", " ".join(result["issues"]))

    async def test_non_live2d_is_rejected(self):
        with self.assertRaises(GenerationError):
            await generate_profile(
                SimpleNamespace(kind="spine", root=".", entry="x.skel"), {},
                audit_runner=audit)

    def test_extract_json_handles_fences_and_prose(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('结果如下：{"a": 2} 完'), {"a": 2})
        self.assertIsNone(extract_json("not json"))

    def test_compact_dossier_drops_bulky_fields(self):
        dossier = {**DOSSET, "parts": ["x"] * 40,
                   "screen_side": {"ParamAngleX": "center"},
                   "probe": {"available": True, "params": {"ParamAngleX": {}}},
                   "motions": [{"group": "idle", "params": ["P"] * 40}]}
        compact = compact_dossier(dossier)
        self.assertNotIn("parts", compact)
        self.assertEqual(compact["probe"]["count"], 1)
        self.assertNotIn("params", compact["probe"])
        self.assertNotIn("screen_side", compact)
        self.assertNotIn("params", compact["motions"][0])

    def test_stage1_prompt_carries_exact_allowed_names(self):
        messages = stage1_messages(DOSSET)
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["ALLOWED"]["expressions"], ["开心"])
        self.assertIn("happy", payload["ALLOWED"]["emotions"])
        self.assertIn("ParamAngleX", payload["ALLOWED"]["param_ids"])

    def test_prompt_action_tags_keep_the_full_catalog(self):
        from sidekick.avatar.autonomy.validate import validate

        clean, _ = validate(fallback.fallback_profile(DOSSET), DOSSET)
        for name in ("bounce", "gesture", "sob", "long_skirt", "normal_body"):
            self.assertIn(name, clean["prompt"]["action_tags"])

    def test_alias_to_an_unmapped_but_real_parameter_is_kept(self):
        from sidekick.avatar.autonomy.validate import validate

        dossier = {
            **DOSSET,
            "unmapped": {"count": 1, "sample": ["Param99"],
                         "rows": [{"id": "Param99", "name": "眼眯",
                                   "min": -1, "max": 1, "default": 0}]},
        }
        draft = fallback.fallback_profile(dossier)
        draft["aliases"] = {"Param99": "eye_squint"}
        clean, issues = validate(draft, dossier)
        self.assertEqual(clean["aliases"], {"Param99": "eye_squint"})
        self.assertFalse([x for x in issues if "Param99" in x.message])

    def test_stage1_prompt_keeps_leg_actions_for_leg_models(self):
        messages = stage1_messages(DOSSET)
        payload = json.loads(messages[1]["content"])
        self.assertIn("leg_shift", payload["ACTION_CATALOG"]["actions"])
        self.assertIn("leg_shift", payload["ALLOWED"]["actions"])
        self.assertIn("kick", payload["ACTION_CATALOG"]["actions"])
        self.assertIn("leg_ / foot_", messages[0]["content"])

    async def test_baseline_restores_action_tags_omitted_by_the_llm(self):
        draft = valid_profile()
        draft["prompt"]["action_tags"] = ["wave", "nod"]
        llm = FakeLLM([draft])
        result = await generate_profile(AVATAR, {}, llm=llm, audit_runner=audit)
        self.assertFalse(result["fallback"])
        tags = result["profile"]["prompt"]["action_tags"]
        for name in ("wave", "nod", "long_skirt", "short_skirt",
                     "normal_body"):
            self.assertIn(name, tags)

    async def test_baseline_keeps_the_idle_motion_disabled(self):
        draft = valid_profile()
        draft["motion_policy"]["model_motions"]["idle"]["enabled"] = True
        llm = FakeLLM([draft])
        result = await generate_profile(AVATAR, {}, llm=llm, audit_runner=audit)
        self.assertFalse(result["fallback"])
        self.assertFalse(
            result["profile"]["motion_policy"]["model_motions"]["idle"]["enabled"])
        self.assertIn("待机 motion", " ".join(result["profile"]["warnings"]))

    async def test_baseline_restores_a_capability_the_llm_dropped(self):
        dossier = {
            **DOSSET,
            "by_channel": {**DOSSET["by_channel"], "leg_r": "LegR1Z",
                           "hip": "HipZ"},
            "params": [*DOSSET["params"], {"id": "LegR1Z"}, {"id": "HipZ"}],
        }
        draft = fallback.fallback_profile(dossier)
        draft["actions"]["kick"] = {"enabled": False}
        draft["actions"].pop("leg_shift", None)
        llm = FakeLLM([draft])
        result = await generate_profile(
            AVATAR, {}, llm=llm,
            audit_runner=lambda _root, _path: dossier)
        self.assertFalse(result["fallback"])
        self.assertTrue(result["profile"]["actions"]["kick"]["enabled"])
        self.assertTrue(result["profile"]["actions"]["leg_shift"]["enabled"])
        self.assertEqual(result["profile"]["channel_bindings"]["leg_r"], "LegR1Z")

    def test_state_without_builtin_action_gets_state_actions(self):
        dossier = {
            **DOSSET,
            "states": {
                "outfit_chest": {
                    "param_id": "Clothes3", "mode": "persistent",
                    "aliases": ["换衣服", "外套"],
                },
            },
        }
        profile = fallback.fallback_profile(dossier)
        on = profile["actions"]["state_outfit_chest_on"]
        off = profile["actions"]["state_outfit_chest_off"]
        self.assertEqual(on["states"], {"outfit_chest": True})
        self.assertEqual(off["states"], {"outfit_chest": False})
        self.assertIn("关闭换衣服", off["aliases"])

    def test_persistent_expression_generates_on_and_off_actions(self):
        dossier = {
            **DOSSET,
            "expressions": [{
                "name": "裙子切换", "kind": "persistent",
                "controls": [{"id": "Clothes1", "value": 0.0,
                              "default": 1.0}],
            }],
        }
        profile = fallback.fallback_profile(dossier)
        on = [row for name, row in profile["actions"].items()
              if "裙子切换" in (row.get("aliases") or [])]
        off = [row for name, row in profile["actions"].items()
               if any(alias.startswith("关闭") for alias in row.get("aliases") or [])]
        self.assertEqual(len(on), 1)
        self.assertEqual(len(off), 1)
        self.assertEqual(on[0]["persistent_raw"], {"Clothes1": 0.0})
        self.assertEqual(off[0]["persistent_raw"], {"Clothes1": 1.0})
        leftover = [x for x in (profile.get("expressions") or {}).values()
                    if x == "裙子切换"]
        self.assertEqual(leftover, [])


if __name__ == "__main__":
    unittest.main()
