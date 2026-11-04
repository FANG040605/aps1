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


if __name__ == "__main__":
    unittest.main()
