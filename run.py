"""命令行入口：一条命令从台账到五件产物。

日常（只交订单，设备与工艺用上次存下的产线基线）：

    python run.py 订单.xlsx --out out

第一次或产线变了（完整台账，顺手更新基线）：

    python run.py data/宏达精密_订单台账.xlsx --out out

机台坏了（先记一条事件，再排）：

    python run.py 订单.xlsx --out out --fault "GRD-01@2026-11-12 08:00@主轴异响"

印出"本次参数"一行，校验器 0 违反才正常退出（否则退出码 2）。
真正的流程在 core/pipeline.py，网页版走同一个函数。
"""

import argparse
import sys
import time as clock
from datetime import datetime

import config
from preflight import ensure_deps

ensure_deps()  # 缺 openpyxl 时给中文提示，不甩堆栈

from core import checker, events, pipeline  # noqa: E402


def _parse_start(text):
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%d").replace(hour=config.DAY_START_HOUR)


def _parse_fault(spec):
    """把 机台@恢复时间@原因 拆开。

    分隔符用 @ 不用冒号：时间本身带冒号（2026-11-12 08:00），
    用冒号切会把"08:00"切碎，结果解析失败、悄悄变成"归期未定"。
    """
    parts = [x.strip() for x in spec.split("@")]
    machine = parts[0]
    until = events.parse_time(parts[1]) if len(parts) > 1 and parts[1] else None
    note = parts[2] if len(parts) > 2 else ""
    return machine, until, note


def _report(run, elapsed):
    sum4 = checker.summary(run["violations"])
    print("本次参数：" + run["params"])
    print(f"工序任务 {len(run['tasks'])} 道 ｜ 参与排产 {len({t['order_id'] for t in run['tasks']})} 单"
          f" ｜ 退回待补 {len(run['rollback'])} 单 ｜ 总完工 {run['makespan']:%Y-%m-%d %H:%M}")
    for item in run["rollback"]:
        print(f'  ! {item["order"]}（{item["where"]}）：{item["reason"]} → {item["fix"]}')
    late = [r for r in run["risks"] if r["status"] == "迟到"]
    bad = [r for r in run["risks"] if r["status"] == "不可行"]
    print(f"规则对比：{run['rule']} 最优（{' / '.join(config.DISPATCH_RULES)} 各排一遍，"
          f"再局部交换 {run['search']['moves']} 次，共评估 {run['search']['evals']} 次）")
    print(f"交期：迟到（可优化）{len(late)} 单 ｜ 交期不可行（改期/外协）{len(bad)} 单")
    print("校验：" + " ｜ ".join(f"{k} {v}" for k, v in sum4.items())
          + ("  → 通过" if not run["violations"] else "  → 未通过"))
    for v in run["violations"][:10]:
        print("  ✗ " + v["detail"])
    need = sum(1 for i in run["issues"] if i["need_confirm"])
    print(f"体检：{len(run['issues'])} 条 / {len({i['category'] for i in run['issues']})} 类"
          f" / 需客户确认 {need} 条")
    print(f"用时 {elapsed:.2f}s")


def main(argv=None):
    ap = argparse.ArgumentParser(description="宏达精密 · 本地排产助手")
    ap.add_argument("xlsx", help="客户台账 .xlsx")
    ap.add_argument("--out", default="out", help="产物输出目录")
    ap.add_argument("--start", default=None, help="起始日 YYYY-MM-DD，默认现在之后第一个可开工时刻")
    ap.add_argument("--no-switch", action="store_true", help="不允许同组换机台")
    ap.add_argument("--profile", default=None, help="产线基线文件路径，默认 data/产线基线.xlsx")
    ap.add_argument("--no-profile", action="store_true",
                    help="不用产线基线（要求文件里三张表齐全，也不回写基线）")
    ap.add_argument("--fault", action="append", default=[], metavar="机台@恢复时间[@原因]",
                    help='登记设备停机后重排，可重复。例："GRD-01@2026-11-12 08:00@主轴异响"，'
                         "只写机台表示归期未定")
    ap.add_argument("--recover", action="append", default=[], metavar="机台",
                    help="登记设备已恢复，可重复")
    args = ap.parse_args(argv)
    t0 = clock.time()
    for spec in args.fault:
        machine, until, note = _parse_fault(spec)
        events.add(machine, "故障", until=until, note=note)
        print(f"已登记：{machine} 停机至 {until:%Y-%m-%d %H:%M}" if until else f"已登记：{machine} 停机（归期未定）")
    for machine in args.recover:
        print(f"已登记：{machine} 恢复（截断 {events.recover(machine)} 条停机）")
    run = pipeline.run_pipeline(args.xlsx, args.out, _parse_start(args.start), not args.no_switch,
                                profile_path=args.profile, use_profile=not args.no_profile)
    _report(run, clock.time() - t0)
    return 0 if not run["violations"] else 2


if __name__ == "__main__":
    sys.exit(main())
