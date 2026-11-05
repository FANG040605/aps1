"""一条龙：读表 → 清洗 → 建实例 → 排程 → 校验 → 出报表。

CLI（run.py）和网页（web/app.py）都调这一个函数：
两条路径必须给出同一个结果，否则"命令行对、网页错"这类问题极难查。
"""

import os
from datetime import date, datetime, time, timedelta

import config
from . import calendar_util as cal
from . import checker, cleaner, instance as inst, reader, report, solver

KINDS = {"schedule": ("排单表.xlsx", "schedule"), "gantt": ("甘特图.html", "gantt"),
         "risk": ("风险清单.md", "risk"), "checkup": ("体检报告.md", "checkup")}


def default_start(orders, now=None):
    """起始日默认取"现在之后第一个能开工的时刻"，不再无脑跳到下周一。

    以前默认跳到最近周一：周四下午点排产，明后天（周五、周六）的产能直接被扔掉，
    同一批台账从周四起排是 4 单迟到，拖到下周一就变成 10 单——工具自己制造迟到。
    现在只在两种情况下才往后挪：现在已过今日收工时刻，或所有订单都还没接到。

    注意取的是最早的接单日，不是最晚的：最晚那个会把整批计划拖到"最后一张单
    接到的那天"，前面几天白白空着。单张单不能早于自己接单日开工，
    这件事由 instance 的 _earliest 逐单兜住，不需要在起始日上下狠手。
    """
    now = now or datetime.now()
    recv = [_as_date(o["recv"]) for o in orders if o["recv"]]
    if not recv:
        return cal.ceil_to_work(now, config.DEFAULT_SHIFTS_PER_DAY)
    first = min(recv)
    anchor = now if first <= now.date() else datetime.combine(first, time.min).replace(hour=config.DAY_START_HOUR)
    return cal.ceil_to_work(anchor, config.DEFAULT_SHIFTS_PER_DAY)


def _resolve_start(start, orders, issues, now):
    """确定本次排产的起始时刻，并挡住"从过去开始排"这种一眼假的计划。"""
    if start is None:
        return default_start(orders, now)
    if start < now:
        fixed = cal.ceil_to_work(now, config.DEFAULT_SHIFTS_PER_DAY)
        issues.append({
            "category": "起始日校验", "where": "参数·起始日",
            "symptom": f"指定起始 {cal.fmt_full(start)} 早于当前时刻 {cal.fmt_full(now)}",
            "action": f"已改为 {cal.fmt_full(fixed)}，避免排出已经过去的开工时刻",
            "need_confirm": False,
        })
        return fixed
    return cal.ceil_to_work(start, config.DEFAULT_SHIFTS_PER_DAY)



def _as_date(value):
    """datetime → date；本来就是 date 就原样返回；None 保持 None。"""
    if isinstance(value, datetime):
        return value.date()
    return value


def build_params(start_dt, allow_switch, machines):
    """把本次真正生效的口径拼成一行，打印并写进每份产物。"""
    shifts = "、".join(f"{s} 班" for s in sorted({m["shifts"] for m in machines.values()}))
    down = "、".join(m["code"] for m in machines.values()
                     if not m["usable"] and "外协" not in m["status"]) or "无"
    out = "、".join(m["code"] for m in machines.values() if "外协" in m["status"]) or "无"
    idle = "、".join(m["code"] for m in machines.values() if "闲置" in m["status"]) or "无"
    return (f'起始 {cal.fmt_full(start_dt)} ｜ 每班 {config.HOURS_PER_SHIFT}h，班制 {shifts}'
            f' ｜ 周日不排 ｜ 交期当天 {config.DUE_HOUR}:00 ｜ 工时 {config.TIME_UNIT}×数量'
            f' ｜ 目标 迟到单数→迟到天数→总完工'
            f' ｜ 换机台 {"允许" if allow_switch else "不允许"} ｜ 数量空白 不排产'
            f' ｜ 停机 {down} ｜ 外协 {out} 不计产能 ｜ 闲置 {idle} 可启用')


def run_pipeline(xlsx_path, out_dir, start=None, allow_switch=True, now=None):
    """跑完整条链，产物写到 out_dir，返回全部中间结果供 CLI / JSON 使用。"""
    data = reader.read_workbook(xlsx_path)
    clean, issues = cleaner.clean_all(data)
    if not clean["orders"]:
        raise reader.WorkbookError("「订单」表里没有读到数据行，请检查是不是空表或表头被删了")
    if not clean["routes"]:
        raise reader.WorkbookError("「工艺路线」表里没有产品，算不出工序，请检查该表")
    now = now or datetime.now()
    start_dt = _resolve_start(start, clean["orders"], issues, now)
    machines = {m["code"]: m for m in clean["machines"]}
    tasks, skipped, more = inst.load_instance(clean, allow_switch, start_dt)
    issues += more
    try:
        result = solver.schedule(tasks, machines, start_dt)
    except RuntimeError as exc:                 # 求解器兜底：宁可报清楚，也不要 500 堆栈
        raise reader.WorkbookError(f"排产失败：{exc}。请检查「设备」表里这些工序有没有可用机台") from exc
    violations = checker.check(tasks, result["rows"], machines, start_dt,
                               orders=clean["orders"], skipped=skipped)
    bounds = solver.lower_bounds(tasks, machines, start_dt)
    params = build_params(start_dt, allow_switch, machines)
    risks = report.build_risks(clean["orders"], result["rows"], skipped, bounds)
    paths = _write(out_dir, result, risks, issues, params, start_dt, len(clean["orders"]))
    return {"rows": result["rows"], "tasks": tasks, "skipped": skipped, "issues": issues,
            "violations": violations, "risks": risks, "params": params, "start": start_dt,
            "makespan": result["makespan"], "rule": result["rule"], "paths": paths,
            "machines": machines, "orders": clean["orders"], "bounds": bounds,
            "search": {"evals": result.get("evals", 0), "moves": result.get("moves", 0)}}



def _write(out_dir, result, risks, issues, params, start_dt, n_orders):
    os.makedirs(out_dir, exist_ok=True)
    rows = result["rows"]
    stats = {"orders": n_orders, "planned": len({r["order"] for r in rows}), "tasks": len(rows)}
    report.write_schedule_xlsx(os.path.join(out_dir, "排单表.xlsx"), rows, params,
                               f"宏达精密 · 周排单表（{cal.fmt_full(start_dt)} 起）")
    report.write_gantt_html(os.path.join(out_dir, "甘特图.html"), rows, {}, start_dt,
                            result["makespan"], params)
    report.write_risk_md(os.path.join(out_dir, "风险清单.md"), risks, params)
    report.write_checkup_md(os.path.join(out_dir, "体检报告.md"), issues, stats, params)
    return {k: os.path.join(out_dir, name) for k, (name, _) in KINDS.items()}


def payload(run, job_id):
    """转成网页要的 JSON：指标卡 / 排单表 / 甘特 / 风险 / 体检 / 下载链接。"""
    rows, risks, issues = run["rows"], run["risks"], run["issues"]
    return {
        "job_id": job_id,
        "params": run["params"],
        "rule": run["rule"],
        "start": cal.fmt_full(run["start"]),
        "finish": cal.fmt_full(run["makespan"]),
        "metrics": _metrics(run),
        "schedule": [_schedule_row(r) for r in sorted(rows, key=lambda x: (x["machine"], x["start"]))],
        "gantt": _gantt(rows, run["start"], run["makespan"]),
        "risks": [_risk(r) for r in risks],
        "issues": issues,
        "violations": run["violations"],
        "downloads": [{"kind": k, "name": KINDS[k][0], "url": f"/api/download/{job_id}/{k}"}
                      for k in KINDS],
    }


def _metrics(run):
    late = len([r for r in run["risks"] if r["status"] == "迟到"])
    hopeless = len([r for r in run["risks"] if r["status"] == "不可行"])
    need = len([i for i in run["issues"] if i["need_confirm"]])
    return [
        {"k": "读入订单", "v": len(run["orders"]), "u": "条", "s": f'排产 {len({r["order"] for r in run["rows"]})} 单'},
        {"k": "工序任务", "v": len(run["rows"]), "u": "道", "s": "分钟/件 × 数量"},
        {"k": "总完工", "v": run["makespan"].strftime("%m-%d"), "u": run["makespan"].strftime("%H:%M"),
         "s": f"规则 {run['rule']} 最优"},
        {"k": "校验违反", "v": len(run["violations"]), "u": "项", "s": "五条硬约束", "tone": "ok" if not run["violations"] else "bad"},
        {"k": "迟到订单", "v": late, "u": "单",
         "s": f"另 {hopeless} 单交期物理不可能" if hopeless else "见风险清单",
         "tone": "bad" if (late or hopeless) else "ok"},
        {"k": "数据问题", "v": len(run["issues"]), "u": "条",
         "s": f"{len({i['category'] for i in run['issues']})} 类，{need} 条待确认", "tone": "warn"},
    ]



def _schedule_row(r):
    return {"machine": r["machine"], "order": r["order"], "op": r["op"], "qty": r["qty"],
            "start": cal.fmt(r["start"]), "end": cal.fmt(r["end"]),
            "minutes": round(r["minutes"]), "product": r["product"]}


def _gantt(rows, start, end):
    """按机台分组，条形位置用百分比，前端直接画。"""
    span = max((end - start).total_seconds() / 60, 1)
    out = {}
    for r in rows:
        item = out.setdefault(r["machine"], {"name": r["machine"], "load": 0, "bars": []})
        item["load"] += r["minutes"]
        item["bars"].append({
            "order": r["order"], "op": r["op"], "qty": r["qty"], "product": r["product"],
            "left": round((r["start"] - start).total_seconds() / 60 / span * 100, 3),
            "width": round(max((r["end"] - r["start"]).total_seconds() / 60 / span * 100, 0.25), 3),
            "tip": f'{r["order"]} · {r["op"]} · {r["qty"]}件 · {cal.fmt(r["start"])}→{cal.fmt(r["end"])}',
        })
    for item in out.values():
        item["load"] = round(item["load"] / 60)
        item["bars"].sort(key=lambda b: b["left"])
    return {"machines": [out[k] for k in sorted(out)], "days": _days(start, end, span)}


def _days(start, end, span):
    out, cur = [], start
    for _ in range(60):
        if cur > end:
            break
        out.append({"label": cur.strftime("%m-%d"),
                    "left": round(max((cur - start).total_seconds() / 60 / span * 100, 0), 3)})
        cur = (cur + timedelta(days=1)).replace(hour=config.DAY_START_HOUR, minute=0)
    return out


def _risk(r):
    return {"order": r["order"], "due": r["due"],
            "eta": cal.fmt(r["eta"]) if r["eta"] else "未排产",
            "gap": None if r["gap"] is None else round(r["gap"], 1),
            "machine": r["machine"], "op": r["op"], "status": r["status"], "note": r["note"],
            "lb": cal.fmt(r["lb"]) if r.get("lb") else "—",
            "infeasible": bool(r.get("infeasible"))}
