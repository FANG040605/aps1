"""校验器的单测：故意造一个撞机，它必须抓得出来。

这是全项目最重要的一条测试——校验器抓不住问题，后面所有结论都不可信。
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import checker  # noqa: E402

T0 = datetime(2026, 11, 9, 8, 0)
MACHINES = {
    "CNC-01": {"code": "CNC-01", "ops": ["铣"], "usable": True, "shifts": 2, "status": "在用"},
    "CNC-03": {"code": "CNC-03", "ops": ["铣"], "usable": False, "shifts": 2, "status": "停机维修"},
}


def _task(oid, seq, minutes, machines=("CNC-01",)):
    return {"order_id": oid, "seq": seq, "op": "铣", "qty": 1,
            "per_piece": minutes, "minutes": minutes, "machines": list(machines)}


def _row(oid, seq, start_min, minutes, machine="CNC-01"):
    start = T0 + timedelta(minutes=start_min)
    return {"machine": machine, "order": oid, "seq": seq, "op": "铣", "qty": 1,
            "per_piece": minutes, "minutes": minutes, "start": start,
            "end": start + timedelta(minutes=minutes)}


class TestCheckOverlap(unittest.TestCase):
    """C4：同一台机两个活同时干。"""

    def test_overlap_is_caught(self):
        tasks = [_task("A", 1, 600), _task("B", 1, 600)]
        rows = [_row("A", 1, 0, 600), _row("B", 1, 60, 600)]
        v = checker.check(tasks, rows, MACHINES)
        self.assertTrue(any(x["code"] == checker.C4 for x in v), "撞机必须被 C4 抓到")

    def test_no_overlap_passes(self):
        tasks = [_task("A", 1, 600), _task("B", 1, 600)]
        rows = [_row("A", 1, 0, 600), _row("B", 1, 600, 600)]
        v = checker.check(tasks, rows, MACHINES)
        self.assertFalse([x for x in v if x["code"] == checker.C4])


class TestCheckOthers(unittest.TestCase):
    """C1 漏排 / C2 机台与工时 / C3 工序倒序。"""

    def test_missing_task_caught(self):
        tasks = [_task("A", 1, 600), _task("A", 2, 300)]
        rows = [_row("A", 1, 0, 600)]
        v = checker.check(tasks, rows, MACHINES)
        self.assertTrue(any(x["code"] == checker.C1 for x in v))

    def test_downtime_machine_caught(self):
        tasks = [_task("A", 1, 600, machines=("CNC-03",))]
        rows = [_row("A", 1, 0, 600, machine="CNC-03")]
        v = checker.check(tasks, rows, MACHINES)
        self.assertTrue(any(x["code"] == checker.C2 for x in v), "排到停机机台必须报")

    def test_wrong_minutes_caught(self):
        tasks = [_task("A", 1, 600)]
        rows = [_row("A", 1, 0, 480)]          # 工时被写少了
        v = checker.check(tasks, rows, MACHINES)
        self.assertTrue(any(x["code"] == checker.C2 for x in v))

    def test_sequence_reversed_caught(self):
        tasks = [_task("A", 1, 600), _task("A", 2, 300)]
        rows = [_row("A", 1, 0, 600, "CNC-01"), _row("A", 2, 300, 300, "CNC-01")]
        v = checker.check(tasks, rows, MACHINES)
        self.assertTrue(any(x["code"] == checker.C3 for x in v), "后道早于前道完工必须报")


class TestCheckC5(unittest.TestCase):
    """C5：不早于可开工时刻。这条以前是盲区——

    把一道工序整体提前 10 天（早于接单日），机台不撞、工时自洽、顺序不乱，
    C1~C4 全过，但计划员一眼就看穿"活还没接就开工了"。
    """

    def _task(self, oid, recv=None, material=None):
        task = _task(oid, 1, 120)
        task["recv"] = recv
        task["material_ready"] = material
        return task

    def test_start_before_recv_is_caught(self):
        tasks = [self._task("A", recv=datetime(2026, 11, 12).date())]
        rows = [_row("A", 1, 0, 120)]                       # 11-09 08:00，早于 11-12
        v = checker.check(tasks, rows, MACHINES, T0)
        self.assertTrue(any(x["code"] == checker.C5 for x in v), "早于接单日必须被 C5 抓到")

    def test_start_before_material_is_caught(self):
        tasks = [self._task("A", material=datetime(2026, 11, 20).date())]
        rows = [_row("A", 1, 0, 120)]
        v = checker.check(tasks, rows, MACHINES, T0)
        self.assertTrue(any(x["code"] == checker.C5 for x in v), "早于到料日必须被 C5 抓到")

    def test_start_after_floor_passes(self):
        tasks = [self._task("A", recv=datetime(2026, 11, 10).date())]
        rows = [_row("A", 1, 1500, 120)]                    # 11-10 09:00，晚于 11-10 08:00
        v = checker.check(tasks, rows, MACHINES, T0)
        self.assertFalse([x for x in v if x["code"] == checker.C5])


class TestCheckVanishedOrder(unittest.TestCase):
    """C1：整单凭空消失。只拿 tasks 当期望值时，tasks 少了一单反而查不出来。"""

    def test_whole_order_missing_is_caught(self):
        tasks = [_task("A", 1, 120)]
        orders = [{"order_id": "A", "due": None}, {"order_id": "B", "due": None}]
        rows = [_row("A", 1, 0, 120)]
        v = checker.check(tasks, rows, MACHINES, T0, orders=orders, skipped=[])
        self.assertTrue(any("整单未出现" in x["detail"] for x in v), "整单失踪必须报 C1")

    def test_skipped_order_is_not_flagged(self):
        """数据不全被判定"未排产"的单，不算违反——它已经在未排产清单里了。"""
        tasks = [_task("A", 1, 120)]
        orders = [{"order_id": "A", "due": None}, {"order_id": "B", "due": None}]
        rows = [_row("A", 1, 0, 120)]
        v = checker.check(tasks, rows, MACHINES, T0,
                          orders=orders, skipped=[{"order": {"order_id": "B"}, "reason": "数量缺失"}])
        self.assertFalse([x for x in v if "整单未出现" in x["detail"]])


if __name__ == "__main__":
    unittest.main()
