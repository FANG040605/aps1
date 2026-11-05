"""设备状态事件：故障 / 保养 / 停机 / 恢复。

为什么单独记事件，而不是直接改设备表里的"状态"列？
- 设备表记的是"这台机器是什么、能干什么"，是长期属性；
  "周三上午主轴坏了、周五四点修好"是临时状态，两件事混在一列里必然出事：
  修好之后忘了改回来，那台机器就永远排不上活；改回来时又把"每周六保养"这类
  长期信息一起抹掉。
- 故障要能追溯："上周为什么这批单排成这样"——翻流水比翻脑记可靠。
- 恢复不用改数据，只记一条"恢复"把之前的停机截断，基线的历史不被破坏。

存储是 data/设备事件.json（流水，追加不改写），排产时实时叠加到基线上。
"""

import json
import os
import uuid
from datetime import datetime, timedelta

import config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_NAME = "设备事件.json"

# 会占住机台的类型；"恢复"是动作，不是状态
DOWN_KINDS = ("故障", "保养", "停机")
KINDS = DOWN_KINDS + ("恢复",)


def default_path():
    return os.path.join(HERE, "data", DEFAULT_NAME)


def load(path=None):
    path = path or default_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        # 文件被手工改坏时不要让整批排产挂掉：当作没有事件，排产照常
        return []


def save(items, path=None):
    path = path or default_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    return path


def add(machine, kind, until=None, note="", start=None, path=None, now=None):
    """登记一条设备事件，返回这条事件。

    kind=故障/保养/停机 → 这台机从 start（默认现在）到 until 之间不排活；
    until 留空表示"归期未定"，排产时按整个计划期不可用算——
    宁可报"这单要等修好"，也不要排出个假计划让人以为能按期。
    """
    if not machine:
        raise ValueError("缺少机台编号")
    if kind not in KINDS:
        raise ValueError(f"事件类型只能是 {'/'.join(KINDS)}")
    now = now or datetime.now()
    items = load(path)
    if kind == "恢复":
        return recover(machine, path=path, now=now, note=note, items=items)
    event = {
        "id": now.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4],
        "machine": machine,
        "kind": kind,
        "from": (start or now).strftime("%Y-%m-%d %H:%M"),
        "until": until.strftime("%Y-%m-%d %H:%M") if until else None,
        "note": note.strip(),
        "created": now.strftime("%Y-%m-%d %H:%M"),
    }
    items.append(event)
    save(items, path)
    return event


def recover(machine, path=None, now=None, note="", items=None):
    """机台修好了：把该机器所有还没结束的停机截断到此刻。

    不改动历史记录（"上周停过三天"这段事实要留着），只补一个结束时间。

    还没开始的登记直接作废删掉：提前登记了"明天故障、停到周五"，
    结果今天下午就好了，这条事件留着会生成"8:00 停到昨天 17:00"这种倒置区间，
    后面的排产就得绕着一段负时间长度的坑走。
    """
    now = now or datetime.now()
    items = items if items is not None else load(path)
    hit, drop = 0, []
    for ev in items:
        if ev.get("machine") != machine or ev.get("kind") not in DOWN_KINDS:
            continue
        begin = parse_time(ev.get("from"))
        if begin is not None and begin >= now:        # 还没开始就恢复 → 整条作废
            drop.append(ev)
            hit += 1
            continue
        end = parse_time(ev.get("until"))
        if end is None or end > now:
            ev["until"] = now.strftime("%Y-%m-%d %H:%M")
            ev["recovered"] = now.strftime("%Y-%m-%d %H:%M")
            if note:
                ev["note"] = (ev.get("note", "") + f"；{note}").strip("；")
            hit += 1
    if hit:
        if drop:
            items = [e for e in items if e not in drop]
        save(items, path)
    return hit


def remove(event_id, path=None):
    """撤销一条登记错的事件。"""
    items = load(path)
    left = [e for e in items if e.get("id") != event_id]
    if len(left) == len(items):
        return False
    save(left, path)
    return True


# ---------------- 叠加到排产 ----------------

def windows(items, start_dt, horizon_days=None):
    """事件 → 各机台的不可用区间 {code: [(起, 止), ...]}，已按时间合并。

    只保留跟本次排产窗口有交集的部分：上个月的故障不该影响今天的计划。
    """
    horizon = start_dt + timedelta(days=horizon_days or config.PLAN_HORIZON_DAYS)
    out = {}
    for ev in items:
        if ev.get("kind") not in DOWN_KINDS:
            continue
        code = ev.get("machine")
        if not code:
            continue
        begin = parse_time(ev.get("from")) or start_dt
        end = parse_time(ev.get("until")) or horizon       # 没写恢复时间 = 计划期内一直停
        if end <= start_dt or begin >= horizon:
            continue
        begin, end = max(begin, start_dt), min(end, horizon)
        if end <= begin:                            # 倒置区间（登记错/已提前恢复）
            continue
        out.setdefault(code, []).append((begin, end))
    return {code: _merge(rows) for code, rows in out.items()}


def _merge(rows):
    """把重叠/首尾相接的区间并起来，减少求解器要绕的障碍物。"""
    rows = sorted(rows)
    out = []
    for begin, end in rows:
        if out and begin <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((begin, end))
    return out


def stopped_whole(items, start_dt, horizon_days=None):
    """整段计划期都停着的机台：直接从候选池里拿掉。

    留着它们只会让求解器把活排到半年后，排出一张没法执行的表。
    """
    horizon = start_dt + timedelta(days=horizon_days or config.PLAN_HORIZON_DAYS)
    out = set()
    for code, rows in windows(items, start_dt, horizon_days).items():
        if any(begin <= start_dt and end >= horizon for begin, end in rows):
            out.add(code)
    return out


def ongoing(items, now=None):
    """此刻正在停机的机台：{编号: 预计恢复时刻或 None}，给页面显示红点用。"""
    now = now or datetime.now()
    out = {}
    for ev in items:
        if ev.get("kind") not in DOWN_KINDS:
            continue
        begin, end = parse_time(ev.get("from")), parse_time(ev.get("until"))
        if begin and begin <= now and (end is None or end > now):
            code = ev.get("machine")
            latest = out.get(code)
            if latest is None or (end and (latest is None or end > latest)):
                out[code] = end
    return out


def describe(items, now=None):
    """一句话说清现在有几台在停、停到什么时候，写进参数行。"""
    now = now or datetime.now()
    live = ongoing(items, now)
    if not live:
        return "无停机事件"
    parts = []
    for code in sorted(live):
        end = live[code]
        parts.append(f"{code} 停至 {end:%m-%d %H:%M}" if end else f"{code} 归期未定")
    return "、".join(parts)


def parse_time(text):
    """事件里的时间字符串 → datetime；认 2026-11-08 / 2026-11-08 08:00 / ISO。"""
    if not text:
        return None
    if isinstance(text, datetime):
        return text
    s = str(text).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            got = datetime.strptime(s, fmt)
            if fmt in ("%Y-%m-%d", "%Y/%m/%d"):
                got = got.replace(hour=config.DAY_START_HOUR)   # 只说日期就是从当天开工算
            return got
        except ValueError:
            continue
    return None
