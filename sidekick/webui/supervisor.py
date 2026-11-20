"""WebUI 的运行时进程管理。

WebUI 是父进程，`python -m sidekick` 是子进程：
  · 停止/重启直播不会把 WebUI 一起关掉；
  · 普通日志由 runtime 写 state/run.log；子进程 stdout 丢弃，stderr 写
    state/runtime.console.log，只接崩溃类输出；
  · 用 pid 文件 + psutil 兜底，WebUI 崩了也能在下次启动时看到残留进程。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import psutil

from sidekick.core import atomic
from sidekick.core.log import log
from sidekick.core.winjob import KillOnClose
from sidekick.diagnostics.server import DEBUG_PORT


def _tail_lines(path: Path, n: int, chunk: int = 256 * 1024) -> list[str]:
    """只读文件末尾 chunk 字节，取最后 n 行。文件不存在返回空列表。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - chunk)
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    # 从中间切开时第一行可能是半截，直接丢掉
    if start > 0 and lines:
        lines = lines[1:]
    return lines[-n:]


CREATE_NEW_PROCESS_GROUP = 0x00000200 if sys.platform == "win32" else 0


class Supervisor:
    def __init__(self, root: Path, *, debug_port: int = DEBUG_PORT) -> None:
        self.root = root
        self.debug_port = int(debug_port)
        self.state = root / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.pid_file = self.state / "webui-runtime.pid"
        self.ready_file = self.state / "runtime.ready"
        self.console_log = self.state / "runtime.console.log"
        self.proc: asyncio.subprocess.Process | None = None
        self.started_at: float = 0.0
        self._log_fp = None
        self._lock = asyncio.Lock()
        self._http: aiohttp.ClientSession | None = None
        self._watch_task: asyncio.Task | None = None
        self._generation = 0
        self.on_exit = None
        # 关窗口/结束 WebUI 时由内核带走整棵子进程树（见 sidekick/core/winjob.py）
        self._job: KillOnClose | None = None
        # tail() 的缓存：(文件指纹, n) → 行
        self._tail_key: tuple | None = None
        self._tail_lines: list[str] = []

    # ── 状态 ──────────────────────────────────────────
    def _pid_record(self) -> dict | None:
        try:
            data = json.loads(self.pid_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _pid(self) -> int | None:
        data = self._pid_record()
        if data is None:
            return None
        try:
            return int(data.get("pid") or 0) or None
        except (TypeError, ValueError):
            return None

    def _pid_alive(self, pid: int | None) -> bool:
        if not pid:
            return False
        try:
            p = psutil.Process(pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False

    def _pid_owned(self, pid: int | None) -> bool:
        """pid 文件是否仍对应同一进程；防止 PID 复用后误杀。"""
        if not pid:
            return False
        data = self._pid_record()
        try:
            recorded = int(data.get("pid") or 0) if data is not None else 0
            target = int(pid)
        except (TypeError, ValueError):
            return False
        if data is None or recorded != target:
            return False
        try:
            p = psutil.Process(pid)
            if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
                return False
            created = data.get("create_time")
            if created is None:
                return False
            return abs(float(p.create_time()) - float(created)) <= 1.0
        except Exception:
            return False

    def _write_pid(self, pid: int) -> None:
        create_time = 0.0
        try:
            create_time = float(psutil.Process(pid).create_time())
        except Exception:
            pass
        try:
            atomic.write_text(self.pid_file, json.dumps(
                {"pid": pid, "started_at": time.time(),
                 "create_time": create_time},
                ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _clear_pid(self) -> None:
        try:
            self.pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    async def _runtime_status(self) -> dict | None:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=0.5))
        try:
            async with self._http.get(
                    f"http://127.0.0.1:{self.debug_port}/status") as r:
                if r.status == 200:
                    return await r.json(content_type=None)
        except Exception:
            return None
        return None

    async def _close_http(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    async def _watch(self, proc, generation: int) -> None:
        try:
            await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if generation != self._generation:
            return
        if self.on_exit is not None:
            self.on_exit()

    async def status(self) -> dict:
        own = bool(self.proc and self.proc.returncode is None)
        pid = self.proc.pid if own else self._pid()
        external = False
        if not own and pid and self._pid_owned(pid):
            own = False
            running = True
            external = True
        else:
            running = own
        rt = await self._runtime_status() if running else None
        alive = bool(running and (own or self._pid_owned(pid)))
        if self.proc is not None and self.proc.returncode is not None:
            alive = False
        return {
            "running": alive,
            "external": external,
            "pid": pid if alive else None,
            "started_at": self.started_at or None,
            "uptime_s": round(time.time() - self.started_at, 1)
            if self.started_at and alive else None,
            "returncode": self.proc.returncode if self.proc else None,
            "debug_port": self.debug_port,
            "runtime": rt,
            "console_log": str(self.console_log),
        }

    # ── 启停 ──────────────────────────────────────────
    async def start(self) -> dict:
        async with self._lock:
            st = await self.status()
            if st.get("running"):
                return {"ok": True, "already": True, **st}
            # Windows 的 venv python.exe 是一个 launcher：它自己会再起一个 base
            # python，父进程被杀时子进程可能留下。这里直接用 base 解释器 +
            # venv 的 site-packages，PID 就是真正跑 runtime 的进程。
            exe = getattr(sys, "_base_executable", "") or sys.executable
            cmd = [exe, "-m", "sidekick"]
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            venv = Path(os.environ.get("VIRTUAL_ENV") or sys.prefix)
            site = venv / "Lib" / "site-packages"
            if site.exists():
                env["VIRTUAL_ENV"] = str(venv)
                env["PYTHONPATH"] = os.pathsep.join(
                    [str(site)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
            try:
                self._close_log()          # 上一轮崩溃可能留下旧句柄
                self._clear_logs()
                self.ready_file.unlink(missing_ok=True)
                self._log_fp = open(self.console_log, "ab", buffering=0)
                self._log_fp.write(
                    f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} WebUI 启动 =====\n"
                    .encode("utf-8"))
                self.proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=str(self.root), env=env,
                    stdout=subprocess.DEVNULL, stderr=self._log_fp,
                    creationflags=CREATE_NEW_PROCESS_GROUP)
            except Exception as e:
                self._close_log()
                return {"ok": False, "error": f"启动失败：{type(e).__name__}: {e}"}
            if self._job is None:
                self._job = KillOnClose()
                # 非 Windows 本来就没有作业对象，不算失败
                if sys.platform == "win32" and not self._job.active:
                    log("[webui] 作业对象建不起来：关窗口时可能留下子进程")
            # 收不进来只可能是 runtime 已经死了，那条路本来就有 pid + taskkill 兜底
            self._job.assign(self.proc.pid)
            self.started_at = time.time()
            self._write_pid(self.proc.pid)
            self._generation += 1
            self._watch_task = asyncio.create_task(
                self._watch(self.proc, self._generation))

            # 就绪标记由 runtime 在 startup 完成后写；不依赖可选的 /status 端口。
            deadline = time.time() + 180
            while time.time() < deadline:
                if self.proc.returncode is not None:
                    break
                try:
                    ready = self.ready_file.read_text(
                        encoding="utf-8", errors="replace").strip()
                except OSError:
                    ready = ""
                if ready == str(self.proc.pid):
                    return {"ok": True, **await self.status()}
                await asyncio.sleep(0.2)
            tail = self.tail(40)
            err = "runtime 没在 180 秒内就绪"
            if self.proc.returncode is not None:
                err = f"runtime 启动后退出（code={self.proc.returncode}）"
            await self._stop_locked()
            return {"ok": False, "error": err, "log": tail}

    async def stop(self) -> dict:
        async with self._lock:
            return await self._stop_locked()

    async def _stop_locked(self) -> dict:
        """停止逻辑；调用方必须已经持有 self._lock。

        `start()` 失败路径也要清理进程，但它已经在锁里，不能调会再次拿锁的
        `stop()`——非重入锁会直接把自己锁死。
        """
        self._generation += 1
        if self._watch_task is not None:
            self._watch_task.cancel()
            self._watch_task = None
        own = bool(self.proc and self.proc.returncode is None)
        pid = self.proc.pid if own else self._pid()
        had_process = bool(pid and (own or self._pid_owned(pid)))
        if had_process:
            if sys.platform == "win32":
                # Windows 上 terminate() 就是 TerminateProcess，runtime 的
                # finally 不会执行；desktop 子进程也不会被回收。直接杀整棵树，
                # 再等 runtime 被回收。
                try:
                    await asyncio.to_thread(
                        subprocess.run,
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True, timeout=10)
                except Exception:
                    pass
                if self.proc is not None and self.proc.returncode is None:
                    try:
                        await asyncio.wait_for(self.proc.wait(), timeout=8)
                    except Exception:
                        pass
            else:
                # POSIX 先给 runtime 一个收尾机会。
                if self.proc is not None and self.proc.returncode is None:
                    try:
                        self.proc.terminate()
                        await asyncio.wait_for(self.proc.wait(), timeout=8)
                    except Exception:
                        pass
            if self._pid_alive(pid):
                try:
                    p = psutil.Process(pid)
                    p.kill()
                except Exception:
                    pass
        # 无论有没有 pid，都清掉旧 proc/日志句柄；否则 /status 会一直显示上一轮的运行态。
        self.proc = None
        self.started_at = 0.0
        self._clear_pid()
        self.ready_file.unlink(missing_ok=True)
        self._close_log()
        return {"ok": True, **({"already": True} if not had_process else {})}

    async def restart(self) -> dict:
        await self.stop()
        return await self.start()

    # ── 日志 ──────────────────────────────────────────
    # 正常读 run.log；只有 run.log 不存在/为空时才回退 console（启动即崩、
    # 还没写出 run.log 的情况）。两个文件都只从末尾读 256 KB，结果按指纹缓存。
    _TAIL_CHUNK = 256 * 1024

    def _clear_logs(self) -> None:
        for p in (self.root / "state" / "run.log", self.console_log):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        self._tail_key = None
        self._tail_lines = []

    def tail(self, n: int = 160) -> list[str]:
        n = max(1, int(n))
        run_log = self.root / "state" / "run.log"
        files = (run_log, self.console_log)
        key = []
        for p in files:
            try:
                st = p.stat()
                key.append((st.st_mtime_ns, st.st_size))
            except OSError:
                key.append(None)
        key = tuple(key)
        if self._tail_key == (key, n):
            return list(self._tail_lines)

        lines = _tail_lines(run_log, n, self._TAIL_CHUNK)
        if not lines:
            lines = _tail_lines(self.console_log, n, self._TAIL_CHUNK)
        self._tail_key = (key, n)
        self._tail_lines = lines[-n:]
        return list(self._tail_lines)

    def _close_log(self) -> None:
        try:
            if self._log_fp is not None:
                self._log_fp.close()
        except Exception:
            pass
        self._log_fp = None

    async def shutdown(self) -> None:
        await self.stop()
        # 常规清理没带走的（runtime 被硬杀时它的桌宠子进程）由内核补一刀。
        if self._job is not None:
            self._job.close()
            self._job = None
        await self._close_http()
