"""一条龙：读表 → 补基线 → 清洗 → 建实例 → 排程 → 校验 → 出报表。

CLI（run.py）和网页（web/app.py）都调这一个函数：
两条路径必须给出同一个结果，否则"命令行对、网页错"这类问题极难查。

三种输入都走这里，行为一致：
1) 完整台账（订单+设备+工艺路线）—— 顺手把后两张存成产线基线，下次不用再传
2) 只有订单表 —— 设备与工艺从产线基线取（车间天天变的只有订单）
3) 只有订单表 + 设备事件（故障/保养）—— 停机窗口叠加到基线上再排
"""

import os
from datetime import date, datetime, time, timedelta

import config
from . import calendar_util as cal
from . import checker, cleaner, events, instance as inst, profile, reader, report, solver

KINDS = {"schedule": ("排单表.xlsx", "schedule"), "gantt": ("甘特图.html", "gantt"),
         "risk": ("风险清单.md", "risk"), "checkup": ("体检报告.md", "checkup"),
         "rollback": ("待补充订单.xlsx", "rollback")}

BASE_TABLES = ("设备", "工艺路线")


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


# ---------------- 基线 ----------------

def _fingerprint_of(data):
    """设备与工艺的内容指纹（只洗这两张表，避免重复洗订单产生多余体检条目）。"""
    machines, _ = cleaner.clean_machines(data["设备"])
    routes, _ = cleaner.clean_routes(data["工艺路线"])
    return profile.fingerprint({"machines": machines, "routes": routes})


def _attach_base(data, issues, profile_path, use_profile, now):
    """把设备与工艺路线补齐，返回基线说明。

    完整台账：直接用自带的，并顺手存成基线（下次只传订单就行）。
    只有订单：从基线读。没有基线就明说，不要排出个空计划。
    """
    info = {"used": False, "source": "", "summary": "", "fingerprint": "",
            "changed": False, "path": profile_path or profile.default_path(),
            "mtime": None}
    own = all(t in data for t in BASE_TABLES)

    if own:
        fp = _fingerprint_of(data)
        old = profile.load(profile_path) if use_profile else None
        if old is not None:
            old_fp = _fingerprint_of(old)
            info["changed"] = old_fp != fp
            if info["changed"]:
                issues.append({
                    "category": "产线基线", "where": "设备 / 工艺路线",
                    "symptom": f"设备或工艺路线与上次保存的基线不一致（{old_fp} → {fp}）",
                    "action": "已按这次上传的内容更新基线，本次排产用的是新基线",
                    "need_confirm": False,
                })
        if use_profile:
            path = profile.save(data, profile_path)
            info["path"] = path
        info.update(used=True, source="本次上传", fingerprint=fp)
        if info["changed"] or old is None:
            info["mtime"] = profile.mtime(profile_path)
        else:
            info["mtime"] = profile.mtime(profile_path)
        return info

    if not use_profile:
        raise reader.WorkbookError(
            "这次只传了订单表，但当前设置为不使用产线基线，排不出工序。"
            "请把设备表与工艺路线表一起传上来。")

    base = profile.load(profile_path)
    if base is None:
        raise reader.WorkbookError(
            "这次只传了订单表，但还没有保存过产线基线（设备 + 工艺路线）。\n"
            "第一次用：把完整台账（订单 / 设备 / 工艺路线三张表）传一次，"
            "工具会自动把设备和工艺路线存成基线；之后每天只传订单表就行。")
    data["设备"] = base["设备"]
    data["工艺路线"] = base["工艺路线"]
    info.update(used=True, source="产线基线", fingerprint=_fingerprint_of(data),
                mtime=profile.mtime(profile_path))
    return info


# ---------------- 停机事件 ----------------

def _apply_events(ev, machines, start_dt, issues):
    """把故障/保养事件叠加到机台上，返回 (停机窗口, 停机说明)。"""
    down = events.windows(ev, start_dt)
    stopped = events.stopped_whole(ev, start_dt)
    note = _down_note(down, start_dt + timedelta(days=config.PLAN_HORIZON_DAYS))
    for code in sorted(stopped):
        machine = machines.get(code)
        if machine is None:
            issues.append({
                "category": "设备事件", "where": f"设备!{code}",
                "symptom": f"事件里登记的机台 {code} 在设备表中不存在",
                "action": "这条事件已忽略，请核对机台编号", "need_confirm": True,
            })
            continue
        if machine["usable"]:
            machine["usable"] = False
            issues.append({
                "category": "设备停机", "where": f"设备!{code}",
                "symptom": f"{code} 在本次计划期内一直停机",
                "action": "本次不参与排产，它的活改派到同工序机台；没有替补的订单会退回待补充清单",
                "need_confirm": False,
            })
    return down, note


def _down_note(down, horizon=None):
    """说清这次排产绕开了哪些停机。

    不能只看"此刻是否在停"：计划员常常今天登记明天开始的故障，
    排产已经绕开了，报告却写"无停机事件"——那比没有这句话更糟。
    """
    if not down:
        return "无停机事件"
    parts = []
    for code in sorted(down):
        for begin, end in down[code]:
            if horizon and end >= horizon:
                parts.append(f"{code} 停 {begin:%m-%d %H:%M}→归期未定")
            else:
                parts.append(f"{code} 停 {begin:%m-%d %H:%M}→{end:%m-%d %H:%M}")
    return "、".join(parts[:6]) + ("…" if len(parts) > 6 else "")


# ---------------- 待补充订单 ----------------

def _rollback(skipped):
    """把"这次没排上的单"整理成能直接回给计划员的清单：卡在哪 + 怎么改。"""
    out = []
    for item in skipped:
        od = item["order"]
        out.append({
            "order": od["order_id"], "customer": od["customer"],
            "product": od["product"],
            "qty": "" if od["qty"] is None else od["qty"],
            "due": od["due_text"] or (od["due"].isoformat() if od["due"] else ""),
            "recv": od["recv"].isoformat() if od["recv"] else "",
            "where": f'订单 第 {od["row"]} 行',
            "reason": item["reason"],
            "fix": _fix_hint(item["reason"], od),
        })
    return out


def _fix_hint(reason, od):
    """按卡住的原因给一条能照做的话，不要只说"数据有问题"。"""
    if "数量缺失" in reason:
        return "在「数量」列填件数（例如 40），不能写「待定」「若干」"
    if "不在工艺路线表" in reason:
        return f'产品名「{od["product"]}」在工艺路线表里找不到：核对写法是否一致（空格/大小写），或让工艺科补这个产品的工序'
    if "工艺路线为空" in reason:
        return "工艺路线表里这个产品的工序列是空的，按「工序（机台）分钟」补，如 车（LAT）45"
    if "没有可用机台" in reason or "没有能做" in reason:
        return "设备表里没有能做这道工序的机台：补一台，或把这道工序标成外协"
    if "改派" in reason or "不允许换机台" in reason:
        return "工艺点名的机台不可用，且参数不允许换机台：改用同工序机台，或把参数改成允许换机台"
    if "数据异常" in reason:
        return "这行数据读不出来，逐列核对一遍（尤其日期和数量列有没有混进文字）"
    return "按「卡在哪」这一列核对订单行，补齐后重新上传"


# ---------------- 主流程 ----------------

def run_pipeline(xlsx_path, out_dir, start=None, allow_switch=True, now=None,
                 profile_path=None, events_path=None, use_profile=True):
    """跑完整条链，产物写到 out_dir，返回全部中间结果供 CLI / JSON 使用。"""
    now = now or datetime.now()
    data = reader.read_workbook(xlsx_path, required=("订单",))
    base_issues = []
    base_info = _attach_base(data, base_issues, profile_path, use_profile, now)
    clean, issues = cleaner.clean_all(data)
    issues = base_issues + issues
    if not clean["orders"]:
        raise reader.WorkbookError("「订单」表里没有读到数据行，请检查是不是空表或表头被删了")
    if not clean["routes"]:
        raise reader.WorkbookError("「工艺路线」表里没有产品，算不出工序，请检查该表")

    start_dt = _resolve_start(start, clean["orders"], issues, now)
    machines = {m["code"]: m for m in clean["machines"]}
    ev = events.load(events_path)
    down, event_note = _apply_events(ev, machines, start_dt, issues)

    tasks, skipped, more = inst.load_instance(clean, allow_switch, start_dt)
    issues += more
    try:
        result = solver.schedule(tasks, machines, start_dt, down)
    except RuntimeError as exc:                 # 求解器兜底：宁可报清楚，也不要 500 堆栈
        raise reader.WorkbookError(f"排产失败：{exc}。请检查「设备」表里这些工序有没有可用机台") from exc
    violations = checker.check(tasks, result["rows"], machines, start_dt,
                               orders=clean["orders"], skipped=skipped)
    bounds = solver.lower_bounds(tasks, machines, start_dt)
    layout = _layout_note(data, base_info)
    params = build_params(start_dt, allow_switch, machines, layout, base_info, event_note)
    risks = report.build_risks(clean["orders"], result["rows"], skipped, bounds)
    rollback = _rollback(skipped)
    paths = _write(out_dir, result, risks, issues, params, start_dt, len(clean["orders"]),
                   rollback, clean["orders"])
    return {"rows": result["rows"], "tasks": tasks, "skipped": skipped, "issues": issues,
            "violations": violations, "risks": risks, "params": params, "start": start_dt,
            "makespan": result["makespan"], "rule": result["rule"], "paths": paths,
            "machines": machines, "orders": clean["orders"], "bounds": bounds,
            "profile": base_info, "rollback": rollback, "down": down, "events": ev,
            "search": {"evals": result.get("evals", 0), "moves": result.get("moves", 0)}}


def _layout_note(data, base_info):
    """参数行里"数据从哪读的"。

    只交订单时，设备与工艺是从基线补的，不能写成"三张工作表"——
    计划员看到会以为文件里真有三张表，回头排查数据问题就会找错地方。
    """
    if base_info.get("source") == "产线基线":
        return f'订单@{data["订单"]["sheet"]}（设备与工艺路线来自产线基线，没随本次文件上传）'
    return reader.layout_note(data)


def build_params(start_dt, allow_switch, machines, layout="", base_info=None, event_note=""):
    """把本次真正生效的口径拼成一行，打印并写进每份产物。"""
    shifts = "、".join(f"{s} 班" for s in sorted({m["shifts"] for m in machines.values()}))
    down = "、".join(m["code"] for m in machines.values()
                     if not m["usable"] and "外协" not in m["status"]) or "无"
    out = "、".join(m["code"] for m in machines.values() if "外协" in m["status"]) or "无"
    idle = "、".join(m["code"] for m in machines.values() if "闲置" in m["status"]) or "无"
    from_where = f'读取 {layout} ｜ ' if layout else ""
    base = ""
    if base_info and base_info.get("used"):
        when = base_info["mtime"].strftime("%m-%d %H:%M") if base_info.get("mtime") else "—"
        base = f'产线基线 {base_info["source"]}（{base_info["fingerprint"]}，{when} 更新） ｜ '
    fault = f"停机事件 {event_note} ｜ " if event_note and event_note != "无停机事件" else ""
    return (f'{from_where}{base}{fault}起始 {cal.fmt_full(start_dt)} ｜ 每班 {config.HOURS_PER_SHIFT}h，'
            f'班制 {shifts} ｜ 周日不排 ｜ 交期当天 {config.DUE_HOUR}:00 ｜ 工时 {config.TIME_UNIT}×数量'
            f' ｜ 目标 迟到单数→迟到天数→总完工'
            f' ｜ 换机台 {"允许" if allow_switch else "不允许"} ｜ 数量空白 不排产'
            f' ｜ 停机 {down} ｜ 外协 {out} 不计产能 ｜ 闲置 {idle} 可启用')


def _write(out_dir, result, risks, issues, params, start_dt, n_orders, rollback, orders):
    os.makedirs(out_dir, exist_ok=True)
    rows = result["rows"]
    stats = {"orders": n_orders, "planned": len({r["order"] for r in rows}), "tasks": len(rows),
             "rollback": len(rollback)}
    report.write_schedule_xlsx(os.path.join(out_dir, "排单表.xlsx"), rows, params,
                               f"宏达精密 · 周排单表（{cal.fmt_full(start_dt)} 起）")
    report.write_gantt_html(os.path.join(out_dir, "甘特图.html"), rows, {}, start_dt,
                            result["makespan"], params)
    report.write_risk_md(os.path.join(out_dir, "风险清单.md"), risks, params)
    report.write_checkup_md(os.path.join(out_dir, "体检报告.md"), issues, stats, params)
    report.write_rollback_xlsx(os.path.join(out_dir, "待补充订单.xlsx"), rollback, orders)
    return {k: os.path.join(out_dir, name) for k, (name, _) in KINDS.items()}


def payload(run, job_id):
    """转成网页要的 JSON：指标卡 / 排单表 / 甘特 / 风险 / 体检 / 待补充 / 下载链接。"""
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
        "rollback": run.get("rollback", []),
        "profile": _profile_json(run.get("profile") or {}),
        "machines": _machine_json(run),
        "downloads": [{"kind": k, "name": KINDS[k][0], "url": f"/api/download/{job_id}/{k}"}
                      for k in KINDS],
    }


def _profile_json(info):
    when = info.get("mtime")
    return {"used": bool(info.get("used")), "source": info.get("source", ""),
            "fingerprint": info.get("fingerprint", ""), "changed": bool(info.get("changed")),
            "updated": when.strftime("%Y-%m-%d %H:%M") if when else ""}


def _machine_json(run):
    """机台清单 + 停机区间，给页面上的设备面板画红条用。"""
    down = run.get("down") or {}
    out = []
    for code in sorted(run.get("machines", {})):
        m = run["machines"][code]
        rows = [(cal.fmt(s), cal.fmt(e)) for s, e in down.get(code, [])]
        out.append({"code": code, "ops": m.get("ops", []), "shifts": m.get("shifts", 0),
                    "status": m.get("status", ""), "usable": bool(m.get("usable")),
                    "down": rows})
    return out


def _metrics(run):
    late = len([r for r in run["risks"] if r["status"] == "迟到"])
    hopeless = len([r for r in run["risks"] if r["status"] == "不可行"])
    need = len([i for i in run["issues"] if i["need_confirm"]])
    rollback = len(run.get("rollback") or [])
    planned = len({r["order"] for r in run["rows"]})
    return [
        {"k": "读入订单", "v": len(run["orders"]), "u": "条",
         "s": f"排产 {planned} 单" + (f"，退回 {rollback} 单待补" if rollback else ""),
         "tone": "warn" if rollback else ""},
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
