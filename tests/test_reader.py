"""读取层测试：三张表怎么放都行。

这里刻意不提交任何 .xlsx 客户台账（.gitignore 屏蔽了所有 xlsx），
用例全部在临时目录里用 openpyxl 现场造表——换台机器也能跑出同样结果。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openpyxl import Workbook  # noqa: E402

from core import reader  # noqa: E402

ORDER_HEAD = ["单号", "客户", "接单日期", "交期", "产品", "数量", "备注"]
ORDER_ROWS = [
    ["HD-2026-101", "甲", "2026-11-03", "2026-11-10", "阀体A", 40, ""],
    ["HD2026102", "乙", "2026-11-04", "尽快", "阀体B", 25, "客户催"],
]
MACHINE_HEAD = ["设备号", "编号", "名称", "可做工序", "每日班次", "状态"]
MACHINE_ROWS = [
    ["1", "CNC-01", "加工中心", "铣", "2", "在用"],
    ["2", "CNC-03", "加工中心", "铣", "2", "停机维修"],
]
ROUTE_HEAD = ["产品", "工序1", "工序2", "单件工时合计", "备注"]
ROUTE_ROWS = [
    ["阀体A", "下料（SAW-01）20", "铣（CNC）45", 65, ""],
    ["阀体B", "下料（SAW-01）20", "", 20, "瓶颈在铣"],
]


def _fill(ws, row, values):
    for c, v in enumerate(values, 1):
        ws.cell(row, c).value = v
    return row + 1


def _three_sheets(path):
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("订单")
    _fill(ws, 1, ["宏达精密 · 待排产订单"])
    _fill(ws, 2, ORDER_HEAD)
    row = 3
    for r in ORDER_ROWS:
        row = _fill(ws, row, r)
    _fill(ws, row, ["注：交期一栏是客户原话"])          # 单格注释行，应跳过
    ws2 = wb.create_sheet("设备")
    _fill(ws2, 2, MACHINE_HEAD)
    row = 3
    for r in MACHINE_ROWS:
        row = _fill(ws2, row, r)
    ws3 = wb.create_sheet("工艺路线")
    _fill(ws3, 2, ROUTE_HEAD)
    row = 3
    for r in ROUTE_ROWS:
        row = _fill(ws3, row, r)
    wb.save(path)


def _one_sheet(path, sheet_name="台账"):
    """三张表上下排在一张 sheet 里，段与段之间空两行。"""
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    row = 1
    for title, head, rows in (("订单表", ORDER_HEAD, ORDER_ROWS),
                              ("设备表", MACHINE_HEAD, MACHINE_ROWS),
                              ("工艺路线表", ROUTE_HEAD, ROUTE_ROWS)):
        _fill(ws, row, [title])
        row += 1
        _fill(ws, row, head)
        row += 1
        for r in rows:
            row = _fill(ws, row, r)
        _fill(ws, row, ["注：这一段的备注"])            # 注释行
        row += 3
    wb.save(path)


class ReaderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aps_reader_")

    def path(self, name):
        return os.path.join(self.tmp, name)


class TestThreeSheets(ReaderCase):
    """老布局（客户台账原件就是这样）不能因为改造而读错。"""

    def test_reads_all_three(self):
        p = self.path("a.xlsx")
        _three_sheets(p)
        data = reader.read_workbook(p)
        self.assertEqual(sorted(data), sorted(reader.TABLES))
        self.assertEqual(len(data["订单"]["records"]), 2)
        self.assertEqual(len(data["设备"]["records"]), 2)
        self.assertEqual(len(data["工艺路线"]["records"]), 2)

    def test_note_row_is_skipped(self):
        p = self.path("a.xlsx")
        _three_sheets(p)
        data = reader.read_workbook(p)
        self.assertFalse([r for r in data["订单"]["records"]
                          if "注：" in str(r.get("单号") or "")])

    def test_layout_note_says_three_sheets(self):
        p = self.path("a.xlsx")
        _three_sheets(p)
        self.assertIn("三张工作表", reader.layout_note(reader.read_workbook(p)))


class TestOneSheet(ReaderCase):
    """单 sheet 三段：这次改造要支持的主场景。"""

    def test_reads_three_regions_in_one_sheet(self):
        p = self.path("b.xlsx")
        _one_sheet(p)
        data = reader.read_workbook(p)
        self.assertEqual(len(data["订单"]["records"]), 2)
        self.assertEqual(len(data["设备"]["records"]), 2)
        self.assertEqual(len(data["工艺路线"]["records"]), 2)

    def test_regions_do_not_leak_into_each_other(self):
        """订单段不能把后面设备段、工艺路线段的行也吃进来。"""
        p = self.path("b.xlsx")
        _one_sheet(p)
        data = reader.read_workbook(p)
        orders = data["订单"]["records"]
        self.assertNotIn("CNC-01", [str(r.get("单号")) for r in orders])
        machines = data["设备"]["records"]
        self.assertNotIn("阀体A", [str(r.get("设备号")) for r in machines])

    def test_sheet_name_is_carried_for_issue_location(self):
        """体检位置要写真实工作表名，客户照着才找得到那张表。"""
        p = self.path("b.xlsx")
        _one_sheet(p, sheet_name="宏达台账")
        data = reader.read_workbook(p)
        for name in reader.TABLES:
            self.assertEqual(data[name]["sheet"], "宏达台账")
            self.assertTrue(data[name]["records"])
            for rec in data[name]["records"]:
                self.assertEqual(rec["__sheet__"], "宏达台账")

    def test_header_row_found_below_title_rows(self):
        """表头前面有几行标题也不能死在第 2 行。"""
        p = self.path("c.xlsx")
        wb = Workbook()
        ws = wb.active
        ws.title = "台账"
        _fill(ws, 1, ["宏达精密 · 本周待排产"])
        _fill(ws, 2, ["编制：计划员"])
        _fill(ws, 3, ["2026-11-05"])
        _fill(ws, 4, ORDER_HEAD)
        row = 5
        for r in ORDER_ROWS:
            row = _fill(ws, row, r)
        _fill(ws, row + 2, MACHINE_HEAD)
        row = row + 3
        for r in MACHINE_ROWS:
            row = _fill(ws, row, r)
        _fill(ws, row + 2, ROUTE_HEAD)
        row = row + 3
        for r in ROUTE_ROWS:
            row = _fill(ws, row, r)
        wb.save(p)
        data = reader.read_workbook(p)
        self.assertEqual(data["订单"]["header_row"], 4)
        self.assertEqual(len(data["订单"]["records"]), 2)

    def test_layout_note_says_one_sheet(self):
        p = self.path("b.xlsx")
        _one_sheet(p)
        note = reader.layout_note(reader.read_workbook(p))
        self.assertIn("一张表三种区域", note)

    def test_custom_sheet_name_still_works(self):
        """工作表叫 Sheet1 也无所谓——靠表头认，不靠表名认。"""
        p = self.path("d.xlsx")
        _one_sheet(p, sheet_name="Sheet1")
        data = reader.read_workbook(p)
        self.assertEqual(len(data["订单"]["records"]), 2)


class TestSameResultEitherLayout(ReaderCase):
    """同一份数据，两种放法必须读出完全一样的内容。"""

    def test_records_identical(self):
        p1, p2 = self.path("t.xlsx"), self.path("o.xlsx")
        _three_sheets(p1)
        _one_sheet(p2)
        a = reader.read_workbook(p1)
        b = reader.read_workbook(p2)
        for name in reader.TABLES:
            strip = lambda recs: [{k: v for k, v in r.items() if not k.startswith("__")}
                                  for r in recs]
            self.assertEqual(strip(a[name]["records"]), strip(b[name]["records"]),
                             f"{name} 两种布局读出来的内容不一致")
            self.assertEqual(a[name]["headers"], b[name]["headers"])


class TestMissingTable(ReaderCase):
    def test_message_lists_both_layouts(self):
        p = self.path("only_order.xlsx")
        wb = Workbook()
        ws = wb.active
        _fill(ws, 1, ORDER_HEAD)
        _fill(ws, 2, ORDER_ROWS[0])
        wb.save(p)
        with self.assertRaises(reader.WorkbookError) as ctx:
            reader.read_workbook(p)
        msg = str(ctx.exception)
        self.assertIn("三个工作表", msg)
        self.assertIn("上下排三段", msg)
        self.assertIn("设备", msg)
        self.assertIn("工艺路线", msg)

    def test_empty_workbook_fails_readably(self):
        p = self.path("empty.xlsx")
        Workbook().save(p)
        with self.assertRaises(reader.WorkbookError):
            reader.read_workbook(p)


class TestHeaderGuess(unittest.TestCase):
    """订单和工艺路线都含「产品」，不能只看这一个词。"""

    def test_order_header_wins_on_danjia(self):
        vals = ["单号", "客户", "接单日期", "交期", "产品", "数量"]
        self.assertEqual(reader._guess_table(vals)[0], "订单")

    def test_route_header_wins_on_gongxu(self):
        vals = ["产品", "工序1", "工序2", "单件工时合计"]
        self.assertEqual(reader._guess_table(vals)[0], "工艺路线")

    def test_machine_header(self):
        vals = ["设备号", "编号", "可做工序", "状态"]
        self.assertEqual(reader._guess_table(vals)[0], "设备")

    def test_plain_data_row_is_not_a_header(self):
        vals = ["HD-2026-101", "甲", "2026-11-03", "2026-11-10", "阀体A", "40"]
        self.assertLess(reader._guess_table(vals)[1], reader.MIN_HITS)


if __name__ == "__main__":
    unittest.main()
