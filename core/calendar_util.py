"""日历换算：工作分钟 ↔ 真实时刻（手册 3.4 ②：单独写）。

约定（来自 config.py）：
- 每天 08:00 开工
- 2 班制：08:00–24:00，每天 16 小时；3 班制：08:00–次日 08:00，24 小时
- 周日不排产

排程内部一律用真实 datetime 计算，对外展示再格式化，
避免"工作分钟轴"和"日历"两套坐标混用——这是甘特图日期错位的根因。
"""

from datetime import datetime, time, timedelta

DAY_START = 8          # 每天开工小时
SUNDAY = 6             # weekday()==6 即周日


def is_working(dt, shifts):
    """判断某一时刻该机台是否在开工。"""
    if shifts >= 3:                       # 3 班制：只有周日 08:00 到周一 08:00 停
        if dt.weekday() == SUNDAY and dt.hour >= DAY_START:
            return False
        if dt.weekday() == 0 and dt.hour < DAY_START:
            return False
        return True
    if dt.weekday() == SUNDAY:            # 2 班制：周日全天停
        return False
    return DAY_START <= dt.hour < 24       # 00:00–08:00 停


def ceil_to_work(dt, shifts):
    """把时刻推进到最近的可用开工时刻（不早于 dt）。"""
    cur = dt
    for _ in range(32):
        if shifts >= 3:
            if cur.weekday() == SUNDAY and cur.hour >= DAY_START:
                cur = (cur + timedelta(days=1)).replace(hour=DAY_START, minute=0, second=0, microsecond=0)
                continue
            if cur.weekday() == 0 and cur.hour < DAY_START:
                cur = cur.replace(hour=DAY_START, minute=0, second=0, microsecond=0)
                continue
            return cur
        if cur.weekday() == SUNDAY:
            cur = (cur + timedelta(days=1)).replace(hour=DAY_START, minute=0, second=0, microsecond=0)
            continue
        if cur.hour < DAY_START:
            cur = cur.replace(hour=DAY_START, minute=0, second=0, microsecond=0)
            continue
        return cur
    return cur


def window_end(dt, shifts):
    """返回 dt 所在工作窗口的结束时刻。"""
    if shifts >= 3:
        if dt.hour < DAY_START:           # 仍在昨天 08:00 开的窗口里
            return dt.replace(hour=DAY_START, minute=0, second=0, microsecond=0)
        return (dt + timedelta(days=1)).replace(hour=DAY_START, minute=0, second=0, microsecond=0)
    return (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def add_work_minutes(dt, minutes, shifts):
    """从 dt 起算，往后消耗 minutes 个工作分钟，返回结束时刻（会跳过非工作时段）。"""
    cur = ceil_to_work(dt, shifts)
    rem = float(minutes)
    for _ in range(5000):
        if rem <= 1e-6:
            break
        end = window_end(cur, shifts)
        avail = (end - cur).total_seconds() / 60.0
        take = min(avail, rem)
        cur = cur + timedelta(minutes=take)
        rem -= take
        if rem > 1e-6:
            cur = ceil_to_work(cur, shifts)
    return cur


def work_minutes_between(a, b, shifts):
    """统计 a→b 之内真正在开工的分钟数，用于校验器复核工时。"""
    if b <= a:
        return 0.0
    total, cur = 0.0, a
    for _ in range(5000):
        if cur >= b:
            break
        start = ceil_to_work(cur, shifts)
        if start >= b:
            break
        end = min(window_end(start, shifts), b)
        total += (end - start).total_seconds() / 60.0
        cur = end if end > cur else cur + timedelta(minutes=1)
    return total


def fmt(dt):
    """统一展示格式：11-09 18:20。"""
    if dt is None:
        return "—"
    return dt.strftime("%m-%d %H:%M")


def fmt_full(dt):
    """带年份的完整格式，用于参数行与报表头。"""
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M")


def next_monday(day):
    """取 day 当天或之后最近的周一（本周起始日，可推导参数，不让客户填）。"""
    base = day + timedelta(days=(7 - day.weekday()) % 7)
    if isinstance(base, datetime):
        return base.replace(hour=DAY_START, minute=0, second=0, microsecond=0)
    return datetime.combine(base, time(DAY_START))
