"""命令行入口：一条命令从台账到四件产物。

    python run.py data/宏达精密_订单台账.xlsx --out out

印出"本次参数"一行，校验器 0 违反才正常退出（否则退出码 2）。
"""

import argparse
import os
import sys
import time as clock
from datetime import date, datetime

import config
from core import calendar_util as cal
from core import checker, cleaner, instance as inst, reader, report, solver


def build_params(start_dt, allow_switch, machines):
    """把本次真正生效的口径拼成一行，打印并写进每份产物。"""
    shifts = sorted({m["shifts"] for m in machines.values()})
    shift_txt = "、".join(f"{s} 班" for s in shifts)
    down = "、".join(m["code"] for m in machines.values()
                     if not m["usable"] and "外协" not in m["status"]) or "无"
    out = "、".join(m["code"] for m in machines.values() if "外协" in m["status"]) or "无"
    idle = "、".join(m["code"] for m in machines.values() if "闲置" in m["status"]) or "无"
    return (f'起始 {cal.fmt_full(start_dt)} ｜ 每班 {config.HOURS_PER_SHIFT}h，班制 {shift_txt}'
            f' ｜ 周日不排 ｜ 交期当天 {config.DUE_HOUR}:00 ｜ 工时 {config.TIME_UNIT}×数量'
            f' ｜ 换机台 {"允许" if allow_switch else "不允许"} ｜ 数量空白 不排产'
            f' ｜ 停机 {down} ｜ 外协 {out} 不计产能 ｜ 闲置 {idle} 可启用')


def _start(arg, orders):
    """起始日默认取最近周一；有到料日的单会自动往后推。"""
    if arg:
        return datetime.strptime(arg, "%Y-%m-%d").replace(hour=config.DAY_START_HOUR)
    recv = [o["recv"] for o in orders if o["recv"]]
    base = max([date.today()] + recv) if recv else date.today()
    return cal.next_monday(base)


def main(argv=None):
    ap = argparse.ArgumentParser(description="宏达精密 · 本地排产助手")
    ap.add_argument("xlsx", help="客户台账 .xlsx")
    ap.add_argument("--out", default="out", help="产物输出目录")
    ap.add_argument("--start", default=None, help="起始日 YYYY-MM-DD，默认最近周一")
    ap.add_argument("--no-switch", action="store_true", help="不允许同组换机台")
    args = ap.parse_args(argv)
    t0 = clock.time()

    data = reader.read_workbook(args.xlsx)
    clean, issues = cleaner.clean_all(data)
    start_dt = _start(args.start, clean["orders"])
    machines = {m["code"]: m for m in clean["machines"]}
    tasks, skipped, more = inst.load_instance(clean, not args.no_switch, start_dt)
    issues += more

    result = solver.schedule(tasks, machines, start_dt)
    violations = checker.check(tasks, result["rows"], machines)
    params = build_params(start_dt, not args.no_switch, machines)
    risks = report.build_risks(clean["orders"], result["rows"], skipped)
    _write_all(args.out, result, machines, risks, issues, params, start_dt, len(clean["orders"]))
    _report(result, tasks, issues, violations, skipped, params, t0)
    return 0 if not violations else 2


def _write_all(out, result, machines, risks, issues, params, start_dt, n_orders):
    os.makedirs(out, exist_ok=True)
    rows = result["rows"]
    end = result["makespan"]
    stats = {"orders": n_orders, "planned": len({r["order"] for r in rows}), "tasks": len(rows)}
    report.write_schedule_xlsx(os.path.join(out, "排单表.xlsx"), rows, params,
                               f"宏达精密 · 周排单表（{cal.fmt_full(start_dt)} 起）")
    report.write_gantt_html(os.path.join(out, "甘特图.html"), rows, machines, start_dt, end, params)
    report.write_risk_md(os.path.join(out, "风险清单.md"), risks, params)
    report.write_checkup_md(os.path.join(out, "体检报告.md"), issues, stats, params)


def _report(result, tasks, issues, violations, skipped, params, t0):
    sum4 = checker.summary(violations)
    print("本次参数：" + params)
    print(f"工序任务 {len(tasks)} 道 ｜ 参与排产 {len({t['order_id'] for t in tasks})} 单"
          f" ｜ 未排产 {len(skipped)} 单 ｜ 总完工 {cal.fmt_full(result['makespan'])}")
    print(f"规则对比：{result['rule']} 最优（FIFO / SPT / MWKR 各排一遍取最短）")
    print("校验：" + " ｜ ".join(f"{k} {v}" for k, v in sum4.items())
          + ("  → 通过" if not violations else "  → 未通过"))
    for v in violations[:10]:
        print("  ✗ " + v["detail"])
    need = sum(1 for i in issues if i["need_confirm"])
    print(f"体检：{len(issues)} 条 / {len({i['category'] for i in issues})} 类 / 需客户确认 {need} 条")
    print(f"用时 {clock.time() - t0:.2f}s")


if __name__ == "__main__":
    sys.exit(main())
