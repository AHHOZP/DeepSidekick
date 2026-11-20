"""LLM 厂商预设：默认地址、模型和静态请求头。

这里**只描述厂商**，不发请求、不读配置；内核 `sidekick/brain/llm.py` 不 import
本模块，只认 profile 字段。本模块只被 `sidekick/config/spec.py`（生成默认配置）
和 `sidekick/webui/`（新建预填、启动闸门）读取。

每条 preset 的键：

    label      显示名（启动闸门文案）
    base_url   默认 Base URL；可以写到 `/v1`，也可以写完整 `/chat/completions`
    model      默认模型 id
    headers    可选，静态请求头（浏览器 UA / session 等）；不能放密钥型头
    needs_key  可选，默认 true；本地无鉴权服务写 false

空模板：

    "vendor": {
        "label": "Vendor",
        "base_url": "https://api.vendor.com/v1",
        "model": "model-id",
    },

新增一家 OpenAI 兼容厂商：

1. 复制一条改字段；base_url / model 只用实测过的值；
2. 跑 `python -m unittest discover -s tests`（目录测试会查必填、URL、类型、无密钥头）；
3. 重启 WebUI（Python 模块启动时加载）；
4. 模型页「Profile 名称」填 preset 名 → 「新建」→ 地址/模型/headers 自动带出。

不要改：`sidekick/brain/llm.py`、`sidekick/webui/form.py`、`sidekick/webui/static/app.js`、
`sidekick/webui/static/index.html`、`sidekick/config/spec.py`、测试文件。
"""
from __future__ import annotations

import copy

PRESETS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
    },
    "opencode": {
        "label": "OpenCode Go",
        "base_url": "https://opencode.ai/zen/go/v1",
        "model": "deepseek-v4.1-flash",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/141.0.0.0 Safari/537.36"
            ),
            "x-opencode-session": "deepsidekick-main",
        },
    },
}

DEFAULT_ID = "deepseek"
DEFAULT_GEN = {"temperature": 0.9, "reasoning_effort": "low"}


def get(name: str) -> dict | None:
    """按名字找 preset；大小写不敏感，找不到返回 None。"""
    key = str(name or "").strip().lower()
    for preset_id, spec in PRESETS.items():
        if preset_id.lower() == key:
            return spec
    return None


def profile_block(preset_id: str) -> dict:
    """生成默认 profile 块：连接信息来自 preset，生成参数来自 DEFAULT_GEN。"""
    spec = PRESETS[preset_id]
    return {
        "base_url": spec["base_url"],
        "model": spec["model"],
        "api_key": "",
        **copy.deepcopy(DEFAULT_GEN),
        "headers": copy.deepcopy(spec.get("headers") or {}),
        "body": {},
    }
