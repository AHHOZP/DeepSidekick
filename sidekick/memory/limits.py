"""记忆子系统的写死上限。

这些是“异常兜底”，正常对话碰不到，因此不进配置、不进 WebUI。
可调参数（条数、阈值、超时）在 `sidekick/config/spec.py`；这里只放
不会因机器/玩家习惯而变化的常量，避免同一数字散落在多个模块。
"""
from __future__ import annotations

#: 每个池强相关最多召回几条（病态上限，防止一次注入几百条）。
HARD_MAX_ITEMS = 16

#: 单条 summary 的关键词超过这个数才触发统一审核剪枝。
KEYWORD_AUDIT_AT = 32

#: 每个关键词命中给关键词候选加的分数。
KEYWORD_HIT_WEIGHT = 0.02

#: 总结连续失败达到这个次数后，恢复 40 条窗口并开始丢弃最老历史。
SUMMARY_MAX_FAILURES = 5

#: 总结失败期间，prompt 历史最多额外保留的消息数（10 条 = 5 轮对话）。
SUMMARY_HISTORY_EXTRA = 10

#: 长文本编辑器提示阈值；只提示，不截断。
WORLDBOOK_WARN_CHARS = 6000

#: 统一审核：温度与尝试次数。
AUDIT_TEMPERATURE = 0.0
AUDIT_ATTEMPTS = 1
