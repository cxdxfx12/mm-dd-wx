"""时间范围过滤（parse_clock / in_time_window）的单元测试。"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.sources.dingtalk_ocr import is_cross_day, parse_clock, in_time_window


class TestParseClock(unittest.TestCase):
    def test_today_clock(self):
        self.assertEqual(parse_clock("今天 09:15"), (9, 15))
        self.assertEqual(parse_clock("09:15"), (9, 15))

    def test_relative_times(self):
        base = datetime(2026, 10, 16, 10, 0)
        self.assertEqual(parse_clock("30分钟前", base), (9, 30))
        self.assertEqual(parse_clock("2小时前", base), (8, 0))
        self.assertEqual(parse_clock("刚刚", base), (10, 0))

    def test_unknown_returns_none(self):
        self.assertIsNone(parse_clock("昨天 09:15"))
        self.assertIsNone(parse_clock("星期三 09:15"))
        self.assertIsNone(parse_clock(""))
        self.assertIsNone(parse_clock("10月1日"))


class TestTimeWindow(unittest.TestCase):
    def test_normal_window(self):
        ok, _ = in_time_window("09:15", "08:00-18:00")
        self.assertTrue(ok)
        ok, _ = in_time_window("19:15", "08:00-18:00")
        self.assertFalse(ok)

    def test_overnight_window(self):
        ok, _ = in_time_window("23:30", "22:00-06:00")
        self.assertTrue(ok)
        ok, _ = in_time_window("05:00", "22:00-06:00")
        self.assertTrue(ok)
        ok, _ = in_time_window("12:00", "22:00-06:00")
        self.assertFalse(ok)

    def test_no_timestamp_uses_now(self):
        # 无时间戳 → 按当前时刻判定；所以窗口覆盖当前时刻时应通过
        now = datetime.now()
        wide = f"{(now.hour - 1) % 24:02d}:{now.minute:02d}-{(now.hour + 1) % 24:02d}:{now.minute:02d}"
        ok, _ = in_time_window("", wide, now)
        self.assertTrue(ok)
        # 互斥窗口一定不通过
        bad = f"{(now.hour + 2) % 24:02d}:00-{(now.hour + 3) % 24:02d}:00"
        ok, _ = in_time_window("", bad, now)
        self.assertFalse(ok)

    def test_bad_window_config_passes_through(self):
        ok, _ = in_time_window("09:15", "")
        self.assertTrue(ok)
        ok, _ = in_time_window("09:15", "abc")
        self.assertTrue(ok)

    def test_relative_time_resolved(self):
        base = datetime(2026, 10, 16, 10, 0)
        ok, _ = in_time_window("30分钟前", "08:00-18:00", base)
        self.assertTrue(ok)
        ok, _ = in_time_window("2小时前", "08:00-18:00", base)
        self.assertTrue(ok)
        ok, _ = in_time_window("9小时前", "08:00-18:00", base)   # 01:00 → 不在
        self.assertFalse(ok)

    def test_cross_day_still_blocked_first(self):
        # 窗口模式下跨天消息仍要先被 is_cross_day 拦下（在 source 里组合）
        self.assertTrue(is_cross_day("昨天 09:15"))


if __name__ == "__main__":
    unittest.main()
