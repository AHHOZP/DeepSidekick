"""Runtime 生命周期：启动顺序、端口、后台任务、关闭顺序。

顺序很重要：**先加载识别模型，再开麦克风**；先收 TTS/记忆，再关连接。
"""
from __future__ import annotations

import asyncio
import os

from sidekick.core.log import log
from sidekick.avatar.stage import serve_stage, stage_url
from sidekick.desktop import launcher as desktop_launcher
from sidekick.input.window_context import NullWindowContext, WindowContext


def pet_rect_provider(cfg: dict):
    """组合根注入 capture 的只读回调；capture 自己不 import desktop。"""
    return desktop_launcher.pet_rect_provider(cfg)


def window_context(cfg: dict):
    """组合根注入窗口上下文；非 Windows 用空对象，不把平台判断散到业务层。"""
    return WindowContext(cfg) if os.name == "nt" else NullWindowContext()


async def startup(rt) -> None:
    rt.said.start()
    if getattr(rt, "embedder", None) is not None:
        rt.embedder.start()

    if bool(rt.cfg["stt"]["enabled"]):
        if rt.stt.warmup():
            log(f"[stt] engine={rt.stt.name} model={rt.stt.model_size} "
                f"threads={rt.cfg['stt']['num_threads']}")
            log(f"[stt] loaded in {rt.stt.load_s:.2f}s")
            if rt.stt_worker is not None:
                rt.stt_worker.start()
        else:
            rt._stt_unavailable = True
            log(f"[!!] feature stt unavailable：{rt.stt.error}"
                f"（语音输入不可用；截图/弹幕/脑/TTS 不受影响）")
            rt.mic.on_segment = None
    else:
        log("STT 已关闭（stt.enabled=false；麦克风仍可让路/打断）")

    rt.mic.start()
    if getattr(rt.mic, "error", None):
        log(rt.mic.error)

    if rt.frames.enabled:
        rt.frames.start()
        log(f"截图：每 {rt.frames.interval} 秒一张，一轮最多 "
            f"{rt.frames.max_frames} 张，{rt.cfg['capture']['resolution']}")

    src = str(rt.cfg["danmu_source"]).lower()
    if src == "none":
        log("弹幕源=无")
    elif src == "bili":
        from sidekick.input import bili as danmu_bili

        async def run_danmu() -> None:
            try:
                await danmu_bili.run(rt.events, rt.cfg, log)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                rt._danmu_error = f"{type(e).__name__}: {e}"
                log(f"[!!] 弹幕源失败：{rt._danmu_error}")

        rt._danmu_task = asyncio.create_task(run_danmu())
    else:
        log(f"未知 danmu_source: {src}")

    if bool(rt.cfg["stage"]["enabled"]):
        try:
            rt._stage_runner = await serve_stage(
                rt.stage, rt.cfg, cfg_provider=lambda: rt.cfg,
                catalog=rt.catalog, on_click=rt.on_desktop_click,
                autonomy_active=lambda: rt.stage_motion is not None)
            log(f"形象层：{stage_url(rt.cfg)}（直播姬加「浏览器」素材指向它）")
            log(f"自检页：{stage_url(rt.cfg)}probe")
            mode = str(rt.cfg["stage"]["mode"]).lower()
            if mode == "desktop":
                avatar_id = ""
                if rt.catalog is not None:
                    try:
                        avatar_id = rt.catalog.selected().id
                    except Exception:
                        avatar_id = ""
                rt._desktop_proc = desktop_launcher.launch(
                    rt.cfg, log, avatar_id=avatar_id)
                if rt._desktop_proc is not None:
                    log(f"桌面模式：{mode}（空白处穿透，拖角色移动）")
        except Exception as e:
            rt._stage_error = f"{type(e).__name__}: {e}"
            log(f"[!!] feature stage unavailable：{rt._stage_error}"
                f"（不影响说话）")

    log(f"启动完成：直播模式={'开' if rt.session.live else '关'}  "
        f"弹幕={src}  TTS={rt.speaker.profile_name}  "
        f"截屏={'开' if rt.frames.enabled else '关'}  脑={rt.brain.model}")
    bst = rt.brain.stats()
    log(f"预设 {bst['preset_id'] or '（未设置）'} {bst['preset_blocks']} 块"
        f" · 人设 {bst['persona_chars']} 字 · 用户 {bst['user_chars']} 字"
        f" · 游戏 {bst['game_id'] or '无'} {bst['game_chars']} 字"
        f" · 记忆 {bst['memory_chars']} 字"
        f" · system {bst['prefix_systems']} 条（含输出约束 {bst['output_chars']} 字）"
        f" · 形象 {bst['avatar']}")
    if bst["preset_error"]:
        log(f"[!!] 对话预设：{bst['preset_error']}")
    if bst["game_error"]:
        log(f"[!!] 游戏知识包：{bst['game_error']}")
    line = await rt.brain.warmup()
    if line:
        log(f"缓存预热：{line}")


async def shutdown(rt) -> None:
    rt._closed = True
    task = getattr(rt, "_stage_motion_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        rt._stage_motion_task = None
    rt.mic.close()
    rt.frames.stop()
    if rt._danmu_task:
        rt._danmu_task.cancel()
        try:
            await rt._danmu_task
        except (asyncio.CancelledError, Exception):
            pass
        rt._danmu_task = None
    stt_worker_closed = True
    if rt.stt_worker is not None:
        stt_worker_closed = await asyncio.to_thread(rt.stt_worker.close)
        if not stt_worker_closed:
            log("[!!] STT worker 未在超时内退出，跳过引擎关闭")
    if stt_worker_closed:
        try:
            rt.stt.close()
        except Exception:
            pass
    desktop_launcher.stop(rt._desktop_proc, log)
    rt._desktop_proc = None
    if rt._stage_runner is not None:
        try:
            await rt._stage_runner.cleanup()
        except Exception:
            pass
    try:
        await rt.brain.close()
    except Exception as e:
        log(f"brain 关闭失败：{type(e).__name__}: {e}")
    if getattr(rt, "embedder", None) is not None:
        try:
            await asyncio.to_thread(rt.embedder.close)
        except Exception as e:
            log(f"embed 关闭失败：{type(e).__name__}: {e}")
    try:
        rt.history.close()
    except Exception:
        pass
    try:
        rt.memory.close()
    except Exception:
        pass
    try:
        await rt.speaker.close()
    except Exception as e:
        log(f"speaker 关闭失败：{type(e).__name__}: {e}")
