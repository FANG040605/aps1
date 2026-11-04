"""洗脏数据 + 逐条记录体检 issues。

台账是客户用 Excel 手工维护的，脏数据分三类处理：
- 能确定的（格式统一、去空格）→ 直接改，记一条 issue
- 能给保守口径的（尽快 / 月底前）→ 按 config 的规则折算，标"需客户确认"
- 不能猜的（数量空白）→ 不填数，标"需客户确认"，该单不排产

铁律：凡是会影响"交期承诺"的处理，都要留痕并可标"需客户确认"——
那是客户的权责，不是我们的。
"""

import re
from datetime import date, datetime, timedelta

import config

ISO_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
CN_RE = re.compile(r"^(\d{1,2})\s*月\s*(\d{1,2})\s*日?$")
DIGITS_RE = re.compile(r"\D")
OP_RE = re.compile(r"^(?P<op>[^（(]+)[（(](?P<mc>[^）)]+)[）)]\s*(?P<mins>\d+(?:\.\d+)?)\s*$")
MATERIAL_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def _txt(v):
    return "" if v is None else str(v).strip()


def _loc(sheet, headers, head, row):
    """体检报告里的位置，形如 订单!D4。"""
    col = headers.index(head) + 1 if head in headers else 1
    return f"{sheet}!{chr(64 + col)}{row}"


def _issue(issues, category, where, symptom, action, need_confirm=False):
    issues.append({"category": category, "where": where, "symptom": symptom,
                   "action": action, "need_confirm": need_confirm})


# ---------------- 单号 ----------------

def normalize_order_no(value):
    """三种写法归一：HD-2026-101 / HD2026102 / 104 → HD-2026-1xx。"""
    s = _txt(value)
    digits = DIGITS_RE.sub("", s)
    if len(digits) == 7 and digits.startswith("2026"):
        return f"HD-2026-{digits[-3:]}"
    if len(digits) == 3:
        return f"HD-2026-{digits}"
    if len(digits) > 3:
        return f"HD-2026-{digits[-3:]}"
    return s


def _dedupe(ids):
    """重复单号加 -A / -B 后缀，否则"每道工序恰好一次"必然报违反。"""
    seen, counter, out = {}, {}, []
    for i in ids:
        seen[i] = seen.get(i, 0) + 1
    for i in ids:
        if seen[i] == 1:
            out.append(i)
            continue
        counter[i] = counter.get(i, 0) + 1
        out.append(f"{i}-{chr(64 + counter[i])}")
    return out


# ---------------- 日期 ----------------

def parse_date(value):
    """解析日期：2026/11/3、2026-11-03、2026.11.4、11月12日、Excel 日期。

    一律返回 date，绝不返回 datetime。
    原因（客户真实踩过，整条链路直接崩）：datetime 是 date 的子类，
    台账被 Excel 另存一次后，接单日期会从文字变成真日期（datetime），
    于是一批订单里混着 date 和 datetime，`max()` 一比较就
    TypeError: '>' not supported between instances of 'datetime.datetime' and 'datetime.date'
    ——客户只是拿 Excel 打开存了一下，什么也没改，工具就说排产失败。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = _txt(value).replace(" ", "")
    if not s:
        return None
    m = ISO_RE.match(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return _safe_date(y, mo, d)
    m = CN_RE.match(s)
    if m:
        mo, d = (int(x) for x in m.groups())
        return _safe_date(2026, mo, d)     # 台账全部是 2026 年，跨年需客户补年份
    return None


def _safe_date(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def add_workdays(day, n):
    """往后推 n 个工作日（只跳周日，与排产日历一致）。"""
    cur, left = day, n
    while left > 0:
        cur += timedelta(days=1)
        if cur.weekday() != config.NON_WORKING_WEEKDAYS[0]:
            left -= 1
    return cur


def month_end(day):
    nxt = date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)
    return nxt - timedelta(days=1)


def parse_due(value, recv):
    """交期是人话列：返回 (日期|None, 原文, 是否需客户确认)。"""
    text = _txt(value)
    if not text:
        return None, "", True
    exact = parse_date(text)
    if exact:
        return exact, "", False
    for key, (_, days) in config.DUE_TEXT_RULES.items():
        if key in text and recv:
            if days is None:
                return month_end(recv), text, True
            return add_workdays(recv, days), text, True
    return None, text, True


# ---------------- 订单 ----------------

def clean_orders(sheet):
    """洗订单表：单号归一/去重、日期统一、数量判空、备注转结构化规则。"""
    issues, orders = [], []
    headers, raw = sheet["headers"], sheet["records"]
    norm = [normalize_order_no(r.get("单号")) for r in raw]
    dup = {n for n in norm if norm.count(n) > 1}
    for rec, oid, base in zip(raw, _dedupe(norm), norm):
        _order_issues(rec, oid, base, dup, norm, headers, issues)
        recv = parse_date(rec.get("接单日期"))
        due, _, _ = parse_due(rec.get("交期"), recv)
        qty = _parse_qty(rec.get("数量"))
        orders.append(_order_dict(rec, oid, recv, due, qty, issues, headers))
    return orders, issues


def _order_issues(rec, oid, base, dup, norm, headers, issues):
    """单号 / 接单日期 / 交期 / 数量 四类问题在这里统一登记。"""
    row = rec["__row__"]
    oid_raw = _txt(rec.get("单号"))
    if base in dup:
        _issue(issues, "单号重复", _loc("订单", headers, "单号", row),
               f"「{oid_raw}」同号共 {norm.count(base)} 条", f"拆分为 {oid}，按独立订单排产")
    elif oid != oid_raw:
        _issue(issues, "单号格式", _loc("订单", headers, "单号", row),
               f"原文「{oid_raw}」", f"统一为 {oid}")
    _recv_issue(rec, headers, issues)
    _due_issue(rec, headers, issues)
    if _parse_qty(rec.get("数量")) is None:
        _issue(issues, "数量缺失", _loc("订单", headers, "数量", row),
               f"数量为空（备注：{_txt(rec.get('备注'))}）", "不排产，标需客户确认", True)


def _recv_issue(rec, headers, issues):
    """接单日期：空白、无法解析、非标准格式，三种都留痕。"""
    raw = _txt(rec.get("接单日期"))
    loc = _loc("订单", headers, "接单日期", rec["__row__"])
    parsed = parse_date(raw)
    if not raw:
        _issue(issues, "接单日期", loc, "接单日期为空", "标需客户确认，交期按原文处理", True)
    elif parsed is None:
        _issue(issues, "接单日期", loc, f"原文「{raw}」无法解析", "标需客户确认", True)
    elif not ISO_RE.match(raw.replace(" ", "")):
        _issue(issues, "接单日期", loc, f"格式「{raw}」", f"统一为 {parsed.isoformat()}")


def _due_issue(rec, headers, issues):
    """交期：空白 / 文字，都要记，且文字折算一律标需客户确认。"""
    raw = _txt(rec.get("交期"))
    recv = parse_date(rec.get("接单日期"))
    due, text, need = parse_due(raw, recv)
    loc = _loc("订单", headers, "交期", rec["__row__"])
    if not raw:
        _issue(issues, "交期口径", loc, "交期为空", "按无交期处理，风险清单标需客户确认", True)
    elif text:
        _issue(issues, "交期口径", loc, f"原文「{text}」不是日期",
               f"按口径折算为 {due.isoformat() if due else '无'}", need)


def _parse_qty(value):
    s = _txt(value)
    if not s:
        return None
    try:
        n = int(float(s))
    except ValueError:
        return None
    return n if n > 0 else None


def _order_dict(rec, oid, recv, due, qty, issues, headers):
    """把一条订单整理成标准结构，顺带识别备注里的口头规则。"""
    remark = _txt(rec.get("备注"))
    loc = _loc("订单", headers, "备注", rec["__row__"])
    urgent = any(k in remark for k in config.RULE_KEYWORDS["urgent"])
    if urgent:
        _issue(issues, "备注规则", loc, f"备注「{remark}」", "识别为急单，排产时优先")
    qty = _half_qty(remark, qty, loc, issues)
    ready = _material_date(remark, loc, issues)
    return {"order_id": oid, "customer": _txt(rec.get("客户")), "recv": recv, "due": due,
            "due_text": _txt(rec.get("交期")), "product": _txt(rec.get("产品")),
            "qty": qty, "remark": remark, "urgent": urgent, "material_ready": ready,
            "row": rec["__row__"]}


def _half_qty(remark, qty, loc, issues):
    """备注"先做一半" → 数量减半，并留痕。"""
    if not qty or not any(k in remark for k in config.RULE_KEYWORDS["half"]):
        return qty
    half = max(1, round(qty / 2))
    _issue(issues, "备注规则", loc, f"备注「{remark}」", f"按一半数量排产：{qty} → {half}")
    return half


def _material_date(remark, loc, issues):
    """备注"材料 X 月 X 日才到" → 最早开工日。"""
    m = MATERIAL_RE.search(remark)
    if not m:
        return None
    ready = date(2026, int(m.group(1)), int(m.group(2)))
    _issue(issues, "备注规则", loc, f"备注「{remark}」", f"该单最早开工日 {ready.isoformat()}")
    return ready


# ---------------- 设备 ----------------

def clean_machines(sheet):
    """洗设备表：拆可做工序、读班次、判可用性，外协机台不进可用池。"""
    issues, machines = [], []
    for rec in sheet["records"]:
        code = _txt(rec.get("编号"))
        dev_no = _txt(rec.get("设备号"))
        status = _txt(rec.get("状态"))
        ops = [x.strip() for x in re.split(r"[、,，/]", _txt(rec.get("可做工序"))) if x.strip()]
        shifts = _parse_shifts(_txt(rec.get("每日班次")))
        if not code:
            _issue(issues, "设备编号缺失", f"设备!{rec['__row__']}",
                   f"设备号「{dev_no}」为空", "无法识别机台，剔除")
            continue
        usable = _judge_status(issues, rec, code, status)
        if shifts != config.DEFAULT_SHIFTS_PER_DAY:
            _issue(issues, "班次差异", f"设备!{rec['__row__']}", f"{code} 为 {shifts} 班",
                   f"按 {shifts * config.HOURS_PER_SHIFT}h/天 计算产能")
        machines.append({"code": code, "dev_no": dev_no, "status": status, "ops": ops,
                         "shifts": shifts, "usable": usable, "row": rec["__row__"]})
    return machines, issues


def _judge_status(issues, rec, code, status):
    """外协 / 停机 / 闲置三种状态各记一条，别混成一句话。"""
    loc = f"设备!{rec['__row__']}"
    if any(k in status for k in ("外协",)):
        _issue(issues, "外协剔除", loc, f"{code} 状态「{status}」", "不计本厂产能，不进可用池")
        return False
    if any(k in status for k in config.UNAVAILABLE_KEYWORDS):
        _issue(issues, "设备状态", loc, f"{code} 状态「{status}」", "不进入可用机台池")
        return False
    if any(k in status for k in config.IDLE_KEYWORDS):
        _issue(issues, "设备状态", loc, f"{code} 状态「{status}」", "判定为可启用备机（假设 A5）")
    return True


def _parse_shifts(text):
    m = re.search(r"(\d)", text)
    return int(m.group(1)) if m else config.DEFAULT_SHIFTS_PER_DAY


# ---------------- 工艺路线 ----------------

def clean_routes(sheet):
    """洗工艺路线：把「下料（SAW-01）20」拆成 工序 / 机台 / 分钟，并与合计对账。"""
    issues, routes = [], []
    headers = sheet["headers"]
    for rec in sheet["records"]:
        product = _txt(rec.get("产品"))
        if not product:
            continue
        steps = _steps_of(rec, headers, issues)
        total = sum(s["minutes"] for s in steps)
        _route_issues(rec, product, total, headers, issues)
        routes.append({"product": product, "steps": steps, "total": total,
                       "remark": _txt(rec.get("备注")), "row": rec["__row__"]})
    return routes, issues


def _steps_of(rec, headers, issues):
    """工序1~5 逐个解析，解析不了的跳过并留痕。"""
    steps = []
    for i in range(1, 6):
        cell = _txt(rec.get(f"工序{i}"))
        if not cell:
            continue
        step = parse_op(cell)
        if step is None:
            _issue(issues, "工时解析", _loc("工艺路线", headers, f"工序{i}", rec["__row__"]),
                   f"「{cell}」无法解析", "跳过该工序，需人工核对", True)
            continue
        steps.append(step)
    return steps


def _route_issues(rec, product, total, headers, issues):
    """合计对不上 / 备注有口头规则，各记一条。"""
    declared = _parse_float(rec.get("单件工时合计"))
    if declared is not None and abs(declared - total) > 0.01:
        _issue(issues, "工时合计校验", _loc("工艺路线", headers, "单件工时合计", rec["__row__"]),
               f"{product} 合计 {declared} vs 逐道累加 {total}", "以逐道累加为准，提示核对", True)
    remark = _txt(rec.get("备注"))
    if remark:
        need = ("瓶颈" in remark) or ("别排太散" in remark)
        _issue(issues, "工艺备注", _loc("工艺路线", headers, "备注", rec["__row__"]),
               f"{product}：{remark}",
               "转人工确认口径" if need else "记录备查（机台指定已并入机台解析）", need)


def parse_op(text):
    """解析「工序（机台）分钟」，如 车（LAT）45。"""
    m = OP_RE.match(_txt(text))
    if not m:
        return None
    return {"op": m.group("op").strip(), "machine": m.group("mc").strip(),
            "minutes": float(m.group("mins"))}


def _parse_float(value):
    try:
        return float(_txt(value))
    except (TypeError, ValueError):
        return None


def clean_all(data):
    """三张表一起洗，issues 合并返回。"""
    orders, i1 = clean_orders(data["订单"])
    machines, i2 = clean_machines(data["设备"])
    routes, i3 = clean_routes(data["工艺路线"])
    return {"orders": orders, "machines": machines, "routes": routes}, i1 + i2 + i3
