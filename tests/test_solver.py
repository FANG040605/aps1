"""求解器的单测：目标函数、确定性、交期下界。

这一层以前一条测试都没有——只有 cleaner / instance / checker 被测过，
结果"目标函数不认交期"这种最要命的问题，26 个测试全绿也发现不了。
"""

import os
import sys
import unittest
from datetime import date, datetime, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from core import checker, solver  # noqa: E402

T0 = datetime(2026, 11, 9, 8, 0)          # 周一 08:00
MACHINES = {"M1": {"code": "M1", "ops": ["铣"], "usable": True, "shifts": 2, "status": "在用"}}


def _task(oid, minutes, due, seq=1, urgent=False, recv=date(2026, 11, 1)):
    return {"order_id": oid, "product": "P", "seq": seq, "op": "铣", "qty": 1,
            "per_piece": minutes, "minutes": minutes, "machines": ["M1"],
            "earliest": T0, "urgent": urgent, "recv": recv, "due": due,
            "due_text": "", "customer": "甲", "material_ready": None}


def _due(d):
    return datetime.combine(d, time(config.DUE_HOUR))


class TestScore(unittest.TestCase):
    """评分必须是字典序：先少迟到单，再少迟到天数，最后才看总完工。"""

    def _rows(self, entries):
        """entries: [(单号, 完工时刻)] → 假的排程结果。"""
        return [{"order": oid, "end": end, "start": end, "machine": "M1", "seq": 1,
                 "op": "铣", "qty": 1, "per_piece": 10, "minutes": 10, "product": "P"}
                for oid, end in entries]

    def test_one_late_beat_earlier_finish(self):
        due = {"A": _due(date(2026, 11, 11))}
        # 方案一：A 迟到 1 单，但总完工更早；方案二：A 准时，但总完工更晚
        bad = self._rows([("A", datetime(2026, 11, 12, 9, 0))])
        good = self._rows([("A", datetime(2026, 11, 11, 9, 0))])
        self.assertLess(solver._score(good, due, T0), solver._score(bad, due, T0))

    def test_fewer_late_days_wins(self):
        due = {"A": _due(date(2026, 11, 10)), "B": _due(date(2026, 11, 10))}
        near = self._rows([("A", datetime(2026, 11, 10, 18, 0)), ("B", datetime(2026, 11, 10, 9, 0))])
        far = self._rows([("A", datetime(2026, 11, 14, 9, 0)), ("B", datetime(2026, 11, 10, 9, 0))])
        self.assertLess(solver._score(near, due, T0), solver._score(far, due, T0))

    def test_makespan_only_breaks_ties(self):
        due = {}                                   # 没有交期：只剩总完工可比
        early = self._rows([("A", datetime(2026, 11, 10, 9, 0))])
        late = self._rows([("A", datetime(2026, 11, 11, 9, 0))])
        self.assertLess(solver._score(early, due, T0), solver._score(late, due, T0))


class TestSchedule(unittest.TestCase):
    """端到端：同一个输入必然同一个输出，且校验器 0 违反。"""

    def _case(self):
        # 短单交期很紧、长单交期很松，一台机只能串行
        return [_task("HD-1", 600, date(2026, 11, 20)), _task("HD-2", 120, date(2026, 11, 9))]

    def test_tight_order_gets_priority(self):
        result = solver.schedule(self._case(), MACHINES, T0)
        late = [r for r in result["rows"] if r["order"] == "HD-2"][-1]
        self.assertLessEqual(late["end"], _due(date(2026, 11, 9)), "交期紧的单不能迟到")
        self.assertEqual(result["score"][0], 0)

    def test_runs_are_identical(self):
        a = solver.schedule(self._case(), MACHINES, T0)
        b = solver.schedule(self._case(), MACHINES, T0)
        key = lambda r: (r["machine"], r["order"], r["seq"], r["start"], r["end"])  # noqa: E731
        self.assertEqual([key(r) for r in a["rows"]], [key(r) for r in b["rows"]])
        self.assertEqual(a["rule"], b["rule"])

    def test_no_violations(self):
        result = solver.schedule(self._case(), MACHINES, T0)
        self.assertEqual(checker.check(self._case(), result["rows"], MACHINES, T0), [])

    def test_empty_tasks_do_not_crash(self):
        result = solver.schedule([], MACHINES, T0)
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["makespan"], T0)


class TestLowerBound(unittest.TestCase):
    """下界是物理极限：任何排法的完工都不可能早于它。"""

    def _case(self):
        return [_task("HD-1", 600, date(2026, 11, 20)), _task("HD-2", 120, date(2026, 11, 9))]

    def test_bound_is_not_later_than_actual(self):
        tasks = self._case()
        result = solver.schedule(tasks, MACHINES, T0)
        bounds = solver.lower_bounds(tasks, MACHINES, T0)
        for oid, bound in bounds.items():
            actual = max(r["end"] for r in result["rows"] if r["order"] == oid)
            self.assertLessEqual(bound, actual, f"{oid} 的下界晚于实际完工，说明下界算错了")

    def test_bound_ignores_queueing(self):
        """两台同工序机台时，下界按最快那台算，单台机排队影响不计入。"""
        two = {"M1": dict(MACHINES["M1"]), "M2": dict(MACHINES["M1"], code="M2", shifts=3)}
        tasks = [dict(_task("HD-9", 600, date(2026, 11, 20)), machines=["M1", "M2"])]
        bound = solver.lower_bounds(tasks, two, T0)["HD-9"]
        self.assertEqual(bound, datetime(2026, 11, 9, 18, 0), "3 班制机台 600 分钟应 18:00 完工")


if __name__ == "__main__":
    unittest.main()
