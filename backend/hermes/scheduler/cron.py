"""
hermes/scheduler/cron.py —— 极简 Cron 表达式解析（零依赖）

为什么不引 APScheduler：
本机与工厂内网部署常无外网，多一个依赖就多一个装不上的理由。
5 字段 cron 的匹配逻辑本身只有几十行，自己实现可控、可测、可打包。

支持语法（每字段）：
    *        任意值
    */n      每 n 个单位
    a-b      区间
    a-b/n    区间内每 n 个
    a,b,c    枚举
    a        单值

字段顺序：分 时 日 月 周
    分 0-59 / 时 0-23 / 日 1-31 / 月 1-12 / 周 0-6（0=周日，7 视为 0）
"""
from datetime import datetime, timedelta
from typing import List, Optional

_FIELDS = ("minute", "hour", "day", "month", "weekday")

_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "day": (1, 31),
    "month": (1, 12),
    "weekday": (0, 7),
}


class CronParseError(ValueError):
    pass


def _parse_field(expr: str, name: str) -> List[int]:
    lo, hi = _RANGES[name]
    values = set()

    for part in str(expr).strip().split(","):
        part = part.strip()
        if not part:
            raise CronParseError(f"{name} 字段存在空项: {expr!r}")

        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) <= 0:
                raise CronParseError(f"{name} 步长非法: {step_s!r}")
            step = int(step_s)
            part = part.strip() or "*"

        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.strip().isdigit() and b.strip().isdigit()):
                raise CronParseError(f"{name} 区间非法: {part!r}")
            start, end = int(a), int(b)
        else:
            if not part.isdigit():
                raise CronParseError(f"{name} 取值非法: {part!r}")
            start = end = int(part)

        if start < lo or end > hi or start > end:
            raise CronParseError(f"{name} 超出范围 [{lo},{hi}]: {part!r}")

        values.update(range(start, end + 1, step))

    if name == "weekday" and 7 in values:
        values.add(0)
        values.discard(7)
    return sorted(values)


class CronExpression:
    """5 字段 cron 表达式：matches(dt) / next_after(dt)"""

    def __init__(self, expr: str):
        self.expr = " ".join(str(expr or "").split())
        parts = self.expr.split(" ")
        if len(parts) != 5:
            raise CronParseError(f"cron 表达式必须为 5 字段（分 时 日 月 周），收到: {expr!r}")
        self.minute = _parse_field(parts[0], "minute")
        self.hour = _parse_field(parts[1], "hour")
        self.day = _parse_field(parts[2], "day")
        self.month = _parse_field(parts[3], "month")
        self.weekday = _parse_field(parts[4], "weekday")

    def matches(self, dt: datetime) -> bool:
        """判断给定时间（精确到分钟）是否命中。周日=0，Python weekday() 为 周一=0。"""
        py_wd = dt.weekday()                 # 周一=0 ... 周日=6
        cron_wd = (py_wd + 1) % 7            # 周日=0 ... 周六=6
        return (dt.minute in self.minute
                and dt.hour in self.hour
                and dt.day in self.day
                and dt.month in self.month
                and cron_wd in self.weekday)

    def next_after(self, dt: datetime, max_days: int = 14) -> Optional[datetime]:
        """下一次命中的时间（分钟粒度）；max_days 内无命中返回 None。"""
        cur = (dt + timedelta(minutes=1)).replace(second=0, microsecond=0)
        limit = cur + timedelta(days=max_days)
        while cur < limit:
            if self.matches(cur):
                return cur
            cur += timedelta(minutes=1)
        return None

    def __repr__(self) -> str:
        return f"CronExpression({self.expr!r})"


def validate(expr: str) -> str:
    """校验表达式，返回规范化字符串；非法则抛 CronParseError。"""
    return CronExpression(expr).expr
