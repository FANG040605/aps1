"""四件产物：排单表 xlsx / 甘特图 html / 风险清单 md / 体检报告 md。

每份产物开头都带"本次参数"行——事后争议全靠这一行。
甘特图是单文件 HTML，不引任何外部资源，断网双击也能打开。
"""

import os
from datetime import datetime, time, timedelta

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import config
from . import calendar_util as cal

PALETTE = ["#2563eb", "#0d9488", "#7c3aed", "#db2777", "#ea580c", "#0891b2", "#65a30d"]


def _ensure(path):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)


def _color_map(rows):
    products = sorted({r["product"] for r in rows})
    return {p: PALETTE[i % len(PALETTE)] for i, p in enumerate(products)}


# ---------------- 风险计算 ----------------

def build_risks(orders, rows, skipped, bounds=None):
    """算每单的预计完工、差额、卡在哪台机哪道工序，以及交期可行性下界。

    bounds 是"独占最快机台、零排队"的最早完工：现实只会更晚。
    下界都赶不上交期的单，属于交期本身不可能，必须和"排得不好"分开报。
    """
    bounds = bounds or {}
    by_order = {}
    for r in rows:
        by_order.setdefault(r["order"], []).append(r)
    risks = [_risk_of(od, by_order.get(od["order_id"]), bounds.get(od["order_id"]))
             for od in orders]
    risks = [r for r in risks if r]
    for item in skipped:
        od = item["order"]
        risks.append({"order": od["order_id"], "due": od["due_text"] or "—", "eta": None,
                      "gap": None, "machine": "—", "op": "—", "status": "未排产",
                      "note": item["reason"], "lb": bounds.get(od["order_id"]),
                      "infeasible": False})
    risks.sort(key=_risk_key)
    return risks


def _risk_of(od, items, bound):
    """末道工序的完工时间决定这单能不能交；下界决定这交期有没有可能。"""
    if not items:
        return None
    last = max(items, key=lambda x: x["end"])
    due_dt = datetime.combine(od["due"], time(config.DUE_HOUR)) if od["due"] else None
    gap = round((due_dt - last["end"]).total_seconds() / 86400, 1) if due_dt else None
    status, note = _risk_note(gap, due_dt, last, bound)
    if od["due_text"] and not od["due_text"][:4].isdigit():
        note = f'【按口径折算：{od["due_text"]}】' + note
    bound_gap = round((bound - due_dt).total_seconds() / 86400, 1) if (bound and due_dt) else None
    return {"order": od["order_id"],
            "due": od["due"].isoformat() if od["due"] else (od["due_text"] or "—"),
            "eta": last["end"], "gap": gap, "machine": last["machine"], "op": last["op"],
            "status": status, "note": note, "lb": bound, "bound_gap": bound_gap,
            "infeasible": status == "不可行"}


def _risk_note(gap, due_dt, last, bound=None):
    if due_dt is None:
        return "需确认", "交期是文字或空白，无法确定日期"
    if bound and bound > due_dt:
        late = (bound - due_dt).total_seconds() / 86400
        return "不可行", (f"独占最快机台、零排队也要 {cal.fmt(bound)} 才完工，比交期晚 {late:.1f} 天"
                          "——交期本身不成立，只能改期或外协，不是排产能优化掉的")
    if gap < 0:
        return "迟到", f"预计晚 {abs(gap):.1f} 天，末道积压在 {last['machine']}"
    if gap < 1:
        return "临界", "余量不足 1 天，机台稍波动就迟到"
    return "按期", f"余量 {gap:.1f} 天"


def _risk_key(r):
    rank = {"不可行": 0, "迟到": 1, "需确认": 2, "临界": 3, "按期": 4, "未排产": 5}
    return (rank[r["status"]], r["gap"] if r["gap"] is not None else 0)


# ---------------- ① 排单表 xlsx ----------------

def write_schedule_xlsx(path, rows, params_line, title):
    """周排单表：可直接打印，字段不缺。"""
    _ensure(path)
    wb = Workbook()
    ws = wb.active
    ws.title = "排单表"
    cols = ["机台", "单号", "工序", "数量", "开工", "完工", "工时(分钟)", "产品"]
    _sheet_head(ws, title, params_line, cols)
    ordered = sorted(rows, key=lambda r: (r["machine"], r["start"]))
    for i, r in enumerate(ordered, start=4):
        vals = [r["machine"], r["order"], r["op"], r["qty"],
                cal.fmt(r["start"]), cal.fmt(r["end"]), round(r["minutes"]), r["product"]]
        for j, v in enumerate(vals, start=1):
            cell = ws.cell(i, j, v)
            cell.border = _thin()
            if j in (4, 7):
                cell.number_format = "0"
                cell.alignment = Alignment(horizontal="right")
    _sheet_tail(ws, ordered)
    wb.save(path)


def _sheet_head(ws, title, params_line, cols):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(cols))
    c = ws.cell(1, 1, title)
    c.font = Font(size=14, bold=True)
    ws.row_dimensions[1].height = 26
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(cols))
    ws.cell(2, 1, "本次参数：" + params_line).font = Font(size=9, color="475569")
    widths = [12, 15, 8, 7, 13, 13, 11, 9]
    for j, name in enumerate(cols, start=1):
        cell = ws.cell(3, j, name)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2563EB")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = _thin()
        ws.column_dimensions[get_column_letter(j)].width = widths[j - 1]
    ws.freeze_panes = "A4"


def _sheet_tail(ws, ordered):
    line = len(ordered) + 4
    total = round(sum(r["minutes"] for r in ordered))
    finish = cal.fmt(max((r["end"] for r in ordered), default=None))
    ws.merge_cells(start_row=line, start_column=1, end_row=line, end_column=8)
    c = ws.cell(line, 1, f"合计 {len(ordered)} 道工序 · 总工时 {total} 分钟 · 总完工 {finish}")
    c.font = Font(bold=True)


def _thin():
    side = Side(style="thin", color="E2E8F0")
    return Border(left=side, right=side, top=side, bottom=side)


# ---------------- ② 甘特图 html ----------------

def write_gantt_html(path, rows, machines, start, end, params_line):
    """单文件甘特图：行=机台，列=时间，悬停看单号/工序/起止。"""
    _ensure(path)
    span = max((end - start).total_seconds() / 60, 1)
    colors = _color_map(rows)
    ticks = _ticks(start, end, span, label=True)
    grids = _ticks(start, end, span, label=False)
    body = "".join(_gantt_row(mid, rows, start, span, grids, colors)
                   for mid in sorted({r["machine"] for r in rows}))
    html = _GANTT_TPL.format(title="宏达精密 · 排产甘特图", params=params_line, rows=body,
                             ticks=ticks, begin=cal.fmt_full(start), finish=cal.fmt_full(end))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def _ticks(start, end, span, label=True):
    """每天 08:00 一条竖线；label=True 时带日期标签（只在顶部坐标轴用）。"""
    out, cur = [], start
    for _ in range(60):
        if cur > end:
            break
        left = max((cur - start).total_seconds() / 60 / span * 100, 0)
        text = f'<span>{cur.strftime("%m-%d")}</span>' if label else ""
        out.append(f'<div class="tick" style="left:{left:.3f}%">{text}</div>')
        cur = (cur + timedelta(days=1)).replace(hour=config.DAY_START_HOUR, minute=0)
    return "".join(out)


def _gantt_row(mid, rows, start, span, grids, colors):
    items = sorted([r for r in rows if r["machine"] == mid], key=lambda x: x["start"])
    bars = "".join(_bar(r, start, span, colors) for r in items)
    load = sum(r["minutes"] for r in items) / 60
    return (f'<div class="row"><div class="name">{mid}<em>{load:.0f}h</em></div>'
            f'<div class="track">{grids}{bars}</div></div>')


def _bar(r, start, span, colors):
    left = max((r["start"] - start).total_seconds() / 60 / span * 100, 0)
    width = max((r["end"] - r["start"]).total_seconds() / 60 / span * 100, 0.25)
    tip = f'{r["order"]} · {r["op"]} · {r["qty"]}件 · {cal.fmt(r["start"])}→{cal.fmt(r["end"])} · {round(r["minutes"])}分钟'
    return (f'<div class="bar" title="{tip}" style="left:{left:.3f}%;width:{width:.3f}%;'
            f'background:{colors[r["product"]]}"><span>{r["order"][-3:]}</span></div>')


_GANTT_TPL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Microsoft YaHei",sans-serif;background:#f8fafc;color:#0f172a;padding:24px 28px;font-size:13px}}
h1{{font-size:18px}}
.meta{{color:#64748b;font-size:12px;margin-top:4px}}
.params{{background:#eff6ff;border:1px solid #dbeafe;color:#1e3a8a;border-radius:8px;padding:8px 12px;font-size:12px;margin:12px 0 16px}}
.chart{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px}}
.axis{{position:relative;height:18px;margin-left:120px}}
.row{{display:flex;align-items:center;height:40px;border-top:1px solid #f1f5f9}}
.name{{width:120px;flex:none;font-weight:600;font-size:12px}}
.name em{{color:#94a3b8;font-style:normal;margin-left:6px;font-weight:400}}
.track{{position:relative;flex:1;height:40px}}
.bar{{position:absolute;top:10px;height:20px;border-radius:4px;opacity:.92;color:#fff;
     font-size:10px;line-height:20px;padding-left:5px;overflow:hidden;white-space:nowrap;cursor:default}}
.bar:hover{{opacity:1;outline:2px solid #0f172a}}
.tick{{position:absolute;top:0;bottom:0;border-left:1px dashed #e2e8f0}}
.tick span{{position:absolute;top:0;left:3px;font-size:10px;color:#94a3b8;white-space:nowrap}}
.legend{{margin-top:14px;color:#94a3b8;font-size:11px}}
</style></head>
<body>
<h1>{title}</h1>
<div class="meta">{begin} → {finish}</div>
<div class="params">本次参数：{params}</div>
<div class="chart">
  <div class="axis">{ticks}</div>
  {rows}
</div>
<div class="legend">每行一台机，横条是一道工序；悬停可看单号 / 工序 / 数量 / 起止 / 工时。虚线为每日 08:00 分界，跨过虚线的横条表示该工序在夜间与周日暂停、机台仍被占用（工序不可中断，假设 A2）。</div>
</body></html>"""


# ---------------- ③ 风险清单 md ----------------

def write_risk_md(path, risks, params_line):
    """交期风险清单：每条都能指到机台与工序。"""
    _ensure(path)
    lines = ["# 交期风险清单", "", f"> 本次参数：{params_line}", "", "## 一、总览", "",
             "| 项目 | 数值 |", "|---|---|"]
    lines += _risk_overview(risks)
    lines += ["", "## 二、明细", "",
              "| 单号 | 交期 | 交期下界 | 预计完工 | 差额(天) | 卡在哪台机 | 卡在哪道工序 | 状态 | 说明 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in risks:
        gap = "—" if r["gap"] is None else f'{r["gap"]:+.1f}'
        eta = cal.fmt(r["eta"]) if r["eta"] else "未排产"
        bound = cal.fmt(r["lb"]) if r.get("lb") else "—"
        lines.append(f'| {r["order"]} | {r["due"]} | {bound} | {eta} | {gap} | {r["machine"]} | '
                     f'{r["op"]} | {r["status"]} | {r["note"]} |')
    lines += ["", "## 三、合计", "", _risk_total(risks), "", "## 四、口径说明", "",
              f'差额 = 交期当天 {config.DUE_HOUR}:00 − 该单末道工序完工时间；负数为迟到天数，正数为余量。',
              "交期下界 = 该单独占最快机台、前后无排队时的最早完工。它是物理极限，现实只会更晚。",
              "下界都超过交期的单标「不可行」：这类单再怎么调顺序也来不及，只能改期、分批或外协，",
              "　不属于排产算法能优化的范围；「迟到」才是排产没排好、还有优化空间的那部分。",
              "交期为文字（尽快 / 月底前 / 空白）的单按约定口径折算并标「需确认」，不当作准确交期。", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _risk_overview(risks):
    cnt = {}
    for r in risks:
        cnt[r["status"]] = cnt.get(r["status"], 0) + 1
    worst = min((r for r in risks if r["gap"] is not None), key=lambda x: x["gap"], default=None)
    worst_txt = f'{worst["order"]}（晚 {abs(worst["gap"]):.1f} 天）' if worst and worst["gap"] < 0 else "无"
    return [f'| 参与排产订单 | {len([r for r in risks if r["status"] != "未排产"])} |',
            f'| 交期不可行（物理不可能） | {cnt.get("不可行", 0)} |',
            f'| 迟到（可优化） | {cnt.get("迟到", 0)} |',
            f'| 临界（余量<1天） | {cnt.get("临界", 0)} |',
            f'| 按期 | {cnt.get("按期", 0)} |',
            f'| 交期待确认 | {cnt.get("需确认", 0)} |',
            f'| 未排产 | {cnt.get("未排产", 0)} |',
            f'| 最晚一单 | {worst_txt} |']


def _risk_total(risks):
    late = [r for r in risks if r["status"] == "迟到"]
    hopeless = [r for r in risks if r["status"] == "不可行"]
    parts = []
    if hopeless:
        orders = "、".join(f'{r["order"]}({r["bound_gap"]:+.1f}d)' if r["bound_gap"] is not None
                           else r["order"] for r in hopeless)
        parts.append(f'**交期不可行 {len(hopeless)} 单（改期/外协，非排产问题）→ {orders}**')
    if late:
        orders = "、".join(f'{r["order"]}({r["gap"]:+.1f}d)' for r in late)
        parts.append(f'**迟到（可优化）{len(late)} 单 → {orders}**')
    if not parts:
        return "**合计：0 单迟到，0 单交期不可行。**"
    return "　".join(parts)


# ---------------- ④ 体检报告 md ----------------

def write_checkup_md(path, issues, stats, params_line):
    """数据体检：按类别分组，每条都有位置、现象、处理方式。"""
    _ensure(path)
    groups = _group_issues(issues)
    need = sum(1 for i in issues if i["need_confirm"])
    lines = ["# 数据体检报告", "", f"> 本次参数：{params_line}", "", "## 一、总览", "",
             "| 项目 | 数值 |", "|---|---|",
             f'| 订单行数（清洗后） | {stats["orders"]} |',
             f'| 参与排产订单 | {stats["planned"]} |',
             f'| 工序任务数 | {stats["tasks"]} |',
             f'| 体检条目 | {len(issues)} 条 |',
             f'| 问题类别 | {len(groups)} 类 |',
             f'| 需客户确认 | {need} 条 |', "",
             "## 二、问题分类", ""]
    for idx, (cat, items) in enumerate(groups, start=1):
        lines += _checkup_block(idx, cat, items)
    lines += ["## 三、本报告不处理的事", "",
              "- 不替客户补数量、不替客户定交期：拿不准一律标「需客户确认」",
              "- 不建模换型时间、人员资质、外协周期（见设计说明书假设 A1~A9）",
              "- 台账里的口头规则只做关键词识别，识别不到的不猜", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _group_issues(issues):
    order, groups = [], {}
    for i in issues:
        if i["category"] not in groups:
            groups[i["category"]] = []
            order.append(i["category"])
        groups[i["category"]].append(i)
    return [(c, groups[c]) for c in order]


def _checkup_block(idx, cat, items):
    need = sum(1 for i in items if i["need_confirm"])
    head = f'### {idx}. {cat}（{len(items)} 条' + (f'，需确认 {need} 条）' if need else '）')
    rows = ["", "| 位置 | 现象 | 处理方式 | 需客户确认 |", "|---|---|---|---|"]
    for i in items:
        rows.append(f'| {i["where"]} | {i["symptom"]} | {i["action"]} | {"是" if i["need_confirm"] else "否"} |')
    return [head] + rows + [""]
