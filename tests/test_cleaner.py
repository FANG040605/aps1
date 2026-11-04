"""单号归一与日期解析的单测（N4 要求 ≥3 个）。

跑法：python -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cleaner import _dedupe, normalize_order_no, parse_date, parse_due  # noqa: E402
from core.pipeline import default_start  # noqa: E402


class TestOrderNo(unittest.TestCase):
    """台账里单号有三种写法，必须归一到同一个格式。"""

    def test_three_formats(self):
        self.assertEqual(normalize_order_no("HD-2026-101"), "HD-2026-101")
        self.assertEqual(normalize_order_no("HD2026102"), "HD-2026-102")
        self.assertEqual(normalize_order_no("104"), "HD-2026-104")

    def test_duplicate_gets_suffix(self):
        """HD-2026-106 出现两次（25 件 + 40 件追加），必须拆开否则必然"重排"。"""
        self.assertEqual(_dedupe(["HD-2026-106", "HD-2026-106"]),
                         ["HD-2026-106-A", "HD-2026-106-B"])

    def test_unique_untouched(self):
        self.assertEqual(_dedupe(["HD-2026-101", "HD-2026-102"]),
                         ["HD-2026-101", "HD-2026-102"])


class TestDate(unittest.TestCase):
    """接单日期有 2026/11/3、2026-11-03、2026.11.4 三种写法。"""

    def test_slash_dash_dot(self):
        self.assertEqual(parse_date("2026/11/3"), date(2026, 11, 3))
        self.assertEqual(parse_date("2026-11-03"), date(2026, 11, 3))
        self.assertEqual(parse_date("2026.11.4"), date(2026, 11, 4))

    def test_chinese_month_day(self):
        self.assertEqual(parse_date("11月12日"), date(2026, 11, 12))

    def test_bad_input(self):
        self.assertIsNone(parse_date("尽快"))
        self.assertIsNone(parse_date(""))


class TestDue(unittest.TestCase):
    """交期列是人话：能按口径推的推，推不出的标"需客户确认"。"""

    def test_plain_date_ok(self):
        due, text, need = parse_due("2026-11-10", date(2026, 11, 3))
        self.assertEqual(due, date(2026, 11, 10))
        self.assertFalse(need)

    def test_asap_adds_3_workdays_skip_sunday(self):
        # 接单 11-05（周四）+3 个工作日：11-06、11-07、跳过 11-08 周日、11-09
        due, _, need = parse_due("尽快", date(2026, 11, 5))
        self.assertEqual(due, date(2026, 11, 9))
        self.assertTrue(need, "折算出来的交期必须标需客户确认")

    def test_month_end(self):
        due, _, need = parse_due("月底前", date(2026, 11, 6))
        self.assertEqual(due, date(2026, 11, 30))
        self.assertTrue(need)

    def test_blank_returns_none(self):
        due, _, need = parse_due("", date(2026, 11, 4))
        self.assertIsNone(due)
        self.assertTrue(need)


class TestExcelDateType(unittest.TestCase):
    """回归：台账被 Excel 另存一次后，日期会变成 datetime，曾导致整条链路 TypeError。

    现场：客户只是在 Excel 里打开、存了一下，什么也没改，点排产回
    "排产时出错：TypeError"。原因是 datetime 是 date 的子类，
    混在一批订单里做 max() 比较就崩。
    """

    def test_datetime_becomes_date(self):
        got = parse_date(datetime(2026, 11, 3, 14, 30, 15))
        self.assertEqual(got, date(2026, 11, 3))
        self.assertNotIsInstance(got, datetime, "必须丢掉时间部分，统一成 date")

    def test_mixed_types_can_be_compared(self):
        """一单是文字日期、一单是 Excel 日期，两者必须能比较。"""
        mixed = [parse_date("2026/11/3"), parse_date(datetime(2026, 11, 8, 9, 0))]
        self.assertEqual(max(mixed), date(2026, 11, 8))

    def test_default_start_survives_datetime(self):
        """即使上游漏了 datetime 进来，起始日计算也不能崩。"""
        orders = [{"recv": date(2026, 11, 3)}, {"recv": datetime(2026, 11, 9, 8, 0)}]
        # 返回值是"当天 08:00"的 datetime，不是纯日期
        self.assertEqual(default_start(orders), datetime(2026, 11, 9, 8, 0))


if __name__ == "__main__":
    unittest.main()
