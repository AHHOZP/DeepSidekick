"""Runtime 的只读状态：feature 四态、/status、/peek。

放在单独模块，是为了让 runtime.py 只保留装配/主循环/生命周期。
这里的函数**不改变任何运行时状态**：配置刷新只在 `Runtime.main_loop`，
模式状态机只在 `Scheduler` 里推进。
"""
from __future__ import annotations

import time

from sidekick import config as configmod
from sidekick.brain import material
from sidekick.scheduling import mode as pacing
from sidekick.config import secrets as secretstore
from sidekick.core.ports import FeatureStatus
from sidekick.avatar.stage import stage_url
from sidekick.brain.user import UserProfile
from sidekick.input import composer
from sidekick.input.barge import active_for


def stt_status(rt) -> dict:
    if rt.stt_worker is not None:
        return rt.stt_worker.status()
    return dict(rt.stt.snapshot())


def features(rt) -> dict[str, dict]:
    """从真实对象读运行态；配置只决定热更后的期望值，不作为状态。"""
    out: dict[str, dict] = {}

    def add(st: FeatureStatus) -> None:
        out[st.name] = st.to_dict()

    mic_on = bool(getattr(rt.mic, "enabled", False))
    mic_error = getattr(rt.mic, "error", None)
    add(FeatureStatus("mic", enabled=mic_on,
                      degraded=mic_on and bool(mic_error),
                      error=mic_error))

    stt_worker = getattr(rt, "stt_worker", None)
    stt_on = stt_worker is not None
    stt_unavailable = bool(getattr(rt, "stt_unavailable", False))
    stt = (stt_worker.status() if stt_worker is not None
           else rt.stt.snapshot())
    stt_dead = bool(stt.get("dead"))
    stt_disabled = bool(stt.get("disabled"))
    add(FeatureStatus(
        "stt", enabled=stt_on,
        available=stt_on and not stt_unavailable,
        healthy=stt_on and not (stt_dead or stt_disabled),
        degraded=stt_on and (stt_dead or stt_disabled),
        error=(stt.get("error") or getattr(rt.stt, "error", None))
        if stt_on else None))

    frames_on = bool(getattr(rt.frames, "enabled", False))
    frames_error = getattr(rt.frames, "last_error", None)
    add(FeatureStatus("capture", enabled=frames_on,
                      degraded=frames_on and bool(frames_error),
                      error=frames_error))

    stage_enabled = bool((rt.cfg.get("stage") or {}).get("enabled", False))
    stage_available = getattr(rt, "stage_runner", None) is not None
    stage_error = getattr(rt, "stage_error", None)
    add(FeatureStatus("stage", enabled=stage_enabled,
                      available=stage_available,
                      degraded=stage_enabled and bool(stage_error),
                      error=stage_error))

    tts = rt.speaker.snapshot()
    tts_on = str(getattr(rt.speaker, "profile_name", "none")) != "none"
    tts_error = tts.get("error")
    add(FeatureStatus("tts", enabled=tts_on,
                      available=(tts_on
                                 and bool(getattr(rt.speaker, "available", False))),
                      degraded=tts_on and bool(tts_error),
                      error=tts_error))

    memory_on = bool(getattr(rt.memory, "enabled", False))
    mem_error = getattr(rt.memory, "last_summary_error", None)
    add(FeatureStatus("memory", enabled=memory_on,
                      degraded=memory_on and bool(mem_error),
                      error=mem_error))

    embedder = getattr(rt, "embedder", None)
    embed_on = bool(getattr(embedder, "enabled", False))
    embed_state = str(getattr(embedder, "state", "disabled") or "disabled")
    embed_error = getattr(embedder, "error", None)
    embed_degraded = getattr(embedder, "last_degraded", None)
    add(FeatureStatus(
        "embedding", enabled=embed_on,
        available=embed_on and embed_state != "unavailable",
        healthy=embed_on and embed_state == "ready",
        degraded=embed_on and (embed_state not in ("ready", "unavailable")
                               or bool(embed_degraded)),
        error=embed_error or (f"本次召回降级：{embed_degraded}"
                              if embed_degraded else None)))

    worldbook = getattr(rt.brain, "worldbook", None)
    game_on = bool(getattr(worldbook, "enabled", False))
    wb_error = getattr(rt.brain, "worldbook_error", None)
    add(FeatureStatus("game", enabled=game_on,
                      healthy=game_on and not wb_error,
                      degraded=game_on and bool(wb_error),
                      error=wb_error))

    avatar_on = stage_enabled and bool(getattr(rt.actions, "avatar_id", ""))
    broken = list(getattr(rt.actions, "broken", [])) if avatar_on else []
    current = getattr(getattr(rt, "actions", None), "avatar", None)
    profile_error = getattr(current, "profile_error", None) if avatar_on else None
    add(FeatureStatus("avatar", enabled=avatar_on,
                      available=avatar_on and stage_available and not broken,
                      degraded=avatar_on and (bool(stage_error) or bool(broken)
                                              or bool(profile_error)),
                      error="；".join(broken) if broken else
                      (profile_error or stage_error)))
    return out


def _safe(fn, fallback: dict, label: str) -> dict:
    try:
        return fn()
    except Exception as e:
        return {**fallback, "error": f"{label}: {type(e).__name__}: {e}"}


def capture_status(rt) -> dict:
    """截图队列 + 上一轮视觉请求的只读指标。

    `merge_ratio` 是累计合并次数占采样次数的比例；`last_round_*` 由
    Runtime 在每轮 `_think()` 记录，缺省为 0，不影响既有状态形状。
    """
    st = dict(_safe(rt.frames.stats, {}, "capture"))
    merged = int(st.get("merged") or 0)
    grabbed = int(st.get("grabbed") or 0)
    total = merged + grabbed
    st.update({
        "last_round_frames": int(getattr(rt, "last_round_frames", 0) or 0),
        "last_round_images": int(getattr(rt, "last_round_images", 0) or 0),
        "last_round_payload_kb": round(float(
            getattr(rt, "last_round_payload_kb", 0.0) or 0.0), 1),
        "merge_ratio": round(merged / total, 3) if total else 0.0,
    })
    return st


def avatar_status(rt) -> dict:
    """当前角色（形象 + 角色设定），只读。"""
    avatar = getattr(getattr(rt, "actions", None), "avatar", None)
    if avatar is None:
        return {}
    persona = rt.cfg.get("persona") or {}
    return {
        "id": str(getattr(avatar, "id", "") or ""),
        "name": str(getattr(avatar, "shown_name", "") or ""),
        "profile_error": getattr(avatar, "profile_error", None),
        "stage": dict(getattr(avatar, "stage", None) or {}),
        "bot_name": str(persona.get("bot_name") or ""),
        "aliases": list(persona.get("aliases") or []),
    }


def user_status(rt) -> dict:
    user = rt.cfg.get("user") or {}
    profile = UserProfile.from_cfg(rt.cfg)
    return {"id": str(user.get("id") or ""), "name": profile.name,
            "error": str(user.get("error") or "").strip() or None}


def _autonomy_status(rt, stage_stats: dict) -> dict:
    """只读快照：配置开关 + 桌面/浏览器自报的引擎状态。"""
    stage_cfg = rt.cfg.get("stage") or {}
    enabled = bool((stage_cfg.get("autonomy") or {}).get("enabled", True))
    report = stage_stats.get("report") if isinstance(stage_stats, dict) else None
    auto = report.get("autonomy") if isinstance(report, dict) else None
    auto = auto if isinstance(auto, dict) else {}
    host = getattr(rt, "stage_motion", None)
    if host is not None:
        auto = host.report()
    out = {
        "enabled": enabled,
        "active": False,
        "source": "off",
        "tier": "",
        "archetype": "unknown",
        "fallback": False,
        "params": 0,
        "action": "",
        "emotion": "neutral",
        "mouth": 0.0,
        "owned_params": 0,
        "warnings": 0,
        "error": None,
    }
    if enabled:
        out["active"] = bool(auto.get("active", False))
        out["source"] = str(auto.get("source") or "off")
        out["tier"] = str(auto.get("tier") or "")
        out["archetype"] = str(auto.get("archetype") or "unknown")
        out["fallback"] = bool(auto.get("fallback", False))
        out["params"] = int(auto.get("params") or 0)
        out["action"] = str(auto.get("action") or "")
        out["emotion"] = str(auto.get("emotion") or "neutral")
        try:
            out["mouth"] = round(float(auto.get("mouth") or 0.0), 3)
        except (TypeError, ValueError):
            out["mouth"] = 0.0
        out["owned_params"] = int(auto.get("owned_params") or 0)
        out["warnings"] = int(auto.get("warnings") or 0)
        out["error"] = auto.get("error") or getattr(
            rt, "_stage_motion_error", None)
    return out


def status(rt) -> dict:
    mode = rt.current_mode()
    try:
        feat = features(rt)
        features_error = None
    except Exception as e:
        feat = {}
        features_error = f"{type(e).__name__}: {e}"
    memory = _safe(rt.memory.stats, {"enabled": bool(
        getattr(rt.memory, "enabled", False))}, "memory")
    brain = _safe(rt.brain.stats, {}, "brain")
    tts = _safe(rt.speaker.snapshot, {}, "tts")
    scheduler = _safe(rt.scheduler.stats, {}, "scheduler")
    capture = _safe(lambda: capture_status(rt), {}, "capture")
    errors = _safe(rt.fb.stats, {}, "errors")
    stt = _safe(lambda: stt_status(rt), {}, "stt")
    stage_stats = _safe(rt.stage.stats, {}, "stage")
    embedding = (_safe(rt.embedder.stats, {}, "embedding")
                 if getattr(rt, "embedder", None) is not None else {})
    recall = (_safe(rt.recall.status, {}, "recall")
              if getattr(rt, "recall", None) is not None else {})
    return {
        "uptime_s": round(time.time() - rt.said.started_at, 1)
        if getattr(rt.said, "started_at", None) else None,
        "rounds": rt.rounds,
        "live": rt.session.live,
        "mode": mode,
        "mode_label": pacing.LABEL.get(mode, ""),
        "viewers": rt.events.viewers,
        "danmu_5min": rt.events.count_audience(300),
        "said_last_hour": rt.said.count_last_hour(),
        "last_me": rt.stt_worker.last_me if rt.stt_worker is not None else None,
        "mic": {"level": round(rt.mic.level, 4),
                "speaking": rt.mic.user_speaking,
                "error": rt.mic.error,
                "restarts": rt.mic.restarts,
                "speech_ms": round(rt.mic.speech_ms),
                "mode": rt.mic.mode,
                "mode_cfg": rt.mic.mode_cfg,
                "gate_percent": rt.mic.gate_percent,
                "noise_floor": round(rt.mic.noise, 5),
                "noise_peak": round(rt.mic.noise_high, 5),
                "gate": round(rt.mic.gate, 5),
                "gate_continue": round(rt.mic.gate_continue, 5),
                "barge_ms": round(rt.mic.barge_ms)},
        "barge": {"active": active_for(str(rt.cfg["barge"]["enabled"]).lower(),
                                       rt.mic.mode),
                  "enabled": str(rt.cfg["barge"]["enabled"]).lower(),
                  "triggers": rt.mic.barge_triggers,
                  "ms": round(rt.mic.barge_ms),
                  "play_ref": round(float(getattr(rt.speaker, "play_ref",
                                                  (0.0, 0.0))[0]), 3),
                  "ratio": float(rt.cfg["barge"]["ratio"])},
        "stt": stt,
        "tts": tts,
        "brain": brain,
        "memory": memory,
        "embedding": embedding,
        "recall": recall,
        "scheduler": scheduler,
        "capture": capture,
        "errors": errors,
        "said_error": getattr(rt.said, "last_error", None),
        "display_error": getattr(rt.display, "last_error", None),
        "config_error": configmod.last_error,
        "secrets_error": secretstore.last_error,
        "silent_after_me": rt.silent_after_me,
        "stage": {**stage_stats, "url": stage_url(rt.cfg),
                  "enabled": bool(rt.cfg["stage"]["enabled"]),
                  "error": rt.stage_error or stage_stats.get("error")},
        "autonomy": _autonomy_status(rt, stage_stats),
        "source": rt.cfg["danmu_source"],
        "avatar": avatar_status(rt),
        "user": user_status(rt),
        "danmu_error": getattr(rt, "_danmu_error", None),
        "features": feat,
        "features_error": features_error,
    }


def peek(rt, seconds: float = 90.0) -> str:
    mode = rt.current_mode()
    p = rt.cfg["persona"]
    profile = UserProfile.from_cfg(rt.cfg)
    bot_names = [str(x) for x in p["aliases"] if str(x).strip()]
    frames = rt.frames.preview()
    pack = composer.plan(frames, rt.cfg)
    window = getattr(rt, "window", None)
    window_line, privacy_note = (window.material_lines()
                                 if window is not None else ("", ""))
    text, _ = material.build_round(
        rt.events, rt.said, mode, seconds=seconds,
        frame_ts=[f.ts for f in frames],
        name=profile.name, reason="预览", bot_names=bot_names,
        live=rt.session.live, remember_cold=False, pack=pack,
        window_line=window_line, privacy_note=privacy_note)
    return text
