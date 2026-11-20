"""TTS 厂商预设：协议模板、表单字段、标签规则。

这里**只描述厂商**，不发请求、不读配置；`sidekick/output/tts/http.py` 只认
`wire`，不认厂商名。新增一家同形状的厂商 = 在 `PRESETS` 里加一段 dict。

占位符只有 `{text}`、preset 字段名和 `{pcm_sr}`；响应路径用点分，数字段
表示数组下标。没有表达式、条件或循环。
"""
from __future__ import annotations

import re

PROFILE_NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")

# 首批 9 家；顺序就是 WebUI 厂商下拉的顺序（Edge 默认，OpenAI 兼容第二）。
PRESETS: dict[str, dict] = {
    "edge": {
        "label": "Edge（免费，免注册）",
        "desc": "微软在线语音，无需注册；无情感标签，不能克隆音色",
        "doc": "https://github.com/rany2/edge-tts",
        "wire": "edge",
        "auth": None,
        "fields": {
            "voice": {
                "label": "音色", "type": "select",
                "default": "zh-CN-XiaoyiNeural",
                "options": [
                    ("zh-CN-XiaoyiNeural", "晓伊 · 活泼少女"),
                    ("zh-CN-XiaoxiaoNeural", "晓晓 · 温柔女声"),
                    ("zh-CN-XiaomoNeural", "晓墨 · 情感丰富"),
                    ("zh-CN-YunxiNeural", "云希 · 少年"),
                    ("zh-CN-YunyangNeural", "云扬 · 播音男声"),
                    ("zh-TW-HsiaoChenNeural", "曉臻 · 台湾女声"),
                    ("ja-JP-NanamiNeural", "七海 · 日语"),
                    ("en-US-AvaMultilingualNeural", "Ava · 英语"),
                ],
            },
            "rate": {
                "label": "语速", "type": "text", "default": "+6%", "help": "如 +10% / -5%",
            },
        },
    },
    "openai": {
        "label": "OpenAI 兼容（网关 / 第三方）",
        "desc": "任何 /v1/audio/speech 兼容服务：OpenAI、one-api/new-api、Kokoro、LocalAI…",
        "doc": "https://developers.openai.com/api/docs/guides/text-to-speech",
        "wire": "http",
        "auth": {"kind": "bearer"},
        "path": "/audio/speech",
        "body": {
            "model": "{model}", "input": "{text}", "voice": "{voice}",
            "response_format": "pcm", "instructions": "{instructions}",
        },
        "body_optional": ["instructions"],
        "response": {"kind": "raw"},
        "pcm_sr": 24000,
        "fields": {
            "base_url": {
                "label": "Base URL", "type": "text",
                "default": "https://api.openai.com/v1",
                "help": "填到 /v1 为止；网关示例 https://api.siliconflow.cn/v1",
            },
            "model": {
                "label": "模型", "type": "text", "default": "gpt-4o-mini-tts",
                "help": "官方 tts-1/gpt-4o-mini-tts 于 2027-01-06 停用；网关请填网关支持的模型名",
            },
            "voice": {
                "label": "音色", "type": "select", "default": "alloy",
                "options": [
                    ("alloy", "alloy"), ("ash", "ash"), ("ballad", "ballad"),
                    ("coral", "coral"), ("echo", "echo"), ("fable", "fable"),
                    ("nova", "nova"), ("onyx", "onyx"), ("sage", "sage"),
                    ("shimmer", "shimmer"), ("verse", "verse"),
                    ("marin", "marin"), ("cedar", "cedar"),
                ],
            },
            "instructions": {
                "label": "风格提示", "type": "text", "default": "",
                "help": "如“用轻快上扬的语气，语速稍快”；留空不传",
            },
            "pcm_sr": {
                "label": "PCM 采样率", "type": "select", "default": "24000",
                "options": [
                    ("16000", "16000"), ("22050", "22050"),
                    ("24000", "24000"), ("32000", "32000"),
                    ("44100", "44100"), ("48000", "48000"),
                ],
                "help": "声音变快/变慢时改这里；OpenAI 官方固定 24000",
            },
        },
    },
    "doubao": {
        "label": "豆包（火山引擎）",
        "desc": "中文直播常用；有声音复刻；需在火山引擎控制台开通语音合成",
        "doc": "https://console.volcengine.com/speech/new/setting/apikeys",
        "wire": "http",
        "auth": {"kind": "header", "name": "X-Api-Key"},
        "path": "/api/v3/tts/unidirectional/sse",
        "headers": {"X-Api-Resource-Id": "{model}"},
        "body": {
            "user": {"uid": "deepsidekick"},
            "req_params": {
                "text": "{text}", "speaker": "{voice}",
                "sample_rate": "{pcm_sr}",
                "audio_params": {"format": "pcm"},
            },
        },
        "response": {"kind": "sse", "path": "data", "encoding": "base64"},
        "pcm_sr": 24000,
        "fields": {
            "base_url": {
                "label": "Base URL", "type": "text",
                "default": "https://openspeech.bytedance.com",
            },
            "model": {
                "label": "模型", "type": "text",
                "default": "seed-tts-2.0",
                "help": "控制台开通的语音合成版本；一般不用改",
            },
            "voice": {
                "label": "音色", "type": "text", "default": "",
                "required": True, "help": "复制控制台里的 speaker / voice_type",
                "doc": "https://www.volcengine.com/docs/6561/1257544",
            },
        },
    },
    "fish": {
        "label": "Fish Audio",
        "desc": "音色克隆；注册后填 API Key 和音色",
        "doc": "https://fish.audio/app/developers/get-started/",
        "wire": "http",
        "auth": {"kind": "bearer"},
        "path": "/v1/tts",
        "headers": {"model": "{model}"},
        "body": {
            "text": "{text}",
            "reference_id": "{voice}",
            "format": "pcm",
            "sample_rate": "{pcm_sr}",
            "latency": "low",
            "chunk_length": 100,
            "normalize": True,
            "prosody": {"speed": 1, "volume": 0, "normalize_loudness": True},
        },
        "body_optional": ["reference_id"],
        "response": {"kind": "raw"},
        "pcm_sr": 24000,
        "fields": {
            "base_url": {
                "label": "Base URL", "type": "text",
                "default": "https://api.fish.audio",
            },
            "model": {
                "label": "模型", "type": "select", "default": "s2.1-pro-free",
                "options": [
                    ("s2.1-pro-free", "s2.1-pro-free（免费额度）"),
                    ("s2.1-pro", "s2.1-pro"),
                    ("drama-3", "drama-3"),
                    ("s2-pro", "s2-pro"), ("s1", "s1"),
                ],
            },
            "voice": {
                "label": "音色", "type": "text", "default": "",
                "required": False,
                "help": "在 fish.audio 的音色页复制 reference_id；留空用默认音色",
                "doc": "https://fish.audio/",
            },
        },
        "tags": {
            "label": "Fish S2",
            "open": "[", "close": "]",
            "intro": "情绪或语气真到了位，可以在正文顺手插一个半角方括号发声标签，比如「诶嘿，这波稳了[chuckling]」。",
            "words_intro": "只能从这些里挑，别自己造：",
            "outro": "一句话最多加一个，没必要就别加。不要用中文圆括号当标签，也别在正文里写「（转身）」「（拿起水杯）」这种动作描写，动作归 `<ACT>` 管；发声标签也别写到 `<ACT>` 或 `<scene>` 里去。",
            "words": [
                "chuckling", "laughing", "sighing", "whispering", "break",
                "long-break", "emphasis", "in a hurry tone", "slow", "sobbing",
                "gasping", "muttering", "uncertain", "nervous", "unhappy",
                "surprised", "proud", "resigned", "cough", "laughs", "sighs",
                "pause", "long pause", "fast", "drawn out", "inhale",
                "choked up", "hearty laugh",
            ],
            "map": {
                "小声": "whispering",
                "低语": "whispering",
                "耳语": "whispering",
                "叹气": "sighing",
                "笑": "chuckling",
                "爽朗大笑": "laughing",
                "停顿": "break",
                "长停顿": "long-break",
                "强调": "emphasis",
                "语速加快": "in a hurry tone",
                "语速放缓": "slow",
                "哽咽": "sobbing",
                "吸气": "gasping",
                "碎碎念": "muttering",
                "小声嘀咕": "muttering",
                "欲言又止": "uncertain",
                "沉默片刻": "break",
                "紧张": "nervous",
                "委屈": "unhappy",
                "惊讶": "surprised",
                "得意": "proud",
                "无奈": "resigned",
            },
        },
    },
    "minimax": {
        "label": "MiniMax",
        "desc": "中文主流；332 个预置音色、可克隆；默认 speech-2.8-hd",
        "doc": "https://platform.minimaxi.com/",
        "wire": "http",
        "auth": {"kind": "bearer"},
        "path": "/v1/t2a_v2",
        "body": {
            "model": "{model}", "text": "{text}", "stream": True,
            "voice_setting": {
                "voice_id": "{voice}", "speed": 1, "vol": 1,
                "pitch": 0, "emotion": "{emotion}",
            },
            "audio_setting": {
                "sample_rate": "{pcm_sr}", "format": "pcm", "channel": 1,
            },
        },
        "response": {"kind": "sse", "path": "data.audio", "encoding": "hex"},
        "pcm_sr": 24000,
        "fields": {
            "base_url": {
                "label": "Base URL", "type": "text",
                "default": "https://api.minimaxi.com",
                "help": "海外平台填 https://api.minimax.io",
            },
            "model": {
                "label": "模型", "type": "select", "default": "speech-2.8-hd",
                "options": [
                    ("speech-2.8-hd", "speech-2.8-hd（最佳质量）"),
                    ("speech-2.8-turbo", "speech-2.8-turbo（更快）"),
                    ("speech-2.6-hd", "speech-2.6-hd"),
                    ("speech-2.6-turbo", "speech-2.6-turbo"),
                ],
            },
            "voice": {
                "label": "音色", "type": "text", "default": "",
                "required": True, "help": "控制台或 /v1/get_voice 里的 voice_id",
            },
            "emotion": {
                "label": "情绪", "type": "select", "default": "calm",
                "options": [
                    ("calm", "平静"), ("happy", "开心"), ("sad", "难过"),
                    ("angry", "生气"), ("fearful", "害怕"),
                    ("disgusted", "厌恶"), ("surprised", "惊讶"),
                    ("fluent", "流畅"), ("whisper", "低语"),
                ],
            },
        },
        "tags": {
            "label": "MiniMax",
            "open": "(", "close": ")",
            "intro": "情绪真到了位，可以在正文里用半角英文圆括号插一个发声标签，比如「(laughs)哈哈哈」。",
            "words_intro": "只能用这几个，别自己造：",
            "outro": "一句话最多加一个，没必要就别加。别用中文圆括号当标签，也别写「（转身）」「（拿起水杯）」这种动作描写，动作归 `<ACT>` 管；发声标签别写进 `<ACT>` 或 `<scene>` 里。",
            "words": ["laughs", "sighs", "coughs", "gasps", "sniffs",
                      "chuckles", "yawns"],
        },
    },
    "cartesia": {
        "label": "Cartesia",
        "desc": "海外低延迟；44 语言、可克隆；按量计费",
        "doc": "https://play.cartesia.ai/",
        "wire": "http",
        "auth": {"kind": "bearer"},
        "path": "/tts/bytes",
        "headers": {"Cartesia-Version": "2026-08-14"},
        "body": {
            "model_id": "{model}", "transcript": "{text}",
            "voice": {"id": "{voice}"},
            "output_format": {
                "container": "raw", "encoding": "pcm_s16le",
                "sample_rate": "{pcm_sr}",
            },
        },
        "response": {"kind": "raw"},
        "pcm_sr": 24000,
        "fields": {
            "base_url": {
                "label": "Base URL", "type": "text",
                "default": "https://api.cartesia.ai",
            },
            "model": {
                "label": "模型", "type": "text", "default": "sonic-3.6",
            },
            "voice": {
                "label": "音色", "type": "text", "default": "",
                "required": True, "help": "在 Voice Library 复制 voice ID",
                "doc": "https://play.cartesia.ai/voices",
            },
        },
    },
    "elevenlabs": {
        "label": "ElevenLabs",
        "desc": "海外音色克隆标杆；需要海外网络环境",
        "doc": "https://elevenlabs.io/app/settings/api-keys",
        "wire": "http",
        "auth": {"kind": "header", "name": "xi-api-key"},
        "path": "/v1/text-to-speech/{voice}/stream",
        "query": {"output_format": "pcm_24000"},
        "body": {"text": "{text}", "model_id": "{model}"},
        "response": {"kind": "raw"},
        "pcm_sr": 24000,
        "fields": {
            "base_url": {
                "label": "Base URL", "type": "text",
                "default": "https://api.elevenlabs.io",
            },
            "model": {
                "label": "模型", "type": "text",
                "default": "eleven_multilingual_v2",
                "help": "v3/v4 才支持 [音频标签]；填控制台里的模型 id",
            },
            "voice": {
                "label": "音色", "type": "text", "default": "",
                "required": True, "help": "在 Voice Library 复制 voice_id",
                "doc": "https://elevenlabs.io/app/voice-library",
            },
        },
    },
    "aivis": {
        "label": "Aivis Cloud（日语）",
        "desc": "日语动漫音色；AivisSpeech 生态；按量计费",
        "doc": "https://aivis-project.com/cloud-api/",
        "wire": "http",
        "auth": {"kind": "bearer"},
        "path": "/v1/tts/synthesize",
        "body": {
            "model_uuid": "{voice}", "text": "{text}",
            "output_format": "wav", "output_sampling_rate": 24000,
            "output_audio_channels": "mono", "use_volume_normalizer": True,
        },
        "response": {"kind": "raw"},
        "audio": "wav",
        "fields": {
            "base_url": {
                "label": "Base URL", "type": "text",
                "default": "https://api.aivis-project.com",
            },
            "voice": {
                "label": "音色", "type": "text", "default": "",
                "required": True, "help": "在 AivisHub 复制 model_uuid",
                "doc": "https://hub.aivis-project.com",
            },
        },
    },
    "mimo": {
        "label": "小米 MiMo",
        "desc": "中文情感标签；chat/completions 形态；当前免费",
        "doc": "https://platform.xiaomimimo.com/",
        "wire": "http",
        "auth": {"kind": "header", "name": "api-key"},
        "path": "/chat/completions",
        "body": {
            "model": "{model}",
            "messages": [{"role": "assistant", "content": "{text}"}],
            "audio": {"format": "pcm", "voice": "{voice}"},
            "stream": True,
        },
        "response": {
            "kind": "sse", "path": "choices.0.delta.audio.data",
            "encoding": "base64", "end": "[DONE]",
        },
        "pcm_sr": 24000,
        "fields": {
            "base_url": {
                "label": "Base URL", "type": "text",
                "default": "https://api.xiaomimimo.com/v1",
            },
            "model": {
                "label": "模型", "type": "text", "default": "mimo-v2.5-tts",
                "help": "voiceclone 模型需要上传音色样本，暂不支持",
            },
            "voice": {
                "label": "音色", "type": "select", "default": "冰糖",
                "options": [
                    ("mimo_default", "默认"), ("冰糖", "冰糖"),
                    ("茉莉", "茉莉"), ("苏打", "苏打"), ("白桦", "白桦"),
                    ("Mia", "Mia"), ("Chloe", "Chloe"), ("Milo", "Milo"),
                    ("Dean", "Dean"),
                ],
            },
        },
        "tags": {
            "label": "MiMo",
            "open": "（", "close": "）",
            "intro": "情绪或语气真到了位，可以在正文里用全角中文圆括号插一个发声标签，比如「（小声）诶～？」。",
            "words_intro": "只能从这些词里挑，别自己造：",
            "outro": "一句话最多加一个，没必要就别加。括号里只放上面的发声词，别写成「（转身）」「（拿起水杯）」这种动作描写，动作归 `<ACT>` 管；发声标签也别写进 `<ACT>` 或 `<scene>` 里。",
            "words": [
                "停顿", "长停顿", "急促", "拖音", "语速加快", "语速放缓",
                "轻声", "低语", "叹气", "吸气", "哽咽", "强调", "笑",
                "爽朗大笑", "欲言又止", "碎碎念", "沉默片刻", "小声",
                "大喊", "耳语", "哭",
            ],
        },
    },
}


def get(preset_id: str) -> dict | None:
    return PRESETS.get(str(preset_id or "").strip())


def default_block(preset_id: str) -> dict:
    """该 preset 的默认 profile 块（`preset` + 字段默认值）。"""
    spec = PRESETS[preset_id]
    out = {"preset": preset_id}
    for key, meta in spec["fields"].items():
        out[key] = meta.get("default", "")
    return out


def field_items(preset_id: str) -> list[tuple[str, dict]]:
    return list(PRESETS[preset_id]["fields"].items())


def _tags(preset_id: str) -> dict | None:
    return PRESETS[preset_id].get("tags")


def prompt_rules(preset_id: str) -> str:
    """把标签数据拼成输出约束；没有标签的厂商返回空串。"""
    tags = _tags(preset_id)
    if not tags:
        return ""
    words = list(tags["words"])
    opener, closer = tags["open"], tags["close"]
    word_text = "、".join(
        f"{opener}{w}{closer}" if opener == "[" else w for w in words)
    return "\n".join([
        f"[发声标签（{tags['label']}）]",
        str(tags["intro"]),
        str(tags["words_intro"]) + word_text + "。",
        str(tags["outro"]),
    ])


def allowed_tags(preset_id: str) -> frozenset[str]:
    tags = _tags(preset_id)
    if not tags:
        return frozenset()
    values = set(tags["words"])
    mapping = tags.get("map") or {}
    values.update(mapping)
    values.update(mapping.values())
    return frozenset(values)


def preserve_square_tags(preset_id: str) -> frozenset[str]:
    tags = _tags(preset_id)
    if not tags or tags.get("open") != "[":
        return frozenset()
    values = {str(x).lower() for x in tags["words"]}
    values.update(str(x).lower() for x in (tags.get("map") or {}).values())
    return frozenset(values)


def adapt_text(preset_id: str, text: str) -> str:
    """把中文圆括号标签改写成厂商语法；没有 map 的厂商原样返回。"""
    tags = _tags(preset_id)
    if not tags or not tags.get("map"):
        return text
    mapping = tags["map"]
    opener, closer = tags["open"], tags["close"]

    def repl(match: re.Match) -> str:
        inner = match.group(1).strip()
        words = [w.strip() for w in re.split(r"[，,、\s]+", inner) if w.strip()]
        out = [mapping[w] for w in words if w in mapping]
        if not out:
            return ""
        if opener == "[":
            return opener + "][".join(out) + closer
        return opener + "，".join(out) + closer

    return re.sub(r"[（(]([^）)]+)[）)]", repl, str(text or ""))


def validate_profile(name: str, block: dict) -> list[str]:
    """校验一个 profile；错误路径带 `tts.<name>.` 前缀。"""
    errs: list[str] = []
    if str(name or "").strip().lower() == "none":
        errs.append(f"tts.{name}.名称 none 是保留值（NullSpeaker）")
        return errs
    if not PROFILE_NAME_RE.match(str(name or "")):
        errs.append(f"tts.{name}.名称只能是小写字母/数字/下划线/短横线（1–40 字）")
        return errs
    if not isinstance(block, dict):
        errs.append(f"tts.{name} 必须是配置块")
        return errs
    preset_id = str(block.get("preset") or "").strip()
    spec = PRESETS.get(preset_id)
    if spec is None:
        errs.append(f"tts.{name}.preset 不认识：{preset_id or '（空）'}")
        return errs
    for key, meta in spec["fields"].items():
        value = block.get(key)
        if value is None:
            value = meta.get("default", "")
        if meta.get("type") == "select":
            allowed = {str(v) for v, _ in meta.get("options", [])}
            if str(value) not in allowed:
                errs.append(f"tts.{name}.{key} 只能是 {' / '.join(sorted(allowed))}")
            continue
        if meta.get("required") and not str(value).strip():
            errs.append(f"tts.{name}.{key} 不能为空")
    if spec.get("wire") == "http":
        base = block.get("base_url")
        if base is None:
            base = spec["fields"]["base_url"].get("default", "")
        if not str(base).strip():
            errs.append(f"tts.{name}.base_url 不能为空")
    return errs
