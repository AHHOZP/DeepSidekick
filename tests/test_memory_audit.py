from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import unittest

from sidekick.brain.llm import LLMError
from sidekick.memory.audit import Audit


class FakeLLM:
    def __init__(self, reply, *, finish="stop", error=None):
        self.reply = reply
        self.finish = finish
        self.error = error

    async def chat(self, messages, *, temperature=None, attempts=1):
        if self.error:
            raise self.error
        return {"choices": [{"message": {"content": self.reply},
                             "finish_reason": self.finish}]}


MSG = [{"role": "system", "content": "规则"},
       {"role": "user", "content": "材料"}]


class AuditTests(QuietIsolatedAsyncioTestCase):
    async def test_pass(self):
        a = Audit(FakeLLM("PASS"), log=lambda *x: None)
        r = await a.review("summary", MSG, '{"segments":[]}')
        self.assertEqual(r.status, "pass")

    async def test_summary_fix(self):
        fixed = '{"segments":[{"content":"修正","keywords":[]}]}'
        a = Audit(FakeLLM(fixed), log=lambda *x: None)
        r = await a.review("summary", MSG, "坏")
        self.assertEqual(r.status, "fixed")
        self.assertEqual(r.text, fixed)

    async def test_keywords_fix(self):
        a = Audit(FakeLLM('["铁匠", "钥匙"]'), log=lambda *x: None)
        r = await a.review("keywords", MSG, '["铁匠","游戏","钥匙"]')
        self.assertEqual(r.status, "fixed")
        self.assertIn("钥匙", r.text)

    async def test_bad_keyword_json_fails(self):
        a = Audit(FakeLLM("不是数组"), log=lambda *x: None)
        r = await a.review("keywords", MSG, "[]")
        self.assertEqual(r.status, "failed")
        self.assertTrue(r.error)

    async def test_llm_error_fails(self):
        a = Audit(FakeLLM("", error=LLMError("boom")), log=lambda *x: None)
        r = await a.review("summary", MSG, "坏")
        self.assertEqual(r.status, "failed")

    async def test_truncated_audit_fails(self):
        a = Audit(FakeLLM("PASS", finish="length"), log=lambda *x: None)
        r = await a.review("summary", MSG, "坏")
        self.assertEqual(r.status, "failed")


if __name__ == "__main__":
    unittest.main()
