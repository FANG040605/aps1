"""脏数据鲁棒性自检：把台账改坏 12 种方式，逐个看还能不能跑完。

为什么需要它：客户拿 Excel 打开台账存一下就报"排产失败"，
现场只看到一句 TypeError。这类问题靠"想"是查不出来的，
只能把客户可能的改法都造一遍，真跑。

用法：python tests/robustness_check.py
前置：data/宏达精密_订单台账.xlsx 存在
产物：跑完自动清理，不留垃圾
"""

import os
import shutil
import sys
import traceback

from openpyxl import load_workbook

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import pipeline  # noqa: E402

SRC = os.path.join(ROOT, "data", "宏达精密_订单台账.xlsx")
OUT = os.path.join(ROOT, "out", "_fuzz")


def headers_of(ws, keys):
    """在前 6 行里找表头行，返回 {表头名: 列号}。"""
    for r in range(1, 7):
        vals = [ws.cell(r, c).value for c in range(1, ws.max_column + 1)]
        texts = [str(v).strip() if v is not None else "" for v in vals]
        if any(k in texts for k in keys):
            return {t: i + 1 for i, t in enumerate(texts) if t}
    return {}


def first_data_row(ws, header_row):
    return header_row + 1


def find_header_row(ws, keys):
    for r in range(1, 7):
        texts = [str(ws.cell(r, c).value or "").strip() for c in range(1, ws.max_column + 1)]
        if any(k in texts for k in keys):
            return r
    return 1


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("数量留空")
def _(ws, hdr, row):
    ws.cell(row, hdr["数量"]).value = None


@case("数量写「40件」")
def _(ws, hdr, row):
    ws.cell(row, hdr["数量"]).value = "40件"


@case("数量写中文「四十」")
def _(ws, hdr, row):
    ws.cell(row, hdr["数量"]).value = "四十"


@case("交期写「下周」")
def _(ws, hdr, row):
    ws.cell(row, hdr["交期"]).value = "下周"


@case("交期留空")
def _(ws, hdr, row):
    ws.cell(row, hdr["交期"]).value = None


@case("交期写成 Excel 日期")
def _(ws, hdr, row):
    ws.cell(row, hdr["交期"]).value = "2026-11-20"


@case("接单日期写「上周」")
def _(ws, hdr, row):
    ws.cell(row, hdr["接单日期"]).value = "上周"


@case("接单日期留空")
def _(ws, hdr, row):
    ws.cell(row, hdr["接单日期"]).value = None


@case("备注写「材料11月才到」")
def _(ws, hdr, row):
    ws.cell(row, hdr["备注"]).value = "材料11月才到"


@case("备注写「材料下月才到」")
def _(ws, hdr, row):
    ws.cell(row, hdr["备注"]).value = "材料下月才到"


@case("客户留空")
def _(ws, hdr, row):
    ws.cell(row, hdr["客户"]).value = None


@case("单号改成纯数字")
def _(ws, hdr, row):
    ws.cell(row, hdr["单号"]).value = 101


def run_case(name, fn):
    path = os.path.join(OUT, "case.xlsx")
    shutil.copy(SRC, path)
    wb = load_workbook(path)
    ws = wb["订单"]
    hdr_row = find_header_row(ws, ("单号",))
    hdr = {str(ws.cell(hdr_row, c).value).strip(): c
           for c in range(1, ws.max_column + 1) if ws.cell(hdr_row, c).value}
    row = first_data_row(ws, hdr_row)
    fn(ws, hdr, row)
    wb.save(path)
    try:
        run = pipeline.run_pipeline(path, os.path.join(OUT, "r"), start=None, allow_switch=True)
        return f"OK    总完工 {run['makespan']:%m-%d %H:%M}   未排产 {len(run['skipped'])} 单"
    except TypeError:
        tb = traceback.format_exc().strip().splitlines()
        return "TypeError ← 就是这个！\n      " + "\n      ".join(tb[-4:])
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def main():
    os.makedirs(OUT, exist_ok=True)
    bad = []
    for name, fn in CASES:
        res = run_case(name, fn)
        print(f"[{name}]  {res}")
        if "TypeError" in res:
            bad.append(name)
    print("\n会抛 TypeError 的改法：", bad or "无")
    shutil.rmtree(OUT, ignore_errors=True)


if __name__ == "__main__":
    main()
