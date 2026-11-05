"""产线基线测试：设备与工艺路线配一次，之后只交订单。

关键约束是"存进去必须能读回来"——基线存成 xlsx 是为了让计划员能直接用
Excel 打开改，所以它必须同时是 reader 认得的格式，不能变成只有程序懂的私有结构。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openpyxl import Workbook  # noqa: E402

from core import cleaner, profile, reader  # noqa: E402


def _sheet(ws, header, rows):
    ws.append(header)
    for r in rows:
        ws.append(r)


def make_tables(path):
    """造一份三表齐全的台账（设备 2 台、工艺 1 个产品）。"""
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("订单")
    _sheet(ws, ["单号", "客户", "接单日期", "交期", "产品", "数量", "备注"],
           [["HD-2026-101", "甲", "2026-11-03", "2026-11-10", "阀体A", 40, ""]])
    ws = wb.create_sheet("设备")
    _sheet(ws, ["设备号", "编号", "名称", "可做工序", "每日班次", "状态"],
           [["1", "LAT-01", "车床", "车", "2", "在用"],
            ["2", "GRD-01", "磨床", "磨", "2", "停机维修"]])
    ws = wb.create_sheet("工艺路线")
    _sheet(ws, ["产品", "工序1", "工序2", "单件工时合计", "备注"],
           [["阀体A", "车（LAT-01）10", "磨（GRD-01）5", 15, ""]])
    wb.save(path)
    return path


class TestSaveLoad(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "产线基线.xlsx")
        self.tables = reader.read_workbook(make_tables(os.path.join(self.tmp.name, "台账.xlsx")))

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_profile_returns_none(self):
        self.assertIsNone(profile.load(self.path))
        self.assertFalse(profile.exists(self.path))

    def test_save_then_load_keeps_every_record(self):
        profile.save(self.tables, self.path)
        back = profile.load(self.path)
        self.assertEqual(len(back["设备"]["records"]), 2)
        self.assertEqual(len(back["工艺路线"]["records"]), 1)
        self.assertEqual(back["设备"]["records"][0]["编号"], "LAT-01")
        self.assertEqual(back["工艺路线"]["records"][0]["工序2"], "磨（GRD-01）5")

    def test_saved_file_is_readable_as_normal_workbook(self):
        """基线自己必须是一份合法台账，否则客户改完就读不回来了。"""
        profile.save(self.tables, self.path)
        back = reader.read_workbook(self.path, required=profile.TABLES)
        self.assertEqual(sorted(back), ["工艺路线", "设备"])

    def test_second_save_keeps_a_backup(self):
        """传错一份台账把基线冲掉时，还能拿回上一次的。"""
        profile.save(self.tables, self.path)
        self.tables["设备"]["records"].pop()          # 模拟少了一台设备
        profile.save(self.tables, self.path)
        backup = os.path.join(self.tmp.name, profile.BACKUP_NAME)
        self.assertTrue(os.path.exists(backup), "覆盖前应留一份上次的基线")
        old = reader.read_workbook(backup, required=profile.TABLES)
        self.assertEqual(len(old["设备"]["records"]), 2)


class TestFingerprint(unittest.TestCase):

    def _clean(self, machines, routes):
        return {"machines": machines, "routes": routes}

    def _m(self, code, ops, shifts=2, usable=True, status="在用"):
        return {"code": code, "ops": ops, "shifts": shifts, "usable": usable, "status": status}

    def _r(self, product, steps):
        return {"product": product, "steps": steps, "total": sum(s["minutes"] for s in steps)}

    def test_same_content_same_fingerprint(self):
        a = self._clean([self._m("A", ["车"])], [self._r("P", [{"op": "车", "machine": "A", "minutes": 10}])])
        b = self._clean([self._m("A", ["车"])], [self._r("P", [{"op": "车", "machine": "A", "minutes": 10}])])
        self.assertEqual(profile.fingerprint(a), profile.fingerprint(b))

    def test_shift_change_moves_fingerprint(self):
        a = self._clean([self._m("A", ["车"])], [])
        b = self._clean([self._m("A", ["车"], shifts=3)], [])
        self.assertNotEqual(profile.fingerprint(a), profile.fingerprint(b))

    def test_status_change_moves_fingerprint(self):
        a = self._clean([self._m("A", ["车"])], [])
        b = self._clean([self._m("A", ["车"], usable=False, status="停机维修")], [])
        self.assertNotEqual(profile.fingerprint(a), profile.fingerprint(b))

    def test_column_order_does_not_matter(self):
        """设备表列顺序被人调了，不算"基线变了"——只看内容。"""
        a = self._clean([self._m("A", ["车"])], [])
        b = self._clean([{"status": "在用", "ops": ["车"], "code": "A", "shifts": 2, "usable": True}], [])
        self.assertEqual(profile.fingerprint(a), profile.fingerprint(b))


class TestDescribe(unittest.TestCase):

    def test_counts_machines_products_and_down_set(self):
        clean = {"machines": [{"code": "A", "ops": ["车"], "usable": True, "status": "在用"},
                              {"code": "B", "ops": ["磨"], "usable": False, "status": "停机"}],
                 "routes": [{"product": "P", "steps": [], "total": 0},
                            {"product": "Q", "steps": [], "total": 0}]}
        text = profile.describe(clean)
        self.assertIn("2 台设备", text)
        self.assertIn("1 台不可用：B", text)
        self.assertIn("2 个产品工艺", text)


if __name__ == "__main__":
    unittest.main()
