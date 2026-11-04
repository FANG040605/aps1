"""读取三个 sheet 的原始数据，不做任何清洗。

台账的真实结构（实测）：
- 第 1 行往往是标题（"宏达精密 · 待排产订单（…2026-11-08）"），不是表头
- 表头在第 2 行左右，靠"单号 / 设备号 / 产品"这类关键字定位，不写死行号
- 末尾常有整行注释（"注：交期一栏…"），只有一个单元格有字，要跳过
因此这里只负责"把格子读成字典"，清洗一律交给 cleaner。
"""

from openpyxl import load_workbook

REQUIRED_SHEETS = ("订单", "设备", "工艺路线")
HEADER_KEYS = {
    "订单": ("单号", "交期", "产品"),
    "设备": ("设备号", "编号", "可做工序"),
    "工艺路线": ("产品", "工序1"),
}
SCAN_ROWS = 6          # 表头最多往下找 6 行


def read_workbook(path):
    """读工作簿，返回 {sheet名: {"header_row", "headers", "records"}}。"""
    wb = load_workbook(path, data_only=True)
    missing = [s for s in REQUIRED_SHEETS if s not in wb.sheetnames]
    if missing:
        raise ValueError(f"缺少工作表：{'、'.join(missing)}；需要「订单 / 设备 / 工艺路线」三张表")
    return {name: _read_sheet(wb[name], name) for name in REQUIRED_SHEETS}


def _find_header(ws, keys):
    """在前几行里找表头，返回 (行号, 表头文字列表)。"""
    for row in ws.iter_rows(min_row=1, max_row=min(SCAN_ROWS, ws.max_row)):
        vals = [_txt(c.value) for c in row]
        hit = sum(1 for k in keys if k in vals)
        if hit >= 2:
            return row[0].row, vals
    raise ValueError(f"工作表「{ws.title}」找不到表头（应含 {'、'.join(keys)}）")


def _read_sheet(ws, name):
    """按表头把每行读成 dict，行号记在 __row__ 里（体检报告要指位置）。"""
    header_row, headers = _find_header(ws, HEADER_KEYS[name])
    records = []
    for r in range(header_row + 1, ws.max_row + 1):
        vals = [ws.cell(r, c).value for c in range(1, len(headers) + 1)]
        if _is_empty(vals) or _is_note(vals):
            continue
        rec = {"__row__": r}
        for i, h in enumerate(headers):
            if h:
                rec[h] = vals[i]
        records.append(rec)
    return {"header_row": header_row, "headers": headers, "records": records}


def _txt(v):
    return "" if v is None else str(v).strip()


def _is_empty(vals):
    return all(_txt(v) == "" for v in vals)


def _is_note(vals):
    """整行只有一个格子有字 → 是注释行（如"注：交期一栏…"），不是数据。"""
    filled = [_txt(v) for v in vals if _txt(v) != ""]
    return len(filled) <= 1


def dump_columns(data, n=3):
    """打印每列前 n 个值，用于 reader 阶段的肉眼核对。"""
    for name, sheet in data.items():
        print(f"\n[{name}] 表头在第 {sheet['header_row']} 行，共 {len(sheet['records'])} 条")
        for h in sheet["headers"]:
            if not h:
                continue
            vals = [_txt(r.get(h)) for r in sheet["records"][:n]]
            print(f"  {h:<10} {' | '.join(v if v else '␀' for v in vals)}")
