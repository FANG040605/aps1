"""四条硬约束校验：不重不漏 / 机台与工时一致 / 不倒序 / 不重叠。

这个文件是整个项目的地基，必须满足两条：
1) 它不调用求解器、不读求解器的中间状态，只拿"任务清单 + 排程结果"重新算一遍；
2) 它不经过任何 AI，只认约束和算术。同样的输入永远给同样的结论。

返回 violations 列表，每条含 code（C1~C4）与 detail；空列表 = 0 违反。
"""

from . import calendar_util as cal

TOL = 1.5          # 分钟容差，容忍浮点误差
C1, C2, C3, C4 = "C1 不重不漏", "C2 机台与工时", "C3 工序倒序", "C4 机台重叠"


def check(tasks, rows, machines):
    """跑完四条约束，返回全部违反项。"""
    expect = {(t["order_id"], t["seq"]): t for t in tasks}
    return (_c1(expect, rows) + _c2(expect, rows, machines)
            + _c3(rows) + _c4(rows))


def _c1(expect, rows):
    """每道工序恰好排一次：漏排、重排、数量对不上都要抓。"""
    v, seen = [], {}
    for r in rows:
        key = (r["order"], r["seq"])
        seen[key] = seen.get(key, 0) + 1
    for key, cnt in seen.items():
        if key not in expect:
            v.append({"code": C1, "detail": f"{key[0]} 第 {key[1]} 道：任务清单里没有，属多排"})
        elif cnt > 1:
            v.append({"code": C1, "detail": f"{key[0]} 第 {key[1]} 道：重复排了 {cnt} 次"})
    for key in expect:
        if key not in seen:
            v.append({"code": C1, "detail": f"{key[0]} 第 {key[1]} 道：漏排"})
    if len(rows) != len(expect):
        v.append({"code": C1, "detail": f"排程条数 {len(rows)} ≠ 应排工序数 {len(expect)}"})
    return v


def _c2(expect, rows, machines):
    """机台必须能干这道工序、在允许范围内，且工时 = 单件 × 数量。"""
    v = []
    for r in rows:
        task = expect.get((r["order"], r["seq"]))
        if task is None:
            continue
        m = machines.get(r["machine"])
        if m is None:
            v.append({"code": C2, "detail": f"{r['order']} 排到不存在的机台 {r['machine']}"})
            continue
        if not m["usable"]:
            v.append({"code": C2, "detail": f"{r['order']} 排到停机/外协机台 {r['machine']}"})
        if r["op"] not in m["ops"]:
            v.append({"code": C2, "detail": f"{r['machine']} 不能做「{r['op']}」"})
        if r["machine"] not in task["machines"]:
            v.append({"code": C2, "detail": f"{r['order']}·{r['op']} 排到未授权机台 {r['machine']}"})
        want = task["per_piece"] * task["qty"]
        if abs(want - r["minutes"]) > TOL:
            v.append({"code": C2, "detail": f"{r['order']}·{r['op']} 工时 {r['minutes']} ≠ {task['per_piece']}×{task['qty']}={want}"})
        if not _duration_ok(r, m["shifts"]):
            v.append({"code": C2, "detail": f"{r['order']}·{r['op']} 起止时刻折算出的工作分钟与工时不符"})
    return v


def _duration_ok(row, shifts):
    """按日历复核：start→end 之间真正开工的分钟数，应等于该工序工时。"""
    real = cal.work_minutes_between(row["start"], row["end"], shifts)
    return abs(real - row["minutes"]) <= TOL


def _c3(rows):
    """同一订单，前道完工时间不得晚于后道开工时间。"""
    v, by_order = [], {}
    for r in rows:
        by_order.setdefault(r["order"], []).append(r)
    for order, items in by_order.items():
        items.sort(key=lambda x: x["seq"])
        for prev, cur in zip(items, items[1:]):
            if prev["end"] > cur["start"]:
                v.append({"code": C3, "detail": f"{order} 第 {prev['seq']} 道完工 {cal.fmt(prev['end'])} 晚于第 {cur['seq']} 道开工 {cal.fmt(cur['start'])}"})
    return v


def _c4(rows):
    """同一台机，任何两段时间不得交叠。"""
    v, by_machine = [], {}
    for r in rows:
        by_machine.setdefault(r["machine"], []).append(r)
    for mid, items in by_machine.items():
        items.sort(key=lambda x: x["start"])
        for prev, cur in zip(items, items[1:]):
            if prev["end"] > cur["start"]:
                v.append({"code": C4, "detail": f"{mid} 上 {prev['order']}（{prev['op']}）与 {cur['order']}（{cur['op']}）时间重叠 {cal.fmt(cur['start'])}"})
    return v


def summary(violations):
    """按约束汇总，给报表和命令行用。"""
    out = {C1: 0, C2: 0, C3: 0, C4: 0}
    for item in violations:
        out[item["code"]] = out.get(item["code"], 0) + 1
    return out
