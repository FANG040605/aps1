"""机台解析的单测：点名停机机台必须改派，不能硬排。"""

import os
import sys
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.instance import _earliest, load_instance, resolve_machines  # noqa: E402

MACHINES = [
    {"code": "CNC-01", "ops": ["铣"], "usable": True},
    {"code": "CNC-02", "ops": ["铣"], "usable": True},
    {"code": "CNC-03", "ops": ["铣", "镗"], "usable": False},
    {"code": "LAT-01", "ops": ["车"], "usable": True},
    {"code": "LAT-03", "ops": ["车"], "usable": True},
    {"code": "GRD-01", "ops": ["磨"], "usable": True},
    {"code": "GRD-02", "ops": ["磨"], "usable": True},
]


class TestResolve(unittest.TestCase):
    def test_group_hint_picks_all(self):
        """车（LAT）是机台组，两台在用都要给。"""
        codes, _ = resolve_machines("LAT", "车", MACHINES, allow_switch=True)
        self.assertEqual(sorted(codes), ["LAT-01", "LAT-03"])

    def test_named_machine_preferred(self):
        """磨（GRD-01）点名 GRD-01，它排第一，闲置的 GRD-02 可替补。"""
        codes, _ = resolve_machines("GRD-01", "磨", MACHINES, allow_switch=True)
        self.assertEqual(codes[0], "GRD-01")
        self.assertIn("GRD-02", codes)

    def test_named_but_down_reroutes(self):
        """阀体A 铣（CNC-03），而 CNC-03 停机 → 必须改派到 CNC-01/02 并留痕。"""
        codes, note = resolve_machines("CNC-03", "铣", MACHINES, allow_switch=True)
        self.assertEqual(sorted(codes), ["CNC-01", "CNC-02"])
        self.assertIn("CNC-03", note)

    def test_no_switch_keeps_strict(self):
        """不允许换机台时，点名停机机台只能报不可排。"""
        codes, note = resolve_machines("CNC-03", "铣", MACHINES, allow_switch=False)
        self.assertEqual(codes, [])
        self.assertTrue(note)


class TestEarliest(unittest.TestCase):
    """回归：起始日调早以后，不能让工序排到接单日之前。

    现场：客户把起始日改到 11/01，排单表出现"11/02 开工、11/05 才接单"，
    活还没接就开工了。原因是 _earliest 只看了到料日，漏了接单日。
    """

    def test_not_before_recv(self):
        od = {"recv": date(2026, 11, 6), "material_ready": None}
        got = _earliest(od, datetime(2026, 11, 1, 8, 0))
        self.assertEqual(got, datetime(2026, 11, 6, 8, 0), "应顺延到接单当天 08:00")

    def test_not_before_material(self):
        """到料日比接单日更晚时，取更晚的那个。"""
        od = {"recv": date(2026, 11, 6), "material_ready": date(2026, 11, 12)}
        got = _earliest(od, datetime(2026, 11, 1, 8, 0))
        self.assertEqual(got, datetime(2026, 11, 12, 8, 0))

    def test_start_later_than_both_wins(self):
        od = {"recv": date(2026, 11, 6), "material_ready": None}
        got = _earliest(od, datetime(2026, 11, 20, 8, 0))
        self.assertEqual(got, datetime(2026, 11, 20, 8, 0), "起始日更晚时以起始日为准")


if __name__ == "__main__":
    unittest.main()


class TestNoMachineDegrade(unittest.TestCase):
    """一道工序找不到机台，只能让这一单不排，绝不能拖垮整批。

    以前的做法是记一条 issue，然后把"机台候选为空"的任务继续传给求解器，
    求解器一抛 RuntimeError，14 张单全部出不来。
    """

    def _clean(self, machines):
        return {
            "orders": [{"order_id": "HD-X-001", "product": "P", "qty": 10, "recv": date(2026, 11, 3),
                        "due": None, "due_text": "", "urgent": False, "material_ready": None,
                        "customer": "甲", "remark": "", "row": 3}],
            "machines": machines,
            "routes": [{"product": "P", "steps": [{"op": "磨", "machine": "GRD-01", "minutes": 10}],
                        "total": 10}],
        }

    def test_no_machine_skips_order_instead_of_raising(self):
        machines = [{"code": "LAT-01", "ops": ["车"], "shifts": 2, "usable": True, "status": "在用"}]
        tasks, skipped, issues = load_instance(self._clean(machines), True, datetime(2026, 11, 9, 8))
        self.assertEqual(tasks, [], "无机台的工序不能变成空机台任务")
        self.assertEqual(len(skipped), 1)
        self.assertIn("磨", skipped[0]["reason"])

    def test_issue_is_marked_need_confirm(self):
        machines = [{"code": "LAT-01", "ops": ["车"], "shifts": 2, "usable": True, "status": "在用"}]
        _, _, issues = load_instance(self._clean(machines), True, datetime(2026, 11, 9, 8))
        self.assertTrue(any(i["need_confirm"] for i in issues), "缺机台必须标需客户确认")

    def test_normal_order_still_planned(self):
        machines = [{"code": "GRD-01", "ops": ["磨"], "shifts": 2, "usable": True, "status": "在用"}]
        tasks, skipped, _ = load_instance(self._clean(machines), True, datetime(2026, 11, 9, 8))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(skipped, [])
