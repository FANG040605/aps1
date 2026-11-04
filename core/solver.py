"""规则排程：FIFO / SPT / MWKR 各算一遍，取总完工最短的一版。

为什么不让 AI 直接排：约束会漏。这里是确定性的贪心列表排程——
按规则决定订单顺序，逐单逐工序挑"最早能结束的机台"，同样的输入必然得到同样的输出。
排完不自己说"OK"，交给 checker 复核。
"""

from datetime import date

import config
from . import calendar_util as cal

FAR_FUTURE = date(2099, 12, 31)


def schedule(tasks, machines, start_dt):
    """三套规则各排一次，返回 makespan 最短的结果。"""
    best = None
    for rule in config.DISPATCH_RULES:
        rows = _run(tasks, machines, start_dt, rule)
        span = max((r["end"] for r in rows), default=start_dt)
        if best is None or span < best["makespan"]:
            best = {"rows": rows, "makespan": span, "rule": rule}
    return best


def _run(tasks, machines, start_dt, rule):
    """按规则顺序逐单排，每道工序取最早能结束的机台。"""
    busy, rows = {}, []
    for job in _ordered(_group(tasks), rule):
        ready = start_dt
        for task in job["tasks"]:
            t0 = max(ready, task["earliest"]) if task["earliest"] else ready
            start, end, mid = _pick(task, busy, machines, t0)
            busy.setdefault(mid, []).append((start, end))
            rows.append(_row(task, mid, start, end))
            ready = end
    return rows


def _row(task, mid, start, end):
    return {"machine": mid, "order": task["order_id"], "seq": task["seq"],
            "op": task["op"], "qty": task["qty"], "per_piece": task["per_piece"],
            "minutes": task["minutes"], "start": start, "end": end,
            "product": task["product"]}


def _pick(task, busy, machines, t0):
    """在候选机台里挑"最早结束"的那台（同结束取编号小的，保证确定性）。"""
    best = None
    for mid in task["machines"]:
        shifts = machines[mid]["shifts"]
        start, end = _slot(busy.get(mid, []), t0, task["minutes"], shifts)
        if best is None or (end, mid) < (best[1], best[2]):
            best = (start, end, mid)
    if best is None:
        raise RuntimeError(f"{task['order_id']} 第 {task['seq']} 道「{task['op']}」没有可用机台")
    return best


def _slot(intervals, t0, minutes, shifts):
    """在已占用区间之外，找最早能放下 minutes 的空档。"""
    cand = cal.ceil_to_work(t0, shifts)
    for _ in range(len(intervals) + 16):
        end = cal.add_work_minutes(cand, minutes, shifts)
        clash = next((iv for iv in intervals if cand < iv[1] and iv[0] < end), None)
        if clash is None:
            return cand, end
        cand = cal.ceil_to_work(max(cand, clash[1]), shifts)
    raise RuntimeError("机台冲突无法消解，请检查日历参数")


def _group(tasks):
    """按订单分组，组内按工序顺序排列。"""
    jobs = {}
    for t in tasks:
        jobs.setdefault(t["order_id"], {"order": None, "tasks": [], "total": 0.0})
        jobs[t["order_id"]]["tasks"].append(t)
        jobs[t["order_id"]]["total"] += t["minutes"]
    for job in jobs.values():
        job["tasks"].sort(key=lambda x: x["seq"])
    return jobs


def _ordered(jobs, rule):
    """按规则给订单排序：急单永远在前，其次才看规则。"""
    def key(item):
        oid, job = item
        urgent = 0 if job["tasks"][0].get("urgent") else 1
        if rule == "SPT":
            return (urgent, job["total"], oid)
        if rule == "MWKR":
            return (urgent, -job["total"], oid)
        recv = job["tasks"][0].get("recv") or FAR_FUTURE
        return (urgent, recv, oid)

    return [job for _, job in sorted(jobs.items(), key=key)]
