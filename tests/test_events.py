"""设备事件测试：故障、保养、恢复。

核心是"临时状态不能污染长期基线"——修好后要把停机截断，
而不是改设备表状态列，否则那台机器永远排不上活。
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from core import events  # noqa: E402

T0 = datetime(2026, 11, 5, 16, 0)          # 周四下午


class EventCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "设备事件.json")

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, machine, kind, until=None, start=None, note=""):
        return events.add(machine, kind, until=until, note=note, start=start,
                          path=self.path, now=T0)

    def windows(self, start=T0):
        return events.windows(events.load(self.path), start)


class TestAdd(EventCase):

    def test_empty_when_no_file(self):
        self.assertEqual(events.load(self.path), [])

    def test_add_records_all_fields(self):
        ev = self.add("GRD-01", "故障", until=datetime(2026, 11, 9, 8), note="主轴异响")
        self.assertEqual(ev["machine"], "GRD-01")
        self.assertEqual(ev["kind"], "故障")
        self.assertEqual(ev["from"], "2026-11-05 16:00")
        self.assertEqual(ev["until"], "2026-11-09 08:00")
        self.assertEqual(len(events.load(self.path)), 1)

    def test_two_events_on_same_machine_both_kept(self):
        self.add("GRD-01", "故障", until=datetime(2026, 11, 7, 8))
        self.add("GRD-01", "保养", until=datetime(2026, 11, 9, 8))
        self.assertEqual(len(events.load(self.path)), 2)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            self.add("GRD-01", "坏了")

    def test_missing_machine_rejected(self):
        with self.assertRaises(ValueError):
            self.add("", "故障")


class TestWindows(EventCase):

    def test_future_fault_is_in_window(self):
        self.add("GRD-01", "故障", until=datetime(2026, 11, 9, 8), start=datetime(2026, 11, 6, 8))
        got = self.windows()
        self.assertIn("GRD-01", got)
        self.assertEqual(got["GRD-01"], [(datetime(2026, 11, 6, 8), datetime(2026, 11, 9, 8))])

    def test_no_until_means_whole_horizon(self):
        """归期未定 → 按整个计划期不可用算，宁可比实际保守。"""
        self.add("GRD-01", "故障")
        got = self.windows()["GRD-01"][0]
        self.assertEqual(got[0], T0)
        self.assertEqual(got[1], T0 + timedelta(days=config.PLAN_HORIZON_DAYS))

    def test_past_event_ignored(self):
        """上个月修好的故障不该影响今天的排产。"""
        self.add("GRD-01", "故障", until=datetime(2026, 10, 20, 8),
                 start=datetime(2026, 10, 1, 8))
        self.assertEqual(self.windows(), {})

    def test_overlapping_windows_merged(self):
        self.add("GRD-01", "故障", until=datetime(2026, 11, 8, 8), start=datetime(2026, 11, 6, 8))
        self.add("GRD-01", "保养", until=datetime(2026, 11, 10, 8), start=datetime(2026, 11, 7, 8))
        got = self.windows()["GRD-01"]
        self.assertEqual(len(got), 1, f"重叠区间应合并，实际 {got}")
        self.assertEqual(got[0], (datetime(2026, 11, 6, 8), datetime(2026, 11, 10, 8)))

    def test_inverted_window_dropped(self):
        """登记错的事件（终点早于起点）不能变成负长度的障碍物。"""
        items = [{"id": "x", "machine": "GRD-01", "kind": "故障",
                  "from": "2026-11-06 08:00", "until": "2026-11-05 17:00"}]
        self.assertEqual(events.windows(items, T0), {})

    def test_recover_event_is_not_a_window(self):
        self.add("GRD-01", "恢复")
        self.assertEqual(self.windows(), {})


class TestRecover(EventCase):

    def test_recover_truncates_open_fault(self):
        self.add("GRD-01", "故障", start=datetime(2026, 11, 5, 9))
        n = events.recover("GRD-01", path=self.path, now=datetime(2026, 11, 5, 18))
        self.assertEqual(n, 1)
        self.assertEqual(events.load(self.path)[0]["until"], "2026-11-05 18:00")

    def test_recover_before_start_drops_the_record(self):
        """提前登记的"明天故障"还没开始就修好了 → 整条作废，不留倒置区间。"""
        self.add("GRD-01", "故障", until=datetime(2026, 11, 9, 8), start=datetime(2026, 11, 6, 8))
        n = events.recover("GRD-01", path=self.path, now=datetime(2026, 11, 5, 17))
        self.assertEqual(n, 1)
        self.assertEqual(events.load(self.path), [])
        self.assertEqual(self.windows(), {})

    def test_recover_other_machine_untouched(self):
        self.add("GRD-01", "故障")
        n = events.recover("LAT-01", path=self.path, now=datetime(2026, 11, 5, 18))
        self.assertEqual(n, 0)
        self.assertIsNone(events.load(self.path)[0]["until"])


class TestStoppedWhole(EventCase):

    def test_long_fault_removes_machine(self):
        self.add("GRD-01", "故障")
        self.assertEqual(events.stopped_whole(events.load(self.path), T0), {"GRD-01"})

    def test_short_fault_keeps_machine(self):
        self.add("GRD-01", "故障", until=datetime(2026, 11, 9, 8))
        self.assertEqual(events.stopped_whole(events.load(self.path), T0), set())


class TestOngoing(EventCase):

    def test_current_fault_listed(self):
        self.add("GRD-01", "故障", start=datetime(2026, 11, 5, 9))
        self.assertIn("GRD-01", events.ongoing(events.load(self.path), datetime(2026, 11, 5, 18)))

    def test_future_fault_not_listed_as_current(self):
        self.add("GRD-01", "故障", start=datetime(2026, 11, 6, 8))
        self.assertEqual(events.ongoing(events.load(self.path), T0), {})


class TestRobustness(EventCase):

    def test_broken_json_does_not_crash(self):
        """文件被人手工改坏时，排产还得能跑，不能整批挂掉。"""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{这不是 json")
        self.assertEqual(events.load(self.path), [])

    def test_undo_removes_only_that_record(self):
        a = self.add("GRD-01", "故障")
        self.add("LAT-01", "保养")
        self.assertTrue(events.remove(a["id"], path=self.path))
        left = events.load(self.path)
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0]["machine"], "LAT-01")

    def test_parse_time_accepts_common_formats(self):
        self.assertEqual(events.parse_time("2026-11-09"), datetime(2026, 11, 9, 8, 0))
        self.assertEqual(events.parse_time("2026-11-09 14:30"), datetime(2026, 11, 9, 14, 30))
        self.assertEqual(events.parse_time("2026-11-09T14:30"), datetime(2026, 11, 9, 14, 30))
        self.assertIsNone(events.parse_time(""))
        self.assertIsNone(events.parse_time("下周二"))

    def test_json_file_is_human_readable(self):
        """流水要能给人看，所以落盘是缩进过的 JSON，不是一行压缩。"""
        self.add("GRD-01", "故障", note="主轴异响")
        with open(self.path, encoding="utf-8") as f:
            raw = f.read()
        self.assertIn("\n", raw)
        self.assertEqual(json.loads(raw)[0]["note"], "主轴异响")


if __name__ == "__main__":
    unittest.main()
