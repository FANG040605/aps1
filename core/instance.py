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
        try:
            made, blocked = _order_tasks(od, routes[od["product"]], usable, allow_switch, start_dt, issues)
        except Exception as exc:                    # 单条数据异常不许拖垮整批
            skipped.append({"order": od, "reason": f"订单数据异常（{type(exc).__name__}），待人工核对"})
            continue
        if blocked:
            issues.append({"category": "机台解析", "where": f"订单!{od['row']}",
                           "symptom": f"{od['order_id']}：{blocked}",
                           "action": "该单整体不排产，其余订单照排；请补设备表后重排",
                           "need_confirm": True})
            skipped.append({"order": od, "reason": blocked})
            continue
        tasks.extend(made)
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
    """一张订单展开成按序的工序任务。

    返回 (任务列表, 卡住的原因)。只要有一道工序找不到可用机台，
    整单都不能排——半张单排出来比不排更糟：计划员会以为这单能做，
    到了车间才发现磨床上根本没这道活。
    以前这里只记一条 issue 就把空机台任务继续往下传，求解器一抛 RuntimeError，
    14 张单全部出不来。现在改成"这一单不排，其余照排"，并标需客户确认。
    """
    out, blocked = [], ""
    earliest = _earliest(od, start_dt)
    for seq, step in enumerate(route["steps"], 1):
        machines, note = resolve_machines(step["machine"], step["op"], usable, allow_switch)
        if not machines:
            blocked = blocked or f'第 {seq} 道「{step["op"]}」：{note or "没有可用机台"}'
            continue
        if note:
            issues.append({"category": "机台解析", "where": f"订单!{od['row']}",
                           "symptom": f"{od['order_id']}·{step['op']}（{step['machine']}）",
                           "action": note, "need_confirm": False})
        out.append({
            "order_id": od["order_id"], "product": od["product"], "seq": seq,
            "op": step["op"], "qty": od["qty"], "per_piece": step["minutes"],
            "minutes": step["minutes"] * od["qty"], "machines": machines,
            "machine_hint": step["machine"],
            "earliest": earliest,
            "urgent": od["urgent"], "recv": od["recv"], "due": od["due"],
            "due_text": od["due_text"], "customer": od["customer"],
            "material_ready": od.get("material_ready"),
        })
    return out, blocked



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
        return [], f"设备表里没有能做「{op}」的机台"
    if not allow_switch:
        return [], f"工艺机台 {hint} 不可用，且参数不允许换机台"
    return same_op, f"工艺机台 {hint} 不可用，已改派到同工序机台（假设 A4）"


def summarize(tasks):
    """打印用的小统计。"""
    total = sum(t["minutes"] for t in tasks)
    return len({t["order_id"] for t in tasks}), len(tasks), total
