"""单号归一与日期解析的单测（N4 要求 ≥3 个）。

跑法：python -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cleaner import _dedupe, _parse_qty, normalize_order_no, parse_date, parse_due  # noqa: E402
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
        """即使上游漏了 datetime 进来，起始日计算也不能崩，且必须仍是 datetime。"""
        orders = [{"recv": date(2026, 11, 3)}, {"recv": datetime(2026, 11, 9, 8, 0)}]
        got = default_start(orders, now=datetime(2026, 11, 5, 15, 10))
        self.assertIsInstance(got, datetime)
        self.assertGreaterEqual(got, datetime(2026, 11, 5, 15, 10), "不能排到过去")

    def test_future_order_does_not_drag_whole_plan(self):
        """一张 11-09 才接的单，不能把整批计划拖到 11-09 才开工。"""
        orders = [{"recv": date(2026, 11, 3)}, {"recv": date(2026, 11, 9)}]
        got = default_start(orders, now=datetime(2026, 11, 5, 9, 30))
        self.assertEqual(got, datetime(2026, 11, 5, 9, 30))

    def test_all_orders_not_yet_received(self):
        """整批单都还没接到（全在未来），才按最早接单日开工。"""
        orders = [{"recv": date(2026, 11, 9)}, {"recv": date(2026, 11, 12)}]
        got = default_start(orders, now=datetime(2026, 11, 5, 9, 30))
        self.assertEqual(got, datetime(2026, 11, 9, 8, 0))

    def test_before_shift_starts_waits_for_morning(self):
        """凌晨 00:30 点排产，2 班制还没开工，起始推到当天 08:00。"""
        got = default_start([], now=datetime(2026, 11, 5, 0, 30))
        self.assertEqual(got, datetime(2026, 11, 5, 8, 0))

    def test_sunday_rolls_to_monday(self):
        """周日不排产，起始推到周一 08:00。"""
        got = default_start([], now=datetime(2026, 11, 8, 10, 0))
        self.assertEqual(got, datetime(2026, 11, 9, 8, 0))


if __name__ == "__main__":
    unittest.main()


class TestQty(unittest.TestCase):
    """数量列：带单位不能当空数量，负数不能当正数。"""

    def test_unit_suffix_is_tolerated(self):
        for text in ("35", "35 件", "35件", "35 PCS", "约 35 个"):
            self.assertEqual(_parse_qty(text), 35, f"「{text}」应该读成 35")

    def test_blank_and_zero_are_missing(self):
        for text in ("", None, "0", "待定"):
            self.assertIsNone(_parse_qty(text))

    def test_negative_is_rejected(self):
        self.assertIsNone(_parse_qty("-5"))


class TestYearNotHardcoded(unittest.TestCase):
    """年份不能写死 2026：跨年台账会把明年的单串到今年。"""

    def test_order_no_keeps_its_own_year(self):
        self.assertEqual(normalize_order_no("HD-2027-101"), "HD-2027-101")
        self.assertEqual(normalize_order_no("HD2027101"), "HD-2027-101")

    def test_short_form_uses_current_year(self):
        self.assertEqual(normalize_order_no("105"), f"HD-{date.today().year}-105")

    def test_chinese_due_accepts_hao(self):
        self.assertEqual(parse_date("11月12号"), date(2026, 11, 12))

    def test_excel_datetime_string_still_parses(self):
        """交期列被 Excel 存成真日期后，读出来是「2026-11-22 00:00:00」这种字符串。"""
        self.assertEqual(parse_date("2026-11-22 00:00:00"), date(2026, 11, 22))
        self.assertEqual(parse_due(datetime(2026, 11, 22, 0, 0), date(2026, 11, 3))[0],
                         date(2026, 11, 22))
