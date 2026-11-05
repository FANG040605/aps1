"""规则排程 + 交期驱动的局部改进。

上一版只按"总完工最短"挑方案，完全不认交期。同一批台账，只把订单顺序换一换，
迟到就能从 10 单降到 4 单，而总完工只差 16 小时——工厂要的是"少迟单"，
不是"早点收工"。所以目标函数必须先把交期放前面。

评分口径（字典序，前一项相同才比后一项）：
1) 迟到单数 —— 越少越好
2) 累计迟到天数 —— 越少越好
3) 总完工时刻 —— 越早越好
4) 排程条数 —— 兜底，保证两个方案永远可比较

做法：先用 6 套规则各排一遍取最优，再在"订单顺序"上做插入式局部搜索。
搜索是确定性的：固定遍历顺序、固定评估次数预算，不看时钟、不用随机数，
同样的输入必然得到同样的输出——这是甲方敢拿这张表去开生产会的前提。
"""

import math
from datetime import date, datetime, time

import config
from . import calendar_util as cal

FAR_FUTURE = date(2099, 12, 31)
NO_DUE = "无交期"


def schedule(tasks, machines, start_dt, down=None):
    """先用多套规则各排一遍，再对最好的那个顺序做局部改进。

    down = {机台: [(起, 止), …]} 是故障/保养造成的停机区间，
    排产时当成"这段时间已经被占住"，机器自然会绕开。
    """
    jobs = _group(tasks)
    if not jobs:
        return {"rows": [], "makespan": start_dt, "rule": "无任务",
                "score": (0, 0.0, start_dt, 0), "order": [], "evals": 0, "moves": 0}
    due = {oid: _due_dt(job["tasks"][0].get("due")) for oid, job in jobs.items()}
    best = None
    for rule in config.DISPATCH_RULES:
        order = _ordered(jobs, rule, start_dt)
        cand = _evaluate(order, rule, jobs, machines, start_dt, due, down)
        if best is None or cand["score"] < best["score"]:
            best = cand
    return _improve(best, jobs, machines, start_dt, due, down)


# ---------------- 评分 ----------------

def _score(rows, due, start_dt):
    """(迟到单数, 累计迟到天数, 总完工, 排程条数)，逐项比较即字典序。"""
    fin = {}
    for r in rows:
        fin[r["order"]] = max(fin.get(r["order"], r["end"]), r["end"])
    late, days = 0, 0.0
    for oid, done in fin.items():
        limit = due.get(oid)
        if limit is not None and done > limit:
            late += 1
            days += (done - limit).total_seconds() / 86400.0
    return (late, round(days, 4), max(fin.values(), default=start_dt), len(rows))


def _due_dt(day):
    """交期日期 → 交期当天 17:00，与风险清单口径一致。"""
    if not day:
        return None
    return datetime.combine(day, time(config.DUE_HOUR))


# ---------------- 单次排程 ----------------

def _evaluate(order, rule, jobs, machines, start_dt, due, down=None):
    rows = _run(order, jobs, machines, start_dt, down)
    return {"rows": rows, "makespan": max((r["end"] for r in rows), default=start_dt),
            "rule": rule, "order": list(order),
            "score": _score(rows, due, start_dt), "evals": 1, "moves": 0}


def _run(order, jobs, machines, start_dt, down=None):
    """按给定订单顺序逐单排，每道工序取最早能结束的机台。

    停机区间直接当成"机台已被占住"塞进档期，后面找空档的逻辑一个字不用改，
    故障机台就会自动被绕开——这比再写一套"跳过维修期"的分支可靠得多。
    """
    busy = {mid: list(iv) for mid, iv in (down or {}).items()}
    out = []
    for oid in order:
        ready = start_dt
        for task in jobs[oid]["tasks"]:
            t0 = max(ready, task["earliest"]) if task["earliest"] else ready
            start, end, mid = _pick(task, busy, machines, t0)
            busy.setdefault(mid, []).append((start, end))
            out.append(_row(task, mid, start, end))
            ready = end
    return out


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
    """在已占用区间之外，找最早能放下 minutes 的空档。

    intervals 里既有别人排的活，也有故障停机窗口——两者对"这台机什么时候能干活"
    是一回事，所以一视同仁。每次冲突至少跳到冲突区间的末尾，最坏 len+1 次收敛，
    这里留 64 次余量兜住停机窗口叠得比较碎的情况。
    """
    cand = cal.ceil_to_work(t0, shifts)
    for _ in range(max(len(intervals) + 16, 64)):
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
        jobs.setdefault(t["order_id"], {"tasks": [], "total": 0.0})
        jobs[t["order_id"]]["tasks"].append(t)
        jobs[t["order_id"]]["total"] += t["minutes"]
    for job in jobs.values():
        job["tasks"].sort(key=lambda x: x["seq"])
    return jobs


# ---------------- 订单排序规则 ----------------

def _ordered(jobs, rule, start_dt):
    """按规则给订单排序：急单永远在前，其次才看规则。"""
    if rule == "ATC":
        prefs = _atc_prefs(jobs, start_dt)
        return sorted(jobs, key=lambda o: (_urgent(jobs, o), prefs[o], o))
    key = _rule_key(jobs, rule)
    return sorted(jobs, key=key)


def _urgent(jobs, oid):
    return 0 if jobs[oid]["tasks"][0].get("urgent") else 1


def _rule_key(jobs, rule):
    recv = {o: (j["tasks"][0].get("recv") or FAR_FUTURE) for o, j in jobs.items()}
    due = {o: (j["tasks"][0].get("due") or FAR_FUTURE) for o, j in jobs.items()}
    total = {o: j["total"] for o, j in jobs.items()}
    if rule == "SPT":
        return lambda o: (_urgent(jobs, o), total[o], o)
    if rule == "MWKR":
        return lambda o: (_urgent(jobs, o), -total[o], o)
    if rule == "EDD":
        return lambda o: (_urgent(jobs, o), due[o], o)
    if rule == "EDD+SPT":
        return lambda o: (_urgent(jobs, o), due[o], total[o], o)
    return lambda o: (_urgent(jobs, o), recv[o], o)      # FIFO


def _atc_prefs(jobs, start_dt):
    """ATC（Apparent Tardiness Cost）：交期越紧、单越短，优先级越高。

    priority = (1/p) × exp(−max(交期 − 现在 − p, 0) / (k × p̄))
    前一项奖励短单，后一项惩罚"还早得很"的单，是交期类问题里最稳的启发式之一。
    """
    total = {o: j["total"] for o, j in jobs.items()}
    pbar = max(sum(total.values()) / max(len(total), 1), 1e-6)
    out = {}
    for o, job in jobs.items():
        p = max(total[o], 1e-6)
        due = _due_dt(job["tasks"][0].get("due"))
        if due is None:
            out[o] = 0.0                                  # 没交期的单排后面
            continue
        slack = max((due - start_dt).total_seconds() / 60.0 - p, 0.0)
        out[o] = -1.0 / p * math.exp(-slack / (config.ATC_K * pbar))
    return out


# ---------------- 局部改进 ----------------

def _improve(best, jobs, machines, start_dt, due, down=None):
    """在订单顺序上做插入式局部搜索：挪一张单，评分变好就接受。

    只和当前最优比（first improvement），遍历顺序固定、评估次数有上限，
    所以结果与机器快慢无关：同样的台账永远排出一模一样的表。
    """
    budget = _budget(sum(len(j["tasks"]) for j in jobs.values()))
    order, cur, evals, moves = best["order"][:], best, 0, 0
    improved = True
    while improved and evals < budget:
        improved = False
        for i, j in _moves(len(order)):
            if evals >= budget:
                break
            cand_order = order[:]
            cand_order.insert(j, cand_order.pop(i))
            cand = _evaluate(cand_order, best["rule"], jobs, machines, start_dt, due, down)
            evals += 1
            if cand["score"] < cur["score"]:
                cur, order, improved, moves = cand, cand_order, True, moves + 1
    cur["evals"] = evals + 1
    cur["moves"] = moves
    cur["rule"] = best["rule"] if not moves else f'{best["rule"]}+局部交换({moves})'
    return cur


def _moves(n):
    """固定的邻域遍历顺序：把第 i 张单插到第 j 个位置。"""
    for i in range(n):
        for j in range(n):
            if i != j:
                yield i, j


def _budget(n_tasks):
    """评估次数预算：按规模反比缩放，让大台账也不至于排太久。

    单次排程约 0.00004 秒/道工序，这个公式大致把总耗时压在 2~3 秒。
    """
    ref = config.LOCAL_SEARCH_REF_TASKS
    scaled = int(config.LOCAL_SEARCH_EVALS * ref / max(n_tasks, 1))
    return max(config.LOCAL_SEARCH_MIN_EVALS, min(config.LOCAL_SEARCH_EVALS, scaled))


# ---------------- 交期可行性下界 ----------------

def lower_bounds(tasks, machines, start_dt):
    """每单"独占最快机台、零排队"下的最早完工时刻。

    这是物理下界：现实只会更晚，不可能更早。
    下界都已经超过交期的单，属于"交期本身不可能"，不是排产没排好——
    必须和"排得不好"分开报，否则老板只会认为算法不行。
    """
    out = {}
    for oid, job in _group(tasks).items():
        ready = start_dt
        for t in job["tasks"]:
            if not t["machines"]:
                out[oid] = None
                break
            shifts = max(machines[mid]["shifts"] for mid in t["machines"])
            begin = max(ready, t["earliest"]) if t["earliest"] else ready
            ready = cal.add_work_minutes(cal.ceil_to_work(begin, shifts), t["minutes"], shifts)
        out.setdefault(oid, ready)
    return out
