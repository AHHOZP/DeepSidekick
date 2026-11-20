"""记忆时间的显示格式：`11月14日 20:40–21:10`。

模型没有日历，只能靠注入的绝对日期算“前天/上周”；格式只在这里维护一份。
"""
from __future__ import annotations

import time

_WEEK = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _parts(ts: float) -> time.struct_time:
    return time.localtime(float(ts))


def format_day(ts: float | None = None, now: float | None = None) -> str:
    """`2026-11-14 周五`；跨年时带年份。"""
    ts = time.time() if ts is None else float(ts)
    now = time.time() if now is None else float(now)
    lt = _parts(ts)
    year = f"{lt.tm_year}年" if lt.tm_year != _parts(now).tm_year else ""
    return f"{year}{lt.tm_mon}月{lt.tm_mday}日 {_WEEK[lt.tm_wday]}"


def format_span(start: float, end: float | None = None,
                now: float | None = None) -> str:
    """时间范围；同一天合并日期，跨天两边都写。"""
    start = float(start or 0)
    end = float(end or 0)
    now = time.time() if now is None else float(now)
    if start <= 0 and end <= 0:
        return "时间不详"
    if start <= 0:
        start = end
    if end <= 0:
        end = start
    if end < start:
        start, end = end, start
    a, b = _parts(start), _parts(end)
    same_year = a.tm_year == _parts(now).tm_year
    year_a = "" if same_year else f"{a.tm_year}年"
    year_b = "" if same_year else f"{b.tm_year}年"
    day_a = f"{year_a}{a.tm_mon}月{a.tm_mday}日"
    day_b = f"{year_b}{b.tm_mon}月{b.tm_mday}日"
    if (a.tm_year, a.tm_mon, a.tm_mday) == (b.tm_year, b.tm_mon, b.tm_mday):
        span = f"{a.tm_hour:02d}:{a.tm_min:02d}"
        if end - start >= 60:
            span += f"–{b.tm_hour:02d}:{b.tm_min:02d}"
        return f"{day_a} {span}"
    return (f"{day_a} {a.tm_hour:02d}:{a.tm_min:02d}–"
            f"{day_b} {b.tm_hour:02d}:{b.tm_min:02d}")
