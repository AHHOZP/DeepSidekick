"""Spine 原生适配层。

这里只有和 Spine 运行时打交道的东西：ctypes 绑定（`binding`）、`.atlas` 文本解析（`atlas`）、
动画名探针（`names`）。**不 import pygame / OpenGL**，也不在包初始化时加载 DLL——
`__init__.py` 必须留空，否则 runtime 进程一 import 包就把原生库拖进来。
"""
