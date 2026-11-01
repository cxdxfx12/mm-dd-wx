# -*- coding: utf-8 -*-
"""倒挂帧里的多张表还原（2026-11-01 17:22 拦截跟单日报真实事故）。

钉钉 H5 卡片滚到顶之后，整条消息的 ``top`` 全被钳成可视区上沿 265，
``bottom`` 变成负数 —— 只有 ``left`` 和 ``bottom`` 还保有真实信息：
**同一行的格子 bottom 完全相同**（表头 -45、数据行 54/153/252…每行 +99）。

旧实现把倒挂节点 ``min/max`` 归一化成 ``[-45, 265]`` 这样的大区间，于是
每个格子都跟别的"重叠"，三张不相关的表被拼成一张错位表：行名全丢、
「拦截成效」那张还丢了 10/28 和 11/01 两列 —— 用户看到的就是一张
「只有日期和数字、看不懂哪行是什么」的图（2026-11-01 17:22 原话：
「这个图不对」）。

这张卡片正好是三条判据的活样本：
  · 一张卡片里三张表，列数 6 / 7 / 3；
  · 两张表**共享** left=250/548/1464 三列（所以按 left 分列必然串表）；
  · 另外三列还错位 24px（789 vs 765），靠列集合变化切表才分得开。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.sources.android_parse import (  # noqa: E402
    Node, _render_table, _table_rows)

VIEW_TOP = 265          # 倒挂时 top 被钳成的值


def _cell(left: int, bottom: int, text: str) -> Node:
    """一个倒挂状态的表格格：top 被钳成 265，bottom 是真实行序。"""
    return Node(text=text, bounds=(left, VIEW_TOP, 964, bottom))


def _row(lefts, bottom, values) -> list:
    return [_cell(l, bottom, v) for l, v in zip(lefts, values)]


# 表一：跟单概况（6 列，无「目标」列）
T1_LEFTS = [250, 548, 789, 1030, 1247, 1464]
T1 = [
    _row(T1_LEFTS, -636, ["指标", "10/28", "10/29", "10/30", "10/31", "11/01"]),
    _row(T1_LEFTS, -537, ["跟单任务数", "28", "16", "27", "21", "23"]),
    _row(T1_LEFTS, -438, ["任务完成数", "28", "16", "26", "20", "3"]),
    _row(T1_LEFTS, -339, ["完成率", "100.00%", "100.00%", "96.30%", "95.24%", "13.04%"]),
]

# 表二：拦截成效（7 列，多一列「目标」；前 5 列 left 与表一错位 24px）
T2_LEFTS = [250, 548, 765, 1006, 1223, 1464, 1681]
T2 = [
    _row(T2_LEFTS, -45, ["指标", "10/28", "10/29", "10/30", "10/31", "11/01", "目标"]),
    _row(T2_LEFTS, 54, ["拦截成功率", "98.15%", "100.00%", "98.31%", "100.00%", "92.59%", "99%"]),
    _row(T2_LEFTS, 153, ["及时转出率", "98.11%", "95.35%", "82.76%", "61.54%", "0.00%", "95%"]),
    _row(T2_LEFTS, 252, ["应拦截量", "54", "43", "59", "26", "27", "-"]),
    _row(T2_LEFTS, 351, ["拦截成功量", "53", "43", "58", "26", "25", "-"]),
]

# 正文：单格（left=212），夹在两张表之间
BODY = [_cell(212, -796, "跟单概况"), _cell(212, -205, "拦截成效")]


class TestInvertedMultiTable(unittest.TestCase):
    def setUp(self):
        self.nodes = [n for row in (T1 + T2) for n in row] + BODY
        self.grids = _table_rows(self.nodes)

    def test_splits_into_two_tables(self):
        """一张卡片两张表 → 必须分开，绝不能拼成一张。"""
        self.assertEqual(len(self.grids), 2)

    def test_each_table_has_own_column_count(self):
        self.assertEqual([len(g[0]) for g in self.grids], [6, 7])

    def test_row_names_survive(self):
        """核心事故点：行名（拦截成功率…）必须在第一列。"""
        t2 = self.grids[1]
        self.assertEqual([r[0][4] for r in t2],
                         ["指标", "拦截成功率", "及时转出率", "应拦截量", "拦截成功量"])

    def test_no_column_lost(self):
        """10/28 和 11/01 两列曾被整列丢掉。"""
        self.assertEqual([c[4] for c in self.grids[1][0]],
                         ["指标", "10/28", "10/29", "10/30", "10/31", "11/01", "目标"])

    def test_header_is_the_top_row(self):
        """bottom 最小的那行是表头（页面上最上面）。"""
        for g in self.grids:
            self.assertEqual(g[0][0][4], "指标")

    def test_cells_in_a_row_are_sorted_by_left(self):
        for g in self.grids:
            for r in g:
                lefts = [c[0] for c in r]
                self.assertEqual(lefts, sorted(lefts))

    def test_render_keeps_kv_pairs(self):
        text = _render_table(self.grids[1])
        self.assertIn("指标: 拦截成功率 ｜ 10/28: 98.15%", text)
        self.assertIn("11/01: 92.59% ｜ 目标: 99%", text)
        self.assertEqual(len(text.splitlines()), 4)      # 表头不渲染成数据行


class TestNormalTableUnchanged(unittest.TestCase):
    """正常坐标（不倒挂）的单表路径不能受影响。"""

    def test_normal_single_table(self):
        def cell(left, top, text):
            return Node(text=text, bounds=(left, top, 964, top + 60))

        nodes = []
        rows = [["姓名", "单号", "票数"],
                ["冯彬", "770355574975393", "1"],
                ["吴怀龙", "777458007041796", "1"]]
        for i, vals in enumerate(rows):
            for j, v in enumerate(vals):
                nodes.append(cell(250 + j * 300, 600 + i * 100, v))
        grids = _table_rows(nodes)
        self.assertEqual(len(grids), 1)
        self.assertEqual(len(grids[0]), 3)
        self.assertEqual([r[0][4] for r in grids[0]], ["姓名", "冯彬", "吴怀龙"])


if __name__ == "__main__":
    unittest.main()
