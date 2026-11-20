"""识别引擎层（ASR）。

分工：
  · `sidekick/input/stt/__init__.py` 是门面（管道）：选引擎、降级、后处理、统计。
  · 本包只放引擎本身，一个 kind 一个分支。

新增模型族的正确姿势是在 `registry.build` 里加一行分支 + 在
`catalog.py` 里加一条记录，**不要**再写一个 adapter 类。
"""
from __future__ import annotations
