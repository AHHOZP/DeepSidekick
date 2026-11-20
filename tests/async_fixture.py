from __future__ import annotations

import asyncio
import unittest


class QuietIsolatedAsyncioTestCase(unittest.IsolatedAsyncioTestCase):
    """测试用异步基类：关闭 event loop debug，避免慢回调日志淹没结果。

    unittest 默认 `Runner(debug=True)`，任何超过 100 ms 的测试/HTTP 请求都会
    打印 "Executing <Task ...> took ..."。这不是产品告警，只是测试运行时诊断。
    """

    def _setupAsyncioRunner(self) -> None:
        self._asyncioRunner = asyncio.Runner(debug=False)
