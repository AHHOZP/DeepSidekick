"""LLM 生成 motion_profile 的 prompt；不包含密钥/绝对路径/曲线点。

V6 的输入不是“五百个参数”，而是程序先算出的高密度证据摘要：
能力/绑定/状态、精选未映射参数、模型自带 motion/expression、VTube 热键。
LLM 输出完整 JSON，生成侧再与确定性 baseline 合并。
"""
from __future__ import annotations

import json

from sidekick.avatar.act_tags import GAZE_POINTS, vocabulary
from sidekick.avatar.autonomy.schema import catalog_for_prompt, tuning_defaults
from sidekick.avatar.autonomy.schema import PROFILE_SCHEMA

_ALLOWED = {
    "emotions": [x["name"] for x in vocabulary()["emotions"]],
    "actions": [x["name"] for x in vocabulary()["actions"]],
    "gaze": list(GAZE_POINTS),
}

SYSTEM = """你是 Live2D 自主驱动配置规划器。只输出一个合法 JSON 对象，禁止输出任何解释、Markdown 代码块或多余文本。

[BASELINE 合并规则]
输入里的 BASELINE 是程序根据模型文件、几何和物理证据确定的骨架。你必须：
- 完整保留 BASELINE 的 channel_bindings、states 和所有 enabled 动作；
- 只做追加或调优，不要因为“没看到”就关闭模型的腿、尾巴、前倾、换装或手型能力；
- 输出符合 PROFILE_SCHEMA 的完整 JSON，供生成侧与 BASELINE 合并。

[全局硬约束]
1. 白名单限制：只能使用输入中给出的 param IDs、motion group、expression name 和 ALLOWED 词表，不得发明；不要修改模型文件，不要输出路径。
2. 通道存在性：如果模型没有某通道，不要绑定，改用降级思路选择可用动作。

[字段级硬约束]
3. archetype：
   - 仅取 chibi / standard / realistic / creature / unknown。
   - 无法判断头身比时写 "unknown"，不要编造 head_ratio。
4. tuning：
   - 键名和范围严格遵循 schema（超出范围会被 clamp）。
   - tuning.body_gain 是全身身体通道的全局倍率，基准 1.0；不要写 0.13 这类旧的“注视分摊”值。
5. expressions：
   - key 只能取 ALLOWED.emotions；value 必须是 MODEL_DOSSIER.expressions 中 kind=expression 的精确 name。
   - persistent / reset 表情不是情绪，不要写进 expressions。
6. aliases（未映射参数）：
   - key 必须是 MODEL_DOSSIER.unmapped.rows 或 ALLOWED.param_ids 中的精确 param ID；value 必须是语义通道名（如 head_x / arm_l / tail / lean_y），不能写成另一个 param ID，不能自映射。
   - channel_bindings 已经绑定的通道不要再写进 aliases；没有额外参数就写 {}。
   - 筛选优先级：优先依据 MODEL_DOSSIER.unmapped.rows 的证据（有 cdi3 name、vtube_channel 或探针 span_ratio > 0.02）；纯数字且无上述证据的参数不要猜。
7. actions 与能力保留：
   - 只能从 ACTION_CATALOG 或 BASELINE 的自定义动作中选择，或补充自定义配方：aliases（中英文触发词）、pose（语义通道到 -1.5..1.5 的目标值）、loops（channel: [幅度, 频率Hz, 相位]）、states（离散状态名到布尔值）；配方只能使用 MODEL_DOSSIER 中真实存在的参数/通道。
   - 有 leg_ / foot_ 通道时，actions 必须保留 kick 和 leg_shift，并在 prompt 中说明可以踢腿/换重心；有 hip 通道时必须保留 sway_hip（这些是让模型真正动起来的主通道，不是装饰）。
   - MODEL_DOSSIER.states、by_channel、vtube 和 BASELINE 中真实存在的尾巴、前倾、换装、手型，必须在 actions 和 prompt 能力说明中保留，不要否认模型真实能力；优先为这些能力写出能让 LLM 真实调用的动作。
   - 有 chibi_mode / long_skirt 这类持久状态时，必须同时保留反向恢复动作（如 正常比例 关闭 Q 版、穿短裙 关闭长裙）；持久状态不能只靠模型 expression 恢复（expression 会过期回弹）。
8. motion_policy：
   - mode 固定为 "auto"。
   - 优先保留模型自带 motion / expression，没有资产时才用程序化。
   - 模型自带 motion 的 top_params 只说明它在动什么，不代表一定要拆成程序动作；能直接播就写进 motion_policy.model_motions，仅在需要与说话叠加时才写自定义动作。
9. prompt：
   - emotion_tags / action_tags / gaze_tags 只能取 ALLOWED 中的名字；不要把模型原生 motion 或 expression 资产名当作 emotion= / action= 的取值。
   - 模型没有 expressions 资产不等于没有情绪：emotion= 仍会驱动程序化面部和身体，必须保留 prompt.emotion_tags，禁止写“没有模型表情所以不要输出 emotion”的规则，也不要因此禁用 emotion 标签。
   - prompt.rules 需明确：长回复可按分句放多个 <ACT>，但一条回复最多 6 个意图，不要每个分句都放；持久状态成对恢复；不否认模型真实身体能力。
10. warnings：
    - 将所有不确定项写入 warnings 数组。

[脱敏示例（仅示范写法，不是本模型数据）]
- 数字参数别名："aliases": {"Param50": "lean_y"}（禁止写成 "Param50": "Param50"）。
- 腿模型自定义动作："sway_hip": {"aliases": ["扭腰", "晃腰"], "pose": {"hip": 0.35}, "loops": {"body_z": [0.18, 0.45, 0.0]}}（没腿/没 hip 的模型不要写）。

[你的任务]
- 判断 archetype（chibi / standard / realistic / creature / unknown）；
- 给出 tuning；
- 选择或补充 actions；
- 决定 motion_policy（mode 为 auto，配置模型 motion 的启用/禁用/映射）；
- 给出 prompt 的标签词表与规则；
- 在 warnings 里写明不确定项。"""


def compact_dossier(dossier: dict) -> dict:
    """Keep high-value evidence, drop the 500-row parameter dump."""
    data = dossier or {}
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    probe = data.get("probe") if isinstance(data.get("probe"), dict) else {}
    physics = data.get("physics") if isinstance(data.get("physics"), dict) else {}
    vtube = data.get("vtube") if isinstance(data.get("vtube"), dict) else {}
    motions = []
    for item in data.get("motions") or []:
        if not isinstance(item, dict):
            continue
        motions.append({
            "group": str(item.get("group") or ""),
            "duration": item.get("duration", 0.0),
            "loop": bool(item.get("loop", False)),
            "param_count": int(item.get("param_count", 0) or 0),
            "top_params": list(item.get("top_params") or [])[:3],
        })
    expressions = []
    for item in data.get("expressions") or []:
        if not isinstance(item, dict):
            continue
        expressions.append({
            "name": str(item.get("name") or ""),
            "kind": str(item.get("kind") or "expression"),
            "param_count": int(item.get("param_count", 0) or 0),
            "controls": [str(row.get("id") or "")
                         for row in item.get("controls") or []
                         if isinstance(row, dict) and row.get("id")][:6],
        })
    unmapped = data.get("unmapped") if isinstance(data.get("unmapped"), dict) else {}
    rows = [row for row in unmapped.get("rows") or []
            if isinstance(row, dict)][:40]
    return {
        "model": {
            "name": str(model.get("name") or ""),
            "entry": str(model.get("entry") or ""),
            "canvas": list(model.get("canvas") or []),
            "tier": str(data.get("tier") or "none"),
        },
        "capabilities": {k: v for k, v in (data.get("capabilities") or {}).items()
                         if v},
        "by_channel": dict(data.get("by_channel") or {}),
        "states": dict(data.get("states") or {}),
        "physics": {
            "present": bool(physics.get("present")),
            "inputs_count": len(physics.get("inputs") or []),
            "outputs_count": len(physics.get("outputs") or []),
        },
        "probe": {
            "available": bool(probe.get("available")),
            "error": str(probe.get("error") or ""),
            "count": len(probe.get("params") or {}),
        },
        "vtube": {
            "present": bool(vtube.get("present")),
            "params_count": len(vtube.get("params") or []),
            "hotkeys": [
                {"name": str(item.get("name") or ""),
                 "action": str(item.get("action") or "")}
                for item in (vtube.get("hotkeys") or [])[:40]
                if isinstance(item, dict)
            ],
            "idle": str(vtube.get("idle") or ""),
        },
        "motions": motions[:40],
        "expressions": expressions[:40],
        "pose": dict(data.get("pose") or {}),
        "unmapped": {"count": int(unmapped.get("count", 0) or 0), "rows": rows},
        "warnings": [str(x) for x in (data.get("warnings") or [])][:20],
    }


def _allowed(dossier: dict, baseline: dict | None = None) -> dict:
    params = {
        str(x.get("id"))
        for x in (dossier.get("params") or [])
        if isinstance(x, dict) and x.get("id")
    }
    for row in ((dossier.get("unmapped") or {}).get("rows") or []):
        if isinstance(row, dict) and row.get("id"):
            params.add(str(row["id"]))
    params.update(str(row.get("param_id"))
                  for row in (dossier.get("states") or {}).values()
                  if isinstance(row, dict) and row.get("param_id"))
    for item in dossier.get("expressions") or []:
        if not isinstance(item, dict):
            continue
        params.update(str(control.get("id"))
                      for control in item.get("controls") or []
                      if isinstance(control, dict) and control.get("id"))
    actions = set(_ALLOWED["actions"])
    if baseline:
        actions.update(str(name) for name in (baseline.get("actions") or {}))
    return {
        **_ALLOWED,
        "expressions": [
            str(x.get("name"))
            for x in (dossier.get("expressions") or [])
            if isinstance(x, dict) and x.get("name")
            and str(x.get("kind") or "expression") == "expression"
        ],
        "param_ids": sorted(params),
        "actions": sorted(actions),
    }


def _baseline_summary(baseline: dict | None) -> dict:
    if not isinstance(baseline, dict):
        return {}
    actions = {
        name: row for name, row in (baseline.get("actions") or {}).items()
        if isinstance(row, dict) and row.get("enabled", True)
    }
    return {
        "channel_bindings": dict(baseline.get("channel_bindings") or {}),
        "states": dict(baseline.get("states") or {}),
        "actions": actions,
        "expressions": dict(baseline.get("expressions") or {}),
        "motion_policy": dict(baseline.get("motion_policy") or {}),
    }


def stage1_messages(dossier: dict, preferences: str = "",
                    baseline: dict | None = None) -> list[dict]:
    prompt = {
        "MODEL_DOSSIER": compact_dossier(dossier),
        "BASELINE": _baseline_summary(baseline),
        "ACTION_CATALOG": catalog_for_prompt(),
        "DEFAULT_TUNING": tuning_defaults("unknown"),
        "PROFILE_SCHEMA": PROFILE_SCHEMA,
        "ALLOWED": _allowed(dossier, baseline),
        "USER_PREFERENCES": str(preferences or "").strip(),
    }
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ]


def repair_messages(dossier: dict, draft: dict, issues: list[str],
                    baseline: dict | None = None) -> list[dict]:
    payload = {
        "MODEL_DOSSIER": compact_dossier(dossier),
        "BASELINE": _baseline_summary(baseline),
        "ALLOWED": _allowed(dossier, baseline),
        "CURRENT_DRAFT": draft,
        "ISSUES": issues[:30],
        "INSTRUCTION": "只改 ISSUES 里指出的问题，其余没毛病的字段和 BASELINE 原样保留；直接返回单个完整合法的 JSON 对象，别包代码块，也别写解释。",
    }
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def extract_json(text: str) -> dict | None:
    """从可能带 Markdown/前后缀的输出里找第一个完整 JSON 对象。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


__all__ = [
    "SYSTEM", "compact_dossier", "extract_json",
    "repair_messages", "stage1_messages",
]
