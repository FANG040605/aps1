"""读取台账原始数据，不做任何清洗。

两种放法都认，客户按自己的习惯存成哪种都行：
A. 三个工作表：订单 / 设备 / 工艺路线
B. 一个工作表里上下排三段：每段第一行是这个表的表头
   （表名随便叫，"订单表""机台清单""工艺"都能认，靠表头判断而不是靠名字）

定位一律靠表头关键字，不写死行号：
- 第 1 行往往是标题（"宏达精密 · 待排产订单（…2026-11-08）"），不是表头
- 表头行里至少有 2 个该表的关键字（见 HEADER_KEYS）
- 一段的结束位置＝下一段表头出现的那一行（所以三段排在一张表里也不会串行）
- 末尾常有整行注释（"注：交期一栏…"），只有一个单元格有字，要跳过

为什么不再要求"必须三个 sheet"：任务书和手册里出现的"三个 sheet"描述的是
客户台账的样子，不是格式要求。计划员把它存成一张表、三段排开是很自然的做法，
工具因为这个拒绝排产，纯属自己给自己设卡。两种布局都读，同一份数据出同样的结果。
"""

from openpyxl import load_workbook

TABLES = ("订单", "设备", "工艺路线")

# 每张表"至少命中 2 个关键字"才算表头。
# 订单和工艺路线都有"产品"，所以订单必须靠"单号 / 交期"认出来，
# 工艺路线靠"工序1"认出来——单看"产品"一定会认错。
HEADER_KEYS = {
    "订单": ("单号", "交期", "产品"),
    "设备": ("设备号", "编号", "可做工序"),
    "工艺路线": ("产品", "工序1"),
}
MIN_HITS = 2
MAX_COLS = 60          # 防护：有些工作簿 max_column 会虚报到上千列


class WorkbookError(Exception):
    """台账结构不对：缺表 / 找不到表头 / 表里没数据。提示可直接给客户看。"""


def read_workbook(path):
    """读工作簿，返回 {表名: {"sheet", "header_row", "headers", "records"}}。

    三张表可以分在三个 sheet，也可以挤在一个 sheet 的上中下三段，
    结果结构完全一样，后面 cleaner 不用关心来源。
    """
    wb = load_workbook(path, data_only=True)
    regions = {}
    for ws in wb.worksheets:
        for reg in _scan_sheet(ws):
            regions.setdefault(reg["table"], reg)     # 先出现的为准
    missing = [t for t in TABLES if t not in regions]
    if missing:
        raise WorkbookError(_missing_msg(regions, missing))
    return {t: _read_region(regions[t]) for t in TABLES}


def _scan_sheet(ws):
    """自上而下扫一张表，切出所有能认出来的表头段。"""
    out, row, last = [], 1, _last_row(ws)
    while row <= last:
        table, hits = _guess_table(_row_values(ws, row))
        if hits < MIN_HITS:
            row += 1
            continue
        end = _region_end(ws, row, last)
        out.append({"table": table, "ws": ws, "sheet": ws.title,
                    "header_row": row, "end_row": end,
                    "headers": _headers_of(ws, row)})
        row = end + 1
    return out


def _region_end(ws, header_row, last):
    """这一段到哪儿结束：下一段表头的前一行；后面没有表头就到最后一行。"""
    for row in range(header_row + 1, last + 1):
        if _guess_table(_row_values(ws, row))[1] >= MIN_HITS:
            return row - 1
    return last


def _guess_table(values):
    """一行格子最像哪张表的表头，返回 (表名, 命中关键字个数)。"""
    best, best_hit = None, 0
    for name in TABLES:
        hit = sum(1 for k in HEADER_KEYS[name] if k in values)
        if hit > best_hit:                 # 平手时保持 TABLES 顺序，结果稳定
            best, best_hit = name, hit
    return best, best_hit


def _read_region(reg):
    """按表头把每行读成 dict，行号记在 __row__ 里（体检报告要指位置）。"""
    ws, header_row, end_row = reg["ws"], reg["header_row"], reg["end_row"]
    headers = reg["headers"]
    records = []
    for row in range(header_row + 1, end_row + 1):
        vals = [ws.cell(row, c).value for c in range(1, len(headers) + 1)]
        if _is_empty(vals) or _is_note(vals):
            continue
        rec = {"__row__": row, "__sheet__": ws.title}
        for i, h in enumerate(headers):
            if h:
                rec[h] = vals[i]           # 原样存，日期还是日期，清洗交给 cleaner
        records.append(rec)
    return {"table": reg["table"], "sheet": ws.title, "header_row": header_row,
            "end_row": end_row, "headers": headers, "records": records}


def _row_values(ws, row):
    """一行格子转成去空白的字符串列表（表头识别用，不碰数据）。"""
    return [_txt(ws.cell(row, c).value) for c in range(1, _width(ws) + 1)]


def _headers_of(ws, row):
    """表头行：去掉右侧的空列。

    三张表挤在一张 sheet 时，窄表（设备只有 6 列）会被宽表（订单 7 列）
    撑出多余的空表头，留着会让两种布局读出来的表头长度不一致。
    """
    vals = _row_values(ws, row)
    while vals and vals[-1] == "":
        vals.pop()
    return vals


def _width(ws):
    return min(ws.max_column or 1, MAX_COLS)


def _last_row(ws):
    return ws.max_row or 1


def _txt(v):
    return "" if v is None else str(v).strip()


def _is_empty(vals):
    return all(_txt(v) == "" for v in vals)


def _is_note(vals):
    """整行只有一个格子有字 → 是注释行（如"注：交期一栏…"），不是数据。"""
    filled = [_txt(v) for v in vals if _txt(v) != ""]
    return len(filled) <= 1


def layout_note(data):
    """一句话说清每张表是从哪读来的，给"本次参数"行用。"""
    parts, same = [], True
    first = data[TABLES[0]]["sheet"]
    for name in TABLES:
        sheet = data[name]["sheet"]
        same = same and sheet == first
        parts.append(f"{name}@{sheet}")
    if same:
        rows = "、".join(f'{name} 第 {data[name]["header_row"]} 行' for name in TABLES)
        return f"一张表三种区域（{first}：{rows}）"
    return "三张工作表（" + " / ".join(parts) + "）"


def _missing_msg(regions, missing):
    """缺表的提示：告诉他工具认两种放法，以及表头该长什么样。"""
    got = "、".join(f'{reg["table"]}（{reg["sheet"]} 第 {reg["header_row"]} 行）'
                   for reg in regions.values()) or "无"
    need = "、".join(f"「{t}」" for t in missing)
    wants = "；".join(f'{t} = {" + ".join(HEADER_KEYS[t])}' for t in TABLES)
    return (f"没找到{need}的表头。已认出：{got}。\n"
            f"两种放法都行：① 三个工作表各放一张表；② 一个工作表里上下排三段，"
            f"每段第一行放该表的表头。\n"
            f"表头至少要有：{wants}")


def dump_columns(data, n=3):
    """打印每列前 n 个值，用于 reader 阶段的肉眼核对。"""
    for name, sheet in data.items():
        print(f"\n[{name}] 来自「{sheet['sheet']}」第 {sheet['header_row']} 行，"
              f"共 {len(sheet['records'])} 条")
        for h in sheet["headers"]:
            if not h:
                continue
            vals = [_txt(r.get(h)) for r in sheet["records"][:n]]
            print(f"  {h:<10} {' | '.join(v if v else '␀' for v in vals)}")
