from __future__ import annotations

from async_fixture import QuietIsolatedAsyncioTestCase

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sidekick import config
from sidekick.app.runtime import Runtime
from sidekick.core.events import EventBus
from sidekick.core.null import (
    InMemoryHistory,
    NullAvatarActions,
    NullFrames,
    NullLongTerm,
    NullMic,
    NullStage,
    NullStt,
)


def _runtime(wait):
    async def refresh():
        return None

    rt = Runtime(
        cfg=config.defaults(), root=Path("."), events=EventBus(),
        said=SimpleNamespace(started_at=0.0), frames=NullFrames(),
        mic=NullMic(), stt=NullStt(), stage=NullStage(),
        display=SimpleNamespace(cps=4.6, status=lambda text: None),
        speaker=SimpleNamespace(
            profile_name="none", preset_name="", available=False,
            preserve_square_tags=frozenset(), prompt_rules=""),
        scheduler=SimpleNamespace(wait=wait, current_mode="normal"),
        brain=SimpleNamespace(model="m", stats=lambda: {}),
        actions=NullAvatarActions(), catalog=None,
        fallback=SimpleNamespace(), history=InMemoryHistory(4),
        memory=NullLongTerm(), session=SimpleNamespace(mode="stream"))
    rt._refresh = refresh
    return rt


class MainLoopCancellationTests(QuietIsolatedAsyncioTestCase):
    async def test_normal_round_dispatches_think_and_talk(self):
        talk_started = asyncio.Event()
        waited = asyncio.Event()
        calls = 0

        async def wait(_timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"round": 1}
            waited.set()
            await asyncio.sleep(999)

        async def think(_snap):
            return "out", 0.0

        async def talk(_snap, _out, _t0):
            talk_started.set()

        rt = _runtime(wait)
        rt._think = think
        rt._talk = talk
        with mock.patch("sidekick.app.runtime.log"):
            task = asyncio.create_task(rt.main_loop())
            await asyncio.wait_for(talk_started.wait(), 1.0)
            await asyncio.wait_for(waited.wait(), 1.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(task.cancelled())

    async def test_cancel_idle_loop_finishes(self):
        started = asyncio.Event()

        async def wait(_timeout):
            started.set()
            await asyncio.sleep(999)

        rt = _runtime(wait)
        with mock.patch("sidekick.app.runtime.log"):
            task = asyncio.create_task(rt.main_loop())
            await asyncio.wait_for(started.wait(), 1.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(task.cancelled())

    async def test_cancel_while_talk_is_playing_cancels_and_awaits_it(self):
        talk_started = asyncio.Event()
        talk_cleaned = asyncio.Event()
        talk_tasks: list[asyncio.Task] = []
        calls = 0

        async def wait(_timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"round": 1}
            await asyncio.sleep(999)

        async def think(_snap):
            return "out", 0.0

        async def talk(_snap, _out, _t0):
            talk_tasks.append(asyncio.current_task())
            talk_started.set()
            try:
                await asyncio.sleep(999)
            finally:
                talk_cleaned.set()

        rt = _runtime(wait)
        rt._think = think
        rt._talk = talk
        with mock.patch("sidekick.app.runtime.log"):
            task = asyncio.create_task(rt.main_loop())
            await asyncio.wait_for(talk_started.wait(), 1.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(talk_cleaned.is_set())
        self.assertTrue(talk_tasks)
        self.assertTrue(talk_tasks[0].cancelled())


if __name__ == "__main__":
    unittest.main()
