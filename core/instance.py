"""订单 → 工序任务（instance）。

两个换算必须写对，错了全盘皆错：
1) 工时 = 单件分钟 × 数量。台账里写的是「分钟/件」，不是「分钟/批」。
2) 机台两种含义要分开：
   - 点名某一台（铣（CNC-03））→ 只能排这台，不可用就要改派并留痕
   - 给的是机台组（车（LAT））→ 同组任选，谁先空谁干
"""

from datetime import datetime

import config


def load_instance(clean, allow_switch=True, start_dt=None):
    """把清洗后的三张表装配成可排程的工序任务列表。"""
    tasks, issues, skipped = [], [], []
    routes = {r["product"]: r for r in clean["routes"]}
    usable = [m for m in clean["machines"] if m["usable"]]
    pushed = 0                                   # 因"起始日早于接单日"被顺延的单数

    for od in clean["orders"]:
        reason = _skip_reason(od, routes)
        if reason:
            skipped.append({"order": od, "reason": reason})
            continue
        if start_dt and od.get("recv") and _day_start(od["recv"]) > start_dt:
            pushed += 1
        tasks.extend(_order_tasks(od, routes[od["product"]], usable, allow_switch, start_dt, issues))
    if pushed:
        _note_pushed(issues, start_dt, pushed)
    return tasks, skipped, issues


def _note_pushed(issues, start_dt, pushed):
    """起始日早于接单日时留一条痕，别让客户以为可以提前开工。"""
    issues.append({
        "category": "起始日校验", "where": "订单!接单日期",
        "symptom": f"起始日 {start_dt:%Y-%m-%d} 早于 {pushed} 张单的接单日期",
        "action": "这些单顺延到接单日 08:00 之后开工，不会排到接单之前",
        "need_confirm": False,
    })


def _skip_reason(od, routes):
    if od["qty"] is None:
        return "数量缺失，待客户确认"
    if od["product"] not in routes:
        return f"产品「{od['product']}」不在工艺路线表"
    if not routes[od["product"]]["steps"]:
        return "工艺路线为空"
    return ""


def _order_tasks(od, route, usable, allow_switch, start_dt, issues):
    """一张订单展开成按序的工序任务。"""
    out = []
    for seq, step in enumerate(route["steps"], 1):
        machines, note = resolve_machines(step["machine"], step["op"], usable, allow_switch)
        if note:
            issues.append({"category": "机台解析", "where": f"订单!{od['row']}",
                           "symptom": f"{od['order_id']}·{step['op']}（{step['machine']}）",
                           "action": note, "need_confirm": False})
        out.append({
            "order_id": od["order_id"], "product": od["product"], "seq": seq,
            "op": step["op"], "qty": od["qty"], "per_piece": step["minutes"],
            "minutes": step["minutes"] * od["qty"], "machines": machines,
            "machine_hint": step["machine"],
            "earliest": _earliest(od, start_dt),
            "urgent": od["urgent"], "recv": od["recv"], "due": od["due"],
            "due_text": od["due_text"], "customer": od["customer"],
        })
    return out


def _earliest(od, start_dt):
    """最早可开工时间：不早于起始日，也不早于接单日、到料日。

    接单日必须算进来。以前漏了这条：把起始日调到接单日之前，
    排单表就会排出"11/05 开工、11/07 才接单"这种一眼假的计划——
    活还没接下来，机器怎么先干上了。
    """
    cands = []
    if start_dt:
        cands.append(start_dt)
    if od.get("recv"):
        cands.append(_day_start(od["recv"]))
    if od.get("material_ready"):
        cands.append(_day_start(od["material_ready"]))
    return max(cands) if cands else None


def _day_start(day):
    """日期 → 当天开工时刻（08:00），与日历口径一致。"""
    return datetime.combine(day, datetime.min.time()).replace(hour=config.DAY_START_HOUR)


def resolve_machines(hint, op, usable, allow_switch=True):
    """把工艺里的机台写法解析成可用机台列表，返回 (机台编号列表, 说明)。

    - 给的是机台组（车（LAT））→ 同组任选
    - 点名某一台（铣（CNC-03））→ 点名的排在最前；允许换机台时，同工序其他机台可替补
      （否则 GRD-01 一台磨床会吃掉两周产能，闲置的 GRD-02 却闲着）
    """
    usable = [m for m in usable if m.get("usable", True)]   # 停机/外协机台不进候选
    exact = [m for m in usable if m["code"] == hint]
    if exact:
        if not allow_switch:
            return [m["code"] for m in exact], ""
        peers = [m["code"] for m in usable if op in m["ops"] and m["code"] != hint]
        return [m["code"] for m in exact] + peers, ""

    group = [m for m in usable if m["code"].split("-")[0] == hint and op in m["ops"]]
    if group:
        return [m["code"] for m in group], ""

    same_op = [m["code"] for m in usable if op in m["ops"]]
    if not same_op:
        return [], f"工序「{op}」找不到可用机台，该单无法排产"
    if not allow_switch:
        return [], f"工艺机台 {hint} 不可用，且参数不允许换机台"
    return same_op, f"工艺机台 {hint} 不可用，已改派到同工序机台（假设 A4）"


def summarize(tasks):
    """打印用的小统计。"""
    total = sum(t["minutes"] for t in tasks)
    return len({t["order_id"] for t in tasks}), len(tasks), total
