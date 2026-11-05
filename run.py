"""命令行入口：一条命令从台账到四件产物。

    python run.py data/宏达精密_订单台账.xlsx --out out

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

from core import checker, pipeline  # noqa: E402


def _parse_start(text):
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%d").replace(hour=config.DAY_START_HOUR)


def _report(run, elapsed):
    sum4 = checker.summary(run["violations"])
    print("本次参数：" + run["params"])
    print(f"工序任务 {len(run['tasks'])} 道 ｜ 参与排产 {len({t['order_id'] for t in run['tasks']})} 单"
          f" ｜ 未排产 {len(run['skipped'])} 单 ｜ 总完工 {run['makespan']:%Y-%m-%d %H:%M}")
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
    args = ap.parse_args(argv)
    t0 = clock.time()
    run = pipeline.run_pipeline(args.xlsx, args.out, _parse_start(args.start), not args.no_switch)
    _report(run, clock.time() - t0)
    return 0 if not run["violations"] else 2


if __name__ == "__main__":
    sys.exit(main())
