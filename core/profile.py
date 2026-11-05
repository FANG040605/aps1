"""产线基线：设备表 + 工艺路线表，配一次，长期复用。

车间里天天变的是订单；设备（除非维修、损坏、新买）和工艺路线基本不动。
所以日常排产只该交一张订单表——设备与工艺路线存在这里，配一次就行。

存成普通 .xlsx 而不是 json，有两个原因：
1) 计划员能直接用 Excel 打开看、改、发给设备科确认，不需要找我们；
2) 每次排产都重读一遍（十几台设备几十毫秒），改完立刻生效，
   不用再点一次"保存配置"这类容易忘的动作。

文件里是"设备 + 工艺路线"上下两段，正好是 reader 认的第二种布局，
所以基线自己能当台账读回来，不会出现"存进去读不出来"。
"""

import hashlib
import json
import os
import shutil
from datetime import datetime

from openpyxl import Workbook

from . import reader

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLES = ("设备", "工艺路线")
BACKUP_NAME = "产线基线.上次.xlsx"


def default_path():
    return os.path.join(HERE, "data", "产线基线.xlsx")


def exists(path=None):
    return os.path.exists(path or default_path())


def load(path=None):
    """读回基线；没有基线返回 None。

    缺设备或缺工艺路线会抛 WorkbookError——那说明文件被人改坏了，
    要让用户知道，而不是悄悄用一份空基线排出个空计划。
    """
    path = path or default_path()
    if not os.path.exists(path):
        return None
    return reader.read_workbook(path, required=TABLES)


def save(data, path=None):
    """把设备表与工艺路线表写成基线文件。

    覆盖前先留一份「产线基线.上次.xlsx」：客户传错一份台账把基线冲掉时，
    还能拿回上一次的，不至于要重新找设备科要数据。
    """
    path = path or default_path()
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    if os.path.exists(path):
        shutil.copyfile(path, os.path.join(folder, BACKUP_NAME))
    wb = Workbook()
    ws = wb.active
    ws.title = "产线"
    row = 1
    for name in TABLES:
        if name not in data:
            continue
        ws.cell(row, 1).value = f"{name}表"          # 段标题：只有一个格子有字，reader 当注释行跳过
        row += 1
        headers = data[name]["headers"]              # 保留原列位置，空表头也占位，避免列错位
        for col, head in enumerate(headers, 1):
            if head:
                ws.cell(row, col).value = head
        row += 1
        for rec in data[name]["records"]:
            for col, head in enumerate(headers, 1):
                value = rec.get(head) if head else None
                if value is not None and str(value).strip() != "":
                    ws.cell(row, col).value = value
            row += 1
        row += 2
    wb.save(path)
    return path


def fingerprint(clean):
    """设备与工艺的内容指纹：只看内容，不看列顺序、不看空白。

    用来判断"基线是不是动过"。计划员改完设备表忘了重排，手上还是旧结果；
    报告里带上指纹比对，就能一眼看出这份结果是不是按最新基线算的。
    """
    machines = sorted(
        [m["code"], m.get("status", ""), m.get("shifts", 0), sorted(m.get("ops", [])),
         bool(m.get("usable"))] for m in clean["machines"]
    )
    routes = sorted(
        [r["product"], [[s["op"], s["machine"], s["minutes"]] for s in r["steps"]]]
        for r in clean["routes"]
    )
    raw = json.dumps([machines, routes], ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def describe(clean):
    """一行话说清这份基线里有什么，给页面和产物抬头用。"""
    machines = clean["machines"]
    down = [m["code"] for m in machines if not m["usable"]]
    text = f"{len(machines)} 台设备"
    if down:
        shown = "、".join(down[:4]) + ("…" if len(down) > 4 else "")
        text += f"（{len(down)} 台不可用：{shown}）"
    ops = sorted({op for m in machines for op in m.get("ops", [])})
    text += f" ｜ {len(clean['routes'])} 个产品工艺 ｜ 覆盖工序 {'、'.join(ops)}"
    return text


def mtime(path=None):
    """基线文件的修改时间，页面显示"上次更新"用。"""
    path = path or default_path()
    if not os.path.exists(path):
        return None
    return datetime.fromtimestamp(os.path.getmtime(path))


def machines_of(clean):
    """给页面用的机台清单（含"当前是否可用"）。"""
    return [{"code": m["code"], "ops": m.get("ops", []), "shifts": m.get("shifts", 0),
             "status": m.get("status", ""), "usable": bool(m.get("usable"))}
            for m in clean["machines"]]


def products_of(clean):
    """给页面用的产品工艺清单。"""
    return [{"product": r["product"],
             "steps": [f'{s["op"]}（{s["machine"]}）{s["minutes"]:g}' for s in r["steps"]],
             "total": round(r["total"], 1)}
            for r in clean["routes"]]
