"""Windows 作业对象：作业一关，成员进程跟着死（Ctrl+C / 关窗口的兜底）。"""
from __future__ import annotations

import subprocess
import sys
import unittest

from sidekick.core.winjob import KillOnClose

_SLEEPER = "import time; time.sleep(30)"


class JobTests(unittest.TestCase):
    def test_member_dies_when_job_closes(self):
        if sys.platform != "win32":
            self.skipTest("作业对象只在 Windows 上有")
        job = KillOnClose()
        proc = subprocess.Popen([sys.executable, "-c", _SLEEPER])
        try:
            self.assertTrue(job.assign(proc.pid),
                            "进程收不进作业：关窗口就带不走子进程")
            job.close()
            self.assertIsNotNone(proc.wait(timeout=15))
        finally:
            job.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=15)

    def test_close_is_idempotent(self):
        job = KillOnClose()
        job.close()
        job.close()
        self.assertFalse(job.active)
        self.assertFalse(job.assign(0))


if __name__ == "__main__":
    unittest.main()
