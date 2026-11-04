"""机台解析的单测：点名停机机台必须改派，不能硬排。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.instance import resolve_machines  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
