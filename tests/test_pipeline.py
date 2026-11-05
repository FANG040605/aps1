"""流水线三种输入：完整台账 / 只交订单 / 订单 + 设备故障。

这是这次改造的核心承诺，必须钉死：
车间天天变的只有订单，设备和工艺路线配一次就该一直用。
三种输入下，同一批订单必须排出同一张表——否则"少传两张表"就成了偷偷改结果。
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openpyxl import Workbook  # noqa: E402

from core import events, pipeline, profile  # noqa: E402
from core.reader import WorkbookError  # noqa: E402

NOW = datetime(2026, 11, 5, 16, 0)
OUT_HEAD = ["单号", "客户", "接单日期", "交期", "产品", "数量", "备注"]
OUT_ROWS = [
    ["HD-2026-101", "甲", "2026-11-03", "2026-11-30", "阀体A", 10, ""],
    ["HD-2026-102", "乙", "2026-11-04", "2026-11-30", "阀体A", 6, ""],
]
DEV_HEAD = ["设备号", "编号", "名称", "可做工序", "每日班次", "状态"]
DEV_ROWS = [
    ["1", "LAT-01", "车床", "车", "2", "在用"],
    ["2", "GRD-01", "磨床", "磨", "2", "在用"],
]
ROUTE_HEAD = ["产品", "工序1", "工序2", "单件工时合计", "备注"]
ROUTE_ROWS = [["阀体A", "车（LAT-01）10", "磨（GRD-01）5", 15, ""]]


def _sheet(ws, header, rows):
    ws.append(header)
    for r in rows:
        ws.append(r)


def make_xlsx(path, orders=None, with_base=True):
    """造一份台账；with_base=False 时只有订单表（模拟计划员每天真正要交的东西）。"""
    wb = Workbook()
    wb.remove(wb.active)
    _sheet(wb.create_sheet("订单"), OUT_HEAD, orders if orders is not None else OUT_ROWS)
    if with_base:
        _sheet(wb.create_sheet("设备"), DEV_HEAD, DEV_ROWS)
        _sheet(wb.create_sheet("工艺路线"), ROUTE_HEAD, ROUTE_ROWS)
    wb.save(path)
    return path


def sig(run):
    return [(r["machine"], r["order"], r["seq"], r["start"], r["end"]) for r in run["rows"]]


class PipelineCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.profile_path = os.path.join(self.dir, "产线基线.xlsx")
        self.events_path = os.path.join(self.dir, "设备事件.json")
        self.full = make_xlsx(os.path.join(self.dir, "完整台账.xlsx"))
        self.orders_only = make_xlsx(os.path.join(self.dir, "订单.xlsx"), with_base=False)

    def tearDown(self):
        self.tmp.cleanup()

    def run_full(self, tag="full"):
        return pipeline.run_pipeline(self.full, os.path.join(self.dir, tag), None, True,
                                     now=NOW, profile_path=self.profile_path,
                                     events_path=self.events_path)

    def run_orders(self, tag="orders"):
        return pipeline.run_pipeline(self.orders_only, os.path.join(self.dir, tag), None, True,
                                     now=NOW, profile_path=self.profile_path,
                                     events_path=self.events_path)


class TestBaseProfile(PipelineCase):

    def test_full_workbook_saves_profile_automatically(self):
        """配一次：完整台账传过一次，设备和工艺就存下来了。"""
        run = self.run_full()
        self.assertEqual(run["profile"]["source"], "本次上传")
        self.assertTrue(profile.exists(self.profile_path))

    def test_orders_only_uses_profile_and_matches_full_run(self):
        """只交订单，排出来的表必须和交全部三张表时一模一样。"""
        a = self.run_full("a")
        b = self.run_orders("b")
        self.assertEqual(b["profile"]["source"], "产线基线")
        self.assertEqual(sig(a), sig(b))
        self.assertEqual(a["makespan"], b["makespan"])
        self.assertEqual(len(a["violations"]), len(b["violations"]))

    def test_orders_only_without_profile_says_how_to_start(self):
        """第一次用就只传订单 → 说清楚要先传一次完整台账，别排个空计划糊弄人。"""
        with self.assertRaises(WorkbookError) as ctx:
            self.run_orders()
        self.assertIn("产线基线", str(ctx.exception))
        self.assertIn("第一次", str(ctx.exception))

    def test_changed_profile_is_reported(self):
        """设备表改过就要留痕：计划员改完忘了重排时能看出来。"""
        self.run_full("v1")
        changed = make_xlsx(os.path.join(self.dir, "改过设备.xlsx"),
                            orders=OUT_ROWS, with_base=False)
        # 手工把设备表塞成"两台磨床"，模拟设备科改了产线
        wb = Workbook()
        wb.remove(wb.active)
        _sheet(wb.create_sheet("订单"), OUT_HEAD, OUT_ROWS)
        _sheet(wb.create_sheet("设备"), DEV_HEAD,
               [["1", "LAT-01", "车床", "车", "3", "在用"],
                ["2", "GRD-01", "磨床", "磨", "2", "在用"]])
        _sheet(wb.create_sheet("工艺路线"), ROUTE_HEAD, ROUTE_ROWS)
        wb.save(changed)
        run = pipeline.run_pipeline(changed, os.path.join(self.dir, "v2"), None, True,
                                    now=NOW, profile_path=self.profile_path,
                                    events_path=self.events_path)
        self.assertTrue(run["profile"]["changed"])
        self.assertTrue(any(i["category"] == "产线基线" for i in run["issues"]))


class TestRollback(PipelineCase):

    def test_missing_qty_goes_to_rollback(self):
        orders = [OUT_ROWS[0], ["HD-2026-103", "丙", "2026-11-04", "2026-11-30", "阀体A", "", "待定"]]
        path = make_xlsx(os.path.join(self.dir, "缺数量.xlsx"), orders=orders)
        run = pipeline.run_pipeline(path, os.path.join(self.dir, "r1"), None, True,
                                    now=NOW, profile_path=self.profile_path,
                                    events_path=self.events_path)
        self.assertEqual(len(run["rollback"]), 1)
        item = run["rollback"][0]
        self.assertEqual(item["order"], "HD-2026-103")
        self.assertIn("数量", item["reason"])
        self.assertIn("数量", item["fix"])
        self.assertIn("第", item["where"])
        # 其余订单照排，不能因为一张单坏了就整批不排
        self.assertEqual({r["order"] for r in run["rows"]}, {"HD-2026-101"})

    def test_unknown_product_goes_to_rollback(self):
        orders = [["HD-2026-104", "丁", "2026-11-04", "2026-11-30", "阀体Z", 5, ""]]
        path = make_xlsx(os.path.join(self.dir, "未知产品.xlsx"), orders=orders)
        run = pipeline.run_pipeline(path, os.path.join(self.dir, "r2"), None, True,
                                    now=NOW, profile_path=self.profile_path,
                                    events_path=self.events_path)
        self.assertEqual(len(run["rollback"]), 1)
        self.assertIn("工艺路线", run["rollback"][0]["fix"])
        self.assertEqual(run["rows"], [])

    def test_rollback_sheet_is_written(self):
        orders = [["HD-2026-105", "戊", "2026-11-04", "2026-11-30", "阀体A", "", ""]]
        path = make_xlsx(os.path.join(self.dir, "退回.xlsx"), orders=orders)
        run = pipeline.run_pipeline(path, os.path.join(self.dir, "r3"), None, True,
                                    now=NOW, profile_path=self.profile_path,
                                    events_path=self.events_path)
        self.assertTrue(os.path.exists(run["paths"]["rollback"]))


class TestFaultReroute(PipelineCase):

    def test_fault_pushes_work_out_of_window(self):
        """磨床停到 11-12，磨工序不能排进停机窗口。"""
        self.run_full("base")
        events.add("GRD-01", "故障", until=datetime(2026, 11, 12, 8),
                   start=datetime(2026, 11, 6, 8), path=self.events_path, now=NOW)
        run = self.run_orders("after")
        window = (datetime(2026, 11, 6, 8), datetime(2026, 11, 12, 8))
        for r in run["rows"]:
            if r["machine"] != "GRD-01":
                continue
            clash = r["start"] < window[1] and window[0] < r["end"]
            self.assertFalse(clash, f'{r["order"]} 的磨工序排进了停机窗口：{r["start"]}→{r["end"]}')
        self.assertIn("停机事件", run["params"])

    def test_recover_restores_original_plan(self):
        """修好之后重排，应该回到和没有故障时一样的表。"""
        base = self.run_full("base")
        events.add("GRD-01", "故障", until=datetime(2026, 11, 12, 8),
                   start=datetime(2026, 11, 6, 8), path=self.events_path, now=NOW)
        self.run_orders("during")
        events.recover("GRD-01", path=self.events_path, now=datetime(2026, 11, 5, 17))
        after = self.run_orders("after")
        self.assertEqual(sig(base), sig(after))

    def test_only_machine_down_becomes_rollback(self):
        """唯一能做磨的机台长期停 → 那两单退回待补，而不是硬排一个假计划。"""
        self.run_full("base")
        events.add("GRD-01", "故障", path=self.events_path, now=NOW)   # 归期未定
        run = self.run_orders("down")
        self.assertEqual(run["rows"], [], "磨床全停时不该还有工序能排出来")
        self.assertEqual(len(run["rollback"]), 2)
        self.assertTrue(all("机台" in r["reason"] or "磨" in r["reason"] for r in run["rollback"]))


if __name__ == "__main__":
    unittest.main()
